"""Session-owned Windows interface priority; installers never need real time.

Diagnostics and command-line audits do not call InterfacePriority.apply(). The
GUI can use Above Normal while its pooled/background worker threads use normal
base priority. No RAM allocation, affinity or other application is changed.
"""
from __future__ import annotations

import ctypes
import sys

NORMAL = 0x20
ABOVE_NORMAL = 0x8000
THREAD_LOWEST = -2  # Above Normal class base 10 - 2 = normal worker base 8.
THREAD_ERROR = 0x7fffffff


def _api():
    if sys.platform != 'win32':
        return None
    from ctypes import wintypes
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.GetCurrentProcess.restype = wintypes.HANDLE
    kernel.GetCurrentThread.restype = wintypes.HANDLE
    kernel.GetPriorityClass.argtypes = [wintypes.HANDLE]
    kernel.GetPriorityClass.restype = wintypes.DWORD
    kernel.SetPriorityClass.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel.SetPriorityClass.restype = wintypes.BOOL
    kernel.GetThreadPriority.argtypes = [wintypes.HANDLE]
    kernel.GetThreadPriority.restype = ctypes.c_int
    kernel.SetThreadPriority.argtypes = [wintypes.HANDLE, ctypes.c_int]
    kernel.SetThreadPriority.restype = wintypes.BOOL
    return kernel


class InterfacePriority:
    def __init__(self):
        self._kernel = None
        self._previous = None

    def apply(self, enabled):
        if enabled is not True:
            return {'status': 'disabled', 'changed': False}
        try:
            kernel = _api()
            if kernel is None:
                return {'status': 'unavailable', 'changed': False,
                        'note': 'Interface CPU priority is a Windows setting.'}
            handle = kernel.GetCurrentProcess()
            previous = int(kernel.GetPriorityClass(handle))
            if previous == 0:
                raise OSError('The interface priority could not be read.')
            if previous == ABOVE_NORMAL:
                return {'status': 'already_configured', 'changed': False,
                        'priority': 'AboveNormal'}
            if previous != NORMAL:
                return {'status': 'external_priority', 'changed': False,
                        'priority_value': previous,
                        'note': 'An externally configured Optimus CPU priority was preserved.'}
            if not kernel.SetPriorityClass(handle, ABOVE_NORMAL):
                raise OSError('Windows did not permit the interface CPU priority preference.')
            if int(kernel.GetPriorityClass(handle)) != ABOVE_NORMAL:
                # Retain ownership if the setter succeeded but confirmation did
                # not: restore() only changes a still-AboveNormal value.
                self._kernel, self._previous = kernel, previous
                raise OSError('The interface CPU priority could not be confirmed.')
            self._kernel, self._previous = kernel, previous
            return {'status': 'applied', 'changed': True, 'priority': 'AboveNormal',
                    'note': 'Heavy worker threads retain normal base priority; RAM is managed by Windows.'}
        except Exception as error:
            return {'status': 'unavailable', 'changed': False, 'note': str(error)}

    def restore(self):
        kernel, previous = self._kernel, self._previous
        self._kernel, self._previous = None, None
        if kernel is None or previous is None:
            return
        try:
            handle = kernel.GetCurrentProcess()
            if int(kernel.GetPriorityClass(handle)) == ABOVE_NORMAL:
                kernel.SetPriorityClass(handle, previous)
        except Exception:
            pass


def lower_worker_priority():
    """Return a safe same-thread restore callback, including on failure.

    Thread-pool threads are reused, so their previous priority must be restored
    at the end of each task. This only adjusts an Above Normal Optimus process;
    it does not interfere with externally configured priority classes.
    """
    def unchanged():
        return None
    try:
        kernel = _api()
        if kernel is None or int(kernel.GetPriorityClass(kernel.GetCurrentProcess())) != ABOVE_NORMAL:
            return unchanged
        thread = kernel.GetCurrentThread()
        previous = int(kernel.GetThreadPriority(thread))
        if previous == THREAD_ERROR or previous == THREAD_LOWEST:
            return unchanged
        if not kernel.SetThreadPriority(thread, THREAD_LOWEST):
            return unchanged
    except Exception:
        return unchanged

    restored = False

    def restore():
        nonlocal restored
        if restored:
            return
        try:
            # Run on the owning task thread. A callback accidentally invoked
            # from the UI must never alter the UI's thread priority.
            if kernel.GetCurrentThreadId() != owner_thread:
                return
            restored = True
            if int(kernel.GetThreadPriority(thread)) == THREAD_LOWEST:
                kernel.SetThreadPriority(thread, previous)
        except Exception:
            pass

    try:
        owner_thread = kernel.GetCurrentThreadId()
    except Exception:
        # GetCurrentThread is a pseudo-handle. Do not retain it for an unknown
        # owner thread; restore immediately while still on this thread.
        try:
            kernel.SetThreadPriority(thread, previous)
        except Exception:
            pass
        return unchanged
    return restore
