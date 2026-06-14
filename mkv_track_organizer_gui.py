from __future__ import annotations

import contextlib
import io
import sys
import threading
import traceback
from pathlib import Path

try:
    from PySide6.QtCore import QEvent, QObject, QThread, Qt, Signal, Slot
    from PySide6.QtGui import QColor, QCloseEvent, QDragEnterEvent, QDropEvent, QTextCursor
    from PySide6.QtWidgets import (
        QApplication,
        QAbstractItemView,
        QCheckBox,
        QComboBox,
        QFileDialog,
        QGridLayout,
        QGroupBox,
        QHBoxLayout,
        QHeaderView,
        QInputDialog,
        QLabel,
        QLineEdit,
        QMainWindow,
        QMessageBox,
        QPlainTextEdit,
        QProgressBar,
        QPushButton,
        QSizePolicy,
        QSplitter,
        QSpinBox,
        QStyle,
        QTabWidget,
        QTableWidget,
        QTableWidgetItem,
        QToolButton,
        QVBoxLayout,
        QWidget,
    )
except ModuleNotFoundError as error:
    print(
        "MKV Track Organizer GUI requires PySide6.\n"
        "Install it with: python -m pip install -r requirements.txt",
        file=sys.stderr,
    )
    raise SystemExit(1) from error

import mkv_track_organizer as organizer
import makemkv_batch as makemkv
import audio_sync


class SignalTextStream(io.TextIOBase):
    def __init__(self, write_callback):
        super().__init__()
        self._write_callback = write_callback

    def writable(self) -> bool:
        return True

    def write(self, text: str) -> int:
        if text:
            self._write_callback(text)
        return len(text)

    def flush(self) -> None:
        return None


class TrackTableWidget(QTableWidget):
    rows_reordered = Signal(list, int)

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._drag_rows: list[int] = []
        self._pending_drop_row: int | None = None

    def startDrag(self, supported_actions) -> None:
        self._drag_rows = self._selected_row_numbers()
        self._pending_drop_row = None
        super().startDrag(supported_actions)

    def dragMoveEvent(self, event) -> None:
        super().dragMoveEvent(event)
        if event.source() is self:
            self._pending_drop_row = self._drop_target_row(event)

    def dropEvent(self, event: QDropEvent) -> None:
        if event.source() is not self:
            super().dropEvent(event)
            return

        selected_rows = self._drag_rows or self._selected_row_numbers()
        if not selected_rows:
            super().dropEvent(event)
            return

        target_row = self._pending_drop_row
        if target_row is None:
            target_row = self._drop_target_row(event)
        target_row = max(0, min(target_row, self.rowCount()))
        self._drag_rows = []
        self._pending_drop_row = None

        event.acceptProposedAction()
        self.rows_reordered.emit(selected_rows, target_row)

    def _selected_row_numbers(self) -> list[int]:
        selected_rows = sorted({index.row() for index in self.selectionModel().selectedRows()})
        if selected_rows:
            return selected_rows
        return sorted({index.row() for index in self.selectedIndexes()})

    def _drop_target_row(self, event) -> int:
        position = event.position().toPoint() if hasattr(event, "position") else event.pos()
        index = self.indexAt(position)
        if not index.isValid():
            return self.rowCount()

        indicator = self.dropIndicatorPosition()
        if indicator == QAbstractItemView.DropIndicatorPosition.AboveItem:
            return index.row()
        if indicator == QAbstractItemView.DropIndicatorPosition.BelowItem:
            return index.row() + 1
        if indicator == QAbstractItemView.DropIndicatorPosition.OnViewport:
            return self.rowCount()

        row_rect = self.visualRect(index)
        return index.row() + (1 if position.y() > row_rect.center().y() else 0)


class OrganizerWorker(QObject):
    log = Signal(str)
    event = Signal(str, str, str, int, int, int, int)
    completed = Signal(object)
    failed = Signal(str)

    def __init__(self, args, config_path: Path | None) -> None:
        super().__init__()
        self.args = args
        self.config_path = config_path
        self._cancel_requested = threading.Event()

    @Slot()
    def run(self) -> None:
        try:
            stream = SignalTextStream(self.log.emit)
            with contextlib.redirect_stdout(stream), contextlib.redirect_stderr(stream):
                result = organizer.run_from_args(
                    self.args,
                    self.config_path,
                    self._emit_event,
                    self.cancel_requested,
                )
            self.completed.emit(result)
        except organizer.OrganizerCancelled:
            self.completed.emit(organizer.BatchRunResult([], 0, [], None, cancelled=True))
        except Exception:
            self.failed.emit(traceback.format_exc())

    @Slot()
    def cancel(self) -> None:
        self._cancel_requested.set()

    def cancel_requested(self) -> bool:
        return self._cancel_requested.is_set()

    def _emit_event(self, event: organizer.BatchRunEvent) -> None:
        self.event.emit(
            event.kind,
            event.message,
            str(event.file or ""),
            event.index or 0,
            event.total or 0,
            event.step or 0,
            event.steps or 0,
        )


class MakeMkvWorker(QObject):
    log = Signal(str)
    event = Signal(str, str, str, int, int, int, int)
    completed = Signal(object)
    failed = Signal(str)

    def __init__(self, job: makemkv.MakeMkvBatchJob, organizer_args=None, organizer_config_path: Path | None = None) -> None:
        super().__init__()
        self.job = job
        self.organizer_args = organizer_args
        self.organizer_config_path = organizer_config_path
        self._cancel_requested = threading.Event()

    @Slot()
    def run(self) -> None:
        result: makemkv.MakeMkvBatchResult | None = None
        try:
            stream = SignalTextStream(self.log.emit)
            with contextlib.redirect_stdout(stream), contextlib.redirect_stderr(stream):
                result = makemkv.run_batch(self.job, self._emit_event, self.cancel_requested)
                if self._should_run_organizer(result):
                    print()
                    print("Running MKV Track Organizer after MakeMKV...")
                    result.organizer_result = organizer.run_from_args(
                        self.organizer_args,
                        self.organizer_config_path,
                        self._emit_organizer_event,
                        self.cancel_requested,
                    )
            self.completed.emit(result)
        except organizer.OrganizerCancelled:
            if result is None:
                result = makemkv.MakeMkvBatchResult(cancelled=True)
            result.cancelled = True
            self.completed.emit(result)
        except Exception:
            self.failed.emit(traceback.format_exc())

    @Slot()
    def cancel(self) -> None:
        self._cancel_requested.set()

    def cancel_requested(self) -> bool:
        return self._cancel_requested.is_set()

    def _should_run_organizer(self, result: makemkv.MakeMkvBatchResult) -> bool:
        return (
            self.job.run_organizer_after
            and not self.job.dry_run
            and not result.cancelled
            and not result.failures
            and self.organizer_args is not None
        )

    def _emit_event(self, event: makemkv.MakeMkvBatchEvent) -> None:
        self.event.emit(
            event.kind,
            event.message,
            str(event.disc or ""),
            event.index or 0,
            event.total or 0,
            event.step or 0,
            event.steps or 0,
        )

    def _emit_organizer_event(self, event: organizer.BatchRunEvent) -> None:
        self.event.emit(
            f"organizer-{event.kind}",
            event.message,
            str(event.file or ""),
            event.index or 0,
            event.total or 0,
            event.step or 0,
            event.steps or 0,
        )


class AudioSyncWorker(QObject):
    log = Signal(str)
    progress = Signal(int, int)
    completed = Signal(object)
    failed = Signal(str)

    def __init__(self, settings: audio_sync.AudioSyncSettings) -> None:
        super().__init__()
        self.settings = settings
        self._cancel_requested = threading.Event()

    @Slot()
    def run(self) -> None:
        try:
            result = audio_sync.estimate_offset(
                self.settings,
                self.log.emit,
                self.progress.emit,
                self.cancel_requested,
            )
            self.completed.emit(result)
        except audio_sync.AudioSyncCancelled:
            self.failed.emit("Audio sync analysis cancelled.")
        except Exception:
            self.failed.emit(traceback.format_exc())

    @Slot()
    def cancel(self) -> None:
        self._cancel_requested.set()

    def cancel_requested(self) -> bool:
        return self._cancel_requested.is_set()


class AudioSyncExportWorker(QObject):
    log = Signal(str)
    completed = Signal(object)
    failed = Signal(str)

    def __init__(
        self,
        source_path: Path,
        streams: list[audio_sync.MediaStream],
        timeline_shift_seconds: float,
        output_dir: Path,
    ) -> None:
        super().__init__()
        self.source_path = source_path
        self.streams = streams
        self.timeline_shift_seconds = timeline_shift_seconds
        self.output_dir = output_dir
        self._cancel_requested = threading.Event()

    @Slot()
    def run(self) -> None:
        try:
            self.log.emit(f"Exporting {len(self.streams)} audio stream(s) to one .mka")
            plan = audio_sync.export_combined_audio_streams(
                self.source_path,
                self.streams,
                self.timeline_shift_seconds,
                self.output_dir,
                cancel_callback=self.cancel_requested,
            )
            self.log.emit(f"  wrote {plan.output_path}")
            self.completed.emit(plan)
        except audio_sync.AudioSyncCancelled:
            self.failed.emit("Audio sync export cancelled.")
        except Exception:
            self.failed.emit(traceback.format_exc())

    @Slot()
    def cancel(self) -> None:
        self._cancel_requested.set()

    def cancel_requested(self) -> bool:
        return self._cancel_requested.is_set()


class MainWindow(QMainWindow):
    FILE_COLUMNS = ["Status", "Input", "Output", "Message"]
    MAKEMKV_COLUMNS = ["Status", "Source", "Output", "Message"]
    AUDIO_SYNC_COLUMNS = ["Export", "Type", "Index", "Codec", "Language", "Title"]
    AUDIO_SYNC_MEDIA_SUFFIXES = {".mkv", ".mka", ".mp4", ".mov", ".avi", ".flac", ".wav", ".aac", ".ac3", ".dts"}
    AUDIO_SYNC_CUSTOM_PRESET = "custom"
    AUDIO_SYNC_DURATION_PRESETS = (
        ("60 s - Fast", 60.0),
        ("120 s - Balanced", 120.0),
        ("180 s - Robust", 180.0),
        ("300 s - Very robust / slow", 300.0),
        ("Custom...", AUDIO_SYNC_CUSTOM_PRESET),
    )
    AUDIO_SYNC_SPACING_PRESETS = (
        ("5 min - Close", 300.0),
        ("10 min", 600.0),
        ("15 min - Balanced", 900.0),
        ("30 min - Wide", 1800.0),
        ("Custom...", AUDIO_SYNC_CUSTOM_PRESET),
    )
    AUDIO_SYNC_CHECKPOINT_PRESETS = (
        ("1 - Fast check", 1),
        ("2 - Basic", 2),
        ("4 - Balanced", 4),
        ("6 - Robust", 6),
        ("8 - Slow", 8),
        ("Custom...", AUDIO_SYNC_CUSTOM_PRESET),
    )
    AUDIO_SYNC_SAMPLE_RATE = 16_000
    FINALIZATION_PROGRESS_UNITS = 10
    TRACK_COLUMNS = [
        "Include",
        "ID",
        "Source",
        "Type",
        "Codec",
        "Input lang",
        "Output lang",
        "Name",
        "Default",
        "Forced",
        "Drop",
        "Role",
        "Delay",
        "Reason",
    ]
    STATUS_COLORS_BY_THEME = {
        "light": {
            "Ready": ("#edf7ed", "#1f6f3f"),
            "Queued": ("#fff7df", "#8a5a00"),
            "Running": ("#eaf2ff", "#1d4ed8"),
            "Done": ("#e7f7ee", "#166534"),
            "Error": ("#fdecec", "#b42318"),
            "Cancelled": ("#f1f5f9", "#475569"),
            "dry-run": ("#eef6ff", "#0369a1"),
            "processed": ("#e7f7ee", "#166534"),
            "metadata-edited": ("#e7f7ee", "#166534"),
            "unchanged": ("#f1f5f9", "#475569"),
            "skipped": ("#fff7df", "#8a5a00"),
            "error": ("#fdecec", "#b42318"),
            "cancelled": ("#f1f5f9", "#475569"),
            "ready": ("#edf7ed", "#1f6f3f"),
        },
        "dark": {
            "Ready": ("#153223", "#9ae6b4"),
            "Queued": ("#3a2d13", "#f6d365"),
            "Running": ("#173153", "#93c5fd"),
            "Done": ("#123524", "#86efac"),
            "Error": ("#4a1d21", "#fca5a5"),
            "Cancelled": ("#293241", "#cbd5e1"),
            "dry-run": ("#15354a", "#7dd3fc"),
            "processed": ("#123524", "#86efac"),
            "metadata-edited": ("#123524", "#86efac"),
            "unchanged": ("#293241", "#cbd5e1"),
            "skipped": ("#3a2d13", "#f6d365"),
            "error": ("#4a1d21", "#fca5a5"),
            "cancelled": ("#293241", "#cbd5e1"),
            "ready": ("#153223", "#9ae6b4"),
        },
    }
    AUDIO_NAME_STYLE_HELP = {
        "auto": (
            "Uses format-only names when the file has one audio language, "
            "and adds the language when multiple audio languages are present."
        ),
        "format": "Names audio tracks by codec, channels, and role. Example: DTS-HD MA 5.1.",
        "language-format": "Adds the language before the format. Example: English - DTS-HD MA 5.1.",
        "keep": "Keeps the existing audio track names from the input file.",
    }
    LANGUAGE_ORDER_STYLE_HELP = {
        "default": "Uses the existing organizer order rules.",
        "regional": "Groups languages by broad regions, with Europe before Americas and Asia.",
    }
    REGIONAL_ORDER_HELP = {
        "europe,americas,asia,oceania,middle-east-africa": "Europe, then Americas, Asia, Oceania, Middle East/Africa.",
        "americas,europe,asia,oceania,middle-east-africa": "Americas, then Europe, Asia, Oceania, Middle East/Africa.",
        "asia,europe,americas,oceania,middle-east-africa": "Asia, then Europe, Americas, Oceania, Middle East/Africa.",
        "oceania,europe,americas,asia,middle-east-africa": "Oceania, then Europe, Americas, Asia, Middle East/Africa.",
        "middle-east-africa,europe,americas,asia,oceania": "Middle East/Africa, then Europe, Americas, Asia, Oceania.",
    }
    MAKEMKV_SELECTION_HELP = {
        "english": "Keeps video, English audio, subtitles, and attachments.",
        "all-audio": "Keeps video, every audio language, subtitles, and attachments.",
        "all-tracks": "Keeps all tracks selected by MakeMKV, except HD audio core tracks.",
        "custom": "Uses the exact MakeMKV selection rule written in the custom rule field.",
    }
    METADATA_MODE_HELP = {
        "off": "Always writes a new remuxed output file when changes are needed.",
        "auto": (
            "Uses mkvpropedit for metadata-only changes when possible; "
            "remuxes only when track removal or ordering requires it."
        ),
        "only": "Only allows metadata-only edits. If a remux is required, the file stops with an error.",
    }

    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("MKV Track Organizer")
        self.resize(1240, 820)
        self.setAcceptDrops(True)

        self.worker_thread: QThread | None = None
        self.worker: OrganizerWorker | None = None
        self.makemkv_worker_thread: QThread | None = None
        self.makemkv_worker: MakeMkvWorker | None = None
        self.audio_sync_worker_thread: QThread | None = None
        self.audio_sync_worker: AudioSyncWorker | AudioSyncExportWorker | None = None
        self.default_args, self.default_config_path = self._load_default_args()
        self.input_paths: list[Path] = []
        self.current_reports: list[dict] = []
        self.makemkv_reports: list[dict] = []
        self.audio_sync_reference_streams: list[audio_sync.MediaStream] = []
        self.audio_sync_source_streams: list[audio_sync.MediaStream] = []
        self.audio_sync_result: audio_sync.AudioSyncResult | None = None
        self.current_theme = "dark"
        self._syncing_input_edit = False
        self._syncing_track_checks = False
        self.manual_track_includes: dict[str, bool] = {}
        self.manual_track_order: list[str] = []

        self.input_edit = QLineEdit()
        self.input_edit.setPlaceholderText("Selected source")
        self.output_edit = QLineEdit()
        self.output_edit.setPlaceholderText("Default: _sorted next to the source")
        self.suffix_edit = QLineEdit()
        self.subtitle_language_edit = QLineEdit()
        self.subtitle_language_edit.setPlaceholderText("spa:7,8; fr-CA:9")
        self.forced_ids_edit = QLineEdit()
        self.forced_ids_edit.setPlaceholderText("5,8,12")
        self.audio_delays_edit = QLineEdit()
        self.audio_delays_edit.setPlaceholderText("1:150, 2:-250")
        self.subtitle_delays_edit = QLineEdit()
        self.subtitle_delays_edit.setPlaceholderText("5:-250")
        self.preferred_language_edit = QLineEdit()
        self.preferred_language_edit.setPlaceholderText("pt-PT")

        self.recursive_check = QCheckBox("Recursive")
        self.merge_inputs_check = QCheckBox("Merge selected sources")
        self.smart_subs_check = QCheckBox("Smart subtitle detection")
        self.drop_empty_check = QCheckBox("Drop empty subtitles")
        self.duplicate_check = QCheckBox("Detect duplicates")
        self.variant_check = QCheckBox("Detect language variants")
        self.auto_pgs_ocr_check = QCheckBox("Auto PGS OCR")
        self.auto_commentary_ocr_check = QCheckBox("Commentary/SDH OCR")
        self.report_check = QCheckBox("Write report")
        self.preferred_audio_first_check = QCheckBox("Audio first")
        self.preferred_audio_default_check = QCheckBox("Audio default")
        self.preferred_subtitle_first_check = QCheckBox("Subtitles first")
        self.preferred_forced_subtitle_default_check = QCheckBox("Forced subs default")

        self.metadata_combo = QComboBox()
        self.metadata_combo.addItems(["off", "auto", "only"])
        self.audio_name_style_combo = QComboBox()
        self.audio_name_style_combo.addItem("Auto", "auto")
        self.audio_name_style_combo.addItem("Format only", "format")
        self.audio_name_style_combo.addItem("Language + format", "language-format")
        self.audio_name_style_combo.addItem("Keep existing", "keep")
        self.language_order_style_combo = QComboBox()
        self.language_order_style_combo.addItem("Default", "default")
        self.language_order_style_combo.addItem("Regional", "regional")
        self.regional_order_combo = QComboBox()
        self.regional_order_combo.addItem("Europe first", "europe,americas,asia,oceania,middle-east-africa")
        self.regional_order_combo.addItem("Americas first", "americas,europe,asia,oceania,middle-east-africa")
        self.regional_order_combo.addItem("Asia first", "asia,europe,americas,oceania,middle-east-africa")
        self.regional_order_combo.addItem("Oceania first", "oceania,europe,americas,asia,middle-east-africa")
        self.regional_order_combo.addItem(
            "Middle East/Africa first",
            "middle-east-africa,europe,americas,asia,oceania",
        )
        self.report_format_combo = QComboBox()
        self.report_format_combo.addItems(["both", "json", "txt"])

        self.advanced_button = QToolButton()
        self.advanced_panel = QWidget()
        self.check_tools_button = QPushButton("Check tools")
        self.preview_button = QPushButton("Preview")
        self.run_button = QPushButton("Run")
        self.cancel_button = QPushButton("Cancel")
        self.track_select_all_button = QPushButton("Select all")
        self.track_deselect_duplicates_button = QPushButton("Deselect duplicates")
        self.organizer_clear_button: QToolButton | None = None
        self.organizer_reset_button: QToolButton | None = None
        self.check_tools_button.setObjectName("secondaryButton")
        self.preview_button.setObjectName("secondaryButton")
        self.run_button.setObjectName("primaryButton")
        self.cancel_button.setObjectName("dangerButton")
        self.track_select_all_button.setObjectName("secondaryButton")
        self.track_deselect_duplicates_button.setObjectName("secondaryButton")
        self.track_select_all_button.setToolTip("Include every displayed track")
        self.track_deselect_duplicates_button.setToolTip("Uncheck duplicate-group members and keep each group leader")
        self.track_select_all_button.setEnabled(False)
        self.track_deselect_duplicates_button.setEnabled(False)
        self.files_table = QTableWidget(0, len(self.FILE_COLUMNS))
        self.results_table = self.files_table
        self.tracks_table = TrackTableWidget(0, len(self.TRACK_COLUMNS))
        self.summary_edit = QPlainTextEdit()
        self.log_edit = QPlainTextEdit()
        self.output_tabs = QTabWidget()
        self.theme_combo = QComboBox()
        self.theme_combo.addItem("Dark", "dark")
        self.theme_combo.addItem("Light", "light")
        self.theme_combo.setToolTip("Choose UI theme")
        self.progress_label = QLabel("Idle")
        self.progress = QProgressBar()

        self.makemkv_path_edit = QLineEdit()
        self.makemkv_path_edit.setPlaceholderText(r"C:\Program Files (x86)\MakeMKV\makemkvcon64.exe")
        try:
            self.makemkv_path_edit.setText(str(makemkv.find_makemkv()))
        except makemkv.MakeMkvError:
            pass
        self.makemkv_source_edit = QLineEdit()
        self.makemkv_source_edit.setPlaceholderText("Folder with MakeMKV disc backups")
        self.makemkv_output_edit = QLineEdit()
        self.makemkv_output_edit.setPlaceholderText("Folder for MakeMKV MKV outputs")
        self.makemkv_min_length_spin = QSpinBox()
        self.makemkv_min_length_spin.setRange(0, 24 * 60 * 60)
        self.makemkv_min_length_spin.setValue(makemkv.DEFAULT_MIN_LENGTH_SECONDS)
        self.makemkv_min_length_spin.setSuffix(" s")
        self.makemkv_selection_combo = QComboBox()
        self.makemkv_selection_combo.addItem("English audio", "english")
        self.makemkv_selection_combo.addItem("All audio", "all-audio")
        self.makemkv_selection_combo.addItem("All tracks", "all-tracks")
        self.makemkv_selection_combo.addItem("Custom", "custom")
        self.makemkv_custom_rule_edit = QLineEdit()
        self.makemkv_custom_rule_edit.setPlaceholderText("-sel:all,+sel:video,+sel:audio,+sel:subtitle,+sel:attachment")
        self.makemkv_pipeline_check = QCheckBox("Run Organizer after MakeMKV")
        self.makemkv_check_button = QPushButton("Check tools")
        self.makemkv_preview_button = QPushButton("Preview")
        self.makemkv_run_button = QPushButton("Run")
        self.makemkv_clear_button: QToolButton | None = None
        self.makemkv_reset_button: QToolButton | None = None
        self.makemkv_cancel_button = QPushButton("Cancel")
        self.makemkv_check_button.setObjectName("secondaryButton")
        self.makemkv_preview_button.setObjectName("secondaryButton")
        self.makemkv_run_button.setObjectName("primaryButton")
        self.makemkv_cancel_button.setObjectName("dangerButton")
        self.makemkv_table = QTableWidget(0, len(self.MAKEMKV_COLUMNS))
        self.makemkv_summary_edit = QPlainTextEdit()
        self.makemkv_log_edit = QPlainTextEdit()
        self.makemkv_output_tabs = QTabWidget()

        self.audio_sync_reference_edit = QLineEdit()
        self.audio_sync_reference_edit.setPlaceholderText("Reference file already synced to the target video")
        self.audio_sync_source_edit = QLineEdit()
        self.audio_sync_source_edit.setPlaceholderText("Source file whose tracks will be aligned")
        self.audio_sync_output_edit = QLineEdit()
        self.audio_sync_output_edit.setPlaceholderText("Default: synced next to the source")
        self.audio_sync_ref_combo = QComboBox()
        self.audio_sync_source_combo = QComboBox()
        self.audio_sync_start_edit = QLineEdit("00:10:00")
        self.audio_sync_duration_combo = QComboBox()
        for label, seconds in self.AUDIO_SYNC_DURATION_PRESETS:
            self.audio_sync_duration_combo.addItem(label, seconds)
        self.audio_sync_duration_combo.setCurrentIndex(1)
        self.audio_sync_spacing_combo = QComboBox()
        for label, seconds in self.AUDIO_SYNC_SPACING_PRESETS:
            self.audio_sync_spacing_combo.addItem(label, seconds)
        self.audio_sync_spacing_combo.setCurrentIndex(2)
        self.audio_sync_custom_duration_seconds = 120.0
        self.audio_sync_custom_spacing_seconds = 900.0
        self.audio_sync_previous_duration_index = self.audio_sync_duration_combo.currentIndex()
        self.audio_sync_previous_spacing_index = self.audio_sync_spacing_combo.currentIndex()
        self._audio_sync_preset_prompt_active = False
        self.audio_sync_max_offset_edit = QLineEdit("5")
        self.audio_sync_checkpoints_combo = QComboBox()
        for label, checkpoints in self.AUDIO_SYNC_CHECKPOINT_PRESETS:
            self.audio_sync_checkpoints_combo.addItem(label, checkpoints)
        self.audio_sync_checkpoints_combo.setCurrentIndex(2)
        self.audio_sync_custom_checkpoints = 4
        self.audio_sync_previous_checkpoints_index = self.audio_sync_checkpoints_combo.currentIndex()
        self.audio_sync_check_button = QPushButton("Check tools")
        self.audio_sync_load_button = QPushButton("Load streams")
        self.audio_sync_analyze_button = QPushButton("Analyze")
        self.audio_sync_apply_organizer_button = QPushButton("Apply delay in Organizer")
        self.audio_sync_export_button = QPushButton("Export shifted .mka")
        self.audio_sync_select_all_button = QPushButton("Select all")
        self.audio_sync_clear_selection_button = QPushButton("Clear selection")
        self.audio_sync_clear_button: QToolButton | None = None
        self.audio_sync_reset_button: QToolButton | None = None
        self.audio_sync_cancel_button = QPushButton("Cancel")
        self.audio_sync_check_button.setObjectName("secondaryButton")
        self.audio_sync_load_button.setObjectName("secondaryButton")
        self.audio_sync_analyze_button.setObjectName("primaryButton")
        self.audio_sync_apply_organizer_button.setObjectName("secondaryButton")
        self.audio_sync_export_button.setObjectName("secondaryButton")
        self.audio_sync_select_all_button.setObjectName("secondaryButton")
        self.audio_sync_clear_selection_button.setObjectName("secondaryButton")
        self.audio_sync_cancel_button.setObjectName("dangerButton")
        self.audio_sync_tracks_table = QTableWidget(0, len(self.AUDIO_SYNC_COLUMNS))
        self.audio_sync_summary_edit = QPlainTextEdit()
        self.audio_sync_log_edit = QPlainTextEdit()
        self.audio_sync_output_tabs = QTabWidget()

        self._build_ui()
        self._apply_theme()
        self._apply_default_args(self.default_args)
        self._connect_signals()
        self._refresh_file_list()

    def _build_ui(self) -> None:
        style = self.style()
        self.tabs = QTabWidget()
        organizer_tab = QWidget()
        organizer_tab.setAcceptDrops(True)
        organizer_tab.installEventFilter(self)
        root = QVBoxLayout(organizer_tab)
        root.setContentsMargins(12, 12, 12, 10)
        root.setSpacing(10)

        source_group = QGroupBox("Source and output")
        source_group.setAcceptDrops(True)
        source_group.installEventFilter(self)
        source_grid = QGridLayout(source_group)
        source_grid.setColumnStretch(1, 1)

        input_row = QHBoxLayout()
        input_label = QLabel("Input")
        file_button = self._tool_button(QStyle.SP_FileIcon, "Choose Matroska files")
        folder_button = self._tool_button(QStyle.SP_DirOpenIcon, "Choose folder")
        self.organizer_clear_button = self._tool_button(QStyle.SP_DialogResetButton, "Clear Organizer inputs")
        self.organizer_reset_button = self._tool_button(QStyle.SP_BrowserReload, "Reset Organizer tab")
        input_row.addWidget(self.input_edit, 1)
        input_row.addWidget(file_button)
        input_row.addWidget(folder_button)
        input_row.addWidget(self.organizer_clear_button)
        input_row.addWidget(self.organizer_reset_button)

        browse_output = self._tool_button(QStyle.SP_DirOpenIcon, "Choose output folder")
        output_row = QHBoxLayout()
        output_row.addWidget(self.output_edit, 1)
        output_row.addWidget(browse_output)

        self.recursive_check.setToolTip("When the input is a folder, include .mkv and .mka files inside subfolders")
        source_grid.addWidget(input_label, 0, 0)
        source_grid.addLayout(input_row, 0, 1)
        source_grid.addWidget(QLabel("Output"), 1, 0)
        source_grid.addLayout(output_row, 1, 1)
        source_grid.addWidget(self.recursive_check, 2, 1)
        root.addWidget(source_group)

        top_bar = QHBoxLayout()
        self.advanced_button.setText("Advanced")
        self.advanced_button.setCheckable(True)
        self.advanced_button.setChecked(False)
        self.advanced_button.setToolButtonStyle(Qt.ToolButtonTextBesideIcon)
        self.advanced_button.setArrowType(Qt.RightArrow)
        self.check_tools_button.setIcon(style.standardIcon(QStyle.SP_DialogApplyButton))
        self.check_tools_button.setToolTip("Validate input paths and required external tools")
        self.preview_button.setIcon(style.standardIcon(QStyle.SP_FileDialogContentsView))
        self.preview_button.setToolTip("Analyze with dry-run enabled")
        self.run_button.setIcon(style.standardIcon(QStyle.SP_MediaPlay))
        self.run_button.setToolTip("Run with the selected settings")
        self.cancel_button.setIcon(style.standardIcon(QStyle.SP_BrowserStop))
        self.cancel_button.setToolTip("Cancel the current run")
        self.cancel_button.setEnabled(False)
        top_bar.addWidget(self.advanced_button)
        top_bar.addStretch(1)
        top_bar.addWidget(self.check_tools_button)
        top_bar.addWidget(self.preview_button)
        top_bar.addWidget(self.run_button)
        top_bar.addWidget(self.cancel_button)
        root.addLayout(top_bar)

        advanced_layout = QGridLayout(self.advanced_panel)
        advanced_layout.setContentsMargins(18, 0, 0, 0)
        advanced_layout.setColumnStretch(1, 1)
        advanced_layout.setColumnStretch(3, 1)

        self.suffix_edit.setToolTip("Optional suffix before the extension, for example movie.fixed.mkv")
        self._apply_combo_help(self.metadata_combo, self.METADATA_MODE_HELP)
        self._apply_combo_help(self.audio_name_style_combo, self.AUDIO_NAME_STYLE_HELP)
        self._apply_combo_help(self.language_order_style_combo, self.LANGUAGE_ORDER_STYLE_HELP)
        self._apply_combo_help(self.regional_order_combo, self.REGIONAL_ORDER_HELP)
        self.subtitle_language_edit.setToolTip("Manual language override, for example spa:7,8; fr-CA:9")
        self.forced_ids_edit.setToolTip("Manual forced-subtitle override, for example 5,8,12")
        self.audio_delays_edit.setToolTip("Manual audio delays in milliseconds. Example: 1:150, 2:-250")
        self.subtitle_delays_edit.setToolTip("Manual subtitle delays in milliseconds. Example: 5:-250")
        self.preferred_language_edit.setToolTip("Language code used by the optional preferred-language rules, for example pt-PT")
        self.merge_inputs_check.setToolTip(
            "Mux selected Matroska inputs into one output. The first source with video supplies video; audio/subtitles come from all sources."
        )
        self.smart_subs_check.setToolTip("Automatically classify forced, empty, commentary, and SDH subtitles")
        self.drop_empty_check.setToolTip("Exclude subtitles classified as empty")
        self.duplicate_check.setToolTip("Highlight likely duplicate audio/subtitle tracks without dropping them")
        self.variant_check.setToolTip("Automatically detect language variants such as es-ES vs es-419")
        self.auto_pgs_ocr_check.setToolTip("Run OCR for PGS subtitles when needed for language detection")
        self.auto_commentary_ocr_check.setToolTip("OCR extra full-size PGS tracks that may be commentary or SDH; normal and named SDH tracks are skipped")
        self.report_check.setToolTip("Write TXT/JSON batch reports")
        self.preferred_audio_first_check.setToolTip("Move preferred-language main audio before other main audio")
        self.preferred_audio_default_check.setToolTip("Make preferred-language audio default when available")
        self.preferred_subtitle_first_check.setToolTip(
            "Move preferred-language normal subtitles before other normal subtitles"
        )
        self.preferred_forced_subtitle_default_check.setToolTip(
            "Move preferred-language forced subtitles before other subtitles and make the first one default"
        )

        metadata_label = QLabel("Metadata mode")
        metadata_label.setToolTip(
            "Controls whether the app can update track metadata directly with mkvpropedit instead of remuxing."
        )
        audio_names_label = QLabel("Audio names")
        audio_names_label.setToolTip("Controls how audio track names are written.")
        language_order_label = QLabel("Language order")
        language_order_label.setToolTip("Controls how languages are sorted in the output.")
        regional_order_label = QLabel("Region order")
        regional_order_label.setToolTip("Controls region priority when Language order is Regional.")
        preferred_language_label = QLabel("Preferred language")
        preferred_language_label.setToolTip("Optional language code used by preferred-language rules.")

        advanced_layout.addWidget(QLabel("Output suffix"), 0, 0)
        advanced_layout.addWidget(self.suffix_edit, 0, 1)
        advanced_layout.addWidget(metadata_label, 0, 2)
        advanced_layout.addWidget(self.metadata_combo, 0, 3)
        advanced_layout.addWidget(audio_names_label, 1, 0)
        advanced_layout.addWidget(self.audio_name_style_combo, 1, 1)
        advanced_layout.addWidget(QLabel("Report format"), 1, 2)
        advanced_layout.addWidget(self.report_format_combo, 1, 3)
        advanced_layout.addWidget(language_order_label, 2, 0)
        advanced_layout.addWidget(self.language_order_style_combo, 2, 1)
        advanced_layout.addWidget(regional_order_label, 2, 2)
        advanced_layout.addWidget(self.regional_order_combo, 2, 3)
        advanced_layout.addWidget(QLabel("Language overrides"), 3, 0)
        advanced_layout.addWidget(self.subtitle_language_edit, 3, 1)
        advanced_layout.addWidget(QLabel("Forced IDs"), 3, 2)
        advanced_layout.addWidget(self.forced_ids_edit, 3, 3)
        advanced_layout.addWidget(QLabel("Audio delays"), 4, 0)
        advanced_layout.addWidget(self.audio_delays_edit, 4, 1)
        advanced_layout.addWidget(QLabel("Subtitle delays"), 4, 2)
        advanced_layout.addWidget(self.subtitle_delays_edit, 4, 3)
        advanced_layout.addWidget(preferred_language_label, 5, 0)
        advanced_layout.addWidget(self.preferred_language_edit, 5, 1)

        preferred_toggles = QHBoxLayout()
        for checkbox in [
            self.preferred_audio_first_check,
            self.preferred_audio_default_check,
            self.preferred_subtitle_first_check,
            self.preferred_forced_subtitle_default_check,
        ]:
            preferred_toggles.addWidget(checkbox)
        preferred_toggles.addStretch(1)
        advanced_layout.addLayout(preferred_toggles, 5, 2, 1, 2)

        advanced_toggles = QHBoxLayout()
        for checkbox in [
            self.merge_inputs_check,
            self.smart_subs_check,
            self.drop_empty_check,
            self.duplicate_check,
            self.variant_check,
            self.auto_pgs_ocr_check,
            self.auto_commentary_ocr_check,
            self.report_check,
        ]:
            advanced_toggles.addWidget(checkbox)
        advanced_toggles.addStretch(1)
        advanced_layout.addLayout(advanced_toggles, 6, 0, 1, 4)
        self.advanced_panel.setVisible(False)
        root.addWidget(self.advanced_panel)

        files_group = QGroupBox("Files")
        files_layout = QVBoxLayout(files_group)
        files_layout.setContentsMargins(8, 8, 8, 8)
        self.files_table.setHorizontalHeaderLabels(self.FILE_COLUMNS)
        self.files_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeToContents)
        self.files_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.Stretch)
        self.files_table.horizontalHeader().setSectionResizeMode(2, QHeaderView.Stretch)
        self.files_table.horizontalHeader().setStretchLastSection(True)
        self.files_table.setAlternatingRowColors(True)
        self.files_table.setSelectionBehavior(QTableWidget.SelectRows)
        self.files_table.setSelectionMode(QTableWidget.SingleSelection)
        self.files_table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.files_table.verticalHeader().setVisible(False)
        self.files_table.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.files_table.setAcceptDrops(True)
        self.files_table.installEventFilter(self)
        files_layout.addWidget(self.files_table)

        tracks_group = QGroupBox("Tracks")
        tracks_layout = QVBoxLayout(tracks_group)
        tracks_layout.setContentsMargins(8, 8, 8, 8)
        tracks_toolbar = QHBoxLayout()
        tracks_toolbar.addWidget(self.track_select_all_button)
        tracks_toolbar.addWidget(self.track_deselect_duplicates_button)
        tracks_toolbar.addStretch(1)
        tracks_layout.addLayout(tracks_toolbar)
        self.tracks_table.setHorizontalHeaderLabels(self.TRACK_COLUMNS)
        self.tracks_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeToContents)
        self.tracks_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeToContents)
        self.tracks_table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeToContents)
        self.tracks_table.horizontalHeader().setSectionResizeMode(7, QHeaderView.Stretch)
        self.tracks_table.horizontalHeader().setStretchLastSection(True)
        self.tracks_table.setAlternatingRowColors(True)
        self.tracks_table.setSelectionBehavior(QTableWidget.SelectRows)
        self.tracks_table.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.tracks_table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.tracks_table.setDragEnabled(True)
        self.tracks_table.setAcceptDrops(True)
        self.tracks_table.setDragDropMode(QAbstractItemView.InternalMove)
        self.tracks_table.setDefaultDropAction(Qt.MoveAction)
        self.tracks_table.setDragDropOverwriteMode(False)
        self.tracks_table.setDropIndicatorShown(True)
        self.tracks_table.setToolTip("Drag rows to change the remux track order")
        self.tracks_table.verticalHeader().setVisible(False)
        self.tracks_table.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.tracks_table.installEventFilter(self)
        tracks_layout.addWidget(self.tracks_table)

        for edit in [self.summary_edit, self.log_edit]:
            edit.setReadOnly(True)
            edit.setLineWrapMode(QPlainTextEdit.NoWrap)
            edit.setAcceptDrops(True)
            edit.installEventFilter(self)
        self.output_tabs.addTab(self.summary_edit, "Summary")
        self.output_tabs.addTab(self.log_edit, "Raw log")

        work_splitter = QSplitter(Qt.Horizontal)
        work_splitter.addWidget(files_group)
        work_splitter.addWidget(tracks_group)
        work_splitter.setSizes([430, 790])

        splitter = QSplitter(Qt.Vertical)
        splitter.addWidget(work_splitter)
        splitter.addWidget(self.output_tabs)
        splitter.setSizes([500, 220])
        root.addWidget(splitter, 1)

        self.progress.setRange(0, 1)
        self.progress.setValue(0)
        self.progress_label.setMinimumWidth(220)
        self.theme_combo.setFixedWidth(92)
        self.statusBar().addPermanentWidget(self.theme_combo)
        self.statusBar().addPermanentWidget(self.progress_label)
        self.statusBar().addPermanentWidget(self.progress, 1)
        self.tabs.addTab(organizer_tab, style.standardIcon(QStyle.SP_FileIcon), "Organizer")
        self.tabs.addTab(self._build_audio_sync_tab(), style.standardIcon(QStyle.SP_MediaSeekForward), "Audio Sync")
        self.tabs.addTab(self._build_makemkv_tab(), style.standardIcon(QStyle.SP_DirOpenIcon), "MakeMKV Batch")
        self.setCentralWidget(self.tabs)

        file_button.clicked.connect(self.choose_file)
        folder_button.clicked.connect(self.choose_folder)
        if self.organizer_clear_button:
            self.organizer_clear_button.clicked.connect(self.clear_inputs)
        if self.organizer_reset_button:
            self.organizer_reset_button.clicked.connect(self.reset_organizer_tab)
        browse_output.clicked.connect(self.choose_output_folder)
        self.advanced_button.toggled.connect(self.toggle_advanced)

    def _build_makemkv_tab(self) -> QWidget:
        style = self.style()
        tab = QWidget()
        root = QVBoxLayout(tab)
        root.setContentsMargins(12, 12, 12, 10)
        root.setSpacing(10)

        source_group = QGroupBox("MakeMKV source and output")
        source_grid = QGridLayout(source_group)
        source_grid.setColumnStretch(1, 1)

        makemkv_button = self._tool_button(QStyle.SP_FileIcon, "Choose makemkvcon64.exe")
        source_button = self._tool_button(QStyle.SP_DirOpenIcon, "Choose source folder")
        output_button = self._tool_button(QStyle.SP_DirOpenIcon, "Choose output folder")
        self.makemkv_clear_button = self._tool_button(QStyle.SP_DialogResetButton, "Clear MakeMKV input folder")
        self.makemkv_reset_button = self._tool_button(QStyle.SP_BrowserReload, "Reset MakeMKV tab")

        makemkv_row = QHBoxLayout()
        makemkv_row.addWidget(self.makemkv_path_edit, 1)
        makemkv_row.addWidget(makemkv_button)
        source_row = QHBoxLayout()
        source_row.addWidget(self.makemkv_source_edit, 1)
        source_row.addWidget(source_button)
        source_row.addWidget(self.makemkv_clear_button)
        source_row.addWidget(self.makemkv_reset_button)
        output_row = QHBoxLayout()
        output_row.addWidget(self.makemkv_output_edit, 1)
        output_row.addWidget(output_button)

        source_grid.addWidget(QLabel("MakeMKV"), 0, 0)
        source_grid.addLayout(makemkv_row, 0, 1)
        source_grid.addWidget(QLabel("Input"), 1, 0)
        source_grid.addLayout(source_row, 1, 1)
        source_grid.addWidget(QLabel("Output"), 2, 0)
        source_grid.addLayout(output_row, 2, 1)
        root.addWidget(source_group)

        options_group = QGroupBox("Batch options")
        options_grid = QGridLayout(options_group)
        options_grid.setColumnStretch(1, 1)
        options_grid.setColumnStretch(3, 1)

        self._apply_combo_help(self.makemkv_selection_combo, self.MAKEMKV_SELECTION_HELP)
        self.makemkv_min_length_spin.setToolTip("Skip titles shorter than this duration.")
        self.makemkv_custom_rule_edit.setToolTip("Advanced MakeMKV default selection string.")
        self.makemkv_pipeline_check.setToolTip(
            "After MakeMKV finishes, run the Organizer on the MakeMKV output folder using the Organizer tab settings."
        )

        options_grid.addWidget(QLabel("Min length"), 0, 0)
        options_grid.addWidget(self.makemkv_min_length_spin, 0, 1)
        options_grid.addWidget(QLabel("Selection"), 0, 2)
        options_grid.addWidget(self.makemkv_selection_combo, 0, 3)
        options_grid.addWidget(QLabel("Custom rule"), 1, 0)
        options_grid.addWidget(self.makemkv_custom_rule_edit, 1, 1, 1, 3)
        options_grid.addWidget(self.makemkv_pipeline_check, 2, 1, 1, 3)
        root.addWidget(options_group)

        top_bar = QHBoxLayout()
        self.makemkv_check_button.setIcon(style.standardIcon(QStyle.SP_DialogApplyButton))
        self.makemkv_check_button.setToolTip("Validate MakeMKV, source folders, output folder, and selection rule")
        self.makemkv_preview_button.setIcon(style.standardIcon(QStyle.SP_FileDialogContentsView))
        self.makemkv_preview_button.setToolTip("Show planned MakeMKV commands without writing outputs")
        self.makemkv_run_button.setIcon(style.standardIcon(QStyle.SP_MediaPlay))
        self.makemkv_run_button.setToolTip("Run MakeMKV with the selected settings")
        self.makemkv_cancel_button.setIcon(style.standardIcon(QStyle.SP_BrowserStop))
        self.makemkv_cancel_button.setToolTip("Cancel the current MakeMKV batch")
        self.makemkv_cancel_button.setEnabled(False)
        top_bar.addStretch(1)
        top_bar.addWidget(self.makemkv_check_button)
        top_bar.addWidget(self.makemkv_preview_button)
        top_bar.addWidget(self.makemkv_run_button)
        top_bar.addWidget(self.makemkv_cancel_button)
        root.addLayout(top_bar)

        files_group = QGroupBox("Disc folders")
        files_layout = QVBoxLayout(files_group)
        files_layout.setContentsMargins(8, 8, 8, 8)
        self.makemkv_table.setHorizontalHeaderLabels(self.MAKEMKV_COLUMNS)
        self.makemkv_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeToContents)
        self.makemkv_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.Stretch)
        self.makemkv_table.horizontalHeader().setSectionResizeMode(2, QHeaderView.Stretch)
        self.makemkv_table.horizontalHeader().setStretchLastSection(True)
        self.makemkv_table.setAlternatingRowColors(True)
        self.makemkv_table.setSelectionBehavior(QTableWidget.SelectRows)
        self.makemkv_table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.makemkv_table.verticalHeader().setVisible(False)
        files_layout.addWidget(self.makemkv_table)

        for edit in [self.makemkv_summary_edit, self.makemkv_log_edit]:
            edit.setReadOnly(True)
            edit.setLineWrapMode(QPlainTextEdit.NoWrap)
        self.makemkv_output_tabs.addTab(self.makemkv_summary_edit, "Summary")
        self.makemkv_output_tabs.addTab(self.makemkv_log_edit, "Raw log")

        splitter = QSplitter(Qt.Vertical)
        splitter.addWidget(files_group)
        splitter.addWidget(self.makemkv_output_tabs)
        splitter.setSizes([500, 220])
        root.addWidget(splitter, 1)

        makemkv_button.clicked.connect(self.choose_makemkv_executable)
        source_button.clicked.connect(self.choose_makemkv_source_folder)
        output_button.clicked.connect(self.choose_makemkv_output_folder)
        self.makemkv_selection_combo.currentIndexChanged.connect(self._makemkv_selection_changed)
        self._makemkv_selection_changed()
        return tab

    def _build_audio_sync_tab(self) -> QWidget:
        style = self.style()
        tab = QWidget()
        root = QVBoxLayout(tab)
        root.setContentsMargins(12, 12, 12, 10)
        root.setSpacing(10)

        files_group = QGroupBox("Reference and source")
        files_grid = QGridLayout(files_group)
        files_grid.setColumnStretch(1, 1)
        for edit in [self.audio_sync_reference_edit, self.audio_sync_source_edit]:
            edit.setAcceptDrops(True)
            edit.installEventFilter(self)

        reference_button = self._tool_button(QStyle.SP_FileIcon, "Choose reference file")
        source_button = self._tool_button(QStyle.SP_FileIcon, "Choose source file")
        output_button = self._tool_button(QStyle.SP_DirOpenIcon, "Choose export folder")
        self.audio_sync_clear_button = self._tool_button(
            QStyle.SP_DialogResetButton,
            "Clear Audio Sync reference and source paths",
        )
        self.audio_sync_reset_button = self._tool_button(QStyle.SP_BrowserReload, "Reset Audio Sync tab")

        reference_row = QHBoxLayout()
        reference_row.addWidget(self.audio_sync_reference_edit, 1)
        reference_row.addWidget(reference_button)
        source_row = QHBoxLayout()
        source_row.addWidget(self.audio_sync_source_edit, 1)
        source_row.addWidget(source_button)
        source_row.addWidget(self.audio_sync_clear_button)
        source_row.addWidget(self.audio_sync_reset_button)
        output_row = QHBoxLayout()
        output_row.addWidget(self.audio_sync_output_edit, 1)
        output_row.addWidget(output_button)

        reference_label = QLabel("Reference")
        reference_label.setToolTip("Media already synced to the target timeline.")
        source_label = QLabel("Source")
        source_label.setToolTip("Media whose audio tracks need the measured delay.")
        export_label = QLabel("Export")
        export_label.setToolTip("Folder for the combined synced .mka output.")
        files_grid.addWidget(reference_label, 0, 0)
        files_grid.addLayout(reference_row, 0, 1)
        files_grid.addWidget(source_label, 1, 0)
        files_grid.addLayout(source_row, 1, 1)
        files_grid.addWidget(export_label, 2, 0)
        files_grid.addLayout(output_row, 2, 1)
        root.addWidget(files_group)

        compare_group = QGroupBox("Comparison")
        compare_grid = QGridLayout(compare_group)
        compare_grid.setColumnStretch(1, 1)
        compare_grid.setColumnStretch(3, 1)
        self.audio_sync_ref_combo.setToolTip("Reference audio stream, counted among audio streams")
        self.audio_sync_source_combo.setToolTip("Source audio stream to compare against the reference")
        self.audio_sync_start_edit.setToolTip("First timestamp used for analysis. Skip intros, logos, or long quiet sections when possible.")
        self.audio_sync_duration_combo.setToolTip("Longer windows are slower but more reliable. 120 seconds is a balanced default.")
        self.audio_sync_checkpoints_combo.setToolTip("Number of timeline positions checked. More checkpoints are slower but help confirm a fixed delay.")
        self.audio_sync_spacing_combo.setToolTip("Distance between checkpoints. Wider spacing checks whether the delay stays stable.")
        self.audio_sync_max_offset_edit.setToolTip("Largest delay to search for in either direction. Increase only when the source may be far out of sync.")
        reference_audio_label = QLabel("Reference audio")
        reference_audio_label.setToolTip("Audio stream from the already synced reference.")
        source_audio_label = QLabel("Source audio")
        source_audio_label.setToolTip("Audio stream from the source to compare with the reference.")
        start_label = QLabel("Start")
        start_label.setToolTip("First timestamp used for analysis.")
        duration_label = QLabel("Duration")
        duration_label.setToolTip("Amount of audio analyzed at each checkpoint.")
        checkpoints_label = QLabel("Checkpoints")
        checkpoints_label.setToolTip("Number of timeline positions checked.")
        spacing_label = QLabel("Spacing")
        spacing_label.setToolTip("Distance between checkpoint start times.")
        max_offset_label = QLabel("Max offset")
        max_offset_label.setToolTip("Largest delay to search for in either direction.")
        compare_grid.addWidget(reference_audio_label, 0, 0)
        compare_grid.addWidget(self.audio_sync_ref_combo, 0, 1)
        compare_grid.addWidget(source_audio_label, 0, 2)
        compare_grid.addWidget(self.audio_sync_source_combo, 0, 3)
        compare_grid.addWidget(start_label, 1, 0)
        compare_grid.addWidget(self.audio_sync_start_edit, 1, 1)
        compare_grid.addWidget(duration_label, 1, 2)
        compare_grid.addWidget(self.audio_sync_duration_combo, 1, 3)
        compare_grid.addWidget(checkpoints_label, 2, 0)
        compare_grid.addWidget(self.audio_sync_checkpoints_combo, 2, 1)
        compare_grid.addWidget(spacing_label, 2, 2)
        compare_grid.addWidget(self.audio_sync_spacing_combo, 2, 3)
        compare_grid.addWidget(max_offset_label, 3, 0)
        compare_grid.addWidget(self.audio_sync_max_offset_edit, 3, 1)
        root.addWidget(compare_group)

        self.audio_sync_check_button.setIcon(style.standardIcon(QStyle.SP_DialogApplyButton))
        self.audio_sync_load_button.setIcon(style.standardIcon(QStyle.SP_BrowserReload))
        self.audio_sync_analyze_button.setIcon(style.standardIcon(QStyle.SP_MediaPlay))
        self.audio_sync_apply_organizer_button.setIcon(style.standardIcon(QStyle.SP_DialogApplyButton))
        self.audio_sync_apply_organizer_button.setToolTip(
            "Fill the Organizer input and audio delay fields; Organizer remux applies the delay with mkvmerge --sync."
        )
        self.audio_sync_apply_organizer_button.setEnabled(False)
        self.audio_sync_export_button.setIcon(style.standardIcon(QStyle.SP_DialogSaveButton))
        self.audio_sync_export_button.setToolTip(
            "Create a separate .mka whose selected audio tracks are shifted by the measured delay."
        )
        self.audio_sync_export_button.setEnabled(False)
        self.audio_sync_select_all_button.setIcon(style.standardIcon(QStyle.SP_DialogApplyButton))
        self.audio_sync_select_all_button.setToolTip("Select every loaded source audio track for .mka export")
        self.audio_sync_select_all_button.setEnabled(False)
        self.audio_sync_clear_selection_button.setIcon(style.standardIcon(QStyle.SP_DialogResetButton))
        self.audio_sync_clear_selection_button.setToolTip("Uncheck every loaded source audio track")
        self.audio_sync_clear_selection_button.setEnabled(False)
        self.audio_sync_cancel_button.setIcon(style.standardIcon(QStyle.SP_BrowserStop))
        self.audio_sync_cancel_button.setEnabled(False)

        top_bar = QHBoxLayout()
        top_bar.addStretch(1)
        top_bar.addWidget(self.audio_sync_check_button)
        top_bar.addWidget(self.audio_sync_load_button)
        top_bar.addWidget(self.audio_sync_analyze_button)
        top_bar.addWidget(self.audio_sync_apply_organizer_button)
        top_bar.addWidget(self.audio_sync_export_button)
        top_bar.addWidget(self.audio_sync_cancel_button)
        root.addLayout(top_bar)

        streams_group = QGroupBox("Source audio to export")
        streams_layout = QVBoxLayout(streams_group)
        streams_layout.setContentsMargins(8, 8, 8, 8)
        streams_bar = QHBoxLayout()
        streams_bar.addStretch(1)
        streams_bar.addWidget(self.audio_sync_select_all_button)
        streams_bar.addWidget(self.audio_sync_clear_selection_button)
        streams_layout.addLayout(streams_bar)
        self.audio_sync_tracks_table.setHorizontalHeaderLabels(self.AUDIO_SYNC_COLUMNS)
        self.audio_sync_tracks_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeToContents)
        self.audio_sync_tracks_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeToContents)
        self.audio_sync_tracks_table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeToContents)
        self.audio_sync_tracks_table.horizontalHeader().setSectionResizeMode(3, QHeaderView.ResizeToContents)
        self.audio_sync_tracks_table.horizontalHeader().setStretchLastSection(True)
        self.audio_sync_tracks_table.setAlternatingRowColors(True)
        self.audio_sync_tracks_table.setSelectionBehavior(QTableWidget.SelectRows)
        self.audio_sync_tracks_table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.audio_sync_tracks_table.verticalHeader().setVisible(False)
        streams_layout.addWidget(self.audio_sync_tracks_table)

        for edit in [self.audio_sync_summary_edit, self.audio_sync_log_edit]:
            edit.setReadOnly(True)
            edit.setLineWrapMode(QPlainTextEdit.NoWrap)
        self.audio_sync_output_tabs.addTab(self.audio_sync_summary_edit, "Summary")
        self.audio_sync_output_tabs.addTab(self.audio_sync_log_edit, "Raw log")

        splitter = QSplitter(Qt.Vertical)
        splitter.addWidget(streams_group)
        splitter.addWidget(self.audio_sync_output_tabs)
        splitter.setSizes([460, 260])
        root.addWidget(splitter, 1)

        reference_button.clicked.connect(self.choose_audio_sync_reference_file)
        source_button.clicked.connect(self.choose_audio_sync_source_file)
        output_button.clicked.connect(self.choose_audio_sync_output_folder)
        return tab

    def _connect_signals(self) -> None:
        self.check_tools_button.clicked.connect(self.check_organizer_tools)
        self.preview_button.clicked.connect(self.start_preview)
        self.run_button.clicked.connect(self.start_run)
        self.cancel_button.clicked.connect(self.cancel_run)
        self.track_select_all_button.clicked.connect(self.select_all_tracks)
        self.track_deselect_duplicates_button.clicked.connect(self.deselect_duplicate_tracks)
        self.tracks_table.itemChanged.connect(self._track_item_changed)
        self.tracks_table.rows_reordered.connect(self._track_rows_reordered)
        self.makemkv_check_button.clicked.connect(self.check_makemkv_tools)
        self.makemkv_preview_button.clicked.connect(self.start_makemkv_preview)
        self.makemkv_run_button.clicked.connect(self.start_makemkv_run)
        if self.makemkv_clear_button:
            self.makemkv_clear_button.clicked.connect(self.clear_makemkv_inputs)
        if self.makemkv_reset_button:
            self.makemkv_reset_button.clicked.connect(self.reset_makemkv_tab)
        self.makemkv_cancel_button.clicked.connect(self.cancel_makemkv_run)
        self.audio_sync_check_button.clicked.connect(self.check_audio_sync_tools)
        self.audio_sync_load_button.clicked.connect(self.load_audio_sync_streams)
        self.audio_sync_analyze_button.clicked.connect(self.start_audio_sync_analysis)
        self.audio_sync_apply_organizer_button.clicked.connect(self.apply_audio_sync_delay_to_organizer)
        self.audio_sync_export_button.clicked.connect(self.start_audio_sync_export)
        self.audio_sync_select_all_button.clicked.connect(self.select_all_audio_sync_streams)
        self.audio_sync_clear_selection_button.clicked.connect(self.clear_audio_sync_stream_selection)
        if self.audio_sync_clear_button:
            self.audio_sync_clear_button.clicked.connect(self.clear_audio_sync_inputs)
        if self.audio_sync_reset_button:
            self.audio_sync_reset_button.clicked.connect(self.reset_audio_sync_tab)
        self.audio_sync_cancel_button.clicked.connect(self.cancel_audio_sync_task)
        self.audio_sync_reference_edit.textEdited.connect(lambda _text: self._clear_audio_sync_loaded_streams())
        self.audio_sync_source_edit.textEdited.connect(lambda _text: self._clear_audio_sync_loaded_streams())
        self.audio_sync_duration_combo.activated.connect(self._audio_sync_duration_preset_activated)
        self.audio_sync_spacing_combo.activated.connect(self._audio_sync_spacing_preset_activated)
        self.audio_sync_checkpoints_combo.activated.connect(self._audio_sync_checkpoints_preset_activated)
        self.input_edit.textEdited.connect(self._manual_input_changed)
        self.files_table.itemSelectionChanged.connect(self._populate_tracks_for_selection)
        self.metadata_combo.currentIndexChanged.connect(
            lambda _index: self._sync_combo_tooltip(self.metadata_combo, self.METADATA_MODE_HELP)
        )
        self.audio_name_style_combo.currentIndexChanged.connect(
            lambda _index: self._sync_combo_tooltip(self.audio_name_style_combo, self.AUDIO_NAME_STYLE_HELP)
        )
        self.language_order_style_combo.currentIndexChanged.connect(
            lambda _index: self._language_order_style_changed()
        )
        self.regional_order_combo.currentIndexChanged.connect(
            lambda _index: self._sync_combo_tooltip(self.regional_order_combo, self.REGIONAL_ORDER_HELP)
        )
        self.theme_combo.currentIndexChanged.connect(self.change_theme)

    def _tool_button(self, icon_id: QStyle.StandardPixmap, tooltip: str) -> QToolButton:
        button = QToolButton()
        button.setIcon(self.style().standardIcon(icon_id))
        button.setToolTip(tooltip)
        button.setAutoRaise(True)
        return button

    @Slot()
    def change_theme(self) -> None:
        self._apply_theme(str(self.theme_combo.currentData() or "dark"))

    def _apply_theme(self, theme: str | None = None) -> None:
        theme = theme or self.current_theme
        self.current_theme = "light" if theme == "light" else "dark"
        palette = self._theme_palette(self.current_theme)
        self.setStyleSheet(
            f"""
            QMainWindow, QWidget {{
                background: {palette['window']};
                color: {palette['text']};
                font-size: 9.5pt;
            }}
            QGroupBox {{
                background: {palette['panel']};
                border: 1px solid {palette['border']};
                border-radius: 6px;
                margin-top: 12px;
                padding-top: 10px;
                font-weight: 600;
            }}
            QGroupBox::title {{
                subcontrol-origin: margin;
                left: 10px;
                padding: 0 4px;
                color: {palette['title']};
                background: {palette['panel']};
            }}
            QLineEdit, QPlainTextEdit, QComboBox, QSpinBox {{
                background: {palette['field']};
                color: {palette['text']};
                border: 1px solid {palette['border']};
                border-radius: 5px;
                padding: 5px 7px;
                selection-background-color: {palette['primary']};
            }}
            QLineEdit:focus, QPlainTextEdit:focus, QComboBox:focus, QSpinBox:focus {{
                border-color: {palette['primary']};
            }}
            QComboBox QAbstractItemView {{
                background: {palette['panel']};
                color: {palette['text']};
                border: 1px solid {palette['border']};
                selection-background-color: {palette['primary']};
            }}
            QPushButton, QToolButton {{
                background: {palette['button']};
                color: {palette['text']};
                border: 1px solid {palette['border']};
                border-radius: 5px;
                padding: 6px 10px;
            }}
            QPushButton:hover, QToolButton:hover {{
                background: {palette['button_hover']};
                border-color: {palette['border_strong']};
            }}
            QPushButton#primaryButton {{
                background: {palette['primary']};
                border-color: {palette['primary_strong']};
                color: #ffffff;
                font-weight: 600;
            }}
            QPushButton#primaryButton:hover {{
                background: {palette['primary_strong']};
            }}
            QPushButton#dangerButton {{
                background: {palette['danger_bg']};
                border-color: {palette['danger_border']};
                color: {palette['danger_text']};
                font-weight: 600;
            }}
            QPushButton#dangerButton:hover {{
                background: {palette['danger_hover']};
            }}
            QPushButton#secondaryButton {{
                background: {palette['secondary_bg']};
                border-color: {palette['secondary_border']};
                color: {palette['secondary_text']};
            }}
            QTabWidget::pane {{
                border: 1px solid {palette['border']};
                border-radius: 6px;
                top: -1px;
            }}
            QTabBar::tab {{
                background: {palette['tab']};
                color: {palette['muted']};
                border: 1px solid {palette['border']};
                border-bottom: none;
                padding: 7px 14px;
                margin-right: 3px;
                border-top-left-radius: 5px;
                border-top-right-radius: 5px;
            }}
            QTabBar::tab:selected {{
                background: {palette['panel']};
                color: {palette['text']};
                border-top: 3px solid {palette['primary']};
            }}
            QHeaderView::section {{
                background: {palette['header']};
                color: {palette['title']};
                border: none;
                border-right: 1px solid {palette['border']};
                padding: 5px 7px;
                font-weight: 600;
            }}
            QTableWidget {{
                background: {palette['panel']};
                color: {palette['text']};
                alternate-background-color: {palette['alternate']};
                gridline-color: {palette['grid']};
                border: 1px solid {palette['border']};
                border-radius: 4px;
            }}
            QProgressBar {{
                background: {palette['progress_bg']};
                color: {palette['text']};
                border: 1px solid {palette['border']};
                border-radius: 4px;
                height: 12px;
                text-align: center;
            }}
            QProgressBar::chunk {{
                background: {palette['progress']};
                border-radius: 3px;
            }}
            QStatusBar {{
                background: {palette['status']};
                color: {palette['muted']};
                border-top: 1px solid {palette['border']};
            }}
            """
        )
        self._refresh_status_styles()

    def _theme_palette(self, theme: str) -> dict[str, str]:
        if theme == "light":
            return {
                "window": "#f5f7fa",
                "panel": "#ffffff",
                "field": "#ffffff",
                "alternate": "#f8fafc",
                "tab": "#e8edf3",
                "header": "#e8edf3",
                "status": "#eef2f7",
                "text": "#1f2933",
                "title": "#334155",
                "muted": "#475569",
                "border": "#d7dde5",
                "border_strong": "#94a3b8",
                "grid": "#e2e8f0",
                "button": "#ffffff",
                "button_hover": "#f1f5f9",
                "primary": "#2563eb",
                "primary_strong": "#1d4ed8",
                "secondary_bg": "#eef6ff",
                "secondary_border": "#bfdbfe",
                "secondary_text": "#1e3a8a",
                "danger_bg": "#fff5f5",
                "danger_border": "#f1a5a5",
                "danger_text": "#9f1239",
                "danger_hover": "#ffe4e6",
                "progress_bg": "#e8edf3",
                "progress": "#16a34a",
            }
        return {
            "window": "#101418",
            "panel": "#171d24",
            "field": "#0d1117",
            "alternate": "#141a21",
            "tab": "#111820",
            "header": "#202833",
            "status": "#0d1117",
            "text": "#e5e7eb",
            "title": "#f3f4f6",
            "muted": "#a7b0bd",
            "border": "#303946",
            "border_strong": "#596579",
            "grid": "#2a3340",
            "button": "#1d2430",
            "button_hover": "#273142",
            "primary": "#2f81f7",
            "primary_strong": "#1f6feb",
            "secondary_bg": "#172536",
            "secondary_border": "#315170",
            "secondary_text": "#9bd1ff",
            "danger_bg": "#331c22",
            "danger_border": "#7f2d3a",
            "danger_text": "#ffb4bd",
            "danger_hover": "#44232b",
            "progress_bg": "#222b36",
            "progress": "#2fb170",
        }

    def _apply_combo_help(self, combo: QComboBox, help_by_key: dict[str, str]) -> None:
        for index in range(combo.count()):
            key = combo.itemData(index)
            if key is None:
                key = combo.itemText(index)
            tooltip = help_by_key.get(str(key), "")
            if tooltip:
                combo.setItemData(index, tooltip, Qt.ToolTipRole)
        self._sync_combo_tooltip(combo, help_by_key)

    def _sync_combo_tooltip(self, combo: QComboBox, help_by_key: dict[str, str]) -> None:
        key = combo.currentData()
        if key is None:
            key = combo.currentText()
        combo.setToolTip(help_by_key.get(str(key), ""))

    def _language_order_style_changed(self) -> None:
        self._sync_combo_tooltip(self.language_order_style_combo, self.LANGUAGE_ORDER_STYLE_HELP)
        self.regional_order_combo.setEnabled(self.language_order_style_combo.currentData() == "regional")
        self._sync_combo_tooltip(self.regional_order_combo, self.REGIONAL_ORDER_HELP)

    def _append_text(self, edit: QPlainTextEdit, text: str) -> None:
        edit.moveCursor(QTextCursor.End)
        edit.insertPlainText(text)
        edit.moveCursor(QTextCursor.End)

    def append_summary_line(self, text: str = "") -> None:
        self._append_text(self.summary_edit, f"{text}\n")

    def append_makemkv_summary_line(self, text: str = "") -> None:
        self._append_text(self.makemkv_summary_edit, f"{text}\n")

    def append_audio_sync_summary_line(self, text: str = "") -> None:
        self._append_text(self.audio_sync_summary_edit, f"{text}\n")

    @Slot(str)
    def append_audio_sync_log(self, text: str) -> None:
        self._append_text(self.audio_sync_log_edit, f"{text}\n")

    def _set_progress_label(self, text: str) -> None:
        self.progress_label.setText(text[:120] if text else "Idle")

    def _load_default_args(self):
        config_defaults, config_path = organizer.config_defaults_from_argv([])
        parser = organizer.build_parser(config_defaults)
        return parser.parse_args([]), config_path

    def _apply_default_args(self, args) -> None:
        if args.path:
            self._set_input_text(str(args.path))
        if args.output_dir:
            self.output_edit.setText(str(args.output_dir))
        self.suffix_edit.setText(args.output_suffix or "")
        self.forced_ids_edit.setText(args.forced_subtitle_ids or "")
        self.subtitle_language_edit.setText("; ".join(args.subtitle_language_ids or []))
        self.audio_delays_edit.setText(getattr(args, "audio_delays", "") or "")
        self.subtitle_delays_edit.setText(getattr(args, "subtitle_delays", "") or "")
        self.preferred_language_edit.setText(getattr(args, "preferred_language", "") or "")

        self.recursive_check.setChecked(bool(args.recursive))
        self.merge_inputs_check.setChecked(bool(getattr(args, "merge_inputs", False)))
        self.smart_subs_check.setChecked(bool(args.smart_sub_detection))
        self.drop_empty_check.setChecked(bool(args.drop_empty_subs))
        self.duplicate_check.setChecked(bool(getattr(args, "detect_duplicate_tracks", True)))
        self.variant_check.setChecked(bool(args.detect_language_variants))
        self.auto_pgs_ocr_check.setChecked(bool(args.auto_pgs_ocr))
        self.auto_commentary_ocr_check.setChecked(bool(args.auto_commentary_ocr))
        self.report_check.setChecked(bool(args.report))
        self.preferred_audio_first_check.setChecked(bool(getattr(args, "preferred_audio_first", False)))
        self.preferred_audio_default_check.setChecked(bool(getattr(args, "preferred_audio_default", False)))
        self.preferred_subtitle_first_check.setChecked(bool(getattr(args, "preferred_subtitle_first", False)))
        self.preferred_forced_subtitle_default_check.setChecked(
            bool(getattr(args, "preferred_forced_subtitle_default", False))
        )

        self.metadata_combo.setCurrentText(args.metadata_edit_mode)
        audio_style_index = self.audio_name_style_combo.findData(getattr(args, "audio_name_style", "auto"))
        if audio_style_index >= 0:
            self.audio_name_style_combo.setCurrentIndex(audio_style_index)
        language_order_index = self.language_order_style_combo.findData(getattr(args, "language_order_style", "default"))
        if language_order_index >= 0:
            self.language_order_style_combo.setCurrentIndex(language_order_index)
        regional_order = ",".join(organizer.parse_regional_order(getattr(args, "regional_order", None)))
        regional_order_index = self.regional_order_combo.findData(regional_order)
        if regional_order_index >= 0:
            self.regional_order_combo.setCurrentIndex(regional_order_index)
        self._sync_combo_tooltip(self.metadata_combo, self.METADATA_MODE_HELP)
        self._sync_combo_tooltip(self.audio_name_style_combo, self.AUDIO_NAME_STYLE_HELP)
        self._language_order_style_changed()
        self.report_format_combo.setCurrentText(args.report_format)

    def _set_input_text(self, text: str) -> None:
        self._syncing_input_edit = True
        self.input_edit.setText(text)
        self._syncing_input_edit = False

    @Slot(str)
    def _manual_input_changed(self, _text: str) -> None:
        if self._syncing_input_edit:
            return
        if self.input_paths:
            self.input_paths = []
            self.current_reports = []
            self.manual_track_includes = {}
            self.manual_track_order = []
            self._refresh_file_list()
            self.tracks_table.setRowCount(0)
            self._set_track_selection_controls_enabled(False)

    @Slot()
    def choose_file(self) -> None:
        paths, _filter = QFileDialog.getOpenFileNames(
            self,
            "Choose Matroska files",
            "",
            "Matroska files (*.mkv *.mka);;Matroska video (*.mkv);;Matroska audio (*.mka)",
        )
        self.add_input_paths(Path(path) for path in paths)

    @Slot()
    def choose_folder(self) -> None:
        path = QFileDialog.getExistingDirectory(self, "Choose folder")
        if path:
            self.add_input_paths([Path(path)])

    @Slot()
    def choose_output_folder(self) -> None:
        path = QFileDialog.getExistingDirectory(self, "Choose output folder")
        if path:
            self.output_edit.setText(path)

    @Slot()
    def choose_makemkv_executable(self) -> None:
        path, _filter = QFileDialog.getOpenFileName(
            self,
            "Choose makemkvcon",
            "",
            "MakeMKV console (makemkvcon64.exe makemkvcon.exe);;Executables (*.exe);;All files (*)",
        )
        if path:
            self.makemkv_path_edit.setText(path)

    @Slot()
    def choose_makemkv_source_folder(self) -> None:
        path = QFileDialog.getExistingDirectory(self, "Choose MakeMKV source folder")
        if path:
            self.makemkv_source_edit.setText(path)

    @Slot()
    def choose_makemkv_output_folder(self) -> None:
        path = QFileDialog.getExistingDirectory(self, "Choose MakeMKV output folder")
        if path:
            self.makemkv_output_edit.setText(path)

    @Slot()
    def choose_audio_sync_reference_file(self) -> None:
        path, _filter = QFileDialog.getOpenFileName(self, "Choose reference media", "", "Media files (*.mkv *.mka *.mp4 *.mov *.avi *.flac *.wav *.aac *.ac3 *.dts);;All files (*)")
        if path:
            self._set_audio_sync_media_path(self.audio_sync_reference_edit, Path(path))

    @Slot()
    def choose_audio_sync_source_file(self) -> None:
        path, _filter = QFileDialog.getOpenFileName(self, "Choose source media", "", "Media files (*.mkv *.mka *.mp4 *.mov *.avi *.flac *.wav *.aac *.ac3 *.dts);;All files (*)")
        if path:
            self._set_audio_sync_media_path(self.audio_sync_source_edit, Path(path))

    @Slot()
    def choose_audio_sync_output_folder(self) -> None:
        path = QFileDialog.getExistingDirectory(self, "Choose audio sync export folder")
        if path:
            self.audio_sync_output_edit.setText(path)

    def _set_audio_sync_media_path(self, edit: QLineEdit, path: Path) -> None:
        resolved_path = Path(path).expanduser().resolve()
        edit.setText(str(resolved_path))
        if edit is self.audio_sync_source_edit and not self.audio_sync_output_edit.text().strip():
            self.audio_sync_output_edit.setText(str(resolved_path.parent / "synced"))
        self._clear_audio_sync_loaded_streams()

    def _clear_audio_sync_loaded_streams(self) -> None:
        self.audio_sync_reference_streams = []
        self.audio_sync_source_streams = []
        self.audio_sync_result = None
        self.audio_sync_ref_combo.clear()
        self.audio_sync_source_combo.clear()
        self.audio_sync_tracks_table.setRowCount(0)
        self.audio_sync_apply_organizer_button.setEnabled(False)
        self.audio_sync_export_button.setEnabled(False)
        self._set_audio_sync_selection_controls_enabled(False)

    @Slot()
    def select_all_audio_sync_streams(self) -> None:
        self._set_audio_sync_stream_checks(Qt.Checked)

    @Slot()
    def clear_audio_sync_stream_selection(self) -> None:
        self._set_audio_sync_stream_checks(Qt.Unchecked)

    def _set_audio_sync_stream_checks(self, check_state: Qt.CheckState) -> None:
        for row in range(self.audio_sync_tracks_table.rowCount()):
            item = self.audio_sync_tracks_table.item(row, 0)
            if item:
                item.setCheckState(check_state)

    def _set_audio_sync_selection_controls_enabled(self, enabled: bool) -> None:
        self.audio_sync_select_all_button.setEnabled(enabled)
        self.audio_sync_clear_selection_button.setEnabled(enabled)

    def _confirm_audio_sync_warnings(self) -> bool:
        if not self.audio_sync_result or not self.audio_sync_result.warnings:
            return True
        details = "\n".join(f"- {warning}" for warning in self.audio_sync_result.warnings)
        answer = QMessageBox.question(
            self,
            "Audio Sync warnings",
            "The current sync result has warnings:\n\n"
            f"{details}\n\n"
            "Continue anyway?",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        return answer == QMessageBox.Yes

    @Slot()
    def apply_audio_sync_delay_to_organizer(self) -> None:
        if not self.audio_sync_result:
            QMessageBox.information(self, "Audio Sync", "Run an analysis first.")
            return
        if not self._confirm_audio_sync_warnings():
            return
        selected_streams = self._selected_audio_sync_streams()
        if not selected_streams:
            QMessageBox.information(self, "Audio Sync", "Select at least one source audio track.")
            return

        try:
            _reference_path, source_path = self._audio_sync_paths()
        except Exception as error:
            QMessageBox.critical(self, "Invalid Audio Sync settings", str(error))
            return

        if not organizer.is_matroska_input_file(source_path):
            QMessageBox.information(self, "Audio Sync", "The Organizer workflow needs a .mkv or .mka source file.")
            return

        delay_ms = int(round(self.audio_sync_result.timeline_shift_seconds * 1000))
        delay_text = ", ".join(f"{stream.index}:{delay_ms:+d}" for stream in selected_streams)
        self.input_paths = []
        self.add_input_paths([source_path])
        self.audio_delays_edit.setText(delay_text)
        self.tabs.setCurrentIndex(0)
        self.append_audio_sync_summary_line(f"Organizer will apply audio delays: {delay_text}")
        self.append_audio_sync_summary_line(
            f"Timeline shift: {audio_sync.format_delay_ms(self.audio_sync_result.timeline_shift_seconds)}"
        )
        self.append_audio_sync_summary_line("Run Preview or Run in Organizer to remux with those delayed tracks.")
        self.statusBar().showMessage("Audio Sync delay prepared in Organizer")

    @Slot(int)
    def _audio_sync_duration_preset_activated(self, index: int) -> None:
        if self.audio_sync_duration_combo.itemData(index) != self.AUDIO_SYNC_CUSTOM_PRESET:
            self.audio_sync_previous_duration_index = index
            return
        if self._audio_sync_preset_prompt_active:
            return
        self._audio_sync_preset_prompt_active = True
        try:
            seconds, accepted = QInputDialog.getDouble(
                self,
                "Custom duration",
                "Duration in seconds",
                self.audio_sync_custom_duration_seconds,
                10.0,
                600.0,
                1,
            )
            if accepted:
                self.audio_sync_custom_duration_seconds = float(seconds)
                self.audio_sync_duration_combo.setItemText(
                    index,
                    f"Custom: {self._format_audio_sync_seconds(seconds)}",
                )
                self.audio_sync_previous_duration_index = index
            else:
                self.audio_sync_duration_combo.setCurrentIndex(self.audio_sync_previous_duration_index)
        finally:
            self._audio_sync_preset_prompt_active = False

    @Slot(int)
    def _audio_sync_spacing_preset_activated(self, index: int) -> None:
        if self.audio_sync_spacing_combo.itemData(index) != self.AUDIO_SYNC_CUSTOM_PRESET:
            self.audio_sync_previous_spacing_index = index
            return
        if self._audio_sync_preset_prompt_active:
            return
        self._audio_sync_preset_prompt_active = True
        try:
            minutes, accepted = QInputDialog.getDouble(
                self,
                "Custom spacing",
                "Spacing in minutes",
                self.audio_sync_custom_spacing_seconds / 60.0,
                1.0,
                60.0,
                1,
            )
            if accepted:
                self.audio_sync_custom_spacing_seconds = float(minutes) * 60.0
                self.audio_sync_spacing_combo.setItemText(
                    index,
                    f"Custom: {self._format_audio_sync_seconds(self.audio_sync_custom_spacing_seconds)}",
                )
                self.audio_sync_previous_spacing_index = index
            else:
                self.audio_sync_spacing_combo.setCurrentIndex(self.audio_sync_previous_spacing_index)
        finally:
            self._audio_sync_preset_prompt_active = False

    @Slot(int)
    def _audio_sync_checkpoints_preset_activated(self, index: int) -> None:
        if self.audio_sync_checkpoints_combo.itemData(index) != self.AUDIO_SYNC_CUSTOM_PRESET:
            self.audio_sync_previous_checkpoints_index = index
            return
        if self._audio_sync_preset_prompt_active:
            return
        self._audio_sync_preset_prompt_active = True
        try:
            checkpoints, accepted = QInputDialog.getInt(
                self,
                "Custom checkpoints",
                "Checkpoints",
                self.audio_sync_custom_checkpoints,
                1,
                20,
                1,
            )
            if accepted:
                self.audio_sync_custom_checkpoints = int(checkpoints)
                self.audio_sync_checkpoints_combo.setItemText(index, f"Custom: {checkpoints}")
                self.audio_sync_previous_checkpoints_index = index
            else:
                self.audio_sync_checkpoints_combo.setCurrentIndex(self.audio_sync_previous_checkpoints_index)
        finally:
            self._audio_sync_preset_prompt_active = False

    def _audio_sync_preset_seconds(self, combo: QComboBox, custom_seconds: float) -> float:
        value = combo.currentData()
        if value == self.AUDIO_SYNC_CUSTOM_PRESET:
            return custom_seconds
        return float(value)

    def _audio_sync_preset_int(self, combo: QComboBox, custom_value: int) -> int:
        value = combo.currentData()
        if value == self.AUDIO_SYNC_CUSTOM_PRESET:
            return custom_value
        return int(value)

    def _format_audio_sync_seconds(self, seconds: float) -> str:
        if seconds >= 60 and seconds % 60 == 0:
            minutes = int(seconds // 60)
            return f"{minutes} min"
        if seconds == int(seconds):
            return f"{int(seconds)} s"
        return f"{seconds:.1f} s"

    @Slot()
    def _makemkv_selection_changed(self) -> None:
        self._sync_combo_tooltip(self.makemkv_selection_combo, self.MAKEMKV_SELECTION_HELP)
        custom = self.makemkv_selection_combo.currentData() == "custom"
        self.makemkv_custom_rule_edit.setEnabled(custom)

    @Slot()
    def clear_inputs(self) -> None:
        if self._reset_or_clear_blocked("Clear Organizer inputs"):
            return
        self.input_paths = []
        self.current_reports = []
        self.manual_track_includes = {}
        self.manual_track_order = []
        self._set_input_text("")
        self._refresh_file_list()
        self.tracks_table.setRowCount(0)
        self._set_track_selection_controls_enabled(False)
        self.statusBar().showMessage("Organizer inputs cleared")

    @Slot()
    def reset_organizer_tab(self) -> None:
        if self._reset_or_clear_blocked("Reset Organizer tab"):
            return
        self._reset_organizer_tab()
        self.progress.setRange(0, 1)
        self.progress.setValue(0)
        self._set_progress_label("Idle")
        self.statusBar().showMessage("Organizer tab reset")

    @Slot()
    def clear_audio_sync_inputs(self) -> None:
        if self._reset_or_clear_blocked("Clear Audio Sync inputs"):
            return
        self.audio_sync_reference_edit.clear()
        self.audio_sync_source_edit.clear()
        self._clear_audio_sync_loaded_streams()
        self.statusBar().showMessage("Audio Sync inputs cleared")

    @Slot()
    def reset_audio_sync_tab(self) -> None:
        if self._reset_or_clear_blocked("Reset Audio Sync tab"):
            return
        self._reset_audio_sync_tab()
        self.progress.setRange(0, 1)
        self.progress.setValue(0)
        self._set_progress_label("Idle")
        self.statusBar().showMessage("Audio Sync tab reset")

    @Slot()
    def clear_makemkv_inputs(self) -> None:
        if self._reset_or_clear_blocked("Clear MakeMKV inputs"):
            return
        self.makemkv_source_edit.clear()
        self.makemkv_reports = []
        self.makemkv_table.setRowCount(0)
        self.statusBar().showMessage("MakeMKV inputs cleared")

    @Slot()
    def reset_makemkv_tab(self) -> None:
        if self._reset_or_clear_blocked("Reset MakeMKV tab"):
            return
        self._reset_makemkv_tab()
        self.progress.setRange(0, 1)
        self.progress.setValue(0)
        self._set_progress_label("Idle")
        self.statusBar().showMessage("MakeMKV tab reset")

    def _reset_or_clear_blocked(self, title: str) -> bool:
        if not self._workflow_is_running():
            return False
        QMessageBox.information(self, title, "Wait for the current task to finish first.")
        return True

    @Slot()
    def reset_all_tabs(self, confirm: bool = True) -> None:
        if self._workflow_is_running():
            QMessageBox.information(self, "Reset all tabs", "Wait for the current task to finish first.")
            return

        if confirm:
            answer = QMessageBox.question(
                self,
                "Reset all tabs",
                "Clear inputs, loaded streams, tables, logs, and results in every tab?\n\n"
                "Organizer options will return to their configured defaults.",
                QMessageBox.Yes | QMessageBox.No,
                QMessageBox.No,
            )
            if answer != QMessageBox.Yes:
                return

        self._reset_organizer_tab()
        self._reset_audio_sync_tab()
        self._reset_makemkv_tab()
        self.progress.setRange(0, 1)
        self.progress.setValue(0)
        self._set_progress_label("Idle")
        self.statusBar().showMessage("All tabs reset")
        self.tabs.setCurrentIndex(0)

    def _reset_organizer_tab(self) -> None:
        self.input_paths = []
        self.current_reports = []
        self.manual_track_includes = {}
        self.manual_track_order = []
        self._set_input_text("")
        self.output_edit.clear()
        self.files_table.setRowCount(0)
        self.tracks_table.setRowCount(0)
        self._set_track_selection_controls_enabled(False)
        self.summary_edit.clear()
        self.log_edit.clear()
        self.output_tabs.setCurrentIndex(0)
        self._apply_default_args(self.default_args)
        self.input_paths = []
        self.current_reports = []
        self.manual_track_includes = {}
        self.manual_track_order = []
        self._set_input_text("")
        self.files_table.setRowCount(0)
        self.tracks_table.setRowCount(0)
        self._set_track_selection_controls_enabled(False)
        self.advanced_button.setChecked(False)
        self._set_running(False)
        self.cancel_button.setEnabled(False)

    def _reset_audio_sync_tab(self) -> None:
        self.audio_sync_reference_edit.clear()
        self.audio_sync_source_edit.clear()
        self.audio_sync_output_edit.clear()
        self.audio_sync_start_edit.setText("00:10:00")
        self.audio_sync_duration_combo.setCurrentIndex(1)
        self.audio_sync_spacing_combo.setCurrentIndex(2)
        self.audio_sync_max_offset_edit.setText("5")
        self.audio_sync_checkpoints_combo.setCurrentIndex(2)
        self.audio_sync_custom_duration_seconds = 120.0
        self.audio_sync_custom_spacing_seconds = 900.0
        self.audio_sync_custom_checkpoints = 4
        self.audio_sync_previous_duration_index = self.audio_sync_duration_combo.currentIndex()
        self.audio_sync_previous_spacing_index = self.audio_sync_spacing_combo.currentIndex()
        self.audio_sync_previous_checkpoints_index = self.audio_sync_checkpoints_combo.currentIndex()
        self.audio_sync_summary_edit.clear()
        self.audio_sync_log_edit.clear()
        self.audio_sync_output_tabs.setCurrentIndex(0)
        self._clear_audio_sync_loaded_streams()
        self._set_audio_sync_running(False)
        self.audio_sync_cancel_button.setEnabled(False)

    def _reset_makemkv_tab(self) -> None:
        self.makemkv_path_edit.setText(self._default_makemkv_path_text())
        self.makemkv_source_edit.clear()
        self.makemkv_output_edit.clear()
        self.makemkv_min_length_spin.setValue(makemkv.DEFAULT_MIN_LENGTH_SECONDS)
        selection_index = self.makemkv_selection_combo.findData("english")
        if selection_index >= 0:
            self.makemkv_selection_combo.setCurrentIndex(selection_index)
        self.makemkv_custom_rule_edit.clear()
        self.makemkv_pipeline_check.setChecked(False)
        self.makemkv_reports = []
        self.makemkv_table.setRowCount(0)
        self.makemkv_summary_edit.clear()
        self.makemkv_log_edit.clear()
        self.makemkv_output_tabs.setCurrentIndex(0)
        self._makemkv_selection_changed()
        self._set_makemkv_running(False)
        self.makemkv_cancel_button.setEnabled(False)

    def _default_makemkv_path_text(self) -> str:
        try:
            return str(makemkv.find_makemkv())
        except makemkv.MakeMkvError:
            return ""

    def add_input_paths(self, paths) -> None:
        added = False
        seen = {str(path.resolve()).casefold() for path in self.input_paths}

        for raw_path in paths:
            path = Path(raw_path).expanduser().resolve()
            if not self._is_supported_input_path(path):
                continue
            key = str(path).casefold()
            if key in seen:
                continue
            seen.add(key)
            self.input_paths.append(path)
            added = True

        if added:
            self.current_reports = []
            self.manual_track_includes = {}
            self.manual_track_order = []
            self._sync_input_summary()
            self._refresh_file_list()
            self.tracks_table.setRowCount(0)
            self._set_track_selection_controls_enabled(False)

    def _is_supported_input_path(self, path: Path) -> bool:
        return path.is_dir() or organizer.is_matroska_input_file(path)

    def _sync_input_summary(self) -> None:
        if not self.input_paths:
            self._set_input_text("")
        elif len(self.input_paths) == 1:
            self._set_input_text(str(self.input_paths[0]))
        else:
            self._set_input_text(f"{len(self.input_paths)} selected sources")

    @Slot(bool)
    def toggle_advanced(self, checked: bool) -> None:
        self.advanced_button.setArrowType(Qt.DownArrow if checked else Qt.RightArrow)
        self.advanced_panel.setVisible(checked)

    @Slot()
    def check_organizer_tools(self) -> None:
        self.summary_edit.clear()
        try:
            args, config_path = self._build_args(dry_run=True)
            if self._has_organizer_input(args):
                context = self._validate_organizer_settings(args, config_path)
            else:
                context = None
                args = self._validate_organizer_settings(args, config_path, allow_empty_input=True)
        except Exception as error:
            self.append_summary_line(f"Check failed: {error}")
            QMessageBox.critical(self, "Organizer check failed", str(error))
            return

        self.append_summary_line("Organizer check passed.")
        if context:
            args = context.args
            self.append_summary_line(f"Matroska files found: {len(context.input_files)}")
        else:
            self.append_summary_line("Input: not selected yet")
            self.append_summary_line("Choose a Matroska file/folder to check file discovery and track IDs.")
        self.append_summary_line(f"mkvmerge: {args.mkvmerge}")
        self.append_summary_line(f"mkvextract: {args.mkvextract}")
        if args.mkvpropedit:
            self.append_summary_line(f"mkvpropedit: {args.mkvpropedit}")
        self.append_summary_line()
        self.statusBar().showMessage("Organizer check passed")
        if context:
            QMessageBox.information(self, "Organizer check", f"Ready. Matroska files found: {len(context.input_files)}")
        else:
            QMessageBox.information(
                self,
                "Organizer check",
                "Tools look ready. Choose an input Matroska file/folder to check files and track IDs.",
            )

    @Slot()
    def check_makemkv_tools(self) -> None:
        try:
            job = self._build_makemkv_job(dry_run=True)
            makemkv_path, disc_folders, selection_rule = self._validate_makemkv_settings(job)
        except Exception as error:
            self.append_makemkv_summary_line(f"Check failed: {error}")
            QMessageBox.critical(self, "MakeMKV check failed", str(error))
            return

        reports = [
            {
                "input": str(disc_folder),
                "output": str(job.output_root / disc_folder.name),
                "status": "ready",
                "message": "Ready for preview or run.",
            }
            for disc_folder in disc_folders
        ]
        self._populate_makemkv_results(reports)
        self.append_makemkv_summary_line("MakeMKV check passed.")
        self.append_makemkv_summary_line(f"MakeMKV: {makemkv_path}")
        self.append_makemkv_summary_line(f"Disc folders found: {len(disc_folders)}")
        self.append_makemkv_summary_line(f"Selection: {self.makemkv_selection_combo.currentText()}")
        self.append_makemkv_summary_line(f"Selection rule: {selection_rule}")
        if job.run_organizer_after:
            self.append_makemkv_summary_line("Pipeline: Organizer will run after MakeMKV.")
        self.append_makemkv_summary_line()
        self.statusBar().showMessage("MakeMKV check passed")
        QMessageBox.information(self, "MakeMKV check", f"Ready. Disc folders found: {len(disc_folders)}")

    @Slot()
    def check_audio_sync_tools(self) -> None:
        self.audio_sync_summary_edit.clear()
        try:
            ffmpeg = audio_sync.resolve_binary("ffmpeg")
            ffprobe = audio_sync.resolve_binary("ffprobe")
        except Exception as error:
            self.append_audio_sync_summary_line(f"Check failed: {error}")
            QMessageBox.critical(self, "Audio Sync check failed", str(error))
            return

        self.append_audio_sync_summary_line("Audio Sync check passed.")
        self.append_audio_sync_summary_line(f"ffmpeg: {ffmpeg}")
        self.append_audio_sync_summary_line(f"ffprobe: {ffprobe}")
        self.append_audio_sync_summary_line()
        self.statusBar().showMessage("Audio Sync check passed")
        QMessageBox.information(self, "Audio Sync check", "ffmpeg and ffprobe are ready.")

    @Slot()
    def load_audio_sync_streams(self) -> bool:
        try:
            reference_path, source_path = self._audio_sync_paths()
            self.audio_sync_reference_streams = audio_sync.probe_media_streams(reference_path)
            self.audio_sync_source_streams = audio_sync.probe_media_streams(source_path)
        except Exception as error:
            self.append_audio_sync_summary_line(f"Load failed: {error}")
            QMessageBox.critical(self, "Audio Sync load failed", str(error))
            return False

        reference_audio = [stream for stream in self.audio_sync_reference_streams if stream.type == "audio"]
        source_audio = [stream for stream in self.audio_sync_source_streams if stream.type == "audio"]
        if not reference_audio:
            QMessageBox.critical(self, "Audio Sync load failed", "Reference file has no audio streams.")
            return False
        if not source_audio:
            QMessageBox.critical(self, "Audio Sync load failed", "Source file has no audio streams.")
            return False

        self._populate_audio_sync_combo(self.audio_sync_ref_combo, reference_audio)
        self._populate_audio_sync_combo(self.audio_sync_source_combo, source_audio)
        self._populate_audio_sync_export_table(self.audio_sync_source_streams)
        self.audio_sync_result = None
        self.audio_sync_apply_organizer_button.setEnabled(False)
        self.audio_sync_export_button.setEnabled(False)
        self._set_audio_sync_selection_controls_enabled(self.audio_sync_tracks_table.rowCount() > 0)
        self.audio_sync_summary_edit.clear()
        self.append_audio_sync_summary_line("Streams loaded.")
        self.append_audio_sync_summary_line(f"Reference audio streams: {len(reference_audio)}")
        self.append_audio_sync_summary_line(f"Source audio streams: {len(source_audio)}")
        self.append_audio_sync_summary_line(
            f"Source subtitle streams: {len([stream for stream in self.audio_sync_source_streams if stream.type == 'subtitle'])}"
        )
        self.append_audio_sync_summary_line()
        self.statusBar().showMessage("Audio Sync streams loaded")
        return True

    @Slot()
    def start_audio_sync_analysis(self) -> None:
        if self.audio_sync_worker_thread and self.audio_sync_worker_thread.isRunning():
            return
        if self._other_workflow_is_running():
            QMessageBox.information(self, "Another task is running", "Wait for the current task to finish first.")
            return

        try:
            if not self.audio_sync_reference_streams or not self.audio_sync_source_streams:
                if not self.load_audio_sync_streams():
                    return
            settings = self._build_audio_sync_settings()
        except Exception as error:
            QMessageBox.critical(self, "Invalid Audio Sync settings", str(error))
            return

        self.audio_sync_result = None
        self.audio_sync_apply_organizer_button.setEnabled(False)
        self.audio_sync_export_button.setEnabled(False)
        self.audio_sync_summary_edit.clear()
        self.audio_sync_log_edit.clear()
        self.append_audio_sync_summary_line("Analysis started.")
        self.append_audio_sync_summary_line(f"Reference: {settings.reference_path}")
        self.append_audio_sync_summary_line(f"Source: {settings.source_path}")
        self.append_audio_sync_summary_line(
            f"Streams: reference 0:a:{settings.reference_audio_stream}, source 0:a:{settings.source_audio_stream}"
        )
        self.append_audio_sync_summary_line()
        self.progress.setRange(0, settings.checkpoints)
        self.progress.setValue(0)
        self._set_progress_label("Audio sync analysis")
        self._set_audio_sync_running(True)

        self.audio_sync_worker_thread = QThread(self)
        self.audio_sync_worker = AudioSyncWorker(settings)
        self.audio_sync_worker.moveToThread(self.audio_sync_worker_thread)
        self.audio_sync_worker_thread.started.connect(self.audio_sync_worker.run)
        self.audio_sync_worker.log.connect(self.handle_audio_sync_log)
        self.audio_sync_worker.progress.connect(self.handle_audio_sync_progress)
        self.audio_sync_worker.completed.connect(self.handle_audio_sync_completed)
        self.audio_sync_worker.failed.connect(self.handle_audio_sync_failed)
        self.audio_sync_worker.completed.connect(self.audio_sync_worker_thread.quit)
        self.audio_sync_worker.failed.connect(self.audio_sync_worker_thread.quit)
        self.audio_sync_worker_thread.finished.connect(self._audio_sync_thread_finished)
        self.audio_sync_worker_thread.start()

    @Slot()
    def start_audio_sync_export(self) -> None:
        if not self.audio_sync_result:
            QMessageBox.information(self, "Audio Sync export", "Run an analysis first.")
            return
        if self.audio_sync_worker_thread and self.audio_sync_worker_thread.isRunning():
            return
        if self._other_workflow_is_running():
            QMessageBox.information(self, "Another task is running", "Wait for the current task to finish first.")
            return
        if not self._confirm_audio_sync_warnings():
            return

        selected_streams = self._selected_audio_sync_streams()
        if not selected_streams:
            QMessageBox.information(self, "Audio Sync export", "Select at least one source audio track to export.")
            return

        try:
            _reference_path, source_path = self._audio_sync_paths()
            output_dir = self._audio_sync_output_dir(source_path)
        except Exception as error:
            QMessageBox.critical(self, "Invalid Audio Sync export settings", str(error))
            return

        self.append_audio_sync_summary_line("Export started.")
        self.append_audio_sync_summary_line(
            f"Writing {len(selected_streams)} selected audio track(s) to one shifted .mka."
        )
        self.append_audio_sync_summary_line(
            f"Timeline shift baked into export: {audio_sync.format_delay_ms(self.audio_sync_result.timeline_shift_seconds)}"
        )
        self.progress.setRange(0, 0)
        self._set_progress_label("Audio sync export")
        self._set_audio_sync_running(True)

        self.audio_sync_worker_thread = QThread(self)
        self.audio_sync_worker = AudioSyncExportWorker(
            source_path,
            selected_streams,
            self.audio_sync_result.timeline_shift_seconds,
            output_dir,
        )
        self.audio_sync_worker.moveToThread(self.audio_sync_worker_thread)
        self.audio_sync_worker_thread.started.connect(self.audio_sync_worker.run)
        self.audio_sync_worker.log.connect(self.handle_audio_sync_log)
        self.audio_sync_worker.completed.connect(self.handle_audio_sync_export_completed)
        self.audio_sync_worker.failed.connect(self.handle_audio_sync_failed)
        self.audio_sync_worker.completed.connect(self.audio_sync_worker_thread.quit)
        self.audio_sync_worker.failed.connect(self.audio_sync_worker_thread.quit)
        self.audio_sync_worker_thread.finished.connect(self._audio_sync_thread_finished)
        self.audio_sync_worker_thread.start()

    @Slot()
    def start_preview(self) -> None:
        self._start_run(dry_run=True)

    @Slot()
    def start_run(self) -> None:
        answer = QMessageBox.question(
            self,
            "Run remux",
            "This will write output files. Continue?",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        if answer != QMessageBox.Yes:
            return
        self._start_run(dry_run=False)

    @Slot()
    def cancel_run(self) -> None:
        if not self.worker:
            return
        self.worker.cancel()
        self.cancel_button.setEnabled(False)
        self.statusBar().showMessage("Cancelling...")

    @Slot()
    def start_makemkv_preview(self) -> None:
        self._start_makemkv(dry_run=True)

    @Slot()
    def start_makemkv_run(self) -> None:
        answer = QMessageBox.question(
            self,
            "Run MakeMKV batch",
            "This will write MKV outputs. Continue?",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        if answer != QMessageBox.Yes:
            return
        self._start_makemkv(dry_run=False)

    @Slot()
    def cancel_makemkv_run(self) -> None:
        if not self.makemkv_worker:
            return
        self.makemkv_worker.cancel()
        self.makemkv_cancel_button.setEnabled(False)
        self.statusBar().showMessage("Cancelling MakeMKV batch...")

    @Slot()
    def cancel_audio_sync_task(self) -> None:
        if not self.audio_sync_worker:
            return
        self.audio_sync_worker.cancel()
        self.audio_sync_cancel_button.setEnabled(False)
        self.statusBar().showMessage("Cancelling Audio Sync task...")

    def _start_run(self, dry_run: bool) -> None:
        if self.worker_thread and self.worker_thread.isRunning():
            return
        if self.makemkv_worker_thread and self.makemkv_worker_thread.isRunning():
            QMessageBox.information(self, "MakeMKV is running", "Wait for the MakeMKV batch to finish first.")
            return
        if self.audio_sync_worker_thread and self.audio_sync_worker_thread.isRunning():
            QMessageBox.information(self, "Audio Sync is running", "Wait for the Audio Sync task to finish first.")
            return

        try:
            args, config_path = self._build_args(dry_run)
            self._validate_organizer_settings(args, config_path)
        except Exception as error:
            QMessageBox.critical(self, "Invalid settings", str(error))
            return

        self.summary_edit.clear()
        self.log_edit.clear()
        self.current_reports = []
        self.tracks_table.setRowCount(0)
        self._set_track_selection_controls_enabled(False)
        self._refresh_file_list(running=True)
        self.progress.setRange(0, 0)
        self._set_progress_label("Starting")
        self.append_summary_line("Preview started." if dry_run else "Run started.")
        self.statusBar().showMessage("Starting...")
        self._set_running(True)

        self.worker_thread = QThread(self)
        self.worker = OrganizerWorker(args, config_path)
        self.worker.moveToThread(self.worker_thread)
        self.worker_thread.started.connect(self.worker.run)
        self.worker.log.connect(self.append_log)
        self.worker.event.connect(self.handle_event)
        self.worker.completed.connect(self.handle_completed)
        self.worker.failed.connect(self.handle_failed)
        self.worker.completed.connect(self.worker_thread.quit)
        self.worker.failed.connect(self.worker_thread.quit)
        self.worker_thread.finished.connect(self._thread_finished)
        self.worker_thread.start()

    def _start_makemkv(self, dry_run: bool) -> None:
        if self.makemkv_worker_thread and self.makemkv_worker_thread.isRunning():
            return
        if self.worker_thread and self.worker_thread.isRunning():
            QMessageBox.information(self, "Organizer is running", "Wait for the Organizer run to finish first.")
            return
        if self.audio_sync_worker_thread and self.audio_sync_worker_thread.isRunning():
            QMessageBox.information(self, "Audio Sync is running", "Wait for the Audio Sync task to finish first.")
            return

        try:
            job = self._build_makemkv_job(dry_run)
            self._validate_makemkv_settings(job)
            organizer_args = None
            organizer_config_path = None
            if job.run_organizer_after and not dry_run:
                organizer_args, organizer_config_path = self._build_pipeline_organizer_args(job.output_root)
                self._validate_organizer_settings(organizer_args, organizer_config_path, allow_empty_input=True)
        except Exception as error:
            QMessageBox.critical(self, "Invalid MakeMKV settings", str(error))
            return

        self.makemkv_summary_edit.clear()
        self.makemkv_log_edit.clear()
        self.makemkv_reports = []
        self.makemkv_table.setRowCount(0)
        self.progress.setRange(0, 0)
        self._set_progress_label("Starting MakeMKV")
        self.append_makemkv_summary_line("Preview started." if dry_run else "Run started.")
        if job.run_organizer_after and not dry_run:
            self.append_makemkv_summary_line("Pipeline: Organizer will run after MakeMKV.")
        self.statusBar().showMessage("Starting MakeMKV batch...")
        self._set_makemkv_running(True)

        self.makemkv_worker_thread = QThread(self)
        self.makemkv_worker = MakeMkvWorker(job, organizer_args, organizer_config_path)
        self.makemkv_worker.moveToThread(self.makemkv_worker_thread)
        self.makemkv_worker_thread.started.connect(self.makemkv_worker.run)
        self.makemkv_worker.log.connect(self.append_makemkv_log)
        self.makemkv_worker.event.connect(self.handle_makemkv_event)
        self.makemkv_worker.completed.connect(self.handle_makemkv_completed)
        self.makemkv_worker.failed.connect(self.handle_makemkv_failed)
        self.makemkv_worker.completed.connect(self.makemkv_worker_thread.quit)
        self.makemkv_worker.failed.connect(self.makemkv_worker_thread.quit)
        self.makemkv_worker_thread.finished.connect(self._makemkv_thread_finished)
        self.makemkv_worker_thread.start()

    def _build_args(self, dry_run: bool):
        args, config_path = self._load_default_args()
        if self.input_paths:
            args.input_paths = list(self.input_paths)
            args.path = self.input_paths[0]
        else:
            input_text = self.input_edit.text().strip()
            if input_text:
                args.path = Path(input_text)

        args.output_dir = Path(self.output_edit.text().strip()) if self.output_edit.text().strip() else None
        args.output_suffix = self.suffix_edit.text().strip()
        args.forced_subtitle_ids = self.forced_ids_edit.text().strip()
        args.audio_delays = self.audio_delays_edit.text().strip()
        args.subtitle_delays = self.subtitle_delays_edit.text().strip()
        args.subtitle_language_ids = [
            item.strip()
            for item in self.subtitle_language_edit.text().split(";")
            if item.strip()
        ]
        args.preferred_language = self.preferred_language_edit.text().strip()

        args.recursive = self.recursive_check.isChecked()
        args.dry_run = dry_run
        args.merge_inputs = self.merge_inputs_check.isChecked()
        if self.tracks_table.rowCount():
            self._sync_track_order_from_table()
        args.track_selection_overrides = dict(self.manual_track_includes)
        args.track_order_overrides = list(self.manual_track_order)
        args.smart_sub_detection = self.smart_subs_check.isChecked()
        args.drop_empty_subs = self.drop_empty_check.isChecked()
        args.detect_duplicate_tracks = self.duplicate_check.isChecked()
        args.detect_language_variants = self.variant_check.isChecked()
        args.auto_pgs_ocr = self.auto_pgs_ocr_check.isChecked()
        args.auto_commentary_ocr = self.auto_commentary_ocr_check.isChecked()
        args.report = self.report_check.isChecked()
        args.preferred_audio_first = self.preferred_audio_first_check.isChecked()
        args.preferred_audio_default = self.preferred_audio_default_check.isChecked()
        args.preferred_subtitle_first = self.preferred_subtitle_first_check.isChecked()
        args.preferred_forced_subtitle_default = self.preferred_forced_subtitle_default_check.isChecked()
        args.metadata_edit_mode = self.metadata_combo.currentText()
        args.audio_name_style = self.audio_name_style_combo.currentData() or "auto"
        args.language_order_style = self.language_order_style_combo.currentData() or "default"
        args.regional_order = self.regional_order_combo.currentData() or ""
        args.report_format = self.report_format_combo.currentText()
        return args, config_path

    def _build_makemkv_job(self, dry_run: bool) -> makemkv.MakeMkvBatchJob:
        source_text = self.makemkv_source_edit.text().strip()
        output_text = self.makemkv_output_edit.text().strip()
        if not source_text:
            raise ValueError("Choose a MakeMKV input folder.")
        if not output_text:
            raise ValueError("Choose a MakeMKV output folder.")

        makemkv_text = self.makemkv_path_edit.text().strip()
        return makemkv.MakeMkvBatchJob(
            source_root=Path(source_text),
            output_root=Path(output_text),
            makemkv_path=Path(makemkv_text) if makemkv_text else None,
            min_length_seconds=self.makemkv_min_length_spin.value(),
            selection_mode=self.makemkv_selection_combo.currentData() or "english",
            custom_selection_rule=self.makemkv_custom_rule_edit.text(),
            dry_run=dry_run,
            run_organizer_after=self.makemkv_pipeline_check.isChecked(),
        )

    def _audio_sync_paths(self) -> tuple[Path, Path]:
        reference_text = self.audio_sync_reference_edit.text().strip()
        source_text = self.audio_sync_source_edit.text().strip()
        if not reference_text:
            raise ValueError("Choose a reference media file.")
        if not source_text:
            raise ValueError("Choose a source media file.")
        reference_path = Path(reference_text).expanduser().resolve()
        source_path = Path(source_text).expanduser().resolve()
        if not reference_path.is_file():
            raise ValueError(f"Reference file not found: {reference_path}")
        if not source_path.is_file():
            raise ValueError(f"Source file not found: {source_path}")
        return reference_path, source_path

    def _audio_sync_output_dir(self, source_path: Path) -> Path:
        output_text = self.audio_sync_output_edit.text().strip()
        return Path(output_text).expanduser().resolve() if output_text else source_path.parent / "synced"

    def _build_audio_sync_settings(self) -> audio_sync.AudioSyncSettings:
        reference_path, source_path = self._audio_sync_paths()
        reference_stream = self.audio_sync_ref_combo.currentData()
        source_stream = self.audio_sync_source_combo.currentData()
        if reference_stream is None:
            raise ValueError("Load streams and choose a reference audio stream.")
        if source_stream is None:
            raise ValueError("Load streams and choose a source audio stream.")
        return audio_sync.AudioSyncSettings(
            reference_path=reference_path,
            source_path=source_path,
            reference_audio_stream=int(reference_stream),
            source_audio_stream=int(source_stream),
            start_seconds=audio_sync.parse_time(self.audio_sync_start_edit.text()),
            duration_seconds=self._audio_sync_preset_seconds(
                self.audio_sync_duration_combo,
                self.audio_sync_custom_duration_seconds,
            ),
            checkpoints=self._audio_sync_preset_int(
                self.audio_sync_checkpoints_combo,
                self.audio_sync_custom_checkpoints,
            ),
            checkpoint_spacing_seconds=self._audio_sync_preset_seconds(
                self.audio_sync_spacing_combo,
                self.audio_sync_custom_spacing_seconds,
            ),
            max_offset_seconds=audio_sync.parse_time(self.audio_sync_max_offset_edit.text()),
            sample_rate=self.AUDIO_SYNC_SAMPLE_RATE,
        )

    def _populate_audio_sync_combo(self, combo: QComboBox, streams: list[audio_sync.MediaStream]) -> None:
        combo.clear()
        for stream in streams:
            combo.addItem(stream.label, stream.relative_index)

    def _populate_audio_sync_export_table(self, streams: list[audio_sync.MediaStream]) -> None:
        exportable_streams = [stream for stream in streams if stream.type == "audio"]
        self.audio_sync_tracks_table.setRowCount(len(exportable_streams))
        for row, stream in enumerate(exportable_streams):
            export_item = QTableWidgetItem("")
            export_item.setFlags(export_item.flags() | Qt.ItemIsUserCheckable)
            export_item.setCheckState(Qt.Checked)
            export_item.setData(Qt.UserRole, stream)
            self.audio_sync_tracks_table.setItem(row, 0, export_item)
            values = [
                stream.type.title(),
                f"0:a:{stream.relative_index}",
                stream.codec,
                stream.language,
                stream.title,
            ]
            for column, value in enumerate(values, start=1):
                item = QTableWidgetItem(value)
                item.setData(Qt.UserRole, stream)
                self.audio_sync_tracks_table.setItem(row, column, item)
        self.audio_sync_tracks_table.resizeColumnsToContents()

    def _selected_audio_sync_streams(self) -> list[audio_sync.MediaStream]:
        selected: list[audio_sync.MediaStream] = []
        for row in range(self.audio_sync_tracks_table.rowCount()):
            item = self.audio_sync_tracks_table.item(row, 0)
            if item and item.checkState() == Qt.Checked:
                stream = item.data(Qt.UserRole)
                if isinstance(stream, audio_sync.MediaStream):
                    selected.append(stream)
        return selected

    def _build_pipeline_organizer_args(self, makemkv_output_root: Path):
        args, config_path = self._build_args(dry_run=False)
        args.path = Path(makemkv_output_root)
        args.input_paths = [Path(makemkv_output_root)]
        args.output_dir = None
        args.recursive = True
        args.dry_run = False
        return args, config_path

    def _validate_organizer_settings(self, args, config_path: Path | None, allow_empty_input: bool = False):
        self._validate_organizer_output_location(args)
        if allow_empty_input:
            return self._validate_organizer_tools_only(args, config_path)
        return organizer.prepare_batch_run(args, config_path)

    def _validate_organizer_tools_only(self, args, config_path: Path | None):
        args.config_path = config_path
        args.mkvmerge = organizer.resolve_tool_path(
            args.mkvmerge,
            "mkvmerge",
            "MKVMERGE",
            organizer.common_mkvtoolnix_paths("mkvmerge.exe"),
        )
        if args.mkvextract is None:
            mkvextract_fallbacks = []
            if args.mkvmerge:
                mkvextract_fallbacks.append(args.mkvmerge.with_name(organizer.MKVEXTRACT.name))
            mkvextract_fallbacks.extend(organizer.common_mkvtoolnix_paths("mkvextract.exe"))
            args.mkvextract = organizer.resolve_tool_path(None, "mkvextract", "MKVEXTRACT", mkvextract_fallbacks)
        else:
            args.mkvextract = organizer.resolve_tool_path(
                args.mkvextract,
                "mkvextract",
                "MKVEXTRACT",
                organizer.common_mkvtoolnix_paths("mkvextract.exe"),
            )

        mkvpropedit_fallbacks = []
        if args.mkvmerge:
            mkvpropedit_fallbacks.append(args.mkvmerge.with_name(organizer.MKVPROPEDIT.name))
        mkvpropedit_fallbacks.extend(organizer.common_mkvtoolnix_paths("mkvpropedit.exe"))
        args.mkvpropedit = organizer.resolve_tool_path(
            args.mkvpropedit,
            "mkvpropedit",
            "MKVPROPEDIT",
            mkvpropedit_fallbacks,
        )
        args.subtitle_edit = organizer.resolve_tool_path(
            args.subtitle_edit,
            "SubtitleEdit",
            "SUBTITLE_EDIT",
            organizer.common_subtitle_edit_paths(),
        )
        args.seconv = organizer.resolve_seconv_path(args.seconv)
        args.tesseract = organizer.resolve_tool_path(
            args.tesseract,
            "tesseract",
            "TESSERACT",
            organizer.common_tesseract_paths(),
        )

        if args.pgs_ocr_timeout_seconds <= 0:
            raise organizer.OrganizerError("--pgs-ocr-timeout-seconds must be greater than zero.")
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
                raise organizer.OrganizerError(f"--variant-context-dir is not a valid folder: {context_dir}")

        if args.overwrite and args.skip_existing:
            raise organizer.OrganizerError("Use only one option: --overwrite or --skip-existing.")
        if args.report_format not in {"json", "txt", "both"}:
            raise organizer.OrganizerError("--report-format must be json, txt, or both.")
        if args.tessdata_model not in organizer.TESSDATA_REPOS:
            raise organizer.OrganizerError("--tessdata-model must be best or fast.")
        args.audio_name_style = str(getattr(args, "audio_name_style", "auto") or "auto").strip().lower().replace("_", "-")
        if args.audio_name_style not in organizer.AUDIO_NAME_STYLES:
            allowed = ", ".join(sorted(organizer.AUDIO_NAME_STYLES))
            raise organizer.OrganizerError(f"--audio-name-style must be one of these values: {allowed}.")
        args.language_order_style = (
            str(getattr(args, "language_order_style", "default") or "default").strip().lower().replace("_", "-")
        )
        if args.language_order_style not in organizer.LANGUAGE_ORDER_STYLES:
            allowed = ", ".join(sorted(organizer.LANGUAGE_ORDER_STYLES))
            raise organizer.OrganizerError(f"--language-order-style must be one of these values: {allowed}.")
        args.regional_order = organizer.parse_regional_order(getattr(args, "regional_order", None))
        args.preferred_language = organizer.normalize_preferred_language(getattr(args, "preferred_language", ""))
        if (
            not args.preferred_language
            and (
                getattr(args, "preferred_audio_first", False)
                or getattr(args, "preferred_audio_default", False)
                or getattr(args, "preferred_subtitle_first", False)
                or getattr(args, "preferred_forced_subtitle_default", False)
            )
        ):
            raise organizer.OrganizerError(
                "--preferred-language is required when preferred language rules are enabled."
            )

        organizer.require_tool(args.mkvmerge, "mkvmerge")
        if args.metadata_edit_mode == "only":
            organizer.require_tool(args.mkvpropedit, "mkvpropedit")
        if (
            args.analyze_sub_sizes
            or args.smart_sub_detection
            or args.drop_empty_subs
            or args.detect_duplicate_tracks
            or args.detect_language_variants
            or args.prepare_pgs_ocr
            or args.auto_commentary_ocr
        ):
            organizer.require_tool(args.mkvextract, "mkvextract")

        organizer.parse_id_list(args.forced_subtitle_ids, "--forced-subtitle-ids")
        organizer.parse_subtitle_language_overrides(args.subtitle_language_ids)
        organizer.parse_track_delay_overrides(args.audio_delays, "--audio-delays")
        organizer.parse_track_delay_overrides(args.subtitle_delays, "--subtitle-delays")
        organizer.parse_id_list(args.explain_track, "--explain-track")
        return args

    def _validate_organizer_output_location(self, args) -> None:
        if not args.output_dir:
            return

        output_dir = Path(args.output_dir).expanduser().resolve()
        input_candidates = list(getattr(args, "input_paths", []) or [])
        if not input_candidates and args.path:
            input_candidates = [args.path]

        for candidate in input_candidates:
            source = Path(candidate).expanduser().resolve()
            source_root = source.parent if source.is_file() else source
            if output_dir == source_root:
                raise ValueError("Choose an output folder that is not the same as the input folder.")
            if (
                args.recursive
                and self._path_is_relative_to(output_dir, source_root)
                and output_dir.name.lower() != organizer.SORTED_DIR_NAME.lower()
            ):
                raise ValueError(
                    "With Recursive enabled, use an output folder outside the input tree, "
                    "or leave Output empty to use the safe _sorted folder."
                )

    def _has_organizer_input(self, args) -> bool:
        return bool(getattr(args, "input_paths", None)) or bool(getattr(args, "path", None))

    def _validate_makemkv_settings(self, job: makemkv.MakeMkvBatchJob) -> tuple[Path, list[Path], str]:
        makemkv_path = makemkv.find_makemkv(job.makemkv_path)
        selection_rule = makemkv.selection_rule_for_mode(job.selection_mode, job.custom_selection_rule)
        disc_folders = makemkv.discover_disc_folders(job.source_root)
        source_root = Path(job.source_root).expanduser().resolve()
        output_root = Path(job.output_root).expanduser().resolve()

        if output_root == source_root:
            raise ValueError("Choose a MakeMKV output folder that is not the same as the input folder.")
        if self._path_is_relative_to(output_root, source_root):
            raise ValueError("Choose a MakeMKV output folder outside the input folder.")

        return makemkv_path, disc_folders, selection_rule

    @staticmethod
    def _path_is_relative_to(path: Path, parent: Path) -> bool:
        try:
            path.relative_to(parent)
            return True
        except ValueError:
            return False

    @Slot(str)
    def append_log(self, text: str) -> None:
        self._append_text(self.log_edit, text)

    @Slot(str)
    def append_makemkv_log(self, text: str) -> None:
        self._append_text(self.makemkv_log_edit, text)

    @Slot(str, str, str, int, int, int, int)
    def handle_event(self, kind: str, message: str, file_path: str, index: int, total: int, step: int, steps: int) -> None:
        if total:
            steps = steps or 100
            total_units = self._progress_total_units(total, steps)
            self.progress.setRange(0, total_units)
            if kind == "file-started":
                value = max(0, index - 1) * steps
            elif kind == "file-progress":
                value = max(0, index - 1) * steps + max(0, min(step, steps))
            elif kind in {"file-finished", "file-error"}:
                value = index * steps
            else:
                value = self.progress.value()
            self.progress.setValue(min(value, total_units - self.FINALIZATION_PROGRESS_UNITS))
        if file_path:
            status = {
                "file-started": "Running",
                "file-progress": "Running",
                "file-finished": "Done",
                "file-error": "Error",
                "file-cancelled": "Cancelled",
            }.get(kind)
            if status:
                self._set_file_status(Path(file_path), status, message)
        if kind in {"batch-started", "batch-finished", "batch-cancelled", "file-started", "file-finished", "file-error", "file-cancelled"}:
            self.append_summary_line(message)
        self._set_progress_label(message)
        self.statusBar().showMessage(message)

    @Slot(str, str, str, int, int, int, int)
    def handle_makemkv_event(
        self,
        kind: str,
        message: str,
        disc_path: str,
        index: int,
        total: int,
        step: int,
        steps: int,
    ) -> None:
        if kind.startswith("organizer-"):
            self.handle_event(kind.removeprefix("organizer-"), message, disc_path, index, total, step, steps)
            return

        if total:
            steps = steps or 100
            total_units = self._progress_total_units(total, steps)
            self.progress.setRange(0, total_units)
            if kind == "disc-started":
                value = max(0, index - 1) * steps
            elif kind == "disc-progress":
                value = max(0, index - 1) * steps + max(0, min(step, steps))
            elif kind in {"disc-finished", "disc-error", "disc-cancelled"}:
                value = index * steps
            else:
                value = self.progress.value()
            self.progress.setValue(min(value, total_units - self.FINALIZATION_PROGRESS_UNITS))

        if disc_path:
            status = {
                "disc-started": "Running",
                "disc-progress": "Running",
                "disc-finished": "Done",
                "disc-error": "Error",
                "disc-cancelled": "Cancelled",
            }.get(kind)
            if status:
                self._set_makemkv_status(Path(disc_path), status, message)
        if kind in {"batch-started", "batch-finished", "batch-cancelled", "disc-started", "disc-finished", "disc-error", "disc-cancelled"}:
            self.append_makemkv_summary_line(message)
        self._set_progress_label(message)
        self.statusBar().showMessage(message)

    def _progress_total_units(self, total: int, steps: int = 100) -> int:
        return max(1, total * steps + self.FINALIZATION_PROGRESS_UNITS)

    @Slot(object)
    def handle_completed(self, result: organizer.BatchRunResult) -> None:
        total_units = self._progress_total_units(len(result.input_files), 100)
        self.progress.setRange(0, total_units)
        self.progress.setValue(total_units)
        self._populate_results(result.reports)
        if result.cancelled:
            self.statusBar().showMessage("Cancelled")
            self._set_progress_label("Cancelled")
        else:
            self.statusBar().showMessage(
                f"Completed with {result.failures} error(s)" if result.failures else "Completed without errors"
            )
            self._set_progress_label("Completed")
        self._append_organizer_result_summary(result)
        self._set_running(False)

    @Slot(str)
    def handle_failed(self, details: str) -> None:
        self.append_log(details)
        self.statusBar().showMessage("Failed")
        self._set_progress_label("Failed")
        self.append_summary_line("Run failed.")
        QMessageBox.critical(self, "Run failed", details)
        self._set_running(False)

    @Slot(object)
    def handle_makemkv_completed(self, result: makemkv.MakeMkvBatchResult) -> None:
        total = len(result.discs) or len(result.reports) or 1
        total_units = self._progress_total_units(total, 100)
        self.progress.setRange(0, total_units)
        self.progress.setValue(total_units)
        self._populate_makemkv_results(result.reports)
        if result.organizer_result:
            self._populate_results(result.organizer_result.reports)

        if result.cancelled:
            self.statusBar().showMessage("MakeMKV batch cancelled")
            self._set_progress_label("MakeMKV cancelled")
        elif result.failures:
            self.statusBar().showMessage(f"MakeMKV completed with {result.failures} error(s)")
            self._set_progress_label("MakeMKV completed with errors")
        elif result.organizer_result and result.organizer_result.failures:
            self.statusBar().showMessage(f"Organizer completed with {result.organizer_result.failures} error(s)")
            self._set_progress_label("Organizer completed with errors")
        elif result.organizer_result:
            self.statusBar().showMessage("MakeMKV and Organizer completed without errors")
            self._set_progress_label("Pipeline completed")
        else:
            self.statusBar().showMessage("MakeMKV completed without errors")
            self._set_progress_label("MakeMKV completed")
        self._append_makemkv_result_summary(result)
        self._set_makemkv_running(False)

    @Slot(str)
    def handle_makemkv_failed(self, details: str) -> None:
        self.append_makemkv_log(details)
        self.statusBar().showMessage("MakeMKV failed")
        self._set_progress_label("MakeMKV failed")
        self.append_makemkv_summary_line("MakeMKV failed.")
        QMessageBox.critical(self, "MakeMKV failed", details)
        self._set_makemkv_running(False)

    @Slot(str)
    def handle_audio_sync_log(self, message: str) -> None:
        self.append_audio_sync_log(message)
        if message.startswith("Checkpoint") or message.startswith("  offset=") or message.startswith("  skipped="):
            self.append_audio_sync_summary_line(message)
        self.statusBar().showMessage(message[:160])

    @Slot(int, int)
    def handle_audio_sync_progress(self, index: int, total: int) -> None:
        self.progress.setRange(0, max(1, total))
        self.progress.setValue(index)
        self._set_progress_label(f"Audio sync checkpoint {index}/{total}")

    @Slot(object)
    def handle_audio_sync_completed(self, result: audio_sync.AudioSyncResult) -> None:
        self.audio_sync_result = result
        self.progress.setRange(0, max(1, len(result.estimates)))
        self.progress.setValue(len(result.estimates))
        self._set_progress_label("Audio sync completed")
        self.append_audio_sync_summary_line()
        self.append_audio_sync_summary_line("Result")
        self.append_audio_sync_summary_line(
            f"Checkpoints used: {result.used_checkpoints or len(result.estimates)}/{len(result.estimates)}"
        )
        self.append_audio_sync_summary_line(
            f"Source offset vs reference: {audio_sync.format_delay_ms(result.median_offset_seconds)}"
        )
        self.append_audio_sync_summary_line(
            f"Timeline shift to apply: {audio_sync.format_delay_ms(result.timeline_shift_seconds)}"
        )
        self.append_audio_sync_summary_line(f"Checkpoint spread: {result.spread_seconds * 1000:.2f} ms")
        if result.ignored_checkpoints:
            self.append_audio_sync_summary_line(f"All-checkpoint spread: {result.all_spread_seconds * 1000:.2f} ms")
            self.append_audio_sync_summary_line(f"Ignored outliers: {result.ignored_checkpoints}")
        self.append_audio_sync_summary_line(
            f"Correlation confidence: {result.confidence_summary or audio_sync.confidence_label(result.average_confidence)} "
            f"({result.average_confidence:.2f})"
        )
        self.append_audio_sync_summary_line(f"Consistency: {result.consistency}")
        self.append_audio_sync_summary_line(f"Verdict: {result.verdict}")
        for warning in result.warnings:
            self.append_audio_sync_summary_line(f"Warning: {warning}.")
        self.append_audio_sync_summary_line()
        self.audio_sync_apply_organizer_button.setEnabled(self.audio_sync_tracks_table.rowCount() > 0)
        self.audio_sync_export_button.setEnabled(self.audio_sync_tracks_table.rowCount() > 0)
        self._set_audio_sync_selection_controls_enabled(self.audio_sync_tracks_table.rowCount() > 0)
        self.statusBar().showMessage("Audio Sync analysis completed")
        self._set_audio_sync_running(False)

    @Slot(object)
    def handle_audio_sync_export_completed(self, plan: audio_sync.ExportPlan) -> None:
        self.progress.setRange(0, 1)
        self.progress.setValue(1)
        self._set_progress_label("Audio sync export completed")
        self.append_audio_sync_summary_line()
        self.append_audio_sync_summary_line("Exported shifted .mka")
        self.append_audio_sync_summary_line(str(plan.output_path))
        if self.audio_sync_result:
            self.append_audio_sync_summary_line(
                f"Timeline shift: {audio_sync.format_delay_ms(self.audio_sync_result.timeline_shift_seconds)}"
            )
        self.append_audio_sync_summary_line(f"Audio tracks: {len(plan.streams)}")
        self.append_audio_sync_summary_line()
        self.statusBar().showMessage("Audio Sync export completed")
        self._set_audio_sync_running(False)

    @Slot(str)
    def handle_audio_sync_failed(self, details: str) -> None:
        self.append_audio_sync_log(details)
        self.append_audio_sync_summary_line(details.splitlines()[0] if details else "Audio Sync failed.")
        cancelled = "cancelled" in details.lower()
        status_text = "Audio Sync cancelled" if cancelled else "Audio Sync failed"
        self.statusBar().showMessage(status_text)
        self._set_progress_label(status_text)
        if not cancelled:
            QMessageBox.critical(self, "Audio Sync failed", details)
        self._set_audio_sync_running(False)

    @Slot()
    def _thread_finished(self) -> None:
        if self.worker:
            self.worker.deleteLater()
        if self.worker_thread:
            self.worker_thread.deleteLater()
        self.worker = None
        self.worker_thread = None

    @Slot()
    def _makemkv_thread_finished(self) -> None:
        if self.makemkv_worker:
            self.makemkv_worker.deleteLater()
        if self.makemkv_worker_thread:
            self.makemkv_worker_thread.deleteLater()
        self.makemkv_worker = None
        self.makemkv_worker_thread = None

    @Slot()
    def _audio_sync_thread_finished(self) -> None:
        if self.audio_sync_worker:
            self.audio_sync_worker.deleteLater()
        if self.audio_sync_worker_thread:
            self.audio_sync_worker_thread.deleteLater()
        self.audio_sync_worker = None
        self.audio_sync_worker_thread = None

    def _refresh_file_list(self, running: bool = False) -> None:
        if self.current_reports:
            self._populate_results(self.current_reports)
            return

        self.files_table.setRowCount(len(self.input_paths))
        for row, path in enumerate(self.input_paths):
            values = [
                "Queued" if running else "Ready",
                str(path),
                "",
                "Folder" if path.is_dir() else "",
            ]
            self._set_file_row(row, values, path)
        self.files_table.resizeColumnsToContents()

    def _populate_results(self, reports: list[dict]) -> None:
        self.current_reports = reports
        self.files_table.setRowCount(len(reports))
        for row, report in enumerate(reports):
            input_path = Path(report.get("input", ""))
            values = [
                report.get("status", ""),
                str(input_path),
                report.get("output", ""),
                report.get("message", ""),
            ]
            self._set_file_row(row, values, input_path)

        self.files_table.resizeColumnsToContents()
        if reports:
            self.files_table.selectRow(0)
            self._populate_tracks_for_row(0)
        else:
            self.tracks_table.setRowCount(0)

    def _populate_makemkv_results(self, reports: list[dict]) -> None:
        self.makemkv_reports = reports
        self.makemkv_table.setRowCount(len(reports))
        for row, report in enumerate(reports):
            input_path = Path(report.get("input", ""))
            values = [
                report.get("status", ""),
                str(input_path),
                report.get("output", ""),
                report.get("message", ""),
            ]
            self._set_makemkv_row(row, values, input_path)

        self.makemkv_table.resizeColumnsToContents()

    def _append_organizer_result_summary(self, result: organizer.BatchRunResult) -> None:
        self.append_summary_line()
        self.append_summary_line("Summary")
        self.append_summary_line(f"Files: {len(result.reports)}")
        self.append_summary_line(f"Errors: {result.failures}")
        self.append_summary_line(f"Cancelled: {'yes' if result.cancelled else 'no'}")
        for status, count in self._status_counts(result.reports).items():
            self.append_summary_line(f"{status}: {count}")
        for output_dir in self._output_dirs(result.reports):
            self.append_summary_line(f"Output: {output_dir}")
        self.append_summary_line()

    def _append_makemkv_result_summary(self, result: makemkv.MakeMkvBatchResult) -> None:
        self.append_makemkv_summary_line()
        self.append_makemkv_summary_line("Summary")
        self.append_makemkv_summary_line(f"Disc folders: {len(result.reports)}")
        self.append_makemkv_summary_line(f"Errors: {result.failures}")
        self.append_makemkv_summary_line(f"Cancelled: {'yes' if result.cancelled else 'no'}")
        for status, count in self._status_counts(result.reports).items():
            self.append_makemkv_summary_line(f"{status}: {count}")
        for output_dir in self._output_dirs(result.reports):
            self.append_makemkv_summary_line(f"MakeMKV output: {output_dir}")

        organizer_result = result.organizer_result
        if organizer_result:
            self.append_makemkv_summary_line()
            self.append_makemkv_summary_line("Organizer pipeline")
            self.append_makemkv_summary_line(f"Files: {len(organizer_result.reports)}")
            self.append_makemkv_summary_line(f"Errors: {organizer_result.failures}")
            for status, count in self._status_counts(organizer_result.reports).items():
                self.append_makemkv_summary_line(f"{status}: {count}")
            for output_dir in self._output_dirs(organizer_result.reports):
                self.append_makemkv_summary_line(f"Organizer output: {output_dir}")
        self.append_makemkv_summary_line()

    def _status_counts(self, reports: list[dict]) -> dict[str, int]:
        counts: dict[str, int] = {}
        for report in reports:
            status = str(report.get("status", "unknown") or "unknown")
            counts[status] = counts.get(status, 0) + 1
        return counts

    def _output_dirs(self, reports: list[dict], limit: int = 3) -> list[str]:
        seen: list[str] = []
        for report in reports:
            output = str(report.get("output", "") or "")
            if not output:
                continue
            output_dir = str(Path(output).parent)
            if output_dir not in seen:
                seen.append(output_dir)
            if len(seen) >= limit:
                break
        return seen

    def _set_file_row(self, row: int, values: list[str], path: Path) -> None:
        key = str(path.resolve()).casefold() if str(path) else ""
        for column, value in enumerate(values):
            item = QTableWidgetItem(str(value))
            if key:
                item.setData(Qt.UserRole, key)
            if column == 0:
                self._apply_status_style(item, str(value))
            self.files_table.setItem(row, column, item)

    def _set_makemkv_row(self, row: int, values: list[str], path: Path) -> None:
        key = str(path.resolve()).casefold() if str(path) else ""
        for column, value in enumerate(values):
            item = QTableWidgetItem(str(value))
            if key:
                item.setData(Qt.UserRole, key)
            if column == 0:
                self._apply_status_style(item, str(value))
            self.makemkv_table.setItem(row, column, item)

    def _set_file_status(self, path: Path, status: str, message: str) -> None:
        key = str(path.resolve()).casefold()
        row = self._file_row_for_key(key)
        if row is None:
            row = self.files_table.rowCount()
            self.files_table.insertRow(row)
            self._set_file_row(row, ["", str(path), "", ""], path)

        self.files_table.item(row, 0).setText(status)
        self._apply_status_style(self.files_table.item(row, 0), status)
        self.files_table.item(row, 3).setText(message)
        self.files_table.resizeColumnsToContents()

    def _set_makemkv_status(self, path: Path, status: str, message: str) -> None:
        key = str(path.resolve()).casefold()
        row = self._makemkv_row_for_key(key)
        if row is None:
            row = self.makemkv_table.rowCount()
            self.makemkv_table.insertRow(row)
            output_root = self.makemkv_output_edit.text().strip()
            output = str(Path(output_root) / path.name) if output_root else ""
            self._set_makemkv_row(row, ["", str(path), output, ""], path)

        self.makemkv_table.item(row, 0).setText(status)
        self._apply_status_style(self.makemkv_table.item(row, 0), status)
        self.makemkv_table.item(row, 3).setText(message)
        self.makemkv_table.resizeColumnsToContents()

    def _apply_status_style(self, item: QTableWidgetItem, status: str) -> None:
        colors = self.STATUS_COLORS_BY_THEME[self.current_theme].get(status)
        if not colors:
            return
        background, foreground = colors
        item.setBackground(QColor(background))
        item.setForeground(QColor(foreground))

    def _refresh_status_styles(self) -> None:
        for table in [self.files_table, self.makemkv_table]:
            for row in range(table.rowCount()):
                item = table.item(row, 0)
                if item:
                    self._apply_status_style(item, item.text())

    def _file_row_for_key(self, key: str) -> int | None:
        for row in range(self.files_table.rowCount()):
            item = self.files_table.item(row, 0)
            if item and item.data(Qt.UserRole) == key:
                return row
        return None

    def _makemkv_row_for_key(self, key: str) -> int | None:
        for row in range(self.makemkv_table.rowCount()):
            item = self.makemkv_table.item(row, 0)
            if item and item.data(Qt.UserRole) == key:
                return row
        return None

    @Slot()
    def _populate_tracks_for_selection(self) -> None:
        self._populate_tracks_for_row(self.files_table.currentRow())

    def _populate_tracks_for_row(self, row: int) -> None:
        if row < 0 or row >= len(self.current_reports):
            self._syncing_track_checks = True
            self.tracks_table.setRowCount(0)
            self._syncing_track_checks = False
            self._set_track_selection_controls_enabled(False)
            return

        report = self.current_reports[row]
        tracks = self._report_tracks(report)
        self._syncing_track_checks = True
        self.tracks_table.setRowCount(len(tracks))
        for track_row, track in enumerate(tracks):
            selection_key = self._track_selection_key(track)
            include_track = self.manual_track_includes.get(selection_key, not bool(track.get("drop")))
            track["drop"] = not include_track
            values = [
                "",
                track.get("id", ""),
                track.get("source_name", ""),
                self._track_type_label(track.get("type", "")),
                track.get("codec", ""),
                track.get("input_language", ""),
                track.get("output_language", ""),
                track.get("name", ""),
                self._yes_no(track.get("default")),
                self._yes_no(track.get("forced")),
                self._yes_no(track.get("drop")),
                track.get("role", ""),
                self._delay_text(track.get("delay_ms")),
                self._track_reason(track),
            ]
            for column, value in enumerate(values):
                item = QTableWidgetItem(str(value))
                if column == 0:
                    item.setFlags(
                        (item.flags() | Qt.ItemIsUserCheckable | Qt.ItemIsEnabled | Qt.ItemIsSelectable)
                        & ~Qt.ItemIsEditable
                    )
                    item.setCheckState(Qt.Checked if include_track else Qt.Unchecked)
                    item.setData(Qt.UserRole, selection_key)
                    item.setToolTip("Included in the remux" if include_track else "Excluded from the remux")
                else:
                    item.setFlags(item.flags() & ~Qt.ItemIsEditable)
                tooltips = []
                if column == 7 and track.get("original_name"):
                    tooltips.append(f"Original: {track['original_name']}")
                if track.get("duplicate_reason"):
                    tooltips.append(str(track["duplicate_reason"]))
                if tooltips:
                    item.setToolTip("\n".join(tooltips))
                self._style_track_item(item, track)
                self.tracks_table.setItem(track_row, column, item)
        self._syncing_track_checks = False
        self.tracks_table.resizeColumnsToContents()
        self._set_track_selection_controls_enabled(bool(tracks))

    def _report_tracks(self, report: dict) -> list[dict]:
        tracks = report.get("tracks", {})
        report_tracks = [
            *tracks.get("video", []),
            *tracks.get("audio", []),
            *tracks.get("subtitles", []),
        ]
        if not self.manual_track_order:
            return report_tracks

        manual_rank = {selection_key: index for index, selection_key in enumerate(self.manual_track_order)}

        def sort_key(item: tuple[int, dict]) -> tuple[int, int]:
            index, track = item
            selection_key = self._track_selection_key(track)
            if selection_key in manual_rank:
                return (0, manual_rank[selection_key])
            return (1, index)

        return [track for _index, track in sorted(enumerate(report_tracks), key=sort_key)]

    def _track_selection_key(self, track: dict) -> str:
        existing_key = str(track.get("selection_key") or "")
        if existing_key:
            return existing_key
        return organizer.track_selection_key(
            int(track.get("source_index") or 0),
            str(track.get("type") or ""),
            int(track.get("id") or 0),
        )

    def _current_track_rows(self) -> list[dict]:
        row = self.files_table.currentRow()
        if row < 0 or row >= len(self.current_reports):
            return []
        return self._report_tracks(self.current_reports[row])

    @Slot(QTableWidgetItem)
    def _track_item_changed(self, item: QTableWidgetItem) -> None:
        if self._syncing_track_checks or item.column() != 0:
            return
        selection_key = str(item.data(Qt.UserRole) or "")
        if not selection_key:
            return
        include_track = item.checkState() == Qt.Checked
        self.manual_track_includes[selection_key] = include_track

        tracks = self._current_track_rows()
        if 0 <= item.row() < len(tracks):
            track = tracks[item.row()]
            track["drop"] = not include_track
            drop_item = self.tracks_table.item(item.row(), 10)
            if drop_item:
                drop_item.setText(self._yes_no(track.get("drop")))
            item.setToolTip("Included in the remux" if include_track else "Excluded from the remux")

    @Slot(list, int)
    def _track_rows_reordered(self, selected_rows: list[int], target_row: int) -> None:
        current_order = self._track_order_keys_from_table()
        if not current_order:
            return

        valid_rows = sorted({row for row in selected_rows if 0 <= row < len(current_order)})
        if not valid_rows:
            return

        moving = [current_order[row] for row in valid_rows]
        moving_row_set = set(valid_rows)
        remaining = [key for row, key in enumerate(current_order) if row not in moving_row_set]
        insert_row = target_row - sum(1 for row in valid_rows if row < target_row)
        insert_row = max(0, min(insert_row, len(remaining)))
        self.manual_track_order = remaining[:insert_row] + moving + remaining[insert_row:]

        current_file_row = self.files_table.currentRow()
        self._populate_tracks_for_row(current_file_row)
        self.tracks_table.clearSelection()
        for row in range(insert_row, min(insert_row + len(moving), self.tracks_table.rowCount())):
            self.tracks_table.selectRow(row)
        self.statusBar().showMessage("Track order updated")

    def _sync_track_order_from_table(self) -> None:
        self.manual_track_order = self._track_order_keys_from_table()

    def _track_order_keys_from_table(self) -> list[str]:
        ordered_keys: list[str] = []
        for row in range(self.tracks_table.rowCount()):
            item = self.tracks_table.item(row, 0)
            selection_key = str(item.data(Qt.UserRole) or "") if item else ""
            if selection_key:
                ordered_keys.append(selection_key)
        return ordered_keys

    @Slot()
    def select_all_tracks(self) -> None:
        self._set_displayed_track_checks(Qt.Checked)

    @Slot()
    def deselect_duplicate_tracks(self) -> None:
        self._set_displayed_track_checks(Qt.Unchecked, duplicate_members_only=True)

    def _set_displayed_track_checks(self, check_state: Qt.CheckState, duplicate_members_only: bool = False) -> None:
        tracks = self._current_track_rows()
        for row, track in enumerate(tracks):
            if duplicate_members_only and track.get("duplicate_of_id") is None:
                continue
            item = self.tracks_table.item(row, 0)
            if item:
                item.setCheckState(check_state)

    def _set_track_selection_controls_enabled(self, enabled: bool) -> None:
        self.track_select_all_button.setEnabled(enabled)
        self.track_deselect_duplicates_button.setEnabled(
            enabled and any(track.get("duplicate_of_id") is not None for track in self._current_track_rows())
        )

    def _track_type_label(self, track_type: str) -> str:
        if track_type == "subtitles":
            return "Subtitle"
        return track_type.title() if track_type else ""

    def _track_reason(self, track: dict) -> str:
        reasons = [track.get("duplicate_reason") or "", track.get("role_reason") or ""]
        reason_text = " | ".join(reason for reason in reasons if reason)
        if reason_text:
            return reason_text
        scores = track.get("role_scores") or {}
        score_parts = [f"{name}:{score}" for name, score in scores.items() if score]
        return ", ".join(score_parts)

    def _style_track_item(self, item: QTableWidgetItem, track: dict) -> None:
        if track.get("duplicate_group"):
            if self.current_theme == "light":
                item.setBackground(QColor("#ffe4e6"))
                item.setForeground(QColor("#9f1239"))
            else:
                item.setBackground(QColor("#3a1f27"))
                item.setForeground(QColor("#ffb4bd"))
            return

        if track.get("drop"):
            item.setForeground(QColor("#64748b") if self.current_theme == "light" else QColor("#94a3b8"))

    def _yes_no(self, value) -> str:
        return "Yes" if value else ""

    def _delay_text(self, value) -> str:
        try:
            delay_ms = int(value or 0)
        except (TypeError, ValueError):
            return ""
        return f"{delay_ms:+d} ms" if delay_ms else ""

    def _set_running(self, running: bool) -> None:
        if self.organizer_clear_button:
            self.organizer_clear_button.setEnabled(not running)
        if self.organizer_reset_button:
            self.organizer_reset_button.setEnabled(not running)
        self.check_tools_button.setEnabled(not running)
        self.preview_button.setEnabled(not running)
        self.run_button.setEnabled(not running)
        self.cancel_button.setEnabled(running)
        self.makemkv_check_button.setEnabled(not running)
        self.makemkv_preview_button.setEnabled(not running)
        self.makemkv_run_button.setEnabled(not running)
        if self.makemkv_clear_button:
            self.makemkv_clear_button.setEnabled(not running)
        if self.makemkv_reset_button:
            self.makemkv_reset_button.setEnabled(not running)
        self.audio_sync_check_button.setEnabled(not running)
        self.audio_sync_load_button.setEnabled(not running)
        self.audio_sync_analyze_button.setEnabled(not running)
        if self.audio_sync_clear_button:
            self.audio_sync_clear_button.setEnabled(not running)
        if self.audio_sync_reset_button:
            self.audio_sync_reset_button.setEnabled(not running)
        self.audio_sync_apply_organizer_button.setEnabled(bool(self.audio_sync_result) and not running)
        self.audio_sync_export_button.setEnabled(bool(self.audio_sync_result) and not running)
        self._set_audio_sync_selection_controls_enabled(self.audio_sync_tracks_table.rowCount() > 0 and not running)
        self._set_track_selection_controls_enabled(self.tracks_table.rowCount() > 0 and not running)

    def _set_makemkv_running(self, running: bool) -> None:
        if self.organizer_clear_button:
            self.organizer_clear_button.setEnabled(not running)
        if self.organizer_reset_button:
            self.organizer_reset_button.setEnabled(not running)
        self.makemkv_check_button.setEnabled(not running)
        self.makemkv_preview_button.setEnabled(not running)
        self.makemkv_run_button.setEnabled(not running)
        if self.makemkv_clear_button:
            self.makemkv_clear_button.setEnabled(not running)
        if self.makemkv_reset_button:
            self.makemkv_reset_button.setEnabled(not running)
        self.makemkv_cancel_button.setEnabled(running)
        self.check_tools_button.setEnabled(not running)
        self.preview_button.setEnabled(not running)
        self.run_button.setEnabled(not running)
        self.audio_sync_check_button.setEnabled(not running)
        self.audio_sync_load_button.setEnabled(not running)
        self.audio_sync_analyze_button.setEnabled(not running)
        if self.audio_sync_clear_button:
            self.audio_sync_clear_button.setEnabled(not running)
        if self.audio_sync_reset_button:
            self.audio_sync_reset_button.setEnabled(not running)
        self.audio_sync_apply_organizer_button.setEnabled(bool(self.audio_sync_result) and not running)
        self.audio_sync_export_button.setEnabled(bool(self.audio_sync_result) and not running)
        self._set_audio_sync_selection_controls_enabled(self.audio_sync_tracks_table.rowCount() > 0 and not running)
        self._set_track_selection_controls_enabled(self.tracks_table.rowCount() > 0 and not running)

    def _set_audio_sync_running(self, running: bool) -> None:
        if self.organizer_clear_button:
            self.organizer_clear_button.setEnabled(not running)
        if self.organizer_reset_button:
            self.organizer_reset_button.setEnabled(not running)
        self.audio_sync_check_button.setEnabled(not running)
        self.audio_sync_load_button.setEnabled(not running)
        self.audio_sync_analyze_button.setEnabled(not running)
        if self.audio_sync_clear_button:
            self.audio_sync_clear_button.setEnabled(not running)
        if self.audio_sync_reset_button:
            self.audio_sync_reset_button.setEnabled(not running)
        self.audio_sync_apply_organizer_button.setEnabled(bool(self.audio_sync_result) and not running)
        self.audio_sync_export_button.setEnabled(bool(self.audio_sync_result) and not running)
        self._set_audio_sync_selection_controls_enabled(self.audio_sync_tracks_table.rowCount() > 0 and not running)
        self.audio_sync_cancel_button.setEnabled(running)
        self.check_tools_button.setEnabled(not running)
        self.preview_button.setEnabled(not running)
        self.run_button.setEnabled(not running)
        self.makemkv_check_button.setEnabled(not running)
        self.makemkv_preview_button.setEnabled(not running)
        self.makemkv_run_button.setEnabled(not running)
        if self.makemkv_clear_button:
            self.makemkv_clear_button.setEnabled(not running)
        if self.makemkv_reset_button:
            self.makemkv_reset_button.setEnabled(not running)
        self._set_track_selection_controls_enabled(self.tracks_table.rowCount() > 0 and not running)

    def _workflow_is_running(self) -> bool:
        return bool(
            (self.worker_thread and self.worker_thread.isRunning())
            or (self.makemkv_worker_thread and self.makemkv_worker_thread.isRunning())
            or (self.audio_sync_worker_thread and self.audio_sync_worker_thread.isRunning())
        )

    def _other_workflow_is_running(self) -> bool:
        return self._workflow_is_running()

    def _paths_from_mime(self, mime_data) -> list[Path]:
        if not mime_data.hasUrls():
            return []
        paths = []
        for url in mime_data.urls():
            if url.isLocalFile():
                paths.append(Path(url.toLocalFile()))
        return paths

    def _is_supported_audio_sync_media_path(self, path: Path) -> bool:
        return path.is_file() and path.suffix.lower() in self.AUDIO_SYNC_MEDIA_SUFFIXES

    def _audio_sync_drop_target(self, watched) -> QLineEdit | None:
        if watched is self.audio_sync_reference_edit:
            return self.audio_sync_reference_edit
        if watched is self.audio_sync_source_edit:
            return self.audio_sync_source_edit
        return None

    def _accepts_audio_sync_drop_event(self, event) -> bool:
        paths = self._paths_from_mime(event.mimeData())
        if not any(self._is_supported_audio_sync_media_path(path) for path in paths):
            return False
        event.acceptProposedAction()
        return True

    def _handle_audio_sync_drop_event(self, event, target_edit: QLineEdit) -> bool:
        paths = self._paths_from_mime(event.mimeData())
        supported_paths = [path for path in paths if self._is_supported_audio_sync_media_path(path)]
        if not supported_paths:
            return False
        event.acceptProposedAction()
        self._set_audio_sync_media_path(target_edit, supported_paths[0])
        return True

    def _accepts_drop_event(self, event) -> bool:
        paths = self._paths_from_mime(event.mimeData())
        if not any(self._is_supported_input_path(path) for path in paths):
            return False
        event.acceptProposedAction()
        return True

    def _handle_drop_event(self, event) -> bool:
        paths = self._paths_from_mime(event.mimeData())
        supported_paths = [path for path in paths if self._is_supported_input_path(path)]
        if not supported_paths:
            return False
        event.acceptProposedAction()
        self.add_input_paths(supported_paths)
        return True

    def eventFilter(self, watched, event) -> bool:
        audio_sync_target = self._audio_sync_drop_target(watched)
        if audio_sync_target is not None:
            if event.type() in {QEvent.Type.DragEnter, QEvent.Type.DragMove}:
                if self._accepts_audio_sync_drop_event(event):
                    return True
            if event.type() == QEvent.Type.Drop:
                if self._handle_audio_sync_drop_event(event, audio_sync_target):
                    return True
        if event.type() in {QEvent.Type.DragEnter, QEvent.Type.DragMove}:
            if self._accepts_drop_event(event):
                return True
        if event.type() == QEvent.Type.Drop:
            if self._handle_drop_event(event):
                return True
        return super().eventFilter(watched, event)

    def dragEnterEvent(self, event: QDragEnterEvent) -> None:
        if not self._accepts_drop_event(event):
            super().dragEnterEvent(event)

    def dragMoveEvent(self, event: QDragEnterEvent) -> None:
        if not self._accepts_drop_event(event):
            super().dragMoveEvent(event)

    def dropEvent(self, event: QDropEvent) -> None:
        if not self._handle_drop_event(event):
            super().dropEvent(event)

    def closeEvent(self, event: QCloseEvent) -> None:
        organizer_running = self.worker_thread and self.worker_thread.isRunning()
        makemkv_running = self.makemkv_worker_thread and self.makemkv_worker_thread.isRunning()
        audio_sync_running = self.audio_sync_worker_thread and self.audio_sync_worker_thread.isRunning()
        if organizer_running or makemkv_running or audio_sync_running:
            answer = QMessageBox.question(
                self,
                "Close",
                "A run is still active. Close anyway?",
                QMessageBox.Yes | QMessageBox.No,
                QMessageBox.No,
            )
            if answer != QMessageBox.Yes:
                event.ignore()
                return
        event.accept()


def main() -> int:
    app = QApplication(sys.argv)
    window = MainWindow()
    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
