"""Small Windows startup-write preflight and bounded native error metadata."""
from __future__ import annotations

import ctypes
import json
from pathlib import Path
import re
import shutil

MINIMUM_SYSTEM_FREE_BYTES = 2 * 1024**3


class StartupPreflightError(ValueError):
    outcomeKnown = True
    running = False
    preflight_refused = True
    manifest_written = False
    task_registration_started = False


def windows_directory():
    """Ask Windows itself; TEMP, working directory and SystemRoot are irrelevant."""
    from ctypes import wintypes
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    query = kernel.GetWindowsDirectoryW
    query.argtypes = [wintypes.LPWSTR, wintypes.UINT]
    query.restype = wintypes.UINT
    buffer = ctypes.create_unicode_buffer(32768)
    length = query(buffer, len(buffer))
    path = Path(buffer.value)
    if not 0 < length < len(buffer) or not path.is_absolute() or not re.fullmatch('[A-Za-z]:', path.drive):
        raise StartupPreflightError('The actual Windows system volume could not be verified; startup was not changed.')
    return path


def require_windows_startup_headroom():
    try:
        directory = windows_directory()
        volume = Path(directory.anchor)
        free = shutil.disk_usage(volume).free
        if type(free) is not int or free < MINIMUM_SYSTEM_FREE_BYTES:
            raise StartupPreflightError('Startup registration requires at least 2 GiB free on the Windows system volume; no manifest or task write was started.')
    except StartupPreflightError:
        raise
    except (OSError, ValueError, AttributeError) as error:
        raise StartupPreflightError('Available space on the Windows system volume is unknown; startup was not changed.') from error
    return {'system_volume': str(volume), 'free_bytes': free, 'required_bytes': MINIMUM_SYSTEM_FREE_BYTES}


def native_failure(error_type, value, returncode):
    """Retain numeric/type facts only, never native messages, argv or request data."""
    facts = {'native_returncode': returncode if type(returncode) is int and -(2**31) <= returncode < 2**32 else None,
             'native_error_chain': []}
    if isinstance(value, dict):
        facts['task_registration_started'] = value.get('task_registration_started') is True
        kind = value.get('failure_kind')
        if kind in ('system_volume_space', 'persisted_task_unverified'):
            facts['failure_kind'] = kind
        chain = value.get('native_error_chain', [])
        if isinstance(chain, list):
            for row in chain[:8]:
                if not isinstance(row, dict):
                    continue
                item = {}
                if isinstance(row.get('type'), str) and re.fullmatch(r'[A-Za-z_][A-Za-z0-9_.+`]{0,159}', row['type']):
                    item['type'] = row['type']
                if isinstance(row.get('hresult'), str) and re.fullmatch(r'0x[0-9A-Fa-f]{8}', row['hresult']):
                    item['hresult'] = row['hresult'].upper().replace('0X', '0x')
                if type(row.get('win32_code')) is int and 0 <= row['win32_code'] < 2**31:
                    item['win32_code'] = row['win32_code']
                if item:
                    facts['native_error_chain'].append(item)
    error = error_type('Windows startup operation failed; inspect its actual state before retrying. ' + json.dumps(facts, separators=(',', ':')))
    error.native_diagnostics = facts
    return error
