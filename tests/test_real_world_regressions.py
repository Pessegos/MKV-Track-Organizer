from __future__ import annotations

import json
from pathlib import Path

import pytest

import audio_sync
import mkv_track_organizer as organizer


FIXTURE_DIR = Path(__file__).parent / "fixtures" / "real_world"


def load_case(name: str) -> dict:
    return json.loads((FIXTURE_DIR / name).read_text(encoding="utf-8"))


def build_source_tracks(source: dict, source_index: int = 0) -> list[organizer.TrackInfo]:
    return organizer.build_tracks(
        source["metadata"],
        source_index=source_index,
        source_path=Path(source["file"]),
    )


def tracks_by_id(tracks: list[organizer.TrackInfo]) -> dict[int, organizer.TrackInfo]:
    return {track.id: track for track in tracks}


def test_mulan_audio_variants_remain_distinct_and_are_not_duplicates() -> None:
    case = load_case("mulan_language_variants.json")
    tracks = build_source_tracks(case["source"])
    audio_tracks = [track for track in tracks if track.type == "audio"]

    organizer.detect_duplicate_tracks(Path(case["source"]["file"]), audio_tracks, [])

    by_id = tracks_by_id(audio_tracks)
    assert {str(track_id): by_id[track_id].output_language for track_id in by_id} == case[
        "expected_languages"
    ]
    assert {str(track_id): by_id[track_id].language_name for track_id in by_id} == case["expected_names"]
    assert all(not track.duplicate_group for track in audio_tracks)


def test_atlantis_regional_forced_subtitles_remain_distinct() -> None:
    case = load_case("atlantis_forced_variants.json")
    subtitles = build_source_tracks(case["source"])

    organizer.classify_subtitle_roles(
        subtitles=subtitles,
        audio_tracks=[],
        forced_subtitle_ids=set(),
        smart_sub_detection=True,
        drop_empty_subs=False,
    )
    organizer.detect_duplicate_tracks(Path(case["source"]["file"]), [], subtitles)

    by_id = tracks_by_id(subtitles)
    assert {str(track_id): by_id[track_id].output_language for track_id in by_id} == case[
        "expected_languages"
    ]
    assert {str(track_id): by_id[track_id].role for track_id in by_id} == case["expected_roles"]
    assert all(not track.duplicate_group for track in subtitles)


def test_mulan_merged_sources_flag_regional_pairs_as_probable_only() -> None:
    case = load_case("mulan_merged_probables.json")
    subtitles: list[organizer.TrackInfo] = []
    for source_index, source in enumerate(case["sources"]):
        source_subtitles = build_source_tracks(source, source_index)
        organizer.classify_subtitle_roles(
            subtitles=source_subtitles,
            audio_tracks=[],
            forced_subtitle_ids=set(),
            smart_sub_detection=True,
            drop_empty_subs=False,
        )
        subtitles.extend(source_subtitles)

    organizer.detect_duplicate_tracks(
        Path(case["sources"][0]["file"]),
        [],
        subtitles,
        detect_exact_duplicates=True,
        detect_subtitle_language_duplicates=True,
    )

    by_source_and_id = {(track.source_index, track.id): track for track in subtitles}
    exact_duplicates = [track for track in subtitles if track.duplicate_group]
    assert len(exact_duplicates) == case["expected_exact_duplicate_count"]
    for expected_pair in case["expected_probable_groups"]:
        first = by_source_and_id[tuple(expected_pair[0])]
        second = by_source_and_id[tuple(expected_pair[1])]
        assert first.probable_duplicate_group
        assert second.probable_duplicate_group == first.probable_duplicate_group
        assert not first.drop
        assert not second.drop

    summary = organizer.plan_summary_for_tracks([], [], subtitles)
    assert summary["counts"]["regional_duplicate"] == 4


@pytest.mark.parametrize(
    "fixture_name",
    [
        "ratatouille_audio_sync.json",
        "hercules_audio_sync.json",
        "hunchback_audio_sync.json",
        "fantasia_2000_audio_sync.json",
    ],
)
def test_real_world_audio_sync_keeps_stable_offsets(
    fixture_name: str,
    monkeypatch,
    tmp_path: Path,
) -> None:
    case = load_case(fixture_name)
    raw_settings = case["settings"]
    requested_checkpoints = raw_settings.get("requested_checkpoints", len(case["estimates"]))
    settings = audio_sync.AudioSyncSettings(
        reference_path=tmp_path / "reference.mkv",
        source_path=tmp_path / "source.mka",
        start_seconds=raw_settings["start_seconds"],
        duration_seconds=raw_settings["duration_seconds"],
        checkpoints=requested_checkpoints,
        checkpoint_spacing_seconds=raw_settings["spacing_seconds"],
        max_offset_seconds=raw_settings["max_offset_seconds"],
    )
    estimates = iter(case["estimates"])

    monkeypatch.setattr(audio_sync, "validate_settings", lambda _settings: None)

    def fake_estimate(*_args, checkpoint_seconds: float, **_kwargs):
        try:
            raw_estimate = next(estimates)
        except StopIteration as error:
            raise audio_sync.AudioSyncNoAudio("no audio decoded near the end of the reference") from error
        return audio_sync.OffsetEstimate(
            checkpoint_seconds=checkpoint_seconds,
            offset_seconds=raw_estimate["offset"],
            coarse_seconds=raw_estimate["coarse"],
            confidence=raw_estimate["confidence"],
        )

    monkeypatch.setattr(audio_sync, "estimate_at_checkpoint", fake_estimate)

    result = audio_sync.estimate_offset(settings)
    expected = case["expected"]
    assert round(result.median_offset_seconds * 1000) == expected["offset_ms"]
    assert round(result.timeline_shift_seconds * 1000) == expected["timeline_shift_ms"]
    assert result.consistency == expected["consistency"]
    assert result.delay_reliability == expected.get("delay_reliability", "high")
    assert result.used_checkpoints == expected["used_checkpoints"]
    assert result.ignored_checkpoints == expected["ignored_checkpoints"]
    assert result.unavailable_checkpoints == expected.get("unavailable_checkpoints", 0)
    if expected.get("delay_reliability", "high") == "high":
        assert not result.warnings


def test_bambi_mux_plan_preserves_roles_order_delays_and_commentary_name(tmp_path: Path) -> None:
    case = load_case("bambi_mux_plan.json")
    tracks = build_source_tracks(case["source"])
    videos = [track for track in tracks if track.type == "video"]
    audio_tracks = [track for track in tracks if track.type == "audio"]
    subtitles = [track for track in tracks if track.type == "subtitles"]

    organizer.classify_subtitle_roles(
        subtitles=subtitles,
        audio_tracks=audio_tracks,
        forced_subtitle_ids=set(),
        smart_sub_detection=True,
        drop_empty_subs=False,
    )
    organizer.apply_audio_names(audio_tracks, "auto")
    organizer.apply_preserved_commentary_names([*audio_tracks, *subtitles])
    organizer.apply_track_delay_overrides(
        audio_tracks,
        subtitles,
        {3: case["delays_ms"]["audio:3"]},
        {6: case["delays_ms"]["subtitles:6"]},
    )
    organizer.apply_default_flags(videos, audio_tracks, subtitles)

    ordered = organizer.ordered_tracks(videos, audio_tracks, subtitles)
    assert [track.id for track in ordered if track.type == "audio"] == case["expected_audio_order"]
    assert tracks_by_id(audio_tracks)[2].suggested_name == case["expected_commentary_name"]
    subtitle_by_id = tracks_by_id(subtitles)
    assert {str(track_id): subtitle_by_id[track_id].role for track_id in subtitle_by_id} == case[
        "expected_roles"
    ]

    command = organizer.build_mkvmerge_command(
        mkvmerge=Path("mkvmerge"),
        input_path=tmp_path / case["source"]["file"],
        output_path=tmp_path / "bambi-organized.mkv",
        videos=videos,
        audio_tracks=audio_tracks,
        subtitles=subtitles,
    )
    sync_values = [command[index + 1] for index, value in enumerate(command) if value == "--sync"]
    assert "3:975" in sync_values
    assert "6:975" in sync_values
    assert "--disable-track-statistics-tags" in command
    assert "--no-track-tags" in command
