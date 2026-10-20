"""AudiobookAI V2.4 offline production tests (no GPU or Qt required)."""
import hashlib
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch
from dataclasses import dataclass

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import audiobookai_production as p


@dataclass
class Chunk:
    """One speakable text unit plus the boundary context needed for audio assembly."""
    text: str
    boundary: str = 'sentence'
    dialogue: bool = False


class FakeEngine:
    """Tiny engine stub exposing the raw cache directory and deterministic cache keys."""
    def __init__(self, cache):
        """Initialize this isolated test double with its minimal in-memory state."""
        self.CACHE_DIR = cache
    def cache_key(self, text):
        """Build the cache identity for this text and the current narration configuration."""
        return hashlib.sha256(text.encode()).hexdigest()[:24]


class ProductionTests(unittest.TestCase):
    """Verify chunk editing, quality checks, and safe cleanup behavior."""
    def setUp(self):
        """Prepare isolated fixtures and temporary data for each test."""
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.out = self.root / 'audiobooks' / 'book'
        self.out.mkdir(parents=True)
        self.cache = self.root / 'test_raw_cache'
        self.cache.mkdir()
        self.engine = FakeEngine(self.cache)
        self.chunks = [Chunk('Hello, traveler.'), Chunk('How are you doing today?', 'end')]

    def tearDown(self):
        """Release fixtures and temporary resources after each test."""
        self.temp.cleanup()

    def test_non_destructive_takes_and_revert(self):
        """Verify non destructive takes and revert behavior."""
        p.save_index(self.out, 3, 'Third chapter', self.chunks, self.engine, 'sig')
        orig = self.cache / (self.engine.cache_key(self.chunks[0].text) + '.wav')
        orig.write_bytes(b'a' * 1024)
        before = p.edit_digest(self.out, 3, self.chunks)
        rel, path = p.take_destination(self.out, 3, 1)
        path.write_bytes(b'b' * 1024)
        take1 = p.register_take(self.out, 3, 1, self.chunks[0].text, 'Hello dear traveler.', rel)
        self.assertEqual(p.active_take(self.out, 3, 1, self.chunks[0].text)['text'], 'Hello dear traveler.')
        self.assertNotEqual(before, p.edit_digest(self.out, 3, self.chunks))
        rel2, path2 = p.take_destination(self.out, 3, 1)
        path2.write_bytes(b'c' * 1024)
        take2 = p.register_take(self.out, 3, 1, self.chunks[0].text, 'Welcome.', rel2)
        self.assertEqual(len(p.all_takes(self.out, 3, 1, self.chunks[0].text)[0]), 2)
        p.choose_take(self.out, 3, 1, self.chunks[0].text, take1['id'])
        self.assertEqual(p.active_take(self.out, 3, 1, self.chunks[0].text)['text'], 'Hello dear traveler.')
        p.choose_take(self.out, 3, 1, self.chunks[0].text, None)
        self.assertIsNone(p.active_take(self.out, 3, 1, self.chunks[0].text))
        self.assertEqual(before, p.edit_digest(self.out, 3, self.chunks))
        self.assertEqual(orig.read_bytes(), b'a' * 1024)
        self.assertEqual(path.read_bytes(), b'b' * 1024)
        self.assertEqual(path2.read_bytes(), b'c' * 1024)

    def test_outdated_source_and_unsafe_take_path_rejected(self):
        """Verify outdated source and unsafe take path rejected behavior."""
        rel, path = p.take_destination(self.out, 5, 1)
        path.write_bytes(b'x'*1024)
        p.register_take(self.out, 5, 1, 'Old words', 'New words', rel)
        self.assertIsNone(p.active_take(self.out, 5, 1, 'Different original'))
        with self.assertRaises(ValueError):
            p.register_take(self.out, 5, 1, 'New original', 'New words', rel)
        with self.assertRaises(ValueError):
            p.register_take(self.out, 5, 2, 'X', 'X', Path('../../../outside.wav'))

    def test_audio_validation_reports_hard_and_soft_problems(self):
        """Verify audio validation reports hard and soft problems behavior."""
        sr = 24000
        silence = p.quality_report(np.zeros(sr, dtype=np.float32), sr, words=7)
        self.assertTrue(silence['unusable'])
        wave = 0.2 * np.sin(np.linspace(0, 1200, sr*3)).astype(np.float32)
        good = p.quality_report(wave, sr, words=9)
        self.assertFalse(good['unusable'])
        short = p.quality_report(wave[:2400], sr, words=15)
        self.assertTrue(short['unusable'])
        clipped = p.quality_report(np.ones(sr*3, dtype=np.float32), sr, words=7)
        self.assertIn('possible clipping', clipped['flags'])

    def test_manual_cache_cleanup_does_not_follow_symlinks_or_touch_other_audio(self):
        """Verify manual cache cleanup does not follow symlinks or touch other audio behavior."""
        older = self.cache / 'old.wav'
        newer = self.cache / 'new.wav'
        other = self.cache / 'manifest.json'
        external = self.out / 'chapter.wav'
        for path in (older, newer, other, external):
            path.write_bytes(b'x' * 1024)
        link = self.cache / 'shortcut.wav'
        link.symlink_to(external)
        past = time.time() - 120*86400
        os.utime(older, (past, past))
        count, size = p.cache_summary(90, cache_dir=self.cache)
        self.assertEqual((count, size), (1, 1024))
        # Test-only cleanup must explicitly treat this isolated folder as
        # the configured cache; a name starting with "test_" grants no access.
        with patch.object(p, 'RAW_CACHE', self.cache):
            self.assertEqual(p.remove_raw_cache(90, cache_dir=self.cache, expected_count=1),
                             (1, 1024))
            self.assertFalse(older.exists())
            for path in (newer, other, external, link):
                self.assertTrue(path.exists() or path.is_symlink())
            with self.assertRaises(RuntimeError):
                p.remove_raw_cache(None, cache_dir=self.cache, expected_count=0)

    def test_test_named_directory_has_no_special_delete_permission(self):
        # Previously any path basename beginning "test_" passed containment.
        wav = self.cache / 'keep.wav'
        wav.write_bytes(b'keep')
        with self.assertRaisesRegex(ValueError, 'AudiobookAI raw cache'):
            p.remove_raw_cache(None, cache_dir=self.cache, expected_count=1)
        self.assertEqual(wav.read_bytes(), b'keep')

    def test_cache_cleanup_supports_external_symlink(self):
        # Emulate output/narration_cache_v2 -> mounted external SSD.
        link = self.root / 'linked_cache'
        link.symlink_to(self.cache, target_is_directory=True)
        test_wav = self.cache / 'one.wav'
        test_wav.write_bytes(b'wav-bytes')
        with patch.object(p, 'RAW_CACHE', link):
            self.assertEqual(p.remove_raw_cache(None, cache_dir=link, expected_count=1),
                             (1, len(b'wav-bytes')))
        self.assertFalse(test_wav.exists())

    def test_cleanup_refuses_arbitrary_directory(self):
        """Verify cleanup refuses arbitrary directory behavior."""
        with self.assertRaises(ValueError):
            p.remove_raw_cache(None, cache_dir=self.out)


if __name__ == '__main__':
    unittest.main()
