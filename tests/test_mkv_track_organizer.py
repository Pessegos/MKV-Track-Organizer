import argparse
from pathlib import Path

import mkv_track_organizer as m


def subtitle_track(track_id: int, language: str = "eng", size_class: str = "large") -> m.TrackInfo:
    track = m.TrackInfo(
        id=track_id,
        type="subtitles",
        codec="SubRip/SRT",
        codec_id="S_TEXT/UTF8",
        language=language,
        output_language=language,
        language_name=m.language_display_name(language),
        original_name="",
        order=track_id,
        properties={},
    )
    track.analysis = m.SubtitleAnalysis(size_bytes=0, size_class=size_class)
    return track


def pgs_subtitle_track(
    track_id: int,
    language: str = "por",
    size_bytes: int = 4_000_000,
    size_class: str = "large",
    display_events: int = 900,
) -> m.TrackInfo:
    track = m.TrackInfo(
        id=track_id,
        type="subtitles",
        codec="HDMV PGS",
        codec_id="S_HDMV/PGS",
        language=language,
        output_language=language,
        language_name=m.language_display_name(language),
        original_name="",
        order=track_id,
        properties={},
    )
    track.analysis = m.SubtitleAnalysis(
        size_bytes=size_bytes,
        size_class=size_class,
        pgs=m.PgsStats(display_events=display_events),
    )
    return track


def audio_track(
    track_id: int,
    language: str = "eng",
    codec: str = "DTS-HD Master Audio",
    codec_id: str = "A_DTS",
    channels: int | None = 6,
    original_name: str = "",
) -> m.TrackInfo:
    return m.TrackInfo(
        id=track_id,
        type="audio",
        codec=codec,
        codec_id=codec_id,
        language=language,
        output_language=language,
        language_name=m.language_display_name(language),
        original_name=original_name,
        order=track_id,
        properties={},
        channels=channels,
    )


def video_track(track_id: int = 0) -> m.TrackInfo:
    return m.TrackInfo(
        id=track_id,
        type="video",
        codec="HEVC",
        codec_id="V_MPEGH/ISO/HEVC",
        language="und",
        output_language="und",
        language_name=m.language_display_name("und"),
        original_name="",
        order=track_id,
        properties={},
    )


def test_language_names_and_aliases() -> None:
    assert m.normalize_language_code("pt-PT") == "pt-PT"
    assert m.language_display_name("pt-PT") == "Portuguese (Iberian)"
    assert m.language_display_name("es-ES") == "Spanish (Castilian)"
    assert m.normalize_language_code("zh-HK") == "zh-HK"
    assert m.language_display_name("zh-HK") == "Chinese (Hong Kong)"
    assert m.normalize_language_code("zh-TW") == "zh-TW"
    assert m.language_display_name("zh-TW") == "Chinese (Taiwan)"
    assert m.language_display_name("cmn") == "Mandarin Chinese"
    assert m.language_display_name("yue") == "Cantonese"
    assert m.normalize_language_code("or") == "ori"
    assert m.language_display_name("ori") == "Odia"
    assert m.normalize_language_code("is") == "ice"
    assert m.language_display_name("ice") == "Icelandic"
    assert m.normalize_language_code("mk") == "mk"
    assert m.language_display_name("mk") == "Macedonian"
    assert m.normalize_language_code("sk") == "sk"
    assert m.language_display_name("sk") == "Slovak"
    assert m.normalize_language_code("nb") == "nob"
    assert m.normalize_language_code("nob") == "nob"
    assert m.language_display_name("nb") == "Norwegian Bokmål"
    assert m.language_display_name("nob") == "Norwegian Bokmål"
    assert m.normalize_language_code("nn") == "nno"
    assert m.language_display_name("nno") == "Norwegian Nynorsk"
    assert m.language_for_mkvmerge("nob") == "nb"
    assert m.legacy_language_for_mkvpropedit("nob") == "nob"
    assert m.ietf_language_for_mkvpropedit("nob") == "nb"
    assert m.normalize_language_code("msa") == "may"
    assert m.normalize_language_code("ms") == "may"
    assert m.language_display_name("msa") == "Malay"
    assert m.normalize_language_from_properties("und", "Malay") == "may"
    assert m.ietf_language_for_mkvpropedit("may") == "ms"


def test_language_hints_fix_wrong_metadata_language() -> None:
    assert m.normalize_language_from_properties("or", "Français (Canadien)") == "fr-CA"
    assert m.normalize_language_from_properties("und", "Taiwan") == "zh-TW"
    assert m.normalize_language_from_properties("zh", "Chinese (Hong Kong)") == "zh-HK"
    assert m.normalize_language_from_properties("zh", "Chinese (Simplified)") == "zh-Hans"
    assert m.normalize_language_from_properties("yue", "Cantonese") == "yue"
    assert m.normalize_language_from_properties("por", "European (Forced)") == "pt-PT"
    assert m.normalize_language_from_properties("spa", "European") == "es-ES"
    assert m.normalize_language_from_properties("fre", "European") == "fr-FR"


def test_audio_name_auto_keeps_format_only_for_single_language() -> None:
    english_main = audio_track(1, "eng", channels=6)
    english_commentary = audio_track(2, "eng", codec="AC-3", codec_id="A_AC3", channels=2)
    english_commentary.role = "commentary"

    m.apply_audio_names([english_main, english_commentary], "auto")

    assert english_main.suggested_name == "DTS-HD MA 5.1"
    assert english_commentary.suggested_name == "AC-3 2.0 Commentary"


def test_audio_name_auto_adds_language_for_multiple_languages() -> None:
    english = audio_track(1, "eng", channels=6)
    japanese = audio_track(2, "jpn", codec="FLAC", codec_id="A_FLAC", channels=2)

    m.apply_audio_names([english, japanese], "auto")

    assert english.suggested_name == "English - DTS-HD MA 5.1"
    assert japanese.suggested_name == "Japanese - FLAC 2.0"


def test_audio_name_style_keep_preserves_original_name() -> None:
    track = audio_track(1, "fre", original_name="VFQ 5.1")

    m.apply_audio_names([track], "keep")

    assert track.suggested_name == "VFQ 5.1"


def test_variant_classifiers() -> None:
    assert m.classify_portuguese_variant("Onde está o telemóvel? Despacha-te.")["code"] == "pt-PT"
    assert m.classify_portuguese_variant("Cadê meu celular? Estou falando com você.")["code"] == "pt-BR"
    assert m.classify_spanish_variant("Vale, vosotros podéis coger el coche.")["code"] == "es-ES"
    assert m.classify_spanish_variant("Ustedes pueden manejar el carro hasta el departamento.")["code"] == "es-419"
    assert m.classify_french_variant("Mon char est au stationnement près du dépanneur.")["code"] == "fr-CA"
    assert m.classify_chinese_variant("我們這個電話聲音還在門後。")["code"] == "zh-Hant"


def test_chinese_hong_kong_metadata_survives_traditional_ocr(tmp_path: Path) -> None:
    hong_kong = subtitle_track(21, "zh-HK")
    hong_kong.analysis.text_sample = "我們這個電話聲音還在門後。"

    m.detect_language_variants(tmp_path / "movie.mkv", [hong_kong], tmp_path / "cache")

    assert hong_kong.output_language == "zh-HK"
    assert hong_kong.language_name == "Chinese (Hong Kong)"


def test_chinese_taiwan_metadata_survives_traditional_ocr(tmp_path: Path) -> None:
    taiwan = subtitle_track(21, "zh-TW")
    taiwan.analysis.text_sample = "我們這個電話聲音還在門後。"

    m.detect_language_variants(tmp_path / "movie.mkv", [taiwan], tmp_path / "cache")

    assert taiwan.output_language == "zh-TW"
    assert taiwan.language_name == "Chinese (Taiwan)"


def test_mandarin_subtitle_can_be_classified_by_chinese_script(tmp_path: Path) -> None:
    mandarin = subtitle_track(21, "cmn")
    mandarin.analysis.text_sample = "我们这个电话声音还在门后。"

    m.detect_language_variants(tmp_path / "movie.mkv", [mandarin], tmp_path / "cache")

    assert mandarin.output_language == "zh-Hans"
    assert mandarin.language_name == "Chinese (Simplified)"


def test_mandarin_subtitle_without_text_stays_mandarin(tmp_path: Path) -> None:
    mandarin = subtitle_track(21, "cmn")

    m.detect_language_variants(tmp_path / "movie.mkv", [mandarin], tmp_path / "cache")

    assert mandarin.output_language == "cmn"
    assert mandarin.language_name == "Mandarin Chinese"


def test_mandarin_pgs_ocr_tries_both_chinese_script_models() -> None:
    mandarin = pgs_subtitle_track(21, "cmn")

    assert m.ocr_language_candidates_for_track(mandarin, "auto") == ["chi_sim", "chi_tra"]


def test_chinese_ocr_selection_prefers_script_evidence(tmp_path: Path) -> None:
    simplified = tmp_path / "simplified.srt"
    traditional = tmp_path / "traditional.srt"
    simplified.write_text("我们这个电话声音还在门后。", encoding="utf-8")
    traditional.write_text("hello", encoding="utf-8")

    selected = m.select_chinese_ocr_output([("chi_sim", simplified), ("chi_tra", traditional)])

    assert selected is not None
    assert selected[0] == "chi_sim"
    assert selected[1] == simplified


def test_portuguese_variant_uses_known_iberian_vocabulary() -> None:
    result = m.classify_portuguese_variant(
        "Liga para o telemóvel. "
        "Vai de autocarro até à estação do comboio. "
        "Deixei o bilhete de identidade na casa de banho."
    )

    assert result["code"] == "pt-PT"
    assert result["pt_pt_score"] >= m.VARIANT_METADATA_OVERRIDE_MIN_SCORE


def test_portuguese_variant_uses_known_brazilian_vocabulary() -> None:
    result = m.classify_portuguese_variant(
        "Liga no celular. "
        "Pegue o ônibus até a delegacia. "
        "Deixei a carteira de identidade na geladeira."
    )

    assert result["code"] == "pt-BR"
    assert result["pt_br_score"] >= m.VARIANT_METADATA_OVERRIDE_MIN_SCORE


def test_spanish_variant_ignores_common_false_friends() -> None:
    result = m.classify_spanish_variant(
        "Segundo piso. Vale la pena mirarlo. "
        "Que sabe lo que vale una pierna."
    )

    assert result["code"] == "spa"


def test_spanish_variant_uses_repeated_ustedes_for_generic_latin_american() -> None:
    result = m.classify_spanish_variant(
        "Pero ustedes no la habrían visto. "
        "Estamos bien, ¿cómo están ustedes?"
    )

    assert result["code"] == "es-419"


def test_spanish_variant_uses_latam_floor_and_enojar_markers() -> None:
    result = m.classify_spanish_variant(
        "Enojaremos a los estadounidenses. "
        "¡Al piso! "
        "Te enojaste porque no lo dije la última vez."
    )

    assert result["code"] == "es-419"
    assert result["es_419_score"] >= m.VARIANT_METADATA_OVERRIDE_MIN_SCORE


def test_spanish_variant_treats_computadora_as_strong_latam_anchor() -> None:
    result = m.classify_spanish_variant("Se llama computadora.")

    assert result["code"] == "es-419"
    assert result["es_419_score"] >= m.VARIANT_METADATA_OVERRIDE_MIN_SCORE


def test_spanish_variant_uses_broader_latin_american_terms() -> None:
    result = m.classify_spanish_variant(
        "Estoy enojado. "
        "Maneja hasta el estacionamiento y toma el elevador. "
        "Necesito mis lentes."
    )

    assert result["code"] == "es-419"


def test_spanish_variant_uses_known_latin_american_vocabulary() -> None:
    result = m.classify_spanish_variant(
        "Deja el celular junto a la computadora. "
        "Compra jugo, frijoles y boletos. "
        "Estaciona el carro frente al departamento."
    )

    assert result["code"] == "es-419"
    assert result["es_419_score"] >= m.VARIANT_METADATA_OVERRIDE_MIN_SCORE


def test_spanish_variant_uses_broader_castilian_terms() -> None:
    result = m.classify_spanish_variant(
        "Estoy enfadado. "
        "Conduce el coche hasta el aparcamiento y sube por el ascensor. "
        "He perdido las gafas."
    )

    assert result["code"] == "es-ES"


def test_spanish_variant_uses_known_castilian_vocabulary() -> None:
    result = m.classify_spanish_variant(
        "Coge el móvil y apaga el ordenador. "
        "Compra zumo y patatas fritas. "
        "Deja el coche en el aparcamiento."
    )

    assert result["code"] == "es-ES"
    assert result["es_es_score"] >= m.VARIANT_METADATA_OVERRIDE_MIN_SCORE


def test_spanish_variant_ignores_mojibake_os_false_castilian_marker() -> None:
    result = m.classify_spanish_variant(
        "de secuencias de sue├▒os, "
        "que nunca he hecho en todos estos a├▒os. "
        "y creamos algunos dise├▒os."
    )

    assert result["code"] == "spa"
    assert result["es_es_score"] == 0


def test_spanish_variant_uses_enojar_and_ustedes_as_strong_latam_pair() -> None:
    result = m.classify_spanish_variant(
        "Estamos bien, ¿cómo están ustedes? "
        "Te enojaste porque no lo dije la última vez."
    )

    assert result["code"] == "es-419"
    assert result["es_419_score"] >= m.VARIANT_METADATA_OVERRIDE_MIN_SCORE


def test_spanish_variant_prefers_latam_despite_mojibake_years_noise() -> None:
    result = m.classify_spanish_variant(
        "Durante los ├║ltimos ocho a├▒os, "
        "manejas un auto de hace 10 a├▒os. "
        "trabajaban ustedes juntos. "
        "creadas enteramente por computadora."
    )

    assert result["code"] == "es-419"


def test_weak_spanish_ocr_does_not_override_explicit_variant(tmp_path: Path) -> None:
    spanish = subtitle_track(1, "es-ES", "large")
    spanish.analysis = m.SubtitleAnalysis(
        size_bytes=60_000,
        size_class="large",
        text_sample="Donde ustedes no han mirado. Vale la pena averiguarlo.",
    )

    m.detect_language_variants(tmp_path / "movie.mkv", [spanish], tmp_path / "cache")

    assert spanish.output_language == "es-ES"


def test_spanish_ocr_with_no_variant_markers_downgrades_explicit_variant() -> None:
    spanish = subtitle_track(1, "es-ES", "large")
    result = m.classify_spanish_variant(
        "de secuencias de sue├▒os, "
        "que nunca he hecho en todos estos a├▒os."
    )

    m.apply_language_variant_result(spanish, result)

    assert spanish.output_language == "spa"


def test_strong_spanish_ocr_overrides_explicit_variant(tmp_path: Path) -> None:
    spanish = subtitle_track(1, "es-ES", "large")
    spanish.analysis = m.SubtitleAnalysis(
        size_bytes=60_000,
        size_class="large",
        text_sample="Ustedes pueden manejar el carro. Dejé que se suba a mi auto.",
    )

    m.detect_language_variants(tmp_path / "movie.mkv", [spanish], tmp_path / "cache")

    assert spanish.output_language == "es-419"


def test_batch_variant_consensus_uses_clear_majority() -> None:
    votes = {"spa": ["es-419", "es-ES", "es-419"]}

    assert m.batch_language_variant_consensus_from_votes(votes) == {"spa": "es-419"}


def test_batch_variant_consensus_overrides_weak_track_metadata() -> None:
    spanish = subtitle_track(1, "es-ES", "large")
    spanish.analysis = m.SubtitleAnalysis(
        size_bytes=60_000,
        size_class="large",
        text_sample="Donde ustedes no han mirado. Vale la pena averiguarlo.",
    )
    result = m.classify_spanish_variant(spanish.analysis.text_sample)

    m.apply_language_variant_result(spanish, result)
    changes = m.apply_batch_language_variant_consensus(
        [spanish],
        {spanish.id: result},
        {"spa": "es-419"},
    )

    assert spanish.output_language == "es-419"
    assert changes


def test_batch_variant_consensus_does_not_override_strong_track_evidence() -> None:
    spanish = subtitle_track(1, "es-ES", "large")
    spanish.analysis = m.SubtitleAnalysis(
        size_bytes=60_000,
        size_class="large",
        text_sample="Vosotros podéis coger el coche. Vale, os veo luego.",
    )
    result = m.classify_spanish_variant(spanish.analysis.text_sample)

    changes = m.apply_batch_language_variant_consensus(
        [spanish],
        {spanish.id: result},
        {"spa": "es-419"},
    )

    assert spanish.output_language == "es-ES"
    assert changes == []


def test_batch_variant_consensus_does_not_override_strong_latin_american_track_evidence() -> None:
    spanish = subtitle_track(1, "es-419", "large")
    spanish.analysis = m.SubtitleAnalysis(
        size_bytes=60_000,
        size_class="large",
        text_sample=(
            "Estamos bien, ¿cómo están ustedes? "
            "Te enojaste porque no lo dije la última vez."
        ),
    )
    result = m.classify_spanish_variant(spanish.analysis.text_sample)

    changes = m.apply_batch_language_variant_consensus(
        [spanish],
        {spanish.id: result},
        {"spa": "es-ES"},
    )

    assert spanish.output_language == "es-419"
    assert changes == []


def test_batch_variant_consensus_does_not_override_explicit_name_variant() -> None:
    forced = subtitle_track(1, "pt-PT", "small")
    forced.original_name = "European (Forced)"
    forced.analysis = m.SubtitleAnalysis(
        size_bytes=1_000,
        size_class="small",
        text_sample="Vai-te embora, cara.",
    )
    result = m.classify_portuguese_variant(forced.analysis.text_sample)

    changes = m.apply_batch_language_variant_consensus(
        [forced],
        {forced.id: result},
        {"por": "pt-BR"},
    )

    assert forced.output_language == "pt-PT"
    assert changes == []


def test_french_variant_uses_known_canadian_vocabulary() -> None:
    result = m.classify_french_variant(
        "Passe au depanneur avec ton cellulaire. "
        "On ira magasiner en fin de semaine, puis au stationnement."
    )

    assert result["code"] == "fr-CA"


def test_french_variant_uses_known_france_vocabulary() -> None:
    result = m.classify_french_variant(
        "Prends ton telephone portable et gare la voiture au parking. "
        "On prendra le petit-dejeuner ce week-end."
    )

    assert result["code"] == "fr-FR"


def test_explicit_metadata_variant_votes_use_track_language_and_name() -> None:
    latin = subtitle_track(1, "es-419", "large")
    latin.original_name = "Spanish (Latin American)"
    latin.properties = {
        "language": "spa",
        "language_ietf": "es-419",
        "track_name": "Spanish (Latin American)",
    }
    sdh = subtitle_track(2, "es-ES", "large")
    sdh.original_name = "Spanish (Castilian) SDH"
    sdh.properties = {
        "language": "spa",
        "language_ietf": "es-ES",
        "track_name": "Spanish (Castilian) SDH",
        "flag_hearing_impaired": True,
    }

    assert m.explicit_metadata_variant_votes([latin, sdh]) == {"spa": {"es-419"}}


def test_sibling_metadata_consensus_can_fix_generic_spanish_special() -> None:
    special = subtitle_track(1, "spa", "large")
    special.analysis = m.SubtitleAnalysis(size_bytes=60_000, size_class="large")

    changes = m.apply_batch_language_variant_consensus(
        [special],
        {},
        {"spa": "es-419"},
    )

    assert special.output_language == "es-419"
    assert changes


def test_ordered_pgs_variants_skip_normal_forced_pair() -> None:
    normal = pgs_subtitle_track(1, size_bytes=4_000_000, display_events=900)
    forced = pgs_subtitle_track(2, size_bytes=120_000, size_class="small", display_events=12)

    m.apply_ordered_pgs_language_variants([normal, forced])

    assert normal.output_language == "por"
    assert forced.output_language == "por"


def test_ordered_pgs_variants_still_apply_to_two_full_tracks() -> None:
    first = pgs_subtitle_track(1, size_bytes=4_000_000, display_events=900)
    second = pgs_subtitle_track(2, size_bytes=3_800_000, display_events=880)

    m.apply_ordered_pgs_language_variants([first, second])

    assert first.output_language == "pt-BR"
    assert second.output_language == "pt-PT"


def test_ordered_pgs_variants_do_not_guess_spanish_variants_without_evidence() -> None:
    first = pgs_subtitle_track(1, language="spa", size_bytes=4_000_000, display_events=900)
    second = pgs_subtitle_track(2, language="spa", size_bytes=3_800_000, display_events=880)

    m.apply_ordered_pgs_language_variants([first, second])

    assert first.output_language == "spa"
    assert second.output_language == "spa"


def test_ordered_pgs_variants_do_not_guess_chinese_scripts_without_evidence() -> None:
    first = pgs_subtitle_track(1, language="chi", size_bytes=4_000_000, display_events=900)
    second = pgs_subtitle_track(2, language="chi", size_bytes=3_800_000, display_events=880)

    m.apply_ordered_pgs_language_variants([first, second])

    assert first.output_language == "chi"
    assert second.output_language == "chi"


def test_short_variant_subtitle_inherits_single_full_anchor(tmp_path: Path) -> None:
    normal = subtitle_track(1, "por", "large")
    normal.analysis = m.SubtitleAnalysis(
        size_bytes=60_000,
        size_class="large",
        text_sample="Cadê meu celular? Estou falando com você.",
    )
    forced = subtitle_track(2, "por", "small")
    forced.analysis = m.SubtitleAnalysis(size_bytes=800, size_class="small")

    m.detect_language_variants(tmp_path / "movie.mkv", [normal, forced], tmp_path / "cache")

    assert normal.output_language == "pt-BR"
    assert forced.output_language == "pt-BR"


def test_short_variant_subtitle_overrides_explicit_conflicting_hint(tmp_path: Path) -> None:
    normal = subtitle_track(1, "por", "large")
    normal.analysis = m.SubtitleAnalysis(
        size_bytes=60_000,
        size_class="large",
        text_sample="Cadê meu celular? Estou falando com você.",
    )
    forced = subtitle_track(2, "pt-PT", "small")
    forced.original_name = "Portuguese (Iberian) Forced"
    forced.analysis = m.SubtitleAnalysis(size_bytes=800, size_class="small")

    m.detect_language_variants(tmp_path / "movie.mkv", [normal, forced], tmp_path / "cache")

    assert normal.output_language == "pt-BR"
    assert forced.output_language == "pt-BR"


def test_short_variant_subtitle_keeps_variant_when_multiple_full_anchors_exist(tmp_path: Path) -> None:
    brazilian = subtitle_track(1, "pt-BR", "large")
    brazilian.analysis = m.SubtitleAnalysis(size_bytes=60_000, size_class="large")
    iberian = subtitle_track(2, "pt-PT", "large")
    iberian.analysis = m.SubtitleAnalysis(size_bytes=58_000, size_class="large")
    forced = subtitle_track(3, "pt-PT", "small")
    forced.original_name = "Portuguese (Iberian) Forced"
    forced.analysis = m.SubtitleAnalysis(size_bytes=800, size_class="small")

    m.detect_language_variants(tmp_path / "movie.mkv", [brazilian, iberian, forced], tmp_path / "cache")

    assert forced.output_language == "pt-PT"


def test_output_suffix_and_dir(tmp_path: Path) -> None:
    input_path = tmp_path / "Movie.mkv"
    output_dir = tmp_path / "out"
    assert m.output_path_for(input_path, None, output_dir=output_dir, output_suffix="fixed") == output_dir / "Movie.fixed.mkv"


def test_collect_mkv_files_allows_explicit_sorted_root(tmp_path: Path) -> None:
    sorted_dir = tmp_path / "_sorted"
    season_dir = sorted_dir / "Season 1"
    season_dir.mkdir(parents=True)
    movie = season_dir / "Episode.mkv"
    movie.write_bytes(b"")

    files, root = m.collect_mkv_files(sorted_dir, recursive=True)

    assert files == [movie]
    assert root == sorted_dir


def test_collect_mkv_files_skips_nested_generated_dirs(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    wanted = source / "Episode.mkv"
    wanted.write_bytes(b"")
    sorted_dir = source / "_sorted"
    sorted_dir.mkdir()
    generated = sorted_dir / "Episode.fixed.mkv"
    generated.write_bytes(b"")

    files, root = m.collect_mkv_files(source, recursive=True)

    assert files == [wanted]
    assert root == source


def test_collect_mkv_files_from_paths_single_folder_keeps_source_root(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    movie = source / "Movie.mkv"
    movie.write_bytes(b"")

    files, root = m.collect_mkv_files_from_paths([source], recursive=False)

    assert files == [movie]
    assert root == source


def test_collect_mkv_files_from_paths_dedupes_multiple_inputs(tmp_path: Path) -> None:
    source = tmp_path / "source"
    other = tmp_path / "other"
    source.mkdir()
    other.mkdir()
    first = source / "Movie.mkv"
    second = other / "Episode.mkv"
    first.write_bytes(b"")
    second.write_bytes(b"")

    files, root = m.collect_mkv_files_from_paths([first, source, other], recursive=False)

    assert files == [first, second]
    assert root is None


def test_empty_subtitle_is_dropped_by_default() -> None:
    empty = subtitle_track(1, "eng", "empty")
    normal = subtitle_track(2, "eng", "large")
    m.classify_subtitle_roles(
        subtitles=[empty, normal],
        audio_tracks=[],
        forced_subtitle_ids=set(),
        smart_sub_detection=True,
        drop_empty_subs=True,
    )
    assert empty.role == "empty"
    assert empty.drop is True
    assert "Forced Empty" not in empty.suggested_name


def test_mkvmerge_command_excludes_dropped_subtitles(tmp_path: Path) -> None:
    video = m.TrackInfo(
        id=0,
        type="video",
        codec="HEVC",
        codec_id="V_MPEGH/ISO/HEVC",
        language="und",
        output_language="und",
        language_name="Undetermined",
        original_name="",
        order=0,
        properties={},
    )
    kept = subtitle_track(1, "eng", "large")
    dropped = subtitle_track(2, "eng", "empty")
    kept.suggested_name = "English"
    dropped.suggested_name = "English"
    dropped.drop = True
    command = m.build_mkvmerge_command(
        mkvmerge=Path("mkvmerge"),
        input_path=tmp_path / "in.mkv",
        output_path=tmp_path / "out.mkv",
        videos=[video],
        audio_tracks=[],
        subtitles=[kept, dropped],
    )
    assert command[:3] == ["mkvmerge", "--ui-language", "en"]
    assert "--subtitle-tracks" in command
    assert command[command.index("--subtitle-tracks") + 1] == "1"
    assert "2:English" not in command


def test_parse_track_delay_overrides() -> None:
    assert m.parse_track_delay_overrides("1: 150, 2:-250 3=0", "--audio-delays") == {
        1: 150,
        2: -250,
        3: 0,
    }


def test_parse_track_delay_overrides_rejects_bad_input() -> None:
    try:
        m.parse_track_delay_overrides("1:+abc", "--audio-delays")
    except m.OrganizerError as error:
        assert "Invalid delay override" in str(error)
    else:
        raise AssertionError("Expected OrganizerError")


def test_mkvmerge_command_applies_audio_and_subtitle_delays(tmp_path: Path) -> None:
    audio = audio_track(1)
    subtitle = subtitle_track(2, "eng", "large")
    audio.suggested_name = "DTS-HD MA 5.1"
    subtitle.suggested_name = "English"
    audio.delay_ms = 150
    subtitle.delay_ms = -250

    command = m.build_mkvmerge_command(
        mkvmerge=Path("mkvmerge"),
        input_path=tmp_path / "in.mkv",
        output_path=tmp_path / "out.mkv",
        videos=[video_track(0)],
        audio_tracks=[audio],
        subtitles=[subtitle],
    )

    sync_values = [
        command[index + 1]
        for index, value in enumerate(command)
        if value == "--sync"
    ]
    assert "1:150" in sync_values
    assert "2:-250" in sync_values


def test_track_delay_overrides_validate_track_type() -> None:
    audio = audio_track(1)
    subtitle = subtitle_track(2)

    m.apply_track_delay_overrides([audio], [subtitle], {1: 100}, {2: -100})
    assert audio.delay_ms == 100
    assert subtitle.delay_ms == -100

    try:
        m.apply_track_delay_overrides([audio], [subtitle], {2: 100}, {})
    except m.OrganizerError as error:
        assert "audio tracks" in str(error)
    else:
        raise AssertionError("Expected OrganizerError")


def test_metadata_edit_plan_rejects_track_delays() -> None:
    audio = audio_track(1)
    audio.delay_ms = 120

    plan = m.metadata_edit_plan([video_track(0)], [audio], [])

    assert plan.can_edit is False
    assert plan.reason == "track delays require remux"


def test_metadata_edit_plan_allows_metadata_only_language_and_name_change(tmp_path: Path) -> None:
    video = m.TrackInfo(
        id=0,
        type="video",
        codec="HEVC",
        codec_id="V_MPEGH/ISO/HEVC",
        language="eng",
        output_language="eng",
        language_name="English",
        original_name="",
        order=0,
        properties={"number": 1, "language": "eng", "language_ietf": "en", "default_track": True},
        default=True,
    )
    audio = m.TrackInfo(
        id=1,
        type="audio",
        codec="DTS-HD Master Audio",
        codec_id="A_DTS",
        language="eng",
        output_language="eng",
        language_name="English",
        original_name="DTS-HD MA 5.1",
        order=1,
        properties={
            "number": 2,
            "language": "eng",
            "language_ietf": "en",
            "track_name": "DTS-HD MA 5.1",
            "default_track": True,
        },
        default=True,
    )
    audio.suggested_name = "DTS-HD MA 5.1"
    subtitle = subtitle_track(2, "es-ES")
    subtitle.order = 2
    subtitle.properties = {
        "number": 3,
        "language": "spa",
        "language_ietf": "es-ES",
        "track_name": "Spanish (Castilian)",
        "default_track": False,
        "forced_track": False,
    }
    subtitle.original_name = "Spanish (Castilian)"
    subtitle.output_language = "es-419"
    subtitle.language_name = m.language_display_name("es-419")
    subtitle.suggested_name = "Spanish (Latin American)"

    plan = m.metadata_edit_plan([video], [audio], [subtitle])
    command = m.build_mkvpropedit_command(Path("mkvpropedit"), tmp_path / "movie.mkv", plan.edits)

    assert plan.can_edit is True
    assert len(plan.edits) == 1
    assert plan.edits[0].properties["language-ietf"] == "es-419"
    assert plan.edits[0].properties["name"] == "Spanish (Latin American)"
    assert command[:3] == ["mkvpropedit", "--ui-language", "en"]
    assert "--edit" in command
    assert "track:@3" in command
    assert "language-ietf=es-419" in command


def test_metadata_edit_plan_rejects_dropped_subtitle() -> None:
    normal = subtitle_track(1, "eng")
    normal.properties = {"number": 1, "language": "eng", "language_ietf": "en"}
    dropped = subtitle_track(2, "eng", "empty")
    dropped.properties = {"number": 2, "language": "eng", "language_ietf": "en"}
    dropped.drop = True

    plan = m.metadata_edit_plan([], [], [normal, dropped])

    assert plan.can_edit is False
    assert "removed" in plan.reason


def test_english_forced_subtitle_is_first_and_only_default() -> None:
    english_normal = subtitle_track(1, "eng")
    portuguese_forced = subtitle_track(2, "por")
    english_forced = subtitle_track(3, "eng")
    portuguese_forced.role = "forced"
    portuguese_forced.forced = True
    english_forced.role = "forced"
    english_forced.forced = True

    subtitles = [english_normal, portuguese_forced, english_forced]
    m.apply_default_flags([], [], subtitles)

    assert sorted(subtitles, key=m.subtitle_sort_key)[0] is english_forced
    assert english_forced.default is True
    assert english_normal.default is False
    assert portuguese_forced.default is False


def test_no_default_subtitle_without_english_forced() -> None:
    english_normal = subtitle_track(1, "eng")
    portuguese_forced = subtitle_track(2, "por")
    portuguese_forced.role = "forced"
    portuguese_forced.forced = True

    subtitles = [english_normal, portuguese_forced]
    m.apply_default_flags([], [], subtitles)

    assert all(track.default is False for track in subtitles)


def test_ordered_tracks_puts_default_audio_first() -> None:
    video = video_track(0)
    catalan = audio_track(1, "cat")
    taiwan = audio_track(2, "zh-TW")
    english = audio_track(6, "eng")
    audio_tracks = [catalan, taiwan, english]

    m.apply_default_flags([video], audio_tracks, [])

    assert english.default is True
    assert [track.id for track in m.ordered_tracks([video], audio_tracks, [])] == [0, 6, 1, 2]


def test_ordered_tracks_can_group_audio_by_region() -> None:
    video = video_track(0)
    cantonese = audio_track(1, "yue")
    catalan = audio_track(2, "cat")
    arabic = audio_track(3, "ara")
    taiwan = audio_track(4, "zh-TW")
    spanish = audio_track(5, "spa")
    english = audio_track(6, "eng")
    audio_tracks = [cantonese, catalan, arabic, taiwan, spanish, english]

    m.apply_default_flags([video], audio_tracks, [], "regional")

    assert english.default is True
    assert [track.id for track in m.ordered_tracks([video], audio_tracks, [], "regional")] == [0, 6, 5, 2, 1, 4, 3]


def test_ordered_tracks_can_customize_regional_order() -> None:
    video = video_track(0)
    cantonese = audio_track(1, "yue")
    catalan = audio_track(2, "cat")
    arabic = audio_track(3, "ara")
    taiwan = audio_track(4, "zh-TW")
    spanish = audio_track(5, "spa")
    english = audio_track(6, "eng")
    audio_tracks = [cantonese, catalan, arabic, taiwan, spanish, english]

    m.apply_default_flags([video], audio_tracks, [], "regional", "asia,europe,americas")

    assert english.default is True
    assert [track.id for track in m.ordered_tracks([video], audio_tracks, [], "regional", "asia,europe,americas")] == [
        0,
        6,
        1,
        4,
        5,
        2,
        3,
    ]


def test_parse_regional_order_accepts_aliases_and_appends_missing_regions() -> None:
    assert m.parse_regional_order("asia; middle east africa") == (
        "asia",
        "middle-east-africa",
        "europe",
        "americas",
        "oceania",
    )


def test_subtitle_regional_sort_keeps_related_languages_together() -> None:
    cantonese = subtitle_track(1, "yue")
    catalan = subtitle_track(2, "cat")
    arabic = subtitle_track(3, "ara")
    taiwan = subtitle_track(4, "zh-TW")
    spanish = subtitle_track(5, "spa")
    subtitles = [cantonese, catalan, arabic, taiwan, spanish]

    ordered = sorted(subtitles, key=lambda track: m.subtitle_sort_key(track, "regional"))

    assert [track.id for track in ordered] == [5, 2, 1, 4, 3]


def test_config_defaults(tmp_path: Path) -> None:
    config = tmp_path / "config.json"
    config.write_text(
        (
            '{"recursive": true, "output_suffix": "fixed", "detect_language_variants": false, '
            '"metadata_edit_mode": true, "audio_name_style": "language-format", '
            '"language_order_style": "regional", "regional_order": "asia;americas"}'
        ),
        encoding="utf-8",
    )
    defaults = m.load_config_file(config)
    assert defaults["recursive"] is True
    assert defaults["output_suffix"] == "fixed"
    assert defaults["detect_language_variants"] is False
    assert defaults["metadata_edit_mode"] == "auto"
    assert defaults["audio_name_style"] == "language-format"
    assert defaults["language_order_style"] == "regional"
    assert defaults["regional_order"] == ["asia", "americas"]


def test_config_metadata_edit_mode_accepts_off_and_only(tmp_path: Path) -> None:
    config = tmp_path / "config.json"
    config.write_text('{"metadata_edit_mode": "only"}', encoding="utf-8")
    assert m.load_config_file(config)["metadata_edit_mode"] == "only"

    config.write_text('{"metadata_edit_mode": false}', encoding="utf-8")
    assert m.load_config_file(config)["metadata_edit_mode"] == "off"


def test_config_variant_context_dirs_accepts_list_and_semicolon_text(tmp_path: Path) -> None:
    config = tmp_path / "config.json"
    config.write_text('{"variant_context_dirs": ["Season 7", "Season 8"]}', encoding="utf-8")
    defaults = m.load_config_file(config)
    assert defaults["variant_context_dirs"] == [Path("Season 7"), Path("Season 8")]

    config.write_text('{"variant_context_dirs": "Season 7;Season 8"}', encoding="utf-8")
    defaults = m.load_config_file(config)
    assert defaults["variant_context_dirs"] == [Path("Season 7"), Path("Season 8")]


def test_subtitle_language_overrides_support_multiple_languages() -> None:
    subtitles = [subtitle_track(7, "und"), subtitle_track(9, "und")]
    overrides = m.parse_subtitle_language_overrides(["spa:7", "es-419:9"])

    m.apply_subtitle_language_overrides(subtitles, overrides)

    assert subtitles[0].output_language == "spa"
    assert subtitles[0].language_name == "Spanish"
    assert subtitles[1].output_language == "es-419"
    assert subtitles[1].language_name == "Spanish (Latin American)"


def test_subtitle_language_overrides_accept_semicolon_config_text(tmp_path: Path) -> None:
    config = tmp_path / "config.json"
    config.write_text('{"subtitle_language_ids": "spa:7,8;fr-CA:9"}', encoding="utf-8")

    assert m.load_config_file(config)["subtitle_language_ids"] == ["spa:7,8", "fr-CA:9"]


def test_subtitle_language_overrides_reject_conflicts() -> None:
    try:
        m.parse_subtitle_language_overrides("spa:7;por:7")
    except m.OrganizerError as error:
        assert "conflicting language overrides" in str(error)
    else:
        raise AssertionError("Expected conflicting language overrides to fail")


def test_run_batch_returns_reports_and_events(tmp_path: Path, monkeypatch) -> None:
    input_path = tmp_path / "movie.mkv"
    input_path.write_bytes(b"")
    args = argparse.Namespace(
        path=input_path,
        output_dir=None,
        output_suffix="",
        report=False,
        report_dir=None,
        report_format="both",
        dry_run=True,
        detect_language_variants=False,
        batch_language_variant_consensus=True,
    )
    context = m.BatchRunContext(
        args=args,
        input_files=[input_path],
        source_root=None,
        forced_subtitle_ids=set(),
    )

    def fake_process_file(
        input_file,
        output_file,
        _args,
        _forced_ids,
        _consensus,
        progress_callback=None,
        cancel_callback=None,
    ):
        if progress_callback:
            progress_callback("Reading metadata", 5, 100)
            progress_callback("Preview complete", 100, 100)
        return m.file_report_data(input_file, output_file, "processed")

    monkeypatch.setattr(m, "process_file", fake_process_file)
    events: list[m.BatchRunEvent] = []

    result = m.run_batch(context, events.append)

    assert result.return_code == 0
    assert result.failures == 0
    assert result.reports[0]["status"] == "processed"
    assert [event.kind for event in events] == [
        "batch-started",
        "file-started",
        "file-progress",
        "file-progress",
        "file-finished",
        "batch-finished",
    ]
    progress_events = [event for event in events if event.kind == "file-progress"]
    assert [(event.step, event.steps) for event in progress_events] == [(5, 100), (100, 100)]


def test_run_batch_can_be_cancelled(tmp_path: Path, monkeypatch) -> None:
    input_path = tmp_path / "movie.mkv"
    input_path.write_bytes(b"")
    args = argparse.Namespace(
        path=input_path,
        output_dir=None,
        output_suffix="",
        report=False,
        report_dir=None,
        report_format="both",
        dry_run=False,
        detect_language_variants=False,
        batch_language_variant_consensus=True,
    )
    context = m.BatchRunContext(
        args=args,
        input_files=[input_path],
        source_root=None,
        forced_subtitle_ids=set(),
    )

    def fake_process_file(*_args, **_kwargs):
        raise m.OrganizerCancelled("stop")

    monkeypatch.setattr(m, "process_file", fake_process_file)
    events: list[m.BatchRunEvent] = []

    result = m.run_batch(context, events.append)

    assert result.cancelled is True
    assert result.return_code == 130
    assert result.reports[0]["status"] == "cancelled"
    assert [event.kind for event in events][-2:] == ["file-cancelled", "batch-cancelled"]


def test_normalize_ocr_output_moves_from_work_dir(tmp_path: Path) -> None:
    cache_dir = tmp_path / "cache"
    work_dir = tmp_path / "work"
    cache_dir.mkdir()
    work_dir.mkdir()
    sup_path = cache_dir / "movie.track_3.sup"
    expected_srt = cache_dir / "movie.track_3.srt"
    stray_srt = work_dir / "movie.track_3.srt"
    stray_srt.write_text("hello", encoding="utf-8")

    assert m.normalize_ocr_output(cache_dir, sup_path, expected_srt, work_dir)
    assert expected_srt.read_text(encoding="utf-8") == "hello"
    assert not stray_srt.exists()
