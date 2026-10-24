"""Read-only readiness decisions at safe boundaries; never suspend live work."""
from __future__ import annotations

import copy
import ctypes
from datetime import datetime
import math
import os
import time

DEFAULTS = {'enabled': False, 'cpu_limit': 80, 'memory_limit': 90,
            'idle_seconds': 0, 'require_ac': False, 'avoid_fullscreen': True,
            'avoid_metered': True, 'start_hour': 0, 'end_hour': 0}


def validate_configuration(value):
    if not isinstance(value, dict) or set(value) - set(DEFAULTS):
        raise ValueError('Unsupported adaptive maintenance choices.')
    result = dict(DEFAULTS, **copy.deepcopy(value))
    for key in ('enabled', 'require_ac', 'avoid_fullscreen', 'avoid_metered'):
        if type(result[key]) is not bool:
            raise ValueError(key + ' must be on or off.')
    for key, low, high in (('cpu_limit', 10, 100), ('memory_limit', 10, 100),
                           ('idle_seconds', 0, 3600), ('start_hour', 0, 23), ('end_hour', 0, 23)):
        if type(result[key]) is not int or not low <= result[key] <= high:
            raise ValueError('Unsupported adaptive ' + key + '.')
    return result


def _windows_interaction():
    if os.name != 'nt':
        return {'idle_seconds': None, 'fullscreen': None, 'ac_power': None}
    from ctypes import wintypes
    class Input(ctypes.Structure):
        _fields_ = [('cbSize', wintypes.UINT), ('dwTime', wintypes.DWORD)]
    class Power(ctypes.Structure):
        _fields_ = [('ACLineStatus', wintypes.BYTE), ('BatteryFlag', wintypes.BYTE),
                    ('BatteryLifePercent', wintypes.BYTE), ('SystemStatusFlag', wintypes.BYTE),
                    ('BatteryLifeTime', wintypes.DWORD), ('BatteryFullLifeTime', wintypes.DWORD)]
    class Monitor(ctypes.Structure):
        _fields_ = [('cbSize', wintypes.DWORD), ('rcMonitor', wintypes.RECT),
                    ('rcWork', wintypes.RECT), ('dwFlags', wintypes.DWORD)]
    user = ctypes.WinDLL('user32', use_last_error=True)
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    user.GetLastInputInfo.argtypes = [ctypes.POINTER(Input)]
    kernel.GetTickCount.restype = wintypes.DWORD
    user.GetForegroundWindow.restype = wintypes.HWND
    user.GetShellWindow.restype = wintypes.HWND
    user.GetDesktopWindow.restype = wintypes.HWND
    user.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    user.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    user.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
    user.MonitorFromWindow.argtypes = [wintypes.HWND, wintypes.DWORD]
    user.MonitorFromWindow.restype = wintypes.HANDLE
    user.GetMonitorInfoW.argtypes = [wintypes.HANDLE, ctypes.POINTER(Monitor)]
    kernel.GetSystemPowerStatus.argtypes = [ctypes.POINTER(Power)]
    last, power = Input(), Power()
    last.cbSize = ctypes.sizeof(last)
    idle = ((kernel.GetTickCount() - last.dwTime) & 0xffffffff) / 1000 if user.GetLastInputInfo(ctypes.byref(last)) else None
    ac = None
    if kernel.GetSystemPowerStatus(ctypes.byref(power)) and power.ACLineStatus in (0, 1):
        ac = power.ACLineStatus == 1
    fullscreen = None
    window = user.GetForegroundWindow()
    if window:
        pid, bounds, monitor = wintypes.DWORD(), wintypes.RECT(), Monitor()
        monitor.cbSize = ctypes.sizeof(monitor)
        user.GetWindowThreadProcessId(window, ctypes.byref(pid))
        name = ctypes.create_unicode_buffer(256)
        user.GetClassNameW(window, name, len(name))
        if pid.value == os.getpid() or window in (user.GetShellWindow(), user.GetDesktopWindow()) or name.value in ('Progman', 'WorkerW'):
            fullscreen = False
        elif user.GetWindowRect(window, ctypes.byref(bounds)) and user.GetMonitorInfoW(user.MonitorFromWindow(window, 2), ctypes.byref(monitor)):
            fullscreen = all(abs(getattr(bounds, key) - getattr(monitor.rcMonitor, key)) <= 1
                             for key in ('left', 'right', 'top', 'bottom'))
    return {'idle_seconds': idle, 'fullscreen': fullscreen, 'ac_power': ac}


def collect_state(*, network_reader=None):
    import psutil
    result = {'captured_at': time.time(), 'cpu_percent': None, 'memory_percent': None,
              'idle_seconds': None, 'fullscreen': None, 'ac_power': None, 'metered': None, 'errors': []}
    for key, operation in (('cpu_percent', lambda: psutil.cpu_percent(interval=.15)),
                           ('memory_percent', lambda: psutil.virtual_memory().percent)):
        try:
            value = operation()
            if type(value) in (int, float) and math.isfinite(value) and 0 <= value <= 100:
                result[key] = value
        except (OSError, psutil.Error) as error:
            result['errors'].append(key + ': ' + type(error).__name__)
    try:
        result.update(_windows_interaction())
        if os.name != 'nt':
            battery = psutil.sensors_battery()
            if battery is not None:
                result['ac_power'] = battery.power_plugged if type(battery.power_plugged) is bool else None
    except (OSError, AttributeError, psutil.Error) as error:
        result['errors'].append('Interaction/power: ' + type(error).__name__)
    if callable(network_reader):
        try:
            value = network_reader()
            if isinstance(value, dict) and type(value.get('metered')) is bool:
                result['metered'] = value['metered']
        except Exception as error:
            result['errors'].append('Network cost: ' + type(error).__name__)
    return result


def assess(configuration, state, *, network=False, now=None):
    config = validate_configuration(configuration)
    reasons = []
    if config['enabled']:
        current = datetime.now() if now is None else now
        start, end, hour = config['start_hour'], config['end_hour'], current.hour
        within = (start == end or start <= hour < end) if start <= end else (hour >= start or hour < end)
        if not within:
            reasons.append('Outside the saved maintenance hours.')
        for key, limit, label in (('cpu_percent', 'cpu_limit', 'CPU'), ('memory_percent', 'memory_limit', 'Memory')):
            value = state.get(key)
            if type(value) not in (int, float) or not math.isfinite(value) or not 0 <= value <= 100:
                reasons.append(label + ' pressure is unavailable.')
            elif value >= config[limit]:
                reasons.append(label + ' pressure exceeds the saved limit.')
        idle = state.get('idle_seconds')
        if config['idle_seconds'] and (type(idle) not in (int, float) or not math.isfinite(idle) or idle < config['idle_seconds']):
            reasons.append('Waiting for the saved idle period.')
        if config['require_ac'] and state.get('ac_power') is not True:
            reasons.append('AC power is required and has not been established.')
        if config['avoid_fullscreen'] and state.get('fullscreen') is True:
            reasons.append('A foreground application occupies the full screen.')
        if network and config['avoid_metered'] and state.get('metered') is not False:
            reasons.append('An unrestricted network connection has not been established.')
    return {'schema': 1, 'read_only': True, 'enabled': config['enabled'], 'allowed': not reasons,
            'status': 'deferred' if reasons else 'ready', 'reasons': reasons, 'observations': copy.deepcopy(state),
            'note': 'Decisions apply before new work. Active installers are allowed to finish; no application is suspended or terminated.'}


def deferred_result(assessment):
    return {'skipped': True, 'deferred': True, 'installer_started': False, 'outcomeKnown': True,
            'failed_items': 0, 'adaptive_readiness': assessment,
            'note': ' '.join(assessment.get('reasons', [])) or 'Deferred by the saved maintenance policy.'}


def checkpoint(backend, *, title=None, network=False):
    """Return None when disabled or outside a mutation boundary; no host probe."""
    if title is not None:
        network = network or title in ('Review or update configured software',
            'Review or install approved native package updates', 'Install approved driver updates',
            'Check or install Windows security updates', 'Update approved Chocolatey packages')
        if not network and not title.startswith(('Clean ', 'Apply ', 'Remove ', 'Optimize ', 'Expire ')):
            return None
    reader = getattr(backend, 'preferences', None)
    settings = reader() if callable(reader) else {}
    if not isinstance(settings, dict) or settings.get('toolkit_settings', {}).get('adaptive', {}).get('enabled') is not True:
        return None
    method = getattr(backend, 'maintenance_readiness', None)
    result = method(network=network) if callable(method) else None
    if not isinstance(result, dict) or type(result.get('allowed')) is not bool:
        return {'allowed': False, 'status': 'deferred', 'reasons': ['Adaptive readiness is unavailable.']}
    return result
