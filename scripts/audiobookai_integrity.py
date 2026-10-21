"""Read-only book/narrator completion inventory for the Studio dashboard.

This deliberately does not infer that EPUB front/back matter is narration.
Recorded WAVs, queue plans, completion receipts, QC and M4B files remain
separate signals; none implies another has succeeded.
"""
from __future__ import annotations

import json
from pathlib import Path


def _numbers(raw):
    try:
        values = json.loads(raw) if isinstance(raw, str) else raw
        if not isinstance(values, list):
            return set()
        return {n for n in values if type(n) is int and n > 0}
    except (ValueError, TypeError):
        return set()


def inspect_book(folder, total, jobs=(), receipts=(), *, expected=None):
    """Return lightweight, file-existence-based integrity metrics.

    Does not decode WAVs or assume that matching M4B chapter counts guarantee
    identical audio; the deep Quality Control scan handles signal integrity.
    """
    folder = Path(folder)
    total = int(total)
    if total < 1:
        raise ValueError('The EPUB has no indexed sections.')
    manifest_path = folder / 'manifest.json'
    manifest_error = ''
    if manifest_path.is_file():
        try:
            manifest = json.loads(manifest_path.read_text(encoding='utf-8'))
            recorded = manifest.get('chapters', {})
            if not isinstance(recorded, dict):
                raise ValueError('Invalid chapters dictionary')
        except (OSError, ValueError, TypeError) as exc:
            recorded = {}
            manifest_error = str(exc)
    else:
        recorded = {}
        manifest_error = 'No chapter manifest found'
    jobs = [dict(j) for j in jobs]
    receipts = [dict(r) for r in receipts]
    planned = set().union(*(_numbers(j.get('indices')) for j in jobs), *(_numbers(r.get('indices')) for r in receipts))
    valid_keys = {int(k) for k in recorded if str(k).isdecimal() and str(int(k)) == str(k)
                  and 1 <= int(k) <= total}
    if expected is None:
        anchors = sorted((planned | valid_keys) & set(range(1, total + 1)))
        expected_set = set(range(anchors[0], anchors[-1] + 1)) if anchors else set()
    else:
        expected_set = {i for i in expected if type(i) is int and 1 <= i <= total}
    queued = set()
    for job in jobs:
        if job.get('status') != 'complete':
            queued.update(_numbers(job.get('indices')))
    receipt_ids = set().union(*(_numbers(r.get('indices')) for r in receipts))
    verified_receipts = [r for r in receipts
                         if r.get('output_path') and Path(r['output_path']).is_file()
                         and Path(r['output_path']).stat().st_size > 1024]
    rows = []
    for i in sorted(expected_set):
        entry = recorded.get(str(i))
        path = folder / 'chapters' / f'{i:04d}.wav'
        if entry is not None:
            valid_meta = isinstance(entry, dict) and type(entry.get('samples')) is int and entry['samples'] > 0 and type(entry.get('sample_rate')) is int and entry['sample_rate'] > 0
            state = 'Recorded' if path.is_file() and path.stat().st_size > 44 and valid_meta else 'Damaged / missing'
        elif i in queued:
            state = 'Queued / unfinished'
        elif i in planned or i in receipt_ids:
            state = 'Missing audio'
        else:
            state = 'Not started'
        rows.append({'section': i, 'state': state, 'path': path})
    good = [r['section'] for r in rows if r['state'] == 'Recorded']
    bad = [r['section'] for r in rows if r['state'] in ('Damaged / missing', 'Missing audio')]
    queued_indices = [r['section'] for r in rows if r['state'] == 'Queued / unfinished']
    untouched = [r['section'] for r in rows if r['state'] == 'Not started']
    outside = sorted(set(range(1, total + 1)) - expected_set)
    wav_ok = bool(rows) and len(good) == len(rows)
    rate_values = {recorded[str(i)]['sample_rate'] for i in good}
    compatible = len(rate_values) == 1
    qc_path = folder / 'quality_reports' / 'summary.json'
    qc = 'Not scanned'
    if qc_path.is_file() and good:
        try:
            report = json.loads(qc_path.read_text(encoding='utf-8'))
            scanned = set(report['chapters'])
            if not expected_set.issubset(scanned):
                qc = 'Incomplete scan'
            elif any((folder / 'chapters' / f'{i:04d}.wav').stat().st_mtime > float(report['timestamp']) for i in expected_set):
                qc = 'Stale scan'
            else:
                issues = [issue for issue in report['issues'] if issue.get('chapter') in expected_set]
                errors = sum(issue.get('severity') == 'error' for issue in issues)
                warnings = sum(issue.get('severity') == 'warning' for issue in issues)
                qc = f'{errors} errors, {warnings} warnings' if errors or warnings else 'Passed'
        except (OSError, ValueError, KeyError, TypeError):
            qc = 'Invalid report'
    completed_file = folder / 'manifest.json'
    full_exports = [p for p in folder.glob('*_COMPLETE.m4b') if p.is_file() and p.stat().st_size > 1024]
    return {
        'total': total, 'expected': sorted(expected_set), 'outside': outside,
        'rows': rows, 'recorded': good, 'missing': bad, 'queued': queued_indices,
        'not_started': untouched, 'manifest_error': manifest_error,
        'wav_ready': wav_ok and compatible and not manifest_error and not any(j.get('status') != 'complete' for j in jobs),
        'compatible': compatible, 'queue_parts': len(jobs),
        'finished_parts': sum(j.get('status') == 'complete' for j in jobs),
        'receipt_parts': len(receipts), 'verified_receipt_parts': len(verified_receipts),
        'qc': qc, 'full_exports': full_exports,
    }
