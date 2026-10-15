# Changelog

## 2.6.0 (unreleased) — Archive Manager preview

- New Archive page for narrator-specific completed editions.
- Editable title, author, series, volume, narrator and optional artwork metadata.
- Atomic M4B archival packaging with chapter markers and AAC stream copy.
- Regenerate stale complete editions from existing chapter WAVs, never TTS.
- Export to user-selected destinations, including mounted external drives.
- EPUB2/EPUB3 cover lookup and six new regression tests with real FFmpeg.


## 2.5.0 — Pronunciation dictionary

- Added global and EPUB-specific written-to-spoken text rules in a GUI page.
- Pronunciation rules are saved locally and frozen for each new queued part.
- Existing jobs and chapter audio retain their original text and cache keys.
- Preserved per-chapter rule provenance for chunk editing and offline rebuilding.
- Added text-only preview, validation, and automated regression tests.
- No new CUDA or Python dependencies.

## 2.4.x — Production features

- Chunk editing, narrator selection, queue recovery, complete M4B export,
  quality-control scanning, and GUI stability mitigations.
