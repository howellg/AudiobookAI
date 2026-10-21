"""Offline book integrity inventory and completed-container regression tests."""
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from audiobookai_integrity import inspect_book, inspect_complete_m4b


class BookIntegrityTests(unittest.TestCase):
    def test_regenerated_202_chapters_are_complete_without_early_part_receipts(self):
        with tempfile.TemporaryDirectory() as temp:
            folder = Path(temp)
            (folder / 'chapters').mkdir()
            entries = {}
            for i in range(3, 205):
                (folder / 'chapters' / f'{i:04d}.wav').write_bytes(b'RIFF' + b'0' * 128)
                entries[str(i)] = {'samples': 24000, 'sample_rate': 24000}
            (folder / 'manifest.json').write_text(json.dumps({'chapters': entries}))
            jobs = [{'indices': json.dumps(list(range(123, 205))), 'status': 'complete'}]
            report = inspect_book(folder, 205, jobs)
            self.assertEqual(len(report['recorded']), 202)
            self.assertEqual(report['outside'], [1, 2, 205])
            self.assertEqual(report['queued'], [])
            self.assertEqual(report['missing'], [])
            self.assertTrue(report['wav_ready'])
            self.assertEqual(report['expected_seconds'], 202)

    def test_missing_wavs_are_not_counted_as_recorded(self):
        with tempfile.TemporaryDirectory() as temp:
            folder = Path(temp)
            (folder / 'chapters').mkdir()
            (folder / 'chapters' / '0003.wav').write_bytes(b'RIFF' + b'0' * 128)
            entries = {str(i): {'samples': 24000, 'sample_rate': 24000} for i in (3, 4)}
            (folder / 'manifest.json').write_text(json.dumps({'chapters': entries}))
            report = inspect_book(folder, 5)
            self.assertEqual(report['recorded'], [3])
            self.assertEqual(report['missing'], [4])
            self.assertFalse(report['wav_ready'])

    def test_queued_and_not_started_are_distinct(self):
        with tempfile.TemporaryDirectory() as temp:
            folder = Path(temp)
            (folder / 'manifest.json').write_text(json.dumps({'chapters': {}}))
            report = inspect_book(folder, 5, [
                {'indices': '[2]', 'status': 'running'},
                {'indices': '[5]', 'status': 'complete'}
            ])
            self.assertEqual(report['queued'], [2])
            self.assertEqual(report['not_started'], [3, 4])
            self.assertEqual(report['missing'], [5])
            self.assertFalse(report['wav_ready'])

    def test_bad_manifest_is_not_reported_complete(self):
        with tempfile.TemporaryDirectory() as temp:
            folder = Path(temp)
            (folder / 'manifest.json').write_text('not json')
            report = inspect_book(folder, 5, [
                {'indices': '[1,2]', 'status': 'complete'}
            ])
            self.assertEqual(report['missing'], [1, 2])
            self.assertTrue(report['manifest_error'])
            self.assertFalse(report['wav_ready'])

    def test_qc_report_requires_full_fresh_coverage(self):
        with tempfile.TemporaryDirectory() as temp:
            folder = Path(temp)
            (folder / 'chapters').mkdir()
            manifest = {'chapters': {}}
            for index in (3, 4):
                (folder / 'chapters' / f'{index:04d}.wav').write_bytes(b'RIFF' + b'0' * 128)
                manifest['chapters'][str(index)] = {'samples': 24000, 'sample_rate': 24000}
            (folder / 'manifest.json').write_text(json.dumps(manifest))
            reports = folder / 'quality_reports'
            reports.mkdir()
            (reports / 'summary.json').write_text(json.dumps({
                'chapters': [3], 'timestamp': 9999999999, 'issues': []}))
            self.assertEqual(inspect_book(folder, 5)['qc'], 'Incomplete scan')
            (reports / 'summary.json').write_text(json.dumps({
                'chapters': [3, 4], 'timestamp': 9999999999, 'issues': []}))
            self.assertEqual(inspect_book(folder, 5)['qc'], 'Passed')

    @patch('audiobookai_integrity.subprocess.run')
    def test_ffprobe_requires_audio_chapters_and_duration(self, run):
        with tempfile.TemporaryDirectory() as temp:
            target = Path(temp) / 'Complete.m4b'
            target.write_bytes(b'audio')
            def result(n, duration, audio=True):
                return SimpleNamespace(stdout=json.dumps({
                    'streams': [{'codec_type': 'audio'}] if audio else [],
                    'chapters': [{}] * n, 'format': {'duration': str(duration)}
                }))
            run.return_value = result(202, 202)
            self.assertIn('Passed', inspect_complete_m4b(target, range(3, 205), 202))
            run.return_value = result(82, 202)
            self.assertIn('Failed: 82', inspect_complete_m4b(target, range(3, 205), 202))
            run.return_value = result(202, 400)
            self.assertIn('duration differs', inspect_complete_m4b(target, range(3, 205), 202))
            run.return_value = result(202, 202, audio=False)
            self.assertIn('no audio stream', inspect_complete_m4b(target, range(3, 205), 202))


if __name__ == '__main__':
    unittest.main()
