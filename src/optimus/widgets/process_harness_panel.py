"""Read-only process review followed by explicitly selected, reversible controls."""
from __future__ import annotations

from datetime import datetime
import json
import math
import threading
import time

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (QCheckBox, QFileDialog, QHBoxLayout, QLabel, QLineEdit,
                              QListWidget, QListWidgetItem, QPlainTextEdit, QPushButton,
                              QSplitter, QVBoxLayout, QWidget)


def _value(record, *keys, default=''):
    if not isinstance(record, dict):
        return default
    lower = {str(key).casefold(): value for key, value in record.items()}
    for key in keys:
        if key.casefold() in lower:
            return lower[key.casefold()]
    return default


def _text(value):
    if value is None:
        return 'Unavailable'
    if isinstance(value, (dict, list)):
        return json.dumps(value, indent=2, ensure_ascii=False, default=str)
    return str(value)


def _expiry(value):
    try:
        result = float(value) if isinstance(value, (int, float)) else datetime.fromisoformat(str(value).replace('Z', '+00:00')).timestamp()
        return result if math.isfinite(result) else 0.0
    except (ValueError, TypeError, OverflowError, OSError):
        return 0.0


class ProcessHarnessPanel(QWidget):
    def __init__(self, window):
        super().__init__(window)
        self.window = window
        self._plan = None
        self._processes = []
        self._scan_cancel = threading.Event()
        self._closed = False
        self._timer = QTimer(self)
        self._timer.setInterval(1000)
        self._timer.timeout.connect(self._selection_changed)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        intro = QLabel('Scan running processes and their observed program/launch evidence, then choose eligible changes. '
                       'Nothing is selected automatically. Cleanup can pause selected application processes or disable '
                       'selected startup registrations; application files remain installed. Save work before pausing an app.')
        intro.setWordWrap(True)
        layout.addWidget(intro)
        bar = QHBoxLayout()
        self.scan_button = QPushButton('Scan processes and cleanup choices')
        self.scan_button.setObjectName('Primary')
        self.scan_button.clicked.connect(self.scan)
        bar.addWidget(self.scan_button)
        self.cancel_button = QPushButton('Cancel scan')
        self.cancel_button.setEnabled(False)
        self.cancel_button.clicked.connect(self.cancel_scan)
        bar.addWidget(self.cancel_button)
        self.export_button = QPushButton('Export redacted scan')
        self.export_button.setEnabled(False)
        self.export_button.clicked.connect(self.export_scan)
        bar.addWidget(self.export_button)
        bar.addStretch()
        layout.addLayout(bar)
        self.summary = QLabel('No scan collected. Essential processes are hidden from the table until you choose Show protected.')
        self.summary.setTextFormat(Qt.PlainText)
        self.summary.setWordWrap(True)
        layout.addWidget(self.summary)
        filters = QHBoxLayout()
        self.search = QLineEdit()
        self.search.setPlaceholderText('Filter program, process, PID or path')
        self.search.textChanged.connect(self._filter)
        filters.addWidget(self.search, 1)
        self.show_protected = QCheckBox('Show protected / essential processes')
        self.show_protected.toggled.connect(self._filter)
        filters.addWidget(self.show_protected)
        layout.addLayout(filters)
        from optimus.ui import DataTable
        self.table = DataTable([
            ('Program', lambda row: _value(row, 'application', 'program', 'app_name', 'AppName', 'root_program', 'RootProgram', 'name')),
            ('Process', 'name|Name'), ('PID', 'pid|ProcessId'),
            ('RAM MB', lambda row: round(float(_value(row, 'memory_rss', default=0) or 0) / 1048576, 1)),
            ('Protection', lambda row: _value(row, 'protection_reason', 'reason', 'ProtectionReason') if self._protected(row) else 'Eligible for review'),
            ('Program path', 'exe|Path')])
        self.table.itemSelectionChanged.connect(self._process_selected)
        layout.addWidget(self.table, 2)
        detail_bar = QHBoxLayout()
        self.details_button = QPushButton('Full selected process details')
        self.details_button.setEnabled(False)
        self.details_button.clicked.connect(self.full_details)
        detail_bar.addWidget(self.details_button)
        detail_bar.addStretch()
        layout.addLayout(detail_bar)
        splitter = QSplitter(Qt.Horizontal)
        self.details = QPlainTextEdit()
        self.details.setReadOnly(True)
        self.details.setPlaceholderText('Select a process to review its owning program, launch evidence and protection reason.')
        splitter.addWidget(self.details)
        action_box = QWidget()
        action_layout = QVBoxLayout(action_box)
        action_layout.setContentsMargins(0, 0, 0, 0)
        action_layout.addWidget(QLabel('ELIGIBLE CLEANUP CHOICES — select each action explicitly'))
        self.actions = QListWidget()
        self.actions.itemChanged.connect(self._selection_changed)
        action_layout.addWidget(self.actions)
        self.apply_button = QPushButton('Clean up selected choices')
        self.apply_button.setEnabled(False)
        self.apply_button.clicked.connect(self.apply_selected)
        action_layout.addWidget(self.apply_button)
        splitter.addWidget(action_box)
        layout.addWidget(splitter, 2)
        recovery = QHBoxLayout()
        wake = QPushButton('Resume paused apps')
        wake.clicked.connect(window.resume_processes)
        recovery.addWidget(wake)
        history = QPushButton('Restore / history')
        history.clicked.connect(lambda: window.select_page('Restore/history'))
        recovery.addWidget(history)
        apps = QPushButton('Review installed applications')
        apps.clicked.connect(lambda: window.select_page('Applications'))
        recovery.addWidget(apps)
        recovery.addStretch()
        layout.addLayout(recovery)
        note = QLabel('Pausing can make an application unresponsive and may retain its memory. Resume paused apps to recover; '
                      'use Restore/history for supported startup restores. Process age or high memory alone does not prove '
                      'an application is unwanted. Scan limits, inaccessible data and launch inferences remain visible in the results.')
        note.setWordWrap(True)
        layout.addWidget(note)

    @staticmethod
    def _protected(record):
        return bool(_value(record, 'protected', 'IsProtected', default=True))

    def _invalidate(self):
        self._plan = None
        self._timer.stop()
        self.actions.clear()
        self.apply_button.setEnabled(False)
        self.export_button.setEnabled(False)

    def scan(self):
        if self._closed or getattr(self.window, 'busy', False):
            return
        self._invalidate()
        self._processes = []
        self.table.populate([])
        self.details.clear()
        self._scan_cancel = threading.Event()
        self.cancel_button.setEnabled(True)
        self.summary.setText('Scanning processes and startup evidence. No process or registration is being changed.')
        self.window._run('process_harness_scan', cancel_event=self._scan_cancel, done=self._scan_ready)

    def cancel_scan(self):
        self._scan_cancel.set()
        self.cancel_button.setEnabled(False)
        self.summary.setText('Scan cancellation requested. Wait for the current inventory step to return.')

    def _scan_ready(self, report):
        if self._closed:
            return
        self._invalidate()
        self.cancel_button.setEnabled(False)
        if not isinstance(report, dict):
            self.summary.setText('The scan returned no trusted report. Run a new scan before cleanup.')
            return
        self._plan = report
        self._processes = [row for row in report.get('processes', []) if isinstance(row, dict)]
        self.export_button.setEnabled(isinstance(report.get('id'), str) and bool(report['id']))
        seen = set()
        self.actions.blockSignals(True)
        for action in report.get('actions', []):
            if not isinstance(action, dict) or action.get('kind') not in ('sleep_process', 'disable_startup'):
                continue
            action_id = action.get('id')
            if not isinstance(action_id, str) or not action_id or action_id in seen:
                continue
            seen.add(action_id)
            item = QListWidgetItem(str(action.get('label') or action['kind']))
            item.setData(Qt.UserRole, action)
            item.setToolTip(str(action.get('reason') or 'Review this exact selected action.'))
            item.setFlags(item.flags() | Qt.ItemIsUserCheckable)
            item.setCheckState(Qt.Unchecked)
            if not self._can_apply():
                item.setFlags(item.flags() & ~Qt.ItemIsEnabled)
            self.actions.addItem(item)
        self.actions.blockSignals(False)
        self._filter()
        self._selection_changed()
        if self._can_apply():
            self._timer.start()

    def _can_apply(self):
        plan = self._plan
        return bool(isinstance(plan, dict) and isinstance(plan.get('id'), str) and plan['id']
                    and plan.get('complete') is True and not plan.get('errors') and not plan.get('cancelled')
                    and not self._scan_cancel.is_set() and _expiry(plan.get('expires_at')) > time.time())

    def _filter(self, *_):
        query = self.search.text().strip().casefold()
        protected = sum(self._protected(row) for row in self._processes)
        shown = [row for row in self._processes
                 if (self.show_protected.isChecked() or not self._protected(row))
                 and (not query or query in ' '.join(_text(_value(row, key)) for key in (
                     'name', 'pid', 'exe', 'Path', 'application', 'program', 'app_name', 'AppName', 'root_program', 'RootProgram')).casefold())]
        self.table.populate(shown)
        state = 'Complete scan.' if self._can_apply() else 'Partial, cancelled, expired or unavailable scan; cleanup is disabled.'
        notes = _text(self._plan.get('notes', [])) if isinstance(self._plan, dict) else ''
        errors = _text(self._plan.get('errors', [])) if isinstance(self._plan, dict) else ''
        self.summary.setText(f'{len(self._processes):,} processes; {protected:,} protected; {len(shown):,} shown. '
                             + state + ' ' + notes + (' Errors: ' + errors if errors and errors != '[]' else ''))

    def _process_selected(self):
        record = self.table.selected()
        self.details_button.setEnabled(bool(record))
        if not record:
            self.details.clear()
            return
        lines = [
            'Process: ' + _text(_value(record, 'name', 'Name')) + ' — PID ' + _text(_value(record, 'pid', 'ProcessId')),
            'Program: ' + _text(_value(record, 'application', 'program', 'app_name', 'AppName', 'root_program', 'RootProgram', 'name')),
            'Program path: ' + _text(_value(record, 'exe', 'Path')),
            'Observed ownership evidence: ' + _text(_value(record, 'ownership_evidence', 'owners', default='Unavailable')),
            'Parent PID: ' + _text(_value(record, 'ppid', 'ParentProcessId')),
            'Parent/program evidence: ' + _text(_value(record, 'parent', 'parent_name', 'ParentName')),
            'Protection: ' + (_text(_value(record, 'protection_reason', 'reason', 'ProtectionReason')) if self._protected(record) else 'Eligible for review; no action is required.'),
            'Why running: ' + _text(_value(record, 'why_running', 'launch_evidence', 'launch_explanation', 'LaunchExplanation', 'evidence', default='Exact launch reason unavailable.')),
            'Launch explanation confidence: ' + _text(_value(record, 'why_running_confidence', default='unknown')),
            'Matched startup registrations: ' + _text(_value(record, 'startup_matches', default=[])),
            'Measured application resources (same executable grouped across processes): ' + _text(_value(record, 'metrics', default='Unavailable')),
            'Suggested next step: ' + _text(_value(record, 'recommendation', 'analysis', 'reasons', 'notes', default='No additional analysis supplied.')),
            '', 'Relationships and resource measurements are evidence, not proof that the process is unnecessary.'
        ]
        self.details.setPlainText('\n'.join(lines))

    def full_details(self):
        record = self.table.selected()
        if record:
            self.window._run('process_details', record,
                             done=lambda result: self.details.setPlainText(json.dumps(result, indent=2, ensure_ascii=False, default=str)))

    def _selected_actions(self):
        return [self.actions.item(index).data(Qt.UserRole) for index in range(self.actions.count())
                if self.actions.item(index).checkState() == Qt.Checked
                and self.actions.item(index).flags() & Qt.ItemIsEnabled]

    def _selection_changed(self, *_):
        fresh = self._can_apply()
        selected = self._selected_actions()
        self.apply_button.setEnabled(fresh and 0 < len(selected) <= 25)
        if self._plan is not None and not fresh:
            self._timer.stop()
            self.summary.setText(f'{len(self._processes):,} processes in the report. Cleanup is disabled because '
                                 'the scan is partial, cancelled, expired or unavailable. Run a fresh complete scan.')
            self.actions.blockSignals(True)
            for index in range(self.actions.count()):
                item = self.actions.item(index)
                item.setCheckState(Qt.Unchecked)
                item.setFlags(item.flags() & ~Qt.ItemIsEnabled)
            self.actions.blockSignals(False)
            self.apply_button.setToolTip('Run a fresh, complete scan before selecting cleanup choices.')
        else:
            self.apply_button.setToolTip('Select between one and 25 exact actions; nothing is chosen automatically.')

    def apply_selected(self):
        if getattr(self.window, 'busy', False):
            return
        self._selection_changed()
        selected = self._selected_actions()
        if not self._can_apply() or not 0 < len(selected) <= 25:
            self.window._error('Run a fresh, complete scan and select between one and 25 eligible cleanup choices.')
            return
        details = '\n\n'.join(str(action.get('label') or action['kind']) + '\n' + str(action.get('reason') or '') for action in selected)
        if not self.window._confirm('Review selected process cleanup',
                                    f'Apply these {len(selected)} selected pause/startup-disable changes? Save your work first. '
                                    'Pausing can make applications unresponsive and does not guarantee reclaimed RAM. '
                                    'Use Resume paused apps to wake them, and Restore/history for supported startup restores. '
                                    'Application files stay installed.', details=details, phrase='APPLY SELECTED CHANGES'):
            return
        # The review dialog can remain open past expiry; never apply an expired selection.
        if not self._can_apply():
            self._selection_changed()
            self.window._error('The scan expired or was cancelled while reviewing. Run a new scan.')
            return
        request = {'plan_id': self._plan['id'], 'selected_ids': [action['id'] for action in selected], 'confirmed': True}
        self._invalidate()
        self.window._run('process_harness_apply', request, done=self._applied)

    def _applied(self, result):
        self._invalidate()
        self.summary.setText('Selected cleanup returned results. Review each applied, skipped or failed item. Run a new scan before another cleanup.')
        self.details.setPlainText(json.dumps(result, indent=2, ensure_ascii=False, default=str))

    def export_scan(self):
        if getattr(self.window, 'busy', False) or not isinstance(self._plan, dict) or not self._plan.get('id'):
            return
        destination, _filter = QFileDialog.getSaveFileName(self, 'Export redacted process scan', 'Optimus-process-review.json', 'JSON files (*.json)')
        if destination:
            self.window._run('process_harness_export', self._plan['id'], destination,
                             done=lambda result: self.details.setPlainText(json.dumps(result, indent=2, ensure_ascii=False, default=str)))

    def shutdown(self):
        self._closed = True
        self._scan_cancel.set()
        self._timer.stop()
        self._invalidate()
