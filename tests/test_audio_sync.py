from __future__ import annotations

import sys
from pathlib import Path

import audio_sync as sync


def test_parse_and_format_time() -> None:
    assert sync.parse_time("00:10:00") == 600
    assert sync.parse_time("10:30") == 630
    assert sync.parse_time("12.5") == 12.5
    assert sync.format_time(600.25) == "00:10:00.250"
    assert sync.format_time(-1.5) == "-00:00:01.500"


def test_parse_ffprobe_streams_assigns_relative_indexes() -> None:
    payload = {
        "streams": [
            {"index": 0, "codec_type": "video", "codec_name": "hevc"},
            {
                "index": 1,
                "codec_type": "audio",
                "codec_name": "dts",
                "channels": 6,
                "tags": {"language": "eng", "title": "DTS-HD MA 5.1"},
            },
            {
                "index": 2,
                "codec_type": "audio",
                "codec_name": "aac",
                "channels": 2,
                "tags": {"language": "por"},
            },
            {
                "index": 57,
                "codec_type": "subtitle",
                "codec_name": "subrip",
                "tags": {"language": "por", "title": "Forced"},
            },
        ]
    }

    streams = sync.parse_ffprobe_streams(payload)

    assert [(item.type, item.index, item.relative_index) for item in streams] == [
        ("audio", 1, 0),
        ("audio", 2, 1),
        ("subtitle", 57, 0),
    ]
    assert "DTS-HD MA 5.1" in streams[0].label


def test_consistency_labels() -> None:
    assert sync.consistency_label(0.003, 4) == "excellent"
    assert sync.consistency_label(0.015, 4) == "good"
    assert sync.consistency_label(0.040, 4) == "fair"
    assert sync.consistency_label(0.100, 4) == "poor"


def test_audio_sync_result_timeline_shift_is_inverse_offset() -> None:
    result = sync.AudioSyncResult([], -0.975, 0.001, 8.0, "excellent", "high fixed-delay confidence")

    assert result.timeline_shift_seconds == 0.975


def test_estimate_offset_skips_checkpoints_with_no_decoded_audio(monkeypatch, tmp_path: Path) -> None:
    reference = tmp_path / "reference.mkv"
    source = tmp_path / "source.mkv"
    reference.touch()
    source.touch()
    settings = sync.AudioSyncSettings(reference, source, checkpoints=3)
    logs: list[str] = []
    progress: list[tuple[int, int]] = []

    monkeypatch.setattr(sync, "validate_settings", lambda _settings: None)

    def fake_estimate_at_checkpoint(*_args, checkpoint_seconds: float, **_kwargs):
        if checkpoint_seconds > settings.start_seconds:
            raise sync.AudioSyncNoAudio("no audio decoded")
        return sync.OffsetEstimate(checkpoint_seconds, 0.125, 0.120, 6.0)

    monkeypatch.setattr(sync, "estimate_at_checkpoint", fake_estimate_at_checkpoint)

    result = sync.estimate_offset(settings, logs.append, lambda index, total: progress.append((index, total)))

    assert result.median_offset_seconds == 0.125
    assert len(result.estimates) == 1
    assert any("skipped=no audio decoded" in line for line in logs)
    assert progress == [(1, 3), (2, 3), (3, 3)]


def test_estimate_offset_rejects_when_all_checkpoints_decode_no_audio(monkeypatch, tmp_path: Path) -> None:
    reference = tmp_path / "reference.mkv"
    source = tmp_path / "source.mkv"
    reference.touch()
    source.touch()
    settings = sync.AudioSyncSettings(reference, source, checkpoints=2)

    monkeypatch.setattr(sync, "validate_settings", lambda _settings: None)
    monkeypatch.setattr(
        sync,
        "estimate_at_checkpoint",
        lambda *_args, **_kwargs: (_ for _item in ()).throw(sync.AudioSyncNoAudio("no audio decoded")),
    )

    try:
        sync.estimate_offset(settings)
    except sync.AudioSyncError as error:
        assert "No usable checkpoints" in str(error)
    else:
        raise AssertionError("Expected all-empty checkpoints to fail")


def test_estimate_offset_marks_consistent_low_confidence_result_as_uncertain(monkeypatch, tmp_path: Path) -> None:
    reference = tmp_path / "reference.mkv"
    source = tmp_path / "source.mkv"
    reference.touch()
    source.touch()
    settings = sync.AudioSyncSettings(reference, source, checkpoints=3)

    monkeypatch.setattr(sync, "validate_settings", lambda _settings: None)
    estimates = iter(
        [
            sync.OffsetEstimate(600.0, -0.975, -0.980, 1.2),
            sync.OffsetEstimate(1500.0, -0.974, -0.980, 1.1),
            sync.OffsetEstimate(2400.0, -0.976, -0.980, 1.3),
        ]
    )
    monkeypatch.setattr(sync, "estimate_at_checkpoint", lambda *_args, **_kwargs: next(estimates))

    result = sync.estimate_offset(settings)

    assert result.consistency == "excellent"
    assert result.confidence_summary == "very low"
    assert result.verdict == "uncertain: very low correlation confidence"
    assert any("very low" in warning for warning in result.warnings)


def test_estimate_offset_ignores_main_cluster_outliers(monkeypatch, tmp_path: Path) -> None:
    reference = tmp_path / "reference.mkv"
    source = tmp_path / "source.mkv"
    reference.touch()
    source.touch()
    settings = sync.AudioSyncSettings(reference, source, checkpoints=4)

    monkeypatch.setattr(sync, "validate_settings", lambda _settings: None)
    estimates = iter(
        [
            sync.OffsetEstimate(600.0, 0.170, 0.170, 6.0),
            sync.OffsetEstimate(1500.0, 0.270, 0.270, 6.0),
            sync.OffsetEstimate(2400.0, 0.280, 0.280, 6.0),
            sync.OffsetEstimate(3300.0, 0.272, 0.270, 6.0),
        ]
    )
    monkeypatch.setattr(sync, "estimate_at_checkpoint", lambda *_args, **_kwargs: next(estimates))

    result = sync.estimate_offset(settings)

    assert round(result.median_offset_seconds, 3) == 0.272
    assert result.used_checkpoints == 3
    assert result.ignored_checkpoints == 1
    assert result.all_spread_seconds > 0.050
    assert any("outlier" in warning for warning in result.warnings)


def test_build_audio_export_plan(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(sync, "resolve_binary", lambda name, explicit_path=None: Path("ffmpeg"))
    stream = sync.MediaStream(index=3, relative_index=1, type="audio", codec="aac", language="por")

    plan = sync.build_export_plan(tmp_path / "source.mkv", stream, 0.97513, tmp_path / "out")

    assert plan.output_path.name == "source.a1.por.delay+975ms.mka"
    assert "-itsoffset" in plan.command
    assert "0:a:1" in plan.command
    assert plan.command[-2:] == ["copy", str(plan.output_path)]


def test_build_combined_audio_export_plan(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(sync, "resolve_binary", lambda name, explicit_path=None: Path("ffmpeg"))
    streams = [
        sync.MediaStream(index=1, relative_index=0, type="audio", codec="aac", language="eng"),
        sync.MediaStream(index=2, relative_index=1, type="audio", codec="eac3", language="por"),
    ]

    plan = sync.build_combined_audio_export_plan(tmp_path / "source.mkv", streams, 0.97513, tmp_path / "out")

    assert plan.output_path.name == "source.synced.delay+975ms.mka"
    assert plan.streams == tuple(streams)
    assert plan.command.count("-map") == 2
    assert "0:a:0" in plan.command
    assert "0:a:1" in plan.command
    assert plan.command[-2:] == ["copy", str(plan.output_path)]


def test_combined_audio_export_rejects_subtitles(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(sync, "resolve_binary", lambda name, explicit_path=None: Path("ffmpeg"))
    streams = [sync.MediaStream(index=57, relative_index=0, type="subtitle", codec="subrip", language="por")]

    try:
        sync.build_combined_audio_export_plan(tmp_path / "source.mkv", streams, 0.250, tmp_path / "out")
    except sync.AudioSyncError as error:
        assert "only supports audio" in str(error)
    else:
        raise AssertionError("Expected subtitle streams to be rejected")


def test_build_subtitle_export_plan(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(sync, "resolve_binary", lambda name, explicit_path=None: Path("ffmpeg"))
    stream = sync.MediaStream(index=57, relative_index=0, type="subtitle", codec="subrip", language="por")

    plan = sync.build_export_plan(tmp_path / "source.mkv", stream, -0.250, tmp_path / "out")

    assert plan.output_path.name == "source.s0.por.delay-250ms.mks"
    assert "0:57" in plan.command
    assert plan.command[-2:] == ["copy", str(plan.output_path)]


def test_run_capture_drains_large_stdout() -> None:
    result = sync.run_capture(
        [
            sys.executable,
            "-c",
            "import sys; sys.stdout.buffer.write(b'x' * 1048576)",
        ]
    )

    assert len(result.stdout) == 1_048_576
