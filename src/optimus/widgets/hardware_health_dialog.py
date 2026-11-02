"""Saved hardware observations, with explicit asynchronous read-only refresh."""
import threading

from PySide6.QtCore import Qt, QThreadPool, QTimer, Signal, Slot
from PySide6.QtWidgets import QDialog, QDialogButtonBox, QLabel, QVBoxLayout

from .hardware_health_panel import HardwareHealthPanel

_MISSING = object()
_HISTORY_WORKERS = {}  # Own the Python runnable/signals until global-pool completion.


class SnapshotHealthDialog(QDialog):
    reportRefreshed = Signal(object)

    def __init__(self, backend, report, parent=None, *, history=_MISSING):
        super().__init__(parent)
        self.backend, self._worker, self._closed = backend, None, False
        self._operation, self._refresh_pending = None, False
        self._refresh_cancel = threading.Event()
        self.destroyed.connect(lambda _object=None, event=self._refresh_cancel: event.set())
        self.setWindowTitle('Optimus — hardware snapshot and history')
        self.resize(850, 650)
        self.setMinimumSize(480, 360)
        layout = QVBoxLayout(self)
        self.snapshot_note = QLabel('Saved scan snapshot. Opening this view reads local history only. '
            'Refresh hardware check requests a fresh read-only observation and updates local history; maintenance can continue.')
        self.snapshot_note.setTextFormat(Qt.PlainText)
        self.snapshot_note.setWordWrap(True)
        self.snapshot_note.setMinimumWidth(0)
        self.snapshot_note.setTextInteractionFlags(Qt.TextSelectableByMouse | Qt.TextSelectableByKeyboard)
        layout.addWidget(self.snapshot_note)
        self.panel = HardwareHealthPanel(self)
        self.panel.refreshRequested.connect(self._refresh_hardware)
        layout.addWidget(self.panel, 1)
        self.history_status = QLabel('Loading local hardware history…')
        self.history_status.setTextFormat(Qt.PlainText)
        self.history_status.setWordWrap(True)
        self.history_status.setTextInteractionFlags(Qt.TextSelectableByMouse | Qt.TextSelectableByKeyboard)
        layout.addWidget(self.history_status)
        self.buttons = QDialogButtonBox(QDialogButtonBox.Close, self)
        self.buttons.button(QDialogButtonBox.Close).setToolTip('Close this hardware view without waiting. Maintenance continues; an outstanding read stops at its supported cancellation boundary.')
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
        self.panel.status.setText(status + ' — saved snapshot. Refresh requests a new read-only check.')
        self.panel.refresh_button.setEnabled(self._operation != 'refresh' and not self._refresh_pending)

    def _start_worker(self, operation, kwargs, *, kind):
        from ..ui import BackendWorker
        worker = BackendWorker(operation, (), kwargs)
        self._worker, self._operation = worker, kind
        token = id(worker)
        _HISTORY_WORKERS[token] = worker
        worker.signals.result.connect(self._details_result if kind == 'refresh' else self._history_result)
        worker.signals.error.connect(self._refresh_error if kind == 'refresh' else self._history_error)
        worker.signals.finished.connect(self._history_finished)
        # Only the token outlives this QObject; closing never destroys an active
        # runnable or keeps the dialog alive through a callback closure.
        worker.signals.finished.connect(lambda token=token: _HISTORY_WORKERS.pop(token, None))
        QThreadPool.globalInstance().start(worker)

    @Slot()
    def _load_history(self):
        if self._closed or self._worker is not None:
            return
        try:
            operation = getattr(self.backend, 'hardware_health_history', None)
            if not callable(operation):
                raise ValueError('Local history is unavailable.')
            self._start_worker(operation, {}, kind='history')
        except Exception:
            if self._worker is not None:
                _HISTORY_WORKERS.pop(id(self._worker), None)
                self._worker, self._operation = None, None
            self._history_error('')

    @Slot()
    def _refresh_hardware(self):
        if self._closed or self._operation == 'refresh' or self._refresh_pending:
            return
        self._history_timer.stop()
        self._status_before_refresh = self.panel.status.text().removesuffix(' · refresh unavailable')
        self.panel.set_busy(True)
        self.history_status.setText('Reading fresh battery and physical-drive telemetry, then loading local history…')
        if self._worker is not None:
            self._refresh_pending = True
            return
        self._start_refresh()

    @Slot()
    def _start_refresh(self):
        if self._closed or self._worker is not None:
            return
        self._refresh_pending = False
        self._refresh_cancel.clear()
        self.panel.set_busy(True)
        self.history_status.setText('Reading fresh battery and physical-drive telemetry, then loading local history…')
        try:
            operation = getattr(self.backend, 'hardware_health_details', None)
            if not callable(operation):
                raise ValueError('Fresh hardware details are unavailable.')
            self._start_worker(operation, {'refresh': True, 'force_refresh': True,
                'cancel_event': self._refresh_cancel}, kind='refresh')
        except Exception:
            if self._worker is not None:
                _HISTORY_WORKERS.pop(id(self._worker), None)
                self._worker, self._operation = None, None
            self._refresh_error('')
            self.panel.refresh_button.setEnabled(not self._closed)

    @Slot(object)
    def _details_result(self, result):
        if self._closed:
            return
        report = result.get('report') if isinstance(result, dict) else None
        if not isinstance(report, dict) or report.get('read_only') is not True:
            self._refresh_error('')
            return
        self.panel.set_report(report)
        self.panel.refresh_button.setEnabled(False)
        self.panel.status.setText(self.panel.status.text() + ' — fresh read-only observation.')
        self.snapshot_note.setText('Latest hardware observation. Refresh hardware check reads native provider telemetry '
            'and records available local history. It does not repair disks, run self-tests, install drivers or stop maintenance.')
        history = result.get('history')
        if not self._apply_history(history):
            self.history_status.setText('Fresh hardware readings loaded. Local history is unavailable; prior graph data remains visible.')
        else:
            self.history_status.setText('Fresh hardware readings and local history loaded. Missing measurements remain gaps.')
        self.reportRefreshed.emit(self.panel.report)

    @Slot(str)
    def _refresh_error(self, _message):
        if not self._closed:
            self.panel.status.setText(getattr(self, '_status_before_refresh', 'Saved hardware observation')
                + ' · refresh unavailable')
            self.history_status.setText('Hardware refresh unavailable. The saved readings remain visible; try Refresh again.')

    def _apply_history(self, history):
        if not isinstance(history, dict) or history.get('status') == 'unavailable' or not isinstance(history.get('series'), list):
            return False
        self.panel.set_history(history)
        return True

    @Slot(object)
    def _history_result(self, history):
        if self._closed:
            return
        if not self._apply_history(history):
            self._history_error('')
            return
        if not self._refresh_pending:
            self.history_status.setText('Local history loaded. Missing readings remain gaps; hardware was not checked again.')

    @Slot(str)
    def _history_error(self, _message):
        if not self._closed:
            self.history_status.setText('History unavailable. The saved scan snapshot remains visible.')

    @Slot()
    def _history_finished(self):
        self._worker, self._operation = None, None
        if self._closed:
            return
        if self._refresh_pending:
            self._start_refresh()
        else:
            self.panel.refresh_button.setEnabled(True)

    def _stop_pending_read(self):
        self._closed = True
        self._refresh_pending = False
        self._refresh_cancel.set()
        if hasattr(self, '_history_timer'):
            self._history_timer.stop()

    def close(self):
        self._stop_pending_read()  # Also covers a not-yet-shown dialog.
        return super().close()

    def closeEvent(self, event):
        self._stop_pending_read()
        super().closeEvent(event)

    def done(self, result):
        self._stop_pending_read()
        super().done(result)  # Close, Escape and reject never wait for the global pool.
