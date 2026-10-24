"""A private ConPTY supervisor for observed WinGet progress on Windows.

WinGet 1.29 suppresses progress with redirected stdout. A pseudoconsole restores
its native progress output. The supervisor owns only this console, starts the
unchanged argument vector, and proves that no other console client remains.
Closing a pseudoconsole can terminate clients, so a timeout never closes it.
"""
from __future__ import annotations

import base64
import codecs
import ctypes
from ctypes import wintypes as w
import hashlib
import json
import math
import ntpath
import os
import re
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path

_FLAG = '--winget-console-monitor'
_RETAINED = set()
_RETAINED_LOCK = threading.RLock()
_MAX_RECEIPT = 8192


class ConsolePreflightError(RuntimeError):
    """Verified failure before any supervisor or target process was created."""
    installer_started = False
    outcomeKnown = True
    running = False


class ConsoleOutcomeError(RuntimeError):
    """A supervisor started and the target's terminal state is unknown."""
    installer_started = True
    outcomeKnown = False
    running = True


class _Coord(ctypes.Structure):
    _fields_ = [('X', w.SHORT), ('Y', w.SHORT)]


class _StartupInfo(ctypes.Structure):
    _fields_ = [('cb', w.DWORD), ('lpReserved', w.LPWSTR), ('lpDesktop', w.LPWSTR), ('lpTitle', w.LPWSTR),
                ('dwX', w.DWORD), ('dwY', w.DWORD), ('dwXSize', w.DWORD), ('dwYSize', w.DWORD),
                ('dwXCountChars', w.DWORD), ('dwYCountChars', w.DWORD), ('dwFillAttribute', w.DWORD),
                ('dwFlags', w.DWORD), ('wShowWindow', w.WORD), ('cbReserved2', w.WORD),
                ('lpReserved2', ctypes.POINTER(w.BYTE)), ('hStdInput', w.HANDLE),
                ('hStdOutput', w.HANDLE), ('hStdError', w.HANDLE)]


class _StartupInfoEx(ctypes.Structure):
    _fields_ = [('StartupInfo', _StartupInfo), ('lpAttributeList', ctypes.c_void_p)]


class _ProcessInfo(ctypes.Structure):
    _fields_ = [('hProcess', w.HANDLE), ('hThread', w.HANDLE), ('dwProcessId', w.DWORD), ('dwThreadId', w.DWORD)]


class _SecurityAttributes(ctypes.Structure):
    _fields_ = [('nLength', w.DWORD), ('lpSecurityDescriptor', ctypes.c_void_p), ('bInheritHandle', w.BOOL)]


class _JobAccounting(ctypes.Structure):
    _fields_ = [('TotalUserTime', ctypes.c_longlong), ('TotalKernelTime', ctypes.c_longlong),
                ('ThisPeriodTotalUserTime', ctypes.c_longlong), ('ThisPeriodTotalKernelTime', ctypes.c_longlong),
                ('TotalPageFaultCount', w.DWORD), ('TotalProcesses', w.DWORD),
                ('ActiveProcesses', w.DWORD), ('TotalTerminatedProcesses', w.DWORD)]


def _arguments(values):
    if (not isinstance(values, (list, tuple)) or not 1 <= len(values) <= 256
            or any(not isinstance(value, str) or not value or '\0' in value for value in values)
            or not (ntpath.isabs(values[0]) or os.path.isabs(values[0]))):
        raise ValueError('Use one exact executable path and bounded argument vector.')
    return list(values)


def _digest(arguments):
    return hashlib.sha256(json.dumps(arguments, ensure_ascii=True, separators=(',', ':')).encode('ascii')).hexdigest()


def _frozen_console_executable():
    # The main PyInstaller application is GUI-subsystem/windowed and cannot
    # attach to ConPTY. The shipped console bootloader shares its _internal.
    helper = Path(sys.executable).with_name('OptimusConsole.exe')
    if (not helper.is_file() or helper.is_symlink()
            or getattr(helper.lstat(), 'st_file_attributes', 0) & 0x400):
        raise FileNotFoundError('The shipped OptimusConsole.exe progress helper is unavailable.')
    return str(helper)


def _source_console_executable():
    # A source launcher can itself be pythonw.exe. Its trusted base path must
    # select the sibling console interpreter; GUI executables cannot attach to
    # ConPTY. Do not search PATH or fall back to another Python installation.
    executable = Path(getattr(sys, '_base_executable', None) or sys.executable)
    if executable.name.casefold() == 'pythonw.exe':
        executable = executable.with_name('python.exe')
    if not executable.is_absolute() or not executable.is_file():
        raise FileNotFoundError('The source console Python interpreter is unavailable.')
    if (executable.is_symlink()
            or getattr(executable.lstat(), 'st_file_attributes', 0) & 0x400):
        raise FileNotFoundError('The source console Python interpreter must be an ordinary file.')
    return str(executable)


def _helper_arguments(receipt_handle, nonce, arguments, *, allow_resident_handoff=False):
    values = ({'schema': 1, 'arguments': arguments, 'resident_handoff': True}
              if allow_resident_handoff else arguments)
    payload = base64.b64encode(json.dumps(values, ensure_ascii=True, separators=(',', ':')).encode('ascii')).decode('ascii')
    tail = [str(receipt_handle), nonce, payload]
    if getattr(sys, 'frozen', False):
        return [_frozen_console_executable(), _FLAG, *tail]
    # The Windows venv redirector can remain a second console client. Invoke
    # the base interpreter directly, with only this source package added.
    executable = _source_console_executable()
    source = str(Path(__file__).resolve().parents[1])
    # The trusted base interpreter bypasses a venv redirector, so -I also
    # bypasses its site-packages. Add only the already-imported dependency's
    # actual package directory; native private-job image checks need psutil.
    dependency_code = ''
    try:
        import psutil
    except ModuleNotFoundError:
        if allow_resident_handoff:
            raise  # Optional feature cannot start without its native identity dependency.
    else:
        dependency = str(Path(psutil.__file__).resolve().parents[1])
        dependency_code = 'sys.path.append(' + repr(dependency) + ');'
    code = ('import sys;sys.path.insert(0,' + repr(source) + ');' + dependency_code
            + 'from optimus.winget_console import monitor_main;raise SystemExit(monitor_main(sys.argv[1:]))')
    return [executable, '-I', '-u', '-c', code, *tail]


def _kernel():
    if sys.platform != 'win32':
        raise OSError('The console progress transport requires Windows.')
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    declarations = (
        ('CreatePipe', [ctypes.POINTER(w.HANDLE), ctypes.POINTER(w.HANDLE), ctypes.c_void_p, w.DWORD], w.BOOL),
        ('CreatePseudoConsole', [_Coord, w.HANDLE, w.HANDLE, w.DWORD, ctypes.POINTER(ctypes.c_void_p)], ctypes.c_long),
        ('ClosePseudoConsole', [ctypes.c_void_p], None),
        ('InitializeProcThreadAttributeList', [ctypes.c_void_p, w.DWORD, w.DWORD, ctypes.POINTER(ctypes.c_size_t)], w.BOOL),
        ('UpdateProcThreadAttribute', [ctypes.c_void_p, w.DWORD, ctypes.c_size_t, ctypes.c_void_p,
                                      ctypes.c_size_t, ctypes.c_void_p, ctypes.c_void_p], w.BOOL),
        ('DeleteProcThreadAttributeList', [ctypes.c_void_p], None),
        ('CreateProcessW', [w.LPCWSTR, w.LPWSTR, ctypes.c_void_p, ctypes.c_void_p, w.BOOL, w.DWORD,
                            ctypes.c_void_p, w.LPCWSTR, ctypes.POINTER(_StartupInfoEx), ctypes.POINTER(_ProcessInfo)], w.BOOL),
        ('ReadFile', [w.HANDLE, ctypes.c_void_p, w.DWORD, ctypes.POINTER(w.DWORD), ctypes.c_void_p], w.BOOL),
        ('WriteFile', [w.HANDLE, ctypes.c_void_p, w.DWORD, ctypes.POINTER(w.DWORD), ctypes.c_void_p], w.BOOL),
        ('CloseHandle', [w.HANDLE], w.BOOL),
        ('SetHandleInformation', [w.HANDLE, w.DWORD, w.DWORD], w.BOOL),
        ('GetFileType', [w.HANDLE], w.DWORD),
        ('WaitForSingleObject', [w.HANDLE, w.DWORD], w.DWORD),
        ('GetExitCodeProcess', [w.HANDLE, ctypes.POINTER(w.DWORD)], w.BOOL),
        ('SetStdHandle', [w.DWORD, w.HANDLE], w.BOOL),
        ('GetConsoleProcessList', [ctypes.POINTER(w.DWORD), w.DWORD], w.DWORD),
        ('CreateFileW', [w.LPCWSTR, w.DWORD, w.DWORD, ctypes.c_void_p, w.DWORD, w.DWORD, w.HANDLE], w.HANDLE),
        ('GetConsoleScreenBufferInfo', [w.HANDLE, ctypes.c_void_p], w.BOOL),
        ('CreateJobObjectW', [ctypes.c_void_p, w.LPCWSTR], w.HANDLE),
        ('QueryInformationJobObject', [w.HANDLE, ctypes.c_int, ctypes.c_void_p, w.DWORD, ctypes.POINTER(w.DWORD)], w.BOOL),
        ('IsProcessInJob', [w.HANDLE, w.HANDLE, ctypes.POINTER(w.BOOL)], w.BOOL),
        ('ResumeThread', [w.HANDLE], w.DWORD),
        ('GetCurrentProcess', [], w.HANDLE),
        ('DuplicateHandle', [w.HANDLE, w.HANDLE, w.HANDLE, ctypes.POINTER(w.HANDLE), w.DWORD, w.BOOL, w.DWORD], w.BOOL),
    )
    for name, arguments, returned in declarations:
        function = getattr(kernel, name)
        function.argtypes, function.restype = arguments, returned
    return kernel


class _TargetProcess:
    """Exact suspended target assigned atomically to a private observation job.

    No CPU, memory, UI or kill-on-close limits are configured. Descendants are
    counted before console attachment; a long-lived launched app conservatively
    keeps the parent pending instead of being terminated with its console.
    """
    def __init__(self, kernel, arguments, console_input, console_output):
        import msvcrt
        self.kernel, self.job, self.process, self.thread, self.pid = kernel, None, None, None, None
        duplicates, attributes, initialized = [], None, False
        try:
            self.job = kernel.CreateJobObjectW(None, None)
            if not self.job:
                raise ctypes.WinError(ctypes.get_last_error())
            current = kernel.GetCurrentProcess()
            for stream in (console_input, console_output):
                copied = w.HANDLE()
                if not kernel.DuplicateHandle(current, msvcrt.get_osfhandle(stream.fileno()), current,
                                               ctypes.byref(copied), 0, True, 2):
                    raise ctypes.WinError(ctypes.get_last_error())
                duplicates.append(copied.value)
            required = ctypes.c_size_t()
            kernel.InitializeProcThreadAttributeList(None, 2, 0, ctypes.byref(required))
            if not 0 < required.value <= 1024 * 1024:
                raise OSError('Native target startup attributes are unavailable.')
            attributes = ctypes.create_string_buffer(required.value)
            if not kernel.InitializeProcThreadAttributeList(attributes, 2, 0, ctypes.byref(required)):
                raise ctypes.WinError(ctypes.get_last_error())
            initialized = True
            inherited, jobs = (w.HANDLE * 2)(*duplicates), (w.HANDLE * 1)(self.job)
            for name, handles in ((0x00020002, inherited), (0x0002000D, jobs)):
                if not kernel.UpdateProcThreadAttribute(attributes, 0, name,
                        ctypes.cast(handles, ctypes.c_void_p), ctypes.sizeof(handles), None, None):
                    raise ctypes.WinError(ctypes.get_last_error())
            startup = _StartupInfoEx()
            startup.StartupInfo.cb = ctypes.sizeof(startup)
            startup.StartupInfo.dwFlags = 0x00000100
            startup.StartupInfo.hStdInput, startup.StartupInfo.hStdOutput, startup.StartupInfo.hStdError = (
                duplicates[0], duplicates[1], duplicates[1])
            startup.lpAttributeList = ctypes.cast(attributes, ctypes.c_void_p)
            information = _ProcessInfo()
            command = subprocess.list2cmdline(arguments)
            if len(command) >= 32767:
                raise ValueError('The exact target command exceeds the Windows argument bound.')
            # Atomic job assignment occurs before the initial suspended thread
            # can execute. No assignment failure can launch an untracked target.
            if not kernel.CreateProcessW(arguments[0], ctypes.create_unicode_buffer(command), None, None,
                    True, 0x00080004, None, None, ctypes.byref(startup), ctypes.byref(information)):
                raise ctypes.WinError(ctypes.get_last_error())
            self.process, self.thread, self.pid = information.hProcess, information.hThread, information.dwProcessId
            member = w.BOOL()
            if not kernel.IsProcessInJob(self.process, self.job, ctypes.byref(member)) or not member.value:
                raise RuntimeError('The suspended target job membership could not be verified.')
        except BaseException:
            # Closing these plain handles never kills a target. A created but
            # unverified target stays an unknown outcome and is not resumed.
            self.close()
            raise
        finally:
            if initialized:
                kernel.DeleteProcThreadAttributeList(attributes)
            for handle in duplicates:
                kernel.CloseHandle(handle)

    def resume(self):
        if self.kernel.ResumeThread(self.thread) != 1:
            raise RuntimeError('The exact suspended target could not be resumed once.')
        self.kernel.CloseHandle(self.thread)
        self.thread = None

    def wait(self):
        result = self.kernel.WaitForSingleObject(self.process, 0xffffffff)
        if result != 0:
            raise ctypes.WinError(ctypes.get_last_error())
        code = w.DWORD()
        if not self.kernel.GetExitCodeProcess(self.process, ctypes.byref(code)):
            raise ctypes.WinError(ctypes.get_last_error())
        return code.value

    def active_processes(self):
        accounting, returned = _JobAccounting(), w.DWORD()
        if not self.kernel.QueryInformationJobObject(self.job, 1, ctypes.byref(accounting),
                ctypes.sizeof(accounting), ctypes.byref(returned)) or returned.value != ctypes.sizeof(accounting):
            raise RuntimeError('The private target job inventory could not be verified.')
        return accounting.ActiveProcesses

    def process_rows(self):
        """Exact private-job members, including GUI images before attachment."""
        import psutil
        class ProcessIds(ctypes.Structure):
            _fields_ = [('assigned', w.DWORD), ('listed', w.DWORD),
                        ('pids', ctypes.c_size_t * 64)]
        inventory, returned = ProcessIds(), w.DWORD()
        if (not self.kernel.QueryInformationJobObject(self.job, 3, ctypes.byref(inventory),
                ctypes.sizeof(inventory), ctypes.byref(returned))
                or not 1 <= inventory.listed == inventory.assigned <= 8):
            raise RuntimeError('The remaining private job inventory is unavailable or unsupported.')
        rows = []
        for pid in inventory.pids[:inventory.listed]:
            process = psutil.Process(pid)
            rows.append({'pid': pid, 'path': process.exe(), 'created_at': process.create_time()})
        return rows

    def close(self):
        for name in ('thread', 'process', 'job'):
            handle = getattr(self, name, None)
            if handle:
                self.kernel.CloseHandle(handle)
                setattr(self, name, None)


class _ConsoleProcess:
    """Prepared native handles; no target is started until launch()."""
    def __init__(self):
        self.kernel = _kernel()
        self.handles = {}
        self.console = ctypes.c_void_p()
        self.started, self.pid, self.process = False, None, None
        self.closed = False
        try:
            for prefix in ('input', 'output', 'receipt'):
                left, right = w.HANDLE(), w.HANDLE()
                security = _SecurityAttributes(ctypes.sizeof(_SecurityAttributes), None, True) if prefix == 'receipt' else None
                if not self.kernel.CreatePipe(ctypes.byref(left), ctypes.byref(right),
                                              ctypes.byref(security) if security else None, 0):
                    raise ctypes.WinError(ctypes.get_last_error())
                self.handles[prefix + '_read'], self.handles[prefix + '_write'] = left.value, right.value
                if prefix == 'receipt' and not self.kernel.SetHandleInformation(left, 1, 0):
                    raise ctypes.WinError(ctypes.get_last_error())
            result = self.kernel.CreatePseudoConsole(_Coord(240, 40), self.handles['input_read'],
                self.handles['output_write'], 0, ctypes.byref(self.console))
            if result < 0:
                raise OSError('CreatePseudoConsole failed (HRESULT 0x' + format(result & 0xffffffff, '08X') + ').')
        except BaseException:
            self.dispose_unused()
            raise

    def _close_handle(self, name):
        value = self.handles.pop(name, None)
        if value:
            self.kernel.CloseHandle(value)

    def launch(self, nonce, arguments):
        helper = _helper_arguments(self.handles['receipt_write'], nonce, arguments,
            allow_resident_handoff=getattr(self, 'allow_resident_handoff', False))
        command = subprocess.list2cmdline(helper)
        if len(command) >= 32767:
            raise ValueError('The console progress command exceeds the Windows argument bound.')
        required = ctypes.c_size_t()
        self.kernel.InitializeProcThreadAttributeList(None, 2, 0, ctypes.byref(required))
        if not 0 < required.value <= 1024 * 1024:
            raise OSError('The native startup attribute size is unavailable.')
        attributes = ctypes.create_string_buffer(required.value)
        initialized = False
        try:
            if not self.kernel.InitializeProcThreadAttributeList(attributes, 2, 0, ctypes.byref(required)):
                raise ctypes.WinError(ctypes.get_last_error())
            initialized = True
            inherited = (w.HANDLE * 1)(self.handles['receipt_write'])
            for name, value, size in (
                    (0x00020016, self.console, ctypes.sizeof(self.console)),
                    (0x00020002, ctypes.cast(inherited, ctypes.c_void_p), ctypes.sizeof(inherited))):
                if not self.kernel.UpdateProcThreadAttribute(attributes, 0, name, value, size, None, None):
                    raise ctypes.WinError(ctypes.get_last_error())
            startup = _StartupInfoEx()
            startup.StartupInfo.cb = ctypes.sizeof(startup)
            startup.StartupInfo.dwFlags, startup.StartupInfo.wShowWindow = 1, 0
            startup.lpAttributeList = ctypes.cast(attributes, ctypes.c_void_p)
            information = _ProcessInfo()
            line = ctypes.create_unicode_buffer(command)
            if not self.kernel.CreateProcessW(helper[0], line, None, None, True, 0x00080000,
                    None, None, ctypes.byref(startup), ctypes.byref(information)):
                raise ctypes.WinError(ctypes.get_last_error())
            self.started = True
            self.pid, self.process = information.dwProcessId, information.hProcess
            self.kernel.CloseHandle(information.hThread)
        finally:
            if initialized:
                self.kernel.DeleteProcThreadAttributeList(attributes)
        # The output/receipt reader must see only the child's retained copies.
        for name in ('input_read', 'output_write', 'receipt_write'):
            self._close_handle(name)

    def read(self, channel):
        buffer, count = ctypes.create_string_buffer(4096), w.DWORD()
        if not self.kernel.ReadFile(self.handles[channel + '_read'], buffer, len(buffer), ctypes.byref(count), None):
            code = ctypes.get_last_error()
            if code in (109, 232, 233):
                return b''
            raise ctypes.WinError(code)
        return buffer.raw[:count.value]

    def poll(self):
        if not self.started:
            return None
        result = self.kernel.WaitForSingleObject(self.process, 0)
        if result == 258:
            return None
        if result != 0:
            raise ctypes.WinError(ctypes.get_last_error())
        code = w.DWORD()
        if not self.kernel.GetExitCodeProcess(self.process, ctypes.byref(code)):
            raise ctypes.WinError(ctypes.get_last_error())
        return code.value

    def dispose_unused(self):
        if self.started:
            raise RuntimeError('A launched console must keep its clients alive until verified quiescent.')
        for name in ('input_read', 'output_write', 'receipt_write'):
            self._close_handle(name)
        if self.console.value:
            self.kernel.ClosePseudoConsole(self.console)
            self.console = ctypes.c_void_p()
        for name in list(self.handles):
            self._close_handle(name)
        self.closed = True

    def close_quiescent(self):
        # Only the session's private authenticated receipt permits this call.
        if self.console.value:
            self.kernel.ClosePseudoConsole(self.console)
            self.console = ctypes.c_void_p()

    def finish(self):
        for name in list(self.handles):
            self._close_handle(name)
        if self.process:
            self.kernel.CloseHandle(self.process)
            self.process = None
        self.closed = True


class _Session:
    def __init__(self, native, arguments, callback):
        from .winget_progress import _Capture, WinGetProgressParser
        self.native, self.arguments, self.callback = native, arguments, callback
        self.nonce, self.argument_digest = uuid.uuid4().hex, _digest(arguments)
        self.output, self.reporting = _Capture(), threading.Event()
        self.launch_ready, self.output_eof, self.receipt_eof, self.finished = (threading.Event() for _ in range(4))
        self.ready, self.fault, self.terminal_error = threading.Event(), threading.Event(), threading.Event()
        self.provider_ready = threading.Event()
        self.lock, self.latest, self.download = threading.RLock(), [], []
        self.receipts, self.receipt_error = [], False
        self.returncode, self.aborted = None, False
        self.parser = WinGetProgressParser(self._remember)
        self.reporting.set()

    def _remember(self, value):
        if not self.reporting.is_set():
            return
        with self.lock:
            if value.get('phase') == 'download' and type(value.get('downloaded_bytes')) is int:
                if not self.download or value['downloaded_bytes'] >= self.download[0]['downloaded_bytes']:
                    self.download[:] = [dict(value)]
            self.latest[:] = [dict(value)]
        self.ready.set()

    def _dispatch(self):
        while self.reporting.is_set():
            self.ready.wait(0.1)
            self.ready.clear()
            with self.lock:
                value = self.latest.pop() if self.latest else None
            if value is not None and self.reporting.is_set():
                try:
                    self.callback(value)
                except Exception:
                    pass

    def _output_reader(self):
        decoder = codecs.getincrementaldecoder('utf-8')(errors='replace')
        while True:
            try:
                block = self.native.read('output')
                if not block:
                    text = decoder.decode(b'', final=True)
                    self.output.append(text)
                    if self.reporting.is_set():
                        self.parser.feed(text)
                        self.parser.finish()
                    self.output_eof.set()
                    return
                text = decoder.decode(block)
                self.output.append(text)
                if self.reporting.is_set():
                    self.parser.feed(text)
            except Exception:
                self.fault.set()
                if self.aborted:
                    return
                # A transient reader failure must not abandon a live installer
                # or make it safe to close its console. Keep draining passively.
                time.sleep(0.25)

    def _receipt_reader(self):
        data = bytearray()
        pending = bytearray()
        while True:
            try:
                block = self.native.read('receipt')
                if not block:
                    self.launch_ready.wait()
                    if not self.aborted:
                        try:
                            if pending or len(self.receipts) not in (2, 3):
                                raise ValueError('The private console receipt is incomplete.')
                        except (ValueError, UnicodeError):
                            self.receipt_error = True
                    self.receipt_eof.set()
                    return
                if len(data) + len(block) <= _MAX_RECEIPT:
                    data.extend(block)
                    pending.extend(block)
                    while b'\n' in pending:
                        line, _, tail = pending.partition(b'\n')
                        pending[:] = tail
                        try:
                            value = json.loads(bytes(line).decode('ascii'))
                            if not isinstance(value, dict) or len(self.receipts) >= 3:
                                raise ValueError('The private console receipt exceeds its bound.')
                            self.receipts.append(value)
                            if self._handoff() is not None and not self.receipt_error:
                                self.provider_ready.set()
                        except (ValueError, UnicodeError):
                            self.receipt_error = True
                else:
                    self.receipt_error = True
            except Exception:
                self.fault.set()
                if self.aborted:
                    return
                time.sleep(0.25)

    def _terminal(self):
        if not self.receipt_eof.is_set() or self.receipt_error or len(self.receipts) not in (2, 3):
            return None
        initial, final = self.receipts[0], self.receipts[-1]
        if len(self.receipts) == 3 and self._handoff() is None:
            return None
        fields = ('schema', 'nonce', 'supervisor_pid', 'argv_sha256', 'target_pid')
        if (any(initial.get(key) != final.get(key) for key in fields)
                or type(initial.get('schema')) is not int or initial['schema'] != 1
                or type(final.get('schema')) is not int or final['schema'] != 1
                or type(initial.get('supervisor_pid')) is not int or type(final.get('supervisor_pid')) is not int
                or type(final.get('target_pid')) is not int
                or initial.get('nonce') != self.nonce or initial.get('supervisor_pid') != self.native.pid
                or initial.get('argv_sha256') != self.argument_digest
                or type(initial.get('target_pid')) is not int or initial['target_pid'] <= 0
                or initial.get('event') != 'started' or final.get('event') != 'quiescent'
                or final.get('quiescent') is not True
                or type(final.get('console_client_count')) is not int or final['console_client_count'] != 1
                or final.get('job_verified') is not True
                or type(final.get('job_active_processes')) is not int or final['job_active_processes'] != 0
                or type(final.get('exit_code')) is not int or not 0 <= final['exit_code'] <= 0xffffffff):
            return None
        return final

    def _handoff(self):
        """A distinct authenticated receipt; never a quiescent/EOF receipt."""
        if self.receipt_error or len(self.receipts) < 2:
            return None
        initial, value = self.receipts[:2]
        fields = ('schema', 'nonce', 'supervisor_pid', 'argv_sha256', 'target_pid')
        proof = value.get('resident_proof')
        if (any(initial.get(key) != value.get(key) for key in fields)
                or type(initial.get('schema')) is not int or initial['schema'] != 1
                or type(value.get('schema')) is not int or value['schema'] != 1
                or initial.get('event') != 'started' or value.get('event') != 'resident_handoff'
                or initial.get('nonce') != self.nonce or initial.get('supervisor_pid') != self.native.pid
                or initial.get('argv_sha256') != self.argument_digest
                or type(initial.get('target_pid')) is not int or initial['target_pid'] <= 0
                or type(value.get('exit_code')) is not int or value['exit_code'] != 0
                or value.get('quiescent') is not False or value.get('job_verified') is not True
                or type(value.get('console_client_count')) is not int or value['console_client_count'] != 1
                or type(value.get('job_active_processes')) is not int or not 1 <= value['job_active_processes'] <= 8
                or not isinstance(proof, dict) or proof.get('kind') != 'verified-ollama-resident-runtime-v1'
                or proof.get('target_pid') != value['target_pid'] or proof.get('restart_required') is not False
                or proof.get('package_id') != 'Ollama.Ollama'
                or not isinstance(proof.get('processes'), list)
                or len(proof['processes']) != value['job_active_processes']
                or len(self.arguments) < 9 or self.arguments[3] != proof.get('package_id')
                or self.arguments[8] != proof.get('version')):
            return None
        for key in ('winget_log_sha256', 'installer_log_sha256'):
            if not isinstance(proof.get(key), str) or re.fullmatch(r'[a-f0-9]{64}', proof[key]) is None:
                return None
        from .resident_update_completion import verified_resident_handoff
        transport = subprocess.CompletedProcess(self.arguments, 0, '', '')
        transport.native_completion_verified, transport.capture_complete = True, False
        transport.console_receipt = dict(value, resident_handoff_verified=True,
            output_eof=False, supervisor_exit_code=None, target_exit_code=0)
        if not verified_resident_handoff(transport, self.arguments):
            return None
        return value

    def _monitor(self):
        self.launch_ready.wait()
        if self.aborted:
            return
        while True:
            try:
                exited = self.native.poll()
                terminal = self._terminal()
                if exited is not None and terminal is not None and exited == 0:
                    # The target is finished, no attached clients remain, and
                    # the supervisor itself has exited. Drain the final frame.
                    self.native.close_quiescent()
                    self.output_eof.wait()
                    self.returncode = terminal['exit_code']
                    self.native.finish()
                    self.finished.set()
                    with _RETAINED_LOCK:
                        _RETAINED.discard(self)
                    return
                if exited is not None and self.receipt_eof.is_set() and (terminal is None or exited != 0):
                    self.fault.set()  # PID exit alone never authorizes teardown.
                    self.terminal_error.set()
            except Exception:
                self.fault.set()
            time.sleep(0.05)

    def start(self):
        with _RETAINED_LOCK:
            _RETAINED.add(self)
        try:
            for target, daemon in ((self._output_reader, True), (self._receipt_reader, True),
                                   (self._dispatch, True), (self._monitor, False)):
                # The non-daemon monitor owns the native console lifetime even
                # after a timed-out GUI workflow returns and the window closes.
                # Normal Python exit must never kill a vendor by closing HPCON.
                threading.Thread(target=target, daemon=daemon).start()
            self.native.launch(self.nonce, self.arguments)
            self.launch_ready.set()
        except BaseException:
            self.reporting.clear()
            self.ready.set()
            if not self.native.started:
                self.aborted = True
                self.native.dispose_unused()
                self.launch_ready.set()
                with _RETAINED_LOCK:
                    _RETAINED.discard(self)
            else:
                self.launch_ready.set()
            raise

    def stop_reporting(self):
        self.reporting.clear()
        self.ready.set()


def run_winget_console(arguments, *, timeout, progress_callback, allow_resident_handoff=False):
    """Run once in a hidden console; a timeout retains every live native handle.

    stdout and stderr share the pseudoconsole's ordered output stream. Callback
    dispatch is independent of drainage, and missing private terminal evidence
    never becomes a successful result or permission to kill console clients.
    """
    arguments = _arguments(arguments)
    if type(timeout) not in (int, float) or not math.isfinite(timeout) or timeout <= 0:
        raise ValueError('Use a finite positive console worker timeout.')
    if not callable(progress_callback):
        raise ValueError('Console progress needs a callable reporter.')
    if type(allow_resident_handoff) is not bool:
        raise ValueError('Resident handoff needs an explicit Boolean choice.')
    started = time.monotonic()
    native = None
    try:
        if getattr(sys, 'frozen', False):
            _frozen_console_executable()  # No console or process exists yet.
        else:
            _source_console_executable()  # Reject a missing helper before preparation.
        native = _ConsoleProcess()
        native.allow_resident_handoff = allow_resident_handoff
        session = _Session(native, arguments, progress_callback)
        session.start()
    except Exception as error:
        if native is None or native.started is False:
            if native is not None and not native.closed:
                native.dispose_unused()
            raise ConsolePreflightError('The private console could not start; no application installer was launched ('
                + type(error).__name__ + ').') from error
        raise ConsoleOutcomeError('The console supervisor started but its target outcome is unknown; no retry was attempted.') from error
    while not session.finished.wait(0.025):
        if allow_resident_handoff and session.provider_ready.is_set() and session._handoff() is not None:
            break
        if session.terminal_error.is_set():
            session.stop_reporting()
            raise ConsoleOutcomeError('The console supervisor exited without a verified target outcome; '
                'its console remains retained and no automatic retry was attempted.')
        if time.monotonic() - started >= timeout:
            session.stop_reporting()
            raise subprocess.TimeoutExpired(arguments, timeout, output=session.output.value(), stderr='')
    session.stop_reporting()
    handoff = session._handoff() if not session.finished.is_set() else None
    result = subprocess.CompletedProcess(arguments, 0 if handoff is not None else session.returncode, session.output.value(), '')
    if handoff is not None:
        result.capture_complete = False  # Console/job stay owned and drained passively.
        result.native_completion_verified = True
        result.console_receipt = dict(handoff, resident_handoff_verified=True,
            output_eof=False, supervisor_exit_code=None, target_exit_code=0)
        with session.lock:
            if session.download:
                result.download_progress = dict(session.download[0])
        return result
    result.capture_complete = not session.fault.is_set() and session.output.complete()
    result.console_receipt = {'schema': 1, 'verified': True, 'quiescent': True,
        'console_client_count': 1, 'supervisor_exit_code': 0, 'target_exit_code': session.returncode,
        'job_verified': True, 'job_active_processes': 0,
        'output_eof': session.output_eof.is_set()}
    with session.lock:
        if session.download:
            result.download_progress = dict(session.download[0])
    return result


def monitor_main(arguments):
    """Private child entry: own console only; no GUI, setup or maintenance."""
    if sys.platform != 'win32' or len(arguments) != 3:
        return 125
    handle_text, nonce, payload = arguments
    if (not re.fullmatch(r'[0-9]{1,20}', handle_text)
            or not re.fullmatch(r'[a-f0-9]{32}', nonce) or len(payload) > 44000):
        return 125
    handle, kernel = int(handle_text), _kernel()
    if not handle or kernel.GetFileType(handle) != 3 or not kernel.SetHandleInformation(handle, 1, 0):
        return 125
    try:
        values = json.loads(base64.b64decode(payload, validate=True).decode('ascii'))
        allow_resident_handoff = False
        if isinstance(values, dict):
            if (set(values) != {'schema', 'arguments', 'resident_handoff'}
                    or type(values.get('schema')) is not int or values['schema'] != 1
                    or values.get('resident_handoff') is not True):
                return 125
            allow_resident_handoff = True
            values = values['arguments']
        target = _arguments(values)
        import msvcrt
        with open('CONIN$', 'r', encoding='utf-8') as console_input, open('CONOUT$', 'w', encoding='utf-8') as console_output:
            for identifier, stream in ((-10, console_input), (-11, console_output), (-12, console_output)):
                if not kernel.SetStdHandle(identifier & 0xffffffff, msvcrt.get_osfhandle(stream.fileno())):
                    raise ctypes.WinError(ctypes.get_last_error())
            # Explicit console streams prevent inherited parent pipes from
            # suppressing WinGet's native bars. The receipt handle is excluded.
            child = _TargetProcess(kernel, target, console_input, console_output)
            common = {'schema': 1, 'nonce': nonce, 'supervisor_pid': os.getpid(),
                      'target_pid': child.pid, 'argv_sha256': _digest(target)}

            def write(value):
                data = (json.dumps(value, ensure_ascii=True, separators=(',', ':')) + '\n').encode('ascii')
                written = w.DWORD()
                if not kernel.WriteFile(handle, data, len(data), ctypes.byref(written), None) or written.value != len(data):
                    raise ctypes.WinError(ctypes.get_last_error())

            write(dict(common, event='started'))
            started_at = time.time()
            child.resume()
            returncode = child.wait()
            proof_attempted = False
            while True:
                clients = (w.DWORD * 64)()
                count = kernel.GetConsoleProcessList(clients, len(clients))
                if count > len(clients):
                    if count > 65536:
                        raise RuntimeError('Console client inventory exceeded its bound.')
                    clients = (w.DWORD * count)()
                    count = kernel.GetConsoleProcessList(clients, len(clients))
                if count == 1 and clients[0] == os.getpid() and child.active_processes() == 0:
                    break
                if (allow_resident_handoff and not proof_attempted and returncode == 0 and count == 1 and clients[0] == os.getpid()
                        and child.active_processes() > 0):
                    proof_attempted = True
                    try:
                        from .resident_update_completion import verify_resident_completion
                        rows = child.process_rows()
                        proof = verify_resident_completion(target, child.pid, started_at, rows)
                        if len(json.dumps(proof, ensure_ascii=True, separators=(',', ':'))) > 4096:
                            raise ValueError('The resident proof exceeds its private receipt bound.')
                        # Recheck native membership and console clients after
                        # reading logs/ARP/files. A changed job keeps waiting.
                        fresh = child.process_rows()
                        again = (w.DWORD * 64)()
                        if (fresh == rows and kernel.GetConsoleProcessList(again, len(again)) == 1
                                and again[0] == os.getpid() and child.active_processes() == len(rows)):
                            write(dict(common, event='resident_handoff', quiescent=False,
                                console_client_count=1, job_verified=True, job_active_processes=len(rows),
                                exit_code=0, resident_proof=proof))
                    except Exception:
                        pass  # Unknown/native GUI installers retain the original guard.
                # An incomplete query or remaining client cannot authorize
                # console teardown, regardless of the WinGet exit code.
                time.sleep(0.05)
            write(dict(common, event='quiescent', quiescent=True, console_client_count=1,
                       job_verified=True, job_active_processes=0, exit_code=returncode & 0xffffffff))
            child.close()
        return 0
    except Exception:
        # Missing receipt remains an unknown outcome. Never terminate a target
        # or close the console in an error handler.
        return 125
    finally:
        kernel.CloseHandle(handle)


def probe_child_main():
    """Emit local fixture progress through native CONOUT, including frozen builds."""
    if sys.platform != 'win32':
        return 125
    kernel = _kernel()
    handle = kernel.CreateFileW('CONOUT$', 0xc0000000, 3, None, 3, 0, None)
    if handle in (None, 0, ctypes.c_void_p(-1).value):
        return 125
    try:
        verified = bool(kernel.GetConsoleScreenBufferInfo(handle, ctypes.create_string_buffer(64)))
        for value, pause in (('OPTIMUS_CONSOLE_QUERY=' + ('1' if verified else '0') + '\n'
                              + 'Downloading https://example.invalid/optimus-local-fixture.exe\n'
                              + '512 KB / 1 MB\r', .15),
                             ('Starting package install...\n25%\r', .15)):
            data, written = value.encode('ascii'), w.DWORD()
            if not kernel.WriteFile(handle, data, len(data), ctypes.byref(written), None) or written.value != len(data):
                return 125
            time.sleep(pause)
        return 0 if verified else 125
    finally:
        kernel.CloseHandle(handle)


def _probe_arguments():
    if getattr(sys, 'frozen', False):
        return [_frozen_console_executable(), '--console-progress-fixture-child']
    source = str(Path(__file__).resolve().parents[1])
    code = ('import sys;sys.path.insert(0,' + repr(source) + ');'
            'from optimus.winget_console import probe_child_main;raise SystemExit(probe_child_main())')
    return [_source_console_executable(), '-I', '-u', '-c', code]


def console_progress_probe(output_directory):
    """Write one exclusive local-fixture proof; never create a Backend or use WinGet."""
    if sys.platform != 'win32':
        return 2
    descriptor = None
    try:
        directory = Path(os.path.abspath(os.fspath(output_directory)))
        for candidate in (directory, *directory.parents):
            if candidate.exists() and (candidate.is_symlink()
                    or getattr(candidate.lstat(), 'st_file_attributes', 0) & 0x400):
                raise ValueError('The probe output must use ordinary local folders.')
        directory.mkdir(parents=True, exist_ok=True)
        descriptor = os.open(directory / 'console-progress-probe.json', os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        start, events = time.monotonic(), []
        def observe(value):
            if isinstance(value, dict) and len(events) < 64:
                events.append({'elapsed_seconds': time.monotonic() - start,
                               'phase': value.get('phase'), 'percent': value.get('percent')})
        result = run_winget_console(_probe_arguments(), timeout=10, progress_callback=observe)
        elapsed = time.monotonic() - start
        from .winget_progress import _ANSI
        output = _ANSI.sub('', result.stdout or '')
        queried = output.count('OPTIMUS_CONSOLE_QUERY=1') == 1 and 'OPTIMUS_CONSOLE_QUERY=0' not in output
        receipt = getattr(result, 'console_receipt', None)
        authenticated = (isinstance(receipt, dict) and type(receipt.get('schema')) is int and receipt['schema'] == 1
            and all(receipt.get(key) is True for key in ('verified', 'quiescent', 'output_eof'))
            and type(receipt.get('console_client_count')) is int and receipt['console_client_count'] == 1
            and receipt.get('job_verified') is True
            and type(receipt.get('job_active_processes')) is int and receipt['job_active_processes'] == 0
            and type(receipt.get('supervisor_exit_code')) is int and receipt['supervisor_exit_code'] == 0
            and type(receipt.get('target_exit_code')) is int and receipt['target_exit_code'] == result.returncode == 0)
        def before_exit(phase, percent):
            return any(row['phase'] == phase and row['percent'] == percent
                       and row['elapsed_seconds'] < elapsed - .05 for row in events)
        download_live, install_live = before_exit('download', 50), before_exit('install', 25)
        passed = (type(result.returncode) is int and result.returncode == 0 and result.capture_complete is True
                  and queried and authenticated and download_live and install_live)
        report = {'schema': 1, 'probe': 'windows-console-progress', 'status': 'passed' if passed else 'failed',
                  'platform': 'Windows', 'frozen': bool(getattr(sys, 'frozen', False)),
                  'network_requests': 0, 'real_winget_invocations': 0, 'maintenance_applied': False,
                  'console_query_verified': queried, 'download_progress_before_exit': download_live,
                  'install_progress_before_exit': install_live, 'target_exit_code': result.returncode,
                  'capture_complete': result.capture_complete is True, 'private_receipt_verified': authenticated,
                  'console_receipt': receipt, 'events': events, 'elapsed_seconds': elapsed}
        code = 0 if passed else 2
    except Exception as error:
        report = {'schema': 1, 'probe': 'windows-console-progress', 'status': 'failed',
                  'network_requests': 0, 'real_winget_invocations': 0, 'maintenance_applied': False,
                  'error_type': type(error).__name__}
        code = 2
    if descriptor is not None:
        with os.fdopen(descriptor, 'w', encoding='utf-8') as stream:
            json.dump(report, stream, indent=2, allow_nan=False)
            stream.write('\n')
            stream.flush()
            os.fsync(stream.fileno())
    return code


if __name__ == '__main__':
    if sys.argv[1:2] == ['--console-progress-fixture-child']:
        raise SystemExit(probe_child_main())
    raise SystemExit(monitor_main(sys.argv[1:]))
