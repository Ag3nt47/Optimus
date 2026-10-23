"""Targeted recovery evidence for exact offered Windows driver replacements.

Only an installed OEM INF matched through a present device's exact hardware or
compatible ID is exported. Inbox drivers have no invented export fallback.
PnPUtil /export-driver accepts one published OEM INF on Windows 10 1607+:
https://learn.microsoft.com/windows-hardware/drivers/devtest/pnputil-command-syntax#export-driver
No driver install/remove/device/reboot command is issued here.
"""
from __future__ import annotations

import copy
import ctypes
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
import time
import uuid

from .automated_system import driver_descriptor
from .driver_backups import _devices, _hash_file
from .filesystem import _ordinary_data_path, resolve_data_root
from .speed_enhancements import _current_owner, _private_json

_OEM = re.compile(r'oem[0-9]+\.inf\Z', re.I)
_SHA = re.compile(r'[a-f0-9]{64}\Z')


def _cancelled(event):
    return event is not None and event.is_set()


def _mapping(inventory, offers):
    normalized = {row['device_id'].casefold(): row for row in _devices(inventory)}
    candidates = {}
    for raw in inventory['devices']:
        if raw.get('present') is not True:
            continue
        device = normalized.get(str(raw.get('deviceId', '')).casefold())
        if device is None:
            continue
        values = []
        for name in ('hardwareIds', 'compatibleIds'):
            ids = raw.get(name)
            if not isinstance(ids, list) or len(ids) > 512 or any(not isinstance(value, str) or not value or len(value) > 4096 for value in ids):
                raise ValueError('Complete exact hardware/compatible identifiers are required for targeted driver backup.')
            values.extend(ids)
        for identity in set(value.casefold() for value in values):
            candidates.setdefault(identity, []).append(device)
    result = {}
    for offer in offers:
        key = offer['id'], offer['revision']
        matches = candidates.get(str(offer.get('hardwareId', '')).casefold(), [])
        if not matches:
            result[key] = {'reason': 'No exact present-device hardware/compatible ID match was available.'}
            continue
        if any(not device['drivers'] for device in matches):
            result[key] = {'reason': 'A matching present device has no complete installed-driver package identity.'}
            continue
        infs = sorted({driver['inf'].casefold() for device in matches for driver in device['drivers']})
        if not infs or any(not _OEM.fullmatch(inf) for inf in infs):
            result[key] = {'reason': 'A matched installed package is an inbox or unsupported non-OEM INF; no export backup is claimed.'}
            continue
        if any(not all(driver[name] for name in ('version', 'date', 'provider', 'inf')) for device in matches for driver in device['drivers']):
            result[key] = {'reason': 'Installed OEM driver identity is incomplete; targeted backup cannot be verified.'}
            continue
        result[key] = {'packages': infs, 'devices': sorted(
            ({'id': device['device_id'], 'fingerprint': device['fingerprint']} for device in matches), key=lambda row: row['id'].casefold())}
    return result


class WindowsTargetedExport:
    """Scoped cancellable read-only export subprocess, never an installer."""
    def __init__(self, backend):
        self.backend = backend

    def inventory(self):
        return self.backend.driver_devices()

    def installed_inf_hash(self, inf):
        if not _OEM.fullmatch(inf):
            raise ValueError('Only an exact published OEM INF can be inspected.')
        return _hash_file(Path(os.environ.get('SystemRoot', r'C:\Windows')) / 'INF' / inf)['sha256']

    @staticmethod
    def _stop_export(process):
        # This handle belongs only to the read-only PnPUtil export created below.
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)

    def export(self, inf, destination, *, cancel_event=None, timeout=180, progress_callback=None):
        if sys.platform != 'win32' or not _OEM.fullmatch(inf):
            raise ValueError('Targeted PnPUtil exports require Windows and an exact OEM INF.')
        if _cancelled(cancel_event):
            return {'exit_code': 1, 'reason': 'cancelled', 'cancelled': True, 'timed_out': False, 'output': ''}
        destination = Path(destination)
        _ordinary_data_path(destination)
        buffer = ctypes.create_unicode_buffer(32768)
        length = ctypes.windll.kernel32.GetSystemDirectoryW(buffer, len(buffer))
        if not 0 < length < len(buffer):
            raise OSError('The native Windows system directory is unavailable.')
        executable = Path(buffer.value) / 'pnputil.exe'
        _ordinary_data_path(executable.parent)
        info = executable.lstat()
        # Windows itself hardlinks System32 binaries to WinSxS. Reject indirect
        # paths here; the single-link requirement applies to backup evidence.
        if not stat.S_ISREG(info.st_mode) or executable.is_symlink() or getattr(info, 'st_file_attributes', 0) & 0x400:
            raise ValueError('The native PnPUtil path is not an ordinary Windows executable.')
        output_path = destination.parent / (destination.name + '-export.log')
        _ordinary_data_path(output_path, file=True)
        started = last_progress = time.monotonic()
        reason = ''
        # File output avoids a full stdout pipe stopping this native process.
        with output_path.open('xb') as output:
            process = subprocess.Popen([str(executable), '/export-driver', inf, str(destination)],
                stdout=output, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0), shell=False)
            try:
                while process.poll() is None:
                    if _cancelled(cancel_event) or time.monotonic() - started >= timeout:
                        reason = 'cancelled' if _cancelled(cancel_event) else 'timed_out'
                        break
                    if progress_callback and time.monotonic() - last_progress >= 1:
                        progress_callback({'description': f'Exporting current OEM driver {inf}; elapsed {int(time.monotonic() - started)}s', 'phase': 'backup'})
                        last_progress = time.monotonic()
                    time.sleep(0.1)
            finally:
                # Errors in a progress callback also drain our own export.
                # No installer, PowerShell bridge or Windows service is killed.
                if process.poll() is None:
                    self._stop_export(process)
            code = process.wait()
            output.flush()
            os.fsync(output.fileno())
        _ordinary_data_path(output_path, file=True)
        with output_path.open('rb') as stream:
            output = stream.read(32768).decode('utf-8', errors='replace')
        return {'exit_code': code, 'reason': reason, 'output': output, 'cancelled': reason == 'cancelled',
                'timed_out': reason == 'timed_out', 'elapsed_seconds': time.monotonic() - started}


class DriverUpdateBackup:
    def __init__(self, backend, *, adapter=None, owner=None, system=None, package_timeout=180, total_timeout=600):
        self.backend, self.adapter, self.owner = backend, adapter, owner
        self.system = system or sys.platform
        self.directory = Path(backend.data_dir) / 'recovery' / 'driver-update-backups'
        self.package_timeout, self.total_timeout = package_timeout, total_timeout

    def _adapter(self):
        if self.adapter is None:
            self.adapter = WindowsTargetedExport(self.backend)
        return self.adapter

    def _save(self, path, receipt):
        _ordinary_data_path(path, file=True)
        _private_json(path, receipt)
        _ordinary_data_path(path, file=True)
        if json.loads(path.read_text(encoding='utf-8')) != receipt:
            raise OSError('The targeted driver backup receipt could not be verified after saving.')

    def _files(self, directory, cancel_event, deadline, progress_callback=None):
        files, total = [], 0
        last_progress = time.monotonic()
        for current, folders, names in os.walk(directory, followlinks=False):
            _ordinary_data_path(Path(current))
            for folder in folders:
                _ordinary_data_path(Path(current) / folder)
            for name in names:
                path = Path(current) / name
                item = dict(path=path.relative_to(directory).as_posix(), **_hash_file(path, cancel_event=cancel_event, deadline=deadline))
                total += item['size']
                if len(files) >= 10000 or total > 16 * 1024 ** 3:
                    raise ValueError('The targeted export exceeded its file or byte verification bound.')
                files.append(item)
                if progress_callback and time.monotonic() - last_progress >= 1:
                    progress_callback({'description': f'Verifying {directory.name}: {len(files)} files, {total} bytes', 'phase': 'backup',
                        'verified_files': len(files), 'verified_bytes': total})
                    last_progress = time.monotonic()
        if not files:
            raise ValueError('Windows reported export success but there are no verified package files.')
        return sorted(files, key=lambda row: row['path'])

    def backup(self, offers, *, cancel_event=None, item_callback=None):
        if type(offers) is not list or not 0 < len(offers) <= 200:
            raise ValueError('Choose 1 to 200 exact driver update offers for targeted backup.')
        rows = [driver_descriptor(row) for row in offers]
        if len({(row['id'], row['revision']) for row in rows}) != len(rows):
            raise ValueError('Driver backup offers must have unique exact identities and revisions.')
        result = {'exit_code': 1, 'destination': None, 'backed_updates': [], 'blocked_updates': [], 'packages': []}
        def blocked_all(reason):
            result['blocked_updates'] = [dict(id=row['id'], revision=row['revision'], reason=reason) for row in rows]
            result['reason'] = reason
            return result
        if self.system != 'win32':
            return blocked_all('Targeted driver-package backups are supported only on Windows.')
        if _cancelled(cancel_event):
            return blocked_all('Driver backup was cancelled before any export started.')
        if self.backend.is_admin() is not True:
            return blocked_all('Administrator access is required to export targeted OEM driver packages.')
        # Resolve the configured owner data root, never a caller-provided export target.
        data_dir = resolve_data_root(Path(self.backend.data_dir))
        if data_dir != self.directory.parents[1]:
            raise ValueError('The targeted driver backup data root changed.')
        _ordinary_data_path(self.directory)
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        folder = self.directory / uuid.uuid4().hex
        folder.mkdir(mode=0o700)
        _ordinary_data_path(folder)
        path = folder / 'receipt.json'
        result['destination'] = str(folder)
        started, deadline = time.time(), time.monotonic() + self.total_timeout
        receipt = {'schema': 1, 'id': folder.name, 'kind': 'targeted-driver-update-backup',
            'owner': self.owner or _current_owner(), 'status': 'prepared', 'created_at': started,
            'offers': rows, 'packages': [], 'backed_updates': [], 'blocked_updates': []}
        self._save(path, receipt)  # Durable intent before any native export.
        try:
            mapping = _mapping(self._adapter().inventory(), rows)
            receipt['mapping'] = [{'id': row['id'], 'revision': row['revision'], **mapping[(row['id'], row['revision'])]} for row in rows]
            receipt['status'] = 'exporting'
            self._save(path, receipt)
            needed = sorted({inf for item in mapping.values() for inf in item.get('packages', [])})
            packages = {}
            for index, inf in enumerate(needed):
                package = {'inf': inf, 'verified': False}
                try:
                    if _cancelled(cancel_event) or time.monotonic() >= deadline:
                        raise RuntimeError('Targeted driver backup was cancelled or exceeded its total time budget.')
                    if item_callback:
                        item_callback({'description': f'Backing up current driver package {index + 1}/{len(needed)}: {inf}',
                            'current': index, 'total': len(needed), 'phase': 'backup'})
                    digest = self._adapter().installed_inf_hash(inf)
                    if not isinstance(digest, str) or not _SHA.fullmatch(digest):
                        raise ValueError('The installed OEM INF hash is unavailable.')
                    destination = folder / inf
                    destination.mkdir(mode=0o700)
                    outcome = self._adapter().export(inf, destination, cancel_event=cancel_event,
                        timeout=min(self.package_timeout, max(0.1, deadline - time.monotonic())), progress_callback=item_callback)
                    package['export_result'] = copy.deepcopy(outcome)
                    if (not isinstance(outcome, dict) or type(outcome.get('exit_code')) is not int
                            or outcome['exit_code'] != 0 or outcome.get('cancelled') or outcome.get('timed_out')):
                        raise ValueError('The targeted OEM export did not complete successfully: ' + str(outcome.get('reason') or outcome.get('exit_code')) if isinstance(outcome, dict) else 'The targeted export returned no native outcome.')
                    files = self._files(destination, cancel_event, deadline, item_callback)
                    inf_matches = [item['path'] for item in files if item['path'].casefold().endswith('.inf') and item['sha256'] == digest]
                    if len(inf_matches) != 1 or self._adapter().installed_inf_hash(inf) != digest:
                        raise ValueError('The exported INF does not map uniquely to the unchanged installed OEM INF hash.')
                    package.update(verified=True, installed_inf_sha256=digest, exported_inf=inf_matches[0], files=files)
                except Exception as error:
                    package['reason'] = str(error)[:2000]
                packages[inf] = package
                receipt['packages'] = list(packages.values())
                self._save(path, receipt)
            # Recheck only relevant device mappings, not unrelated hardware changes.
            stop_reason = ('Driver backup was cancelled.' if _cancelled(cancel_event) else
                'Targeted driver backup exceeded its total time budget.' if time.monotonic() >= deadline else None)
            fresh = _mapping(self._adapter().inventory(), rows) if not stop_reason else None
            # Several offers/devices can share one published OEM package. Do
            # the final hash pass once per package in this call, then apply its
            # result to each exact offer. Nothing survives as a cross-run cache.
            final_packages = {}
            for inf, package in packages.items():
                reason = stop_reason
                if not package['verified']:
                    reason = reason or package.get('reason') or 'A required OEM package was not verified.'
                elif not reason:
                    try:
                        if _cancelled(cancel_event) or time.monotonic() >= deadline:
                            raise RuntimeError('Driver backup was cancelled or exceeded its total time budget.')
                        if (self._adapter().installed_inf_hash(inf) != package['installed_inf_sha256']
                                or self._files(folder / inf, cancel_event, deadline, item_callback) != package['files']):
                            reason = 'A required current OEM package or exported file changed after verification.'
                    except Exception as error:
                        reason = 'A required current OEM package or exported file could not be reverified: ' + str(error)[:2000]
                package['final_verified'] = reason is None
                if reason:
                    package['final_reason'] = reason
                final_packages[inf] = reason
            for row in rows:
                key = row['id'], row['revision']
                original = mapping[key]
                reason = original.get('reason') or stop_reason
                if not reason and fresh[key] != original:
                    reason = 'The matched present device or installed-driver identity changed during backup.'
                for inf in original.get('packages', []):
                    reason = reason or final_packages[inf]
                item = {'id': row['id'], 'revision': row['revision']}
                if reason or _cancelled(cancel_event):
                    result['blocked_updates'].append(dict(item, reason=reason or 'Driver backup was cancelled.'))
                else:
                    result['backed_updates'].append(item)
            result['packages'] = list(packages.values())
            result['exit_code'] = 0 if result['backed_updates'] else 1
            receipt.update(status='completed' if not result['blocked_updates'] else 'partial' if result['backed_updates'] else 'blocked',
                completed_at=time.time(), elapsed_seconds=time.time() - started,
                backed_updates=result['backed_updates'], blocked_updates=result['blocked_updates'])
            self._save(path, receipt)
            return dict(result, receipt=str(path), status=receipt['status'], elapsed_seconds=receipt['elapsed_seconds'])
        except Exception as error:
            result = blocked_all('Targeted driver backup could not be verified: ' + str(error)[:2000])
            receipt.update(status='cancelled' if _cancelled(cancel_event) else 'failed', completed_at=time.time(),
                backed_updates=[], blocked_updates=result['blocked_updates'], reason=result['reason'])
            self._save(path, receipt)
            return dict(result, receipt=str(path), status=receipt['status'])
