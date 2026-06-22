# Changelog

All notable user-facing changes are recorded here.

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
