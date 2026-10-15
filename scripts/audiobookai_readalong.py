"""Read-only synchronized narration transcript for AudiobookAI Studio V2.7.

Completed chapter WAVs and saved chunk indexes are the source of truth. V2.6
indexes lack exact sample offsets, so chunk positions are *estimates* based on
the durations of saved chunk WAVs scaled to the assembled chapter. No model,
Qt, GPU or generation work is needed.
"""
from __future__ import annotations

from bisect import bisect_right
import json
from pathlib import Path
from types import SimpleNamespace

import soundfile as sf

from audiobookai_production import (load_index, active_take, original_chunk_file,
                                    edit_digest)


def read_chapter(folder: str | Path, chapter: int) -> dict:
    """Return a playable chapter transcript and a duration-weighted seek timeline.

    Use the assembled chapter WAV for playback, never stitched raw-cache chunks.
    The active editor take is preferred when available. When raw cache files have
    been cleaned, estimate their lengths from word counts without regenerating.
    Errors for absent finished chapters or invalid chunk indexes are explicit.
    """
    folder = Path(folder).resolve()
    number = int(chapter)
    if number < 1:
        raise ValueError("Chapter number must be positive.")
    wav = folder / "chapters" / f"{number:04d}.wav"
    if not wav.is_file():
        raise FileNotFoundError(f"Completed chapter WAV is missing: {wav}")
    info = sf.info(str(wav))
    if info.frames <= 0 or info.samplerate <= 0:
        raise ValueError("Chapter WAV contains no playable frames.")
    duration_ms = round(1000 * info.frames / info.samplerate)

    index = load_index(folder, number)
    entries = index.get("chunks", [])
    if not isinstance(entries, list) or not entries:
        raise FileNotFoundError(
            "No saved chunk index for this chapter. Use Review audio to index it first."
        )
    if len(entries) > 50000:
        raise ValueError("Chapter index contains too many chunks.")

    # An editor take can be selected before the assembled chapter is rebuilt.
    # Its metadata and duration belong to the chapter only if the digest agrees.
    original_texts = []
    for expected, entry in enumerate(entries, 1):
        if not isinstance(entry, dict) or not str(entry.get("text", "")).strip():
            raise ValueError(f"Invalid chunk entry #{expected}")
        if int(entry.get("number", -1)) != expected:
            raise ValueError(f"Chunk index is out of sequence at #{expected}")
        original_texts.append(str(entry["text"]))
    chapter_record = {}
    manifest_path = folder / "manifest.json"
    if manifest_path.is_file():
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        chapter_record = manifest.get("chapters", {}).get(str(number), {})
    recorded_digest = chapter_record.get("edits_digest") if isinstance(chapter_record, dict) else None
    applied_edits = bool(recorded_digest and recorded_digest == edit_digest(
        folder, number, [SimpleNamespace(text=text) for text in original_texts]))

    chunks = []
    lengths = []
    missing = 0
    for expected, entry in enumerate(entries, 1):
        original_text = original_texts[expected - 1]
        chosen = active_take(folder, number, expected, original_text) if applied_edits else None
        text = str(chosen.get("text") or original_text) if chosen else original_text
        text = text.replace("\n", " ").strip()
        recording = Path(chosen["path"]) if chosen else original_chunk_file(entry)
        try:
            if not recording.is_file():
                raise FileNotFoundError()
            audio = sf.info(str(recording))
            if audio.frames <= 0 or audio.samplerate <= 0:
                raise ValueError("Empty speech chunk")
            length = 1000 * audio.frames / audio.samplerate
        except (OSError, ValueError, RuntimeError, FileNotFoundError):
            missing += 1
            length = None
        chunks.append({"number": expected, "text": text})
        lengths.append(length)

    measured = [
        length / max(1, len(chunk["text"].split()))
        for chunk, length in zip(chunks, lengths) if length is not None
    ]
    per_word_ms = sorted(measured)[len(measured) // 2] if measured else 380.0
    weights = [
        max(1., length if length is not None
            else per_word_ms * max(1, len(chunk["text"].split())))
        for chunk, length in zip(chunks, lengths)
    ]
    total_weight = sum(weights)
    elapsed_weight = 0.0
    timeline = []
    for index_number, (chunk, weight) in enumerate(zip(chunks, weights)):
        start = round(duration_ms * elapsed_weight / total_weight)
        elapsed_weight += weight
        end = (duration_ms if index_number == len(chunks) - 1
               else round(duration_ms * elapsed_weight / total_weight))
        timeline.append({**chunk, "start_ms": start, "end_ms": max(start, end)})

    timing = ("Approximate chunk timing from recorded durations"
              if not missing else
              f"Approximate timing; {missing} missing raw chunk recording(s)")
    return {
        "wav": str(wav), "chapter": number,
        "title": str(index.get("title") or f"Section {number}"),
        "duration_ms": duration_ms, "chunks": timeline,
        "timing": timing, "approximate": True,
    }


def chunk_at(timeline: list[dict], position_ms: int) -> int:
    """Find the zero-based active chunk; an exact boundary belongs to the next."""
    if not timeline:
        return -1
    starts = [int(chunk["start_ms"]) for chunk in timeline]
    return min(len(timeline) - 1, max(0, bisect_right(starts, int(position_ms)) - 1))

def clamp_resume_ms(position_ms, duration_ms: int) -> int:
    """Normalize saved UI position; a completed chapter resumes at its beginning."""
    try:
        duration = max(0, int(duration_ms))
        position = max(0, int(position_ms))
    except (TypeError, ValueError, OverflowError):
        return 0
    if duration == 0 or position >= max(0, duration - 1000):
        return 0
    return min(position, duration)
