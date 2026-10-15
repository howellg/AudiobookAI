# Changelog

## 2.8.1 — Pronunciation snapshot ID hotfix

- Preserve immutable pronunciation snapshots when older queue entries have been deleted.
- Allocate new queue job IDs above active rows, historical snapshot filenames, and a persistent sequence.
- Prevent new EPUB sections from failing to queue due to reused `job_1.json` or similar names.
- Add regression tests for deleted jobs, existing snapshots, and restarting SQLite.


## 2.8.0 — Reliability & Recovery

- Safely requeue only validated interrupted jobs, retaining cached generation progress.
- Save bounded per-job converter logs with diagnostic failure hints.
- Make consistent SQLite and pronunciation metadata ZIP backups on startup/periodically.
- Add manual backup and SHA256 verification controls; keep 10 rolling auto backups.
- Backups are metadata-only and never overwrite a live library.
- Add regression tests for recovery checks, crash state migration and backups.


## 2.7.0 — Read Along

- Read-only chapter transcript synchronized approximately to saved WAV playback.
- Save and restore the last book, narrator, chapter, and playback position between sessions.
- Click chunks to seek, scrub chapter audio and open the existing audio editor.
- Reload repaired chapter WAVs and applied replacement-take text on editor close.
- Narrator and completed-chapter selection without TTS or database writes.
- Seven offline tests for timeline mapping, missing caches, invalid manifests, rebuild text and persisted seek positions.
- Exact chunk timestamps and original EPUB text fidelity remain follow-up work.


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
