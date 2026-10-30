# Installing AudiobookAI Studio

This guide covers a **source installation on Linux**, with tested development context on **Arch Linux / KDE Plasma / NVIDIA CUDA**. The repository does not currently include a packaged executable or automated first-run installer.

[← Back to README](README.md)

## 0. Before you start

You will need:

- A Linux desktop and a terminal.
- Internet access to install Python packages and download Chatterbox model files on first use.
- A **DRM-free EPUB** you are permitted to convert.
- A **WAV reference recording** you are permitted to use as a narrator sample. This is not bundled in the repository.
- Sufficient storage for model dependencies, cached speech, chapter WAVs, and final M4Bs. Large books can create many intermediate files.

**Python 3.11 is recommended and required by the current `pyproject.toml` manifest.** The project was developed against a Python 3.11 Chatterbox environment. Dependencies are now declared, but a **tested lockfile has not been committed yet**; clean installs may require GPU-specific troubleshooting. **Use PySide6 6.11.2 rather than 6.12.0.** Long-running GUI sessions on the original developer machine repeatedly crashed with a native `none_dealloc` error under 6.12.0 and have shown substantially improved stability after downgrading to 6.11.2. This is a tested workaround, not a proven upstream root cause.

> **If AudiobookAI already runs on your computer:** Do **not** delete `.venv-chatterbox/`, `output/`, `books/`, or `voices/`, and do not blindly reinstall PyTorch. Skip to [Start the application](#5-start-the-application) using your existing virtual environment.

## 1. Install system tools

On **Arch Linux**:

```bash
sudo pacman -S --needed git uv ffmpeg
```

On other Linux distributions, install `git`, `ffmpeg` (including `ffprobe`), and a Python 3.11 environment manager such as `uv` using your distribution's instructions.

Confirm FFmpeg is available:

```bash
ffmpeg -version
ffprobe -version
```

For GPU generation, install and verify your graphics driver independently. On NVIDIA systems, `nvidia-smi` is a useful quick check. The appropriate PyTorch build depends on your driver and accelerator; see the [official PyTorch installation selector](https://pytorch.org/get-started/locally/).

## 2. Clone the repository

Replace `<YOUR-GITHUB-USERNAME>` with the actual account hosting this repository:

```bash
git clone https://github.com/<YOUR-GITHUB-USERNAME>/AudiobookAI.git
cd AudiobookAI
```

Or, if you've already cloned it, just open the existing repository folder.

The project expects these folders at the repository root. They are local data folders and are intentionally Git-ignored:

```bash
mkdir -p books voices output
```

## 3. Create a Python 3.11 environment

The application works from the source checkout; no `pip install -e .` step is required. Its `pyproject.toml` is **dependency-only**, not a distributable Python wheel, because scripts rely on paths relative to this checkout.

```bash
uv python install 3.11
uv venv --python 3.11 .venv-chatterbox
```

Activate it using your shell.

**Fish (Arch/KDE users):**

```fish
source .venv-chatterbox/bin/activate.fish
```

**Bash/Zsh:**

```bash
source .venv-chatterbox/bin/activate
```

Confirm it is the correct environment:

```bash
python --version
python -c 'import sys; print(sys.executable)'
```

The executable should live inside the repository's `.venv-chatterbox/` directory.

## 4. Install Python dependencies

### Dependency manifest and locking

[`pyproject.toml`](pyproject.toml) declares the core GUI/EPUB/audio packages plus optional extras:

| Extra | Installs | Needed for |
| --- | --- | --- |
| Base | **PySide6==6.11.2**, EbookLib, Beautiful Soup, lxml, NumPy, SoundFile | Desktop application and EPUB processing |
| `tts` | `chatterbox-tts==0.1.7` and its upstream dependencies | Speech generation, Torch, TorchAudio |
| `mysql` | `mysql-connector-python`, `keyring` | Optional MySQL metadata mirror / credential storage |

For a **new checkout and disposable environment only**, you can use uv's project workflow instead of the manual commands below:

```bash
uv python install 3.11
uv sync --python 3.11 --extra tts --extra mysql
# uv creates .venv/ by default, not .venv-chatterbox/
source .venv/bin/activate       # Bash/Zsh
# source .venv/bin/activate.fish # Fish
python scripts/audiobook_gui_v2.py
```

**Do not run `uv sync` in your current working Chatterbox environment without a backup.** It may change Torch/CUDA packages or remove dependencies not listed in the project. The `tts` extra currently selects upstream Chatterbox **0.1.7**, which has **not** been verified against V7 Smooth on the developer's GTX 1650. You can keep the proven manual setup below instead.

`pyproject.toml` expresses direct dependency constraints, **not exact transitive reproducibility**. After validating a fresh installation on your hardware, generate and review a `uv.lock` (`uv lock`), then commit it as a separate change. On later fresh installs, `uv sync --locked --extra tts --extra mysql` can reproduce the resolved Python dependencies for the same platform, subject to GPU drivers and model downloads. Keep the model and platform dependencies in mind. For a snapshot of the **existing working environment** without modifying it, run `uv pip freeze > requirements-tested.txt`, inspect the file for any private paths or URLs, and decide whether to commit it as a hardware-specific reference.

### 4.1 Chatterbox, PyTorch, and audio generation

For a **fresh** environment, start with Chatterbox's upstream package, which installs its declared dependencies:

```bash
uv pip install chatterbox-tts
```

Chatterbox's upstream project recommends Python 3.11 and uses `from chatterbox.tts import ChatterboxTTS`. The local V7 engine uses that **original English Chatterbox class**, not the newer Turbo or Multilingual API.

**CUDA matters:** Installing a Python package does not guarantee you got a GPU-compatible PyTorch build. After installation, verify:

```bash
python -c 'import torch, torchaudio; print("torch:", torch.__version__); print("torch CUDA:", torch.version.cuda); print("GPU available:", torch.cuda.is_available())'
```

For an NVIDIA GPU, `GPU available: True` is what you want. If it is `False`, check the driver and follow the platform-specific instructions at https://pytorch.org/get-started/locally/ to install a **compatible pair** of PyTorch and TorchAudio packages. Avoid upgrading either independently without checking version compatibility.

**Important for existing users:** If you already have a functioning Chatterbox/CUDA setup, preserve it. The project contains some engine-specific memory and generation safeguards that should be regression-tested before upgrading its model stack.

### 4.2 Application dependencies

```bash
uv pip install 'PySide6==6.11.2' ebooklib beautifulsoup4 lxml numpy soundfile
```

**Why this version is pinned:** PySide6 6.12.0 and its matching Shiboken bindings were present during multiple long-running native `none_dealloc` / `SIGABRT` GUI crashes. PySide6 **6.11.2** has run materially longer without that failure on the development laptop. The cause is still unconfirmed, so avoid upgrading to 6.12.x until the issue is understood and retested.

For an **existing environment already using 6.12.0**, downgrade the Qt bindings without recreating the environment:

```bash
uv pip install 'PySide6==6.11.2'
uv pip check
```

The main `PySide6` package installs matching **PySide6-Addons**, **PySide6-Essentials**, and **shiboken6**. Some older environments also have optional `pyside6-pdf` or `pyside6-webengine` packages tied to PySide6 6.12.0. AudiobookAI does **not** use these extras. If `uv pip check` reports those specific version conflicts and you do not use these packages elsewhere in this environment, uninstall them:

```bash
uv pip uninstall pyside6-pdf pyside6-webengine
uv pip check
```

Check all installed Qt bindings match:

```bash
python -c 'import PySide6, shiboken6; print("PySide6:", PySide6.__version__, "Shiboken:", shiboken6.__version__)'
uv pip freeze | grep -iE 'pyside6|shiboken6'
```

**Optional MySQL mirror / credential storage:**

```bash
uv pip install mysql-connector-python keyring
```

You can omit both MySQL packages if you only want to generate audiobooks with SQLite. `keyring` enables optional desktop credential storage; otherwise enter the password in Settings or provide it through an environment variable.

Check the core imports:

```bash
python -c 'import PySide6, ebooklib, bs4, lxml, numpy, soundfile, torch, torchaudio; from chatterbox.tts import ChatterboxTTS; print("Core imports OK")'
```

### 4.3 Supply a narrator WAV

The default path expected by the V7 engine is:

```text
voices/british_narrator.wav
```

Place **your own** permitted WAV recording there if you want to use the default narrator. Alternatively, start the GUI and use **Generate → Import WAV** to add another narrator; choose that voice before queueing jobs.

A relatively clean sample with consistent narration, little background noise, and no music is a sensible starting point. WAV validation is performed during voice import. Reference audio is not included with the source checkout.

### 4.4 Add an EPUB

Place a DRM-free EPUB in `books/`, or import it from another local directory through the Library page. For example:

```text
books/novel.epub
```

There are no example copyrighted books in the repository.

## 5. Start the application

With the virtual environment activated and from the repository root:

```bash
python scripts/audiobook_gui_v2.py
```

The application creates its local SQLite database under `output/audiobookai_studio.sqlite3` as needed.

The five primary pages are **Library**, **Generate**, **Queue**, **Review audio**, and **Settings**. For the first test:

1. Import a DRM-free EPUB through Library.
2. Inspect its section list in Generate; EPUB section numbers may include front matter and may not equal printed chapter numbers.
3. Choose a narrator WAV, then select a short section.
4. Queue and generate that section before processing a full book.
5. Check the completed chapter WAV and M4B in Review audio.

### Working in small batches and keeping completed parts

New parts are exported as `Book Name - Part(1).m4b`,
`Book Name - Part(2).m4b`, and so on. After the entire part is
**successfully exported and verified**, Studio saves a permanent receipt
in the local SQLite `completed_parts` metadata table. This receipt includes
the part number, exact EPUB sections, narrator snapshot, output location and
completion timestamp.

You can **delete a finished part from Queue** without deleting its audio
or losing its completed history. The Generate page lists saved parts; use
**Next unqueued** to move on without accidentally re-queueing completed
sections. The Review audio and full-book export pages also find completed
parts from these saved records after their queue rows are removed.

Failed or interrupted parts are **not** recorded as completed. Keep their
queue entries if you want to resume them using cached audio and their
frozen pronunciation snapshots. Old queue rows retain their original
filenames; files created before this feature are not renamed. For
older already-deleted parts, Studio attempts a one-time FFprobe validation of
old filenames with unambiguous EPUB section ranges; it does not assume a
missing or damaged M4B was completed.

`output/audiobookai_studio.sqlite3` is the permanent source of truth for
these receipts: include it in your metadata backups. You do not need to
keep dozens of **completed queue rows** to preserve part numbering.

**Do not use a 15-chapter overnight queue as your first installation test.** V2.10.1 does not download Chatterbox model weights implicitly. Download the model explicitly while connected, then test CUDA, voice-file parsing, and chapter boundaries with one short section.

### V2.10.4: External SSD cache preflight and recovery

AudiobookAI checks `output/narration_cache_v2` **before** launching the
converter. If that directory is a symlink into an unmounted SSD, Studio
shows **Storage unavailable** and pauses the queue without marking the pending
part as failed or deleting any audio. Mount the original drive, verify the
symlink still points at the same cache, then click **Queue → Check storage
& resume**. The storage line also reports disk free space when mounted.
The headless queue runner makes this check before marking a job running.

You can enable **Auto-resume when SSD returns** on Queue to retry access
every 15 seconds while paused for an unavailable cache. This option is
**off by default** and does not start jobs merely because Studio opened.
**Pause after part** and **Stop safely** cancel a pending SSD-based restart.
Previously failed jobs still require **Retry selected**. The preflight
does not try to mount or repair drives and will never replace a dangling
cache symlink.

On Arch Linux, check the mount with:

```bash
findmnt /run/media/garret/MySSD
test -d /run/media/garret/MySSD/AudiobookAI/narration_cache_v2 && echo 'Cache ready'
```

### V2.10.3: Drag-and-drop queue display fix

V2.10.2's drag-and-drop could leave blank rows because Qt removed native
table cells after the application saved the reordered queue. V2.10.3 disables
the destructive native MoveAction and refreshes from SQLite when dragging
finishes. A blank row does **not** mean its queued job was deleted. Restart the
GUI after an active conversion finishes to load the fix. Do not interrupt
generation just to repair the display.

### V2.10.2: Persistent conversion queue order

The Queue page shows the execution order top to bottom. Drag a pending part below
the rest of a book, or use **Move up**, **Move down**, and **Move to end**. The
order is saved immediately to SQLite and mirrored to MySQL when connected.
Clear search and status filters before rearranging. The currently active job
cannot be moved or preempted; changes affect the next job selected after it
finishes. To continue with remaining queued parts after a manually selected
chapter, press **Start queue** while that chapter is running. Reordering does
not modify audio caches, frozen pronunciation rules, or output filenames.

### V2.10.1: Offline-first model setup

AudiobookAI's converter uses locally cached Chatterbox files by default and does not check the Hugging Face Hub when starting each queued part. You **must explicitly download the model before the first generation** on a fresh installation:

1. Connect to the internet and open **Studio → Settings → Chatterbox models**.
2. Click **Download missing files…** and wait for the "verified locally" message.
3. Click **Check local models** to verify the complete cache *without* accessing the internet.
4. You can then generate offline. **Refresh all model files…** deliberately redownloads approximately 3 GB from Hugging Face; use it only when needed.

The same maintenance operations are available from the repository root with your existing virtual environment active:

```bash
python scripts/audiobookai_models.py check       # Offline check, never downloads
python scripts/audiobookai_models.py download    # Explicit online fetch
python scripts/audiobookai_models.py refresh     # Explicit full online refresh
```

Generation sets `HF_HUB_OFFLINE=1` by default before importing Chatterbox/Hugging Face, including the headless queue runner. A missing model produces instructions to use the download action instead of hanging on network access. Your model cache usually lives under `~/.cache/huggingface/hub` unless you have configured Hugging Face cache environment variables. You can still override the default by explicitly launching with `HF_HUB_OFFLINE=0`; the Settings buttons are preferred for updates. Model downloads do not replace your EPUB files, speech cache, or completed M4Bs.

### Optional command-line check

Inspect a book's section numbers (does not synthesize speech):

```bash
python scripts/epub_to_audiobook_v7_studio.py inspect books/novel.epub
```

Convert only section 1 if it is a suitable test section (change the section number after inspecting):

```bash
python scripts/epub_to_audiobook_v7_studio.py convert books/novel.epub \
  --chapters 1 --output-name first_test
```

For an alternate narrator, add `--voice voices/my_narrator.wav`.

## 6. Optional: MySQL synchronization

**MySQL is not required.** The local SQLite database controls the queue, even if MySQL is offline. MySQL is a **one-way mirror** for querying metadata, not a second writeable source of truth.

If you have a local MySQL server, the repository contains `sql/setup_mysql.sql`. Before running it:

1. Open the script in your SQL client.
2. **Replace the placeholder password.** The script creates the database `audiobookai` and a dedicated user at `127.0.0.1`.
3. Execute the SQL using an account with permission to create the database and user.
4. In **Settings → MySQL**, enter the matching host (`127.0.0.1`), port (normally `3306`), database (`audiobookai`), username, and password.
5. Click **Connect & sync**.

For an existing MySQL deployment you may instead use credentials for an account that already has access to the `audiobookai` schema. The application's config deliberately **does not store the password in JSON**. You can enter it on launch, use an optional keyring, or set `AUDIOBOOKAI_MYSQL_PASSWORD` in the application's environment.

The GUI's MySQL sync uses a separate worker process. A working connection and stable operation should still be tested on your own machine; integration tests do not replace that test.

Never put real passwords in `sql/setup_mysql.sql`, source files, or a Git commit.

## 7. Optional: KDE Plasma application launcher

After verifying that the app launches correctly in a terminal, create a desktop entry for your user. The following commands assume you cloned the repository to `~/AudiobookAI`:

```bash
mkdir -p ~/.local/share/applications
printf '%s\n' \
  '[Desktop Entry]' \
  'Version=1.0' \
  'Type=Application' \
  'Name=AudiobookAI Studio' \
  'Comment=Local EPUB-to-audiobook converter' \
  "Exec=$HOME/AudiobookAI/.venv-chatterbox/bin/python $HOME/AudiobookAI/scripts/audiobook_gui_v2.py" \
  "Path=$HOME/AudiobookAI" \
  'Icon=audio-headphones' \
  'Terminal=false' \
  'Categories=AudioVideo;Audio;' \
  'StartupNotify=true' \
  > ~/.local/share/applications/audiobookai-studio.desktop
```

Then refresh KDE's application database if the command is available:

```bash
kbuildsycoca6
```

Find **AudiobookAI Studio** in KDE's Application Launcher and pin it to the task manager if desired.

**Other checkout location?** Edit `Exec=` and `Path=` to your actual absolute paths. The `.desktop` file is a launcher for your existing Python environment, **not** a compiled standalone application.

## 8. Run tests

```bash
python -m unittest discover -s tests -v
```

The tests exercise the database, queue, voice paths, caching, quality checks, editing, and simulated recovery. They do not validate a live GPU conversion or a live MySQL server.

## 9. Files, storage, and upgrades

The following folders are created on your machine and ignored by Git:

```text
books/                          Local EPUBs
voices/                         Narrator WAVs and hidden queued-voice snapshots
output/audiobooks/              Chapter WAVs and finished M4B files
output/narration_cache_v2/      Raw WAV speech chunks
output/audiobookai_studio.sqlite3  SQLite queue, completed parts and library
```

To update a Git checkout **without changing the audio/cache folders**:

```bash
cd ~/AudiobookAI
git status
# Back up locally modified source files before merging updates.
git pull
```

Before any significant upgrade, close active conversions and back up the SQLite database, your narrator WAVs, and edited-take metadata. Do not delete `output/` or `voices/` to fix a Git problem.

**Cache cleanup:** Use Settings to preview and manually delete old raw chunk WAVs. Doing so can force regeneration of incomplete chapters or make offline rebuild impossible until those chunks are recreated.

## 10. Troubleshooting

| Symptom | What to check |
| --- | --- |
| `ModuleNotFoundError` or "Install inside your venv" | Activate `.venv-chatterbox` and confirm `sys.executable`; install the missing library in that environment. |
| CUDA unavailable / CPU generation is extremely slow | Verify `nvidia-smi`, `torch.cuda.is_available()`, and matching Torch/TorchAudio installations. |
| Missing narrator sample | Place your WAV in `voices/` or import/select an alternate WAV through Generate. |
| EPUB chapters incorrectly grouped | Run `inspect` and check section numbering. EPUB structures vary; do not assume every XHTML file is one chapter. |
| Missing Chatterbox model files / cannot generate offline | In V2.10.1 open **Settings → Chatterbox models → Download missing files…** while connected. Use **Check local models** to verify offline readiness; normal conversion does not download automatically. |
| MySQL offline or sync pending | SQLite still holds the local queue. Check server availability, credentials, and `sql/setup_mysql.sql`; do not delete the SQLite database. |
| `none_dealloc` / `SIGABRT` native GUI crash | Check for **PySide6 / Shiboken 6.12.x**. Pin the matched stack to **6.11.2** as shown in §4.2. The downgrade improved long-run stability on the development machine but is not a confirmed root-cause fix. Check for a surviving converter process before resuming an existing job; the Qt-free headless runner is available for unattended work. Save the traceback and exact versions. |
| Reused audio has the wrong narrator | Confirm the job narrator and voice snapshot, and avoid changing reference WAVs mid-job. |
| Resume seems to skip chapters | The backend recognizes already completed chapter WAVs and compatible cached speech. Check the output folder and chapter index before deleting anything. |
| Disk usage grows | Raw chunk caches and chapter WAVs can be large; preview cleanup in Settings only after you no longer need to resume or rebuild. |

For useful debugging details:

```bash
python --version
python -c 'import torch, PySide6; print("PyTorch", torch.__version__, "PySide6", PySide6.__version__, "CUDA", torch.cuda.is_available())'
ffmpeg -version
```

When filing a bug report, **redact passwords, credentials, private filenames, and copyrighted content**. Do not upload voice samples or full audiobooks.

## 11. Current scope

The project's [MIT license](LICENSE) applies only to original source and documentation. It does not give permission to redistribute book content, reference narrator voices, generated audiobooks, or third-party models. The `pyproject.toml` is a dependency manifest, **not** a ready-to-distribute standalone package.

This repository presently documents a **Linux source install**, not a universal turnkey installer. AMD ROCm, CPU-only, Windows, macOS, a bundled read-along EPUB reader, and distributable AppImage packaging are not yet validated or shipped in this source archive.

[← Back to README](README.md)

## V2.5 pronunciation dictionaries

Open the **Pronunciation** sidebar page after importing an EPUB. Add a global
rule or select **Specific book** to override a term for just that title.
For compatibility, no new Python dependencies are needed.

Each newly queued part saves its pronunciation snapshot under
`output/pronunciation/jobs/`. These files are deliberately not included in
public Git repositories, but **must be backed up** if you plan to rebuild
archival chapter audio later. Editing a rule does not alter existing jobs;
re-queue a new part if you want updated pronunciation, and keep a separate
backup before regenerating chapters that already exist.
