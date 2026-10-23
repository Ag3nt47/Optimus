"""Reviewed undo for supported owned records; no actions during construction."""
from __future__ import annotations

import json
from pathlib import Path
import threading

from PySide6.QtCore import QObject, QRunnable, Qt, QTimer, Signal, Slot
from PySide6.QtWidgets import (QAbstractItemView, QDialog, QGridLayout, QHeaderView,
    QLabel, QPlainTextEdit, QProgressBar, QPushButton, QSizePolicy, QTableWidget, QTableWidgetItem, QVBoxLayout)

from .questions import ask_yes_no


class _Signals(QObject):
    result = Signal(object)
    progress = Signal(object)
    error = Signal(str)
    finished = Signal()


class _Worker(QRunnable):
    def __init__(self, operation, cancel):
        super().__init__()
        self.operation, self.cancel = operation, cancel
        self.signals = _Signals()

    def run(self):
        try:
            self.signals.result.emit(self.operation(self.signals.progress.emit, self.cancel))
        except Exception as error:
            self.signals.error.emit(str(error))
        finally:
            self.signals.finished.emit()


class RevertDialog(QDialog):
    def __init__(self, owner):
        super().__init__(owner)
        self.owner, self.backend = owner, owner.backend
        self._worker, self._plan, self._rows = None, None, []
        self._cancel = threading.Event()
        self._closed = self._close_when_idle = False
        self.setWindowTitle('Optimus — Revert supported changes')
        self.setMinimumSize(480, 400)
        self.resize(980, 720)
        layout = QVBoxLayout(self)
        note = QPlainTextEdit('Review Optimus history, newest first. Nothing is selected automatically. '
            'Revert all available applies only supported restorations listed here. Temporary-file deletion, app removals, '
            'updates, overwritten data and deleted Recall snapshots cannot all be undone. Existing restorers preserve their '
            'ownership and current-state checks; conflicts and unavailable recovery remain visible. '
            'Saved maintenance settings can apply these changes again; disable those modules or automatic cleaning in Settings.')
        note.setReadOnly(True)
        note.setMinimumHeight(55)
        note.setMaximumHeight(100)
        note.setAccessibleName('Limits of Revert and warning about automatic reapplication')
        layout.addWidget(note)
        self.table = QTableWidget(0, 5)
        self.table.setHorizontalHeaderLabels(('Select', 'Recorded change', 'Target', 'Status', 'Undo availability'))
        check_image = (Path(__file__).parents[1] / 'assets' / 'checkbox-check.svg').as_posix()
        self.table.setStyleSheet('QTableWidget::indicator {width:18px;height:18px;background:#142333;'
            'border:1px solid #8fbfe9;border-radius:3px;} QTableWidget::indicator:checked {'
            'background:#1f9cff;image:url("' + check_image + '");}')
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Ignored)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.Interactive)
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.setColumnWidth(0, 60)
        self.table.setColumnWidth(1, 180)
        self.table.setColumnWidth(2, 230)
        self.table.setColumnWidth(3, 100)
        self.table.itemChanged.connect(self._controls)
        self.table.itemSelectionChanged.connect(self._selection)
        layout.addWidget(self.table, 3)
        self.details = QPlainTextEdit()
        self.details.setReadOnly(True)
        self.details.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Ignored)
        self.details.setMaximumHeight(170)
        layout.addWidget(self.details, 1)
        self.status = QLabel('Refresh to read supported local history. No restoration has been requested.')
        self.status.setWordWrap(True)
        self.status.setTextFormat(Qt.PlainText)
        layout.addWidget(self.status)
        self.progress = QProgressBar()
        self.progress.hide()
        layout.addWidget(self.progress)
        self.refresh_button = self._button('Refresh history', self.refresh)
        self.select_button = self._button('Select all available', self.select_all)
        self.clear_button = self._button('Clear selection', self.clear_selection)
        self.revert_selected_button = self._button('Revert selected…', self.revert_selected)
        self.revert_all_button = self._button('Revert all available…', self.revert_all)
        self.cancel_button = self._button('Cancel safely', self.cancel)
        self.settings_button = self._button('Settings', self.adjust_settings)
        self.close_button = self._button('Close', self.close)
        for button in (self.revert_selected_button, self.revert_all_button):
            button.setStyleSheet('QPushButton{background:#8f2535;color:#fff;border:1px solid #ef6c7c;} '
                                'QPushButton:disabled{background:#30202a;color:#968b93;border:1px solid #55313a;}')
        self._buttons = (self.refresh_button, self.select_button, self.clear_button,
                         self.revert_selected_button, self.revert_all_button, self.settings_button,
                         self.cancel_button, self.close_button)
        self._button_grid, self._button_columns = QGridLayout(), None
        layout.addLayout(self._button_grid)
        self._reflow_buttons()
        self.close_button.setDefault(True)
        self._controls()

    @staticmethod
    def _button(label, callback):
        button = QPushButton(label)
        button.setAutoDefault(False)
        button.setMinimumHeight(36)
        button.setMinimumWidth(0)
        button.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Fixed)
        button.clicked.connect(callback)
        return button

    def _selected(self):
        return [row for index, row in enumerate(self._rows)
                if row.get('undo_available') is True and self.table.item(index, 0).checkState() == Qt.Checked]

    def _controls(self, *_):
        idle = not self._closed and self._worker is None
        can_restore = idle and bool(self._plan) and not self.owner.smoke_test and callable(getattr(self.backend, 'change_history_revert', None))
        available = any(row.get('undo_available') is True for row in self._rows)
        self.refresh_button.setEnabled(idle and callable(getattr(self.backend, 'change_history_scan', None)))
        self.select_button.setEnabled(idle and available)
        self.clear_button.setEnabled(idle and available)
        self.revert_all_button.setEnabled(can_restore and available)
        self.revert_selected_button.setEnabled(can_restore and bool(self._selected()))
        self.table.setEnabled(idle)
        self.settings_button.setEnabled(idle)
        self.cancel_button.setVisible(self._worker is not None)
        self.cancel_button.setEnabled(self._worker is not None and not self._cancel.is_set())

    def select_all(self):
        if self._worker is None:
            for index, row in enumerate(self._rows):
                if row.get('undo_available') is True:
                    self.table.item(index, 0).setCheckState(Qt.Checked)

    def clear_selection(self):
        if self._worker is None:
            for index in range(self.table.rowCount()):
                self.table.item(index, 0).setData(Qt.CheckStateRole,
                    Qt.Unchecked if self._rows[index].get('undo_available') is True else None)

    def _reflow_buttons(self):
        columns = 3 if self.width() < 760 else 4
        if columns == self._button_columns:
            return
        self._button_columns = columns
        while self._button_grid.count():
            self._button_grid.takeAt(0)
        for index, button in enumerate(self._buttons):
            self._button_grid.addWidget(button, index // columns, index % columns)
        for index in range(4):
            self._button_grid.setColumnStretch(index, int(index < columns))

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if hasattr(self, '_button_grid'):
            self._reflow_buttons()

    def adjust_settings(self):
        if self._worker is None:
            self.close()
            self.owner.open_settings(page='Settings')

    def _selection(self):
        indexes = self.table.selectionModel().selectedRows()
        if indexes:
            row = self._rows[indexes[0].row()]
            self.details.setPlainText(json.dumps(row, indent=2, ensure_ascii=False))

    def _start(self, operation, kind):
        if self._closed or self._worker is not None or self.owner.busy:
            self.status.setText('Wait for the active operation before reviewing or reverting history.')
            return False
        self._kind = kind
        self._cancel = threading.Event()
        self.owner.busy = True
        self._worker = _Worker(operation, self._cancel)
        self._worker.signals.result.connect(self._result)
        self._worker.signals.progress.connect(self._progress)
        self._worker.signals.error.connect(self._error)
        self._worker.signals.finished.connect(self._finished)
        self.progress.setRange(0, 0)
        self.progress.show()
        self.status.setText('Reading local history.' if kind == 'scan' else 'Restoring the reviewed available records. Cancellation stops before the next record.')
        self._controls()
        self.owner.pool.start(self._worker)
        return True

    def refresh(self):
        method = getattr(self.backend, 'change_history_scan', None)
        if callable(method):
            self._start(lambda progress, cancel: method(), 'scan')
        else:
            self.status.setText('Unified history is unavailable on this backend. No restoration was started.')

    def _set_inventory(self, result):
        self._plan = result.get('plan_id')
        self._rows = result.get('items') if isinstance(result.get('items'), list) else []
        self._rows = [row for row in self._rows[:2000] if isinstance(row, dict)]
        self.table.blockSignals(True)
        try:
            self.table.setRowCount(len(self._rows))
            for index, row in enumerate(self._rows):
                choice = QTableWidgetItem()
                flags = Qt.ItemIsEnabled | Qt.ItemIsSelectable
                if row.get('undo_available') is True:
                    flags |= Qt.ItemIsUserCheckable
                choice.setFlags(flags)
                if row.get('undo_available') is True:
                    choice.setCheckState(Qt.Unchecked)
                self.table.setItem(index, 0, choice)
                for column, value in enumerate((row.get('kind'), row.get('target'), row.get('status'),
                        'Available — review required' if row.get('undo_available') is True else row.get('reason')), 1):
                    cell = QTableWidgetItem(str(value or ''))
                    cell.setToolTip(str(value or ''))
                    self.table.setItem(index, column, cell)
        finally:
            self.table.blockSignals(False)
        count = sum(row.get('undo_available') is True for row in self._rows)
        self.status.setText(f'{len(self._rows)} history record(s); {count} supported restoration(s) available. Nothing selected.'
                            + (' Some history could not be verified; review the errors.' if result.get('complete') is not True else ''))
        self.details.setPlainText(str(result.get('note') or '') + ('\n' + '\n'.join(str(e) for e in result.get('errors', [])) if result.get('errors') else ''))

    def _confirm(self, rows):
        labels = [str(row.get('kind')) + ': ' + str(row.get('target') or row.get('record_id')) for row in rows]
        summary = '\n'.join(labels[:20]) + (f'\n…and {len(labels) - 20} additional selected supported records.' if len(labels) > 20 else '')
        return ask_yes_no(self, 'Revert these supported Optimus changes?',
            f'Restore {len(rows)} selected record(s), newest first?\n\n' + summary
            + '\n\nThis changes saved settings/registrations or restores eligible browser backups. Conflicts can skip a restoration. '
            'This does not recover every deleted file, removed app, installed update or Recall snapshot. '
            'The batch stops on an unknown outcome or failure; it never restarts the PC. '
            'Saved maintenance settings can apply these changes again; disable the matching modules or automatic cleaning in Settings.',
            yes_label='Yes — revert these records', no_label='No — keep current state')

    def revert_selected(self):
        self._revert(self._selected())

    def revert_all(self):
        self._revert([row for row in self._rows if row.get('undo_available') is True])

    def _revert(self, rows):
        if self._closed or self._worker is not None or self.owner.busy or self.owner.smoke_test:
            self.status.setText('Revert is unavailable during another operation or an interface smoke test.')
            return
        method = getattr(self.backend, 'change_history_revert', None)
        if not self._plan or not rows or not callable(method) or not self._confirm(rows):
            return
        plan, identities = self._plan, [row['selection_id'] for row in rows]
        self._start(lambda progress, cancel: method(plan, identities, confirmed=True,
                    progress_callback=progress, cancel_event=cancel), 'revert')

    @Slot(object)
    def _result(self, result):
        if not isinstance(result, dict):
            self.status.setText('No usable result was returned; the outcome remains unverified.')
        elif self._kind == 'scan':
            self._set_inventory(result)
        else:
            self._plan = None
            for index, row in enumerate(self._rows):
                row['undo_available'] = False
                self.table.item(index, 0).setData(Qt.CheckStateRole, None)
            self.status.setText(str(result.get('note') or result.get('status') or 'Review the saved restoration outcome.'))
            self.details.setPlainText(json.dumps(result, indent=2, ensure_ascii=False, default=str)[:30000])

    @Slot(object)
    def _progress(self, event):
        if not isinstance(event, dict):
            return
        if type(event.get('current')) is int and type(event.get('total')) is int and event['total'] > 0:
            self.progress.setRange(0, event['total'])
            self.progress.setValue(event['current'])
        self.status.setText(str(event.get('description') or event.get('stage') or 'Restoring the selected record.'))

    @Slot(str)
    def _error(self, message):
        self._plan = None
        self.status.setText('Restoration did not complete. Review the saved outcome before retrying: ' + message)

    @Slot()
    def _finished(self):
        self._worker = None
        self.owner.busy = False
        self.progress.hide()
        self._controls()
        if self.owner._close_when_idle:
            QTimer.singleShot(0, self.owner.quit_application)
        elif self._close_when_idle:
            QTimer.singleShot(0, self.close)

    def cancel(self):
        self._cancel.set()
        self.cancel_button.setEnabled(False)
        self.status.setText('Cancellation requested. Waiting for the current restorer to save its outcome; later records will not start.')

    def closeEvent(self, event):
        if self._worker is not None:
            self._close_when_idle = True
            self.cancel()
            event.ignore()
        else:
            event.accept()

    def showEvent(self, event):
        if self._worker is None:
            self._close_when_idle = False
        super().showEvent(event)

    def shutdown(self):
        self._closed = True
        self._cancel.set()
