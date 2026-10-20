"""Pure-Python helpers to keep frequent TTS events out of Qt widget updates.

A large novel can emit tens of thousands of events. The GUI should repaint
sparingly, but persist every real queue checkpoint regardless of paint rate.
"""
from __future__ import annotations

import re
from collections import deque

_CACHE_OUTPUT = re.compile(r"^Generated \d+(?:\.\d+)?s in \d+(?:\.\d+)?s$")


_CHUNK_SOURCE = re.compile(r"^\d+/\d+:\s")


def chunk_source_log(line: str) -> bool:
    """Identify the converter's per-chunk spoken-text prefix."""
    return bool(_CHUNK_SOURCE.match(line))


class BufferedGenerationLog:
    """Keep bounded log history while batching GUI appends every 500 ms.

    'Detailed' events remain available after switching views, even if they
    were hidden when received. Persistent diagnostic files are independent.
    """

    def __init__(self, history_limit=10000, pending_limit=5000):
        self.history = deque(maxlen=history_limit)
        self.pending = deque(maxlen=pending_limit)
        self.omitted = 0

    def append(self, message: str, *, detailed=False):
        entry = (str(message), bool(detailed))
        self.history.append(entry)
        if len(self.pending) == self.pending.maxlen:
            self.omitted += 1
        self.pending.append(entry)

    def drain(self, detailed=False):
        entries = list(self.pending)
        self.pending.clear()
        omitted, self.omitted = self.omitted, 0
        lines = [line for line, extra in entries if detailed or not extra]
        if omitted:
            lines.insert(0, f"[{omitted:,} older log lines omitted from live view; see job log on disk]")
        return "\n".join(lines)

    def replay(self, detailed=False):
        self.pending.clear()
        self.omitted = 0
        return "\n".join(line for line, extra in self.history
                         if detailed or not extra)

    def clear(self):
        self.history.clear()
        self.pending.clear()
        self.omitted = 0


def routine_speech_log(line: str) -> bool:
    """True only for routine per-chunk chatter; warnings/errors stay visible."""
    return line == "Using cached audio." or bool(_CACHE_OUTPUT.fullmatch(line))


def progress_snapshot(plan, done_chunks, current_section, exporting, chunk_durations,
                      recent_chunk_durations=None):
    """Compute bounded progress labels/values without touching any Qt objects."""
    if not plan:
        return {}, None
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
    # When provided, recent durations include cache hits and edited takes.
    # Their pace is observational, not a guarantee that future chunks are cached.
    # Legacy callers still use generated-only timings to preserve compatibility.
    samples = recent_chunk_durations if recent_chunk_durations is not None else chunk_durations
    if not exporting and samples:
        remaining = max(0, total - completed)
        mean = sum(max(0.001, float(value)) for value in samples) / len(samples)
        return result, remaining * mean
    return result, None
