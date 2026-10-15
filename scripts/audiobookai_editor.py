"""Non-destructive chunk-level review for AudiobookAI Studio V2.4.

Audio chunks are played in the user's default player. New takes are generated
through a subprocess, preventing CUDA/Qt contention in the GUI process.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

from PySide6.QtCore import Qt, QProcess, QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (QDialog, QVBoxLayout, QHBoxLayout, QTableWidget,
    QTableWidgetItem, QPushButton, QLabel, QPlainTextEdit, QComboBox,
    QMessageBox, QHeaderView, QAbstractItemView)

import audiobookai_production as production

SCRIPT = Path(__file__).with_name('epub_to_audiobook_v7_studio.py')
ROOT = SCRIPT.parent.parent


class ChunkEditor(QDialog):
    def __init__(self, book_path, folder, chapter, voice_path=None, completed_jobs=(), parent=None):
        super().__init__(parent)
        self.book_path = Path(book_path)
        self.folder = Path(folder)
        self.chapter = int(chapter)
        self.voice_path = str(voice_path) if voice_path else None
        self.completed_jobs = list(completed_jobs)
        self.proc = None
        self.commands = []
        self.on_commands_finished = None
        self.index = {}
        self.setWindowTitle(f'Audio Chunk Editor — Chapter/EPUB section {chapter}')
        self.resize(1050, 770)
        root = QVBoxLayout(self)
        self.banner = QLabel('Editing is non-destructive: the old WAV and M4B stay unchanged until you rebuild.')
        self.banner.setWordWrap(True)
        root.addWidget(self.banner)
        self.table = QTableWidget(0, 4)
        self.table.setHorizontalHeaderLabels(['#', 'Original spoken text', 'Selected take', 'Audio'])
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.Stretch)
        self.table.setColumnWidth(0, 53)
        self.table.setColumnWidth(2, 165)
        self.table.setColumnWidth(3, 100)
        self.table.itemSelectionChanged.connect(self.select_chunk)
        root.addWidget(self.table, 3)
        row = QHBoxLayout()
        row.addWidget(QLabel('Recorded takes:'))
        self.takes = QComboBox()
        row.addWidget(self.takes, 1)
        row.addWidget(self.button('▶ Original', self.play_original))
        row.addWidget(self.button('▶ Selected take', self.play_take))
        row.addWidget(self.button('Use selected take', self.activate_take))
        row.addWidget(self.button('Revert to original', self.revert))
        root.addLayout(row)
        root.addWidget(QLabel('Replacement text — change only what the narrator should speak:'))
        self.text = QPlainTextEdit()
        self.text.setMaximumHeight(110)
        root.addWidget(self.text)
        actions = QHBoxLayout()
        actions.addWidget(self.button('🎙 Generate new take', self.generate_take))
        actions.addWidget(self.button('Index / refresh chapter', self.index_chapter))
        actions.addWidget(self.button('Scan audio quality', self.audit))
        actions.addStretch()
        root.addLayout(actions)
        self.report = QLabel('Select a chunk to inspect its audio quality and take history.')
        self.report.setWordWrap(True)
        root.addWidget(self.report)
        lower = QHBoxLayout()
        lower.addWidget(self.button('Rebuild chapter and completed M4Bs', self.rebuild))
        lower.addStretch()
        lower.addWidget(self.button('Close', self.close))
        root.addLayout(lower)
        self.logs = QPlainTextEdit()
        self.logs.setReadOnly(True)
        self.logs.setMaximumHeight(150)
        self.logs.setMaximumBlockCount(300)
        root.addWidget(self.logs)
        if not production.index_path(self.folder, self.chapter).exists():
            self.index_chapter()
        else:
            self.reload()

    def button(self, text, func):
        button = QPushButton(text)
        button.clicked.connect(func)
        return button

    def voice_args(self):
        return ['--voice', self.voice_path] if self.voice_path else []

    def command(self, action, **kwargs):
        args = ['-u', str(SCRIPT), action, str(self.book_path),
                '--chapters', str(self.chapter), *self.voice_args()]
        for key, value in kwargs.items():
            args += ['--' + key.replace('_', '-'), str(value)]
        return args

    def log(self, message):
        self.logs.appendPlainText(str(message))

    def run_commands(self, commands, done=None):
        if self.proc is not None:
            QMessageBox.information(self, 'Operation running', 'An audio operation is already in progress.')
            return
        self.commands = list(commands)
        self.on_commands_finished = done
        self.run_next()

    def run_next(self):
        if not self.commands:
            callback = self.on_commands_finished
            self.on_commands_finished = None
            self.banner.setText('Operation complete. Your existing recordings were preserved until verified replacements were ready.')
            self.reload()
            if callback:
                callback()
            return
        args = self.commands.pop(0)
        self.banner.setText('Working: ' + args[2] + ' — do not close this editor.')
        self.log('> ' + args[2] + ' (section ' + str(self.chapter) + ')')
        self.proc = QProcess(self)
        self.proc.setProgram(sys.executable)
        self.proc.setArguments(args)
        self.proc.setWorkingDirectory(str(ROOT))
        self.proc.setProcessChannelMode(QProcess.MergedChannels)
        self.proc.readyReadStandardOutput.connect(self.drain)
        self.proc.finished.connect(self.process_finished)
        self.proc.start()

    def drain(self):
        if self.proc is not None:
            text = bytes(self.proc.readAllStandardOutput()).decode('utf-8', errors='replace')
            for line in text.replace('\r', '\n').splitlines():
                if not line.startswith('AUDIOBOOKAI_EVENT '):
                    self.log(line)

    def process_finished(self, exit_code, exit_status):
        self.drain()
        self.proc.deleteLater()
        self.proc = None
        if exit_code != 0 or exit_status != QProcess.NormalExit:
            self.commands.clear()
            self.on_commands_finished = None
            self.banner.setText(f'Operation failed (exit {exit_code}); previous recordings were not overwritten.')
            self.log('Failed: check log above. Existing M4B exports remain available.')
            QMessageBox.warning(self, 'Audio operation failed', 'The operation failed; see the log at the bottom of the editor.')
            return
        self.run_next()

    def reload(self):
        try:
            self.index = production.load_index(self.folder, self.chapter)
            chunks = self.index.get('chunks', [])
            saved_row = self.table.currentRow()
            self.table.blockSignals(True)
            self.table.setRowCount(len(chunks))
            flagged = {}
            report_file = self.folder / 'quality_reports' / f'{self.chapter:04d}.json'
            if report_file.exists():
                try:
                    flagged = {int(r['number']): r for r in production.read_json(report_file).get('flags', [])}
                except (ValueError, KeyError):
                    pass
            for row, ch in enumerate(chunks):
                active = production.active_take(self.folder, self.chapter, row+1, ch['text'])
                original = production.original_chunk_file(ch)
                status = 'Edited' if active else 'Original'
                path = Path(active['path']) if active else original
                if not path.is_file():
                    status = 'Missing WAV'
                elif row+1 in flagged:
                    status += ' ⚠ Review'
                for col, value in enumerate((row+1, ch['text'], status, 'Available' if path.is_file() else 'Missing')):
                    self.table.setItem(row, col, QTableWidgetItem(str(value)))
            self.table.blockSignals(False)
            if chunks:
                self.table.selectRow(min(max(0, saved_row), len(chunks)-1))
                self.select_chunk()
        except Exception as exc:
            self.log(f'Unable to load chunk index: {exc}')
            self.banner.setText(f'Unable to read chunk metadata: {exc}')

    def selected(self):
        row = self.table.currentRow()
        chunks = self.index.get('chunks', [])
        if row < 0 or row >= len(chunks):
            return None
        return chunks[row]

    def select_chunk(self):
        ch = self.selected()
        if ch is None:
            return
        takes, active = production.all_takes(self.folder, self.chapter, ch['number'], ch['text'])
        self.takes.blockSignals(True)
        self.takes.clear()
        self.takes.addItem('Original recording', None)
        for take in takes:
            self.takes.addItem(f"{take['created'][:16]} — {take['text'][:52]}", take['id'])
        selected_take = next((i for i in range(self.takes.count()) if self.takes.itemData(i) == active), 0)
        self.takes.setCurrentIndex(selected_take)
        self.takes.blockSignals(False)
        active_take = production.active_take(self.folder, self.chapter, ch['number'], ch['text'])
        self.text.setPlainText(active_take['text'] if active_take else ch['text'])
        path = Path(active_take['path']) if active_take else production.original_chunk_file(ch)
        if path.is_file():
            try:
                report = production.inspect_wav(path, len(self.text.toPlainText().split()))
                message = f"Audio: {report['duration']} sec | peak {report['peak']} | RMS {report['rms']}"
                if report['flags']:
                    message += ' | REVIEW: ' + ', '.join(report['flags'])
                self.report.setText(message)
            except Exception as exc:
                self.report.setText(f'Could not inspect audio: {exc}')
        else:
            self.report.setText('Raw WAV missing. The original chapter WAV may still be available; generate again to restore raw cache.')

    def play_file(self, path):
        if path and Path(path).is_file():
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(Path(path).resolve())))
        else:
            QMessageBox.information(self, 'Missing WAV', 'This take is not on disk.')

    def play_original(self):
        ch = self.selected()
        if ch:
            self.play_file(production.original_chunk_file(ch))

    def chosen_take(self):
        ch = self.selected()
        if not ch:
            return None
        take_id = self.takes.currentData()
        takes, _ = production.all_takes(self.folder, self.chapter, ch['number'], ch['text'])
        return next((take for take in takes if take['id'] == take_id), None)

    def play_take(self):
        take = self.chosen_take()
        ch = self.selected()
        if ch:
            self.play_file(self.folder / take['wav'] if take else production.original_chunk_file(ch))

    def activate_take(self):
        if self.proc is not None:
            return
        ch = self.selected()
        if ch is None:
            return
        try:
            production.choose_take(self.folder, self.chapter, ch['number'], ch['text'], self.takes.currentData())
            self.reload()
            self.banner.setText('Take selected. Click Rebuild to apply it to the chapter WAV and completed M4Bs.')
        except Exception as exc:
            QMessageBox.warning(self, 'Unable to activate take', str(exc))

    def revert(self):
        if self.proc is not None:
            return
        ch = self.selected()
        if ch:
            try:
                production.choose_take(self.folder, self.chapter, ch['number'], ch['text'], None)
                self.reload()
                self.banner.setText('Original take restored in edit selection. Rebuild the chapter to apply.')
            except Exception as exc:
                QMessageBox.warning(self, 'Revert failed', str(exc))

    def index_chapter(self):
        self.run_commands([self.command('index')])

    def generate_take(self):
        ch = self.selected()
        if not ch:
            return
        replacement = self.text.toPlainText().strip()
        if not replacement or len(replacement.split()) > 45:
            QMessageBox.warning(self, 'Text length', 'Enter 1–45 words for this chunk.')
            return
        self.run_commands([self.command('render-chunk', chunk=ch['number'], replacement_text=replacement)])

    def audit(self):
        self.run_commands([self.command('audit')])

    def rebuild(self):
        if self.proc is not None:
            return
        if not self.index.get('chunks'):
            QMessageBox.information(self, 'No chunk index', 'Index this chapter first.')
            return
        exports = []
        for job in self.completed_jobs:
            path = Path(job['output_path']) if job['output_path'] else None
            if path and path.is_file() and path.parent.resolve() == self.folder.resolve():
                if self.chapter in json.loads(job['indices']):
                    args = ['-u', str(SCRIPT), 'export', str(self.book_path),
                            '--chapters', ','.join(map(str, json.loads(job['indices']))),
                            '--output-name', job['output_name'], *self.voice_args()]
                    exports.append(args)
        explanation = ('Reassemble this chapter using its existing raw WAVs and selected edited takes. '
                       f'Then re-encode {len(exports)} completed M4B part(s). No GPU generation is needed. '
                       'Existing exports remain unchanged if FFmpeg fails.')
        if QMessageBox.question(self, 'Rebuild audiobook?', explanation,
                                QMessageBox.Yes | QMessageBox.No, QMessageBox.Yes) != QMessageBox.Yes:
            return
        self.run_commands([self.command('rebuild'), *exports])

    def reject(self):
        if self.proc is not None:
            QMessageBox.information(self, 'Editor busy', 'Finish or cancel the current audio operation before closing.')
            return
        super().reject()

    def closeEvent(self, event):
        if self.proc is not None:
            event.ignore()
            QMessageBox.information(self, 'Editor busy', 'An audio process is running. Keep the editor open until it finishes.')
        else:
            event.accept()
