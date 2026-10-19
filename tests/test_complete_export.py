"""Offline full-book export tests: no TTS inference or GUI session required."""

import json
import subprocess
import sys
import tempfile
import unittest
import wave
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from audiobookai_fullbook import collect_export_groups, verify_export_group
import epub_to_audiobook_v7_studio as converter


class CompleteExportTests(unittest.TestCase):
    """Ensure all parts are included once, in EPUB order, for one narrator."""

    @staticmethod
    def job(job_id, book_id, selected, folder_name, status='complete'):
        return {'id': job_id, 'book_id': book_id, 'book_path': '/tmp/test-book.epub',
                'voice_path': folder_name, 'status': status, 'indices': json.dumps(selected)}

    @staticmethod
    def narrator_folder(book_path, voice_path):
        return Path('/tmp/test-outputs') / ('default' if voice_path is None else voice_path.name)

    def test_narrator_isolation_and_epub_order(self):
        jobs = [self.job(1, 4, [5, 3], 'kramer.wav'),
                self.job(2, 4, [4, 6], 'kramer.wav'),
                self.job(3, 4, [3, 4], 'british.wav'),
                self.job(4, 5, [1], 'kramer.wav')]
        groups = collect_export_groups(jobs, 4, self.narrator_folder)
        self.assertEqual(len(groups), 2)
        by_voice = {Path(group['voice_path']).name: group for group in groups}
        self.assertEqual(by_voice['kramer.wav']['indices'], [3, 4, 5, 6])
        self.assertEqual(by_voice['british.wav']['indices'], [3, 4])
        self.assertEqual(len(by_voice['kramer.wav']['parts']), 2)

    def test_unfinished_part_prevents_false_complete_book(self):
        with tempfile.TemporaryDirectory() as tmp:
            group = {'folder': Path(tmp), 'voice_path': None, 'indices': [2, 3],
                     'unfinished': [{'id': 8, 'status': 'queued'}]}
            with self.assertRaisesRegex(ValueError, 'not complete'):
                verify_export_group(group, 5)

    def test_missing_rendered_chapter_prevents_truncated_export(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            (folder / 'manifest.json').write_text(json.dumps({
                'chapters': {'2': {'sample_rate': 24000, 'samples': 24000}}
            }))
            group = {'folder': folder, 'voice_path': None, 'indices': [2, 3], 'unfinished': []}
            with self.assertRaisesRegex(ValueError, 'rendered chapter WAV'):
                verify_export_group(group, 5)

    def test_ffmpeg_makes_a_single_m4b_with_three_chapter_markers(self):
        """Exercise real FFmpeg chapter markers on WAVs assembled from separate parts."""
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            wav_dir = folder / 'chapters'
            wav_dir.mkdir()
            sr, samples = 24000, 24000
            manifest = {'chapters': {}}
            for index in (3, 4, 5):
                wav = wav_dir / f'{index:04d}.wav'
                with wave.open(str(wav), 'wb') as output:
                    output.setnchannels(1)
                    output.setsampwidth(2)
                    output.setframerate(sr)
                    # A second of silence per section is sufficient to verify
                    # timestamps, encoding, and chapter count without GPU use.
                    output.writeframes(b'\x00\x00' * samples)
                manifest['chapters'][str(index)] = {
                    'sample_rate': sr, 'samples': samples, 'title': f'Chapter {index}', 'wav': str(wav)}
            (folder / 'manifest.json').write_text(json.dumps(manifest))
            group = {'folder': folder, 'voice_path': None, 'indices': [5, 3, 4], 'unfinished': []}
            # Planner always sorts indices before they reach the exporter.
            group['indices'].sort()
            chosen = verify_export_group(group, 5)
            sections = [converter.Section(i, f'Chapter {i}', [], 'test.xhtml') for i in range(1, 6)]
            epub = folder / 'book.epub'
            exported = converter.export_m4b(epub, 'Test Book', 'Author', sections, chosen,
                                             folder, 'Test_Book_COMPLETE')
            self.assertTrue(exported.is_file())
            # A readable queued part basename must survive converter sanitizing
            # unchanged, while legacy _COMPLETE exports remain discoverable.
            friendly = converter.export_m4b(
                epub, 'Test Book', 'Author', sections, chosen, folder,
                'Test Book - Part 01 (Sections 3-5)')
            self.assertEqual(friendly.name, 'Test Book - Part 01 (Sections 3-5).m4b')
            self.assertTrue(exported.is_file())
            probe = subprocess.run(['ffprobe', '-v', 'error', '-show_chapters',
                                    '-show_entries', 'chapter=start_time,end_time:chapter_tags=title',
                                    '-of', 'json', str(exported)], capture_output=True,
                                   text=True, check=True)
            chapters = json.loads(probe.stdout)['chapters']
            self.assertEqual(len(chapters), 3)
            self.assertEqual([c['tags']['title'] for c in chapters],
                             ['Chapter 3', 'Chapter 4', 'Chapter 5'])
            self.assertFalse(list(folder.glob('*.partial.m4b')))
            self.assertTrue(all((wav_dir / f'{index:04d}.wav').exists() for index in chosen))


if __name__ == '__main__':
    unittest.main()
