"""AudiobookAI V2.10.1 offline-first model tests: no GPU, Hub or network."""
from __future__ import annotations

import io
import os
import sys
import tempfile
import types
import unittest
from contextlib import redirect_stdout, redirect_stderr
from pathlib import Path
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))
import audiobookai_models as models


class LocalEntryNotFoundError(Exception):
    pass


class OfflineModelsTests(unittest.TestCase):
    def test_default_is_offline_without_overwriting_an_explicit_override(self):
        empty = {}
        self.assertEqual(models.prefer_local_models(empty), "1")
        self.assertEqual(empty, {"HF_HUB_OFFLINE": "1"})
        explicit = {"HF_HUB_OFFLINE": "0"}
        self.assertEqual(models.prefer_local_models(explicit), "0")
        self.assertEqual(explicit["HF_HUB_OFFLINE"], "0")

    def test_model_loading_inherits_offline_flag_without_network(self):
        class FakeModel:
            @staticmethod
            def from_pretrained(device):
                self.assertEqual(os.environ["HF_HUB_OFFLINE"], "1")
                self.assertEqual(device, "cuda")
                return "fake-engine"
        fake = types.ModuleType("chatterbox.tts")
        fake.ChatterboxTTS = FakeModel
        parent = types.ModuleType("chatterbox")
        parent.__path__ = []
        with patch.dict(sys.modules, {"chatterbox": parent, "chatterbox.tts": fake}):
            with patch.dict(os.environ, {}, clear=True):
                self.assertEqual(models.load_chatterbox("cuda"), "fake-engine")

    def test_missing_cached_model_gives_friendly_actionable_error(self):
        class FakeModel:
            @staticmethod
            def from_pretrained(device):
                raise LocalEntryNotFoundError("no cached weights")
        fake = types.ModuleType("chatterbox.tts")
        fake.ChatterboxTTS = FakeModel
        parent = types.ModuleType("chatterbox")
        parent.__path__ = []
        with patch.dict(sys.modules, {"chatterbox": parent, "chatterbox.tts": fake}):
            with patch.dict(os.environ, {"HF_HUB_OFFLINE": "1"}):
                with self.assertRaisesRegex(RuntimeError, "Download missing files"):
                    models.load_chatterbox("cpu")

    def test_cuda_failure_is_not_mislabeled_as_missing_model(self):
        class FakeModel:
            @staticmethod
            def from_pretrained(device):
                raise RuntimeError("CUDA out of memory")
        fake = types.ModuleType("chatterbox.tts")
        fake.ChatterboxTTS = FakeModel
        parent = types.ModuleType("chatterbox")
        parent.__path__ = []
        with patch.dict(sys.modules, {"chatterbox": parent, "chatterbox.tts": fake}):
            with patch.dict(os.environ, {"HF_HUB_OFFLINE": "1"}):
                with self.assertRaisesRegex(RuntimeError, "CUDA out of memory"):
                    models.load_chatterbox("cuda")

    def test_check_uses_local_files_only_and_reports_missing_items(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fetched = []
            def fake_download(*, repo_id, filename, local_files_only=False):
                fetched.append((repo_id, filename, local_files_only))
                if filename == "conds.pt":
                    raise LocalEntryNotFoundError("missing")
                path = root / filename
                path.write_bytes(b"cached weights")
                return str(path)
            fake_hub = types.ModuleType("huggingface_hub")
            fake_hub.hf_hub_download = fake_download
            with patch.dict(sys.modules, {"huggingface_hub": fake_hub}):
                available, missing = models.check_local_models()
            self.assertEqual(set(available), set(models.MODEL_FILES) - {"conds.pt"})
            self.assertEqual(missing, ["conds.pt"])
            self.assertEqual(len(fetched), 5)
            self.assertTrue(all(t[2] for t in fetched))
            self.assertTrue(all(t[0] == models.MODEL_REPO for t in fetched))

    def test_download_and_refresh_are_explicit_separate_online_actions(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for name in models.MODEL_FILES:
                (root / name).write_bytes(b"weights")
            calls = []
            def fake_snapshot(**kwargs):
                calls.append((dict(kwargs), os.environ["HF_HUB_OFFLINE"]))
                return str(root)
            fake_hub = types.ModuleType("huggingface_hub")
            fake_hub.snapshot_download = fake_snapshot
            with patch.dict(sys.modules, {"huggingface_hub": fake_hub}):
                with patch.dict(os.environ, {"HF_HUB_OFFLINE": "1"}):
                    found, missing = models.download_models(refresh=False)
                    self.assertEqual(missing, [])
                    self.assertEqual(len(found), len(models.MODEL_FILES))
                with patch.dict(os.environ, {"HF_HUB_OFFLINE": "1"}):
                    found, missing = models.download_models(refresh=True)
                    self.assertEqual(missing, [])
            self.assertEqual([value["force_download"] for value, _ in calls], [False, True])
            self.assertEqual([offline for _, offline in calls], ["0", "0"])
            self.assertTrue(all(set(kwargs["allow_patterns"]) == set(models.MODEL_FILES)
                                for kwargs, _ in calls))

    def test_model_cli_check_requires_no_network(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fake_hub = types.ModuleType("huggingface_hub")
            def fake_download(*, filename, local_files_only, **kwargs):
                self.assertTrue(local_files_only)
                path = root / filename
                path.write_bytes(b"weights")
                return str(path)
            fake_hub.hf_hub_download = fake_download
            output = io.StringIO()
            with patch.dict(sys.modules, {"huggingface_hub": fake_hub}):
                with patch.dict(os.environ, {"HF_HUB_OFFLINE": "1"}):
                    with redirect_stdout(output):
                        self.assertEqual(models.main(["check"]), 0)
            self.assertIn("Future generation can run offline", output.getvalue())


if __name__ == "__main__":
    unittest.main()
