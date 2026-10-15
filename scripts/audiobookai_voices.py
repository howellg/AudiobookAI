"""Voice selection, safe imports, and stable queued voice snapshots.

No Chatterbox / torch dependency: safe to import in the UI and unit tests.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
VOICE_DIR = ROOT / 'voices'
DEFAULT_VOICE = VOICE_DIR / 'british_narrator.wav'
SNAPSHOT_DIR = VOICE_DIR / '.audiobookai_voice_snapshots'


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with Path(path).open('rb') as file:
        for chunk in iter(lambda: file.read(1024 * 1024), b''):
            h.update(chunk)
    return h.hexdigest()


def validate_voice(path: Path):
    """Check that a user-supplied reference is an actual decodable WAV with audio."""
    path = Path(path).expanduser().resolve()
    if not path.is_file() or path.suffix.lower() != '.wav':
        raise ValueError('Choose an existing .wav narrator recording.')
    if path.stat().st_size < 512:
        raise ValueError('The narrator WAV appears empty or truncated.')
    try:
        result = subprocess.run(
            ['ffprobe', '-v', 'error', '-select_streams', 'a:0',
             '-show_entries', 'stream=codec_name,sample_rate,channels:format=duration',
             '-of', 'json', str(path)],
            capture_output=True, text=True, check=True, timeout=15)
    except (FileNotFoundError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        raise ValueError('FFprobe could not decode this narrator WAV. Check the recording and FFmpeg installation.') from exc
    try:
        payload = json.loads(result.stdout)
        if not payload.get('streams'):
            raise ValueError('No audio stream found in the WAV.')
        duration = float(payload.get('format', {}).get('duration') or 0)
        if duration < 0.5:
            raise ValueError('Narrator recording is too short; use a clear sample at least half a second long.')
    except (ValueError, TypeError, KeyError) as exc:
        if isinstance(exc, ValueError) and str(exc).startswith(('No audio', 'Narrator recording')):
            raise
        raise ValueError('Could not determine WAV audio format or duration.') from exc
    return path


def voice_choices():
    """Human-selectable recordings, excluding hidden job snapshots."""
    VOICE_DIR.mkdir(parents=True, exist_ok=True)
    return sorted((p.resolve() for p in VOICE_DIR.rglob('*')
                   if p.is_file() and p.suffix.lower() == '.wav'
                   and SNAPSHOT_DIR not in p.parents),
                  key=lambda p: p.name.casefold())


def import_voice(source: Path) -> Path:
    """Copy an external recording into the user's voices library (never overwrite)."""
    source = validate_voice(source)
    VOICE_DIR.mkdir(parents=True, exist_ok=True)
    if source.parent == VOICE_DIR or VOICE_DIR in source.parents:
        return source
    digest = sha256_file(source)[:12]
    safe_stem = re.sub(r'[^\w-]+', '_', source.stem, flags=re.UNICODE).strip('_')[:55] or 'narrator'
    dest = VOICE_DIR / f'{safe_stem}_{digest}.wav'
    if dest.exists() and sha256_file(dest) != sha256_file(source):
        raise OSError('The imported narrator file has changed; rename it or restore the original.')
    if not dest.exists():
        temp = dest.with_name(dest.name + f'.{os.getpid()}.partial')
        try:
            shutil.copy2(source, temp)
            if sha256_file(temp) != sha256_file(source):
                raise OSError('Voice recording changed during copy; import cancelled.')
            temp.replace(dest)
        finally:
            temp.unlink(missing_ok=True)
    return dest


def freeze_voice(source: Path | None) -> Path | None:
    """Freeze a *nondefault* queued narrator by content so later edits won't change the job.

    None denotes the original V7 default narrator, preserving its old caches.
    """
    if not source:
        return None
    source = validate_voice(source)
    if source == DEFAULT_VOICE.resolve():
        return None
    digest = sha256_file(source)
    SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)
    stem = re.sub(r'[^\w-]+', '_', source.stem, flags=re.UNICODE).strip('_')[:48] or 'narrator'
    dest = SNAPSHOT_DIR / f'{stem}_{digest[:20]}.wav'
    if dest.exists() and sha256_file(dest) != digest:
        raise OSError('Saved narrator snapshot is corrupted; cannot queue this voice.')
    if not dest.exists():
        temp = dest.with_name(dest.name + f'.{os.getpid()}.partial')
        try:
            shutil.copy2(source, temp)
            if sha256_file(temp) != digest:
                raise OSError('Narrator WAV changed while queueing; try again.')
            temp.replace(dest)
        finally:
            temp.unlink(missing_ok=True)
    return dest


def voice_name(path: str | Path | None) -> str:
    if not path:
        return 'British narrator (default)'
    p = Path(path)
    if p.parent == SNAPSHOT_DIR:
        stem = re.sub(r'_[a-f0-9]{20}$', '', p.stem)
        stem = re.sub(r'_[a-f0-9]{12}$', '', stem)
        return stem.replace('_', ' ')
    return p.stem.replace('_', ' ')
