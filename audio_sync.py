from __future__ import annotations

import json
import math
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import numpy as np


class AudioSyncError(RuntimeError):
    """Raised for expected audio sync workflow failures."""


class AudioSyncCancelled(AudioSyncError):
    """Raised when the caller cancels an audio sync operation."""


class AudioSyncNoAudio(AudioSyncError):
    """Raised when FFmpeg returns no decoded samples for a checkpoint."""


@dataclass(frozen=True)
class MediaStream:
    index: int
    relative_index: int
    type: str
    codec: str
    language: str = ""
    title: str = ""
    channels: int | None = None

    @property
    def label(self) -> str:
        parts = [f"{self.type} {self.relative_index}", f"stream {self.index}", self.codec]
        if self.language:
            parts.append(self.language)
        if self.channels:
            parts.append(f"{self.channels}ch")
        if self.title:
            parts.append(self.title)
        return " | ".join(parts)


@dataclass(frozen=True)
class OffsetEstimate:
    checkpoint_seconds: float
    offset_seconds: float
    coarse_seconds: float
    confidence: float


@dataclass(frozen=True)
class AudioSyncSettings:
    reference_path: Path
    source_path: Path
    reference_audio_stream: int = 0
    source_audio_stream: int = 0
    start_seconds: float = 600.0
    duration_seconds: float = 120.0
    checkpoints: int = 4
    checkpoint_spacing_seconds: float = 900.0
    max_offset_seconds: float = 5.0
    sample_rate: int = 16_000
    envelope_hop_seconds: float = 0.010
    refine_seconds: float = 0.050
    ffmpeg_path: Path | None = None


@dataclass(frozen=True)
class AudioSyncResult:
    estimates: list[OffsetEstimate]
    median_offset_seconds: float
    spread_seconds: float
    average_confidence: float
    consistency: str
    verdict: str
    used_checkpoints: int = 0
    ignored_checkpoints: int = 0
    all_spread_seconds: float = 0.0
    confidence_summary: str = ""
    delay_reliability: str = ""
    reliability_reason: str = ""
    attempted_checkpoints: int = 0
    notes: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()

    @property
    def timeline_shift_seconds(self) -> float:
        return -self.median_offset_seconds

    @property
    def unavailable_checkpoints(self) -> int:
        return max(0, self.attempted_checkpoints - len(self.estimates))


@dataclass(frozen=True)
class ExportPlan:
    stream: MediaStream | None
    output_path: Path
    command: list[str]
    streams: tuple[MediaStream, ...] = ()


def parse_time(value: str | float | int) -> float:
    if isinstance(value, (int, float)):
        return float(value)

    text = str(value).strip()
    if not text:
        raise AudioSyncError("time cannot be empty")

    parts = text.split(":")
    try:
        if len(parts) == 1:
            return float(parts[0])
        if len(parts) == 2:
            minutes, seconds = parts
            return int(minutes) * 60 + float(seconds)
        if len(parts) == 3:
            hours, minutes, seconds = parts
            return int(hours) * 3600 + int(minutes) * 60 + float(seconds)
    except ValueError as error:
        raise AudioSyncError(f"invalid time: {value!r}") from error

    raise AudioSyncError(f"invalid time: {value!r}")


def format_time(seconds: float) -> str:
    sign = "-" if seconds < 0 else ""
    seconds = abs(seconds)
    whole = int(seconds)
    millis = int(round((seconds - whole) * 1000))
    if millis == 1000:
        whole += 1
        millis = 0

    hours, rem = divmod(whole, 3600)
    minutes, secs = divmod(rem, 60)
    return f"{sign}{hours:02d}:{minutes:02d}:{secs:02d}.{millis:03d}"


def format_delay_ms(seconds: float) -> str:
    return f"{seconds * 1000:+.2f} ms"


def resolve_binary(name: str, explicit_path: Path | None = None) -> Path:
    if explicit_path:
        path = Path(explicit_path).expanduser()
        if path.is_file():
            return path.resolve()
        raise AudioSyncError(f"Could not find {name}: {path}")

    match = shutil.which(name)
    if match:
        return Path(match).resolve()
    raise AudioSyncError(f"Missing required executable: {name}")


def probe_media_streams(path: Path, ffprobe_path: Path | None = None) -> list[MediaStream]:
    media_path = Path(path).expanduser()
    if not media_path.is_file():
        raise AudioSyncError(f"Media file not found: {media_path}")

    ffprobe = resolve_binary("ffprobe", ffprobe_path)
    command = [
        str(ffprobe),
        "-v",
        "error",
        "-show_entries",
        "stream=index,codec_type,codec_name,channels:stream_tags=language,title",
        "-of",
        "json",
        str(media_path),
    ]
    result = subprocess.run(command, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if result.returncode != 0:
        details = result.stderr.strip() or result.stdout.strip()
        raise AudioSyncError(f"ffprobe failed for {media_path}:\n{details}")

    try:
        payload = json.loads(result.stdout or "{}")
    except json.JSONDecodeError as error:
        raise AudioSyncError(f"ffprobe returned invalid JSON for {media_path}: {error}") from error
    return parse_ffprobe_streams(payload)


def parse_ffprobe_streams(payload: dict) -> list[MediaStream]:
    streams: list[MediaStream] = []
    relative_counts: dict[str, int] = {"audio": 0, "subtitle": 0}

    for raw_stream in payload.get("streams", []):
        stream_type = str(raw_stream.get("codec_type") or "")
        if stream_type not in {"audio", "subtitle"}:
            continue

        tags = raw_stream.get("tags") or {}
        relative_index = relative_counts[stream_type]
        relative_counts[stream_type] += 1
        streams.append(
            MediaStream(
                index=int(raw_stream.get("index", 0)),
                relative_index=relative_index,
                type=stream_type,
                codec=str(raw_stream.get("codec_name") or ""),
                language=str(tags.get("language") or ""),
                title=str(tags.get("title") or ""),
                channels=int(raw_stream["channels"]) if raw_stream.get("channels") is not None else None,
            )
        )

    return streams


def estimate_offset(
    settings: AudioSyncSettings,
    event_callback: Callable[[str], None] | None = None,
    progress_callback: Callable[[int, int], None] | None = None,
    cancel_callback: Callable[[], bool] | None = None,
) -> AudioSyncResult:
    validate_settings(settings)
    checkpoints = [
        settings.start_seconds + index * settings.checkpoint_spacing_seconds
        for index in range(settings.checkpoints)
    ]
    estimates: list[OffsetEstimate] = []

    for index, checkpoint in enumerate(checkpoints, start=1):
        ensure_not_cancelled(cancel_callback)
        emit(event_callback, f"Checkpoint {index}/{len(checkpoints)} at {format_time(checkpoint)}")
        try:
            estimate = estimate_at_checkpoint(
                settings.reference_path,
                settings.source_path,
                ref_stream=settings.reference_audio_stream,
                src_stream=settings.source_audio_stream,
                checkpoint_seconds=checkpoint,
                sample_rate=settings.sample_rate,
                duration_seconds=settings.duration_seconds,
                max_offset_seconds=settings.max_offset_seconds,
                envelope_hop_seconds=settings.envelope_hop_seconds,
                refine_seconds=settings.refine_seconds,
                ffmpeg_path=settings.ffmpeg_path,
                cancel_callback=cancel_callback,
            )
        except AudioSyncNoAudio as error:
            emit(event_callback, f"  skipped={error}")
            if progress_callback:
                progress_callback(index, len(checkpoints))
            continue
        estimates.append(estimate)
        emit(
            event_callback,
            (
                f"  offset={format_delay_ms(estimate.offset_seconds)} "
                f"(coarse={estimate.coarse_seconds * 1000:+.0f} ms, "
                f"match strength={confidence_label(estimate.confidence)})"
            ),
        )
        if progress_callback:
            progress_callback(index, len(checkpoints))

    if not estimates:
        raise AudioSyncError(
            "No usable checkpoints decoded audio. Move Start earlier, reduce Checkpoints, or reduce Spacing."
        )

    selected_estimates, ignored_checkpoints = select_estimates_for_result(estimates)
    offsets = np.array([estimate.offset_seconds for estimate in selected_estimates], dtype=np.float64)
    all_offsets = np.array([estimate.offset_seconds for estimate in estimates], dtype=np.float64)
    median = float(np.median(offsets))
    spread = float(np.max(np.abs(offsets - median))) if offsets.size else 0.0
    all_spread = float(np.max(np.abs(all_offsets - float(np.median(all_offsets))))) if all_offsets.size else 0.0
    average_confidence = float(np.mean([estimate.confidence for estimate in selected_estimates]))
    consistency = consistency_label(spread, len(selected_estimates))
    confidence_summary = confidence_label(average_confidence)
    reliability = delay_reliability_label(
        spread,
        average_confidence,
        len(selected_estimates),
        ignored_checkpoints,
        len(checkpoints),
    )
    reliability_reason = delay_reliability_reason(
        reliability,
        spread,
        len(selected_estimates),
        ignored_checkpoints,
    )
    notes = result_notes(
        reliability=reliability,
        average_confidence=average_confidence,
    )
    warnings = result_warnings(
        selected_estimates=selected_estimates,
        ignored_checkpoints=ignored_checkpoints,
        spread_seconds=spread,
        all_spread_seconds=all_spread,
        average_confidence=average_confidence,
        attempted_checkpoints=len(checkpoints),
    )
    verdict = verdict_label(
        spread,
        average_confidence,
        len(selected_estimates),
        ignored_checkpoints,
        len(checkpoints),
    )
    return AudioSyncResult(
        estimates=estimates,
        median_offset_seconds=median,
        spread_seconds=spread,
        average_confidence=average_confidence,
        consistency=consistency,
        verdict=verdict,
        used_checkpoints=len(selected_estimates),
        ignored_checkpoints=ignored_checkpoints,
        all_spread_seconds=all_spread,
        confidence_summary=confidence_summary,
        delay_reliability=reliability,
        reliability_reason=reliability_reason,
        attempted_checkpoints=len(checkpoints),
        notes=notes,
        warnings=warnings,
    )


def validate_settings(settings: AudioSyncSettings) -> None:
    if not Path(settings.reference_path).is_file():
        raise AudioSyncError(f"Reference file not found: {settings.reference_path}")
    if not Path(settings.source_path).is_file():
        raise AudioSyncError(f"Source file not found: {settings.source_path}")
    if settings.checkpoints < 1:
        raise AudioSyncError("checkpoints must be at least 1")
    if settings.duration_seconds <= 0:
        raise AudioSyncError("duration must be positive")
    if settings.max_offset_seconds <= 0:
        raise AudioSyncError("max offset must be positive")
    if settings.sample_rate <= 0:
        raise AudioSyncError("sample rate must be positive")
    if settings.envelope_hop_seconds <= 0:
        raise AudioSyncError("envelope hop must be positive")
    if settings.refine_seconds <= 0:
        raise AudioSyncError("refine radius must be positive")
    resolve_binary("ffmpeg", settings.ffmpeg_path)


def estimate_at_checkpoint(
    ref_path: Path,
    src_path: Path,
    *,
    ref_stream: int,
    src_stream: int,
    checkpoint_seconds: float,
    sample_rate: int,
    duration_seconds: float,
    max_offset_seconds: float,
    envelope_hop_seconds: float,
    refine_seconds: float,
    ffmpeg_path: Path | None = None,
    cancel_callback: Callable[[], bool] | None = None,
) -> OffsetEstimate:
    pad = max_offset_seconds + 1.0
    start = max(0.0, checkpoint_seconds - pad)
    duration = duration_seconds + 2.0 * pad

    ref_audio = decode_audio(
        ref_path,
        stream_index=ref_stream,
        start_seconds=start,
        duration_seconds=duration,
        sample_rate=sample_rate,
        ffmpeg_path=ffmpeg_path,
        cancel_callback=cancel_callback,
    )
    src_audio = decode_audio(
        src_path,
        stream_index=src_stream,
        start_seconds=start,
        duration_seconds=duration,
        sample_rate=sample_rate,
        ffmpeg_path=ffmpeg_path,
        cancel_callback=cancel_callback,
    )

    frame_size = max(256, int(round(0.050 * sample_rate)))
    hop_size = max(1, int(round(envelope_hop_seconds * sample_rate)))
    ref_env = rms_envelope(ref_audio, frame_size, hop_size)
    src_env = rms_envelope(src_audio, frame_size, hop_size)

    max_lag_frames = int(math.ceil(max_offset_seconds / envelope_hop_seconds))
    best_lag_frames, confidence = correlate_limited(ref_env, src_env, max_lag=max_lag_frames)
    coarse_seconds = best_lag_frames * envelope_hop_seconds

    refined_seconds = refine_offset_seconds(
        ref_audio,
        src_audio,
        sample_rate=sample_rate,
        coarse_seconds=coarse_seconds,
        search_radius_seconds=refine_seconds,
    )

    return OffsetEstimate(checkpoint_seconds, refined_seconds, coarse_seconds, confidence)


def decode_audio(
    path: Path,
    *,
    stream_index: int,
    start_seconds: float,
    duration_seconds: float,
    sample_rate: int,
    ffmpeg_path: Path | None = None,
    cancel_callback: Callable[[], bool] | None = None,
) -> np.ndarray:
    if duration_seconds <= 0:
        raise ValueError("duration_seconds must be positive")

    ffmpeg = resolve_binary("ffmpeg", ffmpeg_path)
    start_seconds = max(0.0, start_seconds)
    command = [
        str(ffmpeg),
        "-hide_banner",
        "-loglevel",
        "error",
        "-ss",
        f"{start_seconds:.6f}",
        "-i",
        str(path),
        "-map",
        f"0:a:{stream_index}",
        "-t",
        f"{duration_seconds:.6f}",
        "-vn",
        "-sn",
        "-dn",
        "-ac",
        "1",
        "-ar",
        str(sample_rate),
        "-f",
        "f32le",
        "-",
    ]
    result = run_capture(command, cancel_callback)
    audio = np.frombuffer(result.stdout, dtype=np.float32).copy()
    if audio.size == 0:
        raise AudioSyncNoAudio(f"no audio decoded from {path} at {format_time(start_seconds)}")
    return audio


def run_capture(command: list[str], cancel_callback: Callable[[], bool] | None = None) -> subprocess.CompletedProcess:
    process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    while True:
        ensure_not_cancelled(cancel_callback, process)
        try:
            stdout, stderr = process.communicate(timeout=0.1)
            break
        except subprocess.TimeoutExpired:
            continue

    result = subprocess.CompletedProcess(command, process.returncode, stdout, stderr)
    if result.returncode != 0:
        details = stderr.decode("utf-8", errors="replace").strip()
        raise AudioSyncError(f"Command failed:\n{format_command(command)}\n{details}")
    return result


def rms_envelope(audio: np.ndarray, frame_size: int, hop_size: int) -> np.ndarray:
    if audio.size < frame_size:
        raise ValueError("audio segment is too short for analysis")

    audio = np.asarray(audio, dtype=np.float32)
    frame_count = 1 + (audio.size - frame_size) // hop_size
    trimmed = audio[: frame_size + (frame_count - 1) * hop_size]
    frames = np.lib.stride_tricks.sliding_window_view(trimmed, frame_size)[::hop_size]
    env = np.sqrt(np.mean(frames * frames, axis=1, dtype=np.float64))
    env = np.log1p(env * 100.0)
    return whiten(env)


def whiten(values: np.ndarray) -> np.ndarray:
    values = np.asarray(values, dtype=np.float64)
    values = values - np.mean(values)
    std = np.std(values)
    if std < 1e-12:
        return values
    return values / std


def highpass_for_correlation(audio: np.ndarray) -> np.ndarray:
    audio = np.asarray(audio, dtype=np.float64)
    audio = audio - np.mean(audio)
    if audio.size > 1:
        audio = np.diff(audio, prepend=audio[0])
    return whiten(audio)


def correlate_limited(left: np.ndarray, right: np.ndarray, *, max_lag: int) -> tuple[int, float]:
    left = np.asarray(left, dtype=np.float64)
    right = np.asarray(right, dtype=np.float64)
    if left.size == 0 or right.size == 0:
        raise ValueError("cannot correlate empty arrays")

    lag_min = max(-max_lag, -(left.size - 1))
    lag_max = min(max_lag, right.size - 1)
    if lag_min > lag_max:
        raise ValueError("max_lag excludes all correlation values")

    lags = np.arange(lag_min, lag_max + 1)
    scores = np.empty(lags.size, dtype=np.float64)

    for index, lag in enumerate(lags):
        if lag >= 0:
            samples = min(left.size, right.size - lag)
            a = left[:samples]
            b = right[lag : lag + samples]
        else:
            samples = min(left.size + lag, right.size)
            a = left[-lag : -lag + samples]
            b = right[:samples]
        scores[index] = float(np.dot(a, b) / max(1, samples))

    best_index = int(np.argmax(scores))
    best_lag = int(lags[best_index])
    median = float(np.median(scores))
    mad = float(np.median(np.abs(scores - median))) + 1e-12
    confidence = max(0.0, (float(scores[best_index]) - median) / (mad * 12.0))
    return best_lag, confidence


def parabolic_subsample_peak(values: np.ndarray, index: int) -> float:
    if index <= 0 or index >= len(values) - 1:
        return 0.0
    y0 = float(values[index - 1])
    y1 = float(values[index])
    y2 = float(values[index + 1])
    denom = y0 - 2.0 * y1 + y2
    if abs(denom) < 1e-12:
        return 0.0
    return 0.5 * (y0 - y2) / denom


def refine_offset_seconds(
    ref_audio: np.ndarray,
    src_audio: np.ndarray,
    *,
    sample_rate: int,
    coarse_seconds: float,
    search_radius_seconds: float,
) -> float:
    ref = highpass_for_correlation(ref_audio)
    src = highpass_for_correlation(src_audio)

    center = int(round(coarse_seconds * sample_rate))
    radius = max(1, int(round(search_radius_seconds * sample_rate)))
    lags = np.arange(center - radius, center + radius + 1)
    scores = np.empty(lags.size, dtype=np.float64)

    for index, lag in enumerate(lags):
        if lag >= 0:
            samples = min(ref.size, src.size - lag)
            if samples <= 0:
                scores[index] = -np.inf
                continue
            a = ref[:samples]
            b = src[lag : lag + samples]
        else:
            samples = min(ref.size + lag, src.size)
            if samples <= 0:
                scores[index] = -np.inf
                continue
            a = ref[-lag : -lag + samples]
            b = src[:samples]
        scores[index] = float(np.dot(a, b) / max(1, samples))

    best_index = int(np.argmax(scores))
    sub = parabolic_subsample_peak(scores, best_index)
    return (float(lags[best_index]) + sub) / sample_rate


def estimate_spread_seconds(estimates: list[OffsetEstimate]) -> float:
    if not estimates:
        return 0.0
    offsets = np.array([estimate.offset_seconds for estimate in estimates], dtype=np.float64)
    median = float(np.median(offsets))
    return float(np.max(np.abs(offsets - median)))


def select_estimates_for_result(
    estimates: list[OffsetEstimate],
    cluster_tolerance_seconds: float = 0.050,
) -> tuple[list[OffsetEstimate], int]:
    if len(estimates) < 3:
        return estimates, 0

    best_cluster = estimates
    best_key: tuple[int, float, float] | None = None
    for center in estimates:
        cluster = [
            estimate
            for estimate in estimates
            if abs(estimate.offset_seconds - center.offset_seconds) <= cluster_tolerance_seconds
        ]
        spread = estimate_spread_seconds(cluster)
        confidence = float(np.mean([estimate.confidence for estimate in cluster])) if cluster else 0.0
        key = (len(cluster), confidence, -spread)
        if best_key is None or key > best_key:
            best_key = key
            best_cluster = cluster

    min_cluster_size = max(2, math.ceil(len(estimates) / 2))
    if len(best_cluster) < len(estimates) and len(best_cluster) >= min_cluster_size:
        best_ids = {id(estimate) for estimate in best_cluster}
        selected = [estimate for estimate in estimates if id(estimate) in best_ids]
        return selected, len(estimates) - len(selected)

    return estimates, 0


def confidence_label(value: float) -> str:
    if value >= 8.0:
        return "high"
    if value >= 4.0:
        return "medium"
    if value >= 2.0:
        return "low"
    return "very low"


def consistency_label(spread_seconds: float, checkpoints: int) -> str:
    if checkpoints < 2:
        return "single checkpoint"
    if spread_seconds <= 0.005:
        return "excellent"
    if spread_seconds <= 0.020:
        return "good"
    if spread_seconds <= 0.050:
        return "fair"
    return "poor"


def delay_reliability_label(
    spread_seconds: float,
    average_confidence: float,
    checkpoints: int,
    ignored_checkpoints: int = 0,
    attempted_checkpoints: int = 0,
) -> str:
    if checkpoints < 2:
        return "low"

    total = attempted_checkpoints or (checkpoints + ignored_checkpoints)
    coverage = checkpoints / max(1, total)
    if checkpoints >= 4 and coverage >= 0.5 and spread_seconds <= 0.005 and average_confidence >= 1.0:
        return "high"
    if checkpoints >= 3 and coverage >= 0.4 and spread_seconds <= 0.020 and average_confidence >= 0.75:
        return "medium"
    if checkpoints >= 2 and spread_seconds <= 0.020 and average_confidence >= 4.0:
        return "medium"
    return "low"


def delay_reliability_reason(
    reliability: str,
    spread_seconds: float,
    checkpoints: int,
    ignored_checkpoints: int = 0,
) -> str:
    spread_ms = spread_seconds * 1000
    if reliability == "high":
        reason = f"{checkpoints} independent checkpoints agree within +/-{spread_ms:.2f} ms"
    elif reliability == "medium":
        reason = f"{checkpoints} checkpoints form a plausible cluster within +/-{spread_ms:.2f} ms"
    elif checkpoints < 2:
        reason = "only one usable checkpoint contributed to the delay"
    else:
        reason = f"checkpoint evidence is too weak or dispersed (+/-{spread_ms:.2f} ms)"
    if ignored_checkpoints:
        reason += f" after ignoring {ignored_checkpoints} outlier(s)"
    return reason


def result_notes(
    *,
    reliability: str,
    average_confidence: float,
) -> tuple[str, ...]:
    notes: list[str] = []
    if average_confidence < 2.0 and reliability in {"high", "medium"}:
        notes.append(
            "individual correlation peaks are weak, but repeated checkpoints independently agree on the delay"
        )
    elif average_confidence < 4.0 and reliability == "high":
        notes.append("individual correlation peaks are modest, but checkpoint consensus is strong")
    return tuple(notes)


def result_warnings(
    *,
    selected_estimates: list[OffsetEstimate],
    ignored_checkpoints: int,
    spread_seconds: float,
    all_spread_seconds: float,
    average_confidence: float,
    attempted_checkpoints: int = 0,
) -> tuple[str, ...]:
    warnings: list[str] = []
    reliability = delay_reliability_label(
        spread_seconds,
        average_confidence,
        len(selected_estimates),
        ignored_checkpoints,
        attempted_checkpoints,
    )
    if reliability == "low":
        warnings.append("delay reliability is low; verify manually before applying the correction")
    if ignored_checkpoints:
        warnings.append(f"{ignored_checkpoints} outlier checkpoint(s) were ignored outside the main delay cluster")
    if len(selected_estimates) < 2:
        warnings.append("only one usable checkpoint contributed to the result")
    if spread_seconds > 0.020:
        warnings.append("selected checkpoints disagree by more than 20 ms")
    if all_spread_seconds > 0.050 and not ignored_checkpoints:
        warnings.append("checkpoints do not form a stable delay cluster")
    return tuple(warnings)


def verdict_label(
    spread_seconds: float,
    average_confidence: float,
    checkpoints: int,
    ignored_checkpoints: int = 0,
    attempted_checkpoints: int = 0,
) -> str:
    if checkpoints < 2:
        return "single checkpoint; verify manually"
    reliability = delay_reliability_label(
        spread_seconds,
        average_confidence,
        checkpoints,
        ignored_checkpoints,
        attempted_checkpoints,
    )
    if reliability == "high":
        return "reliable fixed delay: strong checkpoint consensus"
    if reliability == "medium":
        if ignored_checkpoints:
            return "likely fixed delay after rejecting outliers; spot-check recommended"
        return "likely fixed delay; spot-check recommended"
    if spread_seconds > 0.020:
        return "uncertain: checkpoints do not agree on one fixed delay"
    return "uncertain: insufficient evidence for a reliable delay"


def build_export_plan(
    source_path: Path,
    stream: MediaStream,
    timeline_shift_seconds: float,
    output_dir: Path,
    ffmpeg_path: Path | None = None,
) -> ExportPlan:
    ffmpeg = resolve_binary("ffmpeg", ffmpeg_path)
    output_dir = Path(output_dir).expanduser()
    delay_ms = int(round(timeline_shift_seconds * 1000))
    suffix = f"delay{delay_ms:+d}ms"
    safe_language = stream.language or "und"

    if stream.type == "audio":
        output_path = output_dir / f"{Path(source_path).stem}.a{stream.relative_index}.{safe_language}.{suffix}.mka"
        map_value = f"0:a:{stream.relative_index}"
        codec_args = ["-c", "copy"]
    elif stream.type == "subtitle":
        output_path = output_dir / f"{Path(source_path).stem}.s{stream.relative_index}.{safe_language}.{suffix}.mks"
        map_value = f"0:{stream.index}"
        codec_args = ["-c", "copy"]
    else:
        raise AudioSyncError(f"Cannot export unsupported stream type: {stream.type}")

    command = [
        str(ffmpeg),
        "-y",
        "-hide_banner",
        "-loglevel",
        "error",
        "-itsoffset",
        f"{timeline_shift_seconds:.6f}",
        "-i",
        str(source_path),
        "-map",
        map_value,
        *codec_args,
        str(output_path),
    ]
    return ExportPlan(stream, output_path, command)


def build_combined_audio_export_plan(
    source_path: Path,
    streams: list[MediaStream],
    timeline_shift_seconds: float,
    output_dir: Path,
    ffmpeg_path: Path | None = None,
) -> ExportPlan:
    ffmpeg = resolve_binary("ffmpeg", ffmpeg_path)
    audio_streams = [stream for stream in streams if stream.type == "audio"]
    if len(audio_streams) != len(streams):
        raise AudioSyncError("Combined .mka export only supports audio streams.")
    if not audio_streams:
        raise AudioSyncError("Select at least one audio stream to export.")

    output_dir = Path(output_dir).expanduser()
    delay_ms = int(round(timeline_shift_seconds * 1000))
    suffix = f"delay{delay_ms:+d}ms"
    output_path = output_dir / f"{Path(source_path).stem}.synced.{suffix}.mka"

    command = [
        str(ffmpeg),
        "-y",
        "-hide_banner",
        "-loglevel",
        "error",
        "-itsoffset",
        f"{timeline_shift_seconds:.6f}",
        "-i",
        str(source_path),
    ]
    for stream in audio_streams:
        command.extend(["-map", f"0:a:{stream.relative_index}"])
    command.extend(["-c", "copy", str(output_path)])
    return ExportPlan(None, output_path, command, tuple(audio_streams))


def export_combined_audio_streams(
    source_path: Path,
    streams: list[MediaStream],
    timeline_shift_seconds: float,
    output_dir: Path,
    ffmpeg_path: Path | None = None,
    cancel_callback: Callable[[], bool] | None = None,
) -> ExportPlan:
    plan = build_combined_audio_export_plan(source_path, streams, timeline_shift_seconds, output_dir, ffmpeg_path)
    plan.output_path.parent.mkdir(parents=True, exist_ok=True)
    run_capture(plan.command, cancel_callback)
    return plan


def export_shifted_stream(
    source_path: Path,
    stream: MediaStream,
    timeline_shift_seconds: float,
    output_dir: Path,
    ffmpeg_path: Path | None = None,
    cancel_callback: Callable[[], bool] | None = None,
) -> ExportPlan:
    plan = build_export_plan(source_path, stream, timeline_shift_seconds, output_dir, ffmpeg_path)
    plan.output_path.parent.mkdir(parents=True, exist_ok=True)
    run_capture(plan.command, cancel_callback)
    return plan


def ensure_not_cancelled(cancel_callback: Callable[[], bool] | None, process: subprocess.Popen | None = None) -> None:
    if cancel_callback and cancel_callback():
        if process and process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
        raise AudioSyncCancelled("Operation cancelled.")


def emit(callback: Callable[[str], None] | None, message: str) -> None:
    if callback:
        callback(message)


def format_command(command: list[str]) -> str:
    return subprocess.list2cmdline([str(part) for part in command])
