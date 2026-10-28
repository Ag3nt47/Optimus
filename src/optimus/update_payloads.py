"""Live, per-update authority for disposable installer payloads only.

No manifest or receipt can recreate deletion authority. Failed, unverified,
running and restart-pending updates retain their complete private workspace.
"""
from __future__ import annotations

from contextlib import AbstractContextManager
import copy
import ctypes
import json
import os
from pathlib import Path
import re
import stat
import threading
import time
import uuid

from .cleaner import Cleaner, REPARSE, OFFLINE, RECALL, SYSTEM, READONLY
from .filesystem import _local_data_path, _ordinary_data_path

_SUFFIXES = frozenset(('.exe', '.msi', '.msp', '.msix', '.msixbundle', '.appx', '.appxbundle',
    '.cab', '.zip', '.7z', '.dmg', '.pkg', '.deb', '.rpm', '.tar', '.gz', '.bz2', '.xz', '.tgz', '.zst'))
_PROTECTED = re.compile(r'backup|rollback|receipt|recovery|uninstall|(^|[^a-z])(logs?|audit|evidence|diagnostics)([^a-z]|$)', re.I)
_MAX_ENTRIES, _MAX_BYTES, _MAX_SECONDS, _MAX_DEPTH = 4096, 4 * 1024 ** 3, 5.0, 12


def _identity(info):
    return info.st_dev, info.st_ino


def _ordinary(info, *, directory=False):
    return (stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode) and info.st_nlink == 1) and not (
        getattr(info, 'st_file_attributes', 0) & (REPARSE | OFFLINE | RECALL | SYSTEM | READONLY))


def _windows_directory(path, info, removable=False):
    """Pin ordinary directory identity and deny ancestor renames until closed."""
    from ctypes import wintypes
    class Information(ctypes.Structure):
        _fields_ = [('attrs', wintypes.DWORD), ('created', wintypes.FILETIME), ('accessed', wintypes.FILETIME),
            ('written', wintypes.FILETIME), ('volume', wintypes.DWORD), ('high', wintypes.DWORD), ('low', wintypes.DWORD),
            ('links', wintypes.DWORD), ('index_high', wintypes.DWORD), ('index_low', wintypes.DWORD)]
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p,
                                  wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    kernel.CreateFileW.restype = wintypes.HANDLE
    kernel.GetFileInformationByHandle.argtypes = [wintypes.HANDLE, ctypes.POINTER(Information)]
    kernel.GetFinalPathNameByHandleW.argtypes = [wintypes.HANDLE, wintypes.LPWSTR, wintypes.DWORD, wintypes.DWORD]
    kernel.GetFinalPathNameByHandleW.restype = wintypes.DWORD
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    # FILE_LIST_DIRECTORY participates in sharing checks; an attributes-only
    # handle does not reliably prevent Windows renames despite omitting DELETE.
    handle = kernel.CreateFileW(str(path), 0x81 | (0x10000 if removable else 0), 3, None, 3, 0x02000000 | 0x00200000, None)
    if handle == ctypes.c_void_p(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        opened, final = Information(), ctypes.create_unicode_buffer(32768)
        if not kernel.GetFileInformationByHandle(handle, ctypes.byref(opened)):
            raise ctypes.WinError(ctypes.get_last_error())
        length = kernel.GetFinalPathNameByHandleW(handle, final, len(final), 0)
        physical = final.value.removeprefix('\\\\?\\')
        if (not length or length >= len(final) or opened.attrs & REPARSE
                or opened.volume != info.st_dev & 0xffffffff
                or (opened.index_high << 32 | opened.index_low) != info.st_ino
                or os.path.normcase(os.path.abspath(physical)) != os.path.normcase(str(path))):
            raise ValueError('The pinned workspace directory identity changed.')
        def remove_empty():
            kernel.SetFileInformationByHandle.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
            flag = wintypes.BOOLEAN(1)
            if not kernel.SetFileInformationByHandle(handle, 4, ctypes.byref(flag), ctypes.sizeof(flag)):
                raise ctypes.WinError(ctypes.get_last_error())
        return handle, lambda: kernel.CloseHandle(handle), remove_empty if removable else None
    except BaseException:
        kernel.CloseHandle(handle)
        raise


class _Directories(AbstractContextManager):
    def __init__(self):
        self.entries = {}

    def add(self, path, *, removable=False):
        path = Path(path)
        if path in self.entries:
            return
        if path != path.parent:
            self.add(path.parent)
        _ordinary_data_path(path)
        info = path.lstat()
        if not stat.S_ISDIR(info.st_mode):
            raise ValueError('Workspace ancestry must remain ordinary directories.')
        if os.name == 'nt':
            handle, close, remove = _windows_directory(path, info, removable)
        else:
            parent = self.entries.get(path.parent)
            handle = os.open(path.name if parent else str(path), os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                             **({'dir_fd': parent[1]} if parent else {}))
            close = lambda descriptor=handle: os.close(descriptor)
            remove = None
            if _identity(os.fstat(handle)) != _identity(info):
                close()
                raise ValueError('Workspace ancestry changed while opening.')
        self.entries[path] = (info, handle, close, remove)

    def check(self, expected):
        for path, identity in expected.items():
            self.add(path)
            if _identity(self.entries[path][0]) != identity or _identity(path.lstat()) != identity:
                raise ValueError('The owned workspace was moved or replaced.')

    def mkdir(self, path, *, existing=False):
        path = Path(path)
        self.add(path.parent)
        try:
            if os.name == 'nt':
                path.mkdir()  # Inherit Data ACL; never reset an existing ACL or use Windows 0700.
            else:
                os.mkdir(path.name, mode=0o700, dir_fd=self.entries[path.parent][1])
        except FileExistsError:
            if not existing:
                raise
        self.add(path)

    def open(self, path, flags, mode=0o600):
        path = Path(path)
        self.add(path.parent)
        if os.name == 'nt':
            return os.open(path, flags, mode)
        return os.open(path.name, flags | os.O_NOFOLLOW, mode, dir_fd=self.entries[path.parent][1])

    def scan(self, path):
        self.add(path)
        return os.scandir(path if os.name == 'nt' else self.entries[path][1])

    def remove_empty(self, path):
        # POSIX directory fds cannot prohibit rename races. Retain those empty
        # folders; Windows can dispose the exact still-pinned directory handle.
        info, _, close, remove = self.entries[path]
        if remove is None:
            return False
        if _identity(path.lstat()) != _identity(info):
            raise ValueError('The empty payload directory identity changed.')
        with self.scan(path) as entries:
            if next(entries, None) is not None:
                return False
        remove()  # The kernel refuses a nonempty directory, including raced-in files.
        close()
        del self.entries[path]
        try:
            path.lstat()
        except FileNotFoundError:
            return True
        raise OSError('Empty-directory removal could not be confirmed.')

    def __exit__(self, *args):
        for record in reversed(list(self.entries.values())):
            record[2]()
        self.entries.clear()


def _write_new(pins, path, value):
    raw = json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False).encode('utf-8')
    with os.fdopen(pins.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL), 'wb') as stream:
        stream.write(raw)
        stream.flush()
        os.fsync(stream.fileno())
        info = os.fstat(stream.fileno())
    return raw, _identity(info)


def _successful(outcome, verified, quiescent, restart_pending):
    if (verified is not True or quiescent is not True or restart_pending is not False
            or not isinstance(outcome, dict) or type(outcome.get('exitCode')) is not int or outcome['exitCode'] != 0
            or outcome.get('installer_started') is not True or outcome.get('outcomeKnown') is not True
            or outcome.get('running') is not False):
        return False
    for key in ('restartNeeded', 'reboot_required', 'cancelled', 'skipped', 'requiresReview', 'uncertain'):
        if key in outcome and outcome[key] is not False:
            return False
    if (outcome.get('success', True) is not True
            or outcome.get('status') in ('failed', 'blocked', 'deferred', 'cancelled', 'uncertain', 'running')):
        return False
    pending, inspected = [(outcome, 0)], 0
    while pending:
        value, depth = pending.pop()
        inspected += 1
        if inspected > 512 or depth > 8:
            return False
        if isinstance(value, dict):
            if any(key in value and value[key] is not False for key in ('restartNeeded', 'reboot_required')):
                return False
            if value.get('running') is True or value.get('outcomeKnown') is False or value.get('uncertain') is True:
                return False
            pending.extend((item, depth + 1) for item in value.values() if isinstance(item, (dict, list)))
        elif isinstance(value, list):
            pending.extend((item, depth + 1) for item in value if isinstance(item, (dict, list)))
    return True


class PayloadWorkspace:
    """The live object, not its on-disk evidence, owns this one payload scope."""
    def __init__(self):
        raise TypeError('Use PayloadWorkspace.create(). Existing workspaces cannot be loaded.')

    def __reduce__(self):
        raise TypeError('Payload cleanup authority cannot be serialized.')

    @classmethod
    def create(cls, data_dir, provider, package_id, enabled=True):
        if type(enabled) is not bool:
            raise ValueError('Choose an explicit payload-cleanup setting.')
        if any(not isinstance(value, str) or not value or len(value) > 280
               or any(ord(char) < 32 for char in value) for value in (provider, package_id)):
            raise ValueError('A bounded provider and exact package identity are required.')
        self = object.__new__(cls)
        self._provider, self._package_id, self._enabled = provider, package_id, enabled
        self._lock, self._finished, self._receipt = threading.Lock(), False, None
        self._id, self._token = uuid.uuid4().hex, uuid.uuid4().hex
        if not enabled:
            return self
        data = _local_data_path(str(data_dir))
        if not data.is_absolute() or data == Path(data.anchor) or '..' in data.parts:
            raise ValueError('Use the existing absolute ordinary Optimus data directory.')
        _ordinary_data_path(data)
        data = data.resolve(strict=True)
        with _Directories() as pins:
            pins.add(data)
            base = data / 'update-payloads'
            pins.mkdir(base, existing=True)
            self._workspace, self._payload = base / self._id, base / self._id / 'payload'
            pins.mkdir(self._workspace)
            pins.mkdir(self._payload)
            self._identities = {path: _identity(pins.entries[path][0]) for path in (data, base, self._workspace, self._payload)}
            if any(identity[0] != self._identities[data][0] for identity in self._identities.values()):
                raise ValueError('Payload workspaces cannot cross filesystems.')
            manifest = dict(schema=1, workspace_id=self._id, token=self._token, provider=provider, package_id=package_id,
                workspace_identity=self._identities[self._workspace], payload_identity=self._identities[self._payload])
            self._manifest, self._manifest_identity = _write_new(pins, self._workspace / 'manifest.json', manifest)
        return self

    def env_overrides(self):
        if not self._enabled or self._finished:
            return {}
        return {name: str(self._payload) for name in ('TEMP', 'TMP', 'TMPDIR')}

    def _validate(self, pins):
        pins.check(self._identities)
        path = self._workspace / 'manifest.json'
        info = _ordinary_data_path(path, file=True)
        if info is None or _identity(info) != self._manifest_identity or info.st_size != len(self._manifest):
            raise ValueError('Workspace evidence identity changed; payloads were preserved.')
        with os.fdopen(pins.open(path, os.O_RDONLY), 'rb') as stream:
            opened = os.fstat(stream.fileno())
            raw = stream.read(len(self._manifest) + 1)
            if (_identity(opened) != self._manifest_identity or opened.st_nlink != 1
                    or raw != self._manifest or _identity(os.fstat(stream.fileno())) != self._manifest_identity):
                raise ValueError('Workspace evidence changed; payloads were preserved.')

    def finish(self, outcome, *, verified=False, quiescent=False, restart_pending=False):
        with self._lock:
            if self._finished:
                return copy.deepcopy(self._receipt)
            self._finished = True
            report = dict(schema=1, kind='update_payload_cleanup', workspace_id=self._id, provider=self._provider,
                package_id=self._package_id, status='preserved', removed_files=0, removed_bytes=0,
                ownership='none', workspace_owned=False,
                removed_directories=0, retained_entries=0, inspected_entries=0, complete=True, errors=[],
                note='Payloads are preserved unless installation, verification and process quiescence are explicit.')
            if not self._enabled:
                report.update(status='disabled', note='Installer-payload cleanup is disabled.')
            elif not _successful(outcome, verified, quiescent, restart_pending):
                report['note'] = 'Unverified, active, failed or restart-pending update: payloads and evidence were preserved.'
            else:
                try:
                    self._clean(report)
                except Exception as failure:
                    report.update(status='partial' if report['removed_files'] else 'preserved', complete=False)
                    report['errors'] = (report['errors'] + [type(failure).__name__ + ': payload cleanup stopped safely.'])[:8]
            self._receipt = copy.deepcopy(report)
            return copy.deepcopy(report)

    def _clean(self, report):
        deadline, records, directories = time.monotonic() + _MAX_SECONDS, [], []
        def error(message):
            report['complete'] = False
            if len(report['errors']) < 8:
                report['errors'].append(str(message)[:400])
        try:
            with _Directories() as pins:
                self._validate(pins)
                report.update(ownership='optimus_private_update_workspace', workspace_owned=True)
                _write_new(pins, self._workspace / 'cleanup-intent.json',
                    dict(schema=1, workspace_id=self._id, verified=True, quiescent=True, restart_pending=False))
                stack = [(self._payload, 0)]
                while stack:
                    directory, depth = stack.pop()
                    self._validate(pins)
                    with pins.scan(directory) as entries:
                        for entry in entries:
                            if report['inspected_entries'] >= _MAX_ENTRIES or time.monotonic() >= deadline:
                                raise TimeoutError('The bounded payload scan stopped; remaining entries were preserved.')
                            report['inspected_entries'] += 1
                            path = directory / entry.name
                            # Windows DirEntry.stat omits inode/device/link count;
                            # use lstat under the pinned ordinary parent instead.
                            info = path.lstat() if os.name == 'nt' else entry.stat(follow_symlinks=False)
                            if (_PROTECTED.search(entry.name) or not _ordinary(info, directory=stat.S_ISDIR(info.st_mode))
                                    or info.st_dev != self._identities[self._payload][0]):
                                report['retained_entries'] += 1
                                continue
                            if stat.S_ISDIR(info.st_mode):
                                if depth >= _MAX_DEPTH:
                                    report['retained_entries'] += 1
                                    continue
                                pins.add(path, removable=True)
                                directories.append(path)
                                stack.append((path, depth + 1))
                            elif path.suffix.casefold() in _SUFFIXES or re.fullmatch(r'[a-fA-F0-9]{64}', path.name):
                                records.append(dict(path=str(path), root=str(self._payload), size=info.st_size,
                                    modified_ns=info.st_mtime_ns, inode=info.st_ino, device=info.st_dev))
                            else:
                                report['retained_entries'] += 1
                for record in records:
                    if time.monotonic() >= deadline or report['removed_bytes'] + record['size'] > _MAX_BYTES:
                        error('The bounded payload deletion stopped; remaining payloads were preserved.')
                        break
                    try:
                        self._validate(pins)
                        Cleaner._delete_exact(record)
                        try:
                            Path(record['path']).lstat()
                        except FileNotFoundError:
                            pass
                        else:
                            raise OSError('Payload deletion could not be confirmed; no bytes were counted.')
                        report['removed_files'] += 1
                        report['removed_bytes'] += record['size']
                    except (OSError, ValueError) as failure:
                        report['retained_entries'] += 1
                        error(type(failure).__name__ + ': ' + str(failure))
                for directory in sorted(directories, key=lambda path: len(path.parts), reverse=True):
                    if time.monotonic() >= deadline:
                        error('The bounded cleanup stopped; remaining empty folders were preserved.')
                        break
                    try:
                        self._validate(pins)
                        report['removed_directories'] += int(pins.remove_empty(directory))
                    except (OSError, ValueError) as failure:
                        error(type(failure).__name__ + ': ' + str(failure))
                report['status'] = 'completed' if report['complete'] else 'partial'
                report['note'] = ('Only recognized payload files in this update workspace were considered. Evidence, protected files and workspace roots were retained. '
                    'Empty child directories are removed only with pinned Windows handles; other folders are retained.')
                _write_new(pins, self._workspace / 'cleanup-result.json', report)
        except (OSError, ValueError) as failure:
            error(type(failure).__name__ + ': ' + str(failure))
            report['status'] = 'partial' if report['removed_files'] else 'preserved'

