"""Queue-wide preflight checks source inputs and storage without changing them."""
from pathlib import Path
import json
import shutil
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import wave

from ebooklib import epub

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from audiobookai_storage import Store
import audiobookai_queue_preflight as preflight


@unittest.skipUnless(shutil.which('ffprobe') and shutil.which('ffmpeg'), 'FFmpeg is required')
class QueuePreflightTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.cache = self.root / 'cache'
        self.outputs = self.root / 'audiobooks'
        self.snapshots = self.root / 'pronunciation' / 'jobs'
        self.voice = self.root / 'narrator.wav'
        with wave.open(str(self.voice), 'wb') as file:
            file.setparams((1, 2, 24_000, 0, 'NONE', 'not compressed'))
            file.writeframes(b'\0' * 48_000)
        self.epub = self.root / 'novel.epub'
        novel = epub.EpubBook()
        novel.set_identifier('queue-preflight')
        novel.set_title('Fixture')
        novel.set_language('en')
        chapter = epub.EpubHtml(title='Chapter 1', file_name='chapter.xhtml', lang='en')
        chapter.set_content('<html><body><h1>Chapter 1</h1><p>' +
                            'words ' * 3000 + '</p></body></html>')
        novel.add_item(chapter)
        novel.toc = [chapter]
        novel.add_item(epub.EpubNcx())
        novel.add_item(epub.EpubNav())
        novel.spine = [chapter]
        epub.write_epub(str(self.epub), novel)
        self.store = Store(self.root / 'studio.sqlite3')
        self.addCleanup(self.store.close)
        self.book = self.store.import_book(self.epub, 'Fixture', 'Author', [])

    def check(self):
        return '\n'.join(preflight.inspect_queue(
            self.store.jobs(), self.cache, output_root=self.outputs,
            snapshot_dir=self.snapshots, default_voice=self.voice))

    def test_reports_multiple_later_job_problems_and_keeps_queue_intact(self):
        good = self.store.add_job(self.book, 'First part', [1], 'part1')
        bad = self.store.add_job(self.book, 'Later part', [9], 'part2',
                                 voice_path=self.root / 'missing.wav', pronunciation_required=True)
        ignored = self.store.add_job(self.book, 'Interrupted part', [10], 'part3')
        self.store.update_job(ignored, 'interrupted')
        report = self.check()
        self.assertIn('Needs attention · 2 queued parts checked', report)
        self.assertLess(report.index(f'#{good}'), report.index(f'#{bad}'))
        self.assertIn('Narrator WAV unavailable', report)
        self.assertIn('Required pronunciation snapshot', report)
        self.assertIn('only 1 readable sections', report)
        self.assertNotIn('Interrupted part', report)
        self.assertEqual(self.store.job(bad)['status'], 'queued')
        self.assertFalse(self.cache.exists())
        self.assertFalse(self.outputs.exists())
        self.assertFalse(self.snapshots.exists())

    def test_cache_and_outputs_on_same_filesystem_share_available_space(self):
        self.store.add_job(self.book, 'Part 1', [1], 'part1')
        # Enough for the output and cache individually, too little for both.
        with patch.object(preflight.shutil, 'disk_usage', return_value=SimpleNamespace(free=200_000_000)):
            report = self.check()
        self.assertIn('Estimated space exceeds available space', report)
        self.assertEqual(report.count(' estimated output'), 1)
        self.assertIn('estimated raw cache', report)

    def test_broken_cache_symlink_and_corrupt_snapshot_are_reported(self):
        self.cache.symlink_to(self.root / 'offline' / 'cache')
        job_id = self.store.add_job(self.book, 'Part 1', [1], 'part1', pronunciation_required=True)
        self.snapshots.mkdir(parents=True)
        snapshot = self.snapshots / f'job_{job_id}.json'
        snapshot.write_text(json.dumps({'version': 1, 'rules': []}))
        self.assertIn('Inputs ready', self.check())
        snapshot.write_text('invalid JSON')
        report = self.check()
        self.assertIn('Cache link target is unavailable', report)
        self.assertIn('Required pronunciation snapshot cannot be read', report)
        self.assertTrue(self.cache.is_symlink())


if __name__ == '__main__':
    unittest.main()
