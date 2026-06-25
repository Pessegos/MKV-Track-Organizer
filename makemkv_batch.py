from __future__ import annotations

import csv
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
DISC_IMAGE_SUFFIXES = {".iso"}
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


@dataclass(frozen=True)
class MakeMkvCommandResult:
    return_code: int
    diagnostics: list[str] = field(default_factory=list)


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


def check_makemkv_runtime(makemkv_path: Path, timeout_seconds: int = 15) -> list[str]:
    try:
        result = subprocess.run(
            [str(makemkv_path), "-r", "info"],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout_seconds,
        )
    except subprocess.TimeoutExpired as error:
        raise MakeMkvError(f"MakeMKV did not respond within {timeout_seconds} seconds.") from error
    except OSError as error:
        raise MakeMkvError(f"Could not run MakeMKV: {error}") from error

    diagnostics = collect_diagnostic_messages(result.stdout.splitlines())
    blocking_messages = [
        message
        for message in diagnostics
        if "too old" in message.casefold() or "registration key" in message.casefold()
    ]
    if blocking_messages:
        raise MakeMkvError(blocking_messages[-1])
    return diagnostics


def discover_disc_folders(source_root: Path) -> list[Path]:
    return discover_disc_sources(source_root)


def discover_disc_sources(source_root: Path) -> list[Path]:
    source_root = Path(source_root).expanduser().resolve()
    if not source_root.exists():
        raise MakeMkvError(f"Source path does not exist: {source_root}")
    if source_root.is_file():
        if _looks_like_disc_image(source_root):
            return [source_root]
        raise MakeMkvError(f"Source path is not a supported disc source: {source_root}")

    if _looks_like_disc_folder(source_root):
        return [source_root]

    immediate_sources = _sorted_disc_sources(
        item
        for item in source_root.iterdir()
        if (item.is_dir() and _looks_like_disc_folder(item)) or _looks_like_disc_image(item)
    )
    if immediate_sources:
        return immediate_sources

    recursive_sources = _sorted_disc_sources(
        item
        for item in source_root.rglob("*")
        if (item.is_dir() and _looks_like_disc_folder(item)) or _looks_like_disc_image(item)
    )
    if recursive_sources:
        return recursive_sources

    return [
        folder
        for folder in sorted(source_root.iterdir(), key=lambda item: item.name.casefold())
        if folder.is_dir()
    ]


def build_makemkv_command(
    makemkv_path: Path,
    disc_source: Path,
    output_folder: Path,
    min_length_seconds: int = DEFAULT_MIN_LENGTH_SECONDS,
) -> list[str]:
    return [
        str(makemkv_path),
        "-r",
        f"--minlength={min_length_seconds}",
        "mkv",
        source_spec_for_source(disc_source),
        "all",
        str(output_folder),
    ]


def source_spec_for_source(disc_source: Path) -> str:
    prefix = "iso" if _has_disc_image_suffix(disc_source) else "file"
    return f"{prefix}:{disc_source}"


def output_folder_for_source(output_root: Path, disc_source: Path) -> Path:
    name = disc_source.stem if _has_disc_image_suffix(disc_source) else disc_source.name
    return output_root / name


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


def parse_robot_message(line: str) -> str | None:
    stripped = line.strip()
    if not stripped.startswith("MSG:"):
        return None
    try:
        fields = next(csv.reader([stripped[4:].strip()]))
    except csv.Error:
        return None
    if len(fields) < 4:
        return None
    return fields[3].strip()


def collect_diagnostic_messages(lines) -> list[str]:
    diagnostics: list[str] = []
    for line in lines:
        message = parse_robot_message(line)
        if not message:
            continue
        if message.startswith("MakeMKV v") and "started" in message:
            continue
        diagnostics.append(message)
    return diagnostics


def command_failure_message(command_result: MakeMkvCommandResult) -> str:
    if command_result.diagnostics:
        details = " | ".join(command_result.diagnostics[-3:])
        return f"MakeMKV exited with code {command_result.return_code}: {details}"
    return f"MakeMKV exited with code {command_result.return_code}."


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
    disc_sources = discover_disc_sources(source_root)
    result = MakeMkvBatchResult(discs=disc_sources, output_root=output_root)

    if not disc_sources:
        raise MakeMkvError(f"No disc sources found in: {source_root}")

    _emit(event_callback, "batch-started", f"Disc sources found: {len(disc_sources)}", total=len(disc_sources))
    print(f"MakeMKV: {makemkv_path}")
    print(f"Source: {source_root}")
    print(f"Output: {output_root}")
    print(f"Disc sources found: {len(disc_sources)}")
    print(f"Selection mode: {SELECTION_MODE_LABELS.get(job.selection_mode, job.selection_mode)}")
    print(f"Selection rule: {selection_rule}")

    registry_scope = _null_context() if job.dry_run else temporary_selection_rule(selection_rule)

    try:
        with registry_scope:
            if not job.dry_run:
                output_root.mkdir(parents=True, exist_ok=True)

            for index, disc_source in enumerate(disc_sources, start=1):
                _ensure_not_cancelled(cancel_callback)
                output_folder = output_folder_for_source(output_root, disc_source)
                command = build_makemkv_command(makemkv_path, disc_source, output_folder, min_length)
                _emit(
                    event_callback,
                    "disc-started",
                    f"Processing {disc_source.name}",
                    disc=disc_source,
                    index=index,
                    total=len(disc_sources),
                    step=0,
                    steps=100,
                )
                print()
                print(f"Processing: {disc_source.name}")
                print(f"Command: {format_command(command)}")

                try:
                    if job.dry_run:
                        report = _report(disc_source, output_folder, "dry-run", "Preview only.", command)
                        result.reports.append(report)
                        _emit(
                            event_callback,
                            "disc-finished",
                            f"Previewed {disc_source.name}",
                            disc=disc_source,
                            index=index,
                            total=len(disc_sources),
                            step=100,
                            steps=100,
                        )
                        continue

                    output_folder.mkdir(parents=True, exist_ok=True)
                    command_result = _run_command(command, event_callback, cancel_callback, disc_source, index, len(disc_sources))
                    if command_result.return_code == 0:
                        report = _report(disc_source, output_folder, "processed", "Completed.", command)
                        result.reports.append(report)
                        _emit(
                            event_callback,
                            "disc-finished",
                            f"Finished {disc_source.name}",
                            disc=disc_source,
                            index=index,
                            total=len(disc_sources),
                            step=100,
                            steps=100,
                        )
                    else:
                        result.failures += 1
                        message = command_failure_message(command_result)
                        result.reports.append(_report(disc_source, output_folder, "error", message, command))
                        _emit(
                            event_callback,
                            "disc-error",
                            f"{disc_source.name}: {message}",
                            disc=disc_source,
                            index=index,
                            total=len(disc_sources),
                            step=100,
                            steps=100,
                        )
                except MakeMkvCancelled:
                    result.cancelled = True
                    result.reports.append(_report(disc_source, output_folder, "cancelled", "Operation cancelled.", command))
                    _emit(
                        event_callback,
                        "disc-cancelled",
                        f"Cancelled {disc_source.name}",
                        disc=disc_source,
                        index=index,
                        total=len(disc_sources),
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


def _looks_like_disc_image(path: Path) -> bool:
    return path.is_file() and _has_disc_image_suffix(path)


def _has_disc_image_suffix(path: Path) -> bool:
    return path.suffix.casefold() in DISC_IMAGE_SUFFIXES


def _sorted_disc_sources(paths) -> list[Path]:
    return sorted(paths, key=lambda item: str(item).casefold())


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
) -> MakeMkvCommandResult:
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
    diagnostics: list[str] = []
    try:
        while process.poll() is None or not output_lines.empty():
            _ensure_not_cancelled(cancel_callback)
            try:
                line = output_lines.get(timeout=0.1)
            except queue.Empty:
                continue
            else:
                print(line, end="")
                message = parse_robot_message(line)
                if message and not (message.startswith("MakeMKV v") and "started" in message):
                    diagnostics.append(message)
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
        return MakeMkvCommandResult(process.wait(), diagnostics[-10:])
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
