"""Synthetic voice-independent converter rebuild and real FFmpeg export test."""
import hashlib
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import torch
import torchaudio as ta

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import epub_to_audiobook_v7_studio as converter
import audiobookai_production as p


class FakeEngine:
    """Tiny engine stub exposing the raw cache directory and deterministic cache keys."""
    SMOOTH_VERSION = 'v7-smooth-1'
    FINAL_CONTINUATION_PAUSE = .055
    EDGE_KEEP_SECONDS = .065
    MAX_EDGE_TRIM_SECONDS = .55
    FINAL_COMMA_PAUSE = .16
    FINAL_SENTENCE_PAUSE = .27
    FINAL_DIALOGUE_PAUSE = .46
    FINAL_PARAGRAPH_PAUSE = .4
    FINAL_SCENE_PAUSE = .65
    LEADING_PAD_FINAL = .1
    MAX_PEAK = .92
    def __init__(self, cache):
        """Initialize this isolated test double with its minimal in-memory state."""
        self.CACHE_DIR = cache
        cache.mkdir(parents=True, exist_ok=True)
    def cache_key(self, text):
        """Build the cache identity for this text and the current narration configuration."""
        return hashlib.sha256(text.encode()).hexdigest()[:24]
    def make_chunks(self, text):
        """Produce ordered narration chunks with sentence and paragraph boundary metadata."""
        return [type('Chunk', (), dict(text=t, boundary='end' if i == len(text.split('\n\n'))-1 else 'sentence', dialogue=False))()
                for i, t in enumerate(text.split('\n\n'))]
    def clean_tensor(self, clip):
        """Normalize generated audio into the channel and sample shape expected by assembly."""
        return clip.float()
    def assemble(self, chunks, recordings, sr):
        """Concatenate speech with context-sensitive pauses and final peak normalization."""
        return torch.cat(recordings, dim=-1), 0.0


class RebuildTests(unittest.TestCase):
    """Regression cases for rebuild behavior."""
    def test_offline_rebuild_new_take_and_export(self):
        """Rebuild audio using selected takes and export an M4B without GPU synthesis."""
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            epub = root / 'book.epub'
            epub.write_bytes(b'fake book; load_epub not used')
            out = root / 'output' / 'audiobooks' / 'book'
            engine = FakeEngine(root / 'cache')
            text = 'The first sentence.\n\nThe second sentence.'
            sections = [converter.Section(1, 'Chapter One', text.split('\n\n'), 'book.xhtml')]
            for i, chunk in enumerate(engine.make_chunks(text), 1):
                path = engine.CACHE_DIR / (engine.cache_key(chunk.text)+'.wav')
                ta.save(str(path), torch.full((1, 24000), .06 + i*.01), 24000)
            with patch.object(converter, 'load_engine', return_value=engine):
                converter.rebuild_chapters(epub, 'Book', 'Author', sections, [1], out)
                chapter = out / 'chapters' / '0001.wav'
                self.assertTrue(chapter.exists())
                original_bytes = (engine.CACHE_DIR / (engine.cache_key('The first sentence.')+'.wav')).read_bytes()
                rel, replace = p.take_destination(out, 1, 1)
                ta.save(str(replace), torch.full((1, 48000), .13), 24000)
                p.register_take(out, 1, 1, 'The first sentence.', 'A changed sentence.', rel)
                converter.rebuild_chapters(epub, 'Book', 'Author', sections, [1], out)
                wave, sr = ta.load(str(chapter))
                self.assertEqual(sr, 24000)
                self.assertEqual(wave.shape[-1], 72000)
                self.assertEqual((engine.CACHE_DIR / (engine.cache_key('The first sentence.')+'.wav')).read_bytes(), original_bytes)
                manifest = json.loads((out / 'manifest.json').read_text())
                self.assertEqual(manifest['chapters']['1']['samples'], 72000)
                exported = converter.export_m4b(epub, 'Book', 'Author', sections, [1], out, 'Part_01')
                self.assertGreater(exported.stat().st_size, 1024)
                self.assertEqual(exported.suffix, '.m4b')
                self.assertTrue((out / 'chunk_indexes' / '0001.json').exists())
                self.assertTrue((out / 'chunk_edits' / '0001.json').exists())

if __name__ == '__main__':
    unittest.main()
