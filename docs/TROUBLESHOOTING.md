# Troubleshooting

## Start here

Use **Check tools** in the relevant tab. Then run **Preview** and inspect **Summary** before **Raw log**. The summary explains normal decisions; the raw log contains commands and external-tool errors needed for diagnosis.

## A required tool is not found

- Organizer requires MKVToolNix (`mkvmerge` and `mkvextract`; `mkvpropedit` is needed for metadata-only edits).
- Audio Sync requires FFmpeg and FFprobe.
- MakeMKV Batch requires `makemkvcon64.exe`.
- Automatic PGS OCR may require Tesseract and `seconv`/Subtitle Edit components.

Install the tool, add it to `PATH`, or configure its explicit path where supported. Restart the application after changing `PATH`.

## OCR appears stuck

PGS OCR is CPU-intensive and can take a long time on subtitle-heavy UHD releases. The progress label should show the active track and OCR stage; Raw log shows the exact external command. A large file on an SSD can still take a long time because storage is not usually the limiting factor.

Do not cancel merely because one percentage remains visible for several minutes. Cancel when the activity text stops changing for an unreasonable period, the external process is no longer using CPU, or Raw log reports an error. Existing `_ocr_cache` results are reused when valid.

## Audio Sync gives inconsistent offsets

- Select the same language and equivalent mix in Reference and Source.
- Avoid commentary, audio description, different edits/cuts, and logo-heavy opening sections.
- Use more checkpoints or wider spacing for long films.
- Low correlation confidence means the number may be consistent but still unreliable.
- Audio Sync corrects one fixed offset; it cannot repair gradual drift or different edits.

The displayed timeline shift is the delay applied to the source. Exported shifted MKA tracks already contain that correction. Organizer remuxes audio and subtitle delays with `mkvmerge`; Matroska tags do not store or control those delays.

## Duplicate or language detection is uncertain

Red rows are strong duplicate groups. Probable regional matches are warnings and remain included until you deselect them. Generic tags such as `dut`, `nor`, `spa`, or `fre` may not contain enough evidence to prove a regional variant, especially for bitmap subtitles without OCR.

Use the track details panel to inspect the reason and source. Manual selection and ordering in Preview always take precedence for the next run.

## Output already exists

The default mode stops instead of overwriting. Choose **Overwrite** only when replacement is intentional, choose **Skip** for completed batch items, or change the output suffix/folder.

## Windows blocks or quarantines the EXE

The release executable is not code-signed. Download only from this repository's Releases page, keep the full extracted folder together, and verify the ZIP digest shown by GitHub when available. Antivirus false positives can be reported with the release version and scanner name.

## Reporting a bug

Include:

- MKV Track Organizer version from **Help > About**.
- The tab and exact action used.
- Summary and the relevant Raw log section.
- A screenshot of the affected preview rows.
- Sanitized `mkvmerge -J` metadata when the problem involves track tags or languages.

Do not upload copyrighted media. A reduced metadata fixture is normally enough to reproduce classification bugs.
