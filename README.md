# MKV Track Organizer

MKV Track Organizer is a Python tool for batch-cleaning MKV track metadata and order with MKVToolNix. It can analyze audio, video, and subtitle tracks, rename them consistently, detect common subtitle roles, and write organized outputs without touching the original files by default.

The project currently includes a command-line interface, a desktop UI, an early MakeMKV batch workflow, and an audio sync helper for fixed-delay corrections.

## Features

- Batch process one MKV file, a folder, or recursive folders.
- Keep originals safe by writing remuxed files to `_sorted` by default.
- Preview changes before writing output files.
- Rename audio tracks by format, language plus format, or keep existing names.
- Choose default or regional language ordering for audio/subtitle tracks, with optional region priority presets.
- Apply manual audio/subtitle delays by track ID when a source needs sync correction.
- Estimate fixed audio delay between a reference file and a source file, then apply it in Organizer or export shifted audio tracks.
- Detect forced, empty, commentary, and SDH subtitle tracks.
- Detect regional language variants such as Portuguese, Spanish, French, and Chinese variants.
- Use OCR for PGS subtitles when text is needed for language or role detection.
- Generate TXT/JSON reports for batch runs.
- Use `mkvpropedit` for metadata-only updates when enabled.
- Desktop UI with organizer and MakeMKV batch tabs, dark/light theme selector, clearer status colors, preview, run, cancel, summaries, raw logs, and progress.
- Validate configured tools and source/output paths before starting a run.
- Batch convert MakeMKV disc backup folders into MKVs.
- Optionally run MKV Track Organizer automatically after a MakeMKV batch finishes.
- Export selected synced source audio tracks together as one `.mka` file for later muxing.

## Requirements

- Python 3.10 or newer.
- MKVToolNix, especially `mkvmerge`, `mkvextract`, and optionally `mkvpropedit`.
- PySide6 for the desktop UI.
- NumPy for the Audio Sync analyzer.
- Optional: MakeMKV for the batch disc backup to MKV workflow.
- Optional: FFmpeg and FFprobe for the Audio Sync workflow.
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

Use **Check tools** to validate paths and external tools before a run. Use **Preview** to analyze files without writing outputs. Use **Run** to apply the selected settings.

The lower panel separates the readable **Summary** from the full **Raw log**, so normal runs are easier to scan while still keeping the diagnostic output available.

The desktop UI opens in dark mode by default. Use the theme selector in the status bar to switch between **Dark** and **Light**.

The **MakeMKV Batch** tab can convert one disc backup folder or a folder containing multiple disc backup folders. Its selection modes cover English audio, all audio, all tracks, or a custom MakeMKV selection string. Enable **Run Organizer after MakeMKV** to feed the MakeMKV output folder into the Organizer tab settings automatically. MakeMKV runs in robot mode so progress is used when the console output exposes it.

The **Audio Sync** tab compares one reference audio stream against one source audio stream using FFmpeg-decoded checkpoints. Positive source offset means the source is late; the displayed timeline shift is the inverse delay to apply to the source tracks. Duration and spacing use safe presets with a bounded custom option. After analysis, select source audio tracks and either copy the delay into the Organizer tab or export the selected tracks together into one `.mka` file in the `synced` folder.

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

Group languages by broad regions in the output order:

```powershell
python .\mkv_track_organizer.py ".\movie.mkv" --language-order-style regional
```

Choose which regions come first:

```powershell
python .\mkv_track_organizer.py ".\movie.mkv" --language-order-style regional --regional-order asia,americas,europe
```

Apply manual sync delays in milliseconds:

```powershell
python .\mkv_track_organizer.py ".\movie.mkv" --audio-delays 1:150 --subtitle-delays 5:-250
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
python -m py_compile .\mkv_track_organizer.py .\mkv_track_organizer_gui.py .\makemkv_batch.py .\audio_sync.py .\tests\test_mkv_track_organizer.py .\tests\test_makemkv_batch.py .\tests\test_audio_sync.py
```

## Project Status

This is still a work in progress. The core organizer is usable from the CLI, and the desktop UI is being shaped into a more polished app for publishing. The MakeMKV batch and Audio Sync tabs are new workflows and should be tested across more real media before release builds are treated as stable.
