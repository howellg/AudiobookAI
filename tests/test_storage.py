"""Run: python -m unittest discover -s tests -v
No PySide6, CUDA, MySQL server, or GPU needed for these tests.
"""
import json
import os
import sqlite3
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parents[1] / 'scripts'
sys.path.insert(0, str(SCRIPTS))
from audiobookai_storage import (Store, MySQLMirror, default_config, read_config,
                                 write_config)


class Section:
    """One ordered EPUB segment with source metadata and its extracted paragraphs."""
    def __init__(self, index, title, text, source='source.xhtml'):
        """Initialize this isolated test double with its minimal in-memory state."""
        self.index, self.title, self.text, self.source = index, title, text, source


class StoreTests(unittest.TestCase):
    """Regression cases for store behavior."""
    def setUp(self):
        """Prepare isolated fixtures and temporary data for each test."""
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / 'audiobookai_studio.sqlite3'

    def tearDown(self):
        """Release fixtures and temporary resources after each test."""
        self.tmp.cleanup()

    def test_existing_v2_migrates_without_losing_jobs(self):
        """Verify existing v2 migrates without losing jobs behavior."""
        old = sqlite3.connect(self.path)
        old.executescript('''
            CREATE TABLE books(id INTEGER PRIMARY KEY,path TEXT NOT NULL UNIQUE,
                title TEXT NOT NULL,author TEXT NOT NULL,sections INTEGER NOT NULL,
                words INTEGER NOT NULL,added TEXT NOT NULL);
            CREATE TABLE jobs(id INTEGER PRIMARY KEY,book_id INTEGER NOT NULL REFERENCES books(id),
                name TEXT NOT NULL,indices TEXT NOT NULL,output_name TEXT NOT NULL,
                status TEXT NOT NULL,added TEXT NOT NULL,finished TEXT,
                output_path TEXT,error TEXT DEFAULT '');
            INSERT INTO books VALUES(1,'/tmp/sample.epub','Example','Author',2,32,'2026-10-12 11:00:00');
            INSERT INTO jobs(id,book_id,name,indices,output_name,status,added)
                VALUES(1,1,'Part 01','[1,2]','old_export','running','2026-10-12 11:00:00');
        ''')
        old.close()
        store = Store(self.path)
        self.assertEqual(store.job(1)['status'], 'interrupted')
        self.assertEqual(store.job(1)['output_name'], 'old_export')
        self.assertEqual(store.job(1)['completed_chunks'], 0)
        store.update_progress(1, 500, 120, 1, 120, 300)
        self.assertEqual(store.job(1)['completed_chunks'], 120)
        store.close()
        # Re-running migration should not destroy data or columns.
        reopened = Store(self.path)
        self.assertEqual(reopened.job(1)['completed_chunks'], 120)
        self.assertEqual(reopened.job(1)['status'], 'interrupted')
        reopened.close()

    def test_import_backfill_queue_progress_and_deletion(self):
        """Verify import backfill queue progress and deletion behavior."""
        store = Store(self.path)
        sections = [Section(1,'Chapter I','Some initial content'),
                    Section(2,'Chapter II','More words in chapter two')]
        changes = []
        store.on_change = lambda: changes.append(True)
        book = store.import_book('/tmp/novel.epub','Novel','Writer',sections)
        self.assertEqual(len(store.snapshot()['book_sections']), 2)
        job = store.add_job(book,'Part 1',[1,2],'novel_part_1')
        store.update_job(job,'running')
        store.update_progress(job,150,29,1,29,80)
        self.assertEqual(store.job(job)['completed_chunks'],29)
        self.assertEqual(store.claimed_sections(book), {1,2})
        with self.assertRaises(ValueError): store.delete_book(book)
        store.update_job(job,'complete','/tmp/novel_part_1.m4b')
        self.assertEqual(store.job(job)['output_path'],'/tmp/novel_part_1.m4b')
        store.remove_job(job)
        store.delete_book(book)
        self.assertEqual(store.snapshot()['books'],[])
        self.assertEqual(store.snapshot()['book_sections'],[])
        self.assertGreater(len(changes),4)
        store.close()

    def test_deleted_job_does_not_reuse_immutable_pronunciation_snapshot(self):
        """Fix regression: deleted SQLite job #1 must not overwrite job_1.json."""
        from audiobookai_pronunciation import snapshot_for_job
        root = self.path.parent
        snapshot_dir = root / 'pronunciation' / 'jobs'
        snapshot_dir.mkdir(parents=True)
        old_snapshot = snapshot_dir / 'job_1.json'
        old_snapshot.write_text('{"version":1,"job_id":1,"rules":[]}',
                                encoding='utf-8')
        previous_bytes = old_snapshot.read_bytes()
        book_path = root / 'novel.epub'
        book_path.write_bytes(b'fixture')
        dictionary = root / 'dictionary.json'
        dictionary.write_text('{"version":1,"global":[],"books":{}}',
                              encoding='utf-8')
        store = Store(self.path)
        try:
            book_id = store.import_book(book_path, 'Novel', 'Writer',
                                        [Section(1, 'Chapter', 'Some text')])
            job = store.add_job(book_id, 'Remaining chapters', [1], 'part_02',
                                pronunciation_required=True)
            self.assertEqual(job, 2)
            frozen = snapshot_for_job(job, book_path, dictionary, snapshot_dir)
            self.assertTrue(frozen.is_file())
            self.assertEqual(old_snapshot.read_bytes(), previous_bytes)
            self.assertEqual(store.job(job)['status'], 'queued')
            store.remove_job(job)
            later = store.add_job(book_id, 'Another part', [1], 'part_03',
                                  pronunciation_required=True)
            self.assertEqual(later, 3)
            self.assertEqual(old_snapshot.read_bytes(), previous_bytes)
        finally:
            store.close()
        # Persisted sequence must not shrink after all jobs are deleted.
        reopened = Store(self.path)
        try:
            latest = reopened.add_job(book_id, 'After restart', [1], 'part_04',
                                      pronunciation_required=True)
            self.assertEqual(latest, 4)
        finally:
            reopened.close()

    def test_allocator_reserves_highest_historical_snapshot(self):
        """Queue IDs must not collide with snapshots left by old or removed jobs."""
        job_dir = self.path.parent / 'pronunciation' / 'jobs'
        job_dir.mkdir(parents=True)
        for name in ('job_1.json', 'job_87.json', 'job_not_a_number.json',
                     'job_90.json.tmp'):
            (job_dir / name).write_text('old snapshot', encoding='utf-8')
        store = Store(self.path)
        try:
            book = store.import_book('/tmp/test_book.epub', 'Book', 'Author',
                                     [Section(1, 'One', 'Text')])
            new_id = store.add_job(book, 'A', [1], 'new_part',
                                   pronunciation_required=True)
            self.assertEqual(new_id, 88)
            self.assertEqual((job_dir / 'job_87.json').read_text(), 'old snapshot')
        finally:
            store.close()

    def test_allocator_preserves_ids_without_snapshot_files(self):
        """Removing the final row must not reset the ID counter to 1."""
        store = Store(self.path)
        try:
            book = store.import_book('/tmp/test_book.epub', 'Book', 'Author',
                                     [Section(1, 'One', 'Text')])
            first = store.add_job(book, 'A', [1], 'part1')
            store.remove_job(first)
            second = store.add_job(book, 'B', [1], 'part2')
            self.assertGreater(second, first)
            store.remove_job(second)
        finally:
            store.close()
        reopened = Store(self.path)
        try:
            self.assertGreater(reopened.add_job(book, 'C', [1], 'part3'), second)
        finally:
            reopened.close()

    def test_backfill_existing_sections(self):
        """Verify backfill existing sections behavior."""
        store = Store(self.path)
        book = store.import_book('/tmp/a.epub','A','B',[Section(1,'One','Hi')])
        store.db.execute('DELETE FROM book_sections WHERE book_id=?',(book,))
        store.db.commit()
        store.populate_missing_sections(book,[Section(1,'One','Hi')])
        self.assertEqual(len(store.snapshot()['book_sections']),1)
        store.close()

    def test_config_excludes_secrets(self):
        """Ensure saved public settings never serialize passwords and receive restrictive permissions."""
        cfg = default_config()
        cfg['mysql'].update(enabled=True, password='SECRET_DO_NOT_WRITE',user='app')
        target = Path(self.tmp.name) / 'studio_config.json'
        write_config(cfg,target)
        self.assertNotIn('SECRET_DO_NOT_WRITE',target.read_text())
        self.assertEqual(read_config(target)['mysql']['user'],'app')
        self.assertEqual(target.stat().st_mode & 0o777,0o600)


class DummyCursor:
    """Minimal cursor mock for schema-migration and SQL transaction assertions."""
    def __init__(self, conn):
        """Initialize this isolated test double with its minimal in-memory state."""
        self.conn = conn
    def execute(self, sql, params=None):
        """Record a SQL statement instead of sending it to a real server."""
        self.conn.statements.append((sql,params))
    def executemany(self, sql, params):
        """Record all SQL argument tuples to verify batch inserts."""
        self.conn.statements.append((sql,list(params)))
    def fetchone(self):
        """Simulate an absent database row to exercise the migration path."""
        return None  # Simulate MySQL table missing the new voice_path column.
    def close(self):
        """Simulate closing a database cursor or connection."""
        pass


class DummyConnection:
    """Stateful fake connection that tracks commits, rollbacks, and close calls."""
    def __init__(self):
        """Initialize this isolated test double with its minimal in-memory state."""
        self.statements = []
        self.commits = 0
        self.rollbacks = 0
        self.is_open = True
    def cursor(self):
        """Return a recording cursor attached to the fake connection."""
        return DummyCursor(self)
    def commit(self):
        """Count transaction commits without writing to a real database."""
        self.commits += 1
    def rollback(self):
        """Count transaction rollbacks for failure-path assertions."""
        self.rollbacks += 1
    def close(self):
        """Simulate closing a database cursor or connection."""
        self.is_open = False
    def is_connected(self):
        """Report whether this fake MySQL connection is still open."""
        return self.is_open


class MySQLMirrorTests(unittest.TestCase):
    """Use a fake MySQL connector to test transactional mirroring."""
    def test_generates_transactional_mysql_snapshot(self):
        """Check that mirrored records are synchronized in a single transaction."""
        with tempfile.TemporaryDirectory() as temp:
            store = Store(Path(temp)/'local.sqlite3')
            book = store.import_book('/tmp/a.epub','Title','Author',
                [Section(1,'Chapter I','Once upon a time')])
            store.add_job(book,'Part 01',[1],'part_one')
            fake_conn = DummyConnection()
            fake_mysql = types.ModuleType('mysql')
            fake_connector = types.ModuleType('mysql.connector')
            fake_connector.connect = lambda **kw: fake_conn
            fake_mysql.connector = fake_connector
            cfg = default_config()['mysql']
            with patch.dict(sys.modules,{'mysql':fake_mysql,'mysql.connector':fake_connector}):
                mirror = MySQLMirror(cfg,'testpassword')
                books, jobs = mirror.sync(store)
                self.assertEqual((books,jobs),(1,1))
                self.assertTrue(mirror.connected())
                self.assertEqual(fake_conn.rollbacks,0)
                self.assertGreaterEqual(fake_conn.commits,2)
                joined = '\n'.join(q for q,_ in fake_conn.statements)
                for table in ('books','book_sections','jobs','sync_state'):
                    self.assertIn('CREATE TABLE IF NOT EXISTS '+table,joined)
                self.assertIn('INSERT INTO jobs',joined)
                self.assertIn('DELETE FROM books',joined)
                # The replica contains SQL metadata; it doesn't store audio blobs.
                self.assertNotIn('LONGBLOB',joined)
                mirror.disconnect()
            store.close()


if __name__ == '__main__':
    unittest.main()
