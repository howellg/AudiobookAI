"""Cache catalog and guarded cleanup: no GPU/audio, all deletes inside temp dirs."""
from pathlib import Path
from unittest.mock import patch
import json
import os
import sqlite3
import stat
import tempfile
import time
import unittest
import sys

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
import audiobookai_cache_manager as cm


class CacheManagerTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root=Path(self.tmp.name)
        self.output=root/'output';self.output.mkdir()
        self.cache=self.output/'narration_cache_v2';self.cache.mkdir()
        self.work=self.output/'cache_manager'
        self.catalog=self.work/'catalog.sqlite3'
        self.voice=root/'voices'/'british_narrator.wav'
        self.voice.parent.mkdir()
        self.voice.write_bytes(b'voice-fingerprint-fixture')
        self.stat=self.voice.stat()
        self.wavs={}
        self.older=time.time()-160*86400

    def add(self, text, mode='standard', *, indexed=False, tracked=True,
            dialogue=None, age=150):
        dialogue = (text.startswith('"') if dialogue is None else dialogue)
        ex,cfg=cm._delivery(mode,dialogue)
        name=cm.key_for(text,self.voice,self.stat.st_size,
                        self.stat.st_mtime_ns,ex,cfg)
        file=self.cache/name
        file.write_bytes(b'RIFF' + b'fake-audio'*100)
        age_time=time.time()-age*86400
        os.utime(file,(age_time,age_time))
        self.wavs[text]=file
        if tracked:
            with patch.object(cm,'CATALOG',self.catalog):
                cm.note_chunk(file,text,self.voice,mode,dialogue)
            with cm._db(self.catalog) as db:
                db.execute('UPDATE entries SET last_seen=? WHERE name=?',
                           (age_time,name))
        if indexed:
            folder=self.output/'audiobooks'/'book'/'performances'/mode
            (folder/'chunk_indexes').mkdir(parents=True,exist_ok=True)
            (folder/'manifest.json').write_text(json.dumps({
                'voice_path':str(self.voice),'acting_mode':mode}))
            idx=folder/'chunk_indexes'/f'{len(list(folder.glob("chunk_indexes/*.json")))+1:04d}.json'
            idx.write_text(json.dumps({'chunks':[{'text':text,'dialogue':dialogue,
                'cache_file':str(file.resolve()),'number':1}]}))
        return file

    def scan(self,scope='dialogue',days=90):
        return cm.inventory(cache=self.cache,output=self.output,
                            work=self.work,catalog=self.catalog,
                            scope=scope,days=days)

    def delete(self,report):
        return cm.delete_preview(report['id'],cache=self.cache,
                                 output=self.output,work=self.work,
                                 catalog=self.catalog)

    def test_validates_generation_hash_and_stores_only_hash_of_text(self):
        file=self.add('"This was recorded before the upgrade."')
        with cm._db(self.catalog) as db:
            row=db.execute('SELECT * FROM entries').fetchone()
        self.assertEqual(row[0],file.name)
        self.assertEqual(row[7],1)
        self.assertEqual(row[-1],'engine')
        raw=self.catalog.read_bytes()
        self.assertNotIn(b'This was recorded',raw)
        self.assertNotIn(b'upgrade',raw)
        self.assertEqual(row[1],str(self.voice.resolve()))

    def test_preview_protects_indexed_and_unclassified_files(self):
        old=self.add('"Old dialogue sample."')
        active=self.add('"Active chapter quote."',
                        indexed=True)
        prose=self.add('Narration is a normal prose paragraph.')
        other=self.cache/('f'*24+'.wav')
        other.write_bytes(b'unclassified')
        result=self.scan()
        self.assertEqual(result['candidate_files'],1)
        self.assertEqual(result['candidate_bytes'],old.stat().st_size)
        self.assertEqual(result['categories']['unknown']['files'],1)
        self.assertEqual(result['categories']['protected_index']['files'],1)
        self.assertTrue(active.exists())
        self.assertTrue(prose.exists())
        preview=cm._load_json(self.work/'cleanup_preview.json')
        self.assertEqual([x['name'] for x in preview['items']],[old.name])
        self.assertEqual(stat.S_IMODE((self.work/'cleanup_preview.json').stat().st_mode),0o600)

    def test_delete_removes_only_exact_eligible_matches_after_confirmation_token(self):
        old=self.add('"Old dialogue to clean."')
        protected=self.add('"Chapter still indexed."',indexed=True)
        unknown=self.cache/('e'*24+'.wav')
        unknown.write_bytes(b'keep')
        result=self.scan()
        with self.assertRaisesRegex(RuntimeError,'token'):
            cm.delete_preview('not-the-token',cache=self.cache,
                              output=self.output,catalog=self.catalog,work=self.work)
        assert old.exists()
        outcome=self.delete(result)
        self.assertEqual(outcome['deleted_files'],1)
        self.assertFalse(old.exists())
        self.assertTrue(protected.exists())
        self.assertTrue(unknown.exists())
        self.assertFalse((self.work/'cleanup_preview.json').exists())

    def test_changed_file_aborts_before_removing_any_candidate(self):
        a=self.add('"First old dialogue."')
        b=self.add('"Second old dialogue."')
        result=self.scan()
        b.write_bytes(b'changed')
        with self.assertRaisesRegex(RuntimeError,'changed'):
            self.delete(result)
        self.assertTrue(a.exists())
        self.assertTrue(b.exists())

    def test_new_index_reference_aborts_cleanup(self):
        old=self.add('"Referenced after preview."')
        report=self.scan()
        self.add('"Referenced after preview."',indexed=True,tracked=False)
        with self.assertRaisesRegex(RuntimeError,'referenced'):
            self.delete(report)
        self.assertTrue(old.exists())

    def test_recent_cache_hit_is_never_stale(self):
        file=self.add('"Recently reused."')
        with patch.object(cm,'CATALOG',self.catalog):
            cm.note_chunk(file,'"Recently reused."',self.voice,'standard',True)
        result=self.scan()
        self.assertEqual(result['candidate_files'],0)

    def test_legacy_default_never_selects_expressive_or_prose_or_new_files(self):
        old=self.add('"Historical standard dialogue."',age=180)
        expressive=self.add('"Expressive performance."','expressive',age=180)
        prose=self.add('Normal narration.',age=180)
        new=self.add('"Recent standard dialogue."',age=12)
        report=self.scan(scope='legacy',days=1)
        preview=cm._load_json(self.work/'cleanup_preview.json')
        self.assertEqual([r['name'] for r in preview['items']],[old.name])
        self.assertEqual(report['candidate_files'],1)
        self.assertTrue(expressive.exists() and prose.exists() and new.exists())

    def test_unclassified_and_symlinks_never_delete_even_in_all_scope(self):
        self.add('Verified stale prose.',age=150)
        unclassified=self.cache/('a'*24+'.wav');unclassified.write_bytes(b'unknown')
        outside=self.output/'important.m4b';outside.write_bytes(b'important')
        symlink=self.cache/('b'*24+'.wav')
        symlink.symlink_to(outside)
        result=self.scan(scope='all')
        self.assertEqual(result['candidate_files'],1)
        self.delete(result)
        self.assertTrue(unclassified.exists())
        self.assertTrue(symlink.is_symlink())
        self.assertEqual(outside.read_bytes(),b'important')

    def test_index_backfill_requires_exact_matching_signature(self):
        file=self.add('"Historical indexed recording."',indexed=True,tracked=False)
        self.scan()
        with cm._db(self.catalog) as db:
            row=db.execute('SELECT source,dialogue,exaggeration FROM entries WHERE name=?',
                           (file.name,)).fetchone()
        self.assertEqual(row,('index',1,.35))
        self.assertTrue(file.exists())

    def test_preflight_rejects_unavailable_symlink_before_preview(self):
        missing=self.output/'missing-drive'
        link=self.output/'broken-link';link.symlink_to(missing,target_is_directory=True)
        with self.assertRaisesRegex(RuntimeError,'storage unavailable'):
            cm.inventory(cache=link,output=self.output,catalog=self.catalog,work=self.work)

    def test_lock_marks_cache_as_busy(self):
        lock=self.work/'operation.lock'
        self.assertFalse(cm.operation_busy(lock))
        with cm.operation_lock(lock):
            self.assertTrue(cm.operation_busy(lock))
            with self.assertRaisesRegex(RuntimeError,'busy'):
                with cm.operation_lock(lock):pass
        self.assertFalse(cm.operation_busy(lock))

    def test_preview_expiry_invalidates_confirmation(self):
        old=self.add('"Expire my preview."')
        report=self.scan()
        preview=cm._load_json(self.work/'cleanup_preview.json')
        preview['created_at']-=3601
        cm._secret_json(self.work/'cleanup_preview.json',preview)
        with self.assertRaisesRegex(RuntimeError,'expired'):
            self.delete(report)
        self.assertTrue(old.exists())


if __name__=='__main__':
    unittest.main()
