"""Crash-isolated MySQL mirror for AudiobookAI Studio.

Never import mysql.connector or its C extension in the Qt GUI process.
Uses a short-lived helper process and the existing MySQLMirror implementation.
SQLite remains the canonical source of truth.
"""

# MAINTAINER NOTE: GUI-side MySQL isolation layer. The native connector stays out of the Qt process, and metadata is sent to a worker over stdin.
from __future__ import annotations

import json
import subprocess
import sys
from datetime import datetime
from pathlib import Path

_WORKER = Path(__file__).with_name('audiobookai_mysql_worker.py')


class MySQLMirror:
    """GUI-compatible adapter matching the previous storage.MySQLMirror API."""
    def __init__(self, cfg, password):
        """Store mirror configuration without importing the native MySQL driver."""
        self.cfg = dict(cfg)
        self.password = password
        self.last_sync = None
        self.last_error = None
        self.dirty = True
        self._last_sync_succeeded = False

    def sync(self, store):
        # The GUI's SQLite connection produces a coherent snapshot on its own
        # thread. All MySQL operations are performed by a separate interpreter.
        """Send a consistent snapshot to the helper via stdin and translate errors."""
        snapshot = store.snapshot()
        # Send credentials over a child-process pipe, never shell arguments or disk files.
        payload = {'cfg': self.cfg, 'password': self.password,
                   'snapshot': snapshot}
        try:
            result = subprocess.run(
                [sys.executable, str(_WORKER)],
                input=json.dumps(payload, ensure_ascii=False),
                text=True, capture_output=True, timeout=30,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            self._last_sync_succeeded = False
            self.dirty = True
            self.last_error = 'MySQL worker timed out after 30 seconds'
            raise RuntimeError(self.last_error) from exc
        except OSError as exc:
            self._last_sync_succeeded = False
            self.dirty = True
            self.last_error = f'MySQL worker failed to start: {exc}'
            raise RuntimeError(self.last_error) from exc
        try:
            data = json.loads(result.stdout.strip())
        except (json.JSONDecodeError, ValueError):
            data = {}
        if result.returncode != 0 or not data.get('ok'):
            if result.returncode < 0:
                message = f'MySQL worker terminated by signal {-result.returncode}'
            else:
                message = data.get('error') or f'MySQL worker failed (exit {result.returncode})'
            # A native-extension crash in the child must not crash the GUI.
            if self.password:
                message = message.replace(self.password, '[redacted]')
            self._last_sync_succeeded = False
            self.dirty = True
            self.last_error = message[:500]
            raise RuntimeError(self.last_error)
        self._last_sync_succeeded = True
        self.dirty = False
        self.last_error = None
        self.last_sync = data.get('synced_at') or datetime.now().isoformat(timespec='seconds')
        return int(data['books']), int(data['jobs'])

    def connected(self):
        # A successful completed sync, not a persistent network connection.
        """Return the last known state of the helper-backed mirror."""
        return self._last_sync_succeeded

    def disconnect(self):
        """Forget connection state without affecting the canonical SQLite data."""
        self._last_sync_succeeded = False
