#!/usr/bin/env python3
"""Read-only, offline WAV quality audit for AudiobookAI Studio.

The worker never imports Chatterbox, PySide6, or MySQL and never regenerates or
modifies audio. It streams chapter WAVs in bounded blocks, then optionally
inspects the original (or currently selected replacement) speech chunks.
Reports are written atomically beside the book's existing chapter manifest.

Run directly to inspect a narrator's output folder:
    python scripts/audiobookai_quality.py --folder output/audiobooks/BOOK \
        --chapters 3,4,5 --required 3,4,5 --deep
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sys
import time

import numpy as np
import soundfile as sf

EVENT_PREFIX = 'AUDIOBOOKAI_QC_EVENT '
REPORT_NAME = 'summary.json'
SILENCE_AMPLITUDE = 0.002
LONG_SILENCE_SECONDS = 4.0
BLOCK_FRAMES = 262144


def parse_chapters(text):
    """Validate a comma-delimited set of positive EPUB section numbers."""
    if not text:
        return []
    values = set()
    for part in text.split(','):
        part = part.strip()
        if not part.isdecimal() or int(part) <= 0:
            raise ValueError(f'Invalid EPUB chapter number: {part!r}')
        values.add(int(part))
    return sorted(values)


def issue(severity, chapter, code, message, path=None, chunk=None, seconds=None):
    """Make a stable, JSON-serializable report entry for the GUI."""
    return {'severity': severity, 'chapter': int(chapter), 'chunk': chunk,
            'code': code, 'message': message,
            'path': str(path) if path is not None else '', 'seconds': seconds}


def analyze_wav(path, *, words=0, check_pauses=True):
    """Stream a WAV with bounded memory and return objective signal metrics.

    Silence detection carries open runs between blocks. Only *interior* pauses
    trigger the four-second review flag; opening and closing silence is reported
    separately because it is often intentional in an audiobook.
    """
    peak = square_sum = clipping = active = frames_read = 0
    longest_pause = (0, 0)  # (starting sample, number of samples)
    run_start = None
    current_offset = 0
    pauses = []
    with sf.SoundFile(str(path)) as audio:
        sample_rate = audio.samplerate
        channels = audio.channels
        declared_frames = audio.frames
        if sample_rate <= 0 or channels <= 0:
            raise ValueError('WAV has an invalid sample rate or channel count')
        for block in audio.blocks(blocksize=BLOCK_FRAMES, dtype='float32', always_2d=True):
            if not np.isfinite(block).all():
                raise ValueError('WAV has non-finite samples')
            count = len(block)
            if not count:
                continue
            amplitude = np.abs(block)
            peak = max(peak, float(amplitude.max()))
            square_sum += float(np.sum(block.astype(np.float64) ** 2))
            clipping += int(np.count_nonzero(amplitude >= 0.995))
            active += int(np.count_nonzero(np.max(amplitude, axis=1) > SILENCE_AMPLITUDE))
            frames_read += count

            if check_pauses:
                # Find *runs*, not individual zero samples. Every block uses
                # vectorized transitions, avoiding a Python loop per sample.
                silent = np.max(amplitude, axis=1) <= SILENCE_AMPLITUDE
                padded = np.empty(count + 2, dtype=np.bool_)
                padded[0] = (run_start is not None)
                padded[1:-1] = silent
                padded[-1] = False
                changes = np.flatnonzero(padded[1:] != padded[:-1])
                for local in changes:
                    absolute = current_offset + int(local)
                    if padded[local + 1]:
                        # A new run starts, or continues from the previous block.
                        if run_start is None:
                            run_start = absolute
                    elif run_start is not None:
                        # The last transition is artificial when the block ends
                        # in silence. Keep the run open until a real sound arrives.
                        if local == count and silent[-1]:
                            continue
                        duration = absolute - run_start
                        if duration > longest_pause[1]:
                            longest_pause = (run_start, duration)
                        if duration >= LONG_SILENCE_SECONDS * sample_rate:
                            pauses.append((run_start, duration))
                        run_start = None
            current_offset += count
        if run_start is not None:
            duration = frames_read - run_start
            if duration > longest_pause[1]:
                longest_pause = (run_start, duration)
            if duration >= LONG_SILENCE_SECONDS * sample_rate:
                pauses.append((run_start, duration))
    if frames_read == 0:
        raise ValueError('WAV contains no audio frames')
    total_values = frames_read * channels
    duration = frames_read / sample_rate
    rms = (square_sum / total_values) ** 0.5
    # Track longest interior pauses only; avoid false positives for intentional
    # silence at the beginning or end of a book/chapter.
    interior = sorted(((start, length) for start, length in pauses
                       if start > 0 and start + length < frames_read),
                      key=lambda x: x[1], reverse=True)
    return {'sample_rate': sample_rate, 'channels': channels,
            'frames': frames_read, 'declared_frames': declared_frames,
            'duration': round(duration, 3), 'peak': round(peak, 5),
            'rms': round(rms, 6), 'clipped_fraction': clipping / total_values,
            'active_fraction': active / frames_read,
            'interior_pauses': [{'seconds': round(start / sample_rate, 2),
                                 'length': round(length / sample_rate, 2)}
                                for start, length in interior[:3]],
            'words': words}


def signal_issues(stat, chapter, *, path, chunk=None, words=0, check_pauses=True):
    """Translate waveform metrics to hard failures and softer review warnings."""
    found = []
    add = lambda severity, code, msg, seconds=None: found.append(
        issue(severity, chapter, code, msg, path, chunk, seconds))
    if stat['rms'] < 0.0005 or stat['active_fraction'] < 0.01:
        add('error', 'silent', 'Silent or nearly silent recording')
    if stat['duration'] < 0.20:
        add('error', 'too_short', 'Recording is shorter than 0.2 seconds')
    if words >= 3 and stat['duration'] < max(0.2, words * 0.065):
        add('error', 'short_for_text', f'{stat["duration"]:.1f}s seems too short for {words} spoken words')
    if words and stat['duration'] > max(24, words * 3.2):
        add('warning', 'long_for_text', f'{stat["duration"]:.1f}s seems long for {words} spoken words')
    if stat['clipped_fraction'] > 0.015:
        add('warning', 'clipping', f'{stat["clipped_fraction"]:.1%} of audio samples near clipping')
    if check_pauses:
        for pause in stat['interior_pauses']:
            add('warning', 'long_silence',
                f'Possible {pause["length"]:.1f}s silent gap at {format_time(pause["seconds"])}',
                pause['seconds'])
    return found


def format_time(seconds):
    """Display a chapter-local timestamp in HH:MM:SS."""
    seconds = max(0, int(seconds))
    return f'{seconds // 3600:02d}:{seconds // 60 % 60:02d}:{seconds % 60:02d}'


def _safe_chunk_source(output_folder, entry, chapter_number, cache_root):
    """Resolve an active edited take or safe raw cache reference.

    Report JSON is input data, not permission to open an arbitrary filesystem
    path. Edited takes must remain under the narrator output folder, raw audio
    under AudiobookAI's raw-cache folder.
    """
    chunk_no = int(entry['number'])
    edits_file = output_folder / 'chunk_edits' / f'{chapter_number:04d}.json'
    if edits_file.is_file():
        edits = json.loads(edits_file.read_text(encoding='utf-8')).get('edits', {})
        selected = edits.get(str(chunk_no), {})
        if selected.get('original_text') == entry.get('text'):
            active_id = selected.get('active')
            for take in selected.get('takes', []):
                if take.get('id') == active_id and active_id is not None:
                    p = (output_folder / take['wav']).resolve()
                    if not p.is_relative_to(output_folder.resolve()) or p.suffix.lower() != '.wav':
                        raise ValueError('Unsafe edited-take WAV path')
                    return p, take.get('text') or entry.get('text') or '', True
    raw = Path(entry['cache_file']).resolve()
    if not raw.is_relative_to(cache_root.resolve()) or raw.suffix.lower() != '.wav':
        raise ValueError('Unsafe raw-cache WAV path')
    return raw, entry.get('text') or '', False


def scan_book(folder, *, chapters=None, required=None, deep=False, cache_root=None, progress=None,
              save=True):
    """Audit existing WAVs without producing or deleting speech.

    ``chapters`` identifies available or intended EPUB sections. ``required``
    marks sections that belong to completed jobs: a missing required section is
    an error, while absent sections from *unfinished* parts are not scanned.
    When ``deep`` is enabled, deleted raw caches are counted as unavailable,
    not classified as broken finished chapters.
    """
    folder = Path(folder).resolve()
    if not folder.is_dir():
        raise ValueError(f'Output directory not found: {folder}')
    manifest_path = folder / 'manifest.json'
    try:
        manifest = json.loads(manifest_path.read_text(encoding='utf-8'))
    except (ValueError, OSError) as exc:
        raise ValueError(f'Missing or unreadable chapter manifest: {manifest_path}') from exc
    recorded = manifest.get('chapters')
    if not isinstance(recorded, dict):
        raise ValueError('Manifest does not contain a valid chapters dictionary')
    available_keys = sorted(int(k) for k in recorded if str(k).isdecimal() and int(k) > 0)
    target = sorted(set(chapters if chapters is not None else available_keys))
    required_set = set(required if required is not None else target)
    target = sorted(set(target) | required_set)
    if not target:
        raise ValueError('There are no completed chapters available for a scan')
    cache_root = Path(cache_root).resolve() if cache_root else Path(__file__).resolve().parents[1] / 'output' / 'narration_cache_v2'
    issues = []
    metrics = {'chapters_scanned': 0, 'chunks_scanned': 0,
               'raw_chunks_unavailable': 0, 'duration_seconds': 0.0}
    for position, chapter in enumerate(target, 1):
        if progress:
            progress({'kind': 'progress', 'chapter': chapter, 'current': position,
                      'total': len(target), 'issues': len(issues)})
        entry = recorded.get(str(chapter), {})
        wav = folder / 'chapters' / f'{chapter:04d}.wav'
        if not isinstance(entry, dict) or not wav.is_file():
            if chapter in required_set:
                issues.append(issue('error', chapter, 'missing_chapter',
                                    'Completed job is missing its chapter WAV or manifest entry', wav))
            continue
        try:
            stats = analyze_wav(wav)
            metrics['chapters_scanned'] += 1
            metrics['duration_seconds'] += stats['duration']
            issues.extend(signal_issues(stats, chapter, path=wav))
            # The assembler stores exact frame count and rate for chapter markers.
            # A discrepancy means that exported chapter boundaries could drift.
            if stats['sample_rate'] != entry.get('sample_rate'):
                issues.append(issue('error', chapter, 'sample_rate_mismatch',
                                    'WAV sample rate differs from its export manifest', wav))
            declared = entry.get('samples')
            if isinstance(declared, int) and abs(stats['frames'] - declared) > stats['sample_rate'] * 0.25:
                issues.append(issue('error', chapter, 'sample_count_mismatch',
                                    'WAV duration differs from the stored chapter metadata', wav))
        except (OSError, ValueError, RuntimeError, sf.LibsndfileError) as exc:
            issues.append(issue('error', chapter, 'unreadable_chapter', f'Cannot inspect WAV: {exc}', wav))
            continue
        if not deep:
            continue
        index_path = folder / 'chunk_indexes' / f'{chapter:04d}.json'
        if not index_path.is_file():
            # Older narration runs may have chapter WAVs but no chunk index.
            metrics.setdefault('chapters_without_chunk_index', 0)
            metrics['chapters_without_chunk_index'] += 1
            continue
        try:
            index = json.loads(index_path.read_text(encoding='utf-8'))
            chunks = index['chunks']
            if not isinstance(chunks, list):
                raise ValueError('Invalid chunk list')
            if index.get('signature') != entry.get('signature'):
                # Stale indexes describe different speech and must not be used.
                issues.append(issue('warning', chapter, 'stale_chunk_index',
                                    'Chunk index signature differs from the assembled WAV', index_path))
                continue
        except (OSError, ValueError, TypeError, KeyError) as exc:
            issues.append(issue('warning', chapter, 'unreadable_index', f'Cannot read chunk index: {exc}', index_path))
            continue
        for chunk in chunks:
            try:
                path, text, edited = _safe_chunk_source(folder, chunk, chapter, cache_root)
                if not path.is_file():
                    # A finished chapter still plays after raw cache cleanup.
                    metrics['raw_chunks_unavailable'] += 1
                    if edited:
                        issues.append(issue('warning', chapter, 'missing_edited_take',
                                            'Active replacement take is missing', path, int(chunk['number'])))
                    continue
                stats = analyze_wav(path, words=len(text.split()), check_pauses=False)
                metrics['chunks_scanned'] += 1
                issues.extend(signal_issues(stats, chapter, path=path,
                                             chunk=int(chunk['number']), words=len(text.split()),
                                             check_pauses=False))
            except (OSError, ValueError, RuntimeError, KeyError, TypeError, sf.LibsndfileError) as exc:
                issues.append(issue('warning', chapter, 'chunk_check_failed',
                                    f'Chunk could not be checked: {exc}',
                                    chunk=int(chunk.get('number', 0))))
    issues.sort(key=lambda x: (0 if x['severity'] == 'error' else 1, x['chapter'], x['chunk'] or 0))
    now = time.time()
    report = {'version': 1, 'timestamp': now, 'scanned_at': datetime.fromtimestamp(now, timezone.utc).isoformat(),
              'folder': str(folder), 'chapters': target, 'required': sorted(required_set),
              'deep': bool(deep), 'metrics': metrics, 'issues': issues,
              'errors': sum(x['severity'] == 'error' for x in issues),
              'warnings': sum(x['severity'] == 'warning' for x in issues)}
    if save:
        report_path = folder / 'quality_reports' / REPORT_NAME
        report_path.parent.mkdir(parents=True, exist_ok=True)
        pending = report_path.with_name(f'.{REPORT_NAME}.{os.getpid()}.tmp')
        try:
            pending.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
            pending.replace(report_path)
        finally:
            pending.unlink(missing_ok=True)
    if progress:
        progress({'kind': 'done', 'errors': report['errors'], 'warnings': report['warnings'],
                  'report': str(folder / 'quality_reports' / REPORT_NAME)})
    return report


def quality_preflight(folder, chapters):
    """Summarize existing QC coverage for the Complete M4B confirmation dialog.

    The check is advisory rather than destructive. A change to any chapter WAV
    after the scan makes that coverage stale. Files are never modified here.
    """
    folder = Path(folder).resolve()
    report_path = folder / 'quality_reports' / REPORT_NAME
    if not report_path.is_file():
        return 'No quality-control scan yet. Use Quality Control before archival export.'
    try:
        report = json.loads(report_path.read_text(encoding='utf-8'))
        requested = set(chapters)
        if not requested.issubset(set(report['chapters'])):
            return 'Quality-control scan has not covered all selected chapters.'
        scanned_time = float(report['timestamp'])
        if any((folder / 'chapters' / f'{idx:04d}.wav').stat().st_mtime > scanned_time
               for idx in chapters):
            return 'Quality-control results are stale: chapter audio changed after the scan.'
        errors = [x for x in report['issues'] if x['severity'] == 'error' and x['chapter'] in requested]
        warnings = [x for x in report['issues'] if x['severity'] == 'warning' and x['chapter'] in requested]
        if errors:
            return f'Quality control: {len(errors)} errors, {len(warnings)} warnings. Review before archiving.'
        return f'Quality control: {len(warnings)} warnings, no hard errors.'
    except (OSError, ValueError, KeyError, TypeError):
        return 'Quality-control results could not be verified; scan again.'


def emit(event):
    """Emit a machine-readable progress line for the independent PySide GUI."""
    print(EVENT_PREFIX + json.dumps(event, ensure_ascii=False), flush=True)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--folder', type=Path, required=True)
    parser.add_argument('--chapters', default='', help='EPUB indices to scan; default all manifest chapters')
    parser.add_argument('--required', default=None, help='Completed-job indices whose missing WAVs are errors')
    parser.add_argument('--deep', action='store_true', help='Also inspect available cached/edited speech chunks')
    args = parser.parse_args(argv)
    try:
        report = scan_book(args.folder, chapters=parse_chapters(args.chapters) if args.chapters else None,
                           required=parse_chapters(args.required) if args.required is not None else None,
                           deep=args.deep, progress=emit)
        print(f'Quality scan finished: {report["metrics"]["chapters_scanned"]} chapters, '
              f'{report["errors"]} errors, {report["warnings"]} warnings.', flush=True)
        return 0
    except (ValueError, OSError) as exc:
        print(f'Quality scan failed: {exc}', file=sys.stderr, flush=True)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
