"""Regression test: ensure MySQL INSERT placeholders match every supplied row.

Uses an in-memory fake MySQL connection, so no server credentials are required.
"""
from __future__ import annotations

import sys
import tempfile
import unittest
import json
import io
from unittest.mock import patch
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))

from audiobookai_storage import Store, MySQLMirror
import audiobookai_mysql_worker as worker


class CheckingCursor:
    """SQL spy that checks parameter binding counts before recording statements."""
    def __init__(self, calls):
        """Initialize this isolated test double with its minimal in-memory state."""
        self.calls = calls

    def execute(self, sql, args=None):
        """Record a SQL statement instead of sending it to a real server."""
        if args is not None:
            self.assert_bindings(sql, args)
        self.calls.append((sql, args))

    def executemany(self, sql, rows):
        """Record all SQL argument tuples to verify batch inserts."""
        rows = list(rows)
        for row in rows:
            self.assert_bindings(sql, row)
        self.calls.append((sql, rows))

    @staticmethod
    def assert_bindings(sql, row):
        """Reject SQL executions whose placeholder count differs from the tuple length."""
        expected = sql.count('%s')
        actual = len(row)
        if expected != actual:
            raise AssertionError(f'MySQL query has {expected} placeholders for {actual} values: {sql[:90]}')

    def close(self):
        """Simulate closing a database cursor or connection."""
        pass


class FakeConnection:
    """Fake transaction-capable connection for MySQL mirror tests."""
    def __init__(self):
        """Initialize this isolated test double with its minimal in-memory state."""
        self.calls = []
        self.commits = 0
        self.rollbacks = 0

    def cursor(self):
        """Return a recording cursor attached to the fake connection."""
        return CheckingCursor(self.calls)

    def is_connected(self):
        """Report whether this fake MySQL connection is still open."""
        return True

    def commit(self):
        """Count transaction commits without writing to a real database."""
        self.commits += 1

    def rollback(self):
        """Count transaction rollbacks for failure-path assertions."""
        self.rollbacks += 1


class MySQLSyncParameterTests(unittest.TestCase):
    """Guard against the historical MySQL job INSERT placeholder mismatch."""
    def test_mysql_snapshot_with_a_narrator_job(self):
        """Check that a narrator-bearing job inserts all columns including queue position."""
        with tempfile.TemporaryDirectory() as tmp:
            db = Store(Path(tmp) / 'studio.sqlite3')
            try:
                sec = SimpleNamespace(index=3, title='Chapter 1', text='Once upon a time.', source='test.xhtml')
                book_id = db.import_book(Path(tmp) / 'book.epub', 'Test book', 'Author', [sec])
                db.add_job(book_id, 'Part 01', [3], 'test.m4b', voice_path='/tmp/voice.wav')
                fake_connection = FakeConnection()
                mirror = MySQLMirror({'host': '127.0.0.1', 'port': 3306, 'database': 'audiobookai', 'user': 'root'}, '')
                mirror.connection = fake_connection
                self.assertEqual(mirror.sync(db), (1, 1))
                self.assertEqual(fake_connection.commits, 1)
                jobs_inserts = [(sql, rows) for sql, rows in fake_connection.calls
                                if sql.lstrip().startswith('INSERT INTO jobs')]
                self.assertEqual(len(jobs_inserts), 1)
                self.assertEqual(jobs_inserts[0][0].count('%s'), 19)
                self.assertEqual(len(jobs_inserts[0][1][0]), 19)
                self.assertIn('queue_position', jobs_inserts[0][0])
                self.assertEqual(jobs_inserts[0][1][0][-1], 1)
            finally:
                db.close()


    def test_worker_rejects_snapshot_missing_completed_parts(self):
        payload = {'password': '', 'cfg': {}, 'snapshot': {
            'books': [], 'book_sections': [], 'jobs': []}}
        output = io.StringIO()
        with patch.object(worker.sys, 'stdin', io.StringIO(json.dumps(payload))), \
             patch.object(worker.sys, 'stdout', output), \
             patch.object(worker, 'MySQLMirror') as mirror:
            result = worker.main()
        self.assertEqual(result, 1)
        self.assertIn('Invalid snapshot: completed_parts', json.loads(output.getvalue())['error'])
        mirror.assert_not_called()

    def test_worker_accepts_complete_empty_snapshot(self):
        payload = {'password': '', 'cfg': {}, 'snapshot': {
            'books': [], 'book_sections': [], 'jobs': [], 'completed_parts': []}}
        output = io.StringIO()
        with patch.object(worker.sys, 'stdin', io.StringIO(json.dumps(payload))), \
             patch.object(worker.sys, 'stdout', output), \
             patch.object(worker, 'MySQLMirror') as mirror:
            mirror.return_value.sync.return_value = (0, 0)
            result = worker.main()
        self.assertEqual(result, 0)
        self.assertTrue(json.loads(output.getvalue())['ok'])
        mirror.return_value.disconnect.assert_called_once()


if __name__ == '__main__':
    unittest.main()
