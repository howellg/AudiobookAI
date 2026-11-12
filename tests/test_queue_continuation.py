"""Regression: a multi-part Start queue / From selected run never stops at part 1.

Real isolated SQLite store and verified-output receipts, fake Qt converters;
no GPU, existing audiobook, user database, or internet access.
"""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from audiobookai_storage import Store
import audiobook_gui_v2 as gui


class QueueContinuationTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.store = Store(self.root / 'studio.sqlite3')
        self.addCleanup(self.store.close)
        epub = self.root / 'example.epub'
        epub.write_text('EPUB fixture')
        chapters = [SimpleNamespace(index=i, title=f'Chapter {i}',
                                    text='This is a test', source=f'{i}.xhtml')
                    for i in range(1,4)]
        bid = self.store.import_book(epub, 'Test book', 'Author', chapters)
        self.jobs = [self.store.add_job(bid, f'Part {i}', [i], f'part{i}')
                     for i in range(1,4)]
        self.scheduled = []
        self.logs = []
        self.events = []
        self.started = []
        self.mode = FakeMode()

        harness = SimpleNamespace(
            store=self.store, proc=None, run_queue=False, active_job_id=None,
            quality_proc=None, auto_quality_scanning=False, buffer='',
            stop_requested=False, active_job_log=None,
            last_output_path=None, diagnostic_lines=[], storage_waiting=False,
            quality_auto=SimpleNamespace(isChecked=lambda:False),
            auto_quality_completed={}, full_export_proc=None,
            chapter_progress=Mock(),overall_progress=Mock(),overall_text=Mock(),
            active_title=Mock(),elapsed_timer=Mock(),
            write_log=self.logs.append,
            statusBar=lambda:SimpleNamespace(showMessage=self.logs.append),
            selected_job_id=lambda:self.jobs[0],
            clear_storage_wait=lambda:None,
            persist_progress=lambda force=False:None,
            consume_output=lambda:None,
            refresh_saved_parts=lambda:None,
            refresh_jobs=lambda:None,
            refresh_review=lambda:None,
            tick=lambda:None,
            flush_generation_logs=lambda:None,
            queue_mode_status=self.mode,
            notify_ntfy=self.events.append)
        self.h=harness
        harness.show_queue_mode=lambda message: gui.Studio.show_queue_mode(harness,message)
        harness.start_queue=lambda: gui.Studio.start_queue(harness)
        harness.continue_queue_if_requested=lambda:gui.Studio.continue_queue_if_requested(harness)
        harness.start_job=self.start_job
        harness._run_selected_part=lambda *,single_part: gui.Studio._run_selected_part(harness,single_part=single_part)

    def start_job(self, job_id):
        """Simulate a valid converter launch and its resulting M4B path."""
        job=self.store.job(job_id)
        self.assertEqual(job['status'],'queued')
        self.store.update_job(job_id,'running')
        file=self.root / (job['output_name']+'.m4b')
        file.write_bytes(b'valid fixture ' * 1024)
        self.h.active_job_id=job_id
        self.h.last_output_path=str(file)
        self.h.proc=Mock()
        self.started.append(job_id)

    def complete(self):
        with patch.object(gui.QTimer,'singleShot',side_effect=lambda _,f:self.scheduled.append(f)):
            gui.Studio.process_finished(self.h,0,gui.QProcess.NormalExit)
        self.assertIsNone(self.h.proc)

    def callbacks(self):
        to_call=self.scheduled[:]
        self.scheduled.clear()
        for cb in to_call:
            cb()

    def test_start_queue_processes_all_three_parts_without_manual_restart(self):
        self.h.start_queue()
        self.assertEqual(self.started,[self.jobs[0]])
        self.assertTrue(self.h.run_queue)
        for number in range(3):
            self.complete()
            self.assertEqual(self.store.job(self.jobs[number])['status'],'complete')
            if number<2:
                self.assertEqual(len(self.scheduled),1)
                self.callbacks()
                self.assertEqual(self.started,self.jobs[:number+2])
                self.assertTrue(self.h.run_queue)
            else:
                self.callbacks()
                self.assertEqual(self.started,self.jobs)
                self.assertFalse(self.h.run_queue)
        self.assertEqual(self.events,['queue_complete'])
        self.assertEqual(len(self.store.completed_parts()),3)
        self.assertIn('Queue idle',self.mode.text)

    def test_from_selected_also_continues_automatically(self):
        gui.Studio.run_selected_job(self.h)
        self.assertTrue(self.h.run_queue)
        self.assertIn('Continuous queue ON',self.mode.text)
        self.complete()
        self.callbacks()
        self.assertEqual(self.started,self.jobs[:2])

    def test_explicit_one_part_only_does_not_advance(self):
        gui.Studio.run_one_job(self.h)
        self.assertFalse(self.h.run_queue)
        self.complete()
        self.assertEqual(self.started,self.jobs[:1])
        self.assertEqual(self.scheduled,[])
        self.assertEqual(len([r for r in self.store.jobs() if r['status']=='queued']),2)
        self.assertIn('Queue paused after this part',self.mode.text)

    def test_pause_after_part_prevents_next_job(self):
        self.h.start_queue()
        gui.Studio.pause_after_part(self.h)
        self.complete()
        self.assertEqual(self.scheduled,[])
        self.assertEqual(self.started,self.jobs[:1])
        self.assertFalse(self.h.run_queue)

    def test_enable_continuous_mode_during_one_part(self):
        gui.Studio.run_one_job(self.h)
        self.assertFalse(self.h.run_queue)
        self.h.start_queue()
        self.assertTrue(self.h.run_queue)
        self.complete()
        self.callbacks()
        self.assertEqual(self.started,self.jobs[:2])


class FakeMode:
    def __init__(self): self.text=''
    def setText(self, text): self.text=text


if __name__ == '__main__':
    unittest.main()
