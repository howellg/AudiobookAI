"""Chatterbox model loading: offline by default, with explicit online maintenance.

This module deliberately imports huggingface_hub / Chatterbox only inside
functions. HF_HUB_OFFLINE must be set BEFORE those libraries are imported,
because Hugging Face reads some settings at import time.

The normal converter uses only local model weights; model downloads and refresh
are explicit, separate actions launched from Studio Settings or the CLI.
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

MODEL_REPO = "ResembleAI/chatterbox"
MODEL_FILES = (
    "ve.safetensors",
    "t3_cfg.safetensors",
    "s3gen.safetensors",
    "tokenizer.json",
    "conds.pt",
)


def prefer_local_models(environ=None) -> str:
    """Prevent startup network checks unless the caller explicitly opts in."""
    env = os.environ if environ is None else environ
    return env.setdefault("HF_HUB_OFFLINE", "1")


def load_chatterbox(device):
    """Load the existing Chatterbox engine without a normal online Hub check."""
    prefer_local_models()
    from chatterbox.tts import ChatterboxTTS

    try:
        return ChatterboxTTS.from_pretrained(device=device)
    except Exception as exc:
        # Don't mislabel VRAM, CUDA or malformed-weight errors as cache misses.
        missing_cache = type(exc).__name__ in {
            "LocalEntryNotFoundError", "OfflineModeIsEnabled",
            "EntryNotFoundError", "RevisionNotFoundError",
        }
        if os.environ.get("HF_HUB_OFFLINE") == "1" and missing_cache:
            raise RuntimeError(
                "Chatterbox model files are missing from the local Hugging Face "
                "cache. Open Studio > Settings > Chatterbox models and select "
                "'Download missing files' while online. Then generation can "
                "run entirely offline."
            ) from exc
        raise


def check_local_models() -> tuple[dict[str, Path], list[str]]:
    """Check local Hub cache only; never issue an HTTP request."""
    from huggingface_hub import hf_hub_download

    available: dict[str, Path] = {}
    missing: list[str] = []
    for name in MODEL_FILES:
        try:
            path = Path(hf_hub_download(
                repo_id=MODEL_REPO, filename=name, local_files_only=True
            ))
            if not path.is_file() or path.stat().st_size == 0:
                missing.append(name)
            else:
                available[name] = path
        except (OSError, ValueError):
            missing.append(name)
        except Exception as exc:
            # Hub versions differ in the exception used for missing snapshots.
            if type(exc).__name__ in {
                "LocalEntryNotFoundError", "EntryNotFoundError",
                "RevisionNotFoundError",
            }:
                missing.append(name)
            else:
                raise
    return available, missing


def download_models(*, refresh: bool = False) -> tuple[dict[str, Path], list[str]]:
    """Explicitly contact Hugging Face; optionally redownload current weights."""
    # This action runs in its own process. Never alter the GUI or converter env.
    os.environ["HF_HUB_OFFLINE"] = "0"
    os.environ["HF_HUB_DISABLE_PROGRESS_BARS"] = "1"
    from huggingface_hub import snapshot_download

    print(("Refreshing" if refresh else "Downloading missing") +
          " Chatterbox model files from Hugging Face...", flush=True)
    folder = Path(snapshot_download(
        repo_id=MODEL_REPO, allow_patterns=list(MODEL_FILES),
        force_download=refresh, max_workers=2
    ))
    found = {name: folder / name for name in MODEL_FILES
             if (folder / name).is_file() and (folder / name).stat().st_size > 0}
    missing = [name for name in MODEL_FILES if name not in found]
    return found, missing


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="Inspect local Chatterbox weights or explicitly download them."
    )
    parser.add_argument("action", choices=("check", "download", "refresh"))
    args = parser.parse_args(argv)

    # The default application process never contacts Hugging Face.
    if args.action == "check":
        prefer_local_models()
    try:
        if args.action == "check":
            found, missing = check_local_models()
        else:
            found, missing = download_models(refresh=args.action == "refresh")
    except Exception as exc:
        print(f"Chatterbox model {args.action} failed: "
              f"{type(exc).__name__}: {exc}", file=sys.stderr, flush=True)
        return 1

    for name, path in found.items():
        print(f"Ready: {name} ({path.stat().st_size:,} bytes)", flush=True)
    if missing:
        print("Missing model files: " + ", ".join(missing), file=sys.stderr, flush=True)
        print("Connect to the internet and use 'Download missing files' in "
              "Studio Settings.", file=sys.stderr, flush=True)
        return 2
    print("All Chatterbox files are available locally. "
          "Future generation can run offline.", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
