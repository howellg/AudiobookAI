"""Small optional ntfy.sh notification sender; never runs on the narration thread.

The ntfy.sh service has public/readable-by-topic topics by default. Treat the
random topic as a secret; don't include book names, filenames, URLs, or logs
in messages. No Qt, SQLite, TTS or dependencies beyond Python's stdlib.
"""
from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
import queue
import re
import secrets
import stat
import tempfile
import threading
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[1]
CONFIG_PATH = ROOT / 'output' / 'ntfy.json'
NTFY_HOST = 'https://ntfy.sh'
TOPIC_RE = re.compile(r'^[A-Za-z0-9_-]{24,64}$')
EVENTS = {
    'test': {
        'title': 'AudiobookAI - notification test',
        'message': 'Notifications are working. Your phone does not need Tailscale for alerts.',
        'priority': '3',
        'tags': 'headphones,white_check_mark',
    },
    'queue_complete': {
        'title': 'AudiobookAI - queue finished',
        'message': 'All queued audiobook parts finished successfully.',
        'priority': '3',
        'tags': 'headphones,white_check_mark',
    },
    'generation_failed': {
        'title': 'AudiobookAI - generation failed',
        'message': 'An audiobook part needs attention. Check the queue and diagnostic log.',
        'priority': '4',
        'tags': 'warning',
    },
    'storage_unavailable': {
        'title': 'AudiobookAI - cache SSD unavailable',
        'message': 'Generation is paused because narration cache storage is unavailable.',
        'priority': '4',
        'tags': 'warning',
    },
    'queue_stalled': {
        'title': 'AudiobookAI - queue may be stalled',
        'message': 'Progress has stopped for longer than expected. Check Studio queue activity.',
        'priority': '4',
        'tags': 'warning',
    },
    'quality_attention': {
        'title': 'AudiobookAI - quality review needed',
        'message': 'Automatic quality checks flagged audio that may need review. Open Studio Quality Control.',
        'priority': '3',
        'tags': 'headphones,warning',
    },
}
DEFAULT_FLAGS = {
    'queue_complete': True,
    'generation_failed': True,
    'storage_unavailable': True,
    'quality_attention': True,
    'queue_stalled': False,
}


def _validated(config: dict) -> dict:
    if not isinstance(config, dict):
        raise ValueError('Notification config must be a JSON object.')
    topic = config.get('topic')
    if not isinstance(topic, str) or not TOPIC_RE.fullmatch(topic):
        raise ValueError('Invalid ntfy topic. Configure a random secret topic.')
    if type(config.get('enabled')) is not bool:
        raise ValueError('Notification enabled field must be boolean.')
    flags = config.get('events')
    # V2.12.0 profiles lack newer alert types. Preserve their topic and
    # existing choices while enabling safe defaults for additional events.
    if not isinstance(flags, dict) or any(type(flags.get(k, DEFAULT_FLAGS[k])) is not bool
                                          for k in DEFAULT_FLAGS):
        raise ValueError('Notification event settings are invalid.')
    return {'topic': topic, 'enabled': config['enabled'],
            'events': {key: flags.get(key, default) for key, default in DEFAULT_FLAGS.items()}}


def read_config(path: Path = CONFIG_PATH) -> dict:
    path = Path(path)
    if path.is_symlink():
        raise ValueError('Notification config must not be a symlink.')
    with path.open('r', encoding='utf-8') as file:
        return _validated(json.load(file))


def write_config(config: dict, path: Path = CONFIG_PATH) -> None:
    """Atomic private write (0600) with safe replacement; topic never hits Git."""
    data = _validated(config)
    path = Path(path)
    if path.is_symlink():
        raise ValueError('Notification config must not be a symlink.')
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix='.ntfy_', suffix='.json', dir=path.parent)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, 'w', encoding='utf-8') as stream:
            json.dump(data, stream, indent=2)
            stream.write('\n')
        if path.is_symlink():
            raise ValueError('Notification config changed to a symlink.')
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def ensure_config(path: Path = CONFIG_PATH, *, enabled=False) -> dict:
    """Create a private 192-bit random topic if one does not already exist."""
    path = Path(path)
    if path.exists() or path.is_symlink():
        config = read_config(path)
        # Harden any previously created config on this machine.
        if stat.S_IMODE(path.stat().st_mode) & 0o077:
            path.chmod(0o600)
        return config
    config = {'topic': 'audiobookai_' + secrets.token_urlsafe(24),
              'enabled': bool(enabled),
              'events': DEFAULT_FLAGS.copy()}
    write_config(config, path)
    return config


def publish(topic: str, event: str, *, opener=urlopen) -> None:
    """Synchronous HTTP publish. Always invoke from a worker, never Qt."""
    if not TOPIC_RE.fullmatch(topic):
        raise ValueError('Invalid ntfy topic')
    if event not in EVENTS:
        raise ValueError('Unsupported notification event')
    content = EVENTS[event]
    request = Request(
        f'{NTFY_HOST}/{topic}',
        data=content['message'].encode('utf-8'),
        method='POST',
        headers={'Content-Type': 'text/plain; charset=utf-8',
                 'Title': content['title'],
                 'Priority': content['priority'],
                 'Tags': content['tags']})
    with opener(request, timeout=8) as response:
        if not (200 <= response.status < 300):
            raise RuntimeError('ntfy rejected the notification')


@dataclass(frozen=True)
class DeliveryResult:
    event: str
    sent: bool
    detail: str


class NtfyPublisher:
    """Fast, non-blocking notification dispatch; failures never stop TTS."""
    def __init__(self, path: Path = CONFIG_PATH, *, sender=publish):
        self.path = Path(path)
        self.config = ensure_config(self.path)
        self.sender = sender
        self.results: queue.SimpleQueue[DeliveryResult] = queue.SimpleQueue()
        self._lock = threading.Lock()
        self._pending = 0

    def update(self, *, enabled=None, event=None, checked=None) -> dict:
        with self._lock:
            config = {**self.config, 'events': dict(self.config['events'])}
            if enabled is not None:
                config['enabled'] = bool(enabled)
            if event is not None:
                if event not in DEFAULT_FLAGS or checked is None:
                    raise ValueError('Unknown event setting')
                config['events'][event] = bool(checked)
            write_config(config, self.path)
            self.config = config
            return config

    def notify(self, event: str) -> bool:
        if event not in EVENTS:
            raise ValueError('Unsupported notification event')
        with self._lock:
            if not self.config['enabled']:
                return False
            if event != 'test' and not self.config['events'][event]:
                return False
            # Avoid queuing repeated alerts if ntfy.sh is unreachable.
            if self._pending >= 4:
                return False
            topic = self.config['topic']
            self._pending += 1

        def deliver():
            try:
                self.sender(topic, event)
                result = DeliveryResult(event, True, 'Delivered to ntfy.sh')
            except Exception:
                # urllib HTTPError includes the secret topic in its URL.
                # Never display/log the exception or traceback.
                result = DeliveryResult(event, False, 'Delivery failed; check internet and ntfy.sh')
            finally:
                with self._lock:
                    self._pending -= 1
            self.results.put(result)

        threading.Thread(target=deliver, daemon=True,
                         name=f'AudiobookAI-ntfy-{event}').start()
        return True

    def drain(self) -> tuple[list[DeliveryResult], int]:
        messages = []
        while True:
            try:
                messages.append(self.results.get_nowait())
            except queue.Empty:
                break
        with self._lock:
            pending = self._pending
        return messages, pending
