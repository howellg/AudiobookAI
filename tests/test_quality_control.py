"""Offline, synthetic-WAV regression tests for Quality Control.

These create tiny fixtures inside temporary directories; no EPUBs, TTS model,
Qt window, MySQL connection or real audiobook assets are needed.
"""
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import time
import unittest

import numpy as np
import soundfile as sf

MODULE = Path(__file__).resolve().parents[1] / 'scripts' / 'audiobookai_quality.py'
SPEC = importlib.util.spec_from_file_location('audiobookai_quality', MODULE)
QC = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(QC)


class QualityTests(unittest.TestCase):
    """Ensure the scanner is conservative, non-destructive and reportable."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.folder = self.root / 'book' / 'voice'
        (self.folder / 'chapters').mkdir(parents=True)
        self.cache = self.root / 'raw_cache'
        self.cache.mkdir()
        self.manifest = {'chapters': {}}
        self.rate = 24000

    def wav(self, chapter, audio, *, samples=None, signature='known'):
        file = self.folder / 'chapters' / f'{chapter:04d}.wav'
        sf.write(file, audio, self.rate)
        self.manifest['chapters'][str(chapter)] = {
            'sample_rate': self.rate, 'samples': len(audio) if samples is None else samples,
            'signature': signature, 'wav': str(file), 'title': f'Chapter {chapter}',
        }
        self.store_manifest()
        return file

    def store_manifest(self):
        (self.folder / 'manifest.json').write_text(json.dumps(self.manifest))

    def test_detects_internal_silence_and_clipping(self):
        """Report long internal gaps and hard-clipped audio as review warnings."""
        a = np.concatenate([np.ones(self.rate) * .999,
                            np.zeros(self.rate * 5),
                            np.ones(self.rate) * .999])
        self.wav(3, a)
        report = QC.scan_book(self.folder, chapters=[3], cache_root=self.cache)
        kinds = {x['code'] for x in report['issues']}
        self.assertIn('clipping', kinds)
        self.assertIn('long_silence', kinds)
        self.assertEqual(report['errors'], 0)
        self.assertEqual(report['metrics']['chapters_scanned'], 1)
        self.assertAlmostEqual(report['issues'][0]['chapter'], 3)
        self.assertTrue((self.folder / 'quality_reports' / 'summary.json').is_file())

    def test_missing_completed_wav_is_hard_error(self):
        """A completed job with absent chapter audio must never pass QC."""
        self.store_manifest()
        report = QC.scan_book(self.folder, chapters=[7], required=[7], cache_root=self.cache)
        self.assertEqual(report['errors'], 1)
        self.assertEqual(report['issues'][0]['code'], 'missing_chapter')

    def test_unfinished_parts_are_not_flagged_as_missing(self):
        """Incomplete queue sections do not create false missing-audio errors."""
        self.wav(3, np.sin(np.arange(self.rate) / 7) * 0.3)
        report = QC.scan_book(self.folder, chapters=[3, 4], required=[3], cache_root=self.cache)
        self.assertFalse(any(e['code'] == 'missing_chapter' for e in report['issues']))

    def test_missing_raw_cache_is_not_broken_finished_audio(self):
        """Cache cleanup must not make an already assembled chapter fail QC."""
        self.wav(3, np.sin(np.arange(self.rate) / 7) * .3)
        index = self.folder / 'chunk_indexes' / '0003.json'
        index.parent.mkdir()
        index.write_text(json.dumps({'signature': 'known', 'chunks': [
            {'number': 1, 'text': 'Some spoken words here.',
             'cache_file': str(self.cache / 'cleaned.wav')}]}))
        report = QC.scan_book(self.folder, deep=True, cache_root=self.cache)
        self.assertEqual(report['errors'], 0)
        self.assertEqual(report['metrics']['raw_chunks_unavailable'], 1)

    def test_deep_scan_finds_short_chunk(self):
        """Compare cached speech length with its associated chunk word count."""
        self.wav(3, np.sin(np.arange(self.rate) / 7) * .3)
        chunk = self.cache / 'short.wav'
        sf.write(chunk, np.sin(np.arange(1200) / 7) * .3, self.rate)
        index = self.folder / 'chunk_indexes' / '0003.json'
        index.parent.mkdir()
        index.write_text(json.dumps({'signature': 'known', 'chunks': [
            {'number': 1, 'text': 'This passage has several normal spoken words here',
             'cache_file': str(chunk)}]}))
        report = QC.scan_book(self.folder, deep=True, cache_root=self.cache)
        self.assertEqual(report['metrics']['chunks_scanned'], 1)
        self.assertTrue(any(x['code'] == 'short_for_text' and x['chunk'] == 1 for x in report['issues']))

    def test_metadata_mismatch_prevents_safe_assumptions(self):
        """Detect chapter duration metadata that would misplace M4B boundaries."""
        self.wav(3, np.sin(np.arange(self.rate) / 7) * .3,
                 samples=self.rate * 3)
        report = QC.scan_book(self.folder)
        self.assertIn('sample_count_mismatch', {x['code'] for x in report['issues']})

    def test_report_stale_when_wav_changes(self):
        """Notify the export dialog if repaired audio makes a prior scan stale."""
        file = self.wav(3, np.sin(np.arange(self.rate) / 7) * .3)
        QC.scan_book(self.folder)
        self.assertIn('no hard errors', QC.quality_preflight(self.folder, [3]))
        self.assertIn('not covered', QC.quality_preflight(self.folder, [3, 4]))
        newer = time.time() + 10
        import os
        os.utime(file, (newer, newer))
        self.assertIn('stale', QC.quality_preflight(self.folder, [3]))

    def test_rejects_unsafe_cache_path(self):
        """Never follow an untrusted chunk-index path outside the raw-cache tree."""
        self.wav(3, np.sin(np.arange(self.rate) / 7) * .3)
        index = self.folder / 'chunk_indexes' / '0003.json'
        index.parent.mkdir()
        index.write_text(json.dumps({'signature': 'known', 'chunks': [
            {'number': 1, 'text': 'Example words', 'cache_file': str(self.folder / 'manifest.json')}]}))
        report = QC.scan_book(self.folder, deep=True, cache_root=self.cache)
        self.assertIn('chunk_check_failed', {x['code'] for x in report['issues']})
        self.assertEqual(report['metrics']['chunks_scanned'], 0)

    def test_leading_and_trailing_silence_are_not_internal_gaps(self):
        """Avoid flagging ordinary chapter fade-in/fade-out padding as a gap."""
        a = np.concatenate([np.zeros(self.rate * 5),
                            np.sin(np.arange(self.rate) / 7) * .3,
                            np.zeros(self.rate * 5)])
        self.wav(3, a)
        report = QC.scan_book(self.folder)
        self.assertNotIn('long_silence', {e['code'] for e in report['issues']})

    def test_long_silence_across_streaming_blocks(self):
        """An internal silence continuing across two read buffers is detected."""
        sr = self.rate
        a = np.concatenate([np.sin(np.arange(sr * 8) / 7) * .3,
                            np.zeros(sr * 15),
                            np.sin(np.arange(sr) / 7) * .3])
        self.wav(3, a)
        report = QC.scan_book(self.folder)
        pauses = [e for e in report['issues'] if e['code'] == 'long_silence']
        self.assertEqual(len(pauses), 1)
        self.assertIn('15.0s', pauses[0]['message'])

    def test_parsing_chapters(self):
        """Chapter list parsing ignores duplicates and rejects invalid numbers."""
        self.assertEqual(QC.parse_chapters('5, 3,5'), [3, 5])
        with self.assertRaises(ValueError):
            QC.parse_chapters('0,2')
        with self.assertRaises(ValueError):
            QC.parse_chapters('1,hello')


if __name__ == '__main__':
    unittest.main()
