"""Pure-Python helpers to keep frequent TTS events out of Qt widget updates.

A large novel can emit tens of thousands of events. The GUI should repaint
sparingly, but persist every real queue checkpoint regardless of paint rate.
"""
from __future__ import annotations

import re

_CACHE_OUTPUT = re.compile(r"^Generated \d+(?:\.\d+)?s in \d+(?:\.\d+)?s$")


def routine_speech_log(line: str) -> bool:
    """True only for routine per-chunk chatter; warnings/errors stay visible."""
    return line == "Using cached audio." or bool(_CACHE_OUTPUT.fullmatch(line))


def progress_snapshot(plan, done_chunks, current_section, exporting, chunk_durations):
    """Compute bounded progress labels/values without touching any Qt objects."""
    if not plan:
        return {}
    completed = sum(max(0, int(value)) for value in done_chunks.values())
    total = max(1, sum(max(0, int(item["chunks"])) for item in plan.values()))
    completed = min(completed, total)
    display = 98.0 * completed / total
    result = {
        "overall_progress": round(display * 10),
        "overall_text": f"Overall: {display:.1f}% · {completed:,} / {total:,} chunks",
        "stat_chunks": f"Chunks: {completed:,} / {total:,}",
    }
    if current_section in plan and not exporting:
        section = plan[current_section]
        ct = max(1, int(section["chunks"]))
        cd = min(ct, max(0, int(done_chunks.get(current_section, 0))))
        result["chapter_progress"] = round(1000 * cd / ct)
        result["chapter_text"] = (
            f"Section {current_section}: {section['title']} — "
            f"{cd:,}/{ct:,} chunks ({100 * cd / ct:.1f}%)"
        )
    if chunk_durations and not exporting:
        remaining = max(0, total - completed)
        mean = sum(chunk_durations) / len(chunk_durations)
        if mean > 0:
            return result, remaining * mean
    return result, None
