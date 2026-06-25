import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6")

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication

import mkv_track_organizer as organizer
import mkv_track_organizer_gui as gui
from app_metadata import APP_NAME, APP_VERSION


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance() or QApplication([])
    return app


@pytest.fixture(autouse=True)
def isolated_profile_store(tmp_path, monkeypatch):
    profile_path = tmp_path / "profiles.json"
    monkeypatch.setattr(gui, "gui_profile_store_path", lambda: profile_path)
    monkeypatch.setattr(organizer, "DEFAULT_CONFIG_PATH", tmp_path / "config.json")
    return profile_path


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


def test_run_keeps_existing_preview_tracks_visible(qapp):
    audio = report_track(1, "audio", name="English - DTS-HD MA 5.1")
    report = {
        "status": "dry-run",
        "input": str(Path("C:/tmp/source.mkv")),
        "output": str(Path("C:/tmp/out.mkv")),
        "message": "",
        "command": [],
        "tracks": {"video": [], "audio": [audio], "subtitles": []},
        "plan_summary": {"counts": {}, "items": []},
    }
    window = gui.MainWindow()
    try:
        window._populate_results([report])

        window._prepare_organizer_run_ui(dry_run=False)

        assert window.current_reports == [report]
        assert window.tracks_table.rowCount() == 1
        assert window.tracks_table.item(0, window.TRACK_NAME_COLUMN).text() == "English - DTS-HD MA 5.1"
        assert not window.tracks_table.isEnabled()
        assert "Run started." in window.summary_edit.toPlainText()
    finally:
        window._set_running(False)
        window._reset_progress_session()
        window.close()


def test_add_preview_to_queue_uses_preview_overrides(qapp, monkeypatch):
    audio = report_track(1, "audio", name="English - DTS-HD MA 5.1")
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
        "plan_summary": {"counts": {"duplicate": 1}, "items": []},
    }

    window = gui.MainWindow()
    try:
        monkeypatch.setattr(window, "_validate_organizer_settings", lambda args, config_path: None)
        assert window.queue_add_button.text() == "Add preview"
        assert not window.queue_add_button.isEnabled()

        window._populate_results([report])
        preview_args, preview_config_path = window._build_args(dry_run=True)
        window.last_preview_args = preview_args
        window.last_preview_config_path = preview_config_path
        window._update_organizer_queue_add_button()

        assert window.queue_add_button.isEnabled()

        window.tracks_table.selectRow(1)
        window.deselect_duplicate_subtitle_tracks()
        window._track_rows_reordered([1], 0)
        duplicate_key = organizer.track_selection_key(0, "subtitles", 2)

        window.add_current_organizer_to_queue()

        assert len(window.organizer_queue) == 1
        item = window.organizer_queue[0]
        assert item.args.dry_run is False
        assert item.args.track_selection_overrides[duplicate_key] is False
        assert item.args.track_order_overrides[0] == duplicate_key
        assert "Preview plan" in item.message
    finally:
        window.close()


def test_single_track_check_updates_only_its_row(qapp, monkeypatch):
    tracks = [report_track(track_id, "audio", name=f"Audio {track_id}") for track_id in range(120)]
    report = {
        "status": "dry-run",
        "input": str(Path("C:/tmp/source.mkv")),
        "output": str(Path("C:/tmp/out.mkv")),
        "message": "",
        "command": [],
        "tracks": {"video": [], "audio": tracks, "subtitles": []},
        "plan_summary": {"counts": {}, "items": []},
    }
    window = gui.MainWindow()
    repopulated_rows: list[int] = []
    try:
        window._populate_results([report])
        monkeypatch.setattr(window, "_populate_tracks_for_row", repopulated_rows.append)

        window.tracks_table.item(50, window.TRACK_INCLUDE_COLUMN).setCheckState(Qt.Unchecked)
        qapp.processEvents()

        assert repopulated_rows == []
        assert window.tracks_table.rowCount() == 120
        assert window.tracks_table.item(50, window.TRACK_PLAN_COLUMN).text() == "Exclude manually"
        assert "Excluded" in window.tracks_table.item(50, window.TRACK_FLAGS_COLUMN).text()
        assert window.manual_track_includes[organizer.track_selection_key(0, "audio", 50)] is False
    finally:
        window.close()


def test_track_reorder_preserves_every_row_in_large_preview(qapp, monkeypatch):
    audio_tracks = [report_track(track_id, "audio", name=f"Audio {track_id}") for track_id in range(160)]
    report = {
        "status": "dry-run",
        "input": str(Path("C:/tmp/source.mkv")),
        "output": str(Path("C:/tmp/out.mkv")),
        "message": "",
        "command": [],
        "tracks": {"video": [], "audio": audio_tracks, "subtitles": []},
        "plan_summary": {"counts": {}, "items": []},
    }
    window = gui.MainWindow()
    repopulated_rows: list[int] = []
    try:
        window._populate_results([report])
        original_keys = window._track_order_keys_from_table()
        moved_items = [
            window.tracks_table.item(row, window.TRACK_INCLUDE_COLUMN)
            for row in (40, 41, 42)
        ]
        monkeypatch.setattr(window, "_populate_tracks_for_row", repopulated_rows.append)

        window._track_rows_reordered([40, 41, 42], 125)

        reordered_keys = window._track_order_keys_from_table()
        args, _config_path = window._build_args(dry_run=False)
        assert repopulated_rows == []
        assert len(reordered_keys) == len(original_keys) == 160
        assert set(reordered_keys) == set(original_keys)
        assert reordered_keys[122:125] == original_keys[40:43]
        assert [
            window.tracks_table.item(row, window.TRACK_INCLUDE_COLUMN)
            for row in (122, 123, 124)
        ] == moved_items
        assert args.track_order_overrides == reordered_keys
        assert window.tracks_table.rowCount() == 160
    finally:
        window.close()


def test_internal_track_drop_uses_captured_rows_when_qt_reports_viewport_source(qapp):
    table = gui.TrackTableWidget(4, 2)
    emitted: list[tuple[list[int], int]] = []

    class DropEvent:
        accepted = False

        def source(self):
            return table.viewport()

        def acceptProposedAction(self):
            self.accepted = True

    event = DropEvent()
    table._drag_rows = [1]
    table._pending_drop_row = 3
    table.rows_reordered.connect(lambda rows, target: emitted.append((rows, target)))

    table.dropEvent(event)

    assert event.accepted
    assert emitted == [([1], 3)]
    assert table._drag_rows == []
    assert table._pending_drop_row is None


def test_colored_duplicate_checkbox_does_not_reenter_item_changed(qapp):
    duplicate = report_track(
        2,
        "subtitles",
        duplicate_group="source.mkv:subtitles:1",
        duplicate_of_id=1,
        duplicate_reason="Exact duplicate of source.mkv track 1",
    )
    report = {
        "status": "dry-run",
        "input": str(Path("C:/tmp/source.mkv")),
        "output": str(Path("C:/tmp/out.mkv")),
        "message": "",
        "command": [],
        "tracks": {"video": [], "audio": [], "subtitles": [duplicate]},
        "plan_summary": {"counts": {"duplicate": 1}, "items": []},
    }
    window = gui.MainWindow()
    try:
        window._populate_results([report])

        window.tracks_table.item(0, window.TRACK_INCLUDE_COLUMN).setCheckState(Qt.Unchecked)
        qapp.processEvents()

        selection_key = organizer.track_selection_key(0, "subtitles", 2)
        assert window.manual_track_includes[selection_key] is False
        assert window.tracks_table.item(0, window.TRACK_PLAN_COLUMN).text() == "Exclude manually"
        assert window.tracks_table.item(0, window.TRACK_INCLUDE_COLUMN).checkState() == Qt.Unchecked
    finally:
        window.close()


def test_duplicate_cleanup_buttons_update_large_table_without_repopulate(qapp, monkeypatch):
    subtitles = []
    for track_id in range(240):
        overrides = {}
        if track_id % 4 == 1:
            overrides = {
                "duplicate_group": f"exact:{track_id - 1}",
                "duplicate_of_id": track_id - 1,
                "duplicate_reason": f"Exact duplicate of track {track_id - 1}",
            }
        elif track_id % 4 == 3:
            overrides = {
                "probable_duplicate_group": f"probable:{track_id - 1}",
                "probable_duplicate_of_id": track_id - 1,
                "probable_duplicate_reason": f"Possible regional duplicate of track {track_id - 1}",
            }
        subtitles.append(report_track(track_id, "subtitles", **overrides))

    report = {
        "status": "dry-run",
        "input": str(Path("C:/tmp/source.mkv")),
        "output": str(Path("C:/tmp/out.mkv")),
        "message": "",
        "command": [],
        "tracks": {"video": [], "audio": [], "subtitles": subtitles},
        "plan_summary": {"counts": {}, "items": []},
    }
    window = gui.MainWindow()
    repopulated_rows: list[int] = []
    try:
        window._populate_results([report])
        monkeypatch.setattr(window, "_populate_tracks_for_row", repopulated_rows.append)

        window.deselect_duplicate_tracks()
        window.deselect_probable_duplicate_tracks()
        qapp.processEvents()

        assert repopulated_rows == []
        assert window.tracks_table.rowCount() == 240
        for row in range(240):
            expected = Qt.Unchecked if row % 4 in {1, 3} else Qt.Checked
            assert window.tracks_table.item(row, window.TRACK_INCLUDE_COLUMN).checkState() == expected
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


def test_progress_uses_known_organizer_milestones_before_remux(qapp):
    window = gui.MainWindow()
    try:
        window._start_progress_session("Organizer", "Starting run")
        window.handle_event(
            "batch-progress",
            "Analyzing language context",
            "",
            0,
            2,
            0,
            0,
        )

        assert window.progress.minimum() == 0
        assert window.progress.maximum() == 0

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
        assert window.progress.maximum() == window._progress_total_units(2, 100)
        assert window.progress.value() == 30
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


def test_audio_sync_progress_advances_by_completed_checkpoints(qapp):
    window = gui.MainWindow()
    try:
        window._start_progress_session("Audio Sync", "Starting analysis")
        window._set_progress_value(11, 0)

        window.handle_audio_sync_log("Checkpoint 1/11 at 00:05:06.470")

        assert window.progress.minimum() == 0
        assert window.progress.maximum() == 11
        assert window.progress.value() == 0
        assert "Checkpoint 1/11" in window.progress_label.text()

        window.handle_audio_sync_progress(1, 11)

        assert window.progress.maximum() == 11
        assert window.progress.value() == 1
        assert window.progress.format() == "%p%"
        assert "Checkpoint 1/11 complete" in window.progress_label.text()
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


def test_profile_v1_migrates_to_complete_v2_payload(qapp, isolated_profile_store):
    isolated_profile_store.write_text(
        json.dumps(
            {
                "version": 1,
                "last_profile": "Legacy",
                "profiles": {
                    "Legacy": {
                        "output_suffix": "-legacy",
                        "overwrite": True,
                        "language_order_style": "regional",
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    window = gui.MainWindow()
    try:
        legacy = window.profiles["Legacy"]

        assert set(legacy) == set(window.PROFILE_FIELDS)
        assert legacy["output_suffix"] == "-legacy"
        assert legacy["existing_output_mode"] == "overwrite"
        assert legacy["language_order_style"] == "regional"
        assert window._loaded_profile_name == "Legacy"

        stored = json.loads(window.profile_store_path.read_text(encoding="utf-8"))

        assert stored["version"] == 2
        assert stored["ui"]["theme"] == "dark"
        assert set(stored["profiles"]["Legacy"]) == set(window.PROFILE_FIELDS)
    finally:
        window.close()


def test_profile_dirty_state_and_revert(qapp):
    window = gui.MainWindow()
    try:
        payload = window._validated_profile_payload_from_ui()
        payload["output_suffix"] = "-cinema"
        window.profiles = {"Cinema": payload}
        window._refresh_profile_combo("Cinema")
        window._apply_current_profile()

        assert window.profile_status_label.text() == "Saved"
        assert not window.update_profile_button.isEnabled()

        window.suffix_edit.setText("-changed")

        assert window.profile_status_label.text() == "Unsaved changes"
        assert window.update_profile_button.isEnabled()
        assert window.revert_profile_button.isEnabled()

        window.revert_current_profile()

        assert window.suffix_edit.text() == "-cinema"
        assert window.profile_status_label.text() == "Saved"
        assert not window.update_profile_button.isEnabled()
    finally:
        window.close()


def test_profile_update_rolls_back_when_store_write_fails(qapp, monkeypatch):
    window = gui.MainWindow()
    try:
        payload = window._validated_profile_payload_from_ui()
        payload["output_suffix"] = "-saved"
        window.profiles = {"Cinema": payload}
        window._refresh_profile_combo("Cinema")
        window._apply_current_profile()
        window.suffix_edit.setText("-unsaved")
        monkeypatch.setattr(window, "_write_profile_store", lambda: False)
        monkeypatch.setattr(gui.QMessageBox, "warning", lambda *args: gui.QMessageBox.Ok)

        assert not window._save_loaded_profile_changes()
        assert window.profiles["Cinema"]["output_suffix"] == "-saved"
        assert window.suffix_edit.text() == "-unsaved"
        assert window._profile_is_dirty()

        window.revert_current_profile()
    finally:
        window.close()


def test_config_custom_order_is_separate_from_active_profile_order(qapp):
    window = gui.MainWindow()
    try:
        original_active_order = window.custom_language_order_edit.text()
        window.config_custom_language_order_edit.setText("jpn, eng")

        assert window.config_save_button.isEnabled()
        assert window.custom_language_order_edit.text() == original_active_order

        window.apply_custom_config_to_organizer()

        assert window.custom_language_order_edit.text() == "jpn, eng"
        assert window.language_order_style_combo.currentData() == "custom"

        window.reset_config_defaults()

        assert not window.config_custom_language_order_edit.text()
        assert window.custom_language_order_edit.text() == "jpn, eng"
    finally:
        window.close()


def test_config_save_updates_the_global_baseline(qapp):
    window = gui.MainWindow()
    try:
        window.config_custom_language_order_edit.setText("jpn, eng")
        window.config_use_custom_order_check.setChecked(True)

        assert window._config_is_dirty()
        assert window.save_config_tab()
        assert not window._config_is_dirty()

        stored = json.loads(window._config_file_path().read_text(encoding="utf-8"))
        assert stored["custom_language_order"] == ["jpn", "eng"]
        assert stored["language_order_style"] == "custom"
    finally:
        window.close()


def test_dependency_manager_specs_cover_external_tools(qapp):
    window = gui.MainWindow()
    try:
        keys = {check.key for check in window._dependency_checks()}

        assert window.dependency_manager_button.text() == "Dependency manager"
        assert {
            "mkvmerge",
            "mkvextract",
            "mkvpropedit",
            "ffmpeg",
            "ffprobe",
            "makemkv",
            "seconv",
            "tesseract",
            "subtitle_edit",
        } <= keys
        assert all(check.download_url.startswith("https://") for check in window._dependency_checks())
    finally:
        window.close()


def test_dependency_installer_helpers_choose_github_release_assets():
    seconv = gui.dependency_check_by_key("seconv")
    subtitle_edit = gui.dependency_check_by_key("subtitle_edit")

    assert seconv is not None
    assert subtitle_edit is not None
    assert gui.dependency_is_installable(seconv)
    assert gui.dependency_is_installable(subtitle_edit)
    assert gui.seconv_asset_name_for_machine("AMD64") == "SeConv-Windows-x64.zip"
    assert gui.seconv_asset_name_for_machine("ARM64") == "SeConv-Windows-ARM64.zip"
    assert gui.dependency_asset_name_for_machine(subtitle_edit, "AMD64") == "SubtitleEdit-Windows-x64.zip"
    assert gui.dependency_asset_name_for_machine(subtitle_edit, "ARM64") == "SubtitleEdit-Windows-ARM64.zip"
    assert gui.dependency_install_target_dir(seconv).name == "seconv"
    assert gui.dependency_install_target_dir(subtitle_edit).name == "SubtitleEdit"


def test_github_release_asset_lookup_returns_download_url():
    release = {
        "assets": [
            {"name": "SeConv-Windows-x64.zip", "browser_download_url": "https://example.test/seconv.zip"},
        ]
    }

    assert gui.find_github_release_asset_url(release, "seconv-windows-x64.zip") == "https://example.test/seconv.zip"


def test_dependency_status_rows_distinguish_optional_missing(qapp, monkeypatch):
    window = gui.MainWindow()
    try:
        monkeypatch.setattr(
            window,
            "_resolve_dependency_path",
            lambda check, _args: (None, "searched nowhere") if check.key in {"mkvmerge", "mkvpropedit"} else (None, ""),
        )

        rows = window._dependency_status_rows()
        by_tool = {row["tool"]: row for row in rows}

        assert by_tool["mkvmerge"]["status"] == "Missing"
        assert by_tool["mkvpropedit"]["status"] == "Optional missing"
        assert "searched nowhere" in by_tool["mkvmerge"]["details"]
    finally:
        window.close()


def test_dependency_version_uses_first_output_line(qapp, monkeypatch, tmp_path):
    window = gui.MainWindow()
    captured: dict[str, object] = {}

    class Completed:
        stdout = "tool version 1.2.3\nextra details\n"
        stderr = ""
        returncode = 0

    def fake_run(command, **kwargs):
        captured["command"] = command
        captured["kwargs"] = kwargs
        return Completed()

    try:
        tool_path = tmp_path / "tool.exe"
        tool_path.write_text("", encoding="utf-8")
        monkeypatch.setattr(gui.subprocess, "run", fake_run)

        assert window._dependency_version(tool_path, ("--version",)) == "tool version 1.2.3"
        assert captured["command"] == [str(tool_path), "--version"]
        assert captured["kwargs"]["timeout"] == 5
    finally:
        window.close()


def test_organizer_queue_adds_current_preview_plan(qapp, monkeypatch):
    window = gui.MainWindow()
    try:
        args = SimpleNamespace(
            input_paths=[Path("C:/Movies/aladdin.mkv")],
            path=Path("C:/Movies/aladdin.mkv"),
            output_dir=Path("D:/sorted"),
            output_suffix="-queued",
            dry_run=True,
            track_selection_overrides={},
            track_order_overrides=[],
        )
        monkeypatch.setattr(window, "_validate_organizer_settings", lambda _args, _config_path: None)
        report = {
            "status": "dry-run",
            "input": str(Path("C:/Movies/aladdin.mkv")),
            "output": str(Path("D:/sorted/aladdin-queued.mkv")),
            "message": "",
            "command": [],
            "tracks": {"video": [], "audio": [report_track(1, "audio")], "subtitles": []},
            "plan_summary": {"counts": {}, "items": []},
        }
        window._populate_results([report])
        window.last_preview_args = args
        window.last_preview_config_path = Path("config.json")
        window._update_organizer_queue_add_button()

        window.add_current_organizer_to_queue()

        assert len(window.organizer_queue) == 1
        assert window.organizer_queue[0].args is not args
        assert window.organizer_queue[0].args.dry_run is False
        assert window.organizer_queue[0].status == "Queued"
        assert window.queue_table.item(0, window.QUEUE_PROJECT_COLUMN).text() == "aladdin"
        assert "Preview plan" in window.queue_table.item(0, window.QUEUE_MESSAGE_COLUMN).text()
        assert window.queue_run_button.isEnabled()

        window.queue_table.selectRow(0)
        window.remove_selected_queue_items()

        assert window.organizer_queue == []
    finally:
        window.close()


def test_organizer_queue_starts_next_item_after_completion(qapp, monkeypatch):
    window = gui.MainWindow()
    started: list[int] = []
    try:
        first = gui.OrganizerQueueItem(
            item_id=1,
            name="first",
            args=SimpleNamespace(),
            config_path=None,
            input_summary="first.mkv",
            output_summary="_sorted",
        )
        second = gui.OrganizerQueueItem(
            item_id=2,
            name="second",
            args=SimpleNamespace(),
            config_path=None,
            input_summary="second.mkv",
            output_summary="_sorted",
        )
        window.organizer_queue = [first, second]
        window._refresh_queue_table()

        def fake_start_worker(_args, _config_path, dry_run=False):
            assert window.current_queue_item is not None
            started.append(window.current_queue_item.item_id)

        monkeypatch.setattr(window, "_start_organizer_worker", fake_start_worker)

        assert window._start_next_organizer_queue_item()
        assert started == [1]
        assert first.status == "Running"

        result = organizer.BatchRunResult(reports=[], failures=0, input_files=[], source_root=None)
        window.handle_completed(result)

        assert first.status == "Done"
        assert window.start_next_queue_after_thread

        window._thread_finished()

        assert started == [1, 2]
        assert second.status == "Running"
    finally:
        window.close()


def test_audio_sync_queue_adds_current_settings(qapp, tmp_path):
    window = gui.MainWindow()
    try:
        reference_path = tmp_path / "reference.mkv"
        source_path = tmp_path / "aladdin.mkv"
        reference_path.write_bytes(b"")
        source_path.write_bytes(b"")
        reference_streams = [
            gui.audio_sync.MediaStream(1, 0, "audio", "truehd", "eng", "English", 8),
        ]
        source_streams = [
            gui.audio_sync.MediaStream(2, 0, "audio", "eac3", "eng", "English", 6),
            gui.audio_sync.MediaStream(3, 1, "audio", "eac3", "por", "Portuguese", 6),
            gui.audio_sync.MediaStream(4, 0, "subtitle", "subrip", "eng", "English"),
        ]
        window.audio_sync_reference_edit.setText(str(reference_path))
        window.audio_sync_source_edit.setText(str(source_path))
        window.audio_sync_output_edit.setText(str(tmp_path / "synced"))
        window._apply_audio_sync_streams(
            reference_path.resolve(),
            source_path.resolve(),
            reference_streams,
            source_streams,
            5400.0,
            5300.0,
        )
        window.audio_sync_tracks_table.item(1, 0).setCheckState(Qt.Unchecked)

        window.add_current_audio_sync_to_queue()

        assert len(window.audio_sync_queue) == 1
        item = window.audio_sync_queue[0]
        assert item.status == "Queued"
        assert item.name == "aladdin"
        assert item.settings.reference_path == reference_path.resolve()
        assert item.settings.source_path == source_path.resolve()
        assert item.reference_streams == reference_streams
        assert item.source_streams == source_streams
        assert item.selected_audio_indices == [0]
        assert item.output_dir_text == str(tmp_path / "synced")
        assert window.audio_sync_queue_table.item(0, window.AUDIO_SYNC_QUEUE_PROJECT_COLUMN).text() == "aladdin"
        assert window.audio_sync_queue_table.item(0, window.AUDIO_SYNC_QUEUE_MESSAGE_COLUMN).text() == "Waiting"
        assert window.audio_sync_queue_run_button.isEnabled()

        window.audio_sync_queue_table.selectRow(0)
        window.remove_selected_audio_sync_queue_items()

        assert window.audio_sync_queue == []
    finally:
        window.close()


def test_audio_sync_queue_starts_next_item_after_completion(qapp, monkeypatch):
    window = gui.MainWindow()
    started: list[int] = []
    try:
        reference_streams = [gui.audio_sync.MediaStream(1, 0, "audio", "truehd", "eng")]
        source_streams = [gui.audio_sync.MediaStream(2, 0, "audio", "eac3", "eng")]
        first = gui.AudioSyncQueueItem(
            item_id=1,
            name="first",
            settings=gui.audio_sync.AudioSyncSettings(
                reference_path=Path("C:/Movies/first-ref.mkv"),
                source_path=Path("C:/Movies/first.mkv"),
                checkpoints=1,
            ),
            reference_streams=reference_streams,
            source_streams=source_streams,
            reference_duration_seconds=3600.0,
            source_duration_seconds=3600.0,
            output_dir_text="",
            selected_audio_indices=[0],
        )
        second = gui.AudioSyncQueueItem(
            item_id=2,
            name="second",
            settings=gui.audio_sync.AudioSyncSettings(
                reference_path=Path("C:/Movies/second-ref.mkv"),
                source_path=Path("C:/Movies/second.mkv"),
                checkpoints=1,
            ),
            reference_streams=reference_streams,
            source_streams=source_streams,
            reference_duration_seconds=3600.0,
            source_duration_seconds=3600.0,
            output_dir_text="",
            selected_audio_indices=[0],
        )
        window.audio_sync_queue = [first, second]
        window.audio_sync_auto_queue_organizer_check.setChecked(False)
        window._refresh_audio_sync_queue_table()

        def fake_start_worker(_settings):
            assert window.current_audio_sync_queue_item is not None
            started.append(window.current_audio_sync_queue_item.item_id)

        monkeypatch.setattr(window, "_start_audio_sync_worker", fake_start_worker)

        assert window._start_next_audio_sync_queue_item()
        assert started == [1]
        assert first.status == "Running"

        result = gui.audio_sync.AudioSyncResult(
            estimates=[gui.audio_sync.OffsetEstimate(600.0, -1.0, -1.0, 1.0)],
            median_offset_seconds=-1.0,
            spread_seconds=0.0,
            average_confidence=1.0,
            consistency="excellent",
            verdict="reliable fixed delay: strong checkpoint consensus",
            used_checkpoints=1,
            attempted_checkpoints=1,
            delay_reliability="high",
        )
        window.handle_audio_sync_completed(result)

        assert first.status == "Done"
        assert first.result is result
        assert "Delay source by 1000.00 ms" in first.message
        assert window.start_next_audio_sync_queue_after_thread
        assert window.progress.maximum() == 2
        assert window.progress.value() == 1
        assert "Queue item 1/2: first" in window.audio_sync_summary_edit.toPlainText()
        assert "Recommended correction: Delay source by 1000.00 ms" in window.audio_sync_summary_edit.toPlainText()

        window._audio_sync_thread_finished()

        assert started == [1, 2]
        assert second.status == "Running"

        second_result = gui.audio_sync.AudioSyncResult(
            estimates=[gui.audio_sync.OffsetEstimate(600.0, -0.5, -0.5, 1.0)],
            median_offset_seconds=-0.5,
            spread_seconds=0.0,
            average_confidence=1.0,
            consistency="excellent",
            verdict="reliable fixed delay: strong checkpoint consensus",
            used_checkpoints=1,
            attempted_checkpoints=1,
            delay_reliability="high",
        )
        window.handle_audio_sync_progress(1, 1)
        window.handle_audio_sync_completed(second_result)
        window._audio_sync_thread_finished()

        summary = window.audio_sync_summary_edit.toPlainText()
        assert "Queue item 1/2: first" in summary
        assert "Queue item 2/2: second" in summary
        assert "Recommended correction: Delay source by 500.00 ms" in summary
        assert "Queue summary" in summary
        assert "Jobs: 2 done, 0 error(s), 0 cancelled" in summary
        assert window.progress.maximum() == 2
        assert window.progress.value() == 2
        assert "2/2" in window.progress_label.text()
    finally:
        window.close()


def test_audio_sync_result_can_queue_organizer_job(qapp, tmp_path, monkeypatch):
    window = gui.MainWindow()
    try:
        reference_path = tmp_path / "reference.mkv"
        source_path = tmp_path / "source.mkv"
        reference_path.write_bytes(b"")
        source_path.write_bytes(b"")
        reference_streams = [gui.audio_sync.MediaStream(1, 0, "audio", "truehd", "eng")]
        source_streams = [
            gui.audio_sync.MediaStream(2, 0, "audio", "eac3", "eng"),
            gui.audio_sync.MediaStream(3, 1, "audio", "eac3", "por"),
            gui.audio_sync.MediaStream(4, 0, "subtitle", "subrip", "eng"),
        ]
        window.audio_sync_reference_edit.setText(str(reference_path))
        window.audio_sync_source_edit.setText(str(source_path))
        window._apply_audio_sync_streams(
            reference_path.resolve(),
            source_path.resolve(),
            reference_streams,
            source_streams,
            3600.0,
            3600.0,
        )
        window.audio_sync_tracks_table.item(1, 0).setCheckState(Qt.Unchecked)
        window.audio_sync_result = gui.audio_sync.AudioSyncResult(
            estimates=[gui.audio_sync.OffsetEstimate(600.0, -1.0, -1.0, 1.0)],
            median_offset_seconds=-1.0,
            spread_seconds=0.0,
            average_confidence=1.0,
            consistency="excellent",
            verdict="reliable fixed delay: strong checkpoint consensus",
            used_checkpoints=1,
            attempted_checkpoints=1,
            delay_reliability="high",
        )
        monkeypatch.setattr(window, "_validate_organizer_settings", lambda _args, _config_path: None)
        monkeypatch.setattr(window, "_matroska_track_ids_by_type", lambda _path, _track_type: [4])

        window.add_audio_sync_result_to_organizer_queue()

        assert len(window.organizer_queue) == 1
        item = window.organizer_queue[0]
        assert item.name == "source"
        assert item.message == "From Audio Sync: +1000"
        assert item.args.input_paths == [source_path.resolve()]
        assert item.args.path == source_path.resolve()
        assert item.args.audio_delays == "2:+1000"
        assert item.args.subtitle_delays == "4:+1000"
        assert item.args.track_selection_overrides == {}
        assert item.args.track_order_overrides == []
        assert window.queue_table.item(0, window.QUEUE_MESSAGE_COLUMN).text() == "From Audio Sync: +1000"
    finally:
        window.close()


def test_audio_sync_queue_completion_can_add_organizer_queue_job(qapp, tmp_path, monkeypatch):
    window = gui.MainWindow()
    try:
        reference_path = tmp_path / "reference.mkv"
        source_path = tmp_path / "queued-source.mkv"
        reference_path.write_bytes(b"")
        source_path.write_bytes(b"")
        item = gui.AudioSyncQueueItem(
            item_id=1,
            name="queued-source",
            settings=gui.audio_sync.AudioSyncSettings(
                reference_path=reference_path.resolve(),
                source_path=source_path.resolve(),
                checkpoints=1,
            ),
            reference_streams=[gui.audio_sync.MediaStream(1, 0, "audio", "truehd", "eng")],
            source_streams=[
                gui.audio_sync.MediaStream(2, 0, "audio", "eac3", "eng"),
                gui.audio_sync.MediaStream(4, 0, "subtitle", "subrip", "eng"),
            ],
            reference_duration_seconds=3600.0,
            source_duration_seconds=3600.0,
            output_dir_text="",
            selected_audio_indices=[0],
        )
        window.audio_sync_queue = [item]
        window.current_audio_sync_queue_item = item
        window.audio_sync_auto_queue_organizer_check.setChecked(True)
        monkeypatch.setattr(window, "_validate_organizer_settings", lambda _args, _config_path: None)
        monkeypatch.setattr(window, "_matroska_track_ids_by_type", lambda _path, _track_type: [4])
        result = gui.audio_sync.AudioSyncResult(
            estimates=[gui.audio_sync.OffsetEstimate(600.0, -0.5, -0.5, 1.0)],
            median_offset_seconds=-0.5,
            spread_seconds=0.0,
            average_confidence=1.0,
            consistency="excellent",
            verdict="reliable fixed delay: strong checkpoint consensus",
            used_checkpoints=1,
            attempted_checkpoints=1,
            delay_reliability="high",
        )

        window.handle_audio_sync_completed(result)

        assert item.status == "Done"
        assert item.result is result
        assert "Organizer queued" in item.message
        assert len(window.organizer_queue) == 1
        organizer_item = window.organizer_queue[0]
        assert organizer_item.name == "queued-source"
        assert organizer_item.args.audio_delays == "2:+500"
        assert organizer_item.args.subtitle_delays == "4:+500"
    finally:
        window.close()


def test_imported_profiles_can_keep_or_replace_conflicts(qapp):
    window = gui.MainWindow()
    try:
        original = window._validated_profile_payload_from_ui()
        original["output_suffix"] = "-old"
        replacement = dict(original)
        replacement["output_suffix"] = "-new"
        extra = dict(original)
        extra["output_suffix"] = "-extra"
        window.profiles = {"Cinema": original}

        imported_count, skipped_count = window._merge_imported_profiles(
            {"cinema": replacement, "Extra": extra},
            overwrite=False,
        )

        assert (imported_count, skipped_count) == (1, 1)
        assert window.profiles["Cinema"]["output_suffix"] == "-old"
        assert window.profiles["Extra"]["output_suffix"] == "-extra"

        imported_count, skipped_count = window._merge_imported_profiles(
            {"cinema": replacement},
            overwrite=True,
        )

        assert (imported_count, skipped_count) == (1, 0)
        assert window.profiles["Cinema"]["output_suffix"] == "-new"
    finally:
        window.close()


def test_about_dialog_uses_release_identity(qapp, monkeypatch):
    window = gui.MainWindow()
    captured: dict[str, str] = {}
    try:
        monkeypatch.setattr(
            gui.QMessageBox,
            "about",
            lambda _parent, title, text: captured.update(title=title, text=text),
        )

        window.show_about_dialog()

        assert captured["title"] == f"About {APP_NAME}"
        assert APP_VERSION in captured["text"]
        assert "Documentation" in captured["text"]
    finally:
        window.close()


def test_gui_version_mode_does_not_start_the_event_loop(capsys):
    assert gui.main(["mkv-track-organizer", "--version"]) == 0
    assert f"{APP_NAME} {APP_VERSION}" in capsys.readouterr().out


def test_audio_sync_summary_prioritizes_delay_reliability(qapp):
    window = gui.MainWindow()
    try:
        estimates = [
            gui.audio_sync.OffsetEstimate(600.0 + index * 900, -0.98145, -0.980, 1.5)
            for index in range(6)
        ]
        result = gui.audio_sync.AudioSyncResult(
            estimates=estimates,
            median_offset_seconds=-0.98145,
            spread_seconds=0.00034,
            average_confidence=1.5,
            consistency="excellent",
            verdict="reliable fixed delay: strong checkpoint consensus",
            used_checkpoints=6,
            confidence_summary="very low",
            delay_reliability="high",
            reliability_reason="6 timeline checkpoints agree within +/-0.34 ms",
            attempted_checkpoints=8,
        )

        window.handle_audio_sync_completed(result)

        summary = window.audio_sync_summary_edit.toPlainText()
        assert "Recommended correction: Delay source by 981.45 ms" in summary
        assert "Source offset vs reference:" not in summary
        assert "Timeline shift to apply:" not in summary
        assert "Measured timing:" not in summary
        assert "Delay reliability: High" in summary
        assert "Checkpoint coverage: 6 used / 8 requested" in summary
        assert "Unavailable checkpoints: 2" in summary
        assert "Signal match strength" not in summary
        assert "match strength" not in summary
        assert "Warning:" not in summary
    finally:
        window.close()


def test_audio_sync_summary_explains_linear_drift_correction(qapp):
    window = gui.MainWindow()
    try:
        estimates = [
            gui.audio_sync.OffsetEstimate(300.0 + index * 600.0, -0.340 - index * 0.600, -0.340, 0.8)
            for index in range(6)
        ]
        result = gui.audio_sync.AudioSyncResult(
            estimates=estimates,
            median_offset_seconds=-1.840,
            spread_seconds=1.500,
            average_confidence=0.8,
            consistency="poor",
            verdict="reliable linear drift correction: timestamp stretch required",
            used_checkpoints=6,
            confidence_summary="very low",
            delay_reliability="high",
            reliability_reason="6 checkpoints form a linear drift with residuals within +/-0.00 ms",
            attempted_checkpoints=6,
            drift_slope_seconds_per_second=-0.001,
            drift_intercept_seconds=-0.040,
            drift_residual_spread_seconds=0.0,
            drift_correction_delay_seconds=0.040,
            drift_correction_stretch_factor=1.001,
            drift_reliability="high",
            drift_reason="6 checkpoints form a linear drift with residuals within +/-0.00 ms",
        )

        window.handle_audio_sync_completed(result)

        summary = window.audio_sync_summary_edit.toPlainText()
        assert "Recommended correction: Delay source by 40.00 ms and stretch timestamps x1.001" in summary
        assert "Correction reliability: High" in summary
        assert "Linear drift: -0.1000%" in summary
        assert "Timing agreement after drift fit: max residual 0.00 ms" in summary
        assert "Fixed-delay spread before correction: 1500.00 ms" in summary
        assert window._audio_sync_organizer_sync_value() == "40,1.001"
    finally:
        window.close()


def test_audio_sync_delay_lines_use_highlight_format(qapp):
    window = gui.MainWindow()
    try:
        delay_format = window._audio_sync_summary_line_format(
            "Recommended correction: Delay source by 981.45 ms"
        )

        assert delay_format is not None
        assert delay_format.foreground().color().name() == "#7dd3fc"
        assert delay_format.fontWeight() == gui.QFont.DemiBold
        assert (
            window._audio_sync_summary_line_format("Delay reliability: High").foreground().color().name()
            == "#86efac"
        )
        assert (
            window._audio_sync_summary_line_format("Delay reliability: Medium").foreground().color().name()
            == "#facc15"
        )
        assert (
            window._audio_sync_summary_line_format("Delay reliability: Low").foreground().color().name()
            == "#fca5a5"
        )
    finally:
        window.close()


def test_organizer_summary_includes_full_error_and_warning_details(qapp):
    reports = [
        {
            "status": "error",
            "input": "C:/media/bambi.mkv",
            "output": "C:/media/_sorted/bambi-smart-regional.mkv",
            "message": (
                "Output already exists for this file:\n"
                "C:/media/_sorted/bambi-smart-regional.mkv\n"
                "Will not overwrite."
            ),
            "verification": {},
            "plan_summary": {},
        },
        {
            "status": "processed-with-warnings",
            "input": "C:/media/damaged.mkv",
            "output": "C:/media/_sorted/damaged.mkv",
            "message": (
                "mkvmerge completed with warnings; output track plan verified\n"
                "The last timestamp before the error was 00:53:14.066."
            ),
            "verification": {"status": "ok", "errors": [], "warnings": []},
            "plan_summary": {},
        },
    ]
    result = organizer.BatchRunResult(
        reports=reports,
        failures=1,
        input_files=[Path("C:/media/bambi.mkv"), Path("C:/media/damaged.mkv")],
        source_root=None,
    )
    window = gui.MainWindow()
    try:
        window._append_organizer_result_summary(result)
        summary = window.summary_edit.toPlainText()

        assert "Details" in summary
        assert "Error: bambi.mkv" in summary
        assert "Output already exists for this file:" in summary
        assert "C:/media/_sorted/bambi-smart-regional.mkv" in summary
        assert "Warning: damaged.mkv" in summary
        assert "00:53:14.066" in summary
    finally:
        window.close()


def test_progress_updates_windows_taskbar_state(qapp):
    class FakeTaskbarProgress:
        def __init__(self):
            self.events: list[tuple] = []

        def set_indeterminate(self):
            self.events.append(("indeterminate",))

        def set_value(self, maximum, value):
            self.events.append(("value", maximum, value))

        def clear(self):
            self.events.append(("clear",))

        def close(self):
            self.events.append(("close",))

    window = gui.MainWindow()
    taskbar = FakeTaskbarProgress()
    window._taskbar_progress = taskbar
    try:
        window._start_progress_session("Organizer", "Starting")
        window._set_progress_indeterminate()
        window._set_progress_value(100, 35)
        window._finish_progress_session("Completed")

        assert taskbar.events == [
            ("indeterminate",),
            ("value", 100, 35),
            ("clear",),
        ]
    finally:
        window.close()

    assert taskbar.events[-1] == ("close",)


def test_audio_sync_full_timeline_plan_uses_shared_duration(qapp):
    window = gui.MainWindow()
    try:
        window.audio_sync_reference_duration_seconds = 5400.0
        window.audio_sync_source_duration_seconds = 5200.0
        window._refresh_audio_sync_analysis_plan()

        plan = window._current_audio_sync_analysis_plan()

        assert plan.mode == "full"
        assert plan.media_duration_seconds == 5200.0
        assert plan.checkpoints == 9
        assert plan.last_checkpoint_seconds < 5200.0
        assert "shared duration" in window.audio_sync_analysis_plan_label.text()
        assert "9 checkpoints" in window.audio_sync_analysis_plan_label.text()
        assert window.audio_sync_analysis_plan_label.objectName() == "audioSyncPlan"
        assert window.audio_sync_analysis_plan_label.minimumHeight() == 30
        assert window.audio_sync_analysis_plan_label.wordWrap()
    finally:
        window.close()


def test_audio_sync_prioritizes_summary_space_and_keeps_probe_progress_idle(qapp):
    window = gui.MainWindow()
    try:
        assert window.width() >= 1400
        assert window.height() >= 900
        window.resize(1400, 900)
        window.show()
        qapp.processEvents()

        track_height, output_height = window.audio_sync_splitter.sizes()
        assert output_height > track_height
        assert not window.audio_sync_splitter.isCollapsible(0)
        assert not window.audio_sync_splitter.isCollapsible(1)
        assert window.audio_sync_queue_table.minimumHeight() >= 110

        window._set_progress_indeterminate()
        window._prepare_audio_sync_stream_probe_ui()

        assert window.progress.minimum() == 0
        assert window.progress.maximum() == 1
        assert window.progress.value() == 0
        assert window.progress_label.text() == "Idle"
        assert window.statusBar().currentMessage() == "Loading Audio Sync streams..."
    finally:
        window.close()
