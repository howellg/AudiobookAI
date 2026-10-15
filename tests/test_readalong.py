"""Read Along regression tests: no Qt, GPU, Chatterbox, or generated speech."""
import json
from types import SimpleNamespace
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

import numpy as np
import soundfile as sf

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import audiobookai_readalong as reader
import audiobookai_production as production


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

    def test_selected_take_is_not_shown_until_assembled_wav_contains_it(self):
        """Saved active take may be unapplied until the editor rebuilds the WAV."""
        edit_folder = self.folder / "chunk_edits"
        replacement = edit_folder / "audio" / "0001" / "take1.wav"
        replacement.parent.mkdir(parents=True)
        sf.write(str(replacement), np.full(2 * 16000, 0.12), 16000)
        edits = {
            "version": 1,
            "edits": {
                "1": {
                    "original_text": "Chapter sentence 1",
                    "active": "take1",
                    "takes": [{
                        "id": "take1", "text": "Corrected name spoken here",
                        "wav": str(replacement.relative_to(self.folder)),
                    }],
                },
            },
        }
        production.atomic_json(edit_folder / "0001.json", edits)
        digest = production.edit_digest(
            self.folder, 1, [SimpleNamespace(text=e["text"]) for e in self.entries])
        manifest_path = self.folder / "manifest.json"
        manifest_path.write_text(json.dumps({
            "chapters": {"1": {"edits_digest": "stale-digest"}}
        }))
        unrepaired = self.load()
        self.assertEqual(unrepaired["chunks"][0]["text"], "Chapter sentence 1")
        self.assertEqual(unrepaired["chunks"][1]["start_ms"], 1000)

        manifest_path.write_text(json.dumps({
            "chapters": {"1": {"edits_digest": digest}}
        }))
        rebuilt = self.load()
        self.assertEqual(rebuilt["chunks"][0]["text"], "Corrected name spoken here")
        self.assertEqual(rebuilt["chunks"][1]["start_ms"], 1714)

    def test_resume_time_is_clamped_and_completed_chapter_resets(self):
        """An obsolete or corrupted QSettings seek value cannot escape audio bounds."""
        self.assertEqual(reader.clamp_resume_ms(2100, 6000), 2100)
        self.assertEqual(reader.clamp_resume_ms(-500, 6000), 0)
        self.assertEqual(reader.clamp_resume_ms(6000, 6000), 0)
        self.assertEqual(reader.clamp_resume_ms(5600, 6000), 0)
        self.assertEqual(reader.clamp_resume_ms("invalid", 6000), 0)
        self.assertEqual(reader.clamp_resume_ms(None, 6000), 0)
        self.assertEqual(reader.clamp_resume_ms(400, 0), 0)


if __name__ == "__main__":
    unittest.main()
