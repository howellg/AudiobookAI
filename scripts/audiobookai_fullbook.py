"""Plan a complete audiobook export from the saved, narrator-specific queue.

This module has no Qt, Chatterbox, or GPU dependencies. It groups the user's
existing part jobs by the *actual chapter-WAV output folder*, then validates
that every intended EPUB section has a finished, indexed chapter recording.
The normal Studio converter does the final offline FFmpeg export.
"""

from __future__ import annotations

import json
from pathlib import Path



def compact_section_ranges(indices, limit=12):
    """Summarize numbered EPUB sections without building an unbounded dialog."""
    numbers = sorted(set(int(i) for i in indices))
    spans = []
    for number in numbers:
        if spans and number == spans[-1][1] + 1:
            spans[-1][1] = number
        else:
            spans.append([number, number])
    parts = [str(first) if first == last else f'{first}–{last}'
             for first, last in spans[:limit]]
    if len(spans) > limit:
        parts.append(f'… +{len(spans) - limit} more ranges')
    return ', '.join(parts) if parts else 'none'


def export_coverage(indices, total_sections):
    """Audit queued EPUB coverage without assuming front/back matter is narration.

    Gaps *inside* the selected range are especially likely to be accidental.
    Outer exclusions may be intentional, but must be plainly disclosed.
    This is advisory; verified finished selections may still be exported.
    """
    count = int(total_sections)
    selected = sorted(set(int(i) for i in indices))
    if count < 1 or not selected or selected[0] < 1 or selected[-1] > count:
        raise ValueError('Cannot audit coverage with missing/out-of-range EPUB sections')
    chosen = set(selected)
    return {
        'interior': [i for i in range(selected[0], selected[-1] + 1) if i not in chosen],
        'before': list(range(1, selected[0])),
        'after': list(range(selected[-1] + 1, count + 1)),
        'selected': len(selected),
        'total': count,
    }


def export_coverage_message(indices, total_sections):
    """A readable, bounded summary for both complete-book export workflows."""
    coverage = export_coverage(indices, total_sections)
    interior = coverage['interior']
    outside = coverage['before'] + coverage['after']
    if interior:
        lines = [
            f'WARNING: {len(interior)} unselected EPUB section(s) INSIDE the '
            f'export range: {compact_section_ranges(interior)}.',
            'Check that no story chapters were accidentally skipped.',
        ]
    else:
        lines = ['Coverage: no gaps between the first and last selected sections.']
    if outside:
        lines.append(f'{len(outside)} excluded outside the selected range: '
                     f'{compact_section_ranges(outside)} (may be front/back matter).')
    return '\n'.join(lines)


def collect_export_groups(jobs, book_id, folder_for_voice):
    """Group all of a book's queue parts by their narrator's output directory.

    ``folder_for_voice(book_path, voice_path)`` must use the same folder
    resolution as the converter. This matters because different narrator WAVs
    may have the same human-readable name but must never be combined.

    Entries may be sqlite3.Row or dict. A pending/failed part remains in the
    group's plan so the GUI cannot silently produce a truncated 'complete' book.
    """
    groups = {}
    for original in jobs:
        job = dict(original)
        if int(job['book_id']) != int(book_id):
            continue
        voice_path = job.get('voice_path') or None
        acting_mode = job.get('acting_mode') or 'standard'
        voice = Path(voice_path) if voice_path else None
        if acting_mode == 'standard':
            folder = Path(folder_for_voice(Path(job['book_path']), voice))
        elif acting_mode == 'expressive':
            folder = Path(folder_for_voice(Path(job['book_path']), voice, acting_mode))
        else:
            raise ValueError(f"Invalid acting mode in queue job: {acting_mode!r}")
        key = str(folder.resolve())
        group = groups.setdefault(key, {
            'folder': folder, 'voice_path': voice_path, 'acting_mode': acting_mode,
            'parts': [], 'unfinished': [], 'indices': set(),
        })
        # A missing narrator sample is reported separately during preflight.
        # Grouping by the resolved folder permits content-identical voice WAVs
        # to share audio while preserving the first usable snapshot reference.
        if voice_path and (not group['voice_path'] or not Path(group['voice_path']).is_file()):
            group['voice_path'] = voice_path
        group['parts'].append(job)
        if job['status'] != 'complete':
            group['unfinished'].append(job)
        try:
            indices = json.loads(job['indices'])
        except (ValueError, TypeError) as exc:
            raise ValueError(f"Job #{job['id']} has an invalid chapter selection") from exc
        if not isinstance(indices, list) or not indices or any(
            not isinstance(i, int) or isinstance(i, bool) or i < 1 for i in indices
        ):
            raise ValueError(f"Job #{job['id']} has an invalid chapter selection")
        group['indices'].update(indices)
    return [dict(group, indices=sorted(group['indices'])) for group in groups.values()]


def recorded_export_indices(group, total_sections):
    """Select every indexed chapter WAV for one narrator, independent of queue history.

    A regenerated chapter may exist in the narrator's manifest even if its
    original queue part was deleted or completed under another voice. Never
    merge folders across narrators, or silently ignore broken manifest entries.
    The ordinary export verification still checks completion state, WAVs,
    sample rates, and per-chapter sample counts before exposing this option.
    """
    folder = group['folder']
    try:
        manifest = json.loads((folder / 'manifest.json').read_text(encoding='utf-8'))
    except (OSError, ValueError) as exc:
        raise ValueError('The chapter manifest is missing or unreadable.') from exc
    recorded = manifest.get('chapters')
    if not isinstance(recorded, dict) or not recorded:
        raise ValueError('No recorded chapter entries exist for this narrator.')
    indices = []
    for key in recorded:
        if not isinstance(key, str) or not key.isdecimal() or str(int(key)) != key:
            raise ValueError(f'Invalid chapter number in the manifest: {key!r}')
        index = int(key)
        if index < 1 or index > total_sections:
            raise ValueError(f'Manifest chapter {index} is outside this EPUB (1–{total_sections}).')
        indices.append(index)
    candidate = dict(group, indices=sorted(indices))
    return verify_export_group(candidate, total_sections)


def verify_export_group(group, total_sections):
    """Return the exact ordered chapter numbers eligible for offline export.

    A complete book means *every section assigned to its queued parts* has
    finished. Deliberately unselected EPUB front/back matter is not required.
    Unlike raw speech cache files, these assembled chapter WAVs are the inputs
    used by the normal M4B exporter (including non-destructive chunk edits).
    """
    if group['unfinished']:
        examples = ', '.join(f"#{j['id']} ({j['status']})" for j in group['unfinished'][:5])
        raise ValueError(f"{len(group['unfinished'])} part(s) are not complete: {examples}. Finish or remove them first.")
    selected = group['indices']
    if not selected:
        raise ValueError('No queued sections exist for this narrator.')
    if selected[-1] > total_sections:
        raise ValueError('One or more queued sections are outside the current EPUB. Reimport the book.')
    voice = group['voice_path']
    if voice and not Path(voice).is_file():
        raise ValueError(f'The narrator snapshot is missing: {voice}')
    folder = group['folder']
    try:
        manifest = json.loads((folder / 'manifest.json').read_text(encoding='utf-8'))
    except (OSError, ValueError) as exc:
        raise ValueError('The chapter manifest is missing or unreadable. Rebuild the missing chapters first.') from exc
    recorded = manifest.get('chapters', {})
    missing = [i for i in selected if str(i) not in recorded or
               not (folder / 'chapters' / f'{i:04d}.wav').is_file()]
    if missing:
        shown = ', '.join(str(i) for i in missing[:15])
        raise ValueError(f'{len(missing)} rendered chapter WAV(s) are missing: {shown}. Rebuild them before exporting.')
    rates = {recorded[str(i)].get('sample_rate') for i in selected}
    if len(rates) != 1 or None in rates:
        raise ValueError('Recorded chapters have incompatible sample rates.')
    if any(not isinstance(recorded[str(i)].get('samples'), int) or
           recorded[str(i)]['samples'] <= 0 for i in selected):
        raise ValueError('One or more chapters have invalid sample counts in the manifest.')
    return selected
