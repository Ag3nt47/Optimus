"""Exact-offer app activation evidence; never closes an app or authorizes work.

WinGet can exit successfully while an MSIX registration waits for an application
to close. These records prevent identical downloads until that prerequisite
changes. Every result still needs a fresh installed-version query.
"""
from __future__ import annotations

import ctypes
import hashlib
import json
import math
import os
from pathlib import Path
import re
import time
import uuid

import psutil

from .filesystem import _ordinary_data_path
from .speed_enhancements import _current_owner, _private_json
from .update_resume import boot_identity

_ID = re.compile(r'[0-9a-f]{32}\Z')
_PACKAGE = re.compile(r'([A-Za-z0-9][A-Za-z0-9.\-]{2,100})_([0-9]+(?:\.[0-9]+){3})_(?:x64|x86|arm64|neutral)_([^_\s]*)_([a-z0-9]{13})\Z')
_MAX_LOG = 4 * 1024**2


def target_installed(expected, actual, identity):
    from .automated_software import same_release_version, is_newer_version
    return (isinstance(actual, str) and (actual == expected or same_release_version(expected, actual)
        or is_newer_version(expected, actual, package_id=identity, source='winget')))


def _read(path, maximum):
    before = _ordinary_data_path(path, file=True)
    if before is None or before.st_size > maximum:
        raise ValueError('Bounded ordinary update evidence required.')
    with path.open('rb') as stream:
        opened = os.fstat(stream.fileno())
        raw = stream.read(maximum + 1)
    after = _ordinary_data_path(path, file=True)
    fields = lambda row: (row.st_dev, row.st_ino, row.st_size, row.st_mtime_ns)
    if (after is None or opened.st_nlink != 1 or len(raw) > maximum
            or fields(before) != fields(opened) or fields(before) != fields(after)):
        raise ValueError('Update evidence changed during inspection.')
    return raw


def diagnostic_blockers(text):
    """Return package identities, never arbitrary instructions, from our log."""
    result = set()
    for line in text.splitlines():
        if '0x80073d02' not in line.casefold():
            continue
        for value in line.split():
            match = _PACKAGE.fullmatch(value.rstrip(',.'))
            if match:
                result.add(match[1] + '_' + match[4])
    return sorted(result)[:20]


def _process_family(pid):
    """Read a process package identity with QUERY_LIMITED_INFORMATION only.

    https://learn.microsoft.com/windows/win32/api/appmodel/nf-appmodel-getpackagefamilyname
    """
    if os.name != 'nt':
        return None
    from ctypes import wintypes
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.GetPackageFamilyName.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.UINT), wintypes.LPWSTR]
    kernel.GetPackageFamilyName.restype = wintypes.LONG
    handle = kernel.OpenProcess(0x1000, False, pid)
    if not handle:
        return None
    try:
        length = wintypes.UINT(0)
        if kernel.GetPackageFamilyName(handle, ctypes.byref(length), None) != 122 or not 1 < length.value <= 256:
            return None
        buffer = ctypes.create_unicode_buffer(length.value)
        return buffer.value if kernel.GetPackageFamilyName(handle, ctypes.byref(length), buffer) == 0 else None
    finally:
        kernel.CloseHandle(handle)


def blocker_processes(families):
    """Only positively identified exact package processes become blockers."""
    wanted, result = set(families), []
    if not wanted:
        return result
    for process in psutil.process_iter(['pid', 'name', 'create_time'], ad_value=None):
        try:
            identity = _process_family(process.pid)
            created = process.info.get('create_time')
            if (identity in wanted and type(created) in (int, float) and math.isfinite(created) and created > 0
                    and psutil.Process(process.pid).create_time() == created):
                result.append({'pid': process.pid, 'created_at': created, 'package_family': identity,
                               'name': str(process.info.get('name') or '')[:120]})
        except (OSError, psutil.Error):
            continue
    return result[:256]


class AppUpdateVerification:
    def __init__(self, data_dir, package, *, owner=None, boot=None, now=None, directory_policy=None):
        self.data_dir, self.package = Path(data_dir), dict(package)
        self.base = self.data_dir / 'app-update-verification'
        self.scope = {'id': self.package['id'].casefold(), 'source': 'winget',
                      'from_version': self.package['version'], 'to_version': self.package['available']}
        scope = json.dumps(self.scope, sort_keys=True, separators=(',', ':')).encode('utf-8')
        self.directory = self.base / hashlib.sha256(scope).hexdigest()
        self.owner = owner or _current_owner()
        self.boot = boot_identity if boot is None else boot
        self.now = time.time if now is None else now
        if directory_policy is None:
            from .self_update_permissions import create_private_directory
            directory_policy = create_private_directory
        self.directory_policy = directory_policy
        self.folder = None

    def _owner_anchor(self, directory=None):
        directory = self.directory if directory is None else directory
        anchor = json.loads(_read(directory / 'owner.json', 4096).decode('utf-8'))
        expected = {'schema': 1, 'kind': 'app-update-verification', 'owner': self.owner}
        if directory == self.directory:
            expected['offer'] = self.scope
        if anchor != expected:
            raise ValueError('The app activation evidence belongs to another owner.')

    def pending(self):
        """A record can only restrict retry; it never establishes installation."""
        _ordinary_data_path(self.base)
        if not self.base.exists():
            return None
        self._owner_anchor(self.base)
        _ordinary_data_path(self.directory)
        if not self.directory.exists():
            return None
        self._owner_anchor()
        entries = list(self.directory.iterdir())
        if len(entries) > 4096:
            raise ValueError('App verification history exceeds its bound.')
        candidates = []
        for folder in entries:
            if not _ID.fullmatch(folder.name):
                continue
            _ordinary_data_path(folder)
            path = folder / 'verification.json'
            if not path.exists():
                continue
            row = json.loads(_read(path, 65536).decode('utf-8'))
            if (isinstance(row, dict) and row.get('schema') == 1 and row.get('owner') == self.owner
                    and row.get('id') == self.package.get('id') and row.get('from_version') == self.package.get('version')
                    and row.get('to_version') == self.package.get('available') and row.get('status') in ('pending_activation', 'pending_verification')
                    and type(row.get('recorded_at')) in (int, float) and math.isfinite(row['recorded_at'])):
                candidates.append(row)
        if not candidates:
            return None
        previous = max(candidates, key=lambda row: row['recorded_at'])
        boot = previous.get('boot_identity')
        if type(boot) is int and self.boot() > boot + 10:
            return None  # A changed OS boot permits one freshly validated attempt.
        processes = previous.get('blocking_processes')
        if isinstance(processes, list) and processes:
            active = False
            for row in processes[:256]:
                if not isinstance(row, dict) or type(row.get('pid')) is not int or type(row.get('created_at')) not in (int, float):
                    active = True
                    break
                try:
                    process = psutil.Process(row['pid'])
                    active = active or process.create_time() == row['created_at']
                except psutil.NoSuchProcess:
                    pass
                except (OSError, psutil.Error):
                    active = True
            if not active:
                return None  # Exact blockers exited; revalidation, not this record, authorizes retry.
        return previous

    def prepare(self):
        for directory in (self.base, self.directory):
            _ordinary_data_path(directory)
            try:
                self.directory_policy(directory, self.owner)
            except FileExistsError:
                _ordinary_data_path(directory)
                self._owner_anchor(directory)
            else:
                anchor = {'schema': 1, 'kind': 'app-update-verification', 'owner': self.owner}
                if directory == self.directory:
                    anchor['offer'] = self.scope
                _private_json(directory / 'owner.json', anchor)
        self.folder = self.directory / uuid.uuid4().hex
        self.directory_policy(self.folder, self.owner)
        return self.folder / 'winget.log'

    def finish(self, receipt):
        if self.folder is None:
            return
        text = ''
        try:
            text = _read(self.folder / 'winget.log', _MAX_LOG).decode('utf-8-sig')
        except (OSError, ValueError, UnicodeError):
            pass
        families = diagnostic_blockers(text)
        pending = (receipt.get('verified') is False and receipt.get('installation_reported') is True
            and receipt.get('outcomeKnown') is True and receipt.get('running') is False)
        restart_app = receipt.get('application_restart_required') is True or bool(families)
        pending_status = 'pending_activation' if restart_app else 'pending_verification'
        row = {'schema': 1, 'owner': self.owner, 'id': self.package['id'],
            'from_version': self.package['version'], 'to_version': self.package['available'],
            'installed_version': receipt.get('installed_version'), 'recorded_at': self.now(),
            'status': pending_status if pending else 'verified' if receipt.get('verified') is True else 'not_verified',
            'application_restart_required': restart_app,
            'boot_identity': self.boot(), 'blocking_package_families': families,
            'blocking_processes': blocker_processes(families)}
        _private_json(self.folder / 'verification.json', row)
        receipt['verification_attempt'] = self.folder.name
        if pending:
            receipt.update(verification_status=pending_status,
                blocking_package_families=families, blocking_processes=row['blocking_processes'])
            if families:
                receipt['application_restart_required'] = True
            detail = (' Close ' + ', '.join(families) + ' after saving work.' if families else
                      ' Recheck after the application finishes its own updater or Windows restarts.')
            receipt['note'] += ' The installed version is pending verification.' + detail + ' Optimus will recheck automatically; this package is not counted as installed.'


def deferred_receipt(package, pending, installed_version):
    families = pending.get('blocking_package_families', [])
    detail = ', '.join(value for value in families if isinstance(value, str))[:500] or package['id']
    restart_app = pending.get('application_restart_required') is True
    return {'id': package['id'], 'package_id': package['id'], 'source': 'winget',
        'status': 'deferred', 'skipped': True, 'exitCode': None,
        'installer_started': False, 'outcomeKnown': True, 'running': False,
        'requiresReview': True, 'failure_scope': 'package', 'failureKind': 'app_activation_pending',
        'verified': False, 'installed_version': installed_version,
        'from_version': package['version'], 'to_version': package['available'],
        'application_restart_required': restart_app, 'verification_status': pending.get('status', 'pending_verification'),
        'blocking_package_families': families,
        'note': ('The earlier download is waiting for application activation: ' if restart_app else
                 'The earlier installer returned without a confirmed version change: ') + detail +
            '. The installed version is still ' + str(installed_version or 'unverified') +
            '. This unchanged offer was not downloaded again. Other approved applications continue; retry is revalidated after its exact blocking process exits or Windows restarts.'}
