"""Bounded read-only counters for identified threads of this Optimus process.

Windows psutil's thread census can enumerate much more than the few workflow
threads needed by the dashboard. Query only those TIDs, validate their owning
PID on the opened handle, and carry creation time so reuse resets the baseline.
Failures mean unavailable; this module never opens a thread for modification.

https://learn.microsoft.com/windows/win32/api/processthreadsapi/nf-processthreadsapi-openthread
https://learn.microsoft.com/windows/win32/api/processthreadsapi/nf-processthreadsapi-getprocessidofthread
https://learn.microsoft.com/windows/win32/api/processthreadsapi/nf-processthreadsapi-getthreadtimes
"""
from __future__ import annotations

import ctypes
from functools import lru_cache
import os


MAX_THREADS = 12
_MAX_DWORD = 0xFFFFFFFF
_MAX_FILETIME = 0xFFFFFFFFFFFFFFFF


def _selected_threads(thread_ids):
    if (type(thread_ids) not in (tuple, list, set, frozenset) or len(thread_ids) > MAX_THREADS
            or any(type(tid) is not int or not 0 < tid <= _MAX_DWORD for tid in thread_ids)):
        raise ValueError('Select at most 12 exact native thread identifiers.')
    return sorted(set(thread_ids))


def read_own_thread_times(thread_ids, *, api=None):
    """Return only live, identified own-process threads; close every handle.

    ``identities`` is deliberately separate from CPU seconds so legacy/native
    platforms can retain their own reader while Windows requires birth proof.
    No names, command lines, global process inventories or privileged reads.
    """
    selected = _selected_threads(thread_ids)
    result = dict(threads={}, identities={}, unavailable=[], source='windows_thread_query')
    if not selected:
        return result
    try:
        api = _native_api() if api is None else api
        pid = os.getpid()
        for tid in selected:
            handle = None
            try:
                handle = api.open_thread(tid)
                if not handle:
                    raise OSError('The native worker thread is unavailable.')
                owner = api.thread_process_id(handle)
                if type(owner) is not int or owner != pid:
                    raise OSError('The selected thread does not belong to this process.')
                if api.thread_active(handle) is not True:
                    raise OSError('The selected thread has exited.')
                times = api.thread_times(handle)
                if (type(times) is not tuple or len(times) != 3
                        or any(type(tick) is not int or not 0 <= tick <= _MAX_FILETIME for tick in times)
                        or times[0] == 0):
                    raise ValueError('The native thread lifetime or counters are unavailable.')
                # A thread may exit during GetThreadTimes. Do not publish an
                # exited thread's old counters as an idle live worker.
                if api.thread_active(handle) is not True:
                    raise OSError('The selected thread exited during observation.')
                created, kernel, user = times
                result['threads'][tid] = (kernel + user) / 10_000_000
                result['identities'][tid] = (pid, created)
            except (OSError, ValueError, TypeError, AttributeError, NotImplementedError):
                result['unavailable'].append(tid)
            finally:
                if handle:
                    api.close_handle(handle)
    except (OSError, ValueError, TypeError, AttributeError, NotImplementedError):
        # A native API/load failure must not discard CPU/memory/network metrics
        # or silently fall back to a census without thread-creation identity.
        return dict(threads={}, identities={}, unavailable=selected, source='windows_thread_query')
    return result


@lru_cache(maxsize=1)
def _native_api():
    # Cache only DLL bindings. Thread handles and observations are never cached.
    return _WindowsThreadAPI()


class _WindowsThreadAPI:
    def __init__(self):
        if os.name != 'nt':
            raise NotImplementedError('Windows thread counters are unavailable.')
        from ctypes import wintypes as w
        self.w = w
        # LOAD_LIBRARY_SEARCH_SYSTEM32 avoids the current directory/PATH.
        self.kernel = ctypes.WinDLL('kernel32.dll', use_last_error=True, winmode=0x800)
        self.kernel.OpenThread.argtypes = [w.DWORD, w.BOOL, w.DWORD]
        self.kernel.OpenThread.restype = w.HANDLE
        self.kernel.GetProcessIdOfThread.argtypes = [w.HANDLE]
        self.kernel.GetProcessIdOfThread.restype = w.DWORD
        self.kernel.GetThreadTimes.argtypes = [w.HANDLE, ctypes.POINTER(w.FILETIME),
            ctypes.POINTER(w.FILETIME), ctypes.POINTER(w.FILETIME), ctypes.POINTER(w.FILETIME)]
        self.kernel.GetThreadTimes.restype = w.BOOL
        self.kernel.GetExitCodeThread.argtypes = [w.HANDLE, ctypes.POINTER(w.DWORD)]
        self.kernel.GetExitCodeThread.restype = w.BOOL
        self.kernel.CloseHandle.argtypes = [w.HANDLE]
        self.kernel.CloseHandle.restype = w.BOOL

    def open_thread(self, tid):
        # THREAD_QUERY_LIMITED_INFORMATION only; no debug privilege, inheritance,
        # suspend, termination, scheduling or memory-write access is requested.
        return self.kernel.OpenThread(0x0800, False, tid)

    def thread_process_id(self, handle):
        return int(self.kernel.GetProcessIdOfThread(handle))

    def thread_active(self, handle):
        status = self.w.DWORD()
        if not self.kernel.GetExitCodeThread(handle, ctypes.byref(status)):
            raise ctypes.WinError(ctypes.get_last_error())
        return status.value == 259  # STILL_ACTIVE

    def thread_times(self, handle):
        created, exited, kernel, user = (self.w.FILETIME() for _ in range(4))
        if not self.kernel.GetThreadTimes(handle, ctypes.byref(created), ctypes.byref(exited),
                ctypes.byref(kernel), ctypes.byref(user)):
            raise ctypes.WinError(ctypes.get_last_error())
        ticks = lambda value: (int(value.dwHighDateTime) << 32) | int(value.dwLowDateTime)
        return ticks(created), ticks(kernel), ticks(user)

    def close_handle(self, handle):
        if not self.kernel.CloseHandle(handle):
            raise ctypes.WinError(ctypes.get_last_error())
