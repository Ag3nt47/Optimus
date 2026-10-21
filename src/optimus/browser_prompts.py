"""Explicit browser-close questions marshalled from maintenance to the GUI thread."""
from __future__ import annotations

from dataclasses import dataclass, field
import copy
import threading
import time

from PySide6.QtCore import QObject, QThread, QTimer, Qt, Signal, Slot
from PySide6.QtWidgets import QApplication, QMessageBox


@dataclass(eq=False)
class _PendingPrompt:
    info: dict
    cancel: object
    deadline: float
    done: threading.Event = field(default_factory=threading.Event)
    approved: bool = False
    lock: threading.Lock = field(default_factory=threading.Lock)

    def cancelled(self):
        return bool(self.cancel is not None and self.cancel.is_set())

    def expired(self):
        return time.monotonic() >= self.deadline

    def resolve(self, approved=False):
        with self.lock:
            if not self.done.is_set():
                self.approved = bool(approved and not self.cancelled() and not self.expired())
                self.done.set()


class BrowserPromptBridge(QObject):
    """Own GUI dialogs while a worker waits for a fresh, bounded decision.

    Construct this object on the application thread before starting the worker.
    ``request`` only emits a queued signal and waits; it never creates widgets.
    Cancelling, closing the application, timing out or losing the GUI returns No.
    The optional ``ask`` callable is a GUI-thread test seam, not a saved policy.
    """

    requested = Signal(object)

    def __init__(self, parent=None, *, ask=None, response_timeout=300.0):
        super().__init__(parent)
        application = QApplication.instance()
        if application is None or QThread.currentThread() != application.thread():
            raise RuntimeError('Browser questions must be initialized on the application thread.')
        self._ask = ask
        self._response_timeout = max(0.01, float(response_timeout))
        self._pending = set()
        self._lock = threading.Lock()
        self._closed = False
        self.requested.connect(self._show_request, Qt.QueuedConnection)

    def request(self, info, cancel_event=None):
        # Waiting on the GUI thread would prevent delivery of the queued slot.
        if QThread.currentThread() == self.thread() or not isinstance(info, dict):
            return False
        if cancel_event is not None and cancel_event.is_set():
            return False
        pending = _PendingPrompt(copy.deepcopy(info), cancel_event,
                                 time.monotonic() + self._response_timeout)
        with self._lock:
            if self._closed:
                return False
            self._pending.add(pending)
        try:
            self.requested.emit(pending)
            while not pending.done.wait(0.05):
                if pending.cancelled() or pending.expired():
                    pending.resolve(False)
            return bool(pending.approved and not pending.cancelled() and not pending.expired())
        except RuntimeError:
            # A QObject destroyed during application shutdown cannot approve work.
            return False
        finally:
            pending.resolve(False)
            with self._lock:
                self._pending.discard(pending)

    @Slot(object)
    def _show_request(self, pending):
        if self._closed or pending.done.is_set() or pending.cancelled() or pending.expired():
            pending.resolve(False)
            return
        try:
            approved = self._ask(pending.info) if self._ask is not None else self._ask_dialog(pending)
        except Exception:
            approved = False
        pending.resolve(approved)

    def _ask_dialog(self, pending):
        name = str(pending.info.get('browser') or pending.info.get('browser_id') or 'this browser')
        dialog = QMessageBox(self.parent())
        dialog.setWindowTitle('Optimus — close ' + name + '?')
        dialog.setIcon(QMessageBox.Question)
        dialog.setTextFormat(Qt.PlainText)
        dialog.setText('Close ' + name + ' for this optimization run?')
        dialog.setInformativeText(
            'Save unfinished work first. Yes asks this browser to close normally; all of its '
            'windows and profiles may close. If cookie cleanup is selected, websites may sign you out.\n\n'
            'No skips this browser’s cleanup for this run and lets the remaining optimization continue. '
            'Optimus will not force-terminate the browser. If it cannot close in time, its cleanup is skipped.')
        processes = pending.info.get('processes')
        if isinstance(processes, list) and processes:
            details = []
            for record in processes[:40]:
                if isinstance(record, dict):
                    details.append(str(record.get('name') or name) + ' — PID ' + str(record.get('pid', '?')))
            if details:
                dialog.setDetailedText('Detected browser processes:\n' + '\n'.join(details))
        dialog.setStandardButtons(QMessageBox.Yes | QMessageBox.No)
        dialog.setDefaultButton(QMessageBox.No)
        dialog.setEscapeButton(QMessageBox.No)
        timer = QTimer(dialog)
        timer.setInterval(50)

        def dismiss_if_cancelled():
            if self._closed or pending.done.is_set() or pending.cancelled() or pending.expired():
                pending.resolve(False)
                dialog.done(QMessageBox.No)

        timer.timeout.connect(dismiss_if_cancelled)
        timer.start()
        try:
            return dialog.exec() == QMessageBox.Yes
        finally:
            timer.stop()
            dialog.deleteLater()

    def shutdown(self):
        """Thread-safe: release pending workers even if the GUI stops processing."""
        with self._lock:
            self._closed = True
            pending = tuple(self._pending)
        for request in pending:
            request.resolve(False)
