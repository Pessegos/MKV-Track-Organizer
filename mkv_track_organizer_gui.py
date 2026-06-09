from __future__ import annotations

import contextlib
import io
import sys
import traceback
from pathlib import Path

try:
    from PySide6.QtCore import QEvent, QObject, QThread, Qt, Signal, Slot
    from PySide6.QtGui import QCloseEvent, QDragEnterEvent, QDropEvent, QTextCursor
    from PySide6.QtWidgets import (
        QApplication,
        QCheckBox,
        QComboBox,
        QFileDialog,
        QGridLayout,
        QGroupBox,
        QHBoxLayout,
        QHeaderView,
        QLabel,
        QLineEdit,
        QMainWindow,
        QMessageBox,
        QPlainTextEdit,
        QProgressBar,
        QPushButton,
        QSizePolicy,
        QSplitter,
        QStyle,
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


class OrganizerWorker(QObject):
    log = Signal(str)
    event = Signal(str, str, str, int, int, int, int)
    completed = Signal(object)
    failed = Signal(str)

    def __init__(self, args, config_path: Path | None) -> None:
        super().__init__()
        self.args = args
        self.config_path = config_path

    @Slot()
    def run(self) -> None:
        try:
            stream = SignalTextStream(self.log.emit)
            with contextlib.redirect_stdout(stream), contextlib.redirect_stderr(stream):
                result = organizer.run_from_args(self.args, self.config_path, self._emit_event)
            self.completed.emit(result)
        except Exception:
            self.failed.emit(traceback.format_exc())

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


class MainWindow(QMainWindow):
    FILE_COLUMNS = ["Status", "Input", "Output", "Message"]
    FINALIZATION_PROGRESS_UNITS = 10
    TRACK_COLUMNS = [
        "ID",
        "Type",
        "Codec",
        "Input lang",
        "Output lang",
        "Name",
        "Default",
        "Forced",
        "Drop",
        "Role",
        "Reason",
    ]
    AUDIO_NAME_STYLE_HELP = {
        "auto": (
            "Uses format-only names when the file has one audio language, "
            "and adds the language when multiple audio languages are present."
        ),
        "format": "Names audio tracks by codec, channels, and role. Example: DTS-HD MA 5.1.",
        "language-format": "Adds the language before the format. Example: English - DTS-HD MA 5.1.",
        "keep": "Keeps the existing audio track names from the input file.",
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
        self.default_args, self.default_config_path = self._load_default_args()
        self.input_paths: list[Path] = []
        self.current_reports: list[dict] = []
        self._syncing_input_edit = False

        self.input_edit = QLineEdit()
        self.input_edit.setPlaceholderText("Selected source")
        self.output_edit = QLineEdit()
        self.output_edit.setPlaceholderText("Default: _sorted next to the source")
        self.suffix_edit = QLineEdit()
        self.subtitle_language_edit = QLineEdit()
        self.subtitle_language_edit.setPlaceholderText("spa:7,8; fr-CA:9")
        self.forced_ids_edit = QLineEdit()
        self.forced_ids_edit.setPlaceholderText("5,8,12")

        self.recursive_check = QCheckBox("Recursive")
        self.smart_subs_check = QCheckBox("Smart subtitle detection")
        self.drop_empty_check = QCheckBox("Drop empty subtitles")
        self.variant_check = QCheckBox("Detect language variants")
        self.auto_pgs_ocr_check = QCheckBox("Auto PGS OCR")
        self.auto_commentary_ocr_check = QCheckBox("Commentary/SDH OCR")
        self.report_check = QCheckBox("Write report")

        self.metadata_combo = QComboBox()
        self.metadata_combo.addItems(["off", "auto", "only"])
        self.audio_name_style_combo = QComboBox()
        self.audio_name_style_combo.addItem("Auto", "auto")
        self.audio_name_style_combo.addItem("Format only", "format")
        self.audio_name_style_combo.addItem("Language + format", "language-format")
        self.audio_name_style_combo.addItem("Keep existing", "keep")
        self.report_format_combo = QComboBox()
        self.report_format_combo.addItems(["both", "json", "txt"])

        self.advanced_button = QToolButton()
        self.advanced_panel = QWidget()
        self.preview_button = QPushButton("Preview")
        self.run_button = QPushButton("Run")
        self.files_table = QTableWidget(0, len(self.FILE_COLUMNS))
        self.results_table = self.files_table
        self.tracks_table = QTableWidget(0, len(self.TRACK_COLUMNS))
        self.log_edit = QPlainTextEdit()
        self.progress = QProgressBar()

        self._build_ui()
        self._apply_default_args(self.default_args)
        self._connect_signals()
        self._refresh_file_list()

    def _build_ui(self) -> None:
        style = self.style()
        central = QWidget()
        central.setAcceptDrops(True)
        central.installEventFilter(self)
        root = QVBoxLayout(central)
        root.setContentsMargins(12, 12, 12, 10)
        root.setSpacing(10)

        source_group = QGroupBox("Source and output")
        source_group.setAcceptDrops(True)
        source_group.installEventFilter(self)
        source_grid = QGridLayout(source_group)
        source_grid.setColumnStretch(1, 1)

        input_row = QHBoxLayout()
        input_label = QLabel("Input")
        file_button = self._tool_button(QStyle.SP_FileIcon, "Choose MKV files")
        folder_button = self._tool_button(QStyle.SP_DirOpenIcon, "Choose folder")
        clear_button = self._tool_button(QStyle.SP_DialogResetButton, "Clear selected inputs")
        input_row.addWidget(self.input_edit, 1)
        input_row.addWidget(file_button)
        input_row.addWidget(folder_button)
        input_row.addWidget(clear_button)

        browse_output = self._tool_button(QStyle.SP_DirOpenIcon, "Choose output folder")
        output_row = QHBoxLayout()
        output_row.addWidget(self.output_edit, 1)
        output_row.addWidget(browse_output)

        self.recursive_check.setToolTip("When the input is a folder, include MKVs inside subfolders")
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
        self.preview_button.setIcon(style.standardIcon(QStyle.SP_FileDialogContentsView))
        self.preview_button.setToolTip("Analyze with dry-run enabled")
        self.run_button.setIcon(style.standardIcon(QStyle.SP_MediaPlay))
        self.run_button.setToolTip("Run with the selected settings")
        top_bar.addWidget(self.advanced_button)
        top_bar.addStretch(1)
        top_bar.addWidget(self.preview_button)
        top_bar.addWidget(self.run_button)
        root.addLayout(top_bar)

        advanced_layout = QGridLayout(self.advanced_panel)
        advanced_layout.setContentsMargins(18, 0, 0, 0)
        advanced_layout.setColumnStretch(1, 1)
        advanced_layout.setColumnStretch(3, 1)

        self.suffix_edit.setToolTip("Optional suffix before .mkv, for example movie.fixed.mkv")
        self._apply_combo_help(self.metadata_combo, self.METADATA_MODE_HELP)
        self._apply_combo_help(self.audio_name_style_combo, self.AUDIO_NAME_STYLE_HELP)
        self.subtitle_language_edit.setToolTip("Manual language override, for example spa:7,8; fr-CA:9")
        self.forced_ids_edit.setToolTip("Manual forced-subtitle override, for example 5,8,12")
        self.smart_subs_check.setToolTip("Automatically classify forced, empty, commentary, and SDH subtitles")
        self.drop_empty_check.setToolTip("Exclude subtitles classified as empty")
        self.variant_check.setToolTip("Automatically detect language variants such as es-ES vs es-419")
        self.auto_pgs_ocr_check.setToolTip("Run OCR for PGS subtitles when needed for language detection")
        self.auto_commentary_ocr_check.setToolTip("Run OCR for likely commentary/SDH PGS subtitles")
        self.report_check.setToolTip("Write TXT/JSON batch reports")

        metadata_label = QLabel("Metadata mode")
        metadata_label.setToolTip(
            "Controls whether the app can update track metadata directly with mkvpropedit instead of remuxing."
        )
        audio_names_label = QLabel("Audio names")
        audio_names_label.setToolTip("Controls how audio track names are written.")

        advanced_layout.addWidget(QLabel("Output suffix"), 0, 0)
        advanced_layout.addWidget(self.suffix_edit, 0, 1)
        advanced_layout.addWidget(metadata_label, 0, 2)
        advanced_layout.addWidget(self.metadata_combo, 0, 3)
        advanced_layout.addWidget(audio_names_label, 1, 0)
        advanced_layout.addWidget(self.audio_name_style_combo, 1, 1)
        advanced_layout.addWidget(QLabel("Report format"), 1, 2)
        advanced_layout.addWidget(self.report_format_combo, 1, 3)
        advanced_layout.addWidget(QLabel("Language overrides"), 2, 0)
        advanced_layout.addWidget(self.subtitle_language_edit, 2, 1)
        advanced_layout.addWidget(QLabel("Forced IDs"), 2, 2)
        advanced_layout.addWidget(self.forced_ids_edit, 2, 3)

        advanced_toggles = QHBoxLayout()
        for checkbox in [
            self.smart_subs_check,
            self.drop_empty_check,
            self.variant_check,
            self.auto_pgs_ocr_check,
            self.auto_commentary_ocr_check,
            self.report_check,
        ]:
            advanced_toggles.addWidget(checkbox)
        advanced_toggles.addStretch(1)
        advanced_layout.addLayout(advanced_toggles, 3, 0, 1, 4)
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
        self.tracks_table.setHorizontalHeaderLabels(self.TRACK_COLUMNS)
        self.tracks_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeToContents)
        self.tracks_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeToContents)
        self.tracks_table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeToContents)
        self.tracks_table.horizontalHeader().setSectionResizeMode(5, QHeaderView.Stretch)
        self.tracks_table.horizontalHeader().setStretchLastSection(True)
        self.tracks_table.setAlternatingRowColors(True)
        self.tracks_table.setSelectionBehavior(QTableWidget.SelectRows)
        self.tracks_table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.tracks_table.verticalHeader().setVisible(False)
        self.tracks_table.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.tracks_table.setAcceptDrops(True)
        self.tracks_table.installEventFilter(self)
        tracks_layout.addWidget(self.tracks_table)

        self.log_edit.setReadOnly(True)
        self.log_edit.setLineWrapMode(QPlainTextEdit.NoWrap)
        self.log_edit.setAcceptDrops(True)
        self.log_edit.installEventFilter(self)

        work_splitter = QSplitter(Qt.Horizontal)
        work_splitter.addWidget(files_group)
        work_splitter.addWidget(tracks_group)
        work_splitter.setSizes([430, 790])

        splitter = QSplitter(Qt.Vertical)
        splitter.addWidget(work_splitter)
        splitter.addWidget(self.log_edit)
        splitter.setSizes([500, 220])
        root.addWidget(splitter, 1)

        self.progress.setRange(0, 1)
        self.progress.setValue(0)
        self.statusBar().addPermanentWidget(self.progress, 1)
        self.setCentralWidget(central)

        file_button.clicked.connect(self.choose_file)
        folder_button.clicked.connect(self.choose_folder)
        clear_button.clicked.connect(self.clear_inputs)
        browse_output.clicked.connect(self.choose_output_folder)
        self.advanced_button.toggled.connect(self.toggle_advanced)

    def _connect_signals(self) -> None:
        self.preview_button.clicked.connect(self.start_preview)
        self.run_button.clicked.connect(self.start_run)
        self.input_edit.textEdited.connect(self._manual_input_changed)
        self.files_table.itemSelectionChanged.connect(self._populate_tracks_for_selection)
        self.metadata_combo.currentIndexChanged.connect(
            lambda _index: self._sync_combo_tooltip(self.metadata_combo, self.METADATA_MODE_HELP)
        )
        self.audio_name_style_combo.currentIndexChanged.connect(
            lambda _index: self._sync_combo_tooltip(self.audio_name_style_combo, self.AUDIO_NAME_STYLE_HELP)
        )

    def _tool_button(self, icon_id: QStyle.StandardPixmap, tooltip: str) -> QToolButton:
        button = QToolButton()
        button.setIcon(self.style().standardIcon(icon_id))
        button.setToolTip(tooltip)
        button.setAutoRaise(True)
        return button

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

        self.recursive_check.setChecked(bool(args.recursive))
        self.smart_subs_check.setChecked(bool(args.smart_sub_detection))
        self.drop_empty_check.setChecked(bool(args.drop_empty_subs))
        self.variant_check.setChecked(bool(args.detect_language_variants))
        self.auto_pgs_ocr_check.setChecked(bool(args.auto_pgs_ocr))
        self.auto_commentary_ocr_check.setChecked(bool(args.auto_commentary_ocr))
        self.report_check.setChecked(bool(args.report))

        self.metadata_combo.setCurrentText(args.metadata_edit_mode)
        audio_style_index = self.audio_name_style_combo.findData(getattr(args, "audio_name_style", "auto"))
        if audio_style_index >= 0:
            self.audio_name_style_combo.setCurrentIndex(audio_style_index)
        self._sync_combo_tooltip(self.metadata_combo, self.METADATA_MODE_HELP)
        self._sync_combo_tooltip(self.audio_name_style_combo, self.AUDIO_NAME_STYLE_HELP)
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
            self._refresh_file_list()
            self.tracks_table.setRowCount(0)

    @Slot()
    def choose_file(self) -> None:
        paths, _filter = QFileDialog.getOpenFileNames(self, "Choose MKV files", "", "Matroska video (*.mkv)")
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
    def clear_inputs(self) -> None:
        self.input_paths = []
        self.current_reports = []
        self._set_input_text("")
        self._refresh_file_list()
        self.tracks_table.setRowCount(0)

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
            self._sync_input_summary()
            self._refresh_file_list()
            self.tracks_table.setRowCount(0)

    def _is_supported_input_path(self, path: Path) -> bool:
        return path.is_dir() or (path.is_file() and path.suffix.lower() == ".mkv")

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

    def _start_run(self, dry_run: bool) -> None:
        if self.worker_thread and self.worker_thread.isRunning():
            return

        try:
            args, config_path = self._build_args(dry_run)
        except Exception as error:
            QMessageBox.critical(self, "Invalid settings", str(error))
            return

        self.log_edit.clear()
        self.current_reports = []
        self.tracks_table.setRowCount(0)
        self._refresh_file_list(running=True)
        self.progress.setRange(0, 0)
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
        args.subtitle_language_ids = [
            item.strip()
            for item in self.subtitle_language_edit.text().split(";")
            if item.strip()
        ]

        args.recursive = self.recursive_check.isChecked()
        args.dry_run = dry_run
        args.smart_sub_detection = self.smart_subs_check.isChecked()
        args.drop_empty_subs = self.drop_empty_check.isChecked()
        args.detect_language_variants = self.variant_check.isChecked()
        args.auto_pgs_ocr = self.auto_pgs_ocr_check.isChecked()
        args.auto_commentary_ocr = self.auto_commentary_ocr_check.isChecked()
        args.report = self.report_check.isChecked()
        args.metadata_edit_mode = self.metadata_combo.currentText()
        args.audio_name_style = self.audio_name_style_combo.currentData() or "auto"
        args.report_format = self.report_format_combo.currentText()
        return args, config_path

    @Slot(str)
    def append_log(self, text: str) -> None:
        self.log_edit.moveCursor(QTextCursor.End)
        self.log_edit.insertPlainText(text)
        self.log_edit.moveCursor(QTextCursor.End)

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
            }.get(kind)
            if status:
                self._set_file_status(Path(file_path), status, message)
        self.statusBar().showMessage(message)

    def _progress_total_units(self, total: int, steps: int = 100) -> int:
        return max(1, total * steps + self.FINALIZATION_PROGRESS_UNITS)

    @Slot(object)
    def handle_completed(self, result: organizer.BatchRunResult) -> None:
        total_units = self._progress_total_units(len(result.input_files), 100)
        self.progress.setRange(0, total_units)
        self.progress.setValue(total_units)
        self._populate_results(result.reports)
        self.statusBar().showMessage(
            f"Completed with {result.failures} error(s)" if result.failures else "Completed without errors"
        )
        self._set_running(False)

    @Slot(str)
    def handle_failed(self, details: str) -> None:
        self.append_log(details)
        self.statusBar().showMessage("Failed")
        QMessageBox.critical(self, "Run failed", details)
        self._set_running(False)

    @Slot()
    def _thread_finished(self) -> None:
        if self.worker:
            self.worker.deleteLater()
        if self.worker_thread:
            self.worker_thread.deleteLater()
        self.worker = None
        self.worker_thread = None

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

    def _set_file_row(self, row: int, values: list[str], path: Path) -> None:
        key = str(path.resolve()).casefold() if str(path) else ""
        for column, value in enumerate(values):
            item = QTableWidgetItem(str(value))
            if key:
                item.setData(Qt.UserRole, key)
            self.files_table.setItem(row, column, item)

    def _set_file_status(self, path: Path, status: str, message: str) -> None:
        key = str(path.resolve()).casefold()
        row = self._file_row_for_key(key)
        if row is None:
            row = self.files_table.rowCount()
            self.files_table.insertRow(row)
            self._set_file_row(row, ["", str(path), "", ""], path)

        self.files_table.item(row, 0).setText(status)
        self.files_table.item(row, 3).setText(message)
        self.files_table.resizeColumnsToContents()

    def _file_row_for_key(self, key: str) -> int | None:
        for row in range(self.files_table.rowCount()):
            item = self.files_table.item(row, 0)
            if item and item.data(Qt.UserRole) == key:
                return row
        return None

    @Slot()
    def _populate_tracks_for_selection(self) -> None:
        self._populate_tracks_for_row(self.files_table.currentRow())

    def _populate_tracks_for_row(self, row: int) -> None:
        if row < 0 or row >= len(self.current_reports):
            self.tracks_table.setRowCount(0)
            return

        report = self.current_reports[row]
        tracks = self._report_tracks(report)
        self.tracks_table.setRowCount(len(tracks))
        for track_row, track in enumerate(tracks):
            values = [
                track.get("id", ""),
                self._track_type_label(track.get("type", "")),
                track.get("codec", ""),
                track.get("input_language", ""),
                track.get("output_language", ""),
                track.get("name", ""),
                self._yes_no(track.get("default")),
                self._yes_no(track.get("forced")),
                self._yes_no(track.get("drop")),
                track.get("role", ""),
                self._track_reason(track),
            ]
            for column, value in enumerate(values):
                item = QTableWidgetItem(str(value))
                if column == 5 and track.get("original_name"):
                    item.setToolTip(f"Original: {track['original_name']}")
                self.tracks_table.setItem(track_row, column, item)
        self.tracks_table.resizeColumnsToContents()

    def _report_tracks(self, report: dict) -> list[dict]:
        tracks = report.get("tracks", {})
        return [
            *tracks.get("video", []),
            *tracks.get("audio", []),
            *tracks.get("subtitles", []),
        ]

    def _track_type_label(self, track_type: str) -> str:
        if track_type == "subtitles":
            return "Subtitle"
        return track_type.title() if track_type else ""

    def _track_reason(self, track: dict) -> str:
        reason = track.get("role_reason") or ""
        if reason:
            return reason
        scores = track.get("role_scores") or {}
        score_parts = [f"{name}:{score}" for name, score in scores.items() if score]
        return ", ".join(score_parts)

    def _yes_no(self, value) -> str:
        return "Yes" if value else ""

    def _set_running(self, running: bool) -> None:
        self.preview_button.setEnabled(not running)
        self.run_button.setEnabled(not running)

    def _paths_from_mime(self, mime_data) -> list[Path]:
        if not mime_data.hasUrls():
            return []
        paths = []
        for url in mime_data.urls():
            if url.isLocalFile():
                paths.append(Path(url.toLocalFile()))
        return paths

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
        if self.worker_thread and self.worker_thread.isRunning():
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
