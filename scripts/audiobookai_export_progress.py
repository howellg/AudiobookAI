"""Lightweight FFmpeg -progress parsing for complete M4B exports.

FFmpeg's out_time_ms is actually microseconds in many versions, so prefer
out_time (HH:MM:SS.microseconds) and fall back to out_time_us. Percentages
never claim 100 until the caller validates the completed output.
"""
from __future__ import annotations

import math
import re


def parse_ffmpeg_clock(value):
    """Convert a non-negative HH:MM:SS[.fraction] timestamp to seconds."""
    if not isinstance(value, str):
        return None
    parts = value.strip().split(':')
    if len(parts) != 3:
        return None
    try:
        hours, minutes = int(parts[0]), int(parts[1])
        seconds = float(parts[2])
    except (ValueError, OverflowError):
        return None
    if hours < 0 or minutes < 0 or minutes >= 60 or not math.isfinite(seconds) or not 0 <= seconds < 60:
        return None
    return hours * 3600 + minutes * 60 + seconds


def export_progress_metrics(fields, duration_seconds, previous_percent=0):
    """Return progress data, or None for malformed/empty FFmpeg updates.

    Clamp very small or overshooting FFmpeg timestamps, avoid regressions from
    encoding jitter, and reserve 100% for verified successful process exit.
    """
    try:
        total = float(duration_seconds)
    except (ValueError, TypeError, OverflowError):
        return None
    if not math.isfinite(total) or total <= 0:
        return None
    seconds = parse_ffmpeg_clock(fields.get('out_time'))
    if seconds is None:
        # FFmpeg reports microseconds (including for its historical out_time_ms).
        raw = fields.get('out_time_us', fields.get('out_time_ms'))
        try:
            seconds = float(raw) / 1_000_000
        except (TypeError, ValueError, OverflowError):
            return None
        if not math.isfinite(seconds) or seconds < 0:
            return None
    seconds = min(total, max(0.0, seconds))
    try:
        earlier = int(previous_percent)
    except (TypeError, ValueError):
        earlier = 0
    percent = max(0, min(99, max(earlier, int(seconds * 100 / total))))
    speed_label = str(fields.get('speed', '')).strip()
    match = re.fullmatch(r'(\d+(?:\.\d+)?)x', speed_label)
    speed = float(match.group(1)) if match else None
    if speed is not None and (not math.isfinite(speed) or speed <= 0):
        speed = None
    return {'percent': percent, 'encoded_seconds': round(seconds, 3),
            'duration_seconds': round(total, 3), 'speed': speed}
