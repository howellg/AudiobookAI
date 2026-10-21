"""Offline FFmpeg progress parsing and GUI streaming regression tests."""
from __future__ import annotations

import io
import json
import sys
import tempfile
import unittest
import wave
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))

from audiobookai_export_progress import parse_ffmpeg_clock, export_progress_metrics
import epub_to_audiobook_v7_studio as converter


class ProgressParserTests(unittest.TestCase):
    def test_real_ffmpeg_clock_and_microsecond_fallback(self):
        self.assertEqual(parse_ffmpeg_clock('01:02:03.5'), 3723.5)
        self.assertIsNone(parse_ffmpeg_clock('01:61:00'))
        self.assertIsNone(parse_ffmpeg_clock('-01:00:00'))
        self.assertIsNone(parse_ffmpeg_clock('N/A'))
        self.assertIsNone(parse_ffmpeg_clock('00:00:nan'))
        item = export_progress_metrics({'out_time': '00:02:00.500000',
                                        'speed': '1.73x'}, 241)
        self.assertEqual(item['percent'], 50)
        self.assertEqual(item['encoded_seconds'], 120.5)
        self.assertEqual(item['speed'], 1.73)
        item = export_progress_metrics({'out_time_us': '15000000'}, 60)
        self.assertEqual(item['percent'], 25)
        item = export_progress_metrics({'out_time_ms': '15000000'}, 60)
        self.assertEqual(item['percent'], 25)

    def test_progress_monotonic_and_reserves_100_for_verified_m4b(self):
        progress = export_progress_metrics({'out_time': '00:01:00.000'},
                                           100, 77)
        self.assertEqual(progress['percent'], 77)
        progress = export_progress_metrics({'out_time': '00:20:00.000'},
                                           100, 93)
        self.assertEqual(progress['percent'], 99)
        self.assertIsNone(export_progress_metrics({'out_time': 'N/A'}, 100))
        self.assertIsNone(export_progress_metrics({'out_time': '00:01:00'}, 0))
        self.assertIsNone(export_progress_metrics({'out_time': '00:01:00'}, float('inf')))
        self.assertIsNone(export_progress_metrics({'out_time_us': '-20'}, 100))
        self.assertIsNone(export_progress_metrics({'out_time': '00:01:00'}, 'nope'))

    def test_real_ffmpeg_generates_progress_without_rerunning_tts(self):
        """A two-chapter export must emit time-based progress in the real path."""
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            chapter_dir = folder / 'chapters'
            chapter_dir.mkdir()
            chapters = {}
            sample_rate = 24000
            for idx in (1, 2):
                wav = chapter_dir / f'{idx:04d}.wav'
                with wave.open(str(wav), 'wb') as audio:
                    audio.setnchannels(1)
                    audio.setsampwidth(2)
                    audio.setframerate(sample_rate)
                    audio.writeframes(b'\0\0' * sample_rate)
                chapters[str(idx)] = {'sample_rate': sample_rate,
                                      'samples': sample_rate}
            (folder / 'manifest.json').write_text(json.dumps({'chapters': chapters}))
            sections = [converter.Section(i, f'Chapter {i}', [], 'test.xhtml')
                        for i in (1, 2)]
            output = io.StringIO()
            previous = converter.STUDIO_EVENTS
            converter.STUDIO_EVENTS = True
            try:
                with redirect_stdout(output):
                    result = converter.export_m4b(
                        folder / 'book.epub', 'Progress Book', 'Author',
                        sections, [1, 2], folder, 'Progress_Book_COMPLETE',
                        report_progress=True)
            finally:
                converter.STUDIO_EVENTS = previous
            self.assertTrue(result.is_file())
            events = [json.loads(line.split('AUDIOBOOKAI_EVENT ', 1)[1])
                      for line in output.getvalue().splitlines()
                      if line.startswith('AUDIOBOOKAI_EVENT ')]
            self.assertEqual(events[0]['event'], 'export_started')
            self.assertEqual(events[-1]['event'], 'export_done')
            updates = [event for event in events
                       if event['event'] == 'export_progress']
            self.assertTrue(updates, output.getvalue())
            self.assertTrue(all(0 <= event['percent'] <= 99 for event in updates))
            self.assertEqual(events[0]['duration_seconds'], 2)


class GuiStreamingTests(unittest.TestCase):
    def test_fragmented_events_update_progress_and_preserve_errors(self):
        from audiobook_gui_v2 import Studio

        class FakeOutput:
            def __init__(self):
                self.pieces = []
            def readAllStandardOutput(self):
                return self.pieces.pop(0)

        class FakeBar:
            def __init__(self):
                self.values = []
            def setValue(self, value):
                self.values.append(value)

        output = FakeOutput()
        bar = FakeBar()
        logs = []
        view = SimpleNamespace(
            full_export_proc=output, full_export_buffer='',
            full_export_total_seconds=0.0,
            full_export_encoded_seconds=0.0,
            full_export_percent=0, full_export_speed=None,
            full_export_progress=bar,
            write_log=lambda line: logs.append(line),
            update_full_export_progress_display=lambda: None)
        event = ('AUDIOBOOKAI_EVENT ' + json.dumps({
            'event': 'export_progress', 'percent': 36,
            'encoded_seconds': 108, 'duration_seconds': 300,
            'speed': 1.4}) + '\n')
        output.pieces.append(event[:24].encode())
        Studio.read_complete_export_output(view)
        self.assertEqual(bar.values, [])
        output.pieces.append((event[24:] + 'ffmpeg failed: disk full\n').encode())
        Studio.read_complete_export_output(view)
        self.assertEqual(bar.values, [36])
        self.assertEqual(view.full_export_encoded_seconds, 108)
        self.assertEqual(view.full_export_speed, 1.4)
        self.assertEqual(logs, ['ffmpeg failed: disk full'])


if __name__ == '__main__':
    unittest.main()
