"""Offline ntfy integration checks. No real HTTP, GPU, or user credentials."""
from __future__ import annotations

import json
import os
from pathlib import Path
import queue
import stat
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch

SCRIPTS = Path(__file__).resolve().parents[1] / 'scripts'
sys.path.insert(0, str(SCRIPTS))

from audiobookai_ntfy import (
    CONFIG_PATH, DEFAULT_FLAGS, EVENTS, NtfyPublisher, ensure_config,
    publish, read_config, write_config,
)


class SettingsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'output' / 'ntfy.json'

    def test_private_random_topic_persists_without_enabled_by_default(self):
        first = ensure_config(self.path)
        self.assertEqual(first['enabled'], False)
        self.assertTrue(first['topic'].startswith('audiobookai_'))
        self.assertGreaterEqual(len(first['topic']), 40)
        self.assertEqual(ensure_config(self.path), first)
        self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o600)
        self.assertTrue(all(first['events'].values()))

    def test_config_can_be_activated_and_event_flags_changed(self):
        sender = NtfyPublisher(self.path, sender=lambda *_: None)
        original = sender.config['topic']
        sender.update(enabled=True, event='queue_complete', checked=False)
        saved = read_config(self.path)
        self.assertEqual(saved['topic'], original)
        self.assertTrue(saved['enabled'])
        self.assertFalse(saved['events']['queue_complete'])
        self.assertTrue(saved['events']['generation_failed'])
        self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o600)

    def test_reject_bad_topic_and_symlink(self):
        cfg = ensure_config(self.path)
        cfg['topic'] = 'short'
        with self.assertRaisesRegex(ValueError, 'topic'):
            write_config(cfg, self.path)
        self.assertEqual(ensure_config(self.path)['topic'], read_config(self.path)['topic'])
        # A symlink to a public file must not be followed or replaced.
        fake = Path(self.temp.name) / 'forbidden.txt'
        fake.write_text('do not touch')
        other = Path(self.temp.name) / 'link.json'
        other.symlink_to(fake)
        with self.assertRaisesRegex(ValueError, 'symlink'):
            ensure_config(other, enabled=True)
        self.assertEqual(fake.read_text(), 'do not touch')

    def test_older_permissions_are_hardened_without_changing_topic(self):
        first = ensure_config(self.path)
        self.path.chmod(0o644)
        other = ensure_config(self.path)
        self.assertEqual(first, other)
        self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o600)


class TransportTests(unittest.TestCase):
    def setUp(self):
        self.topic = 'audiobookai_abcdefghijklmnopqrstuvwxyz012345'

    def test_publish_uses_https_and_generic_no_sensitive_data(self):
        seen = []
        class Response:
            status = 200
            def __enter__(self): return self
            def __exit__(self, *args): pass
        def fake_open(req, *, timeout):
            seen.append((req, timeout))
            return Response()
        for event in EVENTS:
            publish(self.topic, event, opener=fake_open)
        self.assertEqual(len(seen), len(EVENTS))
        for req, timeout in seen:
            self.assertEqual(req.full_url, 'https://ntfy.sh/' + self.topic)
            self.assertEqual(req.get_method(), 'POST')
            self.assertLessEqual(timeout, 8)
            self.assertEqual(req.get_header('Content-type'),
                             'text/plain; charset=utf-8')
            self.assertNotIn('books/', req.data.decode())
            self.assertLess(len(req.data), 200)
            self.assertIn(req.get_header('Priority'), ('3', '4'))

    def test_invalid_event_or_topic_cannot_publish(self):
        fake = Mock()
        with self.assertRaises(ValueError):
            publish('bad/topic', 'test', opener=fake)
        with self.assertRaises(ValueError):
            publish(self.topic, 'any-html-or-command', opener=fake)
        fake.assert_not_called()


class AsyncPublisherTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'ntfy.json'

    def collect(self, sender, timeout=3):
        all_results = []
        stop = time.monotonic() + timeout
        while time.monotonic() < stop:
            results, pending = sender.drain()
            all_results.extend(results)
            if pending == 0:
                return all_results
            time.sleep(.005)
        self.fail('Notification worker did not complete')

    def test_disabled_and_events_respected(self):
        calls = []
        sender = NtfyPublisher(self.path, sender=lambda topic, event: calls.append(event))
        self.assertFalse(sender.notify('queue_complete'))
        sender.update(enabled=True)
        sender.update(event='queue_complete', checked=False)
        self.assertFalse(sender.notify('queue_complete'))
        self.assertTrue(sender.notify('generation_failed'))
        self.assertEqual(self.collect(sender)[0].event, 'generation_failed')
        self.assertEqual(calls, ['generation_failed'])

    def test_nonblocking_background_delivery_and_result(self):
        go = threading.Event()
        called = []
        def deliver(topic, event):
            go.wait(2)
            called.append(event)
        sender = NtfyPublisher(self.path, sender=deliver)
        sender.update(enabled=True)
        start = time.monotonic()
        self.assertTrue(sender.notify('queue_complete'))
        self.assertLess(time.monotonic() - start, 0.25)
        self.assertEqual(sender.drain(), ([], 1))
        go.set()
        result = self.collect(sender)
        self.assertEqual(called, ['queue_complete'])
        self.assertEqual(len(result), 1)
        self.assertTrue(result[0].sent)

    def test_network_error_is_sanitized_and_does_not_raise_on_gui_thread(self):
        def deliver(topic, event):
            raise RuntimeError('Sensitive secret URL https://ntfy.sh/' + topic)
        sender = NtfyPublisher(self.path, sender=deliver)
        sender.update(enabled=True)
        self.assertTrue(sender.notify('storage_unavailable'))
        results = self.collect(sender)
        self.assertFalse(results[0].sent)
        self.assertNotIn(sender.config['topic'], results[0].detail)
        self.assertNotIn('https:', results[0].detail)

    def test_max_four_pending_prevents_spam(self):
        go = threading.Event()
        def deliver(topic, event):
            go.wait(2)
        sender = NtfyPublisher(self.path, sender=deliver)
        sender.update(enabled=True)
        for _ in range(4):
            self.assertTrue(sender.notify('generation_failed'))
        self.assertFalse(sender.notify('generation_failed'))
        go.set()
        self.assertEqual(len(self.collect(sender)), 4)


if __name__ == '__main__':
    unittest.main()

class StudioEventHookTests(unittest.TestCase):
    """Exercise Qt event handlers using fake widgets, no QProcess/GPU/network."""

    def setUp(self):
        import audiobook_gui_v2 as gui
        self.gui = gui
        self.events = []
        self.calls = []
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        path = Path(self.tmp.name) / 'book_part.m4b'
        path.write_bytes(b'fake m4b test payload' * 100)
        self.path = path

        class Log:
            def __init__(it):
                it.messages = []
            def write(it, message): it.messages.append(message)

        class Store:
            next_one = None
            def job(it, job_id): return {'id': job_id, 'output_name': path.stem}
            def complete_job(it, job_id, filename):
                self.calls.append(('complete', job_id))
                self.events.append('receipt')
            def next_job(it): return it.next_one
            def update_job(it, job_id, status, **kwargs):
                self.calls.append((status, job_id))
        self.store = Store()

        class Harness:
            proc = object()
            buffer = ''
            stop_requested = False
            active_job_id = 13
            last_output_path = str(path)
            active_job_log = None
            started_at = None
            run_queue = True
            export_in_progress = False
            storage_waiting = False
            storage_last_warning = None
            storage_wait_queue = False
            storage_wait_job_id = None
            diagnostic_lines = []
            def consume_output(it): pass
            def write_log(it, value): self.calls.append(('log',value))
            def persist_progress(it, force=False): pass
            def refresh_saved_parts(it): pass
            def tick(it): pass
            def flush_generation_logs(it): pass
            def refresh_jobs(it): pass
            def refresh_review(it): pass
            def continue_queue_if_requested(it): pass
            def notify_ntfy(it, event): self.events.append(event)
            def clear_storage_wait(it): pass
        self.app = Harness()
        self.app.store = self.store
        self.app.proc = Mock()
        self.app.chapter_progress = Mock()
        self.app.overall_progress = Mock()
        self.app.overall_text = Mock()
        self.app.active_title = Mock()
        self.app.elapsed_timer = Mock()
        self.app.statusBar = lambda: Mock()
        self.app.storage_status_label = Mock()
        self.app.storage_auto_resume = Mock()
        self.app.storage_auto_resume.isChecked.return_value = False

    def test_no_queue_finished_notification_until_export_receipt_committed(self):
        with patch.object(self.gui.QTimer, 'singleShot') as next_call:
            self.gui.Studio.process_finished(self.app, 0, self.gui.QProcess.NormalExit)
            self.assertEqual(self.events, ['receipt','queue_complete'])
            next_call.assert_called_once()
        self.assertIsNone(self.app.proc)

    def test_notifies_failure_but_not_manual_stop(self):
        self.app.last_output_path = None
        with patch.object(self.gui.QTimer, 'singleShot'):
            self.gui.Studio.process_finished(self.app, 1, self.gui.QProcess.NormalExit)
        self.assertEqual(self.events, ['generation_failed'])
        self.assertTrue(any(c[0] == 'failed' for c in self.calls if isinstance(c, tuple)))

    def test_graceful_manual_stop_not_reported_as_failure(self):
        self.app.stop_requested = True
        self.app.run_queue = False
        with patch.object(self.gui.QTimer,'singleShot'):
            self.gui.Studio.process_finished(self.app, 130, self.gui.QProcess.NormalExit)
        self.assertEqual(self.events, [])
        self.assertTrue(any(c[0] == 'interrupted' for c in self.calls if isinstance(c,tuple)))

    def test_storage_wait_only_notifies_once_until_cleared(self):
        self.app.write_log = lambda text: None
        self.gui.Studio.defer_for_storage(self.app, 13, 'SSD unplugged')
        self.gui.Studio.defer_for_storage(self.app, 13, 'SSD unplugged')
        self.assertEqual(self.events, ['storage_unavailable'])
