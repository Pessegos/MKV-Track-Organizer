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
        "probable_duplicate_group": "",
        "probable_duplicate_of_id": None,
        "probable_duplicate_reason": "",
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
        assert window.files_table.parentWidget().minimumWidth() >= 300
        window._populate_results([report])

        assert window.tracks_table.item(0, window.TRACK_PLAN_COLUMN).text() == "Delay +150 ms | Rename"
        assert window.tracks_table.item(1, window.TRACK_PLAN_COLUMN).text() == "Duplicate"
        assert "Plan: Delay +150 ms | Rename" in window.track_details_edit.toPlainText()
        assert "Original name: English" in window.track_details_edit.toPlainText()
        assert "2/2 included" in window.track_status_label.text()
        assert "1 duplicate warning" in window.track_status_label.text()
        assert window.track_select_audio_button.isEnabled()
        assert window.track_select_subtitles_button.isEnabled()
        assert not window.track_deselect_duplicate_audio_button.isEnabled()
        assert window.track_deselect_duplicate_subtitles_button.isEnabled()
        assert not window.track_reset_selection_button.isEnabled()
        assert not window.track_reset_order_button.isEnabled()

        window.tracks_table.selectRow(1)
        assert "Plan: Duplicate" in window.track_details_edit.toPlainText()
        assert "Possible duplicate of source.mkv track 1" in window.track_details_edit.toPlainText()
        assert window.track_include_selected_button.isEnabled()
        assert window.track_exclude_selected_button.isEnabled()

        window.exclude_selected_tracks()

        assert window.tracks_table.item(1, window.TRACK_PLAN_COLUMN).text() == "Exclude manually"
        assert "Excluded" in window.tracks_table.item(1, window.TRACK_FLAGS_COLUMN).text()
        assert "Selection: excluded (manual)" in window.track_details_edit.toPlainText()

        window.include_selected_tracks()

        assert window.tracks_table.item(1, window.TRACK_PLAN_COLUMN).text() == "Duplicate"

        window.deselect_duplicate_subtitle_tracks()

        assert window.tracks_table.item(1, window.TRACK_PLAN_COLUMN).text() == "Exclude manually"
        assert "1 manual selection edit" in window.track_status_label.text()
        assert window.track_reset_selection_button.isEnabled()

        window.reset_track_selection_edits()

        include_item = window.tracks_table.item(0, window.TRACK_INCLUDE_COLUMN)
        include_item.setCheckState(Qt.Unchecked)

        assert window.tracks_table.item(0, window.TRACK_PLAN_COLUMN).text() == "Exclude manually"
        assert "1 manual selection edit" in window.track_status_label.text()
        assert window.track_reset_selection_button.isEnabled()
        assert window.track_reset_button.isEnabled()

        window.reset_track_selection_edits()

        assert window.tracks_table.item(0, window.TRACK_PLAN_COLUMN).text() == "Delay +150 ms | Rename"
        assert not window.track_reset_selection_button.isEnabled()

        window._track_rows_reordered([1], 0)

        assert window.manual_track_order_active
        assert "manual order" in window.track_status_label.text()
        assert window.track_reset_order_button.isEnabled()

        window.reset_track_edits()

        assert not window.track_reset_button.isEnabled()
        assert "2/2 included" in window.track_status_label.text()
    finally:
        window.close()


def test_track_table_shows_regional_duplicate_warning_with_separate_drop_control(qapp):
    generic_dutch = report_track(
        10,
        "subtitles",
        "dut",
        codec="HDMV PGS",
        probable_duplicate_group="source.mkv:subtitles:10:probable",
        probable_duplicate_reason="Possible regional duplicate group: source.mkv track 10, source.mkv track 36",
    )
    flemish = report_track(
        36,
        "subtitles",
        "nl-BE",
        probable_duplicate_group="source.mkv:subtitles:10:probable",
        probable_duplicate_of_id=10,
        probable_duplicate_reason="Possible regional duplicate of source.mkv track 10",
    )
    report = {
        "status": "dry-run",
        "input": str(Path("C:/tmp/source.mkv")),
        "output": str(Path("C:/tmp/out.mkv")),
        "message": "",
        "command": [],
        "tracks": {"video": [], "audio": [], "subtitles": [generic_dutch, flemish]},
        "plan_summary": {
            "counts": {"regional_duplicate": 2},
            "items": [
                plan_item(
                    10,
                    "subtitles",
                    "regional_duplicate",
                    "Flag source.mkv: subtitle 10 Dutch as a possible regional duplicate group",
                    "Possible regional duplicate group: source.mkv track 10, source.mkv track 36",
                ),
                plan_item(
                    36,
                    "subtitles",
                    "regional_duplicate",
                    "Flag source.mkv: subtitle 36 Dutch (Flemish) as a possible regional duplicate",
                    "Possible regional duplicate of source.mkv track 10",
                ),
            ],
        },
    }

    window = gui.MainWindow()
    try:
        window._populate_results([report])

        assert window.tracks_table.item(0, window.TRACK_PLAN_COLUMN).text() == "Regional duplicate?"
        assert window.tracks_table.item(1, window.TRACK_PLAN_COLUMN).text() == "Regional duplicate?"
        assert "2 regional warning" in window.track_status_label.text()
        assert not window.track_deselect_duplicates_button.isEnabled()
        assert not window.track_deselect_duplicate_subtitles_button.isEnabled()
        assert window.track_deselect_probable_duplicates_button.isEnabled()

        window.tracks_table.selectRow(1)
        details = window.track_details_edit.toPlainText()
        assert "Plan: Regional duplicate?" in details
        assert "Possible regional duplicate of source.mkv track 10" in details

        window.deselect_probable_duplicate_tracks()

        assert window.tracks_table.item(0, window.TRACK_PLAN_COLUMN).text() == "Regional duplicate?"
        assert window.tracks_table.item(1, window.TRACK_PLAN_COLUMN).text() == "Exclude manually"
        assert window.tracks_table.item(0, window.TRACK_INCLUDE_COLUMN).checkState() == Qt.Checked
        assert window.tracks_table.item(1, window.TRACK_INCLUDE_COLUMN).checkState() == Qt.Unchecked
        assert "1 manual selection edit" in window.track_status_label.text()

        window.reset_track_selection_edits()

        assert window.tracks_table.item(1, window.TRACK_INCLUDE_COLUMN).checkState() == Qt.Checked
    finally:
        window.close()


def test_progress_switches_between_indeterminate_and_real_values(qapp):
    window = gui.MainWindow()
    try:
        window._start_progress_session("Organizer", "Starting run")
        window.handle_event(
            "file-progress",
            "movie.mkv: OCR PGS track 10 (1/4)",
            str(Path("C:/tmp/movie.mkv")),
            1,
            2,
            30,
            0,
        )

        assert window.progress.minimum() == 0
        assert window.progress.maximum() == 0
        assert "Organizer | 1/2" in window.progress_label.text()
        assert "OCR PGS track 10" in window.progress_label.toolTip()

        assert window._progress_started_at is not None
        window._progress_started_at -= 65
        window._refresh_progress_label()
        assert "01:" in window.progress_label.text()

        window.handle_event(
            "file-progress",
            "movie.mkv: Remuxing output (50%)",
            str(Path("C:/tmp/movie.mkv")),
            1,
            2,
            90,
            100,
        )

        assert window.progress.maximum() == window._progress_total_units(2, 100)
        assert window.progress.value() == 90
        assert window.progress.format() == "%p%"

        window._finish_progress_session("Completed")

        assert not window.progress_timer.isActive()
        assert "Completed" in window.progress_label.text()
    finally:
        window.close()


def test_raw_log_timestamps_chunks_and_output_tools(qapp):
    window = gui.MainWindow()
    try:
        window.append_log("first line\npartial")
        window.append_log(" continuation\n")
        lines = window.log_edit.toPlainText().splitlines()

        assert lines[0].startswith("[") and lines[0].endswith("first line")
        assert lines[1].startswith("[") and lines[1].endswith("partial continuation")
        assert lines[1].count("]") == 1

        controls = window._output_controls[id(window.output_tabs)]
        search_edit = controls["search"]
        assert isinstance(search_edit, gui.QLineEdit)
        window.output_tabs.setCurrentWidget(window.log_edit)
        search_edit.setText("partial continuation")
        window._find_output_text(window.output_tabs)

        assert window.log_edit.textCursor().selectedText() == "partial continuation"

        window._clear_output_text(window.output_tabs)

        assert not window.log_edit.toPlainText()
    finally:
        window.close()
