"""Qt-free queue runner regression tests; no Chatterbox, Qt or GPU required."""
from __future__ import annotations

import io
import json
from pathlib import Path
import sys
import tempfile
from unittest.mock import patch
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import audiobookai_headless as runner
from audiobookai_pronunciation import snapshot_for_job
from audiobookai_storage import Store


class OneSection:
    def __init__(self):
        self.index, self.title, self.text, self.source = 1, "Chapter", "Test text", "one.xhtml"


class FakeProcess:
    def __init__(self, chunks):
        self.stdout = io.StringIO("\n".join(chunks) + "\n")
    def wait(self):
        return 0
    def poll(self):
        return 0


class HeadlessTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.epub = self.root / "novel.epub"
        self.epub.write_bytes(b"epub fixture")
        self.output = self.root / "output"
        self.output.mkdir()
        self.store = Store(self.output / "audiobookai_studio.sqlite3")
        self.addCleanup(self.store.close)
        self.book = self.store.import_book(
            self.epub, "Test Novel", "Author", [OneSection()])
        self.dict_path = self.root / "dictionary.json"
        self.dict_path.write_text(json.dumps({
            "version": 1, "global": [{"term": "Rand", "spoken": "Ranned"}],
            "books": {}}))
        self.snapshots = self.output / "pronunciation" / "jobs"

    def add(self, required=True):
        job_id = self.store.add_job(self.book, "Chapter 1", [1], "chapter_one",
                                    pronunciation_required=required)
        if required:
            snapshot_for_job(job_id, self.epub, self.dict_path, self.snapshots)
        return job_id

    def test_exact_frozen_snapshot_and_saved_chapter_selection(self):
        job_id = self.add()
        job = dict(self.store.job(job_id))
        args = runner.checked_job(job, snapshot_dir=self.snapshots)
        self.assertIn("--pronunciation-file", args)
        self.assertEqual(args[args.index("--pronunciation-file")+1],
                         str(self.snapshots / f"job_{job_id}.json"))
        self.assertEqual(args[args.index("--chapters")+1], "1")
        self.assertEqual(args[args.index("--output-name")+1], "chapter_one")
        self.assertEqual((self.snapshots / f"job_{job_id}.json").exists(), True)

    def test_snapshot_missing_blocks_resume_without_replacing_rules(self):
        job_id = self.add()
        snap = self.snapshots / f"job_{job_id}.json"
        snap.unlink()
        with self.assertRaises(FileNotFoundError):
            runner.checked_job(dict(self.store.job(job_id)), snapshot_dir=self.snapshots)
        self.assertEqual(self.store.job(job_id)["status"], "queued")

    def test_complete_chapter_reuse_progress_and_verified_export(self):
        job_id = self.add(required=False)
        exported = self.output / "chapter_one.m4b"
        exported.write_bytes(b"f" * 2048)
        events = [
            {"event": "plan", "sections": [
                {"index": 1, "chunks": 5, "completed": True}]},
            {"event": "chapter_done", "index": 1, "chunks": 5},
            {"event": "export_done", "path": str(exported)},
        ]
        lines = ["AUDIOBOOKAI_EVENT " + json.dumps(event) for event in events]
        def make_process(*args, **kwargs):
            self.assertIn("--studio-events", args[0])
            return FakeProcess(lines)
        with patch.object(runner, "OUTPUT", self.output):
            code = runner.run_job(self.store, job_id, process_factory=make_process)
        self.assertEqual(code, 0)
        self.assertEqual(self.store.job(job_id)["status"], "complete")
        self.assertEqual(self.store.job(job_id)["completed_chunks"], 5)
        self.assertEqual(self.store.job(job_id)["output_path"], str(exported))
        self.assertEqual(len(self.store.completed_parts(self.book)), 1)
        self.store.remove_job(job_id)
        self.assertEqual(self.store.claimed_sections(self.book), {1})
        self.assertEqual(len(self.store.recorded_jobs_for_export(self.book)), 1)

    def test_unmounted_cache_preflight_never_claims_job_or_launches(self):
        job_id = self.add(required=False)
        link = self.output / 'narration_cache_v2'
        missing = self.root / 'unmounted_ssd' / 'narration_cache_v2'
        link.symlink_to(missing)
        launched = []
        def forbid_launch(*args, **kwargs):
            launched.append(args)
            raise AssertionError('Converter launched with missing SSD')
        with patch.object(runner, 'OUTPUT', self.output):
            with self.assertRaisesRegex(RuntimeError, 'remains queued'):
                runner.run_job(self.store, job_id, process_factory=forbid_launch)
        self.assertEqual(launched, [])
        self.assertEqual(self.store.job(job_id)['status'], 'queued')
        self.assertIsNone(self.store.job(job_id)['output_path'])
        self.assertTrue(link.is_symlink())
        self.assertFalse(missing.exists())

    def test_zero_exit_without_verified_m4b_fails_safely(self):
        job_id = self.add(required=False)
        with patch.object(runner, "OUTPUT", self.output):
            code = runner.run_job(self.store, job_id,
                process_factory=lambda *a, **k: FakeProcess([
                    "AUDIOBOOKAI_EVENT " + json.dumps({
                        "event": "plan", "sections": [
                            {"index": 1, "chunks": 5, "completed": False}]}),
                    "AUDIOBOOKAI_EVENT " + json.dumps({
                        "event": "chunk_done", "index": 1, "chunk": 3, "total": 5})]))
        self.assertEqual(code, 1)
        self.assertEqual(self.store.job(job_id)["status"], "failed")
        self.assertEqual(self.store.job(job_id)["completed_chunks"], 3)
        self.assertEqual(self.store.job(job_id)["output_path"], None)
        self.assertEqual(self.store.completed_parts(self.book), [])

    def test_blocks_completed_job_and_bad_chapter_list(self):
        job_id = self.add(required=False)
        self.store.update_job(job_id, "complete")
        with self.assertRaises(ValueError):
            runner.checked_job(dict(self.store.job(job_id)))
        job = dict(self.store.job(job_id))
        job["status"] = "interrupted"
        job["indices"] = "[true, 2]"
        with self.assertRaises(ValueError):
            runner.checked_job(job)


if __name__ == "__main__":
    unittest.main()
