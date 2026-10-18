"""Explicitly reviewed third-party service controls; constructors never change services."""
from __future__ import annotations
import json
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton, QTableWidget, QTableWidgetItem, QAbstractItemView, QPlainTextEdit

class ServicesPanel(QWidget):
    def __init__(self, window):
        super().__init__()
        self.window = window
        layout = QVBoxLayout(self)
        note = QLabel('Review third-party Windows services. Stopping or disabling one can affect networking, hardware or applications. Essential Windows, security and unknown services are protected. Supported changes have Restore/history records. Administrator access is required.')
        note.setWordWrap(True)
        layout.addWidget(note)
        bar = QHBoxLayout()
        refresh = QPushButton('Refresh services')
        refresh.clicked.connect(self.refresh)
        bar.addWidget(refresh)
        self.actions = {}
        for action, label in [('Stop', 'Stop selected'), ('Manual', 'Set Manual startup'), ('Disabled', 'Disable startup')]:
            button = QPushButton(label)
            button.clicked.connect(lambda checked=False, action=action: self.apply(action))
            button.setEnabled(False)
            bar.addWidget(button)
            self.actions[action] = button
        bar.addStretch()
        layout.addLayout(bar)
        self.table = QTableWidget(0, 7)
        self.table.setHorizontalHeaderLabels(['Service', 'Display name', 'State', 'Start mode', 'PID', 'Protected', 'Executable'])
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setAlternatingRowColors(True)
        self.table.itemSelectionChanged.connect(self._selection_changed)
        layout.addWidget(self.table, 1)
        self.details = QPlainTextEdit()
        self.details.setReadOnly(True)
        self.details.setMaximumHeight(100)
        self.details.setPlaceholderText('Select a service to inspect its protection reason and executable.')
        layout.addWidget(self.details)
        self.summary = QLabel('Services are read only until you select and confirm an eligible action.')
        self.summary.setWordWrap(True)
        layout.addWidget(self.summary)

    def refresh(self):
        self.window._run('services', done=self._loaded)

    def _loaded(self, result):
        values = result if isinstance(result, list) else [result] if isinstance(result, dict) else []
        self.table.setRowCount(0)
        for record in values:
            row = self.table.rowCount()
            self.table.insertRow(row)
            for column, key in enumerate(('Name', 'DisplayName', 'State', 'StartMode', 'ProcessId', 'Protected', 'PathName')):
                value = record.get(key)
                item = QTableWidgetItem('' if value is None else str(value))
                if column == 0:
                    item.setData(Qt.UserRole, record)
                self.table.setItem(row, column, item)
        self.table.clearSelection()
        self.table.resizeColumnsToContents()
        self.summary.setText(f'{len(values):,} services inventoried. Select one to review its reason; no changes were applied.')
        self._selection_changed()

    def selected(self):
        selected = self.table.selectionModel().selectedRows()
        if len(selected) != 1:
            return None
        item = self.table.item(selected[0].row(), 0)
        return item.data(Qt.UserRole) if item else None

    def _selection_changed(self):
        record = self.selected()
        eligible = bool(record) and record.get('Protected') is False
        for action, button in self.actions.items():
            button.setEnabled(eligible and (action != 'Stop' or record.get('State') == 'Running'))
        self.details.setPlainText(json.dumps(record, indent=2, ensure_ascii=False) if record else '')

    def apply(self, action):
        if action not in self.actions:
            raise ValueError('Unsupported service action.')
        record = self.selected()
        if not record or record.get('Protected') is not False:
            self.window._error('Select an eligible unprotected third-party service first.')
            return
        if action == 'Stop' and record.get('State') != 'Running':
            self.window._error('Refresh and select a running service to stop.')
            return
        descriptions = {
            'Stop': 'Stop this running service now? Its application, device or network connection may stop working. Its startup mode remains recorded separately.',
            'Manual': 'Set this service to Manual startup? Windows or its application can still request it. This action does not stop a currently running service.',
            'Disabled': 'Disable this service for future starts? Its application, device or network features may fail until restored. This action does not stop a currently running service.',
        }
        details = dict(record)
        details['Recovery'] = 'Use Restore/history to restore the recorded service settings. Running dependents and changed service identities are checked again by the engine.'
        phrase = 'STOP SERVICE' if action == 'Stop' else 'DISABLE SERVICE' if action == 'Disabled' else None
        if self.window._confirm('Review service change', descriptions[action], details=details, phrase=phrase):
            self.window._run('service_set', record, action, done=self._applied)

    def _applied(self, result):
        self.table.clearSelection()
        self._selection_changed()
        self.summary.setText('Service action returned. Refresh to inspect its current state; supported recovery is in Restore/history.')
        self.window._show_result(result)
