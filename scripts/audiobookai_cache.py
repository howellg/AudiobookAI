"""Non-destructive checks for audiobook narration cache availability.

Many users relocate the raw speech cache to a removable SSD by symlink.
When the disk is unmounted, Path.exists() reports False for the broken link,
but Path.mkdir(exist_ok=True) raises an unhelpful FileExistsError. Never
replace that symlink with a local folder or silently lose the cache.
"""
from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import shutil


@dataclass(frozen=True)
class CacheReadiness:
    """Read-only health report for a raw speech cache."""
    ready: bool
    detail: str
    cache_path: Path
    free_bytes: int | None = None


def _removable_mount(path: Path) -> Path | None:
    """Detect known auto-mounted media roots without guessing other mounts."""
    parts = path.parts
    # udisks: /run/media/<user>/<disk>/... and /media/<user>/<disk>/...
    if (len(parts) >= 5 and parts[0] == '/' and
            (parts[1:3] == ('run', 'media') or parts[1:2] == ('media',))):
        count = 5 if parts[1] == 'run' else 4
        if len(parts) >= count:
            return Path(*parts[:count])
    return None


def cache_preflight(directory: Path) -> CacheReadiness:
    """Check cache/mount without creating, deleting or writing to any folder.

    A missing ordinary directory can be created safely by the normal engine,
    but a dangling symlink or an unmounted udisks volume must be blocked.
    This must remain cheap and GPU/network/Qt independent.
    """
    path = Path(directory)
    try:
        symlinked = path.is_symlink()
        target = path.resolve(strict=False) if symlinked else path
        if symlinked:
            mount_root = _removable_mount(target)
            if mount_root is not None and not os.path.ismount(mount_root):
                return CacheReadiness(False,
                    f'Cache SSD is not mounted at {mount_root}. '
                    f'Mount the drive, then use Check storage & resume. '
                    f'Existing cache link is preserved: {path} -> {target}', path)
            if not path.is_dir():
                return CacheReadiness(False,
                    f'Cache link target is unavailable: {path} -> {target}. '
                    'Mount the SSD and confirm the directory exists; do not replace the link.',
                    path)
        elif path.exists() and not path.is_dir():
            return CacheReadiness(False,
                f'Cache path is not a directory: {path}. Correct the path before generating.', path)
        if path.is_dir():
            if not os.access(path, os.R_OK | os.W_OK | os.X_OK):
                return CacheReadiness(False,
                    f'Cache directory is not readable/writable: {path}. '
                    'Check SSD permissions before generating.', path)
            free = shutil.disk_usage(path).free
            suffix = f' (free: {free / (1024 ** 3):.1f} GiB)'
            return CacheReadiness(True,
                f'Cache ready: {path}{suffix}', path, free)
        # The normal internal cache will be created by ensure_cache_directory()
        # only after conversion starts. Preflight itself never creates it.
        parent = next((candidate for candidate in path.parents if candidate.exists()), None)
        if parent is None or not parent.is_dir() or not os.access(
                parent, os.W_OK | os.X_OK):
            return CacheReadiness(False,
                f'Cache parent directory is inaccessible or unwritable: {path}', path)
        return CacheReadiness(True,
            f'Internal cache directory will be created when needed: {path}', path)
    except OSError as exc:
        return CacheReadiness(False,
            f'Cannot access narration cache {path}: {exc}. '
            'Check the mount and permissions before generating.', path)


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
