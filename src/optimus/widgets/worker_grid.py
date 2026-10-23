"""Equal-width, responsive worker cards without starting work or extra windows."""
from __future__ import annotations
import re
from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QGridLayout, QLayout, QLabel, QVBoxLayout, QWidget


class WorkerGrid(QWidget):
    def __init__(self, factory, parent=None):
        super().__init__(parent)
        self.factory, self.cards, self._columns = factory, {}, 0
        self.grid = QGridLayout(self)
        self.grid.setSizeConstraint(QLayout.SetNoConstraint)
        self.grid.setContentsMargins(0, 0, 0, 0)
        self.grid.setSpacing(7)
        self.setMinimumWidth(0)

    def ensure(self, identity, title):
        if identity in self.cards:
            return self.cards[identity]
        if not isinstance(identity, str) or not re.fullmatch(r'[a-z][a-z0-9_.-]{0,63}', identity) or len(self.cards) >= 12:
            return None
        self.cards[identity] = self.factory(str(title)[:100], self)
        self.reflow(force=True)
        return self.cards[identity]

    def update_jobs(self, jobs):
        if not isinstance(jobs, list):
            return
        for job in jobs[:12]:
            if isinstance(job, dict):
                card = self.ensure(job.get('id'), job.get('title') or str(job.get('id', '')).replace('_', ' ').title())
                if card is not None:
                    card.update_state(job)

    def reflow(self, *, force=False):
        columns = max(1, min(3, self.width() // 245))
        if columns == self._columns and not force:
            return
        self._columns = columns
        while self.grid.count():
            self.grid.takeAt(0)
        for index, card in enumerate(self.cards.values()):
            self.grid.addWidget(card, index // columns, index % columns)
        for index in range(3):
            self.grid.setColumnStretch(index, 1 if index < columns else 0)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self.reflow()


class WorkerBoard(QWidget):
    """Status-only polling; services own all work and durable outcomes."""
    def __init__(self, window):
        super().__init__(window)
        from ..ui import WorkflowWorkerCard
        self.backend = window.backend
        layout = QVBoxLayout(self)
        note = QLabel('Additional workers appear here when a tool is running. Status polling uses cached results and never starts scans, cleanup or installers.')
        note.setWordWrap(True)
        layout.addWidget(note)
        self.grid = WorkerGrid(WorkflowWorkerCard, self)
        layout.addWidget(self.grid)
        layout.addStretch()
        self.timer = QTimer(self)
        self.timer.setInterval(1000)
        self.timer.timeout.connect(self.refresh)
        self._closed = False

    def showEvent(self, event):
        super().showEvent(event)
        if not self._closed:
            self.refresh()
            self.timer.start()

    def hideEvent(self, event):
        self.timer.stop()
        super().hideEvent(event)

    def refresh(self):
        status = getattr(self.backend, 'worker_status', None)
        if callable(status):
            self.grid.update_jobs(status())
        monitor = getattr(self.backend, 'smart_maintenance_status', None)
        if callable(monitor):
            saved = monitor()
            if isinstance(saved, dict) and (saved.get('running') is True or 'smart_cleaning' in self.grid.cards):
                self.grid.update_jobs([dict(id='smart_cleaning', title='Smart cleaning',
                    status=saved.get('status', 'waiting'), description=saved.get('note', ''),
                    current=None, total=None)])

    def shutdown(self):
        self._closed = True
        self.timer.stop()
