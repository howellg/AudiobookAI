# AudiobookAI code walkthrough

This guide accompanies the inline comments and Python docstrings. It describes the **uploaded V2.4 source tree**, not an imagined future API. Start here if you are reading or contributing to AudiobookAI for the first time.

## Start here: which scripts matter?

| File | Responsibility |
| --- | --- |
| `scripts/audiobook_gui_v2.py` | Current PySide6 Studio window: library, narrator choices, batch queue, progress, review, settings |
| `scripts/epub_to_audiobook_v7_studio.py` | Current command-line backend invoked by Studio: EPUB extraction, chapter WAVs, JSON progress events, M4B exports |
| `scripts/test_five_minutes_v7_smooth.py` | Current Chatterbox engine: chunking, audio generation/retry, cache, silence handling, audio assembly |
| `scripts/audiobookai_storage.py` | Persistent SQLite book/job records, migration, optional direct MySQL mirror implementation |
| `scripts/audiobookai_mysql_proxy.py` | GUI-side MySQL proxy that sends a metadata snapshot to a separate worker process |
| `scripts/audiobookai_mysql_worker.py` | Isolated MySQL synchronization process (loads the MySQL connector) |
| `scripts/audiobookai_voices.py` | WAV validation, narrator import, immutable narrator snapshots |
| `scripts/audiobookai_production.py` | Chunk indexes, original/replacement takes, quality checks, and guarded cache cleanup |
| `scripts/audiobookai_editor.py` | Chapter chunk editor; sends regeneration and rebuild requests to the converter |
| `scripts/audiobookai_convenience.py` | Small independent helpers for selection and duration estimates |
| `tests/` | Automated regression tests for storage, output, narrator isolation, recovery, and safety checks |

**Historical scripts:** `audiobook_gui.py`, `epub_to_audiobook.py`, `epub_to_audiobook_v7_smooth.py`, and the `test_five_minutes_v6*.py` files are kept for comparison or backwards compatibility. Don't assume they are used by the current Studio queue.

## Narration pipeline

```text
EPUB file
  |
  v
EPUB spine -> ordered Section objects -> paragraph text
  |
  v
V7 phrase/sentence-aware Chunk objects
  |
  +----> deterministic voice/text/settings cache key
  |          |
  |          +--> compatible, valid WAV exists -> reuse recording
  |          |
  |          +--> cache missing/invalid -> Chatterbox TTS -> quality check
  |                                            |
  |                                            +--> retry / shorter chunks on failure
  v
Chunk WAVs (+ optional selected replacement takes)
  |
  v
Chapter WAV assembly (boundary-sensitive pauses, peak attenuation)
  |
  v
FFmpeg -> chapter-marked M4B part
```

### Why the GPU runs outside the GUI

The Qt window starts the converter in a `QProcess`. The converter loads the large TTS model only when audio is needed, keeping the GUI separate from GPU generation and allowing clean stop/restart of jobs. The converter sends structured lines beginning with `AUDIOBOOKAI_EVENT `; ordinary stdout remains human-readable. Look at `consume_output()` and `progress_event()` in the GUI and `emit_event()` in the backend.

### Cache identity and narrator isolation

Raw audio caches are **per chunk**, not per chapter. The key is derived from text and narration configuration, including narrator identity. A repeated compatible sentence can be loaded without another GPU call. The default British narrator deliberately retains legacy paths for compatibility; alternate narrators receive content-addressed identities and output folders. Queue jobs hold an immutable snapshot of the narrator sample, so editing an imported WAV doesn't silently change a waiting job.

**Important:** raw chunk WAVs and edited takes are not interchangeable. Editor takes live beside audiobook chapter outputs. The editor never overwrites the global shared cache when creating a replacement take.

### Why V7 functions appear twice

`test_five_minutes_v7_smooth.py` retains some V6 implementations earlier in the module and **redefines** chunking/assembly functions in the later **V7 smooth** section. In Python, the last definition is the active one after module import. Carefully inspect the later `split_long()`, `make_chunks()`, `target_pause()` and `assemble()` before modifying narration flow. Removing early definitions may be a legitimate refactor, but should be done separately from documentation changes and tested against audio output.

## Persistence and recovery

`Store` in `audiobookai_storage.py` owns the SQLite database (`output/audiobookai_studio.sqlite3`). Jobs move between queued, running, interrupted, failed, and complete. On launch, jobs left running by a prior crash are marked interrupted. Chunk progress is checkpointed periodically, and the converter also reuses already completed audio based on its manifests and cache.

MySQL is **optional and one-way**: SQLite is authoritative, and the MySQL schema is a read/query mirror. The GUI's current proxy uses a separate interpreter so loading MySQL's native dependencies doesn't happen in the Qt process. Metadata moves through child-process stdin; no EPUB text, raw speech, or audiobook exports need to be sent to MySQL.

## Editing and audio-quality review

Each generated chapter can have a JSON chunk index mapping original text to cached WAV paths. Replacements are kept in an append-only history of takes; choosing an active take doesn't delete older recordings. The original text is checked when loading edits so a changed EPUB or changed chunk layout doesn't apply a stale replacement to the wrong sentence.

`quality_report()` flags possible clipping, silence, abnormal length, and nonfinite samples. Review-only flags are not automatically removed from the audiobook; unusable audio triggers regeneration. Offline rebuilding reassembles cached WAVs and selected takes without rerunning Chatterbox.

## Safety boundaries

- Never commit `books/`, `output/`, voice `.wav` files, credentials, or virtual environments.
- Preserve atomic manifest updates: incomplete JSON can lose the mapping between text and audio.
- Keep output paths narrator-specific so mixed-voice chapters don't silently reuse one another.
- Do not remove raw speech during a conversion or before finishing editing; later offline rebuilds may need it.
- Keep cache deletion limited to the app's raw cache directory. The cleanup API intentionally refuses arbitrary directories and symlinks.
- Do not hardcode user credentials. Config stores nonsecret fields; passwords are read from the environment or optional system keyring.
- Avoid changing CUDA/TTS settings in a GUI-only patch: audio generation stability depends on the existing token and chunk limits.

## Tests and suggested workflow

From the repository root, in the same Python environment used by the app:

```sh
python -m compileall -q scripts tests
python -m unittest discover -s tests -v
```

Some tests use PyTorch, SoundFile/torchaudio, or FFmpeg even though they don't need a CUDA GPU; install the project's documented runtime dependencies before running them. The tests don't replace a full GUI session on a real KDE desktop.

For a focused contribution, change one component, write a regression test, and run the complete suite. A changed narration algorithm also needs a short listening comparison; passing code tests alone cannot establish identical voice quality.
