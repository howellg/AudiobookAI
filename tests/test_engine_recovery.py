"""GPU-free smoke tests for V7 recovery and editor non-cache-overwrite rules."""
from __future__ import annotations
import hashlib
import importlib.util
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

import torch
import torchaudio as ta

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))


class EngineRecoveryTests(unittest.TestCase):
    """Exercise V7 retry behavior without loading real model weights."""
    @classmethod
    def setUpClass(cls):
        """Install fake Chatterbox imports so recovery tests run without loading the real model."""
        cls.original = {key: sys.modules.get(key) for key in ('chatterbox', 'chatterbox.tts')}
        chatterbox = types.ModuleType('chatterbox')
        chatterbox.__path__ = []
        tts = types.ModuleType('chatterbox.tts')
        tts.ChatterboxTTS = type('ChatterboxTTS', (), {})
        sys.modules.setdefault('chatterbox', chatterbox)
        sys.modules.setdefault('chatterbox.tts', tts)
        spec = importlib.util.spec_from_file_location('v7_test_stub', str(Path(__file__).resolve().parents[1] / 'scripts' / 'test_five_minutes_v7_smooth.py'))
        cls.engine = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = cls.engine
        spec.loader.exec_module(cls.engine)

    @classmethod
    def tearDownClass(cls):
        """Restore the Python import table after the engine test class completes."""
        sys.modules.pop('v7_test_stub', None)
        for key, value in cls.original.items():
            if value is None:
                sys.modules.pop(key, None)
            else:
                sys.modules[key] = value

    def test_failed_short_phrase_is_retried_and_split_automatically(self):
        """Prove an overlong chunk is split and a forced editor take leaves the shared cache untouched."""
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            with patch.object(self.engine, 'CACHE_DIR', path):
                with patch.object(self.engine, 'cache_key', side_effect=lambda t: hashlib.sha256(t.encode()).hexdigest()[:24]):
                    calls = []
                    def pretend(_model, text):
                        """Simulate a model that rejects multiword chunks to exercise recursive fallback."""
                        calls.append(text)
                        if len(text.split()) > 1:
                            raise self.engine.OverlongGeneration('simulated limit')
                        return torch.full((1, 24000//2), .15)
                    with patch.object(self.engine, 'capped_generate', side_effect=pretend):
                        audio = self.engine.generate_cached(object(), 'failing phrase', 24000)
                        self.assertGreater(audio.shape[-1], 24000)
                        self.assertIn('failing', calls)
                        cache = path / (self.engine.cache_key('failing phrase') + '.wav')
                        self.assertTrue(cache.is_file())
                        before = cache.read_bytes()
                        # Editor take: force fresh audio but never overwrite the shared cache.
                        edited = self.engine.generate_cached(object(), 'failing phrase', 24000,
                                                             force=True, cache_write=False)
                        self.assertEqual(before, cache.read_bytes())
                        self.assertEqual(edited.shape, audio.shape)

if __name__ == '__main__':
    unittest.main()
