"""Text-substitution pronunciation dictionary for AudiobookAI Studio V2.5.

Chatterbox accepts written speech, not a phoneme/IPA dictionary. The *spoken*
field is therefore a text hint, e.g. ``Nynaeve -> Nigh-neev``. The original EPUB
is never edited. Matching is case-insensitive, whole-word/whole-phrase, longest
first, and single-pass (replacement output is not recursively substituted).

The editable dictionary lives in ignored output/, while per-job immutable snapshots
are saved beside it and referenced by completed chapter manifests. A job created
before V2.5 has no snapshot and retains the original V7 behavior.
"""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = ROOT / 'output' / 'pronunciation'
DICTIONARY_PATH = DATA_DIR / 'dictionary.json'
SNAPSHOT_DIR = DATA_DIR / 'jobs'
MAX_TERM_LENGTH = 100
MAX_SPOKEN_LENGTH = 150


def _atomic_write(path: Path, data: dict) -> None:
    """Avoid truncated JSON if the computer shuts down during a save."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + '.tmp')
    try:
        temporary.write_text(json.dumps(data, indent=2, ensure_ascii=False) + '\n', encoding='utf-8')
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def validate_rule(term: str, spoken: str) -> dict[str, str]:
    """Require nonempty, single-line, reasonably sized speech substitutions."""
    term = re.sub(r'[ \t]+', ' ', term.strip())
    spoken = re.sub(r'[ \t]+', ' ', spoken.strip())
    if not term or not spoken:
        raise ValueError('Both the written term and spoken text are required.')
    if any(c in term + spoken for c in '\r\n\x00'):
        raise ValueError('Pronunciation entries must stay on one line.')
    if len(term) > MAX_TERM_LENGTH or len(spoken) > MAX_SPOKEN_LENGTH:
        raise ValueError('Pronunciation entry is too long.')
    if not (term[0].isalnum() and term[-1].isalnum()):
        raise ValueError('Written term must begin and end with a letter or number.')
    if len(spoken.split()) > 6:
        raise ValueError('Spoken text is limited to six words to protect the TTS chunk size.')
    return {'term': term, 'spoken': spoken}


def _rules(value) -> list[dict[str, str]]:
    """Validate a rule collection and forbid ambiguous duplicate casefold keys."""
    if not isinstance(value, list):
        raise ValueError('Pronunciation rules must be a list.')
    seen = set()
    validated = []
    for rule in value:
        if not isinstance(rule, dict):
            raise ValueError('Pronunciation rule must be an object.')
        clean = validate_rule(rule.get('term', ''), rule.get('spoken', ''))
        key = clean['term'].casefold()
        if key in seen:
            raise ValueError(f'Duplicate pronunciation term: {clean["term"]}')
        seen.add(key)
        validated.append(clean)
    return validated


def load_dictionary(path: Path = DICTIONARY_PATH) -> dict:
    """Read global and per-EPUB dictionaries; no file means empty defaults."""
    if not Path(path).exists():
        return {'version': 1, 'global': [], 'books': {}}
    data = json.loads(Path(path).read_text(encoding='utf-8'))
    if not isinstance(data, dict) or data.get('version') != 1 or not isinstance(data.get('books', {}), dict):
        raise ValueError('Unsupported pronunciation dictionary format.')
    books = {str(k): _rules(v) for k, v in data.get('books', {}).items()}
    return {'version': 1, 'global': _rules(data.get('global', [])), 'books': books}


def save_rule(scope: str, book_path: str | Path | None, term: str, spoken: str,
              path: Path = DICTIONARY_PATH) -> None:
    """Add/update a global or book-local rule without changing any queued job."""
    rule = validate_rule(term, spoken)
    data = load_dictionary(path)
    target = _scope_rules(data, scope, book_path)
    for i, old in enumerate(target):
        if old['term'].casefold() == rule['term'].casefold():
            target[i] = rule
            break
    else:
        target.append(rule)
    _atomic_write(Path(path), data)


def delete_rule(scope: str, book_path: str | Path | None, term: str,
                path: Path = DICTIONARY_PATH) -> None:
    """Remove one dictionary rule; already-queued snapshots remain immutable."""
    data = load_dictionary(path)
    rules = _scope_rules(data, scope, book_path)
    rules[:] = [r for r in rules if r['term'].casefold() != term.casefold()]
    _atomic_write(Path(path), data)


def _scope_rules(data: dict, scope: str, book_path: str | Path | None) -> list:
    """Resolve editable target, using EPUB absolute path as stable book identity."""
    if scope == 'global':
        return data['global']
    if scope != 'book' or not book_path:
        raise ValueError('Choose a book for a book-specific rule.')
    return data['books'].setdefault(str(Path(book_path).expanduser().resolve()), [])


def scoped_rules(scope: str, book_path: str | Path | None, path: Path = DICTIONARY_PATH) -> list:
    """Return a copy of only the rules belonging to one editable GUI scope."""
    data = load_dictionary(path)
    return list(_scope_rules(data, scope, book_path))


def effective_rules(book_path: str | Path, path: Path = DICTIONARY_PATH) -> list:
    """Merge global and book-specific rules; book entries override global ones."""
    data = load_dictionary(path)
    combined = {r['term'].casefold(): r for r in data['global']}
    for rule in data['books'].get(str(Path(book_path).expanduser().resolve()), []):
        combined[rule['term'].casefold()] = rule
    return list(combined.values())


def snapshot_for_job(job_id: int, book_path: str | Path,
                     dictionary_path: Path = DICTIONARY_PATH,
                     snapshot_dir: Path = SNAPSHOT_DIR) -> Path:
    """Freeze effective rules for the lifetime of a queued part and its chapter edits."""
    rules = effective_rules(book_path, dictionary_path)
    path = Path(snapshot_dir) / f'job_{int(job_id)}.json'
    if path.exists():
        raise FileExistsError(f'Existing pronunciation snapshot must not be overwritten: {path}')
    _atomic_write(path, {'version': 1, 'job_id': int(job_id),
                         'book_path': str(Path(book_path).expanduser().resolve()),
                         'rules': rules})
    return path


def load_snapshot(path: str | Path) -> list:
    """Read only a queued immutable snapshot, never the live editable dictionary."""
    data = json.loads(Path(path).read_text(encoding='utf-8'))
    if not isinstance(data, dict) or data.get('version') != 1 or 'rules' not in data:
        raise ValueError('Invalid pronunciation snapshot format.')
    return _rules(data['rules'])


def snapshot_path_for_job(job_id: int, snapshot_dir: Path = SNAPSHOT_DIR) -> Path | None:
    """Old jobs intentionally have no pronunciation snapshot and use original text."""
    path = Path(snapshot_dir) / f'job_{int(job_id)}.json'
    return path if path.is_file() else None


def apply_rules(text: str, rules: list) -> str:
    """Replace whole words/phrases once; never cascade substitutions into each other.

    Longest terms win, so 'Aes Sedai' takes precedence over 'Sedai'. Spaces inside
    a term also match a run of ordinary inline whitespace, but not paragraph breaks.
    """
    rules = _rules(rules)
    if not rules or not text:
        return text
    mapping = {r['term'].casefold(): r['spoken'] for r in rules}
    # Sort longest phrases first to keep multiword rules from being shadowed.
    ordered = sorted(rules, key=lambda r: (-len(r['term']), r['term'].casefold()))
    expressions = [r'(?<!\w)' + r'[ \t]+'.join(map(re.escape, r['term'].split())) + r'(?!\w)'
                   for r in ordered]
    compiled = re.compile('|'.join('(?:' + part + ')' for part in expressions), re.IGNORECASE)
    return compiled.sub(lambda match: mapping[re.sub(r'[ \t]+', ' ', match.group()).casefold()], text)


def rules_digest(rules: list) -> str:
    """Compute a stable identifier for audit logs (no original book text stored)."""
    ordered = sorted(_rules(rules), key=lambda r: r['term'].casefold())
    return hashlib.sha256(json.dumps(ordered, sort_keys=True, ensure_ascii=False).encode()).hexdigest()[:16]
