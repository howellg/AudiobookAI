"""Regression coverage for the V2.6 archive catalog and offline M4B packaging."""
from __future__ import annotations
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
import zipfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import audiobookai_archive as arc


class ArchiveTests(unittest.TestCase):
    def test_catalog_records_are_isolated_by_voice_and_persist(self):
        """Two narrators on the same EPUB must keep independent metadata."""
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            catalog = root / 'catalog.json'
            epub = root / 'book.epub'
            arc.save_edition(epub, root / 'voice_a', {'title': 'Edition A'}, catalog)
            arc.save_edition(epub, root / 'voice_b', {'title': 'Edition B'}, catalog)
            self.assertEqual(arc.edition_info(epub, root / 'voice_a', catalog)['title'], 'Edition A')
            self.assertEqual(arc.edition_info(epub, root / 'voice_b', catalog)['title'], 'Edition B')
            self.assertEqual(len(arc.load_catalog(catalog)['editions']), 2)

    def test_corrupt_catalog_is_not_silently_discarded(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / 'catalog.json'
            path.write_text('{not valid json')
            with self.assertRaises(ValueError):
                arc.load_catalog(path)

    def test_epub2_and_epub3_covers_are_recognized(self):
        """Extract declared artwork without trusting arbitrary ZIP members."""
        for modern in (False, True):
            with self.subTest(modern=modern), tempfile.TemporaryDirectory() as d:
                epub = Path(d) / 'book.epub'
                container = '<container xmlns="urn:oasis:names:tc:opendocument:xmlns:container"><rootfiles><rootfile full-path="OEBPS/content.opf"/></rootfiles></container>'
                opf = ('<package xmlns="http://www.idpf.org/2007/opf"><metadata>'
                       + ('' if modern else '<meta name="cover" content="cover"/>')
                       + '</metadata><manifest><item id="cover" href="images/front.jpg" media-type="image/jpeg"'
                       + (' properties="cover-image"' if modern else '')
                       + '/></manifest></package>')
                payload = b'\xff\xd8\xfffakejpegdata'
                with zipfile.ZipFile(epub, 'w') as z:
                    z.writestr('META-INF/container.xml', container)
                    z.writestr('OEBPS/content.opf', opf)
                    z.writestr('OEBPS/images/front.jpg', payload)
                self.assertEqual(arc.extract_epub_cover(epub), (payload, '.jpg'))

    def test_complete_source_gets_stale_when_chapter_wav_is_repaired(self):
        with tempfile.TemporaryDirectory() as d:
            folder = Path(d)
            (folder / 'chapters').mkdir()
            wav = folder / 'chapters' / '0002.wav'
            wav.write_bytes(b'wav')
            self.assertTrue(arc.needs_complete_export(folder, 'Test_COMPLETE', [2]))
            source = folder / 'Test_COMPLETE.m4b'
            source.write_bytes(b'audio')
            os.utime(wav, ns=(1_000_000_000, 1_000_000_000))
            os.utime(source, ns=(2_000_000_000, 2_000_000_000))
            self.assertFalse(arc.needs_complete_export(folder, 'Test_COMPLETE', [2]))
            os.utime(wav, ns=(3_000_000_000, 3_000_000_000))
            self.assertTrue(arc.needs_complete_export(folder, 'Test_COMPLETE', [2]))

    def test_copy_preserves_chapters_and_embeds_artwork(self):
        """Real FFmpeg muxing must retain source chapter markers and AAC audio."""
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            source = root / 'original.m4b'
            metadata = root / 'chapters.ffmetadata'
            metadata.write_text(';FFMETADATA1\ntitle=Original\n[CHAPTER]\nTIMEBASE=1/1000\nSTART=0\nEND=1000\ntitle=First\n[CHAPTER]\nTIMEBASE=1/1000\nSTART=1000\nEND=2000\ntitle=Second\n')
            audio = root / 'tone.wav'
            subprocess.run(['ffmpeg', '-hide_banner', '-loglevel', 'error', '-y',
                            '-f', 'lavfi', '-i', 'sine=frequency=440:duration=2',
                            '-ar', '24000', str(audio)], check=True)
            subprocess.run(['ffmpeg', '-hide_banner', '-loglevel', 'error', '-y', '-i', str(audio),
                            '-f', 'ffmetadata', '-i', str(metadata), '-map', '0:a:0', '-map_chapters', '1',
                            '-c:a', 'aac', str(source)], check=True)
            cover = root / 'cover.png'
            subprocess.run(['ffmpeg', '-hide_banner', '-loglevel', 'error', '-y',
                            '-f', 'lavfi', '-i', 'color=red:size=64x64', '-frames:v', '1', str(cover)], check=True)
            destination = root / 'archive' / 'finished.m4b'
            arc.archive_copy(source, destination, {'title': 'Better Title', 'author': 'Writer',
                                                   'series': 'Example', 'volume': '2', 'narrator': 'Reader'}, cover)
            result = arc._probe(destination)
            self.assertEqual(len(result['chapters']), 2)
            self.assertEqual(result['format']['tags']['title'], 'Better Title')
            self.assertEqual(result['format']['tags']['artist'], 'Writer')
            self.assertTrue(any(s.get('codec_type') == 'video' for s in result['streams']))
            self.assertEqual(next(s['codec_name'] for s in result['streams'] if s['codec_type'] == 'audio'), 'aac')
            self.assertTrue(source.exists())

    def test_failed_archive_does_not_replace_existing_destination(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            old = root / 'old.m4b'
            old.write_bytes(b'not a valid audiobook')
            destination = root / 'published.m4b'
            destination.write_bytes(b'prior good edition')
            with self.assertRaises(subprocess.CalledProcessError):
                arc.archive_copy(old, destination, {'title': 'test'})
            self.assertEqual(destination.read_bytes(), b'prior good edition')


if __name__ == '__main__':
    unittest.main()