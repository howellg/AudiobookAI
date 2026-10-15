"""Plan a complete audiobook export from the saved, narrator-specific queue.

This module has no Qt, Chatterbox, or GPU dependencies. It groups the user's
existing part jobs by the *actual chapter-WAV output folder*, then validates
that every intended EPUB section has a finished, indexed chapter recording.
The normal Studio converter does the final offline FFmpeg export.
"""

from __future__ import annotations

import json
from pathlib import Path


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
        folder = Path(folder_for_voice(Path(job['book_path']), Path(voice_path) if voice_path else None))
        key = str(folder.resolve())
        group = groups.setdefault(key, {
            'folder': folder, 'voice_path': voice_path,
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
