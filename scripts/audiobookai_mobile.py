"""Loopback-only mobile dashboard HTTP transport for AudiobookAI Studio.

NO Qt, SQLite, torch, file operations, nor converter execution happens in the
HTTP threads. Every status/action request is dispatched to the GUI thread.
Publish to your *private* tailnet with Tailscale Serve, never Funnel.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import hmac
import json
from pathlib import Path
import queue
import threading
from urllib.parse import urlsplit

STATIC_DIR = Path(__file__).resolve().parent / 'mobile'
MAX_JSON = 48 * 1024
MAX_EPUB = 24 * 1024 * 1024
LOCAL_HOST = '127.0.0.1'
DEFAULT_PORT = 8765


@dataclass
class MobileRequest:
    operation: str
    payload: dict
    completed: threading.Event = field(default_factory=threading.Event)
    result: object = None
    error: str | None = None
    processing: bool = False
    cancelled: bool = False
    lock: threading.Lock = field(default_factory=threading.Lock)


class MobileBridge:
    """Thread-safe request inbox consumed by the Qt event loop."""
    def __init__(self):
        self.pending: queue.Queue[MobileRequest] = queue.Queue(maxsize=32)

    def call(self, operation, payload):
        request = MobileRequest(operation, payload)
        try:
            self.pending.put_nowait(request)
        except queue.Full as exc:
            raise RuntimeError('Mobile controller busy; retry shortly.') from exc
        if not request.completed.wait(timeout=45 if operation in ('upload', 'add') else 15):
            # If Qt is still busy with another task, prevent a stale queued
            # command from executing after the caller has already timed out.
            with request.lock:
                if not request.processing:
                    request.cancelled = True
            raise RuntimeError(
                'Studio timed out. The operation may still be finishing; '
                'refresh the queue before trying again.')
        if request.error:
            raise ValueError(request.error)
        return request.result

    def process(self, callback, maximum=12):
        """Must run on the Qt GUI thread, never from an HTTP thread."""
        for _ in range(maximum):
            try:
                request = self.pending.get_nowait()
            except queue.Empty:
                break
            with request.lock:
                if request.cancelled:
                    request.completed.set()
                    continue
                request.processing = True
            try:
                request.result = callback(request.operation, request.payload)
            except Exception as exc:
                request.error = str(exc)[:500] or type(exc).__name__
            finally:
                request.completed.set()


class MobileHTTPServer:
    """Authenticated stdlib HTTP server bound exclusively to loopback."""
    def __init__(self, bridge, token: str, port=DEFAULT_PORT):
        if len(token) < 24:
            raise ValueError('Mobile access key must be at least 24 characters.')
        self.bridge = bridge
        self.token = token
        self.port = int(port)
        if not (1024 <= self.port <= 65535):
            raise ValueError('Mobile dashboard port out of range.')
        owner = self

        class Handler(BaseHTTPRequestHandler):
            server_version = 'AudiobookAI-Mobile/1'
            sys_version = ''

            def log_message(self, format, *args):
                # Never log URL fragments, tokens, user-submitted EPUB names.
                return

            def _headers(self, status, content_type, length):
                self.send_response(status)
                self.send_header('Content-Type', content_type)
                self.send_header('Content-Length', str(length))
                self.send_header('Cache-Control', 'no-store')
                self.send_header('Referrer-Policy', 'no-referrer')
                self.send_header('X-Content-Type-Options', 'nosniff')
                self.send_header('X-Frame-Options', 'DENY')
                self.send_header('Content-Security-Policy',
                                 "default-src 'none'; script-src 'self'; style-src 'self'; "
                                 "connect-src 'self'; img-src 'self' data:; "
                                 "base-uri 'none'; form-action 'self'; frame-ancestors 'none'")
                self.end_headers()

            def _reply(self, status, data):
                raw = json.dumps(data, separators=(',', ':'), ensure_ascii=False).encode('utf-8')
                self._headers(status, 'application/json; charset=utf-8', len(raw))
                self.wfile.write(raw)

            def _allowed(self):
                auth = self.headers.get('Authorization', '')
                expected = 'Bearer ' + owner.token
                return hmac.compare_digest(auth, expected)

            def _same_origin(self):
                origin = self.headers.get('Origin')
                if not origin:
                    return True  # CLI clients still need the bearer key.
                try:
                    origin_parsed = urlsplit(origin)
                    return (origin_parsed.scheme in ('https', 'http')
                            and origin_parsed.netloc.lower() ==
                            self.headers.get('Host', '').lower())
                except ValueError:
                    return False

            def do_GET(self):
                path = urlsplit(self.path).path
                if path in ('/', '/index.html', '/mobile.js', '/mobile.css'):
                    name = {'/': 'index.html', '/index.html': 'index.html',
                            '/mobile.js': 'mobile.js', '/mobile.css': 'mobile.css'}[path]
                    contents = (STATIC_DIR / name).read_bytes()
                    types = {'index.html': 'text/html; charset=utf-8',
                             'mobile.js': 'application/javascript; charset=utf-8',
                             'mobile.css': 'text/css; charset=utf-8'}
                    self._headers(200, types[name], len(contents))
                    self.wfile.write(contents)
                    return
                if not path.startswith('/api/'):
                    self._reply(404, {'error': 'Not found'})
                    return
                if not self._allowed():
                    self._reply(401, {'error': 'Access key required'})
                    return
                try:
                    if path == '/api/status':
                        result = owner.bridge.call('status', {})
                    elif path == '/api/book':
                        args = urlsplit(self.path).query
                        from urllib.parse import parse_qs
                        params = parse_qs(args)
                        result = owner.bridge.call('book', {'book_id': params.get('id', [''])[0]})
                    else:
                        self._reply(404, {'error': 'Unknown endpoint'})
                        return
                    self._reply(200, result)
                except (RuntimeError, ValueError) as exc:
                    self._reply(409, {'error': str(exc)})
                except Exception:
                    self._reply(500, {'error': 'Unexpected dashboard error'})

            def do_POST(self):
                if not self._allowed():
                    self._reply(401, {'error': 'Access key required'})
                    return
                if not self._same_origin():
                    self._reply(403, {'error': 'Cross-origin requests are prohibited'})
                    return
                path = urlsplit(self.path).path
                if path not in ('/api/action', '/api/upload'):
                    self._reply(404, {'error': 'Unknown endpoint'})
                    return
                try:
                    length = int(self.headers.get('Content-Length', '0'))
                except ValueError:
                    self._reply(400, {'error': 'Invalid Content-Length'})
                    return
                maximum = MAX_EPUB if path == '/api/upload' else MAX_JSON
                if length < 1 or length > maximum:
                    self._reply(413, {'error': 'Request body size is invalid or too large'})
                    return
                try:
                    data = self.rfile.read(length)
                    if len(data) != length:
                        raise ValueError('Incomplete request body')
                    if path == '/api/upload':
                        if not self.headers.get('Content-Type', '').startswith(
                                ('application/epub+zip', 'application/octet-stream')):
                            raise ValueError('Upload must be an EPUB file')
                        name = self.headers.get('X-Filename', '')
                        result = owner.bridge.call('upload', {'filename': name, 'content': data})
                    else:
                        if not self.headers.get('Content-Type', '').startswith('application/json'):
                            raise ValueError('Expected application/json')
                        payload = json.loads(data)
                        if not isinstance(payload, dict):
                            raise ValueError('JSON object required')
                        command = payload.get('command')
                        if not isinstance(command, str) or len(command) > 40:
                            raise ValueError('Invalid command')
                        result = owner.bridge.call(command, payload)
                    self._reply(200, {'ok': True, 'result': result})
                except (ValueError, RuntimeError, json.JSONDecodeError) as exc:
                    self._reply(400, {'error': str(exc)})
                except Exception:
                    self._reply(500, {'error': 'Unexpected dashboard error'})

        class HTTPD(ThreadingHTTPServer):
            daemon_threads = True
            allow_reuse_address = True
        self.server = HTTPD((LOCAL_HOST, self.port), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True,
                                       name='AudiobookAI-Mobile-HTTP')

    def start(self):
        self.thread.start()

    def stop(self):
        self.server.shutdown()
        self.server.server_close()
        if self.thread.is_alive():
            self.thread.join(timeout=2)
