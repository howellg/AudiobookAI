"""Read-only inspection of a snapshot of Studio's pending jobs."""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
import json
import math
import os
from pathlib import Path
import shutil
import sys

from audiobookai_cache import cache_preflight
from audiobookai_convenience import estimate_audio_duration
from audiobookai_pronunciation import load_snapshot, snapshot_path_for_job
from audiobookai_voices import DEFAULT_VOICE, validate_voice
from epub_to_audiobook_v7_studio import OUT_ROOT, book_folder, load_epub

# Budget for 24 kHz mono WAV at up to 32 bits/sample, plus 64 kbit/s AAC.
WAV_BYTES_PER_SECOND = 24_000 * 4
M4B_BYTES_PER_SECOND = 64_000 // 8
SPACE_MARGIN = 1.25


@dataclass
class Volume:
    free: int
    paths: set[str] = field(default_factory=set)
    output: int = 0
    cache: int = 0


def volume_for(path, volumes):
    """Find the filesystem a new file would use without creating directories."""
    path = Path(path)
    parent = path
    while not parent.exists():
        if parent.is_symlink() or parent == parent.parent:
            raise OSError(f'Folder or symlink target unavailable: {parent}')
        parent = parent.parent
    if not parent.is_dir() or not os.access(parent, os.W_OK | os.X_OK):
        raise OSError(f'Folder is not a writable directory: {parent}')
    device = parent.stat().st_dev
    if device not in volumes:
        volumes[device] = Volume(shutil.disk_usage(parent).free)
    volume = volumes[device]
    volume.paths.add(str(path))
    return volume


def inspect_queue(jobs, cache_dir, *, output_root=OUT_ROOT,
                  snapshot_dir=None, default_voice=DEFAULT_VOICE):
    """Check every queued job and estimate additional storage by filesystem."""
    pending = [job for job in jobs if job['status'] == 'queued']
    if not pending:
        return ['No queued parts.']

    system_issues = []
    blocked_parts = 0
    warnings = []
    rows = []
    volumes = {}
    cache = cache_preflight(cache_dir)
    cache_volume = None
    if cache.ready:
        try:
            cache_volume = volume_for(cache_dir, volumes)
        except OSError as exc:
            system_issues.append(f'Cannot check narration cache space: {exc}')

    ffprobe = shutil.which('ffprobe')
    for program, available in [('FFmpeg', shutil.which('ffmpeg')), ('FFprobe', ffprobe)]:
        if not available:
            system_issues.append(f'{program} is unavailable on PATH.')

    books = {}
    voices = {}
    rendered_sections = defaultdict(set)
    for job in pending:
        issues = []
        epub = Path(job['book_path'])
        voice = Path(job['voice_path']) if job['voice_path'] else Path(default_voice)
        if epub not in books:
            try:
                if not epub.is_file():
                    raise FileNotFoundError('file is missing')
                _, _, sections = load_epub(epub)
                books[epub] = [len(section.text.split()) for section in sections]
            except (Exception, SystemExit) as exc:
                books[epub] = f'Source EPUB cannot be opened: {epub} ({exc})'
        words = books[epub]
        if isinstance(words, str):
            issues.append(words)

        if voice not in voices:
            try:
                if not voice.is_file():
                    raise FileNotFoundError('file is missing')
                if ffprobe:
                    validate_voice(voice)
                voices[voice] = None
            except (OSError, ValueError) as exc:
                voices[voice] = f'Narrator WAV unavailable or invalid: {voice} ({exc})'
        if voices[voice]:
            issues.append(voices[voice])

        if job['pronunciation_required']:
            try:
                snapshot = (snapshot_path_for_job(job['id']) if snapshot_dir is None
                            else snapshot_path_for_job(job['id'], snapshot_dir))
                if snapshot is None:
                    raise FileNotFoundError('file is missing')
                load_snapshot(snapshot)
            except (OSError, ValueError, TypeError) as exc:
                issues.append(f'Required pronunciation snapshot cannot be read ({exc}).')

        try:
            selected = json.loads(job['indices'])
            if (not isinstance(selected, list) or not selected or
                    any(type(index) is not int or index < 1 for index in selected)):
                raise ValueError('invalid stored selection')
            selected = set(selected)
            if isinstance(words, list) and max(selected) > len(words):
                raise ValueError(f'the current EPUB has only {len(words)} readable sections')
        except (ValueError, TypeError) as exc:
            issues.append(f'Invalid EPUB section selection ({exc}).')

        if not issues:
            try:
                folder = book_folder(epub, voice if job['voice_path'] else None,
                                     job['acting_mode'], output_root=output_root)
                volume = volume_for(folder, volumes)
                chapter_indices = selected - rendered_sections[folder]
                rendered_sections[folder].update(chapter_indices)
                wav_seconds = estimate_audio_duration(sum(words[i - 1] for i in chapter_indices))
                m4b_seconds = estimate_audio_duration(sum(words[i - 1] for i in selected))
                wav_bytes = math.ceil(wav_seconds * WAV_BYTES_PER_SECOND * SPACE_MARGIN)
                volume.output += wav_bytes + math.ceil(
                    m4b_seconds * M4B_BYTES_PER_SECOND * SPACE_MARGIN)
                if cache_volume is not None:
                    cache_volume.cache += wav_bytes
            except (OSError, ValueError) as exc:
                issues.append(f'Cannot use the audiobook output folder ({exc}).')

        label = f"#{job['id']} · {job['book_title']} · {job['name']}"
        if issues:
            blocked_parts += 1
        rows.append(label + ('\n  ' + '\n  '.join(issues) if issues else ' · Inputs ready'))

    space = []
    for volume in volumes.values():
        estimated = volume.output + volume.cache
        locations = ', '.join(sorted(volume.paths))
        if estimated > volume.free:
            warnings.append(f'Estimated space exceeds available space on the filesystem for {locations}.')
        space.append(f'{locations}\n  {format_bytes(volume.free)} free · '
                     f'{format_bytes(volume.output)} estimated output · '
                     f'{format_bytes(volume.cache)} estimated raw cache')

    status = 'Needs attention' if blocked_parts or system_issues or warnings or not cache.ready else 'Ready'
    summary = f'{status} · {len(pending)} queued parts checked'
    if blocked_parts:
        summary += f' · {blocked_parts} with input or output problems'
    return [summary, *system_issues, *warnings, '',
            'Cache storage', cache.detail, '',
            'Estimated additional space by filesystem', *space,
            'Estimates cover parts with ready inputs, assume 130 words/minute and add 25% '
            'headroom. Existing WAVs and cache hits may reduce space needed. Space is not reserved.',
            '', 'Parts in queue order', *rows, '',
            'This report reflects the queue when the check started. Run it again after changes.']


def format_bytes(value):
    if value >= 1024 ** 3:
        return f'{value / 1024 ** 3:.2f} GiB'
    return f'{value / 1024 ** 2:.1f} MiB'


if __name__ == '__main__':
    request = json.load(sys.stdin)
    print(json.dumps(inspect_queue(request['jobs'], Path(request['cache_dir']))))
