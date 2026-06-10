from __future__ import annotations

import os
import queue
import re
import shutil
import subprocess
import threading
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterator

try:
    import winreg
except ModuleNotFoundError:  # pragma: no cover - Windows-only integration.
    winreg = None


DEFAULT_MIN_LENGTH_SECONDS = 1200
DEFAULT_MAKEMKV_PATHS = [
    Path(r"C:\Program Files (x86)\MakeMKV\makemkvcon64.exe"),
    Path(r"C:\Program Files\MakeMKV\makemkvcon64.exe"),
]
REGISTRY_KEY_PATH = r"Software\MakeMKV"
SELECTION_VALUE_NAME = "app_DefaultSelectionString"
ROBOT_PROGRESS_RE = re.compile(r"^PRGV:\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)")

SELECTION_RULES = {
    "english": (
        "-sel:all,"
        "+sel:video,"
        "+sel:(audio*eng),"
        "+sel:subtitle,"
        "+sel:attachment,"
        "-sel:core,"
        "=100:all,"
        "-10:(audio*eng)"
    ),
    "all-audio": (
        "-sel:all,"
        "+sel:video,"
        "+sel:audio,"
        "+sel:subtitle,"
        "+sel:attachment,"
        "-sel:core,"
        "=100:all"
    ),
    "all-tracks": "+sel:all,-sel:core,=100:all",
}

SELECTION_MODE_LABELS = {
    "english": "English audio",
    "all-audio": "All audio",
    "all-tracks": "All tracks",
    "custom": "Custom",
}


class MakeMkvError(RuntimeError):
    """Raised when the MakeMKV batch runner cannot continue."""


class MakeMkvCancelled(MakeMkvError):
    """Raised when a MakeMKV batch run is cancelled."""


@dataclass(frozen=True)
class MakeMkvBatchEvent:
    kind: str
    message: str
    disc: Path | None = None
    index: int | None = None
    total: int | None = None
    step: int | None = None
    steps: int | None = None


@dataclass
class MakeMkvBatchJob:
    source_root: Path
    output_root: Path
    makemkv_path: Path | None = None
    min_length_seconds: int = DEFAULT_MIN_LENGTH_SECONDS
    selection_mode: str = "english"
    custom_selection_rule: str = ""
    dry_run: bool = False
    run_organizer_after: bool = False


@dataclass
class MakeMkvBatchResult:
    reports: list[dict] = field(default_factory=list)
    failures: int = 0
    discs: list[Path] = field(default_factory=list)
    output_root: Path | None = None
    cancelled: bool = False
    organizer_result: object | None = None

    @property
    def return_code(self) -> int:
        if self.cancelled:
            return 130
        return 1 if self.failures else 0


def selection_rule_for_mode(mode: str, custom_rule: str = "") -> str:
    if mode == "custom":
        rule = custom_rule.strip()
        if not rule:
            raise MakeMkvError("Custom MakeMKV selection rule is empty.")
        return rule
    try:
        return SELECTION_RULES[mode]
    except KeyError as error:
        raise MakeMkvError(f"Unknown MakeMKV selection mode: {mode}") from error


def find_makemkv(explicit_path: Path | None = None) -> Path:
    candidates: list[Path] = []
    if explicit_path:
        candidates.append(Path(explicit_path).expanduser())

    env_path = os.environ.get("MAKEMKVCON")
    if env_path:
        candidates.append(Path(env_path).expanduser())

    candidates.extend(DEFAULT_MAKEMKV_PATHS)

    path_match = shutil.which("makemkvcon64") or shutil.which("makemkvcon")
    if path_match:
        candidates.append(Path(path_match))

    for candidate in candidates:
        if candidate.is_file():
            return candidate.resolve()

    searched = ", ".join(str(path) for path in candidates) or "PATH"
    raise MakeMkvError(f"Could not find makemkvcon. Searched: {searched}")


def discover_disc_folders(source_root: Path) -> list[Path]:
    source_root = Path(source_root).expanduser().resolve()
    if not source_root.exists():
        raise MakeMkvError(f"Source folder does not exist: {source_root}")
    if not source_root.is_dir():
        raise MakeMkvError(f"Source path is not a folder: {source_root}")

    if _looks_like_disc_folder(source_root):
        return [source_root]

    disc_folders = [
        folder
        for folder in sorted(source_root.iterdir(), key=lambda item: item.name.casefold())
        if folder.is_dir() and _looks_like_disc_folder(folder)
    ]
    if disc_folders:
        return disc_folders

    return [
        folder
        for folder in sorted(source_root.iterdir(), key=lambda item: item.name.casefold())
        if folder.is_dir()
    ]


def build_makemkv_command(
    makemkv_path: Path,
    disc_folder: Path,
    output_folder: Path,
    min_length_seconds: int = DEFAULT_MIN_LENGTH_SECONDS,
) -> list[str]:
    return [
        str(makemkv_path),
        "-r",
        f"--minlength={min_length_seconds}",
        "mkv",
        f"file:{disc_folder}",
        "all",
        str(output_folder),
    ]


def format_command(command: list[str]) -> str:
    return " ".join(f'"{part}"' if " " in part else part for part in command)


def parse_robot_progress(line: str) -> tuple[int, int] | None:
    match = ROBOT_PROGRESS_RE.match(line.strip())
    if not match:
        return None

    first, second, third = (int(part) for part in match.groups())
    candidates = [
        (first, third),
        (first, second),
        (second, third),
    ]
    for current, maximum in candidates:
        if maximum > 0 and 0 <= current <= maximum:
            return current, maximum
    return None


def run_batch(
    job: MakeMkvBatchJob,
    event_callback: Callable[[MakeMkvBatchEvent], None] | None = None,
    cancel_callback: Callable[[], bool] | None = None,
) -> MakeMkvBatchResult:
    source_root = Path(job.source_root).expanduser().resolve()
    output_root = Path(job.output_root).expanduser().resolve()
    min_length = max(0, int(job.min_length_seconds))
    selection_rule = selection_rule_for_mode(job.selection_mode, job.custom_selection_rule)
    makemkv_path = find_makemkv(job.makemkv_path)
    disc_folders = discover_disc_folders(source_root)
    result = MakeMkvBatchResult(discs=disc_folders, output_root=output_root)

    if not disc_folders:
        raise MakeMkvError(f"No disc folders found in: {source_root}")

    _emit(event_callback, "batch-started", f"Disc folders found: {len(disc_folders)}", total=len(disc_folders))
    print(f"MakeMKV: {makemkv_path}")
    print(f"Source: {source_root}")
    print(f"Output: {output_root}")
    print(f"Disc folders found: {len(disc_folders)}")
    print(f"Selection mode: {SELECTION_MODE_LABELS.get(job.selection_mode, job.selection_mode)}")
    print(f"Selection rule: {selection_rule}")

    registry_scope = _null_context() if job.dry_run else temporary_selection_rule(selection_rule)

    try:
        with registry_scope:
            if not job.dry_run:
                output_root.mkdir(parents=True, exist_ok=True)

            for index, disc_folder in enumerate(disc_folders, start=1):
                _ensure_not_cancelled(cancel_callback)
                output_folder = output_root / disc_folder.name
                command = build_makemkv_command(makemkv_path, disc_folder, output_folder, min_length)
                _emit(
                    event_callback,
                    "disc-started",
                    f"Processing {disc_folder.name}",
                    disc=disc_folder,
                    index=index,
                    total=len(disc_folders),
                    step=0,
                    steps=100,
                )
                print()
                print(f"Processing: {disc_folder.name}")
                print(f"Command: {format_command(command)}")

                try:
                    if job.dry_run:
                        report = _report(disc_folder, output_folder, "dry-run", "Preview only.", command)
                        result.reports.append(report)
                        _emit(
                            event_callback,
                            "disc-finished",
                            f"Previewed {disc_folder.name}",
                            disc=disc_folder,
                            index=index,
                            total=len(disc_folders),
                            step=100,
                            steps=100,
                        )
                        continue

                    output_folder.mkdir(parents=True, exist_ok=True)
                    return_code = _run_command(command, event_callback, cancel_callback, disc_folder, index, len(disc_folders))
                    if return_code == 0:
                        report = _report(disc_folder, output_folder, "processed", "Completed.", command)
                        result.reports.append(report)
                        _emit(
                            event_callback,
                            "disc-finished",
                            f"Finished {disc_folder.name}",
                            disc=disc_folder,
                            index=index,
                            total=len(disc_folders),
                            step=100,
                            steps=100,
                        )
                    else:
                        result.failures += 1
                        message = f"MakeMKV exited with code {return_code}."
                        result.reports.append(_report(disc_folder, output_folder, "error", message, command))
                        _emit(
                            event_callback,
                            "disc-error",
                            f"{disc_folder.name}: {message}",
                            disc=disc_folder,
                            index=index,
                            total=len(disc_folders),
                            step=100,
                            steps=100,
                        )
                except MakeMkvCancelled:
                    result.cancelled = True
                    result.reports.append(_report(disc_folder, output_folder, "cancelled", "Operation cancelled.", command))
                    _emit(
                        event_callback,
                        "disc-cancelled",
                        f"Cancelled {disc_folder.name}",
                        disc=disc_folder,
                        index=index,
                        total=len(disc_folders),
                    )
                    raise
    except MakeMkvCancelled:
        result.cancelled = True
        print()
        print("MakeMKV batch cancelled.")
        _emit(event_callback, "batch-cancelled", "MakeMKV batch cancelled.")
        return result

    if result.failures:
        message = f"MakeMKV batch completed with {result.failures} error(s)."
    else:
        message = "MakeMKV batch completed without errors."
    print()
    print(message)
    _emit(event_callback, "batch-finished", message)
    return result


@contextmanager
def temporary_selection_rule(selection_rule: str) -> Iterator[None]:
    previous_value, had_previous_value = read_selection_rule()
    write_selection_rule(selection_rule)
    try:
        yield
    finally:
        if had_previous_value:
            write_selection_rule(previous_value or "")
        else:
            delete_selection_rule()


def read_selection_rule() -> tuple[str | None, bool]:
    if winreg is None:
        raise MakeMkvError("MakeMKV selection rules require Windows registry access.")
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, REGISTRY_KEY_PATH, 0, winreg.KEY_READ) as key:
            value, _value_type = winreg.QueryValueEx(key, SELECTION_VALUE_NAME)
            return str(value), True
    except FileNotFoundError:
        return None, False


def write_selection_rule(selection_rule: str) -> None:
    if winreg is None:
        raise MakeMkvError("MakeMKV selection rules require Windows registry access.")
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, REGISTRY_KEY_PATH) as key:
        winreg.SetValueEx(key, SELECTION_VALUE_NAME, 0, winreg.REG_SZ, selection_rule)


def delete_selection_rule() -> None:
    if winreg is None:
        raise MakeMkvError("MakeMKV selection rules require Windows registry access.")
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, REGISTRY_KEY_PATH, 0, winreg.KEY_SET_VALUE) as key:
            winreg.DeleteValue(key, SELECTION_VALUE_NAME)
    except FileNotFoundError:
        return


def _looks_like_disc_folder(folder: Path) -> bool:
    return (folder / "BDMV").is_dir() or (folder / "VIDEO_TS").is_dir()


@contextmanager
def _null_context() -> Iterator[None]:
    yield


def _ensure_not_cancelled(cancel_callback: Callable[[], bool] | None) -> None:
    if cancel_callback and cancel_callback():
        raise MakeMkvCancelled("Operation cancelled.")


def _emit(
    callback: Callable[[MakeMkvBatchEvent], None] | None,
    kind: str,
    message: str,
    disc: Path | None = None,
    index: int | None = None,
    total: int | None = None,
    step: int | None = None,
    steps: int | None = None,
) -> None:
    if callback:
        callback(MakeMkvBatchEvent(kind, message, disc, index, total, step, steps))


def _run_command(
    command: list[str],
    event_callback: Callable[[MakeMkvBatchEvent], None] | None,
    cancel_callback: Callable[[], bool] | None,
    disc_folder: Path,
    index: int,
    total: int,
) -> int:
    process = subprocess.Popen(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    output_lines: queue.Queue[str] = queue.Queue()

    def read_output() -> None:
        if not process.stdout:
            return
        for line in process.stdout:
            output_lines.put(line)

    reader = _start_thread(read_output)
    last_step = 5
    try:
        while process.poll() is None or not output_lines.empty():
            _ensure_not_cancelled(cancel_callback)
            try:
                line = output_lines.get(timeout=0.1)
            except queue.Empty:
                continue
            else:
                print(line, end="")
                progress = parse_robot_progress(line)
                if progress:
                    current, maximum = progress
                    last_step = max(1, min(99, round(current * 100 / maximum)))
                _emit(
                    event_callback,
                    "disc-progress",
                    line.strip() or f"Processing {disc_folder.name}",
                    disc=disc_folder,
                    index=index,
                    total=total,
                    step=last_step,
                    steps=100,
                )
        reader.join(timeout=1)
        return process.wait()
    except MakeMkvCancelled:
        _terminate_process(process)
        raise


def _start_thread(target: Callable[[], None]) -> threading.Thread:
    thread = threading.Thread(target=target, daemon=True)
    thread.start()
    return thread


def _terminate_process(process: subprocess.Popen) -> None:
    if process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=5)


def _report(
    disc_folder: Path,
    output_folder: Path,
    status: str,
    message: str,
    command: list[str],
) -> dict:
    return {
        "input": str(disc_folder),
        "output": str(output_folder),
        "status": status,
        "message": message,
        "command": command,
    }
