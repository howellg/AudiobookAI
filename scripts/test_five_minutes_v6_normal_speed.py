"""Arulia narration Test 6: contextual chunks, conservative audio assembly, CUDA OOM retry.

Save as ~/AudiobookAI/scripts/test_five_minutes.py and run in your Chatterbox venv.
Requires torch, torchaudio, and chatterbox-tts. Saves normal-speed audio only.

Optional:
  python scripts/test_five_minutes.py --list-chunks
  python scripts/test_five_minutes.py --regen 12,24

Raw speech is cached by exact input text. Changing speed/pauses never invalidates it.
"""
import argparse
import csv
import os
import gc
import hashlib
import re
import time
from dataclasses import dataclass
from pathlib import Path

import torch
import torchaudio as ta
from chatterbox.tts import ChatterboxTTS

ROOT = Path(__file__).resolve().parents[1]
TEXT_FILE = ROOT / "books" / "five_minute_test.txt"
VOICE_FILE = ROOT / "voices" / "british_narrator.wav"
OUTPUT_DIR = ROOT / "output"
CACHE_DIR = OUTPUT_DIR / "narration_cache_v2"  # Compatible with Tests 3/4/5
READABLE_DIR = OUTPUT_DIR / "narration_chunks_readable"
MANIFEST = OUTPUT_DIR / "narration_chunk_index.csv"
NORMAL_OUTPUT = OUTPUT_DIR / "five_minute_test6_normal.wav"

SPEED = 1.0  # Normal-speed output; pause targets are now applied directly.
EXAGGERATION = 0.35
CFG_WEIGHT = 0.3
TARGET_WORDS = 18
MAX_CHUNK_WORDS = 18  # Conservative for 4 GB VRAM; split larger units.
TOKEN_CAP = 350  # T3 tokens ~= 25/sec. Guard against runaway 800+ token samples.
MIN_PREFERRED_WORDS = 6  # Soft target: don't merge dialogue from different speakers blindly.
MAX_PEAK = 0.92
SILENCE_THRESHOLD = 0.002
SILENCE_SCAN_SECONDS = 0.8
LEADING_PAD_FINAL = 0.10  # Minimum silence before audio if source begins immediately.
# Desired total final-speed pauses, not additional pauses.
FINAL_COMMA_PAUSE = 0.16
FINAL_SENTENCE_PAUSE = 0.27
FINAL_DIALOGUE_PAUSE = 0.46
FINAL_PARAGRAPH_PAUSE = 0.40
FINAL_SCENE_PAUSE = 0.65
DEVICE = "cuda" if torch.cuda.is_available() else "cpu"


@dataclass
class Chunk:
    text: str
    boundary: str = "sentence"  # boundary AFTER this chunk
    dialogue: bool = False


def is_dialogue(text: str) -> bool:
    return text.lstrip().startswith(('"', '“', '‘'))


def paragraph_groups(text: str) -> list[tuple[str, bool]]:
    """Preserve blank lines. Isolate dialogue-starting lines when no blank line exists."""
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    paragraphs = []
    current = []
    def flush():
        if current:
            p = " ".join(current).strip()
            if p:
                paragraphs.append((p, False))
            current.clear()
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            flush()
            continue
        # Markdown title/subtitle: don't fuse headings with narrative.
        if line.startswith('#') or (line.startswith('*') and line.endswith('*') and len(line) < 100):
            flush()
            line = line.strip('#* _')
            if line:
                paragraphs.append((line, True))
            continue
        if current and is_dialogue(line):
            flush()
        current.append(line)
    flush()
    return paragraphs


def split_sentences(text: str) -> list[str]:
    """Heuristic splitter; attaches closing quotation marks to preceding sentence."""
    protected = text
    for abbrev in ("Mr.", "Mrs.", "Ms.", "Dr.", "Prof.", "St."):
        protected = re.sub(re.escape(abbrev), lambda m: m.group().replace('.', '<DOT>'), protected, flags=re.I)
    protected = re.sub(r'''([.!?]["”’']?)\s+(?=["“‘']?[A-Z0-9])''',
                       lambda m: m.group(1) + '\x00', protected)
    units = [p.replace('<DOT>', '.').strip() for p in protected.split('\x00') if p.strip()]
    # Rejoin short quoted speech and its *immediately following* attribution.
    # Example: '"What happened?" Brienne demanded.'
    merged = []
    tag = re.compile(r"^(?:[A-Z][\w'-]*|he|she|they)\s+(?:said|asked|replied|whispered|muttered|demanded|shouted|grumbled|cried|answered|exclaimed|gasped|murmured|called|warned)\b", re.I)
    for unit in units:
        if merged and tag.match(unit) and len(merged[-1].split()) < MIN_PREFERRED_WORDS and merged[-1].rstrip().endswith(('"', '”', '’')):
            merged[-1] += ' ' + unit
        else:
            merged.append(unit)
    return merged


def split_long(text: str, max_words: int = MAX_CHUNK_WORDS) -> list[str]:
    if len(text.split()) <= max_words:
        return [text]
    phrases = re.split(r'(?<=[,;:—–])\s+', text)
    result = []
    current = []
    for phrase in phrases:
        words = phrase.split()
        if current and len(current) + len(words) > max_words:
            result.append(' '.join(current))
            current = []
        if len(words) > max_words:
            if current:
                result.append(' '.join(current))
                current = []
            # Emergency word split only for unusually long punctuation-free phrases.
            result.extend(' '.join(words[i:i + max_words]) for i in range(0, len(words), max_words))
        else:
            current.extend(words)
    if current:
        result.append(' '.join(current))
    return result


def make_chunks(text: str) -> list[Chunk]:
    groups = paragraph_groups(text)
    output = []
    for paragraph, is_heading in groups:
        sentences = split_sentences(paragraph)
        # Combine short neighboring sentences within the SAME paragraph, not
        # across dialogue paragraphs or distinct speaker changes.
        combined = []
        for sentence in sentences:
            words = len(sentence.split())
            if (combined and len(combined[-1].split()) < MIN_PREFERRED_WORDS
                and len(combined[-1].split()) + words <= MAX_CHUNK_WORDS
                and is_dialogue(combined[-1]) == is_dialogue(sentence)):
                combined[-1] += ' ' + sentence
            elif (combined and words <= 3
                  and len(combined[-1].split()) + words <= TARGET_WORDS
                  and is_dialogue(combined[-1]) == is_dialogue(sentence)):
                combined[-1] += ' ' + sentence
            else:
                combined.append(sentence)
        units = []
        for sentence in combined:
            units.extend(split_long(sentence))
        for index, unit in enumerate(units):
            if index == len(units) - 1:
                boundary = 'scene' if is_heading else 'paragraph'
            else:
                stripped = unit.rstrip().rstrip('"”’\'').rstrip()
                boundary = 'comma' if stripped.endswith((',', ';', ':', '—', '–')) else 'sentence'
            output.append(Chunk(unit, boundary, is_dialogue(unit)))
    if output:
        output[-1].boundary = 'end'
    return output


def cache_key(text: str) -> str:
    # Exactly matches Tests 3/4, including the voice file mtime.
    st = VOICE_FILE.stat()
    signature = (f"chatterbox-tts|{text}|{EXAGGERATION}|{CFG_WEIGHT}|"
                 f"{VOICE_FILE.resolve()}|{st.st_size}|{st.st_mtime_ns}")
    return hashlib.sha256(signature.encode('utf-8')).hexdigest()[:24]


def readable_filename(index: int, chunk: Chunk) -> str:
    # The number corresponds to --list-chunks and --regen.
    slug = re.sub(r'[^a-zA-Z0-9]+', '_', chunk.text).strip('_')[:72]
    return f'{index:03d}_{slug or "empty"}.wav'


def write_manifest(chunks: list[Chunk]) -> None:
    READABLE_DIR.mkdir(parents=True, exist_ok=True)
    # Remove old symlinks only. Never remove genuine files.
    for old in READABLE_DIR.glob('*.wav'):
        if old.is_symlink():
            old.unlink()
    with MANIFEST.open('w', encoding='utf-8', newline='') as f:
        writer = csv.writer(f)
        writer.writerow(['number', 'readable_name', 'boundary', 'text', 'cache_file'])
        for n, chunk in enumerate(chunks, 1):
            name = readable_filename(n, chunk)
            raw_path = CACHE_DIR / f'{cache_key(chunk.text)}.wav'
            writer.writerow([n, name, chunk.boundary, chunk.text, str(raw_path)])
            if raw_path.exists():
                (READABLE_DIR / name).symlink_to(raw_path.resolve())


def link_chunk(n: int, chunk: Chunk) -> None:
    READABLE_DIR.mkdir(parents=True, exist_ok=True)
    link = READABLE_DIR / readable_filename(n, chunk)
    raw_path = CACHE_DIR / f'{cache_key(chunk.text)}.wav'
    if raw_path.exists():
        if link.is_symlink():
            link.unlink()
        if not link.exists():
            link.symlink_to(raw_path.resolve())


def clean_tensor(wav: torch.Tensor) -> torch.Tensor:
    # clone() is required to avoid in-place modifications to inference tensors.
    wav = wav.detach().to(device='cpu', dtype=torch.float32).clone()
    if wav.ndim == 1:
        wav = wav.unsqueeze(0)
    if wav.shape[-1] == 0:
        raise ValueError('Chatterbox returned an empty audio tensor.')
    return wav


class OverlongGeneration(Exception):
    """Generation reached the protective token ceiling without a clean finish."""


def capped_generate(model, text: str):
    # Installed Chatterbox hard-codes 1000 T3 tokens inside generate().
    # Limit that internal call to avoid attention-cache growth past GTX 1650 VRAM.
    original = model.t3.inference
    def capped_inference(*args, **kwargs):
        kwargs['max_new_tokens'] = min(kwargs.get('max_new_tokens') or TOKEN_CAP, TOKEN_CAP)
        return original(*args, **kwargs)
    model.t3.inference = capped_inference
    try:
        with torch.inference_mode():
            return model.generate(text, audio_prompt_path=str(VOICE_FILE),
                                  exaggeration=EXAGGERATION, cfg_weight=CFG_WEIGHT)
    finally:
        model.t3.inference = original


def split_for_retry(text: str) -> tuple[str, str]:
    words = text.split()
    mid = len(words) // 2
    candidates = [i for i in range(3, len(words)-2) if words[i-1].endswith((',', ';', ':', '—', '–', '.', '?', '!'))]
    if candidates:
        mid = min(candidates, key=lambda i: abs(i-mid))
    return ' '.join(words[:mid]), ' '.join(words[mid:])


def generate_cached(model, text: str, sr: int, depth: int = 0) -> torch.Tensor:
    path = CACHE_DIR / f'{cache_key(text)}.wav'
    if path.is_file():
        raw, file_sr = ta.load(str(path))
        if file_sr != sr:
            raw = ta.functional.resample(raw, file_sr, sr)
        print('Using cached audio.')
        return clean_tensor(raw)
    try:
        started = time.monotonic()
        raw = clean_tensor(capped_generate(model, text))
        # The model normally finishes well below the cap. If it runs to the
        # ceiling, don't cache possibly truncated speech; split and retry.
        if raw.shape[-1] / sr >= (TOKEN_CAP - 4) / 25.0:
            del raw
            raise OverlongGeneration('Model did not finish before the token ceiling')
        ta.save(str(path), raw, sr)
        print(f'Generated {raw.shape[-1]/sr:.2f}s in {time.monotonic()-started:.2f}s')
        return raw
    except (torch.OutOfMemoryError, OverlongGeneration) as error:
        print(f'{type(error).__name__}: splitting the chunk and retrying')
        gc.collect()
        if torch.cuda.is_available():
            torch.cuda.empty_cache()
        words = text.split()
        if len(words) < 8 or depth >= 5:
            raise RuntimeError(
                'Could not generate even this short chunk. Restart the script '
                '(completed chunks are cached), or reduce TOKEN_CAP / chunk length.'
            ) from None
        left, right = split_for_retry(text)
        print(f'Retrying as {len(left.split())} + {len(right.split())} words')
        a = generate_cached(model, left, sr, depth + 1)
        b = generate_cached(model, right, sr, depth + 1)
        joined = torch.cat((a, torch.zeros((a.shape[0], round(.18*sr))), b), dim=-1)
        ta.save(str(path), joined, sr)
        return joined


def edge_silence(wav: torch.Tensor, sr: int, at_end: bool) -> float:
    n = min(wav.shape[-1], round(SILENCE_SCAN_SECONDS * sr))
    samples = wav[:, -n:] if at_end else wav[:, :n]
    active = (samples.abs().amax(0) > SILENCE_THRESHOLD).nonzero().flatten()
    if active.numel() == 0:
        return n / sr
    return ((n - 1 - int(active[-1])) if at_end else int(active[0])) / sr


def target_pause(cur: Chunk, nxt: Chunk) -> float:
    pauses = {'comma': FINAL_COMMA_PAUSE, 'sentence': FINAL_SENTENCE_PAUSE,
              'paragraph': FINAL_PARAGRAPH_PAUSE, 'scene': FINAL_SCENE_PAUSE}
    target = pauses.get(cur.boundary, FINAL_SENTENCE_PAUSE)
    if cur.dialogue != nxt.dialogue or (cur.dialogue and nxt.dialogue and cur.boundary == 'paragraph'):
        target = max(target, FINAL_DIALOGUE_PAUSE)
    return target


def assemble(chunks: list[Chunk], recordings: list[torch.Tensor], sr: int) -> tuple[torch.Tensor, float]:
    """Preserve raw onsets/endings; insert only missing silence between chunks."""
    parts = []
    added = 0.0
    for i, (chunk, raw) in enumerate(zip(chunks, recordings)):
        wav = clean_tensor(raw)
        if i == 0:
            lead = max(0, LEADING_PAD_FINAL * SPEED - edge_silence(wav, sr, False))
            if lead > 0:
                parts.append(torch.zeros((wav.shape[0], round(lead * sr))))
        parts.append(wav)  # Absolutely no trim or fade: isolate onset-quality issues.
        if i + 1 < len(chunks):
            desired = target_pause(chunk, chunks[i+1]) * SPEED
            existing = edge_silence(wav, sr, True) + edge_silence(recordings[i+1], sr, False)
            extra = max(0, desired - existing)
            if extra > 0:
                parts.append(torch.zeros((wav.shape[0], round(extra * sr))))
                added += extra
    audio = torch.cat(parts, -1)
    peak = float(audio.abs().max())
    if peak > MAX_PEAK:
        audio = audio * (MAX_PEAK / peak)
        print(f'Peak attenuation: {peak:.3f} -> {MAX_PEAK:.3f}')
    return audio, added


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--list-chunks', action='store_true', help='Preview chunk IDs and text without loading Chatterbox')
    parser.add_argument('--regen', default='', help='Comma-separated 1-based chunk numbers to regenerate')
    args = parser.parse_args()
    if not TEXT_FILE.is_file() or not VOICE_FILE.is_file():
        raise FileNotFoundError(f'Check these paths:\n{TEXT_FILE}\n{VOICE_FILE}')
    text = TEXT_FILE.read_text(encoding='utf-8-sig').strip()
    chunks = make_chunks(text)
    if args.list_chunks:
        OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        write_manifest(chunks)
        for i, c in enumerate(chunks, 1):
            print(f'{i:03d} [{c.boundary:9s}] {c.text}')
        print(f'\nTotal: {len(chunks)} chunks')
        return
    regen = {int(x) for x in args.regen.split(',') if x.strip()}
    if any(n < 1 or n > len(chunks) for n in regen):
        parser.error(f'--regen must use chunk numbers 1 to {len(chunks)}')
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    write_manifest(chunks)
    print(f'Readable chunk index: {MANIFEST}')
    print(f'Audio shortcuts: {READABLE_DIR}')
    print(f'Device: {DEVICE} | Words: {len(text.split())} | Chunks: {len(chunks)} | Speed: {SPEED}x')
    for index in sorted(regen):
        path = CACHE_DIR / f'{cache_key(chunks[index-1].text)}.wav'
        if path.is_file():
            path.unlink()
            print(f'Cleared chunk {index} from cache')
    started = time.monotonic()
    model = ChatterboxTTS.from_pretrained(device=DEVICE)
    recordings = []
    for i, chunk in enumerate(chunks, 1):
        print(f'\nChunk {i}/{len(chunks)}: {chunk.text[:120]}')
        recordings.append(generate_cached(model, chunk.text, model.sr))
        link_chunk(i, chunk)
    audio, silence_added = assemble(chunks, recordings, model.sr)
    ta.save(str(NORMAL_OUTPUT), audio, model.sr)
    print('\nNarration complete!')
    print(f'Normal-speed WAV: {NORMAL_OUTPUT}')
    print(f'Added silence (pre-speed): {silence_added:.1f}s')
    print(f'Normal duration: {audio.shape[-1]/model.sr/60:.2f} min')
    print(f'Processing time: {(time.monotonic()-started)/60:.2f} min')

if __name__ == '__main__':
    main()
