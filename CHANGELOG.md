# Changelog

All notable user-facing changes are recorded here.

## 1.0.21 - 2026-06-24

- Added an Audio Sync queue for freezing reference/source sync setups as sequential analyses.
- Let queued Audio Sync entries restore their saved streams, selected export tracks, result summary, and status from the queue table.
- Reused the existing Audio Sync worker path so manual analysis and queued analysis share the same result formatting and progress behavior.

## 1.0.20 - 2026-06-24

- Added an Organizer queue for freezing the current inputs/settings as sequential remux jobs.
- Added queue controls to add the current Organizer setup, run queued jobs, remove queued jobs, and clear finished entries.
- Kept queued remuxes serial and snapshot-based so the user can keep preparing later jobs without launching multiple heavy muxes at once.

## 1.0.19 - 2026-06-24

- Added automatic Subtitle Edit install/update support from the Dependency Manager.
- Generalized GitHub ZIP dependency installs so supported tools can share the same safe `_tools` workflow.
- Verified the official Subtitle Edit Windows ZIP installs into `_tools\SubtitleEdit` and is picked up by existing tool discovery.

## 1.0.18 - 2026-06-24

- Added automatic `seconv` install/update support from the Dependency Manager.
- Downloaded the official Subtitle Edit `SeConv-Windows-x64.zip` or ARM64 asset based on the current machine and installed it into `_tools\seconv`.
- Kept automatic installs explicitly limited to supported tools; other dependencies still open their official download pages.

## 1.0.17 - 2026-06-24

- Added a Config tab Dependency Manager for MKVToolNix, FFmpeg/FFprobe, MakeMKV, Tesseract, `seconv`, and Subtitle Edit discovery.
- Show external tool status, requirement level, detected version, resolved path, and details in one central dialog.
- Added official download-page shortcuts for missing or outdated tools without attempting automatic installs yet.

## 1.0.16 - 2026-06-24

- Added Organizer support for `.mks` Matroska subtitle-only inputs alongside `.mkv` and `.mka`.
- Updated file discovery, CLI help, desktop file picker filters, and README references to include `.mks`.
- Preserved `.mks` output extensions for subtitle-only inputs and kept merged outputs sensible when mixing video, audio, and subtitle-only Matroska files.

## 1.0.15 - 2026-06-23

- Added Audio Sync detection for linear timing drift, such as 24.000 fps audio against a 23.976 fps Blu-ray timeline.
- Applied drift corrections through Organizer with MKVToolNix `--sync delay,stretch`, preserving stream copy remuxing.
- Expanded automatic Audio Sync search range when loaded files have a duration mismatch large enough to exceed the default 5 second window.
- Updated Audio Sync summaries to explain delay-plus-stretch corrections and show residual timing agreement after the drift fit.
- Allowed manual audio/subtitle sync entries such as `7:69,1.001` while keeping existing `7:69` delay-only entries compatible.

## 1.0.14 - 2026-06-22

- Added a Summary details section with the complete error/warning reason, affected input, and relevant paths.
- Preserved MKVToolNix warning/error lines in run reports, including structural recovery timestamps when provided.
- Added full cell tooltips so truncated file-table messages remain directly inspectable.
- Clarified warning verification wording as track-plan verification rather than full media decoding.

## 1.0.13 - 2026-06-22

- Treated MKVToolNix exit code 1 as a completed remux with warnings instead of an immediate failure.
- Required a non-empty output before accepting a warning result, then ran the full output-plan verification.
- Reported verified warning results as `processed-with-warnings` without increasing the error count.
- Kept fatal exit codes, missing/empty outputs, and verification mismatches as real errors.

## 1.0.12 - 2026-06-22

- Reduced the Audio Sync result to one unambiguous recommended correction instead of four equivalent delay descriptions.
- Added green, amber, and red summary emphasis for reliability, timing agreement, verdicts, and warnings.
- Fixed Windows taskbar progress on systems that expose `ITaskbarList4` but reject direct `ITaskbarList3` creation.
- Kept an `ITaskbarList3` fallback for compatible Windows shells.

## 1.0.11 - 2026-06-22

- Made preview track reordering incremental instead of rebuilding and resizing the entire track table after every drop.
- Preserved existing checkbox, style, tooltip, and selection items while moving rows.
- Verified that the visible manual order is passed unchanged to the final remux arguments.
- Reduced a representative 500-track reorder to a single-digit millisecond operation in local testing.

## 1.0.10 - 2026-06-22

- Replaced the track table's native internal drag mutation with an Organizer-controlled drag operation.
- Prevented dragged tracks from disappearing or being moved to the end when Qt reports the table viewport as the drag source.
- Preserved every preview row and its selection state while applying manual track order changes.
- Added large-preview and alternate drag-source regressions for manual ordering.

## 1.0.9 - 2026-06-22

- Fixed an infinite `itemChanged` recursion when excluding duplicate or probable-duplicate tracks with styled table rows.
- Made bulk duplicate cleanup update existing rows directly, keeping large previews responsive and intact.
- Accepted recoverable `mkvextract` warning results when every requested subtitle output was produced.
- Kept genuine extraction failures fatal when MKVToolNix reports an error or an expected output is missing.
- Added regressions for large duplicate tables and damaged Matroska inputs that MKVToolNix can successfully resynchronize.

## 1.0.8 - 2026-06-20

- Kept Preview tracks visible and read-only while Run processes the selected plan.
- Made individual track check/uncheck updates modify only the affected row instead of rebuilding and resizing the full table.
- Highlighted Audio Sync offset, correction, and timeline-shift lines in the Summary.
- Mirrored determinate and indeterminate workflow progress on the Windows taskbar when supported by the active shell.
- Added a modern Windows compatibility manifest and explicit application identity for packaged builds.

## 1.0.7 - 2026-06-20

- Added generic recovery of `und` track languages from clear track names using MKVToolNix's complete language catalog.
- Fixed Maori audio in Frozen, Encanto, and Moana being remuxed as unknown with an empty language name.
- Kept name-based catalog inference conservative: valid language tags are not replaced by generic title matches.

## 1.0.6 - 2026-06-20

- Increased the default desktop window from 1240x820 to 1400x900 so Audio Sync shows a useful number of tracks alongside its larger Summary.
- Made Audio Sync progress determinate, advancing as each planned checkpoint completes.
- Made Organizer progress use its existing analysis milestones before remux instead of hiding them behind an indeterminate animation.

## 1.0.5 - 2026-06-20

- Rebalanced the Audio Sync workspace so the Summary and Raw log receive more vertical space than the export-track table.
- Stopped the global progress bar from using its animated indeterminate state while streams load automatically.

## 1.0.4 - 2026-06-20

- Reduced the Audio Sync Plan field to match the height and padding of the surrounding comparison controls.

## 1.0.3 - 2026-06-20

- Made the Audio Sync calculated Plan a taller, padded information panel that remains readable when text wraps.

## 1.0.2 - 2026-06-20

- Recalibrated Audio Sync reliability around practical checkpoint agreement: dense full-timeline results within 10 ms can now be high reliability.
- Removed the internal correlation-peak strength from normal checkpoint and result summaries.
- Replaced separate Start, Duration, Checkpoints, Spacing, and Max offset controls with one adaptive Analysis preset.
- Added automatic duration probing and a recommended Full timeline plan that distributes checkpoints across the shorter input without overshooting it.
- Kept Balanced, Quick, and a consolidated Custom dialog for unusual sources.
- Added real-world Hunchback of Notre Dame and Fantasia 2000 regressions.

## 1.0.1 - 2026-06-19

- Reworked Audio Sync conclusions to separate individual signal match strength from practical delay reliability.
- Added consensus-based reliability using checkpoint agreement, coverage, outliers, and usable checkpoint count.
- Improved the result summary with a recommended correction, timing direction, requested/used/unavailable checkpoints, and an explainable verdict.
- Added a Hercules regression where six weak individual matches agree within less than one millisecond and correctly produce high delay reliability.

## 1.0.0 - 2026-06-19

### Organizer

- Added MKV/MKA batch processing, optional multi-source merging, preview, manual track inclusion, and drag ordering.
- Added consistent audio/subtitle naming, regional and custom language ordering, preferred-language rules, and configurable defaults.
- Added exact, language-level, and probable regional duplicate detection without silently removing tracks.
- Added forced, SDH, commentary, empty-subtitle, and regional language-variant detection with optional PGS OCR.
- Added audio and subtitle delay application, output verification, readable plans, reports, and track-statistics-tag suppression.

### Desktop workflows

- Added persistent editable profiles with migration, validation, import/export, unsaved-state warnings, and atomic writes.
- Added fixed-delay Audio Sync with automatic stream loading, robust checkpoint aggregation, Organizer handoff, and combined MKA export.
- Added MakeMKV batch processing with an optional Organizer pipeline.
- Reworked track controls, duplicate warnings, progress, summaries, raw logs, reset/clear behavior, themes, and responsive splitters.

### Release quality

- Added sanitized real-world regressions for Mulan, Atlantis, Ratatouille, and Bambi layouts.
- Added centralized version metadata, an About dialog, packaged documentation, EXE metadata, and packaged-app smoke tests.
