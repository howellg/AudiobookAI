"""Persistent, revision-aware QC review states. No audio edits or Qt dependency."""
from __future__ import annotations
import hashlib
import json
import os
from pathlib import Path
import tempfile
from datetime import datetime, timezone

STATE_FILE = 'review_state.json'
STATUSES = frozenset(('reviewed', 'dismissed'))
MAX_STATES = 20000


def issue_id(issue: dict) -> str:
    """Stable identity of the location and problem, not the message wording."""
    identifying = [int(issue['chapter']), issue.get('chunk'), issue['code'],
                   str(issue.get('path') or '')]
    return hashlib.sha256(json.dumps(identifying, separators=(',', ':')).encode()).hexdigest()[:32]


def issue_revision(issue: dict, folder: Path) -> str:
    """Issue status expires when the underlying chapter or flagged WAV changes."""
    chapter = int(issue['chapter'])
    candidates = [Path(folder) / 'chapters' / f'{chapter:04d}.wav']
    flagged = str(issue.get('path') or '')
    if flagged and flagged.endswith('.wav'):
        candidates.append(Path(flagged))
    revisions = []
    for path in candidates:
        try:
            st = path.stat()
            revisions.append([st.st_size, st.st_mtime_ns])
        except OSError:
            revisions.append(None)
    return hashlib.sha256(json.dumps(revisions).encode()).hexdigest()[:24]


def state_path(folder: Path) -> Path:
    return Path(folder) / 'quality_reports' / STATE_FILE


def load_states(folder: Path) -> dict:
    p = state_path(folder)
    if not p.is_file() or p.is_symlink():
        return {}
    try:
        data = json.loads(p.read_text(encoding='utf-8'))
        if data.get('version') != 1 or not isinstance(data.get('states'), dict):
            return {}
        return {k: v for k, v in data['states'].items()
                if isinstance(v, dict) and v.get('status') in STATUSES
                and isinstance(v.get('revision'), str)}
    except (OSError, ValueError, TypeError, AttributeError):
        return {}


def status_of(issue: dict, folder: Path, states: dict | None = None) -> str:
    states = load_states(folder) if states is None else states
    entry = states.get(issue_id(issue), {})
    if entry.get('revision') == issue_revision(issue, folder):
        return entry.get('status', 'needs_review')
    return 'needs_review'


def counts(report: dict, folder: Path) -> dict:
    states = load_states(folder)
    result = {'needs_review': 0, 'reviewed': 0, 'dismissed': 0,
              'errors_needing_review': 0, 'warnings_needing_review': 0}
    for issue in report.get('issues', []):
        status = status_of(issue, folder, states)
        result[status] += 1
        if status == 'needs_review':
            key = 'errors_needing_review' if issue.get('severity') == 'error' else 'warnings_needing_review'
            result[key] += 1
    return result


def set_status(issue: dict, folder: Path, status: str) -> None:
    if status not in STATUSES and status != 'needs_review':
        raise ValueError('Invalid QC review status')
    folder = Path(folder)
    path = state_path(folder)
    if path.is_symlink():
        raise ValueError('QC review state must not be a symlink')
    states = load_states(folder)
    key = issue_id(issue)
    if status == 'needs_review':
        states.pop(key, None)
    else:
        states[key] = {'status': status,
                       'revision': issue_revision(issue, folder),
                       'updated_utc': datetime.now(timezone.utc).isoformat()}
    if len(states) > MAX_STATES:
        # Prefer retaining recent decisions; bound metadata even after years.
        states = dict(sorted(states.items(), key=lambda kv: kv[1].get('updated_utc',''),
                             reverse=True)[:MAX_STATES])
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix='.qc-review-', suffix='.json', dir=path.parent)
    try:
        with os.fdopen(fd,'w',encoding='utf-8') as stream:
            json.dump({'version': 1, 'states': states}, stream, indent=2)
        if path.is_symlink():
            raise ValueError('QC review state became a symlink')
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)
