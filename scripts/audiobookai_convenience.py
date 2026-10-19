"""Pure helpers for AudiobookAI Studio GUI convenience controls.

Does not import Qt, the speech engine, a database driver, or perform I/O.
"""

# MAINTAINER NOTE: Pure helper functions for selection ranges, queue status filters, and audiobook duration estimates. No Qt or TTS imports are needed.
from __future__ import annotations

import re


_RANGE_PART = re.compile(r'^\s*(\d+)\s*(?:-\s*(\d+)\s*)?$')


def parse_section_ranges(expression: str, available_indices) -> list[int]:
    """Parse `1-15, 18, 20-23`, returning unique EPUB indices in spine order.

    Strict input validation helps prevent accidentally queuing the wrong parts
    of a long audiobook. Sections are *EPUB indices*, not display row indices.
    """
    available = list(available_indices)
    allowed = set(available)
    if not expression.strip():
        raise ValueError('Enter section numbers, for example: 1-15, 18, 20-23.')
    selections = set()
    for raw in expression.split(','):
        match = _RANGE_PART.fullmatch(raw)
        if not match:
            raise ValueError(f'Invalid section range: {raw.strip() or "(empty)"}. Use 1-15, 18, 20-23.')
        start = int(match.group(1))
        end = int(match.group(2)) if match.group(2) is not None else start
        if start > end:
            raise ValueError(f'Invalid section range {start}-{end}: the first number must be smaller.')
        if end - start > 100_000:
            raise ValueError('Range is too large. Select a shorter range.')
        unknown = [number for number in range(start, end + 1) if number not in allowed]
        if unknown:
            preview = ', '.join(str(i) for i in unknown[:6])
            raise ValueError(f'Section(s) not found in this EPUB: {preview}.')
        selections.update(range(start, end + 1))
    return [index for index in available if index in selections]



def readable_m4b_basename(value: str, length: int = 130) -> str:
    """Keep readable spaces and punctuation while excluding unsafe path characters.

    Existing underscore-only job names pass through unchanged, so resumed
    pre-upgrade jobs keep their original verified output paths.
    """
    name = re.sub(r'[\x00-\x1f<>:"/\\|?*]', ' ', str(value))
    name = re.sub(r'\s+', ' ', name).strip(' .')
    return name[:length].rstrip(' .') or 'audiobook'


def part_export_basename(title: str, part: int, indices, occupied=(), folder=None) -> str:
    """Generate a readable, non-overwriting basename for a newly queued M4B.

    Names are reserved against existing SQLite jobs and exported files; when
    the same selection is deliberately queued again, use a readable (2), (3)
    suffix instead of a random hash. Already queued jobs aren't renamed.
    """
    numbers = sorted(set(map(int, indices)))
    if not numbers:
        raise ValueError('Cannot name an empty chapter selection')
    span = str(numbers[0]) if len(numbers) == 1 else f'{numbers[0]}-{numbers[-1]}'
    # EPUB section indices are not necessarily numbered story chapters.
    if numbers[-1] - numbers[0] + 1 != len(numbers):
        span += f', {len(numbers)} selected'
    tail = f' - Part {int(part):02d} (Sections {span})'
    reserved = set(occupied)
    for copy in range(1, 10000):
        collision = '' if copy == 1 else f' ({copy})'
        prefix = readable_m4b_basename(title, max(1, 130 - len(tail) - len(collision)))
        candidate = prefix + tail + collision
        if candidate in reserved:
            continue
        if folder is not None and ((folder / (candidate + '.m4b')).exists()
                                   or (folder / (candidate + '.partial.m4b')).exists()):
            continue
        return candidate
    raise RuntimeError('Too many exports with the same title and sections')


def job_category(status: str) -> str:
    """A small stable set of queue filter groups."""
    if status in {'queued', 'running', 'stopping'}:
        return 'Pending / active'
    if status == 'complete':
        return 'Completed'
    return 'Needs attention'


def estimate_audio_duration(words: int, words_per_minute: int = 130) -> float:
    """Audio estimate in seconds (not compute/processing time)."""
    return max(0, words) * 60 / words_per_minute
