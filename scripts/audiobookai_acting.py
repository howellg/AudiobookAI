"""Optional single-narrator acting settings; no Qt, torch, or language model.

The first acting mode deliberately keeps the SAME narrator reference WAV.
It applies a small expressiveness increase only to chunks that the V7 splitter
already considers quoted dialogue. It does not guess identities, assign voices,
rewrite EPUB text, or promise character-specific pitch/accent.
"""
from __future__ import annotations

from pathlib import Path

ACTING_MODES = ("standard", "expressive")
EXPRESSIVE_EXAGGERATION = 0.48
EXPRESSIVE_CFG_WEIGHT = 0.30


def validate_acting_mode(mode: str) -> str:
    if mode not in ACTING_MODES:
        raise ValueError(f"Unknown narration acting mode: {mode!r}")
    return mode


def chunk_settings(mode: str, is_dialogue: bool) -> dict:
    """Return only overrides; legacy standard chunks keep their exact cache key."""
    validate_acting_mode(mode)
    if mode == "expressive" and is_dialogue:
        return {"exaggeration": EXPRESSIVE_EXAGGERATION,
                "cfg_weight": EXPRESSIVE_CFG_WEIGHT}
    return {}


def recorded_folders(base: Path) -> list[Path]:
    """Discover legacy and expressive narrator editions (even without queue rows)."""
    base = Path(base)
    roots = [base, *sorted((base / "voices").glob("voice_*"))]
    result = []
    for root in roots:
        result.append(root)
        result.extend(sorted((root / "performances").glob("expressive")))
    return result


def delivery_label(mode: str) -> str:
    return " · expressive" if mode == "expressive" else ""
