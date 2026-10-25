"""V2.10.2 regression tests for persistent and drag-accessible execution order."""
from __future__ import annotations

import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from audiobookai_storage import Store

class QueueOrderTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / 'studio.sqlite3'
        self.store = Store(self.path)
        self.book = self.store.import_book(
            '/tmp/reorder.epub', 'Queue test', 'Author',
            [SimpleNamespace(index=1, title='Chapter', text='Text', source='test.xhtml')])

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def add_jobs(self, count=4):
        return [self.store.add_job(self.book, f'Part {n}', [1], f'part{n}')
                for n in range(1, count + 1)]

    def ids(self):
        return [r['id'] for r in self.store.jobs()]

    def test_move_to_end_changes_next_job_and_survives_restart(self):
        a, b, c, d = self.add_jobs()
        self.assertEqual(self.ids(), [a, b, c, d])
        self.assertEqual(self.store.next_job()['id'], a)
        self.assertTrue(self.store.move_job(a, 99))
        self.assertEqual(self.ids(), [b, c, d, a])
        self.assertEqual(self.store.next_job()['id'], b)
        self.store.close()
        self.store = Store(self.path)
        self.assertEqual(self.ids(), [b, c, d, a])
        self.assertEqual(self.store.next_job()['id'], b)
        e = self.store.add_job(self.book, 'New part', [1], 'part5')
        self.assertEqual(self.ids(), [b, c, d, a, e])
        self.assertEqual(self.store.move_job(e, 1), True)
        self.assertEqual(self.ids(), [b, e, c, d, a])
        self.assertEqual(self.store.next_job()['id'], b)

    def test_move_while_running_is_safe_and_does_not_touch_progress(self):
        a, b, c, d = self.add_jobs()
        self.store.update_job(b, 'running')
        self.store.update_progress(b, 1000, 245, 1, 245, 600)
        self.assertTrue(self.store.move_job(a, 3))
        self.assertEqual(self.ids(), [b, c, d, a])
        self.assertEqual(self.store.job(b)['completed_chunks'], 245)
        self.assertEqual(self.store.job(b)['status'], 'running')
        self.assertEqual(self.store.next_job()['id'], c)
        with self.assertRaisesRegex(ValueError, 'Only pending'):
            self.store.move_job(b, 2)
        self.assertEqual(self.ids(), [b, c, d, a])
        self.assertTrue(self.store.move_job(d, 0))
        self.assertEqual(self.store.next_job()['id'], d)
        self.store.update_job(d, 'complete')
        with self.assertRaisesRegex(ValueError, 'Only pending'):
            self.store.move_job(d, 3)
        self.assertEqual(self.store.next_job()['id'], c)

    def test_missing_invalid_and_noop_moves_do_not_change_database(self):
        a, b, c, d = self.add_jobs()
        notifications = []
        self.store.on_change = lambda: notifications.append('saved')
        self.assertFalse(self.store.move_job(c, 2))
        self.assertEqual(notifications, [])
        for bad_id, index in ((99999, 1), (a, 'last'), (a, True)):
            with self.assertRaises(ValueError):
                self.store.move_job(bad_id, index)
        self.assertEqual(self.ids(), [a, b, c, d])
        self.assertEqual(notifications, [])
        self.assertTrue(self.store.move_job(d, -20))
        self.assertEqual(self.ids(), [d, a, b, c])
        self.assertEqual(notifications, ['saved'])

    def test_migration_preserves_fifo_and_all_prior_job_metadata(self):
        a, b, c = self.add_jobs(3)
        self.store.update_job(b, 'interrupted', error='Original error')
        self.store.db.execute('DROP INDEX IF EXISTS idx_jobs_queue_position')
        # Exercise the exact pre-V2.10.2 schema on an otherwise valid DB.
        self.store.db.execute('ALTER TABLE jobs DROP COLUMN queue_position')
        self.store.db.commit()
        self.store.close()
        self.store = Store(self.path)
        self.assertEqual(self.ids(), [a, b, c])
        self.assertEqual(self.store.job(b)['status'], 'interrupted')
        self.assertEqual(self.store.job(b)['error'], 'Original error')
        self.assertEqual(self.store.next_job()['id'], a)

class DragWidgetTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
        from PySide6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def test_drop_emits_intent_without_mutating_qt_rows(self):
        from PySide6.QtCore import Qt, QPointF
        from PySide6.QtWidgets import QTableWidgetItem
        from audiobook_gui_v2 import QueueTableWidget

        table = QueueTableWidget()
        table.resize(640, 260)
        table.setRowCount(3)
        for i, ident in enumerate((11, 22, 33)):
            cell = QTableWidgetItem(f'Job {ident}')
            cell.setData(Qt.UserRole, ident)
            table.setItem(i, 0, cell)
        table.show()
        self.app.processEvents()
        requests = []
        table.reorder_requested.connect(lambda job_id, dest: requests.append((job_id, dest)))
        table.selectRow(0)
        last = table.visualRect(table.model().index(2, 0))
        class Drop:
            accepted = False
            def source(self): return table
            def position(self): return QPointF(12, last.bottom() - 1)
            def acceptProposedAction(self): self.accepted = True
            def ignore(self): self.accepted = False
        event = Drop()
        table.dropEvent(event)
        self.assertTrue(event.accepted)
        self.assertEqual(requests, [(11, 2)])
        self.assertEqual([table.item(i, 0).data(Qt.UserRole) for i in range(3)],
                         [11, 22, 33])
        table.close()

if __name__ == '__main__':
    unittest.main()
