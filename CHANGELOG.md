# Changelog

## 2.10.2 — Persistent drag-and-drop queue ordering

- Display pending parts in their real execution order (top to bottom) instead of reversed job ID order.
- Drag queued rows or use **Move up**, **Move down** and **Move to end** to reschedule work without stopping an active narrator process.
- Add SQLite queue-position migration, durable ordering across app restarts, and consistent next-job selection. Existing jobs and per-part caches are preserved.
- Mirror queue position to MySQL with backwards-compatible schema migration; keep SQLite authoritative.
- Disable manual reorder controls when search/status filters hide rows; prevent moving active and completed jobs.
- Add tests for drag intent, storage migration, persistence, pending-job dispatch, active-job safety and MySQL argument counts.


## 2.8.8 — Cache cleanup path safety and MySQL snapshot validation

- Enforce raw-cache deletion containment against the configured AudiobookAI cache path; no longer accept arbitrary directories merely because their names begin with `test_`.
- Continue supporting raw-cache symlinks to mounted external SSDs through safe resolved-path comparison.
- Reject incomplete MySQL sync payloads that omit the permanent `completed_parts` ledger before the worker creates a database connection.
- Add tests for arbitrary test-named directory rejection, external symlink cleanup support, and valid/invalid MySQL worker requests.
- Keep narration splitting, pauses, GPU inference, output formats, cache contents and PySide6 6.11.2 unchanged.


## 2.8.7 — Third-party review fixes and export completeness warnings

- Clear Generate's selected EPUB/chapter state on a missing or unloadable source file, even if the previous sections are cached in memory.
- Calculate a labeled **recent-pace ETA** from all observed chunk durations, including very fast cache hits and edited takes, rather than only chunks taking more than a second.
- Warn explicitly about skipped EPUB section ranges inside the selected story span and skipped front/back sections before the regular or metadata-rich archival M4B export.
- Include permanently recorded completed parts in the Archive page even after users clear their finished queue rows.
- Keep both export paths advisory about deliberate chapter omissions; retain strict checks for all selected finished chapters and preserve existing WAV/M4B files.
- Improve CRLF normalization for converter logs and correct a copy-pasted MySQL mirror docstring.
- Add offline tests for missing EPUB state, fully cached/mixed ETAs and full-book gap detection. No database schema, speech engine, cache paths, M4B naming or pinned dependency changes.


## 2.8.6 — Faster queue refresh and selectable log detail

- Increase Queue progress repaint cadence from 2.0 to **0.5 seconds** for active jobs, while preserving native setter deduplication and throttled SQLite checkpoints.
- Tick the elapsed clock every 500 ms; display remains formatted in whole seconds to avoid redundant native updates.
- Add a Compact/Detailed log toggle with remembered preference and bounded history so users can inspect chunk text, cache reuse, duration and explicit completion events while a conversion runs.
- Batch GUI log output every 500 ms and append it in one Qt call; capture formerly skipped spoken-text chunk lines to persistent per-job diagnostic logs.
- Preserve original PySide6 6.11.2 workaround, lazy multimedia loading, narration settings, caches, queue state and completed-part receipts.
- Add pure helper and GUI-method regression tests for buffering, burst behavior, log modes and stdout parsing.


## 2.8.5 — Elapsed conversion clock hotfix

- Restore the elapsed readout beside ETA using a dedicated one-second Qt timer active only during generation.
- Stop the timer when generation finishes or launch fails and skip redundant label updates.
- Correct the sidebar version and add an elapsed-clock regression test.


## 2.8.4 — Compact part filenames and durable completion history

- New exports from Studio's queue use `Book Name - Part(n).m4b`,
  without EPUB ranges in the actual filename.
- Save verified successful exports to an independent SQLite `completed_parts`
  ledger, atomically with job completion, from both GUI and headless conversion.
- Preserve EPUB section list, part number, narrator, output path, completion
  date and file size after the queue job is deleted. Never record interrupted
  or failed parts.
- Next unqueued skips permanently recorded sections; subsequent part numbers
  include history and existing on-disk exports even if older jobs were removed.
- Review audio and complete-book export can access saved parts without
  retaining dozens of completed queue rows.
- Recover verified older, unambiguous M4Bs from filenames after an older queue
  record was removed; require valid audio and chapter count.
- Include completed-parts metadata in consistent SQLite backups and the
  optional MySQL mirror. Preserve existing queue names, pronunciation
  snapshots, narrated WAVs and generated M4Bs.


## 2.8.3 — Readable part M4B filenames

- Name new part exports like `A New Player in the Game - Part 09 (Sections 123-137).m4b`
  instead of using underscores and a random hash.
- Continue part numbering for each book/narrator across separate queue batches.
- Show explicit EPUB section numbers rather than assuming the EPUB section
  indices equal printed chapter numbers. Note discontiguous selections.
- Avoid replacing an existing export by using readable `(2)`, `(3)`, etc.
  suffixes; check both queued names and previously exported files.
- Keep existing job output names, completed audio, narration caches, and frozen
  pronunciation snapshots unchanged for safe interrupted-job recovery.
- Add naming, collision, legacy-compatibility and FFmpeg integration tests.


## 2.8.2 — Qt stability improvements and headless conversion

- Reduce native PySide6 progress widget setter calls during multi-hour TTS jobs.
- Throttle routine chunk repainting to two seconds while preserving every durable
  SQLite checkpoint and all converter operations.
- Retain per-chunk diagnostic messages in job logs, but omit routine cache/generation
  chatter from the Qt log viewer; warnings/errors remain visible.
- Initialize QtMultimedia only when opening Read Along instead of on app startup,
  keeping the FFmpeg/NVIDIA multimedia backend out of ordinary generation sessions.
- Add a Qt-free headless converter runner for existing queued jobs, including
  frozen pronunciation snapshot, saved voice, SQLite progress checkpoints,
  diagnostic logs, and verified M4B completion.
- Prevent the GUI from being opened while a headless runner owns the lock,
  avoiding concurrent writers and accidental recovery-status migration.
- Add focused regression tests for bursty events, unchanged widgets,
  diagnostics and headless resume validation.
- Pin the GUI dependency and installation instructions to **PySide6==6.11.2**.
  The 6.12.0/6.12.0 Shiboken stack had reproducible native `none_dealloc`
  failures in multi-hour Arch/KDE sessions; downgrading to 6.11.2 substantially
  improved observed long-run stability.
- Note that optional `pyside6-pdf` and `pyside6-webengine` 6.12-era packages
  may require removal if they conflict with the pinned runtime. Neither is
  needed by AudiobookAI.
- Repaint throttling alone did not fix the crashes. Headless conversion remains
  available as a Qt-free option for very long unattended jobs.
- The PySide6 version change is an empirical workaround, not proof of the
  underlying native reference-count defect's root cause.


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
