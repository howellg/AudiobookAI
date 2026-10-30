# AudiobookAI Studio

**A local EPUB-to-audiobook desktop application built with Python, PySide6, and Chatterbox TTS.**

AudiobookAI Studio imports DRM-free EPUBs, turns selected chapters into narrated WAVs, and exports chapter-marked M4B audiobooks. It is designed for long-running conversions on local hardware: audio chunks are cached, interrupted jobs can resume, and completed chapters can be reviewed and repaired without generating an entire book again.

> **Project status:** Personal-use / in-development software (current application: **Studio V2.10.4**, using the **V7 Smooth** narration engine). Developed and exercised on **Arch Linux + KDE Plasma**, using an NVIDIA GTX 1650 with 4 GB VRAM. A clean install on other computers and operating systems is **not yet validated**. This repository is source code, not a standalone AppImage or packaged installer.

## Features

- **EPUB import and chapter selection:** Browse books, inspect EPUB sections, filter chapters, enter ranges (`3-17, 20`), and queue a whole book in manageable parts.
- **Selectable narrator WAVs:** Import a voice reference recording, preview it, and choose a narrator for each queued job. The app snapshots alternate narrators so later edits to the source WAV do not silently change pending jobs.
- **V7 Smooth generation:** Chatterbox narration with conservative text chunking, protective token limits, and audio assembly with pauses.
- **V2.10.4 SSD cache preflight and recovery:** The GUI and headless runner now check the narration-cache directory and any external SSD symlink **before** marking a job running or launching Chatterbox. A missing mount pauses generation with the queued job preserved, rather than producing a repeated converter traceback or silently replacing the cache. Queue shows storage readiness and approximate free capacity, with **Check storage & resume** to resume the exact waiting job or the normal queue order after remounting. Optional **Auto-resume when SSD returns** (off by default, remembered across sessions) checks every 15 seconds **only while the current session is waiting for cache storage**. Manual Pause/Stop cancels pending auto-resume; launching Studio again does not automatically launch pending jobs. The headless runner exits with an actionable storage error while preserving the job's existing status. Cache inspection is read-only and does not mount drives or alter audio.
- **V2.10.3 drag-and-drop stability hotfix:** Corrects a Qt-specific regression where QTableWidget's native MoveAction deleted source table cells *after* the application had already saved the new queue order and refreshed the view, leaving blank rows and misleading execution order. Dragging now uses a copy-only signal and delegates the actual reorder entirely to SQLite; a final refresh after the drag loop restores the table from the authoritative database. **Move up/down/end**, current-part generation, output files, caching, and saved queue metadata are unchanged. Blank rows in V2.10.2 were a display bug; reopen the GUI after finishing the current conversion to load the fix.
- **V2.10.2 persistent queue reordering:** The Queue page now displays the **actual execution order, top to bottom**, instead of newest-first. Drag any pending job to another row, or select it and use **Move up**, **Move down**, or **Move to end**. Reordering is saved immediately in SQLite, survives restarts, and is honored at the next part boundary even while another job is generating. Running/stopping and finished jobs cannot be moved; filters must be cleared while rearranging. The existing MySQL mirror receives the position as metadata. **Start queue** during an active single-part run to allow the following queued parts to run automatically once that part finishes.
- **V2.10.1 offline-first Chatterbox models:** The converter and standalone V7 engine set `HF_HUB_OFFLINE=1` by default *before Hugging Face libraries load*, so completed model downloads run entirely from the local cache without startup network checks. Existing explicit environment overrides remain respected. **Settings → Chatterbox models** adds **Check local models** (never uses internet), **Download missing files** (explicit online download) and **Refresh all model files** (explicit online refresh of about 3 GB). These run in a separate subprocess without altering your queued jobs, generated speech cache, EPUBs, or chapter WAVs. The app displays a useful missing-cache error when the models have not been downloaded; new installs must download the model before their first offline generation. Model updates do not automatically invalidate existing narration or regenerate speech.
- **V2.10.0 experimental single-narrator acting:** On Generate, optionally check **Expressive dialogue · same narrator** before adding chapters to the queue. Chatterbox still uses exactly the selected narrator reference WAV, but applies a modest expressiveness adjustment to quoted-dialogue chunks recognized by the existing heuristic splitter. Normal prose and all default jobs retain their original acoustic settings and cache identities. The mode is frozen in each SQLite queue job and preserved by the headless runner and completed-part receipts. Expressive chapter WAVs/manifests are stored under `performances/expressive/` so they do not overwrite the ordinary narrator edition. Review, Read Along, Quality Control, Book Integrity, Archive, and full-book export can discover the separate edition. **Limitations:** This does not detect character identities, add character-specific pitches/accents, or necessarily classify embedded/multisentence dialogue perfectly; review a short sample before queueing a novel. The effect is not guaranteed to sound better for every reference voice.
- **Resume and recovery:** Reuse cached speech chunks after interruptions; automatically retry and split chunks in some recoverable generation failures.
- **Audio review and editing:** Inspect completed chapter audio, edit the spoken text for individual chunks, generate alternate takes, restore previous takes, and rebuild audio from saved recordings.
- **M4B export:** Assemble completed chapter WAVs into AAC-encoded M4B files with chapter markers. Long books can be exported as separate parts.
- **V2.6 Archive Manager (preview):** Build a separate chaptered complete edition with editable metadata, optional embedded cover art, and your chosen destination (including external drives) without rerunning TTS.
- **V2.7 Read Along:** Play completed chapter WAVs with highlighted saved narration chunks, click a chunk to seek, and open its chapter in the existing audio editor, automatically refresh repaired chapters, and resume where you left off. Chunk timing is estimated rather than sample-accurate.
- **V2.9.2 audiobook container compatibility:** All new M4B part and complete-book exports now declare an explicit `M4B ` major brand and audiobook media type (`media_type=2`), and use `faststart` to put chapter/index metadata ahead of large audio data. This targets Android audiobook players that misclassify `.m4b` files as ordinary M4A or struggle to read metadata at the end of multi-gigabyte files. Both Nero and QuickTime chapter structures are still written; no changes to narration or cache. For existing files, a lossless FFmpeg stream copy can add these flags without regenerating TTS. **Note:** Chapter recognition in any specific Android app still requires testing on that device.
- **V2.9.1 complete M4B encoding progress:** Review Audio now displays a live progress bar for complete-book FFmpeg encoding, including percentage, audio time processed, elapsed time, approximate remaining time, and encoding speed (when reported). A compact indicator remains visible in the status bar when navigating elsewhere. The exporter uses FFmpeg's `-progress` timestamps and the chapter manifest's exact duration, reserves 100% for verified successful completion, and preserves the old M4B and chapter WAVs if encoding or final verification fails. Normal narration/part exports remain unchanged.
- **V2.9.0 Book Completion & Integrity dashboard:** Dedicated sidebar page showing each imported book's narrator-specific recorded sections, queued/unfinished and missing/damaged chapters, completion percentage, expected section range with explicitly excluded EPUB front/back matter, saved part receipts and file availability, QC scan coverage/freshness, and complete M4B presence. Use **Verify final M4B** for an explicit FFprobe check of audio stream, chapter-marker count and expected duration; this is not a full decode or proof of matching speech content. Buttons open Review audio, Quality Control, and the existing full-book export workflow. Works with regenerated WAVs even after their original jobs were removed; does not modify narration.
- **V2.8.9 complete-book recovery:** If the selected narrator's chapter manifest contains finished WAVs absent from saved-part history (for example, earlier chapters re-recorded with a new narrator), **Export complete audiobook** offers a choice between all recorded chapters and saved parts only. It checks WAV existence, EPUB numbering, narrator isolation, sample rates, and manifest sample counts; it preserves the usual unfinished-job block and coverage warnings. No TTS regeneration or database edits are required.
- **V2.8.7 review fixes:** Stop displaying stale Generate chapters when an EPUB has moved or vanished; calculate a recent-pace ETA using fast cached chunks as well as generated chunks; warn about specific internal chapter gaps before either complete-book export flow, while permitting intentional omitted front/back matter; retain completed parts in Archive after clearing finished queue rows. No narration/cache format changes.
- **Persistent queue:** Keep books, selected sections, jobs, and progress in a local SQLite database.
- **V2.8.6 half-second progress and detailed logs:** Refresh the Queue progress display and batch GUI log updates every 500 ms. Use **Detailed view** to show every chunk's text, cache use, generation timing and completion; return to **Compact view** for chapter milestones and warnings. Switching views keeps recent session history; all converter chunk text is retained in job diagnostics. The stable PySide6 6.11.2 pin is unchanged.
- **V2.8.5 elapsed clock:** Update elapsed time once per second only during an active conversion, without restoring the previously disabled always-on Qt monitor.
- **V2.8.2 GUI stability and headless runner:** Reduce high-frequency Qt UI updates, defer FFmpeg playback initialization, and provide Qt-free overnight queue recovery. **PySide6 is pinned to 6.11.2** following repeatable native crashes on 6.12.0; the downgrade has shown substantially better long-run stability on the developer's machine, although the root cause is not confirmed.
- **V2.8.1 hotfix:** Reserve new job IDs beyond all existing frozen pronunciation snapshots, even after deleting an old queue part.
- **V2.8 Reliability & Recovery:** Safely requeue interrupted parts with dependency checks, keep per-job converter logs on disk, and make verified periodic backups of small project metadata.
- **Optional MySQL mirror:** Synchronize library, chapter, and job metadata to a MySQL database for querying. SQLite remains the authoritative local store; audio itself stays on disk.
- **Manual cache cleanup:** Preview cache age and storage usage before deleting raw cached WAVs. Completed exports and narrator files are excluded.

**Not implemented yet:** sample-accurate or word-level forced-aligned EPUB synchronization, and one-click cross-platform installation.

## Getting started

See **[INSTALL.md](INSTALL.md)** for complete Linux setup, including Python 3.11, Chatterbox/PyTorch, FFmpeg, optional MySQL, a KDE desktop launcher, and troubleshooting.

**Already have a working Chatterbox environment?** Use it rather than recreating it:

```fish
cd ~/AudiobookAI
source .venv-chatterbox/bin/activate.fish
python scripts/audiobook_gui_v2.py
```

For a fresh clone, follow [INSTALL.md](INSTALL.md) first. You must provide your own DRM-free EPUB and at least one suitable narrator WAV; neither is distributed with this repository.

### Model downloads and offline use (V2.10.1)

On a new installation, connect to the internet and use **Settings → Chatterbox models → Download missing files…** before generating for the first time. Once all five model files are cached, the normal converter works without internet access. The initial model download is approximately 3 GB.

You can check the cache without connecting to the internet:

```fish
python scripts/audiobookai_models.py check
```

Explicit online maintenance is also available without starting the GUI:

```fish
python scripts/audiobookai_models.py download  # Fetch missing/latest required files
python scripts/audiobookai_models.py refresh   # Force a fresh download of all model files
```

These are optional maintenance commands: generation, EPUB import, and normal M4B export do not run them automatically. The application sets `HF_HUB_OFFLINE=1` as its default for generation; you can intentionally override this with `HF_HUB_OFFLINE=0`, but using the separate maintenance action is recommended.

## Readable M4B part filenames and permanent completion history (V2.8.4)

**Newly queued GUI parts** use names such as `A New Player in the Game - Part(9).m4b`.
The book title and part number are all that appear in the filename. The exact
EPUB section selection, narrator snapshot, export path, output size and finish
timestamp are preserved in **`completed_parts`** in
`output/audiobookai_studio.sqlite3`, independently of the queue.

A part is recorded **only after** the converter exits successfully and the
reported output filename and M4B size pass verification. Failed, cancelled,
partially generated or WAV-only jobs are not marked as completed. The GUI and
the Qt-free headless runner use the same atomic database operation.

**After a part finishes, use Queue → Clear finished jobs** to remove all
verified completed entries, or delete individual entries to keep the
queue short. The completed-part record remains, and Generate's **Next unqueued**
selection skips its EPUB sections. Part numbers continue per book and narrator
even after completed jobs have been removed. **Review audio** lists completed
M4Bs from history, and **Export complete audiobook** can still combine the
original parts after their queue entries have been deleted, provided required
chapter WAVs and narrator snapshots remain accessible. The Generate page
also displays the part numbers and saved section count.

The first time an existing book is selected, Studio attempts to recover
older completed M4Bs whose queue entries were previously removed. It only
imports unambiguous contiguous section ranges from **old-style filenames**
and checks FFprobe audio, duration, and one chapter marker per section.
Ambiguous filenames and incomplete files are **not** silently marked complete.

If a name is already in use, an optional `(2)` suffix prevents overwrites.
**Existing queued jobs and files keep their old names**, so resume and
pronunciation snapshots remain stable. A raw command-line conversion not
started from a saved Studio queue job does not create a completed-part receipt.

Back up the SQLite database alongside pronunciation snapshots when archiving
work. Deleting queue entries does **not** remove M4Bs, WAVs or caches; deleting
the SQLite database **does** lose the completion history.

## External SSD narration cache (V2.10.4)

If output/narration_cache_v2 is a symlink to a removable SSD, keep it intact.
**Do not remove or recreate the symlink when the drive is unmounted.**
Studio checks the link and mount before starting each part; an unavailable
drive leaves the job **Queued** and stops further automatic dispatch. Mount
the same SSD and click **Queue → Check storage & resume**. If you enable
**Auto-resume when SSD returns** on the Queue page, Studio checks at
15-second intervals and resumes that specific waiting operation after the
drive is available. This is opt-in: it is **off by default**. Pausing/stopping
the queue cancels a pending restart. Automatic recovery applies only to an
in-session storage wait, not to an old interrupted/failed job or a relaunch
of the app. To retry an earlier failed part, select it, click **Retry selected**,
then **Start queue**.

Queue's storage line reports whether the cache is accessible and available
free space (for an existing directory). This is advisory, not a disk-space
guarantee. Preflight is read-only and does not mount drives; sudden device
loss while actively generating may still interrupt the converter. The engine
continues to validate the cache when it actually begins work, and the
headless runner performs the same preflight before changing the queued
job's status.

## Queue display modes (V2.8.6)

The Queue page offers a **Detailed view** button above Recent generation messages. In compact mode, the log contains chapter milestones, warnings, errors and export results while chapter/overall progress still updates every **0.5 seconds** when events arrive. Select Detailed view for the individual spoken-text chunks, `Generated … in …` timings, cache reuse lines and chunk-completion notifications. Click **Compact view** to return to a concise log. This display preference is remembered on restart.

Both views buffer incoming messages and append a single batch to the GUI at most every 500 ms, rather than repainting for every chunk. The live log/history is bounded; persistent `output/diagnostics/` job logs retain chunk text and timing details. The **View job log** control opens those diagnostics. The separate SQLite checkpoint cadence remains unchanged. The elapsed clock is still shown in whole seconds but is checked twice a second without redundant label writes.

After updating, finish the current conversion and restart Studio to load new Python code. Pulling a branch while Studio is already running does not reload the GUI.

## Cache cleanup and mirror safety (V2.8.8)

Manual raw-cache cleanup is limited to AudiobookAI's configured narration cache, including a symbolic link to an external SSD. A directory called `test_*` elsewhere cannot bypass this check. The tests explicitly substitute an isolated cache root; the actual cache files and current narration settings are not changed. The MySQL worker now validates the complete metadata snapshot (including finished-part receipts) before attempting a mirror update. These changes do not modify Chatterbox, text chunking, audio trimming, or existing WAV/M4B files.

## Full-book coverage checks (V2.8.7)

The regular **Review audio → Export complete audiobook** dialog and the **Archive → Export archival M4B** dialog now list skipped EPUB section ranges. Missing indices **between** the first and last selected sections receive a strong warning; omitted front/back matter is listed separately. The user must confirm the choice and may continue when those omissions are deliberate. The application still requires every *selected* section to be represented by a completed, verified chapter WAV. This coverage warning does not automatically mark an unqueued chapter complete or alter any files.

If you move the source EPUB, **Generate** now clears its previous chapter selection instead of silently displaying stale rows for a different book. Restore the EPUB to its stored path (or re-import it) before queueing from that book.

**Queue ETA** now estimates from the last 120 observed chunk times, including fast cache hits and edited takes. It is explicitly marked **recent pace**; if the next sections require fresh TTS, the estimate may rise as the workload changes. Export time is not included. SQLite writes remain rate-limited and PySide6 stays pinned to 6.11.2.

## Typical workflow

1. Start the desktop application (`python scripts/audiobook_gui_v2.py`).
2. Under **Library**, import an EPUB. Inspect the detected sections; an EPUB section number may differ from its printed chapter number.
3. Under **Generate**, select sections, choose a narrator WAV, set a batch size, and add parts to the queue.
4. Under **Queue**, start or resume generation. A clean stop preserves completed cached speech.
5. Under **Review audio**, play chapter WAVs, review any problematic chunk, and rebuild audio if you change a take.
6. Play the exported M4B files using your preferred audiobook player.

**Important:** Changing the narrator in Generate does **not** automatically change the narrator already associated with a queued part. Check the Queue page before starting a long conversion.

### Command-line converter

The current studio backend is `scripts/epub_to_audiobook_v7_studio.py`. Run these commands from the repository root with your Python environment activated:

```bash
# Inspect EPUB reading-order section numbers; no TTS generation
python scripts/epub_to_audiobook_v7_studio.py inspect books/novel.epub

# Generate only EPUB sections 3 through 5, with a distinct export name
python scripts/epub_to_audiobook_v7_studio.py convert books/novel.epub \
  --chapters 3-5 --output-name novel_part_01

# Use a different narrator WAV for a conversion
python scripts/epub_to_audiobook_v7_studio.py convert books/novel.epub \
  --chapters 6-10 --voice voices/my_narrator.wav --output-name novel_part_02

# Resume existing speech-generation work (reuses compatible cached chunks)
python scripts/epub_to_audiobook_v7_studio.py resume books/novel.epub \
  --chapters 3-5 --output-name novel_part_01

# Re-export already completed chapter WAVs without generating narration
python scripts/epub_to_audiobook_v7_studio.py export books/novel.epub \
  --chapters 3-5 --output-name novel_part_01
```

The backend also provides `index`, `audit`, `rebuild`, and `render-chunk` commands for production and editing workflows. Use `--help` for the argument list; the graphical editor is recommended for normal chunk repair.

## Project layout

```text
AudiobookAI/
├── scripts/
│   ├── audiobook_gui_v2.py              # Current PySide6 GUI entry point
│   ├── epub_to_audiobook_v7_studio.py    # Current EPUB -> WAV -> M4B backend
│   ├── test_five_minutes_v7_smooth.py   # V7 narration engine
│   ├── audiobookai_editor.py            # Chunk editor UI
│   ├── audiobookai_production.py        # Audio quality, edits, cache helpers
│   ├── audiobookai_voices.py            # Voice import and job snapshots
│   ├── audiobookai_storage.py           # SQLite store and MySQL schema mirror
│   ├── audiobookai_mysql_proxy.py       # Separate-process MySQL bridge
│   ├── audiobookai_mysql_worker.py      # MySQL worker process
│   └── ...                              # Older versions / helper modules
├── sql/setup_mysql.sql                  # Optional MySQL database setup
├── tests/                               # Automated tests
├── README.md
├── INSTALL.md
├── pyproject.toml                       # Dependency metadata (not a wheel)
├── LICENSE                              # MIT license for original project code
├── books/                              # User-created; Git-ignored
├── voices/                             # User-created; WAVs Git-ignored
└── output/                             # User-created; Git-ignored
```

The following are **runtime directories**, not part of the GitHub source distribution:

| Location | Purpose |
| --- | --- |
| `books/` | Your local EPUBs |
| `voices/` | Your narrator WAVs and hidden voice snapshots |
| `output/audiobooks/` | Chapter WAVs and finished M4B files |
| `output/narration_cache_v2/` | Reusable raw speech WAVs |
| `output/audiobookai_studio.sqlite3` | Local library, queue, permanent completed-parts ledger, and progress |
| `~/.config/audiobookai/studio_config.json` | Non-secret MySQL configuration |

The default narrator path is `voices/british_narrator.wav`. Other narrator WAVs can be imported through the GUI. Voice recordings and generated audiobooks are deliberately excluded from Git.

## Narration and cache behavior

The current V7 engine uses an approximately **18-word maximum chunk target**, a protective **350-token** generation cap, and baseline Chatterbox settings of **exaggeration 0.35** and **CFG 0.30**. These are implementation settings, not guarantees of pronunciation or seamless speech.

The raw cache is keyed using the exact text and relevant voice identity. Compatible text may therefore reuse existing audio even when it appears in a different chapter or book. Reuse is deterministic caching—not retraining the model. Alternate narrators are kept separate from the default narrator's output paths.

**Do not clear raw caches during an unfinished book.** Deleting them can force fresh GPU generation when resuming or rebuilding chapters. The Settings page offers a manual, confirmation-based cleanup tool when you're ready.

## Tests

From an activated environment with the application dependencies installed:

```bash
python -m unittest discover -s tests -v
```

The tests cover storage migration, narrator isolation, queue selection, cache safeguards, audio rebuilding, and simulated generation recovery. **Passing tests does not prove GPU generation, MySQL connectivity, or Qt behavior is working on every machine.** A real end-to-end conversion should be tested after a fresh installation.

## Requirements and compatibility

- Linux desktop with Qt/PySide6 (developed on KDE Plasma / Wayland).
- **Python 3.11** recommended for compatibility with the original Chatterbox setup.
- FFmpeg and FFprobe installed and on `PATH`.
- Chatterbox TTS, PyTorch, TorchAudio, **PySide6 6.11.2**, EbookLib, Beautiful Soup, lxml, NumPy, and SoundFile.
- A suitable GPU is strongly recommended for long books. This project has been exercised with NVIDIA CUDA on a **4 GB GTX 1650**; AMD ROCm, CPU-only operation, Windows, and macOS are **not validated** by this repository.
- MySQL is optional; it is **not** required for importing, converting, or playing audiobooks.

Installation details: **[INSTALL.md](INSTALL.md)**. Chatterbox upstream: https://github.com/resemble-ai/chatterbox. PyTorch platform-specific installation selector: https://pytorch.org/get-started/locally/.

## Copyright, voice rights, and privacy

Use EPUBs you are authorized to access and convert, and narrator samples you have permission to use. **No books, third-party recordings, generated audiobooks, or model weights are included.** Do not commit runtime data or credentials to GitHub. The app runs locally, although dependencies/model weights may be downloaded from external services during setup and first use.

This project is not affiliated with Resemble AI or the authors, publishers, and narrators of any books you choose to process. Respect relevant copyrights and voice/likeness rights, particularly before distributing generated recordings.

## License and contributions

AudiobookAI's original source code and documentation are available under the **[MIT License](LICENSE)**. This permission applies to the original project code, **not** to third-party EPUBs, narrator recordings, generated audio, downloaded model weights, or dependencies. Those materials retain their own licenses and rights. The Chatterbox TTS dependency is maintained separately by Resemble AI.

Dependency requirements are declared in [`pyproject.toml`](pyproject.toml). **The repository does not yet include a tested `uv.lock`**: a fully reproducible GPU installation requires validating and locking the PyTorch/CUDA/Chatterbox versions on an actual supported machine. See [INSTALL.md](INSTALL.md#dependency-manifest-and-locking).

Bug reports should include the OS, Python/PyTorch versions, GPU, relevant (redacted) logs, and a small reproducible test case. Never attach copyrighted EPUBs, narrator recordings, credentials, or full audiobook outputs to issues.

### V2.5 pronunciation dictionary

Open **Pronunciation** in the sidebar to add global or book-specific text
substitutions (for example `Nynaeve` → `Nigh-neev`). Book rules override global
rules. Use **Preview** to inspect the text that Chatterbox receives; this is
ordinary respelling, not IPA or guaranteed phoneme control. The original EPUB
is never edited.

Rules are frozen **when you queue a new part**. Old jobs, chapter WAVs, cached
speech, and M4Bs are not automatically regenerated. In the audio editor, each
chapter's saved pronunciation snapshot is used for indexing and rebuilding.
The editable dictionary and per-job snapshots are stored in the ignored
`output/pronunciation/` directory. **Back up this directory together with
`output/audiobooks/` and your SQLite database** if you migrate computers or
want to rebuild chapters years later.

### V2.6 Archive Manager (preview)

Choose **Archive** in the sidebar, select a book and narrator, and inspect its
finished queued parts. When all planned sections are complete and chapter WAVs
are present, edit title, author, series, volume, narrator, and optional cover.
An EPUB cover is used automatically when available; a JPEG/PNG can be selected.

Choose **Export archival M4B…** and select a destination. The worker refreshes
a stale complete M4B from existing chapter WAVs if needed, then copies its
AAC audio with chapter markers into a metadata-rich archival file. Individual
parts, chapter WAVs and raw caches remain untouched. The local archive catalog
is saved under ignored `output/archive/catalog.json`; back it up along with
your audio if moving computers.

This is a development preview: the Archive GUI should be tested on the target
Linux machine before relying on it for valuable recordings. Ensure your external
drive has enough free space and supports large files.

### V2.7 Read Along

Select **Read Along** in the sidebar, then a book, narrator, and completed
chapter. Click **Play / pause** to listen to its assembled WAV. The matching
saved narration chunk is highlighted, and clicking a text chunk seeks near its
beginning. **Edit chapter** jumps into the existing non-destructive audio editor.
After you close the editor, the reader reloads rebuilt chapter audio and the
text of any replacement takes actually included in the saved chapter. The
selected book, narrator, chapter and playback position are restored on restart.
No Chatterbox or GPU generation is involved.

**Precision and text caveat:** Existing V2.6 chunk indexes contain original
chunk sequence and cache references but not exact sample offsets. Highlight
boundaries are estimated from recorded speech-chunk durations and normalized to
the assembled chapter WAV; they can drift around inserted pauses and edits. If
raw chunk recordings were cleaned, timing is estimated using word counts. The
reader displays the saved *spoken* chunk text, which may contain pronunciation
dictionary substitutions rather than the EPUB's original text/formatting.
The next step toward exact sync is to persist assembly sample offsets plus both
original and synthesized text in future chapter indexes.

QtMultimedia must be provided by the same PySide6 installation as the GUI.
Use the existing virtual environment and confirm WAV playback on your Linux
machine; this preview has not yet been validated across audio backends.

### V2.8 Reliability & Recovery

The Queue page provides **Recover interrupted**, which checks interrupted jobs'
EPUB, saved narrator WAV, section selection and frozen pronunciation snapshot
before putting them back into the queue. Nothing starts automatically; click
**Start queue** when ready. Failed jobs are not silently retried. The converter
still resumes from compatible cached speech rather than restarting finished work.

Each attempted conversion creates a bounded, timestamped log under
`output/diagnostics/`. Select a queue part and click **View job log**. The
failure reason is retained on the queue record, and logs are kept locally.

Settings now offers **Backup metadata now**, **Verify latest backup**, and
**Open backups folder**. An automatic backup runs on startup if stale and
approximately every 30 minutes while the desktop app is open. The newest ten
automatic archives are retained; manually requested archives are never pruned.
Backups use SQLite's consistent backup API, include the pronunciation dictionary
and immutable job snapshots plus the archive metadata catalog, and are SHA256
verified before being committed to disk. No automatic restore or replacement
of your live data is performed.

**Important:** these metadata archives do **not** contain source EPUBs,
narrator WAVs, finished M4Bs, chapter WAVs, raw generation caches, AI models,
or private MySQL passwords. They are stored on the same disk by default, so
copy the archives **and your separate media folders** to external storage
to protect against drive failure. Restoring paths and snapshots onto a new
machine may require adjusting paths; there is no one-click restore yet.

### V2.8.2 long-run GUI stability and Qt-free generation

This update reduces GUI calls made for each of the thousands of narration chunks
in a long audiobook. The queue's SQLite checkpoints, Chatterbox inference,
existing cache-key algorithm, part naming and chapter output are unchanged.
Routine `Using cached audio.` and `Generated ...` messages are still saved
to per-job logs but no longer cause every line to relayout the GUI log viewer.
Read Along's QtMultimedia/FFmpeg backend loads only when opening its tab, not
during startup or an ordinary conversion.

**Qt compatibility:** Pin **PySide6==6.11.2**, not 6.12.0. On the developer's Arch/KDE setup, the earlier 6.12.0 stack repeatedly crashed after hours of GUI use; switching to 6.11.2 has substantially extended stable runtime. See [INSTALL.md](INSTALL.md#42-application-dependencies) for the exact installation and instructions to remove optional incompatible `pyside6-pdf` or `pyside6-webengine` packages if necessary. The cause has not been conclusively isolated.

**Caveat:** Python's `none_dealloc` abort indicates a C-extension
reference-count fault. The traceback may identify where it surfaced, not where
it originated. This patch reduces native GUI churn and should be tested with a
long conversion before being treated as a fully proven fix. If the GUI aborts,
the persisted cache and completed chapter audio remain available. Before
retrying, check for a surviving converter process to avoid concurrent writers.

### Long conversions without the GUI (V2.8.2 preview)

A native PySide6 `none_dealloc` abort has been observed in the progress
display after eight and eleven hours. Reducing GUI repaints alone did **not**
resolve it. To avoid depending on a Qt event loop for a 90-hour book, this
preview also provides a Qt-free queue runner. It uses the very same existing
SQLite queue, selected EPUB sections, saved narrator, immutable per-job
pronunciation rules and V7 cache; **do not add duplicate jobs or reset files**.

1. Close Studio; use `pgrep -af 'epub_to_audiobook_v7_studio.py'` to
   confirm a crashed GUI did not leave a converter running.
2. In `~/AudiobookAI` with `.venv-chatterbox` activated, use:
   `python scripts/audiobookai_headless.py --list`
3. Choose the interrupted/failed existing job ID, then run:
   `python scripts/audiobookai_headless.py --job ID`
4. The console prints completed chapter/export milestones. Per-job diagnostics
   remain in `output/diagnostics`, and chunk progress remains in SQLite.
   Run it in `tmux` (if installed) for an overnight conversion that continues
   when a terminal window is closed. Detach with Ctrl+B then D, reattach with
   `tmux attach -t audiobookai`.
5. Do not open Studio during headless conversion or launch another writer for
   the same book. The preview explicitly refuses to start the GUI when the
   headless runner is holding its process lock.

Completed WAVs and raw speech chunks are reused by the unchanged converter.
The runner starts exactly **one** selected job per invocation; it does not
automatically run the entire queue. It marks the job complete only when a
matching M4B file is verified, or records an error/interrupted status. This is
an operational workaround for the observed Qt crash, **not** a root-cause fix.
