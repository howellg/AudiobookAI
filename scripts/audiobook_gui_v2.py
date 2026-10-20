#!/usr/bin/env python3
"""AudiobookAI Studio V2 — local desktop frontend for V7 Smooth Chatterbox.

Place this file, epub_to_audiobook_v7_studio.py and
 test_five_minutes_v7_smooth.py in ~/AudiobookAI/scripts/.

Dependencies (in the existing Chatterbox venv):
    uv pip install PySide6 ebooklib beautifulsoup4 lxml mysql-connector-python
    uv pip install keyring  # optional: secure KDE Wallet credential storage

SQLite stays the crash-safe source of truth. A dedicated MySQL schema is a
queryable mirror, so MySQL outages do not interrupt narration or the queue.
"""

# MAINTAINER NOTE: Current PySide6 desktop application for importing EPUBs, queueing narration, reviewing audio, and managing optional sync.
from __future__ import annotations

import importlib.util
import json
import re
import sys
import time
from collections import deque
from pathlib import Path

from PySide6.QtCore import Qt, QProcess, QTimer, QUrl, QSettings
from PySide6.QtGui import QDesktopServices, QFont, QKeySequence, QShortcut, QPalette
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QComboBox, QFrame, QGridLayout, QHBoxLayout,
    QHeaderView, QLabel, QMainWindow, QMessageBox, QFileDialog, QInputDialog, QPlainTextEdit,
    QProgressBar, QPushButton, QSlider, QSpinBox, QSplitter, QStackedWidget, QTableWidget,
    QTableWidgetItem, QVBoxLayout, QWidget, QAbstractItemView, QLineEdit, QScrollArea,
)

from audiobookai_storage import (Store, read_config, write_config,
                                  lookup_password, save_password)
from audiobookai_mysql_proxy import MySQLMirror
from audiobookai_convenience import (parse_section_ranges, job_category,
                                     estimate_audio_duration, part_export_basename)
from audiobookai_voices import (DEFAULT_VOICE, VOICE_DIR, SNAPSHOT_DIR, voice_choices,
                                import_voice, freeze_voice, voice_name)
from audiobookai_production import cache_summary, remove_raw_cache
from audiobookai_editor import ChunkEditor
from audiobookai_fullbook import (collect_export_groups, verify_export_group,
                                  export_coverage, export_coverage_message,
                                  compact_section_ranges)
from audiobookai_archive import edition_info, save_edition, audio_duration
from audiobookai_readalong import read_chapter, chunk_at, clamp_resume_ms
from audiobookai_gui_throttle import (progress_snapshot, routine_speech_log,
                                      chunk_source_log, BufferedGenerationLog)
from audiobookai_headless import headless_runner_active
from audiobookai_reliability import (
    append_diagnostic, check_resume_job, create_metadata_backup,
    diagnostic_hint, job_log_path, latest_backup, verify_metadata_backup,
)
from audiobookai_quality import quality_preflight, EVENT_PREFIX as QC_EVENT_PREFIX
from audiobookai_pronunciation import (
    scoped_rules, effective_rules, save_rule, delete_rule, apply_rules,
    snapshot_for_job, snapshot_path_for_job,
)

SCRIPTS = Path(__file__).resolve().parent
ROOT = SCRIPTS.parent
CONVERTER_FILE = SCRIPTS / 'epub_to_audiobook_v7_studio.py'
DB_FILE = ROOT / 'output' / 'audiobookai_studio.sqlite3'
STORY_RE = re.compile(r'^\s*(?:chapter|chap\.?|book|part)\s+(?:\d+|[IVXLCDM]+)(?:\b|[.:—–-])', re.I)
EVENT_PREFIX = 'AUDIOBOOKAI_EVENT '


def fmt_seconds(seconds):
    """Format a number of elapsed or estimated seconds for progress labels."""
    if seconds is None:
        return '—'
    secs = max(0, int(seconds))
    hours, rem = divmod(secs, 3600)
    mins, sec = divmod(rem, 60)
    return f'{hours}h {mins:02}m' if hours else f'{mins}m {sec:02}s'


def fmt_size(n):
    """Render a byte count in human-readable binary units."""
    n = float(n)
    for unit in ('B', 'KiB', 'MiB', 'GiB', 'TiB'):
        if n < 1024 or unit == 'TiB':
            return f'{n:.1f} {unit}'
        n /= 1024


def size_on_disk(folder):
    """Sum sizes of files below a folder for the storage display."""
    if not folder.is_dir():
        return 0
    return sum(p.stat().st_size for p in folder.rglob('*') if p.is_file())


def load_converter():
    """Load the current Studio EPUB converter without importing TTS at GUI startup."""
    if not CONVERTER_FILE.is_file():
        raise FileNotFoundError(f'Missing backend: {CONVERTER_FILE}')
    spec = importlib.util.spec_from_file_location('audiobookai_v7_studio_backend', CONVERTER_FILE)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def title_label(text, size=20):
    """Create a consistent bold page heading with the requested font size."""
    label = QLabel(text)
    font = QFont()
    font.setPointSize(size)
    font.setBold(True)
    label.setFont(font)
    return label


def mk_button(label, handler, tooltip=None):
    """Build a button and connect its action to a Qt signal."""
    btn = QPushButton(label)
    btn.clicked.connect(handler)
    if tooltip:
        btn.setToolTip(tooltip)
    return btn


def item(text, data=None, checked=None):
    """Build a read-only table cell, optionally with stored data or a checkbox."""
    it = QTableWidgetItem(str(text))
    if data is not None:
        it.setData(Qt.UserRole, data)
    if checked is not None:
        it.setFlags((it.flags() | Qt.ItemIsUserCheckable) & ~Qt.ItemIsEditable)
        it.setCheckState(Qt.Checked if checked else Qt.Unchecked)
    else:
        it.setFlags(it.flags() & ~Qt.ItemIsEditable)
    return it


class Studio(QMainWindow):
    """Main Qt window coordinating the local library, queue, subprocesses, and review UI."""
    def __init__(self):
        """Create the window, restore saved preferences, open SQLite, and wire the pages and timers."""
        super().__init__()
        self.setWindowTitle('AudiobookAI Studio — V2.8.8')
        self.resize(1330, 900)
        self.prefs = QSettings('AudiobookAI', 'Studio')
        # SQLite owns queue state; MySQL is only a separately synchronized copy.
        self.store = Store(DB_FILE)
        self.store.on_change = self.mark_mysql_dirty
        self._history_scanned = set()
        self.mysql_cfg = read_config()['mysql']
        self.mysql_password = lookup_password(self.mysql_cfg)
        self.mysql_mirror = None
        self.mysql_retry_after = 0.0
        self.mysql_sync_pending = False
        self.last_progress_save = 0.0
        self.last_progress_chunks = 0
        self.converter = None
        self.current_book_id = None
        self.sections = []
        self.book_cache = {}
        # The converter runs in another process; UI callbacks only consume its status.
        self.proc = None
        self.active_job_id = None
        self.stop_requested = False
        self.run_queue = False
        self.buffer = ''
        self.plan = {}
        self.done_chunks = {}
        self.total_chunks = 0
        self.chunk_durations = deque(maxlen=40)
        # Includes fast cache hits, unlike the legacy generated-only ETA sample.
        self.recent_chunk_durations = deque(maxlen=120)
        self.current_section = None
        self.current_chunk = 0
        self.current_total = 0
        self.started_at = None
        self.last_output_path = None
        self.exporting = False
        self.review_manifest = {}
        self.full_export_proc = None
        self.full_export_target = None
        self.archive_proc = None
        self.archive_output = None
        self.archive_groups = []
        self._progress_last_values = {}
        self.active_job_log = None
        self.diagnostic_lines = deque(maxlen=40)
        self.generation_logs = BufferedGenerationLog()
        self.log_detailed = self.prefs.value('detailed_generation_log', False, type=bool)
        self.backup_dir = ROOT / 'output' / 'metadata_backups'
        self.diagnostic_dir = ROOT / 'output' / 'diagnostics'

        main = QWidget()
        self.setCentralWidget(main)
        root = QHBoxLayout(main)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        sidebar = QFrame()
        sidebar.setObjectName('sidebar')
        sidebar.setFixedWidth(205)
        nav = QVBoxLayout(sidebar)
        nav.setContentsMargins(16, 23, 16, 18)
        nav.setSpacing(7)
        nav.addWidget(title_label('AudiobookAI', 16))
        version = QLabel('STUDIO  /  V2.8.8 · V7 SMOOTH')
        version.setObjectName('muted')
        nav.addWidget(version)
        nav.addSpacing(20)
        self.nav = []
        for i, name in enumerate(['Library', 'Generate', 'Queue', 'Review audio', 'Settings', 'Quality Control', 'Pronunciation', 'Archive', 'Read Along']):
            btn = QPushButton(name)
            btn.setObjectName('navButton')
            btn.setCheckable(True)
            btn.clicked.connect(lambda _checked=False, index=i: self.change_page(index))
            nav.addWidget(btn)
            self.nav.append(btn)
        nav.addStretch(1)
        self.backend_status = QLabel('Engine: V7 Smooth')
        self.backend_status.setObjectName('muted')
        self.backend_status.setWordWrap(True)
        nav.addWidget(self.backend_status)
        self.mysql_sidebar_status = QLabel('MySQL: disabled')
        self.mysql_sidebar_status.setObjectName('muted')
        self.mysql_sidebar_status.setWordWrap(True)
        nav.addWidget(self.mysql_sidebar_status)
        root.addWidget(sidebar)

        self.stack = QStackedWidget()
        root.addWidget(self.stack, 1)
        self.make_library_page()
        self.make_generate_page()
        self.make_queue_page()
        self.make_review_page()
        self.make_settings_page()
        self.make_quality_page()
        self.make_pronunciation_page()
        self.make_archive_page()
        self.make_readalong_page()
        self.statusBar().showMessage('Ready — existing V6 recordings and cache are untouched.')
        self.change_page(0)
        saved_book_id = self.prefs.value('last_book_id')
        self.current_book_id = int(saved_book_id) if saved_book_id is not None else None
        self.refresh_books()
        self.refresh_jobs()
        last_page = self.prefs.value('last_page', 0, type=int)
        self.change_page(last_page if 0 <= last_page < len(self.nav) else 0)
        saved_geometry = self.prefs.value('geometry')
        if saved_geometry is not None:
            self.restoreGeometry(saved_geometry)
        self.setAcceptDrops(True)
        self.shortcuts = []
        for key, callback in [('Ctrl+O', self.import_dialog),
                              ('Ctrl+F', self.focus_search),
                              ('Ctrl+Return', self.queue_from_shortcut)]:
            shortcut = QShortcut(QKeySequence(key), self)
            shortcut.activated.connect(callback)
            self.shortcuts.append(shortcut)
        for i in range(len(self.nav)):
            shortcut = QShortcut(QKeySequence(f'Alt+{i+1}'), self)
            shortcut.activated.connect(lambda idx=i: self.change_page(idx))
            self.shortcuts.append(shortcut)
        # A single 500ms timer refreshes elapsed time and batch-renders logs.
        # It runs only during conversion; unchanged progress labels are skipped.
        self.elapsed_timer = QTimer(self)
        self.elapsed_timer.setInterval(500)
        self.elapsed_timer.timeout.connect(self.tick)
        self.elapsed_timer.timeout.connect(self.flush_generation_logs)
        self.mirror_timer = QTimer(self)
        self.mirror_timer.setSingleShot(True)
        self.mirror_timer.timeout.connect(self.maybe_sync_mysql)
        self.reconnect_timer = QTimer(self)
        self.reconnect_timer.timeout.connect(self.maybe_sync_mysql)
        self.reconnect_timer.start(60000)
        QTimer.singleShot(500, self.connect_saved_mysql)
        self.backup_timer = QTimer(self)
        self.backup_timer.timeout.connect(self.maybe_auto_metadata_backup)
        self.backup_timer.start(30 * 60 * 1000)
        QTimer.singleShot(3500, self.maybe_auto_metadata_backup)

    def page_base(self, heading, intro, scroll=False):
        """Create a page header and layout, optionally using a scrolling container."""
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(23, 18, 23, 21)
        layout.setSpacing(12)
        layout.addWidget(title_label(heading))
        sub = QLabel(intro)
        sub.setWordWrap(True)
        sub.setObjectName('muted')
        layout.addWidget(sub)
        if scroll:
            area = QScrollArea()
            area.setWidgetResizable(True)
            area.setFrameShape(QFrame.NoFrame)
            area.setWidget(page)
            self.stack.addWidget(area)
        else:
            self.stack.addWidget(page)
        return layout

    def change_page(self, index):
        """Select the visible page, update sidebar selection, and remember navigation state."""
        self.stack.setCurrentIndex(index)
        self.prefs.setValue('last_page', index)
        for i, button in enumerate(self.nav):
            button.setChecked(i == index)
        if index == 3:
            self.refresh_review()
        if index == 4:
            self.refresh_settings()
        if index == 5 and self.quality_proc is None:
            self.quality_book_changed()
        if index == 6:
            self.pronunciation_refresh_books()
        if index == 7 and self.archive_proc is None:
            self.archive_refresh_books()
        if index == 8:
            self.reader_initialize_audio()
            self.reader_refresh_books()
        elif hasattr(self, 'reader_audio') and self.reader_audio is not None:
            self.reader_save_progress()
            self.reader_audio.pause()

    def make_library_page(self):
        """Build the imported-books table and library actions."""
        l = self.page_base('Your library', 'Import DRM-free EPUB books once. Your job history stays available across restarts.')
        bar = QHBoxLayout()
        bar.addWidget(mk_button('＋ Import EPUB', self.import_dialog))
        bar.addWidget(mk_button('Open in Generate', self.generate_selected_book))
        bar.addWidget(mk_button('Remove from library', self.remove_selected_book))
        bar.addStretch()
        l.addLayout(bar)
        self.library_search = QLineEdit()
        self.library_search.setClearButtonEnabled(True)
        self.library_search.setPlaceholderText('Search books by title or author…  (Ctrl+F)')
        self.library_search.textChanged.connect(self.filter_library)
        l.addWidget(self.library_search)
        self.library_table = QTableWidget(0, 5)
        self.library_table.setHorizontalHeaderLabels(['Book', 'Author', 'Sections', 'Words', 'Imported'])
        self.setup_table(self.library_table)
        self.library_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
        self.library_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.Stretch)
        self.library_table.doubleClicked.connect(self.generate_selected_book)
        l.addWidget(self.library_table, 1)
        self.library_foot = QLabel('')
        self.library_foot.setObjectName('muted')
        l.addWidget(self.library_foot)

    def reload_voices(self, preferred=None):
        """Refresh narrator choices and retain the requested selection where possible."""
        if not hasattr(self, 'voice_combo'):
            return
        old = str(preferred) if preferred else self.voice_combo.currentData()
        self.voice_combo.blockSignals(True)
        self.voice_combo.clear()
        self.voice_combo.addItem('British narrator (default)', None)
        for path in voice_choices():
            if path == DEFAULT_VOICE.resolve():
                continue
            self.voice_combo.addItem(path.stem.replace('_', ' '), str(path))
        if old:
            index = self.voice_combo.findData(str(old))
            if index >= 0:
                self.voice_combo.setCurrentIndex(index)
        self.voice_combo.blockSignals(False)
        self.voice_changed()

    def selected_narrator(self):
        """Return the WAV currently chosen in the Generate view."""
        selected = self.voice_combo.currentData()
        return Path(selected) if selected else DEFAULT_VOICE

    def voice_changed(self, *_):
        """Remember the narrator preference for the currently selected book."""
        if not hasattr(self, 'voice_hint'):
            return
        path = self.selected_narrator()
        if self.current_book_id is not None:
            self.prefs.setValue(f'voice_for_book/{self.current_book_id}',
                                str(path) if path != DEFAULT_VOICE else '')
        self.voice_hint.setText(f'New queued parts will use: {path.name}. '
                                'Previously queued and running parts are unaffected. '
                                '▶ Hear sample plays the reference WAV, not synthesized narration.')

    def restore_voice_for_book(self, book_id):
        """Load the book-specific narrator preference into the selector."""
        saved = self.prefs.value(f'voice_for_book/{book_id}', '')
        index = self.voice_combo.findData(str(saved)) if saved else 0
        self.voice_combo.blockSignals(True)
        self.voice_combo.setCurrentIndex(index if index >= 0 else 0)
        self.voice_combo.blockSignals(False)
        self.voice_changed()

    def import_narrator(self):
        """Prompt for a WAV and copy a validated reference into the local voice library."""
        selection, _ = QFileDialog.getOpenFileName(self, 'Choose narrator reference WAV',
                                                   str(Path.home()), 'WAV audio (*.wav *.WAV)')
        if not selection:
            return
        try:
            imported = import_voice(Path(selection))
            self.reload_voices(imported)
            self.statusBar().showMessage(f'Imported narrator WAV: {imported.name}')
        except Exception as exc:
            self.error('Narrator WAV import failed', exc)

    def play_narrator(self):
        """Open the chosen narrator sample in the desktop audio player."""
        voice = self.selected_narrator()
        if not voice.is_file():
            self.error('Narrator sample missing', f'File not found: {voice}')
            return
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(voice)))

    def open_voices_folder(self):
        """Show the local narrator library in the file manager."""
        VOICE_DIR.mkdir(parents=True, exist_ok=True)
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(VOICE_DIR)))

    def change_queued_narrator(self):
        """Apply a narrator to an unstarted job; reject changes to active jobs."""
        job_id = self.selected_job_id()
        if job_id is None:
            QMessageBox.information(self, 'Select a part', 'Select a queued part in the queue first.')
            return
        job = self.store.job(job_id)
        if job['status'] != 'queued':
            QMessageBox.information(self, 'Part already started',
                'Only jobs that have never started can switch narrators. '
                'Choose another voice on Generate and queue a new part instead.')
            return
        try:
            fixed_voice = freeze_voice(self.selected_narrator())
            self.store.set_job_voice(job_id, fixed_voice)
            self.refresh_jobs()
            self.statusBar().showMessage(f"Part #{job_id} now uses {voice_name(fixed_voice)}.")
        except Exception as exc:
            self.error('Unable to change queued narrator', exc)

    def make_generate_page(self):
        """Build chapter selection, narrator controls, and batch queue options."""
        l = self.page_base('Prepare a conversion', 'Select story chapters or EPUB sections. Export them as one M4B or as separate numbered parts.')
        row = QHBoxLayout()
        row.addWidget(QLabel('Book:'))
        self.gen_book = QComboBox()
        self.gen_book.currentIndexChanged.connect(self.book_combo_changed)
        row.addWidget(self.gen_book, 1)
        row.addWidget(mk_button('Import EPUB…', self.import_dialog))
        l.addLayout(row)
        self.gen_details = QLabel('Import a book to begin.')
        self.gen_details.setObjectName('muted')
        l.addWidget(self.gen_details)
        self.gen_saved_parts = QLabel('Completed parts: none yet')
        self.gen_saved_parts.setObjectName('muted')
        self.gen_saved_parts.setWordWrap(True)
        l.addWidget(self.gen_saved_parts)
        narrator = QHBoxLayout()
        narrator.addWidget(QLabel('Narrator WAV:'))
        self.voice_combo = QComboBox()
        self.voice_combo.setMinimumWidth(220)
        self.voice_combo.setToolTip('The narrator selected here is saved with each new queue part.')
        self.voice_combo.currentIndexChanged.connect(self.voice_changed)
        narrator.addWidget(self.voice_combo, 1)
        narrator.addWidget(mk_button('＋ Import WAV…', self.import_narrator))
        narrator.addWidget(mk_button('▶ Hear sample', self.play_narrator))
        narrator.addWidget(mk_button('Open voices folder', self.open_voices_folder))
        l.addLayout(narrator)
        self.voice_hint = QLabel('Choose a narrator for new jobs. Existing queued jobs keep their own voice.')
        self.voice_hint.setObjectName('muted')
        self.voice_hint.setWordWrap(True)
        l.addWidget(self.voice_hint)
        self.reload_voices()
        self.section_table = QTableWidget(0, 3)
        self.section_table.setHorizontalHeaderLabels(['✓ / #', 'Chapter or EPUB section', 'Words'])
        self.setup_table(self.section_table)
        self.section_table.setSelectionMode(QAbstractItemView.NoSelection)
        self.section_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.Stretch)
        self.section_table.setColumnWidth(0, 100)
        self.section_table.setColumnWidth(2, 110)
        self.section_table.itemChanged.connect(self.update_selected_count)
        l.addWidget(self.section_table, 1)
        self.chapter_search = QLineEdit()
        self.chapter_search.setClearButtonEnabled(True)
        self.chapter_search.setPlaceholderText('Filter chapters by title or section number…')
        self.chapter_search.textChanged.connect(self.filter_chapters)
        l.addWidget(self.chapter_search)
        actions = QHBoxLayout()
        actions.addWidget(mk_button('Story chapters', self.select_story))
        actions.addWidget(mk_button('All', lambda: self.set_checks(lambda _n: True)))
        actions.addWidget(mk_button('None', lambda: self.set_checks(lambda _n: False)))
        actions.addWidget(mk_button('Next unqueued', self.select_next_unqueued,
                                    'Select the next unqueued story chapters up to the batch size'))
        actions.addWidget(mk_button('Select visible', self.select_visible,
                                    'Select chapters currently shown by the title filter'))
        actions.addStretch()
        l.addLayout(actions)
        ranges = QHBoxLayout()
        ranges.addWidget(QLabel('Select EPUB #:'))
        self.range_input = QLineEdit()
        self.range_input.setPlaceholderText('e.g. 8-22, 25, 28-30')
        self.range_input.returnPressed.connect(self.select_range)
        ranges.addWidget(self.range_input, 1)
        ranges.addWidget(mk_button('Apply range', self.select_range,
            'Replace the selection with these EPUB section numbers, not literary chapter numbers'))
        l.addLayout(ranges)
        self.gen_selected = QLabel('0 selected')
        self.gen_selected.setObjectName('muted')
        l.addWidget(self.gen_selected)
        opts = QHBoxLayout()
        opts.addWidget(QLabel('Sections per part:'))
        self.batch_size = QSpinBox()
        self.batch_size.setRange(1, 100)
        self.batch_size.setValue(self.prefs.value('batch_size', 15, type=int))
        self.batch_size.valueChanged.connect(lambda value: self.prefs.setValue('batch_size', value))
        opts.addWidget(self.batch_size)
        self.split_parts = QCheckBox('Create separate M4B parts')
        self.split_parts.setChecked(self.prefs.value('split_parts', True, type=bool))
        self.split_parts.toggled.connect(lambda value: self.prefs.setValue('split_parts', value))
        opts.addWidget(self.split_parts)
        self.autorun = QCheckBox('Start queue immediately')
        self.autorun.setChecked(self.prefs.value('autorun', True, type=bool))
        self.autorun.toggled.connect(lambda value: self.prefs.setValue('autorun', value))
        opts.addWidget(self.autorun)
        opts.addStretch()
        l.addLayout(opts)
        lower = QHBoxLayout()
        lower.addWidget(mk_button('＋ Add selection to queue', self.queue_selection))
        lower.addWidget(mk_button('View queue →', lambda: self.change_page(2)))
        lower.addStretch()
        l.addLayout(lower)

    def make_queue_page(self):
        """Build job controls, progress readouts, log viewer, and job table."""
        l = self.page_base('Conversion queue', 'Jobs and progress persist locally and sync to MySQL when connected. Interrupted jobs reuse V7 chunk caches.')
        bar = QHBoxLayout()
        bar.addWidget(mk_button('▶ Start queue', self.start_queue))
        bar.addWidget(mk_button('Run selected', self.run_selected_job,
            'Generate just the selected queued part, then pause the queue'))
        bar.addWidget(mk_button('Pause after part', self.pause_after_part,
            'Finish the active M4B part without starting the next one'))
        bar.addWidget(mk_button('■ Stop safely', self.stop_job))
        bar.addWidget(mk_button('↻ Retry selected', self.retry_selected))
        bar.addWidget(mk_button('Recover interrupted', self.recover_interrupted,
            'Requeue interrupted parts only when their source files and snapshots still exist'))
        bar.addWidget(mk_button('Remove selected', self.remove_selected_job))
        bar.addStretch()
        l.addLayout(bar)
        additional = QHBoxLayout()
        additional.addWidget(mk_button('Use chosen narrator', self.change_queued_narrator,
            'Apply the voice selected on Generate to the selected QUEUED part'))
        additional.addWidget(mk_button('▶ Play M4B', self.open_selected_output,
            'Play the selected completed M4B in your default audiobook player'))
        additional.addWidget(mk_button('Open folder', self.open_selected_output_folder))
        additional.addWidget(mk_button('Copy path', self.copy_selected_output_path))
        additional.addWidget(mk_button('View job log', self.open_selected_diagnostic))
        additional.addWidget(mk_button('Clear finished jobs', self.clear_finished_jobs,
            'Remove verified completed entries from queue; keep files and permanent history'))
        additional.addStretch()
        l.addLayout(additional)
        filters = QHBoxLayout()
        filters.addWidget(QLabel('Show:'))
        self.queue_filter = QComboBox()
        self.queue_filter.addItems(['All jobs', 'Pending / active', 'Completed', 'Needs attention'])
        self.queue_filter.currentTextChanged.connect(self.filter_queue)
        filters.addWidget(self.queue_filter)
        self.queue_search = QLineEdit()
        self.queue_search.setPlaceholderText('Filter by book, job name or status…')
        self.queue_search.setClearButtonEnabled(True)
        self.queue_search.textChanged.connect(self.filter_queue)
        filters.addWidget(self.queue_search, 1)
        self.queue_count = QLabel('')
        self.queue_count.setObjectName('muted')
        filters.addWidget(self.queue_count)
        l.addLayout(filters)
        self.job_table = QTableWidget(0, 7)
        self.job_table.setHorizontalHeaderLabels(['Job / part', 'Book', 'Sections', 'Narrator', 'Status', 'Progress', 'Created'])
        self.setup_table(self.job_table)
        self.job_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
        self.job_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.Stretch)
        self.job_table.setColumnWidth(3, 165)
        self.job_table.setMinimumHeight(145)
        self.job_table.setMaximumHeight(255)
        self.job_table.doubleClicked.connect(self.open_selected_output)
        l.addWidget(self.job_table)
        self.active_title = QLabel('No active generation')
        self.active_title.setFont(QFont('', 11, QFont.Bold))
        l.addWidget(self.active_title)
        self.chapter_text = QLabel('Current chapter: —')
        l.addWidget(self.chapter_text)
        self.chapter_progress = QProgressBar()
        self.chapter_progress.setRange(0, 1000)
        l.addWidget(self.chapter_progress)
        self.overall_text = QLabel('Overall conversion: —')
        l.addWidget(self.overall_text)
        self.overall_progress = QProgressBar()
        self.overall_progress.setRange(0, 1000)
        l.addWidget(self.overall_progress)
        stats = QHBoxLayout()
        self.stat_elapsed = QLabel('Elapsed: —')
        self.stat_eta = QLabel('ETA: —')
        self.stat_chunks = QLabel('Chunks: —')
        stats.addWidget(self.stat_elapsed)
        stats.addWidget(self.stat_eta)
        stats.addWidget(self.stat_chunks)
        stats.addStretch()
        l.addLayout(stats)
        logbar = QHBoxLayout()
        logbar.addWidget(QLabel('Recent generation messages'))
        logbar.addStretch()
        self.log_mode_button = mk_button(
            'Compact view' if self.log_detailed else 'Detailed view',
            self.toggle_log_mode,
            'Switch between chapter/warning messages and full per-chunk output')
        logbar.addWidget(self.log_mode_button)
        logbar.addWidget(mk_button('Copy log', self.copy_log))
        logbar.addWidget(mk_button('Clear view', self.clear_log))
        l.addLayout(logbar)
        self.logs = QPlainTextEdit()
        self.logs.setReadOnly(True)
        self.logs.setMaximumBlockCount(1500)
        l.addWidget(self.logs, 1)

    def make_review_page(self):
        """Build finished-export playback and chapter review controls."""
        l = self.page_base('Review your audio', 'Review, regenerate or restore individual voice chunks, inspect audio, and rebuild M4Bs without re-running complete chapters.')
        row = QHBoxLayout()
        row.addWidget(QLabel('Book:'))
        self.review_book = QComboBox()
        self.review_book.currentIndexChanged.connect(self.refresh_review)
        row.addWidget(self.review_book, 1)
        l.addLayout(row)
        self.review_table = QTableWidget(0, 5)
        self.review_table.setHorizontalHeaderLabels(['EPUB #', 'Chapter', 'Narrator', 'Length', 'Audio'])
        self.setup_table(self.review_table)
        self.review_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.Stretch)
        l.addWidget(self.review_table, 1)
        buttons = QHBoxLayout()
        buttons.addWidget(mk_button('▶ Play chapter WAV', self.play_selected_chapter))
        buttons.addWidget(mk_button('✎ Edit spoken chunks…', self.open_chunk_editor))
        buttons.addWidget(mk_button('Open audio folder', self.open_current_folder))
        buttons.addStretch()
        l.addLayout(buttons)
        l.addWidget(title_label('Completed M4B parts', 14))
        self.export_table = QTableWidget(0, 3)
        self.export_table.setHorizontalHeaderLabels(['Part', 'Export status', 'Size'])
        self.setup_table(self.export_table)
        self.export_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
        self.export_table.setMaximumHeight(180)
        self.export_table.doubleClicked.connect(self.play_selected_export)
        l.addWidget(self.export_table)
        export_actions = QHBoxLayout()
        export_actions.addWidget(mk_button('▶ Play selected M4B', self.play_selected_export))
        export_actions.addWidget(mk_button('Open M4B folder', self.open_selected_export_folder))
        export_actions.addWidget(mk_button('Export complete M4B', self.export_complete_book,
            'Combine all finished part WAVs for one narrator into a single chaptered M4B; no GPU required'))
        export_actions.addStretch()
        l.addLayout(export_actions)
        note = QLabel('Narration preset: V7 Smooth — 18-word ceiling, 350-token cap, exaggeration 0.35. Narrators have isolated chapter audio and cached speech.')
        note.setWordWrap(True)
        note.setObjectName('muted')
        l.addWidget(note)

    def make_quality_page(self):
        """Create the read-only QC dashboard for existing chapter and chunk WAVs.

        The worker uses a separate Python process, keeping intensive WAV reads
        outside the Qt main thread and away from Chatterbox GPU allocations.
        """
        l = self.page_base('Quality Control',
            'Scan finished narration for missing audio, damaged WAVs, clipping, suspicious silence and short speech. '
            'The scan does not modify recordings or rerun the TTS model.')
        book_line = QHBoxLayout()
        book_line.addWidget(QLabel('Book:'))
        self.quality_book = QComboBox()
        self.quality_book.currentIndexChanged.connect(self.quality_book_changed)
        book_line.addWidget(self.quality_book, 2)
        book_line.addWidget(QLabel('Narrator:'))
        self.quality_narrator = QComboBox()
        self.quality_narrator.currentIndexChanged.connect(self.quality_load_report)
        book_line.addWidget(self.quality_narrator, 2)
        l.addLayout(book_line)
        self.quality_groups = []
        self.quality_proc = None
        self.quality_buffer = ''
        self.quality_target = None
        self.quality_report = None
        self.quality_visible_issues = []
        bar = QHBoxLayout()
        self.quality_deep = QCheckBox('Inspect available individual speech chunks (slower)')
        self.quality_deep.setToolTip('Checks the original/edited WAVs if they are still available. '
            'Removed raw caches are not treated as damaged finished narration.')
        bar.addWidget(self.quality_deep)
        bar.addStretch()
        self.quality_scan_button = mk_button('Scan audio', self.scan_quality)
        self.quality_stop_button = mk_button('Stop scan', self.stop_quality)
        self.quality_stop_button.setEnabled(False)
        bar.addWidget(self.quality_scan_button)
        bar.addWidget(self.quality_stop_button)
        l.addLayout(bar)
        self.quality_progress = QProgressBar()
        self.quality_progress.setRange(0, 100)
        self.quality_progress.setValue(0)
        l.addWidget(self.quality_progress)
        self.quality_status = QLabel('Select a completed book and narrator to scan its chapter WAVs.')
        self.quality_status.setWordWrap(True)
        l.addWidget(self.quality_status)
        results_bar = QHBoxLayout()
        results_bar.addWidget(title_label('Flagged audio', 14))
        results_bar.addStretch()
        results_bar.addWidget(QLabel('Show:'))
        self.quality_filter = QComboBox()
        self.quality_filter.addItems(['All issues', 'Errors only', 'Warnings only'])
        self.quality_filter.currentIndexChanged.connect(self.quality_show_issues)
        results_bar.addWidget(self.quality_filter)
        l.addLayout(results_bar)
        self.quality_table = QTableWidget(0, 5)
        self.quality_table.setHorizontalHeaderLabels(['Severity', 'EPUB #', 'Chunk', 'Issue', 'Time'])
        self.setup_table(self.quality_table)
        self.quality_table.horizontalHeader().setSectionResizeMode(3, QHeaderView.Stretch)
        l.addWidget(self.quality_table, 1)
        commands = QHBoxLayout()
        commands.addWidget(mk_button('Review selected chapter…', self.quality_open_editor))
        commands.addWidget(mk_button('Play flagged WAV', self.quality_play_audio))
        commands.addWidget(mk_button('Open report folder', self.quality_open_folder))
        commands.addStretch()
        l.addLayout(commands)
        note = QLabel('Warnings require listening, not automatic rejection. The chapter WAV is authoritative; '
            'raw speech caches may be safely deleted after a chapter is assembled. '
            'A fresh report is also summarized before complete-book export.')
        note.setWordWrap(True)
        note.setObjectName('muted')
        l.addWidget(note)

    def quality_refresh_books(self):
        """Mirror library selections without disturbing Generate/Review pickers."""
        selected = self.quality_book.currentData()
        self.quality_book.blockSignals(True)
        self.quality_book.clear()
        for book in self.store.books():
            self.quality_book.addItem(f"{book['title']} — {book['author']}", book['id'])
        position = self.quality_book.findData(selected if selected is not None else self.current_book_id)
        if position >= 0:
            self.quality_book.setCurrentIndex(position)
        self.quality_book.blockSignals(False)
        self.quality_book_changed()

    def quality_book_changed(self, *_):
        """Group a book's queue parts by exact narrator folder, not display name."""
        self.quality_narrator.blockSignals(True)
        self.quality_narrator.clear()
        self.quality_groups = []
        book_id = self.quality_book.currentData()
        if book_id is not None:
            try:
                self.converter = self.converter or load_converter()
                self.quality_groups = collect_export_groups(
                    self.store.jobs(), book_id, self.converter.book_folder)
                for n, group in enumerate(self.quality_groups):
                    self.quality_narrator.addItem(
                        f"{voice_name(group['voice_path'])} · {len(group['parts'])} part(s)", n)
            except Exception as exc:
                self.quality_status.setText(f'Cannot locate narrator audio: {exc}')
        self.quality_narrator.blockSignals(False)
        self.quality_load_report()

    def quality_selected_group(self):
        """Return the selected narrator's finished/unfinished queue group."""
        n = self.quality_narrator.currentData()
        return self.quality_groups[n] if isinstance(n, int) and 0 <= n < len(self.quality_groups) else None

    def quality_load_report(self, *_):
        """Display the previous scan when navigating back to Quality Control."""
        group = self.quality_selected_group()
        self.quality_report = None
        self.quality_progress.setValue(0)
        if not group:
            self.quality_status.setText('No queued audiobook parts were found for this book.')
            self.quality_show_issues()
            return
        path = group['folder'] / 'quality_reports' / 'summary.json'
        if path.is_file():
            try:
                self.quality_report = json.loads(path.read_text(encoding='utf-8'))
                r = self.quality_report
                m = r.get('metrics', {})
                self.quality_status.setText(
                    f"Last scan: {m.get('chapters_scanned', 0)} chapters, "
                    f"{m.get('chunks_scanned', 0)} chunks; "
                    f"{r.get('errors', 0)} errors, {r.get('warnings', 0)} warnings. "
                    f"{m.get('raw_chunks_unavailable', 0)} raw cache chunks unavailable (not errors).")
                self.quality_progress.setValue(100)
            except (ValueError, OSError, TypeError):
                self.quality_status.setText('Saved scan report is unreadable. Run a fresh scan.')
        else:
            self.quality_status.setText('No QC scan yet. Scan chapters first; select deep scan to check saved chunks.')
        self.quality_show_issues()

    def quality_show_issues(self, *_):
        """Render report issues in one Qt table refresh, not on each WAV read."""
        all_issues = (self.quality_report or {}).get('issues', [])
        selected = self.quality_filter.currentIndex()
        wanted = 'error' if selected == 1 else 'warning' if selected == 2 else None
        self.quality_visible_issues = [x for x in all_issues if wanted is None or x.get('severity') == wanted][:1000]
        self.quality_table.setRowCount(len(self.quality_visible_issues))
        for r, entry in enumerate(self.quality_visible_issues):
            value = (entry.get('severity', '').upper(), str(entry.get('chapter', '')),
                     str(entry.get('chunk') or '—'), entry.get('message', ''),
                     fmt_seconds(entry['seconds']) if entry.get('seconds') is not None else '—')
            for col, val in enumerate(value):
                cell = self.quality_table.item(r, col)
                if cell is None:
                    self.quality_table.setItem(r, col, item(val))
                elif cell.text() != val:
                    cell.setText(val)
        if len(all_issues) > 1000:
            self.quality_status.setText(self.quality_status.text() + ' Showing first 1,000 issues.')

    def scan_quality(self):
        """Start a separate read-only process; never scan large WAVs inside Qt."""
        if self.quality_proc is not None:
            return
        if self.proc is not None or self.full_export_proc is not None:
            QMessageBox.information(self, 'Audio is busy',
                'Finish the current conversion or complete-book export before scanning.')
            return
        editor = getattr(self, 'editor_dialog', None)
        if editor is not None and editor.isVisible() and editor.proc is not None:
            QMessageBox.information(self, 'Audio editor busy', 'Finish editing before scanning.')
            return
        group = self.quality_selected_group()
        if not group:
            QMessageBox.information(self, 'No audiobook parts', 'Choose a book with queued parts first.')
            return
        required = set()
        for job in group['parts']:
            if job['status'] == 'complete':
                required.update(json.loads(job['indices']))
        # A part in progress may already have assembled chapters. We scan them,
        # but do not flag yet-to-be-generated chapters as missing.
        present = set()
        manifest_path = group['folder'] / 'manifest.json'
        if manifest_path.is_file():
            try:
                manifest = json.loads(manifest_path.read_text(encoding='utf-8'))
                for key in manifest.get('chapters', {}):
                    if str(key).isdecimal() and (group['folder'] / 'chapters' / f'{int(key):04d}.wav').is_file():
                        present.add(int(key))
            except (ValueError, OSError, TypeError):
                pass
        chapters = sorted(required | present)
        if not chapters:
            QMessageBox.information(self, 'No chapter WAVs yet',
                'Generate at least one chapter before running a quality scan.')
            return
        self.quality_target = group['folder']
        self.quality_buffer = ''
        self.quality_proc = QProcess(self)
        proc = self.quality_proc
        proc.setProgram(sys.executable)
        args = ['-u', str(SCRIPTS / 'audiobookai_quality.py'),
                '--folder', str(group['folder']),
                '--chapters', ','.join(map(str, chapters)),
                '--required', ','.join(map(str, sorted(required)))]
        if self.quality_deep.isChecked():
            args.append('--deep')
        proc.setArguments(args)
        proc.setWorkingDirectory(str(ROOT))
        proc.setProcessChannelMode(QProcess.MergedChannels)
        proc.readyReadStandardOutput.connect(self.quality_process_output)
        proc.finished.connect(self.quality_finished)
        proc.errorOccurred.connect(self.quality_process_error)
        self.quality_scan_button.setEnabled(False)
        self.quality_stop_button.setEnabled(True)
        self.quality_progress.setValue(0)
        self.quality_status.setText(f'Scanning {len(chapters)} saved chapters; {len(group["unfinished"])} unfinished part(s) are not treated as failures.')
        proc.start()

    def quality_process_output(self):
        """Consume machine-readable worker progress without rebuilding issue rows."""
        if self.quality_proc is None:
            return
        self.quality_buffer += bytes(self.quality_proc.readAllStandardOutput()).decode('utf-8', 'replace')
        while '\n' in self.quality_buffer:
            line, self.quality_buffer = self.quality_buffer.split('\n', 1)
            if not line.startswith(QC_EVENT_PREFIX):
                if line.strip():
                    self.write_log('QC: ' + line.strip())
                continue
            try:
                event = json.loads(line[len(QC_EVENT_PREFIX):])
                if event.get('kind') == 'progress':
                    self.quality_progress.setValue(int(100 * event['current'] / max(1, event['total'])))
                    self.quality_status.setText(
                        f"Scanning EPUB section {event['chapter']} "
                        f"({event['current']}/{event['total']})… "
                        f"{event['issues']} potential issues found so far.")
            except (ValueError, KeyError, TypeError):
                self.write_log('QC: unrecognized progress event')

    def quality_process_error(self, _error):
        """Log failed subprocess launches; finished() releases the Qt wrapper."""
        if self.quality_proc is not None:
            self.quality_status.setText('Quality scan process failed: ' + self.quality_proc.errorString())
            if _error == QProcess.FailedToStart:
                p = self.quality_proc
                self.quality_proc = None
                self.quality_scan_button.setEnabled(True)
                self.quality_stop_button.setEnabled(False)
                p.deleteLater()

    def quality_finished(self, exit_code, exit_status):
        """Release the worker, then load one atomic report into the issues table."""
        p = self.quality_proc
        if p is None:
            return
        self.quality_process_output()
        self.quality_proc = None
        self.quality_scan_button.setEnabled(True)
        self.quality_stop_button.setEnabled(False)
        successful = exit_code == 0 and exit_status == QProcess.NormalExit
        p.deleteLater()
        if successful:
            self.quality_load_report()
            self.quality_progress.setValue(100)
            self.statusBar().showMessage('Quality Control scan complete.')
        else:
            self.quality_status.setText('QC scan stopped or failed; existing reports and WAVs remain unchanged.')
            self.statusBar().showMessage('Quality Control scan stopped or failed.')

    def stop_quality(self):
        """Stop only the independent scanner, leaving all audio and jobs intact."""
        if self.quality_proc is not None:
            self.quality_proc.kill()

    def quality_current_issue(self):
        """Find the selected issue independent of sorted/filtered table text."""
        row = self.quality_table.currentRow()
        if 0 <= row < len(self.quality_visible_issues):
            return self.quality_visible_issues[row]
        return None

    def quality_play_audio(self):
        """Play the flagged chunk WAV or assembled chapter using desktop audio."""
        problem = self.quality_current_issue()
        path = Path(problem.get('path') or '') if problem else None
        if path is not None and path.is_file() and path.suffix.lower() == '.wav':
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(path)))
        else:
            QMessageBox.information(self, 'Audio unavailable',
                'Choose an issue with an existing WAV file, or regenerate its missing chapter.')

    def quality_open_folder(self):
        """Open the narrator-local report directory without touching the WAVs."""
        group = self.quality_selected_group()
        if group:
            folder = group['folder'] / 'quality_reports'
            if folder.is_dir():
                QDesktopServices.openUrl(QUrl.fromLocalFile(str(folder)))
                return
        QMessageBox.information(self, 'No report yet', 'Run a Quality Control scan first.')

    def quality_open_editor(self):
        """Select the flagged chapter on Review Audio and open its chunk editor."""
        problem = self.quality_current_issue()
        group = self.quality_selected_group()
        book_id = self.quality_book.currentData()
        if not problem or not group or book_id is None:
            QMessageBox.information(self, 'Select an issue', 'Choose a flagged chapter first.')
            return
        self.review_book.setCurrentIndex(self.review_book.findData(book_id))
        self.change_page(3)
        target = (group['folder'] / 'chapters' / f"{int(problem['chapter']):04d}.wav").resolve()
        for r in range(self.review_table.rowCount()):
            cell = self.review_table.item(r, 0)
            if cell is not None and Path(str(cell.data(Qt.UserRole))).resolve() == target:
                self.review_table.selectRow(r)
                self.review_table.scrollToItem(cell)
                self.open_chunk_editor()
                return
        QMessageBox.information(self, 'Chapter not ready',
            'This chapter has no assembled WAV to edit yet. Rebuild the missing chapter first.')

    def make_pronunciation_page(self):
        """Edit global/book-level text hints for NEW queued audio only.

        Chatterbox consumes ordinary text rather than an IPA lexicon. A spoken
        hint is a phonetic respelling and does not guarantee model compliance.
        """
        layout = self.page_base('Pronunciation dictionary',
            'Teach future narration how to read names and unusual words. '
            'Changes apply to NEW queued parts only. Existing jobs, WAVs and M4Bs stay unchanged.')
        selection = QHBoxLayout()
        selection.addWidget(QLabel('Dictionary:'))
        self.pron_scope = QComboBox()
        self.pron_scope.addItem('Global (all books)', 'global')
        self.pron_scope.addItem('Specific book', 'book')
        self.pron_scope.currentIndexChanged.connect(self.pronunciation_refresh)
        selection.addWidget(self.pron_scope)
        self.pron_book = QComboBox()
        self.pron_book.currentIndexChanged.connect(self.pronunciation_refresh)
        selection.addWidget(self.pron_book, 2)
        layout.addLayout(selection)
        self.pron_table = QTableWidget(0, 2)
        self.pron_table.setHorizontalHeaderLabels(['Written in EPUB', 'Spoken text for Chatterbox'])
        self.setup_table(self.pron_table)
        self.pron_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
        self.pron_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.Stretch)
        self.pron_table.itemSelectionChanged.connect(self.pronunciation_select)
        layout.addWidget(self.pron_table, 1)
        fields = QHBoxLayout()
        self.pron_term = QLineEdit()
        self.pron_term.setPlaceholderText('e.g. Nynaeve')
        self.pron_spoken = QLineEdit()
        self.pron_spoken.setPlaceholderText('e.g. Nigh-neev')
        fields.addWidget(QLabel('Written:'))
        fields.addWidget(self.pron_term, 2)
        fields.addWidget(QLabel('Say:'))
        fields.addWidget(self.pron_spoken, 2)
        layout.addLayout(fields)
        actions = QHBoxLayout()
        actions.addWidget(mk_button('Add / update rule', self.pronunciation_save))
        actions.addWidget(mk_button('Delete selected', self.pronunciation_delete))
        actions.addWidget(mk_button('Clear fields', self.pronunciation_clear))
        actions.addStretch()
        layout.addLayout(actions)
        preview = QHBoxLayout()
        self.pron_example = QLineEdit()
        self.pron_example.setPlaceholderText('Preview a sentence without generating audio…')
        self.pron_example.textChanged.connect(self.pronunciation_preview)
        preview.addWidget(self.pron_example, 1)
        layout.addLayout(preview)
        self.pron_preview_label = QLabel('Pronunciation preview appears here.')
        self.pron_preview_label.setWordWrap(True)
        layout.addWidget(self.pron_preview_label)
        note = QLabel('Rules replace text only in Chatterbox input; the source EPUB is never edited. '
            'Book-specific terms override global ones. Queued parts freeze their rules; '
            'audio review/rebuild uses the saved rules from each chapter. '
            'Use sound-alike words, not IPA or SSML; actual pronunciation varies by model.')
        note.setObjectName('muted')
        note.setWordWrap(True)
        layout.addWidget(note)
        self.pronunciation_refresh_books()

    def pronunciation_scope_path(self):
        """Return the selected editable dictionary scope and EPUB path."""
        scope = self.pron_scope.currentData()
        book_id = self.pron_book.currentData()
        book = self.store.book(book_id) if book_id is not None else None
        return scope, book['path'] if book is not None else None

    def pronunciation_refresh_books(self):
        """Refresh the book picker independently from the library/queue selection."""
        selected = self.pron_book.currentData()
        self.pron_book.blockSignals(True)
        self.pron_book.clear()
        for book in self.store.books():
            self.pron_book.addItem(f"{book['title']} — {book['author']}", book['id'])
        index = self.pron_book.findData(selected if selected is not None else self.current_book_id)
        if index >= 0:
            self.pron_book.setCurrentIndex(index)
        self.pron_book.blockSignals(False)
        self.pronunciation_refresh()

    def pronunciation_refresh(self, *_):
        """Display only global OR current book rules without recreating other pages."""
        if not hasattr(self, 'pron_table'):
            return
        scope, path = self.pronunciation_scope_path()
        self.pron_book.setEnabled(scope == 'book')
        try:
            rules = sorted(scoped_rules(scope, path), key=lambda r: r['term'].casefold())
        except Exception as exc:
            self.statusBar().showMessage(f'Pronunciation dictionary could not be read: {exc}')
            rules = []
        table = self.pron_table
        table.blockSignals(True)
        table.setRowCount(len(rules))
        for row, rule in enumerate(rules):
            for column, value in enumerate((rule['term'], rule['spoken'])):
                existing = table.item(row, column)
                if existing is None:
                    table.setItem(row, column, item(value))
                elif existing.text() != value:
                    existing.setText(value)
        table.blockSignals(False)
        self.pronunciation_preview()

    def pronunciation_select(self):
        """Copy selected dictionary entry into editable fields."""
        row = self.pron_table.currentRow()
        if row >= 0:
            source = self.pron_table.item(row, 0)
            spoken = self.pron_table.item(row, 1)
            if source and spoken:
                self.pron_term.setText(source.text())
                self.pron_spoken.setText(spoken.text())

    def pronunciation_clear(self):
        """Clear entry fields without deleting anything from the dictionary."""
        self.pron_term.clear()
        self.pron_spoken.clear()
        self.pron_table.clearSelection()

    def pronunciation_save(self):
        """Save a single mapping for future jobs; existing snapshots remain frozen."""
        try:
            scope, path = self.pronunciation_scope_path()
            save_rule(scope, path, self.pron_term.text(), self.pron_spoken.text())
        except Exception as exc:
            self.error('Invalid pronunciation rule', exc)
            return
        self.pronunciation_refresh()
        self.statusBar().showMessage('Saved pronunciation rule for new queued parts.')

    def pronunciation_delete(self):
        """Remove the entry under the selected scope, not rules in other scopes."""
        row = self.pron_table.currentRow()
        entry = self.pron_table.item(row, 0) if row >= 0 else None
        if entry is None:
            return
        try:
            scope, path = self.pronunciation_scope_path()
            delete_rule(scope, path, entry.text())
        except Exception as exc:
            self.error('Unable to delete pronunciation rule', exc)
            return
        self.pronunciation_clear()
        self.pronunciation_refresh()
        self.statusBar().showMessage('Deleted dictionary rule for future queued parts.')

    def pronunciation_preview(self, *_):
        """Show a safe, text-only approximation of substitutions before synthesis."""
        example = self.pron_example.text()
        scope, path = self.pronunciation_scope_path()
        if not example:
            self.pron_preview_label.setText('Enter a sentence to preview spoken-text substitutions.')
            return
        try:
            if scope == 'global' or not path:
                rules = scoped_rules('global', None)
            else:
                rules = effective_rules(path)
            self.pron_preview_label.setText('Spoken text: ' + apply_rules(example, rules))
        except Exception as exc:
            self.pron_preview_label.setText(f'Preview unavailable: {exc}')

    def make_archive_page(self):
        """Build a single-purpose offline archive workflow for completed books.

        The UI never encodes audio itself. It verifies queue sections and
        launches an isolated worker to create a chapter-marked archival M4B.
        """
        l = self.page_base('Archive complete audiobooks',
            'Package finished parts as one M4B with chapter markers, cover art and library metadata. '
            'Export directly to an external drive without rerunning narration.', scroll=True)
        book_row = QHBoxLayout()
        book_row.addWidget(QLabel('Book:'))
        self.archive_book = QComboBox()
        self.archive_book.currentIndexChanged.connect(self.archive_book_changed)
        book_row.addWidget(self.archive_book, 1)
        book_row.addWidget(mk_button('Refresh', self.archive_refresh_books))
        l.addLayout(book_row)
        narrator_row = QHBoxLayout()
        narrator_row.addWidget(QLabel('Narrator / finished parts:'))
        self.archive_narrator = QComboBox()
        self.archive_narrator.currentIndexChanged.connect(self.archive_narrator_changed)
        narrator_row.addWidget(self.archive_narrator, 1)
        l.addLayout(narrator_row)
        self.archive_summary = QLabel('Select a book to see its completed parts.')
        self.archive_summary.setWordWrap(True)
        l.addWidget(self.archive_summary)
        l.addWidget(title_label('Archival metadata', 14))
        grid = QGridLayout()
        self.archive_title = QLineEdit()
        self.archive_author = QLineEdit()
        self.archive_series = QLineEdit()
        self.archive_volume = QLineEdit()
        self.archive_voice_name = QLineEdit()
        self.archive_cover = QLineEdit()
        self.archive_cover.setPlaceholderText('Leave blank to extract the cover from the EPUB automatically')
        for i, (label, widget) in enumerate([
            ('Title', self.archive_title), ('Author', self.archive_author),
            ('Series (optional)', self.archive_series), ('Series number (optional)', self.archive_volume),
            ('Narrator', self.archive_voice_name), ('Cover JPEG/PNG (optional)', self.archive_cover),
        ]):
            grid.addWidget(QLabel(label), i, 0)
            grid.addWidget(widget, i, 1)
        grid.addWidget(mk_button('Browse…', self.archive_browse_cover), 5, 2)
        l.addLayout(grid)
        row = QHBoxLayout()
        row.addWidget(mk_button('Save book metadata', self.archive_save_metadata))
        row.addWidget(mk_button('Export archival M4B…', self.archive_start_export))
        row.addWidget(mk_button('Open archived folder', self.archive_open_folder))
        row.addStretch()
        l.addLayout(row)
        self.archive_result = QLabel('No archive has been exported for this edition.')
        self.archive_result.setWordWrap(True)
        l.addWidget(self.archive_result)
        guidance = QLabel('An edition is ready only when every queued part for one narrator is finished. '
            'The worker uses existing WAVs to create or refresh the complete M4B, then copies AAC audio '
            'without re-encoding it. Archived files are separate from your working parts.')
        guidance.setWordWrap(True)
        guidance.setObjectName('muted')
        l.addWidget(guidance)
        l.addStretch()

    def archive_refresh_books(self, *_):
        """Reload archive choices without disturbing the currently selected book."""
        selected = self.archive_book.currentData()
        self.archive_book.blockSignals(True)
        self.archive_book.clear()
        for book in self.store.books():
            self.archive_book.addItem(f"{book['title']} — {book['author']}", book['id'])
        if selected is not None:
            found = self.archive_book.findData(selected)
            if found >= 0:
                self.archive_book.setCurrentIndex(found)
        self.archive_book.blockSignals(False)
        self.archive_book_changed()

    def archive_book_changed(self, *_):
        """List available narrator editions for the selected book."""
        book_id = self.archive_book.currentData()
        self.archive_groups = []
        self.archive_narrator.blockSignals(True)
        self.archive_narrator.clear()
        if book_id is not None:
            try:
                conv = self.converter or load_converter()
                self.converter = conv
                # Preserve cleared finished jobs via verified permanent receipts,
                # matching the regular complete-book export's selection.
                history = [dict(row) for row in self.store.jobs()]
                history.extend(self.store.recorded_jobs_for_export(book_id))
                self.archive_groups = collect_export_groups(history, book_id, conv.book_folder)
                for group in self.archive_groups:
                    self.archive_narrator.addItem(
                        f"{voice_name(group['voice_path'])} — {len(group['parts'])} parts, "
                        f"{len(group['unfinished'])} unfinished")
            except Exception as exc:
                self.archive_summary.setText(f'Unable to inspect editions: {exc}')
        self.archive_narrator.blockSignals(False)
        self.archive_narrator_changed()

    def archive_current(self):
        """Return the selected book and narrator-specific group, or empty values."""
        book_id = self.archive_book.currentData()
        idx = self.archive_narrator.currentIndex()
        if book_id is None or idx < 0 or idx >= len(self.archive_groups):
            return None, None
        return self.store.book(book_id), self.archive_groups[idx]

    def archive_narrator_changed(self, *_):
        """Show completion, duration and previously saved edition metadata."""
        book, group = self.archive_current()
        if not book or not group:
            self.archive_summary.setText('No queued parts for this book yet.')
            self.archive_result.setText('No archived edition.')
            return
        try:
            metadata = edition_info(Path(book['path']), group['folder'])
            self.archive_title.setText(metadata.get('title') or book['title'])
            self.archive_author.setText(metadata.get('author') or book['author'])
            self.archive_series.setText(metadata.get('series', ''))
            self.archive_volume.setText(metadata.get('volume', ''))
            self.archive_voice_name.setText(metadata.get('narrator') or voice_name(group['voice_path']))
            self.archive_cover.setText(metadata.get('cover', ''))
            ready = not group['unfinished']
            length = '—'
            coverage_note = ''
            if ready:
                indices = verify_export_group(group, int(book['sections']))
                length = fmt_seconds(audio_duration(group['folder'], indices))
                coverage = export_coverage(indices, int(book['sections']))
                if coverage['interior']:
                    coverage_note = (f" · WARNING: {len(coverage['interior'])} interior "
                                     f"EPUB section(s) missing: "
                                     f"{compact_section_ranges(coverage['interior'])}")
            self.archive_summary.setText(
                f"{'Ready for archive' if ready else 'Still generating'} · "
                f"{len(group['parts'])-len(group['unfinished'])}/{len(group['parts'])} finished parts · "
                f"{len(group['indices'])} selected sections · {length} audio{coverage_note}")
            exported = Path(metadata.get('last_export', '')) if metadata.get('last_export') else None
            self.archive_result.setText(f'Last archive: {exported}' if exported and exported.is_file()
                                        else 'No archived M4B currently found.')
        except Exception as exc:
            self.archive_summary.setText(f'Not ready: {exc}')
            self.archive_result.setText('Finish or repair the missing parts before archiving.')

    def archive_details(self):
        """Collect user-entered tags without modifying a source EPUB."""
        return {'title': self.archive_title.text().strip(),
                'author': self.archive_author.text().strip(),
                'series': self.archive_series.text().strip(),
                'volume': self.archive_volume.text().strip(),
                'narrator': self.archive_voice_name.text().strip(),
                'cover': self.archive_cover.text().strip()}

    def archive_browse_cover(self):
        """Select optional user-supplied artwork; otherwise use EPUB artwork."""
        filename, _ = QFileDialog.getOpenFileName(self, 'Choose cover art', str(Path.home()),
                                                  'Image files (*.jpg *.jpeg *.png)')
        if filename:
            self.archive_cover.setText(filename)

    def archive_save_metadata(self):
        """Persist local catalog information independently of an actual export."""
        book, group = self.archive_current()
        if not book or not group:
            return
        try:
            current = edition_info(Path(book['path']), group['folder'])
            current.update(self.archive_details())
            save_edition(Path(book['path']), group['folder'], current)
            self.statusBar().showMessage('Local archival metadata saved')
        except Exception as exc:
            self.error('Unable to save metadata', exc)

    def archive_start_export(self):
        """Validate completion and start an isolated offline metadata/export worker."""
        if self.proc is not None or self.full_export_proc is not None or self.archive_proc is not None:
            QMessageBox.information(self, 'Export busy', 'Finish the active audio task first.')
            return
        editor = getattr(self, 'editor_dialog', None)
        if editor is not None and editor.isVisible() and editor.proc is not None:
            QMessageBox.information(self, 'Audio editor busy', 'Finish the active chapter rebuild first.')
            return
        book, group = self.archive_current()
        if not book or not group:
            QMessageBox.information(self, 'Select an edition', 'Choose a book and narrator first.')
            return
        try:
            indices = verify_export_group(group, int(book['sections']))
            details = self.archive_details()
            if not details['title'] or not details['author']:
                raise ValueError('Title and author are required for archival export.')
            if details['cover'] and not Path(details['cover']).is_file():
                raise ValueError('The selected cover image no longer exists.')
            coverage_note = export_coverage_message(indices, int(book['sections']))
            filename = re.sub(r'[^\w .-]', '_', details['title']).strip()[:100] or 'audiobook'
            destination, _ = QFileDialog.getSaveFileName(
                self, 'Save complete archival audiobook', str(Path.home() / f'{filename}.m4b'),
                'M4B audiobook (*.m4b)')
            if not destination:
                return
            target = Path(destination)
            if target.suffix.lower() != '.m4b':
                target = target.with_suffix('.m4b')
            if QMessageBox.question(
                    self, 'Confirm complete archival coverage',
                    f'Export {len(indices)} EPUB section(s) to:\n{target}\n\n'
                    f'{coverage_note}\n\n'
                    'Continue only if any skipped sections are intentional.',
                    QMessageBox.Yes | QMessageBox.No, QMessageBox.No) != QMessageBox.Yes:
                return
            if target.exists() and QMessageBox.question(
                    self, 'Overwrite existing archive', f'Replace the existing file?\n{target}',
                    QMessageBox.Yes | QMessageBox.No, QMessageBox.No) != QMessageBox.Yes:
                return
            self.archive_save_metadata()
            args = ['-u', str(SCRIPTS / 'audiobookai_archive.py'),
                    '--epub', str(book['path']), '--folder', str(group['folder']),
                    '--indices', ','.join(map(str, indices)), '--destination', str(target)]
            if group['voice_path']:
                args += ['--voice', str(group['voice_path'])]
            for key in ('title', 'author', 'series', 'volume', 'narrator'):
                args += [f'--{key}', details[key]]
            if details['cover']:
                args += ['--cover', details['cover']]
        except Exception as exc:
            self.error('Cannot archive this edition', exc)
            return
        self.archive_output = target
        self.archive_proc = QProcess(self)
        proc = self.archive_proc
        proc.setProgram(sys.executable)
        proc.setArguments(args)
        proc.setWorkingDirectory(str(ROOT))
        proc.setProcessChannelMode(QProcess.MergedChannels)
        proc.readyReadStandardOutput.connect(self.archive_read_output)
        proc.finished.connect(self.archive_finished)
        proc.errorOccurred.connect(self.archive_error)
        self.archive_result.setText(f'Exporting: {target}')
        self.statusBar().showMessage('Archiving complete audiobook in the background…')
        proc.start()

    def archive_read_output(self):
        """Consume only the available child output without rebuilding tables."""
        if self.archive_proc is not None:
            message = bytes(self.archive_proc.readAllStandardOutput()).decode('utf-8', errors='replace')
            if message.strip():
                self.write_log(message.rstrip())

    def archive_error(self, error):
        """Recover the UI state when a child fails before emitting finished."""
        if self.archive_proc is None:
            return
        self.write_log(f'Archive worker process error: {self.archive_proc.errorString()}')
        if error == QProcess.FailedToStart:
            self.archive_result.setText('Unable to launch archive worker')
            self.archive_proc = None
            self.archive_output = None

    def archive_finished(self, exit_code, exit_status):
        """Report archive outcome, preserving all existing source recordings."""
        self.archive_read_output()
        target = self.archive_output
        passed = (exit_code == 0 and exit_status == QProcess.NormalExit
                  and target is not None and target.is_file())
        self.archive_proc = None
        self.archive_output = None
        if passed:
            self.archive_result.setText(f'Archived successfully: {target} ({fmt_size(target.stat().st_size)})')
            self.statusBar().showMessage('Complete audiobook archived')
            self.archive_narrator_changed()
        else:
            self.archive_result.setText('Archive failed; existing M4B parts and WAVs are unchanged. See Queue log.')
            self.statusBar().showMessage('Archive export failed')

    def archive_open_folder(self):
        """Open the saved archive destination, if still available."""
        book, group = self.archive_current()
        if not book or not group:
            return
        saved = edition_info(Path(book['path']), group['folder']).get('last_export', '')
        if saved and Path(saved).is_file():
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(Path(saved).parent)))
        else:
            QMessageBox.information(self, 'No archive', 'Export a complete edition first.')


    def make_readalong_page(self):
        """Build read-only, chunk-synchronized playback of finished chapter WAVs.

        This page never generates speech. Older chunk indexes lack exact sample
        offsets, so the highlights intentionally describe approximate timing.
        """
        l = self.page_base('Read Along',
            'Read the saved spoken transcript alongside finished chapter audio. '
            'Chunk highlights are estimates; the original EPUB is unchanged.')
        pickers = QHBoxLayout()
        self.reader_book = QComboBox()
        self.reader_book.currentIndexChanged.connect(self.reader_book_changed)
        self.reader_voice = QComboBox()
        self.reader_voice.currentIndexChanged.connect(self.reader_voice_changed)
        self.reader_section = QComboBox()
        self.reader_section.currentIndexChanged.connect(self.reader_section_changed)
        for label, combo in (('Book', self.reader_book), ('Narrator', self.reader_voice),
                             ('Chapter', self.reader_section)):
            pickers.addWidget(QLabel(label))
            pickers.addWidget(combo, 1)
        l.addLayout(pickers)

        self.reader_summary = QLabel('Select a completed chapter to read along.')
        self.reader_summary.setWordWrap(True)
        l.addWidget(self.reader_summary)
        self.reader_text = QPlainTextEdit()
        self.reader_text.setReadOnly(True)
        self.reader_text.setPlaceholderText('Saved narration chunks will appear here.')
        self.reader_text.setLineWrapMode(QPlainTextEdit.WidgetWidth)
        self.reader_text.viewport().installEventFilter(self)
        l.addWidget(self.reader_text, 1)

        controls = QHBoxLayout()
        controls.addWidget(mk_button('▶ Play / pause', self.reader_toggle))
        controls.addWidget(mk_button('■ Stop', self.reader_stop))
        controls.addWidget(mk_button('✎ Edit chapter', self.reader_open_editor))
        self.reader_position = QSlider(Qt.Horizontal)
        self.reader_position.setRange(0, 0)
        self.reader_position.sliderReleased.connect(self.reader_seek_slider)
        controls.addWidget(self.reader_position, 1)
        self.reader_clock = QLabel('0:00 / 0:00')
        controls.addWidget(self.reader_clock)
        l.addLayout(controls)

        self.reader_folder = None
        self.reader_timeline = []
        self.reader_current = -1
        self.reader_voice_manifest = None
        self.reader_wav_path = None
        self.reader_audio = None
        self.reader_active_identity = None
        self.reader_last_ms = 0
        self.reader_last_persist_ms = 0
        self.reader_pending_resume = None
        self.reader_switching = False
        # Keep FFmpeg/NVIDIA multimedia backend unloaded during long TTS jobs.

    def reader_initialize_audio(self):
        """Initialize QtMultimedia only when the Read Along page is requested."""
        if self.reader_audio is not None:
            return
        try:
            from PySide6.QtMultimedia import QMediaPlayer, QAudioOutput
            self.reader_audio = QMediaPlayer(self)
            self.reader_output = QAudioOutput(self)
            self.reader_output.setVolume(0.9)
            self.reader_audio.setAudioOutput(self.reader_output)
            self.reader_audio.positionChanged.connect(self.reader_position_changed)
            self.reader_audio.mediaStatusChanged.connect(self.reader_media_status_changed)
            self.reader_audio.durationChanged.connect(
                lambda ms: self.reader_position.setMaximum(max(0, ms)))
            self.reader_audio.errorOccurred.connect(self.reader_playback_error)
            if self.reader_wav_path:
                self.reader_audio.setSource(QUrl.fromLocalFile(self.reader_wav_path))
        except ImportError:
            self.reader_summary.setText(
                'Qt multimedia support is unavailable. Install the matching PySide6 QtMultimedia component.')

    def reader_refresh_books(self):
        """Restore last book on first use; keep an already loaded chapter untouched."""
        before = self.reader_book.currentData()
        wanted = before
        if wanted is None:
            wanted = self.prefs.value('reader/book_id', -1, type=int)
        self.reader_book.blockSignals(True)
        self.reader_book.clear()
        for book in self.store.books():
            self.reader_book.addItem(f"{book['title']} — {book['author']}", book['id'])
        idx = self.reader_book.findData(wanted)
        if idx >= 0:
            self.reader_book.setCurrentIndex(idx)
        self.reader_book.blockSignals(False)
        current = self.reader_book.currentData()
        if (current == before and self.reader_active_identity is not None
                and self.reader_active_identity[0] == current):
            return
        self.reader_book_changed()
    def reader_book_changed(self, *_):
        """Switch books without replacing the last saved playback position with zero."""
        self.reader_save_progress()
        self.reader_pause_for_transition()
        self.reader_voice.blockSignals(True)
        self.reader_voice.clear()
        book_id = self.reader_book.currentData()
        if book_id is not None:
            book = self.store.book(book_id)
            if book:
                try:
                    self.converter = self.converter or load_converter()
                    base = self.converter.book_folder(Path(book['path']))
                    manifest_paths = [base / 'manifest.json',
                                      *sorted((base / 'voices').glob('*/manifest.json'))]
                    for path in manifest_paths:
                        if path.is_file():
                            info = json.loads(path.read_text(encoding='utf-8'))
                            self.reader_voice.addItem(
                                voice_name(info.get('voice_path')), str(path))
                    if book_id == self.prefs.value('reader/book_id', -1, type=int):
                        previous = self.prefs.value('reader/manifest', '', type=str)
                        idx = self.reader_voice.findData(previous)
                        if idx >= 0:
                            self.reader_voice.setCurrentIndex(idx)
                except (OSError, ValueError, TypeError) as exc:
                    self.reader_summary.setText('Cannot load narrator manifests: ' + str(exc))
        self.reader_voice.blockSignals(False)
        self.reader_voice_changed()
    def reader_voice_changed(self, *_):
        """Restore saved chapter only for the matching book and narrator."""
        self.reader_save_progress()
        self.reader_pause_for_transition()
        self.reader_section.blockSignals(True)
        self.reader_section.clear()
        self.reader_voice_manifest = None
        selected = self.reader_voice.currentData()
        if selected:
            try:
                self.reader_voice_manifest = Path(selected)
                manifest = json.loads(self.reader_voice_manifest.read_text(encoding='utf-8'))
                for key, chapter in sorted(manifest.get('chapters', {}).items(),
                                           key=lambda value: int(value[0])):
                    if isinstance(chapter, dict) and Path(chapter.get('wav', '')).is_file():
                        self.reader_section.addItem(
                            f"{int(key):03d} — {chapter.get('title', '')}", int(key))
                if (self.reader_book.currentData() ==
                        self.prefs.value('reader/book_id', -1, type=int)
                        and selected == self.prefs.value('reader/manifest', '', type=str)):
                    previous = self.prefs.value('reader/chapter', -1, type=int)
                    idx = self.reader_section.findData(previous)
                    if idx >= 0:
                        self.reader_section.setCurrentIndex(idx)
            except (OSError, ValueError, TypeError) as exc:
                self.reader_summary.setText('Cannot inspect chapters: ' + str(exc))
        self.reader_section.blockSignals(False)
        self.reader_section_changed()
    def reader_section_changed(self, *_, force=False):
        """Load a finished chapter and restore saved position only on an identity match."""
        chapter = self.reader_section.currentData()
        manifest = self.reader_voice.currentData()
        identity = (self.reader_book.currentData(), str(manifest or ''), chapter)
        if not force and identity == self.reader_active_identity and self.reader_wav_path:
            return
        self.reader_save_progress()
        self.reader_pause_for_transition()
        self.reader_switching = True
        self.reader_active_identity = None
        self.reader_current = -1
        self.reader_timeline = []
        self.reader_wav_path = None
        self.reader_pending_resume = None
        self.reader_text.clear()
        self.reader_text.setExtraSelections([])
        if chapter is None or not manifest:
            self.reader_summary.setText('No completed chapter selected.')
            self.reader_last_ms = 0
            self.reader_switching = False
            return
        try:
            self.reader_voice_manifest = Path(manifest)
            self.reader_folder = self.reader_voice_manifest.parent
            data = read_chapter(self.reader_folder, chapter)
            self.reader_timeline = data['chunks']
            self.reader_wav_path = data['wav']
            # One displayed QTextBlock per saved spoken chunk for click-to-seek.
            self.reader_text.setPlainText('\n'.join(c['text'] for c in self.reader_timeline))
            self.reader_summary.setText(
                f"{data['title']} · {len(self.reader_timeline)} chunks · "
                f"{data['timing']}. Text reflects saved narration and may contain "
                'pronunciation substitutions or rebuilt replacement takes.')
            self.reader_position.setMaximum(data['duration_ms'])
            saved_identity = (
                self.prefs.value('reader/book_id', -1, type=int),
                self.prefs.value('reader/manifest', '', type=str),
                self.prefs.value('reader/chapter', -1, type=int))
            resume = (clamp_resume_ms(self.prefs.value('reader/position_ms', 0),
                                      data['duration_ms'])
                      if saved_identity == identity else 0)
            self.reader_active_identity = identity
            self.reader_last_ms = resume
            self.reader_last_persist_ms = resume
            if self.reader_audio is not None:
                # Clearing first forces the media backend to re-open rebuilt WAVs.
                self.reader_pending_resume = resume if resume else None
                self.reader_audio.setSource(QUrl())
                self.reader_audio.setSource(QUrl.fromLocalFile(self.reader_wav_path))
            self.reader_position_changed(resume)
            self.reader_save_progress()
        except Exception as exc:
            self.reader_active_identity = None
            self.reader_summary.setText('Read Along cannot load this chapter: ' + str(exc))
        finally:
            self.reader_switching = False
    def reader_toggle(self):
        """Play or pause the assembled chapter WAV through QtMultimedia."""
        if self.reader_audio is None or not self.reader_wav_path:
            return
        from PySide6.QtMultimedia import QMediaPlayer
        if self.reader_audio.playbackState() == QMediaPlayer.PlayingState:
            self.reader_audio.pause()
        else:
            self.reader_audio.play()

    def reader_stop(self):
        """Stop the user-selected playback and intentionally reset this chapter to zero."""
        if self.reader_audio is not None:
            self.reader_audio.stop()
        self.reader_pending_resume = None
        self.reader_last_ms = 0
        self.reader_position_changed(0)
        self.reader_save_progress()

    def reader_pause_for_transition(self):
        """Pause playback without erasing the saved reading position."""
        if self.reader_audio is not None:
            self.reader_audio.pause()

    def reader_save_progress(self):
        """Save only an actually loaded chapter, not transient combobox changes."""
        identity = getattr(self, 'reader_active_identity', None)
        if identity is None or identity[0] is None or identity[2] is None:
            return
        self.prefs.setValue('reader/book_id', int(identity[0]))
        self.prefs.setValue('reader/manifest', identity[1])
        self.prefs.setValue('reader/chapter', int(identity[2]))
        self.prefs.setValue('reader/position_ms', max(0, int(self.reader_last_ms)))
        self.reader_last_persist_ms = self.reader_last_ms

    def reader_media_status_changed(self, status):
        """Wait for an audio source to load before restoring its previous seek offset."""
        if self.reader_audio is None or self.reader_pending_resume is None:
            return
        from PySide6.QtMultimedia import QMediaPlayer
        if status in (QMediaPlayer.LoadedMedia, QMediaPlayer.BufferedMedia):
            offset = self.reader_pending_resume
            self.reader_pending_resume = None
            self.reader_audio.setPosition(offset)
            self.reader_position_changed(offset)
    def reader_playback_error(self, *_):
        """Report backend playback errors without touching audio files."""
        if self.reader_audio is not None:
            self.reader_summary.setText('Audio playback problem: ' + self.reader_audio.errorString())

    def reader_seek_slider(self):
        """Seek after scrubbing and persist this explicit change immediately."""
        if self.reader_audio is not None and self.reader_wav_path:
            target = self.reader_position.value()
            self.reader_audio.setPosition(target)
            self.reader_last_ms = target
            self.reader_position_changed(target)
            self.reader_save_progress()
    def reader_position_changed(self, ms):
        """Update the clock, save progress periodically, and highlight the active chunk."""
        ms = max(0, int(ms))
        if not self.reader_position.isSliderDown():
            self.reader_position.setValue(ms)
        total = self.reader_position.maximum()
        self.reader_clock.setText(
            f"{ms//60000}:{(ms//1000)%60:02d} / {total//60000}:{(total//1000)%60:02d}")
        if (not self.reader_switching and self.reader_pending_resume is None
                and self.reader_active_identity is not None):
            self.reader_last_ms = ms
            if abs(ms - self.reader_last_persist_ms) >= 5000:
                self.reader_save_progress()
        idx = chunk_at(self.reader_timeline, ms)
        if idx == self.reader_current or idx < 0:
            return
        self.reader_current = idx
        from PySide6.QtWidgets import QTextEdit
        from PySide6.QtGui import QTextCursor, QColor
        highlight = QTextEdit.ExtraSelection()
        highlight.cursor = QTextCursor(self.reader_text.document().findBlockByNumber(idx))
        highlight.cursor.select(QTextCursor.BlockUnderCursor)
        highlight.format.setBackground(QColor(80, 145, 205, 90))
        self.reader_text.setExtraSelections([highlight])
        self.reader_text.setTextCursor(highlight.cursor)
        self.reader_text.ensureCursorVisible()
    def reader_seek_chunk(self, block):
        """Click a narration chunk to seek to its approximate beginning."""
        if self.reader_audio is not None and 0 <= block < len(self.reader_timeline):
            target = self.reader_timeline[block]['start_ms']
            self.reader_audio.setPosition(target)
            self.reader_position_changed(target)
            self.reader_save_progress()
    def eventFilter(self, obj, event):
        """Capture only transcript clicks; defer every other event to Qt."""
        from PySide6.QtCore import QEvent
        if (hasattr(self, 'reader_text') and obj is self.reader_text.viewport()
                and event.type() == QEvent.MouseButtonRelease):
            block = self.reader_text.cursorForPosition(event.position().toPoint()).blockNumber()
            self.reader_seek_chunk(block)
        return super().eventFilter(obj, event)

    def reader_open_editor(self):
        """Open the existing editor, preserving playback position for its close callback."""
        if not self.reader_wav_path or self.reader_book.currentData() is None:
            return
        idx = self.review_book.findData(self.reader_book.currentData())
        if idx < 0:
            QMessageBox.information(self, 'Book not in Review',
                                    'Select the same book in Review audio first.')
            return
        self.reader_save_progress()
        self.reader_pause_for_transition()
        self.review_book.setCurrentIndex(idx)
        self.refresh_review()
        selected_path = str(Path(self.reader_wav_path).resolve())
        for row in range(self.review_table.rowCount()):
            cell = self.review_table.item(row, 0)
            if cell and str(Path(str(cell.data(Qt.UserRole))).resolve()) == selected_path:
                self.review_table.selectRow(row)
                self.open_chunk_editor()
                return
        QMessageBox.information(self, 'Chapter unavailable',
                                'The current chapter does not appear in Review audio.')

    def reader_after_editor(self, folder, chapter):
        """Refresh repaired WAV and applied-take text when the editor closes."""
        identity = getattr(self, 'reader_active_identity', None)
        if (identity is None or int(identity[2]) != int(chapter)
                or getattr(self, 'reader_folder', None) is None
                or Path(folder).resolve() != Path(self.reader_folder).resolve()):
            return
        self.reader_save_progress()
        self.reader_pause_for_transition()
        self.reader_active_identity = None
        self.reader_section_changed(force=True)
    def make_settings_page(self):
        """Build optional MySQL, cache, and application settings controls."""
        l = self.page_base('Settings & MySQL',
            'Your existing SQLite library remains the safe source of truth. Connect MySQL to keep '
            'a queryable, automatically refreshed copy of books, chapters, jobs and progress.', scroll=True)
        grid = QGridLayout()
        grid.setHorizontalSpacing(15)
        rows = [
            ('Narration profile', 'V7 Smooth — selectable WAV narrator per queued part'),
            ('Generation settings', '18 words · max 350 tokens · exaggeration 0.35 · CFG 0.30'),
            ('Local library database', str(DB_FILE)),
            ('Output directory', str(ROOT / 'output' / 'audiobooks')),
            ('Raw chunk cache', str(ROOT / 'output' / 'narration_cache_v2')),
        ]
        for i, (left, right) in enumerate(rows):
            label = QLabel(left)
            label.setObjectName('muted')
            grid.addWidget(label, i, 0)
            value = QLabel(right)
            value.setTextInteractionFlags(Qt.TextSelectableByMouse)
            value.setWordWrap(True)
            grid.addWidget(value, i, 1)
        grid.setColumnStretch(1, 1)
        l.addLayout(grid)

        l.addWidget(title_label('MySQL connection', 14))
        self.mysql_status_label = QLabel('MySQL not configured')
        self.mysql_status_label.setWordWrap(True)
        l.addWidget(self.mysql_status_label)
        fields = QGridLayout()
        self.mysql_host = QLineEdit(str(self.mysql_cfg.get('host', '127.0.0.1')))
        self.mysql_port = QSpinBox()
        self.mysql_port.setRange(1, 65535)
        self.mysql_port.setValue(int(self.mysql_cfg.get('port', 3306)))
        self.mysql_database = QLineEdit(str(self.mysql_cfg.get('database', 'audiobookai')))
        self.mysql_user = QLineEdit(str(self.mysql_cfg.get('user', 'audiobookai')))
        self.mysql_password_edit = QLineEdit()
        self.mysql_password_edit.setEchoMode(QLineEdit.Password)
        self.mysql_password_edit.setPlaceholderText('Password (not saved in the app settings)')
        self.mysql_remember = QCheckBox('Store password in system keyring (optional)')
        self.mysql_remember.setChecked(bool(self.mysql_cfg.get('use_keyring', False)))
        for r, (caption, widget) in enumerate((
                ('Host', self.mysql_host), ('Port', self.mysql_port),
                ('Database', self.mysql_database), ('User', self.mysql_user),
                ('Password', self.mysql_password_edit))):
            fields.addWidget(QLabel(caption), r, 0)
            fields.addWidget(widget, r, 1)
        fields.addWidget(self.mysql_remember, 5, 1)
        fields.setColumnStretch(1, 1)
        l.addLayout(fields)
        buttons = QHBoxLayout()
        buttons.addWidget(mk_button('Connect & sync', self.connect_mysql_from_settings))
        buttons.addWidget(mk_button('Sync now', lambda: self.maybe_sync_mysql(force=True)))
        buttons.addWidget(mk_button('Disconnect', self.disconnect_mysql))
        buttons.addStretch()
        l.addLayout(buttons)
        tip = QLabel('MySQL data is read-only from Workbench: the app copies local records into '
                     'a dedicated audiobookai schema. No audio or passwords are stored in MySQL. '
                     'When offline, conversion continues and the next sync catches up. '
                     'Alternatively, set AUDIOBOOKAI_MYSQL_PASSWORD in your environment.')
        tip.setObjectName('muted')
        tip.setWordWrap(True)
        l.addWidget(tip)

        storage = QHBoxLayout()
        self.space_info = QLabel('Cache size: not yet calculated')
        storage.addWidget(self.space_info, 1)
        storage.addWidget(mk_button('Calculate cache', self.calculate_cache_size))
        storage.addWidget(mk_button('Open cache folder', self.open_cache_folder))
        l.addLayout(storage)
        storage_cleanup = QHBoxLayout()
        storage_cleanup.addWidget(QLabel('Remove raw cached speech:'))
        self.cleanup_age = QComboBox()
        for label, days in [('Older than 30 days', 30), ('Older than 90 days', 90),
                            ('Older than 180 days', 180), ('All raw chunk WAVs', None)]:
            self.cleanup_age.addItem(label, days)
        self.cleanup_age.setCurrentIndex(1)
        storage_cleanup.addWidget(self.cleanup_age, 1)
        storage_cleanup.addWidget(mk_button('Preview cleanup', self.preview_cache_cleanup))
        storage_cleanup.addWidget(mk_button('Delete selected cache…', self.clean_cache))
        l.addLayout(storage_cleanup)
        self.cleanup_info = QLabel('Only raw cache WAVs are eligible. Finished M4Bs, chapter WAVs, '
                                   'edited takes, EPUBs, narrator WAVs, and database files are never deleted.')
        self.cleanup_info.setWordWrap(True)
        self.cleanup_info.setObjectName('muted')
        l.addWidget(self.cleanup_info)
        note = QLabel('Clearing raw cache can increase GPU work when resuming unfinished chapters or rebuilding edits. '
                      'Cleanup is always manual and disabled while a conversion/editor operation is active.')
        note.setObjectName('muted')
        note.setWordWrap(True)
        l.addWidget(note)

        l.addWidget(title_label('Metadata backups · V2.8', 14))
        self.backup_status = QLabel('Automatic metadata backups run every 30 minutes and '
            'retain the newest 10. Copies are initially saved on this same disk.')
        self.backup_status.setWordWrap(True)
        self.backup_status.setObjectName('muted')
        l.addWidget(self.backup_status)
        backup_buttons = QHBoxLayout()
        backup_buttons.addWidget(mk_button('Backup metadata now', self.backup_metadata_now))
        backup_buttons.addWidget(mk_button('Verify latest backup', self.verify_latest_metadata_backup))
        backup_buttons.addWidget(mk_button('Open backups folder', self.open_metadata_backup_folder))
        backup_buttons.addStretch()
        l.addLayout(backup_buttons)
        backup_tip = QLabel('Metadata-only: SQLite queue, pronunciation rules/snapshots, '
            'archive catalog and nonsecret configuration. EPUBs, voices, M4Bs, '
            'chapter WAVs and raw caches must be backed up separately.')
        backup_tip.setWordWrap(True)
        backup_tip.setObjectName('muted')
        l.addWidget(backup_tip)
        l.addStretch()

    def mark_mysql_dirty(self):
        """Flag the optional SQL mirror for a later refresh after a SQLite write."""
        self.mysql_sync_pending = True
        if self.mysql_mirror is not None:
            self.mysql_mirror.dirty = True
        if hasattr(self, 'mirror_timer') and self.mysql_cfg.get('enabled'):
            self.mirror_timer.start(1800)

    def mysql_form_config(self):
        """Read nonsecret database connection fields from the Settings form."""
        return {'enabled': True, 'host': self.mysql_host.text().strip(),
                'port': self.mysql_port.value(),
                'database': self.mysql_database.text().strip(),
                'user': self.mysql_user.text().strip(),
                'use_keyring': self.mysql_remember.isChecked()}

    def update_mysql_status(self, explanation=None):
        """Render the current MySQL state without changing canonical SQLite data."""
        if self.mysql_mirror and self.mysql_mirror.last_error:
            text = 'MySQL sync failed — your local SQLite queue is safe'
            brief = 'MySQL: sync pending'
        elif self.mysql_mirror and self.mysql_mirror.connected():
            last = self.mysql_mirror.last_sync or 'not yet'
            text = f'MySQL connected — last synced {last}'
            brief = 'MySQL: connected'
        elif self.mysql_cfg.get('enabled'):
            text = 'MySQL offline — local SQLite queue is still available'
            brief = 'MySQL: offline (safe)'
            if self.mysql_password is None:
                text += '. Open Settings and enter the password.'
        else:
            text = 'MySQL disabled — local SQLite queue is active'
            brief = 'MySQL: disabled'
        if explanation:
            text += f' — {explanation}'
        self.mysql_sidebar_status.setText(brief)
        if hasattr(self, 'mysql_status_label'):
            self.mysql_status_label.setText(text)

    def connect_saved_mysql(self):
        """Attempt reconnect on launch when settings and credentials are available."""
        if self.mysql_cfg.get('enabled') and self.mysql_password is not None:
            self.maybe_sync_mysql(force=False)
        else:
            self.update_mysql_status()

    def connect_mysql_from_settings(self):
        """Validate the entered credentials and synchronize the local catalogue."""
        cfg = self.mysql_form_config()
        if not all((cfg['host'], cfg['user'], cfg['database'])):
            QMessageBox.warning(self, 'Missing connection details', 'Host, database, and user are required.')
            return
        password = self.mysql_password_edit.text()
        if not password:
            password = lookup_password(cfg)
        if password is None:
            QMessageBox.warning(self, 'Password required',
                                'Enter a MySQL password or supply AUDIOBOOKAI_MYSQL_PASSWORD.')
            return
        if self.mysql_mirror:
            self.mysql_mirror.disconnect()
        self.mysql_mirror = MySQLMirror(cfg, password)
        try:
            counts = self.mysql_mirror.sync(self.store)
        except Exception as exc:
            self.update_mysql_status(str(exc))
            QMessageBox.warning(self, 'MySQL connection failed',
                f'{exc}\n\nThe local library and conversion queue have not been affected.')
            return
        self.mysql_cfg = cfg
        self.mysql_password = password
        self.mysql_sync_pending = False
        self.mysql_password_edit.clear()
        if cfg['use_keyring']:
            try:
                save_password(cfg, password)
            except Exception as exc:
                self.mysql_cfg['use_keyring'] = False
                QMessageBox.information(self, 'Keyring unavailable',
                    f'Connected, but the password could not be saved to the OS keyring: {exc}\n'
                    'You can enter it on the next launch or use AUDIOBOOKAI_MYSQL_PASSWORD.')
        write_config({'mysql': self.mysql_cfg})
        self.update_mysql_status()
        self.statusBar().showMessage(f'MySQL synchronized: {counts[0]} books, {counts[1]} jobs.')

    def maybe_sync_mysql(self, force=False):
        """Perform a rate-limited mirror update when local metadata has changed."""
        if not self.mysql_cfg.get('enabled') or self.mysql_password is None:
            self.update_mysql_status()
            return
        if not force and time.monotonic() < self.mysql_retry_after:
            return
        if not force and not self.mysql_sync_pending and self.mysql_mirror and self.mysql_mirror.connected():
            return
        try:
            if self.mysql_mirror is None:
                self.mysql_mirror = MySQLMirror(self.mysql_cfg, self.mysql_password)
            counts = self.mysql_mirror.sync(self.store)
            self.mysql_retry_after = 0
            self.mysql_sync_pending = False
            self.update_mysql_status()
            if force:
                self.statusBar().showMessage(f'MySQL in sync: {counts[0]} books, {counts[1]} jobs.')
        except Exception as exc:
            self.mysql_retry_after = time.monotonic() + 60
            self.mysql_sync_pending = True
            if self.mysql_mirror:
                self.mysql_mirror.last_error = str(exc)
            self.update_mysql_status(str(exc)[:140])
            if force:
                QMessageBox.warning(self, 'MySQL unavailable',
                    f'{exc}\n\nAudiobook generation continues normally with local SQLite.')

    def disconnect_mysql(self):
        """Disable optional synchronization and release the mirror connection."""
        if self.mysql_mirror:
            self.mysql_mirror.disconnect()
        self.mysql_mirror = None
        self.mysql_cfg['enabled'] = False
        self.mysql_password = None
        self.mysql_password_edit.clear()
        write_config({'mysql': self.mysql_cfg})
        self.update_mysql_status()
        self.statusBar().showMessage('MySQL disconnected. Local SQLite library is unchanged.')

    @staticmethod
    def setup_table(table):
        """Apply consistent table selection, resizing, and edit restrictions."""
        table.setAlternatingRowColors(True)
        table.setSelectionBehavior(QAbstractItemView.SelectRows)
        table.setSelectionMode(QAbstractItemView.SingleSelection)
        table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        table.verticalHeader().setVisible(False)
        table.horizontalHeader().setStretchLastSection(True)

    def error(self, title, message):
        """Display a modal error dialog with a useful title and explanation."""
        QMessageBox.critical(self, title, str(message))
        self.write_log(f'ERROR: {message}')

    def write_log(self, text, *, gui=True, detailed=False):
        """Queue GUI log text, then independently persist the diagnostic line."""
        if gui or detailed:
            self.generation_logs.append(text, detailed=detailed)
            # Outside a running conversion, keep user actions immediately visible.
            if self.proc is None:
                self.flush_generation_logs()
        if self.active_job_log is not None:
            try:
                append_diagnostic(self.active_job_log, text)
            except OSError:
                # Diagnostics are best effort, never an interruption to narration.
                self.active_job_log = None

    def flush_generation_logs(self):
        """Append pending lines in one Qt call, at most once per timer tick."""
        pending = self.generation_logs.drain(self.log_detailed)
        if pending:
            self.logs.appendPlainText(pending)

    def toggle_log_mode(self):
        """Switch detailed chunk output on/off without losing session history."""
        self.log_detailed = not self.log_detailed
        self.prefs.setValue('detailed_generation_log', self.log_detailed)
        self.log_mode_button.setText('Compact view' if self.log_detailed else 'Detailed view')
        self.logs.setPlainText(self.generation_logs.replay(self.log_detailed))

    def focus_search(self):
        """Focus the search field belonging to the currently visible page."""
        page = self.stack.currentIndex()
        if page == 0:
            self.library_search.setFocus()
        elif page == 1:
            self.chapter_search.setFocus()
        elif page == 2:
            self.queue_search.setFocus()

    def dragEnterEvent(self, event):
        """Allow dragged EPUB files to be accepted by the window."""
        urls = event.mimeData().urls() if event.mimeData().hasUrls() else []
        if any(url.isLocalFile() and url.toLocalFile().lower().endswith('.epub') for url in urls):
            event.acceptProposedAction()
        else:
            event.ignore()

    def dropEvent(self, event):
        """Import EPUB files dropped onto the application window."""
        paths = [Path(url.toLocalFile()) for url in event.mimeData().urls()
                 if url.isLocalFile() and url.toLocalFile().lower().endswith('.epub')]
        if not paths:
            event.ignore()
            return
        event.acceptProposedAction()
        # Prevent an accidental drag of hundreds of files from freezing the GUI.
        if len(paths) > 10:
            QMessageBox.information(self, 'Import limit',
                'Importing the first 10 EPUBs. You can drag more into the library afterward.')
        for path in paths[:10]:
            self.import_book(path)

    def filter_library(self, *_):
        """Hide library rows not matching the title or author search."""
        query = self.library_search.text().strip().casefold()
        shown = 0
        for row in range(self.library_table.rowCount()):
            title = self.library_table.item(row, 0).text().casefold()
            author = self.library_table.item(row, 1).text().casefold()
            visible = query in (title + ' ' + author)
            self.library_table.setRowHidden(row, not visible)
            shown += visible
        total = self.library_table.rowCount()
        self.library_foot.setText(f'{shown} of {total} books shown · drag EPUBs into this window to import')

    def filter_chapters(self, *_):
        """Filter visible chapter rows without changing their selected state."""
        query = self.chapter_search.text().strip().casefold()
        for row in range(self.section_table.rowCount()):
            index = self.section_table.item(row, 0).text()
            title = self.section_table.item(row, 1).text().casefold()
            visible = not query or query in title or query == index
            self.section_table.setRowHidden(row, not visible)

    def select_visible(self):
        """Check or uncheck chapters currently visible after filtering."""
        visible = {sec.index for row, sec in enumerate(self.sections)
                   if not self.section_table.isRowHidden(row)}
        self.set_checks(lambda index: index in visible)

    def select_range(self):
        """Parse and apply explicit section numbers from the range input."""
        try:
            selection = set(parse_section_ranges(self.range_input.text(),
                                                [section.index for section in self.sections]))
        except ValueError as exc:
            QMessageBox.warning(self, 'Invalid chapter range', str(exc))
            return
        self.set_checks(lambda index: index in selection)
        self.statusBar().showMessage(f'Selected {len(selection)} EPUB sections from your range.')

    def queue_from_shortcut(self):
        """Queue the current selection via the keyboard shortcut."""
        if self.stack.currentIndex() == 1:
            self.queue_selection()

    def filter_queue(self, *_):
        """Show only jobs matching the queue search and status filters."""
        wanted = self.queue_filter.currentText()
        term = self.queue_search.text().strip().casefold()
        shown = 0
        for row in range(self.job_table.rowCount()):
            fields = [self.job_table.item(row, col).text() for col in range(5)]
            matches_group = wanted == 'All jobs' or job_category(fields[4].lower()) == wanted
            matches_text = not term or term in ' '.join(fields).casefold()
            visible = matches_group and matches_text
            self.job_table.setRowHidden(row, not visible)
            shown += visible
        self.queue_count.setText(f'{shown} / {self.job_table.rowCount()} jobs')

    def copy_log(self):
        """Copy visible generation messages to the clipboard for debugging."""
        self.flush_generation_logs()
        text = self.logs.toPlainText()
        if text:
            QApplication.clipboard().setText(text)
            self.statusBar().showMessage('Conversion messages copied to clipboard.')

    def clear_log(self):
        """Clear the displayed log without deleting on-disk audio or job history."""
        self.generation_logs.clear()
        self.logs.clear()
        self.statusBar().showMessage('Log display cleared. Conversion progress and saved jobs are unchanged.')

    def import_dialog(self):
        """Open the file chooser for an EPUB to add to the local library."""
        start = ROOT / 'books'
        if not start.exists():
            start = Path.home()
        path, _ = QFileDialog.getOpenFileName(self, 'Import EPUB', str(start), 'EPUB books (*.epub)')
        if path:
            self.import_book(Path(path))

    def import_book(self, path):
        """Inspect an EPUB and store its section metadata in SQLite."""
        QApplication.setOverrideCursor(Qt.WaitCursor)
        try:
            conv = self.converter or load_converter()
            self.converter = conv
            title, author, sections = conv.load_epub(path)
            book_id = self.store.import_book(path, title, author, sections)
            self.book_cache[book_id] = (title, author, sections)
            self.refresh_books(select_id=book_id)
            self.select_book(book_id)
            self.change_page(1)
            self.statusBar().showMessage(f'Imported {title} — {len(sections)} sections')
        except Exception as exc:
            self.error('EPUB import failed', exc)
        finally:
            QApplication.restoreOverrideCursor()

    def refresh_books(self, select_id=None):
        """Reload library widgets and keep the current book selection when possible."""
        books = self.store.books()
        self.library_table.setRowCount(len(books))
        for r, b in enumerate(books):
            for c, text in enumerate((b['title'], b['author'], b['sections'], f"{b['words']:,}", b['added'])):
                self.library_table.setItem(r, c, item(text, b['id'] if c == 0 else None))
        self.filter_library()
        current = select_id or self.current_book_id
        self.gen_book.blockSignals(True)
        self.review_book.blockSignals(True)
        self.gen_book.clear()
        self.review_book.clear()
        for b in books:
            display = f"{b['title']}  —  {b['author']}"
            self.gen_book.addItem(display, b['id'])
            self.review_book.addItem(display, b['id'])
        if current:
            for combo in (self.gen_book, self.review_book):
                idx = combo.findData(current)
                if idx >= 0:
                    combo.setCurrentIndex(idx)
        self.gen_book.blockSignals(False)
        self.review_book.blockSignals(False)
        self.quality_refresh_books()
        if books:
            self.book_combo_changed()
        else:
            self.section_table.setRowCount(0)
            self.gen_details.setText('Import an EPUB to begin.')

    def current_library_book(self):
        """Return the book record selected in the library table."""
        r = self.library_table.currentRow()
        first = self.library_table.item(r, 0) if r >= 0 else None
        return first.data(Qt.UserRole) if first else None

    def generate_selected_book(self, *_):
        """Open the Generate page for the selected library book."""
        book_id = self.current_library_book()
        if book_id is None:
            return
        self.select_book(book_id)
        self.change_page(1)

    def remove_selected_book(self):
        """Delete a book catalogue entry after enforcing job-history safeguards."""
        book_id = self.current_library_book()
        if book_id is None:
            return
        try:
            self.store.delete_book(book_id)
            self.book_cache.pop(book_id, None)
            if self.current_book_id == book_id:
                self.current_book_id = None
            self.refresh_books()
        except ValueError as exc:
            QMessageBox.warning(self, 'Cannot remove book', str(exc))

    def select_book(self, book_id):
        """Load an EPUB section list and update the book-specific Generate controls."""
        idx = self.gen_book.findData(book_id)
        if idx >= 0:
            self.gen_book.setCurrentIndex(idx)
        self.book_combo_changed()

    def clear_generation_selection(self):
        """Prevent stale EPUB chapters from being queued after a load failure."""
        self.current_book_id = None
        self.sections = []
        self.section_table.setRowCount(0)
        self.gen_details.setText('Source EPUB unavailable — choose another book or restore the file.')
        self.gen_selected.setText('0 selected')
        self.refresh_saved_parts()

    def book_combo_changed(self, *_):
        """React to selecting a different book in the Generate dropdown."""
        book_id = self.gen_book.currentData()
        if book_id is None:
            return
        book = self.store.book(book_id)
        # Recheck the source even if its sections were cached earlier this session.
        # Never show/queue the previous book's chapters under a missing EPUB.
        if book is None or not Path(book['path']).is_file():
            missing = book['path'] if book is not None else f'book #{book_id}'
            self.clear_generation_selection()
            self.error('Unable to open book', f'EPUB moved or missing: {missing}')
            return
        if book_id not in self.book_cache:
            try:
                self.converter = self.converter or load_converter()
                self.book_cache[book_id] = self.converter.load_epub(Path(book['path']))
            except Exception as exc:
                self.clear_generation_selection()
                self.error('Unable to open book', exc)
                return
        self.current_book_id = book_id
        self.prefs.setValue('last_book_id', book_id)
        title, author, self.sections = self.book_cache[book_id]
        self.store.populate_missing_sections(book_id, self.sections)
        if book_id not in self._history_scanned:
            # Older releases stored section ranges in M4B names, but deleting
            # a job erased that map. Import ONLY playable files whose chapter
            # count verifies their original range; never trust partial exports.
            book = self.store.book(book_id)
            base = self.converter.book_folder(Path(book['path']))
            imported = self.store.recover_legacy_exports(book_id, base)
            for voice_dir in sorted((base / 'voices').glob('voice_*')):
                try:
                    manifest = json.loads((voice_dir / 'manifest.json').read_text(encoding='utf-8'))
                    voice = manifest.get('voice_path')
                except (OSError, ValueError):
                    continue
                if voice:
                    imported += self.store.recover_legacy_exports(book_id, voice_dir, voice)
            self._history_scanned.add(book_id)
            if imported:
                self.statusBar().showMessage(
                    f'Recovered {imported} verified legacy part(s) into permanent history.')
        words = sum(len(s.text.split()) for s in self.sections)
        self.gen_details.setText(f'{title} · {author} · {len(self.sections)} sections · {words:,} words')
        self.refresh_saved_parts()
        self.section_table.blockSignals(True)
        self.section_table.setRowCount(len(self.sections))
        for r, sec in enumerate(self.sections):
            checked = bool(STORY_RE.match(sec.title))
            self.section_table.setItem(r, 0, item(str(sec.index), sec.index, checked))
            self.section_table.setItem(r, 1, item(sec.title))
            self.section_table.setItem(r, 2, item(f'{len(sec.text.split()):,}'))
        self.section_table.blockSignals(False)
        if not self.selected_indices():
            self.set_checks(lambda _n: True)
        self.update_selected_count()
        self.filter_chapters()
        self.restore_voice_for_book(book_id)

    def refresh_saved_parts(self):
        """Show durable verified exports even if their queue rows were deleted."""
        if self.current_book_id is None:
            self.gen_saved_parts.setText('Completed parts: none yet')
            return
        parts = self.store.completed_parts(self.current_book_id)
        if not parts:
            self.gen_saved_parts.setText('Completed parts: none yet')
            return
        available = sum(Path(part['output_path']).is_file() for part in parts)
        section_set = {int(index) for part in parts for index in json.loads(part['indices'])}
        numbers = ', '.join(str(part['part_number']) for part in parts[:18])
        if len(parts) > 18:
            numbers += ', …'
        missing = f' · {len(parts) - available} missing file(s)' if available != len(parts) else ''
        self.gen_saved_parts.setText(
            f'Completed parts: {len(parts)} (Parts {numbers}) · '
            f'{len(section_set)} recorded sections{missing}. '
            'History survives deleting queue rows.')

    def set_checks(self, predicate):
        """Apply a predicate to the chapter checkboxes in spine order."""
        self.section_table.blockSignals(True)
        for r, sec in enumerate(self.sections):
            self.section_table.item(r, 0).setCheckState(Qt.Checked if predicate(sec.index) else Qt.Unchecked)
        self.section_table.blockSignals(False)
        self.update_selected_count()

    def select_story(self):
        """Select sections that appear to contain numbered story chapters."""
        self.set_checks(lambda idx: bool(STORY_RE.match(self.sections[idx-1].title)))

    def select_next_unqueued(self):
        """Find the next contiguous batch not claimed by existing jobs."""
        if self.current_book_id is None:
            return
        claimed = self.store.claimed_sections(self.current_book_id)
        story = [s.index for s in self.sections if STORY_RE.match(s.title)]
        available = [i for i in (story or [s.index for s in self.sections]) if i not in claimed]
        next_ids = set(available[:self.batch_size.value()])
        self.set_checks(lambda idx: idx in next_ids)
        if not next_ids:
            QMessageBox.information(self, 'Nothing left', 'All matching sections are already queued or completed.')

    def selected_indices(self):
        """Return checked EPUB section indices in reading order."""
        return [self.section_table.item(r, 0).data(Qt.UserRole)
                for r in range(self.section_table.rowCount())
                if self.section_table.item(r, 0).checkState() == Qt.Checked]

    def update_selected_count(self, *_):
        """Show section count, word count, and rough finished-audio estimate."""
        if not self.sections:
            self.gen_selected.setText('0 selected')
            return
        nums = self.selected_indices()
        words = sum(len(self.sections[n-1].text.split()) for n in nums)
        hours = estimate_audio_duration(words) / 3600
        self.gen_selected.setText(f'{len(nums)} selected sections · {words:,} words · ~{hours:.1f} h finished audio (estimate, not GPU time)')

    def queue_selection(self):
        """Create one or more immutable narrator-specific jobs from checked sections."""
        if self.current_book_id is None:
            return
        selected = self.selected_indices()
        if not selected:
            QMessageBox.information(self, 'Nothing selected', 'Check at least one chapter or EPUB section.')
            return
        claimed = set(selected) & self.store.claimed_sections(self.current_book_id)
        if claimed:
            answer = QMessageBox.question(self, 'Sections already queued',
                f'{len(claimed)} selected section(s) have already been queued or completed. '
                'Add a duplicate conversion anyway?',
                QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
            if answer != QMessageBox.Yes:
                return
        book = self.store.book(self.current_book_id)
        self.converter = self.converter or load_converter()
        size = self.batch_size.value() if self.split_parts.isChecked() else len(selected)
        parts = [selected[i:i+size] for i in range(0, len(selected), size)]
        try:
            selection = self.selected_narrator()
            fixed_voice = freeze_voice(selection)
        except Exception as exc:
            self.error('Unable to queue narrator WAV', exc)
            return
        # Reserve names before persisting immutable jobs. Each narrator has a
        # readable part sequence; existing exports/queued names must never be
        # overwritten, including audio left by a deleted job.
        existing = [j for j in self.store.jobs() if j['book_id'] == self.current_book_id]
        occupied = {j['output_name'] for j in existing}
        voice_id = str(fixed_voice) if fixed_voice is not None else None
        folder = self.converter.book_folder(Path(book['path']), fixed_voice)
        next_part = self.store.next_part_number(self.current_book_id, voice_id, folder)
        for i, numbers in enumerate(parts, next_part):
            span = f'{numbers[0]}-{numbers[-1]}' if len(numbers) > 1 else str(numbers[0])
            name = f'Part {i} · sections {span}'
            output_base = part_export_basename(book['title'], i, numbers, occupied, folder)
            occupied.add(output_base)
            # A new part captures the dictionary as it exists NOW. This file is
            # never updated on subsequent dictionary edits or GUI restarts.
            new_job_id = self.store.add_job(self.current_book_id, name, numbers, output_base,
                                            fixed_voice, pronunciation_required=True)
            try:
                snapshot_for_job(new_job_id, book['path'])
            except Exception as exc:
                # Never queue a job that might silently ignore intended rules.
                self.store.remove_job(new_job_id)
                self.refresh_jobs()
                self.error('Unable to snapshot pronunciation rules', exc)
                return
        self.refresh_jobs()
        self.change_page(2)
        self.write_log(f"Queued {len(parts)} part(s) from '{book['title']}' ({len(selected)} sections).")
        if self.autorun.isChecked() and self.proc is None:
            self.start_queue()

    def refresh_jobs(self):
        """Refresh the queue table without repeatedly destroying QTableWidgetItems.

        Qt owns every QTableWidgetItem supplied with setItem(). Updating an
        existing item's text/data minimizes C++ wrapper churn during a long
        conversion, and blocking signals prevents partially filled row access.
        """
        jobs = self.store.jobs()
        table = self.job_table
        selected_job = self.selected_job_id()
        was_sorted = table.isSortingEnabled()
        previous_signals = table.blockSignals(True)
        table.setUpdatesEnabled(False)
        try:
            table.setSortingEnabled(False)
            table.setRowCount(len(jobs))
            selected_row = None
            for r, job in enumerate(jobs):
                nums = json.loads(job['indices'])
                values = (job['name'], job['book_title'], str(len(nums)),
                          voice_name(job['voice_path']), job['status'].title(),
                          f"{round(100 * job['completed_chunks']/job['total_chunks'])}%"
                          if job['total_chunks'] else '—', job['added'])
                for c, value in enumerate(values):
                    cell = table.item(r, c)
                    if cell is None:
                        table.setItem(r, c, item(value, job['id'] if c == 0 else None))
                    else:
                        if cell.text() != str(value):
                            cell.setText(str(value))
                        if c == 0 and cell.data(Qt.UserRole) != job['id']:
                            cell.setData(Qt.UserRole, job['id'])
                if job['id'] == selected_job:
                    selected_row = r
            if selected_row is not None:
                table.selectRow(selected_row)
            else:
                table.clearSelection()
                table.setCurrentCell(-1, -1)
        finally:
            table.setSortingEnabled(was_sorted)
            table.setUpdatesEnabled(True)
            table.blockSignals(previous_signals)
        self.filter_queue()

    def selected_job_id(self):
        """Look up the stable database identifier stored in the selected queue row."""
        r = self.job_table.currentRow()
        first = self.job_table.item(r, 0) if r >= 0 else None
        return first.data(Qt.UserRole) if first else None

    def recover_interrupted(self):
        """Requeue interrupted parts after non-destructive dependency checks."""
        if self.proc is not None:
            QMessageBox.information(self, 'Queue busy',
                                    'Stop the active conversion before recovering interrupted jobs.')
            return
        items = [j for j in self.store.jobs() if j['status'] == 'interrupted']
        if not items:
            QMessageBox.information(self, 'No interrupted parts', 'The queue has nothing to recover.')
            return
        ready, blocked = [], []
        for job in items:
            problem = check_resume_job(job)
            if problem:
                blocked.append(f"#{job['id']}: {problem}")
            else:
                ready.append(job['id'])
        if not ready:
            QMessageBox.warning(self, 'Recovery blocked',
                'No interrupted parts passed preflight:\n' + '\n'.join(blocked[:12]))
            return
        message = (f'Requeue {len(ready)} interrupted part(s)?\n'
                   'Cached narration and existing WAVs will be preserved. '
                   'The queue will NOT start automatically.')
        if blocked:
            message += '\n\nSkipped:\n' + '\n'.join(blocked[:10])
        if QMessageBox.question(self, 'Recover interrupted parts', message,
                                QMessageBox.Yes | QMessageBox.No, QMessageBox.No) != QMessageBox.Yes:
            return
        count = self.store.requeue_interrupted(ready)
        self.refresh_jobs()
        self.write_log(f'Recovery: {count} interrupted part(s) requeued; {len(blocked)} skipped.')
        self.statusBar().showMessage(f'{count} parts ready. Click Start queue when ready.')

    def open_selected_diagnostic(self):
        """Open the last bounded diagnostic log associated with one queue job."""
        job_id = self.selected_job_id()
        if job_id is None:
            return
        files = list(self.diagnostic_dir.glob(f'job_{int(job_id)}_*.log'))
        if not files:
            QMessageBox.information(self, 'No diagnostic log',
                                    'No V2.8 diagnostic log has been saved for this part.')
            return
        latest = max(files, key=lambda p: p.stat().st_mtime)
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(latest.resolve())))

    def maybe_auto_metadata_backup(self):
        """Save an incremental-time checkpoint; never block narration if it fails."""
        try:
            recent = latest_backup(self.backup_dir, automatic_only=True)
            if recent and time.time() - recent.stat().st_mtime < 30 * 60:
                return
            path = create_metadata_backup(
                self.store.db, ROOT / 'output', self.backup_dir,
                read_config(), automatic=True, keep=10)
            self.write_log(f'Automatic metadata backup verified: {path.name}')
            self.backup_status.setText(f'Latest automatic backup: {path.name}')
        except Exception as exc:
            self.write_log(f'Metadata backup failed (narration unaffected): {exc}')
            self.backup_status.setText(f'Backup error: {exc}')

    def backup_metadata_now(self):
        """Manually save a metadata backup excluded from automatic pruning."""
        try:
            path = create_metadata_backup(
                self.store.db, ROOT / 'output', self.backup_dir,
                read_config(), automatic=False)
            self.backup_status.setText(f'Manual backup verified: {path.name}')
            self.statusBar().showMessage(f'Metadata backup saved: {path}')
        except Exception as exc:
            self.error('Metadata backup failed', exc)

    def verify_latest_metadata_backup(self):
        """Verify every SHA256 member checksum; do not extract to live files."""
        path = latest_backup(self.backup_dir)
        if path is None:
            QMessageBox.information(self, 'No backups', 'Create a metadata backup first.')
            return
        try:
            manifest = verify_metadata_backup(path)
            self.backup_status.setText(
                f'Verified {path.name}: {len(manifest["sha256"])} metadata files')
        except Exception as exc:
            self.error('Backup verification failed', exc)

    def open_metadata_backup_folder(self):
        """Open local metadata archives for external-drive copying."""
        self.backup_dir.mkdir(parents=True, exist_ok=True)
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(self.backup_dir.resolve())))

    def start_queue(self):
        """Start the next pending job unless paused or already generating."""
        if self.proc is not None:
            self.run_queue = True
            return
        self.run_queue = True
        nxt = self.store.next_job()
        if nxt is not None:
            self.start_job(nxt['id'])
        else:
            self.run_queue = False
            self.statusBar().showMessage('Queue empty — no pending jobs.')

    def run_selected_job(self):
        """Start only the selected queue entry instead of auto-running all jobs."""
        job_id = self.selected_job_id()
        if job_id is None:
            QMessageBox.information(self, 'Select a part', 'Choose a queued part first.')
            return
        if self.proc is not None:
            QMessageBox.information(self, 'Generation in progress',
                'Finish or stop the active part before starting another one.')
            return
        job = self.store.job(job_id)
        if job['status'] != 'queued':
            QMessageBox.information(self, 'Job not queued',
                'Use Retry selected first if this part is interrupted or failed.')
            return
        self.run_queue = False
        self.start_job(job_id)

    def pause_after_part(self):
        """Stop automatic queue advancement after the active M4B finishes."""
        self.run_queue = False
        if self.proc is not None:
            self.statusBar().showMessage('Will finish this M4B part, then pause before the next one.')
            self.write_log('Queue set to pause after the current part finishes.')
        else:
            self.statusBar().showMessage('Queue paused. No part is currently running.')

    def start_job(self, job_id):
        """Create a separate converter process with fixed sections, narrator, and output name."""
        if headless_runner_active():
            self.run_queue = False
            self.statusBar().showMessage('Headless converter active; GUI generation paused.')
            self.error('Headless conversion running',
                       'A headless audiobook conversion is active. Do not start a second '
                       'converter for the same cache. Resume GUI generation only after it ends.')
            return
        if self.proc is not None:
            return
        editor = getattr(self, 'editor_dialog', None)
        if editor is not None and editor.isVisible():
            self.run_queue = False
            self.statusBar().showMessage('Close the audio editor before starting conversion to avoid simultaneous chapter changes.')
            return
        job = self.store.job(job_id)
        if job is None:
            return
        epub = Path(job['book_path'])
        if not epub.is_file():
            self.store.update_job(job_id, 'failed', error='EPUB file missing')
            self.refresh_jobs()
            self.run_queue = False
            self.error('Cannot start conversion', f'EPUB not found: {epub}')
            return
        for needed in (CONVERTER_FILE, SCRIPTS / 'test_five_minutes_v7_smooth.py'):
            if not needed.is_file():
                self.run_queue = False
                self.error('Missing component', f'Place the V7 scripts in {SCRIPTS}: {needed.name}')
                return
        nums = json.loads(job['indices'])
        arguments = ['-u', str(CONVERTER_FILE), 'convert', str(epub),
                     '--chapters', ','.join(map(str, nums)), '--output-name', job['output_name'], '--studio-events']
        snapshot = snapshot_path_for_job(job_id) if job['pronunciation_required'] else None
        if job['pronunciation_required'] and snapshot is None:
            self.store.update_job(job_id, 'failed', error='Queued pronunciation snapshot is missing')
            self.refresh_jobs()
            self.run_queue = False
            self.error('Missing pronunciation snapshot',
                'The queued dictionary snapshot is missing. Restore output/pronunciation/jobs/ '
                'from your backup; this job will not run with different pronunciations.')
            return
        if snapshot is not None:
            arguments.extend(['--pronunciation-file', str(snapshot)])
        if job['voice_path']:
            voice = Path(job['voice_path'])
            if not voice.is_file():
                self.store.update_job(job_id, 'failed', error='Queued narrator WAV is missing')
                self.refresh_jobs()
                self.run_queue = False
                self.error('Missing narrator WAV', f'The saved narrator for this part is missing: {voice}')
                return
            arguments.extend(['--voice', str(voice)])
        self.maybe_auto_metadata_backup()
        self.active_job_log = job_log_path(self.diagnostic_dir, job_id)
        self.diagnostic_lines.clear()
        self.proc = QProcess(self)
        self.proc.setProgram(sys.executable)
        self.proc.setArguments(arguments)
        self.proc.setWorkingDirectory(str(ROOT))
        self.proc.setProcessChannelMode(QProcess.MergedChannels)
        self.proc.readyReadStandardOutput.connect(self.consume_output)
        self.proc.finished.connect(self.process_finished)
        self.proc.errorOccurred.connect(self.process_error)
        self.stop_requested = False
        self.active_job_id = job_id
        self.started_at = time.monotonic()
        self.chunk_durations.clear()
        self.recent_chunk_durations.clear()
        self.plan.clear()
        self.done_chunks.clear()
        self.total_chunks = 0
        self.current_section = None
        self.current_chunk = self.current_total = 0
        self.last_output_path = None
        self.exporting = False
        self.buffer = ''
        self.last_progress_save = 0.0
        self.last_progress_chunks = 0
        self._progress_last_values.clear()
        self.chapter_progress.setValue(0)
        self.overall_progress.setValue(0)
        self.active_title.setText(f"Generating: {job['book_title']} — {job['name']}")
        self.chapter_text.setText('Preparing chapter plan…')
        self.overall_text.setText('Overall: preparing…')
        self.stat_eta.setText('ETA: calculating')
        self.stat_elapsed.setText('Elapsed: 0m 00s')
        self.elapsed_timer.start()
        # Mark the job running before launching, so an unclean GUI exit is recoverable.
        self.store.update_job(job_id, 'running')
        self.refresh_jobs()
        self._progress_last_values.clear()
        self._last_paint = 0.0
        self.write_log(f"\n──── Starting job #{job_id}: {job['name']} · {voice_name(job['voice_path'])} ────")
        self.proc.start()
        self.statusBar().showMessage('Chatterbox conversion running…')

    def consume_output(self):
        """Read incremental converter stdout and split control events from readable logs."""
        if self.proc is None:
            return
        incoming = bytes(self.proc.readAllStandardOutput()).decode('utf-8', errors='replace')
        # Normalize CRLF first; do not create a blank line per Windows newline.
        self.buffer += incoming.replace('\r\n', '\n').replace('\r', '\n')
        *lines, self.buffer = self.buffer.split('\n')
        for raw in lines:
            line = raw.strip()
            # Control events are JSON and must not be confused with human-readable logs.
            if line.startswith(EVENT_PREFIX):
                try:
                    self.progress_event(json.loads(line[len(EVENT_PREFIX):]))
                except (ValueError, TypeError, KeyError) as exc:
                    self.write_log(f'Invalid progress event: {exc}')
                continue
            if not line or line.startswith('Sampling:') or 'it/s]' in line:
                continue
            if 'FutureWarning:' in line or 'sdpa' in line or 'self.gen = func' in line:
                continue
            verbose = chunk_source_log(line) or routine_speech_log(line)
            if not verbose:
                self.diagnostic_lines.append(line)
            # Preserve every chunk's text/cache/timing detail in the on-disk
            # diagnostic log. Detailed view shows it via batched Qt updates.
            self.write_log(line, gui=not verbose, detailed=verbose)

    def progress_event(self, event):
        """Interpret JSON progress events and update chapter, total, and export state."""
        kind = event['event']
        if kind == 'plan':
            self.plan = {int(s['index']): s for s in event['sections']}
            self.total_chunks = sum(int(s['chunks']) for s in self.plan.values())
            self.done_chunks = {idx: int(s['chunks']) if s['completed'] else 0 for idx, s in self.plan.items()}
        elif kind == 'chapter_started':
            self.current_section = int(event['index'])
            self.current_total = int(event['chunks'])
            self.current_chunk = 0
        elif kind == 'chunk_done':
            idx = int(event['index'])
            self.current_section = idx
            self.current_chunk = int(event['chunk'])
            self.current_total = int(event['total'])
            # Monotonic accounting prevents duplicate/replayed cache events reversing progress.
            self.done_chunks[idx] = max(self.done_chunks.get(idx, 0), self.current_chunk)
            if event.get('seconds') is not None:
                sample = max(0.001, float(event['seconds']))
                self.recent_chunk_durations.append(sample)
                # Retain the previous generated-only history for compatibility.
                if sample > 1.0:
                    self.chunk_durations.append(sample)
            duration = float(event.get('seconds') or 0)
            source = 'edited take' if event.get('edited') else 'generated/cache'
            self.write_log(f'Section {idx}: chunk {self.current_chunk}/{self.current_total} '
                           f'complete · {duration:.1f}s · {source}',
                           gui=False, detailed=True)
        elif kind == 'chapter_done':
            idx = int(event['index'])
            if idx in self.plan:
                self.done_chunks[idx] = int(self.plan[idx]['chunks'])
            self.current_section = idx
            self.current_chunk = self.current_total = int(event['chunks'])
        elif kind == 'export_started':
            self.exporting = True
            self.chapter_text.setText('Encoding M4B…')
            self.stat_eta.setText('ETA: exporting')
        elif kind == 'export_done':
            self.exporting = False
            self.last_output_path = event['path']
            self.write_log(f"Finished M4B: {self.last_output_path} ({fmt_size(event['size_bytes'])})")
        # Coalesce cached chunk repaint bursts; always paint chapter/export boundaries.
        paint_time = time.monotonic()
        if kind != 'chunk_done' or paint_time - getattr(self, '_last_paint', 0.0) >= 0.5:
            self.paint_progress()
            self._last_paint = paint_time
        if kind in ('plan', 'chapter_started', 'chapter_done', 'export_started', 'export_done'):
            self.persist_progress(force=True)
        elif kind == 'chunk_done':
            self.persist_progress()

    def persist_progress(self, force=False):
        """Rate-limit durable chunk-count writes to avoid excessive SQLite traffic."""
        if self.active_job_id is None or not self.plan:
            return
        done = sum(self.done_chunks.values())
        current_time = time.monotonic()
        # SQLite writes and downstream MySQL updates are intentionally throttled.
        if not force and done-self.last_progress_chunks < 20 and current_time-self.last_progress_save < 15:
            return
        self.store.update_progress(self.active_job_id, self.total_chunks, done,
                                   self.current_section, self.current_chunk, self.current_total)
        self.last_progress_save = current_time
        self.last_progress_chunks = done

    def paint_progress(self):
        """Update changed labels sparingly while all SQLite checkpoints continue."""
        if not self.plan:
            return
        view, eta = progress_snapshot(
            self.plan, self.done_chunks, self.current_section, self.exporting,
            self.chunk_durations,
            recent_chunk_durations=self.recent_chunk_durations)
        if eta is not None:
            view['stat_eta'] = f'ETA: ~{fmt_seconds(eta)} (recent pace) + export'
        elif not self.exporting and sum(self.done_chunks.values()) >= self.total_chunks > 0:
            view['stat_eta'] = 'ETA: finishing chapters / export'
        # Avoid repeatedly entering PySide6 native setters during 90-hour jobs.
        for name, value in view.items():
            if self._progress_last_values.get(name) == value:
                continue
            widget = getattr(self, name)
            if name.endswith('_progress'):
                widget.setValue(value)
            else:
                widget.setText(value)
            self._progress_last_values[name] = value

    def tick(self):
        """Refresh the conversion clock once per second, skipping unchanged text."""
        if self.proc is not None and self.started_at is not None:
            value = f'Elapsed: {fmt_seconds(time.monotonic() - self.started_at)}'
            if self.stat_elapsed.text() != value:
                self.stat_elapsed.setText(value)

    def stop_job(self):
        """Request an orderly stop that retains previously completed chunk audio."""
        self.run_queue = False
        if self.proc is None:
            self.statusBar().showMessage('Queue paused. Start queue when you are ready.')
            return
        if self.stop_requested:
            return
        self.stop_requested = True
        self.store.update_job(self.active_job_id, 'stopping')
        self.refresh_jobs()
        self.write_log('Stop requested. The backend will stop after the current TTS chunk, preserving its cache.')
        self.statusBar().showMessage('Stopping after current chunk…')
        self.proc.terminate()
        QTimer.singleShot(15000, self.offer_force_stop)

    def offer_force_stop(self):
        """Offer process termination if a requested graceful stop is taking too long."""
        if self.proc is None or self.proc.state() == QProcess.NotRunning:
            return
        ask = QMessageBox.question(self, 'Conversion still stopping',
                                   'The current operation has not finished. Force stop the process?\n\n'
                                   'Previous chunk caches and completed WAVs will remain. The current chapter may need rebuilding.',
                                   QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
        if ask == QMessageBox.Yes and self.proc is not None:
            self.proc.kill()

    def process_error(self, error):
        """Persist converter launch failures instead of leaving a phantom running job."""
        if self.proc is None:
            return
        message = f'Process error: {self.proc.errorString()}'
        self.diagnostic_lines.append(message)
        self.write_log(message)
        if error == QProcess.FailedToStart:
            job_id = self.active_job_id
            if job_id is not None:
                self.store.update_job(job_id, 'failed',
                    error=f'Converter could not start: {message}' +
                          (f' · Log: {self.active_job_log}' if self.active_job_log else ''))
            failed_process = self.proc
            self.tick()
            self.flush_generation_logs()
            self.elapsed_timer.stop()
            self.proc = None
            self.active_job_log = None
            self.active_job_id = None
            self.run_queue = False
            failed_process.deleteLater()
            self.active_title.setText('Converter failed to start — see log')
            self.refresh_jobs()
            self.statusBar().showMessage('Converter could not start; queue paused.')

    def process_finished(self, exit_code, exit_status):
        """Verify an export and transition the job to complete, interrupted, or failed."""
        if self.proc is None:
            return
        self.consume_output()
        if self.buffer.strip():
            self.diagnostic_lines.append(self.buffer.strip())
            self.write_log(self.buffer.strip())
        self.buffer = ''
        stopped = self.stop_requested or exit_code == 130
        good = exit_code == 0 and exit_status == QProcess.NormalExit and not stopped
        job_id = self.active_job_id
        job = self.store.job(job_id)
        if good:
            expected = Path(self.last_output_path) if self.last_output_path else None
            # A zero exit code alone is insufficient: the named output must exist.
            if not expected or not expected.is_file() or expected.stat().st_size < 1024 or expected.stem != job['output_name']:
                good = False
                self.write_log('Export verification failed: M4B missing, too small, or incorrect filename.')
        self.persist_progress(force=True)
        if good:
            try:
                self.store.complete_job(job_id, self.last_output_path)
            except Exception as exc:
                good = False
                self.write_log(f'Could not record verified export: {exc}')
        if good:
            self.refresh_saved_parts()
            self.chapter_progress.setValue(1000)
            self.overall_progress.setValue(1000)
            self.overall_text.setText('Overall: 100% · Export verified')
            self.active_title.setText('Finished successfully')
            self.write_log('✓ Conversion and M4B export finished successfully.')
        elif stopped:
            self.store.update_job(job_id, 'interrupted', error='Stopped by user; press Retry selected to resume.')
            self.active_title.setText('Stopped — cached speech retained')
            self.write_log('Job interrupted. Retry this part to continue using cached speech.')
        else:
            hint = diagnostic_hint(self.diagnostic_lines, exit_code)
            detail = hint + (f' · Log: {self.active_job_log}' if self.active_job_log else '')
            self.store.update_job(job_id, 'failed', error=detail)
            self.active_title.setText('Generation failed — see log')
            self.write_log(f'Generation failed (exit code {exit_code}).')
            self.run_queue = False
        self.write_log('Converter exited: ' + ('complete' if good else
            'interrupted' if stopped else f'failed (code {exit_code})'))
        self.active_job_log = None
        self.tick()
        self.flush_generation_logs()
        self.elapsed_timer.stop()
        self.proc.deleteLater()
        self.proc = None
        self.active_job_id = None
        self.stop_requested = False
        self.refresh_jobs()
        self.refresh_review()
        if self.run_queue and good:
            # Schedule the next job after this signal handler unwinds.
            QTimer.singleShot(350, self.start_queue)
        else:
            self.run_queue = False

    def retry_selected(self):
        """Put a failed or interrupted job back into the runnable queue."""
        job_id = self.selected_job_id()
        if job_id is None:
            return
        job = self.store.job(job_id)
        if job_id == self.active_job_id or job['status'] in ('running', 'stopping'):
            return
        if job['status'] == 'complete':
            answer = QMessageBox.question(self, 'Retry completed job?',
                'This job is already complete. Re-running can replace its existing M4B export. Continue?',
                QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
            if answer != QMessageBox.Yes:
                return
        self.store.update_job(job_id, 'queued')
        self.refresh_jobs()
        self.statusBar().showMessage(f'Job #{job_id} requeued. Click Start queue to run.')

    def remove_selected_job(self):
        """Remove the chosen queue record without deleting audiobook output."""
        job_id = self.selected_job_id()
        if job_id is None or job_id == self.active_job_id:
            return
        self.store.remove_job(job_id)
        self.refresh_jobs()
        self.refresh_saved_parts()
        self.refresh_review()
        self.statusBar().showMessage('Job removed from queue. Completed-part history and audio were retained.')

    def clear_finished_jobs(self):
        """Keep the queue compact without losing permanent verified-part metadata."""
        eligible = [row for row in self.store.jobs()
                    if row['status'] == 'complete' and
                    any(part['source_job_id'] == row['id']
                        for part in self.store.completed_parts(row['book_id']))]
        if not eligible:
            QMessageBox.information(self, 'No finished jobs to clear',
                'There are no verified, permanently recorded completed jobs to remove.')
            return
        answer = QMessageBox.question(self, 'Clear finished jobs?',
            f'Remove {len(eligible)} completed job(s) from Queue?\n\n'
            'Recorded part history, completed M4Bs, chapter WAVs, caches, '
            'and pronunciation snapshots will all be kept.',
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
        if answer != QMessageBox.Yes:
            return
        count = self.store.prune_recorded_jobs()
        self.refresh_jobs()
        self.refresh_review()
        self.refresh_saved_parts()
        self.statusBar().showMessage(
            f'Cleared {count} completed queue job(s); permanent history and audio retained.')

    def selected_job_output(self):
        """Locate the M4B path associated with the selected job."""
        job_id = self.selected_job_id()
        return self.store.job(job_id) if job_id is not None else None

    def open_selected_output(self, *_):
        """Open the selected job export with the default desktop player."""
        job = self.selected_job_output()
        if job is None:
            return
        if job['output_path'] and Path(job['output_path']).is_file():
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(job['output_path'])))
        else:
            QMessageBox.information(self, 'M4B not available',
                'The selected job does not have an exported M4B yet. Use Open folder to find its work files.')

    def job_output_folder(self, job):
        """Determine a safe folder to open for a job and its voice-specific output."""
        if job['output_path'] and Path(job['output_path']).parent.is_dir():
            return Path(job['output_path']).parent
        self.converter = self.converter or load_converter()
        return self.converter.book_folder(Path(job['book_path']))

    def open_selected_output_folder(self):
        """Show the selected job output directory in the file manager."""
        job = self.selected_job_output()
        if job is None:
            return
        folder = self.job_output_folder(job)
        if folder.is_dir():
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(folder)))
        else:
            QMessageBox.information(self, 'Folder unavailable',
                'This audiobook has not created an output directory yet.')

    def copy_selected_output_path(self):
        """Copy the export filename for external use."""
        job = self.selected_job_output()
        if job is not None:
            path = job['output_path'] or str(self.job_output_folder(job))
            QApplication.clipboard().setText(path)
            self.statusBar().showMessage('Output path copied to clipboard.')

    def review_selected_book_id(self):
        """Resolve the book selected for the Review Audio page."""
        return self.review_book.currentData()

    def refresh_review(self, *_):
        """Populate generated M4Bs and chapter WAVs available for review."""
        if not hasattr(self, 'review_book'):
            return
        book_id = self.review_selected_book_id()
        if book_id is None:
            self.review_table.setRowCount(0)
            self.export_table.setRowCount(0)
            return
        book = self.store.book(book_id)
        if not book:
            return
        manifest_paths = []
        try:
            self.converter = self.converter or load_converter()
            folder = self.converter.book_folder(Path(book['path']))
            manifest_paths = [folder / 'manifest.json', *sorted((folder / 'voices').glob('*/manifest.json'))]
            rows = []
            for manifest_path in manifest_paths:
                if not manifest_path.is_file():
                    continue
                manifest = json.loads(manifest_path.read_text())
                label = voice_name(manifest.get('voice_path'))
                for key, chapter in manifest.get('chapters', {}).items():
                    rows.append((key, label, chapter))
        except Exception:
            rows = []
        rows.sort(key=lambda entry: (int(entry[0]), entry[1]))
        self.review_chapter_rows = rows
        self.review_table.setRowCount(len(rows))
        for r, (key, narrator_label, chapter) in enumerate(rows):
            wav = Path(chapter.get('wav', ''))
            secs = chapter.get('samples', 0)/max(1, chapter.get('sample_rate', 1))
            fields = (key, chapter.get('title',''), narrator_label, fmt_seconds(secs), 'Ready' if wav.is_file() else 'Missing')
            for c, value in enumerate(fields):
                self.review_table.setItem(r, c, item(value, str(wav) if c == 0 else None))
        jobs = [job for job in self.store.jobs() if job['book_id'] == book_id
                and job['output_path']]
        exports = [(job['name'], Path(job['output_path'])) for job in jobs]
        known = {str(path.resolve()) for _label, path in exports}
        # Completed-parts ledger is independent of queue cleanup.
        for part in self.store.completed_parts(book_id):
            path = Path(part['output_path'])
            if str(path.resolve()) in known:
                continue
            exports.append((f"Part {part['part_number']} · archived", path))
            known.add(str(path.resolve()))
        # Complete exports are archival files, not queue jobs. Discover them
        # in the same narrator-specific directories as their original parts.
        for manifest_path in manifest_paths:
            if manifest_path.is_file():
                for path in sorted(manifest_path.parent.glob('*_COMPLETE.m4b')):
                    if str(path.resolve()) not in known:
                        exports.append(('Complete audiobook · ' + path.stem, path))
                        known.add(str(path.resolve()))
        self.export_table.setRowCount(len(exports))
        for r, (label, path) in enumerate(exports):
            exists = path.is_file()
            details = (label, 'Ready' if exists else 'Missing',
                       fmt_size(path.stat().st_size) if exists else '—')
            for c, value in enumerate(details):
                self.export_table.setItem(r, c, item(value, str(path) if c == 0 else None))

    def export_complete_book(self):
        """Create a single archival M4B from finished queue parts, without TTS.

        All parts must be complete, and every section must have an assembled
        chapter WAV. The narrator grouping uses the converter's actual output
        path, so WAVs from different speakers cannot be mixed accidentally.
        The export runs in its own process to keep Qt responsive.
        """
        if self.proc is not None or self.run_queue or self.full_export_proc is not None:
            QMessageBox.information(self, 'Export unavailable',
                'Finish or pause the active conversion/export before making a complete book.')
            return
        editor = getattr(self, 'editor_dialog', None)
        if editor is not None and editor.isVisible() and editor.proc is not None:
            QMessageBox.information(self, 'Audio editor busy',
                'Finish rebuilding audio in the editor before exporting a complete book.')
            return
        book_id = self.review_selected_book_id()
        if book_id is None:
            QMessageBox.information(self, 'Choose a book', 'Select a book in Review audio first.')
            return
        book = self.store.book(book_id)
        if not book:
            return
        epub = Path(book['path'])
        if not epub.is_file():
            self.error('EPUB unavailable', f'The source EPUB no longer exists: {epub}')
            return
        try:
            conv = self.converter or load_converter()
            self.converter = conv
            # Each job keeps its original narrator snapshot. Resolve folders
            # with that path instead of the currently selected Generate voice.
            history = [dict(row) for row in self.store.jobs()]
            history.extend(self.store.recorded_jobs_for_export(book_id))
            groups = collect_export_groups(history, book_id, conv.book_folder)
            if not groups:
                raise ValueError('No completed or queued parts exist for this book.')
            if len(groups) > 1:
                choices = [f"{voice_name(g['voice_path'])} — "
                           f"{len(g['parts'])} part(s), {len(g['indices'])} sections, "
                           f"{len(g['unfinished'])} unfinished" for g in groups]
                selected, accepted = QInputDialog.getItem(
                    self, 'Choose narrator', 'Combine parts recorded with:', choices, 0, False)
                if not accepted:
                    return
                group = groups[choices.index(selected)]
            else:
                group = groups[0]
            indices = verify_export_group(group, int(book['sections']))
            # Reuse the converter's filename sanitization; put the final book
            # alongside its existing parts in the narrator-specific folder.
            output_name = conv.safe_name(book['title'], 100) + '_COMPLETE'
            output = group['folder'] / (output_name + '.m4b')
            coverage_note = export_coverage_message(indices, int(book['sections']))
            overwrite = '\nAn existing complete M4B will be replaced after verification.' if output.exists() else ''
            quality_note = quality_preflight(group['folder'], indices)
            prompt = (f"Combine {len(group['parts'])} finished part(s) into one M4B?\n\n"
                      f"Narrator: {voice_name(group['voice_path'])}\n"
                      f"Chapters: {len(indices)} (EPUB sections {indices[0]}–{indices[-1]})\n"
                      f"{coverage_note}\n"
                      f"Encoding: AAC 64 kbps mono, with chapter markers\n\n"
                      f"Output: {output}\n\n"
                      f"Quality check: {quality_note}\n\n"
                      'All chapter WAVs and individual part M4Bs will remain unchanged.' + overwrite)
            if QMessageBox.question(self, 'Export complete audiobook', prompt,
                                    QMessageBox.Yes | QMessageBox.No, QMessageBox.No) != QMessageBox.Yes:
                return
        except Exception as exc:
            self.error('Cannot export complete audiobook', exc)
            return

        arguments = ['-u', str(CONVERTER_FILE), 'export', str(epub),
                     '--chapters', ','.join(map(str, indices)),
                     '--output-name', output_name]
        if group['voice_path']:
            arguments += ['--voice', str(group['voice_path'])]
        self.full_export_target = output
        self.full_export_proc = QProcess(self)
        proc = self.full_export_proc
        proc.setProgram(sys.executable)
        proc.setArguments(arguments)
        proc.setWorkingDirectory(str(ROOT))
        proc.setProcessChannelMode(QProcess.MergedChannels)
        proc.readyReadStandardOutput.connect(self.read_complete_export_output)
        proc.finished.connect(self.complete_export_finished)
        proc.errorOccurred.connect(self.complete_export_error)
        self.statusBar().showMessage(f'Encoding complete audiobook: {output.name}')
        self.write_log(f'Complete audiobook export started: {output}')
        proc.start()


    def read_complete_export_output(self):
        """Capture short FFmpeg/converter output without refreshing Qt tables."""
        if self.full_export_proc is not None:
            message = bytes(self.full_export_proc.readAllStandardOutput()).decode('utf-8', errors='replace')
            if message.strip():
                self.write_log(message.rstrip())


    def complete_export_error(self, error):
        """Report process errors, including a child that never started."""
        proc = self.full_export_proc
        if proc is None:
            return
        self.statusBar().showMessage('Complete M4B export encountered a process error')
        self.write_log('Complete export process error: ' + proc.errorString())
        if error == QProcess.FailedToStart:
            # Qt does not guarantee a finished signal when a process fails to
            # launch, so release it here rather than leaving export blocked.
            self.full_export_proc = None
            self.full_export_target = None
            proc.deleteLater()


    def complete_export_finished(self, exit_code, exit_status):
        """Release the background exporter and display the new archival M4B."""
        proc = self.full_export_proc
        if proc is None:
            return
        self.read_complete_export_output()
        target = self.full_export_target
        good = (exit_code == 0 and exit_status == QProcess.NormalExit
                and target is not None and target.is_file())
        if good:
            self.statusBar().showMessage(f'Complete audiobook ready: {target}')
            self.write_log(f'Complete M4B saved: {target}')
        else:
            self.statusBar().showMessage('Complete audiobook export failed; original parts were preserved')
            self.write_log(f'Complete M4B export failed (exit code {exit_code}); see logs in Queue.')
            QMessageBox.warning(self, 'Complete export failed',
                'The complete audiobook could not be exported.\n'
                'Your chapter WAVs and existing M4B parts are unchanged.\n'
                'Open Queue to inspect the converter log.')
        self.full_export_proc = None
        self.full_export_target = None
        proc.deleteLater()
        self.refresh_review()


    def selected_export_path(self):
        """Return the export path selected in the review table."""
        row = self.export_table.currentRow()
        obj = self.export_table.item(row, 0) if row >= 0 else None
        return Path(obj.data(Qt.UserRole)) if obj is not None else None

    def play_selected_export(self, *_):
        """Play the selected completed audiobook part."""
        path = self.selected_export_path()
        if path is not None and path.is_file():
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(path)))
        else:
            QMessageBox.information(self, 'M4B not available', 'Select an exported M4B that exists on disk.')

    def open_selected_export_folder(self):
        """Open the containing folder for the selected M4B."""
        path = self.selected_export_path()
        if path is not None and path.parent.is_dir():
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(path.parent)))
        else:
            QMessageBox.information(self, 'Output folder unavailable', 'Select a completed export.')

    def play_selected_chapter(self):
        """Play a chapter WAV in the default desktop application."""
        row = self.review_table.currentRow()
        obj = self.review_table.item(row, 0) if row >= 0 else None
        if obj is None:
            return
        path = Path(obj.data(Qt.UserRole))
        if path.is_file():
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(path)))
        else:
            QMessageBox.information(self, 'Audio not available', 'The chapter WAV is not on disk.')

    def open_current_folder(self):
        """Open the current review book’s output folder."""
        book_id = self.review_selected_book_id()
        if book_id is None:
            return
        book = self.store.book(book_id)
        self.converter = self.converter or load_converter()
        row = self.review_table.currentRow()
        obj = self.review_table.item(row, 0) if row >= 0 else None
        wav = Path(obj.data(Qt.UserRole)) if obj is not None else None
        folder = wav.parent if wav is not None and wav.is_file() else self.converter.book_folder(Path(book['path']))
        folder.mkdir(parents=True, exist_ok=True)
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(folder)))

    def open_chunk_editor(self):
        """Open chunk-level take review for the selected completed chapter."""
        existing = getattr(self, 'editor_dialog', None)
        if existing is not None and existing.isVisible():
            existing.raise_()
            existing.activateWindow()
            return
        if self.proc is not None:
            QMessageBox.information(self, 'Conversion in progress',
                'Finish or pause the current conversion before editing chapter WAVs. '
                'This prevents overwriting audio that is still being generated.')
            return
        row = self.review_table.currentRow()
        record = self.review_table.item(row, 0) if row >= 0 else None
        if record is None:
            QMessageBox.information(self, 'Select a chapter', 'Choose a chapter from Review audio first.')
            return
        path = Path(str(record.data(Qt.UserRole)))
        if not path.is_file() or path.parent.name != 'chapters':
            QMessageBox.information(self, 'Audio not available',
                'Finish generating a chapter WAV before opening its chunk editor.')
            return
        book_id = self.review_selected_book_id()
        book = self.store.book(book_id)
        if not book:
            return
        folder = path.parent.parent
        chapter = int(path.stem)
        manifest_path = folder / 'manifest.json'
        try:
            manifest = json.loads(manifest_path.read_text(encoding='utf-8'))
        except (OSError, ValueError) as exc:
            self.error('No chapter manifest found', exc)
            return
        voice = manifest.get('voice_path')
        jobs = [dict(job) for job in self.store.jobs()
                if job['book_id'] == book_id and job['status'] == 'complete']
        self.editor_dialog = ChunkEditor(book['path'], folder, chapter, voice, jobs, self)
        if getattr(self, 'reader_active_identity', None) is not None:
            self.reader_save_progress()
            self.reader_pause_for_transition()
        self.editor_dialog.finished.connect(
            lambda _result: (self.refresh_review(), self.reader_after_editor(folder, chapter)))
        self.editor_dialog.show()

    def preview_cache_cleanup(self):
        """Estimate files and bytes eligible for optional raw cache cleanup."""
        days = self.cleanup_age.currentData()
        try:
            count, size = cache_summary(days)
            suffix = f'older than {days} days' if days is not None else '(entire raw cache)'
            self.cleanup_info.setText(
                f'{count:,} raw chunk WAV files eligible {suffix}, freeing up to {fmt_size(size)}. '
                'Chapter WAVs and M4B exports remain untouched. '
                'Incomplete chapters may need fresh generation if their raw cache is deleted.')
        except Exception as exc:
            self.error('Cache preview error', exc)

    def clean_cache(self):
        """Confirm and delete only eligible raw cache files—not edited takes or exports."""
        if self.proc is not None or (getattr(self, 'editor_dialog', None) is not None
                                     and self.editor_dialog.isVisible()):
            QMessageBox.information(self, 'Generation in progress',
                'Stop all generation or audio rebuilding before deleting raw cache files.')
            return
        days = self.cleanup_age.currentData()
        try:
            count, size = cache_summary(days)
            if not count:
                self.cleanup_info.setText('No raw cache WAV files match that age filter.')
                return
            scope = f'older than {days} days' if days is not None else 'of all ages'
            question = (f'Permanently delete {count:,} RAW speech WAVs ({fmt_size(size)}) {scope}?\n\n'
                        'This does NOT delete your finished audiobooks, original chapter WAVs, '
                        'narrator samples, edited takes, or the MySQL/SQLite databases.\n\n'
                        'However, the deleted chunks cannot be used for offline chapter rebuilding '
                        'and may need to be regenerated with the GPU. This cannot be undone.')
            if QMessageBox.warning(self, 'Confirm raw cache deletion', question,
                QMessageBox.Yes | QMessageBox.No, QMessageBox.No) != QMessageBox.Yes:
                return
            removed, freed = remove_raw_cache(days, expected_count=count)
            self.cleanup_info.setText(f'Deleted {removed:,} raw cached WAV files ({fmt_size(freed)}). '
                                      'Completed audiobook exports and edited takes were preserved.')
            self.calculate_cache_size()
        except Exception as exc:
            self.error('Cache cleanup error', exc)

    def refresh_settings(self):
        """Update settings summaries to reflect current on-disk state."""
        self.update_mysql_status()

    def calculate_cache_size(self):
        """Measure the raw narration cache size without deleting files."""
        try:
            folder = ROOT / 'output' / 'narration_cache_v2'
            size = size_on_disk(folder)
            self.space_info.setText(f'Raw chunk cache: {fmt_size(size)} · Stored in {folder}')
        except Exception as exc:
            self.error('Unable to calculate cache size', exc)

    def open_cache_folder(self):
        """Show the raw WAV cache folder in the file manager."""
        folder = ROOT / 'output' / 'narration_cache_v2'
        folder.mkdir(parents=True, exist_ok=True)
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(folder)))

    def closeEvent(self, event):
        """Remember window state and ask for a safe shutdown if generation is active."""
        if self.quality_proc is not None:
            QMessageBox.information(self, 'Quality scan running',
                'Stop the quality scan before closing AudiobookAI.')
            event.ignore()
            return
        editor = getattr(self, 'editor_dialog', None)
        if editor is not None and editor.isVisible() and editor.proc is not None:
            QMessageBox.information(self, 'Audio editor busy',
                'Finish the chunk editing operation before closing AudiobookAI.')
            event.ignore()
            return
        if self.archive_proc is not None:
            QMessageBox.information(self, 'Archive export in progress',
                'Finish the archival M4B export before closing AudiobookAI.')
            event.ignore()
            return
        if self.full_export_proc is not None:
            QMessageBox.information(self, 'Complete audiobook export in progress',
                'Finish exporting the complete M4B before closing AudiobookAI.')
            event.ignore()
            return
        if self.proc is not None:
            QMessageBox.information(self, 'Conversion in progress',
                'Stop the current conversion first, then close the application. All completed cached chunks will remain.')
            event.ignore()
        else:
            self.reader_save_progress()
            self.prefs.setValue('geometry', self.saveGeometry())
            self.prefs.setValue('batch_size', self.batch_size.value())
            self.prefs.setValue('split_parts', self.split_parts.isChecked())
            self.prefs.setValue('autorun', self.autorun.isChecked())
            if self.mysql_mirror:
                self.mysql_mirror.disconnect()
            self.store.close()
            event.accept()


STYLE = '''
QFrame#sidebar { background-color: palette(alternate-base); border-right: 1px solid palette(mid); }
QPushButton#navButton { text-align: left; padding: 11px 15px; border-radius: 7px; border: 0; }
QPushButton#navButton:checked { background: palette(highlight); color: palette(highlighted-text); font-weight: 600; }
QPushButton#navButton:hover:!checked { background: palette(midlight); }
QLabel#muted { color: %%MUTED_COLOR%%; }
QTableWidget { gridline-color: palette(midlight); }
QTableWidget::item { padding: 5px; }
QPushButton { padding: 6px 10px; }
QProgressBar { text-align: center; min-height: 17px; border-radius: 4px; }
'''


def main():
    """Construct the Qt application and run its event loop."""
    app = QApplication(sys.argv)
    app.setApplicationName('AudiobookAI Studio')
    if headless_runner_active():
        QMessageBox.warning(None, 'Headless audiobook conversion running',
            'An audiobook job is running without Qt. Do not open Studio until it finishes; '
            'opening the queue could mislabel its running job as interrupted.\n\n'
            'Check progress in your headless terminal or saved diagnostic log.')
        return 2
    # `palette(mid)` is nearly invisible on some KDE dark themes. Mix the
    # foreground with the window color for accessible secondary text.
    foreground = app.palette().color(QPalette.WindowText)
    background = app.palette().color(QPalette.Window)
    muted = [round(getattr(foreground, c)() * .80 + getattr(background, c)() * .20)
             for c in ('red', 'green', 'blue')]
    app.setStyleSheet(STYLE.replace('%%MUTED_COLOR%%', '#%02x%02x%02x' % tuple(muted)))
    window = Studio()
    window.show()
    if len(sys.argv) > 1:
        path = Path(sys.argv[1]).expanduser()
        if path.is_file() and path.suffix.lower() == '.epub':
            QTimer.singleShot(0, lambda: window.import_book(path))
    sys.exit(app.exec())


if __name__ == '__main__':
    main()
