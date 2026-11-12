"""Automatic end-of-queue QC and persistent review state regression tests.

Offline, small synthetic fixtures; no TTS model, network, SQLite user data.
"""
from __future__ import annotations
import json
import os
from pathlib import Path
import sys
import tempfile
import time
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

import numpy as np
import soundfile as sf

SCRIPTS=Path(__file__).resolve().parents[1]/'scripts'
sys.path.insert(0,str(SCRIPTS))
from audiobookai_quality import scan_book
from audiobookai_quality_review import (
    issue_id, issue_revision, load_states, counts, set_status, status_of
)
import audiobook_gui_v2 as gui
from audiobookai_ntfy import DEFAULT_FLAGS, ensure_config, read_config, NtfyPublisher


class ReviewStateTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.folder=Path(self.temp.name)/'voice'
        (self.folder/'chapters').mkdir(parents=True)
        self.wav=self.folder/'chapters/0003.wav'
        sf.write(self.wav,np.ones(2400,dtype=np.float32)*0.4,24000)
        self.issue={'severity':'warning','chapter':3,'chunk':1,'code':'short_for_text',
                    'path':str(self.wav),'message':'Recording is short'}
        self.report={'issues':[self.issue]}
    def test_mark_reviewed_and_dismissed_persists_on_disk(self):
        self.assertEqual(status_of(self.issue,self.folder),'needs_review')
        set_status(self.issue,self.folder,'reviewed')
        self.assertEqual(status_of(self.issue,self.folder),'reviewed')
        self.assertEqual(counts(self.report,self.folder)['reviewed'],1)
        set_status(self.issue,self.folder,'dismissed')
        self.assertEqual(status_of(self.issue,self.folder),'dismissed')
        self.assertEqual(counts(self.report,self.folder)['needs_review'],0)
        self.assertEqual((self.folder/'quality_reports/review_state.json').is_file(),True)
        set_status(self.issue,self.folder,'needs_review')
        self.assertEqual(status_of(self.issue,self.folder),'needs_review')
        self.assertEqual(load_states(self.folder),{})
    def test_audio_revision_invalidates_dismissal(self):
        set_status(self.issue,self.folder,'dismissed')
        self.assertEqual(status_of(self.issue,self.folder),'dismissed')
        # Regenerated audio at the same path must be independently reviewed.
        sf.write(self.wav,np.ones(4800,dtype=np.float32)*0.1,24000)
        self.assertEqual(status_of(self.issue,self.folder),'needs_review')
    def test_review_state_is_stable_when_message_wording_changes(self):
        set_status(self.issue,self.folder,'reviewed')
        altered={**self.issue,'message':'Audio seems shorter than text'}
        self.assertEqual(issue_id(altered),issue_id(self.issue))
        self.assertEqual(status_of(altered,self.folder),'reviewed')
    def test_invalid_status_does_not_alter_data(self):
        with self.assertRaises(ValueError):
            set_status(self.issue,self.folder,'delete_all')
        self.assertEqual(load_states(self.folder),{})
    def test_scanner_output_survives_review_decision(self):
        sf.write(self.wav,np.ones(2400,dtype=np.float32)*0.4,24000)
        (self.folder/'manifest.json').write_text(json.dumps({'chapters':{
            '3':{'samples':2400,'sample_rate':24000}}}))
        result=scan_book(self.folder,chapters=[3])
        self.assertTrue(result['issues'])
        set_status(result['issues'][0],self.folder,'dismissed')
        later=scan_book(self.folder,chapters=[3])
        self.assertEqual(status_of(later['issues'][0],self.folder),'dismissed')
        self.assertTrue(self.wav.is_file())


class SettingsMigrationTests(unittest.TestCase):
    def test_v212_ntfy_topic_and_choices_remain_when_new_quality_event_added(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'ntfy.json'
            old=ensure_config(path,enabled=True)
            legacy={'topic':old['topic'],'enabled':True,
                    'events':{'queue_complete':False,'generation_failed':True,
                              'storage_unavailable':False}}
            path.write_text(json.dumps(legacy))
            loaded=read_config(path)
            self.assertEqual(loaded['topic'],legacy['topic'])
            self.assertFalse(loaded['events']['queue_complete'])
            self.assertFalse(loaded['events']['storage_unavailable'])
            self.assertTrue(loaded['events']['quality_attention'])
            pub=NtfyPublisher(path,sender=lambda *_: None)
            pub.update(event='quality_attention',checked=False)
            self.assertFalse(read_config(path)['events']['quality_attention'])
            self.assertEqual(read_config(path)['topic'],legacy['topic'])


class AutoPlannerTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.folder=Path(self.temp.name)/'voice'/'performances'/'expressive'
        (self.folder/'chapters').mkdir(parents=True)
        self.folder.joinpath('manifest.json').write_text(json.dumps({'chapters':{
            '1':{'sample_rate':24000, 'samples':2400},
            '2':{'sample_rate':24000, 'samples':2400}}}))
        self.folder.joinpath('chapters/0001.wav').write_bytes(b'placeholder')
        self.calls=[]
        self.h=SimpleNamespace(
            proc=None,quality_proc=None, run_queue=False, full_export_proc=None,
            auto_quality_completed={},auto_quality_pending=[],auto_quality_scanning=False,
            auto_quality_attention=0,auto_quality_failed=0,
            quality_auto=SimpleNamespace(isChecked=lambda:True),
            quality_deep=SimpleNamespace(isChecked=lambda:False),
            write_log=lambda msg: self.calls.append(('log',msg)),
            notify_ntfy=lambda event:self.calls.append(('ntfy',event)),
            statusBar=lambda: SimpleNamespace(showMessage=lambda x:self.calls.append(('status',x))),
        )
        self.h.quality_chapters=lambda folder,req:gui.Studio.quality_chapters(self.h,folder,req)
        self.h.quality_next_automatic=lambda:gui.Studio.quality_next_automatic(self.h)
        self.h.quality_launch=lambda folder,chs,req,**kw:self.calls.append(
            ('scan',Path(folder),list(chs),set(req),kw)) or True

    def test_only_verified_jobs_schedule_auto_scans_after_queue_drains(self):
        self.h.converter=SimpleNamespace(book_folder=lambda *args:self.folder)
        job={'book_path':'/books/book.epub','voice_path':None,'acting_mode':'expressive',
             'indices':'[1,2]'}
        gui.Studio.quality_record_completion(self.h,job)
        self.assertEqual(self.calls,[])
        gui.Studio.quality_queue_drained(self.h)
        self.assertEqual(self.calls[0][0],'scan')
        self.assertEqual(self.calls[0][2],[1,2])
        self.assertEqual(self.calls[0][3],{1,2})
        self.assertTrue(self.calls[0][4]['automatic'])
        self.assertFalse(self.calls[0][4]['deep'])
        self.assertFalse(self.h.auto_quality_completed)

    def test_automatic_scan_never_starts_while_narration_runs(self):
        self.h.auto_quality_completed={str(self.folder):{1}}
        self.h.proc=object()
        gui.Studio.quality_queue_drained(self.h)
        self.assertFalse(self.calls)
        self.assertEqual(len(self.h.auto_quality_pending),1)
        self.h.proc=None
        gui.Studio.quality_next_automatic(self.h)
        self.assertEqual(self.calls[0][0],'scan')

    def test_no_auto_qa_when_disabled(self):
        self.h.quality_auto=SimpleNamespace(isChecked=lambda:False)
        self.h.converter=SimpleNamespace(book_folder=lambda *args:self.folder)
        job={'book_path':'/books/book.epub','voice_path':None,'acting_mode':'expressive','indices':'[1]'}
        gui.Studio.quality_record_completion(self.h,job)
        gui.Studio.quality_queue_drained(self.h)
        self.assertEqual(self.calls,[])

    def test_single_consolidated_alert_after_multiple_editions(self):
        self.h.auto_quality_pending=[]
        self.h.auto_quality_attention=5
        gui.Studio.quality_next_automatic(self.h)
        self.assertEqual([x for x in self.calls if x[0]=='ntfy'],[('ntfy','quality_attention')])
        self.assertEqual(self.h.auto_quality_attention,0)


if __name__=='__main__':
    unittest.main()
