"""Passive scan snapshot and local history; never starts a hardware check."""
from PySide6.QtCore import Qt, QThreadPool, QTimer, Slot
from PySide6.QtWidgets import QDialog, QDialogButtonBox, QLabel, QVBoxLayout

from .hardware_health_panel import HardwareHealthPanel

_MISSING = object()
_HISTORY_WORKERS = {}  # Own the Python runnable/signals until global-pool completion.


class SnapshotHealthDialog(QDialog):
    def __init__(self, backend, report, parent=None, *, history=_MISSING):
        super().__init__(parent)
        self.backend, self._worker, self._closed = backend, None, False
        self.setWindowTitle('Optimus — hardware snapshot and history')
        self.resize(850, 650)
        self.setMinimumSize(480, 360)
        layout = QVBoxLayout(self)
        self.snapshot_note = QLabel('Saved scan snapshot. History is read from local records only. '
            'This window does not run another battery or drive check.')
        self.snapshot_note.setTextFormat(Qt.PlainText)
        self.snapshot_note.setWordWrap(True)
        self.snapshot_note.setMinimumWidth(0)
        layout.addWidget(self.snapshot_note)
        self.panel = HardwareHealthPanel(self)
        layout.addWidget(self.panel, 1)
        self.history_status = QLabel('Loading local hardware history…')
        self.history_status.setTextFormat(Qt.PlainText)
        self.history_status.setWordWrap(True)
        layout.addWidget(self.history_status)
        self.buttons = QDialogButtonBox(QDialogButtonBox.Close, self)
        self.buttons.button(QDialogButtonBox.Close).setToolTip('Close this saved snapshot. Maintenance continues and local history reads finish independently.')
        self.buttons.rejected.connect(self.close)
        layout.addWidget(self.buttons)
        self.set_report(report)
        self._history_timer = QTimer(self)
        self._history_timer.setSingleShot(True)
        self._history_timer.timeout.connect(self._load_history)
        if history is _MISSING:
            self._history_timer.start(0)
        else:
            self._history_result(history)

    def set_report(self, report):
        self.panel.set_report(report)
        status = self.panel.status.text()
        self.panel.set_busy(True)
        self.panel.status.setText(status + ' — saved snapshot; no new hardware check.')
        self.panel.refresh_button.setToolTip('Snapshot only. Use Settings when maintenance is idle to request a fresh hardware check.')

    @Slot()
    def _load_history(self):
        if self._closed or self._worker is not None:
            return
        try:
            from ..ui import BackendWorker
            operation = getattr(self.backend, 'hardware_health_history', None)
            if not callable(operation):
                raise ValueError('Local history is unavailable.')
            worker = BackendWorker(operation, (), {})
            self._worker = worker
            token = id(worker)
            _HISTORY_WORKERS[token] = worker
            worker.signals.result.connect(self._history_result)
            worker.signals.error.connect(self._history_error)
            worker.signals.finished.connect(self._history_finished)
            # This callback retains only an integer, never the dialog or a
            # runnable cycle. Bound QObject slots auto-disconnect on deletion.
            worker.signals.finished.connect(lambda token=token: _HISTORY_WORKERS.pop(token, None))
            QThreadPool.globalInstance().start(worker)
        except Exception:
            if self._worker is not None:
                _HISTORY_WORKERS.pop(id(self._worker), None)
                self._worker = None
            self._history_error('')

    @Slot(object)
    def _history_result(self, history):
        if self._closed:
            return
        if not isinstance(history, dict) or history.get('status') == 'unavailable' or not isinstance(history.get('series'), list):
            self._history_error('')
            return
        self.panel.set_history(history)
        self.history_status.setText('Local history loaded. Missing readings remain gaps; hardware was not checked again.')

    @Slot(str)
    def _history_error(self, _message):
        if not self._closed:
            self.history_status.setText('History unavailable. The saved scan snapshot remains visible.')

    @Slot()
    def _history_finished(self):
        self._worker = None

    def done(self, result):
        self._closed = True
        self._history_timer.stop()
        super().done(result)  # Close, Escape and reject never wait for the global pool.
