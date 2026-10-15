"""AudiobookAI MySQL-only subprocess. Does not import Qt, torch, or Chatterbox."""
from __future__ import annotations

import json
import sys
from datetime import datetime

from audiobookai_storage import MySQLMirror


class SnapshotStore:
    def __init__(self, snapshot):
        self._snapshot = snapshot

    def snapshot(self):
        return self._snapshot


def main():
    password = None
    try:
        req = json.load(sys.stdin)
        password = req['password']
        snapshot = req['snapshot']
        for key in ('books', 'book_sections', 'jobs'):
            if not isinstance(snapshot[key], list):
                raise ValueError(f'Invalid snapshot: {key}')
        mirror = MySQLMirror(req['cfg'], password)
        try:
            books, jobs = mirror.sync(SnapshotStore(snapshot))
        finally:
            mirror.disconnect()
        print(json.dumps({'ok': True, 'books': books, 'jobs': jobs,
                          'synced_at': datetime.now().isoformat(timespec='seconds')}), flush=True)
        return 0
    except Exception as exc:
        error = f'{type(exc).__name__}: {exc}'
        if password:
            error = error.replace(str(password), '[redacted]')
        print(json.dumps({'ok': False, 'error': error[:500]}), flush=True)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
