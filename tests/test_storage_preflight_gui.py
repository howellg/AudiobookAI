"""Queue storage preflight: no converter starts on a broken cache symlink."""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from audiobookai_storage import Store


class FakeLabel:
    def __init__(self):
        self.value = ''
        self.tooltip = ''
    def setText(self, text):
        self.value = text
    def setToolTip(self, tooltip):
        self.tooltip = tooltip


class FakeTimer:
    def __init__(self):
        self.started = False
    def start(self):
        self.started = True
    def stop(self):
        self.started = False


class GuiCachePreflightTests(unittest.TestCase):
    def setUp(self):
        import audiobook_gui_v2 as gui
        self.gui = gui
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.store = Store(self.root / 'studio.sqlite3')
        self.addCleanup(self.store.close)
        epub = self.root / 'novel.epub'
        epub.write_bytes(b'book')
        self.book = self.store.import_book(epub, 'Test book', 'Author',
                    [SimpleNamespace(index=1, title='Chapter', text='Words', source='a.xhtml')])
        self.job = self.store.add_job(self.book, 'Part 1', [1], 'part1')
        self.link = self.root / 'narration_cache_v2'
        self.target = self.root / 'offline' / 'cache'
        self.link.symlink_to(self.target)
        self.started = []
        self.logs = []
        self.messages = []
        self.saved_preferences = []

        class Harness:
            def __init__(inner):
                inner.store = self.store
                inner.prefs = SimpleNamespace(setValue=lambda key, value:
                    self.saved_preferences.append((key, value)))
                inner.proc = None
                inner.model_proc = None
                inner.editor_dialog = None
                inner.run_queue = False
                inner.storage_waiting = False
                inner.storage_wait_queue = False
                inner.storage_wait_job_id = None
                inner.storage_last_warning = None
                inner.storage_timer = FakeTimer()
                inner.storage_auto_resume = SimpleNamespace(isChecked=lambda: self.auto_resume)
                inner.storage_status_label = FakeLabel()
                inner.active_title = FakeLabel()
                inner.write_log = lambda message: self.logs.append(message)
            def statusBar(inner):
                return SimpleNamespace(showMessage=self.messages.append)
            def clear_storage_wait(inner):
                return gui.Studio.clear_storage_wait(inner)
            def defer_for_storage(inner, job_id, detail):
                return gui.Studio.defer_for_storage(inner, job_id, detail)
            def start_queue(inner):
                return gui.Studio.start_queue(inner)
            def start_job(inner, job_id):
                if self.capture_only:
                    self.started.append((job_id, inner.run_queue))
                else:
                    return gui.Studio.start_job(inner, job_id)
            def check_storage_and_resume(inner):
                return gui.Studio.check_storage_and_resume(inner)
        self.fake = Harness()
        self.auto_resume = True
        self.capture_only = False
        self.mock_cache = patch.object(gui, 'CACHE_DIR', self.link)
        self.mock_headless = patch.object(gui, 'headless_runner_active', return_value=False)
        self.mock_cache.start()
        self.mock_headless.start()
        self.addCleanup(self.mock_cache.stop)
        self.addCleanup(self.mock_headless.stop)

    def test_queue_blocks_before_process_or_status_change(self):
        self.fake.start_queue()
        self.assertIsNone(self.fake.proc)
        self.assertTrue(self.fake.storage_waiting)
        self.assertTrue(self.fake.storage_wait_queue)
        self.assertIsNone(self.fake.storage_wait_job_id)
        self.assertEqual(self.store.job(self.job)['status'], 'queued')
        self.assertTrue(self.fake.storage_timer.started)
        self.assertEqual(len(self.logs), 1)
        self.assertIn('Mount the SSD', self.fake.storage_status_label.value)
        self.assertEqual(self.link.readlink(), self.target)
        self.assertFalse(self.target.exists())
        self.fake.start_queue()
        self.assertEqual(len(self.logs), 1, 'Repeated checks must not flood the log')

    def test_reconnect_resumes_queue_once_and_uses_saved_order(self):
        second = self.store.add_job(self.book, 'Part 2', [1], 'part2')
        self.fake.start_queue()
        self.store.move_job(second, 0)
        self.target.mkdir(parents=True)
        self.capture_only = True
        self.fake.check_storage_and_resume()
        self.assertEqual(self.started, [(second, True)])
        self.assertFalse(self.fake.storage_waiting)
        self.assertFalse(self.fake.storage_timer.started)
        self.assertEqual(self.store.job(self.job)['status'], 'queued')
        self.assertEqual(self.store.job(second)['status'], 'queued')
        self.assertIn('Cache ready', self.fake.storage_status_label.value)
        self.fake.check_storage_and_resume()
        self.assertEqual(self.started, [(second, True)], 'No duplicate resume')

    def test_manual_selected_wait_resumes_only_that_job(self):
        self.fake.run_queue = False
        self.fake.defer_for_storage(self.job, 'Drive is offline')
        self.assertEqual(self.fake.storage_wait_job_id, self.job)
        self.target.mkdir(parents=True)
        self.capture_only = True
        self.fake.check_storage_and_resume()
        self.assertEqual(self.started, [(self.job, False)])
        self.assertFalse(self.fake.storage_waiting)

    def test_pause_or_stop_cancels_pending_auto_resume(self):
        self.fake.start_queue()
        self.assertTrue(self.fake.storage_timer.started)
        self.gui.Studio.pause_after_part(self.fake)
        self.assertFalse(self.fake.storage_timer.started)
        self.assertFalse(self.fake.storage_waiting)
        self.target.mkdir(parents=True)
        self.capture_only = True
        self.fake.check_storage_and_resume()
        self.assertEqual(self.started, [])
        self.fake.defer_for_storage(self.job, 'Offline again')
        self.gui.Studio.stop_job(self.fake)
        self.assertFalse(self.fake.storage_timer.started)
        self.assertFalse(self.fake.storage_waiting)
        self.assertEqual(self.store.job(self.job)['status'], 'queued')

    def test_auto_resume_disabled_remains_paused_until_explicit_check(self):
        self.auto_resume = False
        self.fake.start_queue()
        self.assertFalse(self.fake.storage_timer.started)
        self.assertTrue(self.fake.storage_waiting)
        self.target.mkdir(parents=True)
        self.capture_only = True
        self.assertEqual(self.started, [])
        self.fake.check_storage_and_resume()
        self.assertEqual(self.started, [(self.job, True)])

    def test_auto_resume_toggle_only_polls_during_storage_wait(self):
        self.gui.Studio.toggle_storage_auto_resume(self.fake, True)
        self.assertFalse(self.fake.storage_timer.started)
        self.fake.start_queue()
        self.assertTrue(self.fake.storage_timer.started)
        self.gui.Studio.toggle_storage_auto_resume(self.fake, False)
        self.assertFalse(self.fake.storage_timer.started)
        self.assertTrue(self.fake.storage_waiting)
        self.assertEqual(self.saved_preferences, [
            ('storage/auto_resume', True), ('storage/auto_resume', False)])

    def test_ready_check_does_not_start_unscheduled_work(self):
        self.target.mkdir(parents=True)
        self.capture_only = True
        self.fake.check_storage_and_resume()
        self.assertEqual(self.started, [])
        self.assertEqual(self.store.job(self.job)['status'], 'queued')


if __name__ == '__main__':
    unittest.main()
