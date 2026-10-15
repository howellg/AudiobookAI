"""AudiobookAI V2.3 voice safety and backend tests (no GPU/Qt required)."""
import importlib.util
import json
import sys
import tempfile
import unittest
import wave
from pathlib import Path
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parents[1] / 'scripts'
sys.path.insert(0, str(SCRIPTS))
import audiobookai_voices as voices
from audiobookai_storage import Store

spec = importlib.util.spec_from_file_location('voice_test_backend', SCRIPTS / 'epub_to_audiobook_v7_studio.py')
backend = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = backend
spec.loader.exec_module(backend)


def create_sample(path, frequency=500, duration=2):
    """Write a short synthetic WAV fixture suitable for narrator validation."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), 'wb') as f:
        f.setnchannels(1)
        f.setsampwidth(2)
        f.setframerate(16000)
        wavebytes = int(duration*16000)
        f.writeframes(bytes([frequency % 256, 0]) * wavebytes)


class VoiceTests(unittest.TestCase):
    """Regression cases for voice behavior."""
    def setUp(self):
        """Prepare isolated fixtures and temporary data for each test."""
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.voice_folder = self.root / 'voices'
        self.snapshots = self.voice_folder / '.audiobookai_voice_snapshots'
        self.default = self.voice_folder / 'british_narrator.wav'
        create_sample(self.default, 22)
        self.cfg = patch.multiple(voices, VOICE_DIR=self.voice_folder,
                                  DEFAULT_VOICE=self.default, SNAPSHOT_DIR=self.snapshots)
        self.cfg.start()

    def tearDown(self):
        """Release fixtures and temporary resources after each test."""
        self.cfg.stop()
        self.tmp.cleanup()

    def test_import_and_snapshot_stable_after_edit(self):
        """Ensure an imported WAV and queued narrator snapshot survive later source edits."""
        sample = self.root / 'outside' / 'New narrator.wav'
        create_sample(sample, 87)
        imported = voices.import_voice(sample)
        self.assertNotEqual(imported, sample)
        self.assertTrue(imported.is_file())
        snapshot = voices.freeze_voice(imported)
        self.assertTrue(snapshot.is_file())
        original = voices.sha256_file(snapshot)
        self.assertIn('New narrator', voices.voice_name(snapshot))
        create_sample(imported, 21)
        self.assertEqual(original, voices.sha256_file(snapshot))
        self.assertNotEqual(snapshot, voices.freeze_voice(imported))
        self.assertIn(imported, voices.voice_choices())
        self.assertNotIn(snapshot, voices.voice_choices())

    def test_default_preserves_original_cache_and_paths(self):
        """Protect existing cache and chapter paths for the default British narrator."""
        self.assertIsNone(voices.freeze_voice(self.default))
        epub = self.root / 'story.epub'
        original = backend.book_folder(epub)
        with patch.object(backend, 'ROOT', self.root):
            self.assertEqual(original, backend.book_folder(epub, self.default))
        alternative = self.root / 'narrator2.wav'
        create_sample(alternative, 98)
        altfolder = backend.book_folder(epub, alternative)
        self.assertNotEqual(original, altfolder)
        self.assertIn('/voices/voice_', str(altfolder))
        create_sample(alternative, 21)
        self.assertNotEqual(altfolder, backend.book_folder(epub, alternative))

    def test_queued_voice_persists_and_can_change_only_prestart(self):
        """Enforce narrator immutability after a job starts."""
        db = Store(self.root / 'store.sqlite3')
        try:
            book = db.import_book(self.root/'example.epub', 'Test', 'Author', [])
            sample = self.root/'fellow.wav'
            create_sample(sample, 61)
            voice = voices.freeze_voice(sample)
            job = db.add_job(book, 'Part 01', [1,2], 'out1', voice)
            self.assertEqual(db.job(job)['voice_path'], str(voice))
            db.set_job_voice(job, None)
            self.assertIsNone(db.job(job)['voice_path'])
            db.update_job(job, 'running')
            with self.assertRaises(ValueError):
                db.set_job_voice(job, voice)
            db.close()
            db = Store(self.root / 'store.sqlite3')
            self.assertEqual(db.job(job)['status'], 'interrupted')
            self.assertIsNone(db.job(job)['voice_path'])
        finally:
            db.close()

    def test_invalid_voice_rejected(self):
        """Reject recordings with invalid or unreadable WAV input."""
        fake = self.root/'fake.wav'
        fake.write_bytes(b'not a wav' * 200)
        with self.assertRaises(ValueError):
            voices.validate_voice(fake)


if __name__ == '__main__':
    unittest.main()
