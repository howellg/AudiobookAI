#!/usr/bin/env python3
"""Desktop frontend for epub_to_audiobook.py (Chatterbox V6).

Requires: uv pip install PySide6
Run: python scripts/audiobook_gui.py
Put this script beside epub_to_audiobook.py in ~/AudiobookAI/scripts/.
"""

# MAINTAINER NOTE: Legacy first-generation Qt window. The supported desktop UI is `audiobook_gui_v2.py`; this file remains for historical compatibility.
from __future__ import annotations

import importlib.util
import re
import sys
from pathlib import Path

from PySide6.QtCore import Qt, QProcess, QTimer, QUrl
from PySide6.QtGui import QDesktopServices, QFont
from PySide6.QtWidgets import (
    QApplication, QAbstractItemView, QCheckBox, QFileDialog, QHBoxLayout,
    QLabel, QListWidget, QListWidgetItem, QMainWindow, QMessageBox,
    QPlainTextEdit, QProgressBar, QPushButton, QSplitter, QVBoxLayout,
    QWidget,
)

SCRIPT_DIR = Path(__file__).resolve().parent
ROOT = SCRIPT_DIR.parent
CONVERTER = SCRIPT_DIR / 'epub_to_audiobook.py'


def load_converter():
    """Load the original converter lazily when an EPUB needs inspection."""
    if not CONVERTER.is_file():
        raise FileNotFoundError(f'Missing converter script: {CONVERTER}')
    spec = importlib.util.spec_from_file_location('audiobook_epub_converter', CONVERTER)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class AudiobookWindow(QMainWindow):
    """Legacy Qt converter UI retained for reference and backwards compatibility."""
    def __init__(self):
        """Build the original GUI controls and initialize conversion process state."""
        super().__init__()
        self.setWindowTitle('AudiobookAI — EPUB Converter')
        self.resize(1080, 760)
        self.book_path = None
        self.sections = []
        self.converter = None
        self.proc = None
        self.stop_requested = False
        self.expected = 0
        self.finished_sections = set()
        self.current_section = None
        self.current_chunk_count = 0
        self.current_chunk_done = 0
        self.buffer = ''

        main = QWidget()
        self.setCentralWidget(main)
        layout = QVBoxLayout(main)
        layout.setSpacing(9)

        heading = QLabel('EPUB → M4B audiobook')
        f = QFont()
        f.setPointSize(17)
        f.setBold(True)
        heading.setFont(f)
        layout.addWidget(heading)

        book_line = QHBoxLayout()
        self.path_label = QLabel('No EPUB selected')
        self.path_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.path_label.setWordWrap(True)
        self.choose_btn = QPushButton('Choose EPUB…')
        self.choose_btn.clicked.connect(self.choose_book)
        book_line.addWidget(self.path_label, 1)
        book_line.addWidget(self.choose_btn)
        layout.addLayout(book_line)

        self.details = QLabel('Choose a DRM-free EPUB to inspect the book and select sections.')
        self.details.setWordWrap(True)
        layout.addWidget(self.details)

        split = QSplitter(Qt.Horizontal)
        list_wrap = QWidget()
        list_layout = QVBoxLayout(list_wrap)
        list_layout.setContentsMargins(0, 0, 0, 0)
        self.list_title = QLabel('Sections')
        list_layout.addWidget(self.list_title)
        self.chapter_list = QListWidget()
        self.chapter_list.setSelectionMode(QAbstractItemView.NoSelection)
        self.chapter_list.itemChanged.connect(self.update_selection)
        list_layout.addWidget(self.chapter_list)
        select_line = QHBoxLayout()
        self.all_btn = QPushButton('Select all')
        self.all_btn.clicked.connect(lambda: self.select_all(True))
        self.none_btn = QPushButton('Select none')
        self.none_btn.clicked.connect(lambda: self.select_all(False))
        self.story_btn = QPushButton('Select story chapters')
        self.story_btn.setToolTip('Check sections whose titles begin with Chapter; uncheck preface and afterword.')
        self.story_btn.clicked.connect(self.select_story)
        select_line.addWidget(self.all_btn)
        select_line.addWidget(self.none_btn)
        select_line.addWidget(self.story_btn)
        list_layout.addLayout(select_line)
        self.selection_label = QLabel('0 selected')
        list_layout.addWidget(self.selection_label)
        split.addWidget(list_wrap)

        right = QWidget()
        right_layout = QVBoxLayout(right)
        right_layout.setContentsMargins(0, 0, 0, 0)
        right_layout.addWidget(QLabel('Conversion log'))
        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setMaximumBlockCount(4000)
        right_layout.addWidget(self.log)
        split.addWidget(right)
        split.setSizes([430, 650])
        layout.addWidget(split, 1)

        self.export_box = QCheckBox('Create M4B automatically when selected sections are finished')
        self.export_box.setChecked(True)
        layout.addWidget(self.export_box)

        self.progress_text = QLabel('Ready')
        layout.addWidget(self.progress_text)
        self.progress = QProgressBar()
        self.progress.setRange(0, 100)
        self.progress.setValue(0)
        layout.addWidget(self.progress)

        controls = QHBoxLayout()
        self.convert_btn = QPushButton('Convert selected sections')
        self.convert_btn.clicked.connect(lambda: self.start_job('convert'))
        self.export_btn = QPushButton('Export selected sections only')
        self.export_btn.setToolTip('Use WAVs already generated for selected sections; no TTS generation')
        self.export_btn.clicked.connect(lambda: self.start_job('export'))
        self.stop_btn = QPushButton('Stop after current operation')
        self.stop_btn.setToolTip('Requests graceful interruption. Already cached speech is retained.')
        self.stop_btn.clicked.connect(self.stop_job)
        self.open_btn = QPushButton('Open output folder')
        self.open_btn.clicked.connect(self.open_output)
        controls.addWidget(self.convert_btn)
        controls.addWidget(self.export_btn)
        controls.addWidget(self.stop_btn)
        controls.addWidget(self.open_btn)
        layout.addLayout(controls)
        self.refresh_controls()

    def append_log(self, text):
        """Append output to the legacy converter log viewer."""
        if text:
            self.log.appendPlainText(text.rstrip('\n'))
            scroll = self.log.verticalScrollBar()
            scroll.setValue(scroll.maximum())

    def show_error(self, title, err):
        """Display an error message to the user."""
        QMessageBox.critical(self, title, str(err))
        self.append_log(f'ERROR: {err}')

    def choose_book(self):
        """Open a file picker to select an EPUB."""
        start = str((ROOT / 'books') if (ROOT / 'books').exists() else ROOT)
        selected, _ = QFileDialog.getOpenFileName(self, 'Choose an EPUB', start, 'EPUB books (*.epub)')
        if selected:
            self.load_book(Path(selected))

    def load_book(self, path):
        """Inspect the selected EPUB and populate its section checkboxes."""
        if self.proc is not None:
            return
        QApplication.setOverrideCursor(Qt.WaitCursor)
        try:
            conv = load_converter()
            title, author, sections = conv.load_epub(path)
        except Exception as exc:
            self.show_error('Unable to inspect EPUB', exc)
            return
        finally:
            QApplication.restoreOverrideCursor()
        self.converter = conv
        self.book_path = path.resolve()
        self.sections = sections
        self.path_label.setText(str(self.book_path))
        words = sum(len(s.text.split()) for s in sections)
        self.details.setText(f'{title} — {author}    •    {len(sections)} EPUB sections    •    {words:,} words')
        self.chapter_list.blockSignals(True)
        self.chapter_list.clear()
        for section in sections:
            wordcount = len(section.text.split())
            entry = QListWidgetItem(f'{section.index:>3}.  {section.title}  ({wordcount:,} words)')
            entry.setData(Qt.UserRole, section.index)
            entry.setFlags(entry.flags() | Qt.ItemIsUserCheckable)
            entry.setCheckState(Qt.Checked if re.match(r'^chapter\s+\d+\b', section.title, re.I) else Qt.Unchecked)
            self.chapter_list.addItem(entry)
        self.chapter_list.blockSignals(False)
        if not self.selected_indices():
            self.select_all(True)
        self.append_log(f'Loaded: {title} — {author} ({len(sections)} sections).')
        self.append_log('Section numbers refer to EPUB reading order. Confirm selections before converting.')
        self.update_selection()

    def select_all(self, select):
        """Check or uncheck every section."""
        self.chapter_list.blockSignals(True)
        for i in range(self.chapter_list.count()):
            self.chapter_list.item(i).setCheckState(Qt.Checked if select else Qt.Unchecked)
        self.chapter_list.blockSignals(False)
        self.update_selection()

    def select_story(self):
        """Select sections that match likely story chapter headings."""
        self.chapter_list.blockSignals(True)
        for i in range(self.chapter_list.count()):
            item = self.chapter_list.item(i)
            s = self.sections[i]
            item.setCheckState(Qt.Checked if re.match(r'^chapter\s+\d+\b', s.title, re.I) else Qt.Unchecked)
        self.chapter_list.blockSignals(False)
        self.update_selection()

    def selected_indices(self):
        """Collect selected section numbers in spine order."""
        return [self.chapter_list.item(i).data(Qt.UserRole)
                for i in range(self.chapter_list.count())
                if self.chapter_list.item(i).checkState() == Qt.Checked]

    def update_selection(self, *_):
        """Refresh selection counts and conversion controls."""
        nums = self.selected_indices()
        words = sum(len(self.sections[n - 1].text.split()) for n in nums)
        self.selection_label.setText(f'{len(nums)} section(s) selected • {words:,} words')
        self.refresh_controls()

    def refresh_controls(self):
        """Enable and disable buttons based on whether generation is active."""
        running = self.proc is not None
        valid = self.book_path is not None and bool(self.selected_indices())
        self.choose_btn.setEnabled(not running)
        self.convert_btn.setEnabled(valid and not running)
        self.export_btn.setEnabled(valid and not running)
        self.stop_btn.setEnabled(running and not self.stop_requested)
        self.chapter_list.setEnabled(not running)
        self.all_btn.setEnabled(not running)
        self.none_btn.setEnabled(not running)
        self.story_btn.setEnabled(not running)
        self.export_box.setEnabled(not running)
        self.open_btn.setEnabled(self.book_path is not None)

    def start_job(self, command):
        """Launch the requested legacy conversion command in a subprocess."""
        if self.proc is not None or self.book_path is None:
            return
        indices = self.selected_indices()
        if not indices:
            QMessageBox.warning(self, 'Nothing selected', 'Select at least one section.')
            return
        self.stop_requested = False
        self.expected = len(indices)
        self.finished_sections = set()
        self.current_section = None
        self.current_chunk_count = 0
        self.current_chunk_done = 0
        self.buffer = ''
        self.progress.setValue(0)
        self.progress_text.setText('Starting…')

        argv = [str(CONVERTER), command, str(self.book_path), '--chapters', ','.join(map(str, indices))]
        if command == 'convert' and not self.export_box.isChecked():
            argv.append('--no-export')
        self.append_log('\n' + '—' * 55)
        self.append_log(f'Starting {command}: {len(indices)} selected EPUB sections')
        proc = QProcess(self)
        self.proc = proc
        proc.setProgram(sys.executable)
        proc.setArguments(['-u', *argv])
        proc.setWorkingDirectory(str(ROOT))
        proc.setProcessChannelMode(QProcess.MergedChannels)
        proc.readyReadStandardOutput.connect(self.read_output)
        proc.finished.connect(self.job_finished)
        proc.errorOccurred.connect(self.process_error)
        proc.start()
        self.refresh_controls()

    def read_output(self):
        """Append streamed conversion output from the child process."""
        if self.proc is None:
            return
        raw = bytes(self.proc.readAllStandardOutput()).decode('utf-8', errors='replace')
        self.buffer += raw.replace('\r', '\n')
        lines = self.buffer.split('\n')
        self.buffer = lines.pop()
        for line in lines:
            line = line.strip()
            if not line:
                continue
            # PyTorch sampling progress bars are noisy; retain meaningful events only.
            if line.startswith('Sampling:') or 'it/s]' in line:
                continue
            self.append_log(line)
            self.parse_progress(line)

    def parse_progress(self, line):
        """Infer chapter and chunk progress from legacy text logs."""
        start = re.search(r'^\[(\d+)/(\d+)\]\s+(.*?)(?:\s+—\s+(\d+) chunks|\s+\(\d+ chunks\))?$', line)
        if start:
            index = int(start.group(1))
            if index in self.selected_indices():
                self.current_section = index
                self.current_chunk_count = int(start.group(4) or 0)
                self.current_chunk_done = 0
                if 'Already complete' in line:
                    self.finished_sections.add(index)
                self.update_progress()
        chunk = re.search(r'^\s*(\d+)/(\d+):', line)
        if chunk and self.current_section is not None:
            self.current_chunk_done = int(chunk.group(1))
            self.current_chunk_count = int(chunk.group(2))
            self.update_progress()
        if line.startswith('Saved ') and self.current_section is not None:
            self.finished_sections.add(self.current_section)
            self.current_chunk_count = 0
            self.current_chunk_done = 0
            self.update_progress()
        if line.startswith('Encoding '):
            self.progress_text.setText('Encoding M4B with FFmpeg…')
        if line.startswith('M4B: '):
            self.progress_text.setText('M4B export complete')

    def update_progress(self):
        """Display the current estimated legacy conversion progress."""
        if not self.expected:
            return
        fraction = len(self.finished_sections)
        if self.current_section not in self.finished_sections and self.current_chunk_count:
            fraction += min(1, self.current_chunk_done / self.current_chunk_count)
        self.progress.setValue(min(99, round(100 * fraction / self.expected)))
        if self.current_section and self.current_chunk_count:
            self.progress_text.setText(
                f'Section {self.current_section}: chunk {self.current_chunk_done}/{self.current_chunk_count}  '
                f'•  {len(self.finished_sections)}/{self.expected} sections finished')
        else:
            self.progress_text.setText(f'{len(self.finished_sections)}/{self.expected} sections finished')

    def stop_job(self):
        """Ask the child converter to stop without discarding completed files."""
        if self.proc is None:
            return
        self.stop_requested = True
        self.stop_btn.setEnabled(False)
        self.progress_text.setText('Stopping… cached chunks remain saved')
        self.append_log('Requesting graceful stop; already-generated chunk cache is retained.')
        self.proc.terminate()
        # Avoid forced kill; a CUDA operation may need to finish before shutting down.
        QTimer.singleShot(12000, self.offer_force_stop)

    def offer_force_stop(self):
        """Offer forced termination when graceful stop stalls."""
        if self.proc is None or self.proc.state() == QProcess.NotRunning:
            return
        reply = QMessageBox.question(
            self, 'Still running',
            'The converter has not stopped yet. Force-stop it?\n\n'
            'Finished chapter WAVs and cached chunks should remain, but an in-progress chapter may need reassembly.',
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
        if reply == QMessageBox.Yes and self.proc is not None:
            self.proc.kill()

    def process_error(self, error):
        """Display the child process error description."""
        if self.proc is not None:
            self.append_log(f'Process error: {self.proc.errorString()}')

    def job_finished(self, exit_code, status):
        """Handle success, failure, or user stop after the child exits."""
        self.read_output()
        if self.buffer.strip():
            self.append_log(self.buffer.strip())
        self.buffer = ''
        success = exit_code == 0 and status == QProcess.NormalExit and not self.stop_requested
        if success:
            self.progress.setValue(100)
            self.progress_text.setText('Finished successfully')
            self.append_log('✓ Conversion/export completed successfully.')
        elif self.stop_requested:
            self.progress_text.setText('Stopped — rerun to reuse cached audio')
            self.append_log('Stopped. Rerun the same selection to resume cached work.')
        else:
            self.progress_text.setText(f'Failed (exit code {exit_code}); see log')
            self.append_log(f'Failed with exit code {exit_code}.')
        self.proc.deleteLater()
        self.proc = None
        self.stop_requested = False
        self.refresh_controls()

    def open_output(self):
        """Open the completed audiobook output directory."""
        if self.book_path is None or self.converter is None:
            return
        folder = self.converter.book_folder(self.book_path)
        folder.mkdir(parents=True, exist_ok=True)
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(folder)))

    def closeEvent(self, event):
        """Handle window closing while a conversion may be running."""
        if self.proc is not None:
            QMessageBox.information(self, 'Conversion running',
                                    'Stop the conversion before closing the window. You can resume afterward.')
            event.ignore()
        else:
            event.accept()


def main():
    """Parse command-line arguments and dispatch the requested operation."""
    app = QApplication(sys.argv)
    app.setApplicationName('AudiobookAI')
    window = AudiobookWindow()
    window.show()
    if len(sys.argv) > 1:
        candidate = Path(sys.argv[1]).expanduser()
        if candidate.is_file() and candidate.suffix.lower() == '.epub':
            QTimer.singleShot(0, lambda: window.load_book(candidate))
    sys.exit(app.exec())


if __name__ == '__main__':
    main()
