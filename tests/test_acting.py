"""Regression tests for the opt-in same-narrator dialogue performance mode."""
from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))

from audiobookai_acting import (
    chunk_settings, recorded_folders, validate_acting_mode,
    EXPRESSIVE_EXAGGERATION,
)
from audiobookai_storage import Store
from audiobookai_fullbook import collect_export_groups
from audiobookai_headless import checked_job
import epub_to_audiobook_v7_studio as backend


class Source:
    index, title, text, source = 1, "First chapter", '"We made it," he said.', "chapter.xhtml"


class SingleNarratorActingTests(unittest.TestCase):
    def test_mode_is_explicit_and_preserves_normal_reading(self):
        self.assertEqual(validate_acting_mode("standard"), "standard")
        self.assertEqual(chunk_settings("standard", True), {})
        self.assertEqual(chunk_settings("expressive", False), {})
        self.assertEqual(chunk_settings("expressive", True)["exaggeration"],
                         EXPRESSIVE_EXAGGERATION)
        with self.assertRaises(ValueError):
            validate_acting_mode("full-cast")

    def test_expressive_edition_is_isolated_from_legacy_audio(self):
        with tempfile.TemporaryDirectory() as directory:
            p = Path(directory) / "novel.epub"
            p.touch()
            with patch.object(backend, "OUT_ROOT", Path(directory) / "output"):
                original = backend.book_folder(p, acting_mode="standard")
                expressive = backend.book_folder(p, acting_mode="expressive")
                self.assertNotEqual(original, expressive)
                self.assertEqual(expressive, original / "performances" / "expressive")
                self.assertEqual(backend.book_folder(p), original)
                (original / "manifest.json").parent.mkdir(parents=True)
                (original / "manifest.json").write_text("{}")
                (expressive / "manifest.json").parent.mkdir(parents=True)
                (expressive / "manifest.json").write_text("{}")
                self.assertIn(expressive, recorded_folders(original))

    def test_immutable_queue_selection_survives_restart_and_receipt(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "book.epub"
            source.write_bytes(b"epub")
            db_path = root / "output" / "jobs.sqlite3"
            store = Store(db_path)
            try:
                book = store.import_book(source, "Novel", "Author", [Source()])
                legacy = store.add_job(book, "Part 1", [1], "normal_part")
                expressive = store.add_job(book, "Part 2", [1], "expressive_part",
                                           acting_mode="expressive")
                self.assertEqual(store.job(legacy)["acting_mode"], "standard")
                self.assertEqual(store.job(expressive)["acting_mode"], "expressive")
                args = checked_job(dict(store.job(expressive)))
                self.assertEqual(args[args.index("--acting-mode") + 1], "expressive")
                self.assertNotIn("--acting-mode", checked_job(dict(store.job(legacy))))
                exported = root / "expressive_part.m4b"
                exported.write_bytes(b"m4b" * 1024)
                store.complete_job(expressive, exported)
                self.assertEqual(store.completed_parts(book)[0]["acting_mode"], "expressive")
                store.remove_job(expressive)
                saved = store.recorded_jobs_for_export(book)
                self.assertEqual(saved[0]["acting_mode"], "expressive")
                groups = collect_export_groups(
                    [dict(row) for row in store.jobs()] + saved, book,
                    lambda p, voice, mode="standard": root / mode)
                self.assertEqual({g["acting_mode"] for g in groups},
                                 {"standard", "expressive"})
                self.assertEqual(len(groups), 2)
            finally:
                store.close()
            reopened = Store(db_path)
            try:
                self.assertEqual(reopened.job(legacy)["acting_mode"], "standard")
                self.assertEqual(reopened.completed_parts(book)[0]["acting_mode"], "expressive")
            finally:
                reopened.close()

    def test_engine_uses_same_reference_and_separate_dialogue_cache(self):
        # Import actual V7 chunker with only the heavyweight Chatterbox module faked.
        original = {k: sys.modules.get(k) for k in ("chatterbox", "chatterbox.tts")}
        chatterbox = types.ModuleType("chatterbox")
        chatterbox.__path__ = []
        tts = types.ModuleType("chatterbox.tts")
        tts.ChatterboxTTS = type("ChatterboxTTS", (), {})
        sys.modules["chatterbox"] = chatterbox
        sys.modules["chatterbox.tts"] = tts
        try:
            spec = importlib.util.spec_from_file_location(
                "acting_test_engine", SCRIPTS / "test_five_minutes_v7_smooth.py")
            engine = importlib.util.module_from_spec(spec)
            sys.modules[spec.name] = engine
            spec.loader.exec_module(engine)
            with tempfile.TemporaryDirectory() as directory:
                sample = Path(directory) / "narrator.wav"
                sample.write_bytes(b"reference recording")
                with patch.object(engine, "VOICE_FILE", sample):
                    engine.ACTING_MODE = "standard"
                    original_spoken = engine.cache_key('"Hello!"')
                    original_prose = engine.cache_key("The storm was coming.")
                    engine.ACTING_MODE = "expressive"
                    self.assertNotEqual(original_spoken, engine.cache_key('"Hello!"'))
                    self.assertEqual(original_prose, engine.cache_key("The storm was coming."))
                    self.assertEqual(engine.delivery_settings("The storm was coming."),
                                     (engine.EXAGGERATION, engine.CFG_WEIGHT))
                    self.assertEqual(engine.delivery_settings('"Hello!"')[0],
                                     EXPRESSIVE_EXAGGERATION)
                    self.assertEqual(engine.VOICE_FILE, sample)
        finally:
            sys.modules.pop("acting_test_engine", None)
            for name, saved in original.items():
                if saved is None:
                    sys.modules.pop(name, None)
                else:
                    sys.modules[name] = saved


if __name__ == "__main__":
    unittest.main()
