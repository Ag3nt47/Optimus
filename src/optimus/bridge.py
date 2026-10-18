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

class PowerShellBridge:
    def __init__(self, data_dir: Path):
        self.data_dir = Path(data_dir)
        self.script = Path(__file__).with_name('engine') / 'OptimusBridge.ps1'
        self._process = None
        self._replies = queue.Queue()
        self._lock = threading.RLock()
        self._busy = False
        self._pending_lock = threading.Lock()
        self._pending = {}
        self._errors: list[str] = []

    def _start(self):
        self.data_dir.mkdir(parents=True, exist_ok=True)
        executable = Path(os.environ.get('SystemRoot', r'C:\Windows')) / 'System32/WindowsPowerShell/v1.0/powershell.exe'
        self._replies = queue.Queue()
        process = subprocess.Popen([str(executable), '-NoLogo', '-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass',
                                    '-File', str(self.script), '-DataRoot', str(self.data_dir)], stdin=subprocess.PIPE,
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding='utf-8', errors='replace',
                                   bufsize=1, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        self._process = process
        target_queue = self._replies
        def read_output():
            try:
                for line in process.stdout:
                    try:
                        value = json.loads(line.lstrip('\ufeff'))
                        if isinstance(value, dict) and 'id' in value:
                            with self._pending_lock:
                                self._pending.pop(value.get('id'), None)
                            target_queue.put(value)
                    except json.JSONDecodeError:
                        continue
            finally:
                with self._pending_lock:
                    self._pending = {key: value for key, value in self._pending.items() if value[1] != process.pid}
                target_queue.put({'id': None, 'ok': False, 'error': 'PowerShell engine exited.'})
        def read_errors():
            for line in process.stderr:
                self._errors.append(line.rstrip())
                self._errors[:] = self._errors[-12:]
        threading.Thread(target=read_output, daemon=True).start()
        threading.Thread(target=read_errors, daemon=True).start()

    def request(self, operation: str, timeout: float = 180, **arguments):
        with self._lock:
            if self._process is None or self._process.poll() is not None:
                self._start()
            request_id = uuid.uuid4().hex
            self._busy = True
            with self._pending_lock:
                self._pending[request_id] = (operation, self._process.pid)
            try:
                self._process.stdin.write(json.dumps({'id': request_id, 'operation': operation, 'arguments': arguments}, separators=(',', ':')) + '\n')
                self._process.stdin.flush()
                deadline = time.monotonic() + timeout
                while True:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise TimeoutError('The operation is still running. Check its outcome before retrying; Optimus does not replay changes.')
                    try:
                        response = self._replies.get(timeout=remaining)
                    except queue.Empty as error:
                        raise TimeoutError('Operation timeout. Check the outcome before trying again.') from error
                    if response.get('id') not in (None, request_id):
                        continue
                    if not response.get('ok'):
                        detail = response.get('error') or 'PowerShell operation failed.'
                        if response.get('id') is None and self._errors:
                            detail += ' ' + self._errors[-1]
                        raise RuntimeError(detail)
                    return response.get('result')
            finally:
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
                process.stdin.close()
                try:
                    process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    process.terminate()
            self._process = None


