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
