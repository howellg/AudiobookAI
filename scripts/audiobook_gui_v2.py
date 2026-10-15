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
from __future__ import annotations

import importlib.util
import json
import re
import sys
import time
import uuid
from collections import deque
from pathlib import Path

from PySide6.QtCore import Qt, QProcess, QTimer, QUrl, QSettings
from PySide6.QtGui import QDesktopServices, QFont, QKeySequence, QShortcut, QPalette
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QComboBox, QFrame, QGridLayout, QHBoxLayout,
    QHeaderView, QLabel, QMainWindow, QMessageBox, QFileDialog, QPlainTextEdit,
    QProgressBar, QPushButton, QSpinBox, QSplitter, QStackedWidget, QTableWidget,
    QTableWidgetItem, QVBoxLayout, QWidget, QAbstractItemView, QLineEdit, QScrollArea,
)

from audiobookai_storage import (Store, read_config, write_config,
                                  lookup_password, save_password)
from audiobookai_mysql_proxy import MySQLMirror
from audiobookai_convenience import parse_section_ranges, job_category, estimate_audio_duration
from audiobookai_voices import (DEFAULT_VOICE, VOICE_DIR, SNAPSHOT_DIR, voice_choices,
                                import_voice, freeze_voice, voice_name)
from audiobookai_production import cache_summary, remove_raw_cache
from audiobookai_editor import ChunkEditor

SCRIPTS = Path(__file__).resolve().parent
ROOT = SCRIPTS.parent
CONVERTER_FILE = SCRIPTS / 'epub_to_audiobook_v7_studio.py'
DB_FILE = ROOT / 'output' / 'audiobookai_studio.sqlite3'
STORY_RE = re.compile(r'^\s*(?:chapter|chap\.?|book|part)\s+(?:\d+|[IVXLCDM]+)(?:\b|[.:—–-])', re.I)
EVENT_PREFIX = 'AUDIOBOOKAI_EVENT '


def fmt_seconds(seconds):
    if seconds is None:
        return '—'
    secs = max(0, int(seconds))
    hours, rem = divmod(secs, 3600)
    mins, sec = divmod(rem, 60)
    return f'{hours}h {mins:02}m' if hours else f'{mins}m {sec:02}s'


def fmt_size(n):
    n = float(n)
    for unit in ('B', 'KiB', 'MiB', 'GiB', 'TiB'):
        if n < 1024 or unit == 'TiB':
            return f'{n:.1f} {unit}'
        n /= 1024


def size_on_disk(folder):
    if not folder.is_dir():
        return 0
    return sum(p.stat().st_size for p in folder.rglob('*') if p.is_file())


def load_converter():
    if not CONVERTER_FILE.is_file():
        raise FileNotFoundError(f'Missing backend: {CONVERTER_FILE}')
    spec = importlib.util.spec_from_file_location('audiobookai_v7_studio_backend', CONVERTER_FILE)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def title_label(text, size=20):
    label = QLabel(text)
    font = QFont()
    font.setPointSize(size)
    font.setBold(True)
    label.setFont(font)
    return label


def mk_button(label, handler, tooltip=None):
    btn = QPushButton(label)
    btn.clicked.connect(handler)
    if tooltip:
        btn.setToolTip(tooltip)
    return btn


def item(text, data=None, checked=None):
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
    def __init__(self):
        super().__init__()
        self.setWindowTitle('AudiobookAI Studio — V2.4 · Production tools')
        self.resize(1330, 900)
        self.prefs = QSettings('AudiobookAI', 'Studio')
        self.store = Store(DB_FILE)
        self.store.on_change = self.mark_mysql_dirty
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
        self.proc = None
        self.active_job_id = None
        self.stop_requested = False
        self.run_queue = False
        self.buffer = ''
        self.plan = {}
        self.done_chunks = {}
        self.total_chunks = 0
        self.chunk_durations = deque(maxlen=40)
        self.current_section = None
        self.current_chunk = 0
        self.current_total = 0
        self.started_at = None
        self.last_output_path = None
        self.exporting = False
        self.review_manifest = {}

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
        version = QLabel('STUDIO  /  V2.4 · V7 SMOOTH')
        version.setObjectName('muted')
        nav.addWidget(version)
        nav.addSpacing(20)
        self.nav = []
        for i, name in enumerate(['Library', 'Generate', 'Queue', 'Review audio', 'Settings']):
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
        self.monitor = QTimer(self)
        # # self.monitor.timeout.connect(self.tick)  # crash isolation  # crash isolation
        self.monitor.start(1000)
        self.mirror_timer = QTimer(self)
        self.mirror_timer.setSingleShot(True)
        self.mirror_timer.timeout.connect(self.maybe_sync_mysql)
        self.reconnect_timer = QTimer(self)
        self.reconnect_timer.timeout.connect(self.maybe_sync_mysql)
        self.reconnect_timer.start(60000)
        QTimer.singleShot(500, self.connect_saved_mysql)

    def page_base(self, heading, intro, scroll=False):
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
        self.stack.setCurrentIndex(index)
        self.prefs.setValue('last_page', index)
        for i, button in enumerate(self.nav):
            button.setChecked(i == index)
        if index == 3:
            self.refresh_review()
        if index == 4:
            self.refresh_settings()

    def make_library_page(self):
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
        selected = self.voice_combo.currentData()
        return Path(selected) if selected else DEFAULT_VOICE

    def voice_changed(self, *_):
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
        saved = self.prefs.value(f'voice_for_book/{book_id}', '')
        index = self.voice_combo.findData(str(saved)) if saved else 0
        self.voice_combo.blockSignals(True)
        self.voice_combo.setCurrentIndex(index if index >= 0 else 0)
        self.voice_combo.blockSignals(False)
        self.voice_changed()

    def import_narrator(self):
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
        voice = self.selected_narrator()
        if not voice.is_file():
            self.error('Narrator sample missing', f'File not found: {voice}')
            return
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(voice)))

    def open_voices_folder(self):
        VOICE_DIR.mkdir(parents=True, exist_ok=True)
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(VOICE_DIR)))

    def change_queued_narrator(self):
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
        l = self.page_base('Conversion queue', 'Jobs and progress persist locally and sync to MySQL when connected. Interrupted jobs reuse V7 chunk caches.')
        bar = QHBoxLayout()
        bar.addWidget(mk_button('▶ Start queue', self.start_queue))
        bar.addWidget(mk_button('Run selected', self.run_selected_job,
            'Generate just the selected queued part, then pause the queue'))
        bar.addWidget(mk_button('Pause after part', self.pause_after_part,
            'Finish the active M4B part without starting the next one'))
        bar.addWidget(mk_button('■ Stop safely', self.stop_job))
        bar.addWidget(mk_button('↻ Retry selected', self.retry_selected))
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
        logbar.addWidget(mk_button('Copy log', self.copy_log))
        logbar.addWidget(mk_button('Clear view', self.clear_log))
        l.addLayout(logbar)
        self.logs = QPlainTextEdit()
        self.logs.setReadOnly(True)
        self.logs.setMaximumBlockCount(1500)
        l.addWidget(self.logs, 1)

    def make_review_page(self):
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
        export_actions.addStretch()
        l.addLayout(export_actions)
        note = QLabel('Narration preset: V7 Smooth — 18-word ceiling, 350-token cap, exaggeration 0.35. Narrators have isolated chapter audio and cached speech.')
        note.setWordWrap(True)
        note.setObjectName('muted')
        l.addWidget(note)

    def make_settings_page(self):
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
        l.addStretch()

    def mark_mysql_dirty(self):
        self.mysql_sync_pending = True
        if self.mysql_mirror is not None:
            self.mysql_mirror.dirty = True
        if hasattr(self, 'mirror_timer') and self.mysql_cfg.get('enabled'):
            self.mirror_timer.start(1800)

    def mysql_form_config(self):
        return {'enabled': True, 'host': self.mysql_host.text().strip(),
                'port': self.mysql_port.value(),
                'database': self.mysql_database.text().strip(),
                'user': self.mysql_user.text().strip(),
                'use_keyring': self.mysql_remember.isChecked()}

    def update_mysql_status(self, explanation=None):
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
        if self.mysql_cfg.get('enabled') and self.mysql_password is not None:
            self.maybe_sync_mysql(force=False)
        else:
            self.update_mysql_status()

    def connect_mysql_from_settings(self):
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
        table.setAlternatingRowColors(True)
        table.setSelectionBehavior(QAbstractItemView.SelectRows)
        table.setSelectionMode(QAbstractItemView.SingleSelection)
        table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        table.verticalHeader().setVisible(False)
        table.horizontalHeader().setStretchLastSection(True)

    def error(self, title, message):
        QMessageBox.critical(self, title, str(message))
        self.write_log(f'ERROR: {message}')

    def write_log(self, text):
        # QPlainTextEdit maintains the scroll position automatically when
        # appending; avoid retaining a transient QScrollBar wrapper after
        # appendPlainText() causes internal Qt layout/scrollbar changes.
        self.logs.appendPlainText(str(text))

    def focus_search(self):
        page = self.stack.currentIndex()
        if page == 0:
            self.library_search.setFocus()
        elif page == 1:
            self.chapter_search.setFocus()
        elif page == 2:
            self.queue_search.setFocus()

    def dragEnterEvent(self, event):
        urls = event.mimeData().urls() if event.mimeData().hasUrls() else []
        if any(url.isLocalFile() and url.toLocalFile().lower().endswith('.epub') for url in urls):
            event.acceptProposedAction()
        else:
            event.ignore()

    def dropEvent(self, event):
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
        query = self.chapter_search.text().strip().casefold()
        for row in range(self.section_table.rowCount()):
            index = self.section_table.item(row, 0).text()
            title = self.section_table.item(row, 1).text().casefold()
            visible = not query or query in title or query == index
            self.section_table.setRowHidden(row, not visible)

    def select_visible(self):
        visible = {sec.index for row, sec in enumerate(self.sections)
                   if not self.section_table.isRowHidden(row)}
        self.set_checks(lambda index: index in visible)

    def select_range(self):
        try:
            selection = set(parse_section_ranges(self.range_input.text(),
                                                [section.index for section in self.sections]))
        except ValueError as exc:
            QMessageBox.warning(self, 'Invalid chapter range', str(exc))
            return
        self.set_checks(lambda index: index in selection)
        self.statusBar().showMessage(f'Selected {len(selection)} EPUB sections from your range.')

    def queue_from_shortcut(self):
        if self.stack.currentIndex() == 1:
            self.queue_selection()

    def filter_queue(self, *_):
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
        text = self.logs.toPlainText()
        if text:
            QApplication.clipboard().setText(text)
            self.statusBar().showMessage('Conversion messages copied to clipboard.')

    def clear_log(self):
        self.logs.clear()
        self.statusBar().showMessage('Log display cleared. Conversion progress and saved jobs are unchanged.')

    def import_dialog(self):
        start = ROOT / 'books'
        if not start.exists():
            start = Path.home()
        path, _ = QFileDialog.getOpenFileName(self, 'Import EPUB', str(start), 'EPUB books (*.epub)')
        if path:
            self.import_book(Path(path))

    def import_book(self, path):
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
        if books:
            self.book_combo_changed()
        else:
            self.section_table.setRowCount(0)
            self.gen_details.setText('Import an EPUB to begin.')

    def current_library_book(self):
        r = self.library_table.currentRow()
        first = self.library_table.item(r, 0) if r >= 0 else None
        return first.data(Qt.UserRole) if first else None

    def generate_selected_book(self, *_):
        book_id = self.current_library_book()
        if book_id is None:
            return
        self.select_book(book_id)
        self.change_page(1)

    def remove_selected_book(self):
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
        idx = self.gen_book.findData(book_id)
        if idx >= 0:
            self.gen_book.setCurrentIndex(idx)
        self.book_combo_changed()

    def book_combo_changed(self, *_):
        book_id = self.gen_book.currentData()
        if book_id is None:
            return
        if book_id not in self.book_cache:
            book = self.store.book(book_id)
            try:
                if not Path(book['path']).is_file():
                    raise FileNotFoundError(f"EPUB moved or missing: {book['path']}")
                self.converter = self.converter or load_converter()
                self.book_cache[book_id] = self.converter.load_epub(Path(book['path']))
            except Exception as exc:
                self.error('Unable to open book', exc)
                return
        self.current_book_id = book_id
        self.prefs.setValue('last_book_id', book_id)
        title, author, self.sections = self.book_cache[book_id]
        self.store.populate_missing_sections(book_id, self.sections)
        words = sum(len(s.text.split()) for s in self.sections)
        self.gen_details.setText(f'{title} · {author} · {len(self.sections)} sections · {words:,} words')
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

    def set_checks(self, predicate):
        self.section_table.blockSignals(True)
        for r, sec in enumerate(self.sections):
            self.section_table.item(r, 0).setCheckState(Qt.Checked if predicate(sec.index) else Qt.Unchecked)
        self.section_table.blockSignals(False)
        self.update_selected_count()

    def select_story(self):
        self.set_checks(lambda idx: bool(STORY_RE.match(self.sections[idx-1].title)))

    def select_next_unqueued(self):
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
        return [self.section_table.item(r, 0).data(Qt.UserRole)
                for r in range(self.section_table.rowCount())
                if self.section_table.item(r, 0).checkState() == Qt.Checked]

    def update_selected_count(self, *_):
        if not self.sections:
            self.gen_selected.setText('0 selected')
            return
        nums = self.selected_indices()
        words = sum(len(self.sections[n-1].text.split()) for n in nums)
        hours = estimate_audio_duration(words) / 3600
        self.gen_selected.setText(f'{len(nums)} selected sections · {words:,} words · ~{hours:.1f} h finished audio (estimate, not GPU time)')

    def queue_selection(self):
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
        for i, numbers in enumerate(parts, 1):
            span = f'{numbers[0]}-{numbers[-1]}' if len(numbers) > 1 else str(numbers[0])
            # Generate the unique output basename BEFORE writing the job, so a
            # sudden restart cannot leave a job pointing to an old sample file.
            name = f'Part {i:02d} · sections {span}' if len(parts) > 1 else f'Sections {span}'
            suffix = uuid.uuid4().hex[:12]
            output_base = self.converter.safe_name(f"{book['title']}_part_{i:02d}_sections_{span}_{suffix}", 125)
            self.store.add_job(self.current_book_id, name, numbers, output_base, fixed_voice)
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
        r = self.job_table.currentRow()
        first = self.job_table.item(r, 0) if r >= 0 else None
        return first.data(Qt.UserRole) if first else None

    def start_queue(self):
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
        self.run_queue = False
        if self.proc is not None:
            self.statusBar().showMessage('Will finish this M4B part, then pause before the next one.')
            self.write_log('Queue set to pause after the current part finishes.')
        else:
            self.statusBar().showMessage('Queue paused. No part is currently running.')

    def start_job(self, job_id):
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
        if job['voice_path']:
            voice = Path(job['voice_path'])
            if not voice.is_file():
                self.store.update_job(job_id, 'failed', error='Queued narrator WAV is missing')
                self.refresh_jobs()
                self.run_queue = False
                self.error('Missing narrator WAV', f'The saved narrator for this part is missing: {voice}')
                return
            arguments.extend(['--voice', str(voice)])
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
        self.chapter_progress.setValue(0)
        self.overall_progress.setValue(0)
        self.active_title.setText(f"Generating: {job['book_title']} — {job['name']}")
        self.chapter_text.setText('Preparing chapter plan…')
        self.overall_text.setText('Overall: preparing…')
        self.stat_eta.setText('ETA: calculating')
        self.store.update_job(job_id, 'running')
        self.refresh_jobs()
        self.write_log(f"\n──── Starting job #{job_id}: {job['name']} · {voice_name(job['voice_path'])} ────")
        self.proc.start()
        self.statusBar().showMessage('Chatterbox conversion running…')

    def consume_output(self):
        if self.proc is None:
            return
        incoming = bytes(self.proc.readAllStandardOutput()).decode('utf-8', errors='replace')
        self.buffer += incoming.replace('\r', '\n')
        *lines, self.buffer = self.buffer.split('\n')
        for raw in lines:
            line = raw.strip()
            if line.startswith(EVENT_PREFIX):
                try:
                    self.progress_event(json.loads(line[len(EVENT_PREFIX):]))
                except (ValueError, TypeError, KeyError) as exc:
                    self.write_log(f'Invalid progress event: {exc}')
                continue
            if not line or line.startswith('Sampling:') or 'it/s]' in line:
                continue
            # Avoid thousands of text lines in the GUI; individual speech chunks
            # remain visible in terminal logs if the backend is run directly.
            if re.match(r'^\d+/\d+: ', line):
                continue
            if 'FutureWarning:' in line or 'sdpa' in line or 'self.gen = func' in line:
                continue
            self.write_log(line)

    def progress_event(self, event):
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
            self.done_chunks[idx] = max(self.done_chunks.get(idx, 0), self.current_chunk)
            if event.get('seconds') and float(event['seconds']) > 1.0:
                self.chunk_durations.append(float(event['seconds']))
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
        if kind != 'chunk_done' or paint_time - getattr(self, '_last_paint', 0.0) >= 0.4:
            self.paint_progress()
            self._last_paint = paint_time
        if kind in ('plan', 'chapter_started', 'chapter_done', 'export_started', 'export_done'):
            self.persist_progress(force=True)
        elif kind == 'chunk_done':
            self.persist_progress()

    def persist_progress(self, force=False):
        if self.active_job_id is None or not self.plan:
            return
        done = sum(self.done_chunks.values())
        current_time = time.monotonic()
        if not force and done-self.last_progress_chunks < 20 and current_time-self.last_progress_save < 15:
            return
        self.store.update_progress(self.active_job_id, self.total_chunks, done,
                                   self.current_section, self.current_chunk, self.current_total)
        self.last_progress_save = current_time
        self.last_progress_chunks = done

    def paint_progress(self):
        if not self.plan:
            return
        done = sum(self.done_chunks.values())
        total = max(1, self.total_chunks)
        ratio = done / total
        # 98% covers TTS, remaining 2% is final M4B mux/encode.
        display = 98.0 * ratio
        self.overall_progress.setValue(round(display * 10))
        self.overall_text.setText(f'Overall: {display:.1f}% · {done:,} / {total:,} chunks')
        if self.current_section in self.plan and not self.exporting:
            section = self.plan[self.current_section]
            ct = max(1, int(section['chunks']))
            cd = self.done_chunks[self.current_section]
            self.chapter_progress.setValue(round(1000 * cd / ct))
            self.chapter_text.setText(f"Section {self.current_section}: {section['title']} — {cd:,}/{ct:,} chunks ({100*cd/ct:.1f}%)")
        self.stat_chunks.setText(f'Chunks: {done:,} / {total:,}')
        if self.chunk_durations and not self.exporting:
            remaining = max(0, total-done)
            eta = remaining * (sum(self.chunk_durations)/len(self.chunk_durations))
            self.stat_eta.setText(f'ETA: ~{fmt_seconds(eta)} + export')

    def tick(self):
        if self.proc is not None and self.started_at is not None:
            self.stat_elapsed.setText(f'Elapsed: {fmt_seconds(time.monotonic() - self.started_at)}')

    def stop_job(self):
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
        if self.proc is None or self.proc.state() == QProcess.NotRunning:
            return
        ask = QMessageBox.question(self, 'Conversion still stopping',
                                   'The current operation has not finished. Force stop the process?\n\n'
                                   'Previous chunk caches and completed WAVs will remain. The current chapter may need rebuilding.',
                                   QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
        if ask == QMessageBox.Yes and self.proc is not None:
            self.proc.kill()

    def process_error(self, _error):
        if self.proc is not None:
            self.write_log(f'Process error: {self.proc.errorString()}')

    def process_finished(self, exit_code, exit_status):
        if self.proc is None:
            return
        self.consume_output()
        if self.buffer.strip():
            self.write_log(self.buffer.strip())
        self.buffer = ''
        stopped = self.stop_requested or exit_code == 130
        good = exit_code == 0 and exit_status == QProcess.NormalExit and not stopped
        job_id = self.active_job_id
        job = self.store.job(job_id)
        if good:
            expected = Path(self.last_output_path) if self.last_output_path else None
            if not expected or not expected.is_file() or expected.stat().st_size < 1024 or expected.stem != job['output_name']:
                good = False
                self.write_log('Export verification failed: M4B missing, too small, or incorrect filename.')
        self.persist_progress(force=True)
        if good:
            self.store.update_job(job_id, 'complete', self.last_output_path)
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
            self.store.update_job(job_id, 'failed', error=f'Converter exited with code {exit_code}. Check log.')
            self.active_title.setText('Generation failed — see log')
            self.write_log(f'Generation failed (exit code {exit_code}).')
            self.run_queue = False
        self.proc.deleteLater()
        self.proc = None
        self.active_job_id = None
        self.stop_requested = False
        self.refresh_jobs()
        self.refresh_review()
        if self.run_queue and good:
            QTimer.singleShot(350, self.start_queue)
        else:
            self.run_queue = False

    def retry_selected(self):
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
        job_id = self.selected_job_id()
        if job_id is None or job_id == self.active_job_id:
            return
        self.store.remove_job(job_id)
        self.refresh_jobs()
        self.statusBar().showMessage('Job removed from the queue. Audio files were not deleted.')

    def selected_job_output(self):
        job_id = self.selected_job_id()
        return self.store.job(job_id) if job_id is not None else None

    def open_selected_output(self, *_):
        job = self.selected_job_output()
        if job is None:
            return
        if job['output_path'] and Path(job['output_path']).is_file():
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(job['output_path'])))
        else:
            QMessageBox.information(self, 'M4B not available',
                'The selected job does not have an exported M4B yet. Use Open folder to find its work files.')

    def job_output_folder(self, job):
        if job['output_path'] and Path(job['output_path']).parent.is_dir():
            return Path(job['output_path']).parent
        self.converter = self.converter or load_converter()
        return self.converter.book_folder(Path(job['book_path']))

    def open_selected_output_folder(self):
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
        job = self.selected_job_output()
        if job is not None:
            path = job['output_path'] or str(self.job_output_folder(job))
            QApplication.clipboard().setText(path)
            self.statusBar().showMessage('Output path copied to clipboard.')

    def review_selected_book_id(self):
        return self.review_book.currentData()

    def refresh_review(self, *_):
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
        self.export_table.setRowCount(len(jobs))
        for r, job in enumerate(jobs):
            path = Path(job['output_path'])
            exists = path.is_file()
            details = (job['name'], 'Ready' if exists else 'Missing',
                       fmt_size(path.stat().st_size) if exists else '—')
            for c, value in enumerate(details):
                self.export_table.setItem(r, c, item(value, str(path) if c == 0 else None))

    def selected_export_path(self):
        row = self.export_table.currentRow()
        obj = self.export_table.item(row, 0) if row >= 0 else None
        return Path(obj.data(Qt.UserRole)) if obj is not None else None

    def play_selected_export(self, *_):
        path = self.selected_export_path()
        if path is not None and path.is_file():
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(path)))
        else:
            QMessageBox.information(self, 'M4B not available', 'Select an exported M4B that exists on disk.')

    def open_selected_export_folder(self):
        path = self.selected_export_path()
        if path is not None and path.parent.is_dir():
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(path.parent)))
        else:
            QMessageBox.information(self, 'Output folder unavailable', 'Select a completed export.')

    def play_selected_chapter(self):
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
        self.editor_dialog.finished.connect(lambda _result: self.refresh_review())
        self.editor_dialog.show()

    def preview_cache_cleanup(self):
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
        self.update_mysql_status()

    def calculate_cache_size(self):
        try:
            folder = ROOT / 'output' / 'narration_cache_v2'
            size = size_on_disk(folder)
            self.space_info.setText(f'Raw chunk cache: {fmt_size(size)} · Stored in {folder}')
        except Exception as exc:
            self.error('Unable to calculate cache size', exc)

    def open_cache_folder(self):
        folder = ROOT / 'output' / 'narration_cache_v2'
        folder.mkdir(parents=True, exist_ok=True)
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(folder)))

    def closeEvent(self, event):
        editor = getattr(self, 'editor_dialog', None)
        if editor is not None and editor.isVisible() and editor.proc is not None:
            QMessageBox.information(self, 'Audio editor busy',
                'Finish the chunk editing operation before closing AudiobookAI.')
            event.ignore()
            return
        if self.proc is not None:
            QMessageBox.information(self, 'Conversion in progress',
                'Stop the current conversion first, then close the application. All completed cached chunks will remain.')
            event.ignore()
        else:
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
    app = QApplication(sys.argv)
    app.setApplicationName('AudiobookAI Studio')
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
