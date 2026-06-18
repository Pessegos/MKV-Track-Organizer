import os
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6")

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication

import mkv_track_organizer as organizer
import mkv_track_organizer_gui as gui


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance() or QApplication([])
    return app


def report_track(track_id: int, track_type: str, language: str = "eng", **overrides):
    track = {
        "id": track_id,
        "selection_key": organizer.track_selection_key(0, track_type, track_id),
        "source_index": 0,
        "source_name": "source.mkv",
        "type": track_type,
        "codec": "DTS-HD Master Audio" if track_type == "audio" else "SubRip/SRT",
        "input_language": language,
        "output_language": language,
        "name": organizer.language_display_name(language),
        "original_name": "",
        "default": False,
        "forced": False,
        "drop": False,
        "role": "normal",
        "delay_ms": 0,
        "duplicate_group": "",
        "duplicate_of_id": None,
        "duplicate_reason": "",
        "role_reason": "",
        "role_scores": {},
    }
    track.update(overrides)
    return track


def plan_item(track_id: int, track_type: str, category: str, message: str, reason: str = ""):
    return {
        "category": category,
        "track_type": track_type,
        "track_id": track_id,
        "source_index": 0,
        "source_name": "source.mkv",
        "message": message,
        "reason": reason,
    }


def test_track_table_shows_plan_and_manual_exclude(qapp):
    audio = report_track(
        1,
        "audio",
        delay_ms=150,
        name="English - DTS-HD MA 5.1",
        original_name="English",
    )
    duplicate = report_track(
        2,
        "subtitles",
        duplicate_group="source.mkv:subtitles:1",
        duplicate_of_id=1,
        duplicate_reason="Possible duplicate of source.mkv track 1",
    )
    report = {
        "status": "dry-run",
        "input": str(Path("C:/tmp/source.mkv")),
        "output": str(Path("C:/tmp/out.mkv")),
        "message": "",
        "command": [],
        "tracks": {"video": [], "audio": [audio], "subtitles": [duplicate]},
        "plan_summary": {
            "counts": {"delay": 1, "name": 1, "duplicate": 1},
            "items": [
                plan_item(1, "audio", "delay", "Apply delay to source.mkv: audio 1 English: +150 ms"),
                plan_item(1, "audio", "name", "Set source.mkv: audio 1 English name: English - DTS-HD MA 5.1"),
                plan_item(2, "subtitles", "duplicate", "Flag source.mkv: subtitle 2 English as a possible duplicate"),
            ],
        },
    }

    window = gui.MainWindow()
    try:
        window._populate_results([report])

        assert window.tracks_table.item(0, window.TRACK_PLAN_COLUMN).text() == "Delay +150 ms | Rename"
        assert window.tracks_table.item(1, window.TRACK_PLAN_COLUMN).text() == "Duplicate"

        include_item = window.tracks_table.item(0, window.TRACK_INCLUDE_COLUMN)
        include_item.setCheckState(Qt.Unchecked)

        assert window.tracks_table.item(0, window.TRACK_PLAN_COLUMN).text() == "Exclude manually"
        assert window.track_reset_button.isEnabled()

        window.reset_track_edits()

        assert window.tracks_table.item(0, window.TRACK_PLAN_COLUMN).text() == "Delay +150 ms | Rename"
    finally:
        window.close()
