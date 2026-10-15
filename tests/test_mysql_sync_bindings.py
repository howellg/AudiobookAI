"""Regression test: ensure MySQL INSERT placeholders match every supplied row.

Uses an in-memory fake MySQL connection, so no server credentials are required.
"""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))

from audiobookai_storage import Store, MySQLMirror


class CheckingCursor:
    def __init__(self, calls):
        self.calls = calls

    def execute(self, sql, args=None):
        if args is not None:
            self.assert_bindings(sql, args)
        self.calls.append((sql, args))

    def executemany(self, sql, rows):
        rows = list(rows)
        for row in rows:
            self.assert_bindings(sql, row)
        self.calls.append((sql, rows))

    @staticmethod
    def assert_bindings(sql, row):
        expected = sql.count('%s')
        actual = len(row)
        if expected != actual:
            raise AssertionError(f'MySQL query has {expected} placeholders for {actual} values: {sql[:90]}')

    def close(self):
        pass


class FakeConnection:
    def __init__(self):
        self.calls = []
        self.commits = 0
        self.rollbacks = 0

    def cursor(self):
        return CheckingCursor(self.calls)

    def is_connected(self):
        return True

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1


class MySQLSyncParameterTests(unittest.TestCase):
    def test_mysql_snapshot_with_a_narrator_job(self):
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
                self.assertEqual(jobs_inserts[0][0].count('%s'), 17)
                self.assertEqual(len(jobs_inserts[0][1][0]), 17)
            finally:
                db.close()


if __name__ == '__main__':
    unittest.main()
