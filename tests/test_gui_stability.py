"""Regression tests for bounded GUI updates on multi-hour audiobook jobs.

No QWidgets, GPU, or model files are created. The GUI's actual paint/update
methods are called with thin Python stand-ins to count native setter traffic.
"""
from __future__ import annotations

from collections import deque
from pathlib import Path
from types import SimpleNamespace
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from audiobookai_gui_throttle import progress_snapshot, routine_speech_log


class FakeWidget:
    def __init__(self):
        self.writes = []
    def setText(self, text):
        self.writes.append(text)
    def setValue(self, value):
        self.writes.append(value)
    def text(self):
        return self.writes[-1] if self.writes else ""


class GuiStabilityTests(unittest.TestCase):
    def test_only_expected_per_chunk_chatter_is_filtered(self):
        self.assertTrue(routine_speech_log("Using cached audio."))
        self.assertTrue(routine_speech_log("Generated 3.25s in 8.11s"))
        for urgent in ("Unusable cached chunk (silence); regenerating.",
                       "CUDA out of memory",
                       "Audio review warning: clipping",
                       "Generation failed (exit code 1)",
                       "Cache read failed: bad format; attempting regeneration.",
                       "Generated silence due to warning"):
            self.assertFalse(routine_speech_log(urgent))

    def test_progress_snapshot_bounds_and_computes_eta(self):
        plan = {1: {"chunks": 10, "title": "Beginning"},
                2: {"chunks": 20, "title": "End"}}
        view, eta = progress_snapshot(plan, {1: 10, 2: 5}, 2, False, [2., 3.])
        self.assertEqual(view["overall_progress"], 490)
        self.assertEqual(view["chapter_progress"], 250)
        self.assertEqual(view["stat_chunks"], "Chunks: 15 / 30")
        self.assertEqual(eta, 37.5)
        no_view, no_eta = progress_snapshot({}, {}, None, False, [])
        self.assertEqual(no_view, {})
        self.assertIsNone(no_eta)

    def test_export_does_not_override_encoding_ui(self):
        plan = {1: {"chunks": 10, "title": "Beginning"}}
        view, eta = progress_snapshot(plan, {1: 10}, 1, True, [2.])
        self.assertNotIn("chapter_text", view)
        self.assertNotIn("chapter_progress", view)
        self.assertIsNone(eta)

    def test_native_widget_setters_are_skipped_when_state_is_unchanged(self):
        # Import Qt bindings but do not make QApplication/QWidget: this is an
        # isolated test of Studio.paint_progress's setter minimization.
        from audiobook_gui_v2 import Studio
        names = ("overall_progress", "overall_text", "stat_chunks",
                 "chapter_progress", "chapter_text", "stat_eta")
        fake = SimpleNamespace(
            plan={1: {"chunks": 100, "title": "Chapter"}},
            done_chunks={1: 15}, current_section=1, exporting=False,
            chunk_durations=deque([4.0], maxlen=40),
            _progress_last_values={},
            **{name: FakeWidget() for name in names})
        Studio.paint_progress(fake)
        first = {name: len(getattr(fake, name).writes) for name in names}
        Studio.paint_progress(fake)
        self.assertEqual(first,
            {name: len(getattr(fake, name).writes) for name in names})
        fake.done_chunks[1] = 16
        Studio.paint_progress(fake)
        self.assertEqual(len(fake.stat_chunks.writes), 2)
        self.assertEqual(len(fake.chapter_text.writes), 2)

    def test_elapsed_clock_advances_without_redundant_widget_updates(self):
        from audiobook_gui_v2 import Studio
        label = FakeWidget()
        fake = SimpleNamespace(
            proc=object(), started_at=100.0, stat_elapsed=label)
        with patch("audiobook_gui_v2.time.monotonic", return_value=100.2):
            Studio.tick(fake)
        with patch("audiobook_gui_v2.time.monotonic", return_value=100.8):
            Studio.tick(fake)
        with patch("audiobook_gui_v2.time.monotonic", return_value=161.0):
            Studio.tick(fake)
        self.assertEqual(label.writes, ["Elapsed: 0m 00s", "Elapsed: 1m 01s"])
        fake.proc = None
        with patch("audiobook_gui_v2.time.monotonic", return_value=180.0):
            Studio.tick(fake)
        self.assertEqual(len(label.writes), 2)

    def test_bursty_chunk_events_do_not_continuously_repaint(self):
        from audiobook_gui_v2 import Studio
        calls = []
        fake = SimpleNamespace(
            plan={1: {"chunks": 10000, "title": "Long chapter"}},
            total_chunks=10000, done_chunks={1: 0}, current_section=1,
            current_chunk=0, current_total=10000, exporting=False,
            chunk_durations=deque(maxlen=40), _last_paint=100.0,
            paint_progress=lambda: calls.append("paint"),
            persist_progress=lambda force=False: None)
        with patch("audiobook_gui_v2.time.monotonic", return_value=100.5):
            for n in range(1, 1200):
                Studio.progress_event(fake, {
                    "event": "chunk_done", "index": 1, "chunk": n,
                    "total": 10000, "seconds": 0.02})
        self.assertEqual(len(calls), 0)
        with patch("audiobook_gui_v2.time.monotonic", return_value=102.2):
            Studio.progress_event(fake, {
                "event": "chunk_done", "index": 1, "chunk": 1200,
                "total": 10000, "seconds": 0.02})
        self.assertEqual(len(calls), 1)
        self.assertEqual(fake.done_chunks[1], 1200)


if __name__ == "__main__":
    unittest.main()
