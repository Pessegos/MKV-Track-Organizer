# Changelog

All notable user-facing changes are recorded here.

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
