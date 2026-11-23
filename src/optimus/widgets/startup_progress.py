"""Nonmodal progress for the saved startup update and scan jobs."""
from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QAbstractItemView, QDialog, QHBoxLayout, QHeaderView, QLabel, QProgressBar,
    QPushButton, QTableWidget, QTableWidgetItem, QVBoxLayout,
)


class StartupProgressDialog(QDialog):
    """A view of existing work; hiding or reopening never dispatches work."""

    cancelRequested = Signal()

    def __init__(self, jobs, parent=None):
        super().__init__(parent)
        self.setWindowTitle('Optimus — startup updates and scans')
        self.setModal(False)
        self.resize(850, 520)
        self.setMinimumSize(480, 340)
        self.running = False
        self.stopping = False
        self._jobs = []
        self._rows = {}
        self._states = {}
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
        self.table = QTableWidget(0, 3)
        self.table.setHorizontalHeaderLabels(['Job', 'Status', 'Details'])
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setWordWrap(True)
        self.table.verticalHeader().hide()
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.Fixed)
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.Fixed)
        self.table.horizontalHeader().setSectionResizeMode(2, QHeaderView.Stretch)
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
        self._jobs = [(str(job[0]), str(job[1])) for job in jobs]
        self._rows = {key: row for row, (key, _title) in enumerate(self._jobs)}
        self._states = {key: 'pending' for key, _title in self._jobs}
        self.table.setRowCount(len(self._jobs))
        for row, (_key, title) in enumerate(self._jobs):
            for column, text in enumerate((title, 'Queued', 'Waiting for its turn.')):
                self.table.setItem(row, column, QTableWidgetItem(text))
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
        self.table.setColumnWidth(0, max(115, min(240, round((self.width() - 24) * .28))))
        self.table.setColumnWidth(1, 92)
        self.table.resizeRowsToContents()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if hasattr(self, 'table'):
            self._fit_table()

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
            row = self._rows[key]
            self._states[key] = status
            self.table.item(row, 1).setText(status.replace('_', ' ').capitalize())
            detail = self._details(event)
            if event.get('seconds') is not None:
                detail += f' ({event["seconds"]} seconds)'
            self.table.item(row, 2).setText(detail)
            self.table.item(row, 2).setToolTip(detail)
            self.table.resizeRowToContents(row)
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
