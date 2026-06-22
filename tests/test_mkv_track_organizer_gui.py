import json
import os
from pathlib import Path

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


def test_track_reorder_preserves_every_row_in_large_preview(qapp):
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
    try:
        window._populate_results([report])
        original_keys = window._track_order_keys_from_table()

        window._track_rows_reordered([40, 41, 42], 125)

        reordered_keys = window._track_order_keys_from_table()
        assert len(reordered_keys) == len(original_keys) == 160
        assert set(reordered_keys) == set(original_keys)
        assert reordered_keys[122:125] == original_keys[40:43]
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
        assert "Delay reliability: High" in summary
        assert "Checkpoint coverage: 6 used / 8 requested" in summary
        assert "Unavailable checkpoints: 2" in summary
        assert "Signal match strength" not in summary
        assert "match strength" not in summary
        assert "Warning:" not in summary
    finally:
        window.close()


def test_audio_sync_delay_lines_use_highlight_format(qapp):
    window = gui.MainWindow()
    try:
        delay_format = window._audio_sync_summary_line_format(
            "Timeline shift to apply: +981.45 ms"
        )

        assert delay_format is not None
        assert delay_format.foreground().color().name() == "#7dd3fc"
        assert delay_format.fontWeight() == gui.QFont.DemiBold
        assert window._audio_sync_summary_line_format("Delay reliability: High") is None
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

        window._set_progress_indeterminate()
        window._prepare_audio_sync_stream_probe_ui()

        assert window.progress.minimum() == 0
        assert window.progress.maximum() == 1
        assert window.progress.value() == 0
        assert window.progress_label.text() == "Idle"
        assert window.statusBar().currentMessage() == "Loading Audio Sync streams..."
    finally:
        window.close()
