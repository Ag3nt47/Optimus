"""Supported power-policy opportunities with owned, conditional recovery.

Only documented OS controls are used. Readback proves configuration, never a
speed gain. Laptop battery policy, custom schemes, governors, services, process
lifetimes and memory contents are not changed by this service.
"""
from __future__ import annotations

import copy
import ctypes
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
import threading
import time
import uuid

import psutil

from .automation import CURRENT_SETUP_VERSION, RunLease
from .filesystem import _ordinary_data_path, replace_with_retry
from .workflow import Preferences

BALANCED = '381b4222-f694-41f0-9685-ff5bb260df2e'
HIGH_PERFORMANCE = '8c5e7fda-e8bf-4a96-9a85-a6e23a8c635c'
AC_BALANCED = '00000000-0000-0000-0000-000000000000'
AC_PERFORMANCE = 'ded574b5-45a0-4f42-8737-46345c09c238'
_ID = re.compile(r'[a-f0-9]{32}\Z')
_LIMIT = 1024 * 1024
_TITLES = {
    'windows-ac-mode': 'Configure Best Performance for AC power',
    'windows-scheme': 'Select the existing High Performance desktop plan',
    'macos-ac-low-power': 'Disable Low Power Mode for the charger only',
    'linux-profile': 'Select the offered performance profile on an AC desktop',
    'unsupported': 'Review native power-policy support',
}
_DESIRED = {'windows-ac-mode': AC_PERFORMANCE, 'windows-scheme': HIGH_PERFORMANCE,
            'macos-ac-low-power': 0, 'linux-profile': 'performance'}
_BEFORE = {'windows-ac-mode': {AC_BALANCED}, 'windows-scheme': {BALANCED},
           'macos-ac-low-power': {1}, 'linux-profile': {'balanced', 'power-saver'}}


class _GUID(ctypes.Structure):
    _fields_ = [('data1', ctypes.c_uint32), ('data2', ctypes.c_uint16),
                ('data3', ctypes.c_uint16), ('data4', ctypes.c_ubyte * 8)]

    @classmethod
    def parse(cls, value):
        return cls.from_buffer_copy(uuid.UUID(value).bytes_le)

    def text(self):
        return str(uuid.UUID(bytes_le=bytes(self)))


class _SystemPowerStatus(ctypes.Structure):
    _fields_ = [('ac', ctypes.c_ubyte), ('battery', ctypes.c_ubyte),
                ('percent', ctypes.c_ubyte), ('energy_saver', ctypes.c_ubyte),
                ('life', ctypes.c_uint32), ('full', ctypes.c_uint32)]


class _WindowsAPI:
    """Feature-detected documented powrprof calls from the OS system directory."""
    def __init__(self):
        self.kernel = ctypes.WinDLL('kernel32.dll', use_last_error=True)
        self.kernel.GetSystemDirectoryW.argtypes = [ctypes.c_wchar_p, ctypes.c_uint32]
        self.kernel.GetSystemDirectoryW.restype = ctypes.c_uint32
        self.kernel.GetSystemPowerStatus.argtypes = [ctypes.POINTER(_SystemPowerStatus)]
        self.kernel.GetSystemPowerStatus.restype = ctypes.c_int
        directory = ctypes.create_unicode_buffer(32768)
        length = self.kernel.GetSystemDirectoryW(directory, len(directory))
        if not 0 < length < len(directory):
            raise OSError('The Windows system directory is unavailable.')
        self.power = ctypes.WinDLL(str(Path(directory.value) / 'powrprof.dll'), use_last_error=True)
        self.kernel.LocalFree.argtypes = [ctypes.c_void_p]
        self.kernel.LocalFree.restype = ctypes.c_void_p
        self.power.PowerGetActiveScheme.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.POINTER(_GUID))]
        self.power.PowerGetActiveScheme.restype = ctypes.c_uint32
        self.power.PowerSetActiveScheme.argtypes = [ctypes.c_void_p, ctypes.POINTER(_GUID)]
        self.power.PowerSetActiveScheme.restype = ctypes.c_uint32
        self.power.PowerEnumerate.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p,
                                              ctypes.c_uint32, ctypes.c_uint32, ctypes.c_void_p,
                                              ctypes.POINTER(ctypes.c_uint32)]
        self.power.PowerEnumerate.restype = ctypes.c_uint32
        for name in ('PowerGetUserConfiguredACPowerMode', 'PowerSetUserConfiguredACPowerMode',
                     'PowerGetUserConfiguredDCPowerMode'):
            function = getattr(self.power, name, None)
            if function is not None:
                function.argtypes = [ctypes.POINTER(_GUID)]
                function.restype = ctypes.c_uint32

    def source(self):
        value = _SystemPowerStatus()
        if not self.kernel.GetSystemPowerStatus(ctypes.byref(value)):
            return {'ac': None, 'no_battery': None, 'energy_saver': None}
        return {'ac': True if value.ac == 1 else False if value.ac == 0 else None,
                'no_battery': True if value.battery == 128 else None if value.battery == 255 else False,
                'energy_saver': True if value.energy_saver == 1 else False if value.energy_saver == 0 else None}

    def active(self):
        pointer = ctypes.POINTER(_GUID)()
        if self.power.PowerGetActiveScheme(None, ctypes.byref(pointer)) != 0 or not pointer:
            raise OSError('The active Windows power scheme is unavailable.')
        try:
            return pointer.contents.text()
        finally:
            self.kernel.LocalFree(pointer)

    def schemes(self):
        result = []
        for index in range(128):
            value, size = _GUID(), ctypes.c_uint32(ctypes.sizeof(_GUID))
            code = self.power.PowerEnumerate(None, None, None, 16, index, ctypes.byref(value), ctypes.byref(size))
            if code == 259:
                return result
            if code != 0 or size.value != 16:
                raise OSError('The Windows power-plan inventory is incomplete.')
            result.append(value.text())
        raise ValueError('The Windows power-plan inventory exceeds its bound.')

    def ac_mode(self):
        get = getattr(self.power, 'PowerGetUserConfiguredACPowerMode', None)
        set_mode = getattr(self.power, 'PowerSetUserConfiguredACPowerMode', None)
        if get is None or set_mode is None:
            return None
        value = _GUID()
        if get(ctypes.byref(value)) != 0:
            return None
        return value.text()

    def dc_mode(self):
        get = getattr(self.power, 'PowerGetUserConfiguredDCPowerMode', None)
        value = _GUID()
        return value.text() if get is not None and get(ctypes.byref(value)) == 0 else None

    def set(self, mechanism, value):
        if mechanism not in ('windows-ac-mode', 'windows-scheme'):
            return False
        guid = _GUID.parse(value)
        code = (self.power.PowerSetUserConfiguredACPowerMode(ctypes.byref(guid)) if mechanism == 'windows-ac-mode'
                else self.power.PowerSetActiveScheme(None, ctypes.byref(guid)))
        return code == 0


class _WindowsPower:
    def __init__(self, api=None):
        self.api = api or _WindowsAPI()

    def read(self, timeout=30):
        value = self.api.ac_mode()
        active = self.api.active()
        return dict(self.api.source(), mechanism='windows-ac-mode' if value is not None else 'windows-scheme',
                    value=value if value is not None else active, active_scheme=active,
                    offered=self.api.schemes(), dc_mode=self.api.dc_mode(), can_write=True)

    def write(self, mechanism, value):
        return self.api.set(mechanism, value)


def _trusted_executable(path):
    path = Path(path)
    resolved = path.resolve(strict=True)
    if not str(resolved).startswith(('/usr/', '/bin/', '/sbin/')):
        raise ValueError('The native power tool is outside trusted OS paths.')
    for part in (resolved, *resolved.parents):
        info = part.stat()
        if info.st_uid != 0 or info.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
            raise ValueError('The native power tool or its directory is not OS-owned.')
    if not resolved.is_file():
        raise ValueError('The native power tool is unavailable.')
    return str(path)


def _run(argv, timeout=8):
    environment = dict(os.environ, LC_ALL='C', LANG='C')
    completed = subprocess.run(argv, stdin=subprocess.DEVNULL, capture_output=True, text=True,
                               encoding='utf-8', errors='replace', timeout=timeout, env=environment,
                               creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    if len(completed.stdout) + len(completed.stderr) > 65536:
        raise ValueError('The native power-tool result is oversized.')
    if completed.returncode != 0:
        raise OSError('The native power tool refused the operation.')
    return completed.stdout


def _pmset_custom(text):
    if not isinstance(text, str) or len(text) > 65536:
        raise ValueError('Invalid pmset settings.')
    sections, current = {}, None
    for line in text.splitlines():
        heading = re.fullmatch(r'(Battery Power|AC Power|UPS Power):\s*', line)
        if heading:
            current = heading[1]
            if current in sections:
                raise ValueError('Duplicate pmset section.')
            sections[current] = {}
        elif line.strip():
            match = re.fullmatch(r'\s+([a-zA-Z][a-zA-Z0-9_ ]*?)\s+(-?\d+(?:\s.*)?)\s*', line)
            if current is None or match is None or match[1] in sections[current]:
                raise ValueError('Unrecognized pmset settings.')
            sections[current][match[1]] = match[2]
    ac = sections.get('AC Power', {})
    if ac.get('lowpowermode') not in ('0', '1') or ac.get('highpowermode', '0') != '0' or 'powermode' in ac:
        raise ValueError('This macOS power-mode configuration is unsupported.')
    return sections


class _MacPower:
    def __init__(self, runner=None, root=None):
        self.runner = runner or _run
        self.executable = '/usr/bin/pmset' if runner else _trusted_executable('/usr/bin/pmset')
        self.root = root or (lambda: hasattr(os, 'geteuid') and os.geteuid() == 0)

    def read(self, timeout=30):
        command = self.runner if self.runner is not _run else lambda argv: _run(argv, timeout=min(8, timeout / 2))
        source = command([self.executable, '-g', 'batt'])
        lines = source.splitlines()
        ac = True if lines and lines[0] == "Now drawing from 'AC Power'" else False if lines and lines[0] == "Now drawing from 'Battery Power'" else None
        settings = _pmset_custom(command([self.executable, '-g', 'custom']))
        return {'mechanism': 'macos-ac-low-power', 'value': int(settings['AC Power']['lowpowermode']),
                'ac': ac, 'can_write': bool(self.root()), 'settings': settings}

    def write(self, mechanism, value):
        if mechanism != 'macos-ac-low-power' or value not in (0, 1) or not self.root():
            return False
        self.runner([self.executable, '-c', 'lowpowermode', str(value)])
        return True


def _linux_supply(root=Path('/sys/class/power_supply')):
    try:
        entries = list(root.iterdir())
        if len(entries) > 64:
            raise ValueError('Too many power supplies.')
        battery, ac = False, False
        for entry in entries:
            kind = (entry / 'type').read_text(encoding='ascii').strip()
            if len(kind) > 64:
                raise ValueError('Invalid power supply type.')
            if kind == 'Battery':
                battery = True
            elif kind in ('Mains', 'USB', 'USB_C', 'USB_PD', 'USB_DCP', 'USB_CDP', 'USB_ACA'):
                online = (entry / 'online').read_text(encoding='ascii').strip()
                if online not in ('0', '1'):
                    raise ValueError('Invalid online power supply state.')
                ac = ac or online == '1'
            else:
                raise ValueError('Unknown power supply type.')
        return {'ac': True if ac else None, 'no_battery': not battery}
    except (OSError, ValueError, UnicodeError):
        return {'ac': None, 'no_battery': None}


def _profiles(text):
    profiles, current = {}, None
    for line in text.splitlines():
        heading = re.fullmatch(r'[ *]*(balanced|power-saver|performance):\s*', line)
        if heading:
            current = heading[1]
            if current in profiles:
                raise ValueError('Duplicate power profile.')
            profiles[current] = {}
        elif line.strip():
            match = re.fullmatch(r'\s+([A-Za-z][A-Za-z-]*):\s*(.*?)\s*', line)
            if current is None or match is None or match[1] in profiles[current]:
                raise ValueError('Unknown power profile metadata.')
            profiles[current][match[1]] = match[2]
    performance = profiles.get('performance', {})
    clear = performance.get('Degraded') == 'no' and performance.get('Inhibited', 'no') == 'no'
    return profiles, clear


class _LinuxPower:
    def __init__(self, runner=None, supply=None):
        self.runner = runner or _run
        self.executable = '/usr/bin/powerprofilesctl' if runner else _trusted_executable('/usr/bin/powerprofilesctl')
        self.supply = supply or _linux_supply

    def read(self, timeout=30):
        command = self.runner if self.runner is not _run else lambda argv: _run(argv, timeout=min(8, timeout / 3))
        current = command([self.executable, 'get']).strip()
        if current not in ('balanced', 'power-saver', 'performance'):
            raise ValueError('Unknown active power profile.')
        offered, clear = _profiles(command([self.executable, 'list']))
        holds = command([self.executable, 'list-holds'])
        return dict(self.supply(), mechanism='linux-profile', value=current, offered=list(offered),
                    holds_clear=not holds.strip(), performance_clear=clear, can_write=True)

    def write(self, mechanism, value):
        if mechanism != 'linux-profile' or value not in ('balanced', 'power-saver', 'performance'):
            return False
        self.runner([self.executable, 'set', value])
        return True


def _safe(snapshot):
    mechanism = snapshot.get('mechanism')
    if snapshot.get('ac') is not True:
        return 'Battery operation or unknown AC power: keep the current power policy.'
    if mechanism.startswith('windows-') and snapshot.get('energy_saver') is not False:
        return 'Windows Energy Saver is active or unverifiable; preserve the energy-saving choice.'
    if mechanism.startswith('windows-') and snapshot.get('active_scheme') not in (BALANCED, HIGH_PERFORMANCE):
        return 'A custom, OEM or power-saving Windows plan is active; preserve that choice.'
    if mechanism in ('windows-scheme', 'linux-profile') and snapshot.get('no_battery') is not True:
        return 'This global policy could affect future battery operation; automatic changes require a verified battery-free AC desktop.'
    if mechanism == 'windows-scheme' and HIGH_PERFORMANCE not in snapshot.get('offered', []):
        return 'The standard High Performance plan is not already offered; no plan will be created.'
    if mechanism == 'linux-profile' and ('performance' not in snapshot.get('offered', [])
            or snapshot.get('holds_clear') is not True or snapshot.get('performance_clear') is not True):
        return 'Performance is unavailable, held, degraded, inhibited or unverifiable; preserve the active profile.'
    if snapshot.get('can_write') is not True:
        return 'Native permissions are missing. No authentication or elevation attempt was started.'
    return ''


def _same(left, right):
    mechanism = left.get('mechanism')
    return (mechanism == right.get('mechanism') and left.get('value') == right.get('value')
            and (not mechanism.startswith('windows-') or left.get('active_scheme') == right.get('active_scheme')))


def _expected(snapshot, value):
    result = dict(snapshot, value=value)
    if snapshot['mechanism'] == 'windows-scheme':
        result['active_scheme'] = value
    return result


def _preserved(before, after):
    if before['mechanism'] == 'macos-ac-low-power':
        old, new = copy.deepcopy(before['settings']), copy.deepcopy(after.get('settings', {}))
        old.get('AC Power', {}).pop('lowpowermode', None)
        new.get('AC Power', {}).pop('lowpowermode', None)
        return old == new
    if before['mechanism'] == 'windows-ac-mode':
        return (before.get('active_scheme') == after.get('active_scheme')
                and (before.get('dc_mode') is None or before.get('dc_mode') == after.get('dc_mode')))
    return True


def _current_owner():
    if os.name != 'nt':
        return 'uid:' + str(os.getuid())
    # TokenUser, not a display name, binds journals across administrator modes.
    from ctypes import wintypes
    advapi = ctypes.WinDLL('advapi32.dll', use_last_error=True)
    kernel = ctypes.WinDLL('kernel32.dll', use_last_error=True)
    token, length = wintypes.HANDLE(), wintypes.DWORD()
    kernel.GetCurrentProcess.argtypes = []
    kernel.GetCurrentProcess.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.CloseHandle.restype = wintypes.BOOL
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    kernel.LocalFree.restype = ctypes.c_void_p
    advapi.OpenProcessToken.argtypes = [wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE)]
    advapi.OpenProcessToken.restype = wintypes.BOOL
    advapi.GetTokenInformation.argtypes = [wintypes.HANDLE, ctypes.c_uint32, ctypes.c_void_p,
                                          wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)]
    advapi.GetTokenInformation.restype = wintypes.BOOL
    if not advapi.OpenProcessToken(kernel.GetCurrentProcess(), 8, ctypes.byref(token)):
        raise OSError('Current Windows user identity is unavailable.')
    try:
        advapi.GetTokenInformation(token, 1, None, 0, ctypes.byref(length))
        if not 0 < length.value <= 65536:
            raise OSError('Invalid Windows user token.')
        buffer = ctypes.create_string_buffer(length.value)
        if not advapi.GetTokenInformation(token, 1, buffer, length, ctypes.byref(length)):
            raise OSError('Current Windows user token is unavailable.')
        sid = ctypes.cast(buffer, ctypes.POINTER(ctypes.c_void_p))[0]
        text = wintypes.LPWSTR()
        advapi.ConvertSidToStringSidW.argtypes = [ctypes.c_void_p, ctypes.POINTER(wintypes.LPWSTR)]
        advapi.ConvertSidToStringSidW.restype = wintypes.BOOL
        if not advapi.ConvertSidToStringSidW(sid, ctypes.byref(text)):
            raise OSError('Current Windows SID is unavailable.')
        try:
            return 'sid:' + text.value
        finally:
            kernel.LocalFree.argtypes = [ctypes.c_void_p]
            kernel.LocalFree(text)
    finally:
        kernel.CloseHandle(token)


def _diagnostics():
    memory = psutil.virtual_memory()
    return {'id': 'resource-observations', 'title': 'Read-only resource observations', 'status': 'diagnostic',
            'reason': 'CPU count and current memory pressure are observations, not a speed benchmark. Review unnecessary work manually; no process is paused or application removed.',
            'before': {'logical_cpus': psutil.cpu_count(), 'memory_total_bytes': int(memory.total),
                       'memory_percent': float(memory.percent)}, 'desired': None}


def _private_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_name(path.name + '.' + uuid.uuid4().hex + '.tmp')
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, 'w', encoding='utf-8') as stream:
            json.dump(value, stream, allow_nan=False)
            stream.flush()
            os.fsync(stream.fileno())
        replace_with_retry(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


class SpeedEnhancements:
    def __init__(self, backend, *, system=None, adapter=None, diagnostics=None, owner=None):
        self.backend = backend
        self.system = system or getattr(backend, 'system', None) or sys.platform
        self.directory = Path(backend.data_dir) / 'speed-enhancements'
        self.lock = threading.Lock()
        self.adapter = adapter
        self.diagnostics = diagnostics or _diagnostics
        self.owner = owner or _current_owner()
        self._noops = set()

    def _adapter(self):
        if self.adapter is None:
            self.adapter = {'win32': _WindowsPower, 'darwin': _MacPower, 'linux': _LinuxPower}.get(self.system, lambda: None)()
        if self.adapter is None:
            raise ValueError('This platform has no supported automatic power control.')
        return self.adapter

    def _power(self):
        snapshot = self._adapter().read()
        mechanism = snapshot.get('mechanism')
        if mechanism not in _DESIRED:
            raise ValueError('Unknown native power mechanism.')
        reason = _safe(snapshot)
        value, desired = snapshot.get('value'), _DESIRED[mechanism]
        state = ('deferred' if reason else 'already_configured' if value == desired
                 else 'available' if value in _BEFORE[mechanism] else 'deferred')
        reason = reason or ('The supported policy is already configured; no change is needed.' if state == 'already_configured'
                            else 'Supported AC policy can be changed with exact readback and conditional recovery. Higher power or fan noise is possible; no speed gain is promised.' if state == 'available'
                            else 'The existing policy is unknown or intentionally energy-saving; preserve it.')
        return snapshot, {'id': mechanism, 'title': _TITLES[mechanism], 'status': state,
                          'reason': reason, 'before': value, 'desired': desired}

    def scan(self):
        try:
            _, power = self._power()
        except Exception as error:
            power = {'id': 'unsupported', 'title': _TITLES['unsupported'], 'status': 'deferred',
                     'reason': 'Native power control could not be verified (' + type(error).__name__ + '); no change is proposed.', 'before': None, 'desired': None}
        items = [power]
        try:
            items.append(self.diagnostics())
        except Exception:
            items.append({'id': 'resource-observations', 'title': 'Read-only resource observations', 'status': 'diagnostic',
                          'reason': 'Resource observations are unavailable.', 'before': None, 'desired': None})
        return {'platform': self.system, 'items': items, 'summary': power['reason'], 'read_only': True}

    def _path(self, path, *, file=False):
        info = _ordinary_data_path(path, file=file)
        if not file and path.exists():
            info = path.lstat()
        if info is not None and os.name != 'nt' and info.st_uid != os.getuid():
            raise ValueError('Speed history is not owned by the current user.')
        return info

    def _key(self, create=False):
        self._path(self.directory)
        path = self.directory / 'owner.json'
        info = self._path(path, file=True)
        if info is None:
            if not create:
                return None
            self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
            value = {'schema': 1, 'owner': self.owner, 'key': os.urandom(32).hex()}
            _private_json(path, value)
            if os.name != 'nt':
                path.chmod(0o600)
        else:
            value = self._read_file(path)
        if (not isinstance(value, dict) or value.get('schema') != 1 or value.get('owner') != self.owner
                or not re.fullmatch('[a-f0-9]{64}', str(value.get('key', '')))):
            raise ValueError('The speed-history ownership anchor is invalid.')
        return bytes.fromhex(value['key'])

    def _read_file(self, path):
        info = self._path(path, file=True)
        if info is None or info.st_size > _LIMIT:
            raise ValueError('Speed history is missing or oversized.')
        if os.name != 'nt' and info.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
            raise ValueError('Speed history has unsafe write permissions.')
        descriptor = os.open(path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0))
        with os.fdopen(descriptor, 'rb') as stream:
            opened = os.fstat(stream.fileno())
            if (opened.st_dev, opened.st_ino, opened.st_nlink) != (info.st_dev, info.st_ino, 1):
                raise ValueError('Speed history changed identity while opening.')
            raw = stream.read(_LIMIT + 1)
        if len(raw) > _LIMIT:
            raise ValueError('Speed history is oversized.')
        return json.loads(raw)

    @staticmethod
    def _signature(value, key):
        payload = {name: part for name, part in value.items() if name != 'signature'}
        return hmac.new(key, json.dumps(payload, sort_keys=True, separators=(',', ':'), allow_nan=False).encode(), hashlib.sha256).hexdigest()

    def _write(self, value):
        self._path(self.directory)
        path = self.directory / (value['id'] + '.json')
        self._path(path, file=True)
        value['signature'] = self._signature(value, self._key(create=True))
        _private_json(path, value)
        if os.name != 'nt':
            path.chmod(0o600)

    def _load(self, identity):
        if not isinstance(identity, str) or not _ID.fullmatch(identity):
            raise ValueError('Choose one owned speed-history ID.')
        key = self._key()
        if key is None:
            raise ValueError('No owned speed history is available.')
        value = self._read_file(self.directory / (identity + '.json'))
        if (not isinstance(value, dict) or value.get('schema') != 1 or value.get('id') != identity
                or value.get('owner') != self.owner or value.get('platform') != self.system
                or not hmac.compare_digest(str(value.get('signature', '')), self._signature(value, key))):
            raise ValueError('The speed-history identity or signature is invalid.')
        change = value.get('change')
        if (not isinstance(change, dict) or change.get('mechanism') not in _DESIRED
                or change.get('before', {}).get('value') not in _BEFORE[change['mechanism']]
                or change.get('desired') != _DESIRED[change['mechanism']]):
            raise ValueError('Unsupported recovery evidence.')
        return value

    def _records(self):
        self._path(self.directory)
        if not self.directory.exists():
            return []
        paths = list(self.directory.glob('*.json'))
        if len(paths) > 1001:
            raise ValueError('Speed history exceeds its bounded inventory.')
        items = []
        for path in paths:
            if path.name == 'owner.json':
                continue
            value = self._load(path.stem)
            items.append(value)
        return items

    def history(self):
        items = [{name: copy.deepcopy(value.get(name)) for name in
                  ('id', 'status', 'summary', 'restore_available', 'created_at')} for value in self._records()]
        return sorted(items, key=lambda row: row['created_at'], reverse=True)[:100]

    def _result(self):
        return {'schema': 1, 'id': uuid.uuid4().hex, 'platform': self.system, 'owner': self.owner,
                'created_at': time.time(), 'status': 'running', 'items': [], 'counts': {},
                'summary': '', 'log_path': None, 'restore_available': False, 'failed_items': 0, 'cancelled': False}

    def _noop(self, value, status):
        self._noops.add(value['id'])
        if len(self._noops) > 100:
            self._noops = {value['id']}
        return self._finish(value, status)

    @staticmethod
    def _finish(value, status):
        value['status'] = status
        value['counts'] = {state: sum(item.get('status') == state for item in value['items'])
                           for state in ('applied', 'already_configured', 'deferred', 'skipped', 'failed', 'uncertain', 'restored')}
        value['failed_items'] = value['counts']['failed'] + value['counts']['uncertain']
        value['finished_at'] = time.time()
        value['summary'] = ('Power configuration: ' + '; '.join(f'{number} {state.replace("_", " ")}' for state, number in value['counts'].items() if number)
                            + '. Configuration readback is not a speed benchmark.')
        return value

    def apply(self, settings, cancel_event=None, progress_callback=None):
        validator = getattr(getattr(self.backend, 'preference_store', None), 'validate', Preferences.validate)
        configuration = validator(settings)
        value = self._result()
        if configuration.get('speed_enhancements_enabled') is not True or configuration.get('setup_version') != CURRENT_SETUP_VERSION:
            value['items'] = [{'id': 'speed-enhancements', 'title': 'Supported speed enhancements', 'status': 'skipped',
                               'reason': 'Speed enhancements are off or the current setup has not been saved.'}]
            return self._noop(value, 'skipped')
        if not self.lock.acquire(blocking=False):
            raise RuntimeError('A speed-enhancement operation is already active.')
        lease = None
        try:
            self._path(self.directory)
            lease = RunLease(self.directory).acquire()
            if any(row['status'] in ('prepared', 'uncertain') for row in self._records()):
                value['items'] = [{'id': 'power-policy', 'title': 'Review the previous power operation', 'status': 'deferred',
                                   'reason': 'A previous power operation has an uncertain outcome. Review its owned history before another change.'}]
                return self._noop(value, 'deferred')
            try:
                before, item = self._power()
            except Exception as error:
                value['items'] = [{'id': 'power-policy', 'title': 'Review native power support', 'status': 'deferred',
                                   'reason': 'Power state is unavailable (' + type(error).__name__ + ').'}]
                return self._noop(value, 'deferred')
            value['items'] = [item]
            if item['status'] != 'available':
                return self._noop(value, 'skipped' if item['status'] == 'already_configured' else 'deferred')
            if cancel_event is not None and cancel_event.is_set():
                value['cancelled'] = True
                item.update(status='skipped', reason='Cancelled before any power change.')
                return self._noop(value, 'cancelled')
            value['change'] = {'mechanism': before['mechanism'], 'before': copy.deepcopy(before), 'desired': item['desired']}
            value['status'] = 'prepared'
            value['log_path'] = str(self.directory / (value['id'] + '.json'))
            item['status'] = 'prepared'
            self._write(value)  # Never call a power setter without durable undo.
            if progress_callback:
                progress_callback({'stage': item['title'], 'description': item['reason'], 'percentage': 20, 'processes': [item['title']]})
            fresh = self._adapter().read()
            if not _same(before, fresh) or _safe(fresh) or cancel_event is not None and cancel_event.is_set():
                item.update(status='deferred', reason='Power source, policy, permission or cancellation changed before application; no setter was called.')
                value['cancelled'] = cancel_event is not None and cancel_event.is_set()
                self._finish(value, 'cancelled' if value['cancelled'] else 'deferred')
                self._write(value)
                return value
            return self._change(value, item, before, item['desired'], cancel_event)
        finally:
            if lease is not None:
                lease.release()
            self.lock.release()

    def _change(self, value, item, before, desired, cancel_event, restoring=False):
        succeeded, error = False, None
        try:
            succeeded = self._adapter().write(before['mechanism'], desired) is True
        except Exception as caught:
            error = type(caught).__name__
        try:
            after = self._adapter().read()
            value['restore_after' if restoring else 'after'] = after
            good = _same(_expected(before, desired), after) and _preserved(before, after)
            unchanged = _same(before, after)
        except Exception as caught:
            good, unchanged = False, False
            error = error or type(caught).__name__
        value['cancelled'] = cancel_event is not None and cancel_event.is_set()
        item.update(status='restored' if restoring and succeeded and good else 'applied' if succeeded and good
                    else 'failed' if unchanged else 'uncertain',
                    reason='The exact supported configuration was read back; this does not prove a speed increase.' if succeeded and good
                    else 'The power operation did not produce a verified result' + (' (' + error + ')' if error else '') + '. Review native power settings.')
        value['restore_available'] = unchanged if restoring else good
        self._finish(value, 'cancelled' if value['cancelled'] else 'restored' if restoring and succeeded and good
                     else 'completed' if succeeded and good else 'failed' if unchanged else 'uncertain')
        self._write(value)
        return value

    def restore(self, run_id, cancel_event=None, progress_callback=None):
        if not self.lock.acquire(blocking=False):
            raise RuntimeError('A speed-enhancement operation is already active.')
        lease = None
        try:
            self._path(self.directory)
            lease = RunLease(self.directory).acquire()
            value = self._load(run_id)
            if not value.get('restore_available') or value.get('status') in ('restored', 'superseded'):
                raise ValueError('This run has no owned power change available to restore.')
            current = self._adapter().read()
            change = value['change']
            expected = _expected(change['before'], change['desired'])
            item = {'id': change['mechanism'], 'title': 'Restore the saved power configuration',
                    'status': 'prepared', 'before': current.get('value'), 'desired': change['before']['value']}
            value['items'] = [item]
            if not _same(expected, current) or _safe(current):
                item.update(status='deferred', reason='The current power source, policy or permissions no longer match the Optimus-owned change. A later user choice was not overwritten.')
                return self._finish(value, 'deferred')
            if cancel_event is not None and cancel_event.is_set():
                value['cancelled'] = True
                item.update(status='skipped', reason='Cancelled before restoring the power configuration.')
                return self._finish(value, 'cancelled')
            value['status'] = 'prepared'
            value['restore_started_at'] = time.time()
            self._write(value)
            if progress_callback:
                progress_callback({'stage': item['title'], 'description': 'Restoring only the exact saved configuration.', 'percentage': 20, 'processes': [item['title']]})
            fresh = self._adapter().read()
            if not _same(current, fresh) or _safe(fresh) or cancel_event is not None and cancel_event.is_set():
                item.update(status='deferred', reason='Power state changed before recovery; no setter was called.')
                self._finish(value, 'deferred')
                self._write(value)
                return value
            return self._change(value, item, current, change['before']['value'], cancel_event, restoring=True)
        finally:
            if lease is not None:
                lease.release()
            self.lock.release()

    def verify(self, report, cancel_event=None, timeout=30):
        check = {'target': 'Supported power configuration', 'status': 'unverified', 'reason': 'Owned power evidence is unavailable.'}
        try:
            if type(timeout) not in (int, float) or not 0 < timeout <= 120:
                raise ValueError('A bounded verification timeout is required.')
            if cancel_event is not None and cancel_event.is_set():
                check['reason'] = 'Configuration verification was cancelled.'
            elif (isinstance(report, dict) and report.get('id') in self._noops
                  and report.get('log_path') is None and not report.get('restore_available')):
                check.update(status='not_applicable', reason='No power change was claimed; skipped, disabled and diagnostic work is not a confirmed enhancement.')
            else:
                value = self._load(report.get('id') if isinstance(report, dict) else None)
                if value['status'] not in ('completed', 'restored'):
                    check.update(status='needs_review', reason='The owned power operation was not completed with verified readback.')
                else:
                    deadline = time.monotonic() + timeout
                    current = self._adapter().read(timeout=timeout)
                    desired = value['change']['before']['value'] if value['status'] == 'restored' else value['change']['desired']
                    expected = _expected(value['change']['before'], desired)
                    if time.monotonic() > deadline:
                        check['reason'] = 'The bounded readback exceeded its verification budget.'
                    elif _same(expected, current):
                        check.update(status='confirmed', reason='The exact owned power configuration is still selected. This confirms policy only, not effective CPU boost or a speed gain.')
                    else:
                        check.update(status='needs_review', reason='The current power configuration differs from the owned result; later OS or user changes were preserved.')
        except Exception as error:
            check['reason'] = 'Owned configuration verification is unavailable (' + type(error).__name__ + ').'
        return {'status': check['status'], 'checks': [check], 'summary': check['reason'], 'read_only': True}
