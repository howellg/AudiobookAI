"""AudiobookAI V2.4: non-destructive chunk indexing, take history, audio QC, cache cleanup.

No Qt, Torch, or MySQL dependency. Never edits the source EPUB or shared raw
cache when a user makes a new take. Editing is stored alongside chapter WAVs.
"""
from __future__ import annotations

import hashlib
import json
import os
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
RAW_CACHE = ROOT / 'output' / 'narration_cache_v2'


def atomic_json(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f'.{path.name}.{os.getpid()}.tmp')
    try:
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')
        tmp.replace(path)
    finally:
        tmp.unlink(missing_ok=True)


def index_path(out, chapter):
    return Path(out) / 'chunk_indexes' / f'{int(chapter):04d}.json'


def edit_path(out, chapter):
    return Path(out) / 'chunk_edits' / f'{int(chapter):04d}.json'


def read_json(path, default=None):
    path = Path(path)
    if not path.exists():
        return {} if default is None else default
    return json.loads(path.read_text(encoding='utf-8'))


def save_index(out, chapter, title, chunks, engine, signature):
    """Immutable chunk boundaries and voice-keyed cache references for review."""
    path = index_path(out, chapter)
    entries = [{'number': i, 'text': chunk.text, 'boundary': chunk.boundary,
                'dialogue': bool(chunk.dialogue),
                'cache_file': str((engine.CACHE_DIR / (engine.cache_key(chunk.text) + '.wav')).resolve())}
               for i, chunk in enumerate(chunks, 1)]
    data = {'version': 1, 'index': int(chapter), 'title': title,
            'signature': signature, 'chunks': entries}
    if path.exists():
        try:
            if read_json(path) == data:
                return path
        except (OSError, ValueError):
            pass
    atomic_json(path, data)
    return path


def load_index(out, chapter):
    return read_json(index_path(out, chapter), {})


def load_edits(out, chapter):
    data = read_json(edit_path(out, chapter), {'version': 1, 'edits': {}})
    if not isinstance(data.get('edits'), dict):
        raise ValueError('Invalid chapter edit file')
    return data


def active_take(out, chapter, number, original_text):
    entry = load_edits(out, chapter)['edits'].get(str(number), {})
    if entry.get('original_text') != original_text:
        return None  # EPUB/chunking changed. Do not apply mismatched edits.
    active = entry.get('active')
    for take in entry.get('takes', []):
        if active is not None and take.get('id') == active:
            path = (Path(out).resolve() / take['wav']).resolve()
            if not path.is_relative_to(Path(out).resolve()) or not path.is_file():
                raise FileNotFoundError(f'Active edited take is missing or outside audiobook output: {path}')
            return {**take, 'path': str(path)}
    return None


def all_takes(out, chapter, number, original_text):
    entry = load_edits(out, chapter)['edits'].get(str(number), {})
    if entry.get('original_text') != original_text:
        return [], None
    return list(entry.get('takes', [])), entry.get('active')


def edit_digest(out, chapter, chunks):
    """Signature changes when an effective take changes, never on historical takes."""
    signature = []
    for i, chunk in enumerate(chunks, 1):
        take = active_take(out, chapter, i, chunk.text)
        signature.append((i, take['id'], take['text']) if take else (i, None))
    return hashlib.sha256(json.dumps(signature, ensure_ascii=False).encode()).hexdigest()


def take_destination(out, chapter, number):
    relative = Path('chunk_edits') / 'audio' / f'{int(chapter):04d}' / f'{int(number):04d}_{uuid.uuid4().hex}.wav'
    absolute = Path(out) / relative
    absolute.parent.mkdir(parents=True, exist_ok=True)
    return relative, absolute


def register_take(out, chapter, number, original_text, replacement_text, relative):
    out = Path(out).resolve()
    path = (out / relative).resolve()
    if not path.is_relative_to(out) or not path.is_file() or path.stat().st_size < 512:
        raise ValueError('Replacement WAV is missing, too small, or outside the chapter directory')
    data = load_edits(out, chapter)
    edits = data['edits']
    key = str(int(number))
    old = edits.get(key)
    if old is not None and old.get('original_text') != original_text:
        # Never silently apply old takes to a differently chunked book.
        raise ValueError('Source text changed since this chunk was edited. Reindex the chapter first.')
    entry = old or {'original_text': original_text, 'takes': [], 'active': None}
    take = {'id': uuid.uuid4().hex, 'text': replacement_text,
            'wav': str(relative), 'created': datetime.now(timezone.utc).isoformat()}
    entry['takes'].append(take)
    entry['active'] = take['id']
    edits[key] = entry
    atomic_json(edit_path(out, chapter), data)
    return take


def choose_take(out, chapter, number, original_text, take_id=None):
    data = load_edits(out, chapter)
    entry = data['edits'].get(str(number), {})
    if entry.get('original_text') != original_text:
        if take_id is None:
            return  # no edit to revert
        raise ValueError('Original text does not match saved take')
    if take_id is not None and take_id not in {t['id'] for t in entry.get('takes', [])}:
        raise ValueError('Unknown take ID')
    entry['active'] = take_id
    atomic_json(edit_path(out, chapter), data)


def original_chunk_file(index_entry):
    """Raw chunk must be within the app cache, never an arbitrary manifest path."""
    path = Path(index_entry['cache_file']).resolve()
    if not path.is_relative_to(RAW_CACHE.resolve()) or path.suffix.lower() != '.wav':
        raise ValueError(f'Invalid cache reference: {path}')
    return path


def quality_report(samples, sample_rate, words=0):
    """Low-cost diagnostics; only empty/silent/truncated data is hard failure.

    Clipping or unusually long pause is a review flag, not an automatic reject.
    Input is numpy/torch array in [-1,1], shape (channels, samples) or (samples,).
    """
    data = np.asarray(samples, dtype=np.float32)
    if data.ndim == 2:
        data = data.mean(axis=0)
    if data.ndim != 1 or data.size == 0 or int(sample_rate) <= 0:
        return {'unusable': True, 'flags': ['empty audio'], 'duration': 0.0}
    duration = data.size / int(sample_rate)
    peak = float(np.max(np.abs(data)))
    rms = float(np.sqrt(np.mean(data.astype(np.float64)**2)))
    over = float(np.mean(np.abs(data) >= 0.995))
    active = float(np.mean(np.abs(data) > 0.002))
    flags = []
    if not np.all(np.isfinite(data)):
        flags.append('nonfinite samples')
    if rms < 0.0005 or active < 0.01:
        flags.append('silent or nearly silent')
    if words >= 3 and duration < max(0.2, words * 0.065):
        flags.append('unexpectedly short for the text')
    if over > 0.015:
        flags.append('possible clipping')
    if words > 0 and duration > max(24, words * 3.2):
        flags.append('unusually long for the text')
    unusable = bool(set(flags) & {'empty audio', 'nonfinite samples', 'silent or nearly silent',
                                  'unexpectedly short for the text'})
    return {'unusable': unusable, 'flags': flags, 'duration': round(duration, 3),
            'peak': round(peak, 4), 'rms': round(rms, 5), 'active_fraction': round(active, 3)}


def inspect_wav(path, words=0):
    import soundfile as sf
    samples, rate = sf.read(str(path), dtype='float32', always_2d=False)
    if samples.ndim == 2:
        samples = samples.T
    return quality_report(samples, rate, words)


def cache_candidates(older_than_days=None, cache_dir=RAW_CACHE, now=None):
    """Only direct raw WAVs; no symlink traversal, book exports or edited takes."""
    cache = Path(cache_dir)
    if not cache.exists():
        return []
    threshold = (time.time() if now is None else now) - (older_than_days or 0) * 86400
    choices = []
    for path in cache.iterdir():
        if path.is_symlink() or not path.is_file() or path.suffix.lower() != '.wav':
            continue
        if older_than_days is not None and path.stat().st_mtime > threshold:
            continue
        choices.append(path)
    return choices


def cache_summary(older_than_days=None, cache_dir=RAW_CACHE, now=None):
    files = cache_candidates(older_than_days, cache_dir, now)
    return len(files), sum(p.stat().st_size for p in files)


def remove_raw_cache(older_than_days=None, cache_dir=RAW_CACHE, expected_count=None):
    cache = Path(cache_dir).resolve()
    if cache != RAW_CACHE.resolve():
        # Unit tests may specify an alternate isolated location, never permit
        # a broad or home directory cleanup by mistake.
        if not cache.name.startswith('test_'):
            raise ValueError('Cache deletion is limited to AudiobookAI raw cache')
    files = cache_candidates(older_than_days, cache)
    if expected_count is not None and len(files) != expected_count:
        raise RuntimeError('Cache contents changed; preview again before deletion.')
    removed, total = 0, 0
    for path in files:
        size = path.stat().st_size
        path.unlink()
        removed += 1
        total += size
    return removed, total
