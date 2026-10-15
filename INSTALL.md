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

**Python 3.11 is recommended and required by the current `pyproject.toml` manifest.** The project was developed against a Python 3.11 Chatterbox environment. Dependencies are now declared, but a **tested lockfile has not been committed yet**; clean installs may require GPU-specific troubleshooting.

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
| Base | PySide6, EbookLib, Beautiful Soup, lxml, NumPy, SoundFile | Desktop application and EPUB processing |
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
uv pip install PySide6 ebooklib beautifulsoup4 lxml numpy soundfile
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

**Do not use a 15-chapter overnight queue as your first installation test.** The first run downloads model files and is the best time to check CUDA, voice-file parsing, and chapter boundaries.

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
output/audiobookai_studio.sqlite3  SQLite queue and library
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
| Model download warning / slow first run | Chatterbox model files may need an initial Hugging Face download. An authentication warning is not necessarily a fatal error. |
| MySQL offline or sync pending | SQLite still holds the local queue. Check server availability, credentials, and `sql/setup_mysql.sql`; do not delete the SQLite database. |
| `none_dealloc` / `SIGABRT` native GUI crash | This has occurred during development with Qt/MySQL-related native extensions. Save the traceback and exact versions; process isolation reduces one risk but is not a guaranteed cure. |
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
