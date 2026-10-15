#!/usr/bin/env python3
"""AudiobookAI's resilient local job store and optional MySQL read replica.

SQLite is the single source of truth for queue and job progress. MySQL is a
transactional, queryable mirror updated at connection time and after changes.
Generation can continue safely if MySQL is temporarily unavailable, and the
next successful sync restores the database from the local journal of record.
The mirror is intentionally one-way: edit books/jobs through the application.
"""

# MAINTAINER NOTE: Canonical SQLite books/jobs store with optional one-way MySQL catalogue mirroring. SQLite remains usable without MySQL.
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from datetime import datetime
from pathlib import Path

CONFIG_PATH = Path.home() / '.config' / 'audiobookai' / 'studio_config.json'
KEYRING_SERVICE = 'AudiobookAI Studio MySQL'


def now():
    """Return a human-readable local timestamp for catalogue metadata."""
    return datetime.now().strftime('%Y-%m-%d %H:%M:%S')


def default_config():
    """Provide nonsecret connection defaults; MySQL starts disabled."""
    return {'mysql': {'enabled': False, 'host': '127.0.0.1', 'port': 3306,
                      'database': 'audiobookai', 'user': 'audiobookai',
                      'use_keyring': False}}


def read_config(path=CONFIG_PATH):
    """Merge whitelisted stored settings into safe defaults."""
    cfg = default_config()
    if path.is_file():
        try:
            parsed = json.loads(path.read_text(encoding='utf-8'))
            for k in ('enabled', 'host', 'port', 'database', 'user', 'use_keyring'):
                if k in parsed.get('mysql', {}):
                    cfg['mysql'][k] = parsed['mysql'][k]
        except (ValueError, OSError, AttributeError):
            pass
    return cfg


def write_config(cfg, path=CONFIG_PATH):
    """Persist whitelisted settings atomically without storing passwords."""
    path.parent.mkdir(parents=True, exist_ok=True)
    # Explicitly whitelist public fields; never save a password to a JSON file.
    safe = default_config()
    for k in safe['mysql']:
        if k in cfg.get('mysql', {}):
            safe['mysql'][k] = cfg['mysql'][k]
    tmp = path.with_name(path.name + '.tmp')
    tmp.write_text(json.dumps(safe, indent=2), encoding='utf-8')
    os.chmod(tmp, 0o600)
    tmp.replace(path)


def credential_name(cfg):
    """Build a keyring identifier scoped to the configured MySQL endpoint."""
    return f"{cfg['user']}@{cfg['host']}:{cfg['port']}/{cfg['database']}"


def lookup_password(cfg):
    """Prefer an environment variable, then optionally query the OS keyring."""
    env = os.environ.get('AUDIOBOOKAI_MYSQL_PASSWORD')
    if env is not None:
        return env
    if not cfg.get('use_keyring'):
        return None
    try:
        import keyring
        return keyring.get_password(KEYRING_SERVICE, credential_name(cfg))
    except Exception:
        return None


def save_password(cfg, password):
    """Store the MySQL credential in the OS keyring when explicitly requested."""
    import keyring
    keyring.set_password(KEYRING_SERVICE, credential_name(cfg), password)


class Store:
    """SQLite canonical store, backwards-compatible with V2's jobs/books."""

    def __init__(self, path):
        """Open SQLite, create missing tables and columns, and mark abandoned jobs interrupted."""
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        # Keep related books, sections, and jobs consistent across all upgrades.
        self.db.execute('PRAGMA foreign_keys=ON')
        self.db.execute('PRAGMA busy_timeout=5000')
        self.on_change = None
        self.db.executescript('''
        CREATE TABLE IF NOT EXISTS books (
            id INTEGER PRIMARY KEY, path TEXT NOT NULL UNIQUE,
            title TEXT NOT NULL, author TEXT NOT NULL,
            sections INTEGER NOT NULL, words INTEGER NOT NULL,
            added TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS jobs (
            id INTEGER PRIMARY KEY, book_id INTEGER NOT NULL REFERENCES books(id),
            name TEXT NOT NULL, indices TEXT NOT NULL, output_name TEXT NOT NULL,
            status TEXT NOT NULL, added TEXT NOT NULL, finished TEXT,
            output_path TEXT, error TEXT DEFAULT ''
        );
        CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status, id);
        CREATE TABLE IF NOT EXISTS book_sections (
            book_id INTEGER NOT NULL REFERENCES books(id) ON DELETE CASCADE,
            section_index INTEGER NOT NULL,
            title TEXT NOT NULL, words INTEGER NOT NULL, source TEXT NOT NULL,
            PRIMARY KEY(book_id, section_index)
        );
        ''')
        # Existing installations migrate in place; never drop their queued jobs.
        cols = {r['name'] for r in self.db.execute('PRAGMA table_info(jobs)').fetchall()}
        new_columns = {
            'total_chunks': 'INTEGER NOT NULL DEFAULT 0',
            'completed_chunks': 'INTEGER NOT NULL DEFAULT 0',
            'current_section': 'INTEGER',
            'current_chunk': 'INTEGER NOT NULL DEFAULT 0',
            'current_total': 'INTEGER NOT NULL DEFAULT 0',
            'updated': 'TEXT',
            'voice_path': 'TEXT',
            'pronunciation_required': 'INTEGER NOT NULL DEFAULT 0',
        }
        for field, column_sql in new_columns.items():
            if field not in cols:
                self.db.execute(f'ALTER TABLE jobs ADD COLUMN {field} {column_sql}')
        # A process cannot still be generating after a fresh launch.
        self.db.execute("UPDATE jobs SET status='interrupted', updated=? WHERE status IN ('running','stopping')", (now(),))
        self.db.commit()

    def changed(self):
        """Commit the transaction and notify the GUI that its optional mirror is dirty."""
        self.db.commit()
        if self.on_change is not None:
            self.on_change()

    def books(self):
        """List imported books newest first."""
        return self.db.execute('SELECT * FROM books ORDER BY id DESC').fetchall()

    def book(self, book_id):
        """Retrieve one book by persistent SQLite ID."""
        return self.db.execute('SELECT * FROM books WHERE id=?', (book_id,)).fetchone()

    def import_book(self, path, title, author, sections):
        """Upsert an EPUB and replace its stored section metadata."""
        p = str(Path(path).resolve())
        words = sum(len(s.text.split()) for s in sections)
        existing = self.db.execute('SELECT id FROM books WHERE path=?', (p,)).fetchone()
        if existing:
            book_id = existing['id']
            self.db.execute('UPDATE books SET title=?,author=?,sections=?,words=? WHERE id=?',
                            (title, author, len(sections), words, book_id))
        else:
            book_id = self.db.execute('INSERT INTO books(path,title,author,sections,words,added) VALUES(?,?,?,?,?,?)',
                                     (p, title, author, len(sections), words, now())).lastrowid
        self.db.execute('DELETE FROM book_sections WHERE book_id=?', (book_id,))
        self.db.executemany('''INSERT INTO book_sections(book_id,section_index,title,words,source)
                               VALUES (?,?,?,?,?)''',
                            [(book_id, s.index, s.title, len(s.text.split()), s.source) for s in sections])
        self.changed()
        return book_id

    def populate_missing_sections(self, book_id, sections):
        """Backfill chapter metadata when upgrading an existing V2 library."""
        stored = self.db.execute('SELECT COUNT(*) FROM book_sections WHERE book_id=?',
                                 (book_id,)).fetchone()[0]
        if stored == len(sections):
            return
        self.db.execute('DELETE FROM book_sections WHERE book_id=?', (book_id,))
        self.db.executemany('''INSERT INTO book_sections(book_id,section_index,title,words,source)
                               VALUES(?,?,?,?,?)''',
                            [(book_id, sec.index, sec.title, len(sec.text.split()), sec.source)
                             for sec in sections])
        self.changed()

    def delete_book(self, book_id):
        """Remove an unused book while refusing to orphan queue records."""
        if self.db.execute('SELECT COUNT(*) FROM jobs WHERE book_id=?', (book_id,)).fetchone()[0]:
            raise ValueError('This book has queued or completed jobs. Remove its jobs first.')
        self.db.execute('DELETE FROM books WHERE id=?', (book_id,))
        self.changed()

    def add_job(self, book_id, name, indices, output_name, voice_path=None, pronunciation_required=False):
        """Create a queued batch with selected indices and a fixed narrator path."""
        new_id = self.db.execute('''INSERT INTO jobs(book_id,name,indices,output_name,status,added,updated,voice_path,pronunciation_required)
                                    VALUES(?,?,?,?,?,?,?,?,?)''',
                                 (book_id, name, json.dumps(indices), output_name, 'queued', now(), now(),
                                  str(voice_path) if voice_path else None,
                                  1 if pronunciation_required else 0)).lastrowid
        self.changed()
        return new_id

    def set_job_voice(self, job_id, voice_path):
        """Change a narrator only before execution; reset stale progress counters."""
        job = self.job(job_id)
        if job is None or job['status'] != 'queued':
            raise ValueError('Narrator can be changed only before a queued job starts.')
        self.db.execute('''UPDATE jobs SET voice_path=?, total_chunks=0, completed_chunks=0,
                          current_section=NULL, current_chunk=0, current_total=0, updated=? WHERE id=?''',
                        (str(voice_path) if voice_path else None, now(), job_id))
        self.changed()

    def jobs(self):
        """Return queue rows joined to their book titles and source paths."""
        return self.db.execute('''SELECT jobs.*, books.title AS book_title, books.path AS book_path
                                  FROM jobs JOIN books ON jobs.book_id=books.id
                                  ORDER BY jobs.id DESC''').fetchall()

    def job(self, job_id):
        """Look up one queue row together with its book details."""
        return self.db.execute('''SELECT jobs.*, books.title AS book_title, books.path AS book_path
                                  FROM jobs JOIN books ON jobs.book_id=books.id WHERE jobs.id=?''', (job_id,)).fetchone()

    def next_job(self):
        """Return the oldest queued job for FIFO processing."""
        return self.db.execute("SELECT id FROM jobs WHERE status='queued' ORDER BY id LIMIT 1").fetchone()

    def update_job(self, job_id, status, output_path=None, error=''):
        """Persist status, finished time, last error, and export location."""
        self.db.execute('''UPDATE jobs SET status=?, output_path=COALESCE(?,output_path),
                           error=?, finished=?, updated=? WHERE id=?''',
                        (status, output_path, error, now() if status == 'complete' else None, now(), job_id))
        self.changed()

    def update_progress(self, job_id, total_chunks, completed_chunks,
                        current_section=None, current_chunk=0, current_total=0):
        """Store a checkpoint of chapter and overall chunk progress."""
        self.db.execute('''UPDATE jobs SET total_chunks=?,completed_chunks=?, current_section=?,
                           current_chunk=?,current_total=?,updated=? WHERE id=?''',
                        (total_chunks, completed_chunks, current_section, current_chunk, current_total, now(), job_id))
        self.changed()

    def remove_job(self, job_id):
        """Delete one job record without touching generated audio files."""
        self.db.execute('DELETE FROM jobs WHERE id=?', (job_id,))
        self.changed()

    def claimed_sections(self, book_id):
        """Find sections already covered by queued, running, or complete jobs."""
        rows = self.db.execute("SELECT indices FROM jobs WHERE book_id=? AND status IN ('queued','running','complete')",
                               (book_id,)).fetchall()
        return {int(i) for row in rows for i in json.loads(row['indices'])}

    def snapshot(self):
        """Consistent, small metadata snapshot; no EPUB or audio data copied."""
        # All three tables must describe the same point in time for mirror sync.
        self.db.execute('BEGIN')
        try:
            tables = ('books', 'book_sections', 'jobs')
            result = {table: [dict(r) for r in self.db.execute(f'SELECT * FROM {table}').fetchall()]
                      for table in tables}
            self.db.commit()
            return result
        except Exception:
            self.db.rollback()
            raise

    def close(self):
        """Release the SQLite connection."""
        self.db.close()


class MySQLMirror:
    """Transactional one-way mirror of the local SQLite catalogue into MySQL.

    MySQL outages never interrupt generation. This uses the separate, dedicated
    audiobookai schema; don't make manual edits to mirrored tables.
    """

    def __init__(self, cfg, password):
        """Open SQLite, create missing tables and columns, and mark abandoned jobs interrupted."""
        self.cfg = dict(cfg)
        self.password = password
        self.connection = None
        self.last_sync = None
        self.last_error = None
        self.dirty = True

    def connect(self):
        """Open a MySQL connection and create missing tables for the mirror."""
        try:
            import mysql.connector
        except ImportError as exc:
            raise RuntimeError('MySQL driver missing. Install: uv pip install mysql-connector-python') from exc
        self.disconnect()
        # Explicit pure-Python mode reduces dependence on the connector C extension.
        self.connection = mysql.connector.connect(
            host=self.cfg['host'], port=int(self.cfg['port']),
            user=self.cfg['user'], password=self.password,
            database=self.cfg['database'], connection_timeout=4,
            charset='utf8mb4', use_pure=True,
            autocommit=False)
        try:
            cur = self.connection.cursor()
            ddl = [
                '''CREATE TABLE IF NOT EXISTS books (
                    id BIGINT PRIMARY KEY, path LONGTEXT NOT NULL,
                    path_sha256 CHAR(64) NOT NULL UNIQUE,
                    title TEXT NOT NULL, author TEXT NOT NULL,
                    sections INT NOT NULL, words BIGINT NOT NULL,
                    added DATETIME NOT NULL
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4''',
                '''CREATE TABLE IF NOT EXISTS book_sections (
                    book_id BIGINT NOT NULL, section_index INT NOT NULL,
                    title TEXT NOT NULL, words BIGINT NOT NULL, source TEXT NOT NULL,
                    PRIMARY KEY(book_id,section_index),
                    FOREIGN KEY (book_id) REFERENCES books(id) ON DELETE CASCADE
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4''',
                '''CREATE TABLE IF NOT EXISTS jobs (
                    id BIGINT PRIMARY KEY, book_id BIGINT NOT NULL,
                    name TEXT NOT NULL, indices_json JSON NOT NULL,
                    output_name TEXT NOT NULL, status VARCHAR(30) NOT NULL,
                    added DATETIME NOT NULL, finished DATETIME NULL,
                    output_path LONGTEXT NULL, error TEXT,
                    total_chunks BIGINT NOT NULL DEFAULT 0,
                    completed_chunks BIGINT NOT NULL DEFAULT 0,
                    current_section INT NULL,
                    current_chunk INT NOT NULL DEFAULT 0,
                    current_total INT NOT NULL DEFAULT 0,
                    updated DATETIME NULL,
                    voice_path LONGTEXT NULL,
                    INDEX idx_jobs_status (status,id),
                    FOREIGN KEY(book_id) REFERENCES books(id) ON DELETE CASCADE
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4''',
                '''CREATE TABLE IF NOT EXISTS sync_state (
                    source_name VARCHAR(100) PRIMARY KEY,
                    synced_at DATETIME NOT NULL,
                    book_count INT NOT NULL, job_count INT NOT NULL
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4'''
            ]
            for statement in ddl:
                cur.execute(statement)
            # Existing MySQL installs require a non-destructive column migration.
            cur.execute("SHOW COLUMNS FROM jobs LIKE 'voice_path'")
            if cur.fetchone() is None:
                cur.execute('ALTER TABLE jobs ADD COLUMN voice_path LONGTEXT NULL')
            self.connection.commit()
            cur.close()
            self.last_error = None
        except Exception:
            self.disconnect()
            raise
        return True

    def connected(self):
        """Report whether the active MySQL connection can be used."""
        try:
            return bool(self.connection and self.connection.is_connected())
        except Exception:
            return False

    def sync(self, store):
        """Replace mirror metadata with a consistent SQLite snapshot in a transaction."""
        if not self.connected():
            self.connect()
        snapshot = store.snapshot()
        cur = self.connection.cursor()
        try:
            # Replace all rows in one transaction: readers see either the old
            # complete snapshot or the new one. All tables belong only to this
            # local application's MySQL mirror.
            cur.execute('DELETE FROM jobs')
            cur.execute('DELETE FROM book_sections')
            cur.execute('DELETE FROM books')
            books = snapshot['books']
            sections = snapshot['book_sections']
            jobs = snapshot['jobs']
            if books:
                cur.executemany('''INSERT INTO books
                    (id,path,path_sha256,title,author,sections,words,added)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s)''',
                    [(b['id'], b['path'], hashlib.sha256(b['path'].encode('utf-8')).hexdigest(),
                      b['title'], b['author'], b['sections'], b['words'], b['added']) for b in books])
            if sections:
                cur.executemany('''INSERT INTO book_sections
                    (book_id,section_index,title,words,source)
                    VALUES (%s,%s,%s,%s,%s)''',
                    [(s['book_id'], s['section_index'], s['title'], s['words'], s['source']) for s in sections])
            if jobs:
                cur.executemany('''INSERT INTO jobs
                    (id,book_id,name,indices_json,output_name,status,added,finished,
                     output_path,error,total_chunks,completed_chunks,current_section,
                     current_chunk,current_total,updated,voice_path)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)''',
                    [(j['id'],j['book_id'],j['name'],j['indices'],j['output_name'],j['status'],
                      j['added'],j['finished'],j['output_path'],j['error'],j['total_chunks'],
                      j['completed_chunks'],j['current_section'],j['current_chunk'],
                      j['current_total'],j['updated'],j.get('voice_path')) for j in jobs])
            cur.execute('''INSERT INTO sync_state(source_name,synced_at,book_count,job_count)
                           VALUES (%s, %s, %s, %s)
                           ON DUPLICATE KEY UPDATE synced_at=VALUES(synced_at),
                           book_count=VALUES(book_count),job_count=VALUES(job_count)''',
                        ('audiobookai-studio',now(),len(books),len(jobs)))
            self.connection.commit()
            self.dirty = False
            self.last_error = None
            self.last_sync = now()
            return len(books), len(jobs)
        except Exception as exc:
            try:
                self.connection.rollback()
            except Exception:
                pass
            self.dirty = True
            self.last_error = str(exc)
            raise
        finally:
            cur.close()

    def disconnect(self):
        """Close the MySQL connection and clear mirror state."""
        if self.connection is not None:
            try:
                self.connection.close()
            except Exception:
                pass
        self.connection = None
