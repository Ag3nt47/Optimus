"""Persistent, allowlisted PowerShell bridge. No shell command interpolation."""
from __future__ import annotations
import json
import os
import queue
import subprocess
import threading
import time
import uuid
from pathlib import Path

from .item_progress import normalize_item_progress

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
        self._errors: list[str] = []

    def _start(self):
        self.data_dir.mkdir(parents=True, exist_ok=True)
        executable = Path(os.environ.get('SystemRoot', r'C:\Windows')) / 'System32/WindowsPowerShell/v1.0/powershell.exe'
        self._replies = queue.Queue()
        # This host is Windows PowerShell 5.1. A PS7/Codex parent can export Core
        # modules with the same names; restrict this child to its own OS modules.
        child_environment = {key: value for key, value in os.environ.items() if key.casefold() != 'psmodulepath'}
        child_environment['PSModulePath'] = str(executable.parent / 'Modules')
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
                                normalized = normalize_item_progress(dict(payload, key='native', label='Native operation')) if isinstance(payload, dict) else None
                                if normalized is not None:
                                    with self._pending_lock:
                                        pending = self._pending.get(identity)
                                        if identity in self._progress_active and pending is not None and pending[1] == process.pid:
                                            # Coalesce display-only ticks: timeout/slow consumers cannot
                                            # build an unbounded stdout queue or block the installer.
                                            self._progress[identity] = {key: normalized[key] for key in ('phase', 'percent')}
                                continue
                            if value.get('kind') not in (None, 'result') or type(value.get('ok')) is not bool:
                                continue
                            with self._pending_lock:
                                self._pending.pop(identity, None)
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

    def request(self, operation: str, timeout: float = 180, *, progress_callback=None, **arguments):
        if progress_callback is not None and not callable(progress_callback):
            raise ValueError('An optional progress callback must be callable.')
        with self._lock:
            if self._process is None or self._process.poll() is not None:
                self._start()
            request_id = uuid.uuid4().hex
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
            try:
                self._process.stdin.write(json.dumps({'id': request_id, 'operation': operation, 'arguments': arguments}, separators=(',', ':')) + '\n')
                self._process.stdin.flush()
                deadline = time.monotonic() + timeout
                while True:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise TimeoutError('The operation is still running. Check its outcome before retrying; Optimus does not replay changes.')
                    emit_progress()
                    try:
                        response = self._replies.get(timeout=min(remaining, 0.1) if progress_callback is not None else remaining)
                    except queue.Empty as error:
                        if progress_callback is not None:
                            continue
                        raise TimeoutError('Operation timeout. Check the outcome before trying again.') from error
                    emit_progress()
                    if response.get('id') not in (None, request_id):
                        continue
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
                    if exited_pid is not None:
                        self._pending = {key: value for key, value in self._pending.items() if value[1] != exited_pid}
                    self._progress_active.discard(request_id)
                    self._progress.pop(request_id, None)
                self._busy = False

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


