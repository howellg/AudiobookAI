#!/usr/bin/env python3
"""EPUB -> cached Chatterbox V6 chapter WAVs -> chaptered M4B.

Place beside test_five_minutes_v6_normal_speed.py in ~/AudiobookAI/scripts/.

Usage:
  python scripts/epub_to_audiobook.py inspect "books/novel.epub"
  python scripts/epub_to_audiobook.py convert "books/novel.epub" --chapters 1-2
  python scripts/epub_to_audiobook.py convert "books/novel.epub"       # all + M4B
  python scripts/epub_to_audiobook.py export "books/novel.epub"        # existing WAVs -> M4B

Reads ordinary, accessible (DRM-free) EPUB files. EPUB structure varies; inspect
first to spot a misidentified table of contents, footnotes, or front matter.
"""

# MAINTAINER NOTE: Current Studio conversion backend. EPUB parsing and metadata handling run independently of the GUI; GPU generation is loaded only when required.
from __future__ import annotations

import argparse
import signal
import hashlib
import importlib.util
import json
import re
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

import audiobookai_production as production
from audiobookai_convenience import readable_m4b_basename
from audiobookai_pronunciation import load_snapshot, apply_rules, rules_digest
from audiobookai_acting import ACTING_MODES, validate_acting_mode

ROOT = Path(__file__).resolve().parents[1]
BOOKS = ROOT / "books"
OUT_ROOT = ROOT / "output" / "audiobooks"
ENGINE_FILE = Path(__file__).with_name("test_five_minutes_v7_smooth.py")
STUDIO_EVENTS = False
STOP_REQUESTED = False
ACTIVE_VOICE = None  # CLI --voice; None leaves original V7 paths unchanged.
ACTIVE_ACTING_MODE = 'standard'  # Immutable CLI --acting-mode; opt-in only.
ACTIVE_PRONUNCIATIONS = []  # Only set from an immutable per-job snapshot.
PRONUNCIATION_SNAPSHOT = None


def emit_event(kind, **values):
    """Print a machine-readable JSON status record when launched by Studio."""
    # Prefix distinguishes machine-readable updates from arbitrary stdout warnings.
    if STUDIO_EVENTS:
        print("AUDIOBOOKAI_EVENT " + json.dumps({"event": kind, **values}, ensure_ascii=False), flush=True)


def request_stop(_signum, _frame):
    """Set the graceful-stop flag from the process signal handler."""
    global STOP_REQUESTED
    STOP_REQUESTED = True


def check_stop():
    """Exit with a distinct status after finishing the current safe stop point."""
    if STOP_REQUESTED:
        print("Stopping cleanly; previously cached speech is preserved.", flush=True)
        raise SystemExit(130)



@dataclass
class Section:
    """One ordered EPUB segment with source metadata and its extracted paragraphs."""
    index: int
    title: str
    paragraphs: list[str]
    source: str

    @property
    def text(self):
        """Join source paragraphs with blank lines to preserve paragraph boundaries."""
        return "\n\n".join(self.paragraphs)


def safe_name(s: str, length=80):
    """Convert a title or filename into a filesystem-safe basename."""
    return re.sub(r"[^a-zA-Z0-9_-]+", "_", s).strip("_")[:length] or "untitled"


# Chapter headings can be Arabic or Roman numerals, with optional punctuation.
# Match a heading in its entirety so chapter references inside narrative text
# do not create accidental section breaks.
CHAPTER_HEADING = re.compile(
    r"^\s*(?:chapter|chap\.?|book|part)\s+([0-9]+|[IVXLCDM]+)\s*[.:—–-]?\s*$",
    re.IGNORECASE,
)
ROMAN_ONLY_HEADING = re.compile(r"^\s*([IVXLCDM]+)\s*[.:]?\s*$", re.IGNORECASE)


def is_chapter_heading(text: str, tag_name: str = "") -> bool:
    """Recognize numbered headings while rejecting ordinary narrative references."""
    text = re.sub(r"\s+", " ", text).strip()
    if CHAPTER_HEADING.fullmatch(text):
        return True
    # Roman numerals by themselves count as headings only in heading elements.
    return tag_name in {"h1", "h2", "h3", "h4", "h5", "h6"} and bool(ROMAN_ONLY_HEADING.fullmatch(text))


def load_epub(epub_path: Path):
    """Read readable EPUB spine items in order and extract section metadata and prose."""
    try:
        from ebooklib import epub, ITEM_DOCUMENT
        from bs4 import BeautifulSoup
    except ImportError as exc:
        raise SystemExit("Install inside your venv: uv pip install ebooklib beautifulsoup4 lxml") from exc

    book = epub.read_epub(str(epub_path))
    def meta(name, fallback):
        """Read a Dublin Core metadata value or use the provided fallback."""
        vals = book.get_metadata("DC", name)
        return (str(vals[0][0]).strip() if vals else fallback) or fallback

    title = meta("title", epub_path.stem)
    author = meta("creator", "Unknown author")
    sections = []
    heading_tags = {"h1", "h2", "h3", "h4", "h5", "h6"}
    block_tags = heading_tags | {"p", "blockquote"}

    def add_section(name, paragraphs, source):
        """Append a nonempty EPUB section and assign the next reading-order index."""
        if paragraphs:
            sections.append(Section(len(sections) + 1, name, paragraphs, source))

    # Spine order is the book reading order; file names are not reliable chapter order.
    for item_id, linear in book.spine:
        if linear == "no":
            continue
        item = book.get_item_with_id(item_id)
        if item is None or item.get_type() != ITEM_DOCUMENT:
            continue
        source_name = item.get_name()
        if source_name.lower().endswith(("nav.xhtml", "toc.xhtml", "toc.html")):
            continue
        # XHTML is XML, so prefer its native parser. Fall back to HTML for
        # malformed EPUBs that do not parse as XML.
        content = item.get_content()
        try:
            soup = BeautifulSoup(content, "xml")
            body = soup.find("body")
            if body is None:
                raise ValueError("No XHTML body")
        except Exception:
            soup = BeautifulSoup(content, "lxml")
            body = soup.body or soup
        for tag in body.select("script, style, nav, aside, svg, noscript, header, footer"):
            tag.decompose()
        for tag in body.select('a[epub\\:type="noteref"], a[role="doc-noteref"]'):
            tag.decompose()

        blocks = []
        for el in body.find_all(list(block_tags)):
            if el.find_parent(list(block_tags)):
                continue
            txt = re.sub(r"\s+", " ", el.get_text(" ", strip=True)).strip()
            if txt:
                blocks.append((el.name.lower(), txt))
        if not blocks:
            plain = re.sub(r"\s+", " ", body.get_text(" ", strip=True)).strip()
            if plain:
                blocks = [("p", plain)]
        if not blocks:
            continue

        # Crucial for Gutenberg EPUBs: a single XHTML file can contain many
        # complete chapters. Split at chapter headings inside each document.
        current = []
        current_title = None
        found_chapter = False
        # One XHTML file may contain several chapters; split on real heading elements.
        for tag_name, txt in blocks:
            if tag_name in heading_tags and is_chapter_heading(txt, tag_name):
                if current:
                    if current_title is None:
                        prefix_title = next((v for t, v in current[:4] if t in heading_tags and len(v) <= 100), None)
                        add_section(prefix_title or "Front matter", current, source_name)
                    else:
                        add_section(current_title, current, source_name)
                current = [txt]
                current_title = txt
                found_chapter = True
            else:
                current.append(txt)
        if current:
            if current_title is not None:
                add_section(current_title, current, source_name)
            else:
                heading = next((v for t, v in blocks[:4] if len(v) <= 100), None)
                add_section(heading or f"Section {len(sections) + 1}", current, source_name)

    if not sections:
        raise RuntimeError("No readable sections found in EPUB. Check whether it's a valid DRM-free EPUB.")
    return title, author, sections


def load_engine():
    """Load the narration module lazily so inspection does not load the GPU model."""
    if not ENGINE_FILE.exists():
        raise SystemExit(f"Missing V7 engine: {ENGINE_FILE}\nPlace test_five_minutes_v7_smooth.py alongside this script.")
    spec = importlib.util.spec_from_file_location("audiobook_v6_engine", ENGINE_FILE)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    # Override only this converter process; the default file remains unchanged.
    if ACTIVE_VOICE is not None:
        module.VOICE_FILE = ACTIVE_VOICE
    module.ACTING_MODE = validate_acting_mode(ACTIVE_ACTING_MODE)
    if not module.VOICE_FILE.is_file():
        raise SystemExit(f"Missing narrator sample: {module.VOICE_FILE}")
    module.CACHE_DIR.mkdir(parents=True, exist_ok=True)
    return module


def book_folder(path: Path, voice_path: Path | None = None, acting_mode: str | None = None):
    """Separate chapters by narrator AND opted-in delivery without changing legacy paths."""
    # Include the source location so same-titled books do not share chapter folders.
    fingerprint = hashlib.sha256(str(path.resolve()).encode()).hexdigest()[:10]
    original = OUT_ROOT / f"{safe_name(path.stem, 60)}_{fingerprint}_smooth_v7"
    voice = voice_path if voice_path is not None else ACTIVE_VOICE
    # Legacy British-narrator output must remain discoverable after upgrades.
    if voice is None or Path(voice).resolve() == (ROOT / 'voices' / 'british_narrator.wav').resolve():
        folder = original
    else:
        # Isolate assembled chapter WAVs by actual voice content.
        from audiobookai_voices import sha256_file
        digest = sha256_file(Path(voice))[:16]
        folder = original / 'voices' / f'voice_{digest}'
    mode = validate_acting_mode(acting_mode if acting_mode is not None else ACTIVE_ACTING_MODE)
    # Explicitly separate a performance's WAVs/manifest from normal narration.
    return folder if mode == 'standard' else folder / 'performances' / mode


def chapter_signature(engine, section):
    """Hash the text, narrator, and assembly settings used to build a chapter."""
    # Chunk original EPUB prose FIRST so rule edits cannot alter section boundaries.
    # Only new jobs carrying an explicit snapshot replace spoken chunk text.
    chunks = engine.make_chunks(section.text)
    if ACTIVE_PRONUNCIATIONS:
        for chunk in chunks:
            chunk.text = apply_rules(chunk.text, ACTIVE_PRONUNCIATIONS)
    signature = {"chunks": [(c.text, c.boundary, c.dialogue) for c in chunks],
                 "voice": [engine.cache_key(c.text) for c in chunks],
                 "smooth_version": engine.SMOOTH_VERSION,
                 "assembly": [engine.FINAL_CONTINUATION_PAUSE, engine.EDGE_KEEP_SECONDS, engine.MAX_EDGE_TRIM_SECONDS, engine.FINAL_COMMA_PAUSE, engine.FINAL_SENTENCE_PAUSE,
                              engine.FINAL_DIALOGUE_PAUSE, engine.FINAL_PARAGRAPH_PAUSE,
                              engine.FINAL_SCENE_PAUSE, engine.LEADING_PAD_FINAL, engine.MAX_PEAK]}
    # Offline editor/rebuild fixtures (and legacy engines) have no acting setting.
    mode = getattr(engine, 'ACTING_MODE', 'standard')
    if mode != 'standard':
        signature["acting_mode"] = mode
    digest = hashlib.sha256(json.dumps(signature, ensure_ascii=False).encode()).hexdigest()
    return chunks, digest


def atomic_json(path: Path, payload):
    """Write JSON through a temporary file so interrupted writes leave the prior file intact."""
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    temp.replace(path)


def read_manifest(path: Path):
    """Read the previous chapter manifest, tolerating an absent or invalid one."""
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            pass
    return {"chapters": {}}


def chapters_selected(expr: str | None, max_section: int):
    """Parse a comma-separated list of chapter indices and inclusive ranges."""
    if not expr:
        return list(range(1, max_section+1))
    selected = set()
    for part in expr.split(","):
        part = part.strip()
        if "-" in part:
            left, right = part.split("-", 1)
            a, b = int(left), int(right)
            if a > b:
                raise ValueError(f"Invalid range: {part}")
            selected.update(range(a, b+1))
        else:
            selected.add(int(part))
    if not selected or min(selected) < 1 or max(selected) > max_section:
        raise ValueError(f"--chapters must be between 1 and {max_section}")
    return sorted(selected)


def chapter_path(out: Path, index: int):
    """Build the zero-padded chapter WAV path for a given EPUB section."""
    return out / "chapters" / f"{index:04d}.wav"


def generate_sections(epub_path: Path, title, author, sections, chosen, out: Path):
    # Imports the proven V6 engine only when generation is requested.
    """Plan exact chunk counts, synthesize incomplete chapters, and checkpoint metadata."""
    engine = load_engine()
    import torchaudio as ta
    import torch
    out.joinpath("chapters").mkdir(parents=True, exist_ok=True)
    # Manifest signatures determine whether a complete chapter WAV may be reused.
    manifest_path = out / "manifest.json"
    manifest = read_manifest(manifest_path)
    manifest.update({"book": title, "author": author, "source": str(epub_path.resolve()),
                     "voice_path": str(ACTIVE_VOICE) if ACTIVE_VOICE else None,
                     "acting_mode": ACTIVE_ACTING_MODE})
    manifest.setdefault("chapters", {})

    # Plan all selected chapters before CUDA model loading, so the GUI can show
    # *exact* chunk totals rather than guessing from section counts or word counts.
    # Build the full chunk plan before loading the large speech model onto the GPU.
    plans = []
    plan_data = {}
    for index in chosen:
        check_stop()
        section = sections[index-1]
        chunks, signature = chapter_signature(engine, section)
        production.save_index(out, index, section.title, chunks, engine, signature)
        edits_digest = production.edit_digest(out, index, chunks)
        target = chapter_path(out, index)
        old = manifest["chapters"].get(str(index), {})
        has_active_edits = any(production.active_take(out, index, j, chunk.text)
                               for j, chunk in enumerate(chunks, 1))
        edit_matches = (old.get('edits_digest') == edits_digest if 'edits_digest' in old
                        else not has_active_edits)  # Preserve pre-V2.4 completed WAVs.
        complete = (target.is_file() and old.get('signature') == signature and edit_matches)
        plans.append({"index": index, "title": section.title, "chunks": len(chunks), "completed": complete})
        plan_data[index] = (chunks, signature, edits_digest, target, complete)
    emit_event("plan", sections=plans)

    model = None  # lazy: cached full chapters don't require loading Chatterbox
    for index in chosen:
        check_stop()
        section = sections[index-1]
        chunks, signature, edits_digest, target, complete = plan_data[index]
        if complete:
            print(f"[{index}/{len(sections)}] Already complete: {section.title} ({len(chunks)} chunks)", flush=True)
            emit_event("chapter_done", index=index, cached=True, chunks=len(chunks))
            continue
        if not chunks:
            print(f"[{index}] Empty section, skipping")
            continue
        print(f"\n[{index}/{len(sections)}] {section.title} — {len(chunks)} chunks", flush=True)
        emit_event("chapter_started", index=index, title=section.title, chunks=len(chunks))
        if model is None:
            from chatterbox.tts import ChatterboxTTS
            model = ChatterboxTTS.from_pretrained(device=engine.DEVICE)
            print(f"Chatterbox loaded on {engine.DEVICE}", flush=True)
        # Only one chapter held in RAM at a time. Raw chunks cached as generated.
        recordings = []
        for j, chunk in enumerate(chunks, 1):
            check_stop()
            print(f"  {j}/{len(chunks)}: {chunk.text[:85]}", flush=True)
            started = time.monotonic()
            edited = production.active_take(out, index, j, chunk.text)
            if edited:
                clip, clip_sr = ta.load(edited['path'])
                if clip_sr != model.sr:
                    clip = ta.functional.resample(clip, clip_sr, model.sr)
                recordings.append(engine.clean_tensor(clip))
                print(f'Using edited take #{j}.', flush=True)
            else:
                try:
                    recordings.append(engine.generate_cached(model, chunk.text, model.sr))
                except Exception as exc:
                    production.atomic_json(out / 'generation_failure.json',
                        {'chapter': index, 'chunk': j, 'text': chunk.text,
                         'error': str(exc), 'time': time.time()})
                    raise
            emit_event("chunk_done", index=index, chunk=j, total=len(chunks),
                       seconds=round(time.monotonic()-started, 3), edited=bool(edited))
        check_stop()
        audio, silence = engine.assemble(chunks, recordings, model.sr)
        temp = target.with_suffix(".partial.wav")
        ta.save(str(temp), audio, model.sr)
        temp.replace(target)
        samples = audio.shape[-1]
        manifest["chapters"][str(index)] = {
            "title": section.title, "signature": signature, "edits_digest": edits_digest,
            "samples": samples,
            "sample_rate": model.sr, "chunks": len(chunks), "wav": str(target),
            "acting_mode": ACTIVE_ACTING_MODE,
            "pronunciation_snapshot": str(PRONUNCIATION_SNAPSHOT) if PRONUNCIATION_SNAPSHOT else None,
        }
        atomic_json(manifest_path, manifest)
        print(f"Saved {target.name}: {samples/model.sr/60:.1f} minutes; +{silence:.1f}s silence", flush=True)
        emit_event("chapter_done", index=index, cached=False, chunks=len(chunks),
                   duration_seconds=round(samples/model.sr, 2))
        del audio, recordings
    return manifest



def write_indexes(epub_path, sections, chosen, out):
    """Index preexisting chapters without touching recordings or loading a GPU model."""
    engine = load_engine()
    for index in chosen:
        section = sections[index - 1]
        chunks, signature = chapter_signature(engine, section)
        path = production.save_index(out, index, section.title, chunks, engine, signature)
        print(f'Indexed #{index}: {len(chunks)} chunks — {path}', flush=True)
        emit_event('indexed', index=index, chunks=len(chunks), path=str(path))


def rebuild_chapters(epub_path, title, author, sections, chosen, out):
    """Reassemble from raw cached WAVs and saved edited takes. No GPU model."""
    import torchaudio as ta
    engine = load_engine()
    out.joinpath('chapters').mkdir(parents=True, exist_ok=True)
    manifest_path = out / 'manifest.json'
    manifest = read_manifest(manifest_path)
    manifest.setdefault('chapters', {})
    for index in chosen:
        check_stop()
        section = sections[index - 1]
        chunks, signature = chapter_signature(engine, section)
        production.save_index(out, index, section.title, chunks, engine, signature)
        digest = production.edit_digest(out, index, chunks)
        recordings = []
        sr = None
        for j, chunk in enumerate(chunks, 1):
            edited = production.active_take(out, index, j, chunk.text)
            if edited:
                file_path = Path(edited['path'])
            else:
                file_path = (engine.CACHE_DIR / (engine.cache_key(chunk.text) + '.wav'))
            if not file_path.is_file():
                raise RuntimeError(f'Chunk {j}/{len(chunks)} not cached: {file_path}. '
                                   'Generate this chapter normally before editing/rebuilding.')
            clip, clip_sr = ta.load(str(file_path))
            if sr is None:
                sr = clip_sr
            elif clip_sr != sr:
                clip = ta.functional.resample(clip, clip_sr, sr)
            report = production.quality_report(clip, sr, len((edited['text'] if edited else chunk.text).split()))
            if report['unusable']:
                raise RuntimeError(f'Cannot rebuild: chunk {j} has unusable audio: {report["flags"]}.')
            recordings.append(engine.clean_tensor(clip))
        if not recordings:
            continue
        audio, silence = engine.assemble(chunks, recordings, sr)
        target = chapter_path(out, index)
        tmp = target.with_suffix('.partial.wav')
        try:
            ta.save(str(tmp), audio, sr)
            tmp.replace(target)
        finally:
            tmp.unlink(missing_ok=True)
        manifest['chapters'][str(index)] = {
            'title': section.title, 'signature': signature, 'edits_digest': digest,
            'samples': audio.shape[-1], 'sample_rate': sr, 'chunks': len(chunks),
            'wav': str(target),
            'pronunciation_snapshot': str(PRONUNCIATION_SNAPSHOT) if PRONUNCIATION_SNAPSHOT else None,
        }
        manifest.update({'book': title, 'author': author, 'source': str(epub_path.resolve()),
                         'voice_path': str(ACTIVE_VOICE) if ACTIVE_VOICE else None})
        atomic_json(manifest_path, manifest)
        print(f'Rebuilt chapter #{index} from {len(chunks)} existing WAV chunks: '
              f'{audio.shape[-1]/sr/60:.1f} minutes (+{silence:.1f}s pauses)', flush=True)
        emit_event('chapter_rebuilt', index=index, path=str(target))


def render_replacement(sections, index, number, text, out):
    """Create a brand-new take without overwriting the shared raw speech cache."""
    import torchaudio as ta
    engine = load_engine()
    section = sections[index-1]
    chunks, signature = chapter_signature(engine, section)
    if not 1 <= number <= len(chunks):
        raise ValueError(f'Chunk must be between 1 and {len(chunks)}')
    production.save_index(out, index, section.title, chunks, engine, signature)
    from chatterbox.tts import ChatterboxTTS
    original = chunks[number-1].text
    relative, destination = production.take_destination(out, index, number)
    print(f'Generating replacement for chapter {index}, chunk {number}: {text[:100]}', flush=True)
    model = ChatterboxTTS.from_pretrained(device=engine.DEVICE)
    audio = engine.generate_cached(model, text, model.sr, force=True, cache_write=False)
    partial = destination.with_name(destination.stem + '.partial.wav')
    try:
        ta.save(str(partial), audio, model.sr)
        partial.replace(destination)
    finally:
        partial.unlink(missing_ok=True)
    take = production.register_take(out, index, number, original, text, relative)
    print(f'Created new take {take["id"]} at {destination}. Chapter M4B unchanged until rebuild.', flush=True)
    emit_event('take_created', chapter=index, chunk=number, path=str(destination))


def audit_chapter(sections, index, out):
    """Detect suspicious cached/edited WAVs; inspection alone changes no audio."""
    import soundfile as sf
    engine = load_engine()
    section = sections[index-1]
    chunks, signature = chapter_signature(engine, section)
    production.save_index(out, index, section.title, chunks, engine, signature)
    flags = []
    for j, chunk in enumerate(chunks, 1):
        edit = production.active_take(out, index, j, chunk.text)
        path = Path(edit['path']) if edit else engine.CACHE_DIR / (engine.cache_key(chunk.text) + '.wav')
        if not path.is_file():
            continue  # Not generated yet, not an audio defect.
        try:
            report = production.inspect_wav(path, len((edit['text'] if edit else chunk.text).split()))
        except Exception as exc:
            report = {'unusable': True, 'flags': [f'Unreadable WAV: {exc}']}
        if report['flags']:
            item = {'number': j, 'text': chunk.text, 'file': str(path), **report}
            flags.append(item)
            print(f'Quality review #{j}: {", ".join(report["flags"])}', flush=True)
    report_path = out / 'quality_reports' / f'{index:04d}.json'
    production.atomic_json(report_path, {'chapter': index, 'total': len(chunks), 'flags': flags})
    print(f'Audio scan: {len(flags)} item(s) flagged of {len(chunks)} chunks; saved {report_path}', flush=True)
    emit_event('audit_done', chapter=index, flagged=len(flags), total=len(chunks), path=str(report_path))


def ffmeta_escape(s: str):
    """Escape special characters for FFmpeg chapter metadata."""
    return s.replace("\\", "\\\\").replace("=", "\\=").replace(";", "\\;").replace("#", "\\#").replace("\n", " ")


def ffconcat_quote(path: Path):
    """Quote a file path for FFmpeg concat-list syntax."""
    return "'" + str(path.resolve()).replace("'", "'\\''") + "'"


def export_m4b(epub_path: Path, title, author, sections, chosen, out: Path, output_name=None, *, report_progress=False):
    """Export chapter recordings under an explicit noncolliding part filename."""
    manifest = read_manifest(out / "manifest.json")
    recorded = manifest.get("chapters", {})
    absent = [i for i in chosen if not chapter_path(out, i).is_file() or str(i) not in recorded]
    if absent:
        raise SystemExit(f"Missing rendered chapters {absent}. Convert them first (the cache will resume).")
    # All chapter WAVs must have same sample rate and channel layout (V6 does this).
    sample_rates = {recorded[str(i)]["sample_rate"] for i in chosen}
    if len(sample_rates) != 1:
        raise RuntimeError("Chapters have different sample rates. Regenerate to the same format.")
    out.mkdir(parents=True, exist_ok=True)
    concat = out / "chapters.ffconcat"
    concat.write_text("ffconcat version 1.0\n" + "".join(
        f"file {ffconcat_quote(chapter_path(out, i))}\n" for i in chosen), encoding="utf-8")
    metadata = out / "chapters.ffmetadata"
    lines = [";FFMETADATA1", f"title={ffmeta_escape(title)}", f"artist={ffmeta_escape(author)}", f"album={ffmeta_escape(title)}"]
    elapsed_samples = 0
    sr = next(iter(sample_rates))
    for i in chosen:
        start_ms = round(elapsed_samples * 1000 / sr)
        elapsed_samples += recorded[str(i)]["samples"]
        end_ms = round(elapsed_samples * 1000 / sr)
        lines.extend(["[CHAPTER]", "TIMEBASE=1/1000", f"START={start_ms}", f"END={end_ms}",
                      f"title={ffmeta_escape(sections[i-1].title)}"])
    metadata.write_text("\n".join(lines) + "\n", encoding="utf-8")
    name = readable_m4b_basename(output_name, 130) if output_name else (
        safe_name(title, 100) + ("_sample" if len(chosen)<len(sections) else ""))
    output = out / (name + ".m4b")
    partial = output.with_name(output.stem + ".partial.m4b")
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "concat", "-safe", "0",
           "-i", str(concat), "-f", "ffmetadata", "-i", str(metadata),
           "-map", "0:a:0", "-map_metadata", "1", "-map_chapters", "1",
           # Mark these files as audiobooks, not generic M4A music. Some
           # Android players choose chapter handling from the container brand.
           # Put the chapter/index metadata at the beginning for readers that
           # cannot seek efficiently through very large (multi-GB) books.
           "-metadata", "media_type=2", "-brand", "M4B ",
           "-movflags", "+faststart", "-c:a", "aac", "-b:a", "64k",
           "-ac", "1", "-ar", str(sr), str(partial)]
    check_stop()
    print(f"\nEncoding {len(chosen)} section(s) into M4B ...", flush=True)
    duration_seconds = elapsed_samples / sr
    emit_event("export_started", sections=len(chosen), path=str(output),
               duration_seconds=round(duration_seconds, 3))
    try:
        if report_progress:
            # The GUI's full-book export requests real FFmpeg encoding time,
            # not guessed disk-size or chapter-count progress. Normal part
            # exports retain their existing subprocess behavior.
            from audiobookai_export_progress import export_progress_metrics
            tracked_cmd = (cmd[:1] + ['-nostats', '-stats_period', '0.5',
                                       '-progress', 'pipe:1'] + cmd[1:])
            fields = {}
            last_percent = 0
            with subprocess.Popen(tracked_cmd, stdout=subprocess.PIPE,
                                  stderr=subprocess.STDOUT, text=True,
                                  encoding='utf-8', errors='replace',
                                  bufsize=1) as process:
                for raw in process.stdout:
                    line = raw.strip()
                    if '=' in line:
                        key, value = line.split('=', 1)
                        if key in ('out_time', 'out_time_us', 'out_time_ms',
                                   'speed', 'progress'):
                            fields[key] = value
                            if key == 'progress':
                                metrics = export_progress_metrics(
                                    fields, duration_seconds, last_percent)
                                if metrics is not None:
                                    last_percent = metrics['percent']
                                    emit_event('export_progress', **metrics)
                                fields = {}
                            continue
                    if line:
                        print('FFmpeg: ' + line, flush=True)
                return_code = process.wait()
            if return_code:
                raise subprocess.CalledProcessError(return_code, tracked_cmd)
        else:
            subprocess.run(cmd, check=True)
        # Reject zero-length or unplayable files before replacing any prior export.
        probe = subprocess.run(["ffprobe", "-v", "error", "-show_entries",
                                "format=duration", "-of", "default=noprint_wrappers=1:nokey=1",
                                str(partial)], capture_output=True, text=True, check=True)
        if not partial.is_file() or partial.stat().st_size < 1024 or float(probe.stdout.strip()) <= 0:
            raise RuntimeError("FFmpeg produced an invalid audiobook; previous export untouched")
        partial.replace(output)
    finally:
        if partial.exists():
            partial.unlink()
    print(f"M4B: {output}")
    print(f"Duration: {elapsed_samples/sr/3600:.2f} hours | {output.stat().st_size/1024**2:.1f} MiB")
    emit_event("export_done", path=str(output), duration_seconds=round(elapsed_samples/sr, 2),
               size_bytes=output.stat().st_size)
    return output


def main():
    """Handle inspect, convert, rebuild, audit, and export actions from the CLI."""
    global STUDIO_EVENTS, ACTIVE_VOICE, ACTIVE_ACTING_MODE, ACTIVE_PRONUNCIATIONS, PRONUNCIATION_SNAPSHOT
    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("command", choices=["inspect", "convert", "resume", "export", "index", "rebuild", "render-chunk", "audit"])
    parser.add_argument("epub", type=Path, help="EPUB file path")
    parser.add_argument("--chapters", help="Sections, e.g. 1-3 or 2,5,7 (use inspect to see numbers)")
    parser.add_argument("--no-export", action="store_true", help="Convert to chapter WAVs without encoding M4B")
    parser.add_argument("--output-name", help="Unique M4B basename; recommended for batch exports")
    parser.add_argument("--voice", type=Path, help="Alternate narrator reference WAV; omitted uses the original British narrator")
    parser.add_argument("--acting-mode", choices=ACTING_MODES, default='standard',
                        help="Optional single-narrator expressive quoted-dialogue delivery")
    parser.add_argument("--pronunciation-file", type=Path,
                        help="Immutable per-job spoken-text substitutions (new jobs only)")
    parser.add_argument("--studio-events", action="store_true", help="Print machine-readable JSON progress events")
    parser.add_argument("--export-progress", action="store_true", help="Track FFmpeg M4B encoding when used with --studio-events")
    parser.add_argument("--chunk", type=int, help="1-based chunk number for render-chunk")
    parser.add_argument("--replacement-text", help="New spoken text for render-chunk")
    args = parser.parse_args()
    STUDIO_EVENTS = args.studio_events
    ACTIVE_ACTING_MODE = validate_acting_mode(args.acting_mode)
    if args.pronunciation_file is not None:
        PRONUNCIATION_SNAPSHOT = args.pronunciation_file.expanduser().resolve()
        ACTIVE_PRONUNCIATIONS = load_snapshot(PRONUNCIATION_SNAPSHOT)
        print(f'Pronunciation dictionary: {len(ACTIVE_PRONUNCIATIONS)} rules '
              f'(snapshot {rules_digest(ACTIVE_PRONUNCIATIONS)})', flush=True)
    if args.voice is not None:
        ACTIVE_VOICE = args.voice.expanduser().resolve()
        if not ACTIVE_VOICE.is_file() or ACTIVE_VOICE.suffix.lower() != '.wav':
            parser.error(f'Voice WAV not found: {ACTIVE_VOICE}')
    epub_path = args.epub.expanduser().resolve()
    if not epub_path.is_file():
        parser.error(f"EPUB not found: {epub_path}")
    title, author, sections = load_epub(epub_path)
    selected = chapters_selected(args.chapters, len(sections))
    total_words = sum(len(s.text.split()) for s in sections)
    print(f"{title} — {author} | {len(sections)} EPUB sections | {total_words:,} words", flush=True)
    if args.command == "inspect":
        for s in sections:
            print(f"  {s.index:3}. {s.title[:62]:62} {len(s.text.split()):>6} words  ({s.source})")
        print("\nNote: Sections follow EPUB reading order and split at recognized chapter headings. Review front/back matter.")
        return
    out = book_folder(epub_path)
    if args.command == 'index':
        write_indexes(epub_path, sections, selected, out)
        return
    if args.command == 'rebuild':
        rebuild_chapters(epub_path, title, author, sections, selected, out)
        return
    if args.command == 'audit':
        for index in selected:
            audit_chapter(sections, index, out)
        return
    if args.command == 'render-chunk':
        if len(selected) != 1 or args.chunk is None or not (args.replacement_text or '').strip():
            parser.error('render-chunk requires a single --chapters index, --chunk N, --replacement-text')
        if len(args.replacement_text.split()) > 45:
            parser.error('Replacement text must be at most 45 words; use multiple takes for longer passages')
        render_replacement(sections, selected[0], args.chunk, args.replacement_text.strip(), out)
        return
    if args.command in ("convert", "resume"):
        generate_sections(epub_path, title, author, sections, selected, out)
        if not args.no_export:
            check_stop()
            export_m4b(epub_path, title, author, sections, selected, out, args.output_name)
    else:
        export_m4b(epub_path, title, author, sections, selected, out, args.output_name,
                   report_progress=args.export_progress and args.studio_events)


if __name__ == "__main__":
    main()
