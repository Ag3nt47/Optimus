"""Owned driver exports and explicit, verified Windows driver rollback.

Constructors do not query devices or load native libraries. A package export is
not a system image. DiRollbackDriver uses Windows' single previous driver for
one exact device instance; exported packages are separate recovery evidence.

Primary API references:
https://learn.microsoft.com/windows/win32/api/newdev/nf-newdev-dirollbackdriver
https://learn.microsoft.com/windows/win32/api/setupapi/nf-setupapi-setupdiopendeviceinfow
https://learn.microsoft.com/windows-hardware/drivers/devtest/pnputil-command-syntax
https://raw.githubusercontent.com/microsoft/win32metadata/main/generation/WinSDK/RecompiledIdlHeaders/um/newdev.h
"""
from __future__ import annotations

import copy
import ctypes
import hashlib
import hmac
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
import sys
import threading
import time
import uuid

from .automation import RunLease
from .filesystem import _ordinary_data_path, resolve_data_root
from .parallel_updates import pending_updates_barrier
from .speed_enhancements import _current_owner, _private_json

_ID = re.compile(r'[a-f0-9]{32}\Z')
_OEM_INF = re.compile(r'oem[0-9]+\.inf\Z', re.I)
_INF = re.compile(r'[A-Za-z0-9_.-]+\.inf\Z', re.I)
_MAX_FILES = 50000
_MAX_BYTES = 32 * 1024 ** 3
_MAX_MANIFEST = 16 * 1024 ** 2


def _text(value, label, maximum=4096, *, empty=False):
    if (not isinstance(value, str) or len(value) > maximum or (not value and not empty)
            or any(ord(char) < 32 for char in value)):
        raise ValueError('Invalid ' + label + '.')
    return value


def _cancelled(event):
    return event is not None and event.is_set()


def _fingerprint(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()).hexdigest()


def _driver_identity(device):
    return [(driver['version'], driver['date'], driver['inf'].casefold(), driver['provider'].casefold())
            for driver in device['drivers']]


def _devices(report):
    if not isinstance(report, dict) or report.get('inventoryComplete') is not True or not isinstance(report.get('devices'), list):
        raise ValueError('A complete present-device and signed-driver inventory is required.')
    if len(report['devices']) > 20000:
        raise ValueError('The present-device inventory exceeds its bound.')
    result, identities = [], set()
    for source in report['devices']:
        if not isinstance(source, dict):
            raise ValueError('The device inventory contains an invalid record.')
        if source.get('present') is False:
            continue
        identity = _text(source.get('deviceId'), 'device instance ID')
        if identity.casefold() in identities:
            raise ValueError('A device instance identity is ambiguous.')
        identities.add(identity.casefold())
        drivers = []
        raw_drivers = source.get('installedDrivers')
        if not isinstance(raw_drivers, list) or len(raw_drivers) > 100:
            raise ValueError('The installed-driver collection is malformed.')
        for raw in raw_drivers:
            if not isinstance(raw, dict):
                raise ValueError('An installed-driver record is malformed.')
            item = {key: _text(raw.get(key) or '', 'installed driver ' + key, empty=True)
                    for key in ('version', 'date', 'inf', 'provider', 'deviceClass')}
            item['signed'] = raw.get('signed') if type(raw.get('signed')) is bool else None
            drivers.append(item)
        drivers.sort(key=lambda item: json.dumps(item, sort_keys=True))
        item = {'device_id': identity, 'name': str(source.get('displayName') or identity)[:4096],
                'present': source.get('present') if type(source.get('present')) is bool else None,
                'drivers': drivers}
        item['fingerprint'] = _fingerprint({key: item[key] for key in ('device_id', 'present', 'drivers')})
        item['rollback_eligible'] = (item['present'] is True and len(drivers) == 1
                                     and all(drivers[0][key] for key in ('version', 'date', 'inf', 'provider'))
                                     and bool(_INF.fullmatch(drivers[0]['inf'])))
        item['reason'] = ('Windows may have one previous driver for this exact device; availability is established only by the rollback API.'
                          if item['rollback_eligible'] else 'A present device and one complete installed-driver identity are required.')
        result.append(item)
    return result


def _hash_file(path, *, cancel_event=None, deadline=None):
    path = Path(path)
    before = _ordinary_data_path(path, file=True)
    if before is None or before.st_size > 8 * 1024 ** 3:
        raise ValueError('A backup file is missing or exceeds its bound.')
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        opened = os.fstat(stream.fileno())
        if (opened.st_dev, opened.st_ino, opened.st_nlink) != (before.st_dev, before.st_ino, 1):
            raise ValueError('A backup file changed identity while opening.')
        while True:
            if _cancelled(cancel_event) or deadline is not None and time.monotonic() > deadline:
                raise RuntimeError('Backup verification was cancelled or exceeded its time budget.')
            block = stream.read(1024 * 1024)
            if not block:
                break
            digest.update(block)
        after = os.fstat(stream.fileno())
    final = _ordinary_data_path(path, file=True)
    expected = (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
    if final is None or any((row.st_dev, row.st_ino, row.st_size, row.st_mtime_ns) != expected for row in (after, final)):
        raise ValueError('A backup file changed during verification.')
    return {'size': before.st_size, 'sha256': digest.hexdigest()}


class _ManifestStore:
    """Signed app-private evidence; no caller-supplied path becomes authority."""
    def __init__(self, data_dir, owner=None):
        self.data_dir = Path(data_dir)
        self.directory = self.data_dir / 'recovery' / 'driver-backups'
        self._owner = owner

    @property
    def owner(self):
        if self._owner is None:
            self._owner = _current_owner()
        return self._owner

    def _path(self, path, *, file=False):
        info = _ordinary_data_path(Path(path), file=file)
        if info is not None and os.name != 'nt' and info.st_uid != os.getuid():
            raise ValueError('Driver backup evidence has a different owner.')
        return info

    def read(self, path):
        info = self._path(path, file=True)
        if info is None or info.st_size > _MAX_MANIFEST:
            raise ValueError('Driver backup evidence is missing or oversized.')
        if os.name != 'nt' and info.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
            raise ValueError('Driver backup evidence is writable by another user.')
        with Path(path).open('rb') as stream:
            opened = os.fstat(stream.fileno())
            raw = stream.read(_MAX_MANIFEST + 1)
        after = self._path(path, file=True)
        token = (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns)
        if len(raw) > _MAX_MANIFEST or after is None or any((row.st_dev, row.st_ino, row.st_size, row.st_mtime_ns) != token for row in (opened, after)):
            raise ValueError('Driver backup evidence changed while reading.')
        def unique(rows):
            value = {}
            for key, item in rows:
                if key in value:
                    raise ValueError('Duplicate backup evidence fields are not supported.')
                value[key] = item
            return value
        return json.loads(raw, object_pairs_hook=unique, parse_constant=lambda _: (_ for _ in ()).throw(ValueError('Invalid backup number.')))

    def _key(self, create=False):
        path = self.directory / 'owner.json'
        self._path(self.directory)
        if self._path(path, file=True) is None:
            if not create:
                raise ValueError('No owned driver backup catalog is available.')
            self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
            self._path(self.directory)
            _private_json(path, {'schema': 1, 'owner': self.owner, 'key': os.urandom(32).hex()})
        value = self.read(path)
        if not isinstance(value, dict) or value.get('schema') != 1 or value.get('owner') != self.owner or not re.fullmatch(r'[a-f0-9]{64}', str(value.get('key'))):
            raise ValueError('The driver backup ownership anchor is invalid.')
        return bytes.fromhex(value['key'])

    @staticmethod
    def signature(value, key):
        body = {name: item for name, item in value.items() if name != 'signature'}
        return hmac.new(key, json.dumps(body, sort_keys=True, separators=(',', ':'), allow_nan=False).encode(), hashlib.sha256).hexdigest()

    def write(self, value):
        identity = value.get('id')
        if not isinstance(identity, str) or not _ID.fullmatch(identity):
            raise ValueError('Invalid driver recovery identity.')
        path = self.directory / identity / 'manifest.json'
        self._path(path.parent)
        key = self._key(create=True)
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self._path(path.parent)
        self._path(path, file=True)
        value['signature'] = self.signature(value, key)
        if len(json.dumps(value, allow_nan=False).encode()) > _MAX_MANIFEST:
            raise ValueError('Driver recovery evidence exceeds its byte bound.')
        _private_json(path, value)

    def load(self, identity):
        if not isinstance(identity, str) or not _ID.fullmatch(identity):
            raise ValueError('Select one owned backup catalog identity.')
        key = self._key()
        value = self.read(self.directory / identity / 'manifest.json')
        if (not isinstance(value, dict) or value.get('schema') != 1 or value.get('id') != identity or value.get('owner') != self.owner
                or value.get('kind') not in ('driver-export', 'driver-rollback')
                or not hmac.compare_digest(str(value.get('signature', '')), self.signature(value, key))):
            raise ValueError('Driver backup ownership, identity or signature is invalid.')
        return value


class WindowsDriverRecovery:
    """Lazy native adapter; tests inject inventory/export/rollback instead."""
    def __init__(self, backend):
        self.backend = backend

    def inventory(self):
        return self.backend.driver_devices()

    def export(self, destination):
        return self.backend.backup_drivers(destination)

    def installed_inf_hash(self, inf):
        if not _OEM_INF.fullmatch(inf):
            return None
        return _hash_file(Path(os.environ.get('SystemRoot', r'C:\Windows')) / 'INF' / inf)['sha256']

    @staticmethod
    def rollback(device_id):
        if sys.platform != 'win32':
            return {'supported': False, 'success': False, 'called': False, 'error': 'Windows driver rollback is unavailable on this platform.'}
        if ctypes.sizeof(ctypes.c_void_p) == 4 and os.environ.get('PROCESSOR_ARCHITEW6432'):
            return {'supported': False, 'success': False, 'called': False, 'error': 'A native 64-bit process is required for driver rollback.'}
        from ctypes import wintypes
        class GUID(ctypes.Structure):
            _fields_ = [('Data1', ctypes.c_uint32), ('Data2', ctypes.c_uint16), ('Data3', ctypes.c_uint16), ('Data4', ctypes.c_ubyte * 8)]
        class SP_DEVINFO_DATA(ctypes.Structure):
            _fields_ = [('cbSize', ctypes.c_uint32), ('ClassGuid', GUID), ('DevInst', ctypes.c_uint32), ('Reserved', ctypes.c_size_t)]
        try:
            native = Path(os.environ.get('SystemRoot', r'C:\Windows')) / 'System32'
            setup = ctypes.WinDLL(str(native / 'setupapi.dll'), use_last_error=True)
            newdev = ctypes.WinDLL(str(native / 'newdev.dll'), use_last_error=True)
            setup.SetupDiCreateDeviceInfoList.argtypes = [ctypes.c_void_p, wintypes.HWND]
            setup.SetupDiCreateDeviceInfoList.restype = ctypes.c_void_p
            setup.SetupDiOpenDeviceInfoW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR, wintypes.HWND, ctypes.c_uint32, ctypes.POINTER(SP_DEVINFO_DATA)]
            setup.SetupDiOpenDeviceInfoW.restype = wintypes.BOOL
            setup.SetupDiDestroyDeviceInfoList.argtypes = [ctypes.c_void_p]
            setup.SetupDiDestroyDeviceInfoList.restype = wintypes.BOOL
            newdev.DiRollbackDriver.argtypes = [ctypes.c_void_p, ctypes.POINTER(SP_DEVINFO_DATA), wintypes.HWND, ctypes.c_uint32, ctypes.POINTER(wintypes.BOOL)]
            newdev.DiRollbackDriver.restype = wintypes.BOOL
        except (AttributeError, OSError):
            return {'supported': False, 'success': False, 'called': False, 'error': 'The native Windows driver rollback API could not be loaded.'}
        handle = setup.SetupDiCreateDeviceInfoList(None, None)
        if handle in (None, ctypes.c_void_p(-1).value):
            return {'supported': True, 'success': False, 'called': False, 'winerror': ctypes.get_last_error()}
        try:
            record = SP_DEVINFO_DATA()
            record.cbSize = ctypes.sizeof(record)
            if not setup.SetupDiOpenDeviceInfoW(handle, _text(device_id, 'device instance ID'), None, 0, ctypes.byref(record)):
                return {'supported': True, 'success': False, 'called': False, 'winerror': ctypes.get_last_error()}
            need_reboot = wintypes.BOOL(False)
            # ROLLBACK_FLAG_NO_UI=1, from Microsoft's published newdev.h.
            # A non-null NeedReboot pointer lets us finish readback/journaling
            # without Windows initiating a reboot or displaying restart UI.
            success = bool(newdev.DiRollbackDriver(handle, ctypes.byref(record), None, 1, ctypes.byref(need_reboot)))
            error = 0 if success else ctypes.get_last_error()
            return {'supported': True, 'success': success, 'called': True, 'winerror': error,
                    'restartNeeded': bool(need_reboot.value), 'outcomeKnown': True, 'running': False}
        finally:
            setup.SetupDiDestroyDeviceInfoList(handle)


class DriverBackups:
    def __init__(self, backend, *, adapter=None, owner=None, lease_factory=RunLease, barrier_reader=None):
        self.backend = backend
        self.data_dir = Path(backend.data_dir)
        self.system = getattr(backend, 'system', None) or sys.platform
        self.adapter = adapter
        self.store = _ManifestStore(self.data_dir, owner)
        self.lease_factory = lease_factory
        self.barrier_reader = barrier_reader or pending_updates_barrier
        self._plans = {}
        self._lock = threading.Lock()

    def _adapter(self):
        if self.adapter is None:
            self.adapter = WindowsDriverRecovery(self.backend)
        return self.adapter

    def _preflight(self, *, rollback=False):
        if self.system != 'win32':
            return 'Windows driver recovery is unavailable on this platform.'
        if self.backend.is_admin() is not True:
            return 'Administrator access is required; no elevation or driver operation was started.'
        barrier = self.barrier_reader(self.backend)
        if barrier:
            return str(barrier)
        barrier = self.pending_barrier()
        if barrier:
            return barrier
        if rollback:
            security = self.backend.security_status()
            if not isinstance(security, dict) or security.get('reboot_required') is not False:
                return 'A pending restart is present or its state is unverified; driver rollback was deferred.'
            readiness = self.backend.update_disk_readiness()
            if not isinstance(readiness, dict) or readiness.get('ready') is not True:
                return 'Installation space is insufficient or unverified; driver rollback was deferred.'
        return None

    def pending_barrier(self):
        """Expose recovery uncertainty to the owning workflow's shared guard."""
        for summary in self.history():
            if summary.get('kind') != 'driver-rollback':
                continue
            value = self.store.load(summary['id'])
            if value.get('installer_started') is True and (value.get('outcomeKnown') is not True or value.get('uncertain') is True):
                return 'A prior driver rollback has an unknown outcome. Review its recovery journal and Device Manager before another mutation.'
            if value.get('restartNeeded') is True:
                current = self.backend.security_status()
                if not isinstance(current, dict) or current.get('reboot_required') is not False:
                    return 'A prior driver rollback requested a restart; the cleared restart state is not verified.'
        return None

    def scan(self):
        """Fresh device choices plus owned catalog summaries; never exports."""
        if self.system != 'win32':
            return {'status': 'unsupported', 'read_only': True, 'devices': [], 'backups': [], 'reason': 'Windows driver recovery is unavailable on this platform.'}
        devices = _devices(self._adapter().inventory())
        plan_id = uuid.uuid4().hex
        selections = {}
        for device in devices:
            selection = uuid.uuid4().hex
            device['selection_id'] = selection
            selections[selection] = copy.deepcopy(device)
        with self._lock:
            now = time.time()
            self._plans = {key: value for key, value in self._plans.items() if value['expires_at'] >= now}
            if len(self._plans) >= 10:
                self._plans.pop(next(iter(self._plans)))
            self._plans[plan_id] = {'expires_at': now + 600, 'devices': selections}
        return {'schema': 1, 'status': 'completed', 'read_only': True, 'plan_id': plan_id,
                'devices': devices, 'backups': self.history(),
                'note': 'Rollback uses the one previous driver retained by Windows, not an arbitrary exported package. Availability remains unknown until the API checks the selected device.'}

    def history(self):
        self.store._path(self.store.directory)
        if not self.store.directory.exists():
            return []
        directories = [path for path in self.store.directory.iterdir() if path.name != 'owner.json']
        if len(directories) > 1000:
            raise ValueError('Driver backup history exceeds its inventory bound.')
        items = []
        for directory in directories:
            if not _ID.fullmatch(directory.name):
                raise ValueError('The driver recovery catalog contains an unrecognized entry.')
            value = self.store.load(directory.name)
            items.append({key: copy.deepcopy(value.get(key)) for key in
                          ('id', 'kind', 'status', 'created_at', 'finished_at', 'summary', 'verification', 'restartNeeded')})
        return sorted(items, key=lambda item: item.get('created_at', 0), reverse=True)

    def _new(self, kind):
        resolve_data_root(self.data_dir)
        return {'schema': 1, 'id': uuid.uuid4().hex, 'owner': self.store.owner,
                'kind': kind, 'created_at': time.time(), 'status': 'prepared', 'items': [],
                'installer_started': False, 'outcomeKnown': True, 'running': False,
                'note': 'Exported third-party packages do not include every inbox Windows driver, device setting or a full system image.'}

    def _finish(self, value, status, summary):
        value.update(status=status, summary=summary, finished_at=time.time())
        self.store.write(value)
        return copy.deepcopy(value)

    def _export_locked(self, *, cancel_event=None, progress_callback=None):
        value = self._new('driver-export')
        if _cancelled(cancel_event):
            return self._finish(value, 'cancelled', 'Driver export was cancelled before it started.')
        devices = _devices(self._adapter().inventory())
        value['devices'] = devices
        value['provenance'] = {'platform': self.system, 'mechanism': 'Windows pnputil /export-driver *',
                               'captured_at': time.time(), 'inventory_complete': True}
        value['status'] = 'exporting'
        self.store.write(value)
        directory = self.store.directory / value['id'] / 'packages'
        self.store._path(directory)
        directory.mkdir(mode=0o700)
        self.store._path(directory)
        if progress_callback:
            progress_callback({'description': 'Exporting installed third-party driver packages.', 'percentage': 10})
        try:
            outcome = self._adapter().export(directory)
            if not isinstance(outcome, dict) or type(outcome.get('exit_code')) is not int or outcome['exit_code'] != 0:
                value['export_result'] = {key: outcome.get(key) for key in ('exit_code', 'output')} if isinstance(outcome, dict) else None
                return self._finish(value, 'failed', 'Windows did not report a successful driver-package export. Partial files are retained for review.')
            value['export_result'] = {'exit_code': outcome['exit_code'], 'output': str(outcome.get('output') or '')[:20000]}
            deadline = time.monotonic() + 180
            files, total = [], 0
            for current, folders, names in os.walk(directory, followlinks=False):
                self.store._path(Path(current))
                for folder in folders:
                    self.store._path(Path(current) / folder)
                for name in names:
                    path = Path(current) / name
                    item = {'path': path.relative_to(directory).as_posix(), **_hash_file(path, cancel_event=cancel_event, deadline=deadline)}
                    total += item['size']
                    if len(files) >= _MAX_FILES or total > _MAX_BYTES:
                        raise ValueError('The exported package collection exceeds its verification bound.')
                    files.append(item)
            files.sort(key=lambda item: item['path'])
            if not files:
                return self._finish(value, 'failed', 'Windows reported export success but no package files were available to verify.')
            packages = []
            infs = sorted({driver['inf'].casefold() for device in devices for driver in device['drivers'] if _OEM_INF.fullmatch(driver['inf'])})
            for inf in infs:
                digest = self._adapter().installed_inf_hash(inf)
                matches = [item['path'] for item in files if item['path'].casefold().endswith('.inf') and item['sha256'] == digest]
                packages.append({'published_inf': inf, 'installed_inf_sha256': digest,
                                 'exported_infs': matches, 'mapping_verified': len(matches) == 1,
                                 'device_ids': [device['device_id'] for device in devices if any(driver['inf'].casefold() == inf for driver in device['drivers'])]})
            after = _devices(self._adapter().inventory())
            before_state = {item['device_id'].casefold(): item['fingerprint'] for item in devices}
            after_state = {item['device_id'].casefold(): item['fingerprint'] for item in after}
            if before_state != after_state:
                raise ValueError('The device or installed-driver inventory changed during export; the original mapping remains unverified.')
            value.update(files=files, packages=packages,
                         verification={'file_count': len(files), 'total_bytes': total,
                                       'mapped_packages': sum(item['mapping_verified'] for item in packages),
                                       'unmapped_packages': sum(not item['mapping_verified'] for item in packages),
                                       'inventory_unchanged': True, 'verified_at': time.time()})
            if progress_callback:
                progress_callback({'description': 'Verified exported driver file hashes and installed INF mappings.', 'percentage': 100})
            return self._finish(value, 'completed', f'Verified {len(files)} exported files; {sum(item["mapping_verified"] for item in packages)} installed INF mappings. Inbox drivers and device settings are separate recovery scopes.')
        except Exception as error:
            value['error_type'] = type(error).__name__
            value['reason'] = str(error)[:2000]
            return self._finish(value, 'cancelled' if _cancelled(cancel_event) else 'failed', 'Driver export or verification did not complete; partial backup files remain available for review.')

    def export(self, *, confirmed=False, cancel_event=None, progress_callback=None):
        if confirmed is not True:
            raise ValueError('Confirm the driver-package export before creating it.')
        resolve_data_root(self.data_dir)
        with self.lease_factory(self.data_dir):
            reason = self._preflight()
            if reason:
                return {'status': 'blocked', 'reason': reason, 'installer_started': False, 'outcomeKnown': True, 'running': False}
            return self._export_locked(cancel_event=cancel_event, progress_callback=progress_callback)

    def verify(self, backup_id, *, cancel_event=None, progress_callback=None):
        value = self.store.load(backup_id)
        if value.get('kind') != 'driver-export' or value.get('status') != 'completed' or not isinstance(value.get('files'), list):
            raise ValueError('Choose one completed owned driver export to verify.')
        if not 1 <= len(value['files']) <= _MAX_FILES:
            raise ValueError('The driver export file inventory is invalid.')
        base = self.store.directory / backup_id / 'packages'
        checks, seen, total = [], set(), 0
        deadline = time.monotonic() + 180
        for item in value['files']:
            relative = item.get('path') if isinstance(item, dict) else None
            if (not isinstance(relative, str) or '\\' in relative or ':' in relative
                    or PurePosixPath(relative).is_absolute() or any(part in ('', '.', '..') for part in relative.split('/'))
                    or relative.casefold() in seen):
                raise ValueError('The backup contains an invalid or duplicate relative file path.')
            seen.add(relative.casefold())
            actual = _hash_file(base.joinpath(*relative.split('/')), cancel_event=cancel_event, deadline=deadline)
            total += actual['size']
            if total > _MAX_BYTES:
                raise ValueError('The driver export exceeds its verification byte bound.')
            checks.append({'path': relative, 'verified': actual == {key: item.get(key) for key in ('size', 'sha256')}})
        actual_paths = set()
        for current, folders, names in os.walk(base, followlinks=False):
            self.store._path(Path(current))
            for folder in folders:
                self.store._path(Path(current) / folder)
            for name in names:
                path = Path(current) / name
                self.store._path(path, file=True)
                actual_paths.add(path.relative_to(base).as_posix().casefold())
                if len(actual_paths) > _MAX_FILES:
                    raise ValueError('The driver export exceeds its file-inventory bound.')
        unexpected = sorted(actual_paths - seen)
        good = all(item['verified'] for item in checks)
        good = good and not unexpected
        if progress_callback:
            progress_callback({'description': 'Driver backup file verification completed.', 'percentage': 100})
        return {'schema': 1, 'id': backup_id, 'status': 'completed' if good else 'needs_review',
                'read_only': True, 'verified': good, 'items': checks,
                'unexpected_files': unexpected,
                'summary': f'{sum(item["verified"] for item in checks)} / {len(checks)} exported files match their recorded hashes. This does not establish hardware compatibility or a restore point.'}

    def rollback(self, plan_id, selection_id, *, confirmed=False, cancel_event=None, progress_callback=None):
        if confirmed is not True:
            raise ValueError('Confirm rollback of the exact selected device before changing drivers.')
        if not isinstance(plan_id, str) or not _ID.fullmatch(plan_id) or not isinstance(selection_id, str) or not _ID.fullmatch(selection_id):
            raise ValueError('Choose one exact device from a current recovery scan.')
        with self._lock:
            plan = self._plans.get(plan_id)
            if not plan or plan['expires_at'] < time.time() or selection_id not in plan['devices']:
                raise ValueError('The device selection expired or belongs to another session. Refresh first.')
            selected = copy.deepcopy(plan['devices'][selection_id])
            if not selected.get('rollback_eligible'):
                raise ValueError('The selected device lacks a complete rollback identity.')
        resolve_data_root(self.data_dir)
        with self.lease_factory(self.data_dir):
            reason = self._preflight(rollback=True)
            if reason:
                return {'status': 'blocked', 'reason': reason, 'installer_started': False, 'outcomeKnown': True, 'running': False}
            fresh = [item for item in _devices(self._adapter().inventory()) if item['device_id'].casefold() == selected['device_id'].casefold()]
            if len(fresh) != 1 or fresh[0]['fingerprint'] != selected['fingerprint']:
                raise ValueError('The selected device or installed driver changed. Refresh and approve the current identity.')
            with self._lock:
                del plan['devices'][selection_id]  # One reviewed attempt; failures do not authorize silent retries.
            value = self._new('driver-rollback')
            value['before'] = fresh[0]
            value['status'] = 'backing_up'
            self.store.write(value)
            backup = self._export_locked(cancel_event=cancel_event, progress_callback=progress_callback)
            value['preflight_backup_id'] = backup['id']
            if backup['status'] != 'completed':
                return self._finish(value, 'blocked', 'A verified fresh driver-package export is required before rollback. No rollback was started.')
            for driver in fresh[0]['drivers']:
                if _OEM_INF.fullmatch(driver['inf']) and not any(item['published_inf'].casefold() == driver['inf'].casefold() and item['mapping_verified'] for item in backup.get('packages', [])):
                    return self._finish(value, 'blocked', 'The selected installed third-party INF could not be mapped to its verified export. No rollback was started.')
            if _cancelled(cancel_event):
                return self._finish(value, 'cancelled', 'Cancelled after backup and before driver rollback.')
            reason = self._preflight(rollback=True)
            latest = [item for item in _devices(self._adapter().inventory()) if item['device_id'].casefold() == selected['device_id'].casefold()]
            if reason or len(latest) != 1 or latest[0]['fingerprint'] != selected['fingerprint']:
                return self._finish(value, 'blocked', reason or 'The selected driver changed during backup; rollback was not started.')
            value.update(status='prepared', installer_started=True, outcomeKnown=False)
            self.store.write(value)  # Intent is durable before calling the native rollback API.
            if progress_callback:
                progress_callback({'description': 'Rolling back the selected device with the native Windows API.', 'percentage': 80})
            native_returned = False
            try:
                outcome = self._adapter().rollback(selected['device_id'])
                if (not isinstance(outcome, dict) or type(outcome.get('success')) is not bool
                        or type(outcome.get('called')) is not bool or type(outcome.get('supported')) is not bool):
                    raise ValueError('The native rollback API returned an invalid outcome.')
                if (outcome['success'] and (not outcome['called'] or not outcome['supported'])
                        or outcome['called'] and not outcome['supported']
                        or outcome['supported'] and (type(outcome.get('winerror')) is not int or not 0 <= outcome['winerror'] <= 0xffffffff)
                        or outcome['success'] and outcome.get('winerror') != 0):
                    raise ValueError('The native rollback API returned contradictory success or error metadata.')
                if outcome['called'] and (outcome.get('outcomeKnown') is not True or outcome.get('running') is not False
                                          or type(outcome.get('restartNeeded')) is not bool):
                    raise ValueError('The native rollback API returned an unknown lifecycle.')
                value.update(native_result=copy.deepcopy(outcome), installer_started=outcome['called'], outcomeKnown=True, running=False,
                             restartNeeded=outcome.get('restartNeeded', False))
                native_returned = True
                if not outcome['supported']:
                    return self._finish(value, 'unsupported', str(outcome.get('error') or 'Windows driver rollback is unsupported.'))
                after = [item for item in _devices(self._adapter().inventory()) if item['device_id'].casefold() == selected['device_id'].casefold()]
                value['after'] = after[0] if len(after) == 1 else None
                value['verified'] = (outcome['success'] and not value['restartNeeded'] and len(after) == 1
                                     and after[0]['rollback_eligible'] and _driver_identity(after[0]) != _driver_identity(selected))
                if not outcome['success']:
                    no_previous = outcome.get('winerror') == 259
                    return self._finish(value, 'unavailable' if no_previous else 'failed', 'Windows has no previous driver for this device.' if no_previous else 'Windows declined or failed the selected driver rollback. Review the native error and current device state.')
                if value['restartNeeded']:
                    return self._finish(value, 'restart_required', 'Windows reported driver rollback but requires a restart. Optimus did not restart the computer; final driver state remains unverified.')
                return self._finish(value, 'completed' if value['verified'] else 'needs_review',
                                    'The selected device now reports a different installed driver after Windows rollback.' if value['verified'] else
                                    'Windows reported rollback but the selected installed-driver change could not be independently verified. No restored-state claim was made.')
            except Exception as error:
                value.update(error_type=type(error).__name__, reason=str(error)[:2000])
                if native_returned:
                    value['verified'] = False
                    return self._finish(value, 'needs_review', 'The Windows rollback API returned, but the current device readback could not be verified. Review the native result and retained preflight backup.')
                value.update(outcomeKnown=False, uncertain=True)
                return self._finish(value, 'uncertain', 'Driver rollback did not return a verified outcome. Review Device Manager and the retained preflight backup before another attempt.')
