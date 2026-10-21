#!/usr/bin/env python3
"""Run an existing AudiobookAI queue job WITHOUT importing Qt, PySide6 or torch.

Example (with the Studio GUI closed):
    python scripts/audiobookai_headless.py --list
    python scripts/audiobookai_headless.py --job 23

The converter is the same V7 backend that Studio launches. It receives the
same immutable pronunciation snapshot, narrator and stored chapter selection.
Existing cache and completed chapter WAVs are reused by the converter.
"""
from __future__ import annotations

import argparse
from collections import deque
import fcntl
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time

from audiobookai_storage import Store
from audiobookai_pronunciation import load_snapshot
from audiobookai_reliability import append_diagnostic, diagnostic_hint, job_log_path

ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "output"
DB_PATH = OUTPUT / "audiobookai_studio.sqlite3"
CONVERTER = Path(__file__).with_name("epub_to_audiobook_v7_studio.py")
SNAPSHOTS = OUTPUT / "pronunciation" / "jobs"
LOCK_PATH = OUTPUT / ".headless_conversion.lock"
EVENT_PREFIX = "AUDIOBOOKAI_EVENT "


def active_converter_pids():
    """Avoid launching a competing converter or migrating its active SQLite job.

    Linux /proc is readable without additional dependencies. The advisory
    flock also prevents multiple headless runners from executing at once.
    """
    pids = []
    proc_root = Path("/proc")
    if not proc_root.is_dir():
        return pids
    for item in proc_root.iterdir():
        if not item.name.isdecimal():
            continue
        try:
            args = (item / "cmdline").read_bytes().replace(b"\x00", b" ")
        except (OSError, PermissionError):
            continue
        if (b"epub_to_audiobook_v7_studio.py" in args
                and (b" convert " in args or b" resume " in args)):
            pids.append(int(item.name))
    return sorted(pids)


def reserve_runner():
    """Acquire a process-lifetime lock before SQLite's restart migration."""
    OUTPUT.mkdir(parents=True, exist_ok=True)
    lock = LOCK_PATH.open("a+")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError as exc:
        lock.close()
        raise RuntimeError("Another headless conversion is already running.") from exc
    return lock


def headless_runner_active():
    """Check advisory lock without disturbing another runner's ownership."""
    if not LOCK_PATH.exists():
        return False
    with LOCK_PATH.open("a+") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
        else:
            fcntl.flock(lock, fcntl.LOCK_UN)
            return False


def checked_job(job: dict, *, snapshot_dir=SNAPSHOTS, converter=CONVERTER):
    """Construct original V7 converter args; never replace a queued snapshot."""
    if job["status"] not in ("queued", "interrupted", "failed"):
        raise ValueError(f"Job #{job['id']} is {job['status']}; it cannot be started.")
    source = Path(job["book_path"])
    if not source.is_file():
        raise FileNotFoundError(f"Missing book EPUB: {source}")
    if not Path(converter).is_file():
        raise FileNotFoundError(f"Missing converter: {converter}")
    try:
        indices = json.loads(job["indices"])
    except (TypeError, ValueError) as exc:
        raise ValueError("Invalid stored EPUB section selection") from exc
    if (not isinstance(indices, list) or not indices
            or any(type(i) is not int or i < 1 for i in indices)):
        raise ValueError("Invalid stored EPUB section selection")
    args = [sys.executable, "-u", str(converter), "convert", str(source),
            "--chapters", ",".join(map(str, indices)),
            "--output-name", str(job["output_name"]), "--studio-events"]
    if job["pronunciation_required"]:
        snapshot = Path(snapshot_dir) / f"job_{int(job['id'])}.json"
        # Parse the frozen rules before launching: never silently fall back to
        # the live dictionary if this required historical snapshot is absent.
        load_snapshot(snapshot)
        args.extend(("--pronunciation-file", str(snapshot)))
    if job.get("acting_mode", "standard") != "standard":
        from audiobookai_acting import validate_acting_mode
        args.extend(("--acting-mode", validate_acting_mode(job["acting_mode"])))
    if job["voice_path"]:
        voice = Path(job["voice_path"])
        if not voice.is_file() or voice.suffix.lower() != ".wav":
            raise FileNotFoundError(f"Missing queued narrator WAV: {voice}")
        args.extend(("--voice", str(voice)))
    return args


class Checkpoints:
    """Interpret converter progress events and checkpoint in the existing store."""
    def __init__(self, store, job_id):
        self.store = store
        self.job_id = job_id
        self.plan = {}
        self.done = {}
        self.current_section = None
        self.current_chunk = 0
        self.current_total = 0
        self.last_saved_at = 0.0
        self.last_saved_chunks = 0
        self.export_path = None

    def update(self, event):
        kind = event.get("event")
        if kind == "plan":
            self.plan = {int(p["index"]): int(p["chunks"]) for p in event["sections"]}
            self.done = {int(p["index"]): int(p["chunks"]) if p["completed"] else 0
                         for p in event["sections"]}
        elif kind == "chapter_started":
            self.current_section = int(event["index"])
            self.current_chunk = 0
            self.current_total = int(event["chunks"])
        elif kind == "chunk_done":
            section = int(event["index"])
            self.current_section = section
            self.current_chunk = int(event["chunk"])
            self.current_total = int(event["total"])
            self.done[section] = max(self.done.get(section, 0), self.current_chunk)
        elif kind == "chapter_done":
            section = int(event["index"])
            self.current_section = section
            self.done[section] = self.plan.get(section, int(event["chunks"]))
            self.current_chunk = self.current_total = int(event["chunks"])
        elif kind == "export_done":
            self.export_path = str(event["path"])
        self.save(force=kind in ("plan", "chapter_started", "chapter_done",
                                 "export_started", "export_done"))

    def save(self, force=False):
        if not self.plan:
            return
        now = time.monotonic()
        completed = sum(self.done.values())
        if (not force and completed - self.last_saved_chunks < 20
                and now - self.last_saved_at < 15):
            return
        self.store.update_progress(
            self.job_id, sum(self.plan.values()), completed,
            self.current_section, self.current_chunk, self.current_total)
        self.last_saved_at = now
        self.last_saved_chunks = completed


def run_job(store, job_id, *, process_factory=subprocess.Popen, verbose=False):
    job_row = store.job(job_id)
    if job_row is None:
        raise ValueError(f"No queue job #{job_id}. Run --list to find its ID.")
    job = dict(job_row)
    command = checked_job(job)
    log_file = job_log_path(OUTPUT / "diagnostics", job_id)
    checkpoint = Checkpoints(store, job_id)
    recent = deque(maxlen=40)
    store.update_job(job_id, "running")
    print(f"Starting job #{job_id}: {job['book_title']} — {job['name']}", flush=True)
    print(f"Diagnostic log: {log_file}", flush=True)
    proc = None
    interrupted = False
    returncode = None
    try:
        proc = process_factory(
            command, cwd=str(ROOT), stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT, text=True, encoding="utf-8",
            errors="replace", bufsize=1)
        for raw in proc.stdout:
            line = raw.strip()
            if not line:
                continue
            append_diagnostic(log_file, line)
            if line.startswith(EVENT_PREFIX):
                try:
                    event = json.loads(line[len(EVENT_PREFIX):])
                    checkpoint.update(event)
                    if event["event"] in ("chapter_done", "export_started", "export_done"):
                        print(f"{event['event']}: {event.get('index', '')} "
                              f"{event.get('path', '')}", flush=True)
                except (ValueError, TypeError, KeyError) as exc:
                    append_diagnostic(log_file, f"Invalid progress event: {exc}")
                continue
            recent.append(line)
            if verbose or (not line.startswith(("Using cached audio.", "Sampling:"))
                           and not line.startswith("  ")
                           and "it/s]" not in line):
                print(line, flush=True)
        returncode = proc.wait()
    except KeyboardInterrupt:
        interrupted = True
        print("Interrupt requested: asking converter to stop at a safe point.", flush=True)
        if proc is not None and proc.poll() is None:
            proc.send_signal(signal.SIGTERM)
            try:
                returncode = proc.wait(timeout=120)
            except subprocess.TimeoutExpired:
                print("Converter is still stopping. Check its PID before a retry.",
                      file=sys.stderr, flush=True)
                returncode = 130
        else:
            returncode = 130
    except BaseException as exc:
        append_diagnostic(log_file, f"Headless runner failure: {exc}")
        if proc is not None and proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=30)
            except subprocess.TimeoutExpired:
                print("Converter did not stop promptly; check for a surviving process.",
                      file=sys.stderr, flush=True)
        store.update_job(job_id, "failed",
                         error=f"Headless runner error: {exc}; log: {log_file}")
        raise
    finally:
        checkpoint.save(force=True)

    if interrupted or returncode == 130:
        store.update_job(job_id, "interrupted",
                         error=f"Headless conversion interrupted; log: {log_file}")
        return 130
    exported = Path(checkpoint.export_path) if checkpoint.export_path else None
    if (returncode == 0 and exported and exported.is_file()
            and exported.suffix.lower() == ".m4b"
            and exported.stem == job["output_name"] and exported.stat().st_size > 1024):
        try:
            store.complete_job(job_id, exported)
        except (OSError, ValueError) as exc:
            store.update_job(job_id, "failed", error=f"Could not record completed part: {exc}")
            print(f"Job #{job_id} failed to save completion: {exc}", file=sys.stderr, flush=True)
            return 1
        print(f"Job #{job_id} complete and archived in metadata: {exported}", flush=True)
        return 0
    hint = diagnostic_hint(recent, returncode)
    if returncode == 0:
        hint = "Converter returned success, but no verified matching M4B was reported"
    store.update_job(job_id, "failed", error=f"{hint}; log: {log_file}")
    print(f"Job #{job_id} failed: {hint}\nSee {log_file}", file=sys.stderr, flush=True)
    return 1


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    exclusive = parser.add_mutually_exclusive_group(required=True)
    exclusive.add_argument("--list", action="store_true", help="Show job IDs and statuses")
    exclusive.add_argument("--job", type=int, help="Resume one queued/interrupted/failed job by ID")
    parser.add_argument("--verbose", action="store_true",
                        help="Also print cached-chunk chatter to the terminal")
    args = parser.parse_args(argv)
    # Stop before Store initialization because its recovery migration changes
    # running statuses; never make it think another process died while alive.
    lock = reserve_runner()
    try:
        alive = active_converter_pids()
        if alive:
            raise RuntimeError(
                f"Converter process(es) still active: {alive}. "
                "Do not start another conversion or open the GUI simultaneously.")
        if not DB_PATH.is_file():
            raise FileNotFoundError(f"Existing Studio queue database not found: {DB_PATH}")
        store = Store(DB_PATH)
        try:
            if args.list:
                for row in reversed(store.jobs()):
                    print(f"#{row['id']:>4} [{row['status']:<11}] "
                          f"{row['book_title']} — {row['name']} "
                          f"({row['completed_chunks']}/{row['total_chunks']} chunks)")
                return 0
            return run_job(store, args.job, verbose=args.verbose)
        finally:
            store.close()
    finally:
        lock.close()


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (OSError, RuntimeError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)
