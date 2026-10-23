"""Regression tests for SSD-backed symlinked narration caches (no GPU required)."""
from __future__ import annotations

import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parents[1] / 'scripts'
sys.path.insert(0, str(SCRIPTS))

from audiobookai_cache import ensure_cache_directory, cache_error_summary


class CacheStorageTests(unittest.TestCase):
    def test_unmounted_cache_symlink_is_never_replaced(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            link = root / 'output' / 'narration_cache_v2'
            link.parent.mkdir()
            destination = root / 'unmounted-ssd' / 'narration_cache_v2'
            link.symlink_to(destination)
            self.assertFalse(destination.exists())
            with self.assertRaisesRegex(RuntimeError, 'Mount the drive'):
                ensure_cache_directory(link)
            self.assertTrue(link.is_symlink())
            self.assertEqual(link.readlink(), destination)
            self.assertFalse(destination.exists())
            self.assertFalse(link.is_dir())

    def test_mounted_symlink_preserves_cached_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            destination = root / 'ssd' / 'cache'
            destination.mkdir(parents=True)
            recording = destination / 'sample.wav'
            recording.write_bytes(b'preserve cache')
            link = root / 'cache'
            link.symlink_to(destination)
            self.assertEqual(ensure_cache_directory(link), link)
            self.assertTrue(link.is_symlink())
            self.assertEqual(recording.read_bytes(), b'preserve cache')

    def test_regular_cache_directory_created_but_file_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            folder = root / 'output' / 'cache'
            ensure_cache_directory(folder)
            self.assertTrue(folder.is_dir())
            existing = root / 'other-cache'
            existing.write_bytes(b'do not overwrite')
            with self.assertRaises(NotADirectoryError):
                ensure_cache_directory(existing)
            self.assertEqual(existing.read_bytes(), b'do not overwrite')

    def test_native_traceback_is_compact(self):
        native = RuntimeError(
            'Failed to open WAV (Invalid data found when processing input).\n'
            'frame #0: C++ long stack trace\nframe #1: more traceback'
        )
        self.assertEqual(cache_error_summary(native),
                         'Failed to open WAV (Invalid data found when processing input).')

    def test_converter_reports_unmounted_drive_before_generation(self):
        import epub_to_audiobook_v7_studio as converter
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            linked = root / 'cache'
            linked.symlink_to(root / 'offline' / 'cache')
            voice = root / 'narrator.wav'
            voice.write_bytes(b'reference')
            engine = root / 'fake_engine.py'
            engine.write_text(
                f'from pathlib import Path\nVOICE_FILE = Path({str(voice)!r})\n'
                f'CACHE_DIR = Path({str(linked)!r})\n'
            )
            with patch.object(converter, 'ENGINE_FILE', engine), \\
                 patch.object(converter, 'ACTIVE_VOICE', None):
                with self.assertRaisesRegex(RuntimeError, 'cache storage is unavailable'):
                    converter.load_engine()
            self.assertTrue(linked.is_symlink())


if __name__ == '__main__':
    unittest.main()
