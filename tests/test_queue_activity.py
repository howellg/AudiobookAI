"""Queue watchdog and offscreen Studio launch-to-handoff regression tests."""
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from audiobookai_queue_activity import QueueActivity
from audiobookai_ntfy import ensure_config
from audiobookai_storage import default_config
import audiobook_gui_v2 as gui
from PySide6.QtWidgets import QApplication


class QueueActivityTests(unittest.TestCase):
    def test_export_advancement_prevents_stall_and_duplicates_do_not(self):
        now = [0]
        activity = QueueActivity(clock=lambda: now[0])
        activity.transition('exporting', 'Encoding M4B', 1)
        now[0] = 890
        activity.export_progress(42, 10)
        now[0] = 1790
        activity.export_progress(42, 10)
        self.assertTrue(activity.check_stall())
        self.assertFalse(activity.check_stall())
        self.assertIn('Encoding M4B', activity.status)
        self.assertTrue(activity.export_progress(45, 11))
        self.assertEqual(activity.status, 'Encoding M4B')
        self.assertIn('Progress resumed', activity.events[-1])

    def test_handoff_stall_and_intentional_wait(self):
        now = [0]
        activity = QueueActivity(clock=lambda: now[0])
        activity.transition('handoff', 'Next queued part selected', 2)
        now[0] = 30
        self.assertTrue(activity.check_stall())
        activity.transition('waiting', 'Waiting for cache SSD', 2)
        now[0] += 3600
        self.assertFalse(activity.check_stall())


class StudioHandoffTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = root = Path(temp.name)
        ntfy_path = root / 'ntfy.json'
        ensure_config(ntfy_path)
        prefs = Mock()
        prefs.value.side_effect = lambda key, default=None, **kwargs: default
        with patch.multiple(gui, DB_FILE=root / 'queue.sqlite3',
                            NTFY_CONFIG_PATH=ntfy_path, QSettings=Mock(return_value=prefs),
                            read_config=Mock(return_value=default_config()),
                            lookup_password=Mock(return_value=None)):
            self.studio = gui.Studio()
        studio = self.studio
        studio.diagnostic_dir = root / 'diagnostics'
        studio.backup_dir = root / 'backups'
        studio.notify_ntfy = Mock()
        self.addCleanup(studio.close)
        epub = root / 'novel.epub'
        epub.touch()
        book = studio.store.import_book(epub, 'Test novel', 'Author', [
            SimpleNamespace(index=i, title=f'Section {i}', source=f'{i}.xhtml', text='Text')
            for i in (1, 2)])
        self.first = studio.store.add_job(book, 'Part 1', [1], 'part1')
        self.second = studio.store.add_job(book, 'Part 2', [2], 'part2')

        self.processes = []
        self.qt_process = gui.QProcess
        fake_factory = Mock(side_effect=self.fake_process,
                            NormalExit=gui.QProcess.NormalExit,
                            FailedToStart=gui.QProcess.FailedToStart,
                            MergedChannels=gui.QProcess.MergedChannels)
        # Restore QProcess before Studio closes. No real converter is started.
        patches = patch.multiple(gui, QProcess=fake_factory, CACHE_DIR=root,
                                 cache_manager_busy=Mock(return_value=False),
                                 headless_runner_active=Mock(return_value=False),
                                 latest_backup=Mock(return_value=None))
        patches.start()
        self.addCleanup(patches.stop)

    def fake_process(self, parent):
        process = Mock()
        process.readAllStandardOutput.return_value = b''
        process.start.side_effect = lambda: process.started.connect.call_args.args[0]()
        self.processes.append(process)
        return process

    def complete_first_and_advance(self):
        studio = self.studio
        file = self.root / 'part1.m4b'
        file.write_bytes(b'x' * 2048)
        studio.last_output_path = str(file)
        with patch.object(gui.QTimer, 'singleShot') as timer:
            studio.process_finished(0, self.qt_process.NormalExit)
        self.assertEqual(studio.store.job(self.first)['status'], 'complete')
        self.assertEqual(studio.queue_activity.phase, 'handoff')
        callbacks = [call.args[1] for call in timer.call_args_list if call.args[0] == 350]
        self.assertEqual(len(callbacks), 1)
        callbacks[0]()

    def test_handoff_launches_even_if_metadata_backup_fails(self):
        studio = self.studio
        with patch.object(gui, 'create_metadata_backup', side_effect=OSError('disk unavailable')):
            studio.start_queue()
            self.complete_first_and_advance()
        self.assertEqual(studio.store.job(self.second)['status'], 'running')
        self.assertEqual(studio.queue_activity.phase, 'preparing')
        self.assertEqual(len(self.processes), 2)
        self.assertIn('--export-progress', self.processes[1].setArguments.call_args.args[0])
        self.assertIn('Next queued part selected', '\n'.join(studio.queue_activity.events))
        self.assertIn('Converter started', studio.queue_activity.events[-1])
        self.assertIn('Backup error', studio.backup_status.text())
        studio.notify_ntfy.assert_not_called()
        studio.proc = None

    def test_setup_exception_pauses_and_reports_the_failed_launch_step(self):
        studio = self.studio
        studio.maybe_auto_metadata_backup = Mock(
            side_effect=[None, RuntimeError('unexpected setup error')])
        studio.start_queue()
        self.complete_first_and_advance()
        self.assertIsNone(studio.proc)
        self.assertFalse(studio.run_queue)
        self.assertEqual(len(self.processes), 1)
        self.assertEqual(studio.store.job(self.second)['status'], 'failed')
        self.assertIn('Preparing metadata backup failed', studio.queue_activity.status)
        studio.notify_ntfy.assert_called_once_with('generation_failed')

    def test_mobile_reports_export_progress_and_one_alert_per_stall(self):
        studio = self.studio
        studio.maybe_auto_metadata_backup = Mock()
        now = [0]
        studio.queue_activity.clock = lambda: now[0]
        studio.start_queue()
        studio.progress_event({'event': 'export_started'})
        studio.progress_event({'event': 'export_progress', 'encoded_seconds': 80, 'percent': 40})
        now[0] = 900
        studio.check_queue_stall()
        studio.check_queue_stall()
        status = studio.mobile_status_snapshot()
        self.assertEqual(status['export_percent'], 40)
        self.assertIn('No progress', status['queue_status'])
        self.assertIn('Encoding M4B', status['queue_activity'][-1])
        studio.notify_ntfy.assert_called_once_with('queue_stalled')
        studio.progress_event({'event': 'export_progress', 'encoded_seconds': 85, 'percent': 42})
        self.assertEqual(studio.mobile_status_snapshot()['queue_status'], 'Encoding M4B')
        studio.proc = None

    def test_qprocess_failure_is_reported_without_phantom_running_job(self):
        studio = self.studio
        studio.maybe_auto_metadata_backup = Mock()
        studio.start_queue()
        process = self.processes[0]
        process.errorString.return_value = 'Permission denied'
        studio.process_error(self.qt_process.FailedToStart)
        self.assertIsNone(studio.proc)
        self.assertFalse(studio.run_queue)
        self.assertEqual(studio.store.job(self.first)['status'], 'failed')
        self.assertIn('Permission denied', studio.queue_activity.status)
        studio.notify_ntfy.assert_called_once_with('generation_failed')


if __name__ == '__main__':
    unittest.main()
