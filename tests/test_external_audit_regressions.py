"""Regressions for independently reported AudiobookAI issues (offline fixtures)."""
from __future__ import annotations

import ast
from contextlib import redirect_stdout
import io
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import threading
import time
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import audiobookai_archive as archive
import audiobookai_headless as headless
import audiobookai_mobile as mobile
import audiobookai_pronunciation as pron
import audiobookai_quality as quality
import audiobookai_quality_review as review
import audiobookai_integrity as integrity
from audiobookai_storage import Store, read_config
import audiobook_gui_v2 as gui


class DatabaseFixTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.path = self.root / 'studio.sqlite3'
        self.store = Store(self.path)
        self.addCleanup(self.store.close)
        epub = self.root / 'sample.epub'
        epub.write_text('sample')
        self.book = self.store.import_book(epub, 'Book', 'Original Author', [
            SimpleNamespace(index=1, text='words', title='Chapter', source='c.xhtml')])

    def test_bad_mysql_settings_do_not_break_queuing(self):
        for bad in ('mysql string', [], 72, None):
            with self.subTest(value=bad):
                config = self.root / 'studio_config.json'
                config.write_text(json.dumps({'mysql': bad}))
                self.assertFalse(read_config(config)['mysql']['enabled'])

    def test_snapshot_does_not_commit_caller_transaction(self):
        self.store.db.execute('BEGIN')
        self.store.db.execute("UPDATE books SET author='Outer transaction' WHERE id=?", (self.book,))
        self.assertEqual(len(self.store.snapshot()['jobs']), 0)
        self.assertTrue(self.store.db.in_transaction)
        self.store.db.rollback()
        self.assertEqual(self.store.book(self.book)['author'], 'Original Author')

    def test_failing_add_job_preserves_unrelated_pending_writes(self):
        snapshots = self.root / 'pronunciation' / 'jobs'
        snapshots.parent.mkdir(parents=True)
        snapshots.write_text('This is not a directory')
        self.store.db.execute('BEGIN')
        self.store.db.execute("UPDATE books SET author='Outer transaction' WHERE id=?", (self.book,))
        with self.assertRaisesRegex(ValueError, 'not a folder'):
            self.store.add_job(self.book, 'Part', [1], 'part_1')
        self.assertTrue(self.store.db.in_transaction)
        self.assertEqual(self.store.book(self.book)['author'], 'Outer transaction')
        self.store.db.rollback()
        self.assertEqual(self.store.book(self.book)['author'], 'Original Author')

    def test_successful_nested_add_job_leaves_outer_transaction_open(self):
        self.store.db.execute('BEGIN')
        job = self.store.add_job(self.book, 'Part', [1], 'part_1')
        self.assertTrue(self.store.db.in_transaction)
        with sqlite3.connect(self.path) as other:
            self.assertIsNone(other.execute('SELECT id FROM jobs WHERE id=?',(job,)).fetchone())
        self.store.db.commit()
        with sqlite3.connect(self.path) as other:
            self.assertIsNotNone(other.execute('SELECT id FROM jobs WHERE id=?',(job,)).fetchone())

    def test_close_commits_pending_sqlite_writes(self):
        self.store.db.execute("UPDATE books SET author='Recent edit' WHERE id=?", (self.book,))
        self.store.close()
        with sqlite3.connect(self.path) as db:
            self.assertEqual(db.execute('SELECT author FROM books WHERE id=?',(self.book,)).fetchone()[0],
                             'Recent edit')

    def test_skip_stale_recovery_leaves_live_job_running(self):
        job = self.store.add_job(self.book, 'Part', [1], 'part1')
        self.store.update_job(job, 'running')
        other = Store(self.path, recover_stale=False)
        self.assertEqual(other.job(job)['status'], 'running')
        other.close()
        self.assertEqual(self.store.job(job)['status'], 'running')

    def test_headless_list_preserves_running_state_and_queue_order(self):
        first = self.store.add_job(self.book, 'One', [1], 'part1')
        second = self.store.add_job(self.book, 'Two', [1], 'part2')
        self.store.update_job(first, 'running')
        self.store.move_job(second, 0)
        with patch.object(headless,'DB_PATH',self.path),redirect_stdout(io.StringIO()) as output:
            self.assertEqual(headless.main(['--list']), 0)
        text = output.getvalue()
        self.assertLess(text.index('Two'),text.index('One'))
        self.assertEqual(self.store.job(first)['status'],'running')

    def test_part_number_separate_for_standard_and_expressive(self):
        first = self.store.add_job(self.book, 'Part 1', [1], 'Book - Part(1)', acting_mode='standard')
        self.assertEqual(self.store.next_part_number(self.book, acting_mode='expressive'),1)
        second = self.store.add_job(self.book, 'Part 1', [1], 'Book - Part(1)', acting_mode='expressive')
        self.assertEqual(self.store.next_part_number(self.book, acting_mode='expressive'),2)
        self.assertEqual(self.store.next_part_number(self.book, acting_mode='standard'),2)
        self.assertEqual(self.store.claimed_sections(self.book,acting_mode='expressive'),{1})
        self.assertNotEqual(first, second)

    def test_retry_receipt_updates_existing_source_job_even_with_new_path(self):
        job = self.store.add_job(self.book, 'Original', [1], 'Book - Part(1)')
        first = self.root / 'old' / 'Book - Part(1).m4b'
        first.parent.mkdir()
        first.write_bytes(b'old verified export'*150)
        self.store.complete_job(job, first)
        new = self.root / 'new' / 'Book - Part(1).m4b'
        new.parent.mkdir()
        new.write_bytes(b'new verified export'*150)
        self.store.update_job(job, 'queued')
        self.store.complete_job(job, new)
        rows=self.store.completed_parts(self.book)
        self.assertEqual(len(rows),1)
        self.assertEqual(rows[0]['output_path'],str(new))

    def test_move_and_clear_nested_failures_dont_rollback_caller_writes(self):
        job=self.store.add_job(self.book,'Part',[1],'Part1')
        self.store.db.execute('BEGIN')
        self.store.db.execute("UPDATE books SET author='Outer transaction' WHERE id=?", (self.book,))
        with self.assertRaisesRegex(ValueError, 'no longer'):
            self.store.move_job(999999,0)
        self.assertEqual(self.store.book(self.book)['author'],'Outer transaction')
        self.store.update_job(job, 'running')
        # update_job commits by design; start a fresh outer transaction
        self.store.db.execute('BEGIN')
        self.store.db.execute("UPDATE books SET author='Outer 2' WHERE id=?", (self.book,))
        with self.assertRaisesRegex(ValueError, 'still running'):
            self.store.clear_all_jobs()
        self.assertEqual(self.store.book(self.book)['author'],'Outer 2')
        self.store.db.rollback()


class ArchiveAndEditorFixTests(unittest.TestCase):
    def test_expressive_archive_passes_mode_to_folder_and_reexport(self):
        with tempfile.TemporaryDirectory() as path:
            root=Path(path); epub=root/'book.epub';epub.write_text('sample')
            folder=root/'expressive';folder.mkdir()
            out=root/'archived.m4b'
            full=folder/'Book_COMPLETE.m4b'
            def run(command, *,check):
                self.assertIn('--acting-mode',command)
                self.assertEqual(command[command.index('--acting-mode')+1],'expressive')
                full.write_bytes(b'fake re-encoded')
            with patch('epub_to_audiobook_v7_studio.book_folder',return_value=folder) as folder_for,\
                 patch('epub_to_audiobook_v7_studio.load_epub',return_value=('Book','Writer',[1])),\
                 patch('audiobookai_fullbook.verify_export_group'),\
                 patch.object(archive,'needs_complete_export',return_value=True),\
                 patch.object(archive.subprocess,'run',side_effect=run),\
                 patch.object(archive,'extract_epub_cover',return_value=None),\
                 patch.object(archive,'archive_copy'),\
                 patch.object(archive,'save_edition'):
                archive.make_archive(epub,folder,[1],None,out,{'title':'Book'},
                                     acting_mode='expressive')
            folder_for.assert_called_once_with(epub,None,'expressive')

    def test_editor_includes_receipt_after_queue_clear(self):
        with tempfile.TemporaryDirectory() as root:
            temp=Path(root)
            store=Store(temp/'store.sqlite3')
            self.addCleanup(store.close)
            source=temp/'book.epub';source.write_text('sample')
            book=store.import_book(source,'Book','Writer',[
                SimpleNamespace(index=1,title='Chapter',text='Sample',source='one.xhtml')])
            job=store.add_job(book,'Part 1',[1],'Book - Part(1)')
            folder=temp/'voices'/'performances'/'expressive'
            folder.mkdir(parents=True)
            output=folder/'Book - Part(1).m4b';output.write_bytes(b'data'*300)
            store.complete_job(job,output)
            store.prune_recorded_jobs()
            harness=SimpleNamespace(store=store)
            saved=gui.Studio.completed_jobs_for_editor(harness,book,folder)
            self.assertEqual(len(saved),1)
            self.assertEqual(saved[0]['output_name'],output.stem)
            self.assertEqual(json.loads(saved[0]['indices']),[1])
            self.assertEqual(saved[0]['status'],'complete')


class MobileFixTests(unittest.TestCase):
    def test_bridge_waits_for_started_mutation_to_complete_instead_of_timing_out(self):
        bridge=mobile.MobileBridge()
        answer=[]
        worker=threading.Thread(target=lambda: answer.append(bridge.call('add',{},timeout=.02)))
        worker.start()
        deadline=time.monotonic()+2
        while bridge.pending.empty() and time.monotonic()<deadline: time.sleep(.001)
        bridge.process(lambda *_: (time.sleep(.08),{'result':'created once'})[1])
        worker.join(timeout=1)
        self.assertFalse(worker.is_alive())
        self.assertEqual(answer,[{'result':'created once'}])

    def test_cancel_pending_request_on_dashboard_shutdown(self):
        bridge=mobile.MobileBridge()
        errors=[]
        worker=threading.Thread(target=lambda: (
            bridge.call('upload',{},timeout=2)) )
        # Capture error in a wrapper rather than let a thread traceback hide it.
        def send():
            try: bridge.call('upload',{},timeout=2)
            except ValueError as exc: errors.append(str(exc))
        worker=threading.Thread(target=send);worker.start()
        deadline=time.monotonic()+2
        while bridge.pending.empty() and time.monotonic()<deadline: time.sleep(.001)
        bridge.cancel_pending()
        worker.join(timeout=1)
        self.assertFalse(worker.is_alive())
        self.assertIn('Dashboard stopped',errors[0])

    def test_unstarted_timeout_is_safe_to_retry(self):
        bridge=mobile.MobileBridge()
        with self.assertRaisesRegex(RuntimeError,'cancelled before execution'):
            bridge.call('pause',{},timeout=.005)
        called=[]
        bridge.process(lambda *a: called.append(a))
        self.assertEqual(called,[])


class QualityAndPronunciationFixTests(unittest.TestCase):
    def test_unicode_whitespace_pronunciation_does_not_crash(self):
        rules=[{'term':'Aes\u00a0Sedai','spoken':'Ayss Seh-die'}]
        self.assertEqual(pron.apply_rules('An Aes Sedai arrives.',rules),
                         'An Ayss Seh-die arrives.')
        self.assertEqual(pron.apply_rules('An Aes\u00a0Sedai arrives.',rules),
                         'An Ayss Seh-die arrives.')
        with self.assertRaises(ValueError):
            pron.validate_rule('bad\nterm','test')

    def test_integrity_missing_wav_is_not_invalid_report(self):
        with tempfile.TemporaryDirectory() as d:
            folder=Path(d);(folder/'chapters').mkdir()
            (folder/'manifest.json').write_text(json.dumps({'chapters':{
                '1':{'samples':100,'sample_rate':24000},
                '2':{'samples':100,'sample_rate':24000}}}))
            (folder/'chapters/0001.wav').write_bytes(b'RIFF'+b'a'*120)
            (folder/'quality_reports').mkdir()
            (folder/'quality_reports/summary.json').write_text(json.dumps({
                'timestamp':time.time()+100,'chapters':[1,2],'issues':[]}))
            result=integrity.inspect_book(folder,2,expected=[1,2])
            self.assertEqual(result['qc'],'Missing recorded audio')
            self.assertFalse(result['wav_ready'])

    def test_reviewed_issue_does_not_block_quality_preflight(self):
        with tempfile.TemporaryDirectory() as d:
            folder=Path(d);(folder/'chapters').mkdir()
            (folder/'chapters/0001.wav').write_bytes(b'RIFF'+b'a'*120)
            issue={'chapter':1,'severity':'error','code':'silence','chunk':None,
                   'path':str(folder/'chapters/0001.wav'),'message':'Long silence'}
            (folder/'quality_reports').mkdir()
            (folder/'quality_reports/summary.json').write_text(json.dumps({
                'timestamp':time.time()+100,'chapters':[1],
                'issues':[issue],'errors':1,'warnings':0}))
            self.assertIn('1 errors',quality.quality_preflight(folder,[1]))
            review.set_status(issue,folder,'dismissed')
            self.assertIn('no hard errors',quality.quality_preflight(folder,[1]))
            (folder/'manifest.json').write_text(json.dumps({'chapters':{
                '1':{'samples':100,'sample_rate':24000}}}))
            result=integrity.inspect_book(folder,1,expected=[1])
            self.assertEqual(result['qc'],'Passed')


class CodeStructureTests(unittest.TestCase):
    def test_only_one_authoritative_v7_chunking_definition(self):
        path=Path(__file__).resolve().parents[1]/'scripts'/'test_five_minutes_v7_smooth.py'
        module=ast.parse(path.read_text())
        funcs=[x.name for x in module.body if isinstance(x,ast.FunctionDef)]
        for name in ('split_long','make_chunks','target_pause','assemble'):
            self.assertEqual(funcs.count(name),1,name)
        self.assertEqual(funcs[-1],'main')


class LiveQtSmokeTests(unittest.TestCase):
    def test_settings_restore_metadata_backup_controls_and_fail_open(self):
        from PySide6.QtWidgets import QApplication, QPushButton
        from audiobookai_storage import default_config
        from audiobookai_ntfy import ensure_config
        with tempfile.TemporaryDirectory() as d:
            folder=Path(d)
            ensure_config(folder/'ntfy.json',enabled=False)
            config=default_config()
            app=QApplication.instance() or QApplication([])
            class Settings:
                values={}
                def __init__(self,*args):pass
                def value(self,key,default=None,type=None):
                    v=self.values.get(key,default)
                    return type(v) if v is not None and type else v
                def setValue(self,key,value):self.values[key]=value
            with patch.object(gui,'DB_FILE',folder/'test.sqlite3'),                 patch.object(gui,'NTFY_CONFIG_PATH',folder/'ntfy.json'),                 patch.object(gui,'QSettings',Settings),                 patch.object(gui,'read_config',return_value=config),                 patch.object(gui,'lookup_password',return_value=None):
                studio=gui.Studio()
                buttons={b.text() for b in studio.findChildren(QPushButton)}
                for button in ('Backup metadata now','Verify latest backup','Open backups folder'):
                    self.assertIn(button,buttons)
                studio.backup_dir=folder/'backups'
                with patch.object(gui,'latest_backup',return_value=None),                     patch.object(gui,'create_metadata_backup',
                                  return_value=folder/'backups'/'success.zip'):
                    studio.maybe_auto_metadata_backup()
                    self.assertIn('success.zip',studio.backup_status.text())
                    del studio.backup_status
                    # A missing UI label must never abort the converter.
                    studio.maybe_auto_metadata_backup()
                studio.close()

    def test_selected_part_preempts_auto_quality_scan(self):
        cache={'status':'queued','id':7}
        class StoreStub:
            def job(self,jobid):return cache if jobid==7 else None
        scanner=Mock()
        commands=[]
        state=SimpleNamespace(
            store=StoreStub(),selected_job_id=lambda:7,proc=None,
            quality_proc=scanner,auto_quality_scanning=True,
            run_queue=False,auto_quality_restart_selected=None,
            clear_storage_wait=lambda:None,stop_quality=lambda:commands.append('stop_qc'),
            write_log=commands.append,queue_mode_status=SimpleNamespace(setText=lambda x:None))
        gui.Studio._run_selected_part(state,single_part=False)
        self.assertEqual(commands,['stop_qc'])
        self.assertEqual(state.auto_quality_restart_selected,(7,False))
        self.assertTrue(state.run_queue)


class ConverterProcessDetectionTests(unittest.TestCase):
    def test_python_c_filename_argument_is_not_a_running_converter(self):
        import subprocess
        import audiobookai_cache_manager as cache
        child=subprocess.Popen([sys.executable,'-c','import time;time.sleep(4)',
                                'epub_to_audiobook_v7_studio.py','convert'])
        try:
            with patch.object(cache.Path,'iterdir',return_value=[Path(f'/proc/{child.pid}')]):
                self.assertFalse(cache._active_conversion())
            with patch.object(headless.Path,'iterdir',return_value=[Path(f'/proc/{child.pid}')]):
                self.assertEqual(headless.active_converter_pids(),[])
        finally:
            child.terminate()
            child.wait(timeout=5)


if __name__=='__main__':
    unittest.main()
