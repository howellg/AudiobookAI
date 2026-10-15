"""Offline diagnostics, metadata backups, and interrupted-job preflight for Studio 2.8.

All operations run locally. Archives contain a transactionally consistent SQLite
copy and small editable metadata only, never books, voices, generated audio, raw
speech caches, model weights, MySQL credentials, or Qt preferences.
No automatic restore or destructive queue action is performed here.
"""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import uuid
import zipfile

BACKUP_PREFIX = "audiobookai_metadata_"
LOG_LIMIT = 4 * 1024 * 1024
ROOT = Path(__file__).resolve().parents[1]


def check_resume_job(job, snapshot_dir=None) -> str | None:
    """Return a reason an interrupted job cannot safely be requeued, or None."""
    if job["status"] != "interrupted":
        return "Not interrupted"
    try:
        indices = json.loads(job["indices"])
        if (not isinstance(indices, list) or not indices or
                any(type(i) is not int or i < 1 for i in indices)):
            return "Invalid stored chapter selection"
    except (ValueError, TypeError):
        return "Invalid stored chapter selection"
    if not Path(job["book_path"]).is_file():
        return "Source EPUB is missing"
    if job["voice_path"] and not Path(job["voice_path"]).is_file():
        return "Narrator WAV is missing"
    if job["pronunciation_required"]:
        folder = Path(snapshot_dir) if snapshot_dir is not None else (
            ROOT / "output" / "pronunciation" / "jobs")
        snapshot = folder / f"job_{int(job['id'])}.json"
        try:
            from audiobookai_pronunciation import load_snapshot
            load_snapshot(snapshot)
        except (OSError, ValueError, TypeError):
            return "Required pronunciation snapshot is missing or invalid"
    return None


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def create_metadata_backup(db: sqlite3.Connection, output_root, destination,
                           config=None, *, automatic=False, keep=10) -> Path:
    """Atomically create a verified ZIP from SQLite's backup API and metadata.

    The database backup uses SQLite's supported online-copy API; copying its
    underlying .sqlite3 file could omit pending WAL transactions. A failed
    archive leaves existing backups intact. Rotating automatic backups never
    deletes a user-created manual backup.
    """
    output_root = Path(output_root)
    destination = Path(destination)
    destination.mkdir(parents=True, exist_ok=True)
    label = "auto" if automatic else "manual"
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    final = destination / f"{BACKUP_PREFIX}{label}_{stamp}_{uuid.uuid4().hex[:8]}.zip"
    fd, tmp_name = tempfile.mkstemp(prefix=".audiobookai-", suffix=".zip", dir=destination)
    os.close(fd)
    staging = Path(tmp_name)
    try:
        with tempfile.TemporaryDirectory(prefix=".audiobookai-db-", dir=destination) as work:
            backup_db = Path(work) / "studio.sqlite3"
            with sqlite3.connect(backup_db) as target:
                db.backup(target)
                integrity = target.execute("PRAGMA integrity_check").fetchone()[0]
                if integrity != "ok":
                    raise ValueError(f"SQLite integrity check failed: {integrity}")
            sources = [("studio.sqlite3", backup_db)]
            for relative in ("pronunciation/dictionary.json", "archive/catalog.json"):
                path = output_root / relative
                if path.is_file() and not path.is_symlink():
                    sources.append((relative, path))
            jobs_dir = output_root / "pronunciation" / "jobs"
            if jobs_dir.is_dir():
                for path in sorted(jobs_dir.glob("job_*.json")):
                    if path.is_file() and not path.is_symlink():
                        sources.append((f"pronunciation/jobs/{path.name}", path))
            entries = {}
            with zipfile.ZipFile(staging, "w", compression=zipfile.ZIP_DEFLATED,
                                 compresslevel=6, allowZip64=True) as archive:
                for name, path in sources:
                    # Metadata files are intentionally small, and no symlink
                    # may redirect this archive into a private filesystem.
                    if path.stat().st_size > 32 * 1024 * 1024:
                        raise ValueError(f"Metadata file is unexpectedly large: {name}")
                    data = path.read_bytes()
                    entries[name] = _sha256(data)
                    archive.writestr(name, data)
                if config is not None:
                    # Pass only read_config()'s nonsecret whitelist, never
                    # environment variables or a keyring credential.
                    safe = {"mysql": {
                        k: config.get("mysql", {}).get(k)
                        for k in ("enabled", "host", "port", "database", "user", "use_keyring")
                        if k in config.get("mysql", {})
                    }}
                    data = (json.dumps(safe, indent=2) + "\n").encode("utf-8")
                    archive.writestr("studio_config.json", data)
                    entries["studio_config.json"] = _sha256(data)
                archive.writestr("backup_manifest.json", json.dumps({
                    "format": 1, "created_utc": datetime.now(timezone.utc).isoformat(),
                    "automatic": bool(automatic),
                    "note": ("Metadata-only. Keep EPUB, narrator WAV, generated audio "
                             "and raw chunk caches separately; paths in SQLite remain "
                             "specific to the original computer."),
                    "sha256": entries,
                }, indent=2))
        verify_metadata_backup(staging)
        staging.replace(final)
        if automatic:
            old = sorted(destination.glob(f"{BACKUP_PREFIX}auto_*.zip"),
                         key=lambda p: p.stat().st_mtime, reverse=True)
            for outdated in old[max(1, int(keep)):]:
                outdated.unlink()
        return final
    finally:
        staging.unlink(missing_ok=True)


def verify_metadata_backup(path) -> dict:
    """Check every recorded checksum without extracting or modifying any files."""
    with zipfile.ZipFile(path, "r") as archive:
        names = archive.namelist()
        if len(names) != len(set(names)) or "backup_manifest.json" not in names:
            raise ValueError("Missing manifest or duplicate archive members")
        manifest = json.loads(archive.read("backup_manifest.json"))
        entries = manifest.get("sha256")
        if manifest.get("format") != 1 or not isinstance(entries, dict):
            raise ValueError("Unsupported backup manifest")
        if "studio.sqlite3" not in entries or set(names) != set(entries) | {"backup_manifest.json"}:
            raise ValueError("Unexpected archive members")
        for name, checksum in entries.items():
            # This verifier never extracts, but reject unsafe archive names
            # to prevent accidental extraction with external tools.
            parts = Path(name).parts
            if (not parts or Path(name).is_absolute() or ".." in parts or
                    "\\" in name or name.startswith(".")):
                raise ValueError("Unsafe archive member name")
            data = archive.read(name)
            if _sha256(data) != checksum:
                raise ValueError(f"Backup checksum mismatch: {name}")
        if not archive.read("studio.sqlite3").startswith(b"SQLite format 3\x00"):
            raise ValueError("Archive does not contain a SQLite database")
        return manifest


def latest_backup(directory, *, automatic_only=False):
    """Return the newest local backup path if any exists."""
    prefix = f"{BACKUP_PREFIX}auto_" if automatic_only else BACKUP_PREFIX
    found = list(Path(directory).glob(f"{prefix}*.zip"))
    return max(found, key=lambda f: f.stat().st_mtime) if found else None


def append_diagnostic(path, text):
    """Append a timestamped message; rotate a bounded per-job local text log."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_file() and path.stat().st_size >= LOG_LIMIT:
        rolled = path.with_suffix(".1.log")
        path.replace(rolled)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(datetime.now(timezone.utc).isoformat(timespec="seconds")
                     + " " + str(text).rstrip()[:3000] + "\n")


def job_log_path(directory, job_id: int) -> Path:
    """Create a unique path for a new converter process's plain-text log."""
    when = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return Path(directory) / f"job_{int(job_id)}_{when}_{uuid.uuid4().hex[:6]}.log"


def diagnostic_hint(recent_lines, exit_code) -> str:
    """Summarize the final helpful converter error without hiding its exit code."""
    keywords = ("error", "exception", "traceback", "out of memory", "failed",
                "killed", "cuda", "memoryerror")
    for message in reversed(list(recent_lines)):
        line = str(message).strip()
        if line and any(k in line.lower() for k in keywords):
            return f"Exit {exit_code}: {line[:220]}"
    return f"Converter exited with code {exit_code}; see saved diagnostic log."
