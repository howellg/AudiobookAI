#!/usr/bin/env python3
"""V2.14 safe raw-speech cache inventory and manual cleanup.

- GPU/Qt-free. The Chatterbox engine records *only* hashes and generation
  settings; the catalog stores no actual EPUB prose.
- Existing chunk indexes can backfill identity ONLY after the key is
  independently recomputed and matched. Unknown files are never candidates.
- Saved chapter indexes protect raw WAVs even if their reference is stale.
- Every deletion requires a recent identical preview, a full inode/stat
  preflight, no active generation, and a protected-index recheck.
"""
from __future__ import annotations
import argparse
from collections import Counter, defaultdict
from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import sqlite3
import stat
import sys
import tempfile
import time

from audiobookai_acting import chunk_settings
from audiobookai_cache import cache_preflight

ROOT = Path(__file__).resolve().parents[1]
CACHE = ROOT / 'output' / 'narration_cache_v2'
OUTPUT = ROOT / 'output'
WORK = OUTPUT / 'cache_manager'
CATALOG = WORK / 'catalog.sqlite3'
PREVIEW = WORK / 'cleanup_preview.json'
SUMMARY = WORK / 'summary.json'
LOCK = WORK / 'operation.lock'
EVENT_PREFIX = 'AUDIOBOOKAI_CACHE_EVENT '
NAME_RE = re.compile(r'^[0-9a-f]{24}\.wav$')
LEGACY_CUTOFF = datetime(2026, 10, 21, 20, 32, tzinfo=timezone.utc).timestamp()
SCOPE_LABELS = {
    'legacy': 'Pre-V2.10.0 standard dialogue (may be reused if standard mode is selected)',
    'dialogue': 'Verified unreferenced dialogue, all compatible modes',
    'all': 'All verified unreferenced WAVs, INCLUDING reusable prose',
}
SCHEMA = '''CREATE TABLE IF NOT EXISTS entries(
    name TEXT PRIMARY KEY,
    voice_path TEXT NOT NULL,
    voice_size INTEGER NOT NULL,
    voice_mtime_ns INTEGER NOT NULL,
    text_sha TEXT NOT NULL,
    exaggeration REAL NOT NULL,
    cfg REAL NOT NULL,
    dialogue INTEGER NOT NULL,
    modes INTEGER NOT NULL,
    size INTEGER NOT NULL,
    mtime_ns INTEGER NOT NULL,
    device INTEGER NOT NULL,
    inode INTEGER NOT NULL,
    first_seen REAL NOT NULL,
    last_seen REAL NOT NULL,
    source TEXT NOT NULL
)'''
UPSERT = '''INSERT INTO entries (
name,voice_path,voice_size,voice_mtime_ns,text_sha,exaggeration,cfg,dialogue,modes,
size,mtime_ns,device,inode,first_seen,last_seen,source)
VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
ON CONFLICT(name) DO UPDATE SET
 modes=entries.modes | excluded.modes,
 size=excluded.size,mtime_ns=excluded.mtime_ns,device=excluded.device,inode=excluded.inode,
 last_seen=excluded.last_seen,
 source=CASE WHEN excluded.source='engine' THEN 'engine' ELSE entries.source END,
 voice_path=excluded.voice_path, voice_size=excluded.voice_size,
 voice_mtime_ns=excluded.voice_mtime_ns, text_sha=excluded.text_sha,
 exaggeration=excluded.exaggeration, cfg=excluded.cfg, dialogue=excluded.dialogue
'''
MODE = {'standard': 1, 'expressive': 2}


def _safe_name(name):
    return bool(NAME_RE.fullmatch(str(name)))


def _secret_json(path, payload):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_symlink():
        raise RuntimeError('Refusing symlinked cache manifest')
    fd, tmp = tempfile.mkstemp(prefix='.cachemgr_', suffix='.json', dir=path.parent)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, 'w', encoding='utf-8') as stream:
            json.dump(payload, stream, ensure_ascii=False, separators=(',', ':'))
            stream.write('\n')
        if path.is_symlink():
            raise RuntimeError('Cache manifest became a symlink')
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def _load_json(path):
    if Path(path).is_symlink():
        raise RuntimeError('Refusing symlinked cache manifest')
    return json.loads(Path(path).read_text(encoding='utf-8'))


def _db(path):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_symlink():
        raise RuntimeError('Refusing symlinked cache catalog')
    conn = sqlite3.connect(path, timeout=5.0)
    conn.execute('PRAGMA busy_timeout=5000')
    conn.execute(SCHEMA)
    return conn


def _delivery(mode, dialogue):
    ov = chunk_settings(mode, dialogue)
    return ov.get('exaggeration', .35), ov.get('cfg_weight', .3)


def key_for(text, voice_path, size, mtime_ns, exaggeration, cfg):
    signature = (f'chatterbox-tts|{text}|{exaggeration}|{cfg}|'
                 f'{Path(voice_path).resolve()}|{size}|{mtime_ns}')
    return hashlib.sha256(signature.encode('utf-8')).hexdigest()[:24] + '.wav'


def _item(path, st, text, voice, voice_stat, mode, dialogue, source, used=None):
    ex, cfg = _delivery(mode, dialogue)
    return (
        path.name, str(voice.resolve()), voice_stat.st_size, voice_stat.st_mtime_ns,
        hashlib.sha256(text.encode('utf-8')).hexdigest(), ex, cfg, int(dialogue),
        MODE[mode], st.st_size, st.st_mtime_ns, st.st_dev, st.st_ino,
        st.st_mtime, float(used if used is not None else st.st_mtime), source)


def note_chunk(path, text, voice, mode, dialogue):
    """Best-effort, low-overhead catalog record for a validated cache hit/write.

    Do not call from force=True editor takes or from failed/partial generation.
    Recording never modifies the chunk, voice or EPUB.
    """
    path, voice = Path(path), Path(voice)
    if not _safe_name(path.name) or not path.is_file() or path.is_symlink():
        return
    st, vs = path.stat(), voice.stat()
    ex, cfg = _delivery(mode, bool(dialogue))
    if key_for(text, voice, vs.st_size, vs.st_mtime_ns, ex, cfg) != path.name:
        raise ValueError('Cache hash does not match narration settings')
    row = _item(path, st, text, voice, vs, mode, bool(dialogue),
                'engine', used=time.time())
    with _db(CATALOG) as db:
        db.execute(UPSERT, row)


def _index_records(output, cache_names, cache_path):
    """Yield protected keys and cryptographically validated historical rows.

    Bad or unreadable indexes must abort cleanup rather than permit deletion.
    """
    output = Path(output)
    protected = set()
    validated = {}
    count = 0
    index_root = output / 'audiobooks'
    if not index_root.is_dir():
        return protected, validated, 0
    for index_path in index_root.rglob('chunk_indexes/*.json'):
        if index_path.is_symlink():
            raise RuntimeError(f'Unsafe symlinked chapter index: {index_path}')
        try:
            data = _load_json(index_path)
            chunks = data['chunks']
            if not isinstance(chunks, list):
                raise ValueError('Chunk index is not a list')
        except (OSError, KeyError, TypeError, ValueError) as exc:
            raise RuntimeError(f'Cannot verify saved chapter index {index_path}: {exc}') from exc
        count += 1
        folder = index_path.parent.parent
        manifest = folder / 'manifest.json'
        metadata = {}
        if manifest.is_file():
            try:
                metadata = _load_json(manifest)
            except (OSError, TypeError, ValueError):
                # Index still protects its listed names. If metadata is
                # unreadable, never guess that its voice was default.
                metadata = {'voice_path':'INVALID-MANIFEST'}
        vref = metadata.get('voice_path')
        voice = Path(vref) if vref else (ROOT / 'voices' / 'british_narrator.wav')
        mode = metadata.get('acting_mode') or (
            'expressive' if 'expressive' in folder.parts else 'standard')
        try:
            voice_stat = voice.stat()
            if mode not in MODE: raise ValueError('Unknown acting mode')
        except (OSError, ValueError):
            voice_stat = None
        for entry in chunks:
            if not isinstance(entry, dict):
                raise RuntimeError(f'Invalid chunk in {index_path}')
            raw = entry.get('cache_file', '')
            if not isinstance(raw, str):
                raise RuntimeError(f'Invalid cache filename in {index_path}')
            rawpath = Path(raw)
            # Any index reference to our cache protects that filename,
            # even if the voice fingerprint cannot be reconstructed.
            if not _safe_name(rawpath.name):
                continue
            if rawpath.parent.resolve() != Path(cache_path).resolve():
                continue
            name = rawpath.name
            protected.add(name)
            if name not in cache_names or voice_stat is None or name in validated:
                continue
            text = entry.get('text')
            if not isinstance(text, str) or not text:
                continue
            dialogue = text.lstrip().startswith(('"', '“', '‘'))
            ex, cfg = _delivery(mode, dialogue)
            if key_for(text, voice, voice_stat.st_size, voice_stat.st_mtime_ns, ex, cfg) != name:
                continue
            st = cache_names[name]
            validated[name] = _item(Path(cache_path) / name, st, text, voice,
                                    voice_stat, mode, dialogue, 'index')
    return protected, validated, count


def _active_conversion():
    """Reject file deletion during TTS, GUI runner, headless or chunk-editor TTS."""
    for proc in Path('/proc').iterdir():
        if not proc.name.isdecimal():
            continue
        try:
            raw = (proc / 'cmdline').read_bytes().split(b'\0')
        except (OSError, PermissionError):
            continue
        args = [x.decode('utf-8','replace') for x in raw if x]
        if not args: continue
        main = [Path(arg).name for arg in args if arg.endswith('.py')]
        if {'epub_to_audiobook_v7_studio.py', 'audiobookai_headless.py',
            'test_five_minutes_v7_smooth.py'} & set(main):
            return True
    return False


@contextmanager
def operation_lock(lock=LOCK):
    lock = Path(lock)
    lock.parent.mkdir(parents=True, exist_ok=True)
    if lock.is_symlink():
        raise RuntimeError('Unsafe symlinked cache manager lock')
    with lock.open('a+b') as fd:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RuntimeError('Cache manager busy: finish the other inventory or cleanup first') from exc
        try:
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)


def operation_busy(lock=LOCK):
    """Check a cleanup lock without blocking Studio's Qt thread."""
    lock = Path(lock)
    if lock.is_symlink():
        return True
    if not lock.is_file():
        return False
    with lock.open('rb') as fd:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
        finally:
            try: fcntl.flock(fd, fcntl.LOCK_UN)
            except OSError: pass
    return False


def _files(cache):
    """Only directly stored, real, hash-named WAVs, without symlinks."""
    found = {}
    other = [0, 0]
    with os.scandir(cache) as it:
        for de in it:
            try:
                if de.is_symlink() or not de.is_file(follow_symlinks=False):
                    continue
                st = de.stat(follow_symlinks=False)
                if _safe_name(de.name):
                    found[de.name] = st
                elif de.name.lower().endswith('.wav'):
                    other[0] += 1; other[1] += st.st_size
            except OSError:
                continue
    return found, other


def _profile(voice, ex, dialogue):
    return f'{Path(voice).stem} · ' + (
        'dialogue' if dialogue else 'prose') + f' · exaggeration {ex:g}'


def _candidate(row, st, references, now, scope, age_days):
    name, voice, vsz, vtime, text_sha, ex, cfg, dia, modes, size, mtime, dev, ino, first, last, source = row
    if name in references:
        return False
    if (size, mtime, dev, ino) != (st.st_size, st.st_mtime_ns, st.st_dev, st.st_ino):
        return False
    threshold = now - age_days * 86400
    if st.st_mtime > threshold or last > threshold:
        return False
    if scope == 'legacy':
        return bool(dia and abs(ex-.35)<1e-6 and st.st_mtime < LEGACY_CUTOFF
                    and not (modes & MODE['expressive']))
    if scope == 'dialogue':
        return bool(dia)
    if scope == 'all':
        return True
    raise ValueError('Invalid cleanup scope')


def inventory(*, cache=CACHE, output=OUTPUT, catalog=CATALOG,
              work=WORK, scope='legacy', days=90, progress=None):
    cache, output, work = Path(cache), Path(output), Path(work)
    if scope not in SCOPE_LABELS or not 1 <= int(days) <= 3650:
        raise ValueError('Unknown cleanup scope or age filter')
    ready = cache_preflight(cache)
    if not ready.ready or not cache.is_dir():
        raise RuntimeError(f'Cache storage unavailable: {ready.detail}')
    if _active_conversion():
        raise RuntimeError('A narration converter is active. Audit after generation finishes.')
    files, atypical = _files(cache)
    if progress: progress(f'Scanned {len(files):,} hash WAVs')
    refs, backfill, indexes = _index_records(output, files, cache)
    if progress: progress(f'Protected {len(refs):,} names from {indexes:,} chapter indexes')
    with _db(catalog) as db:
        for row in backfill.values():
            db.execute('INSERT OR IGNORE INTO entries VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)', row)
        records = {row[0]: row for row in db.execute('SELECT * FROM entries')}
    candidates = []
    by_profile = defaultdict(lambda: [0,0])
    counts = Counter()
    sizes = Counter()
    now = time.time()
    for name, st in files.items():
        row = records.get(name)
        referenced = name in refs
        known = bool(row and (row[9],row[10],row[11],row[12]) ==
                     (st.st_size,st.st_mtime_ns,st.st_dev,st.st_ino))
        kind = 'protected_index' if referenced else ('identified' if known else 'unknown')
        counts[kind] += 1
        sizes[kind] += st.st_size
        if known:
            key = _profile(row[1], row[5], row[7])
            by_profile[key][0] += 1
            by_profile[key][1] += st.st_size
        if known and _candidate(row, st, refs, now, scope, days):
            candidates.append({
                'name':name,'inode':st.st_ino,'device':st.st_dev,
                'size':st.st_size,'mtime_ns':st.st_mtime_ns,
                'last_seen':row[14]})
    candidates.sort(key=lambda x:x['name'])
    token = secrets.token_hex(16)
    preview = {'version':1,'id':token,'created_at':now,'scope':scope,
               'days':int(days),'cache':str(cache.resolve()),
               'count':len(candidates),'items':candidates}
    work.mkdir(parents=True, exist_ok=True)
    _secret_json(work/'cleanup_preview.json',preview)
    report = {'version':1,'id':token,'created_at':now,'scope':scope,'age_days':days,
              'scope_description':SCOPE_LABELS[scope],
              'total_files':sum(counts.values())+atypical[0],
              'total_bytes':sum(sizes.values())+atypical[1],
              'candidate_files':len(candidates),
              'candidate_bytes':sum(x['size'] for x in candidates),
              'categories':{k:{'files':counts[k],'bytes':sizes[k]} for k in
                           ('identified','protected_index','unknown')},
              'other_wavs':{'files':atypical[0],'bytes':atypical[1]},
              'indexes_checked':indexes, 'index_backfilled':len(backfill),
              'profiles':[{'name':label,'files':v[0],'bytes':v[1]}
                          for label,v in sorted(by_profile.items(),
                            key=lambda kv:kv[1][1],reverse=True)[:30]]}
    _secret_json(work/'summary.json', report)
    return report


def delete_preview(token, *, cache=CACHE, output=OUTPUT,
                   catalog=CATALOG, work=WORK):
    """Delete only previewed filenames after *full* revalidation (not by age)."""
    cache, output, work = Path(cache), Path(output), Path(work)
    ready = cache_preflight(cache)
    if not ready.ready or not cache.is_dir():
        raise RuntimeError('Cache drive unavailable; no files deleted')
    if _active_conversion():
        raise RuntimeError('Narration is running; no files deleted')
    preview = _load_json(work/'cleanup_preview.json')
    if not isinstance(token,str) or not secrets.compare_digest(str(preview.get('id')),token):
        raise RuntimeError('Cleanup confirmation token does not match current preview')
    if preview.get('version')!=1 or time.time()-preview.get('created_at',0)>3600:
        raise RuntimeError('Cleanup preview expired: scan again')
    if preview['cache']!=str(cache.resolve()):
        raise RuntimeError('Cache location changed; no files deleted')
    items=preview['items']
    if (not isinstance(items,list) or len(items)!=preview['count'] or
        len({x.get('name') for x in items})!=len(items)):
        raise RuntimeError('Invalid cleanup preview')
    files,_ = _files(cache)
    refs,_,_ = _index_records(output, files, cache)
    with _db(catalog) as db:
        rows={row[0]:row for row in db.execute('SELECT * FROM entries')}
    now = time.time()
    fd=os.open(cache, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    done=total=0
    try:
        device=os.fstat(fd).st_dev
        # Validate *all* entries and catalog identity before removing the first.
        for x in items:
            name=x['name']
            if not _safe_name(name) or name in refs:
                raise RuntimeError('A candidate is now referenced or invalid; no files deleted')
            try: st=os.stat(name,dir_fd=fd,follow_symlinks=False)
            except FileNotFoundError as exc:
                raise RuntimeError('A candidate vanished; scan again') from exc
            if not stat.S_ISREG(st.st_mode) or st.st_dev!=device or (
                st.st_dev,st.st_ino,st.st_size,st.st_mtime_ns)!=(
                x['device'],x['inode'],x['size'],x['mtime_ns']):
                raise RuntimeError('A candidate changed since preview; no files deleted')
            row=rows.get(name)
            if (row is None or row[14]!=x['last_seen'] or not
                _candidate(row,st,refs,now,preview['scope'],preview['days'])):
                raise RuntimeError('A candidate was reused or no longer qualifies; no files deleted')
        if _active_conversion():
            raise RuntimeError('Narration started during preflight; no files deleted')
        for x in items:
            name=x['name']
            if done % 100 == 0 and _active_conversion():
                raise RuntimeError(f'Narration started; stopped after removing {done} WAVs')
            st=os.stat(name,dir_fd=fd,follow_symlinks=False)
            if not stat.S_ISREG(st.st_mode) or (st.st_dev,st.st_ino,st.st_size,st.st_mtime_ns) != (
                x['device'],x['inode'],x['size'],x['mtime_ns']):
                raise RuntimeError(f'File changed during deletion; stopped after {done}')
            os.unlink(name,dir_fd=fd)
            done+=1;total+=st.st_size
        os.fsync(fd)
    finally:
        os.close(fd)
    # The old preview cannot be replayed.
    (work/'cleanup_preview.json').unlink(missing_ok=True)
    return {'deleted_files':done,'deleted_bytes':total}


def event(message, **other):
    print(EVENT_PREFIX + json.dumps({'message':message,**other}),flush=True)


def main(argv=None):
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('command',choices=['scan','delete'])
    p.add_argument('--scope',choices=tuple(SCOPE_LABELS),default='legacy')
    p.add_argument('--days',type=int,default=90)
    p.add_argument('--token',default='')
    args=p.parse_args(argv)
    try:
        with operation_lock():
            if args.command=='scan':
                result=inventory(scope=args.scope,days=args.days,progress=lambda x:event(x))
            else:
                result=delete_preview(args.token)
            event('Cache manager complete',kind='complete',result=result)
            return 0
    except (OSError,ValueError,RuntimeError,KeyError,sqlite3.Error) as exc:
        event('Cache manager failed',kind='error',error=str(exc)[:420])
        return 1


if __name__=='__main__':
    raise SystemExit(main())
