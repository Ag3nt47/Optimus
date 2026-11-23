"""Nonmodal progress for the saved startup update and scan jobs."""
from __future__ import annotations

from PySide6.QtCore import QEvent, Qt, Signal
from PySide6.QtWidgets import (
    QAbstractItemView, QDialog, QHBoxLayout, QHeaderView, QLabel, QProgressBar,
    QPushButton, QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget,
)

from ..item_progress import normalize_item_progress
from .compact_worker_ring import CompactWorkerActivityRing


class StartupProgressDialog(QDialog):
    """A view of existing work; hiding or reopening never dispatches work."""

    cancelRequested = Signal()
    skipRequested = Signal(str)

    def __init__(self, jobs, parent=None):
        super().__init__(parent)
        self.setWindowTitle('Optimus — startup updates and scans')
        self.setModal(False)
        self.resize(900, 650)
        self.setMinimumSize(480, 340)
        self.running = False
        self.stopping = False
        self._jobs = []
        self._rows = {}
        self._states = {}
        self.rings = {}
        self._ring_states = {}
        self.skip_buttons = {}
        self._skip_requested = set()
        layout = QVBoxLayout(self)
        self.title = QLabel('STARTUP UPDATES AND SCANS')
        self.title.setWordWrap(True)
        layout.addWidget(self.title)
        note = QLabel('These jobs use your saved choices and approvals. Hiding this window keeps them running. '
                      'Open Optimus from its tray icon to return to this view.')
        note.setWordWrap(True)
        note.setTextFormat(Qt.PlainText)
        layout.addWidget(note)
        self.status = QLabel()
        self.status.setWordWrap(True)
        self.status.setTextFormat(Qt.PlainText)
        self.status.setTextInteractionFlags(Qt.TextSelectableByMouse | Qt.TextSelectableByKeyboard)
        layout.addWidget(self.status)
        self.progress = QProgressBar()
        self.progress.setTextVisible(False)
        layout.addWidget(self.progress)
        self.summary = QLabel()
        self.summary.setWordWrap(True)
        layout.addWidget(self.summary)
        self.table = QTableWidget(0, 5)
        self.table.setHorizontalHeaderLabels(['Job', 'Status', 'Details', 'Progress', 'Skip'])
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setWordWrap(True)
        # Cell widgets need their full ring/caption space after the inherited
        # table theme adds padding; compact columns must not clip controls.
        self.table.setStyleSheet('QTableWidget::item {padding: 1px 2px;} '
                                 'QHeaderView::section {padding: 6px 2px;}')
        self.table.verticalHeader().hide()
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.Fixed)
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.Fixed)
        self.table.horizontalHeader().setSectionResizeMode(2, QHeaderView.Stretch)
        self.table.horizontalHeader().setSectionResizeMode(3, QHeaderView.Fixed)
        self.table.horizontalHeader().setSectionResizeMode(4, QHeaderView.Fixed)
        self.table.horizontalHeaderItem(3).setToolTip('Current operation percentage reported by its provider. '
            '— means unknown; ~ marks an estimate. Job and package counts are separate.')
        layout.addWidget(self.table, 1)
        buttons = QHBoxLayout()
        buttons.addStretch()
        self.cancel_button = QPushButton('Stop startup jobs')
        self.cancel_button.setAutoDefault(False)
        self.cancel_button.clicked.connect(self.request_cancel)
        buttons.addWidget(self.cancel_button)
        self.hide_button = QPushButton('Hide')
        self.hide_button.setAutoDefault(False)
        self.hide_button.clicked.connect(self.hide)
        buttons.addWidget(self.hide_button)
        layout.addLayout(buttons)
        self.begin(jobs)

    def begin(self, jobs):
        for ring in self.rings.values():
            ring.timer.stop()
        self.rings = {}
        self._ring_states = {}
        self.skip_buttons = {}
        self._skip_requested = set()
        self._jobs = [(str(job[0]), str(job[1])) for job in jobs]
        self._rows = {key: row for row, (key, _title) in enumerate(self._jobs)}
        self._states = {key: 'pending' for key, _title in self._jobs}
        self.table.setRowCount(len(self._jobs))
        for row, (key, title) in enumerate(self._jobs):
            for column, text in enumerate((title, 'Queued', 'Waiting for its turn.')):
                self.table.setItem(row, column, QTableWidgetItem(text))
            holder = QWidget(self.table)
            ring_layout = QHBoxLayout(holder)
            ring_layout.setContentsMargins(4, 4, 4, 4)
            ring_layout.addStretch()
            ring = self.rings[key] = CompactWorkerActivityRing(holder)
            ring.setAccessibleName(title + ' current operation progress')
            ring_layout.addWidget(ring)
            ring_layout.addStretch()
            self.table.setCellWidget(row, 3, holder)
            ring.set_progress(None, 'unverified', active=False, phase='waiting', description='Queued; no operation has started.')
            skip_holder = QWidget(self.table)
            skip_layout = QHBoxLayout(skip_holder)
            skip_layout.setContentsMargins(2, 4, 2, 4)
            skip_layout.addStretch()
            button = self.skip_buttons[key] = QPushButton('Skip', skip_holder)
            button.setAutoDefault(False)
            button.setFixedWidth(44)
            button.setStyleSheet('QPushButton {padding: 4px;} '
                                'QPushButton:pressed {padding: 5px 3px 3px 5px;}')
            button.setAccessibleName('Skip ' + title)
            button.setToolTip('Skip this startup job only. An active operation finishes at its supported cancellation boundary.')
            button.clicked.connect(lambda checked=False, job=key: self.request_skip(job))
            skip_layout.addWidget(button)
            skip_layout.addStretch()
            self.table.setCellWidget(row, 4, skip_holder)
        self.running = True
        self.stopping = False
        self.cancel_button.setEnabled(True)
        self.hide_button.setText('Hide')
        self.progress.setRange(0, 0)
        self.status.setText('Starting the saved startup jobs…')
        self._fit_table()
        self._update_summary()

    def _fit_table(self):
        # Long job names must wrap instead of consuming the details column on
        # small screens. Keep status readable and give details the free width.
        narrow = self.width() < 650
        self.table.setColumnWidth(0, 90 if narrow else max(140, min(240, round((self.width() - 24) * .25))))
        self.table.setColumnWidth(1, 80 if narrow else 92)
        self.table.setColumnWidth(3, 60 if narrow else 64)
        self.table.setColumnWidth(4, 56)
        self.table.horizontalHeaderItem(3).setText('Item %' if narrow else 'Progress')
        self.table.resizeRowsToContents()
        for row in range(self.table.rowCount()):
            self.table.setRowHeight(row, max(66, self.table.rowHeight(row)))

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if hasattr(self, 'table'):
            self._fit_table()

    def _sync_rings(self):
        for ring in self.rings.values():
            ring._sync()

    def showEvent(self, event):
        super().showEvent(event)
        self._sync_rings()

    def hideEvent(self, event):
        for ring in self.rings.values():
            ring.timer.stop()
        super().hideEvent(event)

    def changeEvent(self, event):
        super().changeEvent(event)
        if event.type() == QEvent.WindowStateChange and hasattr(self, 'rings'):
            self._sync_rings()

    def _update_ring(self, key, status, event):
        detail = event.get('detail')
        detail = detail if isinstance(detail, dict) else {}
        result = event.get('result')
        result = result if isinstance(result, dict) else {}
        # A queue fraction, package count, elapsed time or CPU counter cannot
        # stand in for the current item's native progress. Missing progress
        # deliberately clears the old item/phase value instead of retaining it.
        item = normalize_item_progress(detail.get('item_progress'))
        verified_scan = (key == 'defender' and status in ('completed', 'failed', 'needs_review', 'completed_with_errors')
                         and (detail.get('completion_verified') is True or result.get('completion_verified') is True))
        if verified_scan:
            item = {'key': 'security:defender:quick_scan', 'label': 'Virus, malware and spyware scan',
                    'phase': 'scan', 'percent': 100}
        self._ring_states[key] = dict(status=status, item=item, verified_scan=verified_scan,
                                     description=str(event.get('description') or event.get('title') or key))
        self._paint_ring(key)

    def _paint_ring(self, key):
        state = self._ring_states.get(key, {})
        status = state.get('status', 'pending')
        active = self.running and status == 'running'
        item = state.get('item') if active or state.get('verified_scan') else None
        percent = item.get('percent') if item is not None else None
        estimated = item is not None and item.get('estimated') is True
        health = ('working' if active else 'complete' if status == 'completed' else
                  'error' if status in ('failed', 'error', 'uncertain', 'blocked', 'needs_review', 'completed_with_errors') else
                  'waiting' if status in ('running', 'cancelled', 'skipped', 'deferred') else 'unverified')
        phase = item.get('phase', '') if item is not None else (
            'scan' if active and key == 'defender' else 'working' if active else 'waiting' if status == 'pending' else '')
        description = state.get('description', '')
        if item is not None:
            description = item['label'] + ' · ' + item['phase'].replace('_', ' ') + '. ' + description
        description += (' Current operation: ' + ('approximately ' if estimated else '') + str(int(percent)) + '% reported.'
                        if percent is not None else ' No current operation percentage has been reported.')
        description += ' Job and package counts are separate.'
        self.rings[key].set_progress(percent, health, active=active, description=description,
                                    phase=phase, estimated=estimated)

    def _update_summary(self):
        counts = {}
        for state in self._states.values():
            counts[state] = counts.get(state, 0) + 1
        terminal = sum(count for state, count in counts.items() if state not in ('pending', 'running'))
        details = ', '.join(f'{count} {state.replace("_", " ")}' for state, count in counts.items()
                            if state not in ('pending', 'running'))
        self.summary.setText(f'{terminal} of {len(self._jobs)} jobs finished' + (': ' + details if details else '.'))
        if not self.running:
            self.progress.setRange(0, max(1, len(self._jobs)))
            self.progress.setValue(terminal)

    def request_skip(self, key):
        if (not self.running or self.stopping or key in self._skip_requested
                or self._states.get(key) not in ('pending', 'running')):
            return
        self._skip_requested.add(key)
        self.skip_buttons[key].setEnabled(False)
        row = self._rows[key]
        self.table.item(row, 1).setText('Skipping safely…' if self._states[key] == 'running' else 'Skip requested')
        self.skipRequested.emit(key)

    def skip_response(self, key, accepted):
        if accepted or key not in self.skip_buttons:
            return
        # A provider can finish between the click and the request reaching the
        # controller. Its terminal update remains authoritative.
        self._skip_requested.discard(key)
        self.skip_buttons[key].setEnabled(self.running and not self.stopping
            and self._states.get(key) in ('pending', 'running'))
        row = self._rows[key]
        self.table.item(row, 1).setText(self._states[key].replace('_', ' ').capitalize())

    @staticmethod
    def _details(event):
        if event.get('error'):
            return str(event['error'])[:2000]
        value = event.get('result', event.get('detail'))
        if isinstance(value, dict):
            for key in ('note', 'summary', 'description', 'error', 'reason', 'status'):
                if value.get(key):
                    return str(value[key])[:2000]
        if isinstance(value, str):
            return value[:2000]
        return str(event.get('description') or '')[:2000]

    def update_progress(self, event):
        if not isinstance(event, dict):
            return
        key, status = event.get('job'), str(event.get('status') or 'running')
        if key in self._rows:
            if status == 'running' and self._states[key] not in ('pending', 'running'):
                # A cooperative skip can finish on the worker before its
                # acknowledgement reaches Qt. A late running frame must not
                # revive that finished job while the next job is active.
                return
            row = self._rows[key]
            self._states[key] = status
            if event.get('skip_requested') is True:
                self._skip_requested.add(key)
            self.table.item(row, 1).setText('Skipping safely…' if status == 'running' and key in self._skip_requested
                                          else status.replace('_', ' ').capitalize())
            self.skip_buttons[key].setEnabled(self.running and not self.stopping
                and status in ('pending', 'running') and key not in self._skip_requested)
            detail = self._details(event)
            if event.get('seconds') is not None:
                detail += f' ({event["seconds"]} seconds)'
            self.table.item(row, 2).setText(detail)
            self.table.item(row, 2).setToolTip(detail)
            self._update_ring(key, status, event)
            self.table.resizeRowToContents(row)
            self.table.setRowHeight(row, max(66, self.table.rowHeight(row)))
            if status == 'running' and not self.stopping:
                self.status.setText('Running: ' + str(event.get('description') or event.get('title') or key))
        elif key is None and status != 'running':
            self.finish(status, self._details(event))
        self._update_summary()

    def set_report(self, report):
        if not isinstance(report, dict):
            return
        for entry in report.get('jobs') or []:
            if isinstance(entry, dict):
                self.update_progress(entry)
        self.finish(str(report.get('status') or 'unknown'),
                    str(report.get('error') or report.get('detail') or ''))

    def finish(self, status, detail=''):
        self.running = False
        for key in self.rings:
            self._paint_ring(key)
            self.skip_buttons[key].setEnabled(False)
        self.cancel_button.setEnabled(False)
        self.hide_button.setText('Close')
        messages = {
            'completed': 'Startup jobs finished. Review each job’s result below.',
            'failed': 'Some startup jobs failed. Review their results below.',
            'cancelled': 'Startup jobs stopped. Review completed and cancelled items below.',
            'busy': 'Startup jobs are waiting for another maintenance run.',
            'disabled': 'Startup jobs are disabled in your saved choices.',
        }
        self.status.setText(messages.get(status, 'Startup jobs: ' + status.replace('_', ' '))
                            + ('\n' + detail[:2000] if detail else ''))
        self._update_summary()

    def mark_stopping(self):
        if not self.running:
            return
        self.stopping = True
        self.cancel_button.setEnabled(False)
        for button in self.skip_buttons.values():
            button.setEnabled(False)
        self.status.setText('Stopping startup jobs safely. The active operation can finish before remaining jobs are cancelled.')

    def request_cancel(self):
        if not self.running or self.stopping:
            return
        self.mark_stopping()
        self.cancelRequested.emit()

    def reject(self):
        self.hide()

    def closeEvent(self, event):
        event.ignore()
        self.hide()
