# AudiobookAI external code audit — V2.14.2

Three independent model reports (Claude, DeepSeek, Gemini) were assessed
against actual V2.14.1 source. The reports are hypotheses until reproduced.
All work was developed in a separate Git worktree, without touching the live
SSD cache or interrupting a running Chatterbox conversion.

## Confirmed and fixed

| Origin | Finding | Resolution |
| --- | --- | --- |
| Claude 1 | Duplicate first V6 chunk functions shadowed by live V7 functions | Remove dead first definitions; move CLI main below the live V7 functions |
| DeepSeek 1 | Invalid MySQL config type can raise | Check parsed structure before copying known keys |
| DeepSeek 2 | Snapshot opens a second SQLite transaction | Join existing transaction without committing it |
| DeepSeek 3 | Closing the store discards pending writes | Defensive commit on close; rollback on failure |
| DeepSeek 5 | Failing add_job rolls back caller's transaction | Nested savepoint and scoped rollback, leaving caller transaction intact |
| DeepSeek 6 | Short ffprobe timeout can reject huge M4Bs | Already caught TimeoutExpired; now scale timeout by file size and allow override |
| DeepSeek 8 | Timed-out processed mobile operation can silently continue | Wait for started operation result; cancel only unstarted work; shutdown drains queued calls |
| Gemini 1 | Metadata Backup controls removed, causing start_job failure | Restore backup status/actions; fail open if backup status label is missing |
| Gemini 2 | Expressive acting mode omitted in archive subprocess | Thread mode through GUI, validation and export subprocess |
| Gemini 3 | Chunk editor ignores permanent completed parts after queue prune | Merge completed-parts receipts, deduplicating by folder/path |
| Gemini 4 | Text editors and grep falsely detected as running TTS | Check actual Python invocation and script command |
| Gemini 5 | Second GUI or headless --list rewrites running SQLite job status | Acquire GUI singleton before Store startup; inspect headless list with recover_stale=False |
| Gemini 6 | QC acknowledgments ignored by export/integrity | Exclude Reviewed/Dismissed issues, retain stale/missing checks |
| Gemini 7 | Missing recorded WAV creates invalid QC report status | Identify missing WAV separately; allow zero/one sample-rate sets |
| Gemini 8 | Selected job cannot start while auto QC scan running | Defer selected job until scanner exits; allow Pause/Stop cancellation in timer gap |
| Gemini 9 | Part numbering and mobile duplicate detection span acting modes | Scope narrator/acting-mode identities for new editions |
| Gemini 10 | Retried source job with changed export path violates receipt uniqueness | Replace stale receipt for source ID during verified completion transaction |
| Gemini 11 | Cache backfill ignores dialogue flag and multiple modes | Prefer saved boolean, combine compatible mode masks |
| Gemini 12 | Non-breaking spaces cause pronunciation KeyError | Normalize Unicode whitespace consistently after rejecting newlines |
| Gemini 13 | Mobile voice select resets; proxy Origin mismatch; shutdown requests wait | Only refresh changed options, validate forwarded host on loopback, cancel pending requests |
| Gemini low | Headless order reversed, Alt+10 invalid, editor stays on old chapter, Read Along selection conflict, review folder wrong, hidden narrator snapshot import | Correct respective UI and process behaviors |
| Gemini docs | Package/mobile version and developer guide stale | Synchronize to V2.14.2 and update module guide |

## False positives and design qualifications

- **DeepSeek 4 — mobile filename traversal:** The previous Studio handler
  already rejected decoded slash/backslash/NUL and built a safe,
  content-addressed EPUB destination inside the books directory. We
  added an HTTP-boundary check for defense in depth; no original path
  traversal exploit was demonstrated.
- **DeepSeek 7 — unauthenticated static mobile HTML/JS:** The public login
  page, CSS and JavaScript contain no private queue or book data. The
  bearer-authenticated API prevents exposure of private data and
  mutations. Making the login page require a bearer token would
  prevent a first-time browser from loading it. Static assets
  therefore intentionally remain accessible on the loopback listener;
  do not expose this listener through a public router or Funnel.
- **Mobile exactly-once caveat:** Starting mutations now wait for their
  actual result rather than returning an ambiguous timeout. Queued
  requests cancel safely. A forcefully disconnected client could
  still retry an already committed action; a future durable
  idempotency-key design would help in that separate scenario.

## Validation and safe deployment

Run in the existing Chatterbox environment from the project root:

    QT_QPA_PLATFORM=offscreen .venv-chatterbox/bin/python -m unittest discover -s tests -q

The suite uses temporary SQLite databases, synthetic WAV/index
fixtures, mocked FFmpeg where appropriate, local mobile HTTP tests
and an offscreen Qt Settings window. No GPU, real cache deletion,
model downloads or changes to the active audiobook queue are needed.

A running Studio process must finish or pause safely before restarting
to load V2.14.2. Opening a second instance simultaneously is blocked.
Large real-book archival re-encoding, a physical Android proxy and
interrupted-network idempotency remain manual integration checks.
