from __future__ import annotations

import argparse
import json
import os
import queue
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unicodedata
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Iterable


APP_DIR = Path(__file__).resolve().parent
DEFAULT_CONFIG_PATH = APP_DIR / "mkv_track_organizer.config.json"

# Windows fallbacks. These can also be overridden by CLI arguments or env vars.
MKVMERGE = Path(r"C:\Program Files\MKVToolNix\mkvmerge.exe")
MKVEXTRACT = Path(r"C:\Program Files\MKVToolNix\mkvextract.exe")
MKVPROPEDIT = Path(r"C:\Program Files\MKVToolNix\mkvpropedit.exe")
SUBTITLE_EDIT = Path(r"C:\Program Files\Subtitle Edit\SubtitleEdit.exe")
SECONV = Path(r"C:\Program Files\Subtitle Edit\seconv.exe")
TESSERACT = Path(r"C:\Program Files\Tesseract-OCR\tesseract.exe")

SORTED_DIR_NAME = "_sorted"
OCR_CACHE_DIR_NAME = "_ocr_cache"
TOOLS_DIR_NAME = "_tools"
LOCAL_TESSDATA_DIR_NAME = "tessdata"
REPORTS_DIR_NAME = "_reports"
TESSDATA_REPOS = {
    "best": "https://raw.githubusercontent.com/tesseract-ocr/tessdata_best/main",
    "fast": "https://raw.githubusercontent.com/tesseract-ocr/tessdata_fast/main",
}

# Conservative heuristics for PGS/SUP and bitmap subtitles.
BINARY_EMPTY_SUB_BYTES = 2 * 1024
BINARY_SMALL_SUB_BYTES = 1 * 1024 * 1024

# Separate heuristics for SRT/ASS because text subtitles are much smaller.
TEXT_EMPTY_SUB_BYTES = 200
TEXT_SMALL_SUB_BYTES = 10 * 1024

MAX_TEXT_SAMPLE_CHARS = 1_500_000
FORCED_SCORE_THRESHOLD = 60
COMMENTARY_SCORE_THRESHOLD = 70
SDH_SCORE_THRESHOLD = 55
FORCED_STRONG_SIZE_RATIO = 0.18
FORCED_WEAK_SIZE_RATIO = 0.35
FULL_SIZE_DUPLICATE_RATIO = 0.55
PGS_CLOCK = 90_000
PGS_MAX_EVENT_SECONDS = 20.0
PGS_STRONG_FORCED_EVENT_RATIO = 0.15
PGS_WEAK_FORCED_EVENT_RATIO = 0.30
PGS_OCR_TIMEOUT_SECONDS = 900
PGS_TIMELINE_MATCH_TOLERANCE_SECONDS = 1.25
CHINESE_SCRIPT_OCR_SAMPLE_EVENTS = 240
TEXT_SIMILARITY_HIGH = 0.72
TEXT_SIMILARITY_LOW = 0.28
TIMELINE_SIMILARITY_HIGH = 0.70
TIMELINE_SIMILARITY_LOW = 0.35
VARIANT_ANCHOR_MIN_SCORE = 8
VARIANT_SHORT_EVIDENCE_MAX_SCORE = 16
VARIANT_SHORT_EVIDENCE_MIN_CONFIDENCE = 0.85
VARIANT_METADATA_OVERRIDE_MIN_SCORE = 24
VARIANT_METADATA_OVERRIDE_MIN_CONFIDENCE = 0.70
OCR_VALIDATED_VARIANT_LANGUAGES = {"spa", "chi"}
METADATA_EDIT_MODES = {"off", "auto", "only"}
AUDIO_NAME_STYLES = {"auto", "format", "language-format", "keep"}
LANGUAGE_ORDER_STYLES = {"default", "regional"}
CHINESE_TRADITIONAL_REGIONAL_VARIANTS = {"zh-TW", "zh-HK"}
MATROSKA_INPUT_SUFFIXES = {".mkv", ".mka"}


class OrganizerError(Exception):
    """Friendly error for expected organizer failures."""


class OrganizerCancelled(Exception):
    """Raised when a batch run is cancelled by the caller."""


@dataclass
class SubtitleRoleScore:
    forced: int = 0
    commentary: int = 0
    sdh: int = 0
    forced_evidence: list[str] = field(default_factory=list)
    commentary_evidence: list[str] = field(default_factory=list)
    sdh_evidence: list[str] = field(default_factory=list)


@dataclass
class PgsEvent:
    start: float
    duration: float
    object_count: int


@dataclass
class SubtitleTextStats:
    word_count: int = 0
    commentary_score: int = 0
    sdh_score: int = 0
    commentary_evidence: list[str] = field(default_factory=list)
    sdh_evidence: list[str] = field(default_factory=list)
    tokens: set[str] = field(default_factory=set)


@dataclass
class PgsStats:
    total_bytes: int = 0
    segments: int = 0
    parse_errors: int = 0
    pcs_segments: int = 0
    wds_segments: int = 0
    pds_segments: int = 0
    ods_segments: int = 0
    end_segments: int = 0
    display_events: int = 0
    clear_events: int = 0
    object_refs: int = 0
    ods_payload_bytes: int = 0
    largest_object_area: int = 0
    max_object_width: int = 0
    max_object_height: int = 0
    video_width: int = 0
    video_height: int = 0
    first_pts: float | None = None
    last_pts: float | None = None
    estimated_display_seconds: float = 0.0
    max_event_seconds: float = 0.0
    events: list[PgsEvent] = field(default_factory=list)

    @property
    def span_seconds(self) -> float:
        if self.first_pts is None or self.last_pts is None:
            return 0.0
        return max(0.0, self.last_pts - self.first_pts)

    @property
    def event_density_per_hour(self) -> float:
        if self.span_seconds <= 0:
            return 0.0
        return self.display_events / (self.span_seconds / 3600)

    @property
    def display_ratio(self) -> float:
        if self.span_seconds <= 0:
            return 0.0
        return min(1.0, self.estimated_display_seconds / self.span_seconds)

    @property
    def avg_event_seconds(self) -> float:
        if not self.display_events:
            return 0.0
        return self.estimated_display_seconds / self.display_events


@dataclass
class SubtitleAnalysis:
    size_bytes: int | None = None
    size_class: str = "unknown"
    extraction_command: list[str] = field(default_factory=list)
    text_sample: str = ""
    text_stats: SubtitleTextStats | None = None
    pgs: PgsStats | None = None


@dataclass
class TrackInfo:
    id: int
    type: str
    codec: str
    codec_id: str
    language: str
    output_language: str
    language_name: str
    original_name: str
    order: int
    properties: dict[str, Any]
    channels: int | None = None
    analysis: SubtitleAnalysis | None = None
    suggested_name: str = ""
    role: str = "normal"
    role_reason: str = ""
    role_score: SubtitleRoleScore = field(default_factory=SubtitleRoleScore)
    default: bool = False
    forced: bool = False
    drop: bool = False
    delay_ms: int = 0
    pt_variant: dict[str, Any] | None = None
    duplicate_group: str = ""
    duplicate_member_ids: list[int] = field(default_factory=list)
    duplicate_of_id: int | None = None
    duplicate_reason: str = ""
    duplicate_source: str = ""
    duplicate_of_source: str = ""
    source_index: int = 0
    source_path: str = ""
    source_name: str = ""


@dataclass
class MetadataTrackEdit:
    track: TrackInfo
    properties: dict[str, str]


@dataclass
class MetadataEditPlan:
    can_edit: bool
    reason: str
    edits: list[MetadataTrackEdit] = field(default_factory=list)


@dataclass
class BatchRunContext:
    args: argparse.Namespace
    input_files: list[Path]
    source_root: Path | None
    forced_subtitle_ids: set[int]


@dataclass
class BatchRunResult:
    reports: list[dict[str, Any]]
    failures: int
    input_files: list[Path]
    source_root: Path | None
    cancelled: bool = False

    @property
    def return_code(self) -> int:
        if self.cancelled:
            return 130
        return 1 if self.failures else 0


@dataclass
class BatchRunEvent:
    kind: str
    message: str
    file: Path | None = None
    index: int | None = None
    total: int | None = None
    step: int | None = None
    steps: int | None = None


def track_selection_key(source_index: int, track_type: str, track_id: int) -> str:
    return f"{source_index}:{track_type}:{track_id}"


def track_selection_key_for_track(track: TrackInfo) -> str:
    return track_selection_key(track.source_index, track.type, track.id)


CONFIG_PATH_KEYS = {
    "path",
    "mkvmerge",
    "mkvextract",
    "mkvpropedit",
    "subtitle_edit",
    "seconv",
    "tesseract",
    "tessdata_dir",
    "ocr_cache_dir",
    "output_dir",
    "report_dir",
}

CONFIG_PATH_LIST_KEYS = {
    "variant_context_dirs",
}

CONFIG_STRING_LIST_KEYS = {
    "subtitle_language_ids",
    "regional_order",
}

CONFIG_BOOL_KEYS = {
    "recursive",
    "dry_run",
    "analyze_sub_sizes",
    "smart_sub_detection",
    "drop_empty_subs",
    "detect_duplicate_tracks",
    "merge_inputs",
    "detect_language_variants",
    "batch_language_variant_consensus",
    "prepare_pgs_ocr",
    "auto_pgs_ocr",
    "auto_commentary_ocr",
    "allow_subtitle_edit_legacy_ocr",
    "force_pgs_ocr",
    "auto_download_tessdata",
    "overwrite",
    "skip_existing",
    "report",
}

CONFIG_INT_KEYS = {"pgs_ocr_timeout_seconds"}

CONFIG_STRING_KEYS = {
    "forced_subtitle_ids",
    "audio_delays",
    "subtitle_delays",
    "pgs_ocr_command",
    "pgs_ocr_language",
    "tessdata_model",
    "output_suffix",
    "report_format",
    "explain_track",
    "metadata_edit_mode",
    "audio_name_style",
    "language_order_style",
}


LANGUAGE_ALIASES = {
    "en": "eng",
    "eng": "eng",
    "en-us": "en-US",
    "en-gb": "en-GB",
    "en-uk": "en-GB",
    "en-ca": "en-CA",
    "en-au": "en-AU",
    "pt": "por",
    "por": "por",
    "pt-pt": "pt-PT",
    "pt-br": "pt-BR",
    "br": "pt-BR",
    "fr": "fre",
    "fra": "fre",
    "fre": "fre",
    "fr-fr": "fr-FR",
    "fr-ca": "fr-CA",
    "fr-be": "fr-BE",
    "fr-ch": "fr-CH",
    "es": "spa",
    "spa": "spa",
    "esl": "spa",
    "es-es": "es-ES",
    "es-419": "es-419",
    "es-mx": "es-419",
    "es-ar": "es-419",
    "es-cl": "es-419",
    "es-co": "es-419",
    "es-us": "es-419",
    "de": "ger",
    "deu": "ger",
    "ger": "ger",
    "de-de": "de-DE",
    "de-at": "de-AT",
    "de-ch": "de-CH",
    "it": "ita",
    "ita": "ita",
    "it-it": "it-IT",
    "ja": "jpn",
    "jpn": "jpn",
    "ko": "kor",
    "kor": "kor",
    "zh": "chi",
    "chi": "chi",
    "zho": "chi",
    "cmn": "cmn",
    "yue": "yue",
    "zh-hant": "zh-Hant",
    "zh-hans": "zh-Hans",
    "zh-tw": "zh-TW",
    "zh-hk": "zh-HK",
    "zh-mo": "zh-Hant",
    "zh-cn": "zh-Hans",
    "zh-sg": "zh-Hans",
    "nl": "dut",
    "nld": "dut",
    "dut": "dut",
    "sv": "swe",
    "swe": "swe",
    "da": "dan",
    "dan": "dan",
    "no": "nor",
    "nor": "nor",
    "nb": "nob",
    "nb-no": "nob",
    "nob": "nob",
    "no-bok": "nob",
    "nn": "nno",
    "nn-no": "nno",
    "nno": "nno",
    "no-nyn": "nno",
    "fi": "fin",
    "fin": "fin",
    "is": "ice",
    "isl": "ice",
    "ice": "ice",
    "ru": "rus",
    "rus": "rus",
    "pl": "pol",
    "pol": "pol",
    "tr": "tur",
    "tur": "tur",
    "cs": "cze",
    "ces": "cze",
    "cze": "cze",
    "or": "ori",
    "ori": "ori",
    "hu": "hun",
    "hun": "hun",
    "ro": "rum",
    "ron": "rum",
    "rum": "rum",
    "el": "gre",
    "ell": "gre",
    "gre": "gre",
    "ar": "ara",
    "ara": "ara",
    "he": "heb",
    "heb": "heb",
    "th": "tha",
    "tha": "tha",
    "vi": "vie",
    "vie": "vie",
    "id": "ind",
    "ind": "ind",
    "ms": "may",
    "msa": "may",
    "may": "may",
    "zlm": "may",
    "hi": "hin",
    "hin": "hin",
    "uk": "ukr",
    "ukr": "ukr",
    "bg": "bul",
    "bul": "bul",
    "hr": "hrv",
    "hrv": "hrv",
    "mk": "mk",
    "mkd": "mk",
    "mac": "mk",
    "sk": "sk",
    "slk": "sk",
    "slo": "sk",
    "sl": "slv",
    "slv": "slv",
    "sr": "srp",
    "srp": "srp",
    "lv": "lav",
    "lav": "lav",
    "lt": "lit",
    "lit": "lit",
    "et": "est",
    "est": "est",
    "ca": "cat",
    "cat": "cat",
    "und": "und",
}


IETF_PRIMARY_BY_MKV_LANGUAGE = {
    "eng": "en",
    "por": "pt",
    "fre": "fr",
    "ger": "de",
    "spa": "es",
    "ita": "it",
    "jpn": "ja",
    "kor": "ko",
    "chi": "zh",
    "cmn": "cmn",
    "yue": "yue",
    "dut": "nl",
    "swe": "sv",
    "dan": "da",
    "nor": "no",
    "nob": "nb",
    "nno": "nn",
    "fin": "fi",
    "ice": "is",
    "rus": "ru",
    "pol": "pl",
    "tur": "tr",
    "cze": "cs",
    "hun": "hu",
    "rum": "ro",
    "gre": "el",
    "ara": "ar",
    "heb": "he",
    "tha": "th",
    "vie": "vi",
    "ind": "id",
    "may": "ms",
    "hin": "hi",
    "ukr": "uk",
    "bul": "bg",
    "hrv": "hr",
    "slv": "sl",
    "srp": "sr",
    "lav": "lv",
    "lit": "lt",
    "est": "et",
    "cat": "ca",
    "ori": "or",
    "und": "und",
}


LANGUAGE_NAMES = {
    "eng": "English",
    "en-US": "English (US)",
    "en-GB": "English (British)",
    "en-CA": "English (Canadian)",
    "en-AU": "English (Australian)",
    "por": "Portuguese",
    "pt-PT": "Portuguese (Iberian)",
    "pt-BR": "Portuguese (Brazilian)",
    "fre": "French",
    "fr-FR": "French (Parisian)",
    "fr-CA": "French (Canadian)",
    "fr-BE": "French (Belgian)",
    "fr-CH": "French (Swiss)",
    "spa": "Spanish",
    "es-ES": "Spanish (Castilian)",
    "es-419": "Spanish (Latin American)",
    "ger": "German",
    "de-DE": "German",
    "de-AT": "German (Austrian)",
    "de-CH": "German (Swiss)",
    "ita": "Italian",
    "it-IT": "Italian",
    "jpn": "Japanese",
    "kor": "Korean",
    "chi": "Chinese",
    "cmn": "Mandarin Chinese",
    "yue": "Cantonese",
    "zh-Hant": "Chinese (Traditional)",
    "zh-Hans": "Chinese (Simplified)",
    "zh-TW": "Chinese (Taiwan)",
    "zh-HK": "Chinese (Hong Kong)",
    "dut": "Dutch",
    "swe": "Swedish",
    "dan": "Danish",
    "nor": "Norwegian",
    "nob": "Norwegian Bokmål",
    "nno": "Norwegian Nynorsk",
    "fin": "Finnish",
    "ice": "Icelandic",
    "rus": "Russian",
    "pol": "Polish",
    "tur": "Turkish",
    "cze": "Czech",
    "hun": "Hungarian",
    "rum": "Romanian",
    "gre": "Greek",
    "ara": "Arabic",
    "heb": "Hebrew",
    "tha": "Thai",
    "vie": "Vietnamese",
    "ind": "Indonesian",
    "may": "Malay",
    "hin": "Hindi",
    "ukr": "Ukrainian",
    "bul": "Bulgarian",
    "hrv": "Croatian",
    "mk": "Macedonian",
    "sk": "Slovak",
    "slv": "Slovenian",
    "srp": "Serbian",
    "lav": "Latvian",
    "lit": "Lithuanian",
    "est": "Estonian",
    "cat": "Catalan",
    "ori": "Odia",
    "und": "Undetermined",
}


LANGUAGE_SUBTAG_NAMES = {
    "US": "US",
    "GB": "British",
    "CA": "Canadian",
    "AU": "Australian",
    "PT": "Iberian",
    "BR": "Brazilian",
    "ES": "Castilian",
    "419": "Latin American",
    "FR": "Parisian",
    "BE": "Belgian",
    "CH": "Swiss",
    "AT": "Austrian",
    "DE": "German",
    "Hant": "Traditional",
    "Hans": "Simplified",
    "TW": "Taiwan",
    "HK": "Hong Kong",
}


TESSERACT_LANGUAGE_ALIASES = {
    "eng": "eng",
    "en-US": "eng",
    "en-GB": "eng",
    "en-CA": "eng",
    "en-AU": "eng",
    "por": "por",
    "pt-PT": "por",
    "pt-BR": "por",
    "fre": "fra",
    "fr-FR": "fra",
    "fr-CA": "fra",
    "fr-BE": "fra",
    "fr-CH": "fra",
    "ger": "deu",
    "de-DE": "deu",
    "de-AT": "deu",
    "de-CH": "deu",
    "spa": "spa",
    "es-ES": "spa",
    "es-419": "spa",
    "dut": "nld",
    "swe": "swe",
    "dan": "dan",
    "nor": "nor",
    "nob": "nor",
    "nno": "nor",
    "fin": "fin",
    "ita": "ita",
    "jpn": "jpn",
    "kor": "kor",
    "chi": "chi_sim",
    "zh-Hant": "chi_tra",
    "zh-Hans": "chi_sim",
    "zh-TW": "chi_tra",
    "zh-HK": "chi_tra",
    "rus": "rus",
    "pol": "pol",
    "tur": "tur",
    "cze": "ces",
    "hun": "hun",
    "rum": "ron",
    "gre": "ell",
    "ara": "ara",
    "heb": "heb",
    "tha": "tha",
    "vie": "vie",
    "ind": "ind",
    "hin": "hin",
    "ukr": "ukr",
    "bul": "bul",
    "hrv": "hrv",
    "mk": "mkd",
    "sk": "slk",
    "slv": "slv",
    "srp": "srp",
    "lav": "lav",
    "lit": "lit",
    "est": "est",
    "cat": "cat",
}

CHINESE_OCR_SCRIPT_LANGUAGES = ("chi_sim", "chi_tra")
CHINESE_OCR_LANGUAGE_VARIANTS = {
    "chi_sim": "zh-Hans",
    "chi_tra": "zh-Hant",
}


LANGUAGE_VARIANT_BASES = {
    "en-US": "eng",
    "en-GB": "eng",
    "en-CA": "eng",
    "en-AU": "eng",
    "pt-PT": "por",
    "pt-BR": "por",
    "fr-FR": "fre",
    "fr-CA": "fre",
    "fr-BE": "fre",
    "fr-CH": "fre",
    "es-ES": "spa",
    "es-419": "spa",
    "de-DE": "ger",
    "de-AT": "ger",
    "de-CH": "ger",
    "it-IT": "ita",
    "zh-Hant": "chi",
    "zh-Hans": "chi",
    "zh-TW": "chi",
    "zh-HK": "chi",
}

LANGUAGE_VARIANT_BASE_ALIASES = {
    "cmn": "chi",
}


REGIONAL_LANGUAGE_GROUPS = (
    (
        "europe",
        (
            "eng", "en-GB", "por", "pt-PT", "spa", "es-ES", "cat", "fre", "fr-FR", "fr-BE", "fr-CH",
            "ger", "de-DE", "de-AT", "de-CH", "ita", "it-IT", "dut", "swe", "dan", "nor", "nob",
            "nno", "fin", "ice", "pol", "cze", "sk", "hun", "rum", "gre", "rus", "ukr", "bul",
            "hrv", "mk", "slv", "srp", "lav", "lit", "est", "tur",
        ),
    ),
    (
        "americas",
        (
            "en-US", "en-CA", "pt-BR", "es-419", "fr-CA",
        ),
    ),
    (
        "asia",
        (
            "chi", "cmn", "yue", "zh-Hant", "zh-Hans", "zh-TW", "zh-HK", "jpn", "kor",
            "hin", "ori", "tha", "vie", "ind", "may",
        ),
    ),
    (
        "oceania",
        (
            "en-AU",
        ),
    ),
    (
        "middle-east-africa",
        (
            "ara", "heb",
        ),
    ),
)

DEFAULT_REGIONAL_ORDER = tuple(region_name for region_name, _language_codes in REGIONAL_LANGUAGE_GROUPS)
REGIONAL_LANGUAGE_GROUP_NAMES = set(DEFAULT_REGIONAL_ORDER)
REGIONAL_LANGUAGE_GROUP_ALIASES = {
    "middle_east_africa": "middle-east-africa",
    "middle east africa": "middle-east-africa",
    "middle-east": "middle-east-africa",
    "africa": "middle-east-africa",
    "mea": "middle-east-africa",
}

REGIONAL_LANGUAGE_ORDER = {
    language_code: (region_index, language_index)
    for region_index, (_region_name, language_codes) in enumerate(REGIONAL_LANGUAGE_GROUPS)
    for language_index, language_code in enumerate(language_codes)
}


LANGUAGE_VARIANT_HINTS = {
    "por": (
        ("pt-PT", ("pt-pt", "portugal", "iberian", "european", "european portuguese", "portugues europeu")),
        ("pt-BR", ("pt-br", "brazil", "brazilian", "brasil", "brasileiro")),
    ),
    "spa": (
        ("es-ES", ("es-es", "spain", "castilian", "castellano", "european", "european spanish", "espanha")),
        ("es-419", ("es-419", "latin american", "latam", "latin america", "latinoamericano", "america latina")),
    ),
    "fre": (
        ("fr-FR", ("fr-fr", "france", "parisian", "parisien", "parisienne", "european", "european french")),
        ("fr-CA", ("fr-ca", "canadian", "canadien", "canadienne", "quebec", "quebecois", "québécois")),
        ("fr-BE", ("fr-be", "belgian", "belgique", "belge")),
        ("fr-CH", ("fr-ch", "swiss", "suisse")),
    ),
    "chi": (
        ("zh-TW", ("zh-tw", "taiwan")),
        ("zh-HK", ("zh-hk", "hong kong", "hongkong")),
        ("zh-Hant", ("zh-hant", "traditional", "traditionnel", "tradicional", "繁體", "正體")),
        ("zh-Hans", ("zh-hans", "simplified", "simplifie", "simplified chinese", "简体", "簡体")),
    ),
    "eng": (
        ("en-US", ("en-us", "american", "us english", "usa")),
        ("en-GB", ("en-gb", "en-uk", "british", "uk english")),
        ("en-CA", ("en-ca", "canadian")),
        ("en-AU", ("en-au", "australian")),
    ),
    "ger": (
        ("de-DE", ("de-de", "germany", "deutschland")),
        ("de-AT", ("de-at", "austrian", "osterreich", "österreich")),
        ("de-CH", ("de-ch", "swiss", "schweiz")),
    ),
}


LANGUAGE_NAME_HINTS = (
    ("pt-PT", ("pt-pt", "portuguese european", "european portuguese", "portugues europeu")),
    ("pt-BR", ("pt-br", "brazilian portuguese", "portuguese brazilian", "portugues brasileiro")),
    ("es-ES", ("es-es", "castilian spanish", "spanish castilian", "european spanish")),
    ("es-419", ("es-419", "latin american spanish", "spanish latin american", "latam spanish")),
    ("fr-CA", ("fr-ca", "french canadian", "canadian french", "francais canadien", "français canadien")),
    ("fr-FR", ("fr-fr", "french european", "european french", "parisian french")),
    ("zh-TW", ("zh-tw", "chinese taiwan", "taiwan chinese", "taiwanese chinese", "taiwan")),
    ("zh-HK", ("zh-hk", "chinese hong kong", "hong kong chinese", "hongkong chinese")),
    ("zh-Hant", ("zh-hant", "chinese traditional", "traditional chinese")),
    ("zh-Hans", ("zh-hans", "chinese simplified", "simplified chinese")),
    ("cmn", ("mandarin chinese", "mandarin", "taiwanese mandarin")),
    ("yue", ("cantonese", "yue chinese")),
    ("may", ("malay", "bahasa malaysia", "bahasa melayu")),
)


DEFAULT_LANGUAGE_VARIANTS = {
    "fre": "fr-FR",
    "chi": "zh-Hant",
}


ORDERED_PGS_LANGUAGE_VARIANTS = {
    "por": ("pt-BR", "pt-PT"),
    "fre": ("fr-FR", "fr-CA"),
}


SDH_CUE_WORDS_BY_LANGUAGE = {
    "eng": ("English", (
        "music", "song", "singing", "playing", "stereo", "laugh", "laughs", "laughter", "applause",
        "clapping", "cheering", "sigh", "sighs", "gasp", "gasps", "groan", "groans", "cough",
        "coughs", "sneeze", "sneezing", "sob", "sobs", "crying", "wail", "wailing", "scream",
        "screaming", "whisper", "whispering", "door", "knock", "knocking", "phone", "ringing",
        "gunshot", "gunfire", "thunder", "alarm", "siren", "explosion", "footsteps", "breathing",
        "beeping", "static",
    )),
    "por": ("Portuguese", (
        "musica", "cancao", "cantando", "a tocar", "tocando", "risos", "rindo", "gargalhadas",
        "aplausos", "palmas", "suspiro", "suspira", "ofega", "geme", "tosse", "espirra",
        "espirro", "chora", "chorando", "grita", "grito", "sussurra", "porta", "batida",
        "passos", "telefone", "telemovel", "campainha", "trovao", "trovoes", "alarme", "sirene",
        "explosao", "tiro",
    )),
    "fre": ("French", (
        "musique", "chanson", "chant", "chante", "rires", "rit", "applaudissements", "soupir",
        "tousse", "eternue", "pleure", "cri", "crie", "murmure", "chuchote", "porte",
        "telephone", "sonnerie", "tonnerre", "alarme", "sirene", "explosion", "bruits de pas",
        "coup de feu", "respiration",
    )),
    "spa": ("Spanish", (
        "musica", "cancion", "cantando", "risas", "rie", "aplausos", "suspiro", "suspira",
        "jadea", "gime", "tose", "estornuda", "llora", "grita", "grito", "susurra", "puerta",
        "telefono", "timbre", "trueno", "alarma", "sirena", "explosion", "pasos", "disparo",
    )),
    "ger": ("German", (
        "musik", "lied", "gesang", "singt", "lachen", "gelachter", "applaus", "seufzt", "keucht",
        "stohnt", "hustet", "niest", "weint", "schreit", "flustert", "tur", "telefon",
        "klingeln", "donner", "alarm", "sirene", "explosion", "schritte", "schuss",
    )),
    "ita": ("Italian", (
        "musica", "canzone", "canto", "canta", "risate", "ride", "applausi", "sospira",
        "ansima", "geme", "tossisce", "starnutisce", "piange", "urla", "grida", "sussurra",
        "porta", "telefono", "squilla", "tuono", "allarme", "sirena", "esplosione", "passi",
        "sparo",
    )),
    "dut": ("Dutch", (
        "muziek", "lied", "zingen", "zingt", "gelach", "lacht", "applaus", "zucht", "hijgt",
        "kreunt", "hoest", "niest", "huilt", "schreeuwt", "fluistert", "deur", "telefoon",
        "rinkelt", "donder", "alarm", "sirene", "explosie", "voetstappen", "schot",
    )),
    "jpn": ("Japanese", (
        "音楽", "歌", "歌う", "笑い", "拍手", "ため息", "咳", "くしゃみ", "泣", "叫び",
        "悲鳴", "ささやき", "ドア", "電話", "雷", "警報", "サイレン", "爆発", "足音",
        "銃声",
    )),
    "kor": ("Korean", (
        "음악", "노래", "웃음", "박수", "한숨", "기침", "재채기", "울음", "비명",
        "속삭임", "문", "전화", "천둥", "경보", "사이렌", "폭발", "발소리", "총성",
    )),
    "chi": ("Chinese", (
        "音乐", "音樂", "歌声", "歌聲", "唱歌", "笑声", "笑聲", "掌声", "掌聲", "叹气",
        "嘆氣", "咳嗽", "喷嚏", "噴嚏", "哭声", "哭聲", "尖叫", "低语", "低語", "门",
        "門", "电话", "電話", "雷声", "雷聲", "警报", "警報", "爆炸", "脚步", "腳步",
        "枪声", "槍聲",
    )),
    "swe": ("Swedish", (
        "musik", "sang", "sjunger", "skratt", "skrattar", "applader", "suckar", "flamtar",
        "hostar", "nyser", "grater", "skrik", "skriker", "viskar", "dorr", "telefon",
        "ringer", "aska", "alarm", "siren", "explosion", "fotsteg", "skott",
    )),
    "dan": ("Danish", (
        "musik", "sang", "synger", "latter", "griner", "bifald", "sukker", "hoster", "nyser",
        "græder", "graeder", "skrig", "skriger", "hvisker", "dor", "dør", "telefon", "ringer",
        "torden", "alarm", "sirene", "eksplosion", "fodtrin", "skud",
    )),
    "nor": ("Norwegian", (
        "musikk", "sang", "synger", "latter", "ler", "applaus", "sukker", "hoster", "nyser",
        "grater", "skrik", "skriker", "hvisker", "dor", "dør", "telefon", "ringer", "torden",
        "alarm", "sirene", "eksplosjon", "fottrinn", "skudd",
    )),
    "fin": ("Finnish", (
        "musiikki", "laulu", "laulaa", "naurua", "nauraa", "aplodit", "huokaa", "yskii",
        "aivastaa", "itkee", "huutaa", "huuto", "kuiskaa", "ovi", "puhelin", "soi",
        "ukkonen", "halytys", "hälytys", "sireeni", "rajahdys", "askeleet", "laukaus",
    )),
    "rus": ("Russian", (
        "музыка", "песня", "поет", "смех", "смеется", "аплодисменты", "вздох", "кашель",
        "чихает", "плачет", "крик", "кричит", "шепчет", "дверь", "телефон", "звонок", "гром",
        "тревога", "сирена", "взрыв", "шаги", "выстрел",
    )),
    "pol": ("Polish", (
        "muzyka", "piosenka", "spiew", "spiewa", "smiech", "smieje", "oklaski", "wzdycha",
        "kaszle", "kicha", "placze", "krzyk", "krzyczy", "szepcze", "drzwi", "telefon",
        "dzwonek", "grzmot", "alarm", "syrena", "wybuch", "kroki", "strzal",
    )),
    "tur": ("Turkish", (
        "muzik", "sarki", "soyluyor", "gulme", "guler", "alkis", "ic ceker", "oksurur",
        "hapsirir", "agliyor", "ciglik", "bagirir", "fisildar", "kapi", "telefon", "zil",
        "gok gurultusu", "alarm", "siren", "patlama", "ayak sesleri", "silah sesi",
    )),
    "cze": ("Czech", (
        "hudba", "pisen", "zpiva", "smich", "smeje", "potlesk", "vzdycha", "kasle", "kycha",
        "place", "krik", "krici", "sepota", "dvere", "telefon", "zvoni", "hrom", "poplach",
        "sirena", "vybuch", "kroky", "vystrel",
    )),
    "hun": ("Hungarian", (
        "zene", "dal", "enekel", "nevetes", "nevet", "taps", "sohajt", "kohog", "tusszent",
        "sir", "sikit", "kialt", "suttog", "ajto", "telefon", "csorog", "mennydorges",
        "riasztas", "szirena", "robbanas", "lepesek", "loves",
    )),
    "rum": ("Romanian", (
        "muzica", "cantec", "canta", "rasete", "rade", "aplauze", "ofteaza", "tuseste",
        "stranuta", "plange", "tipa", "striga", "sopteste", "usa", "telefon", "suna", "tunet",
        "alarma", "sirena", "explozie", "pasi", "foc de arma",
    )),
    "gre": ("Greek", (
        "μουσικη", "τραγουδι", "τραγουδα", "γελια", "γελα", "χειροκροτηματα", "αναστεναζει",
        "βηχει", "φτερνιζεται", "κλαιει", "ουρλιαζει", "φωναζει", "ψιθυριζει", "πορτα",
        "τηλεφωνο", "βροντη", "συναγερμος", "σειρηνα", "εκρηξη", "βηματα", "πυροβολισμος",
    )),
    "ara": ("Arabic", (
        "موسيقى", "أغنية", "اغنية", "غناء", "ضحك", "يضحك", "تصفيق", "تنهيدة", "يسعل",
        "عطس", "يبكي", "صراخ", "يصرخ", "همس", "يهمس", "باب", "هاتف", "رعد", "إنذار",
        "انذار", "صفارة", "انفجار", "خطوات", "طلقة",
    )),
    "heb": ("Hebrew", (
        "מוזיקה", "שיר", "שר", "צחוק", "צוחק", "מחיאות כפיים", "נאנח", "משתעל", "מתעטש",
        "בוכה", "צעקה", "צועק", "לוחש", "דלת", "טלפון", "רעם", "אזעקה", "סירנה", "פיצוץ",
        "צעדים", "ירייה",
    )),
    "tha": ("Thai", (
        "เพลง", "ดนตรี", "ร้องเพลง", "หัวเราะ", "เสียงหัวเราะ", "ปรบมือ", "ถอนหายใจ",
        "ไอ", "จาม", "ร้องไห้", "กรีดร้อง", "กระซิบ", "ประตู", "โทรศัพท์", "ฟ้าร้อง",
        "สัญญาณเตือน", "ไซเรน", "ระเบิด", "ฝีเท้า", "ปืน",
    )),
    "vie": ("Vietnamese", (
        "nhac", "bai hat", "hat", "cuoi", "vo tay", "tho dai", "ho", "hat hoi", "khoc",
        "la het", "thi tham", "cua", "dien thoai", "chuong", "sam", "bao dong", "coi bao",
        "no", "buoc chan", "tieng sung",
    )),
    "ind": ("Indonesian", (
        "musik", "lagu", "bernyanyi", "tertawa", "tawa", "tepuk tangan", "mendesah", "batuk",
        "bersin", "menangis", "menjerit", "berteriak", "berbisik", "pintu", "telepon",
        "dering", "guntur", "alarm", "sirene", "ledakan", "langkah kaki", "tembakan",
    )),
    "hin": ("Hindi", (
        "संगीत", "गाना", "गीत", "हँसी", "हंसी", "हँसता", "तालियां", "आह", "खांसी",
        "छींक", "रोता", "चिल्लाहट", "चिल्लाता", "फुसफुसाहट", "दरवाजा", "फोन", "गरज",
        "अलार्म", "सायरन", "विस्फोट", "कदम", "गोली",
    )),
    "ukr": ("Ukrainian", (
        "музика", "пісня", "співає", "сміх", "сміється", "оплески", "зітхає", "кашляє",
        "чхає", "плаче", "крик", "кричить", "шепоче", "двері", "телефон", "дзвінок", "грім",
        "тривога", "сирена", "вибух", "кроки", "постріл",
    )),
    "bul": ("Bulgarian", (
        "музика", "песен", "пее", "смях", "смее", "аплодисменти", "въздиша", "кашля",
        "киха", "плаче", "писък", "крещи", "шепне", "врата", "телефон", "звъни", "гръм",
        "аларма", "сирена", "експлозия", "стъпки", "изстрел",
    )),
    "hrv": ("Croatian", (
        "glazba", "pjesma", "pjeva", "smijeh", "smije se", "pljesak", "uzdah", "kaslje",
        "kise", "place", "vristi", "vice", "sapce", "vrata", "telefon", "zvoni", "grmljavina",
        "alarm", "sirena", "eksplozija", "koraci", "pucanj",
    )),
    "mk": ("Macedonian", (
        "музика", "песна", "пее", "смеа", "смеање", "аплауз", "воздишка", "кашла",
        "кива", "плаче", "вреска", "шепоти", "врата", "телефон", "ѕвони", "грмотевица",
        "аларм", "сирена", "експлозија", "чекори", "истрел",
    )),
    "sk": ("Slovak", (
        "hudba", "piesen", "spieva", "smiech", "smeje", "potlesk", "vzdych", "kasle",
        "kycha", "place", "krik", "krici", "sepka", "dvere", "telefon", "zvoni",
        "hrom", "poplach", "sirena", "vybuch", "kroky", "vystrel",
    )),
    "slv": ("Slovenian", (
        "glasba", "pesem", "poje", "smeh", "smeje", "aplavz", "vzdih", "kaslja", "kiha",
        "joka", "krik", "vpije", "sepeta", "vrata", "telefon", "zvoni", "grom", "alarm",
        "sirena", "eksplozija", "koraki", "strel",
    )),
    "srp": ("Serbian", (
        "muzika", "pesma", "peva", "smeh", "smeje", "aplauz", "uzdah", "kaslje", "kija",
        "place", "vristi", "vice", "sapuce", "vrata", "telefon", "zvoni", "grmljavina",
        "alarm", "sirena", "eksplozija", "koraci", "pucanj",
    )),
    "lav": ("Latvian", (
        "muzika", "dziesma", "dzied", "smiekli", "smejas", "aplausi", "noputas", "klepo",
        "skauda", "raud", "kliedz", "cukst", "durvis", "telefons", "zvana", "perkons",
        "trauksme", "sirena", "spradziens", "soli", "saviens",
    )),
    "lit": ("Lithuanian", (
        "muzika", "daina", "dainuoja", "juokas", "juokiasi", "plojimai", "atsidusta",
        "kosti", "ciaudi", "verkia", "rekia", "saukia", "snabzda", "durys", "telefonas",
        "skamba", "griaustinis", "aliarmas", "sirena", "sprogimas", "zingsniai", "suvis",
    )),
    "est": ("Estonian", (
        "muusika", "laul", "laulab", "naer", "naerab", "aplaus", "ohkab", "kohib",
        "aevastab", "nutab", "karjub", "huuab", "sosistab", "uks", "telefon", "heliseb",
        "aike", "alarm", "sireen", "plahvatus", "sammud", "lask",
    )),
    "cat": ("Catalan", (
        "musica", "canco", "cantant", "rialles", "riu", "aplaudiments", "sospir", "tus",
        "esternuda", "plora", "crida", "xiscla", "xiuxiueja", "porta", "telefon", "timbre",
        "tro", "alarma", "sirena", "explosio", "passos", "tret",
    )),
}

_SDH_TEXT_PATTERNS: list[tuple[str, int, str]] | None = None


TEXT_STOPWORDS = {
    "a", "as", "ao", "aos", "e", "o", "os", "de", "da", "das", "do", "dos", "um", "uma", "uns", "umas",
    "que", "para", "por", "com", "sem", "em", "no", "na", "nos", "nas", "se", "me", "te", "lhe", "eles",
    "elas", "este", "esta", "isto", "isso", "aquele", "aquela", "the", "and", "or", "of", "to", "in", "on",
    "for", "with", "without", "this", "that", "these", "those", "you", "your", "are", "was", "were", "not",
    "but", "have", "has", "had", "from", "we", "our", "they", "their", "il", "elle", "les", "des", "une",
    "est", "pas", "dans", "sur", "pour", "avec", "sans", "nous", "vous", "ist", "der", "die", "das", "und",
}


AUDIO_CODEC_PRIORITY = {
    "TrueHD Atmos": 130,
    "DTS:X": 120,
    "TrueHD": 110,
    "DTS-HD MA": 100,
    "PCM": 90,
    "DTS-HD HRA": 80,
    "FLAC": 70,
    "E-AC-3 Atmos": 60,
    "E-AC-3": 50,
    "DTS": 40,
    "AC-3": 30,
    "AAC": 20,
    "MP3": 10,
}


def remove_accents(text: str) -> str:
    return "".join(
        char for char in unicodedata.normalize("NFD", text)
        if unicodedata.category(char) != "Mn"
    )


def clean_subtitle_text(raw_text: str) -> str:
    lines = []

    for line in raw_text.splitlines():
        line = line.strip()

        if not line:
            continue

        # Ignore SRT numbers and timecodes.
        if line.isdigit() or "-->" in line:
            continue

        line = re.sub(r"<[^>]+>", " ", line)
        line = re.sub(r"\{.*?\}", " ", line)
        lines.append(line)

    return " ".join(lines)


def count_pattern(text: str, pattern: str, max_count: int = 10) -> int:
    matches = re.findall(pattern, text, flags=re.IGNORECASE)
    return min(len(matches), max_count)


def regex_alternatives(words: tuple[str, ...]) -> str:
    normalized_words = sorted(
        {
            remove_accents(word.strip().lower())
            for word in words
            if word.strip()
        },
        key=len,
        reverse=True,
    )
    alternatives = "|".join(re.escape(word).replace(r"\ ", r"\s+") for word in normalized_words)
    return rf"(?<![\w])(?:{alternatives})(?![\w])"


def sdh_text_patterns() -> list[tuple[str, int, str]]:
    global _SDH_TEXT_PATTERNS

    if _SDH_TEXT_PATTERNS is not None:
        return _SDH_TEXT_PATTERNS

    all_words = tuple(
        word
        for _language_name, words in SDH_CUE_WORDS_BY_LANGUAGE.values()
        for word in words
    )
    alternatives = regex_alternatives(all_words)
    profile_count = len(SDH_CUE_WORDS_BY_LANGUAGE)
    patterns: list[tuple[str, int, str]] = [
        (rf"\[[^\]]*(?:{alternatives})[^\]]*\]", 30, f"multilingual SDH in square brackets ({profile_count} profiles)"),
        (rf"\([^\)]*(?:{alternatives})[^\)]*\)", 20, f"multilingual SDH in parentheses ({profile_count} profiles)"),
    ]

    patterns.extend(
        [
            (r"♪|♫", 20, "music notes"),
            (r"[\[\(]\s*[A-Z0-9][A-Z0-9 .'\-]{2,45}\s*[\]\)]", 8, "uppercase SDH cue"),
        ]
    )
    _SDH_TEXT_PATTERNS = patterns
    return patterns


def classify_portuguese_variant(raw_subtitle_text: str) -> dict[str, Any]:
    cleaned = clean_subtitle_text(raw_subtitle_text)
    text = remove_accents(cleaned.lower())

    pt_pt_features = [
        (r"\b(estou|estas|esta|estamos|estao|estava|estavam)\s+a\s+[a-z]+(ar|er|ir)\b", 10, "pt-PT structure: estar a fazer"),
        (r"\b\w+-(me|te|lhe|nos|vos|lhes|lo|la|los|las)\b", 5, "hyphenated pronouns"),
        (r"\btelemovel\b", 8, "telemóvel"),
        (r"\bautocarro\b", 8, "autocarro"),
        (r"\bcomboio\b", 8, "comboio"),
        (r"\bcasa de banho\b", 8, "casa de banho"),
        (r"\bpequeno[- ]almoco\b", 8, "pequeno-almoço"),
        (r"\bsumo\b|\bfrigorifico\b|\brebucado\b|\bpastilha elastica\b", 8, "pt-PT food/household terms"),
        (r"\bcarta de conducao\b|\bbilhete de identidade\b", 9, "pt-PT official documents"),
        (r"\besquadra\b", 8, "esquadra"),
        (r"\becra\b", 7, "ecrã"),
        (r"\bficheiro\b", 7, "ficheiro"),
        (r"\bequipa\b", 7, "equipa"),
        (r"\bmiudo\b|\bmiuda\b|\bmiudos\b|\bmiudas\b", 6, "miúdo/miúda"),
        (r"\brapariga\b|\braparigas\b", 6, "rapariga"),
        (r"\bfixe\b|\bmalta\b", 6, "pt-PT colloquial vocabulary"),
        (r"\bgajo\b|\bgaja\b|\bgajos\b|\bgajas\b", 5, "gajo/gaja"),
        (r"\bparvo\b|\bparva\b", 5, "parvo/parva"),
        (r"\bbocado\b|\bbocadinho\b", 4, "bocado/bocadinho"),
        (r"\bdespacha[- ]te\b|\bdespache[- ]se\b", 5, "despacha-te"),
        (r"\bdesporto\b", 5, "desporto"),
        (r"\bcontacto\b", 4, "contacto"),
        (r"\bconduz\w*\b", 5, "conduzir"),
        (r"\btu\b", 3, "tu"),
        (r"\bcontigo\b", 4, "contigo"),
        (r"\btens\b|\bestas\b|\bfazes\b|\bqueres\b|\bsabes\b|\bpodes\b", 3, "second-person forms"),
    ]

    pt_br_features = [
        (r"\b(estou|esta|estamos|estao|estava|estavam)\s+[a-z]+(ando|endo|indo)\b", 10, "pt-BR structure: estou fazendo"),
        (r"\bme\s+(diz|da|fala|deixa|liga|chama|conta|ajuda|ouve|ve)\b", 6, "pronoun before verb"),
        (r"\bcelular\b", 8, "celular"),
        (r"\bonibus\b", 8, "ônibus"),
        (r"\btrem\b", 8, "trem"),
        (r"\bbanheiro\b", 8, "banheiro"),
        (r"\bcafe da manha\b", 8, "café da manhã"),
        (r"\bsuco\b|\bgeladeira\b|\bbala\b|\bchiclete\b", 8, "pt-BR food/household terms"),
        (r"\bcarteira de identidade\b|\brg\b|\bcpf\b|\bdetran\b", 8, "pt-BR official documents"),
        (r"\btela\b", 7, "tela"),
        (r"\barquivo\b", 7, "arquivo"),
        (r"\bequipe\b", 7, "equipe"),
        (r"\bgaroto\b|\bgarota\b|\bgarotos\b|\bgarotas\b", 6, "garoto/garota"),
        (r"\bmoleque\b|\bmoleques\b", 6, "moleque"),
        (r"\blegal\b", 5, "legal"),
        (r"\bo cara\b|\besse cara\b|\bcara,\b", 3, "cara as guy"),
        (r"\bcade\b", 6, "cadê"),
        (r"\bdelegacia\b|\bprefeitura\b", 6, "pt-BR institutions"),
        (r"\bcarteira de motorista\b", 6, "carteira de motorista"),
        (r"\bmetr[oô]\b", 4, "metrô"),
        (r"\bdirig\w*\b", 5, "dirigir"),
        (r"\bmoca\b|\bmoco\b|\bmocas\b|\bmocos\b", 4, "moço/moça"),
        (r"\be ai\b|\bbeleza\b|\bvaleu\b", 5, "pt-BR colloquial vocabulary"),
        (r"\bvoce\b|\bvoces\b", 4, "você/vocês"),
        (r"\ba gente\b", 4, "a gente"),
        (r"\bpra\b|\bpro\b|\bpros\b|\bpras\b", 3, "pra/pro"),
        (r"\bta\b|\bto\b|\btava\b", 3, "tá/tô/tava"),
        (r"\bne\b", 2, "né"),
    ]

    pt_pt_score = 0
    pt_br_score = 0
    evidence = []

    for pattern, weight, label in pt_pt_features:
        count = count_pattern(text, pattern)
        if count:
            score = count * weight
            pt_pt_score += score
            evidence.append(("pt-PT", label, count, score))

    for pattern, weight, label in pt_br_features:
        count = count_pattern(text, pattern)
        if count:
            score = count * weight
            pt_br_score += score
            evidence.append(("pt-BR", label, count, score))

    total_score = pt_pt_score + pt_br_score

    if total_score == 0:
        return {
            "code": "pt",
            "name": "Portuguese",
            "confidence": 0.0,
            "pt_pt_score": pt_pt_score,
            "pt_br_score": pt_br_score,
            "scores": {"pt-PT": pt_pt_score, "pt-BR": pt_br_score},
            "evidence": [],
            "reason": "Not enough markers.",
        }

    if pt_pt_score > pt_br_score:
        winner_code = "pt-PT"
        winner_name = language_display_name(winner_code)
        winner_score = pt_pt_score
    elif pt_br_score > pt_pt_score:
        winner_code = "pt-BR"
        winner_name = language_display_name(winner_code)
        winner_score = pt_br_score
    else:
        return {
            "code": "pt",
            "name": "Portuguese",
            "confidence": 0.5,
            "pt_pt_score": pt_pt_score,
            "pt_br_score": pt_br_score,
            "scores": {"pt-PT": pt_pt_score, "pt-BR": pt_br_score},
            "evidence": evidence[:10],
            "reason": "Tie between pt-PT and pt-BR.",
        }

    confidence = winner_score / total_score
    sorted_evidence = sorted(evidence, key=lambda item: item[3], reverse=True)[:10]

    if total_score < 8 or confidence < 0.65:
        return {
            "code": "pt",
            "name": "Portuguese",
            "confidence": confidence,
            "pt_pt_score": pt_pt_score,
            "pt_br_score": pt_br_score,
            "scores": {"pt-PT": pt_pt_score, "pt-BR": pt_br_score},
            "evidence": sorted_evidence,
            "reason": "Low confidence.",
        }

    return {
        "code": winner_code,
        "name": winner_name,
        "confidence": confidence,
        "pt_pt_score": pt_pt_score,
        "pt_br_score": pt_br_score,
        "scores": {"pt-PT": pt_pt_score, "pt-BR": pt_br_score},
        "evidence": sorted_evidence,
        "reason": "Classified by linguistic markers.",
    }


def classify_portuguese_variant_from_srt(srt_path: Path) -> dict[str, Any]:
    raw_text = srt_path.read_text(encoding="utf-8", errors="replace")
    return classify_portuguese_variant(raw_text)


def classify_spanish_variant(raw_subtitle_text: str) -> dict[str, Any]:
    cleaned = clean_subtitle_text(raw_subtitle_text)
    text = remove_accents(cleaned.lower())
    raw_normalized = remove_accents(raw_subtitle_text.lower())

    es_es_features = [
        (r"\bvosotros\b|\bvosotras\b", 25, "vosotros/vosotras", 5),
        (r"\bsois\b|\bestais\b|\bteneis\b|\bsabeis\b|\bpodeis\b|\bquereis\b|\bhabeis\b|\bdebeis\b|\bhaceis\b|\bvais\b|\bvenis\b", 18, "vosotros verb forms", 6),
        (r"\bvuestro(?:s|a|as)?\b", 12, "vosotros possessives", 5),
        (r"\bcoche\b|\bordenador\b|\bmovil\b|\bmando a distancia\b", 14, "strong es-ES everyday terms", 5),
        (r"\bzumo\b|\bfrigorifico\b|\bpatatas fritas\b", 14, "es-ES food/household terms", 5),
        (r"\baparcamiento\b|\bascensor\b|\bgafas\b|\bacera\b|\bmaletero\b", 12, "es-ES object/place terms", 5),
        (r"\bconduc\w*\b|\benfad\w*\b", 14, "es-ES verbs", 5),
        (r"\bcoger(?:lo|la|los|las)?\b|\bcoged\b|\bcoges\b|\bcogeis\b", 8, "coger/tomar", 4),
        (r"\bchaval(?:es|a|as)?\b|\bcurro\b|\bcurrar\b|\bpasta\b|\bpavos\b|\bpringad[oa]s?\b|\bguay\b", 10, "colloquial es-ES vocabulary", 5),
        (r"\bjoder\b|\bcoño\b|\bhostia\b|\bostia\b|\bcojones\b|\bfollar\b|\bgilipollas\b|\bgilipolleces\b", 12, "es-ES profanity/slang", 5),
    ]
    es_419_features = [
        (r"\bustedes\b", 10, "ustedes as plural", 6),
        (r"\bplaticar\b|\bplaticando\b|\bplaticamos\b", 14, "platicar", 4),
        (r"\bcelular\b|\bcomputadora\b|\bcontrol remoto\b", 24, "strong es-419 everyday terms", 5),
        (r"\bjugo\b|\brefrigerador\b|\bfrijol(?:es)?\b|\belote\b|\bpalomitas\b", 14, "es-419 food/household terms", 5),
        (r"\bauto\b|\bcarro\b|\bmanej\w*\b|\bbanqueta\b|\bestacionamiento\b|\belevador\b", 12, "es-419 transport terms", 5),
        (r"\bdepartamento\b|\brecamara\b|\bcloset\b|\balberca\b|\bvereda\b|\bcajuela\b|\bllanta\b", 12, "es-419 object/place terms", 5),
        (r"\bal piso\b|\ben el piso\b|\bdesde un piso\b|\bpiso \d+\b", 18, "es-419 piso/suelo usage", 4),
        (r"\benoj\w*\b", 14, "es-419 enojar/enfadarse usage", 5),
        (r"\bplata\b|\bboleto\b|\bchamba\b|\bcolectivo\b|\bcamion\b|\blentes\b|\brent\w*\b", 10, "es-419 vocabulary", 5),
        (r"\bche\b|\borale\b|\bandale\b|\bpinche\b|\bguey\b|\bcuate\b|\bque onda\b|\bchingad[ao]s?\b|\bchingar\b", 14, "colloquial es-419", 5),
        (r"\bvos\b|\bsos\b|\btenes\b|\bqueres\b|\bpodes\b|\bveni\b", 12, "Latin American voseo", 5),
    ]
    es_es_line_features = [
        (r"(?m)^\s*(?:[-–—]\s*)?[¿¡]?(vale|venga)(?:[.!?…]+|,|$)", 8, "vale/venga as interjection", 5),
    ]

    es_es_score = 0
    es_419_score = 0
    evidence = []

    for pattern, weight, label, max_count in es_es_features:
        count = count_pattern(text, pattern, max_count=max_count)
        if count:
            score = count * weight
            es_es_score += score
            evidence.append(("es-ES", label, count, score))

    for pattern, weight, label, max_count in es_es_line_features:
        count = count_pattern(raw_normalized, pattern, max_count=max_count)
        if count:
            score = count * weight
            es_es_score += score
            evidence.append(("es-ES", label, count, score))

    for pattern, weight, label, max_count in es_419_features:
        count = count_pattern(text, pattern, max_count=max_count)
        if count:
            score = count * weight
            es_419_score += score
            evidence.append(("es-419", label, count, score))

    total_score = es_es_score + es_419_score
    if total_score == 0:
        return {
            "code": "spa",
            "name": "Spanish",
            "confidence": 0.0,
            "es_es_score": es_es_score,
            "es_419_score": es_419_score,
            "scores": {"es-ES": es_es_score, "es-419": es_419_score},
            "evidence": [],
            "reason": "Not enough markers.",
        }

    if es_es_score > es_419_score:
        winner_code = "es-ES"
        winner_name = language_display_name(winner_code)
        winner_score = es_es_score
    elif es_419_score > es_es_score:
        winner_code = "es-419"
        winner_name = language_display_name(winner_code)
        winner_score = es_419_score
    else:
        return {
            "code": "spa",
            "name": "Spanish",
            "confidence": 0.5,
            "es_es_score": es_es_score,
            "es_419_score": es_419_score,
            "scores": {"es-ES": es_es_score, "es-419": es_419_score},
            "evidence": sorted(evidence, key=lambda item: item[3], reverse=True)[:10],
            "reason": "Tie between es-ES and es-419.",
        }

    confidence = winner_score / total_score
    sorted_evidence = sorted(evidence, key=lambda item: item[3], reverse=True)[:10]

    if total_score < 18 or confidence < 0.62:
        return {
            "code": "spa",
            "name": "Spanish",
            "confidence": confidence,
            "es_es_score": es_es_score,
            "es_419_score": es_419_score,
            "scores": {"es-ES": es_es_score, "es-419": es_419_score},
            "evidence": sorted_evidence,
            "reason": "Low confidence.",
        }

    return {
        "code": winner_code,
        "name": winner_name,
        "confidence": confidence,
        "es_es_score": es_es_score,
        "es_419_score": es_419_score,
        "scores": {"es-ES": es_es_score, "es-419": es_419_score},
        "evidence": sorted_evidence,
        "reason": "Classified by linguistic markers.",
    }


def classify_spanish_variant_from_srt(srt_path: Path) -> dict[str, Any]:
    raw_text = srt_path.read_text(encoding="utf-8", errors="replace")
    return classify_spanish_variant(raw_text)


def classify_variant_by_features(
    raw_subtitle_text: str,
    fallback_code: str,
    features_by_code: dict[str, list[tuple[str, int, str]]],
    reason_label: str = "Classified by linguistic markers.",
) -> dict[str, Any]:
    cleaned = clean_subtitle_text(raw_subtitle_text)
    text = remove_accents(cleaned.lower())
    scores = {code: 0 for code in features_by_code}
    evidence = []

    for code, features in features_by_code.items():
        for pattern, weight, label in features:
            count = count_pattern(text, pattern)
            if count:
                score = count * weight
                scores[code] += score
                evidence.append((code, label, count, score))

    total_score = sum(scores.values())
    if total_score == 0:
        return {
            "code": fallback_code,
            "name": language_display_name(fallback_code),
            "confidence": 0.0,
            "scores": scores,
            "evidence": [],
            "reason": "Not enough markers.",
        }

    best_score = max(scores.values())
    winners = [code for code, score in scores.items() if score == best_score]
    sorted_evidence = sorted(evidence, key=lambda item: item[3], reverse=True)[:10]
    if len(winners) != 1:
        return {
            "code": fallback_code,
            "name": language_display_name(fallback_code),
            "confidence": 0.5,
            "scores": scores,
            "evidence": sorted_evidence,
            "reason": "Tie between variants.",
        }

    winner_code = winners[0]
    confidence = best_score / total_score
    if total_score < 8 or confidence < 0.65:
        return {
            "code": fallback_code,
            "name": language_display_name(fallback_code),
            "confidence": confidence,
            "scores": scores,
            "evidence": sorted_evidence,
            "reason": "Low confidence.",
        }

    return {
        "code": winner_code,
        "name": language_display_name(winner_code),
        "confidence": confidence,
        "scores": scores,
        "evidence": sorted_evidence,
        "reason": reason_label,
    }


def classify_french_variant(raw_subtitle_text: str) -> dict[str, Any]:
    features_by_code = {
        "fr-FR": [
            (r"\bportable\b|\btelephone portable\b", 8, "portable"),
            (r"\bweek[- ]end\b|\bweekend\b", 6, "week-end"),
            (r"\bpetit[- ]dejeuner\b", 7, "petit-déjeuner"),
            (r"\bordinateur\b|\bparking\b|\bcode postal\b", 6, "fr-FR everyday terms"),
            (r"\bpermis de conduire\b|\bcarte d'identite\b|\bcarte nationale d'identite\b", 7, "fr-FR official terms"),
            (r"\bvoiture\b|\bbagnole\b|\bmec\b|\bmeuf\b", 5, "fr-FR vocabulary"),
            (r"\bputain\b|\bmerde\b|\bbordel\b", 4, "colloquial fr-FR"),
            (r"\blycee\b|\bappartement\b", 4, "fr-FR terms"),
        ],
        "fr-CA": [
            (r"\bcellulaire\b|\bcourriel\b", 8, "fr-CA terms"),
            (r"\bdepanneur\b|\bmagasiner\b|\bstationnement\b", 8, "fr-CA vocabulary"),
            (r"\bfin de semaine\b", 8, "fin de semaine"),
            (r"\bcedule\b|\bbreuvage\b|\bsouliers\b|\bvidanges\b", 7, "fr-CA everyday terms"),
            (r"\bpermis de conduire\b|\bassurance maladie\b|\bcarte soleil\b", 6, "fr-CA official terms"),
            (r"\bchar\b|\bblonde\b|\bchum\b", 7, "coloquial fr-CA"),
            (r"\btabarnak\b|\bosti(?:e)?\b|\bcaliss(?:e)?\b", 9, "fr-CA profanity"),
            (r"\bicitte\b|\bpantoute\b|\bniaiseux\b|\btuque\b", 7, "Quebecisms"),
        ],
    }
    return classify_variant_by_features(raw_subtitle_text, "fre", features_by_code)


def classify_french_variant_from_srt(srt_path: Path) -> dict[str, Any]:
    raw_text = srt_path.read_text(encoding="utf-8", errors="replace")
    return classify_french_variant(raw_text)


def classify_chinese_variant(raw_subtitle_text: str) -> dict[str, Any]:
    cleaned = clean_subtitle_text(raw_subtitle_text)
    traditional_chars = "個們這來時會說國過對還後麼開關門電話聲氣車無為見聽學體樂頭"
    simplified_chars = "个们这来时会说国过对还后么开关门电话声气车无为见听学体乐头"
    hant_count = min(sum(cleaned.count(char) for char in traditional_chars), 50)
    hans_count = min(sum(cleaned.count(char) for char in simplified_chars), 50)
    scores = {"zh-Hant": hant_count * 2, "zh-Hans": hans_count * 2}
    total_score = sum(scores.values())
    evidence = []
    if scores["zh-Hant"]:
        evidence.append(("zh-Hant", "traditional characters", hant_count, scores["zh-Hant"]))
    if scores["zh-Hans"]:
        evidence.append(("zh-Hans", "simplified characters", hans_count, scores["zh-Hans"]))

    if total_score == 0:
        return {
            "code": "chi",
            "name": "Chinese",
            "confidence": 0.0,
            "scores": scores,
            "evidence": [],
            "reason": "Not enough markers.",
        }

    if scores["zh-Hant"] == scores["zh-Hans"]:
        return {
            "code": "chi",
            "name": "Chinese",
            "confidence": 0.5,
            "scores": scores,
            "evidence": evidence,
            "reason": "Tie between zh-Hant and zh-Hans.",
        }

    winner_code = "zh-Hant" if scores["zh-Hant"] > scores["zh-Hans"] else "zh-Hans"
    confidence = scores[winner_code] / total_score
    if confidence < 0.65:
        return {
            "code": "chi",
            "name": "Chinese",
            "confidence": confidence,
            "scores": scores,
            "evidence": evidence,
            "reason": "Low confidence.",
        }

    return {
        "code": winner_code,
        "name": language_display_name(winner_code),
        "confidence": confidence,
        "scores": scores,
        "evidence": evidence,
        "reason": "Classified by traditional/simplified characters.",
    }


def classify_chinese_variant_from_srt(srt_path: Path) -> dict[str, Any]:
    raw_text = srt_path.read_text(encoding="utf-8", errors="replace")
    return classify_chinese_variant(raw_text)


LANGUAGE_VARIANT_CLASSIFIERS = {
    "por": classify_portuguese_variant,
    "spa": classify_spanish_variant,
    "fre": classify_french_variant,
    "chi": classify_chinese_variant,
}


LANGUAGE_VARIANT_FILE_CLASSIFIERS = {
    "por": classify_portuguese_variant_from_srt,
    "spa": classify_spanish_variant_from_srt,
    "fre": classify_french_variant_from_srt,
    "chi": classify_chinese_variant_from_srt,
}


def canonicalize_ietf_code(code: str) -> str:
    parts = code.strip().replace("_", "-").split("-")
    if not parts:
        return code

    canonical = [parts[0].lower()]
    for subtag in parts[1:]:
        if len(subtag) == 4 and subtag.isalpha():
            canonical.append(subtag.title())
        elif (len(subtag) == 2 and subtag.isalpha()) or (len(subtag) == 3 and subtag.isdigit()):
            canonical.append(subtag.upper())
        else:
            canonical.append(subtag.lower())

    return "-".join(canonical)


def normalize_language_code(raw_code: str | None) -> str:
    if not raw_code:
        return "und"

    code = raw_code.strip().replace("_", "-")
    if not code:
        return "und"

    lower_code = code.lower()
    if lower_code in LANGUAGE_ALIASES:
        return LANGUAGE_ALIASES[lower_code]

    primary = lower_code.split("-", 1)[0]
    if "-" in lower_code and primary in LANGUAGE_ALIASES:
        return canonicalize_ietf_code(code)
    if primary in LANGUAGE_ALIASES:
        return LANGUAGE_ALIASES[primary]

    return code


def base_language_code(code: str | None) -> str:
    if not code:
        return "und"

    normalized = normalize_language_code(code)
    if normalized in LANGUAGE_VARIANT_BASES:
        return LANGUAGE_VARIANT_BASES[normalized]

    lower_code = normalized.lower()
    primary = lower_code.split("-", 1)[0]
    return LANGUAGE_ALIASES.get(primary, normalized)


def language_hint_search_text(*texts: str | None) -> str:
    text = remove_accents(" ".join(item or "" for item in texts).casefold())
    text = re.sub(r"[\[\](){},.;:/\\_]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def language_variant_from_hints(base_code: str, *texts: str | None) -> str | None:
    hints = LANGUAGE_VARIANT_HINTS.get(base_code)
    if not hints:
        return None

    text = language_hint_search_text(*texts)
    if not text:
        return None

    for variant_code, variant_hints in hints:
        for hint in variant_hints:
            normalized_hint = language_hint_search_text(hint)
            if normalized_hint and normalized_hint in text:
                return variant_code

    return None


def language_from_track_name_hint(track_name: str | None) -> str | None:
    text = language_hint_search_text(track_name)
    if not text:
        return None

    for language_code, hints in LANGUAGE_NAME_HINTS:
        for hint in hints:
            normalized_hint = language_hint_search_text(hint)
            if normalized_hint and normalized_hint in text:
                return language_code

    return None


def normalize_language_from_properties(raw_code: str | None, track_name: str | None) -> str:
    language = normalize_language_code(raw_code)
    explicit_language = language_from_track_name_hint(track_name)
    if explicit_language:
        return explicit_language

    base_code = base_language_code(language)
    hinted_variant = language_variant_from_hints(base_code, raw_code, track_name)
    return hinted_variant or language


def language_display_name(code: str) -> str:
    if not code:
        return "Undetermined"
    if code in LANGUAGE_NAMES:
        return LANGUAGE_NAMES[code]

    normalized = normalize_language_code(code)
    if normalized in LANGUAGE_NAMES:
        return LANGUAGE_NAMES[normalized]

    parts = canonicalize_ietf_code(normalized).split("-")
    if len(parts) > 1:
        base_name = LANGUAGE_NAMES.get(base_language_code(code), parts[0])
        for subtag in reversed(parts[1:]):
            subtag_name = LANGUAGE_SUBTAG_NAMES.get(subtag)
            if subtag_name:
                return f"{base_name} ({subtag_name})"

    return code


def language_for_mkvmerge(code: str) -> str:
    if code == "pt":
        return "por"
    if code == "nob":
        return "nb"
    if code == "nno":
        return "nn"
    return code or "und"


def legacy_language_for_mkvpropedit(code: str) -> str:
    base_code = base_language_code(code)
    if base_code == "pt":
        return "por"
    return base_code or "und"


def ietf_language_for_mkvpropedit(code: str) -> str:
    normalized = normalize_language_code(code)
    if normalized in LANGUAGE_VARIANT_BASES or "-" in normalized:
        return canonicalize_ietf_code(normalized)

    base_code = base_language_code(normalized)
    return IETF_PRIMARY_BY_MKV_LANGUAGE.get(base_code, base_code or "und")


def is_english(track: TrackInfo) -> bool:
    return base_language_code(track.output_language) == "eng" or base_language_code(track.language) == "eng"


def base_language_key(track: TrackInfo) -> str:
    return base_language_code(track.output_language or track.language)


def variant_base_language_code(code: str | None) -> str:
    base_code = base_language_code(code)
    return LANGUAGE_VARIANT_BASE_ALIASES.get(base_code, base_code)


def variant_base_language_key(track: TrackInfo) -> str:
    return variant_base_language_code(track.output_language or track.language)


def parse_channels(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        match = re.search(r"\d+", str(value))
        return int(match.group(0)) if match else None


def channel_label(channels: int | None) -> str:
    if not channels:
        return ""

    known = {
        1: "1.0",
        2: "2.0",
        3: "2.1",
        4: "4.0",
        5: "5.0",
        6: "5.1",
        7: "6.1",
        8: "7.1",
    }
    return known.get(channels, f"{channels}.0")


def combined_track_text(track: TrackInfo) -> str:
    return remove_accents(
        " ".join(
            [
                track.codec or "",
                track.codec_id or "",
                track.original_name or "",
                language_display_name(track.language),
            ]
        ).lower()
    )


def track_property_enabled(track: TrackInfo, *names: str) -> bool:
    for name in names:
        value = track.properties.get(name)
        if isinstance(value, bool):
            if value:
                return True
        elif isinstance(value, str):
            if value.strip().lower() in {"1", "true", "yes"}:
                return True
        elif value:
            return True

    return False


def has_commentary_flag(track: TrackInfo) -> bool:
    return track_property_enabled(track, "flag_commentary", "commentary")


def has_hearing_impaired_flag(track: TrackInfo) -> bool:
    return track_property_enabled(track, "flag_hearing_impaired", "hearing_impaired")


def has_forced_flag(track: TrackInfo) -> bool:
    return track_property_enabled(track, "forced_track", "flag_forced")


def detect_audio_role(track: TrackInfo) -> str:
    if track.type == "audio" and track.role == "commentary":
        return "Commentary"
    if has_commentary_flag(track):
        return "Commentary"

    text = combined_track_text(track)

    if re.search(r"\bisolated score\b|\bscore only\b", text):
        return "Isolated Score"
    if re.search(r"\baudio description\b|\bdescriptive audio\b|\bdescribed video\b|\bnarration for blind\b", text):
        return "Audio Description"
    if re.search(r"\bcommentary\b|\bcommentaries\b|\bcomentario\b|\bcomentarios\b", text):
        return "Commentary"

    return ""


def detect_subtitle_role_hint(track: TrackInfo) -> str:
    if has_commentary_flag(track):
        return "commentary"
    if has_hearing_impaired_flag(track):
        return "sdh"
    if has_forced_flag(track):
        return "forced"

    text = combined_track_text(track)

    if re.search(r"\bcommentary\b|\bcommentaries\b|\bcomentario\b|\bcomentarios\b", text):
        return "commentary"
    if re.search(r"\bsdh\b|\bcc\b|\bclosed captions?\b|\bhearing impaired\b|\bhard of hearing\b|\bhoh\b", text):
        return "sdh"
    if re.search(r"\bforced\b|\bforcado\b|\bforcada\b", text):
        return "forced"

    return ""


def audio_codec_label(track: TrackInfo) -> str:
    text = combined_track_text(track)

    if "truehd" in text and "atmos" in text:
        return "TrueHD Atmos"
    if "dts:x" in text or "dtsx" in text:
        return "DTS:X"
    if "truehd" in text or "a_truehd" in text:
        return "TrueHD"
    if "dts" in text and ("master audio" in text or "dts-hd ma" in text or "dts_hd ma" in text):
        return "DTS-HD MA"
    if "pcm" in text or "lpcm" in text or "a_pcm" in text:
        return "PCM"
    if "dts" in text and ("high resolution" in text or "dts-hd hra" in text or "dts_hd hra" in text):
        return "DTS-HD HRA"
    if "flac" in text or "a_flac" in text:
        return "FLAC"
    if ("e-ac-3" in text or "eac3" in text or "a_eac3" in text or "e-ac3" in text) and "atmos" in text:
        return "E-AC-3 Atmos"
    if "e-ac-3" in text or "eac3" in text or "a_eac3" in text or "e-ac3" in text:
        return "E-AC-3"
    if "dts" in text or "a_dts" in text:
        return "DTS"
    if "ac-3" in text or "ac3" in text or "a_ac3" in text:
        return "AC-3"
    if "aac" in text or "a_aac" in text:
        return "AAC"
    if "mp3" in text or "mpeg/l3" in text:
        return "MP3"

    return track.codec.strip() or track.codec_id.strip() or "Audio"


def audio_track_format_name(track: TrackInfo) -> str:
    parts = [audio_codec_label(track)]
    channels = channel_label(track.channels)
    role = detect_audio_role(track)

    if channels:
        parts.append(channels)
    if role:
        parts.append(role)

    return " ".join(parts)


def audio_language_name_for_track(track: TrackInfo) -> str:
    language_code = normalize_language_code(track.output_language or track.language)
    if language_code in {"", "und", "zxx", "mul"}:
        return ""

    language_name = track.language_name or language_display_name(language_code)
    if language_name.casefold() in {"undetermined", "multiple languages", "no linguistic content"}:
        return ""
    return language_name


def audio_track_language_format_name(track: TrackInfo) -> str:
    format_name = audio_track_format_name(track)
    language_name = audio_language_name_for_track(track)
    if not language_name:
        return format_name
    return f"{language_name} - {format_name}"


def audio_track_name(track: TrackInfo, style: str = "format") -> str:
    if style == "keep":
        return track.original_name
    if style == "language-format":
        return audio_track_language_format_name(track)
    return audio_track_format_name(track)


def resolve_audio_name_style(audio_tracks: list[TrackInfo], style: str) -> str:
    if style != "auto":
        return style

    languages = {
        normalize_language_code(track.output_language or track.language)
        for track in audio_tracks
        if audio_language_name_for_track(track)
    }
    return "language-format" if len(languages) > 1 else "format"


def audio_quality_score(track: TrackInfo) -> tuple[int, int, int, int]:
    role = detect_audio_role(track)
    codec = audio_codec_label(track)
    is_main_audio = 0 if role else 1
    priority = AUDIO_CODEC_PRIORITY.get(codec, 0)
    channels = track.channels or 0
    return (is_main_audio, priority, channels, -track.order)


def select_default_audio(audio_tracks: list[TrackInfo]) -> TrackInfo | None:
    if not audio_tracks:
        return None

    english_tracks = [track for track in audio_tracks if is_english(track)]
    if not english_tracks:
        return audio_tracks[0]

    return max(english_tracks, key=audio_quality_score)


def track_language_code(track: TrackInfo) -> str:
    return normalize_language_code(track.output_language or track.language)


def normalize_region_name(raw_name: str) -> str:
    normalized = str(raw_name or "").strip().casefold().replace("_", "-")
    normalized = re.sub(r"\s+", "-", normalized)
    return REGIONAL_LANGUAGE_GROUP_ALIASES.get(normalized, normalized)


def parse_regional_order(value: Any) -> tuple[str, ...]:
    raw_items: list[str]
    if value is None:
        raw_items = []
    elif isinstance(value, str):
        raw_items = re.split(r"[;,]", value)
    else:
        raw_items = []
        for item in value:
            raw_items.extend(re.split(r"[;,]", str(item)))

    selected: list[str] = []
    seen: set[str] = set()
    unknown: list[str] = []
    for raw_item in raw_items:
        if not str(raw_item).strip():
            continue
        region_name = normalize_region_name(str(raw_item))
        if region_name not in REGIONAL_LANGUAGE_GROUP_NAMES:
            unknown.append(str(raw_item).strip())
            continue
        if region_name not in seen:
            selected.append(region_name)
            seen.add(region_name)

    if unknown:
        allowed = ", ".join(DEFAULT_REGIONAL_ORDER)
        raise OrganizerError(
            "Unknown region name(s) in regional order: "
            + ", ".join(unknown)
            + f". Allowed values: {allowed}."
        )

    return tuple(selected + [region_name for region_name in DEFAULT_REGIONAL_ORDER if region_name not in seen])


def regional_language_order_map(regional_order: Any = None) -> dict[str, tuple[int, int]]:
    order = parse_regional_order(regional_order)
    groups_by_name = {region_name: language_codes for region_name, language_codes in REGIONAL_LANGUAGE_GROUPS}
    return {
        language_code: (region_index, language_index)
        for region_index, region_name in enumerate(order)
        for language_index, language_code in enumerate(groups_by_name[region_name])
    }


def regional_language_sort_key(code: str | None, regional_order: Any = None) -> tuple[int, int, str]:
    language_code = normalize_language_code(code)
    language_name = remove_accents(language_display_name(language_code)).casefold()
    order_map = REGIONAL_LANGUAGE_ORDER if regional_order is None else regional_language_order_map(regional_order)
    rank = order_map.get(language_code)
    if rank is None:
        rank = order_map.get(base_language_code(language_code))
    if rank is None:
        return (len(REGIONAL_LANGUAGE_GROUPS), 999, language_name)
    return (*rank, language_name)


def language_sort_key(
    track: TrackInfo,
    language_order_style: str = "default",
    regional_order: Any = None,
) -> tuple[Any, ...]:
    language_name = remove_accents(track.language_name or language_display_name(track_language_code(track))).casefold()
    if language_order_style == "regional":
        return (*regional_language_sort_key(track_language_code(track), regional_order), language_name)
    return (language_name,)


def audio_sort_key(
    track: TrackInfo,
    language_order_style: str = "default",
    regional_order: Any = None,
) -> tuple[Any, ...]:
    role = detect_audio_role(track)
    special_role_group = 1 if role in {"Audio Description", "Commentary", "Isolated Score"} else 0
    if language_order_style == "regional":
        return (
            0 if track.default else 1,
            special_role_group,
            *language_sort_key(track, language_order_style, regional_order),
            track.order,
        )
    return (0 if track.default else 1, special_role_group, track.order)


def subtitle_extension(track: TrackInfo) -> str:
    text = combined_track_text(track)

    if "pgs" in text or "hdmv" in text:
        return ".sup"
    if "utf8" in text or "srt" in text:
        return ".srt"
    if "ass" in text:
        return ".ass"
    if "ssa" in text:
        return ".ssa"
    if "vobsub" in text:
        return ".idx"

    return ".sup"


def is_text_subtitle(track: TrackInfo) -> bool:
    text = combined_track_text(track)
    return any(marker in text for marker in ["utf8", "srt", "ass", "ssa", "webvtt", "text"])


def is_pgs_subtitle(track: TrackInfo) -> bool:
    text = combined_track_text(track)
    return any(marker in text for marker in ["pgs", "hdmv", "presentation graphics", "s_hdmv"])


def has_sdh_subtitle_hint(track: TrackInfo) -> bool:
    if has_hearing_impaired_flag(track):
        return True

    text = combined_track_text(track)
    return bool(
        re.search(
            r"\bsdh\b|\bcc\b|\bclosed captions?\b|\bhearing impaired\b|\bhard of hearing\b|\bhoh\b",
            text,
        )
    )


def read_text_sample(path: Path) -> str:
    if not path.exists():
        return ""

    try:
        return path.read_text(encoding="utf-8", errors="replace")[:MAX_TEXT_SAMPLE_CHARS]
    except OSError:
        return ""


def subtitle_text_tokens(raw_text: str) -> set[str]:
    cleaned = remove_accents(clean_subtitle_text(raw_text).lower())
    tokens = {
        token
        for token in re.findall(r"[a-z0-9]{3,}", cleaned)
        if token not in TEXT_STOPWORDS
    }
    return tokens


def add_text_score(stats: SubtitleTextStats, kind: str, points: int, label: str, count: int) -> None:
    if kind == "commentary":
        stats.commentary_score += points
        stats.commentary_evidence.append(f"+{points} {label} x{count}")
    elif kind == "sdh":
        stats.sdh_score += points
        stats.sdh_evidence.append(f"+{points} {label} x{count}")


def analyze_subtitle_text(raw_text: str) -> SubtitleTextStats:
    cleaned = clean_subtitle_text(raw_text)
    text = remove_accents(cleaned.lower())
    raw_marker_text = remove_accents(raw_text.lower())
    tokens = subtitle_text_tokens(raw_text)
    stats = SubtitleTextStats(word_count=len(re.findall(r"\w+", text)), tokens=tokens)

    commentary_patterns = [
        (r"\bcommentary\b|\bcomentario\b|\bcommentaire\b|\bkommentar\b", 45, "commentary word"),
        (r"\bdirector\b|\bdirected\b|\brealisador\b|\bdiretor\b|\brealisateur\b|\bregisseur\b", 20, "directing terms"),
        (r"\bwriter\b|\bwritten\b|\bescritor\b|\bargumentista\b|\broteirista\b|\bscenariste\b|\bautor\b", 20, "writing/script terms"),
        (r"\bproducer\b|\bexecutive producer\b|\bprodutor\b|\bproducteur\b|\bproduzent\b", 18, "production terms"),
        (r"\bactor\b|\bactress\b|\bator\b|\batriz\b|\bacteur\b|\bactrice\b|\bschauspieler\b", 16, "cast terms"),
        (r"\bepisode\b|\bepisodio\b|\bepisodio\b|\bfolge\b", 14, "episode terms"),
        (r"\bscene\b|\bcena\b|\bsequence\b|\bsequencia\b|\bszene\b", 14, "scene/sequence terms"),
        (r"\bfilm(?:ed|ing)?\b|\bshoot(?:ing)?\b|\bfilmamos\b|\bfilmado\b|\brodagem\b|\btournage\b", 14, "filming/shooting terms"),
        (r"\bscript\b|\bguiao\b|\bguion\b|\broteiro\b|\bscenario\b|\bdrehbuch\b", 14, "script terms"),
        (r"\bwe wanted\b|\bqueriamos\b|\bnous voulions\b|\bdecidimos\b|\bwe decided\b", 12, "behind-the-scenes language"),
    ]

    for pattern, weight, label in commentary_patterns:
        count = count_pattern(text, pattern, max_count=5)
        if count:
            add_text_score(stats, "commentary", count * weight, label, count)

    for pattern, weight, label in sdh_text_patterns():
        count = count_pattern(raw_marker_text, pattern, max_count=10)
        if count:
            add_text_score(stats, "sdh", count * weight, label, count)

    speaker_label_count = len(
        re.findall(
            r"(?m)^\s*[-–—]?\s*[A-ZÁÉÍÓÚÀÂÊÔÃÕÇ0-9][A-ZÁÉÍÓÚÀÂÊÔÃÕÇ0-9 .'\-]{1,28}:\s+",
            raw_text,
        )
    )
    if speaker_label_count >= 2:
        count = min(speaker_label_count, 10)
        add_text_score(stats, "sdh", count * 8, "uppercase speaker labels", count)

    stats.commentary_score = min(stats.commentary_score, 100)
    stats.sdh_score = min(stats.sdh_score, 100)
    return stats


def pgs_timestamp(raw_value: bytes) -> float:
    return int.from_bytes(raw_value, "big") / PGS_CLOCK


def capped_event_duration(start_pts: float, end_pts: float) -> float:
    if end_pts <= start_pts:
        return 0.0
    return min(end_pts - start_pts, PGS_MAX_EVENT_SECONDS)


def add_pgs_event_duration(stats: PgsStats, start_pts: float, end_pts: float, object_count: int) -> None:
    duration = capped_event_duration(start_pts, end_pts)
    stats.estimated_display_seconds += duration
    stats.max_event_seconds = max(stats.max_event_seconds, duration)
    if duration > 0:
        stats.events.append(PgsEvent(start=start_pts, duration=duration, object_count=object_count))


def parse_pgs_sup(path: Path) -> PgsStats:
    stats = PgsStats(total_bytes=path.stat().st_size if path.exists() else 0)

    try:
        data = path.read_bytes()
    except OSError:
        stats.parse_errors += 1
        return stats

    offset = 0
    active_start_pts: float | None = None
    active_object_count = 0

    while offset + 13 <= len(data):
        if data[offset:offset + 2] != b"PG":
            next_offset = data.find(b"PG", offset + 1)
            stats.parse_errors += 1
            if next_offset < 0:
                break
            offset = next_offset
            continue

        pts = pgs_timestamp(data[offset + 2:offset + 6])
        segment_type = data[offset + 10]
        segment_length = int.from_bytes(data[offset + 11:offset + 13], "big")
        payload_start = offset + 13
        payload_end = payload_start + segment_length

        if payload_end > len(data):
            stats.parse_errors += 1
            break

        payload = data[payload_start:payload_end]
        stats.segments += 1
        stats.first_pts = pts if stats.first_pts is None else min(stats.first_pts, pts)
        stats.last_pts = pts if stats.last_pts is None else max(stats.last_pts, pts)

        if segment_type == 0x16:
            stats.pcs_segments += 1
            object_count = payload[10] if len(payload) >= 11 else 0
            if len(payload) >= 4:
                stats.video_width = max(stats.video_width, int.from_bytes(payload[0:2], "big"))
                stats.video_height = max(stats.video_height, int.from_bytes(payload[2:4], "big"))

            if object_count:
                stats.display_events += 1
                stats.object_refs += object_count
                if active_start_pts is not None:
                    add_pgs_event_duration(stats, active_start_pts, pts, active_object_count)
                active_start_pts = pts
                active_object_count = object_count
            else:
                stats.clear_events += 1
                if active_start_pts is not None:
                    add_pgs_event_duration(stats, active_start_pts, pts, active_object_count)
                    active_start_pts = None
                    active_object_count = 0
        elif segment_type == 0x17:
            stats.wds_segments += 1
        elif segment_type == 0x14:
            stats.pds_segments += 1
        elif segment_type == 0x15:
            stats.ods_segments += 1
            if len(payload) >= 4:
                sequence_flag = payload[3]
                if sequence_flag & 0x80 and len(payload) >= 11:
                    width = int.from_bytes(payload[7:9], "big")
                    height = int.from_bytes(payload[9:11], "big")
                    stats.max_object_width = max(stats.max_object_width, width)
                    stats.max_object_height = max(stats.max_object_height, height)
                    stats.largest_object_area = max(stats.largest_object_area, width * height)
                    stats.ods_payload_bytes += max(0, len(payload) - 11)
                else:
                    stats.ods_payload_bytes += max(0, len(payload) - 4)
        elif segment_type == 0x80:
            stats.end_segments += 1

        offset = payload_end

    if active_start_pts is not None and stats.last_pts is not None:
        add_pgs_event_duration(stats, active_start_pts, stats.last_pts + 5.0, active_object_count)

    return stats


def classify_subtitle_size(track: TrackInfo, size_bytes: int | None) -> str:
    if size_bytes is None:
        return "unknown"

    if is_text_subtitle(track):
        if size_bytes <= TEXT_EMPTY_SUB_BYTES:
            return "empty"
        if size_bytes <= TEXT_SMALL_SUB_BYTES:
            return "small"
        return "large"

    if size_bytes <= BINARY_EMPTY_SUB_BYTES:
        return "empty"
    if size_bytes <= BINARY_SMALL_SUB_BYTES:
        return "small"
    return "large"


def extracted_size(path: Path) -> int:
    size = path.stat().st_size if path.exists() else 0

    # VobSub pode gerar .idx + .sub; contar os dois se existirem.
    if path.suffix.lower() == ".idx":
        sub_path = path.with_suffix(".sub")
        if sub_path.exists():
            size += sub_path.stat().st_size

    return size


def format_command(command: list[str]) -> str:
    return subprocess.list2cmdline([str(part) for part in command])


def format_size(size_bytes: int | None) -> str:
    if size_bytes is None:
        return "n/a"
    return f"{size_bytes} B ({size_bytes / (1024 * 1024):.2f} MB)"


def parse_id_list(raw_value: str, option_name: str = "--forced-subtitle-ids") -> set[int]:
    ids: set[int] = set()
    if not raw_value:
        return ids

    for part in re.split(r"[,\s]+", raw_value.strip()):
        if not part:
            continue
        if not part.isdigit():
            raise OrganizerError(f"Invalid subtitle ID in {option_name}: {part}")
        ids.add(int(part))

    return ids


def parse_subtitle_language_overrides(
    raw_entries: str | list[str] | None,
    option_name: str = "--subtitle-language-ids",
) -> dict[int, str]:
    if not raw_entries:
        return {}

    if isinstance(raw_entries, str):
        entries = [item.strip() for item in raw_entries.split(";") if item.strip()]
    else:
        entries = []
        for raw_entry in raw_entries:
            entries.extend(item.strip() for item in str(raw_entry).split(";") if item.strip())

    overrides: dict[int, str] = {}
    for entry in entries:
        if ":" not in entry:
            raise OrganizerError(
                f"Invalid language override in {option_name}: {entry}. "
                "Expected LANGUAGE:ID[,ID...]"
            )

        raw_language, raw_ids = entry.split(":", 1)
        language = normalize_language_code(raw_language)
        if not raw_language.strip() or language == "und":
            raise OrganizerError(f"Invalid language code in {option_name}: {raw_language!r}")

        ids = parse_id_list(raw_ids, option_name)
        if not ids:
            raise OrganizerError(f"Missing subtitle IDs in {option_name}: {entry}")

        for track_id in ids:
            existing_language = overrides.get(track_id)
            if existing_language and existing_language != language:
                raise OrganizerError(
                    f"Subtitle track {track_id} has conflicting language overrides: "
                    f"{existing_language} and {language}."
                )
            overrides[track_id] = language

    return overrides


def parse_track_delay_overrides(raw_value: str | None, option_name: str) -> dict[int, int]:
    if not raw_value:
        return {}

    overrides: dict[int, int] = {}
    entry_pattern = re.compile(r"(\d+)\s*[:=]\s*([+-]?\d+)(?:ms)?", re.IGNORECASE)
    matches = list(entry_pattern.finditer(raw_value))
    remainder = entry_pattern.sub("", raw_value)
    if not matches or re.sub(r"[,;\s]+", "", remainder):
        raise OrganizerError(
            f"Invalid delay override in {option_name}: {raw_value}. "
            "Expected TRACK_ID:DELAY_MS, for example 2:150 or 5:-250."
        )

    for match in matches:
        track_id = int(match.group(1))
        delay_ms = int(match.group(2))
        existing_delay = overrides.get(track_id)
        if existing_delay is not None and existing_delay != delay_ms:
            raise OrganizerError(
                f"Track {track_id} has conflicting delay overrides in {option_name}: "
                f"{existing_delay} and {delay_ms}."
            )
        overrides[track_id] = delay_ms

    return overrides


def apply_subtitle_language_overrides(subtitles: list[TrackInfo], overrides: dict[int, str]) -> None:
    if not overrides:
        return

    found_ids = {track.id for track in subtitles}
    missing_ids = sorted(set(overrides) - found_ids)
    if missing_ids:
        raise OrganizerError(
            "IDs provided in --subtitle-language-ids do not exist as subtitle tracks: "
            + ", ".join(str(track_id) for track_id in missing_ids)
        )

    for track in subtitles:
        language = overrides.get(track.id)
        if language:
            track.language = language
            track.output_language = language
            track.language_name = language_display_name(language)


def apply_track_delay_overrides(
    audio_tracks: list[TrackInfo],
    subtitles: list[TrackInfo],
    audio_delays: dict[int, int],
    subtitle_delays: dict[int, int],
) -> None:
    audio_ids = {track.id for track in audio_tracks}
    subtitle_ids = {track.id for track in subtitles}

    missing_audio_ids = sorted(set(audio_delays) - audio_ids)
    if missing_audio_ids:
        raise OrganizerError(
            "IDs provided in --audio-delays do not exist as audio tracks: "
            + ", ".join(str(track_id) for track_id in missing_audio_ids)
        )

    missing_subtitle_ids = sorted(set(subtitle_delays) - subtitle_ids)
    if missing_subtitle_ids:
        raise OrganizerError(
            "IDs provided in --subtitle-delays do not exist as subtitle tracks: "
            + ", ".join(str(track_id) for track_id in missing_subtitle_ids)
        )

    for track in audio_tracks:
        track.delay_ms = audio_delays.get(track.id, 0)
    for track in subtitles:
        track.delay_ms = subtitle_delays.get(track.id, 0)


def require_tool(path: Path | None, description: str) -> None:
    if not path or not path.exists():
        raise OrganizerError(
            f"Could not find {description}: {path}\n"
            f"Use --{description.lower()} to set the correct path, add it to PATH, "
            f"set the matching environment variable, or place the tool in {local_tools_dir()}."
        )


def ensure_not_cancelled(cancel_callback: Callable[[], bool] | None = None) -> None:
    if cancel_callback and cancel_callback():
        raise OrganizerCancelled("Operation cancelled.")


def command_with_mkvtoolnix_ui_language(command: list[str]) -> list[str]:
    if "--ui-language" in command:
        return command

    executable = Path(command[0]).stem.lower()
    if executable not in {"mkvmerge", "mkvextract", "mkvpropedit"}:
        return command

    return [command[0], "--ui-language", "en", *command[1:]]


def terminate_process(process: subprocess.Popen) -> None:
    if process.poll() is not None:
        return

    process.terminate()
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait()


def load_metadata(mkvmerge: Path, input_path: Path) -> dict[str, Any]:
    command = command_with_mkvtoolnix_ui_language([str(mkvmerge), "-J", str(input_path)])
    result = subprocess.run(
        command,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )

    if result.returncode != 0:
        details = result.stderr.strip() or result.stdout.strip()
        raise OrganizerError(f"Failed to read metadata with mkvmerge -J:\n{details}")

    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError as error:
        raise OrganizerError(f"mkvmerge -J returned invalid JSON: {error}") from error


def build_tracks(metadata: dict[str, Any], source_index: int = 0, source_path: Path | None = None) -> list[TrackInfo]:
    tracks: list[TrackInfo] = []
    source_text = str(source_path) if source_path else ""
    source_name = source_path.name if source_path else ""

    for order, raw_track in enumerate(metadata.get("tracks", [])):
        properties = raw_track.get("properties") or {}
        raw_language = properties.get("language_ietf") or properties.get("language")
        original_name = properties.get("track_name", "") or ""
        language = normalize_language_from_properties(raw_language, original_name)

        track = TrackInfo(
            id=int(raw_track["id"]),
            type=raw_track.get("type", ""),
            codec=raw_track.get("codec", "") or "",
            codec_id=properties.get("codec_id", "") or raw_track.get("codec_id", "") or "",
            language=language,
            output_language=language,
            language_name=language_display_name(language),
            original_name=original_name,
            order=source_index * 100000 + order,
            properties=properties,
            channels=parse_channels(properties.get("audio_channels")),
            default=bool(properties.get("default_track", False)),
            forced=bool(properties.get("forced_track", False)),
            source_index=source_index,
            source_path=source_text,
            source_name=source_name,
        )
        tracks.append(track)

    return tracks


def analyze_subtitle_sizes(input_path: Path, subtitles: list[TrackInfo], mkvextract: Path) -> None:
    with tempfile.TemporaryDirectory(prefix="mkv_subs_") as temp_name:
        temp_dir = Path(temp_name)
        extracted_paths: dict[int, Path] = {}
        command = command_with_mkvtoolnix_ui_language([str(mkvextract), "tracks", str(input_path)])

        for track in subtitles:
            extension = subtitle_extension(track)
            output_path = temp_dir / f"{input_path.stem}.track_{track.id}{extension}"
            extracted_paths[track.id] = output_path
            command.append(f"{track.id}:{output_path}")

        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )

        if result.returncode != 0:
            details = result.stderr.strip() or result.stdout.strip()
            raise OrganizerError(
                "Failed to extract subtitles for analysis:\n"
                f"{format_command(command)}\n{details}"
            )

        for track in subtitles:
            output_path = extracted_paths[track.id]
            size_bytes = extracted_size(output_path)
            text_sample = read_text_sample(output_path) if is_text_subtitle(track) else ""
            text_stats = analyze_subtitle_text(text_sample) if text_sample.strip() else None
            pgs_stats = parse_pgs_sup(output_path) if is_pgs_subtitle(track) else None
            size_class = classify_subtitle_size(track, size_bytes)
            if pgs_stats and pgs_stats.display_events == 0 and pgs_stats.segments:
                size_class = "empty"
            track.analysis = SubtitleAnalysis(
                size_bytes=size_bytes,
                size_class=size_class,
                extraction_command=command,
                text_sample=text_sample,
                text_stats=text_stats,
                pgs=pgs_stats,
            )


def evidence_summary(result: dict[str, Any]) -> str:
    evidence = result.get("evidence") or []
    if not evidence:
        return "no strong evidence"

    return ", ".join(
        f"{variant} {label} x{count}"
        for variant, label, count, _score in evidence[:3]
    )


def subtitle_cache_stem(input_path: Path, track: TrackInfo) -> str:
    return f"{input_path.stem}.track_{track.id}"


def cached_text_subtitle_path(input_path: Path, track: TrackInfo, cache_dir: Path) -> Path | None:
    stem = subtitle_cache_stem(input_path, track)
    candidates = [
        cache_dir / f"{stem}.srt",
        cache_dir / f"{stem}.ass",
        cache_dir / f"{stem}.ssa",
        cache_dir / f"{stem}.txt",
    ]

    for candidate in candidates:
        if candidate.exists():
            return candidate

    return None


def run_pgs_ocr_command(
    command_template: str,
    input_path: Path,
    track: TrackInfo,
    sup_path: Path,
    output_srt_path: Path,
) -> bool:
    try:
        command = command_template.format(
            input=str(sup_path),
            output=str(output_srt_path),
            mkv=str(input_path),
            track_id=track.id,
        )
    except KeyError as error:
        raise OrganizerError(f"Invalid placeholder in --pgs-ocr-command: {{{error.args[0]}}}") from error

    print(f"  OCR PGS track {track.id}: {command}")
    result = subprocess.run(
        command,
        shell=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )

    if result.returncode != 0:
        details = result.stderr.strip() or result.stdout.strip()
        print(f"  Warning: OCR failed on track {track.id} with exit code {result.returncode}: {details}")
        return False

    if not output_srt_path.exists():
        print(f"  Warning: OCR finished but did not create {output_srt_path.name}")
        return False

    return True


def which_path(command_name: str) -> Path | None:
    found = shutil.which(command_name)
    return Path(found) if found else None


def unique_paths(paths: list[Path | None]) -> list[Path]:
    unique: list[Path] = []
    seen: set[str] = set()

    for path in paths:
        if path is None:
            continue
        key = str(path).casefold()
        if key in seen:
            continue
        seen.add(key)
        unique.append(path)

    return unique


def candidate_existing_paths(paths: list[Path]) -> list[Path]:
    return [path for path in paths if path.exists()]


def local_tools_dir() -> Path:
    return APP_DIR / TOOLS_DIR_NAME


def is_under_path(path: Path, base: Path) -> bool:
    try:
        path.resolve().relative_to(base.resolve())
    except ValueError:
        return False
    return True


def is_local_tool_candidate(path: Path) -> bool:
    return is_under_path(path, local_tools_dir()) or is_under_path(
        path,
        Path.cwd() / TOOLS_DIR_NAME,
    )


def tool_env_path(env_var: str) -> Path | None:
    value = os.environ.get(env_var)
    return Path(value).expanduser() if value else None


def common_mkvtoolnix_paths(exe_name: str) -> list[Path]:
    return [
        local_tools_dir() / exe_name,
        local_tools_dir() / "mkvtoolnix" / exe_name,
        local_tools_dir() / "MKVToolNix" / exe_name,
        MKVMERGE.parent / exe_name,
        Path(r"C:\Program Files (x86)\MKVToolNix") / exe_name,
    ]


def common_subtitle_edit_paths() -> list[Path]:
    return [
        local_tools_dir() / "Subtitle Edit" / "SubtitleEdit.exe",
        local_tools_dir() / "SubtitleEdit" / "SubtitleEdit.exe",
        SUBTITLE_EDIT,
        Path(r"C:\Program Files (x86)\Subtitle Edit\SubtitleEdit.exe"),
    ]


def common_tesseract_paths() -> list[Path]:
    return [
        local_tools_dir() / "Tesseract-OCR" / "tesseract.exe",
        TESSERACT,
        Path(r"C:\Program Files (x86)\Tesseract-OCR\tesseract.exe"),
    ]


def common_seconv_paths() -> list[Path]:
    home = Path.home()
    cwd = Path.cwd()
    return [
        local_tools_dir() / "seconv.exe",
        local_tools_dir() / "seconv" / "seconv.exe",
        cwd / TOOLS_DIR_NAME / "seconv.exe",
        cwd / TOOLS_DIR_NAME / "seconv" / "seconv.exe",
        home / "Desktop" / "seconv.exe",
        home / "Desktop" / "SeConv" / "seconv.exe",
        home / "OneDrive" / "Desktop" / "seconv.exe",
        home / "OneDrive" / "Desktop" / "SeConv" / "seconv.exe",
        Path(r"C:\Tools\SeConv\seconv.exe"),
        Path(r"C:\Tools\SubtitleEdit5\seconv.exe"),
        SECONV,
    ]


def resolve_tool_path(
    path: Path | None,
    command_name: str,
    env_var: str,
    fallback_paths: list[Path],
) -> Path | None:
    explicit_or_env = unique_paths([path.expanduser() if path else None, tool_env_path(env_var)])
    for candidate in explicit_or_env:
        if candidate.exists():
            return candidate.resolve()

    fallback_paths = unique_paths(fallback_paths)
    local_paths = candidate_existing_paths([item for item in fallback_paths if is_local_tool_candidate(item)])
    if local_paths:
        return local_paths[0].resolve()

    from_path = which_path(command_name)
    if from_path:
        return from_path.resolve()

    common_paths = candidate_existing_paths([item for item in fallback_paths if not is_local_tool_candidate(item)])
    if common_paths:
        return common_paths[0].resolve()

    return None


def resolve_seconv_path(path: Path | None) -> Path | None:
    resolved = resolve_tool_path(path, "seconv", "SECONV", common_seconv_paths())
    if resolved:
        return resolved

    return None


def detect_tesseract_language(subtitle_edit: Path | None, preferred_language: str) -> str:
    if preferred_language == "auto":
        return "auto"

    if preferred_language != "auto":
        return preferred_language

    candidates = []
    if subtitle_edit:
        candidates.append(subtitle_edit.parent / "Tesseract302" / "tessdata")

    for tessdata_dir in candidates:
        if (tessdata_dir / "por.traineddata").exists():
            return "por"
        if (tessdata_dir / "eng.traineddata").exists():
            return "eng"

    return "por"


def installed_tessdata_dir(tesseract: Path | None) -> Path | None:
    if not tesseract or not tesseract.exists():
        return None

    tessdata_dir = tesseract.parent / "tessdata"
    return tessdata_dir if tessdata_dir.exists() else None


def local_tessdata_dir() -> Path:
    return local_tools_dir() / LOCAL_TESSDATA_DIR_NAME


def available_tesseract_languages(tessdata_dirs: list[Path]) -> set[str]:
    languages: set[str] = set()

    for tessdata_dir in tessdata_dirs:
        if not tessdata_dir.exists():
            continue
        languages.update(path.stem for path in tessdata_dir.glob("*.traineddata"))

    return languages


def tessdata_dir_for_language(language_code: str, tessdata_dirs: list[Path]) -> Path | None:
    for tessdata_dir in tessdata_dirs:
        if (tessdata_dir / f"{language_code}.traineddata").exists():
            return tessdata_dir
    return None


def tesseract_env(
    tesseract: Path | None,
    tessdata_dirs: list[Path],
    language_code: str | None = None,
) -> dict[str, str] | None:
    env = env_with_tool_on_path(tesseract)

    tessdata_dir = tessdata_dir_for_language(language_code, tessdata_dirs) if language_code else None
    if not tessdata_dir:
        return env

    env = dict(os.environ) if env is None else env
    env["TESSDATA_PREFIX"] = str(tessdata_dir)
    return env


def download_tessdata(language_code: str, tessdata_dir: Path, model: str) -> bool:
    base_url = TESSDATA_REPOS.get(model)
    if not base_url:
        raise OrganizerError(f"Invalid tessdata model: {model}. Use best or fast.")

    tessdata_dir.mkdir(parents=True, exist_ok=True)
    output_path = tessdata_dir / f"{language_code}.traineddata"
    if output_path.exists():
        return True

    url = f"{base_url}/{language_code}.traineddata"
    temp_path = output_path.with_suffix(".traineddata.tmp")
    print(f"  Tesseract: downloading {language_code}.traineddata ({model})")

    try:
        urllib.request.urlretrieve(url, temp_path)
        temp_path.replace(output_path)
        return True
    except (urllib.error.URLError, OSError) as error:
        if temp_path.exists():
            temp_path.unlink()
        print(f"  Warning: could not download {language_code}.traineddata: {error}")
        return False


def needs_chinese_script_ocr(track: TrackInfo, preferred_language: str) -> bool:
    return (
        preferred_language == "auto"
        and variant_base_language_key(track) == "chi"
    )


def ocr_language_candidates_for_track(track: TrackInfo, preferred_language: str) -> list[str]:
    if preferred_language != "auto":
        return [preferred_language]

    if needs_chinese_script_ocr(track, preferred_language):
        hinted_variant = language_variant_from_hints("chi", track.output_language, track.language, track.original_name)
        hinted_language = {
            "zh-Hans": "chi_sim",
            "zh-Hant": "chi_tra",
            "zh-TW": "chi_tra",
            "zh-HK": "chi_tra",
        }.get(hinted_variant)
        if hinted_language:
            return [hinted_language] + [
                language for language in CHINESE_OCR_SCRIPT_LANGUAGES
                if language != hinted_language
            ]
        return list(CHINESE_OCR_SCRIPT_LANGUAGES)

    candidate = (
        TESSERACT_LANGUAGE_ALIASES.get(track.output_language)
        or TESSERACT_LANGUAGE_ALIASES.get(track.language)
        or TESSERACT_LANGUAGE_ALIASES.get(variant_base_language_key(track))
        or TESSERACT_LANGUAGE_ALIASES.get(base_language_key(track))
    )
    return [candidate] if candidate else []


def ocr_language_for_track(
    track: TrackInfo,
    preferred_language: str,
    available_languages: set[str],
) -> str | None:
    candidates = ocr_language_candidates_for_track(track, preferred_language)
    if not candidates:
        return None

    candidate = candidates[0]
    if available_languages and candidate not in available_languages:
        return None

    return candidate


def cjk_character_count(text: str) -> int:
    return sum(1 for char in text if "\u4e00" <= char <= "\u9fff")


def chinese_ocr_candidate_quality(language_code: str, srt_path: Path) -> tuple[tuple[int, float, float, int], dict[str, Any]]:
    raw_text = srt_path.read_text(encoding="utf-8", errors="replace")
    result = classify_chinese_variant(raw_text)
    expected_variant = CHINESE_OCR_LANGUAGE_VARIANTS.get(language_code)
    matched_expected = int(result.get("code") == expected_variant)
    return (
        (
            matched_expected,
            variant_total_score(result),
            float(result.get("confidence") or 0.0),
            cjk_character_count(raw_text),
        ),
        result,
    )


def select_chinese_ocr_output(candidates: list[tuple[str, Path]]) -> tuple[str, Path, dict[str, Any]] | None:
    scored: list[tuple[tuple[int, float, float, int], str, Path, dict[str, Any]]] = []

    for language_code, srt_path in candidates:
        if not srt_path.exists():
            continue
        quality, result = chinese_ocr_candidate_quality(language_code, srt_path)
        scored.append((quality, language_code, srt_path, result))

    if not scored:
        return None

    _quality, language_code, srt_path, result = max(scored, key=lambda item: item[0])
    return language_code, srt_path, result


def env_with_tool_on_path(tool_path: Path | None) -> dict[str, str] | None:
    if not tool_path or not tool_path.exists():
        return None

    env = dict(os.environ)
    tool_dir = str(tool_path.parent)
    env["PATH"] = tool_dir + ";" + env.get("PATH", "")
    return env


def run_process_with_timeout(
    command: list[str],
    timeout_seconds: int,
    env: dict[str, str] | None = None,
    cwd: Path | None = None,
    heartbeat_callback: Callable[[float], None] | None = None,
    heartbeat_interval_seconds: float = 10.0,
) -> subprocess.CompletedProcess[str]:
    process = subprocess.Popen(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=env,
        cwd=str(cwd) if cwd else None,
    )
    start_time = time.monotonic()

    while True:
        elapsed = time.monotonic() - start_time
        remaining = timeout_seconds - elapsed
        if remaining <= 0:
            terminate_process_tree(process)
            try:
                stdout, stderr = process.communicate(timeout=5)
            except subprocess.TimeoutExpired as error:
                stdout = error.output or ""
                stderr = error.stderr or ""
            raise subprocess.TimeoutExpired(command, timeout_seconds, output=stdout, stderr=stderr)

        wait_seconds = max(0.1, min(float(heartbeat_interval_seconds), remaining))
        try:
            stdout, stderr = process.communicate(timeout=wait_seconds)
            return subprocess.CompletedProcess(command, process.returncode, stdout, stderr)
        except subprocess.TimeoutExpired:
            elapsed = time.monotonic() - start_time
            if heartbeat_callback:
                try:
                    heartbeat_callback(elapsed)
                except BaseException:
                    terminate_process_tree(process)
                    raise


def terminate_process_tree(process: subprocess.Popen[str]) -> None:
    if os.name == "nt":
        subprocess.run(
            ["taskkill", "/PID", str(process.pid), "/T", "/F"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        return

    process.kill()


def format_elapsed_seconds(seconds: float) -> str:
    total_seconds = max(0, int(seconds))
    minutes, remaining_seconds = divmod(total_seconds, 60)
    if minutes:
        return f"{minutes}m {remaining_seconds:02d}s"
    return f"{remaining_seconds}s"


def console_safe(text: str) -> str:
    encoding = sys.stdout.encoding or "utf-8"
    return text.encode(encoding, errors="replace").decode(encoding, errors="replace")


def process_details(result: subprocess.CompletedProcess[str]) -> str:
    return console_safe((result.stderr.strip() or result.stdout.strip())[:4000])


def write_pgs_display_set_sample(
    sup_path: Path,
    sample_path: Path,
    max_display_sets: int = CHINESE_SCRIPT_OCR_SAMPLE_EVENTS,
) -> int:
    if max_display_sets <= 0:
        return 0

    data = sup_path.read_bytes()
    output = bytearray()
    offset = 0
    display_sets = 0

    while offset + 13 <= len(data):
        if data[offset : offset + 2] != b"PG":
            break

        segment_type = data[offset + 10]
        segment_size = int.from_bytes(data[offset + 11 : offset + 13], "big")
        end = offset + 13 + segment_size
        if end > len(data):
            break

        output.extend(data[offset:end])
        offset = end

        if segment_type == 0x80:
            display_sets += 1
            if display_sets >= max_display_sets:
                break

    if not output or display_sets == 0:
        return 0

    sample_path.parent.mkdir(parents=True, exist_ok=True)
    sample_path.write_bytes(output)
    return display_sets


def normalize_ocr_output(cache_dir: Path, sup_path: Path, expected_srt_path: Path, work_dir: Path) -> bool:
    if expected_srt_path.exists():
        return True

    candidates: list[Path] = []
    for candidate_dir in [cache_dir, sup_path.parent, work_dir]:
        candidates.extend(candidate_dir.glob(f"{sup_path.stem}*.srt"))

    candidates = sorted(set(candidates), key=lambda item: item.stat().st_mtime if item.exists() else 0, reverse=True)
    if not candidates:
        return False

    newest = candidates[0]
    if newest == expected_srt_path:
        return True

    if expected_srt_path.exists():
        expected_srt_path.unlink()
    shutil.move(str(newest), str(expected_srt_path))
    return True


def should_ocr_for_commentary_or_sdh_detection(
    track: TrackInfo,
    max_size: int | None,
    first_full_track_id: int | None,
) -> bool:
    size_bytes = subtitle_size_bytes(track)
    is_large_pgs = (
        is_pgs_subtitle(track)
        and track.analysis
        and track.analysis.size_class == "large"
    )
    if (
        not is_large_pgs
        or has_commentary_flag(track)
        or has_forced_flag(track)
        or has_sdh_subtitle_hint(track)
    ):
        return False
    if not max_size or size_bytes is None or first_full_track_id == track.id:
        return False

    return (size_bytes / max_size) >= FULL_SIZE_DUPLICATE_RATIO


def should_ocr_for_language_variant_detection(track: TrackInfo, variant_counts: dict[str, int]) -> bool:
    base_code = variant_base_language_key(track)
    if not is_pgs_subtitle(track) or base_code not in LANGUAGE_VARIANT_CLASSIFIERS:
        return False
    if track.output_language in LANGUAGE_VARIANT_BASES:
        return base_code in OCR_VALIDATED_VARIANT_LANGUAGES and not subtitle_looks_forced_sidecar(track)
    if base_code in DEFAULT_LANGUAGE_VARIANTS and variant_counts.get(base_code, 0) <= 1:
        return False
    return True


def run_seconv_pgs_ocr(
    seconv: Path,
    sup_path: Path,
    output_srt_path: Path,
    cache_dir: Path,
    ocr_language: str,
    timeout_seconds: int,
    tesseract: Path | None,
    tessdata_dirs: list[Path],
    heartbeat_callback: Callable[[float], None] | None = None,
) -> bool:
    command = [
        str(seconv),
        str(sup_path),
        "subrip",
        f"--ocr-engine:tesseract",
        f"--ocr-language:{ocr_language}",
        "--output-folder",
        str(cache_dir),
        "--output-filename",
        output_srt_path.name,
        "--overwrite",
        "--quiet",
    ]
    print(f"  PGS OCR with seconv: {format_command(command)}")

    try:
        result = run_process_with_timeout(
            command,
            timeout_seconds,
            env=tesseract_env(tesseract, tessdata_dirs, ocr_language),
            cwd=cache_dir,
            heartbeat_callback=heartbeat_callback,
        )
    except subprocess.TimeoutExpired:
        print(f"  Warning: seconv OCR exceeded {timeout_seconds}s on {sup_path.name}")
        return False

    if result.returncode != 0:
        details = process_details(result)
        print(f"  Warning: seconv OCR failed with exit code {result.returncode}: {details}")
        return False

    if not normalize_ocr_output(cache_dir, sup_path, output_srt_path, work_dir=Path.cwd()):
        print(f"  Warning: seconv OCR finished but did not create {output_srt_path.name}")
        return False

    return True


def run_subtitle_edit_pgs_ocr(
    subtitle_edit: Path,
    sup_path: Path,
    output_srt_path: Path,
    cache_dir: Path,
    timeout_seconds: int,
    heartbeat_callback: Callable[[float], None] | None = None,
) -> bool:
    command = [
        str(subtitle_edit),
        "/convert",
        str(sup_path),
        "subrip",
        f"/outputfolder:{cache_dir}",
    ]
    print(f"  OCR PGS via Subtitle Edit: {format_command(command)}")

    try:
        result = run_process_with_timeout(
            command,
            timeout_seconds,
            cwd=cache_dir,
            heartbeat_callback=heartbeat_callback,
        )
    except subprocess.TimeoutExpired:
        print(f"  Warning: Subtitle Edit OCR exceeded {timeout_seconds}s on {sup_path.name}")
        return False

    if result.returncode != 0:
        details = process_details(result)
        print(f"  Warning: Subtitle Edit OCR failed with exit code {result.returncode}: {details}")
        return False

    if not normalize_ocr_output(cache_dir, sup_path, output_srt_path, work_dir=Path.cwd()):
        print(f"  Warning: Subtitle Edit OCR finished but did not create {output_srt_path.name}")
        return False

    return True


def run_automatic_pgs_ocr(
    sup_path: Path,
    output_srt_path: Path,
    cache_dir: Path,
    seconv: Path | None,
    subtitle_edit: Path | None,
    tesseract: Path | None,
    tessdata_dirs: list[Path],
    ocr_language: str,
    timeout_seconds: int,
    allow_legacy_subtitle_edit_ocr: bool,
    heartbeat_callback: Callable[[float], None] | None = None,
) -> bool:
    if seconv:
        return run_seconv_pgs_ocr(
            seconv=seconv,
            sup_path=sup_path,
            output_srt_path=output_srt_path,
            cache_dir=cache_dir,
            ocr_language=ocr_language,
            timeout_seconds=timeout_seconds,
            tesseract=tesseract,
            tessdata_dirs=tessdata_dirs,
            heartbeat_callback=heartbeat_callback,
        )

    if subtitle_edit and allow_legacy_subtitle_edit_ocr:
        return run_subtitle_edit_pgs_ocr(
            subtitle_edit=subtitle_edit,
            sup_path=sup_path,
            output_srt_path=output_srt_path,
            cache_dir=cache_dir,
            timeout_seconds=timeout_seconds,
            heartbeat_callback=heartbeat_callback,
        )

    print(
        "  Warning: automatic OCR is unavailable; seconv.exe was not found. "
        "Install the seconv package/asset or use --pgs-ocr-command."
    )
    return False


def ensure_pgs_ocr_cache(
    input_path: Path,
    subtitles: list[TrackInfo],
    cache_dir: Path,
    mkvextract: Path,
    pgs_ocr_command: str | None,
    auto_pgs_ocr: bool,
    auto_commentary_ocr: bool,
    seconv: Path | None,
    subtitle_edit: Path | None,
    tesseract: Path | None,
    tessdata_dirs: list[Path],
    pgs_ocr_language: str,
    pgs_ocr_timeout_seconds: int,
    allow_legacy_subtitle_edit_ocr: bool,
    auto_download_tessdata: bool,
    tessdata_model: str,
    force_pgs_ocr: bool,
    progress_callback: Callable[[str, int, int], None] | None = None,
    cancel_callback: Callable[[], bool] | None = None,
) -> None:
    if not pgs_ocr_command and not auto_pgs_ocr:
        return

    max_sizes = max_subtitle_sizes_by_language(subtitles)
    first_full_tracks = first_full_sized_track_by_language(subtitles, max_sizes)
    variant_counts = count_subtitles_by_variant_base_language(subtitles)
    ocr_targets: dict[int, TrackInfo] = {}
    for track in subtitles:
        if should_ocr_for_language_variant_detection(track, variant_counts):
            ocr_targets[track.id] = track

    if auto_commentary_ocr:
        for track in subtitles:
            language_key = base_language_key(track)
            if should_ocr_for_commentary_or_sdh_detection(
                track,
                max_sizes.get(language_key),
                first_full_tracks.get(language_key),
            ):
                ocr_targets[track.id] = track

    if not ocr_targets:
        return

    cache_dir.mkdir(parents=True, exist_ok=True)
    local_dir = local_tessdata_dir()
    available_languages = available_tesseract_languages(tessdata_dirs + [local_dir])

    ocr_target_items = sorted(ocr_targets.values(), key=lambda item: item.order)
    for ocr_index, track in enumerate(ocr_target_items, start=1):
        ensure_not_cancelled(cancel_callback)
        def ocr_heartbeat(ocr_language: str) -> Callable[[float], None]:
            def heartbeat(elapsed_seconds: float) -> None:
                ensure_not_cancelled(cancel_callback)
                if progress_callback:
                    progress_callback(
                        (
                            f"OCR PGS track {track.id} ({ocr_index}/{len(ocr_target_items)}) "
                            f"with {ocr_language}: "
                            f"{format_elapsed_seconds(elapsed_seconds)} / "
                            f"{format_elapsed_seconds(pgs_ocr_timeout_seconds)}"
                        ),
                        ocr_index,
                        len(ocr_target_items),
                    )

            return heartbeat

        if progress_callback:
            progress_callback(
                f"OCR PGS track {track.id} ({ocr_index}/{len(ocr_target_items)})",
                ocr_index,
                len(ocr_target_items),
            )
        if track.analysis and (
            track.analysis.size_class == "empty"
            or bool(track.analysis.pgs and track.analysis.pgs.display_events == 0)
        ):
            print(f"  OCR PGS track {track.id}: skip, empty track")
            continue

        ocr_languages = ocr_language_candidates_for_track(track, pgs_ocr_language)
        if auto_pgs_ocr and not pgs_ocr_command:
            for ocr_language in list(ocr_languages):
                if (
                    ocr_language not in available_languages
                    and auto_download_tessdata
                    and download_tessdata(ocr_language, local_dir, tessdata_model)
                ):
                    tessdata_dirs = [local_dir] + tessdata_dirs
                    available_languages.add(ocr_language)

            if available_languages:
                ocr_languages = [
                    ocr_language for ocr_language in ocr_languages
                    if ocr_language in available_languages
                ]

        if auto_pgs_ocr and not pgs_ocr_command and not ocr_languages:
            print(
                f"  OCR PGS track {track.id}: skip, missing Tesseract traineddata for "
                f"{track.language_name} ({track.output_language})"
            )
            continue

        cached_text = cached_text_subtitle_path(input_path, track, cache_dir)
        if cached_text and not force_pgs_ocr:
            continue

        sup_path = cache_dir / f"{subtitle_cache_stem(input_path, track)}.sup"
        output_srt_path = cache_dir / f"{subtitle_cache_stem(input_path, track)}.srt"

        if force_pgs_ocr and output_srt_path.exists():
            output_srt_path.unlink()

        if output_srt_path.exists() and not force_pgs_ocr:
            continue

        if force_pgs_ocr or not sup_path.exists():
            command = command_with_mkvtoolnix_ui_language(
                [str(mkvextract), "tracks", str(input_path), f"{track.id}:{sup_path}"]
            )
            result = subprocess.run(
                command,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
            )
            if result.returncode != 0:
                details = result.stderr.strip() or result.stdout.strip()
                print(
                    f"  Warning: could not extract PGS track {track.id} for OCR:\n"
                    f"  {format_command(command)}\n  {details}"
                )
                continue

        ocr_sup_path = sup_path
        use_chinese_script_sample = (
            auto_pgs_ocr
            and not pgs_ocr_command
            and len(ocr_languages) > 1
            and needs_chinese_script_ocr(track, pgs_ocr_language)
        )
        if use_chinese_script_sample:
            sample_sup_path = cache_dir / f"{sup_path.stem}.sample_{CHINESE_SCRIPT_OCR_SAMPLE_EVENTS}.sup"
            if force_pgs_ocr and sample_sup_path.exists():
                sample_sup_path.unlink()
            if not sample_sup_path.exists():
                try:
                    sampled_events = write_pgs_display_set_sample(sup_path, sample_sup_path)
                except OSError as error:
                    sampled_events = 0
                    print(f"  Warning: could not create PGS OCR sample for track {track.id}: {error}")
                if sampled_events:
                    print(
                        f"  OCR PGS track {track.id}: using {sampled_events} event sample "
                        f"for Chinese script detection"
                    )
                elif sample_sup_path.exists():
                    sample_sup_path.unlink()
            if sample_sup_path.exists():
                ocr_sup_path = sample_sup_path

        if pgs_ocr_command:
            run_pgs_ocr_command(
                command_template=pgs_ocr_command,
                input_path=input_path,
                track=track,
                sup_path=sup_path,
                output_srt_path=output_srt_path,
            )
        elif auto_pgs_ocr and len(ocr_languages) > 1 and needs_chinese_script_ocr(track, pgs_ocr_language):
            candidates: list[tuple[str, Path]] = []
            selected: tuple[str, Path, dict[str, Any]] | None = None
            for ocr_language in ocr_languages:
                language_cache_dir = cache_dir / f"_ocr_track_{track.id}_{ocr_language}"
                language_cache_dir.mkdir(parents=True, exist_ok=True)
                candidate_srt_path = language_cache_dir / f"{sup_path.stem}.{ocr_language}.srt"
                if force_pgs_ocr and candidate_srt_path.exists():
                    candidate_srt_path.unlink()
                if force_pgs_ocr or not candidate_srt_path.exists():
                    run_automatic_pgs_ocr(
                        sup_path=ocr_sup_path,
                        output_srt_path=candidate_srt_path,
                        cache_dir=language_cache_dir,
                        seconv=seconv,
                        subtitle_edit=subtitle_edit,
                        tesseract=tesseract,
                        tessdata_dirs=tessdata_dirs,
                        ocr_language=ocr_language,
                        timeout_seconds=pgs_ocr_timeout_seconds,
                        allow_legacy_subtitle_edit_ocr=allow_legacy_subtitle_edit_ocr,
                        heartbeat_callback=ocr_heartbeat(ocr_language),
                    )
                candidates.append((ocr_language, candidate_srt_path))

                if candidate_srt_path.exists():
                    _quality, result = chinese_ocr_candidate_quality(ocr_language, candidate_srt_path)
                    expected_variant = CHINESE_OCR_LANGUAGE_VARIANTS.get(ocr_language)
                    if (
                        result.get("code") == expected_variant
                        and reliable_variant_code_from_result(result, "chi")
                    ):
                        selected = (ocr_language, candidate_srt_path, result)
                        break

            if selected is None:
                selected = select_chinese_ocr_output(candidates)
            if selected:
                selected_language, selected_path, selected_result = selected
                shutil.copyfile(selected_path, output_srt_path)
                print(
                    f"  OCR PGS track {track.id}: selected {selected_language} "
                    f"({selected_result['reason']}; {evidence_summary(selected_result)})"
                )
        elif auto_pgs_ocr and ocr_languages:
            run_automatic_pgs_ocr(
                sup_path=sup_path,
                output_srt_path=output_srt_path,
                cache_dir=cache_dir,
                seconv=seconv,
                subtitle_edit=subtitle_edit,
                tesseract=tesseract,
                tessdata_dirs=tessdata_dirs,
                ocr_language=ocr_languages[0],
                timeout_seconds=pgs_ocr_timeout_seconds,
                allow_legacy_subtitle_edit_ocr=allow_legacy_subtitle_edit_ocr,
                heartbeat_callback=ocr_heartbeat(ocr_languages[0]),
            )


def extract_pgs_for_manual_ocr(
    input_path: Path,
    subtitles: list[TrackInfo],
    cache_dir: Path,
    mkvextract: Path,
) -> None:
    pgs_variant_subtitles = [
        track for track in subtitles
        if variant_base_language_key(track) in LANGUAGE_VARIANT_CLASSIFIERS and is_pgs_subtitle(track)
    ]

    if not pgs_variant_subtitles:
        print("OCR PGS: no relevant PGS subtitle variants to prepare.")
        return

    cache_dir.mkdir(parents=True, exist_ok=True)
    print(f"OCR PGS: files prepared in {cache_dir}")

    for track in pgs_variant_subtitles:
        sup_path = cache_dir / f"{subtitle_cache_stem(input_path, track)}.sup"
        expected_srt_path = cache_dir / f"{subtitle_cache_stem(input_path, track)}.srt"

        if not sup_path.exists():
            command = command_with_mkvtoolnix_ui_language(
                [str(mkvextract), "tracks", str(input_path), f"{track.id}:{sup_path}"]
            )
            result = subprocess.run(
                command,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
            )

            if result.returncode != 0:
                details = result.stderr.strip() or result.stdout.strip()
                print(
                    f"  track {track.id}: extraction for OCR failed:\n"
                    f"  {format_command(command)}\n  {details}"
                )
                continue

        status = "SRT already exists" if expected_srt_path.exists() else "SRT still needs OCR"
        print(f"  track {track.id}: {sup_path.name} -> {expected_srt_path.name} ({status})")


def variant_score_summary(result: dict[str, Any]) -> str:
    scores = result.get("scores") or {}
    return " ".join(f"{code}={score}" for code, score in scores.items())


def variant_total_score(result: dict[str, Any] | None) -> float:
    if not result:
        return 0.0
    scores = result.get("scores") or {}
    return float(sum(score for score in scores.values() if isinstance(score, (int, float))))


def is_language_variant_for_base(code: str | None, base_code: str) -> bool:
    return bool(code and LANGUAGE_VARIANT_BASES.get(code) == base_code)


def reliable_variant_code_from_result(result: dict[str, Any] | None, base_code: str) -> str | None:
    if not result:
        return None

    code = result.get("code")
    if not is_language_variant_for_base(code, base_code):
        return None
    if float(result.get("confidence") or 0.0) < 0.65:
        return None
    if variant_total_score(result) < VARIANT_ANCHOR_MIN_SCORE:
        return None
    return code


def variant_result_strong_enough_to_override_metadata(result: dict[str, Any], base_code: str) -> bool:
    if not reliable_variant_code_from_result(result, base_code):
        return False
    if variant_total_score(result) < VARIANT_METADATA_OVERRIDE_MIN_SCORE:
        return False
    return float(result.get("confidence") or 0.0) >= VARIANT_METADATA_OVERRIDE_MIN_CONFIDENCE


def explicit_variant_hint_for_track(track: TrackInfo, base_code: str) -> str | None:
    if is_language_variant_for_base(track.language, base_code):
        return track.language

    return explicit_variant_name_hint_for_track(track, base_code)


def explicit_variant_name_hint_for_track(track: TrackInfo, base_code: str) -> str | None:
    hinted_variant = language_variant_from_hints(base_code, track.original_name)
    if is_language_variant_for_base(hinted_variant, base_code):
        return hinted_variant
    return None


def subtitle_looks_forced_sidecar(track: TrackInfo, max_pgs_events: float | None = None) -> bool:
    if has_forced_flag(track) or detect_subtitle_role_hint(track) == "forced":
        return True

    analysis = track.analysis
    if not analysis:
        return False

    if analysis.size_class in {"empty", "small"}:
        return True

    pgs = pgs_stats(track)
    if not pgs:
        return False

    if pgs.display_events <= 80:
        return True
    if max_pgs_events and pgs.display_events < max_pgs_events:
        return (pgs.display_events / max_pgs_events) <= PGS_WEAK_FORCED_EVENT_RATIO
    return False


def subtitle_looks_short_or_forced(
    track: TrackInfo,
    max_size: int | None = None,
    max_pgs_events: float | None = None,
) -> bool:
    if subtitle_looks_forced_sidecar(track, max_pgs_events):
        return True

    size_bytes = subtitle_size_bytes(track)
    if max_size and size_bytes is not None and size_bytes < max_size:
        return (size_bytes / max_size) <= FORCED_WEAK_SIZE_RATIO
    return False


def subtitle_looks_full_variant_anchor(track: TrackInfo, max_size: int | None, max_pgs_events: float | None) -> bool:
    if subtitle_looks_short_or_forced(track, max_size, max_pgs_events):
        return False

    analysis = track.analysis
    if not analysis or analysis.size_class != "large":
        return False

    size_bytes = subtitle_size_bytes(track)
    if max_size and size_bytes is not None:
        return (size_bytes / max_size) >= FULL_SIZE_DUPLICATE_RATIO
    return True


def variant_result_is_weak_for_pairing(result: dict[str, Any] | None, base_code: str) -> bool:
    if not result:
        return True
    if not reliable_variant_code_from_result(result, base_code):
        return True
    if variant_total_score(result) < VARIANT_SHORT_EVIDENCE_MAX_SCORE:
        return True
    return float(result.get("confidence") or 0.0) < VARIANT_SHORT_EVIDENCE_MIN_CONFIDENCE


def language_variant_vote_for_track(track: TrackInfo, result: dict[str, Any] | None) -> str | None:
    base_code = variant_base_language_key(track)
    if base_code not in LANGUAGE_VARIANT_CLASSIFIERS:
        return None

    explicit_code = explicit_variant_hint_for_track(track, base_code)
    result_code = reliable_variant_code_from_result(result, base_code)
    if result_code:
        if explicit_code and explicit_code != result_code:
            return result_code if variant_result_strong_enough_to_override_metadata(result, base_code) else explicit_code
        return result_code
    return explicit_code


def batch_language_variant_consensus_from_votes(votes: dict[str, list[str]]) -> dict[str, str]:
    consensus: dict[str, str] = {}

    for base_code, codes in votes.items():
        if len(codes) < 2:
            continue

        counts: dict[str, int] = {}
        for code in codes:
            counts[code] = counts.get(code, 0) + 1

        winner_code, winner_count = max(counts.items(), key=lambda item: item[1])
        tied_winners = [code for code, count in counts.items() if count == winner_count]
        if len(tied_winners) != 1:
            continue
        if winner_count < 2 or winner_count <= len(codes) / 2:
            continue
        consensus[base_code] = winner_code

    return consensus


def variant_detection_result(
    input_path: Path,
    track: TrackInfo,
    cache_dir: Path,
) -> tuple[dict[str, Any] | None, str]:
    base_code = variant_base_language_key(track)
    classifier = LANGUAGE_VARIANT_CLASSIFIERS.get(base_code)
    file_classifier = LANGUAGE_VARIANT_FILE_CLASSIFIERS.get(base_code)
    if not classifier or not file_classifier:
        return None, ""

    if track.analysis and track.analysis.text_sample.strip():
        return classifier(track.analysis.text_sample), "extracted text"

    cached_path = cached_text_subtitle_path(input_path, track, cache_dir)
    if cached_path:
        return file_classifier(cached_path), f"cache {cached_path.name}"

    return None, ""


def apply_language_variant_result(track: TrackInfo, result: dict[str, Any]) -> None:
    code = result.get("code") or variant_base_language_key(track)
    base_code = variant_base_language_key(track)
    if (
        code == base_code
        and track.output_language != base_code
        and track.output_language not in LANGUAGE_VARIANT_BASES
    ):
        return
    if track.output_language in CHINESE_TRADITIONAL_REGIONAL_VARIANTS and code == "zh-Hant":
        return
    if (
        code not in LANGUAGE_VARIANT_BASES
        and track.output_language in LANGUAGE_VARIANT_BASES
    ):
        if base_code in OCR_VALIDATED_VARIANT_LANGUAGES and variant_total_score(result) == 0:
            track.output_language = base_code
            track.language_name = language_display_name(base_code)
        return
    if (
        is_language_variant_for_base(track.output_language, base_code)
        and is_language_variant_for_base(code, base_code)
        and track.output_language != code
        and not variant_result_strong_enough_to_override_metadata(result, base_code)
    ):
        return
    track.output_language = code
    track.language_name = language_display_name(code)


def reconcile_short_language_variants(
    subtitles: list[TrackInfo],
    variant_results: dict[int, dict[str, Any]],
) -> list[tuple[TrackInfo, str, TrackInfo, str]]:
    max_sizes = max_subtitle_sizes_by_language(subtitles)
    max_pgs_events = max_pgs_metric_by_language(subtitles, "display_events")
    tracks_by_base: dict[str, list[TrackInfo]] = {}
    changes: list[tuple[TrackInfo, str, TrackInfo, str]] = []

    for track in subtitles:
        base_code = variant_base_language_key(track)
        if base_code in LANGUAGE_VARIANT_CLASSIFIERS:
            tracks_by_base.setdefault(base_code, []).append(track)

    for base_code, language_tracks in tracks_by_base.items():
        max_size = max_sizes.get(base_code)
        max_events = max_pgs_events.get(base_code)
        anchors: list[tuple[TrackInfo, str]] = []

        for track in language_tracks:
            if not subtitle_looks_full_variant_anchor(track, max_size, max_events):
                continue

            result_code = reliable_variant_code_from_result(variant_results.get(track.id), base_code)
            explicit_code = explicit_variant_hint_for_track(track, base_code)
            anchor_code = result_code or explicit_code
            if explicit_code in CHINESE_TRADITIONAL_REGIONAL_VARIANTS and result_code == "zh-Hant":
                anchor_code = explicit_code
            if anchor_code:
                anchors.append((track, anchor_code))

        anchor_codes = {code for _track, code in anchors}
        if len(anchor_codes) != 1:
            continue

        anchor_code = next(iter(anchor_codes))
        anchor_track = min((track for track, _code in anchors), key=lambda item: item.order)
        anchor_ids = {track.id for track, _code in anchors}

        for track in language_tracks:
            if track.id in anchor_ids:
                continue
            if not subtitle_looks_short_or_forced(track, max_size, max_events):
                continue

            result = variant_results.get(track.id)
            result_code = reliable_variant_code_from_result(result, base_code)
            if (
                result_code
                and result_code != anchor_code
                and not variant_result_is_weak_for_pairing(result, base_code)
            ):
                continue

            previous_language = track.output_language
            if previous_language == anchor_code:
                continue
            if previous_language in CHINESE_TRADITIONAL_REGIONAL_VARIANTS and anchor_code == "zh-Hant":
                continue

            track.output_language = anchor_code
            track.language_name = language_display_name(anchor_code)
            changes.append((track, previous_language, anchor_track, anchor_code))

    return changes


def apply_batch_language_variant_consensus(
    subtitles: list[TrackInfo],
    variant_results: dict[int, dict[str, Any]],
    batch_variant_consensus: dict[str, str] | None,
) -> list[tuple[TrackInfo, str, str]]:
    if not batch_variant_consensus:
        return []

    changes: list[tuple[TrackInfo, str, str]] = []

    for track in subtitles:
        base_code = variant_base_language_key(track)
        consensus_code = batch_variant_consensus.get(base_code)
        if not consensus_code or track.output_language == consensus_code:
            continue
        if track.output_language in CHINESE_TRADITIONAL_REGIONAL_VARIANTS and consensus_code == "zh-Hant":
            continue

        result = variant_results.get(track.id)
        result_code = reliable_variant_code_from_result(result, base_code)
        explicit_name_code = explicit_variant_name_hint_for_track(track, base_code)
        if explicit_name_code and explicit_name_code != consensus_code:
            continue
        if (
            result_code
            and result_code != consensus_code
            and result is not None
            and variant_result_strong_enough_to_override_metadata(result, base_code)
        ):
            continue

        previous_language = track.output_language
        track.output_language = consensus_code
        track.language_name = language_display_name(consensus_code)
        changes.append((track, previous_language, consensus_code))

    return changes


def detect_language_variants(
    input_path: Path,
    subtitles: list[TrackInfo],
    cache_dir: Path,
    batch_variant_consensus: dict[str, str] | None = None,
) -> None:
    variant_subtitles = [
        track
        for track in subtitles
        if variant_base_language_key(track) in LANGUAGE_VARIANT_CLASSIFIERS
    ]
    if not variant_subtitles:
        return

    print(f"Variant cache: {cache_dir}")
    variant_results: dict[int, dict[str, Any]] = {}

    for track in variant_subtitles:
        result, source = variant_detection_result(input_path, track, cache_dir)

        if result is None:
            if track.output_language not in LANGUAGE_VARIANT_BASES:
                print(
                    f"  track {track.id}: no extracted/cache text for "
                    f"{variant_base_language_key(track).upper()} "
                    "(for PGS/SUP, use --pgs-ocr-command or place OCR .srt files in _ocr_cache)"
                )
            continue

        variant_results[track.id] = result
        if variant_base_language_key(track) == "por":
            track.pt_variant = result
        apply_language_variant_result(track, result)
        print(
            f"  track {track.id}: {track.language_name} "
            f"confidence={result['confidence']:.2f} "
            f"scores {variant_score_summary(result)} "
            f"via {source} "
            f"({result['reason']}; {evidence_summary(result)})"
        )

    for track, previous_language, anchor_track, anchor_code in reconcile_short_language_variants(
        variant_subtitles,
        variant_results,
    ):
        print(
            f"  track {track.id}: {language_display_name(previous_language)} -> "
            f"{language_display_name(anchor_code)} "
            f"(aligned with track {anchor_track.id}; short/forced track with little direct evidence)"
        )

    for track, previous_language, consensus_code in apply_batch_language_variant_consensus(
        variant_subtitles,
        variant_results,
        batch_variant_consensus,
    ):
        print(
            f"  track {track.id}: {language_display_name(previous_language)} -> "
            f"{language_display_name(consensus_code)} "
            "(batch consensus; weak direct evidence or metadata only)"
        )


def count_subtitles_by_base_language(subtitles: list[TrackInfo]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for track in subtitles:
        base_code = base_language_key(track)
        counts[base_code] = counts.get(base_code, 0) + 1
    return counts


def count_subtitles_by_variant_base_language(subtitles: list[TrackInfo]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for track in subtitles:
        base_code = variant_base_language_key(track)
        counts[base_code] = counts.get(base_code, 0) + 1
    return counts


def apply_default_language_variants(subtitles: list[TrackInfo]) -> None:
    counts = count_subtitles_by_base_language(subtitles)

    for track in subtitles:
        base_code = base_language_key(track)
        if counts.get(base_code, 0) != 1:
            continue
        if track.output_language in LANGUAGE_VARIANT_BASES:
            continue

        default_variant = DEFAULT_LANGUAGE_VARIANTS.get(base_code)
        if not default_variant:
            continue

        track.output_language = default_variant
        track.language_name = language_display_name(default_variant)


def apply_ordered_pgs_language_variants(subtitles: list[TrackInfo]) -> None:
    tracks_by_base: dict[str, list[TrackInfo]] = {}
    for track in subtitles:
        tracks_by_base.setdefault(base_language_key(track), []).append(track)

    max_pgs_events = max_pgs_metric_by_language(subtitles, "display_events")
    for base_code, variant_order in ORDERED_PGS_LANGUAGE_VARIANTS.items():
        language_tracks = sorted(tracks_by_base.get(base_code, []), key=lambda item: item.order)
        if len(language_tracks) != len(variant_order):
            continue
        if any(not is_pgs_subtitle(track) for track in language_tracks):
            continue
        if any(track.output_language in LANGUAGE_VARIANT_BASES for track in language_tracks):
            continue
        if any(subtitle_looks_forced_sidecar(track, max_pgs_events.get(base_code)) for track in language_tracks):
            continue

        for track, variant_code in zip(language_tracks, variant_order):
            track.output_language = variant_code
            track.language_name = language_display_name(variant_code)


def attach_cached_ocr_text(input_path: Path, subtitles: list[TrackInfo], cache_dir: Path) -> None:
    for track in subtitles:
        if not track.analysis:
            continue
        if track.analysis.text_sample.strip():
            if track.analysis.text_stats is None:
                track.analysis.text_stats = analyze_subtitle_text(track.analysis.text_sample)
            continue

        cached_path = cached_text_subtitle_path(input_path, track, cache_dir)
        if not cached_path:
            continue

        text_sample = read_text_sample(cached_path)
        if not text_sample.strip():
            continue

        track.analysis.text_sample = text_sample
        track.analysis.text_stats = analyze_subtitle_text(text_sample)


def subtitle_size_bytes(track: TrackInfo) -> int | None:
    return track.analysis.size_bytes if track.analysis else None


def max_subtitle_sizes_by_language(subtitles: list[TrackInfo]) -> dict[str, int]:
    max_sizes: dict[str, int] = {}

    for track in subtitles:
        size_bytes = subtitle_size_bytes(track)
        if size_bytes is None:
            continue

        language_key = base_language_key(track)
        max_sizes[language_key] = max(size_bytes, max_sizes.get(language_key, 0))

    return max_sizes


def pgs_stats(track: TrackInfo) -> PgsStats | None:
    return track.analysis.pgs if track.analysis else None


def text_stats(track: TrackInfo) -> SubtitleTextStats | None:
    return track.analysis.text_stats if track.analysis else None


def sequence_timing_similarity(left: list[float], right: list[float], tolerance_seconds: float) -> float:
    if not left or not right:
        return 0.0

    left = sorted(left)
    right = sorted(right)
    i = 0
    j = 0
    matches = 0

    while i < len(left) and j < len(right):
        delta = left[i] - right[j]
        if abs(delta) <= tolerance_seconds:
            matches += 1
            i += 1
            j += 1
        elif delta < 0:
            i += 1
        else:
            j += 1

    return matches / max(len(left), len(right))


def pgs_timeline_similarity(left: PgsStats | None, right: PgsStats | None) -> float | None:
    if not left or not right or not left.events or not right.events:
        return None

    left_starts = [event.start for event in left.events]
    right_starts = [event.start for event in right.events]
    return sequence_timing_similarity(left_starts, right_starts, PGS_TIMELINE_MATCH_TOLERANCE_SECONDS)


def token_similarity(left: set[str], right: set[str]) -> float | None:
    if not left or not right:
        return None
    return len(left & right) / len(left | right)


def max_pgs_metric_by_language(subtitles: list[TrackInfo], metric: str) -> dict[str, float]:
    max_values: dict[str, float] = {}

    for track in subtitles:
        stats = pgs_stats(track)
        if not stats:
            continue

        value = float(getattr(stats, metric))
        language_key = base_language_key(track)
        max_values[language_key] = max(value, max_values.get(language_key, 0.0))

    return max_values


def first_full_sized_track_by_language(
    subtitles: list[TrackInfo],
    max_sizes: dict[str, int],
) -> dict[str, int]:
    first_tracks: dict[str, int] = {}

    for track in sorted(subtitles, key=lambda item: item.order):
        size_bytes = subtitle_size_bytes(track)
        if size_bytes is None:
            continue

        language_key = base_language_key(track)
        max_size = max_sizes.get(language_key, 0)
        if not max_size:
            continue

        if size_bytes / max_size >= FULL_SIZE_DUPLICATE_RATIO:
            first_tracks.setdefault(language_key, track.id)

    return first_tracks


def first_short_subtitle_order_by_language(subtitles: list[TrackInfo]) -> dict[str, int]:
    first_orders: dict[str, int] = {}

    for track in sorted(subtitles, key=lambda item: item.order):
        analysis = track.analysis
        if not analysis:
            continue

        pgs = analysis.pgs
        is_short = analysis.size_class in {"empty", "small"} or bool(pgs and pgs.display_events <= 80)
        if not is_short:
            continue

        language_key = base_language_key(track)
        first_orders.setdefault(language_key, track.order)

    return first_orders


def primary_track_by_language(
    subtitles: list[TrackInfo],
    first_full_tracks: dict[str, int],
) -> dict[str, TrackInfo]:
    by_id = {track.id: track for track in subtitles}
    primary: dict[str, TrackInfo] = {}

    for language_key, track_id in first_full_tracks.items():
        track = by_id.get(track_id)
        if track:
            primary[language_key] = track

    return primary


def comparison_to_primary_by_track(
    subtitles: list[TrackInfo],
    first_full_tracks: dict[str, int],
) -> tuple[dict[int, float], dict[int, float]]:
    primary_by_language = primary_track_by_language(subtitles, first_full_tracks)
    timeline_similarity_by_track: dict[int, float] = {}
    text_similarity_by_track: dict[int, float] = {}

    for track in subtitles:
        language_key = base_language_key(track)
        primary = primary_by_language.get(language_key)
        if not primary or primary.id == track.id:
            continue

        timeline_similarity = pgs_timeline_similarity(pgs_stats(primary), pgs_stats(track))
        if timeline_similarity is not None:
            timeline_similarity_by_track[track.id] = timeline_similarity

        primary_text = text_stats(primary)
        track_text = text_stats(track)
        if primary_text and track_text and primary_text.word_count >= 80 and track_text.word_count >= 80:
            text_similarity = token_similarity(primary_text.tokens, track_text.tokens)
            if text_similarity is not None:
                text_similarity_by_track[track.id] = text_similarity

    return timeline_similarity_by_track, text_similarity_by_track


def structural_commentary_candidates(subtitles: list[TrackInfo]) -> dict[int, str]:
    candidates: dict[int, str] = {}

    for language_key in sorted({base_language_key(track) for track in subtitles}):
        language_tracks = [
            track
            for track in sorted(subtitles, key=lambda item: item.order)
            if base_language_key(track) == language_key
        ]
        candidate_reasons: dict[int, str] = {}

        for track in language_tracks:
            pgs = pgs_stats(track)
            size_bytes = subtitle_size_bytes(track)
            if not pgs or size_bytes is None or pgs.display_events < 250:
                continue

            previous_tracks = [
                previous
                for previous in language_tracks
                if previous.order < track.order
                and subtitle_size_bytes(previous) is not None
                and pgs_stats(previous)
                and pgs_stats(previous).display_events >= 150
            ]
            if not previous_tracks:
                continue

            baseline = min(previous_tracks, key=lambda item: item.order)
            baseline_pgs = pgs_stats(baseline)
            baseline_size = subtitle_size_bytes(baseline)
            if not baseline_pgs or not baseline_size:
                continue

            event_ratio = pgs.display_events / max(1, baseline_pgs.display_events)
            size_ratio = size_bytes / max(1, baseline_size)
            payload_ratio = (
                pgs.ods_payload_bytes / max(1, baseline_pgs.ods_payload_bytes)
                if baseline_pgs.ods_payload_bytes
                else 0.0
            )

            if event_ratio >= 1.55 and (size_ratio >= 1.45 or payload_ratio >= 1.45):
                candidate_reasons[track.id] = (
                    f"commentary structure: {event_ratio:.2f}x events and "
                    f"{size_ratio:.2f}x size vs track {baseline.id}"
                )

        if len(candidate_reasons) >= 1:
            candidates.update(candidate_reasons)

    if len(candidates) >= 2:
        return candidates

    return {
        track_id: reason
        for track_id, reason in candidates.items()
        if (text_stats(next(track for track in subtitles if track.id == track_id)) or SubtitleTextStats()).commentary_score >= 80
    }


def add_role_score(score: SubtitleRoleScore, kind: str, points: int, reason: str) -> None:
    if kind == "forced":
        score.forced += points
        score.forced_evidence.append(f"+{points} {reason}")
    elif kind == "commentary":
        score.commentary += points
        score.commentary_evidence.append(f"+{points} {reason}")
    elif kind == "sdh":
        score.sdh += points
        score.sdh_evidence.append(f"+{points} {reason}")


def score_subtitle_role(
    track: TrackInfo,
    forced_subtitle_ids: set[int],
    smart_sub_detection: bool,
    max_sizes: dict[str, int],
    max_pgs_events: dict[str, float],
    max_pgs_display_seconds: dict[str, float],
    max_pgs_payload_bytes: dict[str, float],
    first_full_tracks: dict[str, int],
    first_short_orders: dict[str, int],
    timeline_similarity_by_track: dict[int, float],
    text_similarity_by_track: dict[int, float],
    commentary_audio_languages: set[str],
    structural_commentary_by_track: dict[int, str],
) -> SubtitleRoleScore:
    score = SubtitleRoleScore()
    role_hint = detect_subtitle_role_hint(track)
    size_class = track.analysis.size_class if track.analysis else "unknown"
    size_bytes = subtitle_size_bytes(track)
    pgs = pgs_stats(track)
    language_key = base_language_key(track)
    max_size = max_sizes.get(language_key, 0)
    size_ratio = (size_bytes / max_size) if size_bytes is not None and max_size else None
    max_events = max_pgs_events.get(language_key, 0.0)
    max_display_seconds = max_pgs_display_seconds.get(language_key, 0.0)
    max_payload_bytes = max_pgs_payload_bytes.get(language_key, 0.0)
    timeline_similarity = timeline_similarity_by_track.get(track.id)
    text_similarity = text_similarity_by_track.get(track.id)
    track_text_stats = text_stats(track)

    if track.id in forced_subtitle_ids:
        add_role_score(score, "forced", 100, "ID provided in --forced-subtitle-ids")
    if has_forced_flag(track):
        add_role_score(score, "forced", 100, "forced flag already present in MKV")
    if has_hearing_impaired_flag(track):
        add_role_score(score, "sdh", 100, "hearing impaired flag already present in MKV")
    if has_commentary_flag(track):
        add_role_score(score, "commentary", 100, "commentary flag already present in MKV")
    if track.id in structural_commentary_by_track:
        add_role_score(score, "commentary", 90, structural_commentary_by_track[track.id])
    if role_hint == "forced" and not has_forced_flag(track):
        add_role_score(score, "forced", 90, "original name suggests forced")
    if role_hint == "commentary" and not has_commentary_flag(track):
        add_role_score(score, "commentary", 100, "original name suggests commentary")
    if role_hint == "sdh" and not has_hearing_impaired_flag(track):
        add_role_score(score, "sdh", 100, "original name suggests SDH/CC")

    if track_text_stats and track_text_stats.sdh_score >= 40:
        add_role_score(
            score,
            "sdh",
            min(80, track_text_stats.sdh_score),
            f"text contains SDH markers ({summarize_role_evidence(track_text_stats.sdh_evidence)})",
        )

    if smart_sub_detection:
        if size_class == "empty":
            add_role_score(score, "forced", 80, "empty/almost empty size")
        elif size_class == "small":
            add_role_score(score, "forced", 65, "small absolute size")

        if size_ratio is not None and size_ratio < 1:
            if size_ratio <= FORCED_STRONG_SIZE_RATIO:
                add_role_score(
                    score,
                    "forced",
                    45,
                    f"much smaller than the largest subtitle in the language ({size_ratio:.2f}x)",
                )
            elif size_ratio <= FORCED_WEAK_SIZE_RATIO:
                add_role_score(
                    score,
                    "forced",
                    25,
                    f"smaller than the largest subtitle in the language ({size_ratio:.2f}x)",
                )

        if pgs:
            if pgs.parse_errors:
                score.forced_evidence.append(f"PGS parse errors={pgs.parse_errors}")

            if pgs.segments and pgs.display_events == 0:
                add_role_score(score, "forced", 95, "PGS has no presentation events")
            elif pgs.display_events <= 8:
                add_role_score(score, "forced", 85, f"PGS has only {pgs.display_events} events")
            elif pgs.display_events <= 30:
                add_role_score(score, "forced", 65, f"PGS has few events ({pgs.display_events})")
            elif pgs.display_events <= 80:
                add_role_score(score, "forced", 35, f"relatively short PGS ({pgs.display_events} events)")

            if max_events and pgs.display_events < max_events:
                event_ratio = pgs.display_events / max_events
                if event_ratio <= PGS_STRONG_FORCED_EVENT_RATIO:
                    add_role_score(
                        score,
                        "forced",
                        45,
                        f"PGS events far below largest subtitle ({event_ratio:.2f}x)",
                    )
                elif event_ratio <= PGS_WEAK_FORCED_EVENT_RATIO:
                    add_role_score(
                        score,
                        "forced",
                        25,
                        f"PGS events below largest subtitle ({event_ratio:.2f}x)",
                    )

            if max_display_seconds and pgs.estimated_display_seconds < max_display_seconds:
                display_ratio = pgs.estimated_display_seconds / max_display_seconds
                if display_ratio <= 0.18:
                    add_role_score(
                        score,
                        "forced",
                        35,
                        f"very low visible PGS time ({display_ratio:.2f}x)",
                    )
                elif display_ratio <= 0.35:
                    add_role_score(
                        score,
                        "forced",
                        20,
                        f"low visible PGS time ({display_ratio:.2f}x)",
                    )

            if max_payload_bytes and pgs.ods_payload_bytes < max_payload_bytes:
                payload_ratio = pgs.ods_payload_bytes / max_payload_bytes
                if payload_ratio <= 0.18:
                    add_role_score(
                        score,
                        "forced",
                        30,
                        f"much smaller PGS bitmap payload ({payload_ratio:.2f}x)",
                    )
                elif payload_ratio <= 0.35:
                    add_role_score(
                        score,
                        "forced",
                        15,
                        f"smaller PGS bitmap payload ({payload_ratio:.2f}x)",
                    )

        first_full_id = first_full_tracks.get(language_key)
        is_full_sized_duplicate = (
            first_full_id is not None
            and track.id != first_full_id
            and size_ratio is not None
            and size_ratio >= FULL_SIZE_DUPLICATE_RATIO
            and size_class == "large"
        )

        likely_duplicate_or_sdh = False
        if has_sdh_subtitle_hint(track):
            likely_duplicate_or_sdh = True
            score.commentary_evidence.append("0 name suggests SDH/CC, not commentary")
        if timeline_similarity is not None and timeline_similarity >= TIMELINE_SIMILARITY_HIGH:
            likely_duplicate_or_sdh = True
            score.commentary_evidence.append(
                f"0 timeline similar to normal subtitle ({timeline_similarity:.2f})"
            )
        if text_similarity is not None and text_similarity >= TEXT_SIMILARITY_HIGH:
            likely_duplicate_or_sdh = True
            score.commentary_evidence.append(
                f"0 text similar to normal subtitle ({text_similarity:.2f})"
            )
        if (
            track_text_stats
            and track_text_stats.sdh_score >= 30
            and (
                (timeline_similarity is not None and timeline_similarity >= TIMELINE_SIMILARITY_HIGH)
                or (text_similarity is not None and text_similarity >= TEXT_SIMILARITY_HIGH)
            )
        ):
            add_role_score(score, "sdh", 25, "similar to normal subtitle and contains SDH cues")
        if track_text_stats and track_text_stats.sdh_score >= 40:
            likely_duplicate_or_sdh = True
            score.commentary_evidence.append(
                f"0 text suggests SDH ({summarize_role_evidence(track_text_stats.sdh_evidence)})"
            )

        if is_full_sized_duplicate and not likely_duplicate_or_sdh:
            add_role_score(score, "commentary", 20, "extra full-size subtitle in the same language")
            if timeline_similarity is not None:
                if timeline_similarity <= TIMELINE_SIMILARITY_LOW:
                    add_role_score(
                        score,
                        "commentary",
                        35,
                        f"timeline differs from normal subtitle ({timeline_similarity:.2f})",
                    )
                elif timeline_similarity < TIMELINE_SIMILARITY_HIGH:
                    add_role_score(
                        score,
                        "commentary",
                        20,
                        f"timeline partially differs from normal subtitle ({timeline_similarity:.2f})",
                    )
            if text_similarity is not None:
                if text_similarity <= TEXT_SIMILARITY_LOW:
                    add_role_score(
                        score,
                        "commentary",
                        25,
                        f"text differs from normal subtitle ({text_similarity:.2f})",
                    )
                elif text_similarity < TEXT_SIMILARITY_HIGH:
                    add_role_score(
                        score,
                        "commentary",
                        10,
                        f"text partially differs from normal subtitle ({text_similarity:.2f})",
                    )
            if language_key in commentary_audio_languages:
                add_role_score(score, "commentary", 35, "matching-language commentary audio exists")

        if pgs and first_full_id is not None and track.id != first_full_id:
            event_ratio = (pgs.display_events / max_events) if max_events else 0.0
            display_ratio = (pgs.estimated_display_seconds / max_display_seconds) if max_display_seconds else 0.0
            payload_ratio = (pgs.ods_payload_bytes / max_payload_bytes) if max_payload_bytes else 0.0
            looks_full_length_pgs = (
                event_ratio >= 0.55
                and display_ratio >= 0.45
                and payload_ratio >= 0.45
                and pgs.display_events >= 150
            )

            if looks_full_length_pgs and not likely_duplicate_or_sdh:
                add_role_score(score, "commentary", 15, "extra full-length PGS in the same language")
                first_short_order = first_short_orders.get(language_key)
                if first_short_order is not None and track.order > first_short_order:
                    add_role_score(
                        score,
                        "commentary",
                        10,
                        "extra PGS appears after forced/empty track in the same language",
                    )
                if (
                    timeline_similarity is not None
                    and timeline_similarity < TIMELINE_SIMILARITY_HIGH
                    and pgs.display_events >= max_events
                ):
                    add_role_score(
                        score,
                        "commentary",
                        10,
                        "extra PGS is the densest subtitle in the language",
                    )
                if language_key in commentary_audio_languages:
                    add_role_score(score, "commentary", 45, "PGS follows commentary audio")
            elif looks_full_length_pgs and likely_duplicate_or_sdh:
                score.commentary_evidence.append("0 full-length PGS but appears duplicate/SDH")

        if track_text_stats:
            if track_text_stats.commentary_score >= 40 and track_text_stats.sdh_score < 40:
                add_role_score(
                    score,
                    "commentary",
                    min(35, track_text_stats.commentary_score),
                    summarize_role_evidence(track_text_stats.commentary_evidence),
                )

    score.forced = min(score.forced, 100)
    score.commentary = min(score.commentary, 100)
    score.sdh = min(score.sdh, 100)
    return score


def summarize_role_evidence(items: list[str]) -> str:
    if not items:
        return "no strong evidence"
    return "; ".join(items[:4])


def classify_subtitle_roles(
    subtitles: list[TrackInfo],
    audio_tracks: list[TrackInfo],
    forced_subtitle_ids: set[int],
    smart_sub_detection: bool,
    drop_empty_subs: bool,
) -> None:
    max_sizes = max_subtitle_sizes_by_language(subtitles)
    max_pgs_events = max_pgs_metric_by_language(subtitles, "display_events")
    max_pgs_display_seconds = max_pgs_metric_by_language(subtitles, "estimated_display_seconds")
    max_pgs_payload_bytes = max_pgs_metric_by_language(subtitles, "ods_payload_bytes")
    first_full_tracks = first_full_sized_track_by_language(subtitles, max_sizes)
    first_short_orders = first_short_subtitle_order_by_language(subtitles)
    timeline_similarity_by_track, text_similarity_by_track = comparison_to_primary_by_track(
        subtitles,
        first_full_tracks,
    )
    structural_commentary_by_track = structural_commentary_candidates(subtitles)
    commentary_audio_languages = {
        base_language_key(track)
        for track in audio_tracks
        if detect_audio_role(track) == "Commentary"
    }

    for track in sorted(subtitles, key=lambda item: item.order):
        size_class = track.analysis.size_class if track.analysis else "unknown"
        track.role = "normal"
        track.role_reason = ""
        track.role_score = score_subtitle_role(
            track=track,
            forced_subtitle_ids=forced_subtitle_ids,
            smart_sub_detection=smart_sub_detection,
            max_sizes=max_sizes,
            max_pgs_events=max_pgs_events,
            max_pgs_display_seconds=max_pgs_display_seconds,
            max_pgs_payload_bytes=max_pgs_payload_bytes,
            first_full_tracks=first_full_tracks,
            first_short_orders=first_short_orders,
            timeline_similarity_by_track=timeline_similarity_by_track,
            text_similarity_by_track=text_similarity_by_track,
            commentary_audio_languages=commentary_audio_languages,
            structural_commentary_by_track=structural_commentary_by_track,
        )
        track.forced = False
        explicit_forced = (
            track.id in forced_subtitle_ids
            or has_forced_flag(track)
            or detect_subtitle_role_hint(track) == "forced"
        )
        inferred_forced_threshold = 80 if not explicit_forced else FORCED_SCORE_THRESHOLD

        if size_class == "empty":
            track.role = "empty"
            track.role_reason = (
                "empty subtitle; removed from final remux"
                if drop_empty_subs
                else "empty subtitle; kept by --keep-empty-subs"
            )
            track.forced = False
        elif (
            track.role_score.sdh >= SDH_SCORE_THRESHOLD
            and not explicit_forced
            and track.role_score.sdh >= track.role_score.commentary
        ):
            track.role = "sdh"
            track.role_reason = (
                f"score SDH={track.role_score.sdh}; "
                f"{summarize_role_evidence(track.role_score.sdh_evidence)}"
            )
        elif (
            track.role_score.forced >= FORCED_SCORE_THRESHOLD
            and track.role_score.forced >= track.role_score.commentary
            and (explicit_forced or track.role_score.forced >= track.role_score.sdh)
            and track.role_score.forced >= inferred_forced_threshold
        ):
            track.role = "forced"
            track.role_reason = (
                f"score forced={track.role_score.forced}; "
                f"{summarize_role_evidence(track.role_score.forced_evidence)}"
            )
            track.forced = True
        elif (
            track.role_score.commentary >= COMMENTARY_SCORE_THRESHOLD
            and track.role_score.commentary > track.role_score.forced
            and track.role_score.commentary > track.role_score.sdh
        ):
            track.role = "commentary"
            track.role_reason = (
                f"score commentary={track.role_score.commentary}; "
                f"{summarize_role_evidence(track.role_score.commentary_evidence)}"
            )

        track.drop = drop_empty_subs and size_class == "empty"
        track.suggested_name = subtitle_track_name(track)


def subtitle_track_name(track: TrackInfo) -> str:
    base_name = track.language_name

    if track.role == "forced":
        return f"{base_name} (Forced)"
    if track.role == "commentary":
        return f"{base_name} (Commentary)"
    if track.role == "sdh":
        return f"{base_name} (SDH)"

    return base_name


def apply_audio_names(audio_tracks: list[TrackInfo], style: str = "auto") -> None:
    resolved_style = resolve_audio_name_style(audio_tracks, style)
    for track in audio_tracks:
        track.suggested_name = audio_track_name(track, resolved_style)


def infer_audio_commentary_from_subtitles(audio_tracks: list[TrackInfo], subtitles: list[TrackInfo]) -> None:
    if not any(track.role == "commentary" for track in subtitles):
        return

    if any(detect_audio_role(track) == "Commentary" for track in audio_tracks):
        return

    english_audio = [track for track in audio_tracks if is_english(track)]
    if len(english_audio) < 2:
        return

    main_audio = select_default_audio(english_audio)
    candidates = [
        track
        for track in english_audio
        if track is not main_audio and (track.channels or 0) <= 2
    ]
    if not candidates:
        return

    candidate = min(candidates, key=lambda track: (audio_quality_score(track), track.order))
    candidate.role = "commentary"
    candidate.role_reason = "inferred from full-length commentary subtitles"


def reset_duplicate_tracking(tracks: Iterable[TrackInfo]) -> None:
    for track in tracks:
        track.duplicate_group = ""
        track.duplicate_member_ids = []
        track.duplicate_of_id = None
        track.duplicate_reason = ""
        track.duplicate_source = ""
        track.duplicate_of_source = ""


def duplicate_source_label(input_path: Path | None = None, track: TrackInfo | None = None) -> str:
    if track and track.source_name:
        return track.source_name
    if track and track.source_path:
        return Path(track.source_path).name
    if input_path:
        return input_path.name or str(input_path)
    return ""


def duplicate_track_label(track: TrackInfo, fallback_input_path: Path | None = None) -> str:
    source = duplicate_source_label(fallback_input_path, track)
    if source:
        return f"{source} track {track.id}"
    return f"track {track.id}"


def duplicate_language_key(track: TrackInfo) -> str:
    language_code = track_language_code(track)
    if language_code in {"", "und", "zxx", "mul"}:
        return ""
    return language_code


def duplicate_text_key(value: str | None) -> str:
    text = remove_accents(value or "").casefold()
    return re.sub(r"[^a-z0-9]+", "-", text).strip("-")


def audio_duplicate_key(track: TrackInfo) -> tuple[Any, ...] | None:
    language_code = duplicate_language_key(track)
    if not language_code or track.channels is None:
        return None
    return (
        "audio",
        language_code,
        detect_audio_role(track) or track.role or "normal",
        duplicate_text_key(audio_codec_label(track)),
        track.channels,
    )


def subtitle_duplicate_key(track: TrackInfo) -> tuple[Any, ...] | None:
    if track.drop:
        return None
    language_code = duplicate_language_key(track)
    if not language_code:
        return None
    analysis = track.analysis or SubtitleAnalysis()
    return (
        "subtitles",
        language_code,
        track.role or "normal",
        bool(track.forced),
        duplicate_text_key(track.codec_id or track.codec),
        analysis.size_class or "unknown",
    )


def duplicate_metric(track: TrackInfo) -> float | None:
    if track.type != "subtitles":
        return None
    pgs = pgs_stats(track)
    if pgs and pgs.display_events:
        return float(pgs.display_events)
    size_bytes = subtitle_size_bytes(track)
    if size_bytes:
        return float(size_bytes)
    return None


def duplicate_metrics_compatible(first: TrackInfo, second: TrackInfo) -> bool:
    first_metric = duplicate_metric(first)
    second_metric = duplicate_metric(second)
    if first_metric is None or second_metric is None:
        return True
    larger = max(first_metric, second_metric)
    if larger <= 0:
        return True
    return min(first_metric, second_metric) / larger >= 0.80


def split_duplicate_metric_groups(tracks: list[TrackInfo]) -> list[list[TrackInfo]]:
    groups: list[list[TrackInfo]] = []
    for track in sorted(tracks, key=lambda item: item.order):
        for group in groups:
            if duplicate_metrics_compatible(group[0], track):
                group.append(track)
                break
        else:
            groups.append([track])
    return groups


def duplicate_group_reason(track: TrackInfo) -> str:
    if track.type == "audio":
        return "same audio language, role, codec, and channel layout"
    return "same subtitle language, role, codec, and comparable size/events"


def mark_duplicate_group(input_path: Path, tracks: list[TrackInfo]) -> None:
    if len(tracks) < 2:
        return
    leader = min(tracks, key=lambda item: item.order)
    member_ids = [track.id for track in sorted(tracks, key=lambda item: item.order)]
    member_text = ", ".join(duplicate_track_label(track, input_path) for track in sorted(tracks, key=lambda item: item.order))
    group_id = f"{duplicate_source_label(input_path, leader)}:{leader.type}:{leader.id}"
    reason = duplicate_group_reason(leader)
    leader_label = duplicate_track_label(leader, input_path)

    for track in tracks:
        source = duplicate_source_label(input_path, track)
        track.duplicate_group = group_id
        track.duplicate_member_ids = member_ids
        track.duplicate_source = source
        if track is leader:
            track.duplicate_of_id = None
            track.duplicate_of_source = ""
            track.duplicate_reason = f"Possible duplicate group: {member_text}; {reason}"
        else:
            track.duplicate_of_id = leader.id
            track.duplicate_of_source = duplicate_source_label(input_path, leader)
            track.duplicate_reason = f"Possible duplicate of {leader_label}; {reason}"


def detect_duplicate_tracks(input_path: Path, audio_tracks: list[TrackInfo], subtitles: list[TrackInfo]) -> None:
    reset_duplicate_tracking([*audio_tracks, *subtitles])
    tracks_by_key: dict[tuple[Any, ...], list[TrackInfo]] = {}

    for track in audio_tracks:
        key = audio_duplicate_key(track)
        if key:
            tracks_by_key.setdefault(key, []).append(track)

    for track in subtitles:
        key = subtitle_duplicate_key(track)
        if key:
            tracks_by_key.setdefault(key, []).append(track)

    for tracks in tracks_by_key.values():
        if len(tracks) < 2:
            continue
        for group in split_duplicate_metric_groups(tracks):
            mark_duplicate_group(input_path, group)


def apply_default_flags(
    videos: list[TrackInfo],
    audio_tracks: list[TrackInfo],
    subtitles: list[TrackInfo],
    language_order_style: str = "default",
    regional_order: Any = None,
) -> None:
    included_videos = [track for track in videos if not track.drop]
    default_video = included_videos[0] if included_videos else None
    for track in videos:
        track.default = track is default_video

    included_audio = [track for track in audio_tracks if not track.drop]
    default_audio = select_default_audio(included_audio)
    for track in audio_tracks:
        track.default = track is default_audio and not track.drop

    included_subtitles = [track for track in subtitles if not track.drop]
    english_forced = [
        track
        for track in sorted(
            included_subtitles,
            key=lambda item: subtitle_sort_key(item, language_order_style, regional_order),
        )
        if is_english_forced_subtitle(track)
    ]
    default_subtitle = english_forced[0] if english_forced else None

    for track in subtitles:
        track.default = track is default_subtitle and not track.drop


def is_forced_subtitle(track: TrackInfo) -> bool:
    return track.role == "forced" or track.forced


def is_english_forced_subtitle(track: TrackInfo) -> bool:
    return is_english(track) and is_forced_subtitle(track)


def normalized_subtitle_language_name(track: TrackInfo) -> str:
    return remove_accents(track.language_name or language_display_name(track.output_language)).casefold()


def subtitle_format_rank(track: TrackInfo) -> int:
    extension = subtitle_extension(track)

    if is_pgs_subtitle(track):
        return 0
    if extension == ".srt":
        return 1
    if is_text_subtitle(track):
        return 2

    return 3


def english_normal_subtitle_rank(track: TrackInfo) -> int:
    is_sdh = track.role == "sdh"
    format_rank = subtitle_format_rank(track)

    if not is_sdh and format_rank == 0:
        return 0
    if not is_sdh and format_rank == 1:
        return 1
    if is_sdh and format_rank == 0:
        return 2
    if is_sdh and format_rank == 1:
        return 3
    if not is_sdh:
        return 4

    return 5


def subtitle_language_sort_key(
    track: TrackInfo,
    language_order_style: str = "default",
    regional_order: Any = None,
) -> tuple[Any, ...]:
    if is_english(track):
        return (0, "")

    if language_order_style == "regional":
        return (1, *language_sort_key(track, language_order_style, regional_order))

    return (1, normalized_subtitle_language_name(track))


def subtitle_sort_key(
    track: TrackInfo,
    language_order_style: str = "default",
    regional_order: Any = None,
) -> tuple[Any, ...]:
    primary_group = 0 if is_english_forced_subtitle(track) else 1

    if is_forced_subtitle(track):
        role_group = 1
    elif track.role == "commentary":
        role_group = 2
    else:
        role_group = 0

    if role_group == 0 and is_english(track):
        return (primary_group, role_group, 0, english_normal_subtitle_rank(track), track.order)

    language_key = subtitle_language_sort_key(track, language_order_style, regional_order)
    if role_group == 0:
        return (primary_group, role_group, 1, *language_key, track.role == "sdh", subtitle_format_rank(track), track.order)

    return (primary_group, role_group, *language_key, track.order)


def ordered_tracks(
    videos: list[TrackInfo],
    audio_tracks: list[TrackInfo],
    subtitles: list[TrackInfo],
    language_order_style: str = "default",
    regional_order: Any = None,
) -> list[TrackInfo]:
    included_videos = [track for track in videos if not track.drop]
    included_audio = [track for track in audio_tracks if not track.drop]
    included_subtitles = [track for track in subtitles if not track.drop]
    return (
        sorted(included_videos, key=lambda item: item.order)
        + sorted(included_audio, key=lambda item: audio_sort_key(item, language_order_style, regional_order))
        + sorted(included_subtitles, key=lambda item: subtitle_sort_key(item, language_order_style, regional_order))
    )


def apply_track_selection_overrides(
    videos: list[TrackInfo],
    audio_tracks: list[TrackInfo],
    subtitles: list[TrackInfo],
    selection_overrides: dict[str, Any] | None,
) -> None:
    if not selection_overrides:
        return

    for track in [*videos, *audio_tracks, *subtitles]:
        selected = selection_overrides.get(track_selection_key_for_track(track))
        if selected is None:
            continue
        track.drop = not bool(selected)


def propedit_bool(value: bool) -> str:
    return "1" if value else "0"


def track_number(track: TrackInfo) -> int | None:
    number = track.properties.get("number")
    try:
        return int(number)
    except (TypeError, ValueError):
        return None


def maybe_add_metadata_change(changes: dict[str, str], current: Any, desired: str, property_name: str) -> None:
    if current is None:
        current_text = ""
    else:
        current_text = str(current)

    if current_text != desired:
        changes[property_name] = desired


def maybe_add_metadata_bool_change(changes: dict[str, str], current: bool, desired: bool, property_name: str) -> None:
    if bool(current) != bool(desired):
        changes[property_name] = propedit_bool(desired)


def metadata_changes_for_track(track: TrackInfo) -> dict[str, str]:
    changes: dict[str, str] = {}
    output_language = track.output_language or track.language

    maybe_add_metadata_change(
        changes,
        track.properties.get("language"),
        legacy_language_for_mkvpropedit(output_language),
        "language",
    )
    maybe_add_metadata_change(
        changes,
        canonicalize_ietf_code(str(track.properties.get("language_ietf") or "")),
        ietf_language_for_mkvpropedit(output_language),
        "language-ietf",
    )

    if track.type in {"audio", "subtitles"}:
        maybe_add_metadata_change(changes, track.original_name, track.suggested_name or "", "name")

    maybe_add_metadata_bool_change(
        changes,
        bool(track.properties.get("default_track", False)),
        track.default,
        "flag-default",
    )

    if track.type == "audio":
        maybe_add_metadata_bool_change(
            changes,
            has_commentary_flag(track),
            detect_audio_role(track) == "Commentary",
            "flag-commentary",
        )
    elif track.type == "subtitles":
        maybe_add_metadata_bool_change(changes, has_forced_flag(track), track.forced, "flag-forced")
        maybe_add_metadata_bool_change(
            changes,
            has_hearing_impaired_flag(track),
            track.role == "sdh",
            "flag-hearing-impaired",
        )
        maybe_add_metadata_bool_change(
            changes,
            has_commentary_flag(track),
            track.role == "commentary",
            "flag-commentary",
        )

    return changes


def metadata_edit_plan(
    videos: list[TrackInfo],
    audio_tracks: list[TrackInfo],
    subtitles: list[TrackInfo],
    language_order_style: str = "default",
    regional_order: Any = None,
) -> MetadataEditPlan:
    all_tracks = sorted(videos + audio_tracks + subtitles, key=lambda item: item.order)
    desired_tracks = ordered_tracks(videos, audio_tracks, subtitles, language_order_style, regional_order)

    dropped_tracks = [track for track in all_tracks if track.drop]
    if dropped_tracks:
        return MetadataEditPlan(False, "tracks need to be removed")

    delayed_tracks = [track for track in audio_tracks + subtitles if track.delay_ms]
    if delayed_tracks:
        return MetadataEditPlan(False, "track delays require remux")

    original_order = [track.id for track in all_tracks]
    desired_order = [track.id for track in desired_tracks]
    if original_order != desired_order:
        return MetadataEditPlan(False, "track order needs to change")

    edits: list[MetadataTrackEdit] = []
    for track in all_tracks:
        number = track_number(track)
        if number is None:
            return MetadataEditPlan(False, f"track {track.id} has no Matroska number for mkvpropedit")

        changes = metadata_changes_for_track(track)
        if changes:
            edits.append(MetadataTrackEdit(track, changes))

    if edits:
        return MetadataEditPlan(True, "only track properties change", edits)
    return MetadataEditPlan(True, "no changes needed", edits)


def build_mkvpropedit_command(
    mkvpropedit: Path,
    input_path: Path,
    edits: list[MetadataTrackEdit],
) -> list[str]:
    command = command_with_mkvtoolnix_ui_language([str(mkvpropedit), str(input_path)])

    for edit in edits:
        number = track_number(edit.track)
        if number is None:
            raise OrganizerError(f"Track {edit.track.id} has no Matroska number for mkvpropedit.")

        command.extend(["--edit", f"track:@{number}"])
        for property_name, value in edit.properties.items():
            command.extend(["--set", f"{property_name}={value}"])

    return command


def build_mkvmerge_command(
    mkvmerge: Path,
    input_path: Path | list[Path],
    output_path: Path,
    videos: list[TrackInfo],
    audio_tracks: list[TrackInfo],
    subtitles: list[TrackInfo],
    language_order_style: str = "default",
    regional_order: Any = None,
) -> list[str]:
    command = command_with_mkvtoolnix_ui_language([str(mkvmerge), "--output", str(output_path)])
    input_paths = [input_path] if isinstance(input_path, Path) else list(input_path)

    for source_index, source_path in enumerate(input_paths):
        source_videos = [track for track in videos if track.source_index == source_index]
        source_audio = [track for track in audio_tracks if track.source_index == source_index]
        source_subtitles = [track for track in subtitles if track.source_index == source_index]
        included_videos = [track for track in source_videos if not track.drop]
        included_audio = [track for track in source_audio if not track.drop]
        included_subtitles = [track for track in source_subtitles if not track.drop]

        if len(input_paths) > 1:
            if included_videos:
                command.extend(["--video-tracks", ",".join(str(track.id) for track in included_videos)])
            else:
                command.append("--no-video")
            if included_audio:
                command.extend(["--audio-tracks", ",".join(str(track.id) for track in included_audio)])
            else:
                command.append("--no-audio")
            if included_subtitles:
                command.extend(["--subtitle-tracks", ",".join(str(track.id) for track in included_subtitles)])
            else:
                command.append("--no-subtitles")
        else:
            if any(track.drop for track in source_videos):
                if included_videos:
                    command.extend(["--video-tracks", ",".join(str(track.id) for track in included_videos)])
                else:
                    command.append("--no-video")
            if any(track.drop for track in source_audio):
                if included_audio:
                    command.extend(["--audio-tracks", ",".join(str(track.id) for track in included_audio)])
                else:
                    command.append("--no-audio")
            if any(track.drop for track in source_subtitles):
                if included_subtitles:
                    command.extend(["--subtitle-tracks", ",".join(str(track.id) for track in included_subtitles)])
                else:
                    command.append("--no-subtitles")

        for track in included_videos:
            command.extend(["--default-track-flag", f"{track.id}:{'yes' if track.default else 'no'}"])

        for track in included_audio:
            command.extend(["--language", f"{track.id}:{language_for_mkvmerge(track.output_language)}"])
            command.extend(["--track-name", f"{track.id}:{track.suggested_name}"])
            command.extend(["--default-track-flag", f"{track.id}:{'yes' if track.default else 'no'}"])
            command.extend(["--commentary-flag", f"{track.id}:{'yes' if detect_audio_role(track) == 'Commentary' else 'no'}"])
            if track.delay_ms:
                command.extend(["--sync", f"{track.id}:{track.delay_ms}"])

        for track in included_subtitles:
            command.extend(["--language", f"{track.id}:{language_for_mkvmerge(track.output_language)}"])
            command.extend(["--track-name", f"{track.id}:{track.suggested_name}"])
            command.extend(["--default-track-flag", f"{track.id}:{'yes' if track.default else 'no'}"])
            command.extend(["--forced-display-flag", f"{track.id}:{'yes' if track.forced else 'no'}"])
            command.extend(["--hearing-impaired-flag", f"{track.id}:{'yes' if track.role == 'sdh' else 'no'}"])
            command.extend(["--commentary-flag", f"{track.id}:{'yes' if track.role == 'commentary' else 'no'}"])
            if track.delay_ms:
                command.extend(["--sync", f"{track.id}:{track.delay_ms}"])

        command.append(str(source_path))

    ordered = ordered_tracks(videos, audio_tracks, subtitles, language_order_style, regional_order)
    if ordered:
        command.extend(["--track-order", ",".join(f"{track.source_index}:{track.id}" for track in ordered)])

    return command


def track_note_text(track: TrackInfo) -> str:
    notes = [note for note in [track.duplicate_reason, track.role_reason] if note]
    return f" | {' | '.join(notes)}" if notes else ""


def track_source_text(track: TrackInfo, show_source: bool) -> str:
    return f" | source={track.source_name}" if show_source and track.source_name else ""


def print_track_plan(
    videos: list[TrackInfo],
    audio_tracks: list[TrackInfo],
    subtitles: list[TrackInfo],
    language_order_style: str = "default",
    regional_order: Any = None,
) -> None:
    print("\nTrack plan:")
    show_source = any(track.source_index for track in [*videos, *audio_tracks, *subtitles])

    for track in sorted(videos, key=lambda item: item.order):
        drop_text = " | DROP" if track.drop else ""
        print(
            f"  video    {track.id:>3}: default={'yes' if track.default else 'no'}"
            f"{track_source_text(track, show_source)}{drop_text}"
        )

    for track in sorted(audio_tracks, key=lambda item: audio_sort_key(item, language_order_style, regional_order)):
        reason_text = track_note_text(track)
        delay_text = f" | delay={track.delay_ms:+d}ms" if track.delay_ms else ""
        drop_text = " | DROP" if track.drop else ""
        print(
            f"  audio    {track.id:>3}: {track.language_name} | "
            f"{track.suggested_name} | default={'yes' if track.default else 'no'}"
            f"{track_source_text(track, show_source)}{delay_text}{drop_text}{reason_text}"
        )

    for track in sorted(
        subtitles,
        key=lambda item: (item.drop, subtitle_sort_key(item, language_order_style, regional_order)),
    ):
        drop_text = " | DROP" if track.drop else ""
        forced_text = " | forced=yes" if track.forced else ""
        default_text = " | default=yes" if track.default else " | default=no"
        delay_text = f" | delay={track.delay_ms:+d}ms" if track.delay_ms else ""
        reason_text = track_note_text(track)
        print(
            f"  subtitle {track.id:>3}: {track.suggested_name}"
            f"{default_text}{forced_text}{track_source_text(track, show_source)}{delay_text}{drop_text}{reason_text}"
        )


def print_subtitle_size_report(subtitles: list[TrackInfo]) -> None:
    print("\nSubtitle size analysis:")
    print("  ID   Language                  Size                Class    PGS events/visible/span       Suggested name")

    for track in sorted(subtitles, key=lambda item: item.order):
        analysis = track.analysis or SubtitleAnalysis()
        if analysis.pgs:
            pgs_text = (
                f"{analysis.pgs.display_events}/"
                f"{analysis.pgs.estimated_display_seconds:.0f}s/"
                f"{analysis.pgs.span_seconds:.0f}s"
            )
        else:
            pgs_text = "-"
        print(
            f"  {track.id:<4} {track.language_name:<25} "
            f"{format_size(analysis.size_bytes):<20} "
            f"{analysis.size_class:<8} {pgs_text:<27} {track.suggested_name}"
        )


def print_subtitle_role_score_report(subtitles: list[TrackInfo]) -> None:
    print("\nSubtitle role scoring:")
    print("  ID   Language                  F-score C-score S-score Decision      PGS events/density      Evidence")

    for track in sorted(subtitles, key=lambda item: item.order):
        score = track.role_score
        if track.role == "sdh":
            evidence_items = score.sdh_evidence + score.forced_evidence + score.commentary_evidence
        elif track.role == "commentary":
            evidence_items = score.commentary_evidence + score.forced_evidence + score.sdh_evidence
        else:
            evidence_items = score.forced_evidence + score.sdh_evidence + score.commentary_evidence
        evidence = summarize_role_evidence(evidence_items)
        pgs = pgs_stats(track)
        if pgs:
            pgs_text = f"{pgs.display_events}/{pgs.event_density_per_hour:.0f}h"
        else:
            pgs_text = "-"
        print(
            f"  {track.id:<4} {track.language_name:<25} "
            f"{score.forced:<7} {score.commentary:<7} {score.sdh:<7} "
            f"{track.role:<12} {pgs_text:<24} {evidence}"
        )


def track_report_data(track: TrackInfo) -> dict[str, Any]:
    analysis = track.analysis
    pgs = analysis.pgs if analysis else None
    text_stats = analysis.text_stats if analysis else None
    score = track.role_score
    return {
        "id": track.id,
        "selection_key": track_selection_key_for_track(track),
        "source_index": track.source_index,
        "source_path": track.source_path,
        "source_name": track.source_name,
        "type": track.type,
        "codec": track.codec,
        "input_language": track.language,
        "output_language": track.output_language,
        "name": track.suggested_name or track.language_name,
        "original_name": track.original_name,
        "default": track.default,
        "forced": track.forced,
        "drop": track.drop,
        "delay_ms": track.delay_ms,
        "role": track.role,
        "role_reason": track.role_reason,
        "duplicate_group": track.duplicate_group,
        "duplicate_member_ids": track.duplicate_member_ids,
        "duplicate_of_id": track.duplicate_of_id,
        "duplicate_reason": track.duplicate_reason,
        "duplicate_source": track.duplicate_source,
        "duplicate_of_source": track.duplicate_of_source,
        "role_scores": {
            "forced": score.forced,
            "commentary": score.commentary,
            "sdh": score.sdh,
        },
        "role_evidence": {
            "forced": score.forced_evidence,
            "commentary": score.commentary_evidence,
            "sdh": score.sdh_evidence,
        },
        "analysis": {
            "size_bytes": analysis.size_bytes if analysis else None,
            "size_class": analysis.size_class if analysis else "unknown",
            "text_words": text_stats.word_count if text_stats else None,
            "pgs_display_events": pgs.display_events if pgs else None,
            "pgs_display_seconds": round(pgs.estimated_display_seconds, 3) if pgs else None,
            "pgs_span_seconds": round(pgs.span_seconds, 3) if pgs else None,
        },
    }


def file_report_data(
    input_path: Path,
    output_path: Path,
    status: str,
    command: list[str] | None = None,
    videos: list[TrackInfo] | None = None,
    audio_tracks: list[TrackInfo] | None = None,
    subtitles: list[TrackInfo] | None = None,
    message: str = "",
    input_paths: list[Path] | None = None,
) -> dict[str, Any]:
    return {
        "input": str(input_path),
        "inputs": [str(path) for path in (input_paths or [input_path])],
        "output": str(output_path),
        "status": status,
        "message": message,
        "command": command or [],
        "command_text": format_command(command or []),
        "tracks": {
            "video": [track_report_data(track) for track in (videos or [])],
            "audio": [track_report_data(track) for track in (audio_tracks or [])],
            "subtitles": [track_report_data(track) for track in (subtitles or [])],
        },
    }


def default_report_dir_for(args: argparse.Namespace, source_root: Path | None, input_files: list[Path]) -> Path:
    if args.report_dir:
        return args.report_dir
    if args.output_dir:
        return args.output_dir / REPORTS_DIR_NAME
    if source_root:
        return source_root / REPORTS_DIR_NAME
    return input_files[0].parent / REPORTS_DIR_NAME


def write_batch_report(
    reports: list[dict[str, Any]],
    args: argparse.Namespace,
    source_root: Path | None,
    input_files: list[Path],
    failures: int,
) -> None:
    if not args.report and not args.report_dir:
        return

    report_dir = default_report_dir_for(args, source_root, input_files)
    report_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    base_path = report_dir / f"mkv_track_organizer_{timestamp}"
    payload = {
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "config": str(getattr(args, "config_path", "") or ""),
        "dry_run": args.dry_run,
        "input": str(args.path),
        "files_total": len(input_files),
        "failures": failures,
        "files": reports,
    }

    if args.report_format in {"json", "both"}:
        json_path = base_path.with_suffix(".json")
        json_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
        print(f"JSON report: {json_path}")

    if args.report_format in {"txt", "both"}:
        txt_path = base_path.with_suffix(".txt")
        lines = [
            "MKV Track Organizer report",
            f"Created: {payload['created_at']}",
            f"Input: {payload['input']}",
            f"Dry-run: {'yes' if args.dry_run else 'no'}",
            f"Files: {len(input_files)}",
            f"Failures: {failures}",
            "",
        ]
        for item in reports:
            lines.extend(
                [
                    f"[{item['status']}] {Path(item['input']).name}",
                    f"  input:  {item['input']}",
                    f"  output: {item['output']}",
                ]
            )
            if item.get("message"):
                lines.append(f"  note:   {item['message']}")
            item_inputs = item.get("inputs") or []
            if len(item_inputs) > 1:
                lines.append("  sources:")
                lines.extend(f"    - {input_item}" for input_item in item_inputs)
            track_groups = item.get("tracks", {})
            audio_tracks = track_groups.get("audio", [])
            subtitles = track_groups.get("subtitles", [])
            duplicates = [
                track for track in [*audio_tracks, *subtitles]
                if track.get("duplicate_group")
            ]
            dropped = [track for track in subtitles if track.get("drop")]
            special = [
                track for track in subtitles
                if track.get("role") in {"forced", "commentary", "sdh"} and not track.get("drop")
            ]
            if duplicates:
                lines.append("  possible duplicates:")
                lines.extend(
                    f"    - {track['id']}: {track['name']} ({track['duplicate_reason']})"
                    for track in duplicates
                )
            if dropped:
                lines.append("  removed:")
                lines.extend(f"    - {track['id']}: {track['name']} ({track['role_reason']})" for track in dropped)
            if special:
                lines.append("  marked:")
                lines.extend(
                    f"    - {track['id']}: {track['name']} [{track['role']}] {track['role_reason']}"
                    for track in special
                )
            lines.append("")
        txt_path.write_text("\n".join(lines), encoding="utf-8")
        print(f"TXT report:  {txt_path}")


def print_track_explanations(subtitles: list[TrackInfo], explain_track_ids: set[int]) -> None:
    if not explain_track_ids:
        return

    tracks_by_id = {track.id: track for track in subtitles}
    print("\nTrack explanations:")
    for track_id in sorted(explain_track_ids):
        track = tracks_by_id.get(track_id)
        if not track:
            print(f"  track {track_id}: does not exist as a subtitle in this file.")
            continue

        analysis = track.analysis or SubtitleAnalysis()
        pgs = analysis.pgs
        text_stats = analysis.text_stats
        print(f"  track {track.id}: {track.suggested_name or track.language_name}")
        print(f"    language: {track.language} -> {track.output_language} ({track.language_name})")
        print(f"    codec: {track.codec} | original name: {track.original_name or '-'}")
        print(f"    decision: role={track.role} drop={'yes' if track.drop else 'no'} default={'yes' if track.default else 'no'} forced={'yes' if track.forced else 'no'}")
        print(f"    reason: {track.role_reason or 'no special reason'}")
        print(f"    size: {format_size(analysis.size_bytes)} | class={analysis.size_class}")
        if pgs:
            print(
                "    PGS: "
                f"events={pgs.display_events}, visible={pgs.estimated_display_seconds:.1f}s, "
                f"span={pgs.span_seconds:.1f}s, density={pgs.event_density_per_hour:.1f}/h"
            )
        if text_stats:
            print(
                f"    text: words={text_stats.word_count}, "
                f"commentary_score={text_stats.commentary_score}, sdh_score={text_stats.sdh_score}"
            )
        score = track.role_score
        print(f"    scores: forced={score.forced}, commentary={score.commentary}, sdh={score.sdh}")
        print(f"    forced: {summarize_role_evidence(score.forced_evidence)}")
        print(f"    commentary: {summarize_role_evidence(score.commentary_evidence)}")
        print(f"    sdh: {summarize_role_evidence(score.sdh_evidence)}")


def config_bool(value: Any, key: str) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"1", "true", "yes", "sim", "on"}:
            return True
        if normalized in {"0", "false", "no", "nao", "não", "off"}:
            return False
    raise OrganizerError(f"Invalid config: {key} must be boolean.")


def config_metadata_edit_mode(value: Any, key: str) -> str:
    if isinstance(value, bool):
        return "auto" if value else "off"

    normalized = str(value or "").strip().lower()
    if normalized in {"1", "true", "yes", "sim", "on"}:
        return "auto"
    if normalized in {"0", "false", "no", "nao", "não", "off"}:
        return "off"
    if normalized in METADATA_EDIT_MODES:
        return normalized

    allowed = ", ".join(sorted(METADATA_EDIT_MODES))
    raise OrganizerError(f"Invalid config: {key} must be one of these values: {allowed}.")


def config_path_list(value: Any, key: str) -> list[Path]:
    if value is None:
        return []
    if isinstance(value, list):
        return [Path(str(item)).expanduser() for item in value if str(item).strip()]
    if isinstance(value, str):
        return [Path(item.strip()).expanduser() for item in value.split(";") if item.strip()]

    raise OrganizerError(f"Invalid config: {key} must be a path list or text separated by ';'.")


def config_string_list(value: Any, key: str) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    if isinstance(value, str):
        return [item.strip() for item in value.split(";") if item.strip()]

    raise OrganizerError(f"Invalid config: {key} must be a string list or text separated by ';'.")


def normalize_config_key(key: str) -> str:
    normalized = key.strip().replace("-", "_")
    if normalized == "detect_pt_variant":
        return "detect_language_variants"
    if normalized == "no_detect_pt_variant":
        return "detect_language_variants"
    if normalized == "variant_context_dir":
        return "variant_context_dirs"
    return normalized


def coerce_config_value(key: str, value: Any) -> Any:
    if key in CONFIG_PATH_KEYS:
        return Path(str(value)).expanduser() if value is not None else None
    if key in CONFIG_PATH_LIST_KEYS:
        return config_path_list(value, key)
    if key in CONFIG_STRING_LIST_KEYS:
        return config_string_list(value, key)
    if key in CONFIG_BOOL_KEYS:
        return config_bool(value, key)
    if key in CONFIG_INT_KEYS:
        try:
            return int(value)
        except (TypeError, ValueError) as error:
            raise OrganizerError(f"Invalid config: {key} must be an integer.") from error
    if key == "metadata_edit_mode":
        return config_metadata_edit_mode(value, key)
    if key in CONFIG_STRING_KEYS:
        return "" if value is None else str(value)
    return value


def load_config_file(config_path: Path | None) -> dict[str, Any]:
    if not config_path:
        return {}
    if not config_path.exists():
        raise OrganizerError(f"Config file not found: {config_path}")

    try:
        raw_config = json.loads(config_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as error:
        raise OrganizerError(f"Invalid config JSON in {config_path}: {error}") from error
    except OSError as error:
        raise OrganizerError(f"Could not read config {config_path}: {error}") from error

    if not isinstance(raw_config, dict):
        raise OrganizerError("Invalid config: top-level JSON value must be an object.")

    defaults: dict[str, Any] = {}
    allowed_keys = (
        CONFIG_PATH_KEYS
        | CONFIG_PATH_LIST_KEYS
        | CONFIG_STRING_LIST_KEYS
        | CONFIG_BOOL_KEYS
        | CONFIG_INT_KEYS
        | CONFIG_STRING_KEYS
    )
    for raw_key, value in raw_config.items():
        key = normalize_config_key(str(raw_key))
        if key not in allowed_keys:
            print(f"Warning: config ignored unknown key: {raw_key}")
            continue
        defaults[key] = coerce_config_value(key, value)

    return defaults


def config_defaults_from_argv(argv: list[str] | None) -> tuple[dict[str, Any], Path | None]:
    pre_parser = argparse.ArgumentParser(add_help=False)
    pre_parser.add_argument("--config", type=Path, default=None)
    pre_parser.add_argument("--no-config", action="store_true")
    known, _unknown = pre_parser.parse_known_args(argv)

    if known.no_config:
        return {}, None

    config_path = known.config
    if config_path is None and DEFAULT_CONFIG_PATH.exists():
        config_path = DEFAULT_CONFIG_PATH

    if config_path is None:
        return {}, None

    config_path = config_path.expanduser().resolve()
    return load_config_file(config_path), config_path


def output_suffix_text(raw_suffix: str | None) -> str:
    if not raw_suffix:
        return ""

    suffix = raw_suffix.strip()
    if not suffix:
        return ""
    if suffix.startswith((".", "_", "-", " ")):
        return suffix
    return f".{suffix}"


def output_name_for(input_path: Path, output_suffix: str | None) -> str:
    suffix = output_suffix_text(output_suffix)
    if not suffix:
        return input_path.name
    return f"{input_path.stem}{suffix}{input_path.suffix}"


def is_matroska_input_file(path: Path) -> bool:
    return path.is_file() and path.suffix.lower() in MATROSKA_INPUT_SUFFIXES


def matroska_input_glob_patterns(recursive: bool) -> list[str]:
    prefix = "**/*" if recursive else "*"
    return [f"{prefix}{suffix}" for suffix in sorted(MATROSKA_INPUT_SUFFIXES)]


def merge_output_extension_for(input_files: list[Path]) -> str:
    if any(path.suffix.lower() == ".mkv" for path in input_files):
        return ".mkv"
    if input_files and input_files[0].suffix.lower() in MATROSKA_INPUT_SUFFIXES:
        return input_files[0].suffix.lower()
    return ".mkv"


def output_path_for(
    input_path: Path,
    source_root: Path | None,
    output_dir: Path | None = None,
    output_suffix: str | None = None,
) -> Path:
    output_name = output_name_for(input_path, output_suffix)

    if source_root is None:
        base_dir = output_dir or (input_path.parent / SORTED_DIR_NAME)
        return base_dir / output_name

    relative_path = input_path.relative_to(source_root)
    relative_output = relative_path.with_name(output_name)
    base_dir = output_dir or (source_root / SORTED_DIR_NAME)
    return base_dir / relative_output


def merge_output_path_for(
    input_files: list[Path],
    output_dir: Path | None = None,
    output_suffix: str | None = None,
) -> Path:
    if not input_files:
        raise OrganizerError("No input files to merge.")
    primary = next((path for path in input_files if path.suffix.lower() == ".mkv"), input_files[0])
    suffix = output_suffix_text(output_suffix) or ".merged"
    output_name = f"{primary.stem}{suffix}{merge_output_extension_for(input_files)}"
    base_dir = output_dir or (primary.parent / SORTED_DIR_NAME)
    return base_dir / output_name


def should_skip_generated_mkv_path(source_root: Path, candidate: Path) -> bool:
    generated_dirs = {SORTED_DIR_NAME.lower(), OCR_CACHE_DIR_NAME.lower()}
    try:
        relative_parts = candidate.relative_to(source_root).parts[:-1]
    except ValueError:
        relative_parts = candidate.parts[:-1]
    return any(part.lower() in generated_dirs for part in relative_parts)


def collect_mkv_files(input_path: Path, recursive: bool) -> tuple[list[Path], Path | None]:
    if input_path.is_file():
        if not is_matroska_input_file(input_path):
            raise OrganizerError(f"File is not a supported Matroska input (.mkv/.mka): {input_path}")
        return [input_path], None

    if not input_path.is_dir():
        raise OrganizerError(f"Path not found: {input_path}")

    files: list[Path] = []
    seen: set[str] = set()

    for pattern in matroska_input_glob_patterns(recursive):
        for path in sorted(input_path.glob(pattern)):
            key = str(path.resolve()).casefold()
            if key in seen:
                continue
            seen.add(key)
            if should_skip_generated_mkv_path(input_path, path):
                continue
            files.append(path)

    files.sort()

    if not files:
        raise OrganizerError(f"No supported Matroska files (.mkv/.mka) found in: {input_path}")

    return files, input_path


def collect_mkv_files_from_paths(input_paths: list[Path], recursive: bool) -> tuple[list[Path], Path | None]:
    if not input_paths:
        raise OrganizerError("Provide at least one Matroska file (.mkv/.mka) or folder.")

    if len(input_paths) == 1:
        return collect_mkv_files(input_paths[0], recursive)

    files: list[Path] = []
    seen: set[str] = set()

    for input_path in input_paths:
        collected, _source_root = collect_mkv_files(input_path, recursive)
        for file_path in collected:
            key = str(file_path.resolve()).casefold()
            if key in seen:
                continue
            seen.add(key)
            files.append(file_path)

    if not files:
        raise OrganizerError("No supported Matroska files (.mkv/.mka) found in the selected inputs.")

    return files, None


def collect_batch_language_variant_consensus(
    input_files: list[Path],
    args: argparse.Namespace,
) -> dict[str, str]:
    if (
        not args.detect_language_variants
        or not getattr(args, "batch_language_variant_consensus", True)
        or len(input_files) < 2
    ):
        return {}

    print("\nAnalyzing batch variant consensus...")
    votes: dict[str, list[str]] = {}

    for index, input_path in enumerate(input_files, start=1):
        try:
            print(f"  [{index}/{len(input_files)}] {input_path.name}")
            metadata = load_metadata(args.mkvmerge, input_path)
            tracks = build_tracks(metadata)
            subtitles = [track for track in tracks if track.type == "subtitles"]
            if not subtitles:
                continue

            apply_subtitle_language_overrides(subtitles, args.subtitle_language_overrides)
            variant_subtitles = [
                track
                for track in subtitles
                if variant_base_language_key(track) in LANGUAGE_VARIANT_CLASSIFIERS
            ]
            if not variant_subtitles:
                continue

            analyze_subtitle_sizes(input_path, variant_subtitles, args.mkvextract)
            cache_dir = args.ocr_cache_dir or (input_path.parent / OCR_CACHE_DIR_NAME)
            ensure_pgs_ocr_cache(
                input_path=input_path,
                subtitles=variant_subtitles,
                cache_dir=cache_dir,
                mkvextract=args.mkvextract,
                pgs_ocr_command=args.pgs_ocr_command,
                auto_pgs_ocr=args.auto_pgs_ocr,
                auto_commentary_ocr=False,
                seconv=args.seconv,
                subtitle_edit=args.subtitle_edit,
                tesseract=args.tesseract,
                tessdata_dirs=args.tessdata_dirs,
                pgs_ocr_language=args.pgs_ocr_language,
                pgs_ocr_timeout_seconds=args.pgs_ocr_timeout_seconds,
                allow_legacy_subtitle_edit_ocr=args.allow_subtitle_edit_legacy_ocr,
                auto_download_tessdata=args.auto_download_tessdata,
                tessdata_model=args.tessdata_model,
                force_pgs_ocr=args.force_pgs_ocr,
            )
            attach_cached_ocr_text(input_path, variant_subtitles, cache_dir)

            max_sizes = max_subtitle_sizes_by_language(variant_subtitles)
            max_pgs_events = max_pgs_metric_by_language(variant_subtitles, "display_events")
            file_votes: dict[str, set[str]] = {}

            for track in variant_subtitles:
                base_code = variant_base_language_key(track)
                size_base_code = base_language_key(track)
                if has_commentary_flag(track) or has_sdh_subtitle_hint(track):
                    continue
                if not subtitle_looks_full_variant_anchor(
                    track,
                    max_sizes.get(size_base_code),
                    max_pgs_events.get(size_base_code),
                ):
                    continue

                result, _source = variant_detection_result(input_path, track, cache_dir)
                vote = language_variant_vote_for_track(track, result)
                if vote:
                    file_votes.setdefault(base_code, set()).add(vote)

            for base_code, codes in file_votes.items():
                if len(codes) == 1:
                    votes.setdefault(base_code, []).append(next(iter(codes)))

        except OrganizerError as error:
            print(f"  Warning: could not pre-analyze {input_path.name}: {error}")

    consensus = batch_language_variant_consensus_from_votes(votes)
    if not consensus:
        print("  No strong variant consensus in batch.")
        return {}

    for base_code, consensus_code in sorted(consensus.items()):
        total = len(votes.get(base_code, []))
        count = votes.get(base_code, []).count(consensus_code)
        print(
            f"  {language_display_name(base_code)}: {language_display_name(consensus_code)} "
            f"({count}/{total} files with votes)"
        )

    return consensus


def explicit_metadata_variant_votes(subtitles: list[TrackInfo]) -> dict[str, set[str]]:
    votes: dict[str, set[str]] = {}

    for track in subtitles:
        base_code = variant_base_language_key(track)
        if base_code not in LANGUAGE_VARIANT_CLASSIFIERS:
            continue
        if has_forced_flag(track) or has_commentary_flag(track) or has_sdh_subtitle_hint(track):
            continue

        vote = explicit_variant_hint_for_track(track, base_code)
        if vote:
            votes.setdefault(base_code, set()).add(vote)

    return votes


def collect_sibling_metadata_variant_consensus(
    input_files: list[Path],
    args: argparse.Namespace,
) -> dict[str, str]:
    if (
        not args.detect_language_variants
        or not getattr(args, "batch_language_variant_consensus", True)
        or not input_files
    ):
        return {}

    context_files: list[Path] = []
    seen: set[str] = {str(path.resolve()).casefold() for path in input_files}
    parent_dirs = sorted({path.parent for path in input_files})

    for parent_dir in parent_dirs:
        for pattern in matroska_input_glob_patterns(False):
            for candidate in sorted(parent_dir.glob(pattern)):
                key = str(candidate.resolve()).casefold()
                if key in seen:
                    continue
                seen.add(key)
                context_files.append(candidate)

    for context_dir in getattr(args, "variant_context_dirs", []) or []:
        for pattern in matroska_input_glob_patterns(True):
            for candidate in sorted(context_dir.glob(pattern)):
                if should_skip_generated_mkv_path(context_dir, candidate):
                    continue
                key = str(candidate.resolve()).casefold()
                if key in seen:
                    continue
                seen.add(key)
                context_files.append(candidate)

    if not context_files:
        return {}

    print("\nAnalyzing sibling-file variant consensus from metadata...")
    votes: dict[str, list[str]] = {}

    for input_path in context_files:
        try:
            metadata = load_metadata(args.mkvmerge, input_path)
            tracks = build_tracks(metadata)
            subtitles = [track for track in tracks if track.type == "subtitles"]
            file_votes = explicit_metadata_variant_votes(subtitles)
            for base_code, codes in file_votes.items():
                if len(codes) == 1:
                    votes.setdefault(base_code, []).append(next(iter(codes)))
        except OrganizerError as error:
            print(f"  Warning: could not read metadata from {input_path.name}: {error}")

    consensus = batch_language_variant_consensus_from_votes(votes)
    if not consensus:
        print("  No strong consensus in sibling files.")
        return {}

    for base_code, consensus_code in sorted(consensus.items()):
        total = len(votes.get(base_code, []))
        count = votes.get(base_code, []).count(consensus_code)
        print(
            f"  {language_display_name(base_code)}: {language_display_name(consensus_code)} "
            f"({count}/{total} sibling files with explicit metadata)"
        )

    return consensus


def merge_variant_consensus(primary: dict[str, str], fallback: dict[str, str]) -> dict[str, str]:
    merged = dict(primary)
    for base_code, code in fallback.items():
        merged.setdefault(base_code, code)
    return merged


def remux_skip_message(input_path: Path, output_path: Path, args: argparse.Namespace) -> str | None:
    if output_path.resolve() == input_path.resolve():
        raise OrganizerError(
            "Calculated output is the same as the input. Use a different output folder or --output-suffix."
        )

    output_exists = output_path.exists()
    if output_exists and args.skip_existing:
        return "output already exists; file skipped"

    if output_exists and not args.dry_run and not args.overwrite:
        raise OrganizerError(
            f"Output already exists for this file:\n{output_path}\n"
            "Will not overwrite. Use --overwrite, --skip-existing, or --output-suffix."
        )
    if output_exists and args.overwrite:
        action = "would overwrite" if args.dry_run else "will overwrite"
        print(f"Warning: output already exists; {action}: {output_path}")
    elif output_exists and args.dry_run:
        print("Warning: output already exists; because this is dry-run, continuing only to show the plan.")

    return None


def merge_remux_skip_message(input_files: list[Path], output_path: Path, args: argparse.Namespace) -> str | None:
    for input_path in input_files:
        if output_path.resolve() == input_path.resolve():
            raise OrganizerError(
                "Calculated merge output is the same as one of the inputs. "
                "Use a different output folder or --output-suffix."
            )

    output_exists = output_path.exists()
    if output_exists and args.skip_existing:
        return "output already exists; merge skipped"

    if output_exists and not args.dry_run and not args.overwrite:
        raise OrganizerError(
            f"Output already exists for this merge:\n{output_path}\n"
            "Will not overwrite. Use --overwrite, --skip-existing, or --output-suffix."
        )
    if output_exists and args.overwrite:
        action = "would overwrite" if args.dry_run else "will overwrite"
        print(f"Warning: merge output already exists; {action}: {output_path}")
    elif output_exists and args.dry_run:
        print("Warning: merge output already exists; because this is dry-run, continuing only to show the plan.")

    return None


def command_with_gui_mode(command: list[str]) -> list[str]:
    if "--gui-mode" in command:
        return command
    return [command[0], "--gui-mode", *command[1:]]


def run_command_with_progress(
    command: list[str],
    progress_callback: Callable[[str, int, int], None] | None,
    message: str,
    start_step: int,
    end_step: int,
    cancel_callback: Callable[[], bool] | None = None,
) -> subprocess.CompletedProcess:
    ensure_not_cancelled(cancel_callback)

    command_to_run = command_with_gui_mode(command) if progress_callback else command
    process = subprocess.Popen(
        command_to_run,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    assert process.stdout is not None

    output_queue: queue.Queue[str | None] = queue.Queue()

    def read_output() -> None:
        try:
            for output_line in process.stdout:
                output_queue.put(output_line)
        finally:
            output_queue.put(None)

    reader_thread = threading.Thread(target=read_output, daemon=True)
    reader_thread.start()

    output_closed = False
    while True:
        if cancel_callback and cancel_callback():
            terminate_process(process)
            reader_thread.join(timeout=1)
            raise OrganizerCancelled("Operation cancelled.")

        try:
            line = output_queue.get(timeout=0.1)
        except queue.Empty:
            if process.poll() is not None and output_closed:
                break
            continue

        if line is None:
            output_closed = True
            if process.poll() is not None:
                break
            continue

        match = re.search(r"#GUI#progress\s+(\d+)", line)
        if match:
            percent = max(0, min(100, int(match.group(1))))
            step = start_step + round((end_step - start_step) * percent / 100)
            if progress_callback:
                progress_callback(f"{message} ({percent}%)", step, 100)
            continue
        print(line, end="")

    reader_thread.join(timeout=1)
    return subprocess.CompletedProcess(command_to_run, process.wait())


def process_file(
    input_path: Path,
    output_path: Path,
    args: argparse.Namespace,
    forced_subtitle_ids: set[int],
    batch_variant_consensus: dict[str, str] | None = None,
    progress_callback: Callable[[str, int, int], None] | None = None,
    cancel_callback: Callable[[], bool] | None = None,
) -> dict[str, Any]:
    def progress(message: str, step: int, steps: int = 100) -> None:
        ensure_not_cancelled(cancel_callback)
        if progress_callback:
            progress_callback(message, step, steps)

    print("\n====================================")
    print(f"Processing:  {input_path}")
    print(f"Output:      {output_path}")
    print("====================================")

    progress("Reading metadata", 5)
    metadata = load_metadata(args.mkvmerge, input_path)
    tracks = build_tracks(metadata, source_index=0, source_path=input_path)

    videos = [track for track in tracks if track.type == "video"]
    audio_tracks = [track for track in tracks if track.type == "audio"]
    subtitles = [track for track in tracks if track.type == "subtitles"]

    if not videos and not audio_tracks and not subtitles:
        raise OrganizerError("No video/audio/subtitle tracks found in this Matroska file.")

    apply_subtitle_language_overrides(subtitles, args.subtitle_language_overrides)
    apply_track_delay_overrides(audio_tracks, subtitles, args.audio_delay_overrides, args.subtitle_delay_overrides)
    if args.detect_language_variants:
        apply_default_language_variants(subtitles)
    audio_name_style = getattr(args, "audio_name_style", "auto")
    language_order_style = getattr(args, "language_order_style", "default")
    regional_order = getattr(args, "regional_order", None)
    apply_audio_names(audio_tracks, audio_name_style)

    progress("Analyzing subtitle sizes", 15)
    needs_subtitle_sizes = (
        args.analyze_sub_sizes
        or args.smart_sub_detection
        or args.drop_empty_subs
        or getattr(args, "detect_duplicate_tracks", True)
        or args.detect_language_variants
        or args.auto_commentary_ocr
    )
    if needs_subtitle_sizes and subtitles:
        analyze_subtitle_sizes(input_path, subtitles, args.mkvextract)
    if args.detect_language_variants:
        apply_ordered_pgs_language_variants(subtitles)

    progress("Preparing OCR cache", 30)
    needs_ocr_cache = args.detect_language_variants or args.auto_commentary_ocr or args.prepare_pgs_ocr
    if needs_ocr_cache:
        cache_dir = args.ocr_cache_dir or (input_path.parent / OCR_CACHE_DIR_NAME)
        def ocr_progress(message: str, index: int, total: int) -> None:
            step = 30 + int(max(0, index - 1) * 15 / max(1, total))
            progress(message, min(step, 44))

        if args.prepare_pgs_ocr:
            extract_pgs_for_manual_ocr(
                input_path=input_path,
                subtitles=subtitles,
                cache_dir=cache_dir,
                mkvextract=args.mkvextract,
            )
        ensure_pgs_ocr_cache(
            input_path=input_path,
            subtitles=subtitles,
            cache_dir=cache_dir,
            mkvextract=args.mkvextract,
            pgs_ocr_command=args.pgs_ocr_command,
            auto_pgs_ocr=args.auto_pgs_ocr,
            auto_commentary_ocr=args.auto_commentary_ocr,
            seconv=args.seconv,
            subtitle_edit=args.subtitle_edit,
            tesseract=args.tesseract,
            tessdata_dirs=args.tessdata_dirs,
            pgs_ocr_language=args.pgs_ocr_language,
            pgs_ocr_timeout_seconds=args.pgs_ocr_timeout_seconds,
            allow_legacy_subtitle_edit_ocr=args.allow_subtitle_edit_legacy_ocr,
            auto_download_tessdata=args.auto_download_tessdata,
            tessdata_model=args.tessdata_model,
            force_pgs_ocr=args.force_pgs_ocr,
            progress_callback=ocr_progress,
            cancel_callback=cancel_callback,
        )
        attach_cached_ocr_text(input_path, subtitles, cache_dir)

    progress("Detecting language variants", 45)
    if args.detect_language_variants:
        cache_dir = args.ocr_cache_dir or (input_path.parent / OCR_CACHE_DIR_NAME)
        detect_language_variants(input_path, subtitles, cache_dir, batch_variant_consensus)

    progress("Classifying tracks", 60)
    classify_subtitle_roles(
        subtitles=subtitles,
        audio_tracks=audio_tracks,
        forced_subtitle_ids=forced_subtitle_ids,
        smart_sub_detection=args.smart_sub_detection,
        drop_empty_subs=args.drop_empty_subs,
    )
    infer_audio_commentary_from_subtitles(audio_tracks, subtitles)
    apply_audio_names(audio_tracks, audio_name_style)
    if getattr(args, "detect_duplicate_tracks", True):
        detect_duplicate_tracks(input_path, audio_tracks, subtitles)
    apply_track_selection_overrides(
        videos,
        audio_tracks,
        subtitles,
        getattr(args, "track_selection_overrides", None),
    )
    apply_default_flags(videos, audio_tracks, subtitles, language_order_style, regional_order)

    progress("Building track plan", 70)
    if args.analyze_sub_sizes:
        print_subtitle_size_report(subtitles)
    if args.smart_sub_detection:
        print_subtitle_role_score_report(subtitles)

    print_track_plan(videos, audio_tracks, subtitles, language_order_style, regional_order)
    print_track_explanations(subtitles, args.explain_track_ids)

    metadata_plan = metadata_edit_plan(videos, audio_tracks, subtitles, language_order_style, regional_order)
    metadata_mode = getattr(args, "metadata_edit_mode", "off")
    if metadata_mode in {"auto", "only"}:
        print(f"\nMetadata-only plan: {'yes' if metadata_plan.can_edit else 'no'} ({metadata_plan.reason})")

    if metadata_mode in {"auto", "only"} and metadata_plan.can_edit:
        if not metadata_plan.edits:
            print("Execution: nothing to change in the original file.")
            progress("Nothing to change", 100)
            return file_report_data(
                input_path,
                input_path,
                "unchanged",
                videos=videos,
                audio_tracks=audio_tracks,
                subtitles=subtitles,
                message=metadata_plan.reason,
            )

        if not args.mkvpropedit:
            if metadata_mode == "only":
                raise OrganizerError("metadata_edit_mode=only needs mkvpropedit, but it was not found.")
            print("Warning: mkvpropedit was not found; falling back to normal remux.")
        else:
            command = build_mkvpropedit_command(args.mkvpropedit, input_path, metadata_plan.edits)

            print("\nmkvpropedit command:")
            print(format_command(command))
            print("Execution: metadata-only in-place (no remux).")

            if args.dry_run:
                print("DRY-RUN active: original file was not changed.")
                progress("Preview complete", 100)
                return file_report_data(
                    input_path,
                    input_path,
                    "dry-run",
                    command=command,
                    videos=videos,
                    audio_tracks=audio_tracks,
                    subtitles=subtitles,
                    message="metadata-only via mkvpropedit",
                )

            progress("Writing metadata", 85)
            result = run_command_with_progress(
                command,
                progress_callback,
                "Writing metadata",
                85,
                100,
                cancel_callback,
            )
            if result.returncode != 0:
                raise OrganizerError(f"mkvpropedit failed with exit code {result.returncode}: {input_path}")

            print(f"Metadata-only completed: {input_path}")
            progress("Metadata updated", 100)
            return file_report_data(
                input_path,
                input_path,
                "metadata-edited",
                command=command,
                videos=videos,
                audio_tracks=audio_tracks,
                subtitles=subtitles,
                message="metadata-only via mkvpropedit",
            )

    if metadata_mode == "only":
        raise OrganizerError(f"metadata_edit_mode=only cannot be used: {metadata_plan.reason}.")

    skip_message = remux_skip_message(input_path, output_path, args)
    if skip_message:
        print(f"Skipping: {skip_message}.")
        progress("Skipped", 100)
        return file_report_data(input_path, output_path, "skipped", message=skip_message)

    progress("Building command", 80)
    command = build_mkvmerge_command(
        mkvmerge=args.mkvmerge,
        input_path=input_path,
        output_path=output_path,
        videos=videos,
        audio_tracks=audio_tracks,
        subtitles=subtitles,
        language_order_style=language_order_style,
        regional_order=regional_order,
    )

    print("\nmkvmerge command:")
    print(format_command(command))

    if args.dry_run:
        print("DRY-RUN active: final remux was not executed.")
        progress("Preview complete", 100)
        return file_report_data(
            input_path,
            output_path,
            "dry-run",
            command=command,
            videos=videos,
            audio_tracks=audio_tracks,
            subtitles=subtitles,
        )

    output_path.parent.mkdir(parents=True, exist_ok=True)
    if output_path.exists() and args.overwrite:
        output_path.unlink()
    progress("Remuxing output", 85)
    result = run_command_with_progress(
        command,
        progress_callback,
        "Remuxing output",
        85,
        100,
        cancel_callback,
    )

    if result.returncode != 0:
        raise OrganizerError(f"mkvmerge failed with exit code {result.returncode}: {input_path}")

    print(f"Completed: {output_path}")
    progress("File complete", 100)
    return file_report_data(
        input_path,
        output_path,
        "processed",
        command=command,
        videos=videos,
        audio_tracks=audio_tracks,
        subtitles=subtitles,
    )


def tracks_for_source(tracks: list[TrackInfo], source_index: int, track_type: str | None = None) -> list[TrackInfo]:
    return [
        track for track in tracks
        if track.source_index == source_index and (track_type is None or track.type == track_type)
    ]


def process_merged_inputs(
    input_files: list[Path],
    output_path: Path,
    args: argparse.Namespace,
    forced_subtitle_ids: set[int],
    batch_variant_consensus: dict[str, str] | None = None,
    progress_callback: Callable[[str, int, int], None] | None = None,
    cancel_callback: Callable[[], bool] | None = None,
) -> dict[str, Any]:
    def progress(message: str, step: int, steps: int = 100) -> None:
        ensure_not_cancelled(cancel_callback)
        if progress_callback:
            progress_callback(message, step, steps)

    if len(input_files) < 2:
        raise OrganizerError("Merge mode needs at least two Matroska input files.")

    print("\n====================================")
    print("Merging sources:")
    for input_path in input_files:
        print(f"  - {input_path}")
    print(f"Output: {output_path}")
    print("====================================")

    progress("Reading metadata", 5)
    all_tracks: list[TrackInfo] = []
    for source_index, input_path in enumerate(input_files):
        metadata = load_metadata(args.mkvmerge, input_path)
        tracks = build_tracks(metadata, source_index=source_index, source_path=input_path)
        all_tracks.extend(tracks)

    videos = [track for track in all_tracks if track.type == "video"]
    audio_tracks = [track for track in all_tracks if track.type == "audio"]
    subtitles = [track for track in all_tracks if track.type == "subtitles"]

    if not videos and not audio_tracks and not subtitles:
        raise OrganizerError("No video/audio/subtitle tracks found in the selected Matroska files.")

    primary_video_source_index = next((track.source_index for track in sorted(videos, key=lambda item: item.order)), 0)
    for track in videos:
        track.drop = track.source_index != primary_video_source_index

    apply_subtitle_language_overrides(subtitles, args.subtitle_language_overrides)
    apply_track_delay_overrides(audio_tracks, subtitles, args.audio_delay_overrides, args.subtitle_delay_overrides)
    if args.detect_language_variants:
        apply_default_language_variants(subtitles)

    audio_name_style = getattr(args, "audio_name_style", "auto")
    language_order_style = getattr(args, "language_order_style", "default")
    regional_order = getattr(args, "regional_order", None)
    apply_audio_names(audio_tracks, audio_name_style)

    progress("Analyzing subtitle sizes", 15)
    needs_subtitle_sizes = (
        args.analyze_sub_sizes
        or args.smart_sub_detection
        or args.drop_empty_subs
        or getattr(args, "detect_duplicate_tracks", True)
        or args.detect_language_variants
        or args.auto_commentary_ocr
    )
    if needs_subtitle_sizes and subtitles:
        for source_index, input_path in enumerate(input_files):
            source_subtitles = tracks_for_source(subtitles, source_index, "subtitles")
            if source_subtitles:
                analyze_subtitle_sizes(input_path, source_subtitles, args.mkvextract)
    if args.detect_language_variants:
        for source_index in range(len(input_files)):
            apply_ordered_pgs_language_variants(tracks_for_source(subtitles, source_index, "subtitles"))

    progress("Preparing OCR cache", 30)
    needs_ocr_cache = args.detect_language_variants or args.auto_commentary_ocr or args.prepare_pgs_ocr
    if needs_ocr_cache:
        for source_index, input_path in enumerate(input_files):
            source_subtitles = tracks_for_source(subtitles, source_index, "subtitles")
            if not source_subtitles:
                continue
            cache_dir = args.ocr_cache_dir or (input_path.parent / OCR_CACHE_DIR_NAME)

            def ocr_progress(message: str, index: int, total: int, source_name: str = input_path.name) -> None:
                step = 30 + int(max(0, index - 1) * 15 / max(1, total))
                progress(f"{source_name}: {message}", min(step, 44))

            if args.prepare_pgs_ocr:
                extract_pgs_for_manual_ocr(
                    input_path=input_path,
                    subtitles=source_subtitles,
                    cache_dir=cache_dir,
                    mkvextract=args.mkvextract,
                )
            ensure_pgs_ocr_cache(
                input_path=input_path,
                subtitles=source_subtitles,
                cache_dir=cache_dir,
                mkvextract=args.mkvextract,
                pgs_ocr_command=args.pgs_ocr_command,
                auto_pgs_ocr=args.auto_pgs_ocr,
                auto_commentary_ocr=args.auto_commentary_ocr,
                seconv=args.seconv,
                subtitle_edit=args.subtitle_edit,
                tesseract=args.tesseract,
                tessdata_dirs=args.tessdata_dirs,
                pgs_ocr_language=args.pgs_ocr_language,
                pgs_ocr_timeout_seconds=args.pgs_ocr_timeout_seconds,
                allow_legacy_subtitle_edit_ocr=args.allow_subtitle_edit_legacy_ocr,
                auto_download_tessdata=args.auto_download_tessdata,
                tessdata_model=args.tessdata_model,
                force_pgs_ocr=args.force_pgs_ocr,
                progress_callback=ocr_progress,
                cancel_callback=cancel_callback,
            )
            attach_cached_ocr_text(input_path, source_subtitles, cache_dir)

    progress("Detecting language variants", 45)
    if args.detect_language_variants:
        for source_index, input_path in enumerate(input_files):
            source_subtitles = tracks_for_source(subtitles, source_index, "subtitles")
            if source_subtitles:
                cache_dir = args.ocr_cache_dir or (input_path.parent / OCR_CACHE_DIR_NAME)
                detect_language_variants(input_path, source_subtitles, cache_dir, batch_variant_consensus)

    progress("Classifying tracks", 60)
    for source_index in range(len(input_files)):
        classify_subtitle_roles(
            subtitles=tracks_for_source(subtitles, source_index, "subtitles"),
            audio_tracks=tracks_for_source(audio_tracks, source_index, "audio"),
            forced_subtitle_ids=forced_subtitle_ids,
            smart_sub_detection=args.smart_sub_detection,
            drop_empty_subs=args.drop_empty_subs,
        )
    infer_audio_commentary_from_subtitles(audio_tracks, subtitles)
    apply_audio_names(audio_tracks, audio_name_style)
    if getattr(args, "detect_duplicate_tracks", True):
        detect_duplicate_tracks(input_files[0], audio_tracks, subtitles)
    apply_track_selection_overrides(
        videos,
        audio_tracks,
        subtitles,
        getattr(args, "track_selection_overrides", None),
    )
    apply_default_flags(videos, audio_tracks, subtitles, language_order_style, regional_order)

    progress("Building track plan", 70)
    if args.analyze_sub_sizes:
        print_subtitle_size_report(subtitles)
    if args.smart_sub_detection:
        print_subtitle_role_score_report(subtitles)
    print_track_plan(videos, audio_tracks, subtitles, language_order_style, regional_order)
    print_track_explanations(subtitles, args.explain_track_ids)

    metadata_mode = getattr(args, "metadata_edit_mode", "off")
    if metadata_mode in {"auto", "only"}:
        print("\nMetadata-only plan: no (multiple input sources require remux)")
    if metadata_mode == "only":
        raise OrganizerError("metadata_edit_mode=only cannot merge multiple input sources.")

    skip_message = merge_remux_skip_message(input_files, output_path, args)
    if skip_message:
        print(f"Skipping: {skip_message}.")
        progress("Skipped", 100)
        return file_report_data(
            input_files[0],
            output_path,
            "skipped",
            videos=videos,
            audio_tracks=audio_tracks,
            subtitles=subtitles,
            message=skip_message,
            input_paths=input_files,
        )

    progress("Building command", 80)
    command = build_mkvmerge_command(
        mkvmerge=args.mkvmerge,
        input_path=input_files,
        output_path=output_path,
        videos=videos,
        audio_tracks=audio_tracks,
        subtitles=subtitles,
        language_order_style=language_order_style,
        regional_order=regional_order,
    )

    print("\nmkvmerge command:")
    print(format_command(command))

    if args.dry_run:
        print("DRY-RUN active: final merge was not executed.")
        progress("Preview complete", 100)
        return file_report_data(
            input_files[0],
            output_path,
            "dry-run",
            command=command,
            videos=videos,
            audio_tracks=audio_tracks,
            subtitles=subtitles,
            message=f"merged preview from {len(input_files)} sources",
            input_paths=input_files,
        )

    output_path.parent.mkdir(parents=True, exist_ok=True)
    if output_path.exists() and args.overwrite:
        output_path.unlink()
    progress("Merging output", 85)
    result = run_command_with_progress(
        command,
        progress_callback,
        "Merging output",
        85,
        100,
        cancel_callback,
    )

    if result.returncode != 0:
        raise OrganizerError(f"mkvmerge failed with exit code {result.returncode}: {output_path}")

    print(f"Completed merge: {output_path}")
    progress("Merge complete", 100)
    return file_report_data(
        input_files[0],
        output_path,
        "processed",
        command=command,
        videos=videos,
        audio_tracks=audio_tracks,
        subtitles=subtitles,
        message=f"merged {len(input_files)} sources",
        input_paths=input_files,
    )


def build_parser(config_defaults: dict[str, Any] | None = None) -> argparse.ArgumentParser:
    config_defaults = config_defaults or {}

    def default(name: str, fallback: Any = None) -> Any:
        return config_defaults.get(name, fallback)

    parser = argparse.ArgumentParser(
        description="Organize Matroska track metadata and order in batch using MKVToolNix.",
    )
    parser.set_defaults(
        smart_sub_detection=default("smart_sub_detection", True),
        detect_duplicate_tracks=default("detect_duplicate_tracks", True),
        merge_inputs=default("merge_inputs", False),
        detect_language_variants=default("detect_language_variants", True),
        auto_pgs_ocr=default("auto_pgs_ocr", True),
        auto_commentary_ocr=default("auto_commentary_ocr", True),
        batch_language_variant_consensus=default("batch_language_variant_consensus", True),
        drop_empty_subs=default("drop_empty_subs", True),
        auto_download_tessdata=default("auto_download_tessdata", True),
        recursive=default("recursive", False),
        dry_run=default("dry_run", False),
        analyze_sub_sizes=default("analyze_sub_sizes", False),
        prepare_pgs_ocr=default("prepare_pgs_ocr", False),
        allow_subtitle_edit_legacy_ocr=default("allow_subtitle_edit_legacy_ocr", False),
        force_pgs_ocr=default("force_pgs_ocr", False),
        overwrite=default("overwrite", False),
        skip_existing=default("skip_existing", False),
        report=default("report", False),
        track_selection_overrides={},
    )
    parser.add_argument("path", nargs="?", type=Path, default=default("path"), help="Matroska file (.mkv/.mka) or folder.")
    parser.add_argument("--config", type=Path, default=None, help="JSON config with personal defaults.")
    parser.add_argument("--no-config", action="store_true", help="Ignore mkv_track_organizer.config.json.")
    parser.add_argument("--recursive", action="store_true", help="Search for Matroska files inside subfolders.")
    parser.add_argument("--dry-run", action="store_true", help="Show the plan and command without running the final remux.")
    parser.add_argument(
        "--merge-inputs",
        dest="merge_inputs",
        action="store_true",
        help=(
            "Merge all discovered/selected Matroska inputs into one output. "
            "The first source with video supplies video; audio/subtitle tracks are taken from every source."
        ),
    )
    parser.add_argument(
        "--no-merge-inputs",
        dest="merge_inputs",
        action="store_false",
        help="Process multiple inputs as a batch instead of one merged output.",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=default("output_dir"),
        help="Alternative output folder. Default: _sorted next to the inputs.",
    )
    parser.add_argument(
        "--output-suffix",
        default=default("output_suffix", ""),
        help='Optional suffix before the extension. Example: --output-suffix fixed -> movie.fixed.mkv.',
    )
    parser.add_argument("--overwrite", action="store_true", help="Overwrite existing outputs.")
    parser.add_argument("--skip-existing", action="store_true", help="Skip files whose output already exists.")
    parser.add_argument("--report", action="store_true", help="Generate a batch report in TXT/JSON.")
    parser.add_argument(
        "--report-dir",
        type=Path,
        default=default("report_dir"),
        help="Report folder. Default: _reports next to output/source.",
    )
    parser.add_argument(
        "--report-format",
        choices=["json", "txt", "both"],
        default=default("report_format", "both"),
        help="Report format when --report is enabled. Default: both.",
    )
    parser.add_argument(
        "--explain-track",
        default=default("explain_track", ""),
        help="Subtitle IDs to explain in detail, separated by commas. Example: 10,14",
    )
    parser.add_argument(
        "--metadata-edit-mode",
        choices=sorted(METADATA_EDIT_MODES),
        default=default("metadata_edit_mode", "off"),
        help=(
            "Control use of mkvpropedit for metadata-only changes: "
            "off=always remux, auto=use mkvpropedit when possible, only=error if remux would be needed. Default: off."
        ),
    )
    parser.add_argument(
        "--audio-name-style",
        choices=sorted(AUDIO_NAME_STYLES),
        default=default("audio_name_style", "auto"),
        help=(
            "Audio track naming style: auto=use format unless multiple audio languages are present, "
            "format=codec/channels only, language-format=language + codec/channels, keep=preserve existing names. "
            "Default: auto."
        ),
    )
    parser.add_argument(
        "--language-order-style",
        choices=sorted(LANGUAGE_ORDER_STYLES),
        default=default("language_order_style", "default"),
        help=(
            "Track language ordering style: default=existing order rules, "
            "regional=group languages by broad regions with Europe before Americas and Asia. Default: default."
        ),
    )
    parser.add_argument(
        "--regional-order",
        default=default("regional_order", []),
        help=(
            "Region order used when --language-order-style regional is active. "
            "Use comma-separated region names, e.g. asia,americas,europe. "
            "Missing regions are appended automatically."
        ),
    )
    parser.add_argument(
        "--variant-context-dir",
        action="append",
        type=Path,
        default=default("variant_context_dirs", []),
        help=(
            "Extra folder with reference Matroska files for language variant consensus. "
            "Can be repeated. Useful for isolated specials outside the season folder."
        ),
    )
    parser.add_argument(
        "--mkvmerge",
        type=Path,
        default=default("mkvmerge"),
        help="Path to mkvmerge.exe. Default: searches MKVMERGE, _tools, PATH, and Program Files.",
    )
    parser.add_argument(
        "--mkvextract",
        type=Path,
        default=default("mkvextract"),
        help=(
            "Path to mkvextract.exe. Default: same folder as mkvmerge.exe, "
            "MKVEXTRACT, _tools, PATH, or Program Files."
        ),
    )
    parser.add_argument(
        "--mkvpropedit",
        type=Path,
        default=default("mkvpropedit"),
        help="Path to mkvpropedit.exe. Default: same folder as mkvmerge.exe, _tools, PATH, or Program Files.",
    )
    parser.add_argument(
        "--subtitle-edit",
        type=Path,
        default=default("subtitle_edit"),
        help="Path to SubtitleEdit.exe, used only when legacy OCR is explicitly enabled.",
    )
    parser.add_argument(
        "--seconv",
        type=Path,
        default=default("seconv"),
        help="Path to seconv.exe. Default: searches SECONV, _tools next to the script, PATH, Desktop, and C:\\Tools.",
    )
    parser.add_argument(
        "--tesseract",
        type=Path,
        default=default("tesseract"),
        help="Path to tesseract.exe. Default: searches TESSERACT, _tools, PATH, and Program Files.",
    )
    parser.add_argument(
        "--tessdata-dir",
        type=Path,
        default=default("tessdata_dir"),
        help="Extra folder with .traineddata files. Default: _tools\\tessdata + tessdata from installed Tesseract.",
    )
    parser.add_argument(
        "--forced-subtitle-ids",
        default=default("forced_subtitle_ids", ""),
        help="Subtitle track IDs to mark as Forced, separated by commas. Example: 5,8,12",
    )
    parser.add_argument(
        "--subtitle-language-ids",
        action="append",
        default=list(default("subtitle_language_ids", [])),
        metavar="LANG:IDS",
        help=(
            "Override subtitle language for specific track IDs. Repeatable. "
            "Examples: --subtitle-language-ids spa:7,8 --subtitle-language-ids es-419:9"
        ),
    )
    parser.add_argument(
        "--audio-delays",
        default=default("audio_delays", ""),
        metavar="ID:MS[,ID:MS]",
        help="Apply audio track delays in milliseconds. Example: --audio-delays 1:150,2:-250",
    )
    parser.add_argument(
        "--subtitle-delays",
        default=default("subtitle_delays", ""),
        metavar="ID:MS[,ID:MS]",
        help="Apply subtitle track delays in milliseconds. Example: --subtitle-delays 5:-250",
    )
    parser.add_argument(
        "--analyze-sub-sizes",
        action="store_true",
        help="Also print the subtitle size/classification report.",
    )
    parser.add_argument(
        "--smart-sub-detection",
        dest="smart_sub_detection",
        action="store_true",
        help="Enable size-based scoring for Forced/Empty/Commentary. Enabled by default.",
    )
    parser.add_argument(
        "--no-smart-sub-detection",
        dest="smart_sub_detection",
        action="store_false",
        help="Disable automatic Forced/Empty/Commentary scoring.",
    )
    parser.add_argument(
        "--drop-empty-subs",
        action="store_true",
        help="Remove subtitles classified as empty from the final remux. Enabled by default.",
    )
    parser.add_argument(
        "--keep-empty-subs",
        dest="drop_empty_subs",
        action="store_false",
        help="Keep subtitles classified as empty in the final remux.",
    )
    parser.add_argument(
        "--detect-duplicate-tracks",
        dest="detect_duplicate_tracks",
        action="store_true",
        help="Highlight likely duplicate audio/subtitle tracks in the plan and report. Enabled by default.",
    )
    parser.add_argument(
        "--no-detect-duplicate-tracks",
        dest="detect_duplicate_tracks",
        action="store_false",
        help="Disable likely duplicate track detection.",
    )
    parser.add_argument(
        "--detect-pt-variant",
        dest="detect_language_variants",
        action="store_true",
        help=argparse.SUPPRESS,
    )
    parser.add_argument(
        "--no-detect-pt-variant",
        dest="detect_language_variants",
        action="store_false",
        help=argparse.SUPPRESS,
    )
    parser.add_argument(
        "--detect-language-variants",
        dest="detect_language_variants",
        action="store_true",
        help="Enable scoring to distinguish language variants: PT, ES, FR, ZH. Enabled by default.",
    )
    parser.add_argument(
        "--no-detect-language-variants",
        dest="detect_language_variants",
        action="store_false",
        help="Disable automatic language variant scoring.",
    )
    parser.add_argument(
        "--batch-language-variant-consensus",
        dest="batch_language_variant_consensus",
        action="store_true",
        help="Use consensus across batch files to correct weak variant guesses. Enabled by default.",
    )
    parser.add_argument(
        "--no-batch-language-variant-consensus",
        dest="batch_language_variant_consensus",
        action="store_false",
        help="Disable variant pre-scan/consensus across batch files.",
    )
    parser.add_argument(
        "--ocr-cache-dir",
        type=Path,
        default=default("ocr_cache_dir"),
        help="Alternative SRT cache folder. Default: _ocr_cache next to the input file.",
    )
    parser.add_argument(
        "--prepare-pgs-ocr",
        action="store_true",
        help="Extract relevant PGS tracks to _ocr_cache and show expected .srt names for manual OCR.",
    )
    parser.add_argument(
        "--pgs-ocr-command",
        default=default("pgs_ocr_command"),
        help=(
            "Optional command/template to convert PGS .sup to .srt before variant scoring. "
            "Placeholders: {input}, {output}, {mkv}, {track_id}."
        ),
    )
    parser.add_argument(
        "--auto-pgs-ocr",
        dest="auto_pgs_ocr",
        action="store_true",
        help="Try automatic OCR for relevant PGS tracks without cached .srt files. Enabled by default.",
    )
    parser.add_argument(
        "--no-auto-pgs-ocr",
        dest="auto_pgs_ocr",
        action="store_false",
        help="Disable automatic PGS OCR; use only existing .srt files in _ocr_cache.",
    )
    parser.add_argument(
        "--auto-commentary-ocr",
        dest="auto_commentary_ocr",
        action="store_true",
        help="Try automatic OCR on extra full-size PGS commentary/SDH candidates when language traineddata exists.",
    )
    parser.add_argument(
        "--no-auto-commentary-ocr",
        dest="auto_commentary_ocr",
        action="store_false",
        help="Disable extra automatic OCR for commentary/SDH candidates.",
    )
    parser.add_argument(
        "--allow-subtitle-edit-legacy-ocr",
        action="store_true",
        help="Allow OCR via SubtitleEdit.exe /convert when seconv is missing. May open the GUI in SE 5.",
    )
    parser.add_argument(
        "--pgs-ocr-language",
        default=default("pgs_ocr_language", "auto"),
        help="Language for automatic Tesseract/seconv OCR. Default: auto (use track language if available, otherwise eng).",
    )
    parser.add_argument(
        "--pgs-ocr-timeout-seconds",
        type=int,
        default=default("pgs_ocr_timeout_seconds", PGS_OCR_TIMEOUT_SECONDS),
        help=f"Per-track timeout for automatic PGS OCR. Default: {PGS_OCR_TIMEOUT_SECONDS}.",
    )
    parser.add_argument(
        "--force-pgs-ocr",
        action="store_true",
        help="Run PGS OCR again even if .srt already exists in _ocr_cache.",
    )
    parser.add_argument(
        "--auto-download-tessdata",
        dest="auto_download_tessdata",
        action="store_true",
        help="Automatically download missing .traineddata files to _tools\\tessdata. Enabled by default.",
    )
    parser.add_argument(
        "--no-auto-download-tessdata",
        dest="auto_download_tessdata",
        action="store_false",
        help="Do not download missing Tesseract models.",
    )
    parser.add_argument(
        "--tessdata-model",
        choices=sorted(TESSDATA_REPOS),
        default=default("tessdata_model", "best"),
        help="Source for automatically downloaded models: best or fast. Default: best.",
    )
    return parser


def emit_batch_event(
    callback: Callable[[BatchRunEvent], None] | None,
    kind: str,
    message: str,
    file: Path | None = None,
    index: int | None = None,
    total: int | None = None,
    step: int | None = None,
    steps: int | None = None,
) -> None:
    if callback:
        callback(
            BatchRunEvent(
                kind=kind,
                message=message,
                file=file,
                index=index,
                total=total,
                step=step,
                steps=steps,
            )
        )


def prepare_batch_run(args: argparse.Namespace, config_path: Path | None = None) -> BatchRunContext:
    args.config_path = config_path
    raw_input_paths = getattr(args, "input_paths", None)
    if raw_input_paths:
        args.input_paths = [Path(path).expanduser().resolve() for path in raw_input_paths]
        if not args.path:
            args.path = args.input_paths[0]
    elif not args.path:
        raise OrganizerError("Provide a Matroska file/folder or define 'path' in the config JSON.")

    args.path = Path(args.path).resolve()
    args.mkvmerge = resolve_tool_path(
        args.mkvmerge,
        "mkvmerge",
        "MKVMERGE",
        common_mkvtoolnix_paths("mkvmerge.exe"),
    )
    if args.mkvextract is None:
        mkvextract_fallbacks = []
        if args.mkvmerge:
            mkvextract_fallbacks.append(args.mkvmerge.with_name(MKVEXTRACT.name))
        mkvextract_fallbacks.extend(common_mkvtoolnix_paths("mkvextract.exe"))
        args.mkvextract = resolve_tool_path(None, "mkvextract", "MKVEXTRACT", mkvextract_fallbacks)
    else:
        args.mkvextract = resolve_tool_path(
            args.mkvextract,
            "mkvextract",
            "MKVEXTRACT",
            common_mkvtoolnix_paths("mkvextract.exe"),
        )

    mkvpropedit_fallbacks = []
    if args.mkvmerge:
        mkvpropedit_fallbacks.append(args.mkvmerge.with_name(MKVPROPEDIT.name))
    mkvpropedit_fallbacks.extend(common_mkvtoolnix_paths("mkvpropedit.exe"))
    args.mkvpropedit = resolve_tool_path(
        args.mkvpropedit,
        "mkvpropedit",
        "MKVPROPEDIT",
        mkvpropedit_fallbacks,
    )
    args.subtitle_edit = resolve_tool_path(
        args.subtitle_edit,
        "SubtitleEdit",
        "SUBTITLE_EDIT",
        common_subtitle_edit_paths(),
    )
    args.seconv = resolve_seconv_path(args.seconv)
    args.tesseract = resolve_tool_path(
        args.tesseract,
        "tesseract",
        "TESSERACT",
        common_tesseract_paths(),
    )
    args.pgs_ocr_language = detect_tesseract_language(args.subtitle_edit, args.pgs_ocr_language)
    args.tessdata_dirs = [local_tessdata_dir()]
    if args.tessdata_dir:
        args.tessdata_dirs.insert(0, Path(args.tessdata_dir).resolve())
    installed_dir = installed_tessdata_dir(args.tesseract)
    if installed_dir:
        args.tessdata_dirs.append(installed_dir)

    if args.pgs_ocr_timeout_seconds <= 0:
        raise OrganizerError("--pgs-ocr-timeout-seconds must be greater than zero.")
    if args.ocr_cache_dir:
        args.ocr_cache_dir = Path(args.ocr_cache_dir).resolve()
    if args.output_dir:
        args.output_dir = Path(args.output_dir).resolve()
    if args.report_dir:
        args.report_dir = Path(args.report_dir).resolve()

    raw_variant_context_dirs = getattr(args, "variant_context_dir", None)
    if raw_variant_context_dirs is None:
        raw_variant_context_dirs = getattr(args, "variant_context_dirs", [])
    args.variant_context_dirs = [
        Path(context_dir).expanduser().resolve()
        for context_dir in (raw_variant_context_dirs or [])
        if context_dir
    ]
    for context_dir in args.variant_context_dirs:
        if not context_dir.is_dir():
            raise OrganizerError(f"--variant-context-dir is not a valid folder: {context_dir}")

    if args.overwrite and args.skip_existing:
        raise OrganizerError("Use only one option: --overwrite or --skip-existing.")
    if args.report_format not in {"json", "txt", "both"}:
        raise OrganizerError("--report-format must be json, txt, or both.")
    if args.tessdata_model not in TESSDATA_REPOS:
        raise OrganizerError("--tessdata-model must be best or fast.")
    if args.metadata_edit_mode not in METADATA_EDIT_MODES:
        allowed = ", ".join(sorted(METADATA_EDIT_MODES))
        raise OrganizerError(f"--metadata-edit-mode must be one of these values: {allowed}.")
    args.audio_name_style = str(getattr(args, "audio_name_style", "auto") or "auto").strip().lower().replace("_", "-")
    if args.audio_name_style not in AUDIO_NAME_STYLES:
        allowed = ", ".join(sorted(AUDIO_NAME_STYLES))
        raise OrganizerError(f"--audio-name-style must be one of these values: {allowed}.")
    args.language_order_style = (
        str(getattr(args, "language_order_style", "default") or "default").strip().lower().replace("_", "-")
    )
    if args.language_order_style not in LANGUAGE_ORDER_STYLES:
        allowed = ", ".join(sorted(LANGUAGE_ORDER_STYLES))
        raise OrganizerError(f"--language-order-style must be one of these values: {allowed}.")
    args.regional_order = parse_regional_order(getattr(args, "regional_order", None))

    require_tool(args.mkvmerge, "mkvmerge")
    if args.metadata_edit_mode == "only":
        require_tool(args.mkvpropedit, "mkvpropedit")

    if (
        args.analyze_sub_sizes
        or args.smart_sub_detection
        or args.drop_empty_subs
        or args.detect_duplicate_tracks
        or args.detect_language_variants
        or args.prepare_pgs_ocr
        or args.auto_commentary_ocr
    ):
        require_tool(args.mkvextract, "mkvextract")

    forced_subtitle_ids = parse_id_list(args.forced_subtitle_ids, "--forced-subtitle-ids")
    args.subtitle_language_overrides = parse_subtitle_language_overrides(args.subtitle_language_ids)
    args.audio_delay_overrides = parse_track_delay_overrides(args.audio_delays, "--audio-delays")
    args.subtitle_delay_overrides = parse_track_delay_overrides(args.subtitle_delays, "--subtitle-delays")
    args.explain_track_ids = parse_id_list(args.explain_track, "--explain-track")
    if raw_input_paths:
        input_files, source_root = collect_mkv_files_from_paths(args.input_paths, args.recursive)
    else:
        input_files, source_root = collect_mkv_files(args.path, args.recursive)

    if args.merge_inputs and len(input_files) < 2:
        raise OrganizerError("Merge mode needs at least two Matroska files.")

    return BatchRunContext(
        args=args,
        input_files=input_files,
        source_root=source_root,
        forced_subtitle_ids=forced_subtitle_ids,
    )


def run_batch(
    context: BatchRunContext,
    event_callback: Callable[[BatchRunEvent], None] | None = None,
    cancel_callback: Callable[[], bool] | None = None,
) -> BatchRunResult:
    args = context.args
    input_files = context.input_files
    source_root = context.source_root
    failures = 0
    cancelled = False
    reports: list[dict[str, Any]] = []
    total_files = len(input_files)

    ensure_not_cancelled(cancel_callback)
    print(f"Matroska files found: {total_files}")
    emit_batch_event(event_callback, "batch-started", f"Matroska files found: {total_files}", total=total_files)

    try:
        batch_variant_consensus = collect_batch_language_variant_consensus(input_files, args)
        ensure_not_cancelled(cancel_callback)
        sibling_variant_consensus = collect_sibling_metadata_variant_consensus(input_files, args)
        batch_variant_consensus = merge_variant_consensus(batch_variant_consensus, sibling_variant_consensus)
        ensure_not_cancelled(cancel_callback)
    except OrganizerCancelled:
        print("\nBatch cancelled.")
        emit_batch_event(event_callback, "batch-cancelled", "Batch cancelled.")
        return BatchRunResult(
            reports=reports,
            failures=failures,
            input_files=input_files,
            source_root=source_root,
            cancelled=True,
        )

    if getattr(args, "merge_inputs", False):
        output_path = merge_output_path_for(
            input_files,
            output_dir=args.output_dir,
            output_suffix=args.output_suffix,
        )
        primary_file = input_files[0]
        emit_batch_event(
            event_callback,
            "file-started",
            f"Merging {len(input_files)} sources",
            file=primary_file,
            index=1,
            total=1,
        )
        try:
            def emit_merge_progress(message: str, step: int, steps: int) -> None:
                emit_batch_event(
                    event_callback,
                    "file-progress",
                    f"Merge: {message}",
                    file=primary_file,
                    index=1,
                    total=1,
                    step=step,
                    steps=steps,
                )

            report = process_merged_inputs(
                input_files,
                output_path,
                args,
                context.forced_subtitle_ids,
                batch_variant_consensus,
                progress_callback=emit_merge_progress if event_callback else None,
                cancel_callback=cancel_callback,
            )
            reports.append(report)
            emit_batch_event(
                event_callback,
                "file-finished",
                f"Finished merge: {report['status']}",
                file=primary_file,
                index=1,
                total=1,
            )
        except OrganizerCancelled:
            cancelled = True
            print("\nCancelled while merging sources.")
            reports.append(
                file_report_data(
                    primary_file,
                    output_path,
                    "cancelled",
                    message="operation cancelled",
                    input_paths=input_files,
                )
            )
            emit_batch_event(
                event_callback,
                "file-cancelled",
                "Cancelled merge",
                file=primary_file,
                index=1,
                total=1,
            )
        except OrganizerError as error:
            failures += 1
            print("\nError while merging sources:")
            print(error)
            reports.append(
                file_report_data(
                    primary_file,
                    output_path,
                    "error",
                    message=str(error),
                    input_paths=input_files,
                )
            )
            emit_batch_event(
                event_callback,
                "file-error",
                f"Merge: {error}",
                file=primary_file,
                index=1,
                total=1,
            )

        write_batch_report(reports, args, source_root, input_files, failures)
        result = BatchRunResult(
            reports=reports,
            failures=failures,
            input_files=input_files,
            source_root=source_root,
            cancelled=cancelled,
        )
        if cancelled:
            print("\nMerge cancelled.")
            emit_batch_event(event_callback, "batch-cancelled", "Merge cancelled.")
        elif failures:
            print(f"\nMerge completed with {failures} error(s).")
            emit_batch_event(event_callback, "batch-finished", f"Merge completed with {failures} error(s).")
        else:
            print("\nMerge completed without errors.")
            emit_batch_event(event_callback, "batch-finished", "Merge completed without errors.")
        return result

    for index, input_path in enumerate(input_files, start=1):
        try:
            ensure_not_cancelled(cancel_callback)
        except OrganizerCancelled:
            cancelled = True
            break
        output_path = output_path_for(
            input_path,
            source_root,
            output_dir=args.output_dir,
            output_suffix=args.output_suffix,
        )
        emit_batch_event(
            event_callback,
            "file-started",
            f"Processing {input_path.name}",
            file=input_path,
            index=index,
            total=total_files,
        )
        try:
            def emit_file_progress(message: str, step: int, steps: int) -> None:
                emit_batch_event(
                    event_callback,
                    "file-progress",
                    f"{input_path.name}: {message}",
                    file=input_path,
                    index=index,
                    total=total_files,
                    step=step,
                    steps=steps,
                )

            report = process_file(
                input_path,
                output_path,
                args,
                context.forced_subtitle_ids,
                batch_variant_consensus,
                progress_callback=emit_file_progress if event_callback else None,
                cancel_callback=cancel_callback,
            )
            reports.append(report)
            emit_batch_event(
                event_callback,
                "file-finished",
                f"Finished {input_path.name}: {report['status']}",
                file=input_path,
                index=index,
                total=total_files,
            )
        except OrganizerCancelled:
            cancelled = True
            print(f"\nCancelled while processing {input_path.name}.")
            reports.append(
                file_report_data(
                    input_path,
                    output_path,
                    "cancelled",
                    message="operation cancelled",
                )
            )
            emit_batch_event(
                event_callback,
                "file-cancelled",
                f"Cancelled {input_path.name}",
                file=input_path,
                index=index,
                total=total_files,
            )
            break
        except OrganizerError as error:
            failures += 1
            print(f"\nError in {input_path.name}:")
            print(error)
            reports.append(
                file_report_data(
                    input_path,
                    output_path,
                    "error",
                    message=str(error),
                )
            )
            emit_batch_event(
                event_callback,
                "file-error",
                f"{input_path.name}: {error}",
                file=input_path,
                index=index,
                total=total_files,
            )

    write_batch_report(reports, args, source_root, input_files, failures)
    result = BatchRunResult(
        reports=reports,
        failures=failures,
        input_files=input_files,
        source_root=source_root,
        cancelled=cancelled,
    )
    if cancelled:
        print("\nBatch cancelled.")
        emit_batch_event(event_callback, "batch-cancelled", "Batch cancelled.")
    elif failures:
        print(f"\nBatch completed with {failures} error(s).")
        emit_batch_event(event_callback, "batch-finished", f"Batch completed with {failures} error(s).")
    else:
        print("\nBatch completed without errors.")
        emit_batch_event(event_callback, "batch-finished", "Batch completed without errors.")

    return result


def run_from_args(
    args: argparse.Namespace,
    config_path: Path | None = None,
    event_callback: Callable[[BatchRunEvent], None] | None = None,
    cancel_callback: Callable[[], bool] | None = None,
) -> BatchRunResult:
    return run_batch(prepare_batch_run(args, config_path), event_callback, cancel_callback)


def main(argv: list[str] | None = None) -> int:
    if argv is None:
        argv = sys.argv[1:]

    try:
        config_defaults, config_path = config_defaults_from_argv(argv)
    except OrganizerError as error:
        print(f"Error: {error}")
        return 1

    parser = build_parser(config_defaults)
    args = parser.parse_args(argv)

    try:
        return run_from_args(args, config_path).return_code
    except OrganizerError as error:
        print(f"Error: {error}")
        return 1
    except KeyboardInterrupt:
        print("\nInterrupted by user.")
        return 130


if __name__ == "__main__":
    sys.exit(main())
