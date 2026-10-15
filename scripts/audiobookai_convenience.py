"""Pure helpers for AudiobookAI Studio GUI convenience controls.

Does not import Qt, the speech engine, a database driver, or perform I/O.
"""
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
