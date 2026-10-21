"""Previewed cache cleanup. Only allowlisted roots and scan-approved identities can be deleted."""
from __future__ import annotations
import ctypes
import json
import os
import stat
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable
import psutil

REPARSE = 0x400
OFFLINE = 0x1000
RECALL = 0x40000 | 0x400000
SYSTEM = 0x4
READONLY = 0x1

@dataclass(frozen=True)
class Category:
    id: str
    name: str
    path: Path
    description: str
    process_names: tuple[str, ...] = ()
    requires_admin: bool = False
    suffixes: tuple[str, ...] = ()

class Cleaner:
    def __init__(self, data_dir: Path, categories: list[Category] | None = None):
        self.data_dir = Path(data_dir)
        self._custom_categories = categories
        self._plans: dict[str, dict] = {}
        self._lock = threading.RLock()

    def _definitions(self) -> list[Category]:
        if self._custom_categories is not None:
            return self._custom_categories
        local = Path(os.environ['LOCALAPPDATA'])
        windows = Path(os.environ.get('SystemRoot', r'C:\Windows'))
        definitions = [
            Category('user-temp', 'User temporary files', Path(os.environ.get('TEMP', str(local / 'Temp'))), 'Old temporary files; active, changed and locked files are skipped.'),
            Category('windows-temp', 'Windows temporary files', windows / 'Temp', 'Old Windows temporary files. Administrator rights may be required.', requires_admin=True),
            Category('error-archive', 'Windows error report archive', Path(os.environ.get('ProgramData', r'C:\ProgramData')) / 'Microsoft/Windows/WER/ReportArchive', 'Archived diagnostics; removes previous error-report details.', requires_admin=True),
            Category('error-queue', 'Windows error report queue', Path(os.environ.get('ProgramData', r'C:\ProgramData')) / 'Microsoft/Windows/WER/ReportQueue', 'Old pending error reports; review before deletion.', requires_admin=True),
            Category('crash-dumps', 'Application crash dumps', local / 'CrashDumps', 'Old application crash dumps; these can be useful for troubleshooting.', suffixes=('.dmp',)),
            Category('shader-cache', 'DirectX shader cache', local / 'D3DSCache', 'Shaders rebuild when needed and may cause temporary stutter after clearing.'),
            Category('user-error-archive', 'User Windows error report archive', local / 'Microsoft/Windows/WER/ReportArchive', 'Old archived application diagnostics for this user. Removes previous troubleshooting details.'),
            Category('user-error-queue', 'User Windows error report queue', local / 'Microsoft/Windows/WER/ReportQueue', 'Old pending application diagnostics for this user. Review before deletion.'),
            Category('windows-minidumps', 'Windows crash minidumps', windows / 'Minidump', 'Old crash diagnostics; keep these when investigating blue screens. Administrator rights may be required.', requires_admin=True, suffixes=('.dmp',)),
            Category('nvidia-dx-cache', 'NVIDIA DirectX shader cache', local / 'NVIDIA/DXCache', 'Optional rebuildable graphics cache. Clearing it can temporarily increase game loading time or shader stutter.'),
            Category('nvidia-gl-cache', 'NVIDIA OpenGL shader cache', local / 'NVIDIA/GLCache', 'Optional rebuildable graphics cache; locked or changed files are skipped.'),
            Category('amd-dx-cache', 'AMD DirectX shader cache', local / 'AMD/DxCache', 'Optional rebuildable graphics cache. Clearing it can temporarily increase game loading time or shader stutter.'),
            Category('amd-dxc-cache', 'AMD shader compiler cache', local / 'AMD/DxcCache', 'Optional rebuildable graphics cache; locked or changed files are skipped.'),
        ]
        chromium = [
            ('edge', 'Microsoft Edge', local / 'Microsoft/Edge/User Data', ('msedge.exe',)),
            ('chrome', 'Google Chrome', local / 'Google/Chrome/User Data', ('chrome.exe',)),
            ('brave', 'Brave', local / 'BraveSoftware/Brave-Browser/User Data', ('brave.exe',)),
            ('vivaldi', 'Vivaldi', local / 'Vivaldi/User Data', ('vivaldi.exe',)),
        ]
        for browser_id, name, base, processes in chromium:
            if not base.is_dir():
                continue
            try:
                profiles = sorted(p for p in base.iterdir() if p.is_dir() and (p.name == 'Default' or p.name.startswith('Profile ')))
            except OSError:
                continue
            for profile in profiles:
                for cache_name in ('Cache', 'Code Cache', 'GPUCache'):
                    ident = f'{browser_id}:{profile.name}:{cache_name}'
                    definitions.append(Category(ident, f'{name} / {profile.name} / {cache_name}', profile / cache_name, 'Cache only. Close this browser first. Passwords, cookies, history and bookmarks are preserved.', processes))
        firefox_base = local / 'Mozilla/Firefox/Profiles'
        if firefox_base.is_dir():
            try:
                for profile in firefox_base.iterdir():
                    if profile.is_dir():
                        definitions.append(Category(f'firefox:{profile.name}', f'Firefox / {profile.name} cache', profile / 'cache2', 'Cache only; browser must be closed.', ('firefox.exe',)))
            except OSError:
                pass
        return definitions

    def categories(self) -> list[dict]:
        result = []
        for category in self._definitions():
            available = category.path.is_dir()
            result.append({'id': category.id, 'name': category.name, 'path': str(category.path), 'available': available,
                           'description': category.description, 'requires_admin': category.requires_admin, 'process_names': list(category.process_names)})
        return result

    @staticmethod
    def _safe_path(path: Path, root: Path) -> bool:
        try:
            full = Path(os.path.abspath(path))
            base = Path(os.path.abspath(root))
            if full == base or os.path.commonpath([str(full), str(base)]).casefold() != str(base).casefold():
                return False
            # Resolve no junctions or cloud placeholders, including any ancestor of the root.
            for component in (full, *full.parents):
                info = component.lstat()
                flags = getattr(info, 'st_file_attributes', 0)
                if stat.S_ISLNK(info.st_mode) or flags & (REPARSE | OFFLINE | RECALL):
                    return False
            return True
        except (OSError, ValueError):
            return False

    @staticmethod
    def _running(names: Iterable[str]) -> list[str]:
        wanted = {name.casefold() for name in names}
        if not wanted:
            return []
        result = set()
        for process in psutil.process_iter(['name']):
            try:
                name = process.info.get('name') or ''
                if name.casefold() in wanted:
                    result.add(name)
            except (psutil.Error, OSError):
                continue
        return sorted(result)

    def scan(self, category_ids: list[str], min_age_days: int = 7, max_files: int = 20000, max_seconds: int = 35) -> dict:
        if not 1 <= int(min_age_days) <= 3650:
            raise ValueError('Minimum age must be between 1 and 3650 days.')
        choices = {item.id: item for item in self._definitions()}
        unknown = set(category_ids) - choices.keys()
        if unknown:
            raise ValueError('Unknown cleanup categories.')
        now = time.time()
        deadline = time.monotonic() + max(1, min(int(max_seconds), 120))
        plan = {'id': uuid.uuid4().hex, 'created_at': now, 'min_age_days': int(min_age_days), 'categories': [], 'files': [],
                'total_bytes': 0, 'total_mb': 0.0, 'complete': True, 'skipped': [], 'errors': []}
        seen = set()
        examined = 0
        for category_id in dict.fromkeys(category_ids):
            category = choices[category_id]
            root = Path(os.path.abspath(category.path))
            busy = self._running(category.process_names)
            if busy:
                plan['skipped'].append(f'{category.name}: close {", ".join(busy)} first.')
                continue
            if not root.is_dir():
                plan['skipped'].append(f'{category.name}: folder not present.')
                continue
            summary = {'id': category.id, 'name': category.name, 'files': 0, 'bytes': 0, 'size_mb': 0.0}
            stack = [root]
            while stack:
                if examined >= max_files or time.monotonic() >= deadline:
                    plan['complete'] = False
                    break
                directory = stack.pop()
                try:
                    if directory != root and not self._safe_path(directory, root):
                        continue
                    # Check root itself before enumeration without allowing it as a deletion target.
                    if directory == root and not self._safe_path(root / '__optimus_path_probe__', root):
                        # Probe need not exist: validate the real directory and its parents separately.
                        unsafe = False
                        for part in (root, *root.parents):
                            attrs = part.lstat()
                            if stat.S_ISLNK(attrs.st_mode) or getattr(attrs, 'st_file_attributes', 0) & (REPARSE | OFFLINE | RECALL):
                                unsafe = True
                                break
                        if unsafe:
                            plan['skipped'].append(f'{category.name}: indirect/cloud folder is excluded.')
                            break
                    with os.scandir(directory) as entries:
                        for entry in entries:
                            if examined >= max_files or time.monotonic() >= deadline:
                                plan['complete'] = False
                                break
                            path = Path(entry.path)
                            try:
                                info = entry.stat(follow_symlinks=False)
                                flags = getattr(info, 'st_file_attributes', 0)
                                if entry.is_symlink() or flags & (REPARSE | OFFLINE | RECALL | SYSTEM | READONLY):
                                    continue
                                if entry.is_dir(follow_symlinks=False):
                                    stack.append(path)
                                    continue
                                examined += 1
                                info = path.lstat()  # DirEntry.stat leaves inode/link/device zero on Windows.
                                if not stat.S_ISREG(info.st_mode) or info.st_nlink > 1:
                                    continue
                                if category.suffixes and path.suffix.casefold() not in category.suffixes:
                                    continue
                                if now - max(info.st_mtime, info.st_ctime) < min_age_days * 86400:
                                    continue
                                key = str(path).casefold()
                                if key in seen or not self._safe_path(path, root):
                                    continue
                                seen.add(key)
                                record = {'id': uuid.uuid4().hex, 'category_id': category.id, 'category': category.name,
                                          'path': str(path), 'root': str(root), 'size': info.st_size, 'size_mb': round(info.st_size / 1048576, 3),
                                          'modified_ns': info.st_mtime_ns, 'created_ns': info.st_ctime_ns,
                                          'inode': info.st_ino, 'device': info.st_dev, 'age_days': round((now - info.st_mtime) / 86400, 1)}
                                plan['files'].append(record)
                                summary['files'] += 1
                                summary['bytes'] += info.st_size
                            except OSError as error:
                                plan['errors'].append(f'{path}: {error.strerror or type(error).__name__}')
                except OSError as error:
                    plan['errors'].append(f'{directory}: {error.strerror or type(error).__name__}')
            summary['size_mb'] = round(summary['bytes'] / 1048576, 3)
            plan['categories'].append(summary)
            if not plan['complete']:
                break
        if plan['errors']:
            plan['complete'] = False
        plan['files'].sort(key=lambda item: item['size'], reverse=True)
        plan['total_bytes'] = sum(item['size'] for item in plan['files'])
        plan['total_mb'] = round(plan['total_bytes'] / 1048576, 3)
        with self._lock:
            # Isolate cached approval records from callers mutating a displayed preview.
            self._plans[plan['id']] = json.loads(json.dumps(plan))
        return plan

    @staticmethod
    def _delete_exact(record: dict) -> None:
        path = Path(record['path'])
        info = path.lstat()
        if not Cleaner._safe_path(path, Path(record['root'])):
            raise ValueError('File moved outside its approved ordinary folder.')
        if (info.st_size, info.st_mtime_ns, info.st_ino, info.st_dev) != (record['size'], record['modified_ns'], record['inode'], record['device']):
            raise ValueError('File changed after preview.')
        if info.st_nlink > 1 or getattr(info, 'st_file_attributes', 0) & (REPARSE | OFFLINE | RECALL | SYSTEM | READONLY):
            raise ValueError('Linked, cloud, system or read-only file excluded.')
        if os.name != 'nt':
            raise OSError('Cleanup deletion is supported only on Windows.')
        from ctypes import wintypes
        class FileInformation(ctypes.Structure):
            _fields_ = [('attrs', wintypes.DWORD), ('created', wintypes.FILETIME), ('accessed', wintypes.FILETIME), ('written', wintypes.FILETIME),
                        ('volume', wintypes.DWORD), ('size_high', wintypes.DWORD), ('size_low', wintypes.DWORD), ('links', wintypes.DWORD),
                        ('index_high', wintypes.DWORD), ('index_low', wintypes.DWORD)]
        kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        kernel.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
        kernel.CreateFileW.restype = wintypes.HANDLE
        kernel.GetFileInformationByHandle.argtypes = [wintypes.HANDLE, ctypes.POINTER(FileInformation)]
        kernel.SetFileInformationByHandle.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        handle = kernel.CreateFileW(str(path), 0x10000 | 0x80, 0, None, 3, 0x00200000, None)
        if handle == ctypes.c_void_p(-1).value:
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            identity = FileInformation()
            if not kernel.GetFileInformationByHandle(handle, ctypes.byref(identity)):
                raise ctypes.WinError(ctypes.get_last_error())
            kernel.GetFinalPathNameByHandleW.argtypes = [wintypes.HANDLE, wintypes.LPWSTR, wintypes.DWORD, wintypes.DWORD]
            buffer = ctypes.create_unicode_buffer(32768)
            length = kernel.GetFinalPathNameByHandleW(handle, buffer, len(buffer), 0)
            if not length or length >= len(buffer):
                raise OSError('Opened file path could not be verified.')
            final_path = buffer.value
            if final_path.startswith('\\\\?\\'):
                final_path = final_path[4:]
            if os.path.normcase(os.path.abspath(final_path)) != os.path.normcase(os.path.abspath(record['path'])):
                raise ValueError('Opened file resolved outside its approved path.')
            if identity.volume != (record['device'] & 0xffffffff):
                raise ValueError('File moved to another volume.')
            size = (identity.size_high << 32) | identity.size_low
            index = (identity.index_high << 32) | identity.index_low
            written = ((identity.written.dwHighDateTime << 32) | identity.written.dwLowDateTime) * 100 - 11644473600000000000
            if size != record['size'] or index != record['inode'] or abs(written - record['modified_ns']) > 100 or identity.links > 1:
                raise ValueError('Open file identity no longer matches the preview.')
            if identity.attrs & (REPARSE | OFFLINE | RECALL | SYSTEM | READONLY):
                raise ValueError('Protected file excluded.')
            delete_flag = wintypes.BOOLEAN(1)
            if not kernel.SetFileInformationByHandle(handle, 4, ctypes.byref(delete_flag), ctypes.sizeof(delete_flag)):
                raise ctypes.WinError(ctypes.get_last_error())
        finally:
            kernel.CloseHandle(handle)

    def apply(self, displayed_plan: dict) -> dict:
        with self._lock:
            plan = self._plans.pop(str(displayed_plan.get('id', '')), None)
        if plan is None:
            raise ValueError('Preview expired. Scan again in this app session.')
        if time.time() - plan['created_at'] > 900:
            raise ValueError('Preview is older than 15 minutes. Scan again.')
        choices = {item.id: item for item in self._definitions()}
        result = {'deleted_count': 0, 'deleted_bytes': 0, 'skipped_count': 0, 'errors': [], 'files': []}
        log_root = self.data_dir / 'cleanup'
        log_root.mkdir(parents=True, exist_ok=True)
        log_path = log_root / f'{plan["id"]}.json'
        # A complete preview journal must exist before any deletion.
        log_path.write_text(json.dumps({'state': 'prepared', 'plan': plan}, indent=2), encoding='utf-8')
        for record in plan['files']:
            try:
                category = choices.get(record['category_id'])
                if category is None or os.path.abspath(category.path).casefold() != record['root'].casefold():
                    raise ValueError('Cleanup category changed.')
                if self._running(category.process_names):
                    raise ValueError('The browser started after the preview. Close it and scan again.')
                self._delete_exact(record)
                result['deleted_count'] += 1
                result['deleted_bytes'] += record['size']
                result['files'].append({'path': record['path'], 'status': 'deleted'})
            except (OSError, ValueError) as error:
                result['skipped_count'] += 1
                result['errors'].append(f'{record["path"]}: {error}')
        result['log_path'] = str(log_path)
        log_path.write_text(json.dumps({'state': 'completed', 'plan_id': plan['id'], 'result': result}, indent=2), encoding='utf-8')
        return result




