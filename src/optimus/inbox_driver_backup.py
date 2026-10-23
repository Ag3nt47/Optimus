"""Verified private snapshots of signed Windows inbox DriverStore packages.

This copies OUT of the protected store. It never stages, installs, deletes,
changes permissions, or writes into Windows. A snapshot is recovery evidence,
not a PnPUtil OEM export or a promise that rollback/reinstallation will succeed.

Primary contracts:
https://learn.microsoft.com/windows/win32/api/setupapi/nf-setupapi-setupgetinfdriverstorelocationw
https://learn.microsoft.com/windows/win32/api/setupapi/nf-setupapi-setupgetinfpublishednamew
https://learn.microsoft.com/windows/win32/api/setupapi/nf-setupapi-setupverifyinffilew
https://learn.microsoft.com/windows-hardware/drivers/install/driver-store
"""
from __future__ import annotations

import ctypes
import hashlib
import os
from pathlib import Path
import re
import shutil
import stat
import sys
import time

from .filesystem import _ordinary_data_path

_INF = re.compile(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,199}\.inf\Z', re.I)
_OEM = re.compile(r'oem[0-9]+\.inf\Z', re.I)
_INBOX_SCORE = 0x0D000003  # SIGNERSCORE_INBOX, Windows SDK setupapi.h.
_MAX_FILES = 4096
_MAX_BYTES = 512 * 1024 ** 2
_RESERVE_BYTES = 2 * 1024 ** 3


def _check(event, deadline):
    if event is not None and event.is_set() or time.monotonic() >= deadline:
        raise RuntimeError('Inbox package backup was cancelled or exceeded its time budget.')


def _ordinary_source(path, *, file=False):
    """Protected Windows files may have legitimate WinSxS hardlinks."""
    path = Path(path)
    if not path.is_absolute():
        raise ValueError('An absolute native Windows source path is required.')
    for current in (path, *path.parents):
        info = current.lstat()
        if stat.S_ISLNK(info.st_mode) or getattr(info, 'st_file_attributes', 0) & 0x400:
            raise ValueError('Indirect Windows package paths are not supported.')
        if current == path and file:
            if not stat.S_ISREG(info.st_mode):
                raise ValueError('An ordinary Windows package file is required.')
            leaf = info
        elif not stat.S_ISDIR(info.st_mode):
            raise ValueError('Ordinary Windows package directories are required.')
    return leaf if file else None


def _inside(path, directory):
    return Path(path).is_relative_to(Path(directory))


def _walk_error(error):
    raise error


def _source_hash(path, event, deadline):
    before = _ordinary_source(path, file=True)
    if before.st_size > _MAX_BYTES:
        raise ValueError('The inbox package exceeds its file-size bound.')
    digest = hashlib.sha256()
    with Path(path).open('rb') as stream:
        opened = os.fstat(stream.fileno())
        if (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino):
            raise ValueError('The Windows package file changed while opening.')
        while True:
            _check(event, deadline)
            block = stream.read(1024 ** 2)
            if not block:
                break
            digest.update(block)
        after = os.fstat(stream.fileno())
    final = _ordinary_source(path, file=True)
    expected = (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
    if any((row.st_dev, row.st_ino, row.st_size, row.st_mtime_ns) != expected for row in (opened, after, final)):
        raise ValueError('The Windows package file changed during hashing.')
    return {'size': before.st_size, 'sha256': digest.hexdigest()}


class _SignerInfo(ctypes.Structure):
    _fields_ = [('cbSize', ctypes.c_uint32), ('CatalogFile', ctypes.c_wchar * 260),
                ('DigitalSigner', ctypes.c_wchar * 260), ('DigitalSignerVersion', ctypes.c_wchar * 260),
                ('SignerScore', ctypes.c_uint32)]


class WindowsInboxCatalog:
    """Lazy native read-only API adapter; no DLL loads in the constructor."""
    def __init__(self):
        self.api = None

    def _load(self):
        if sys.platform != 'win32':
            raise OSError('Inbox package recovery is supported only on Windows.')
        if self.api is not None:
            return
        kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        system = ctypes.create_unicode_buffer(32768)
        windows = ctypes.create_unicode_buffer(32768)
        for name, buffer in (('GetSystemDirectoryW', system), ('GetWindowsDirectoryW', windows)):
            function = getattr(kernel, name)
            function.argtypes = [ctypes.c_wchar_p, ctypes.c_uint32]
            function.restype = ctypes.c_uint32
            count = function(buffer, len(buffer))
            if not 0 < count < len(buffer):
                raise OSError('Native Windows package roots are unavailable.')
        self.system, self.windows = Path(system.value), Path(windows.value)
        _ordinary_source(self.system)
        _ordinary_source(self.windows)
        library = self.system / 'setupapi.dll'
        _ordinary_source(library, file=True)
        api = ctypes.WinDLL(str(library), use_last_error=True)
        api.SetupGetInfDriverStoreLocationW.argtypes = [ctypes.c_wchar_p, ctypes.c_void_p,
            ctypes.c_wchar_p, ctypes.c_wchar_p, ctypes.c_uint32, ctypes.POINTER(ctypes.c_uint32)]
        api.SetupGetInfPublishedNameW.argtypes = [ctypes.c_wchar_p, ctypes.c_wchar_p,
            ctypes.c_uint32, ctypes.POINTER(ctypes.c_uint32)]
        api.SetupVerifyInfFileW.argtypes = [ctypes.c_wchar_p, ctypes.c_void_p, ctypes.POINTER(_SignerInfo)]
        for name in ('SetupGetInfDriverStoreLocationW', 'SetupGetInfPublishedNameW', 'SetupVerifyInfFileW'):
            getattr(api, name).restype = ctypes.c_int
        self.api = api

    @staticmethod
    def _path(function, *args):
        buffer = ctypes.create_unicode_buffer(260)
        required = ctypes.c_uint32()
        if not function(*args, buffer, len(buffer), ctypes.byref(required)):
            raise OSError(ctypes.get_last_error(), 'Windows could not resolve the exact installed INF package.')
        if not buffer.value or required.value > len(buffer):
            raise ValueError('Windows returned an invalid INF package path.')
        return Path(buffer.value)

    def describe(self, inf):
        if not isinstance(inf, str) or not _INF.fullmatch(inf) or _OEM.fullmatch(inf):
            raise ValueError('An exact non-OEM installed inbox INF name is required.')
        self._load()
        published = self.windows / 'INF' / inf
        _ordinary_source(published, file=True)
        store = self._path(self.api.SetupGetInfDriverStoreLocationW, str(published), None, None)
        reverse = self._path(self.api.SetupGetInfPublishedNameW, str(store))
        # Windows can return the published INF basename rather than its full
        # system-INF path. Resolve only that exact known basename; relative
        # directories, drive-relative paths and a different name still refuse.
        if not reverse.is_absolute():
            if str(reverse) != reverse.name or reverse.name.casefold() != inf.casefold():
                raise ValueError('The reverse published INF is not the exact installed basename.')
            reverse = self.windows / 'INF' / reverse.name
        root = self.system / 'DriverStore' / 'FileRepository'
        if (reverse != published or not _inside(store, root) or store.parent.parent != root
                or store.suffix.casefold() != '.inf'):
            raise ValueError('Native forward/reverse inbox INF package binding differs.')
        _ordinary_source(store, file=True)
        info = _SignerInfo()
        info.cbSize = ctypes.sizeof(info)
        if not self.api.SetupVerifyInfFileW(str(store), None, ctypes.byref(info)):
            raise OSError(ctypes.get_last_error(), 'Windows did not verify the inbox INF catalog signature.')
        if info.SignerScore != _INBOX_SCORE or not re.match(r'Microsoft Windows(?:\s|$)', info.DigitalSigner):
            raise ValueError('Windows did not identify a trusted Microsoft inbox catalog signature.')
        catalog = Path(info.CatalogFile)
        if (not catalog.is_absolute() or catalog.suffix.casefold() != '.cat'
                or not any(_inside(catalog, directory) for directory in
                    (store.parent, self.system / 'CatRoot', self.windows / 'WinSxS' / 'Catalogs'))):
            raise ValueError('The verified inbox catalog is not in an ordinary Windows catalog location.')
        _ordinary_source(catalog, file=True)
        return {'kind': 'inbox-driver-store-snapshot', 'published_inf': str(published), 'store_inf': str(store),
                'package_directory': str(store.parent), 'catalog': str(catalog),
                'digital_signer': info.DigitalSigner, 'digital_signer_version': info.DigitalSignerVersion,
                'signer_score': int(info.SignerScore), 'catalog_trust_verified': True}


class InboxDriverSnapshot:
    """Complete private package copy, with a native/source/destination readback."""
    def __init__(self, adapter=None, *, disk_usage=None, reserve_bytes=_RESERVE_BYTES):
        if type(reserve_bytes) is not int or not _RESERVE_BYTES <= reserve_bytes <= 16 * 1024 ** 4:
            raise ValueError('Inbox backups must preserve a known destination reserve of at least 2 GiB.')
        self.adapter = adapter
        self.disk_usage = disk_usage or shutil.disk_usage
        self.reserve_bytes = reserve_bytes

    def _space(self, destination, required=0):
        usage = self.disk_usage(destination)
        if type(usage.free) is not int or usage.free < required + self.reserve_bytes:
            raise OSError('Insufficient verified destination free space for the complete inbox package backup and reserved buffer.')

    def _adapter(self):
        if self.adapter is None:
            self.adapter = WindowsInboxCatalog()
        return self.adapter

    def _describe(self, inf):
        value = self._adapter().describe(inf)
        if (not isinstance(value, dict) or value.get('kind') != 'inbox-driver-store-snapshot'
                or value.get('catalog_trust_verified') is not True or value.get('signer_score') != _INBOX_SCORE):
            raise ValueError('The native inbox catalog proof is incomplete.')
        return value

    def _files(self, description, event, deadline):
        root = Path(description['package_directory'])
        _ordinary_source(root)
        files = []
        total = 0
        for current, directories, names in os.walk(root, followlinks=False, onerror=_walk_error):
            _ordinary_source(Path(current))
            for directory in directories:
                _ordinary_source(Path(current) / directory)
            for name in sorted(names):
                _check(event, deadline)
                source = Path(current) / name
                relative = source.relative_to(root).as_posix()
                value = _source_hash(source, event, deadline)
                total += value['size']
                files.append({'path': 'package/' + relative, 'source': str(source), **value})
                if len(files) > _MAX_FILES or total > _MAX_BYTES:
                    raise ValueError('The complete inbox package exceeds its bounded backup size.')
        if not files:
            raise ValueError('The resolved inbox package is empty.')
        sources = {row['source'] for row in files}
        if description['store_inf'] not in sources:
            raise ValueError('The resolved native INF was absent from the package inventory.')
        for name, source in (('published.inf', description['published_inf']), ('trusted-catalog.cat', description['catalog'])):
            value = _source_hash(Path(source), event, deadline)
            total += value['size']
            files.append({'path': name, 'source': source, **value})
        if total > _MAX_BYTES:
            raise ValueError('The inbox package and catalog exceed the backup byte bound.')
        inf = next(row for row in files if row['source'] == description['store_inf'])
        published = next(row for row in files if row['path'] == 'published.inf')
        if inf['sha256'] != published['sha256']:
            raise ValueError('The published and DriverStore INF bytes differ.')
        return sorted(files, key=lambda row: row['path'])

    def snapshot(self, inf, destination, *, cancel_event=None, deadline=None, progress_callback=None):
        deadline = deadline if deadline is not None else time.monotonic() + 180
        _check(cancel_event, deadline)
        destination = Path(destination)
        _ordinary_data_path(destination)
        if not destination.is_dir() or any(destination.iterdir()):
            raise ValueError('A new empty private inbox snapshot directory is required.')
        description = self._describe(inf)
        files = self._files(description, cancel_event, deadline)
        needed = sum(row['size'] for row in files)
        self._space(destination, needed)
        last_progress = time.monotonic()
        from .driver_backups import _hash_file
        for index, row in enumerate(files):
            _check(cancel_event, deadline)
            self._space(destination, needed)
            source = Path(row['source'])
            target = destination / row['path']
            target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            _ordinary_data_path(target.parent)
            before = _ordinary_source(source, file=True)
            with source.open('rb') as origin, target.open('xb') as output:
                opened = os.fstat(origin.fileno())
                if (opened.st_dev, opened.st_ino, opened.st_size, opened.st_mtime_ns) != (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns):
                    raise ValueError('A protected inbox package file changed before copying.')
                copied = 0
                while True:
                    _check(cancel_event, deadline)
                    block = origin.read(1024 ** 2)
                    if not block:
                        break
                    copied += len(block)
                    if copied > row['size']:
                        raise ValueError('A protected inbox package file grew during copying.')
                    output.write(block)
                output.flush()
                os.fsync(output.fileno())
            needed -= row['size']
            if _hash_file(target, cancel_event=cancel_event, deadline=deadline) != {key: row[key] for key in ('size', 'sha256')}:
                raise ValueError('The private inbox package copy failed its hash readback.')
            if progress_callback and time.monotonic() - last_progress >= 1:
                progress_callback({'phase': 'backup', 'description': f'Verifying Windows inbox package {inf}: {index + 1}/{len(files)} files',
                                   'current': index + 1, 'total': len(files)})
                last_progress = time.monotonic()
        self._space(destination)
        result = {'kind': 'inbox-driver-store-snapshot', 'verified': True, 'inf': inf,
                  'native_binding': description, 'files': files, 'destination': str(destination),
                  'oem_export': False, 'automatic_restore_supported': False, 'destination_reserve_bytes': self.reserve_bytes,
                  'restore_note': 'Verified private Windows package and catalog snapshot. Windows Device Manager may offer rollback; availability and successful reinstall are not guaranteed. No protected store file was changed.'}
        self.verify(result, cancel_event=cancel_event, deadline=deadline)
        return result

    def verify(self, snapshot, *, cancel_event=None, deadline=None):
        from .driver_backups import _hash_file
        deadline = deadline if deadline is not None else time.monotonic() + 180
        _check(cancel_event, deadline)
        if snapshot.get('destination_reserve_bytes') != self.reserve_bytes:
            raise ValueError('The configured destination reserve changed; a fresh inbox backup is required.')
        current = self._describe(snapshot['inf'])
        if current != snapshot['native_binding'] or self._files(current, cancel_event, deadline) != snapshot['files']:
            raise ValueError('The native inbox package identity or complete source bytes changed after backup.')
        destination = Path(snapshot['destination'])
        _ordinary_data_path(destination)
        self._space(destination)
        expected = {row['path']: {key: row[key] for key in ('size', 'sha256')} for row in snapshot['files']}
        actual = {}
        for directory, folders, names in os.walk(destination, followlinks=False, onerror=_walk_error):
            _ordinary_data_path(Path(directory))
            for folder in folders:
                _ordinary_data_path(Path(directory) / folder)
            for name in names:
                _check(cancel_event, deadline)
                path = Path(directory) / name
                actual[path.relative_to(destination).as_posix()] = _hash_file(path, cancel_event=cancel_event, deadline=deadline)
        if actual != expected:
            raise ValueError('The complete private inbox backup changed or contains unexpected files.')
        self._space(destination)
        return True
