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


def part_number_from_name(name: str) -> int | None:
    """Read a part sequence from current and legacy export names.

    New: `Book - Part(12)`. Older names kept their part number before the
    EPUB section span, sometimes with an identifying hex suffix.
    """
    import re

    stem = str(name)
    if stem.lower().endswith('.m4b'):
        stem = stem[:-4]
    patterns = (
        r' - Part\((\d+)\)(?: \(\d+\))?$',
        r' - Part (\d+)(?: \(Sections [^)]+\))?(?: \(\d+\))?$',
        r'[_ -]part_(\d+)_sections_\d+(?:-\d+)?(?:_[a-f0-9]{8,})?$',
    )
    for pattern in patterns:
        match = re.search(pattern, stem, re.I)
        if match:
            number = int(match.group(1))
            return number if number > 0 else None
    return None


def legacy_part_selection(name: str) -> tuple[int, list[int]] | None:
    """Recover exact contiguous section ranges from OLD part filenames only.

    New names have no section span; their selections come from SQLite.
    Ambiguous old noncontiguous selections are skipped.
    """
    stem = str(name)
    if stem.lower().endswith('.m4b'):
        stem = stem[:-4]
    patterns = (
        r' - Part (\d+) \(Sections (\d+)(?:-(\d+))?\)(?: \(\d+\))?$',
        r'_part_(\d+)_sections_(\d+)(?:-(\d+))?_[a-f0-9]{8,}$',
    )
    for expression in patterns:
        match = re.search(expression, stem, re.I)
        if match:
            part, first, last = (int(match.group(1)), int(match.group(2)),
                                 int(match.group(3) or match.group(2)))
            if part > 0 and 1 <= first <= last and last - first <= 10000:
                return part, list(range(first, last + 1))
    return None


def part_export_basename(title: str, part: int, indices, occupied=(), folder=None) -> str:
    """Create 'Book Name - Part(n)' without replacing existing files or jobs.

    EPUB indices remain in SQLite, while old queued jobs keep their original
    output names. A duplicate uses (2), (3), ... instead of overwriting.
    """
    if not list(indices):
        raise ValueError('Cannot name an empty chapter selection')
    if int(part) < 1:
        raise ValueError('Part number must be positive')
    tail = f' - Part({int(part)})'
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
    raise RuntimeError('Too many exports with the same title and part number')


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
