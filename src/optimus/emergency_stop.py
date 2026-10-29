"""Lifetime stop latch and launch ownership for one Optimus application.

Only children created by ``spawn_disposable`` may be terminated. Native package
installers and Windows operations remain observed until their real receipt is
available; stopping never changes an unknown outcome into a completed one.
"""
from __future__ import annotations

from contextlib import contextmanager
import os
import signal
import subprocess
import sys
import threading
import time
import uuid
import weakref


class StopRequested(InterruptedError):
    """The lifetime latch rejected work before its native launch."""
    installer_started = False
    outcomeKnown = True
    running = False
    failure_scope = 'shared'
    failureKind = 'emergency_stop'


class _Cancellation:
    def __init__(self, controller, external):
        self.controller, self.external = controller, external

    def is_set(self):
        return self.controller.is_set() or self.external.is_set()

    def set(self):
        setter = getattr(self.external, 'set', None)
        if callable(setter):
            setter()

    def wait(self, timeout=None):
        deadline = None if timeout is None else time.monotonic() + max(0, timeout)
        while not self.is_set():
            remaining = None if deadline is None else deadline - time.monotonic()
            if remaining is not None and remaining <= 0:
                return self.is_set()
            self.controller.event.wait(.025 if remaining is None else min(.025, remaining))
        return True


class _Operation:
    def __init__(self, controller, label, *, native, cancel=None):
        self.controller, self.id = controller, uuid.uuid4().hex
        self.label, self.native, self.cancel = str(label)[:200], native, cancel
        self.started_at, self.finished_at = time.time(), None
        self.started, self.known, self.error = False, False, None
        self.cancel_sent, self.process, self.pidfd = False, None, None

    def finish(self, *, known=True, error=None):
        with self.controller.lock:
            self.known = known is True
            self.error = str(error)[:300] if error else None
            if self.known:
                self.finished_at = time.time()
                self.controller._operations.pop(self.id, None)
                if self.pidfd is not None:
                    os.close(self.pidfd)
                    self.pidfd = None

    def snapshot(self):
        return {'id': self.id, 'label': self.label,
                'kind': 'native' if self.native else 'disposable',
                'operation_started': self.started,
                'outcomeKnown': self.known, 'running': not self.known,
                'cancel_requested': self.cancel_sent,
                'status': 'waiting_for_native_completion' if self.native and self.controller.is_set()
                          else 'stopping' if self.controller.is_set() else 'running',
                'error': self.error}


class EmergencyStop:
    def __init__(self):
        self.event, self.lock = threading.Event(), threading.RLock()
        self._registration_lock, self._start_lock = threading.RLock(), threading.Lock()
        self._operations, self._events, self._services = {}, [], []
        self._stop_thread = None

    def is_set(self):
        return self.event.is_set()

    def check(self):
        if self.is_set():
            raise StopRequested('Optimus was stopped. Restart it explicitly to begin new work.')

    def bind_cancel(self, event=None):
        """Return an event-compatible token; an ordinary clear cannot reset STOP."""
        if isinstance(event, _Cancellation) and event.controller is self:
            return event
        event = event if event is not None else threading.Event()
        with self._registration_lock:
            # Keep only weak registrations so completed runs cannot accumulate.
            self._events = [value for value in self._events if value() is not None]
            if callable(getattr(event, 'set', None)):
                try:
                    self._events.append(weakref.ref(event))
                except TypeError:
                    pass  # The combined token still observes the latch.
                if self.is_set():
                    event.set()
        return _Cancellation(self, event)

    @contextmanager
    def launch_gate(self):
        """Serialize only the start boundary, never a process's running time."""
        with self.lock:
            self.check()
            yield

    def begin_native(self, label, *, cancel=None):
        with self.launch_gate():
            value = _Operation(self, label, native=True, cancel=cancel)
            self._operations[value.id] = value
            return value

    def register_service(self, stop_if_idle):
        """An owned persistent host may close only after its requests are idle."""
        with self._registration_lock:
            self._services.append(weakref.WeakMethod(stop_if_idle))

    def spawn_disposable(self, arguments, *, label='Read-only worker', **options):
        """Own the original Popen handle, never adopt a PID or process name."""
        with self.launch_gate():
            operation = _Operation(self, label, native=False)
            child = subprocess.Popen(arguments, **options)
            operation.process, operation.started = child, True
            if sys.platform != 'win32' and hasattr(os, 'pidfd_open') and hasattr(signal, 'pidfd_send_signal'):
                try:
                    operation.pidfd = os.pidfd_open(child.pid)
                except OSError:
                    pass  # Without a retained identity handle, never signal a PID.
            self._operations[operation.id] = operation
        def observe():
            try:
                child.wait()
                operation.finish()
            except Exception as error:
                operation.finish(known=False, error=error)
        threading.Thread(target=observe, name='Optimus owned worker observer', daemon=True).start()
        return child

    def observe_native_child(self, child, operation):
        """Retain/drain a synchronous native command after its caller times out.

        This closes only the controller's running-command entry. The caller's
        durable unverified installation receipt is never rewritten to success.
        ConPTY and Windows asynchronous jobs use their own stronger observers.
        """
        def observe():
            try:
                child.communicate()
                operation.finish()
            except Exception as error:
                operation.finish(known=False, error=error)
        threading.Thread(target=observe, name='Optimus native STOP observer', daemon=False).start()

    def request_stop(self):
        """Latch now; dispose owned read-only children outside the UI thread."""
        self.event.set()
        with self._start_lock:
            if self._stop_thread is None:
                self._stop_thread = threading.Thread(target=self._drain, name='Optimus STOP', daemon=True)
                self._stop_thread.start()
        return self.snapshot()

    def _drain(self):
        with self._registration_lock:
            events, services = list(self._events), list(self._services)
        for reference in events:
            value = reference()
            if value is not None:
                try:
                    value.set()
                except Exception:
                    pass  # A foreign token cannot prevent the lifetime latch.
        with self.lock:
            operations = list(self._operations.values())
        for operation in operations:
            try:
                if operation.native:
                    if operation.cancel is not None:
                        operation.cancel()
                        operation.cancel_sent = True
                    continue
                with self.lock:
                    if operation.known or operation.process.poll() is not None:
                        continue
                    if sys.platform == 'win32':
                        # Popen retains the original kernel HANDLE, even after
                        # PID reuse. No process enumeration/tree guessing occurs.
                        operation.process.terminate()
                        operation.cancel_sent = True
                    elif operation.pidfd is not None:
                        signal.pidfd_send_signal(operation.pidfd, signal.SIGTERM)
                        operation.cancel_sent = True
            except Exception as error:
                operation.error = str(error)[:300]
        for reference in services:
            callback = reference()
            if callback is not None:
                try:
                    callback()
                except Exception:
                    pass  # Failure to close a host cannot prove native exit.

    def snapshot(self):
        if not self.lock.acquire(blocking=False):
            # A start boundary may still own the exact child identity. STOP's
            # latch is already set; the UI must not wait for that launch/write.
            return {'stopped': self.is_set(), 'drained': False, 'disposable_running': 0,
                    'native_pending': 0, 'ownership_registration_pending': True, 'operations': []}
        try:
            rows = [value.snapshot() for value in self._operations.values()]
            return {'stopped': self.is_set(), 'drained': not rows,
                    'disposable_running': sum(row['kind'] == 'disposable' for row in rows),
                    'native_pending': sum(row['kind'] == 'native' for row in rows),
                    'ownership_registration_pending': False, 'operations': rows}
        finally:
            self.lock.release()


_ATTACH_LOCK = threading.RLock()


def controller_for(backend):
    """Attach once; shallow lane clones retain the original controller."""
    with _ATTACH_LOCK:
        controller = getattr(backend, '_emergency_stop', None)
        if not isinstance(controller, EmergencyStop):
            controller = EmergencyStop()
            backend._emergency_stop = controller
        for name in ('bridge', 'maintenance', 'native_runner'):
            service = getattr(backend, name, None)
            if service is not None:
                binder = getattr(type(service), '_bind_emergency_stop', None)
                if callable(binder):
                    binder(service, controller)
                else:
                    service.stop_controller = controller
        return controller
