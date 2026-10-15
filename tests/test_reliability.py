"""Offline V2.8 recovery, diagnostic and metadata backup regression tests."""
from __future__ import annotations

import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import patch
import zipfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from audiobookai_reliability import (
    append_diagnostic, check_resume_job, create_metadata_backup, diagnostic_hint,
    job_log_path, latest_backup, verify_metadata_backup,
)
from audiobookai_storage import Store


class ReliabilityTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.output = self.root / "output"
        self.output.mkdir()
        self.backups = self.root / "backups"
        self.db = sqlite3.connect(":memory:")
        self.addCleanup(self.db.close)
        self.db.execute("CREATE TABLE jobs (id INTEGER PRIMARY KEY, status TEXT)")
        self.db.execute("INSERT INTO jobs (status) VALUES ('running')")
        self.db.commit()

    def test_consistent_sqlite_backup_and_pronunciation_data(self):
        (self.output / "pronunciation" / "jobs").mkdir(parents=True)
        (self.output / "pronunciation" / "dictionary.json").write_text('{"version":1}')
        (self.output / "pronunciation" / "jobs" / "job_1.json").write_text('{"rules":[]}')
        (self.output / "archive").mkdir()
        (self.output / "archive" / "catalog.json").write_text('{"editions":{}}')
        (self.output / "audiobooks").mkdir()
        (self.output / "audiobooks" / "private.m4b").write_bytes(b"private!")
        (self.output / "narration_cache_v2").mkdir()
        (self.output / "narration_cache_v2" / "speech.wav").write_bytes(b"no!")
        result = create_metadata_backup(
            self.db, self.output, self.backups,
            config={"mysql": {"enabled": False, "host": "localhost",
                              "password": "secret", "user": "studio"}})
        self.assertTrue(result.is_file())
        manifest = verify_metadata_backup(result)
        with zipfile.ZipFile(result) as zf:
            members = set(zf.namelist())
            self.assertIn("studio.sqlite3", members)
            self.assertIn("pronunciation/jobs/job_1.json", members)
            self.assertIn("archive/catalog.json", members)
            self.assertNotIn("audiobooks/private.m4b", members)
            self.assertNotIn("narration_cache_v2/speech.wav", members)
            self.assertNotIn(b"secret", zf.read("studio_config.json"))
            snapshot_db = self.root / "backup.sqlite3"
            snapshot_db.write_bytes(zf.read("studio.sqlite3"))
        with sqlite3.connect(snapshot_db) as restored:
            self.assertEqual(restored.execute("SELECT status FROM jobs").fetchone()[0], "running")
        self.assertIn("studio.sqlite3", manifest["sha256"])

    def test_backup_rejects_corruption_and_keeps_previous_good_archive(self):
        good = create_metadata_backup(self.db, self.output, self.backups)
        with zipfile.ZipFile(good, "r") as zf:
            payloads = [(name, zf.read(name)) for name in zf.namelist()]
        invalid = self.root / "tampered.zip"
        with zipfile.ZipFile(invalid, "w") as zf:
            for name, data in payloads:
                zf.writestr(name, b"not a sqlite file" if name == "studio.sqlite3" else data)
        with self.assertRaises(ValueError):
            verify_metadata_backup(invalid)
        self.assertTrue(good.exists())
        self.assertTrue(verify_metadata_backup(good))

    def test_rotation_keeps_manual_backups(self):
        manual = create_metadata_backup(self.db, self.output, self.backups)
        for _ in range(4):
            create_metadata_backup(self.db, self.output, self.backups,
                                   automatic=True, keep=2)
        self.assertEqual(len(list(self.backups.glob("audiobookai_metadata_auto_*.zip"))), 2)
        self.assertTrue(manual.is_file())
        self.assertEqual(latest_backup(self.backups, automatic_only=True).suffix, ".zip")

    def test_recovery_checks_data_before_requeue(self):
        epub = self.root / "book.epub"
        voice = self.root / "voice.wav"
        epub.write_bytes(b"dummy")
        voice.write_bytes(b"dummy")
        job = {"id": 3, "status": "interrupted", "indices": "[1,2]",
               "book_path": str(epub), "voice_path": str(voice),
               "pronunciation_required": False}
        self.assertIsNone(check_resume_job(job))
        self.assertEqual(check_resume_job({**job, "status": "failed"}), "Not interrupted")
        self.assertIn("selection", check_resume_job({**job, "indices": "[]"}))
        voice.unlink()
        self.assertIn("Narrator", check_resume_job(job))
        self.assertIn("snapshot", check_resume_job(
            {**job, "voice_path": None, "pronunciation_required": True},
            snapshot_dir=self.root / "nonexistent"))

    def test_restart_marks_running_interrupted_and_batch_requeue_is_scoped(self):
        # Test the actual SQLite Store migration/requeue path without a running GUI.
        path = self.root / "store.sqlite3"
        store = Store(path)
        try:
            store.db.execute("INSERT INTO books(path,title,author,sections,words,added) "
                             "VALUES(?,?,?,?,?,?)", (str(self.root / "book.epub"),
                             "Book", "Author", 1, 10, "2026-10-15"))
            book_id = store.db.execute("SELECT id FROM books").fetchone()[0]
            ids = [store.add_job(book_id, f"Job {i}", "[1]", f"part{i}")
                   if False else None for i in []]  # no implicit test jobs
            first = store.add_job(book_id, "A", [1], "part_a")
            second = store.add_job(book_id, "B", [1], "part_b")
            third = store.add_job(book_id, "C", [1], "part_c")
            store.update_job(first, "running")
            store.update_job(second, "interrupted")
            store.update_job(third, "failed")
        finally:
            store.close()
        recovered = Store(path)
        try:
            self.assertEqual(recovered.job(first)["status"], "interrupted")
            self.assertEqual(recovered.requeue_interrupted([first, second, third, 9999]), 2)
            self.assertEqual(recovered.job(first)["status"], "queued")
            self.assertEqual(recovered.job(second)["status"], "queued")
            self.assertEqual(recovered.job(third)["status"], "failed")
            self.assertEqual(recovered.requeue_interrupted([first]), 0)
        finally:
            recovered.close()

    def test_diagnostics_are_local_bounded_and_report_last_error(self):
        path = job_log_path(self.root / "diagnostics", 12)
        append_diagnostic(path, "Running section 1")
        append_diagnostic(path, "CUDA out of memory")
        self.assertIn("CUDA out of memory", path.read_text())
        self.assertIn("Exit 1:", diagnostic_hint(
            ["hello", "RuntimeError: CUDA out of memory"], 1))
        with patch("audiobookai_reliability.LOG_LIMIT", 10):
            append_diagnostic(path, "following event")
        self.assertTrue(path.with_suffix(".1.log").exists())


if __name__ == "__main__":
    unittest.main()
