"""Scoped browser cleanup and explicitly requested, graceful window closure.

Importing this module never enumerates processes, sends messages or opens a
browser database. Privacy writes use BrowserPrivacy's fresh plans and rollback
backups; cache writes remain delegated to the existing allowlisted cleaner.
"""
from __future__ import annotations

import ctypes
import ctypes.util
import math
import ntpath
import os
import re
import subprocess
import sys
import threading
import time
from typing import Any

import psutil
from .browser_paths import browser_definitions, process_matches

_PLATFORM = sys.platform
BROWSERS = browser_definitions(_PLATFORM)
_PROFILE_BROWSER = {value['name']: key for key, value in BROWSERS.items()}
_HEX32 = re.compile(r'^[a-f0-9]{32}$')
_X11_LOCK = threading.RLock()


def _browser(browser_id):
    if not isinstance(browser_id, str) or browser_id not in BROWSERS:
        raise ValueError('Choose a supported browser identity.')
    return BROWSERS[browser_id]


def _cancelled(event):
    return event is not None and event.is_set()


def _session_id(pid):
    if os.name != 'nt':
        return os.getsid(pid)
    from ctypes import wintypes
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.ProcessIdToSessionId.argtypes = (wintypes.DWORD, ctypes.POINTER(wintypes.DWORD))
    kernel.ProcessIdToSessionId.restype = wintypes.BOOL
    session = wintypes.DWORD()
    if not kernel.ProcessIdToSessionId(pid, ctypes.byref(session)):
        raise ctypes.WinError(ctypes.get_last_error())
    return int(session.value)


class BrowserSessionController:
    """Close verified current-user/session browser windows using WM_CLOSE only.

    A renderer/background process without a closable window is never killed.
    The final complete process inventory must be empty before cleanup can begin.
    """
    def __init__(self, *, timeout_seconds=10.0):
        if type(timeout_seconds) not in (int, float) or not math.isfinite(timeout_seconds) or not 0.1 <= timeout_seconds <= 30:
            raise ValueError('Browser close timeout must be 0.1 to 30 seconds.')
        self.timeout_seconds = float(timeout_seconds)
        self._close_lock = threading.Lock()

    @staticmethod
    def _context():
        user = psutil.Process(os.getpid()).username()
        session = _session_id(os.getpid())
        if not user or type(session) is not int or session <= 0:
            raise OSError('The current user/session is unavailable.')
        return user.casefold(), session

    @staticmethod
    def _identity(process, browser_id, context):
        wanted = _browser(browser_id)['process']
        pid, name, executable = process.pid, process.name(), process.exe()
        created, user, session = process.create_time(), process.username(), _session_id(process.pid)
        native = _PLATFORM != 'win32'
        path_module = os.path if native else ntpath
        if (type(pid) is not int or pid <= 4 or not isinstance(name, str) or not process_matches(browser_id, name, _PLATFORM)
                or not isinstance(executable, str) or not path_module.isabs(executable)
                or not process_matches(browser_id, path_module.basename(executable), _PLATFORM, allow_truncated=False)
                or type(created) not in (int, float) or not math.isfinite(created) or created <= 0
                or not isinstance(user, str) or not user or user.casefold() != context[0]
                or (not native and session != context[1]) or session <= 0):
            raise ValueError('Browser process ownership, session or lifetime could not be verified.')
        if native:
            identities = process.uids()
            if identities.real != os.getuid() or identities.effective != os.geteuid():
                raise ValueError('The browser real and effective user IDs must match the current desktop account.')
        return {'pid': pid, 'name': name, 'exe': path_module.normcase(path_module.normpath(executable)),
                'create_time': float(created), 'username': user.casefold(), 'session_id': session, 'verified': True}

    @staticmethod
    def _native_process_names():
        """Read a fresh Toolhelp snapshot; protected non-browser names stay usable.

        Windows psutil.name() derives its name from the executable path, which
        can be inaccessible for protected processes. Snapshot basenames require
        no process termination/debug privilege and never authorize closure.
        """
        if os.name != 'nt':
            names = {}
            for process in psutil.process_iter(['pid', 'name'], ad_value=None):
                name = process.info.get('name')
                if not isinstance(name, str) or not name:
                    raise OSError('A native process name is inaccessible.')
                names[int(process.pid)] = name
            return names
        from ctypes import wintypes
        class ProcessEntry(ctypes.Structure):
            _fields_ = [('dwSize', wintypes.DWORD), ('cntUsage', wintypes.DWORD),
                        ('th32ProcessID', wintypes.DWORD), ('th32DefaultHeapID', ctypes.c_size_t),
                        ('th32ModuleID', wintypes.DWORD), ('cntThreads', wintypes.DWORD),
                        ('th32ParentProcessID', wintypes.DWORD), ('pcPriClassBase', wintypes.LONG),
                        ('dwFlags', wintypes.DWORD), ('szExeFile', wintypes.WCHAR * 260)]
        kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        kernel.CreateToolhelp32Snapshot.argtypes = (wintypes.DWORD, wintypes.DWORD)
        kernel.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
        for name in ('Process32FirstW', 'Process32NextW'):
            operation = getattr(kernel, name)
            operation.argtypes = (wintypes.HANDLE, ctypes.POINTER(ProcessEntry))
            operation.restype = wintypes.BOOL
        kernel.CloseHandle.argtypes = (wintypes.HANDLE,)
        kernel.CloseHandle.restype = wintypes.BOOL
        handle = kernel.CreateToolhelp32Snapshot(0x00000002, 0)
        if handle == ctypes.c_void_p(-1).value:
            raise ctypes.WinError(ctypes.get_last_error())
        names, deadline = {}, time.monotonic() + 3
        try:
            entry = ProcessEntry()
            entry.dwSize = ctypes.sizeof(entry)
            present = kernel.Process32FirstW(handle, ctypes.byref(entry))
            while present:
                if len(names) >= 20000 or time.monotonic() >= deadline:
                    raise OSError('The native process snapshot exceeded its safety limit.')
                name, pid = str(entry.szExeFile), int(entry.th32ProcessID)
                if not name or '\\' in name or '/' in name or pid in names:
                    raise OSError('The native process snapshot contains an ambiguous identity.')
                names[pid] = name
                present = kernel.Process32NextW(handle, ctypes.byref(entry))
            if ctypes.get_last_error() != 18:  # ERROR_NO_MORE_FILES
                raise ctypes.WinError(ctypes.get_last_error())
            return names
        finally:
            kernel.CloseHandle(handle)

    def _inventory(self, browser_id):
        wanted = _browser(browser_id)['process']
        records, errors, unknown = [], [], []
        try:
            context = self._context()
            deadline = time.monotonic() + 3
            for index, process in enumerate(psutil.process_iter(['pid', 'name'], ad_value=None)):
                if index >= 20000 or time.monotonic() >= deadline:
                    errors.append('Browser process inventory exceeded its safety limit.')
                    break
                name = process.info.get('name')
                if not isinstance(name, str) or not name:
                    unknown.append(process.pid)
                    continue
                if not process_matches(browser_id, name, _PLATFORM):
                    continue
                try:
                    records.append(self._identity(process, browser_id, context))
                except psutil.NoSuchProcess:
                    continue
                except (psutil.Error, OSError, ValueError, TypeError, AttributeError):
                    records.append({'pid': process.pid, 'name': name, 'verified': False})
                    errors.append('A browser process belongs to another user/session or its identity is inaccessible.')
            if unknown:
                # This snapshot is taken after psutil enumeration. Every unknown
                # live PID needs a positive fresh name; never assume an
                # inaccessible process is harmless. Include matching browser PIDs
                # found only in the newer snapshot as well.
                native_names = self._native_process_names()
                browser_pids = {pid for pid, name in native_names.items() if process_matches(browser_id, name, _PLATFORM)}
                for pid in unknown:
                    if pid not in native_names and psutil.pid_exists(pid):
                        errors.append('A live process name could not be inspected safely.')
                already = {record['pid'] for record in records}
                for pid in browser_pids - already:
                    try:
                        records.append(self._identity(psutil.Process(pid), browser_id, context))
                    except psutil.NoSuchProcess:
                        continue
                    except (psutil.Error, OSError, ValueError, TypeError, AttributeError):
                        records.append({'pid': pid, 'name': native_names[pid], 'verified': False})
                        errors.append('A browser process belongs to another user/session or its identity is inaccessible.')
        except (psutil.Error, OSError, ValueError, TypeError, AttributeError):
            errors.append('The complete browser process inventory could not be verified.')
        return {'browser_id': browser_id, 'browser': BROWSERS[browser_id]['name'],
                'running': bool(records), 'complete': not errors,
                'can_close': not errors and all(record.get('verified') is True for record in records),
                'processes': records, 'errors': list(dict.fromkeys(errors))}

    def browser_status(self, browser_id):
        _browser(browser_id)
        return self._inventory(browser_id)

    @staticmethod
    def _windows(pids):
        """Enumerate visible top-level windows before requesting any closure."""
        if os.name != 'nt':
            return _x11_windows(pids)
        from ctypes import wintypes
        user = ctypes.WinDLL('user32', use_last_error=True)
        callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
        user.EnumWindows.argtypes = (callback_type, wintypes.LPARAM)
        user.EnumWindows.restype = wintypes.BOOL
        user.IsWindowVisible.argtypes = (wintypes.HWND,)
        user.IsWindowVisible.restype = wintypes.BOOL
        user.GetWindowThreadProcessId.argtypes = (wintypes.HWND, ctypes.POINTER(wintypes.DWORD))
        user.GetWindowThreadProcessId.restype = wintypes.DWORD
        found, visited, truncated = [], 0, False
        deadline = time.monotonic() + 2
        def visit(handle, unused):
            nonlocal visited, truncated
            visited += 1
            if visited > 20000 or time.monotonic() >= deadline:
                truncated = True
                return False
            pid = wintypes.DWORD()
            user.GetWindowThreadProcessId(handle, ctypes.byref(pid))
            if pid.value in pids and user.IsWindowVisible(handle):
                found.append((int(handle), int(pid.value)))
            return True
        callback = callback_type(visit)
        if not user.EnumWindows(callback, 0) or truncated:
            raise OSError('Browser window inventory did not complete.')
        return found

    def _same_process(self, browser_id, expected):
        try:
            current = self._identity(psutil.Process(expected['pid']), browser_id, self._context())
            return all(current.get(key) == expected.get(key) for key in ('pid', 'name', 'exe', 'create_time', 'username', 'session_id'))
        except (psutil.Error, OSError, ValueError, TypeError, AttributeError, KeyError):
            return False

    @staticmethod
    def _post_close(handle, expected_pid):
        if os.name != 'nt':
            return _x11_post_close(handle, expected_pid)
        from ctypes import wintypes
        user = ctypes.WinDLL('user32', use_last_error=True)
        user.GetWindowThreadProcessId.argtypes = (wintypes.HWND, ctypes.POINTER(wintypes.DWORD))
        user.GetWindowThreadProcessId.restype = wintypes.DWORD
        user.PostMessageW.argtypes = (wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)
        user.PostMessageW.restype = wintypes.BOOL
        actual_pid = wintypes.DWORD()
        user.GetWindowThreadProcessId(handle, ctypes.byref(actual_pid))
        return actual_pid.value == expected_pid and bool(user.PostMessageW(handle, 0x0010, 0, 0))

    def close_browser(self, browser_id, cancel_event=None):
        _browser(browser_id)
        result = {'browser_id': browser_id, 'closed': False, 'complete': False,
                  'cancelled': False, 'requested_windows': 0, 'remaining_processes': [], 'errors': []}
        if not self._close_lock.acquire(blocking=False):
            return dict(result, reason='Another browser close request is still active.')
        try:
            deadline = time.monotonic() + self.timeout_seconds
            if _cancelled(cancel_event):
                return dict(result, cancelled=True, reason='Cancelled before any close request.')
            before = self._inventory(browser_id)
            if before.get('complete') is not True or before.get('can_close') is not True:
                return dict(result, remaining_processes=before.get('processes', []), errors=before.get('errors', []),
                            reason='Browser ownership and complete process identities must be verified before closing.')
            if not before['running']:
                return dict(result, closed=True, complete=True, reason='The browser is already closed.')
            identities = {record['pid']: record for record in before['processes']}
            if _PLATFORM == 'darwin':
                if not all(self._same_process(browser_id, record) for record in identities.values()):
                    return dict(result, reason='A browser process lifetime changed before its normal quit request.')
                _mac_quit(browser_id)
                result['requested_windows'] = 1
                windows = []
            else:
                windows = self._windows(set(identities))
            for handle, pid in windows:
                if _cancelled(cancel_event):
                    return dict(result, cancelled=True, reason='Cancelled; no further windows were asked to close.')
                if time.monotonic() >= deadline:
                    return dict(result, reason='The browser close deadline elapsed; no further close request was sent.')
                if not self._same_process(browser_id, identities[pid]):
                    return dict(result, reason='A browser process lifetime changed; no further close request was sent.')
                if not self._post_close(handle, pid):
                    return dict(result, reason='A verified browser window could not be asked to close.')
                result['requested_windows'] += 1
            while True:
                if _cancelled(cancel_event):
                    return dict(result, cancelled=True, reason='Cancelled while waiting for graceful browser closure.')
                after = self._inventory(browser_id)
                result['remaining_processes'] = after.get('processes', [])
                if after.get('complete') is not True:
                    return dict(result, errors=after.get('errors', []), reason='Browser closure could not be verified.')
                if after.get('running') is False:
                    return dict(result, closed=True, complete=True, reason='All browser processes exited after graceful closure.')
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return dict(result, complete=True, reason='Browser windows or background processes are still running; cleanup was skipped.')
                if cancel_event is not None:
                    cancel_event.wait(min(0.1, remaining))
                else:
                    time.sleep(min(0.1, remaining))
        except (psutil.Error, OSError, ValueError, TypeError, KeyError, AttributeError, subprocess.SubprocessError):
            return dict(result, reason='Graceful browser closure could not be completed or verified.')
        finally:
            self._close_lock.release()


# Keep the shorter name usable by source integrations.
SessionController = BrowserSessionController


def _mac_quit(browser_id):
    """Request the application's ordinary quit action; never send kill signals."""
    bundle_id = _browser(browser_id)['bundle_id']
    subprocess.run(['/usr/bin/osascript', '-e', f'tell application id "{bundle_id}" to quit'],
                   check=True, timeout=10, capture_output=True)


def _x11_connection():
    """X11/XWayland only. Native Wayland exposes no universal close-window API."""
    library = ctypes.util.find_library('X11')
    if not library or not os.environ.get('DISPLAY'):
        raise OSError('Normal browser closure is unavailable in this desktop session. Close the browser yourself or skip it.')
    native = ctypes.CDLL(library)
    native.XOpenDisplay.argtypes = [ctypes.c_char_p]
    native.XOpenDisplay.restype = ctypes.c_void_p
    native.XCloseDisplay.argtypes = [ctypes.c_void_p]
    native.XDefaultRootWindow.argtypes = [ctypes.c_void_p]
    native.XDefaultRootWindow.restype = ctypes.c_ulong
    native.XInternAtom.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_int]
    native.XInternAtom.restype = ctypes.c_ulong
    native.XGetWindowProperty.argtypes = [ctypes.c_void_p, ctypes.c_ulong, ctypes.c_ulong,
        ctypes.c_long, ctypes.c_long, ctypes.c_int, ctypes.c_ulong,
        ctypes.POINTER(ctypes.c_ulong), ctypes.POINTER(ctypes.c_int),
        ctypes.POINTER(ctypes.c_ulong), ctypes.POINTER(ctypes.c_ulong), ctypes.POINTER(ctypes.c_void_p)]
    native.XFree.argtypes = [ctypes.c_void_p]
    display = native.XOpenDisplay(None)
    if not display:
        raise OSError('The X11 display could not be opened for normal browser closure.')
    # A window can disappear after enumeration. Xlib's default error handler can
    # terminate the entire application on BadWindow; keep those races local to
    # this close attempt, and preserve another library's handler for its display.
    _X11_LOCK.acquire()
    try:
        native.XSetErrorHandler.argtypes = [ctypes.c_void_p]
        native.XSetErrorHandler.restype = ctypes.c_void_p
        native.XSync.argtypes = [ctypes.c_void_p, ctypes.c_int]
        handler_type = ctypes.CFUNCTYPE(ctypes.c_int, ctypes.c_void_p, ctypes.c_void_p)
        previous = native.XSetErrorHandler(None)
        old_callback = handler_type(previous) if previous else None
        errors = [False]
        def handle_error(error_display, event):
            if error_display == display:
                errors[0] = True
                return 0
            return old_callback(error_display, event) if old_callback else 0
        callback = handler_type(handle_error)
        native.XSetErrorHandler(ctypes.cast(callback, ctypes.c_void_p))
        native._optimus_callback = callback
        native._optimus_previous_handler = previous
        native._optimus_errors = errors
    except BaseException:
        native.XCloseDisplay(display)
        _X11_LOCK.release()
        raise
    return native, display


def _x11_disconnect(native, display):
    try:
        native.XCloseDisplay(display)
        native.XSetErrorHandler(native._optimus_previous_handler)
    finally:
        _X11_LOCK.release()


def _x11_property(native, display, window, name):
    atom = native.XInternAtom(display, name, True)
    if not atom:
        return []
    actual, count, remaining = ctypes.c_ulong(), ctypes.c_ulong(), ctypes.c_ulong()
    format_value, data = ctypes.c_int(), ctypes.c_void_p()
    result = native.XGetWindowProperty(display, window, atom, 0, 20000, False, 0,
        ctypes.byref(actual), ctypes.byref(format_value), ctypes.byref(count), ctypes.byref(remaining), ctypes.byref(data))
    try:
        if result or native._optimus_errors[0] or remaining.value or count.value > 20000 or format_value.value not in (0, 32):
            raise OSError('The X11 window property inventory is incomplete.')
        if not data.value:
            return []
        return list(ctypes.cast(data, ctypes.POINTER(ctypes.c_ulong * count.value)).contents)
    finally:
        if data.value:
            native.XFree(data)


def _x11_windows(pids):
    native, display = _x11_connection()
    try:
        windows = _x11_property(native, display, native.XDefaultRootWindow(display), b'_NET_CLIENT_LIST')
        result, deadline = [], time.monotonic() + 2
        for window in windows:
            if time.monotonic() >= deadline:
                raise OSError('The X11 browser-window inventory exceeded its deadline.')
            owner = _x11_property(native, display, window, b'_NET_WM_PID')
            if len(owner) == 1 and owner[0] in pids:
                result.append((window, owner[0]))
        return result
    finally:
        _x11_disconnect(native, display)


def _x11_post_close(handle, expected_pid):
    native, display = _x11_connection()
    try:
        if _x11_property(native, display, handle, b'_NET_WM_PID') != [expected_pid]:
            return False
        protocol = native.XInternAtom(display, b'WM_PROTOCOLS', True)
        close_atom = native.XInternAtom(display, b'WM_DELETE_WINDOW', True)
        if not protocol or not close_atom:
            return False
        class MessageData(ctypes.Union):
            _fields_ = [('b', ctypes.c_char * 20), ('s', ctypes.c_short * 10), ('l', ctypes.c_long * 5)]
        class ClientMessage(ctypes.Structure):
            _fields_ = [('type', ctypes.c_int), ('serial', ctypes.c_ulong), ('send_event', ctypes.c_int),
                ('display', ctypes.c_void_p), ('window', ctypes.c_ulong), ('message_type', ctypes.c_ulong),
                ('format', ctypes.c_int), ('data', MessageData)]
        class Event(ctypes.Union):
            _fields_ = [('message', ClientMessage), ('padding', ctypes.c_long * 24)]
        event = Event()
        event.message.type, event.message.display, event.message.window = 33, display, handle
        event.message.message_type, event.message.format = protocol, 32
        event.message.data.l[0] = close_atom
        native.XSendEvent.argtypes = [ctypes.c_void_p, ctypes.c_ulong, ctypes.c_int, ctypes.c_long, ctypes.POINTER(Event)]
        native.XFlush.argtypes = [ctypes.c_void_p]
        sent = bool(native.XSendEvent(display, handle, False, 0, ctypes.byref(event)))
        native.XFlush(display)
        native.XSync(display, False)
        return sent and not native._optimus_errors[0]
    finally:
        _x11_disconnect(native, display)


def _closed(status):
    return (isinstance(status, dict) and status.get('complete') is True
            and status.get('running') is False and isinstance(status.get('processes'), list)
            and not status['processes'] and not status.get('errors'))


def _counts(value, categories):
    return {key: value[key] for key in categories
            if isinstance(value, dict) and type(value.get(key)) is int and value[key] >= 0}


def _cache_summary(outcome):
    """Retain aggregate outcomes, without copying file paths or raw errors."""
    if not isinstance(outcome, dict):
        return {'failed_items': 1, 'reason': 'The cache cleaner returned an invalid outcome.'}
    native = outcome.get('result', outcome)
    if not isinstance(native, dict):
        return {'failed_items': 1, 'reason': 'The cache cleaner returned an invalid result.'}
    summary = {key: native[key] for key in ('deleted_count', 'deleted_bytes', 'skipped_count')
               if type(native.get(key)) is int and native[key] >= 0}
    # Retain an opaque reference to the existing private deletion journal. The
    # final verifier can read bounded file identities without duplicating private
    # paths or raw errors in this compact browser summary.
    log_path = native.get('log_path')
    if isinstance(log_path, str):
        identity = ntpath.basename(log_path).removesuffix('.json')
        if _HEX32.fullmatch(identity):
            summary['cleanup_log_id'] = identity
    errors = native.get('errors', [])
    preview = outcome.get('preview_summary', {})
    failed = outcome.get('failed_items', 0)
    failed = failed if type(failed) is int and failed >= 0 else 1
    summary.update(failed_items=max(failed, len(errors) if isinstance(errors, list) else int(bool(errors))),
                   cancelled=outcome.get('cancelled') is True,
                   partial=outcome.get('partial') is True or isinstance(preview, dict) and preview.get('complete') is False)
    if summary['partial'] and not summary['failed_items']:
        summary['failed_items'] = 1
    return summary


def run_browser_cleanup(backend: Any, settings: dict[str, Any], *, clean, browser_prompt=None,
                        cancel_event=None, item_callback=None):
    """Clean saved categories for selected browsers after per-run close choices.

    Declining an open browser skips every one of its profiles and cache roots.
    A missing prompt never authorizes closing. The only affirmative answer is
    the boolean True; saved cleanup choices do not authorize window closure.
    """
    flags = ('browser_cache_enabled', 'browser_history_enabled', 'browser_cookies_enabled', 'browser_download_history_enabled')
    if any(type(settings.get(key, False)) is not bool for key in flags):
        raise ValueError('Browser cleanup category choices must be booleans.')
    selected = settings.get('browser_cleanup_browsers', list(BROWSERS))
    if (not isinstance(selected, list) or len(selected) > len(BROWSERS)
            or any(not isinstance(value, str) or value not in BROWSERS for value in selected)
            or len(set(selected)) != len(selected)):
        raise ValueError('Choose each supported browser at most once.')
    from .browser_privacy import _domains
    keep = _domains(settings.get('browser_keep_domains', []))
    categories = [category for category, flag in (('history', 'browser_history_enabled'),
                  ('cookies', 'browser_cookies_enabled'), ('downloads', 'browser_download_history_enabled')) if settings.get(flag)]
    result = {'items': [], 'failed_items': 0, 'skips': [], 'cancelled': False,
              'note': 'Only saved browser categories are cleaned. Open browsers require an explicit close choice for this run. Passwords, bookmarks and downloaded files are preserved; private rollback backups retain original privacy databases.'}
    if _cancelled(cancel_event) or not selected or not any(settings.get(key, False) for key in flags):
        result['cancelled'] = _cancelled(cancel_event)
        return result
    profiles, caches = {}, {}
    try:
        if categories:
            discovered = backend.privacy_profiles()
            if not isinstance(discovered, list) or len(discovered) > 1000:
                raise ValueError('Invalid browser profile inventory.')
            seen = set()
            for profile in discovered:
                if not isinstance(profile, dict):
                    raise ValueError('Invalid browser profile record.')
                browser_id = _PROFILE_BROWSER.get(profile.get('browser'))
                if browser_id not in selected:
                    continue
                identity = profile.get('id')
                if not isinstance(identity, str) or not _HEX32.fullmatch(identity) or identity in seen:
                    raise ValueError('Invalid or ambiguous browser profile identity.')
                seen.add(identity)
                profiles.setdefault(browser_id, []).append(profile)
        if settings.get('browser_cache_enabled'):
            available = backend.cleanup_categories()
            if not isinstance(available, list) or len(available) > 2000:
                raise ValueError('Invalid browser cache inventory.')
            for category in available:
                if not isinstance(category, dict):
                    raise ValueError('Invalid browser cache category.')
                identity = category.get('id')
                browser_id = identity.split(':', 1)[0] if isinstance(identity, str) and ':' in identity else None
                if (browser_id in selected and category.get('available') is True
                        and any(process_matches(browser_id, name, _PLATFORM) for name in category.get('process_names', []))):
                    if identity not in caches.setdefault(browser_id, []):
                        caches[browser_id].append(identity)
    except Exception as error:
        result['failed_items'] = 1
        result['items'].append({'operation': 'browser-inventory', 'status': 'failed',
                                'reason': 'Browser cleanup inventory could not be verified.', 'error_type': type(error).__name__})
        return result

    for index, browser_id in enumerate(selected):
        if _cancelled(cancel_event):
            result['cancelled'] = True
            break
        browser = BROWSERS[browser_id]['name']
        item = {'browser_id': browser_id, 'browser': browser, 'status': 'completed', 'profiles': [], 'failed_items': 0}
        result['items'].append(item)
        def skip(reason, *, failure=False):
            item['status'] = 'partial' if item['profiles'] or item.get('cache') else 'skipped'
            item['reason'] = reason
            if failure:
                item['failed_items'] += 1
            result['skips'].append({'browser_id': browser_id, 'reason': reason})
        supported = [category for category in categories if not (browser_id == 'firefox' and category == 'downloads')]
        if browser_id == 'firefox' and 'downloads' in categories:
            item['unsupported_categories'] = ['downloads']
        local_profiles = profiles.get(browser_id, []) if supported else []
        local_caches = caches.get(browser_id, [])
        if not local_profiles and not local_caches:
            skip('No existing supported profile data or cache category was selected for this browser.')
            continue
        if item_callback:
            item_callback({'current': index, 'total': len(selected), 'item': index + 1, 'items': len(selected),
                           'description': 'Checking ' + browser + ' before approved browser cleanup.'})
        try:
            status = backend.browser_cleanup_status(browser_id)
            if not isinstance(status, dict) or status.get('complete') is not True or not isinstance(status.get('processes'), list) or status.get('errors'):
                skip('The complete browser process state could not be verified.', failure=True)
                continue
            if status.get('running') is True:
                process_rows = status['processes']
                if (status.get('can_close') is not True or not process_rows
                        or any(not isinstance(record, dict) or type(record.get('pid')) is not int
                               or record['pid'] <= 4 or not isinstance(record.get('name'), str)
                               or not process_matches(browser_id, record['name'], _PLATFORM) for record in process_rows)
                        or len({record['pid'] for record in process_rows}) != len(process_rows)):
                    skip('Browser ownership/session could not be verified for graceful closure.', failure=True)
                    continue
                request = {'browser_id': browser_id, 'browser': browser,
                           'processes': [{'pid': record['pid'], 'name': record['name']} for record in status['processes']
                                         if isinstance(record, dict) and type(record.get('pid')) is int and isinstance(record.get('name'), str)],
                           'categories': (['cache'] if local_caches else []) + supported,
                           'profile_count': len(local_profiles),
                           'description': 'Close all ' + browser + ' windows normally and clean, or skip this browser for this run. Unsaved pages and active downloads may need your attention.'}
                if _cancelled(cancel_event):
                    result['cancelled'] = True
                    break
                approved = browser_prompt(request) if browser_prompt is not None else False
                if _cancelled(cancel_event):
                    result['cancelled'] = True
                    break
                if approved is not True:
                    skip('This open browser was skipped for this run; no profile or cache was cleaned.')
                    continue
                closed = backend.browser_cleanup_close(browser_id, cancel_event=cancel_event)
                if _cancelled(cancel_event) or isinstance(closed, dict) and closed.get('cancelled') is True:
                    result['cancelled'] = True
                    break
                if not isinstance(closed, dict) or closed.get('closed') is not True or closed.get('complete') is not True:
                    skip('Browser windows or background processes did not close completely; all cleanup for this browser was skipped.', failure=True)
                    continue
            elif status.get('running') is not False or status['processes']:
                skip('The browser process state was inconsistent; cleanup was skipped.', failure=True)
                continue
            if not _closed(backend.browser_cleanup_status(browser_id)):
                skip('The browser is open or its closed state changed; all cleanup for this browser was skipped.', failure=True)
                continue
            reopened = False
            for profile in local_profiles:
                if _cancelled(cancel_event):
                    result['cancelled'] = True
                    break
                if not _closed(backend.browser_cleanup_status(browser_id)):
                    skip('The browser reopened or its state became unknown; remaining cleanup was skipped.', failure=True)
                    reopened = True
                    break
                profile_result = {'profile_id': profile['id'], 'categories': supported, 'status': 'skipped'}
                item['profiles'].append(profile_result)
                try:
                    plan = backend.privacy_scan(profile['id'], categories=list(supported), keep_domains=list(keep))
                    if not isinstance(plan, dict) or type(plan.get('canApply')) is not bool:
                        raise ValueError('Invalid browser privacy plan.')
                    profile_result['counts'] = _counts(plan.get('counts'), supported)
                    if plan['canApply'] is not True:
                        profile_result['reason'] = 'No selected existing privacy database is eligible for cleanup.'
                        continue
                    if not isinstance(plan.get('planId'), str) or not _HEX32.fullmatch(plan['planId']):
                        raise ValueError('Invalid browser privacy plan identity.')
                    chosen = plan.get('categories')
                    if not isinstance(chosen, list) or not chosen or any(category not in supported for category in chosen):
                        raise ValueError('The privacy plan expanded the approved categories.')
                    if _cancelled(cancel_event):
                        result['cancelled'] = True
                        break
                    if not _closed(backend.browser_cleanup_status(browser_id)):
                        skip('The browser reopened after preview; remaining cleanup was skipped.', failure=True)
                        reopened = True
                        break
                    applied = backend.privacy_apply({'planId': plan['planId'], 'selectedCategories': list(chosen), 'confirmed': True})
                    if not isinstance(applied, dict) or applied.get('state') != 'applied':
                        raise RuntimeError('The privacy cleanup did not report successful application.')
                    profile_result.update(status='applied', counts=_counts(applied.get('counts'), chosen))
                    if isinstance(applied.get('backupId'), str) and _HEX32.fullmatch(applied['backupId']):
                        profile_result['backup_id'] = applied['backupId']
                except Exception as error:
                    profile_result.update(status='failed', reason='Privacy cleanup could not be completed; inspect private browser rollback history before retrying.', error_type=type(error).__name__)
                    item['failed_items'] += 1
            if result['cancelled']:
                item['status'] = 'cancelled'
                break
            if local_caches and not reopened:
                if _cancelled(cancel_event):
                    result['cancelled'] = True
                    item['status'] = 'cancelled'
                    break
                if not _closed(backend.browser_cleanup_status(browser_id)):
                    skip('The browser reopened or its state became unknown; cache cleanup was skipped.', failure=True)
                else:
                    item['cache'] = _cache_summary(clean(list(local_caches)))
                    item['failed_items'] += item['cache']['failed_items']
                    if item['cache']['cancelled']:
                        result['cancelled'] = True
                        item['status'] = 'cancelled'
                        break
            if item['failed_items'] and item['status'] == 'completed':
                item['status'] = 'partial' if any(profile['status'] == 'applied' for profile in item['profiles']) or item.get('cache') else 'failed'
        except Exception as error:
            item['failed_items'] += 1
            item.update(status='partial' if any(profile['status'] == 'applied' for profile in item['profiles']) or item.get('cache') else 'failed',
                        reason='Browser cleanup could not complete; no further action was attempted for this browser.', error_type=type(error).__name__)
    result['failed_items'] += sum(item.get('failed_items', 0) for item in result['items'])
    result['cancelled'] = result['cancelled'] or _cancelled(cancel_event)
    if result['cancelled'] and result['items'] and result['items'][-1].get('status') == 'completed':
        result['items'][-1]['status'] = 'cancelled'
    return result
