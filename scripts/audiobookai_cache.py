"""Non-destructive checks for audiobook narration cache availability.

Many users relocate the raw speech cache to a removable SSD by symlink.
When the disk is unmounted, Path.exists() reports False for the broken link,
but Path.mkdir(exist_ok=True) raises an unhelpful FileExistsError. Never
replace that symlink with a local folder or silently lose the cache.
"""
from __future__ import annotations

from pathlib import Path


def ensure_cache_directory(directory: Path) -> Path:
    """Create normal caches; refuse a missing symlink target without modifying it."""
    path = Path(directory)
    if path.is_symlink() and not path.is_dir():
        target = path.readlink()
        raise RuntimeError(
            f"Narration cache storage is unavailable: {path} -> {target}. "
            "The symlink target may be on an unmounted external drive. "
            "Mount the drive and confirm this directory is accessible before "
            "resuming audiobook generation. Do not delete or replace the symlink."
        )
    if path.exists() and not path.is_dir():
        raise NotADirectoryError(
            f"Narration cache location exists but is not a directory: {path}"
        )
    # Existing directories, including valid symlinks, are left untouched.
    path.mkdir(parents=True, exist_ok=True)
    if not path.is_dir():
        raise RuntimeError(f"Narration cache is not accessible: {path}")
    return path


def cache_error_summary(exc: Exception) -> str:
    """Suppress native-library traceback noise while keeping the useful error."""
    line = next((line.strip() for line in str(exc).splitlines() if line.strip()),
                type(exc).__name__)
    return line[:360] + ("…" if len(line) > 360 else "")
