"""Explicit Windows UAC requests; no permission check or UAC bypass."""
from __future__ import annotations

import ctypes
import os
from pathlib import Path
import subprocess
import sys

RELAUNCH_FLAG = '--admin-relaunch'


def relaunch_command(argv=None):
    """Preserve the approved source-workspace argument and other session options."""
    arguments = list(sys.argv[1:] if argv is None else argv)
    if any(not isinstance(value, str) or '\x00' in value for value in arguments):
        raise ValueError('Invalid administrator relaunch arguments.')
    # Restart readiness belongs to the previous editor transaction. Reusing its
    # marker could acknowledge the wrong process after a later manual UAC request.
    kept = []
    index = 0
    while index < len(arguments):
        value = arguments[index]
        if value == '--restart-ready-file':
            index += 2
            continue
        if value.startswith('--restart-ready-file=') or value == RELAUNCH_FLAG:
            index += 1
            continue
        kept.append(value)
        index += 1
    kept.append(RELAUNCH_FLAG)
    if getattr(sys, 'frozen', False):
        return sys.executable, kept
    baseline = Path(os.environ.get('OPTIMUS_ORIGINAL_SOURCE') or Path(__file__).parents[2])
    launcher = baseline / 'launch.py'
    if not launcher.is_file():
        raise FileNotFoundError('The original Optimus launcher is unavailable.')
    return sys.executable, ['-B', str(launcher), *kept]


def request_administrator(argv=None, *, shell_execute=None):
    """Ask Windows to launch one elevated process; the user still approves UAC."""
    try:
        executable, arguments = relaunch_command(argv)
        if shell_execute is None:
            from ctypes import wintypes
            shell_execute = ctypes.windll.shell32.ShellExecuteW
            shell_execute.argtypes = [wintypes.HWND, wintypes.LPCWSTR, wintypes.LPCWSTR,
                                      wintypes.LPCWSTR, wintypes.LPCWSTR, ctypes.c_int]
            shell_execute.restype = wintypes.HINSTANCE
        result = shell_execute(None, 'runas', executable, subprocess.list2cmdline(arguments), None, 1)
        code = result if type(result) is int else getattr(result, 'value', 0)
        if type(code) is int and code > 32:
            return {'started': True, 'status': 'launched', 'code': code,
                    'note': 'Windows started the administrator relaunch after its UAC consent flow.'}
        return {'started': False, 'status': 'cancelled_or_failed', 'code': code,
                'note': 'Administrator permission was declined or unavailable. Continuing with standard access; protected actions still require administrator access.'}
    except (OSError, ValueError, AttributeError):
        return {'started': False, 'status': 'failed',
                'note': 'The administrator relaunch could not start. Continuing with standard access; protected actions still require administrator access.'}


def startup_administrator(backend, args, argv=None):
    """Attempt startup elevation once, only for a normal interactive GUI session."""
    if any(getattr(args, name, False) for name in ('audit', 'process_scan', 'verify_run', 'startup_mode', 'smoke_test', 'restart_ready_file')):
        return {'started': False, 'status': 'skipped'}
    try:
        if backend.is_admin():
            return {'started': False, 'status': 'already_administrator'}
        if getattr(args, 'admin_relaunch', False):
            return {'started': False, 'status': 'not_elevated',
                    'note': 'This relaunch has standard access. Administrator requests will not repeat during this startup; protected actions still require administrator access.'}
        if backend.preferences().get('request_admin_on_startup') is not True:
            return {'started': False, 'status': 'disabled'}
        return request_administrator(argv)
    except Exception:
        return {'started': False, 'status': 'failed',
                'note': 'Startup administrator access could not be checked. Continuing with standard access.'}
