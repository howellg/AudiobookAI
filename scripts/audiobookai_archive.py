"""Offline audiobook archival export and portable metadata for Studio V2.6.

An existing *complete* M4B is refreshed from finished chapter WAVs if needed.
The audio is then stream-copied into an archival edition with proper metadata,
chapter markers, and optional embedded front cover. No TTS or GPU is used.
Local catalog settings remain in the ignored output/ directory.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import posixpath
import subprocess
import sys
import tempfile
from urllib.parse import unquote
import xml.etree.ElementTree as ET
import zipfile

ROOT = Path(__file__).resolve().parents[1]
CATALOG = ROOT / 'output' / 'archive' / 'catalog.json'
MAX_COVER_BYTES = 12 * 1024 * 1024


def _atomic_json(path: Path, value: dict) -> None:
    """Write metadata atomically to avoid losing a catalog during a crash."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix='.archive-', suffix='.json', dir=path.parent)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as out:
            json.dump(value, out, indent=2, ensure_ascii=False)
            out.write('\n')
        os.replace(tmp, path)
    finally:
        Path(tmp).unlink(missing_ok=True)


def catalog_key(epub: Path, narrator_folder: Path) -> str:
    """Identify an archive edition by both EPUB and narrator output folder."""
    return str(epub.expanduser().resolve()) + '\n' + str(narrator_folder.expanduser().resolve())


def load_catalog(path: Path = CATALOG) -> dict:
    """Read user-local archive preferences, failing loudly on corrupt JSON."""
    if not path.is_file():
        return {'version': 1, 'editions': {}}
    data = json.loads(path.read_text(encoding='utf-8'))
    if not isinstance(data, dict) or data.get('version') != 1 or not isinstance(data.get('editions'), dict):
        raise ValueError('Archive catalog is invalid or from an unsupported version')
    return data


def edition_info(epub: Path, folder: Path, path: Path = CATALOG) -> dict:
    """Return saved metadata for one book/narrator combination."""
    return dict(load_catalog(path)['editions'].get(catalog_key(epub, folder), {}))


def save_edition(epub: Path, folder: Path, details: dict, path: Path = CATALOG) -> None:
    """Persist descriptive metadata without changing source EPUB or audio."""
    valid_keys = ('title', 'author', 'series', 'volume', 'narrator', 'cover', 'last_export')
    clean = {k: str(details[k]).strip()[:500] for k in valid_keys if details.get(k) is not None}
    data = load_catalog(path)
    data['editions'][catalog_key(epub, folder)] = clean
    _atomic_json(path, data)


def audio_duration(folder: Path, indices: list[int]) -> float:
    """Compute seconds from indexed chapter WAV sample counts, not file guesses."""
    manifest = json.loads((folder / 'manifest.json').read_text(encoding='utf-8'))
    entries = manifest['chapters']
    return sum(entries[str(i)]['samples'] / entries[str(i)]['sample_rate'] for i in indices)


def _cover_kind(data: bytes) -> str | None:
    if data.startswith(b'\xff\xd8\xff'):
        return '.jpg'
    if data.startswith(b'\x89PNG\r\n\x1a\n'):
        return '.png'
    return None


def extract_epub_cover(epub: Path) -> tuple[bytes, str] | None:
    """Find the OPF-declared cover (EPUB2 or EPUB3) without extracting the archive.

    Do not trust member paths or decompress arbitrarily large images. An absent
    embedded cover is normal; the GUI can supply a user-selected image instead.
    """
    with zipfile.ZipFile(epub) as zf:
        container = ET.fromstring(zf.read('META-INF/container.xml'))
        rootfile = next((x.get('full-path') for x in container.iter()
                         if x.tag.endswith('rootfile') and x.get('full-path')), None)
        if not rootfile:
            return None
        package = ET.fromstring(zf.read(rootfile))
        items = [node for node in package.iter() if node.tag.endswith('item') and node.get('href')]
        cover_id = next((node.get('content') for node in package.iter()
                         if node.tag.endswith('meta') and node.get('name') == 'cover'), None)
        chosen = next((node for node in items if 'cover-image' in node.get('properties', '').split()), None)
        if chosen is None:
            chosen = next((node for node in items if node.get('id') == cover_id), None)
        if chosen is None:
            chosen = next((node for node in items
                           if 'cover' in node.get('href', '').lower() and
                           node.get('media-type', '').startswith('image/')), None)
        if chosen is None:
            return None
        href = unquote(chosen.get('href', '').split('#')[0])
        member = posixpath.normpath(posixpath.join(posixpath.dirname(rootfile), href))
        if member.startswith('../') or member.startswith('/') or member not in zf.namelist():
            return None
        info = zf.getinfo(member)
        if info.file_size > MAX_COVER_BYTES:
            return None
        content = zf.read(member)
        kind = _cover_kind(content)
        return (content, kind) if kind else None


def _probe(path: Path) -> dict:
    process = subprocess.run(['ffprobe', '-v', 'error', '-show_format', '-show_streams',
                              '-show_chapters', '-of', 'json', str(path)], check=True,
                             capture_output=True, text=True)
    return json.loads(process.stdout)


def archive_copy(source: Path, destination: Path, details: dict,
                 cover: Path | None = None) -> Path:
    """Copy audio streams, all chapter markers and tags into a verified archive.

    The existing destination is not replaced until FFprobe validates the new
    file. Metadata and artwork may be edited without lossy AAC re-encoding.
    """
    source = Path(source).resolve()
    destination = Path(destination).expanduser().resolve()
    if not source.is_file() or source.suffix.lower() != '.m4b':
        raise ValueError('A complete source M4B is required')
    if source == destination or destination.suffix.lower() != '.m4b':
        raise ValueError('Choose a distinct destination with an .m4b extension')
    cover = Path(cover).resolve() if cover else None
    if cover is not None and (not cover.is_file() or cover.suffix.lower() not in ('.jpg', '.jpeg', '.png')
                              or cover.stat().st_size > MAX_COVER_BYTES):
        raise ValueError('Choose a JPEG/PNG cover smaller than 12 MiB')
    old = _probe(source)
    if not any(s.get('codec_type') == 'audio' for s in old.get('streams', [])):
        raise ValueError('The complete source contains no audio')
    destination.parent.mkdir(parents=True, exist_ok=True)
    # A .m4b extension on the temporary file lets FFmpeg select the MP4 muxer.
    with tempfile.TemporaryDirectory(prefix='.archive-', dir=destination.parent) as tmp:
        staging = Path(tmp) / (destination.stem + '.m4b')
        cmd = ['ffmpeg', '-hide_banner', '-loglevel', 'error', '-y', '-i', str(source)]
        if cover:
            cmd += ['-i', str(cover)]
        cmd += ['-map', '0:a:0']
        if cover:
            cmd += ['-map', '1:v:0']
        cmd += ['-map_metadata', '0', '-map_chapters', '0', '-c:a', 'copy']
        if cover:
            cmd += ['-c:v', 'mjpeg', '-disposition:v:0', 'attached_pic']
        for field, key in [('title', 'title'), ('album', 'title'), ('artist', 'author'),
                           ('album_artist', 'author'), ('composer', 'narrator'),
                           ('show', 'series'), ('episode_id', 'volume')]:
            value = str(details.get(key, '')).strip()
            if value:
                cmd += ['-metadata', f'{field}={value}']
        if details.get('series'):
            label = str(details['series']) + (f" #{details['volume']}" if details.get('volume') else '')
            cmd += ['-metadata', f'grouping={label}']
        cmd += [str(staging)]
        subprocess.run(cmd, check=True)
        new = _probe(staging)
        old_chapters = len(old.get('chapters', []))
        if len(new.get('chapters', [])) != old_chapters:
            raise RuntimeError('Archive lost chapter markers; original output preserved')
        try:
            old_length = float(old['format']['duration'])
            new_length = float(new['format']['duration'])
        except (KeyError, ValueError) as exc:
            raise RuntimeError('Could not verify archive duration') from exc
        if old_length <= 0 or abs(old_length - new_length) > 2:
            raise RuntimeError('Archive duration differs unexpectedly; original preserved')
        if cover and not any(s.get('codec_type') == 'video' and
                             s.get('disposition', {}).get('attached_pic') for s in new['streams']):
            raise RuntimeError('Archive cover art was not embedded; original preserved')
        os.replace(staging, destination)
    return destination


def needs_complete_export(folder: Path, name: str, indices: list[int]) -> bool:
    """An old complete export is stale if any of its source chapters changed."""
    full = folder / f'{name}.m4b'
    if not full.is_file():
        return True
    age = full.stat().st_mtime_ns
    return any((folder / 'chapters' / f'{i:04d}.wav').stat().st_mtime_ns > age for i in indices)


def make_archive(epub: Path, folder: Path, indices: list[int], voice: Path | None,
                 destination: Path, details: dict, cover: Path | None = None) -> Path:
    """Refresh a complete edition from WAVs, then package it for long-term storage."""
    # Imports are cheap; GPU engine loading in the converter remains lazy.
    import epub_to_audiobook_v7_studio as converter
    from audiobookai_fullbook import verify_export_group
    # Recheck source validity in the subprocess; UI may have been open for hours.
    group = {'unfinished': [], 'indices': indices, 'voice_path': str(voice) if voice else None,
             'folder': folder}
    verify_export_group(group, len(converter.load_epub(epub)[2]))
    expected_folder = converter.book_folder(epub, voice)
    if expected_folder.resolve() != folder.resolve():
        raise ValueError('Narrator output folder mismatch')
    title, _author, _sections = converter.load_epub(epub)
    name = converter.safe_name(title, 100) + '_COMPLETE'
    if needs_complete_export(folder, name, indices):
        command = [sys.executable, str(converter.__file__), 'export', str(epub),
                   '--chapters', ','.join(str(i) for i in indices), '--output-name', name]
        if voice:
            command += ['--voice', str(voice)]
        print('Updating complete audiobook from saved WAVs…', flush=True)
        subprocess.run(command, check=True)
    full = folder / f'{name}.m4b'
    with tempfile.TemporaryDirectory(prefix='audiobook-cover-') as work:
        actual_cover = cover
        if not actual_cover:
            embedded = extract_epub_cover(epub)
            if embedded:
                payload, ext = embedded
                actual_cover = Path(work) / ('cover' + ext)
                actual_cover.write_bytes(payload)
        print('Packaging archival M4B with metadata and cover…', flush=True)
        archive_copy(full, destination, details, actual_cover)
    details = dict(details, last_export=str(Path(destination).resolve()))
    save_edition(epub, folder, details)
    return Path(destination)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--epub', type=Path, required=True)
    parser.add_argument('--folder', type=Path, required=True)
    parser.add_argument('--indices', required=True)
    parser.add_argument('--voice', type=Path)
    parser.add_argument('--destination', type=Path, required=True)
    parser.add_argument('--title', default='')
    parser.add_argument('--author', default='')
    parser.add_argument('--series', default='')
    parser.add_argument('--volume', default='')
    parser.add_argument('--narrator', default='')
    parser.add_argument('--cover', type=Path)
    args = parser.parse_args()
    indices = [int(v) for v in args.indices.split(',') if v.strip()]
    if not indices or len(indices) != len(set(indices)) or min(indices) < 1:
        parser.error('Provide a nonempty list of unique positive chapter numbers')
    details = {k: getattr(args, k) for k in ('title', 'author', 'series', 'volume', 'narrator')}
    if args.cover:
        details['cover'] = str(args.cover)
    result = make_archive(args.epub.resolve(), args.folder.resolve(), sorted(indices),
                          args.voice.resolve() if args.voice else None, args.destination, details,
                          args.cover)
    print(f'Archive ready: {result}', flush=True)


if __name__ == '__main__':
    main()