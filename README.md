# MKV Track Organizer

MKV Track Organizer is a Python tool for batch-cleaning MKV track metadata and order with MKVToolNix. It can analyze audio, video, and subtitle tracks, rename them consistently, detect common subtitle roles, and write organized outputs without touching the original files by default.

The project currently includes both a command-line interface and an early desktop UI.

## Features

- Batch process one MKV file, a folder, or recursive folders.
- Keep originals safe by writing remuxed files to `_sorted` by default.
- Preview changes before writing output files.
- Rename audio tracks by format, language plus format, or keep existing names.
- Detect forced, empty, commentary, and SDH subtitle tracks.
- Detect regional language variants such as Portuguese, Spanish, French, and Chinese variants.
- Use OCR for PGS subtitles when text is needed for language or role detection.
- Generate TXT/JSON reports for batch runs.
- Use `mkvpropedit` for metadata-only updates when enabled.
- Desktop UI with drag-and-drop input, preview, run, track tables, logs, and progress.

## Requirements

- Python 3.10 or newer.
- MKVToolNix, especially `mkvmerge`, `mkvextract`, and optionally `mkvpropedit`.
- PySide6 for the desktop UI.
- Optional: Tesseract and `seconv` for automatic PGS OCR.

Install the Python UI dependency:

```powershell
python -m pip install -r requirements.txt
```

For tests, install pytest if needed:

```powershell
python -m pip install pytest
```

## Desktop UI

Run:

```powershell
python .\mkv_track_organizer_gui.py
```

Use **Preview** to analyze files without writing outputs. Use **Run** to apply the selected settings.

## Command Line

Preview a folder recursively:

```powershell
python .\mkv_track_organizer.py "D:\Media\Movies" --recursive --dry-run
```

Write organized outputs to the default `_sorted` folder:

```powershell
python .\mkv_track_organizer.py "D:\Media\Movies" --recursive --report
```

Write outputs with a suffix:

```powershell
python .\mkv_track_organizer.py ".\movie.mkv" --output-suffix organized
```

Use metadata-only edits when possible:

```powershell
python .\mkv_track_organizer.py ".\movie.mkv" --metadata-edit-mode auto
```

## Configuration

Copy the example config and adjust your defaults:

```powershell
Copy-Item .\mkv_track_organizer.config.example.json .\mkv_track_organizer.config.json
```

The local `mkv_track_organizer.config.json` file is ignored by git so personal paths and preferences do not get committed.

## Development

Run the test suite:

```powershell
python -m pytest -q
```

Run syntax checks:

```powershell
python -m py_compile .\mkv_track_organizer.py .\mkv_track_organizer_gui.py .\tests\test_mkv_track_organizer.py
```

## Project Status

This is still a work in progress. The core organizer is usable from the CLI, and the desktop UI is being shaped into a more polished app for publishing.
