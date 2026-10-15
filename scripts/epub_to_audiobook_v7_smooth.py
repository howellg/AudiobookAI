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
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import re
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BOOKS = ROOT / "books"
OUT_ROOT = ROOT / "output" / "audiobooks"
ENGINE_FILE = Path(__file__).with_name("test_five_minutes_v7_smooth.py")


@dataclass
class Section:
    index: int
    title: str
    paragraphs: list[str]
    source: str

    @property
    def text(self):
        return "\n\n".join(self.paragraphs)


def safe_name(s: str, length=80):
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
    text = re.sub(r"\s+", " ", text).strip()
    if CHAPTER_HEADING.fullmatch(text):
        return True
    # Roman numerals by themselves count as headings only in heading elements.
    return tag_name in {"h1", "h2", "h3", "h4", "h5", "h6"} and bool(ROMAN_ONLY_HEADING.fullmatch(text))


def load_epub(epub_path: Path):
    try:
        from ebooklib import epub, ITEM_DOCUMENT
        from bs4 import BeautifulSoup
    except ImportError as exc:
        raise SystemExit("Install inside your venv: uv pip install ebooklib beautifulsoup4 lxml") from exc

    book = epub.read_epub(str(epub_path))
    def meta(name, fallback):
        vals = book.get_metadata("DC", name)
        return (str(vals[0][0]).strip() if vals else fallback) or fallback

    title = meta("title", epub_path.stem)
    author = meta("creator", "Unknown author")
    sections = []
    heading_tags = {"h1", "h2", "h3", "h4", "h5", "h6"}
    block_tags = heading_tags | {"p", "blockquote"}

    def add_section(name, paragraphs, source):
        if paragraphs:
            sections.append(Section(len(sections) + 1, name, paragraphs, source))

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
    if not ENGINE_FILE.exists():
        raise SystemExit(f"Missing V6 engine: {ENGINE_FILE}\nPlace your working test_five_minutes_v6_normal_speed.py alongside this script.")
    spec = importlib.util.spec_from_file_location("audiobook_v6_engine", ENGINE_FILE)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    if not module.VOICE_FILE.is_file():
        raise SystemExit(f"Missing narrator sample: {module.VOICE_FILE}")
    module.CACHE_DIR.mkdir(parents=True, exist_ok=True)
    return module


def book_folder(path: Path):
    # Stable for a particular file path, and distinct from other copies/titles.
    fingerprint = hashlib.sha256(str(path.resolve()).encode()).hexdigest()[:10]
    return OUT_ROOT / f"{safe_name(path.stem, 60)}_{fingerprint}_smooth_v7"


def chapter_signature(engine, section):
    chunks = engine.make_chunks(section.text)
    signature = {"chunks": [(c.text, c.boundary, c.dialogue) for c in chunks],
                 "voice": [engine.cache_key(c.text) for c in chunks],
                 "smooth_version": engine.SMOOTH_VERSION,
                 "assembly": [engine.FINAL_CONTINUATION_PAUSE, engine.EDGE_KEEP_SECONDS, engine.MAX_EDGE_TRIM_SECONDS, engine.FINAL_COMMA_PAUSE, engine.FINAL_SENTENCE_PAUSE,
                              engine.FINAL_DIALOGUE_PAUSE, engine.FINAL_PARAGRAPH_PAUSE,
                              engine.FINAL_SCENE_PAUSE, engine.LEADING_PAD_FINAL, engine.MAX_PEAK]}
    digest = hashlib.sha256(json.dumps(signature, ensure_ascii=False).encode()).hexdigest()
    return chunks, digest


def atomic_json(path: Path, payload):
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    temp.replace(path)


def read_manifest(path: Path):
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            pass
    return {"chapters": {}}


def chapters_selected(expr: str | None, max_section: int):
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
    return out / "chapters" / f"{index:04d}.wav"


def generate_sections(epub_path: Path, title, author, sections, chosen, out: Path):
    # Imports the proven V6 engine only when generation is requested.
    engine = load_engine()
    import torchaudio as ta
    import torch
    out.joinpath("chapters").mkdir(parents=True, exist_ok=True)
    manifest_path = out / "manifest.json"
    manifest = read_manifest(manifest_path)
    manifest.update({"book": title, "author": author, "source": str(epub_path.resolve())})
    manifest.setdefault("chapters", {})

    model = None  # lazy: cached full chapters don't require loading Chatterbox
    for index in chosen:
        section = sections[index-1]
        chunks, signature = chapter_signature(engine, section)
        target = chapter_path(out, index)
        old = manifest["chapters"].get(str(index), {})
        if target.is_file() and old.get("signature") == signature:
            print(f"[{index}/{len(sections)}] Already complete: {section.title} ({len(chunks)} chunks)", flush=True)
            continue
        if not chunks:
            print(f"[{index}] Empty section, skipping")
            continue
        print(f"\n[{index}/{len(sections)}] {section.title} — {len(chunks)} chunks", flush=True)
        if model is None:
            from chatterbox.tts import ChatterboxTTS
            model = ChatterboxTTS.from_pretrained(device=engine.DEVICE)
            print(f"Chatterbox loaded on {engine.DEVICE}", flush=True)
        # Only one chapter held in RAM at a time. Raw chunks cached as generated.
        recordings = []
        for j, chunk in enumerate(chunks, 1):
            print(f"  {j}/{len(chunks)}: {chunk.text[:85]}", flush=True)
            recordings.append(engine.generate_cached(model, chunk.text, model.sr))
        audio, silence = engine.assemble(chunks, recordings, model.sr)
        temp = target.with_suffix(".partial.wav")
        ta.save(str(temp), audio, model.sr)
        temp.replace(target)
        samples = audio.shape[-1]
        manifest["chapters"][str(index)] = {
            "title": section.title, "signature": signature, "samples": samples,
            "sample_rate": model.sr, "chunks": len(chunks), "wav": str(target),
        }
        atomic_json(manifest_path, manifest)
        print(f"Saved {target.name}: {samples/model.sr/60:.1f} minutes; +{silence:.1f}s silence", flush=True)
        del audio, recordings
    return manifest


def ffmeta_escape(s: str):
    return s.replace("\\", "\\\\").replace("=", "\\=").replace(";", "\\;").replace("#", "\\#").replace("\n", " ")


def ffconcat_quote(path: Path):
    return "'" + str(path.resolve()).replace("'", "'\\''") + "'"


def export_m4b(epub_path: Path, title, author, sections, chosen, out: Path):
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
    output = out / (safe_name(title, 100) + ("_sample" if len(chosen)<len(sections) else "") + ".m4b")
    partial = output.with_name(output.stem + ".partial.m4b")
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "concat", "-safe", "0",
           "-i", str(concat), "-f", "ffmetadata", "-i", str(metadata),
           "-map", "0:a:0", "-map_metadata", "1", "-map_chapters", "1",
           "-c:a", "aac", "-b:a", "64k", "-ac", "1", "-ar", str(sr), str(partial)]
    print(f"\nEncoding {len(chosen)} section(s) into M4B ...", flush=True)
    try:
        subprocess.run(cmd, check=True)
        partial.replace(output)
    finally:
        if partial.exists():
            partial.unlink()
    print(f"M4B: {output}")
    print(f"Duration: {elapsed_samples/sr/3600:.2f} hours | {output.stat().st_size/1024**2:.1f} MiB")
    return output


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("command", choices=["inspect", "convert", "resume", "export"])
    parser.add_argument("epub", type=Path, help="EPUB file path")
    parser.add_argument("--chapters", help="Sections, e.g. 1-3 or 2,5,7 (use inspect to see numbers)")
    parser.add_argument("--no-export", action="store_true", help="Convert to chapter WAVs without encoding M4B")
    args = parser.parse_args()
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
    if args.command in ("convert", "resume"):
        generate_sections(epub_path, title, author, sections, selected, out)
        if not args.no_export:
            export_m4b(epub_path, title, author, sections, selected, out)
    else:
        export_m4b(epub_path, title, author, sections, selected, out)


if __name__ == "__main__":
    main()
