from __future__ import annotations

import contextlib
import ctypes
import io
import json
import os
import sys
import threading
import time
import traceback
import uuid
from pathlib import Path

try:
    from PySide6.QtCore import QEvent, QObject, QThread, QTimer, Qt, Signal, Slot
    from PySide6.QtGui import (
        QAction,
        QBrush,
        QColor,
        QCloseEvent,
        QDragEnterEvent,
        QDropEvent,
        QFont,
        QTextCharFormat,
        QTextCursor,
    )
    from PySide6.QtWidgets import (
        QApplication,
        QAbstractItemView,
        QCheckBox,
        QComboBox,
        QDialog,
        QDialogButtonBox,
        QDoubleSpinBox,
        QFileDialog,
        QFormLayout,
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
from app_metadata import APP_DESCRIPTION, APP_NAME, APP_VERSION, DOCUMENTATION_URL, ISSUES_URL


def gui_profile_store_path() -> Path:
    appdata = os.environ.get("APPDATA")
    base_dir = Path(appdata) if appdata else Path.home() / ".config"
    return base_dir / "MKV Track Organizer" / "profiles.json"


def set_windows_app_user_model_id() -> None:
    if sys.platform != "win32":
        return
    try:
        setter = ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID
        setter.argtypes = [ctypes.c_wchar_p]
        setter.restype = ctypes.c_long
        setter("Pessegos.MKVTrackOrganizer")
    except (OSError, AttributeError):
        pass


class WindowsTaskbarProgress:
    NO_PROGRESS = 0
    INDETERMINATE = 1
    NORMAL = 2

    class GUID(ctypes.Structure):
        _fields_ = [
            ("Data1", ctypes.c_uint32),
            ("Data2", ctypes.c_uint16),
            ("Data3", ctypes.c_uint16),
            ("Data4", ctypes.c_ubyte * 8),
        ]

        @classmethod
        def from_text(cls, value: str) -> "WindowsTaskbarProgress.GUID":
            return cls.from_buffer_copy(uuid.UUID(value).bytes_le)

    def __init__(self, window: QMainWindow) -> None:
        self.window = window
        self.interface = ctypes.c_void_p()
        self.available = False
        self.error = ""
        self._ole32 = None
        self._com_initialized = False
        if sys.platform != "win32" or QApplication.platformName().casefold() != "windows":
            return
        try:
            self._initialize()
        except (OSError, ValueError, AttributeError) as error:
            self.error = str(error)
            self.close()

    def _initialize(self) -> None:
        self._ole32 = ctypes.OleDLL("ole32")
        initialize_result = self._ole32.CoInitialize(None)
        self._com_initialized = initialize_result in {0, 1}
        clsid = self.GUID.from_text("56FDF344-FD6D-11D0-958A-006097C9A090")
        iid = self.GUID.from_text("EA1AFB91-9E28-4B86-90E9-9E9F8A5EEA84")
        self._ole32.CoCreateInstance.argtypes = [
            ctypes.POINTER(self.GUID),
            ctypes.c_void_p,
            ctypes.c_uint32,
            ctypes.POINTER(self.GUID),
            ctypes.POINTER(ctypes.c_void_p),
        ]
        self._ole32.CoCreateInstance.restype = ctypes.c_long
        result = self._ole32.CoCreateInstance(
            ctypes.byref(clsid),
            None,
            1,
            ctypes.byref(iid),
            ctypes.byref(self.interface),
        )
        if result < 0 or not self.interface.value:
            raise OSError(f"Could not initialize Windows taskbar progress: HRESULT {result:#x}")

        initialize = self._method(3)
        if initialize(self.interface) < 0:
            raise OSError("Could not initialize ITaskbarList3")
        self.available = True

    def _method(self, index: int, *argument_types):
        vtable = ctypes.cast(
            self.interface,
            ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p)),
        ).contents
        function_type = ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_void_p, *argument_types)
        return function_type(vtable[index])

    def _window_handle(self) -> ctypes.c_void_p:
        return ctypes.c_void_p(int(self.window.winId()))

    def set_indeterminate(self) -> None:
        self._set_state(self.INDETERMINATE)

    def set_value(self, maximum: int, value: int) -> None:
        if not self.available:
            return
        maximum = max(1, int(maximum))
        value = max(0, min(int(value), maximum))
        self._set_state(self.NORMAL)
        set_value = self._method(9, ctypes.c_void_p, ctypes.c_ulonglong, ctypes.c_ulonglong)
        set_value(self.interface, self._window_handle(), value, maximum)

    def clear(self) -> None:
        self._set_state(self.NO_PROGRESS)

    def _set_state(self, state: int) -> None:
        if not self.available:
            return
        set_state = self._method(10, ctypes.c_void_p, ctypes.c_uint32)
        set_state(self.interface, self._window_handle(), state)

    def close(self) -> None:
        if self.interface.value:
            try:
                self.clear()
                release = self._method(2)
                release(self.interface)
            except (OSError, ValueError, AttributeError):
                pass
        self.interface = ctypes.c_void_p()
        self.available = False
        if self._com_initialized and self._ole32 is not None:
            self._ole32.CoUninitialize()
        self._com_initialized = False
        self._ole32 = None


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


class AudioSyncProbeWorker(QObject):
    completed = Signal(object, object, object, object)
    failed = Signal(str)

    def __init__(self, reference_path: Path, source_path: Path) -> None:
        super().__init__()
        self.reference_path = reference_path
        self.source_path = source_path

    @Slot()
    def run(self) -> None:
        try:
            reference_probe = audio_sync.probe_media(self.reference_path)
            source_probe = audio_sync.probe_media(self.source_path)
            self.completed.emit(self.reference_path, self.source_path, reference_probe, source_probe)
        except Exception:
            self.failed.emit(traceback.format_exc())


class MainWindow(QMainWindow):
    PROFILE_NONE_LABEL = "Custom"
    PROFILE_STORE_VERSION = 2
    PROFILE_FIELDS = (
        "output_suffix",
        "existing_output_mode",
        "merge_inputs",
        "metadata_edit_mode",
        "audio_name_style",
        "language_order_style",
        "regional_order",
        "custom_language_order",
        "report_format",
        "smart_sub_detection",
        "drop_empty_subs",
        "detect_duplicate_tracks",
        "detect_subtitle_language_duplicates",
        "disable_track_statistics_tags",
        "detect_language_variants",
        "auto_pgs_ocr",
        "auto_commentary_ocr",
        "report",
        "preserve_commentary_names",
        "preferred_language",
        "preferred_audio_first",
        "preferred_audio_default",
        "preferred_subtitle_first",
        "preferred_forced_subtitle_default",
    )
    PROFILE_BOOL_FIELDS = {
        "merge_inputs",
        "smart_sub_detection",
        "drop_empty_subs",
        "detect_duplicate_tracks",
        "detect_subtitle_language_duplicates",
        "disable_track_statistics_tags",
        "detect_language_variants",
        "auto_pgs_ocr",
        "auto_commentary_ocr",
        "report",
        "preserve_commentary_names",
        "preferred_audio_first",
        "preferred_audio_default",
        "preferred_subtitle_first",
        "preferred_forced_subtitle_default",
    }
    PROFILE_ENUM_FIELDS = {
        "existing_output_mode": {"stop", "overwrite", "skip"},
        "metadata_edit_mode": organizer.METADATA_EDIT_MODES,
        "audio_name_style": organizer.AUDIO_NAME_STYLES,
        "language_order_style": organizer.LANGUAGE_ORDER_STYLES,
        "report_format": {"json", "txt", "both"},
    }
    FILE_COLUMNS = ["Status", "Input", "Output", "Message"]
    MAKEMKV_COLUMNS = ["Status", "Source", "Output", "Message"]
    AUDIO_SYNC_COLUMNS = ["Export", "Type", "Index", "Codec", "Language", "Title"]
    AUDIO_SYNC_MEDIA_SUFFIXES = {".mkv", ".mka", ".mp4", ".mov", ".avi", ".flac", ".wav", ".aac", ".ac3", ".dts"}
    AUDIO_SYNC_CUSTOM_PRESET = "custom"
    AUDIO_SYNC_ANALYSIS_PRESETS = (
        ("Full timeline - Recommended", "full"),
        ("Balanced", "balanced"),
        ("Quick check", "quick"),
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
        "Flags",
        "Delay",
        "Plan",
    ]
    TRACK_INCLUDE_COLUMN = TRACK_COLUMNS.index("Include")
    TRACK_NAME_COLUMN = TRACK_COLUMNS.index("Name")
    TRACK_FLAGS_COLUMN = TRACK_COLUMNS.index("Flags")
    TRACK_PLAN_COLUMN = TRACK_COLUMNS.index("Plan")
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
            "verification-failed": ("#fdecec", "#b42318"),
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
            "verification-failed": ("#4a1d21", "#fca5a5"),
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
        "custom": "Uses the active language-code order shown below and saved with Organizer profiles.",
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
        self.setWindowTitle(APP_NAME)
        self.resize(1400, 900)
        self.setAcceptDrops(True)

        self.worker_thread: QThread | None = None
        self.worker: OrganizerWorker | None = None
        self.makemkv_worker_thread: QThread | None = None
        self.makemkv_worker: MakeMkvWorker | None = None
        self.audio_sync_worker_thread: QThread | None = None
        self.audio_sync_worker: AudioSyncWorker | AudioSyncExportWorker | None = None
        self.audio_sync_probe_thread: QThread | None = None
        self.audio_sync_probe_worker: AudioSyncProbeWorker | None = None
        self.audio_sync_stream_paths: tuple[Path, Path] | None = None
        self.audio_sync_probe_automatic = True
        self.audio_sync_probe_retry_after_finish = False
        self.start_audio_sync_analysis_after_probe = False
        self.audio_sync_auto_load_timer = QTimer(self)
        self.audio_sync_auto_load_timer.setSingleShot(True)
        self.progress_timer = QTimer(self)
        self.progress_timer.setInterval(1000)
        self.progress_timer.timeout.connect(self._refresh_progress_label)
        self._progress_started_at: float | None = None
        self._progress_scope = ""
        self._progress_activity = "Idle"
        self._progress_index = 0
        self._progress_total = 0
        self._progress_finished_elapsed: float | None = None
        self._log_line_starts: dict[int, bool] = {}
        self._output_follow_by_edit: dict[int, QCheckBox] = {}
        self._output_controls: dict[int, dict[str, object]] = {}
        self.default_args, self.default_config_path = self._load_default_args()
        self.profile_store_path = gui_profile_store_path()
        self.profiles: dict[str, dict] = {}
        self.last_profile_name = ""
        self.saved_theme = "dark"
        self._loaded_profile_name = ""
        self._applying_profile = False
        self._applying_config = False
        self._profile_store_needs_migration = False
        self._profile_default_payload: dict[str, object] = {}
        self._config_baseline: dict[str, object] = {}
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
        self.manual_track_order_active = False

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
        self.subtitle_language_duplicates_check = QCheckBox("Subtitle lang duplicates")
        self.disable_track_statistics_tags_check = QCheckBox("Skip track tags")
        self.variant_check = QCheckBox("Detect language variants")
        self.auto_pgs_ocr_check = QCheckBox("Auto PGS OCR")
        self.auto_commentary_ocr_check = QCheckBox("Commentary/SDH OCR")
        self.report_check = QCheckBox("Write report")
        self.preserve_commentary_names_check = QCheckBox("Keep commentary names")
        self.preferred_audio_first_check = QCheckBox("Audio first")
        self.preferred_audio_default_check = QCheckBox("Audio default")
        self.preferred_subtitle_first_check = QCheckBox("Subtitles first")
        self.preferred_forced_subtitle_default_check = QCheckBox("Forced subs default")

        self.profile_combo = QComboBox()
        self.profile_combo.setToolTip("Saved Organizer option profile")
        self.update_profile_button = QPushButton("Save changes")
        self.save_profile_button = QPushButton("Save as")
        self.revert_profile_button = QPushButton("Revert")
        self.delete_profile_button = QPushButton("Delete")
        self.profile_status_label = QLabel("Custom settings")
        self.update_profile_button.setObjectName("secondaryButton")
        self.save_profile_button.setObjectName("secondaryButton")
        self.revert_profile_button.setObjectName("secondaryButton")
        self.delete_profile_button.setObjectName("secondaryButton")
        self.update_profile_button.setToolTip("Save the current Organizer options into the selected profile")
        self.save_profile_button.setToolTip("Save the current Organizer options as a new profile or replace an existing one")
        self.revert_profile_button.setToolTip("Discard unsaved changes and reload the selected profile")
        self.delete_profile_button.setToolTip("Delete the selected saved profile")

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
        self.language_order_style_combo.addItem("Custom", "custom")
        self.regional_order_combo = QComboBox()
        self.regional_order_combo.addItem("Europe first", "europe,americas,asia,oceania,middle-east-africa")
        self.regional_order_combo.addItem("Americas first", "americas,europe,asia,oceania,middle-east-africa")
        self.regional_order_combo.addItem("Asia first", "asia,europe,americas,oceania,middle-east-africa")
        self.regional_order_combo.addItem("Oceania first", "oceania,europe,americas,asia,middle-east-africa")
        self.regional_order_combo.addItem(
            "Middle East/Africa first",
            "middle-east-africa,europe,americas,asia,oceania",
        )
        self.custom_language_order_edit = QLineEdit()
        self.custom_language_order_edit.setPlaceholderText("eng, pt-PT, por, es-ES, es-419, fr-FR, fr-CA")
        self.report_format_combo = QComboBox()
        self.report_format_combo.addItems(["both", "json", "txt"])
        self.existing_output_combo = QComboBox()
        self.existing_output_combo.addItem("Stop if exists", "stop")
        self.existing_output_combo.addItem("Overwrite", "overwrite")
        self.existing_output_combo.addItem("Skip existing", "skip")
        self.config_custom_language_order_edit = QLineEdit()
        self.config_custom_language_order_edit.setPlaceholderText("eng, pt-PT, por, es-ES, es-419, fr-FR, fr-CA")
        self.config_use_custom_order_check = QCheckBox("Use custom order by default")
        self.config_path_label = QLabel(str(organizer.DEFAULT_CONFIG_PATH))
        self.config_status_label = QLabel("")
        self.config_reload_button = QPushButton("Reload")
        self.config_save_button = QPushButton("Save config")
        self.config_apply_button = QPushButton("Use in Organizer")
        self.config_reset_button = QPushButton("Reset defaults")
        self.profile_store_path_label = QLabel(str(self.profile_store_path))
        self.profile_library_status_label = QLabel("")
        self.profile_import_button = QPushButton("Import")
        self.profile_export_button = QPushButton("Export")
        self.config_reload_button.setObjectName("secondaryButton")
        self.config_save_button.setObjectName("primaryButton")
        self.config_apply_button.setObjectName("secondaryButton")
        self.config_reset_button.setObjectName("secondaryButton")
        self.profile_import_button.setObjectName("secondaryButton")
        self.profile_export_button.setObjectName("secondaryButton")
        self.config_reset_button.setToolTip("Restore the built-in values in this Config panel")
        self.profile_store_path_label.setToolTip("Per-user file containing saved Organizer profiles")
        self.profile_import_button.setToolTip("Import profiles from a profile library JSON file")
        self.profile_export_button.setToolTip("Export all saved profiles to a JSON file")

        self.advanced_button = QToolButton()
        self.advanced_panel = QWidget()
        self.check_tools_button = QPushButton("Check tools")
        self.preview_button = QPushButton("Preview")
        self.run_button = QPushButton("Run")
        self.cancel_button = QPushButton("Cancel")
        self.track_select_all_button = QPushButton("Select all")
        self.track_select_audio_button = QPushButton("Select audio")
        self.track_select_subtitles_button = QPushButton("Select subs")
        self.track_include_selected_button = QPushButton("Include selected")
        self.track_exclude_selected_button = QPushButton("Exclude selected")
        self.track_deselect_duplicates_button = QPushButton("Drop duplicates")
        self.track_deselect_duplicate_audio_button = QPushButton("Drop dup audio")
        self.track_deselect_duplicate_subtitles_button = QPushButton("Drop dup subs")
        self.track_deselect_probable_duplicates_button = QPushButton("Drop probable")
        self.track_reset_selection_button = QPushButton("Reset selection")
        self.track_reset_order_button = QPushButton("Reset order")
        self.track_reset_button = QPushButton("Reset all")
        self.track_status_label = QLabel("No preview")
        self.track_status_label.setObjectName("trackStatusLabel")
        self.organizer_clear_button: QToolButton | None = None
        self.organizer_reset_button: QToolButton | None = None
        self.check_tools_button.setObjectName("secondaryButton")
        self.preview_button.setObjectName("secondaryButton")
        self.run_button.setObjectName("primaryButton")
        self.cancel_button.setObjectName("dangerButton")
        self.track_select_all_button.setObjectName("secondaryButton")
        self.track_select_audio_button.setObjectName("secondaryButton")
        self.track_select_subtitles_button.setObjectName("secondaryButton")
        self.track_include_selected_button.setObjectName("secondaryButton")
        self.track_exclude_selected_button.setObjectName("secondaryButton")
        self.track_deselect_duplicates_button.setObjectName("secondaryButton")
        self.track_deselect_duplicate_audio_button.setObjectName("secondaryButton")
        self.track_deselect_duplicate_subtitles_button.setObjectName("secondaryButton")
        self.track_deselect_probable_duplicates_button.setObjectName("secondaryButton")
        self.track_reset_selection_button.setObjectName("secondaryButton")
        self.track_reset_order_button.setObjectName("secondaryButton")
        self.track_reset_button.setObjectName("secondaryButton")
        self.track_select_all_button.setToolTip("Include every displayed track")
        self.track_select_audio_button.setToolTip("Include every displayed audio track")
        self.track_select_subtitles_button.setToolTip("Include every displayed subtitle track")
        self.track_include_selected_button.setToolTip("Include the selected track rows")
        self.track_exclude_selected_button.setToolTip("Exclude the selected track rows from the next run")
        self.track_deselect_duplicates_button.setToolTip("Uncheck duplicate-group members and keep each group leader")
        self.track_deselect_duplicate_audio_button.setToolTip("Uncheck duplicate audio-group members")
        self.track_deselect_duplicate_subtitles_button.setToolTip("Uncheck duplicate subtitle-group members")
        self.track_deselect_probable_duplicates_button.setToolTip(
            "Uncheck probable regional duplicate members and keep each suggested group leader"
        )
        self.track_reset_selection_button.setToolTip("Restore the preview include/exclude state")
        self.track_reset_order_button.setToolTip("Restore the automatic preview track order")
        self.track_reset_button.setToolTip("Restore the preview include state and automatic track order")
        self.track_select_all_button.setEnabled(False)
        self.track_select_audio_button.setEnabled(False)
        self.track_select_subtitles_button.setEnabled(False)
        self.track_include_selected_button.setEnabled(False)
        self.track_exclude_selected_button.setEnabled(False)
        self.track_deselect_duplicates_button.setEnabled(False)
        self.track_deselect_duplicate_audio_button.setEnabled(False)
        self.track_deselect_duplicate_subtitles_button.setEnabled(False)
        self.track_deselect_probable_duplicates_button.setEnabled(False)
        self.track_reset_selection_button.setEnabled(False)
        self.track_reset_order_button.setEnabled(False)
        self.track_reset_button.setEnabled(False)
        self.files_table = QTableWidget(0, len(self.FILE_COLUMNS))
        self.results_table = self.files_table
        self.tracks_table = TrackTableWidget(0, len(self.TRACK_COLUMNS))
        self.track_details_edit = QPlainTextEdit()
        self.track_details_edit.setObjectName("trackDetails")
        self.track_details_edit.setPlaceholderText("Select a track to inspect its plan, source, flags, and reasons.")
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
        self.audio_sync_analysis_combo = QComboBox()
        for label, mode in self.AUDIO_SYNC_ANALYSIS_PRESETS:
            self.audio_sync_analysis_combo.addItem(label, mode)
        self.audio_sync_analysis_plan_label = QLabel("Duration will be detected after loading both files.")
        self.audio_sync_analysis_plan_label.setObjectName("audioSyncPlan")
        self.audio_sync_analysis_plan_label.setMinimumHeight(30)
        self.audio_sync_analysis_plan_label.setAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        self.audio_sync_analysis_plan_label.setWordWrap(True)
        self.audio_sync_reference_duration_seconds: float | None = None
        self.audio_sync_source_duration_seconds: float | None = None
        self.audio_sync_custom_duration_seconds = 120.0
        self.audio_sync_custom_spacing_seconds = 900.0
        self.audio_sync_custom_start_seconds = 600.0
        self.audio_sync_custom_max_offset_seconds = 5.0
        self.audio_sync_custom_checkpoints = 8
        self.audio_sync_previous_analysis_index = self.audio_sync_analysis_combo.currentIndex()
        self._audio_sync_preset_prompt_active = False
        self.audio_sync_check_button = QPushButton("Check tools")
        self.audio_sync_analyze_button = QPushButton("Analyze")
        self.audio_sync_apply_organizer_button = QPushButton("Apply delay in Organizer")
        self.audio_sync_export_button = QPushButton("Export shifted .mka")
        self.audio_sync_select_all_button = QPushButton("Select all")
        self.audio_sync_clear_selection_button = QPushButton("Clear selection")
        self.audio_sync_clear_button: QToolButton | None = None
        self.audio_sync_reset_button: QToolButton | None = None
        self.audio_sync_cancel_button = QPushButton("Cancel")
        self.audio_sync_check_button.setObjectName("secondaryButton")
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
        self._taskbar_progress = WindowsTaskbarProgress(self)
        self._apply_theme()
        self._apply_default_args(self.default_args)
        self._profile_default_payload = self._profile_payload_from_ui()
        self._load_profile_store()
        self._apply_theme(self.saved_theme)
        self._refresh_profile_combo(self.last_profile_name)
        if self.last_profile_name in self.profiles:
            self._apply_profile_payload(self.profiles[self.last_profile_name])
            self._loaded_profile_name = self.last_profile_name
        self._capture_config_baseline()
        self._update_profile_state()
        self._update_profile_library_status()
        if self._profile_store_needs_migration and self._write_profile_store():
            self.append_summary_line("Saved profile library migration to schema v2.")
        self._connect_signals()
        self._refresh_file_list()

    def _build_ui(self) -> None:
        style = self.style()
        help_menu = self.menuBar().addMenu("&Help")
        self.about_action = QAction(f"About {APP_NAME}", self)
        self.about_action.triggered.connect(self.show_about_dialog)
        help_menu.addAction(self.about_action)

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
        self.existing_output_combo.setToolTip(
            "Choose what happens when the target output file already exists. Stop is safest; Overwrite replaces it; Skip leaves it untouched."
        )
        self.subtitle_language_edit.setToolTip("Manual language override, for example spa:7,8; fr-CA:9")
        self.forced_ids_edit.setToolTip("Manual forced-subtitle override, for example 5,8,12")
        self.audio_delays_edit.setToolTip("Manual audio delays in milliseconds. Example: 1:150, 2:-250")
        self.subtitle_delays_edit.setToolTip("Manual subtitle delays in milliseconds. Example: 5:-250")
        self.preferred_language_edit.setToolTip("Language code used by the optional preferred-language rules, for example pt-PT")
        self.config_custom_language_order_edit.setToolTip(
            "App-wide default copied into the Organizer when requested."
        )
        self.custom_language_order_edit.setToolTip(
            "Active comma-separated language order saved with Organizer profiles."
        )
        self.config_use_custom_order_check.setToolTip("Use the custom order as the app-wide default")
        self.config_path_label.setToolTip("Config file used for app-wide defaults")
        self.config_reload_button.setToolTip("Reload the config file from disk")
        self.config_save_button.setToolTip("Save these app-wide defaults to the config file")
        self.config_apply_button.setToolTip("Copy this default order into the active Organizer settings")
        self.merge_inputs_check.setToolTip(
            "Mux selected Matroska inputs into one output. The first source with video supplies video; audio/subtitles come from all sources."
        )
        self.smart_subs_check.setToolTip("Automatically classify forced, empty, commentary, and SDH subtitles")
        self.drop_empty_check.setToolTip("Exclude subtitles classified as empty")
        self.duplicate_check.setToolTip("Highlight likely duplicate audio/subtitle tracks without dropping them")
        self.subtitle_language_duplicates_check.setToolTip(
            "Also mark subtitle tracks with the same language/role across formats; keeps ASS before PGS before SRT"
        )
        self.disable_track_statistics_tags_check.setToolTip(
            "Do not copy per-track tags from inputs and do not write mkvmerge statistics tags. "
            "This does not affect audio or subtitle delays."
        )
        self.variant_check.setToolTip("Automatically detect language variants such as es-ES vs es-419")
        self.auto_pgs_ocr_check.setToolTip("Run OCR for PGS subtitles when needed for language detection")
        self.auto_commentary_ocr_check.setToolTip("OCR extra full-size PGS tracks that may be commentary or SDH; normal and named SDH tracks are skipped")
        self.report_check.setToolTip("Write TXT/JSON batch reports")
        self.preserve_commentary_names_check.setToolTip(
            "Keep existing audio/subtitle commentary names, for example 'Commentary by Producer X', instead of rewriting them"
        )
        self.preferred_audio_first_check.setToolTip(
            "Move preferred-language main audio before other non-English main audio"
        )
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
        profile_label = QLabel("Profile")
        profile_label.setToolTip("Saved Organizer option profile.")
        audio_names_label = QLabel("Audio names")
        audio_names_label.setToolTip("Controls how audio track names are written.")
        language_order_label = QLabel("Language order")
        language_order_label.setToolTip("Controls how languages are sorted in the output.")
        regional_order_label = QLabel("Region order")
        regional_order_label.setToolTip("Controls region priority when Language order is Regional.")
        existing_output_label = QLabel("Existing output")
        existing_output_label.setToolTip("Controls what happens when the target output file already exists.")
        preferred_language_label = QLabel("Preferred language")
        preferred_language_label.setToolTip("Optional language code used by preferred-language rules.")

        profile_actions = QHBoxLayout()
        profile_actions.addWidget(self.update_profile_button)
        profile_actions.addWidget(self.save_profile_button)
        profile_actions.addWidget(self.revert_profile_button)
        profile_actions.addWidget(self.delete_profile_button)
        profile_actions.addStretch(1)
        profile_actions.addWidget(self.profile_status_label)

        advanced_layout.addWidget(profile_label, 0, 0)
        advanced_layout.addWidget(self.profile_combo, 0, 1)
        advanced_layout.addLayout(profile_actions, 0, 2, 1, 2)
        advanced_layout.addWidget(QLabel("Output suffix"), 1, 0)
        advanced_layout.addWidget(self.suffix_edit, 1, 1)
        advanced_layout.addWidget(metadata_label, 1, 2)
        advanced_layout.addWidget(self.metadata_combo, 1, 3)
        advanced_layout.addWidget(audio_names_label, 2, 0)
        advanced_layout.addWidget(self.audio_name_style_combo, 2, 1)
        advanced_layout.addWidget(QLabel("Report format"), 2, 2)
        advanced_layout.addWidget(self.report_format_combo, 2, 3)
        advanced_layout.addWidget(language_order_label, 3, 0)
        advanced_layout.addWidget(self.language_order_style_combo, 3, 1)
        advanced_layout.addWidget(regional_order_label, 3, 2)
        advanced_layout.addWidget(self.regional_order_combo, 3, 3)
        advanced_layout.addWidget(QLabel("Custom order"), 4, 0)
        advanced_layout.addWidget(self.custom_language_order_edit, 4, 1, 1, 3)
        advanced_layout.addWidget(QLabel("Language overrides"), 5, 0)
        advanced_layout.addWidget(self.subtitle_language_edit, 5, 1)
        advanced_layout.addWidget(QLabel("Forced IDs"), 5, 2)
        advanced_layout.addWidget(self.forced_ids_edit, 5, 3)
        advanced_layout.addWidget(QLabel("Audio delays"), 6, 0)
        advanced_layout.addWidget(self.audio_delays_edit, 6, 1)
        advanced_layout.addWidget(QLabel("Subtitle delays"), 6, 2)
        advanced_layout.addWidget(self.subtitle_delays_edit, 6, 3)
        advanced_layout.addWidget(preferred_language_label, 7, 0)
        advanced_layout.addWidget(self.preferred_language_edit, 7, 1)

        preferred_toggles = QHBoxLayout()
        for checkbox in [
            self.preferred_audio_first_check,
            self.preferred_audio_default_check,
            self.preferred_subtitle_first_check,
            self.preferred_forced_subtitle_default_check,
        ]:
            preferred_toggles.addWidget(checkbox)
        preferred_toggles.addStretch(1)
        advanced_layout.addLayout(preferred_toggles, 7, 2, 1, 2)
        advanced_layout.addWidget(existing_output_label, 8, 0)
        advanced_layout.addWidget(self.existing_output_combo, 8, 1)

        advanced_toggles = QHBoxLayout()
        for checkbox in [
            self.merge_inputs_check,
            self.smart_subs_check,
            self.drop_empty_check,
            self.duplicate_check,
            self.subtitle_language_duplicates_check,
            self.disable_track_statistics_tags_check,
            self.variant_check,
            self.auto_pgs_ocr_check,
            self.auto_commentary_ocr_check,
            self.preserve_commentary_names_check,
            self.report_check,
        ]:
            advanced_toggles.addWidget(checkbox)
        advanced_toggles.addStretch(1)
        advanced_layout.addLayout(advanced_toggles, 9, 0, 1, 4)
        self.advanced_panel.setVisible(False)
        root.addWidget(self.advanced_panel)

        files_group = QGroupBox("Files")
        files_layout = QVBoxLayout(files_group)
        files_layout.setContentsMargins(8, 8, 8, 8)
        files_group.setMinimumWidth(300)
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
        tracks_toolbar = QVBoxLayout()
        tracks_toolbar.setContentsMargins(0, 0, 0, 0)
        tracks_toolbar.setSpacing(6)
        track_selection_toolbar = QHBoxLayout()
        track_selection_toolbar.addWidget(self.track_select_all_button)
        track_selection_toolbar.addWidget(self.track_select_audio_button)
        track_selection_toolbar.addWidget(self.track_select_subtitles_button)
        track_selection_toolbar.addSpacing(8)
        track_selection_toolbar.addWidget(self.track_include_selected_button)
        track_selection_toolbar.addWidget(self.track_exclude_selected_button)
        track_selection_toolbar.addStretch(1)
        track_selection_toolbar.addWidget(self.track_status_label)
        tracks_toolbar.addLayout(track_selection_toolbar)
        track_cleanup_toolbar = QHBoxLayout()
        track_cleanup_toolbar.addWidget(self.track_deselect_duplicates_button)
        track_cleanup_toolbar.addWidget(self.track_deselect_duplicate_audio_button)
        track_cleanup_toolbar.addWidget(self.track_deselect_duplicate_subtitles_button)
        track_cleanup_toolbar.addWidget(self.track_deselect_probable_duplicates_button)
        track_cleanup_toolbar.addSpacing(8)
        track_cleanup_toolbar.addWidget(self.track_reset_selection_button)
        track_cleanup_toolbar.addWidget(self.track_reset_order_button)
        track_cleanup_toolbar.addWidget(self.track_reset_button)
        track_cleanup_toolbar.addStretch(1)
        tracks_toolbar.addLayout(track_cleanup_toolbar)
        tracks_layout.addLayout(tracks_toolbar)
        self.tracks_table.setHorizontalHeaderLabels(self.TRACK_COLUMNS)
        track_header = self.tracks_table.horizontalHeader()
        for column in [
            self.TRACK_INCLUDE_COLUMN,
            self.TRACK_COLUMNS.index("ID"),
            self.TRACK_COLUMNS.index("Source"),
            self.TRACK_COLUMNS.index("Type"),
            self.TRACK_COLUMNS.index("Input lang"),
            self.TRACK_COLUMNS.index("Output lang"),
            self.TRACK_FLAGS_COLUMN,
            self.TRACK_COLUMNS.index("Delay"),
        ]:
            track_header.setSectionResizeMode(column, QHeaderView.ResizeToContents)
        track_header.setSectionResizeMode(self.TRACK_NAME_COLUMN, QHeaderView.Stretch)
        track_header.setSectionResizeMode(self.TRACK_PLAN_COLUMN, QHeaderView.Stretch)
        track_header.setStretchLastSection(False)
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
        self.track_details_edit.setReadOnly(True)
        self.track_details_edit.setLineWrapMode(QPlainTextEdit.WidgetWidth)
        self.track_details_edit.setMaximumHeight(120)
        self.track_details_edit.setAcceptDrops(True)
        self.track_details_edit.installEventFilter(self)
        tracks_layout.addWidget(self.track_details_edit)

        for edit in [self.summary_edit, self.log_edit]:
            edit.setAcceptDrops(True)
            edit.installEventFilter(self)
        output_panel = self._build_output_panel(self.output_tabs, self.summary_edit, self.log_edit)

        work_splitter = QSplitter(Qt.Horizontal)
        work_splitter.addWidget(files_group)
        work_splitter.addWidget(tracks_group)
        work_splitter.setCollapsible(0, False)
        work_splitter.setCollapsible(1, False)
        work_splitter.setStretchFactor(0, 1)
        work_splitter.setStretchFactor(1, 3)
        work_splitter.setSizes([430, 790])

        splitter = QSplitter(Qt.Vertical)
        splitter.addWidget(work_splitter)
        splitter.addWidget(output_panel)
        splitter.setSizes([500, 220])
        root.addWidget(splitter, 1)

        self.progress.setRange(0, 1)
        self.progress.setValue(0)
        self.progress.setFormat("%p%")
        self.progress_label.setMinimumWidth(320)
        self.theme_combo.setFixedWidth(92)
        self.statusBar().addPermanentWidget(self.theme_combo)
        self.statusBar().addPermanentWidget(self.progress_label)
        self.statusBar().addPermanentWidget(self.progress, 1)
        self.tabs.addTab(organizer_tab, style.standardIcon(QStyle.SP_FileIcon), "Organizer")
        self.tabs.addTab(self._build_config_tab(), style.standardIcon(QStyle.SP_FileDialogDetailedView), "Config")
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
        self.config_reload_button.clicked.connect(self.reload_config_tab)
        self.config_save_button.clicked.connect(self.save_config_tab)
        self.config_apply_button.clicked.connect(self.apply_custom_config_to_organizer)
        self.config_reset_button.clicked.connect(self.reset_config_defaults)
        self.profile_import_button.clicked.connect(self.import_profile_library)
        self.profile_export_button.clicked.connect(self.export_profile_library)

    @Slot()
    def show_about_dialog(self) -> None:
        QMessageBox.about(
            self,
            f"About {APP_NAME}",
            (
                f"<h3>{APP_NAME} {APP_VERSION}</h3>"
                f"<p>{APP_DESCRIPTION}</p>"
                "<p>Built for MKVToolNix workflows with optional FFmpeg, MakeMKV, "
                "Tesseract, and Subtitle Edit integrations.</p>"
                f'<p><a href="{DOCUMENTATION_URL}">Documentation</a> &nbsp; '
                f'<a href="{ISSUES_URL}">Report an issue</a></p>'
            ),
        )

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

        makemkv_output_panel = self._build_output_panel(
            self.makemkv_output_tabs,
            self.makemkv_summary_edit,
            self.makemkv_log_edit,
        )

        splitter = QSplitter(Qt.Vertical)
        splitter.addWidget(files_group)
        splitter.addWidget(makemkv_output_panel)
        splitter.setSizes([500, 220])
        root.addWidget(splitter, 1)

        makemkv_button.clicked.connect(self.choose_makemkv_executable)
        source_button.clicked.connect(self.choose_makemkv_source_folder)
        output_button.clicked.connect(self.choose_makemkv_output_folder)
        self.makemkv_selection_combo.currentIndexChanged.connect(self._makemkv_selection_changed)
        self._makemkv_selection_changed()
        return tab

    def _build_config_tab(self) -> QWidget:
        tab = QWidget()
        root = QVBoxLayout(tab)
        root.setContentsMargins(12, 12, 12, 10)
        root.setSpacing(10)

        config_group = QGroupBox("App defaults")
        config_layout = QGridLayout(config_group)
        config_layout.setColumnStretch(1, 1)
        config_layout.addWidget(QLabel("Config file"), 0, 0)
        config_layout.addWidget(self.config_path_label, 0, 1, 1, 3)
        config_layout.addWidget(QLabel("Custom language order"), 1, 0)
        config_layout.addWidget(self.config_custom_language_order_edit, 1, 1, 1, 3)
        config_layout.addWidget(self.config_use_custom_order_check, 2, 1, 1, 3)

        actions = QHBoxLayout()
        actions.addWidget(self.config_reload_button)
        actions.addWidget(self.config_save_button)
        actions.addWidget(self.config_reset_button)
        actions.addWidget(self.config_apply_button)
        actions.addStretch(1)
        config_layout.addLayout(actions, 3, 1, 1, 3)
        config_layout.addWidget(self.config_status_label, 4, 1, 1, 3)

        root.addWidget(config_group)

        profiles_group = QGroupBox("Profile library")
        profiles_layout = QGridLayout(profiles_group)
        profiles_layout.setColumnStretch(1, 1)
        profiles_layout.addWidget(QLabel("Profile file"), 0, 0)
        profiles_layout.addWidget(self.profile_store_path_label, 0, 1, 1, 3)
        library_actions = QHBoxLayout()
        library_actions.addWidget(self.profile_import_button)
        library_actions.addWidget(self.profile_export_button)
        library_actions.addStretch(1)
        profiles_layout.addLayout(library_actions, 1, 1, 1, 3)
        profiles_layout.addWidget(self.profile_library_status_label, 2, 1, 1, 3)
        root.addWidget(profiles_group)
        root.addStretch(1)
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
        self.audio_sync_analysis_combo.setToolTip(
            "Automatically distribute checkpoints across the shared media duration. Full timeline is recommended."
        )
        self.audio_sync_analysis_plan_label.setToolTip(
            "The calculated checkpoint count, range, spacing, and window duration."
        )
        reference_audio_label = QLabel("Reference audio")
        reference_audio_label.setToolTip("Audio stream from the already synced reference.")
        source_audio_label = QLabel("Source audio")
        source_audio_label.setToolTip("Audio stream from the source to compare with the reference.")
        analysis_label = QLabel("Analysis")
        analysis_label.setToolTip("Choose how thoroughly Audio Sync samples the shared timeline.")
        plan_label = QLabel("Plan")
        plan_label.setToolTip("Settings calculated from the shorter file duration.")
        compare_grid.addWidget(reference_audio_label, 0, 0)
        compare_grid.addWidget(self.audio_sync_ref_combo, 0, 1)
        compare_grid.addWidget(source_audio_label, 0, 2)
        compare_grid.addWidget(self.audio_sync_source_combo, 0, 3)
        compare_grid.addWidget(analysis_label, 1, 0)
        compare_grid.addWidget(self.audio_sync_analysis_combo, 1, 1, 1, 3)
        compare_grid.addWidget(plan_label, 2, 0)
        compare_grid.addWidget(self.audio_sync_analysis_plan_label, 2, 1, 1, 3)
        root.addWidget(compare_group)

        self.audio_sync_check_button.setIcon(style.standardIcon(QStyle.SP_DialogApplyButton))
        self.audio_sync_analyze_button.setIcon(style.standardIcon(QStyle.SP_MediaPlay))
        self.audio_sync_apply_organizer_button.setIcon(style.standardIcon(QStyle.SP_DialogApplyButton))
        self.audio_sync_apply_organizer_button.setToolTip(
            "Fill the Organizer input, audio delay, and subtitle delay fields; Organizer remux applies them with mkvmerge --sync."
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

        audio_sync_output_panel = self._build_output_panel(
            self.audio_sync_output_tabs,
            self.audio_sync_summary_edit,
            self.audio_sync_log_edit,
        )

        self.audio_sync_splitter = QSplitter(Qt.Vertical)
        self.audio_sync_splitter.addWidget(streams_group)
        self.audio_sync_splitter.addWidget(audio_sync_output_panel)
        self.audio_sync_splitter.setCollapsible(0, False)
        self.audio_sync_splitter.setCollapsible(1, False)
        self.audio_sync_splitter.setStretchFactor(0, 2)
        self.audio_sync_splitter.setStretchFactor(1, 3)
        self.audio_sync_splitter.setSizes([300, 420])
        root.addWidget(self.audio_sync_splitter, 1)

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
        self.track_select_audio_button.clicked.connect(self.select_audio_tracks)
        self.track_select_subtitles_button.clicked.connect(self.select_subtitle_tracks)
        self.track_include_selected_button.clicked.connect(self.include_selected_tracks)
        self.track_exclude_selected_button.clicked.connect(self.exclude_selected_tracks)
        self.track_deselect_duplicates_button.clicked.connect(self.deselect_duplicate_tracks)
        self.track_deselect_duplicate_audio_button.clicked.connect(self.deselect_duplicate_audio_tracks)
        self.track_deselect_duplicate_subtitles_button.clicked.connect(self.deselect_duplicate_subtitle_tracks)
        self.track_deselect_probable_duplicates_button.clicked.connect(self.deselect_probable_duplicate_tracks)
        self.track_reset_selection_button.clicked.connect(self.reset_track_selection_edits)
        self.track_reset_order_button.clicked.connect(self.reset_track_order_edits)
        self.track_reset_button.clicked.connect(self.reset_track_edits)
        self.tracks_table.itemChanged.connect(self._track_item_changed)
        self.tracks_table.rows_reordered.connect(self._track_rows_reordered)
        self.tracks_table.itemSelectionChanged.connect(self._update_track_details_for_selection)
        self.makemkv_check_button.clicked.connect(self.check_makemkv_tools)
        self.makemkv_preview_button.clicked.connect(self.start_makemkv_preview)
        self.makemkv_run_button.clicked.connect(self.start_makemkv_run)
        if self.makemkv_clear_button:
            self.makemkv_clear_button.clicked.connect(self.clear_makemkv_inputs)
        if self.makemkv_reset_button:
            self.makemkv_reset_button.clicked.connect(self.reset_makemkv_tab)
        self.makemkv_cancel_button.clicked.connect(self.cancel_makemkv_run)
        self.audio_sync_check_button.clicked.connect(self.check_audio_sync_tools)
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
        self.audio_sync_reference_edit.textEdited.connect(lambda _text: self._audio_sync_path_text_edited())
        self.audio_sync_source_edit.textEdited.connect(lambda _text: self._audio_sync_path_text_edited())
        self.audio_sync_auto_load_timer.timeout.connect(self.start_audio_sync_stream_auto_load)
        self.audio_sync_analysis_combo.activated.connect(self._audio_sync_analysis_preset_activated)
        self.input_edit.textEdited.connect(self._manual_input_changed)
        self.files_table.itemSelectionChanged.connect(self._populate_tracks_for_selection)
        self.profile_combo.currentIndexChanged.connect(self._profile_combo_changed)
        self.update_profile_button.clicked.connect(self.update_current_profile)
        self.save_profile_button.clicked.connect(self.save_current_profile)
        self.revert_profile_button.clicked.connect(self.revert_current_profile)
        self.delete_profile_button.clicked.connect(self.delete_current_profile)
        self.config_custom_language_order_edit.textChanged.connect(self._config_ui_changed)
        self.config_use_custom_order_check.toggled.connect(self._config_ui_changed)
        for widget in [
            self.suffix_edit,
            self.preferred_language_edit,
            self.custom_language_order_edit,
        ]:
            widget.textChanged.connect(self._profile_ui_changed)
        for widget in [
            self.existing_output_combo,
            self.metadata_combo,
            self.audio_name_style_combo,
            self.language_order_style_combo,
            self.regional_order_combo,
            self.report_format_combo,
        ]:
            widget.currentIndexChanged.connect(self._profile_ui_changed)
        for widget in [
            self.merge_inputs_check,
            self.smart_subs_check,
            self.drop_empty_check,
            self.duplicate_check,
            self.subtitle_language_duplicates_check,
            self.disable_track_statistics_tags_check,
            self.variant_check,
            self.auto_pgs_ocr_check,
            self.auto_commentary_ocr_check,
            self.report_check,
            self.preserve_commentary_names_check,
            self.preferred_audio_first_check,
            self.preferred_audio_default_check,
            self.preferred_subtitle_first_check,
            self.preferred_forced_subtitle_default_check,
        ]:
            widget.toggled.connect(self._profile_ui_changed)
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
            lambda _index: self._regional_order_changed()
        )
        self.theme_combo.currentIndexChanged.connect(self.change_theme)

    def _tool_button(self, icon_id: QStyle.StandardPixmap, tooltip: str) -> QToolButton:
        button = QToolButton()
        button.setIcon(self.style().standardIcon(icon_id))
        button.setToolTip(tooltip)
        button.setAutoRaise(True)
        return button

    def _build_output_panel(
        self,
        tabs: QTabWidget,
        summary_edit: QPlainTextEdit,
        log_edit: QPlainTextEdit,
    ) -> QWidget:
        panel = QWidget()
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)

        search_edit = QLineEdit()
        search_edit.setPlaceholderText("Find in output")
        search_edit.setMaximumWidth(260)
        find_button = QPushButton("Find next")
        find_button.setObjectName("secondaryButton")
        copy_button = QPushButton("Copy all")
        copy_button.setObjectName("secondaryButton")
        save_button = self._tool_button(QStyle.SP_DialogSaveButton, "Save the visible output to a text file")
        clear_button = self._tool_button(QStyle.SP_DialogResetButton, "Clear the visible output")
        follow_check = QCheckBox("Follow")
        follow_check.setChecked(True)
        follow_check.setToolTip("Keep the visible output scrolled to the newest line")

        toolbar = QHBoxLayout()
        toolbar.setContentsMargins(0, 0, 0, 0)
        toolbar.setSpacing(6)
        toolbar.addWidget(search_edit)
        toolbar.addWidget(find_button)
        toolbar.addStretch(1)
        toolbar.addWidget(follow_check)
        toolbar.addWidget(copy_button)
        toolbar.addWidget(save_button)
        toolbar.addWidget(clear_button)
        layout.addLayout(toolbar)

        for edit in [summary_edit, log_edit]:
            edit.setReadOnly(True)
            edit.setLineWrapMode(QPlainTextEdit.NoWrap)
            self._output_follow_by_edit[id(edit)] = follow_check
        tabs.addTab(summary_edit, "Summary")
        tabs.addTab(log_edit, "Raw log")
        layout.addWidget(tabs)

        controls = {
            "search": search_edit,
            "follow": follow_check,
            "summary": summary_edit,
            "log": log_edit,
        }
        self._output_controls[id(tabs)] = controls
        search_edit.returnPressed.connect(lambda tabs=tabs: self._find_output_text(tabs))
        find_button.clicked.connect(lambda _checked=False, tabs=tabs: self._find_output_text(tabs))
        copy_button.clicked.connect(lambda _checked=False, tabs=tabs: self._copy_output_text(tabs))
        save_button.clicked.connect(lambda _checked=False, tabs=tabs: self._save_output_text(tabs))
        clear_button.clicked.connect(lambda _checked=False, tabs=tabs: self._clear_output_text(tabs))
        return panel

    @staticmethod
    def _active_output_edit(tabs: QTabWidget) -> QPlainTextEdit | None:
        widget = tabs.currentWidget()
        return widget if isinstance(widget, QPlainTextEdit) else None

    def _find_output_text(self, tabs: QTabWidget) -> None:
        controls = self._output_controls.get(id(tabs), {})
        search_edit = controls.get("search")
        edit = self._active_output_edit(tabs)
        if not isinstance(search_edit, QLineEdit) or edit is None:
            return
        query = search_edit.text().strip()
        if not query:
            search_edit.setFocus()
            return
        if edit.find(query):
            return
        cursor = edit.textCursor()
        cursor.movePosition(QTextCursor.Start)
        edit.setTextCursor(cursor)
        if edit.find(query):
            self.statusBar().showMessage("Search wrapped to the beginning")
        else:
            self.statusBar().showMessage(f"Not found: {query}")

    def _copy_output_text(self, tabs: QTabWidget) -> None:
        edit = self._active_output_edit(tabs)
        if edit is None:
            return
        QApplication.clipboard().setText(edit.toPlainText())
        self.statusBar().showMessage("Visible output copied")

    def _save_output_text(self, tabs: QTabWidget) -> None:
        edit = self._active_output_edit(tabs)
        if edit is None:
            return
        tab_name = tabs.tabText(tabs.currentIndex()).lower().replace(" ", "-")
        output_path, _selected_filter = QFileDialog.getSaveFileName(
            self,
            "Save output",
            f"mkv-track-organizer-{tab_name}.txt",
            "Text files (*.txt);;All files (*)",
        )
        if not output_path:
            return
        try:
            Path(output_path).write_text(edit.toPlainText(), encoding="utf-8")
        except OSError as error:
            QMessageBox.critical(self, "Save output failed", str(error))
            return
        self.statusBar().showMessage(f"Output saved: {output_path}")

    def _clear_output_text(self, tabs: QTabWidget) -> None:
        edit = self._active_output_edit(tabs)
        if edit is None:
            return
        edit.clear()
        self._log_line_starts[id(edit)] = True
        self.statusBar().showMessage("Visible output cleared")

    @Slot()
    def change_theme(self) -> None:
        self._apply_theme(str(self.theme_combo.currentData() or "dark"))
        self._write_profile_store()

    def _apply_theme(self, theme: str | None = None) -> None:
        theme = theme or self.current_theme
        self.current_theme = "light" if theme == "light" else "dark"
        self.saved_theme = self.current_theme
        theme_index = self.theme_combo.findData(self.current_theme)
        if theme_index >= 0 and theme_index != self.theme_combo.currentIndex():
            previous_block_state = self.theme_combo.blockSignals(True)
            self.theme_combo.setCurrentIndex(theme_index)
            self.theme_combo.blockSignals(previous_block_state)
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
                text-align: center;
            }}
            QPushButton:hover, QToolButton:hover {{
                background: {palette['button_hover']};
                border-color: {palette['border_strong']};
            }}
            QPushButton:pressed, QToolButton:pressed {{
                background: {palette['button_pressed']};
                border-color: {palette['primary']};
                padding-top: 7px;
                padding-bottom: 5px;
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
            QPushButton#primaryButton:pressed {{
                background: {palette['primary_pressed']};
                border-color: {palette['primary_pressed']};
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
            QPushButton#dangerButton:pressed {{
                background: {palette['danger_pressed']};
                border-color: {palette['danger_text']};
            }}
            QPushButton#secondaryButton {{
                background: {palette['secondary_bg']};
                border-color: {palette['secondary_border']};
                color: {palette['secondary_text']};
            }}
            QPushButton#secondaryButton:pressed {{
                background: {palette['secondary_pressed']};
                border-color: {palette['secondary_text']};
            }}
            QLabel#trackStatusLabel {{
                color: {palette['muted']};
                padding: 0 4px;
            }}
            QLabel#audioSyncPlan {{
                background: {palette['field']};
                color: {palette['text']};
                border: 1px solid {palette['border']};
                border-radius: 5px;
                padding: 5px 7px;
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
                "button_pressed": "#e2e8f0",
                "primary": "#2563eb",
                "primary_strong": "#1d4ed8",
                "primary_pressed": "#1e40af",
                "secondary_bg": "#eef6ff",
                "secondary_border": "#bfdbfe",
                "secondary_text": "#1e3a8a",
                "secondary_pressed": "#dbeafe",
                "danger_bg": "#fff5f5",
                "danger_border": "#f1a5a5",
                "danger_text": "#9f1239",
                "danger_hover": "#ffe4e6",
                "danger_pressed": "#fecdd3",
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
            "button_pressed": "#334155",
            "primary": "#2f81f7",
            "primary_strong": "#1f6feb",
            "primary_pressed": "#1158c7",
            "secondary_bg": "#172536",
            "secondary_border": "#315170",
            "secondary_text": "#9bd1ff",
            "secondary_pressed": "#203a56",
            "danger_bg": "#331c22",
            "danger_border": "#7f2d3a",
            "danger_text": "#ffb4bd",
            "danger_hover": "#44232b",
            "danger_pressed": "#5b2631",
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
        self._clear_manual_track_order()
        self._sync_combo_tooltip(self.language_order_style_combo, self.LANGUAGE_ORDER_STYLE_HELP)
        style = self.language_order_style_combo.currentData()
        self.regional_order_combo.setEnabled(style == "regional")
        self.custom_language_order_edit.setEnabled(style == "custom")
        self._sync_combo_tooltip(self.regional_order_combo, self.REGIONAL_ORDER_HELP)

    def _regional_order_changed(self) -> None:
        self._clear_manual_track_order()
        self._sync_combo_tooltip(self.regional_order_combo, self.REGIONAL_ORDER_HELP)

    def _append_text(
        self,
        edit: QPlainTextEdit,
        text: str,
        char_format: QTextCharFormat | None = None,
    ) -> None:
        follow_check = self._output_follow_by_edit.get(id(edit))
        follow = follow_check.isChecked() if follow_check else True
        previous_cursor = edit.textCursor()
        scroll_bar = edit.verticalScrollBar()
        previous_scroll = scroll_bar.value()

        end_cursor = edit.textCursor()
        end_cursor.movePosition(QTextCursor.End)
        end_cursor.insertText(text, char_format or QTextCharFormat())
        if follow:
            edit.setTextCursor(end_cursor)
            edit.ensureCursorVisible()
        else:
            edit.setTextCursor(previous_cursor)
            scroll_bar.setValue(previous_scroll)

    def _append_timestamped_log(self, edit: QPlainTextEdit, text: str) -> None:
        if not text:
            return
        at_line_start = self._log_line_starts.get(id(edit), True)
        output: list[str] = []
        for segment in text.splitlines(keepends=True):
            content = segment.rstrip("\r\n")
            ending = segment[len(content):]
            if content and at_line_start:
                output.append(f"[{time.strftime('%H:%M:%S')}] ")
            output.append(content)
            output.append(ending)
            at_line_start = bool(ending)
        if text and not text.splitlines(keepends=True):
            output.append(text)
            at_line_start = text.endswith(("\n", "\r"))
        self._log_line_starts[id(edit)] = at_line_start
        self._append_text(edit, "".join(output))

    def append_summary_line(self, text: str = "") -> None:
        self._append_text(self.summary_edit, f"{text}\n")

    def append_makemkv_summary_line(self, text: str = "") -> None:
        self._append_text(self.makemkv_summary_edit, f"{text}\n")

    def append_audio_sync_summary_line(self, text: str = "") -> None:
        self._append_text(
            self.audio_sync_summary_edit,
            f"{text}\n",
            self._audio_sync_summary_line_format(text),
        )

    def _audio_sync_summary_line_format(self, text: str) -> QTextCharFormat | None:
        line = text.strip()
        delay_prefixes = (
            "offset=",
            "Recommended correction:",
            "Source offset vs reference:",
            "Timeline shift to apply:",
            "Measured timing:",
            "Timeline shift:",
            "Timeline shift baked into export:",
            "Organizer will apply audio delays:",
            "Organizer will apply subtitle delays:",
        )
        if not line.startswith(delay_prefixes):
            return None

        char_format = QTextCharFormat()
        char_format.setForeground(QColor("#0369a1" if self.current_theme == "light" else "#7dd3fc"))
        char_format.setFontWeight(QFont.DemiBold)
        return char_format

    @Slot(str)
    def append_audio_sync_log(self, text: str) -> None:
        self._append_timestamped_log(self.audio_sync_log_edit, f"{text}\n")

    @staticmethod
    def _format_progress_elapsed(seconds: float) -> str:
        total_seconds = max(0, int(seconds))
        hours, remainder = divmod(total_seconds, 3600)
        minutes, seconds = divmod(remainder, 60)
        if hours:
            return f"{hours:d}:{minutes:02d}:{seconds:02d}"
        return f"{minutes:02d}:{seconds:02d}"

    def _start_progress_session(self, scope: str, activity: str) -> None:
        self._progress_started_at = time.monotonic()
        self._progress_scope = scope
        self._progress_activity = activity
        self._progress_index = 0
        self._progress_total = 0
        self._progress_finished_elapsed = None
        self.progress_timer.start()
        self._refresh_progress_label()

    def _set_progress_context(self, index: int = 0, total: int = 0) -> None:
        self._progress_index = max(0, index)
        self._progress_total = max(0, total)
        self._refresh_progress_label()

    def _set_progress_label(self, text: str) -> None:
        if text == "Idle":
            self._reset_progress_session()
            return
        self._progress_activity = text or "Working"
        self._refresh_progress_label()

    def _finish_progress_session(self, activity: str) -> None:
        if self._progress_started_at is not None:
            self._progress_finished_elapsed = time.monotonic() - self._progress_started_at
        self._progress_started_at = None
        self._progress_activity = activity
        self.progress_timer.stop()
        self._taskbar_progress.clear()
        self._refresh_progress_label()

    def _reset_progress_session(self) -> None:
        self.progress_timer.stop()
        self._progress_started_at = None
        self._progress_finished_elapsed = None
        self._progress_scope = ""
        self._progress_activity = "Idle"
        self._progress_index = 0
        self._progress_total = 0
        self.progress.setRange(0, 1)
        self.progress.setValue(0)
        self.progress.setFormat("%p%")
        self._taskbar_progress.clear()
        self._refresh_progress_label()

    @Slot()
    def _refresh_progress_label(self) -> None:
        parts: list[str] = []
        if self._progress_scope:
            parts.append(self._progress_scope)
        if self._progress_total and self._progress_index:
            parts.append(f"{self._progress_index}/{self._progress_total}")
        parts.append(self._progress_activity or "Idle")

        elapsed: float | None = self._progress_finished_elapsed
        if self._progress_started_at is not None:
            elapsed = time.monotonic() - self._progress_started_at
        if elapsed is not None:
            parts.append(self._format_progress_elapsed(elapsed))

        full_text = " | ".join(parts)
        self.progress_label.setText(full_text[:120])
        self.progress_label.setToolTip(full_text)

    def _set_progress_indeterminate(self) -> None:
        self.progress.setRange(0, 0)
        self.progress.setFormat("Working")
        self._taskbar_progress.set_indeterminate()

    def _set_progress_value(self, maximum: int, value: int) -> None:
        maximum = max(1, maximum)
        self.progress.setRange(0, maximum)
        self.progress.setValue(max(0, min(value, maximum)))
        self.progress.setFormat("%p%")
        self._taskbar_progress.set_value(maximum, value)

    def _load_default_args(self):
        config_defaults, config_path = organizer.config_defaults_from_argv([])
        parser = organizer.build_parser(config_defaults)
        return parser.parse_args([]), config_path

    def _config_file_path(self) -> Path:
        return Path(self.default_config_path or organizer.DEFAULT_CONFIG_PATH)

    def _read_raw_config(self) -> dict:
        path = self._config_file_path()
        if not path.exists():
            return {}
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            self.config_status_label.setText(f"Could not read config: {error}")
            return {}
        return data if isinstance(data, dict) else {}

    def reload_config_tab(self) -> None:
        config = self._read_raw_config()
        raw_order = config.get("custom_language_order", getattr(self.default_args, "custom_language_order", []))
        if isinstance(raw_order, (list, tuple)):
            order_text = ", ".join(str(item) for item in raw_order if str(item).strip())
        else:
            order_text = str(raw_order or "")
        self._applying_config = True
        try:
            self.config_custom_language_order_edit.setText(order_text)
            self.config_use_custom_order_check.setChecked(
                str(config.get("language_order_style", getattr(self.default_args, "language_order_style", ""))) == "custom"
            )
        finally:
            self._applying_config = False
        self.config_path_label.setText(str(self._config_file_path()))
        self.config_status_label.setText("Config loaded.")
        self._capture_config_baseline()

    def _config_payload_from_ui(self) -> dict[str, object]:
        order_text = self.config_custom_language_order_edit.text().strip()
        try:
            order_value: object = list(organizer.parse_custom_language_order(order_text))
        except organizer.OrganizerError:
            order_value = order_text
        return {
            "custom_language_order": order_value,
            "language_order_style": "custom" if self.config_use_custom_order_check.isChecked() else "default",
        }

    def _capture_config_baseline(self) -> None:
        self._config_baseline = self._config_payload_from_ui()
        self._update_config_state()

    def _config_is_dirty(self) -> bool:
        return self._config_payload_from_ui() != self._config_baseline

    @Slot()
    def _config_ui_changed(self) -> None:
        if self._applying_config:
            return
        self._update_config_state()

    def _update_config_state(self) -> None:
        dirty = self._config_is_dirty()
        self.config_save_button.setEnabled(dirty)
        if dirty:
            self.config_status_label.setText("Unsaved changes")
        elif not self.config_status_label.text():
            self.config_status_label.setText("Saved")

    def save_config_tab(self) -> bool:
        order_text = self.config_custom_language_order_edit.text().strip()
        try:
            parsed_order = organizer.parse_custom_language_order(order_text)
        except organizer.OrganizerError as error:
            self.config_status_label.setText(str(error))
            return False

        config = self._read_raw_config()
        if parsed_order:
            config["custom_language_order"] = list(parsed_order)
        else:
            config.pop("custom_language_order", None)
        if self.config_use_custom_order_check.isChecked():
            if not parsed_order:
                self.config_status_label.setText("Custom order is required when custom order is the default.")
                return False
            config["language_order_style"] = "custom"
        elif config.get("language_order_style") == "custom":
            config["language_order_style"] = "default"

        path = self._config_file_path()
        try:
            self._write_json_atomic(path, config)
        except OSError as error:
            self.config_status_label.setText(f"Could not save config: {error}")
            return False

        self.default_args, self.default_config_path = self._load_default_args()
        self.config_path_label.setText(str(self._config_file_path()))
        self.config_status_label.setText("Config saved.")
        self._capture_config_baseline()
        return True

    @Slot()
    def reset_config_defaults(self) -> None:
        self.config_custom_language_order_edit.clear()
        self.config_use_custom_order_check.setChecked(False)
        self._update_config_state()

    def apply_custom_config_to_organizer(self) -> None:
        order_text = self.config_custom_language_order_edit.text().strip()
        try:
            parsed_order = organizer.parse_custom_language_order(order_text)
        except organizer.OrganizerError as error:
            self.config_status_label.setText(str(error))
            return
        if not parsed_order:
            self.config_status_label.setText("Set a custom language order first.")
            return
        self.custom_language_order_edit.setText(", ".join(parsed_order))
        index = self.language_order_style_combo.findData("custom")
        if index >= 0:
            self.language_order_style_combo.setCurrentIndex(index)
        self._clear_manual_track_order()
        self.config_status_label.setText("Custom order copied to Organizer.")

    def _load_profile_store(self) -> None:
        self.profiles = {}
        self.last_profile_name = ""
        self.saved_theme = "dark"
        self._profile_store_needs_migration = False
        if not self.profile_store_path.exists():
            return

        try:
            raw_store = json.loads(self.profile_store_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            self.append_summary_line(f"Profile store ignored: {error}")
            return

        if not isinstance(raw_store, dict):
            self.append_summary_line("Profile store ignored: root value must be an object.")
            return

        try:
            version = int(raw_store.get("version", 1))
        except (TypeError, ValueError):
            self.append_summary_line("Profile store ignored: version must be a number.")
            return
        if version < 1 or version > self.PROFILE_STORE_VERSION:
            self.append_summary_line(
                f"Profile store ignored: unsupported version {version} (supports up to {self.PROFILE_STORE_VERSION})."
            )
            return
        self._profile_store_needs_migration = version != self.PROFILE_STORE_VERSION

        raw_ui = raw_store.get("ui") or {}
        if isinstance(raw_ui, dict) and str(raw_ui.get("theme") or "") in {"dark", "light"}:
            self.saved_theme = str(raw_ui["theme"])

        raw_profiles = raw_store.get("profiles", {})
        if isinstance(raw_profiles, dict):
            for raw_name, raw_payload in raw_profiles.items():
                name = str(raw_name).strip()
                if not name or not isinstance(raw_payload, dict):
                    self._profile_store_needs_migration = True
                    continue
                try:
                    normalized_payload = self._normalize_profile_payload(
                        raw_payload,
                        strict=version >= self.PROFILE_STORE_VERSION,
                    )
                    self.profiles[name] = normalized_payload
                    if normalized_payload != raw_payload:
                        self._profile_store_needs_migration = True
                except ValueError as error:
                    self.append_summary_line(f"Profile ignored ({name}): {error}")
                    self._profile_store_needs_migration = True

        raw_last_profile = raw_store.get("last_profile", "")
        if str(raw_last_profile) in self.profiles:
            self.last_profile_name = str(raw_last_profile)

    @staticmethod
    def _write_json_atomic(path: Path, payload: dict) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temp_path = path.with_suffix(path.suffix + ".tmp")
        try:
            temp_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
            temp_path.replace(path)
        finally:
            if temp_path.exists():
                temp_path.unlink()

    def _profile_store_payload(self, last_profile: str | None = None) -> dict:
        selected_profile = self._current_profile_name() if last_profile is None else last_profile
        return {
            "version": self.PROFILE_STORE_VERSION,
            "last_profile": selected_profile if selected_profile in self.profiles else "",
            "ui": {"theme": self.current_theme},
            "profiles": {
                name: self.profiles[name]
                for name in sorted(self.profiles, key=str.casefold)
            },
        }

    def _write_profile_store(self) -> bool:
        selected_profile = self._current_profile_name()
        previous_last_profile = self.last_profile_name
        self.last_profile_name = selected_profile
        try:
            self._write_json_atomic(self.profile_store_path, self._profile_store_payload(selected_profile))
        except OSError as error:
            self.last_profile_name = previous_last_profile
            self.append_summary_line(f"Could not save profiles: {error}")
            return False
        self._profile_store_needs_migration = False
        self._update_profile_library_status()
        return True

    @staticmethod
    def _coerce_profile_bool(value: object, key: str, strict: bool, default: object = False) -> bool:
        try:
            return organizer.config_bool(value, key)
        except organizer.OrganizerError as error:
            if strict:
                raise ValueError(str(error)) from error
            return bool(default)

    def _normalize_profile_payload(self, raw_payload: dict, strict: bool = True) -> dict:
        baseline = dict(self._profile_default_payload)
        if not baseline:
            baseline = {key: "" for key in self.PROFILE_FIELDS}
        payload = {key: baseline.get(key) for key in self.PROFILE_FIELDS}
        raw = dict(raw_payload)

        if "existing_output_mode" not in raw:
            if raw.get("overwrite"):
                raw["existing_output_mode"] = "overwrite"
            elif raw.get("skip_existing"):
                raw["existing_output_mode"] = "skip"

        for key in self.PROFILE_FIELDS:
            if key not in raw:
                continue
            value = raw[key]
            if key in self.PROFILE_BOOL_FIELDS:
                payload[key] = self._coerce_profile_bool(value, key, strict, payload.get(key, False))
                continue
            if key in self.PROFILE_ENUM_FIELDS:
                normalized = str(value or "").strip().lower().replace("_", "-")
                if normalized == "skip-existing" and key == "existing_output_mode":
                    normalized = "skip"
                if normalized not in self.PROFILE_ENUM_FIELDS[key]:
                    if strict:
                        allowed = ", ".join(sorted(self.PROFILE_ENUM_FIELDS[key]))
                        raise ValueError(f"Invalid {key}: {value!r}. Expected one of: {allowed}.")
                    continue
                payload[key] = normalized
                continue
            if key == "regional_order":
                try:
                    payload[key] = ",".join(organizer.parse_regional_order(value))
                except organizer.OrganizerError as error:
                    if strict:
                        raise ValueError(str(error)) from error
                continue
            if key == "custom_language_order":
                try:
                    payload[key] = ", ".join(organizer.parse_custom_language_order(value))
                except organizer.OrganizerError as error:
                    if strict:
                        raise ValueError(str(error)) from error
                continue
            if key == "preferred_language":
                payload[key] = organizer.normalize_preferred_language(value)
                continue
            payload[key] = str(value or "").strip()

        if payload.get("language_order_style") == "custom" and not payload.get("custom_language_order"):
            if strict:
                raise ValueError("Custom language order is required when Language order is Custom.")
            payload["language_order_style"] = "default"

        preferred_flags = [
            "preferred_audio_first",
            "preferred_audio_default",
            "preferred_subtitle_first",
            "preferred_forced_subtitle_default",
        ]
        if not payload.get("preferred_language") and any(payload.get(key) for key in preferred_flags):
            if strict:
                raise ValueError("Preferred language is required when preferred-language rules are enabled.")
            for key in preferred_flags:
                payload[key] = False

        return payload

    def _refresh_profile_combo(self, selected_name: str = "") -> None:
        previous_block_state = self.profile_combo.blockSignals(True)
        try:
            self.profile_combo.clear()
            self.profile_combo.addItem(self.PROFILE_NONE_LABEL, "")
            for name in sorted(self.profiles, key=str.casefold):
                self.profile_combo.addItem(name, name)
            selected_index = self.profile_combo.findData(selected_name)
            self.profile_combo.setCurrentIndex(selected_index if selected_index >= 0 else 0)
        finally:
            self.profile_combo.blockSignals(previous_block_state)
        self._update_profile_state()

    def _current_profile_name(self) -> str:
        return str(self.profile_combo.currentData() or "")

    def _profile_is_dirty(self) -> bool:
        profile_name = self._loaded_profile_name
        if not profile_name or profile_name not in self.profiles or self._applying_profile:
            return False
        try:
            current_payload = self._normalize_profile_payload(self._profile_payload_from_ui(), strict=True)
        except ValueError:
            return True
        return current_payload != self.profiles[profile_name]

    def _update_profile_state(self) -> None:
        profile_name = self._loaded_profile_name
        has_profile = bool(profile_name and profile_name in self.profiles)
        dirty = self._profile_is_dirty()
        self.update_profile_button.setEnabled(has_profile and dirty)
        self.revert_profile_button.setEnabled(has_profile and dirty)
        self.delete_profile_button.setEnabled(has_profile)
        if not has_profile:
            status = "Custom settings"
        elif dirty:
            status = "Unsaved changes"
        else:
            status = "Saved"
        self.profile_status_label.setText(status)
        self.profile_status_label.setToolTip(
            f"Loaded profile: {profile_name}" if has_profile else "No saved profile is currently loaded"
        )

    def _update_profile_library_status(self, message: str = "") -> None:
        count = len(self.profiles)
        summary = f"{count} saved profile{'s' if count != 1 else ''}"
        self.profile_library_status_label.setText(f"{message} | {summary}" if message else summary)
        self.profile_store_path_label.setText(str(self.profile_store_path))

    def _read_profile_library_file(self, path: Path) -> dict[str, dict]:
        try:
            raw_store = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as error:
            raise ValueError(f"Invalid profile JSON: {error}") from error
        except OSError as error:
            raise ValueError(f"Could not read profile library: {error}") from error
        if not isinstance(raw_store, dict):
            raise ValueError("Profile library root must be an object.")
        try:
            version = int(raw_store.get("version", 1))
        except (TypeError, ValueError) as error:
            raise ValueError("Profile library version must be a number.") from error
        if version > self.PROFILE_STORE_VERSION:
            raise ValueError(
                f"Profile library version {version} is newer than supported version {self.PROFILE_STORE_VERSION}."
            )
        raw_profiles = raw_store.get("profiles")
        if not isinstance(raw_profiles, dict):
            raise ValueError("Profile library does not contain a profiles object.")

        imported: dict[str, dict] = {}
        seen_names: set[str] = set()
        for raw_name, raw_payload in raw_profiles.items():
            name = str(raw_name).strip()
            if not name or name.casefold() == self.PROFILE_NONE_LABEL.casefold():
                raise ValueError(f"Invalid or reserved profile name: {raw_name!r}.")
            if name.casefold() in seen_names:
                raise ValueError(f"Duplicate profile name: {name}.")
            if not isinstance(raw_payload, dict):
                raise ValueError(f"Profile '{name}' must be an object.")
            try:
                imported[name] = self._normalize_profile_payload(raw_payload, strict=True)
            except ValueError as error:
                raise ValueError(f"Profile '{name}': {error}") from error
            seen_names.add(name.casefold())
        return imported

    def _merge_imported_profiles(self, imported: dict[str, dict], overwrite: bool) -> tuple[int, int]:
        imported_count = 0
        skipped_count = 0
        for name, payload in imported.items():
            existing_name = next(
                (saved_name for saved_name in self.profiles if saved_name.casefold() == name.casefold()),
                "",
            )
            if existing_name and not overwrite:
                skipped_count += 1
                continue
            target_name = existing_name or name
            self.profiles[target_name] = payload
            imported_count += 1
        return imported_count, skipped_count

    @Slot()
    def import_profile_library(self) -> None:
        input_path, _selected_filter = QFileDialog.getOpenFileName(
            self,
            "Import profile library",
            "",
            "Profile libraries (*.json);;All files (*)",
        )
        if not input_path:
            return
        try:
            imported = self._read_profile_library_file(Path(input_path))
        except ValueError as error:
            QMessageBox.critical(self, "Import profiles failed", str(error))
            return
        conflicts = [
            name
            for name in imported
            if any(saved_name.casefold() == name.casefold() for saved_name in self.profiles)
        ]
        overwrite = False
        if conflicts:
            answer = QMessageBox.question(
                self,
                "Import profile library",
                f"{len(conflicts)} profile name(s) already exist. Replace them?\n"
                "Choose No to import only new profiles.",
                QMessageBox.Yes | QMessageBox.No | QMessageBox.Cancel,
                QMessageBox.Cancel,
            )
            if answer == QMessageBox.Cancel:
                return
            overwrite = answer == QMessageBox.Yes
        previous_profiles = dict(self.profiles)
        previous_loaded_profile = self._loaded_profile_name
        imported_count, skipped_count = self._merge_imported_profiles(imported, overwrite)
        self._refresh_profile_combo(self._loaded_profile_name)
        if not self._write_profile_store():
            self.profiles = previous_profiles
            self._loaded_profile_name = previous_loaded_profile
            self._refresh_profile_combo(previous_loaded_profile)
            QMessageBox.warning(self, "Import profiles failed", "Could not save the imported profile library.")
            return
        message = f"Imported {imported_count}"
        if skipped_count:
            message += f", kept {skipped_count} existing"
        self._update_profile_library_status(message)
        self.statusBar().showMessage(message)

    @Slot()
    def export_profile_library(self) -> None:
        output_path, _selected_filter = QFileDialog.getSaveFileName(
            self,
            "Export profile library",
            "mkv-track-organizer-profiles.json",
            "Profile libraries (*.json);;All files (*)",
        )
        if not output_path:
            return
        try:
            self._write_json_atomic(Path(output_path), self._profile_store_payload(self._loaded_profile_name))
        except OSError as error:
            QMessageBox.critical(self, "Export profiles failed", str(error))
            return
        self._update_profile_library_status(f"Exported {len(self.profiles)}")
        self.statusBar().showMessage(f"Profiles exported: {output_path}")

    def _existing_output_mode(self) -> str:
        mode = str(self.existing_output_combo.currentData() or "stop")
        return mode if mode in {"stop", "overwrite", "skip"} else "stop"

    def _set_existing_output_mode(self, mode: str) -> None:
        normalized_mode = str(mode or "stop").strip().lower().replace("_", "-")
        if normalized_mode in {"skip-existing", "skip_existing"}:
            normalized_mode = "skip"
        if normalized_mode not in {"stop", "overwrite", "skip"}:
            normalized_mode = "stop"
        index = self.existing_output_combo.findData(normalized_mode)
        self.existing_output_combo.setCurrentIndex(index if index >= 0 else 0)

    @staticmethod
    def _existing_output_mode_from_args(args) -> str:
        if bool(getattr(args, "overwrite", False)):
            return "overwrite"
        if bool(getattr(args, "skip_existing", False)):
            return "skip"
        return "stop"

    def _profile_payload_from_ui(self) -> dict:
        return {
            "output_suffix": self.suffix_edit.text().strip(),
            "existing_output_mode": self._existing_output_mode(),
            "merge_inputs": self.merge_inputs_check.isChecked(),
            "metadata_edit_mode": self.metadata_combo.currentText(),
            "audio_name_style": self.audio_name_style_combo.currentData() or "auto",
            "language_order_style": self.language_order_style_combo.currentData() or "default",
            "regional_order": self.regional_order_combo.currentData() or "",
            "custom_language_order": self.custom_language_order_edit.text().strip(),
            "report_format": self.report_format_combo.currentText(),
            "smart_sub_detection": self.smart_subs_check.isChecked(),
            "drop_empty_subs": self.drop_empty_check.isChecked(),
            "detect_duplicate_tracks": self.duplicate_check.isChecked(),
            "detect_subtitle_language_duplicates": self.subtitle_language_duplicates_check.isChecked(),
            "disable_track_statistics_tags": self.disable_track_statistics_tags_check.isChecked(),
            "detect_language_variants": self.variant_check.isChecked(),
            "auto_pgs_ocr": self.auto_pgs_ocr_check.isChecked(),
            "auto_commentary_ocr": self.auto_commentary_ocr_check.isChecked(),
            "report": self.report_check.isChecked(),
            "preserve_commentary_names": self.preserve_commentary_names_check.isChecked(),
            "preferred_language": self.preferred_language_edit.text().strip(),
            "preferred_audio_first": self.preferred_audio_first_check.isChecked(),
            "preferred_audio_default": self.preferred_audio_default_check.isChecked(),
            "preferred_subtitle_first": self.preferred_subtitle_first_check.isChecked(),
            "preferred_forced_subtitle_default": self.preferred_forced_subtitle_default_check.isChecked(),
        }

    def _validated_profile_payload_from_ui(self) -> dict:
        return self._normalize_profile_payload(self._profile_payload_from_ui(), strict=True)

    def _apply_profile_payload(self, payload: dict) -> None:
        if "output_suffix" in payload:
            self.suffix_edit.setText(str(payload["output_suffix"] or ""))
        if "existing_output_mode" in payload:
            self._set_existing_output_mode(str(payload["existing_output_mode"]))
        elif payload.get("overwrite"):
            self._set_existing_output_mode("overwrite")
        elif payload.get("skip_existing"):
            self._set_existing_output_mode("skip")
        if "merge_inputs" in payload:
            self.merge_inputs_check.setChecked(bool(payload["merge_inputs"]))
        if "metadata_edit_mode" in payload:
            self.metadata_combo.setCurrentText(str(payload["metadata_edit_mode"]))
        if "audio_name_style" in payload:
            index = self.audio_name_style_combo.findData(str(payload["audio_name_style"]))
            if index >= 0:
                self.audio_name_style_combo.setCurrentIndex(index)
        if "language_order_style" in payload:
            index = self.language_order_style_combo.findData(str(payload["language_order_style"]))
            if index >= 0:
                self.language_order_style_combo.setCurrentIndex(index)
        if "regional_order" in payload:
            index = self.regional_order_combo.findData(str(payload["regional_order"]))
            if index >= 0:
                self.regional_order_combo.setCurrentIndex(index)
        if "custom_language_order" in payload:
            raw_order = payload["custom_language_order"]
            if isinstance(raw_order, (list, tuple)):
                self.custom_language_order_edit.setText(
                    ", ".join(str(item) for item in raw_order if str(item).strip())
                )
            else:
                self.custom_language_order_edit.setText(str(raw_order or ""))
        if "report_format" in payload:
            self.report_format_combo.setCurrentText(str(payload["report_format"]))

        self.smart_subs_check.setChecked(bool(payload.get("smart_sub_detection", self.smart_subs_check.isChecked())))
        self.drop_empty_check.setChecked(bool(payload.get("drop_empty_subs", self.drop_empty_check.isChecked())))
        self.duplicate_check.setChecked(bool(payload.get("detect_duplicate_tracks", self.duplicate_check.isChecked())))
        self.subtitle_language_duplicates_check.setChecked(
            bool(
                payload.get(
                    "detect_subtitle_language_duplicates",
                    self.subtitle_language_duplicates_check.isChecked(),
                )
            )
        )
        self.disable_track_statistics_tags_check.setChecked(
            bool(
                payload.get(
                    "disable_track_statistics_tags",
                    self.disable_track_statistics_tags_check.isChecked(),
                )
            )
        )
        self.variant_check.setChecked(bool(payload.get("detect_language_variants", self.variant_check.isChecked())))
        self.auto_pgs_ocr_check.setChecked(bool(payload.get("auto_pgs_ocr", self.auto_pgs_ocr_check.isChecked())))
        self.auto_commentary_ocr_check.setChecked(
            bool(payload.get("auto_commentary_ocr", self.auto_commentary_ocr_check.isChecked()))
        )
        self.report_check.setChecked(bool(payload.get("report", self.report_check.isChecked())))
        self.preserve_commentary_names_check.setChecked(
            bool(payload.get("preserve_commentary_names", self.preserve_commentary_names_check.isChecked()))
        )
        self.preferred_language_edit.setText(str(payload.get("preferred_language", self.preferred_language_edit.text())))
        self.preferred_audio_first_check.setChecked(
            bool(payload.get("preferred_audio_first", self.preferred_audio_first_check.isChecked()))
        )
        self.preferred_audio_default_check.setChecked(
            bool(payload.get("preferred_audio_default", self.preferred_audio_default_check.isChecked()))
        )
        self.preferred_subtitle_first_check.setChecked(
            bool(payload.get("preferred_subtitle_first", self.preferred_subtitle_first_check.isChecked()))
        )
        self.preferred_forced_subtitle_default_check.setChecked(
            bool(
                payload.get(
                    "preferred_forced_subtitle_default",
                    self.preferred_forced_subtitle_default_check.isChecked(),
                )
            )
        )
        self._sync_combo_tooltip(self.metadata_combo, self.METADATA_MODE_HELP)
        self._sync_combo_tooltip(self.audio_name_style_combo, self.AUDIO_NAME_STYLE_HELP)
        self._language_order_style_changed()

    def _apply_current_profile(self) -> None:
        profile_name = self._current_profile_name()
        if profile_name:
            self._applying_profile = True
            try:
                self._apply_profile_payload(self.profiles.get(profile_name, {}))
            finally:
                self._applying_profile = False
        self._loaded_profile_name = profile_name
        self._update_profile_state()

    @Slot()
    def _profile_ui_changed(self) -> None:
        if self._applying_profile:
            return
        self._update_profile_state()

    @Slot()
    def _profile_combo_changed(self) -> None:
        requested_profile = self._current_profile_name()
        if requested_profile == self._loaded_profile_name:
            return
        if self._profile_is_dirty():
            answer = QMessageBox.question(
                self,
                "Unsaved profile changes",
                f"Discard unsaved changes to '{self._loaded_profile_name}'?",
                QMessageBox.Yes | QMessageBox.No,
                QMessageBox.No,
            )
            if answer != QMessageBox.Yes:
                previous_block_state = self.profile_combo.blockSignals(True)
                try:
                    index = self.profile_combo.findData(self._loaded_profile_name)
                    self.profile_combo.setCurrentIndex(index if index >= 0 else 0)
                finally:
                    self.profile_combo.blockSignals(previous_block_state)
                return
        self._clear_manual_track_order()
        self._apply_current_profile()
        self._write_profile_store()
        profile_name = self._current_profile_name()
        self.statusBar().showMessage(f"Profile loaded: {profile_name}" if profile_name else "Custom profile")

    @Slot()
    def update_current_profile(self) -> None:
        profile_name = self._current_profile_name()
        if not profile_name:
            QMessageBox.information(self, "Update profile", "Choose a saved profile first, or use Save as.")
            return

        if self._save_loaded_profile_changes():
            self.statusBar().showMessage(f"Profile updated: {profile_name}")

    def _save_loaded_profile_changes(self) -> bool:
        profile_name = self._loaded_profile_name
        if not profile_name or profile_name not in self.profiles:
            return False
        try:
            payload = self._validated_profile_payload_from_ui()
        except ValueError as error:
            QMessageBox.warning(self, "Save profile changes", str(error))
            return False
        previous_payload = self.profiles[profile_name]
        self.profiles[profile_name] = payload
        if not self._write_profile_store():
            self.profiles[profile_name] = previous_payload
            self._update_profile_state()
            QMessageBox.warning(self, "Save profile changes", "Could not write the profile library.")
            return False
        self._update_profile_state()
        return True

    @Slot()
    def save_current_profile(self) -> None:
        current_name = self._current_profile_name()
        name, accepted = QInputDialog.getText(self, "Save profile as", "Profile name:", text=current_name)
        if not accepted:
            return
        name = name.strip()
        if not name:
            QMessageBox.warning(self, "Save profile as", "Choose a profile name.")
            return
        if name.casefold() == self.PROFILE_NONE_LABEL.casefold():
            QMessageBox.warning(self, "Save profile as", f"'{self.PROFILE_NONE_LABEL}' is reserved.")
            return

        existing_name = next((profile for profile in self.profiles if profile.casefold() == name.casefold()), "")
        if existing_name and existing_name != current_name:
            answer = QMessageBox.question(
                self,
                "Save profile as",
                f"Overwrite the existing profile '{existing_name}'?",
                QMessageBox.Yes | QMessageBox.No,
                QMessageBox.No,
            )
            if answer != QMessageBox.Yes:
                return
            name = existing_name

        try:
            payload = self._validated_profile_payload_from_ui()
        except ValueError as error:
            QMessageBox.warning(self, "Save profile as", str(error))
            return
        previous_profiles = dict(self.profiles)
        previous_loaded_profile = self._loaded_profile_name
        self.profiles[name] = payload
        self._loaded_profile_name = name
        self._refresh_profile_combo(name)
        if not self._write_profile_store():
            self.profiles = previous_profiles
            self._loaded_profile_name = previous_loaded_profile
            self._refresh_profile_combo(previous_loaded_profile)
            QMessageBox.warning(self, "Save profile as", "Could not write the profile library.")
            return
        self._update_profile_state()
        self.statusBar().showMessage(f"Profile saved: {name}")

    @Slot()
    def revert_current_profile(self) -> None:
        profile_name = self._loaded_profile_name
        if not profile_name or profile_name not in self.profiles:
            return
        self._applying_profile = True
        try:
            self._apply_profile_payload(self.profiles[profile_name])
        finally:
            self._applying_profile = False
        self._clear_manual_track_order()
        self._update_profile_state()
        self.statusBar().showMessage(f"Profile reverted: {profile_name}")

    @Slot()
    def delete_current_profile(self) -> None:
        profile_name = self._current_profile_name()
        if not profile_name:
            return
        answer = QMessageBox.question(
            self,
            "Delete profile",
            f"Delete profile '{profile_name}'?",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        if answer != QMessageBox.Yes:
            return
        previous_profiles = dict(self.profiles)
        previous_loaded_profile = self._loaded_profile_name
        self.profiles.pop(profile_name, None)
        self._loaded_profile_name = ""
        self._refresh_profile_combo("")
        if not self._write_profile_store():
            self.profiles = previous_profiles
            self._loaded_profile_name = previous_loaded_profile
            self._refresh_profile_combo(previous_loaded_profile)
            QMessageBox.warning(self, "Delete profile", "Could not write the profile library.")
            return
        self._update_profile_state()
        self.statusBar().showMessage(f"Profile deleted: {profile_name}")

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
        self._set_existing_output_mode(self._existing_output_mode_from_args(args))

        self.recursive_check.setChecked(bool(args.recursive))
        self.merge_inputs_check.setChecked(bool(getattr(args, "merge_inputs", False)))
        self.smart_subs_check.setChecked(bool(args.smart_sub_detection))
        self.drop_empty_check.setChecked(bool(args.drop_empty_subs))
        self.duplicate_check.setChecked(bool(getattr(args, "detect_duplicate_tracks", True)))
        self.subtitle_language_duplicates_check.setChecked(
            bool(getattr(args, "detect_subtitle_language_duplicates", False))
        )
        self.disable_track_statistics_tags_check.setChecked(
            bool(getattr(args, "disable_track_statistics_tags", True))
        )
        self.variant_check.setChecked(bool(args.detect_language_variants))
        self.auto_pgs_ocr_check.setChecked(bool(args.auto_pgs_ocr))
        self.auto_commentary_ocr_check.setChecked(bool(args.auto_commentary_ocr))
        self.report_check.setChecked(bool(args.report))
        self.preserve_commentary_names_check.setChecked(bool(getattr(args, "preserve_commentary_names", False)))
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
        custom_order = getattr(args, "custom_language_order", []) or []
        if isinstance(custom_order, (list, tuple)):
            custom_order = ", ".join(str(item) for item in custom_order if str(item).strip())
        self.config_custom_language_order_edit.setText(str(custom_order))
        self.custom_language_order_edit.setText(str(custom_order))
        self.config_use_custom_order_check.setChecked(
            str(getattr(args, "language_order_style", "") or "") == "custom"
        )
        self._sync_combo_tooltip(self.metadata_combo, self.METADATA_MODE_HELP)
        self._sync_combo_tooltip(self.audio_name_style_combo, self.AUDIO_NAME_STYLE_HELP)
        self._language_order_style_changed()
        self.report_format_combo.setCurrentText(args.report_format)

    def _set_input_text(self, text: str) -> None:
        self._syncing_input_edit = True
        self.input_edit.setText(text)
        self._syncing_input_edit = False

    def _clear_manual_track_order(self) -> None:
        self.manual_track_order = []
        self.manual_track_order_active = False

    @Slot(str)
    def _manual_input_changed(self, _text: str) -> None:
        if self._syncing_input_edit:
            return
        if self.input_paths:
            self.input_paths = []
            self.current_reports = []
            self.manual_track_includes = {}
            self._clear_manual_track_order()
            self._refresh_file_list()
            self.tracks_table.setRowCount(0)
            self._set_track_selection_controls_enabled(False)
            self._update_track_details_for_selection()

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
        self._schedule_audio_sync_auto_load()

    def _clear_audio_sync_loaded_streams(self) -> None:
        self.audio_sync_reference_streams = []
        self.audio_sync_source_streams = []
        self.audio_sync_stream_paths = None
        self.audio_sync_reference_duration_seconds = None
        self.audio_sync_source_duration_seconds = None
        self.audio_sync_result = None
        self.audio_sync_ref_combo.clear()
        self.audio_sync_source_combo.clear()
        self.audio_sync_tracks_table.setRowCount(0)
        self.audio_sync_apply_organizer_button.setEnabled(False)
        self.audio_sync_export_button.setEnabled(False)
        self._set_audio_sync_selection_controls_enabled(False)
        self._refresh_audio_sync_analysis_plan()

    def _audio_sync_path_text_edited(self) -> None:
        self._clear_audio_sync_loaded_streams()
        self._schedule_audio_sync_auto_load()

    def _current_audio_sync_valid_paths(self) -> tuple[Path, Path] | None:
        if not self.audio_sync_reference_edit.text().strip() or not self.audio_sync_source_edit.text().strip():
            return None
        try:
            return self._audio_sync_paths()
        except Exception:
            return None

    def _audio_sync_streams_loaded_for_current_paths(self) -> bool:
        paths = self._current_audio_sync_valid_paths()
        return bool(
            paths
            and self.audio_sync_stream_paths == paths
            and self.audio_sync_reference_streams
            and self.audio_sync_source_streams
        )

    def _schedule_audio_sync_auto_load(self) -> None:
        if self._workflow_is_running():
            return
        if self._audio_sync_streams_loaded_for_current_paths():
            return
        if not self._current_audio_sync_valid_paths():
            self.audio_sync_auto_load_timer.stop()
            return
        if self.audio_sync_probe_thread and self.audio_sync_probe_thread.isRunning():
            self.audio_sync_probe_retry_after_finish = True
            return
        self.audio_sync_auto_load_timer.start(350)

    @Slot()
    def start_audio_sync_stream_auto_load(self) -> None:
        self.start_audio_sync_stream_probe(automatic=True)

    def _prepare_audio_sync_stream_probe_ui(self) -> None:
        self._reset_progress_session()
        self.statusBar().showMessage("Loading Audio Sync streams...")

    def start_audio_sync_stream_probe(self, automatic: bool = True) -> bool:
        if self.audio_sync_probe_thread and self.audio_sync_probe_thread.isRunning():
            return False

        paths = self._current_audio_sync_valid_paths()
        if not paths:
            if not automatic:
                QMessageBox.information(
                    self,
                    "Audio Sync",
                    "Choose valid reference and source media files first.",
                )
            return False

        self.audio_sync_auto_load_timer.stop()
        self.audio_sync_probe_automatic = automatic
        self.audio_sync_probe_retry_after_finish = False
        self._prepare_audio_sync_stream_probe_ui()
        self._set_audio_sync_probe_running(True)

        reference_path, source_path = paths
        self.audio_sync_probe_thread = QThread(self)
        self.audio_sync_probe_worker = AudioSyncProbeWorker(reference_path, source_path)
        self.audio_sync_probe_worker.moveToThread(self.audio_sync_probe_thread)
        self.audio_sync_probe_thread.started.connect(self.audio_sync_probe_worker.run)
        self.audio_sync_probe_worker.completed.connect(self.handle_audio_sync_probe_completed)
        self.audio_sync_probe_worker.failed.connect(self.handle_audio_sync_probe_failed)
        self.audio_sync_probe_worker.completed.connect(self.audio_sync_probe_thread.quit)
        self.audio_sync_probe_worker.failed.connect(self.audio_sync_probe_thread.quit)
        self.audio_sync_probe_thread.finished.connect(self._audio_sync_probe_thread_finished)
        self.audio_sync_probe_thread.start()
        return True

    @Slot(object, object, object, object)
    def handle_audio_sync_probe_completed(
        self,
        reference_path: Path,
        source_path: Path,
        reference_probe: audio_sync.MediaProbe,
        source_probe: audio_sync.MediaProbe,
    ) -> None:
        current_paths = self._current_audio_sync_valid_paths()
        if current_paths != (reference_path, source_path):
            self.audio_sync_probe_retry_after_finish = True
            return

        try:
            self._apply_audio_sync_streams(
                reference_path,
                source_path,
                list(reference_probe.streams),
                list(source_probe.streams),
                reference_probe.duration_seconds,
                source_probe.duration_seconds,
            )
            self._reset_progress_session()
        except Exception as error:
            self.append_audio_sync_summary_line(f"Auto-load failed: {error}")
            self._reset_progress_session()
            if self.start_audio_sync_analysis_after_probe or not self.audio_sync_probe_automatic:
                QMessageBox.critical(self, "Audio Sync load failed", str(error))
            self.start_audio_sync_analysis_after_probe = False

    @Slot(str)
    def handle_audio_sync_probe_failed(self, details: str) -> None:
        self.append_audio_sync_log(details)
        first_line = details.splitlines()[-1] if details else "Audio Sync stream load failed."
        self.append_audio_sync_summary_line(f"Auto-load failed: {first_line}")
        self._reset_progress_session()
        if self.start_audio_sync_analysis_after_probe or not self.audio_sync_probe_automatic:
            QMessageBox.critical(self, "Audio Sync load failed", details)
        self.start_audio_sync_analysis_after_probe = False

    @Slot()
    def _audio_sync_probe_thread_finished(self) -> None:
        if self.audio_sync_probe_worker:
            self.audio_sync_probe_worker.deleteLater()
        if self.audio_sync_probe_thread:
            self.audio_sync_probe_thread.deleteLater()
        self.audio_sync_probe_worker = None
        self.audio_sync_probe_thread = None
        self._set_audio_sync_probe_running(False)

        if self.audio_sync_probe_retry_after_finish:
            self.audio_sync_probe_retry_after_finish = False
            self._schedule_audio_sync_auto_load()
            return

        if self.start_audio_sync_analysis_after_probe:
            self.start_audio_sync_analysis_after_probe = False
            if self._audio_sync_streams_loaded_for_current_paths():
                QTimer.singleShot(0, self.start_audio_sync_analysis)

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

    def _matroska_track_ids_by_type(self, source_path: Path, track_type: str) -> list[int]:
        try:
            args, _config_path = self._load_default_args()
            mkvmerge = organizer.resolve_tool_path(
                args.mkvmerge,
                "mkvmerge",
                "MKVMERGE",
                organizer.common_mkvtoolnix_paths("mkvmerge.exe"),
            )
            if not mkvmerge:
                return []
            metadata = organizer.load_metadata(mkvmerge, source_path)
        except Exception as error:
            self.append_audio_sync_summary_line(f"Could not read subtitle track IDs for Organizer delays: {error}")
            return []

        return [
            int(track.get("id"))
            for track in metadata.get("tracks", [])
            if track.get("type") == track_type and track.get("id") is not None
        ]

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
        subtitle_track_ids = self._matroska_track_ids_by_type(source_path, "subtitles")
        if not subtitle_track_ids:
            subtitle_track_ids = [stream.index for stream in self.audio_sync_source_streams if stream.type == "subtitle"]
        subtitle_delay_text = ", ".join(f"{track_id}:{delay_ms:+d}" for track_id in subtitle_track_ids)
        self.input_paths = []
        self.add_input_paths([source_path])
        self.audio_delays_edit.setText(delay_text)
        self.subtitle_delays_edit.setText(subtitle_delay_text)
        self.tabs.setCurrentIndex(0)
        self.append_audio_sync_summary_line(f"Organizer will apply audio delays: {delay_text}")
        if subtitle_delay_text:
            self.append_audio_sync_summary_line(f"Organizer will apply subtitle delays: {subtitle_delay_text}")
        else:
            self.append_audio_sync_summary_line("Organizer found no source subtitles to delay.")
        self.append_audio_sync_summary_line(
            f"Timeline shift: {audio_sync.format_delay_ms(self.audio_sync_result.timeline_shift_seconds)}"
        )
        self.append_audio_sync_summary_line("Run Preview or Run in Organizer to remux with those delayed tracks.")
        self.statusBar().showMessage("Audio Sync delay prepared in Organizer")

    @Slot(int)
    def _audio_sync_analysis_preset_activated(self, index: int) -> None:
        if self.audio_sync_analysis_combo.itemData(index) != self.AUDIO_SYNC_CUSTOM_PRESET:
            self.audio_sync_previous_analysis_index = index
            self._refresh_audio_sync_analysis_plan()
            return
        if self._audio_sync_preset_prompt_active:
            return
        self._audio_sync_preset_prompt_active = True
        try:
            dialog = QDialog(self)
            dialog.setWindowTitle("Custom Audio Sync analysis")
            form = QFormLayout(dialog)

            start_spin = QDoubleSpinBox(dialog)
            start_spin.setRange(0.0, 240.0)
            start_spin.setDecimals(1)
            start_spin.setSuffix(" min")
            start_spin.setValue(self.audio_sync_custom_start_seconds / 60.0)
            duration_spin = QDoubleSpinBox(dialog)
            duration_spin.setRange(10.0, 600.0)
            duration_spin.setDecimals(0)
            duration_spin.setSuffix(" s")
            duration_spin.setValue(self.audio_sync_custom_duration_seconds)
            checkpoints_spin = QSpinBox(dialog)
            checkpoints_spin.setRange(1, 20)
            checkpoints_spin.setValue(self.audio_sync_custom_checkpoints)
            spacing_spin = QDoubleSpinBox(dialog)
            spacing_spin.setRange(0.5, 120.0)
            spacing_spin.setDecimals(1)
            spacing_spin.setSuffix(" min")
            spacing_spin.setValue(self.audio_sync_custom_spacing_seconds / 60.0)
            max_offset_spin = QDoubleSpinBox(dialog)
            max_offset_spin.setRange(0.1, 60.0)
            max_offset_spin.setDecimals(1)
            max_offset_spin.setSuffix(" s")
            max_offset_spin.setValue(self.audio_sync_custom_max_offset_seconds)

            form.addRow("Start", start_spin)
            form.addRow("Window per checkpoint", duration_spin)
            form.addRow("Checkpoints", checkpoints_spin)
            form.addRow("Spacing", spacing_spin)
            form.addRow("Maximum offset", max_offset_spin)
            buttons = QDialogButtonBox(
                QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel,
                parent=dialog,
            )
            buttons.accepted.connect(dialog.accept)
            buttons.rejected.connect(dialog.reject)
            form.addRow(buttons)

            if dialog.exec() == QDialog.DialogCode.Accepted:
                self.audio_sync_custom_start_seconds = start_spin.value() * 60.0
                self.audio_sync_custom_duration_seconds = duration_spin.value()
                self.audio_sync_custom_checkpoints = checkpoints_spin.value()
                self.audio_sync_custom_spacing_seconds = spacing_spin.value() * 60.0
                self.audio_sync_custom_max_offset_seconds = max_offset_spin.value()
                self.audio_sync_previous_analysis_index = index
            else:
                self.audio_sync_analysis_combo.setCurrentIndex(self.audio_sync_previous_analysis_index)
            self._refresh_audio_sync_analysis_plan()
        finally:
            self._audio_sync_preset_prompt_active = False

    def _audio_sync_common_duration(self) -> float | None:
        durations = [
            duration
            for duration in [
                self.audio_sync_reference_duration_seconds,
                self.audio_sync_source_duration_seconds,
            ]
            if duration is not None and duration > 0
        ]
        return min(durations) if len(durations) == 2 else None

    def _current_audio_sync_analysis_plan(self) -> audio_sync.AdaptiveAnalysisPlan:
        mode = str(self.audio_sync_analysis_combo.currentData() or "full")
        common_duration = self._audio_sync_common_duration()
        if mode == self.AUDIO_SYNC_CUSTOM_PRESET:
            return audio_sync.AdaptiveAnalysisPlan(
                mode="custom",
                media_duration_seconds=common_duration,
                start_seconds=self.audio_sync_custom_start_seconds,
                duration_seconds=self.audio_sync_custom_duration_seconds,
                checkpoints=self.audio_sync_custom_checkpoints,
                checkpoint_spacing_seconds=self.audio_sync_custom_spacing_seconds,
                max_offset_seconds=self.audio_sync_custom_max_offset_seconds,
            )
        return audio_sync.adaptive_analysis_plan(common_duration, mode)

    def _refresh_audio_sync_analysis_plan(self) -> None:
        plan = self._current_audio_sync_analysis_plan()
        if plan.media_duration_seconds is None and plan.mode != "custom":
            self.audio_sync_analysis_plan_label.setText(
                "Duration will be detected after loading both files; fallback settings are ready."
            )
            return
        duration_text = (
            f"shared duration {audio_sync.format_time(plan.media_duration_seconds)}; "
            if plan.media_duration_seconds is not None
            else "duration unavailable; "
        )
        if plan.checkpoints > 1:
            range_text = (
                f"{audio_sync.format_time(plan.start_seconds)} to "
                f"{audio_sync.format_time(plan.last_checkpoint_seconds)}, "
                f"about {self._format_audio_sync_seconds(plan.checkpoint_spacing_seconds)} apart"
            )
        else:
            range_text = f"one checkpoint at {audio_sync.format_time(plan.start_seconds)}"
        self.audio_sync_analysis_plan_label.setText(
            f"{duration_text}{plan.checkpoints} checkpoints, {range_text}; "
            f"{self._format_audio_sync_seconds(plan.duration_seconds)} per checkpoint."
        )

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
        self._clear_manual_track_order()
        self._set_input_text("")
        self._refresh_file_list()
        self.tracks_table.setRowCount(0)
        self._set_track_selection_controls_enabled(False)
        self._update_track_details_for_selection()
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
        self.audio_sync_auto_load_timer.stop()
        self.audio_sync_probe_retry_after_finish = False
        self.start_audio_sync_analysis_after_probe = False
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
        self._clear_manual_track_order()
        self._set_input_text("")
        self.output_edit.clear()
        self.files_table.setRowCount(0)
        self.tracks_table.setRowCount(0)
        self._set_track_selection_controls_enabled(False)
        self._update_track_details_for_selection()
        self.summary_edit.clear()
        self.log_edit.clear()
        self._log_line_starts[id(self.log_edit)] = True
        self.output_tabs.setCurrentIndex(0)
        self._apply_default_args(self.default_args)
        self._apply_current_profile()
        self.input_paths = []
        self.current_reports = []
        self.manual_track_includes = {}
        self._clear_manual_track_order()
        self._set_input_text("")
        self.files_table.setRowCount(0)
        self.tracks_table.setRowCount(0)
        self._set_track_selection_controls_enabled(False)
        self._update_track_details_for_selection()
        self.advanced_button.setChecked(False)
        self._set_running(False)
        self.cancel_button.setEnabled(False)

    def _reset_audio_sync_tab(self) -> None:
        self.audio_sync_auto_load_timer.stop()
        self.audio_sync_probe_retry_after_finish = False
        self.start_audio_sync_analysis_after_probe = False
        self.audio_sync_reference_edit.clear()
        self.audio_sync_source_edit.clear()
        self.audio_sync_output_edit.clear()
        self.audio_sync_analysis_combo.setCurrentIndex(0)
        self.audio_sync_custom_start_seconds = 600.0
        self.audio_sync_custom_duration_seconds = 120.0
        self.audio_sync_custom_spacing_seconds = 900.0
        self.audio_sync_custom_checkpoints = 8
        self.audio_sync_custom_max_offset_seconds = 5.0
        self.audio_sync_previous_analysis_index = self.audio_sync_analysis_combo.currentIndex()
        self.audio_sync_summary_edit.clear()
        self.audio_sync_log_edit.clear()
        self._log_line_starts[id(self.audio_sync_log_edit)] = True
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
        self._log_line_starts[id(self.makemkv_log_edit)] = True
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
            self._clear_manual_track_order()
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
            reference_probe = audio_sync.probe_media(reference_path)
            source_probe = audio_sync.probe_media(source_path)
            self._apply_audio_sync_streams(
                reference_path,
                source_path,
                list(reference_probe.streams),
                list(source_probe.streams),
                reference_probe.duration_seconds,
                source_probe.duration_seconds,
            )
        except Exception as error:
            self.append_audio_sync_summary_line(f"Load failed: {error}")
            QMessageBox.critical(self, "Audio Sync load failed", str(error))
            return False

        return True

    def _apply_audio_sync_streams(
        self,
        reference_path: Path,
        source_path: Path,
        reference_streams: list[audio_sync.MediaStream],
        source_streams: list[audio_sync.MediaStream],
        reference_duration_seconds: float | None = None,
        source_duration_seconds: float | None = None,
    ) -> None:
        self.audio_sync_reference_streams = reference_streams
        self.audio_sync_source_streams = source_streams
        self.audio_sync_stream_paths = (reference_path, source_path)
        self.audio_sync_reference_duration_seconds = reference_duration_seconds
        self.audio_sync_source_duration_seconds = source_duration_seconds
        reference_audio = [stream for stream in self.audio_sync_reference_streams if stream.type == "audio"]
        source_audio = [stream for stream in self.audio_sync_source_streams if stream.type == "audio"]
        if not reference_audio:
            raise ValueError("Reference file has no audio streams.")
        if not source_audio:
            raise ValueError("Source file has no audio streams.")

        self._populate_audio_sync_combo(self.audio_sync_ref_combo, reference_audio)
        self._populate_audio_sync_combo(self.audio_sync_source_combo, source_audio)
        auto_selected = self._select_matching_audio_sync_streams(reference_audio, source_audio)
        self._populate_audio_sync_export_table(self.audio_sync_source_streams)
        self.audio_sync_result = None
        self._refresh_audio_sync_analysis_plan()
        self.audio_sync_apply_organizer_button.setEnabled(False)
        self.audio_sync_export_button.setEnabled(False)
        self._set_audio_sync_selection_controls_enabled(self.audio_sync_tracks_table.rowCount() > 0)
        self.audio_sync_summary_edit.clear()
        self.append_audio_sync_summary_line("Streams loaded.")
        self.append_audio_sync_summary_line(f"Reference audio streams: {len(reference_audio)}")
        self.append_audio_sync_summary_line(f"Source audio streams: {len(source_audio)}")
        if self._audio_sync_common_duration() is not None:
            self.append_audio_sync_summary_line(
                f"Shared usable duration: {audio_sync.format_time(self._audio_sync_common_duration() or 0.0)}"
            )
        if auto_selected:
            self.append_audio_sync_summary_line(f"Auto-selected: {auto_selected}")
        self.append_audio_sync_summary_line(
            f"Source subtitle streams: {len([stream for stream in self.audio_sync_source_streams if stream.type == 'subtitle'])}"
        )
        self.append_audio_sync_summary_line()
        self.statusBar().showMessage("Audio Sync streams loaded")

    @Slot()
    def start_audio_sync_analysis(self) -> None:
        if self.audio_sync_worker_thread and self.audio_sync_worker_thread.isRunning():
            return
        if self._other_workflow_is_running():
            QMessageBox.information(self, "Another task is running", "Wait for the current task to finish first.")
            return

        try:
            if not self._audio_sync_streams_loaded_for_current_paths():
                if self.audio_sync_probe_thread and self.audio_sync_probe_thread.isRunning():
                    self.start_audio_sync_analysis_after_probe = True
                    self.statusBar().showMessage("Audio Sync streams are still loading; analysis will start after that")
                    return
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
        self._log_line_starts[id(self.audio_sync_log_edit)] = True
        self.append_audio_sync_summary_line("Analysis started.")
        self.append_audio_sync_summary_line(f"Reference: {settings.reference_path}")
        self.append_audio_sync_summary_line(f"Source: {settings.source_path}")
        self.append_audio_sync_summary_line(
            f"Streams: reference 0:a:{settings.reference_audio_stream}, source 0:a:{settings.source_audio_stream}"
        )
        self.append_audio_sync_summary_line(
            f"Plan: {settings.checkpoints} checkpoints from {audio_sync.format_time(settings.start_seconds)} "
            f"to {audio_sync.format_time(settings.start_seconds + max(0, settings.checkpoints - 1) * settings.checkpoint_spacing_seconds)}, "
            f"{self._format_audio_sync_seconds(settings.duration_seconds)} per checkpoint"
        )
        self.append_audio_sync_summary_line()
        self._start_progress_session("Audio Sync", "Starting analysis")
        self._set_progress_value(settings.checkpoints, 0)
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
        self._start_progress_session("Audio Sync", "Exporting shifted .mka")
        self._set_progress_indeterminate()
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
        self._set_progress_indeterminate()
        self._set_progress_label("Cancelling")
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
        self._set_progress_indeterminate()
        self._set_progress_label("Cancelling")
        self.statusBar().showMessage("Cancelling MakeMKV batch...")

    @Slot()
    def cancel_audio_sync_task(self) -> None:
        if not self.audio_sync_worker:
            return
        self.audio_sync_worker.cancel()
        self.audio_sync_cancel_button.setEnabled(False)
        self._set_progress_indeterminate()
        self._set_progress_label("Cancelling")
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

        self._prepare_organizer_run_ui(dry_run)

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

    def _prepare_organizer_run_ui(self, dry_run: bool) -> None:
        self.summary_edit.clear()
        self.log_edit.clear()
        self._log_line_starts[id(self.log_edit)] = True
        preserve_preview = not dry_run and bool(self.current_reports) and self.tracks_table.rowCount() > 0
        if not preserve_preview:
            self.current_reports = []
            self.tracks_table.setRowCount(0)
            self._set_track_selection_controls_enabled(False)
            self._update_track_details_for_selection()
            self._refresh_file_list(running=True)
        self._start_progress_session("Organizer", "Starting preview" if dry_run else "Starting run")
        self._set_progress_indeterminate()
        self.append_summary_line("Preview started." if dry_run else "Run started.")
        self.statusBar().showMessage("Starting...")
        self._set_running(True)

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
        self._log_line_starts[id(self.makemkv_log_edit)] = True
        self.makemkv_reports = []
        self.makemkv_table.setRowCount(0)
        self._start_progress_session("MakeMKV", "Starting preview" if dry_run else "Starting batch")
        self._set_progress_indeterminate()
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
        existing_output_mode = self._existing_output_mode()
        args.overwrite = existing_output_mode == "overwrite"
        args.skip_existing = existing_output_mode == "skip"
        if self.tracks_table.rowCount() and self.manual_track_order_active:
            self._sync_track_order_from_table()
        args.track_selection_overrides = dict(self.manual_track_includes)
        args.track_order_overrides = list(self.manual_track_order) if self.manual_track_order_active else []
        args.smart_sub_detection = self.smart_subs_check.isChecked()
        args.drop_empty_subs = self.drop_empty_check.isChecked()
        args.detect_duplicate_tracks = self.duplicate_check.isChecked()
        args.detect_subtitle_language_duplicates = self.subtitle_language_duplicates_check.isChecked()
        args.disable_track_statistics_tags = self.disable_track_statistics_tags_check.isChecked()
        args.detect_language_variants = self.variant_check.isChecked()
        args.auto_pgs_ocr = self.auto_pgs_ocr_check.isChecked()
        args.auto_commentary_ocr = self.auto_commentary_ocr_check.isChecked()
        args.report = self.report_check.isChecked()
        args.preserve_commentary_names = self.preserve_commentary_names_check.isChecked()
        args.preferred_audio_first = self.preferred_audio_first_check.isChecked()
        args.preferred_audio_default = self.preferred_audio_default_check.isChecked()
        args.preferred_subtitle_first = self.preferred_subtitle_first_check.isChecked()
        args.preferred_forced_subtitle_default = self.preferred_forced_subtitle_default_check.isChecked()
        args.metadata_edit_mode = self.metadata_combo.currentText()
        args.audio_name_style = self.audio_name_style_combo.currentData() or "auto"
        args.language_order_style = self.language_order_style_combo.currentData() or "default"
        args.regional_order = self.regional_order_combo.currentData() or ""
        args.custom_language_order = self.custom_language_order_edit.text().strip()
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
            raise ValueError("Choose a reference audio stream.")
        if source_stream is None:
            raise ValueError("Choose a source audio stream.")
        plan = self._current_audio_sync_analysis_plan()
        return audio_sync.AudioSyncSettings(
            reference_path=reference_path,
            source_path=source_path,
            reference_audio_stream=int(reference_stream),
            source_audio_stream=int(source_stream),
            start_seconds=plan.start_seconds,
            duration_seconds=plan.duration_seconds,
            checkpoints=plan.checkpoints,
            checkpoint_spacing_seconds=plan.checkpoint_spacing_seconds,
            max_offset_seconds=plan.max_offset_seconds,
            sample_rate=self.AUDIO_SYNC_SAMPLE_RATE,
        )

    def _populate_audio_sync_combo(self, combo: QComboBox, streams: list[audio_sync.MediaStream]) -> None:
        combo.clear()
        for stream in streams:
            combo.addItem(stream.label, stream.relative_index)

    def _select_matching_audio_sync_streams(
        self,
        reference_audio: list[audio_sync.MediaStream],
        source_audio: list[audio_sync.MediaStream],
    ) -> str:
        match = self._best_audio_sync_language_match(reference_audio, source_audio)
        if not match:
            return ""

        reference_stream, source_stream = match
        self._set_audio_sync_combo_to_stream(self.audio_sync_ref_combo, reference_stream)
        self._set_audio_sync_combo_to_stream(self.audio_sync_source_combo, source_stream)
        language_code = self._audio_sync_stream_language_code(reference_stream)
        return organizer.language_display_name(language_code)

    def _best_audio_sync_language_match(
        self,
        reference_audio: list[audio_sync.MediaStream],
        source_audio: list[audio_sync.MediaStream],
    ) -> tuple[audio_sync.MediaStream, audio_sync.MediaStream] | None:
        candidates: list[tuple[tuple[int, int, int, int, int], audio_sync.MediaStream, audio_sync.MediaStream]] = []

        for reference_stream in reference_audio:
            reference_code = self._audio_sync_stream_language_code(reference_stream)
            reference_base = organizer.base_language_code(reference_code)
            if reference_base == "und":
                continue

            for source_stream in source_audio:
                source_code = self._audio_sync_stream_language_code(source_stream)
                source_base = organizer.base_language_code(source_code)
                if source_base == "und":
                    continue

                exact_match = reference_code == source_code
                base_match = reference_base == source_base
                if not exact_match and not base_match:
                    continue

                rank = (
                    0 if reference_base == "eng" else 1,
                    0 if exact_match else 1,
                    self._audio_sync_stream_role_rank(reference_stream) + self._audio_sync_stream_role_rank(source_stream),
                    reference_stream.relative_index,
                    source_stream.relative_index,
                )
                candidates.append((rank, reference_stream, source_stream))

        if not candidates:
            return None

        _rank, reference_stream, source_stream = min(candidates, key=lambda item: item[0])
        return reference_stream, source_stream

    @staticmethod
    def _audio_sync_stream_language_code(stream: audio_sync.MediaStream) -> str:
        return organizer.normalize_language_code(stream.language)

    @staticmethod
    def _audio_sync_stream_role_rank(stream: audio_sync.MediaStream) -> int:
        text = organizer.language_hint_search_text(stream.title)
        return 1 if "commentary" in text or "commentaire" in text or "comentario" in text else 0

    @staticmethod
    def _set_audio_sync_combo_to_stream(combo: QComboBox, stream: audio_sync.MediaStream) -> None:
        index = combo.findData(stream.relative_index)
        if index >= 0:
            combo.setCurrentIndex(index)

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
        args.custom_language_order = organizer.parse_custom_language_order(
            getattr(args, "custom_language_order", None)
        )
        if args.language_order_style == "custom" and not args.custom_language_order:
            raise organizer.OrganizerError(
                "--custom-language-order is required when --language-order-style custom is active."
            )
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
        self._append_timestamped_log(self.log_edit, text)

    @Slot(str)
    def append_makemkv_log(self, text: str) -> None:
        self._append_timestamped_log(self.makemkv_log_edit, text)

    @Slot(str, str, str, int, int, int, int)
    def handle_event(self, kind: str, message: str, file_path: str, index: int, total: int, step: int, steps: int) -> None:
        if index or total:
            self._set_progress_context(index, total)
        if kind in {"batch-progress", "file-progress"} and steps <= 0 and step <= 0:
            self._set_progress_indeterminate()
        elif total:
            steps = steps or 100
            total_units = self._progress_total_units(total, steps)
            if kind == "file-started":
                value = max(0, index - 1) * steps
            elif kind == "file-progress":
                value = max(0, index - 1) * steps + max(0, min(step, steps))
            elif kind in {"file-finished", "file-error", "file-cancelled"}:
                value = index * steps
            else:
                value = self.progress.value()
            self._set_progress_value(
                total_units,
                min(value, total_units - self.FINALIZATION_PROGRESS_UNITS),
            )
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
            self._progress_scope = "Organizer pipeline"
            self.handle_event(kind.removeprefix("organizer-"), message, disc_path, index, total, step, steps)
            return

        self._progress_scope = "MakeMKV"
        if index or total:
            self._set_progress_context(index, total)
        if total:
            steps = steps or 100
            total_units = self._progress_total_units(total, steps)
            if kind == "disc-started":
                value = max(0, index - 1) * steps
            elif kind == "disc-progress":
                value = max(0, index - 1) * steps + max(0, min(step, steps))
            elif kind in {"disc-finished", "disc-error", "disc-cancelled"}:
                value = index * steps
            else:
                value = self.progress.value()
            self._set_progress_value(
                total_units,
                min(value, total_units - self.FINALIZATION_PROGRESS_UNITS),
            )

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
        self._set_progress_value(total_units, total_units)
        self._populate_results(result.reports)
        if result.cancelled:
            self.statusBar().showMessage("Cancelled")
            self._finish_progress_session("Cancelled")
        else:
            self.statusBar().showMessage(
                f"Completed with {result.failures} error(s)" if result.failures else "Completed without errors"
            )
            self._finish_progress_session(
                f"Completed with {result.failures} error(s)" if result.failures else "Completed"
            )
        self._append_organizer_result_summary(result)
        self._set_running(False)

    @Slot(str)
    def handle_failed(self, details: str) -> None:
        self.append_log(details)
        self.statusBar().showMessage("Failed")
        self._finish_progress_session("Failed")
        first_line = details.strip().splitlines()[-1] if details.strip() else "Unknown error"
        self.append_summary_line(f"Run failed: {first_line}")
        self.append_summary_line("See Raw log for the full traceback.")
        self.output_tabs.setCurrentIndex(1)
        QMessageBox.critical(self, "Run failed", details)
        self._set_running(False)

    @Slot(object)
    def handle_makemkv_completed(self, result: makemkv.MakeMkvBatchResult) -> None:
        total = len(result.discs) or len(result.reports) or 1
        total_units = self._progress_total_units(total, 100)
        self._set_progress_value(total_units, total_units)
        self._populate_makemkv_results(result.reports)
        if result.organizer_result:
            self._populate_results(result.organizer_result.reports)

        if result.cancelled:
            self.statusBar().showMessage("MakeMKV batch cancelled")
            self._finish_progress_session("Cancelled")
        elif result.failures:
            self.statusBar().showMessage(f"MakeMKV completed with {result.failures} error(s)")
            self._finish_progress_session(f"Completed with {result.failures} error(s)")
        elif result.organizer_result and result.organizer_result.failures:
            self.statusBar().showMessage(f"Organizer completed with {result.organizer_result.failures} error(s)")
            self._finish_progress_session(
                f"Organizer completed with {result.organizer_result.failures} error(s)"
            )
        elif result.organizer_result:
            self.statusBar().showMessage("MakeMKV and Organizer completed without errors")
            self._finish_progress_session("Pipeline completed")
        else:
            self.statusBar().showMessage("MakeMKV completed without errors")
            self._finish_progress_session("Completed")
        self._append_makemkv_result_summary(result)
        self._set_makemkv_running(False)

    @Slot(str)
    def handle_makemkv_failed(self, details: str) -> None:
        self.append_makemkv_log(details)
        self.statusBar().showMessage("MakeMKV failed")
        self._finish_progress_session("Failed")
        first_line = details.strip().splitlines()[-1] if details.strip() else "Unknown error"
        self.append_makemkv_summary_line(f"MakeMKV failed: {first_line}")
        self.append_makemkv_summary_line("See Raw log for the full traceback.")
        self.makemkv_output_tabs.setCurrentIndex(1)
        QMessageBox.critical(self, "MakeMKV failed", details)
        self._set_makemkv_running(False)

    @Slot(str)
    def handle_audio_sync_log(self, message: str) -> None:
        self.append_audio_sync_log(message)
        if message.startswith("Checkpoint") or message.startswith("  offset=") or message.startswith("  skipped="):
            self.append_audio_sync_summary_line(message)
        if message.startswith("Checkpoint"):
            self._set_progress_label(message)
        self.statusBar().showMessage(message[:160])

    @Slot(int, int)
    def handle_audio_sync_progress(self, index: int, total: int) -> None:
        self._set_progress_context(index, total)
        self._set_progress_value(total, index)
        self._set_progress_label(f"Checkpoint {index}/{total} complete")

    @Slot(object)
    def handle_audio_sync_completed(self, result: audio_sync.AudioSyncResult) -> None:
        self.audio_sync_result = result
        self._set_progress_value(max(1, len(result.estimates)), len(result.estimates))
        self._finish_progress_session("Analysis completed")
        self.append_audio_sync_summary_line()
        self.append_audio_sync_summary_line("Result")
        requested_checkpoints = result.attempted_checkpoints or len(result.estimates)
        used_checkpoints = result.used_checkpoints or len(result.estimates)
        offset_ms = abs(result.median_offset_seconds * 1000)
        shift_ms = abs(result.timeline_shift_seconds * 1000)
        if abs(result.median_offset_seconds) < 0.0005:
            source_timing = "Source is aligned with the reference"
        else:
            timing_direction = "late" if result.median_offset_seconds > 0 else "early"
            source_timing = f"Source is {offset_ms:.2f} ms {timing_direction} relative to the reference"
        if abs(result.timeline_shift_seconds) < 0.0005:
            correction = "No practical source shift is needed"
        else:
            correction_action = "Delay" if result.timeline_shift_seconds > 0 else "Advance"
            correction = f"{correction_action} source by {shift_ms:.2f} ms"

        self.append_audio_sync_summary_line(f"Recommended correction: {correction}")
        self.append_audio_sync_summary_line(
            f"Source offset vs reference: {audio_sync.format_delay_ms(result.median_offset_seconds)}"
        )
        self.append_audio_sync_summary_line(
            f"Timeline shift to apply: {audio_sync.format_delay_ms(result.timeline_shift_seconds)}"
        )
        self.append_audio_sync_summary_line(f"Measured timing: {source_timing}")
        self.append_audio_sync_summary_line(
            f"Delay reliability: {(result.delay_reliability or 'unknown').capitalize()}"
        )
        if result.reliability_reason:
            self.append_audio_sync_summary_line(f"Why: {result.reliability_reason}")
        self.append_audio_sync_summary_line(
            f"Checkpoint coverage: {used_checkpoints} used / {requested_checkpoints} requested"
        )
        if result.unavailable_checkpoints:
            self.append_audio_sync_summary_line(f"Unavailable checkpoints: {result.unavailable_checkpoints}")
        if result.ignored_checkpoints:
            self.append_audio_sync_summary_line(f"Ignored outliers: {result.ignored_checkpoints}")
            self.append_audio_sync_summary_line(f"All-checkpoint spread: {result.all_spread_seconds * 1000:.2f} ms")
        self.append_audio_sync_summary_line(
            f"Timing agreement: {result.consistency.capitalize()} "
            f"(max deviation {result.spread_seconds * 1000:.2f} ms)"
        )
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
        self._set_progress_value(1, 1)
        self._finish_progress_session("Export completed")
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
        self._finish_progress_session("Cancelled" if cancelled else "Failed")
        if not cancelled:
            self.append_audio_sync_summary_line("See Raw log for the full traceback.")
            self.audio_sync_output_tabs.setCurrentIndex(1)
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
            self._set_track_selection_controls_enabled(False)
            self._update_track_details_for_selection()

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
        verification_counts = self._verification_counts(result.reports)
        if verification_counts:
            self.append_summary_line(
                "Verification: "
                f"{verification_counts.get('ok', 0)} ok, "
                f"{verification_counts.get('failed', 0)} failed"
            )
        self._append_plan_summary_lines(self.append_summary_line, result.reports)
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
            verification_counts = self._verification_counts(organizer_result.reports)
            if verification_counts:
                self.append_makemkv_summary_line(
                    "Verification: "
                    f"{verification_counts.get('ok', 0)} ok, "
                    f"{verification_counts.get('failed', 0)} failed"
                )
            self._append_plan_summary_lines(self.append_makemkv_summary_line, organizer_result.reports)
            for output_dir in self._output_dirs(organizer_result.reports):
                self.append_makemkv_summary_line(f"Organizer output: {output_dir}")
        self.append_makemkv_summary_line()

    def _status_counts(self, reports: list[dict]) -> dict[str, int]:
        counts: dict[str, int] = {}
        for report in reports:
            status = str(report.get("status", "unknown") or "unknown")
            counts[status] = counts.get(status, 0) + 1
        return counts

    def _verification_counts(self, reports: list[dict]) -> dict[str, int]:
        counts: dict[str, int] = {}
        for report in reports:
            verification = report.get("verification") or {}
            status = str(verification.get("status") or "")
            if not status:
                continue
            counts[status] = counts.get(status, 0) + 1
        return counts

    def _plan_summary_counts(self, reports: list[dict]) -> dict[str, int]:
        counts: dict[str, int] = {}
        for report in reports:
            summary = report.get("plan_summary") or {}
            for category, count in (summary.get("counts") or {}).items():
                counts[str(category)] = counts.get(str(category), 0) + int(count or 0)
        return counts

    def _plan_summary_items(self, reports: list[dict], limit: int = 8) -> list[dict]:
        items: list[dict] = []
        for report in reports:
            summary = report.get("plan_summary") or {}
            for item in summary.get("items") or []:
                items.append(item)
                if len(items) >= limit:
                    return items
        return items

    def _append_plan_summary_lines(self, append_line, reports: list[dict], limit: int = 8) -> None:
        counts = self._plan_summary_counts(reports)
        if not counts:
            return

        append_line("Planned changes: " + organizer.format_plan_summary_counts({"counts": counts}))
        for item in self._plan_summary_items(reports, limit=limit):
            message = str(item.get("message") or "")
            if not message:
                continue
            reason = f" ({item.get('reason')})" if item.get("reason") else ""
            append_line(f"- {message}{reason}")

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
            self._update_track_details_for_selection()
            return

        report = self.current_reports[row]
        tracks = self._report_tracks(report)
        self._syncing_track_checks = True
        self.tracks_table.setRowCount(len(tracks))
        for track_row, track in enumerate(tracks):
            selection_key = self._track_selection_key(track)
            if "_preview_base_drop" not in track:
                track["_preview_base_drop"] = bool(track.get("drop"))
            base_drop = bool(track.get("_preview_base_drop"))
            include_track = self.manual_track_includes.get(selection_key, not base_drop)
            track["drop"] = not include_track
            plan_text, plan_tooltip, plan_categories = self._track_plan_details(
                report,
                track,
                include_track,
                base_drop,
            )
            track["_preview_plan_categories"] = plan_categories
            values = [
                "",
                track.get("id", ""),
                track.get("source_name", ""),
                self._track_type_label(track.get("type", "")),
                track.get("codec", ""),
                track.get("input_language", ""),
                track.get("output_language", ""),
                track.get("name", ""),
                self._track_flags_text(track),
                self._delay_text(track.get("delay_ms")),
                plan_text,
            ]
            for column, value in enumerate(values):
                item = QTableWidgetItem(str(value))
                if column == self.TRACK_INCLUDE_COLUMN:
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
                if column == self.TRACK_NAME_COLUMN and track.get("original_name"):
                    tooltips.append(f"Original: {track['original_name']}")
                if column == self.TRACK_PLAN_COLUMN and plan_tooltip:
                    tooltips.append(plan_tooltip)
                if track.get("duplicate_reason"):
                    tooltips.append(str(track["duplicate_reason"]))
                if track.get("probable_duplicate_reason"):
                    tooltips.append(str(track["probable_duplicate_reason"]))
                if tooltips:
                    item.setToolTip("\n".join(tooltips))
                self._style_track_item(item, track, column)
                self.tracks_table.setItem(track_row, column, item)
        self._syncing_track_checks = False
        self.tracks_table.resizeColumnsToContents()
        if tracks and not self.tracks_table.selectionModel().selectedRows():
            self.tracks_table.selectRow(0)
        self._set_track_selection_controls_enabled(bool(tracks))
        self._update_track_details_for_selection()

    def _report_tracks(self, report: dict) -> list[dict]:
        tracks = report.get("tracks", {})
        report_tracks = [
            *tracks.get("video", []),
            *tracks.get("audio", []),
            *tracks.get("subtitles", []),
        ]
        report_tracks = self._tracks_in_command_order(report, report_tracks)
        if not self.manual_track_order_active or not self.manual_track_order:
            return report_tracks

        manual_rank = {selection_key: index for index, selection_key in enumerate(self.manual_track_order)}

        def sort_key(item: tuple[int, dict]) -> tuple[int, int]:
            index, track = item
            selection_key = self._track_selection_key(track)
            if selection_key in manual_rank:
                return (0, manual_rank[selection_key])
            return (1, index)

        return [track for _index, track in sorted(enumerate(report_tracks), key=sort_key)]

    def _tracks_in_command_order(self, report: dict, tracks: list[dict]) -> list[dict]:
        command = report.get("command") or []
        try:
            order_index = command.index("--track-order")
            order_text = str(command[order_index + 1])
        except (ValueError, IndexError):
            return tracks

        tracks_by_command_key: dict[tuple[int, int], dict] = {}
        for track in tracks:
            try:
                key = (int(track.get("source_index") or 0), int(track.get("id") or 0))
            except (TypeError, ValueError):
                continue
            tracks_by_command_key[key] = track

        ordered: list[dict] = []
        seen: set[int] = set()
        for raw_part in order_text.split(","):
            source_text, separator, track_text = raw_part.partition(":")
            if not separator:
                continue
            try:
                key = (int(source_text), int(track_text))
            except ValueError:
                continue
            track = tracks_by_command_key.get(key)
            if track is None:
                continue
            ordered.append(track)
            seen.add(id(track))

        if not ordered:
            return tracks
        ordered.extend(track for track in tracks if id(track) not in seen)
        return ordered

    def _track_selection_key(self, track: dict) -> str:
        existing_key = str(track.get("selection_key") or "")
        if existing_key:
            return existing_key
        return organizer.track_selection_key(
            int(track.get("source_index") or 0),
            str(track.get("type") or ""),
            int(track.get("id") or 0),
        )

    def _plan_item_selection_key(self, item: dict) -> str:
        try:
            source_index = int(item.get("source_index") or 0)
            track_type = str(item.get("track_type") or "")
            track_id = int(item.get("track_id") or 0)
        except (TypeError, ValueError):
            return ""
        return organizer.track_selection_key(source_index, track_type, track_id)

    def _plan_items_for_track(self, report: dict, track: dict) -> list[dict]:
        selection_key = self._track_selection_key(track)
        summary = report.get("plan_summary") or {}
        return [
            item for item in summary.get("items") or []
            if self._plan_item_selection_key(item) == selection_key
        ]

    def _track_plan_details(
        self,
        report: dict,
        track: dict,
        include_track: bool,
        base_drop: bool,
    ) -> tuple[str, str, list[str]]:
        items = self._plan_items_for_track(report, track)
        tooltip_lines = []
        for item in items:
            tooltip = self._plan_item_tooltip(item)
            if tooltip:
                tooltip_lines.append(tooltip)
        categories: list[str] = []

        if not include_track:
            categories.append("drop")
            if base_drop:
                return "Remove", "\n".join(tooltip_lines), categories
            return "Exclude manually", "User unchecked this track for the next run.", categories

        labels: list[str] = []
        if base_drop:
            labels.append("Include manually")
            categories.append("manual")
            if tooltip_lines:
                tooltip_lines.insert(0, "Overrides the planned removal from the preview.")

        if track.get("duplicate_group") and track.get("duplicate_of_id") is None:
            labels.append("Duplicate group")
            categories.append("duplicate")

        has_regional_duplicate_item = any(
            str(item.get("category") or "") == "regional_duplicate"
            for item in items
        )
        if track.get("probable_duplicate_group") and not has_regional_duplicate_item:
            labels.append("Regional duplicate?")
            categories.append("regional_duplicate")
            if track.get("probable_duplicate_reason"):
                tooltip_lines.append(str(track["probable_duplicate_reason"]))

        for item in items:
            category = str(item.get("category") or "other")
            if category == "drop":
                continue
            label = self._short_plan_label(category, item, track)
            if label and label not in labels:
                labels.append(label)
            if category not in categories:
                categories.append(category)

        if not labels:
            return "Keep", "No metadata or selection changes planned for this track.", categories
        return " | ".join(labels), "\n".join(tooltip_lines), categories

    def _plan_item_tooltip(self, item: dict) -> str:
        message = str(item.get("message") or "")
        reason = str(item.get("reason") or "")
        if message and reason:
            return f"{message}\nReason: {reason}"
        return message or reason

    def _short_plan_label(self, category: str, item: dict, track: dict) -> str:
        if category == "duplicate":
            return "Duplicate"
        if category == "regional_duplicate":
            return "Regional duplicate?"
        if category == "language":
            input_language = str(track.get("input_language") or "")
            output_language = str(track.get("output_language") or "")
            return f"Language {input_language} -> {output_language}" if input_language and output_language else "Language"
        if category == "name":
            return "Rename"
        if category == "flag":
            message = str(item.get("message") or "")
            return "Default off" if message.startswith("Unset") else "Default on"
        if category == "role":
            role = str(track.get("role") or "").strip().lower()
            message = str(item.get("message") or "").lower()
            if role == "sdh":
                return "Mark SDH"
            if role == "forced":
                return "Mark forced"
            if role == "commentary" or "commentary" in message:
                return "Mark commentary"
            return "Mark role"
        if category == "delay":
            return f"Delay {self._delay_text(track.get('delay_ms'))}".strip()
        return category.replace("_", " ").title()

    def _current_track_rows(self) -> list[dict]:
        row = self.files_table.currentRow()
        if row < 0 or row >= len(self.current_reports):
            return []
        return self._report_tracks(self.current_reports[row])

    def _current_report(self) -> dict | None:
        row = self.files_table.currentRow()
        if row < 0 or row >= len(self.current_reports):
            return None
        return self.current_reports[row]

    @Slot()
    def _update_track_details_for_selection(self) -> None:
        self._sync_track_selection_action_buttons()
        report = self._current_report()
        tracks = self._current_track_rows()
        selected_rows = self._selected_track_rows()
        if report is None or not tracks or not selected_rows:
            self.track_details_edit.setPlainText("No track selected.")
            return

        row = selected_rows[0]
        if row < 0 or row >= len(tracks):
            self.track_details_edit.setPlainText("No track selected.")
            return

        track = tracks[row]
        base_drop = bool(track.get("_preview_base_drop", track.get("drop")))
        include_track = not bool(track.get("drop"))
        plan_text, plan_tooltip, _categories = self._track_plan_details(report, track, include_track, base_drop)
        selection_key = self._track_selection_key(track)
        manual_selection = selection_key in self.manual_track_includes
        source = str(track.get("source_name") or track.get("source_path") or "-")
        input_language = str(track.get("input_language") or "")
        output_language = str(track.get("output_language") or "")
        original_name = str(track.get("original_name") or "-")
        current_name = str(track.get("name") or "-")
        role = str(track.get("role") or "normal")
        delay = self._delay_text(track.get("delay_ms")) or "0 ms"
        reason = self._track_reason(track) or "-"

        lines = [
            f"Track {track.get('id', '')} | {self._track_type_label(str(track.get('type') or ''))} | {source}",
            f"Selection: {'included' if include_track else 'excluded'}"
            f"{' (manual)' if manual_selection else ''}",
            f"Language: {input_language or '-'} -> {output_language or '-'}",
            f"Name: {current_name}",
            f"Original name: {original_name}",
            f"Codec: {track.get('codec') or '-'}",
            "Flags: "
            f"default={self._yes_no(track.get('default')) or 'no'}, "
            f"forced={self._yes_no(track.get('forced')) or 'no'}, "
            f"role={role}, delay={delay}",
            f"Plan: {plan_text}",
            f"Reason: {reason}",
        ]
        if plan_tooltip:
            lines.append("Plan details:")
            lines.extend(f"- {line}" for line in plan_tooltip.splitlines() if line.strip())

        self.track_details_edit.setPlainText("\n".join(lines))

    @Slot(QTableWidgetItem)
    def _track_item_changed(self, item: QTableWidgetItem) -> None:
        if self._syncing_track_checks or item.column() != self.TRACK_INCLUDE_COLUMN:
            return
        selection_key = str(item.data(Qt.UserRole) or "")
        if not selection_key:
            return
        include_track = item.checkState() == Qt.Checked

        tracks = self._current_track_rows()
        if 0 <= item.row() < len(tracks):
            track = tracks[item.row()]
            self._set_manual_track_include(track, include_track)
            track["drop"] = not include_track
            report = self._current_report()
            if report is not None:
                self._refresh_track_row_after_selection(item.row(), report, track, include_track)
        self._set_track_selection_controls_enabled(bool(tracks))

    def _refresh_track_row_after_selection(
        self,
        row: int,
        report: dict,
        track: dict,
        include_track: bool,
        refresh_details: bool = True,
    ) -> None:
        previous_sync_state = self._syncing_track_checks
        self._syncing_track_checks = True
        try:
            base_drop = bool(track.get("_preview_base_drop", track.get("drop")))
            plan_text, plan_tooltip, plan_categories = self._track_plan_details(
                report,
                track,
                include_track,
                base_drop,
            )
            track["_preview_plan_categories"] = plan_categories

            include_item = self.tracks_table.item(row, self.TRACK_INCLUDE_COLUMN)
            if include_item:
                include_item.setToolTip("Included in the remux" if include_track else "Excluded from the remux")

            flags_item = self.tracks_table.item(row, self.TRACK_FLAGS_COLUMN)
            if flags_item:
                flags_item.setText(self._track_flags_text(track))

            plan_item = self.tracks_table.item(row, self.TRACK_PLAN_COLUMN)
            if plan_item:
                plan_item.setText(plan_text)
                tooltip_lines = [plan_tooltip] if plan_tooltip else []
                if track.get("duplicate_reason"):
                    tooltip_lines.append(str(track["duplicate_reason"]))
                if track.get("probable_duplicate_reason"):
                    tooltip_lines.append(str(track["probable_duplicate_reason"]))
                plan_item.setToolTip("\n".join(tooltip_lines))

            for column in range(self.tracks_table.columnCount()):
                row_item = self.tracks_table.item(row, column)
                if row_item:
                    self._style_track_item(row_item, track, column)
        finally:
            self._syncing_track_checks = previous_sync_state

        if refresh_details:
            self._update_track_details_for_selection()

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
        self.manual_track_order_active = True

        current_file_row = self.files_table.currentRow()
        self._populate_tracks_for_row(current_file_row)
        self.tracks_table.clearSelection()
        for row in range(insert_row, min(insert_row + len(moving), self.tracks_table.rowCount())):
            self.tracks_table.selectRow(row)
        self.statusBar().showMessage("Track order updated")

    def _sync_track_order_from_table(self) -> None:
        if not self.manual_track_order_active:
            return
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
    def select_audio_tracks(self) -> None:
        self._set_displayed_track_checks(Qt.Checked, track_type="audio")

    @Slot()
    def select_subtitle_tracks(self) -> None:
        self._set_displayed_track_checks(Qt.Checked, track_type="subtitles")

    @Slot()
    def include_selected_tracks(self) -> None:
        self._set_selected_track_checks(Qt.Checked)

    @Slot()
    def exclude_selected_tracks(self) -> None:
        self._set_selected_track_checks(Qt.Unchecked)

    @Slot()
    def deselect_duplicate_tracks(self) -> None:
        self._set_displayed_track_checks(Qt.Unchecked, duplicate_members_only=True)

    @Slot()
    def deselect_duplicate_audio_tracks(self) -> None:
        self._set_displayed_track_checks(Qt.Unchecked, duplicate_members_only=True, track_type="audio")

    @Slot()
    def deselect_duplicate_subtitle_tracks(self) -> None:
        self._set_displayed_track_checks(Qt.Unchecked, duplicate_members_only=True, track_type="subtitles")

    @Slot()
    def deselect_probable_duplicate_tracks(self) -> None:
        self._set_displayed_track_checks(Qt.Unchecked, probable_duplicate_members_only=True)

    @Slot()
    def reset_track_selection_edits(self) -> None:
        current_keys = self._current_track_selection_keys()
        if current_keys:
            for key in current_keys:
                self.manual_track_includes.pop(key, None)
        else:
            self.manual_track_includes = {}
        self._populate_tracks_for_row(self.files_table.currentRow())
        self.statusBar().showMessage("Track selection reset")

    @Slot()
    def reset_track_order_edits(self) -> None:
        self._clear_manual_track_order()
        self._populate_tracks_for_row(self.files_table.currentRow())
        self.statusBar().showMessage("Track order reset")

    @Slot()
    def reset_track_edits(self) -> None:
        self.manual_track_includes = {}
        self._clear_manual_track_order()
        self._populate_tracks_for_row(self.files_table.currentRow())
        self.statusBar().showMessage("Track edits reset")

    def _set_displayed_track_checks(
        self,
        check_state: Qt.CheckState,
        duplicate_members_only: bool = False,
        probable_duplicate_members_only: bool = False,
        track_type: str | None = None,
    ) -> None:
        tracks = self._current_track_rows()
        report = self._current_report()
        include_track = check_state == Qt.Checked
        previous_sync_state = self._syncing_track_checks
        self._syncing_track_checks = True
        try:
            for row, track in enumerate(tracks):
                if track_type and track.get("type") != track_type:
                    continue
                if duplicate_members_only and track.get("duplicate_of_id") is None:
                    continue
                if probable_duplicate_members_only and track.get("probable_duplicate_of_id") is None:
                    continue
                self._set_manual_track_include(track, include_track)
                track["drop"] = not include_track
                item = self.tracks_table.item(row, self.TRACK_INCLUDE_COLUMN)
                if item:
                    item.setCheckState(check_state)
                if report is not None:
                    self._refresh_track_row_after_selection(
                        row,
                        report,
                        track,
                        include_track,
                        refresh_details=False,
                    )
        finally:
            self._syncing_track_checks = previous_sync_state
        self._set_track_selection_controls_enabled(bool(tracks))
        self._update_track_details_for_selection()

    def _selected_track_rows(self) -> list[int]:
        return sorted({index.row() for index in self.tracks_table.selectionModel().selectedRows()})

    def _set_selected_track_checks(self, check_state: Qt.CheckState) -> None:
        tracks = self._current_track_rows()
        report = self._current_report()
        include_track = check_state == Qt.Checked
        selected_rows = self._selected_track_rows()
        previous_sync_state = self._syncing_track_checks
        self._syncing_track_checks = True
        try:
            for row in selected_rows:
                if not 0 <= row < len(tracks):
                    continue
                track = tracks[row]
                self._set_manual_track_include(track, include_track)
                track["drop"] = not include_track
                item = self.tracks_table.item(row, self.TRACK_INCLUDE_COLUMN)
                if item:
                    item.setCheckState(check_state)
                if report is not None:
                    self._refresh_track_row_after_selection(
                        row,
                        report,
                        track,
                        include_track,
                        refresh_details=False,
                    )
        finally:
            self._syncing_track_checks = previous_sync_state
        self._set_track_selection_controls_enabled(bool(tracks))
        self._update_track_details_for_selection()

    def _current_track_selection_keys(self) -> set[str]:
        return {self._track_selection_key(track) for track in self._current_track_rows()}

    def _track_base_included(self, track: dict) -> bool:
        return not bool(track.get("_preview_base_drop", track.get("drop")))

    def _set_manual_track_include(self, track: dict, include_track: bool) -> None:
        selection_key = self._track_selection_key(track)
        if include_track == self._track_base_included(track):
            self.manual_track_includes.pop(selection_key, None)
        else:
            self.manual_track_includes[selection_key] = include_track

    def _current_manual_selection_count(self) -> int:
        keys = self._current_track_selection_keys()
        if not keys:
            return 0
        return sum(1 for key in keys if key in self.manual_track_includes)

    def _set_track_selection_controls_enabled(self, enabled: bool) -> None:
        manual_selection_count = self._current_manual_selection_count() if enabled else 0
        current_tracks = self._current_track_rows() if enabled else []
        has_audio = any(track.get("type") == "audio" for track in current_tracks)
        has_subtitles = any(track.get("type") == "subtitles" for track in current_tracks)
        has_audio_duplicates = any(
            track.get("type") == "audio" and track.get("duplicate_of_id") is not None
            for track in current_tracks
        )
        has_subtitle_duplicates = any(
            track.get("type") == "subtitles" and track.get("duplicate_of_id") is not None
            for track in current_tracks
        )
        has_probable_duplicates = any(
            track.get("probable_duplicate_of_id") is not None
            for track in current_tracks
        )
        self.track_select_all_button.setEnabled(enabled)
        self.track_select_audio_button.setEnabled(enabled and has_audio)
        self.track_select_subtitles_button.setEnabled(enabled and has_subtitles)
        self._sync_track_selection_action_buttons(enabled, current_tracks)
        self.track_deselect_duplicates_button.setEnabled(
            enabled and (has_audio_duplicates or has_subtitle_duplicates)
        )
        self.track_deselect_duplicate_audio_button.setEnabled(enabled and has_audio_duplicates)
        self.track_deselect_duplicate_subtitles_button.setEnabled(enabled and has_subtitle_duplicates)
        self.track_deselect_probable_duplicates_button.setEnabled(enabled and has_probable_duplicates)
        self.track_reset_selection_button.setEnabled(enabled and manual_selection_count > 0)
        self.track_reset_order_button.setEnabled(enabled and self.manual_track_order_active)
        self.track_reset_button.setEnabled(
            enabled and (manual_selection_count > 0 or self.manual_track_order_active)
        )
        self._update_track_status_label(enabled, manual_selection_count)

    def _sync_track_selection_action_buttons(
        self,
        enabled: bool | None = None,
        current_tracks: list[dict] | None = None,
    ) -> None:
        if enabled is None:
            enabled = self.tracks_table.rowCount() > 0
        if current_tracks is None:
            current_tracks = self._current_track_rows() if enabled else []
        selected_rows = self._selected_track_rows() if enabled else []
        has_selected = any(0 <= row < len(current_tracks) for row in selected_rows)
        self.track_include_selected_button.setEnabled(enabled and has_selected)
        self.track_exclude_selected_button.setEnabled(enabled and has_selected)

    def _update_track_status_label(self, enabled: bool, manual_selection_count: int = 0) -> None:
        if not enabled:
            self.track_status_label.setText("No preview")
            self.track_status_label.setToolTip("")
            return

        tracks = self._current_track_rows()
        total = len(tracks)
        included = sum(1 for track in tracks if not track.get("drop"))
        excluded = total - included
        duplicates = sum(1 for track in tracks if track.get("duplicate_group"))
        regional_duplicates = sum(
            1
            for track in tracks
            if track.get("probable_duplicate_group") and not track.get("duplicate_group")
        )
        parts = [f"{included}/{total} included"]
        if excluded:
            parts.append(f"{excluded} excluded")
        if duplicates:
            parts.append(f"{duplicates} duplicate warning(s)")
        if regional_duplicates:
            parts.append(f"{regional_duplicates} regional warning(s)")
        if manual_selection_count:
            parts.append(f"{manual_selection_count} manual selection edit(s)")
        if self.manual_track_order_active:
            parts.append("manual order")

        self.track_status_label.setText(" | ".join(parts))
        self.track_status_label.setToolTip(
            "Track table status for the selected preview. Manual edits apply to the next run."
        )

    def _track_type_label(self, track_type: str) -> str:
        if track_type == "subtitles":
            return "Subtitle"
        return track_type.title() if track_type else ""

    def _track_reason(self, track: dict) -> str:
        reasons = [
            track.get("duplicate_reason") or "",
            track.get("probable_duplicate_reason") or "",
            track.get("role_reason") or "",
        ]
        reason_text = " | ".join(reason for reason in reasons if reason)
        if reason_text:
            return reason_text
        scores = track.get("role_scores") or {}
        score_parts = [f"{name}:{score}" for name, score in scores.items() if score]
        return ", ".join(score_parts)

    def _track_flags_text(self, track: dict) -> str:
        flags: list[str] = []
        if track.get("default"):
            flags.append("Default")
        if track.get("forced"):
            flags.append("Forced")
        role = str(track.get("role") or "").strip()
        if role and role != "normal":
            flags.append(role.upper() if role == "sdh" else role.title())
        if track.get("drop"):
            flags.append("Excluded")
        return ", ".join(flags)

    def _style_track_item(self, item: QTableWidgetItem, track: dict, column: int | None = None) -> None:
        item.setBackground(QBrush())
        item.setForeground(QBrush())
        column = item.column() if column is None else column

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

        if track.get("probable_duplicate_group"):
            if self.current_theme == "light":
                item.setBackground(QColor("#ffedd5"))
                item.setForeground(QColor("#9a3412"))
            else:
                item.setBackground(QColor("#3b2a14"))
                item.setForeground(QColor("#fdba74"))

        if column == self.TRACK_PLAN_COLUMN:
            self._style_plan_item(item, track)

    def _style_plan_item(self, item: QTableWidgetItem, track: dict) -> None:
        categories = set(track.get("_preview_plan_categories") or [])
        if not categories:
            return

        if "drop" in categories:
            background, foreground = (
                ("#fee2e2", "#991b1b")
                if self.current_theme == "light"
                else ("#4a1d21", "#fecaca")
            )
        elif "duplicate" in categories:
            background, foreground = (
                ("#fff1f2", "#9f1239")
                if self.current_theme == "light"
                else ("#3a1f27", "#ffb4bd")
            )
        elif "regional_duplicate" in categories:
            background, foreground = (
                ("#ffedd5", "#9a3412")
                if self.current_theme == "light"
                else ("#3b2a14", "#fdba74")
            )
        elif "manual" in categories:
            background, foreground = (
                ("#ecfdf5", "#166534")
                if self.current_theme == "light"
                else ("#123524", "#86efac")
            )
        else:
            background, foreground = (
                ("#eff6ff", "#1d4ed8")
                if self.current_theme == "light"
                else ("#15354a", "#7dd3fc")
            )
        item.setBackground(QColor(background))
        item.setForeground(QColor(foreground))

    def _yes_no(self, value) -> str:
        return "Yes" if value else ""

    def _delay_text(self, value) -> str:
        try:
            delay_ms = int(value or 0)
        except (TypeError, ValueError):
            return ""
        return f"{delay_ms:+d} ms" if delay_ms else ""

    def _set_audio_sync_probe_running(self, running: bool) -> None:
        self.audio_sync_check_button.setEnabled(not running)
        self.audio_sync_analyze_button.setEnabled(not running)
        self.audio_sync_apply_organizer_button.setEnabled(bool(self.audio_sync_result) and not running)
        self.audio_sync_export_button.setEnabled(bool(self.audio_sync_result) and not running)
        if self.audio_sync_clear_button:
            self.audio_sync_clear_button.setEnabled(not running)
        if self.audio_sync_reset_button:
            self.audio_sync_reset_button.setEnabled(not running)
        self._set_audio_sync_selection_controls_enabled(self.audio_sync_tracks_table.rowCount() > 0 and not running)

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
        self.audio_sync_analyze_button.setEnabled(not running)
        if self.audio_sync_clear_button:
            self.audio_sync_clear_button.setEnabled(not running)
        if self.audio_sync_reset_button:
            self.audio_sync_reset_button.setEnabled(not running)
        self.audio_sync_apply_organizer_button.setEnabled(bool(self.audio_sync_result) and not running)
        self.audio_sync_export_button.setEnabled(bool(self.audio_sync_result) and not running)
        self._set_audio_sync_selection_controls_enabled(self.audio_sync_tracks_table.rowCount() > 0 and not running)
        self.tracks_table.setEnabled(not running)
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
        audio_sync_probe_running = self.audio_sync_probe_thread and self.audio_sync_probe_thread.isRunning()
        if organizer_running or makemkv_running or audio_sync_running or audio_sync_probe_running:
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
        if self._profile_is_dirty():
            answer = QMessageBox.question(
                self,
                "Unsaved profile changes",
                f"Save changes to '{self._loaded_profile_name}' before closing?",
                QMessageBox.Save | QMessageBox.Discard | QMessageBox.Cancel,
                QMessageBox.Save,
            )
            if answer == QMessageBox.Cancel:
                event.ignore()
                return
            if answer == QMessageBox.Save and not self._save_loaded_profile_changes():
                event.ignore()
                return
        if self._config_is_dirty():
            answer = QMessageBox.question(
                self,
                "Unsaved app defaults",
                "Save changes to the Config tab before closing?",
                QMessageBox.Save | QMessageBox.Discard | QMessageBox.Cancel,
                QMessageBox.Save,
            )
            if answer == QMessageBox.Cancel:
                event.ignore()
                return
            if answer == QMessageBox.Save and not self.save_config_tab():
                event.ignore()
                return
        self._write_profile_store()
        self._taskbar_progress.close()
        event.accept()


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv if argv is None else argv)
    if "--version" in argv:
        print(f"{APP_NAME} {APP_VERSION}")
        return 0

    set_windows_app_user_model_id()
    app = QApplication.instance() or QApplication(argv)
    window = MainWindow()
    if "--taskbar-smoke-test" in argv:
        window.show()
        app.processEvents()
        available = window._taskbar_progress.available
        if available:
            window._set_progress_value(100, 50)
            app.processEvents()
        window.close()
        return 0 if available else 3
    if "--smoke-test" in argv:
        window.show()
        app.processEvents()
        window.close()
        return 0
    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
