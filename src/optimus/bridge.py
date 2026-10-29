"""Persistent, allowlisted PowerShell bridge. No shell command interpolation."""
from __future__ import annotations
import json
import os
import queue
import subprocess
import threading
import time
import uuid
from contextlib import nullcontext
from pathlib import Path

from .item_progress import normalize_item_progress
from .filesystem import _ordinary_data_path

# These bounded bridge handlers execute synchronously and cannot launch an
# installer or Windows asynchronous mutator. Their exact terminal error is not
# an unknown installation. Everything else keeps the conservative native guard.
_SYNCHRONOUS_TERMINAL = frozenset({'update_recovery_probe', 'system_diagnostics', 'adaptive_network',
    'audit', 'process_details', 'startup_list', 'installed_apps', 'services', 'storage_scan',
    'duplicate_scan', 'drivers_list', 'changes', 'registry_scan', 'power_plans', 'registry_audit',
    'driver_device_inventory', 'startup_set', 'startup_remove', 'service_set', 'duplicate_recycle',
    'registry_remove', 'registry_restore', 'set_power_plan'})

class PowerShellBridge:
    def __init__(self, data_dir: Path):
        self.data_dir = Path(data_dir)
        self.script = Path(__file__).with_name('engine') / 'OptimusBridge.ps1'
        self._process = None
        self._closing_process = None
        self._replies = queue.Queue()
        self._lock = threading.RLock()
        self._busy = False
        self._pending_lock = threading.Lock()
        self._pending = {}
        self._progress_active = set()
        self._progress = {}
        self._cancellation_paths = {}
        self._errors: list[str] = []
        self.stop_controller = None
        self._stop_operations = {}

    def _bind_emergency_stop(self, controller):
        if self.stop_controller is not controller:
            self.stop_controller = controller
            controller.register_service(self._stop_idle_host)

    def _stop_idle_host(self):
        # Requests are registered inside the same launch gate. Once STOP is
        # latched no new request can race this check. Never kill a busy host.
        with self._pending_lock:
            if self._pending or self._stop_operations or not self.stop_controller.is_set():
                return
            process = self._process
            if process is not None and process.poll() is None:
                process.terminate()  # Original Windows Popen HANDLE only.

    def _complete_stop_operation(self, identity, response):
        with self._pending_lock:
            entry = self._stop_operations.get(identity)
        if entry is None:
            return
        ticket, operation = entry
        result = response.get('result')
        lifecycle = isinstance(result, dict) and any(key in result for key in ('outcomeKnown', 'running', 'installer_started'))
        known = (result.get('outcomeKnown') is True and result.get('running') is False
                 and type(result.get('installer_started')) is bool) if lifecycle else (
                     response.get('ok') is True or operation in _SYNCHRONOUS_TERMINAL)
        ticket.finish(known=known, error=None if known else 'Native completion remains unverified.')
        if known:
            with self._pending_lock:
                self._stop_operations.pop(identity, None)

    def _start(self):
        self.data_dir.mkdir(parents=True, exist_ok=True)
        executable = Path(os.environ.get('SystemRoot', r'C:\Windows')) / 'System32/WindowsPowerShell/v1.0/powershell.exe'
        self._replies = queue.Queue()
        # This host is Windows PowerShell 5.1. A PS7/Codex parent can export Core
        # modules with the same names; restrict this child to its own OS modules.
        child_environment = {key: value for key, value in os.environ.items() if key.casefold() != 'psmodulepath'}
        child_environment['PSModulePath'] = str(executable.parent / 'Modules')
        with self.stop_controller.launch_gate() if self.stop_controller else nullcontext():
            process = subprocess.Popen([str(executable), '-NoLogo', '-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass',
                                        '-File', str(self.script), '-DataRoot', str(self.data_dir)], stdin=subprocess.PIPE,
                                       stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding='utf-8', errors='replace',
                                       bufsize=1, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0), env=child_environment)
            self._process = process
            self._closing_process = None
        target_queue = self._replies
        def read_output():
            try:
                for line in process.stdout:
                    try:
                        value = json.loads(line.lstrip('\ufeff'))
                        if isinstance(value, dict) and 'id' in value:
                            identity = value.get('id')
                            if (not isinstance(identity, str) or len(identity) != 32
                                    or any(char not in '0123456789abcdef' for char in identity)):
                                continue
                            if value.get('kind') == 'progress':
                                payload = value.get('progress')
                                identified = isinstance(payload, dict) and ('key' in payload or 'label' in payload)
                                normalized = normalize_item_progress(payload if identified else
                                    dict(payload, key='native', label='Native operation')) if isinstance(payload, dict) else None
                                if normalized is not None:
                                    with self._pending_lock:
                                        pending = self._pending.get(identity)
                                        if identity in self._progress_active and pending is not None and pending[1] == process.pid:
                                            # Coalesce display-only ticks: timeout/slow consumers cannot
                                            # build an unbounded stdout queue or block the installer.
                                            self._progress[identity] = {key: field for key, field in normalized.items()
                                                                       if identified or key not in ('key', 'label')}
                                continue
                            if value.get('kind') not in (None, 'result') or type(value.get('ok')) is not bool:
                                continue
                            self._clear_cancellation_marker(identity)
                            with self._pending_lock:
                                self._pending.pop(identity, None)
                            self._complete_stop_operation(identity, value)
                            target_queue.put(value)
                    except json.JSONDecodeError:
                        continue
            finally:
                def clear_exited_pending():
                    with self._pending_lock:
                        self._pending = {key: value for key, value in self._pending.items() if value[1] != process.pid}
                try:
                    exited = process.poll() is not None
                except OSError:
                    exited = False
                if exited:
                    clear_exited_pending()
                else:
                    if self._closing_process is not process:
                        def await_native_exit():
                            try:
                                process.wait()
                                if process.poll() is not None:
                                    clear_exited_pending()
                            except Exception:
                                pass  # No EOF/observer error proves an active native child stopped.
                        threading.Thread(target=await_native_exit, daemon=True).start()
                target_queue.put({'id': None, 'ok': False, 'error': ('PowerShell engine exited.' if exited else
                    'PowerShell output ended while native engine exit remains unverified; check the operation before retrying.')})
        def read_errors():
            for line in process.stderr:
                self._errors.append(line.rstrip())
                self._errors[:] = self._errors[-12:]
        threading.Thread(target=read_output, daemon=True).start()
        threading.Thread(target=read_errors, daemon=True).start()

    def request(self, operation: str, timeout: float = 180, *, progress_callback=None, cancel_event=None, **arguments):
        if progress_callback is not None and not callable(progress_callback):
            raise ValueError('An optional progress callback must be callable.')
        if cancel_event is not None and operation not in ('driver_updates_scan', 'driver_update_apply'):
            raise ValueError('This native operation has no supported cooperative cancellation contract.')
        controller = self.stop_controller
        if controller is not None:
            controller.check()
            if operation in ('driver_updates_scan', 'driver_update_apply'):
                cancel_event = controller.bind_cancel(cancel_event)
        with self._lock:
            if controller is not None:
                controller.check()
            if self._process is None or self._process.poll() is not None:
                self._start()
            request_id = uuid.uuid4().hex
            cancel_directory = None
            terminal = False
            if cancel_event is not None:
                # The native side derives this exact path from its request ID;
                # callers cannot supply a file path or a different job identity.
                cancel_directory = self.data_dir / 'request-cancellation' / request_id
                _ordinary_data_path(cancel_directory)
                cancel_directory.mkdir(parents=True, exist_ok=False)
                _ordinary_data_path(cancel_directory)
                info = cancel_directory.lstat()
                with self._pending_lock:
                    self._cancellation_paths[request_id] = {
                        'directory': cancel_directory, 'identity': (info.st_dev, info.st_ino),
                        'marker_identity': None, 'lock': threading.RLock(), 'closed': False}
                arguments = dict(arguments, cooperativeCancellation=True)
            cancellation_sent = False
            submitted = False
            self._busy = True
            with self._pending_lock:
                self._pending[request_id] = (operation, self._process.pid)
                if progress_callback is not None:
                    self._progress_active.add(request_id)
            def emit_progress():
                with self._pending_lock:
                    value = self._progress.pop(request_id, None)
                if value is not None and progress_callback is not None:
                    try:
                        progress_callback(value)
                    except Exception:
                        pass  # Display failure cannot cancel or replay a native operation.
            def request_cancellation(*, force=False):
                nonlocal cancellation_sent
                if cancel_directory is None or cancellation_sent or not (force or cancel_event.is_set()):
                    return
                with self._pending_lock:
                    record = self._cancellation_paths.get(request_id)
                if record is None:
                    return
                try:
                    # Both the request loop and STOP callback can arrive here.
                    # Publish identity before terminal retirement can inspect
                    # the file, and never write after that request has closed.
                    with record['lock']:
                        if record['closed'] or cancellation_sent:
                            return
                        self._check_cancellation_directory(request_id)
                        with (cancel_directory / 'cancel.request').open('x', encoding='ascii') as marker:
                            marker.write('cancel\n')
                            info = os.fstat(marker.fileno())
                            record['marker_identity'] = (info.st_dev, info.st_ino)
                        cancellation_sent = True
                except (OSError, ValueError, KeyError):
                    # Failure to request abort never authorizes abandoning or
                    # killing the still-owned native operation.
                    pass
            try:
                with controller.launch_gate() if controller else nullcontext():
                    if controller is not None:
                        ticket = controller.begin_native('Windows operation: ' + operation,
                            cancel=(lambda: request_cancellation(force=True)) if cancel_directory is not None else None)
                        ticket.started = True
                        with self._pending_lock:
                            self._stop_operations[request_id] = (ticket, operation)
                    request_cancellation()  # An already-set local token precedes launch.
                    submitted = True  # A partial write is conservatively unresolved.
                    self._process.stdin.write(json.dumps({'id': request_id, 'operation': operation, 'arguments': arguments}, separators=(',', ':')) + '\n')
                    self._process.stdin.flush()
                deadline = time.monotonic() + timeout
                while True:
                    request_cancellation()
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise TimeoutError('The operation is still running. Check its outcome before retrying; Optimus does not replay changes.')
                    emit_progress()
                    try:
                        response = self._replies.get(timeout=min(remaining, 0.1) if progress_callback is not None or cancel_event is not None else remaining)
                    except queue.Empty as error:
                        if progress_callback is not None or cancel_event is not None:
                            continue
                        raise TimeoutError('Operation timeout. Check the outcome before trying again.') from error
                    emit_progress()
                    if response.get('id') not in (None, request_id):
                        continue
                    terminal = response.get('id') == request_id
                    if not response.get('ok'):
                        detail = response.get('error') or 'PowerShell operation failed.'
                        if response.get('id') is None and self._errors:
                            detail += ' ' + self._errors[-1]
                        raise RuntimeError(detail)
                    return response.get('result')
            finally:
                try:
                    exited_pid = self._process.pid if self._process.poll() is not None else None
                except OSError:
                    exited_pid = None
                with self._pending_lock:
                    if not submitted:
                        self._pending.pop(request_id, None)
                    if exited_pid is not None:
                        self._pending = {key: value for key, value in self._pending.items() if value[1] != exited_pid}
                    self._progress_active.discard(request_id)
                    self._progress.pop(request_id, None)
                self._busy = False
                if cancel_directory is not None and (not submitted or terminal or exited_pid is not None):
                    # Never remove an in-flight request marker on a timeout:
                    # its native worker still needs to observe the request.
                    self._clear_cancellation_marker(request_id)

    def _clear_cancellation_marker(self, request_id):
        with self._pending_lock:
            record = self._cancellation_paths.get(request_id)
        if record is not None:
            with record['lock']:
                # A callback already captured by STOP may still be queued.
                # Closing its lifecycle precedes filesystem cleanup, including
                # failed cleanup of substituted paths that must be preserved.
                record['closed'] = True
                try:
                    self._check_cancellation_directory(request_id)
                    directory = record['directory']
                    marker = directory / 'cancel.request'
                    info = _ordinary_data_path(marker, file=True)
                    if info is not None:
                        if (info.st_dev, info.st_ino) != record['marker_identity']:
                            return  # A substituted marker is never deletion authority.
                        marker.unlink()
                    directory.rmdir()
                    with self._pending_lock:
                        if self._cancellation_paths.get(request_id) is record:
                            self._cancellation_paths.pop(request_id, None)
                except (OSError, ValueError, KeyError):
                    pass

    def _check_cancellation_directory(self, request_id):
        with self._pending_lock:
            record = self._cancellation_paths[request_id]
        directory = record['directory']
        _ordinary_data_path(directory)
        info = directory.lstat()
        if (info.st_dev, info.st_ino) != record['identity']:
            raise ValueError('The owned native cancellation directory changed.')

    def close(self):
        with self._pending_lock:
            if self._pending:
                return  # Caller timeouts do not cancel in-flight Windows operations.
        with self._lock:
            with self._pending_lock:
                if self._pending:
                    return
            process = self._process
            if process and process.poll() is None:
                self._closing_process = process
                process.stdin.close()
                try:
                    process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    process.terminate()
            self._process = None


