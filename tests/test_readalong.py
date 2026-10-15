"""Read Along regression tests: no Qt, GPU, Chatterbox, or generated speech."""
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

import numpy as np
import soundfile as sf

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import audiobookai_readalong as reader


class ReadAlongTests(unittest.TestCase):
    def setUp(self):
        self.work = tempfile.TemporaryDirectory()
        self.addCleanup(self.work.cleanup)
        self.root = Path(self.work.name)
        self.folder = self.root / "novel"
        (self.folder / "chapters").mkdir(parents=True)
        (self.folder / "chunk_indexes").mkdir()
        sf.write(str(self.folder / "chapters" / "0001.wav"), np.zeros(6 * 16000), 16000)
        self.cache = self.root / "cache"
        self.cache.mkdir()
        self.entries = []
        for n, seconds in enumerate([1, 2, 3], 1):
            file = self.cache / f"{n}.wav"
            sf.write(str(file), np.full(int(seconds * 16000), 0.1), 16000)
            self.entries.append({
                "number": n, "text": f"Chapter sentence {n}",
                "cache_file": str(file),
            })
        self.save_index()

    def save_index(self):
        path = self.folder / "chunk_indexes" / "0001.json"
        path.write_text(json.dumps({"title": "Chapter 1", "chunks": self.entries}))

    def load(self):
        # The test cache is outside the live app's protected cache root; scope
        # only this file-path validator to our disposable test fixture.
        with patch("audiobookai_readalong.original_chunk_file",
                   side_effect=lambda e: Path(e["cache_file"])):
            return reader.read_chapter(self.folder, 1)

    def test_duration_weighted_boundaries(self):
        chapter = self.load()
        self.assertEqual(chapter["duration_ms"], 6000)
        self.assertEqual([(c["start_ms"], c["end_ms"]) for c in chapter["chunks"]],
                         [(0, 1000), (1000, 3000), (3000, 6000)])
        self.assertTrue(chapter["approximate"])

    def test_seek_lookup_uses_next_chunk_at_boundary(self):
        timeline = self.load()["chunks"]
        actual = [reader.chunk_at(timeline, pos)
                  for pos in [-1, 0, 999, 1000, 2999, 3000, 6000]]
        self.assertEqual(actual, [0, 0, 0, 1, 1, 2, 2])
        self.assertEqual(reader.chunk_at([], 10), -1)

    def test_missing_raw_cache_estimates_and_preserves_chapter(self):
        (self.cache / "2.wav").unlink()
        chapter = self.load()
        self.assertIn("1 missing", chapter["timing"])
        self.assertEqual(chapter["chunks"][-1]["end_ms"], 6000)

    def test_missing_chapter_wav_reports_error(self):
        (self.folder / "chapters" / "0001.wav").unlink()
        with self.assertRaises(FileNotFoundError):
            self.load()

    def test_bad_or_empty_index_rejected(self):
        self.entries.clear()
        self.save_index()
        with self.assertRaises(FileNotFoundError):
            self.load()
        self.entries.append({"number": 7, "text": "Out of order",
                             "cache_file": str(self.cache / "1.wav")})
        self.save_index()
        with self.assertRaises(ValueError):
            self.load()


if __name__ == "__main__":
    unittest.main()
