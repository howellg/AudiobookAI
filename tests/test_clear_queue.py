"""Bulk queue removal must never delete audio or interrupt a live job."""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from audiobookai_storage import Store


class Section:
    def __init__(self, index):
        self.index = index
        self.title = f'Chapter {index}'
        self.text = f'Sample chapter {index}.'
        self.source = f'chapter{index}.xhtml'


class ClearQueueStorageTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.db_path = self.root / 'audiobookai_studio.sqlite3'
        self.store = Store(self.db_path)
        self.addCleanup(self.store.close)
        self.epub = self.root / 'book.epub'
        self.epub.write_bytes(b'original epub')
        self.book_id = self.store.import_book(self.epub, 'Bulk clear test', 'Author',
                                              [Section(i) for i in range(1, 6)])
        self.cache = self.root / 'narration_cache_v2'
        self.cache.mkdir()
        self.cached_wav = self.cache / 'cached_chunk.wav'
        self.cached_wav.write_bytes(b'previously generated audio')
        self.snapshots = self.root / 'pronunciation' / 'jobs'
        self.snapshots.mkdir(parents=True)

    def test_clear_mixed_statuses_and_permanent_receipts(self):
        queued = self.store.add_job(self.book_id, 'Incorrect first batch', [1, 2], 'Part 1')
        interrupted = self.store.add_job(self.book_id, 'Interrupted', [3], 'Part 2')
        failed = self.store.add_job(self.book_id, 'Failed', [4], 'Part 3')
        complete = self.store.add_job(self.book_id, 'Complete', [5], 'Book - Part(4)')
        self.store.update_job(interrupted, 'interrupted', error='missing cache drive')
        self.store.update_job(failed, 'failed', error='past error')
        self.store.update_progress(interrupted, 100, 60, 3, 60, 100)
        exported = self.root / 'Book - Part(4).m4b'
        exported.write_bytes(b'm4b' * 1000)
        self.store.complete_job(complete, exported)
        for job_id in (queued, interrupted, failed, complete):
            (self.snapshots / f'job_{job_id}.json').write_text(f'Frozen {job_id}')
        self.assertEqual(self.store.claimed_sections(self.book_id), {1, 2, 5})
        receipts_before = [dict(r) for r in self.store.completed_parts(self.book_id)]
        notices = []
        self.store.on_change = lambda: notices.append('changed')
        self.assertEqual(self.store.clear_all_jobs(), 4)
        self.assertEqual(notices, ['changed'])  # single atomic change for MySQL mirror
        self.assertEqual(self.store.jobs(), [])
        self.assertIsNone(self.store.next_job())
        self.assertEqual(self.store.snapshot()['jobs'], [])
        self.assertEqual([dict(r) for r in self.store.completed_parts(self.book_id)],
                         receipts_before)
        self.assertEqual(self.store.claimed_sections(self.book_id), {5})
        self.assertEqual(self.epub.read_bytes(), b'original epub')
        self.assertEqual(self.cached_wav.read_bytes(), b'previously generated audio')
        self.assertEqual(exported.read_bytes(), b'm4b' * 1000)
        for job_id in (queued, interrupted, failed, complete):
            self.assertEqual((self.snapshots / f'job_{job_id}.json').read_text(),
                             f'Frozen {job_id}')
        self.assertIsNotNone(self.store.book(self.book_id))
        self.assertEqual(len(self.store.db.execute('SELECT * FROM book_sections WHERE book_id=?', (self.book_id,)).fetchall()), 5)

        self.store.close()
        self.store = Store(self.db_path)
        self.assertEqual(self.store.jobs(), [])
        new_job = self.store.add_job(self.book_id, 'Correct batch size', [1], 'Part after clear')
        self.assertGreater(new_job, complete)
        self.assertEqual(self.store.claimed_sections(self.book_id), {1, 5})
        self.assertEqual((self.snapshots / f'job_{complete}.json').read_text(),
                         f'Frozen {complete}')

    def test_active_running_or_stopping_aborts_atomically(self):
        a = self.store.add_job(self.book_id, 'Queued', [1], 'part_1')
        b = self.store.add_job(self.book_id, 'Running', [2], 'part_2')
        self.store.update_job(b, 'running')
        self.store.update_progress(b, 100, 40, 2, 40, 100)
        notifications = []
        self.store.on_change = lambda: notifications.append('changed')
        with self.assertRaisesRegex(ValueError, 'still running or stopping'):
            self.store.clear_all_jobs()
        self.assertEqual([j['id'] for j in self.store.jobs()], [a, b])
        self.assertEqual(self.store.job(b)['completed_chunks'], 40)
        self.assertEqual(notifications, [])
        self.store.update_job(b, 'stopping')
        notifications.clear()
        with self.assertRaisesRegex(ValueError, 'still running or stopping'):
            self.store.clear_all_jobs()
        self.assertEqual([j['id'] for j in self.store.jobs()], [a, b])
        self.assertEqual(notifications, [])
        self.store.update_job(b, 'interrupted')
        self.assertEqual(self.store.clear_all_jobs(), 2)

    def test_empty_queue_does_not_trigger_write_or_sync(self):
        updates = []
        self.store.on_change = lambda: updates.append('changed')
        self.assertEqual(self.store.clear_all_jobs(), 0)
        self.assertEqual(updates, [])
        new_job = self.store.add_job(self.book_id, 'First', [1], 'first')
        self.assertEqual(new_job, 1)


class ClearQueueGuiTests(unittest.TestCase):
    def setUp(self):
        import audiobook_gui_v2 as gui
        from PySide6.QtWidgets import QMessageBox
        self.gui = gui
        self.msg = QMessageBox
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = Store(Path(self.temp.name) / 'studio.sqlite3')
        self.addCleanup(self.store.close)
        self.book_id = self.store.import_book(
            Path(self.temp.name) / 'book.epub', 'Book', 'Author', [Section(1)])
        self.pending = self.store.add_job(self.book_id, 'Queued part', [1], 'queued_part')
        self.failed = self.store.add_job(self.book_id, 'Failed part', [1], 'failed_part')
        self.store.update_job(self.failed, 'failed')
        self.calls = []
        self.messages = []

        class Harness:
            def __init__(inner):
                inner.store = self.store
                inner.proc = None
                inner.run_queue = True
            def statusBar(inner):
                return SimpleNamespace(showMessage=self.messages.append)
            def clear_storage_wait(inner):
                self.calls.append('cancel storage resume')
            def refresh_jobs(inner):
                self.calls.append('refresh queue')
            def refresh_review(inner):
                self.calls.append('refresh review')
            def refresh_saved_parts(inner):
                self.calls.append('refresh saved parts')

        self.harness = Harness()
        self.mock_headless = patch.object(gui, 'headless_runner_active', return_value=False)
        self.mock_headless.start()
        self.addCleanup(self.mock_headless.stop)

    def test_cancel_is_safe_and_default_selection_is_no(self):
        with patch.object(self.msg, 'question', return_value=self.msg.No) as question:
            self.gui.Studio.clear_entire_queue(self.harness)
        self.assertEqual(len(self.store.jobs()), 2)
        self.assertEqual(self.calls, [])
        self.assertEqual(self.harness.run_queue, True)
        args = question.call_args.args
        self.assertIn('ALL 2 queue entries', args[2])
        self.assertIn('1 queued', args[2])
        self.assertIn('1 failed', args[2])
        self.assertIn('DOES NOT delete', args[2])
        self.assertIn('\n\n', args[2])
        self.assertEqual(args[-1], self.msg.No)

    def test_confirm_removes_all_and_cancels_deferred_resumption(self):
        with patch.object(self.msg, 'question', return_value=self.msg.Yes):
            self.gui.Studio.clear_entire_queue(self.harness)
        self.assertEqual(self.store.jobs(), [])
        self.assertFalse(self.harness.run_queue)
        self.assertEqual(self.calls,
                         ['cancel storage resume', 'refresh queue',
                          'refresh saved parts', 'refresh review'])
        self.assertIn('Cleared 2 queue entries', self.messages[-1])

    def test_active_process_and_headless_runner_block_confirmation(self):
        self.harness.proc = object()
        with patch.object(self.msg, 'information') as info, \
             patch.object(self.msg, 'question') as confirm:
            self.gui.Studio.clear_entire_queue(self.harness)
        info.assert_called_once()
        confirm.assert_not_called()
        self.assertEqual(len(self.store.jobs()), 2)
        self.harness.proc = None
        with patch.object(self.gui, 'headless_runner_active', return_value=True), \
             patch.object(self.msg, 'information') as info, \
             patch.object(self.msg, 'question') as confirm:
            self.gui.Studio.clear_entire_queue(self.harness)
        info.assert_called_once()
        confirm.assert_not_called()
        self.assertEqual(len(self.store.jobs()), 2)

    def test_database_running_flag_blocks_bulk_clear(self):
        self.store.update_job(self.pending, 'running')
        with patch.object(self.msg, 'warning') as warning, \
             patch.object(self.msg, 'question') as confirm:
            self.gui.Studio.clear_entire_queue(self.harness)
        warning.assert_called_once()
        confirm.assert_not_called()
        self.assertEqual(len(self.store.jobs()), 2)

    def test_empty_queue_reports_no_changes(self):
        self.store.clear_all_jobs()
        with patch.object(self.msg, 'information') as info, \
             patch.object(self.msg, 'question') as confirm:
            self.gui.Studio.clear_entire_queue(self.harness)
        info.assert_called_once()
        confirm.assert_not_called()
        self.assertEqual(self.calls, [])


if __name__ == '__main__':
    unittest.main()
