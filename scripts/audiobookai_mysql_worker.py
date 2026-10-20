"""AudiobookAI MySQL-only subprocess. Does not import Qt, torch, or Chatterbox."""

# MAINTAINER NOTE: One-shot MySQL synchronization worker. It reads metadata from stdin, updates MySQL, and prints a small machine-readable result.
from __future__ import annotations

import json
import sys
from datetime import datetime

from audiobookai_storage import MySQLMirror


class SnapshotStore:
    """Tiny adapter exposing a precomputed snapshot to the SQL mirror."""
    def __init__(self, snapshot):
        """Hold the snapshot received from the GUI proxy."""
        self._snapshot = snapshot

    def snapshot(self):
        """Return the input snapshot without querying SQLite again."""
        return self._snapshot


def main():
    """Read one request from stdin, synchronize MySQL, and report success or failure."""
    password = None
    try:
        req = json.load(sys.stdin)
        password = req['password']
        snapshot = req['snapshot']
        # Validate the entire snapshot before opening a MySQL connection.
        # A malformed payload must not reach the replace-all mirror operation.
        if not isinstance(snapshot, dict):
            raise ValueError('Invalid snapshot: expected a dictionary')
        for key in ('books', 'book_sections', 'jobs', 'completed_parts'):
            if not isinstance(snapshot.get(key), list):
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
