"""Mobile dashboard API, Qt bridge and safe remote queue actions (no GPU)."""
from __future__ import annotations

import io
import json
from pathlib import Path
import socket
import sys
import tempfile
import threading
import time
import unittest
from types import SimpleNamespace
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from unittest.mock import patch
import zipfile

SCRIPTS = Path(__file__).resolve().parents[1] / 'scripts'
sys.path.insert(0, str(SCRIPTS))
from audiobookai_mobile import MobileBridge, MobileHTTPServer
from audiobookai_storage import Store


def free_port():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(('127.0.0.1', 0))
        return s.getsockname()[1]


class HTTPTransportTests(unittest.TestCase):
    def setUp(self):
        self.bridge = SimpleNamespace(call=self.dispatch)
        self.received = []
        self.token = 'test-secret-key-with-more-than-24-characters'
        self.port = free_port()
        self.server = MobileHTTPServer(self.bridge, self.token, self.port)
        self.server.start()
        self.addCleanup(self.server.stop)
        self.base = f'http://127.0.0.1:{self.port}'

    def dispatch(self, action, data):
        self.received.append((action, data))
        return {'action': action}

    def fetch(self, path, *, token=None, method='GET', content=None,
              content_type='application/json', headers=None):
        props = {'Authorization': 'Bearer ' + (token or self.token)}
        if headers:
            props.update(headers)
        if content is not None:
            props['Content-Type'] = content_type
        req = Request(self.base + path, data=content, method=method, headers=props)
        try:
            with urlopen(req, timeout=5) as r:
                return r.status, r.read(), r.headers
        except HTTPError as e:
            return e.code, e.read(), e.headers

    def test_html_css_js_are_local_and_safely_headered(self):
        for path in ('/', '/index.html', '/mobile.js', '/mobile.css'):
            status, data, headers = self.fetch(path)
            self.assertEqual(status, 200)
            self.assertGreater(len(data), 100)
            self.assertIn("default-src 'none'", headers['Content-Security-Policy'])
            self.assertEqual(headers['X-Frame-Options'], 'DENY')
            self.assertEqual(headers['Cache-Control'], 'no-store')

    def test_api_requires_key_and_routes_status(self):
        status, data, _ = self.fetch('/api/status', token='bad key')
        self.assertEqual(status, 401)
        self.assertEqual(self.received, [])
        status, data, _ = self.fetch('/api/status')
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(data), {'action': 'status'})
        self.assertEqual(self.received, [('status', {})])

    def test_post_requires_valid_key_valid_json_and_same_origin(self):
        body = json.dumps({'command': 'pause'}).encode()
        status, _, _ = self.fetch('/api/action', method='POST', content=body, token='invalid')
        self.assertEqual(status, 401)
        status, _, _ = self.fetch('/api/action', method='POST', content=body,
                                  headers={'Origin': 'https://evil.example'})
        self.assertEqual(status, 403)
        status, _, _ = self.fetch('/api/action', method='POST', content=body,
                                  headers={'Origin': self.base})
        self.assertEqual(status, 200)
        self.assertEqual(self.received, [('pause', {'command': 'pause'})])
        status, _, _ = self.fetch('/api/action', method='POST', content=b'[]')
        self.assertEqual(status, 400)
        status, _, _ = self.fetch('/api/action', method='POST', content=b'{' + b'x' * (48*1024))
        self.assertEqual(status, 413)

    def test_upload_binary_forwards_bytes_not_paths(self):
        payload = b'PK-test-file'
        status, data, _ = self.fetch('/api/upload',method='POST',content=payload,
            content_type='application/epub+zip',headers={'X-Filename':'some%20book.epub'})
        self.assertEqual(status, 200)
        self.assertEqual(self.received, [('upload', {'content':payload,
                                                    'filename':'some%20book.epub'})])
        status, _, _ = self.fetch('/api/upload',method='POST',content=b'x',
            content_type='text/plain')
        self.assertEqual(status, 400)

    def test_api_does_not_allow_arbitrary_endpoints(self):
        status, _, _ = self.fetch('/api/private')
        self.assertEqual(status, 404)
        status, _, _ = self.fetch('/../../etc/passwd')
        self.assertEqual(status, 404)


class BridgeTests(unittest.TestCase):
    def test_calls_execute_on_processing_thread(self):
        bridge = MobileBridge()
        caller = []
        received = []
        def invoke():
            caller.append(bridge.call('pause', {}))
        thread = threading.Thread(target=invoke)
        thread.start()
        for _ in range(100):
            if not bridge.pending.empty(): break
            time.sleep(.01)
        original_thread = threading.get_ident()
        bridge.process(lambda operation,payload: (
            received.append(threading.get_ident()), {'result':operation})[1])
        thread.join(timeout=3)
        self.assertEqual(received, [original_thread])
        self.assertEqual(caller, [{'result': 'pause'}])

    def test_error_does_not_leak_unhandled_exception(self):
        bridge = MobileBridge()
        errors=[]
        def invoke():
            try: bridge.call('test', {})
            except ValueError as e: errors.append(str(e))
        thread=threading.Thread(target=invoke);thread.start()
        for _ in range(100):
            if not bridge.pending.empty():break
            time.sleep(.01)
        bridge.process(lambda *a: (_ for _ in ()).throw(ValueError('Unknown command')))
        thread.join(timeout=3)
        self.assertEqual(errors,['Unknown command'])


class GUIActionsTests(unittest.TestCase):
    def setUp(self):
        import audiobook_gui_v2 as gui
        self.gui = gui
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name)
        self.store=Store(self.root/'test.sqlite3')
        self.addCleanup(self.store.close)
        self.epub=self.root/'book.epub'
        self.epub.write_bytes(b'epub test fixture')
        self.book_id=self.store.import_book(
            self.epub,'Queue test','Author',[
                SimpleNamespace(index=i,title=f'Chapter {i}',
                                source=f'{i}.xhtml',text=f'This is chapter {i}')
                for i in range(1,9)])
        self.calls=[]
        fake_converter=SimpleNamespace(
            book_folder=lambda *args: self.root/'output')
        class Harness:
            def __init__(inner):
                inner.store=self.store
                inner.converter=fake_converter
                inner.proc=None
                inner.active_job_id=None
                inner.run_queue=False
                inner.current_section=None
                inner.storage_waiting=False
                inner.total_chunks=0
                inner.done_chunks={}
                inner.stat_eta=SimpleNamespace(text=lambda:'ETA: 10m')
                inner.diagnostic_lines=[]
                inner.refresh_jobs=lambda:self.calls.append('refresh')
                inner.refresh_saved_parts=lambda:self.calls.append('parts')
                inner.refresh_review=lambda:self.calls.append('review')
                inner.clear_storage_wait=lambda:self.calls.append('cancel wait')
                inner.start_queue=lambda:self.calls.append('start')
                inner.pause_after_part=lambda:self.calls.append('pause')
                inner.start_job=lambda job_id:self.calls.append(('run',job_id))
                inner.check_storage_and_resume=lambda:self.calls.append('check storage')
                inner.book_cache={}
                inner.refresh_books=lambda **kw: self.calls.append('books')
            def mobile_add_jobs(inner,payload):
                return gui.Studio.mobile_add_jobs(inner,payload)
            def mobile_book_snapshot(inner,payload):
                return gui.Studio.mobile_book_snapshot(inner,payload)
            def mobile_upload_epub(inner,payload):
                return gui.Studio.mobile_upload_epub(inner,payload)
        self.app=Harness()
        self.snaps=self.root/'snapshots'
        self.snaps.mkdir()
        self.snapshot_patch=patch.object(gui, 'snapshot_for_job',
                side_effect=lambda jid,p:(self.snaps/f'job_{jid}.json').write_text('frozen'))
        self.snapshot_patch.start()
        self.addCleanup(self.snapshot_patch.stop)
        self.freeze_patch=patch.object(gui, 'freeze_voice', return_value=None)
        self.freeze_patch.start()
        self.addCleanup(self.freeze_patch.stop)

    def test_add_parts_respects_batch_count_and_pronunciation(self):
        result=self.app.mobile_add_jobs(dict(
            book_id=self.book_id,ranges='1-8',batch_size=3,
            voice='default',acting_mode='expressive'))
        ids=result['job_ids']
        self.assertEqual(len(ids),3)
        self.assertEqual([json.loads(self.store.job(i)['indices']) for i in ids],
                         [[1,2,3],[4,5,6],[7,8]])
        self.assertTrue(all(self.store.job(i)['acting_mode']=='expressive' for i in ids))
        self.assertTrue(all(self.store.job(i)['pronunciation_required'] for i in ids))
        self.assertTrue(all((self.snaps/f'job_{i}.json').is_file() for i in ids))
        self.assertEqual([r['id'] for r in self.store.jobs()], ids)
        # A separate expressive/standard narrator edition must be allowed.
        second=self.app.mobile_add_jobs(dict(book_id=self.book_id,ranges='1-2',
            batch_size=2,voice='default',acting_mode='standard'))
        self.assertEqual(len(second['job_ids']),1)
        with self.assertRaisesRegex(ValueError,'already have jobs'):
            self.app.mobile_add_jobs(dict(book_id=self.book_id,ranges='1-2',
                batch_size=2,voice='default',acting_mode='expressive'))
        self.assertEqual(len(self.store.jobs()),4)

    def test_unqueued_story_selection_never_duplicates_finished(self):
        first=self.store.add_job(self.book_id, 'Original', [2,3], 'part_original')
        result=self.app.mobile_add_jobs(dict(
            book_id=self.book_id,all_unqueued=True,batch_size=2,
            voice='default',acting_mode='standard'))
        all_new=[k for jid in result['job_ids'] for k in json.loads(self.store.job(jid)['indices'])]
        self.assertEqual(all_new, [1,4,5,6,7,8])
        details=self.app.mobile_book_snapshot({'book_id':self.book_id})
        claimed=[row['index'] for row in details['sections'] if row['claimed']]
        self.assertEqual(claimed, list(range(1,9)))
        self.assertEqual(self.store.job(first)['status'],'queued')

    def test_remote_dispatch_pause_and_clear_all(self):
        dispatch=lambda cmd,args={}: self.gui.Studio.handle_mobile_request(self.app, cmd, args)
        self.assertIn('start',dispatch('start')['message'].lower())
        self.assertIn('continuation disabled',dispatch('pause')['message'].lower())
        self.assertEqual(self.calls[:2],['start','pause'])
        ids=self.app.mobile_add_jobs(dict(book_id=self.book_id,ranges='1-3',
            batch_size=1,voice='default',acting_mode='expressive'))['job_ids']
        self.assertEqual(dispatch('move',{'job_id':ids[0],'index':2})['message'],
                         'Queue order saved.')
        self.assertEqual([r['id'] for r in self.store.jobs()],
                         [ids[1],ids[2],ids[0]])
        with self.assertRaisesRegex(ValueError,'CLEAR QUEUE'):
            dispatch('clear_all',{})
        # Stub the headless detection for offline test.
        with patch.object(self.gui,'headless_runner_active',return_value=False):
            result=dispatch('clear_all',{'confirmation':'CLEAR QUEUE'})
        self.assertIn('3',result['message'])
        self.assertEqual(self.store.jobs(),[])

    def test_pause_after_part_cancels_late_queue_advance_callback(self):
        # If a completion callback has already been scheduled, Pausing before
        # the 350ms deadline must still prevent a new part from starting.
        self.app.run_queue = True
        self.gui.Studio.continue_queue_if_requested(self.app)
        self.assertEqual(self.calls, ['start'])
        self.calls.clear()
        self.app.run_queue = False  # desktop or remote Pause after part
        self.gui.Studio.continue_queue_if_requested(self.app)
        self.assertEqual(self.calls, [])
        self.app.run_queue = True
        self.app.proc = object()
        self.gui.Studio.continue_queue_if_requested(self.app)
        self.assertEqual(self.calls, [])

    def test_remote_does_not_clear_while_gui_conversion_running(self):
        self.store.add_job(self.book_id,'Job',[1],'Part 1')
        self.app.proc=object()
        with patch.object(self.gui,'headless_runner_active',return_value=False):
            with self.assertRaisesRegex(ValueError, 'Stop generation'):
                self.gui.Studio.handle_mobile_request(self.app,'clear_all',
                    {'confirmation':'CLEAR QUEUE'})
        self.assertEqual(len(self.store.jobs()),1)

    def test_bad_import_parser_does_not_leave_uploaded_file(self):
        bytes_file=io.BytesIO()
        with zipfile.ZipFile(bytes_file,'w') as z:
            z.writestr('META-INF/container.xml','<container/>')
        self.app.converter.load_epub=lambda path: (
            (_ for _ in ()).throw(ValueError('Could not parse EPUB')))
        with patch.object(self.gui, 'ROOT', self.root):
            with self.assertRaisesRegex(ValueError, 'Could not parse EPUB'):
                self.app.mobile_upload_epub({
                    'filename':'broken.epub','content':bytes_file.getvalue()})
        self.assertEqual(list((self.root / 'books').glob('*.epub')), [])
        self.assertEqual(len(self.store.books()), 1)

    def test_uploaded_epub_import_is_unique_and_rejects_non_epub(self):
        data=io.BytesIO()
        with zipfile.ZipFile(data,'w') as z:
            z.writestr('META-INF/container.xml','<container />')
            z.writestr('mimetype','application/epub+zip')
        blob=data.getvalue()
        self.app.converter.load_epub=lambda p: (
            'Imported novel','Author',[SimpleNamespace(index=1,title='One',
            source='one.xhtml',text='Hello there')])
        with patch.object(self.gui,'ROOT',self.root):
            result=self.app.mobile_upload_epub({
                'filename':'Book%20Name.epub','content':blob})
        self.assertEqual(result['book_id'],self.book_id+1)
        imported=self.store.book(result['book_id'])
        self.assertTrue(Path(imported['path']).is_file())
        self.assertEqual(Path(imported['path']).read_bytes(),blob)
        with patch.object(self.gui,'ROOT',self.root):
            with self.assertRaises(ValueError):
                self.app.mobile_upload_epub({'filename':'../bad.epub','content':blob})
            with self.assertRaises(zipfile.BadZipFile):
                self.app.mobile_upload_epub({'filename':'bad.epub','content':b'not an epub'})


if __name__=='__main__':
    unittest.main()
