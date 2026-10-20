"""Regression tests for bounded GUI updates on multi-hour audiobook jobs.

No QWidgets, GPU, or model files are created. The GUI's actual paint/update
methods are called with thin Python stand-ins to count native setter traffic.
"""
from __future__ import annotations

from collections import deque
from pathlib import Path
from types import SimpleNamespace
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from audiobookai_gui_throttle import (progress_snapshot, routine_speech_log,
                                      chunk_source_log, BufferedGenerationLog)


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
    def test_chunk_text_classification(self):
        self.assertTrue(chunk_source_log("173/200: The army approached."))
        self.assertFalse(chunk_source_log("Generated 5.0s in 8.2s"))
        self.assertFalse(chunk_source_log("ERROR: generator failed"))

    def test_buffered_log_keeps_compact_and_detailed_history(self):
        buffer = BufferedGenerationLog(history_limit=20, pending_limit=10)
        buffer.append("Chapter 172 begun")
        buffer.append("173/200: The army approached.", detailed=True)
        buffer.append("Using cached audio.", detailed=True)
        buffer.append("Chunk 173 complete", detailed=True)
        buffer.append("A warning remains visible")
        self.assertEqual(buffer.drain(), "Chapter 172 begun\nA warning remains visible")
        self.assertEqual(buffer.drain(detailed=True), "")
        self.assertEqual(buffer.replay(detailed=True).splitlines(),
                         ["Chapter 172 begun", "173/200: The army approached.",
                          "Using cached audio.", "Chunk 173 complete",
                          "A warning remains visible"])
        self.assertEqual(buffer.replay().splitlines(),
                         ["Chapter 172 begun", "A warning remains visible"])
        buffer.clear()
        self.assertEqual(buffer.replay(detailed=True), "")

    def test_bounded_log_overflow_warns_and_preserves_latest(self):
        buffer = BufferedGenerationLog(history_limit=4, pending_limit=3)
        for i in range(10):
            buffer.append(f"Chunk {i}", detailed=True)
        result = buffer.drain(detailed=True)
        self.assertIn("7 older log lines omitted", result)
        self.assertIn("Chunk 9", result)
        self.assertNotIn("Chunk 1\n", result)
        self.assertEqual(buffer.replay(detailed=True).splitlines(),
                         ["Chunk 6", "Chunk 7", "Chunk 8", "Chunk 9"])

    def test_gui_batches_messages_and_replays_verbose_lines(self):
        from audiobook_gui_v2 import Studio

        class FakePane:
            def __init__(self):
                self.appended = []
                self.replaced = []
            def appendPlainText(self, text):
                self.appended.append(text)
            def setPlainText(self, text):
                self.replaced.append(text)

        class FakePreferences:
            def __init__(self):
                self.saved = []
            def setValue(self, key, value):
                self.saved.append((key, value))

        pane = FakePane()
        label = FakeWidget()
        log_buffer = BufferedGenerationLog()
        fake = SimpleNamespace(
            generation_logs=log_buffer, proc=object(), logs=pane,
            active_job_log=None, log_detailed=False,
            prefs=FakePreferences(), log_mode_button=label)
        fake.flush_generation_logs = lambda: Studio.flush_generation_logs(fake)

        for i in range(100):
            Studio.write_log(fake, f"{i}/100: Narration text", gui=False, detailed=True)
            Studio.write_log(fake, f"chunk {i} complete", gui=False, detailed=True)
        Studio.write_log(fake, "Chapter progress saved")
        self.assertEqual(pane.appended, [])
        Studio.flush_generation_logs(fake)
        self.assertEqual(pane.appended, ["Chapter progress saved"])
        Studio.toggle_log_mode(fake)
        self.assertTrue(fake.log_detailed)
        self.assertEqual(fake.prefs.saved, [("detailed_generation_log", True)])
        self.assertIn("99/100: Narration text", pane.replaced[-1])
        self.assertEqual(label.writes, ["Compact view"])
        Studio.write_log(fake, "100/100: Ending", gui=False, detailed=True)
        Studio.flush_generation_logs(fake)
        self.assertEqual(pane.appended[-1], "100/100: Ending")
        Studio.toggle_log_mode(fake)
        self.assertFalse(fake.log_detailed)
        self.assertNotIn("99/100: Narration text", pane.replaced[-1])
        self.assertIn("Chapter progress saved", pane.replaced[-1])

    def test_stdout_keeps_numbered_chunks_for_detailed_display(self):
        from audiobook_gui_v2 import Studio

        class FakeProcess:
            def readAllStandardOutput(self):
                return (b"173/200: The army advanced.\nUsing cached audio.\n"
                        b"Generated 3.00s in 4.00s\nCUDA out of memory\n")

        received = []
        fake = SimpleNamespace(
            proc=FakeProcess(), buffer="", diagnostic_lines=deque(maxlen=40),
            write_log=lambda text, **kwargs: received.append((text, kwargs)))
        Studio.consume_output(fake)
        self.assertEqual(fake.buffer, "")
        self.assertEqual(len(received), 4)
        self.assertTrue(all(p["detailed"] for _, p in received[:3]))
        self.assertFalse(received[-1][1]["detailed"])
        self.assertIn("CUDA out of memory", fake.diagnostic_lines)
        self.assertNotIn("173/200: The army advanced.", fake.diagnostic_lines)

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

    def test_recent_chunk_eta_accounts_for_cached_audio(self):
        plan = {1: {'chunks': 20, 'title': 'Test'}}
        done = {1: 10}
        # Fresh synthesis may be 8s/chunk; recent cache reads are much faster.
        view, eta = progress_snapshot(plan, done, 1, False, [8.0],
                                      recent_chunk_durations=[0.04] * 8 + [8.0, 8.0])
        self.assertEqual(view['stat_chunks'], 'Chunks: 10 / 20')
        self.assertAlmostEqual(eta, 16.32)
        _, fully_cached = progress_snapshot(plan, done, 1, False, [],
                                            recent_chunk_durations=[0.02] * 10)
        self.assertAlmostEqual(fully_cached, 0.2)
        _, completed = progress_snapshot(plan, {1: 20}, 1, False, [],
                                         recent_chunk_durations=[0.02])
        self.assertEqual(completed, 0)

    def test_missing_epub_clears_stale_generate_selection_even_when_cached(self):
        from audiobook_gui_v2 import Studio

        class FakeTable:
            def __init__(self):
                self.rows = 2
            def setRowCount(self, rows):
                self.rows = rows

        with tempfile.TemporaryDirectory() as tmp:
            lost_epub = Path(tmp) / 'disconnected.epub'
            table = FakeTable()
            fake = SimpleNamespace(
                gen_book=SimpleNamespace(currentData=lambda: 42),
                store=SimpleNamespace(book=lambda _book_id: {'path': str(lost_epub)}),
                book_cache={42: ('Cached book', 'Author', ['stale chapter'])},
                current_book_id=7, sections=['previous chapter'],
                section_table=table, gen_details=FakeWidget(),
                gen_selected=FakeWidget(), gen_saved_parts=FakeWidget(),
                error=lambda *args: None)
            fake.clear_generation_selection = lambda: Studio.clear_generation_selection(fake)
            fake.refresh_saved_parts = lambda: Studio.refresh_saved_parts(fake)
            Studio.book_combo_changed(fake)
            self.assertIsNone(fake.current_book_id)
            self.assertEqual(fake.sections, [])
            self.assertEqual(table.rows, 0)
            self.assertEqual(fake.gen_selected.text(), '0 selected')
            self.assertIn('unavailable', fake.gen_details.text().lower())

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
            recent_chunk_durations=deque([0.03, 4.0], maxlen=120),
            total_chunks=100,
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
            chunk_durations=deque(maxlen=40),
            recent_chunk_durations=deque(maxlen=120), _last_paint=100.0,
            paint_progress=lambda: calls.append("paint"),
            persist_progress=lambda force=False: None,
            write_log=lambda *args, **kwargs: None)
        with patch("audiobook_gui_v2.time.monotonic", return_value=100.49):
            for n in range(1, 1200):
                Studio.progress_event(fake, {
                    "event": "chunk_done", "index": 1, "chunk": n,
                    "total": 10000, "seconds": 0.02})
        self.assertEqual(len(calls), 0)
        with patch("audiobook_gui_v2.time.monotonic", return_value=100.5):
            Studio.progress_event(fake, {
                "event": "chunk_done", "index": 1, "chunk": 1200,
                "total": 10000, "seconds": 0.02})
        self.assertEqual(len(calls), 1)
        self.assertEqual(fake.done_chunks[1], 1200)


if __name__ == "__main__":
    unittest.main()
