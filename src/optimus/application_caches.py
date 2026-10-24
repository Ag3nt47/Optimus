"""Opt-in, bounded cleanup of exact rebuildable application web-cache leaves.

Account databases, media/offline downloads, Steam games and shader caches are
outside these recipes. Unknown process state preserves the affected scope.
"""
from __future__ import annotations

from dataclasses import dataclass
import copy
import hashlib
import json
import math
import os
from pathlib import Path
import re
import stat
import sys
import threading
import time
import uuid

import psutil

from .cleaner import Cleaner, REPARSE, OFFLINE, RECALL, SYSTEM, READONLY
from .filesystem import _ordinary_data_path
from .retention_cleanup import _known_folder


DEFAULTS = {'enabled': False, 'apps': [], 'min_age_days': 7,
            'max_entries': 5000, 'max_bytes': 2 * 1024**3, 'max_seconds': 10}
APP_NAMES = {'discord': 'Discord', 'spotify': 'Spotify', 'steam': 'Steam', 'teams': 'Microsoft Teams'}
_BAD_FLAGS = REPARSE | OFFLINE | RECALL | SYSTEM | READONLY
_CACHE_NAME = re.compile(r'(?:data_\d+|f_[a-f0-9]+|index|the-real-index|[a-f0-9]{8,}(?:_\d+)?)', re.I)
_CACHE_DIRS = frozenset(('cache_data', 'index-dir', 'js', 'wasm'))


def validate_configuration(value):
    if not isinstance(value, dict) or set(value) - DEFAULTS.keys():
        raise ValueError('Use the supported application-cache settings.')
    result = copy.deepcopy(DEFAULTS)
    result.update(copy.deepcopy(value))
    if type(result['enabled']) is not bool:
        raise ValueError('Application-cache cleanup must be explicitly enabled or disabled.')
    apps = result['apps']
    if (not isinstance(apps, list) or len(apps) > 4 or any(type(app) is not str or app not in APP_NAMES for app in apps)
            or len(set(apps)) != len(apps)):
        raise ValueError('Choose exact supported application-cache identities.')
    for key, low, high in (('min_age_days', 1, 3650), ('max_entries', 1, 50000),
                            ('max_bytes', 1, 8 * 1024**3), ('max_seconds', 1, 60)):
        if type(result[key]) is not int or not low <= result[key] <= high:
            raise ValueError('Application-cache ' + key + ' is outside its safe bound.')
    result['apps'] = sorted(apps)
    return result


@dataclass(frozen=True)
class CacheRecipe:
    id: str
    app: str
    root: Path
    process_names: tuple[str, ...]
    description: str = 'Rebuildable web cache only; accounts, personal data and offline downloads are preserved.'


def discover_recipes(*, system=None, home=None):
    """Fixed native-user locations; arbitrary settings paths are never accepted."""
    system, home = system or sys.platform, Path(home) if home is not None else Path.home()
    if system == 'win32':
        local = _known_folder('F1B32785-6FBA-4FCF-9D55-7B8E7F157091')
        roaming = _known_folder('3EB685DB-65F9-4CF6-A03A-E3EF65729F3D')
        bases = [('discord', roaming / 'discord', ('discord.exe',)),
                 ('spotify', local / 'Spotify/Browser', ('spotify.exe',)),
                 ('steam', local / 'Steam/htmlcache', ('steam.exe', 'steamwebhelper.exe')),
                 ('teams', roaming / 'Microsoft/Teams', ('teams.exe', 'ms-teams.exe', 'msteams.exe', 'msedgewebview2.exe')),
                 ('teams', local / 'Packages/MSTeams_8wekyb3d8bbwe/LocalCache/Microsoft/MSTeams/EBWebView/Default',
                  ('teams.exe', 'ms-teams.exe', 'msteams.exe', 'msedgewebview2.exe'))]
    elif system == 'darwin':
        support, caches = home / 'Library/Application Support', home / 'Library/Caches'
        bases = [('discord', support / 'discord', ('Discord', 'Discord Helper', 'Discord Helper (Renderer)')),
                 ('spotify', caches / 'com.spotify.client/Browser', ('Spotify', 'Spotify Helper')),
                 ('steam', support / 'Steam/config/htmlcache', ('steam_osx', 'steam', 'steamwebhelper')),
                 ('teams', support / 'Microsoft/Teams', ('Microsoft Teams', 'Microsoft Teams Helper', 'MSTeams'))]
    elif system == 'linux':
        # Deliberately do not trust inherited XDG environment variables as deletion authority.
        bases = [('discord', home / '.config/discord', ('discord', 'Discord')),
                 ('spotify', home / '.cache/spotify/Browser', ('spotify',)),
                 ('steam', home / '.local/share/Steam/config/htmlcache', ('steam', 'steamwebhelper'))]
    else:
        return []
    result = []
    for app, base, names in bases:
        for profile in (base, base / 'Default'):
            for leaf in ('Cache', 'Code Cache'):
                root = profile / leaf
                identity = app + ':' + hashlib.sha256(str(root).encode()).hexdigest()[:16]
                result.append(CacheRecipe(identity, app, root, names))
    return result


def _windows_process_names():
    """Toolhelp exposes image names even for Registry/protected system processes.

    psutil name lookup can be denied for those processes under a normal account;
    treating their blank names as app inactivity would be unsafe, while always
    failing that inventory would make supported cache cleanup permanently inert.
    """
    import ctypes
    from ctypes import wintypes
    class ProcessEntry(ctypes.Structure):
        _fields_ = [('dwSize', wintypes.DWORD), ('cntUsage', wintypes.DWORD),
                    ('th32ProcessID', wintypes.DWORD), ('th32DefaultHeapID', ctypes.c_size_t),
                    ('th32ModuleID', wintypes.DWORD), ('cntThreads', wintypes.DWORD),
                    ('th32ParentProcessID', wintypes.DWORD), ('pcPriClassBase', wintypes.LONG),
                    ('dwFlags', wintypes.DWORD), ('szExeFile', wintypes.WCHAR * 260)]
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.CreateToolhelp32Snapshot.argtypes, kernel.CreateToolhelp32Snapshot.restype = [wintypes.DWORD, wintypes.DWORD], wintypes.HANDLE
    kernel.Process32FirstW.argtypes, kernel.Process32FirstW.restype = [wintypes.HANDLE, ctypes.POINTER(ProcessEntry)], wintypes.BOOL
    kernel.Process32NextW.argtypes, kernel.Process32NextW.restype = [wintypes.HANDLE, ctypes.POINTER(ProcessEntry)], wintypes.BOOL
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    handle = kernel.CreateToolhelp32Snapshot(2, 0)
    if handle in (None, ctypes.c_void_p(-1).value):
        raise ctypes.WinError(ctypes.get_last_error())
    names, seen, entry, deadline = [], set(), ProcessEntry(), time.monotonic() + 3
    entry.dwSize = ctypes.sizeof(entry)
    try:
        available = kernel.Process32FirstW(handle, ctypes.byref(entry))
        while available:
            if len(seen) >= 20000 or time.monotonic() >= deadline or entry.th32ProcessID in seen or not entry.szExeFile:
                return names, False
            names.append(entry.szExeFile)
            seen.add(entry.th32ProcessID)
            available = kernel.Process32NextW(handle, ctypes.byref(entry))
        return names, bool(names) and ctypes.get_last_error() == 18
    finally:
        kernel.CloseHandle(handle)


def process_state(recipe):
    wanted, active, observed = {name.casefold() for name in recipe.process_names}, [], 0
    deadline = time.monotonic() + 3
    try:
        if sys.platform == 'win32':
            names, complete = _windows_process_names()
            active = sorted({name for name in names if name.casefold() in wanted})
            return {'complete': complete is True, 'active': bool(active), 'processes': active}
        for index, process in enumerate(psutil.process_iter(['name'], ad_value=None)):
            observed += 1
            if index >= 20000 or time.monotonic() >= deadline:
                return {'complete': False, 'active': bool(active), 'processes': active}
            name = process.info.get('name')
            if not isinstance(name, str) or not name:
                return {'complete': False, 'active': bool(active), 'processes': active}
            if name.casefold() in wanted or (sys.platform == 'darwin' and any(
                    name.casefold().startswith(allowed + ' (') for allowed in wanted if allowed.endswith('helper'))):
                active.append(name)
    except (psutil.Error, OSError, AttributeError, NotImplementedError):
        return {'complete': False, 'active': bool(active), 'processes': active}
    return {'complete': observed > 0, 'active': bool(active), 'processes': sorted(set(active))}


def _ordinary_root(root):
    root = Path(root)
    text = str(root)
    if (not root.is_absolute() or root == root.parent or '..' in root.parts
            or text.startswith(('\\\\', '//', '\\\\?\\', '\\\\.\\'))):
        raise ValueError('Choose an ordinary local directory below a drive root.')
    _ordinary_data_path(root)
    info = root.lstat()
    for ancestor in (root, *root.parents):
        if getattr(ancestor.lstat(), 'st_file_attributes', 0) & (REPARSE | OFFLINE | RECALL):
            raise ValueError('Cloud placeholders and indirect directory ancestry are excluded.')
    if not stat.S_ISDIR(info.st_mode) or getattr(info, 'st_file_attributes', 0) & _BAD_FLAGS:
        raise ValueError('Linked, cloud, system or read-only directories are excluded.')
    if os.name != 'nt' and info.st_uid != os.getuid():
        raise ValueError('The directory belongs to another user.')
    if os.name == 'nt':
        import ctypes
        drive_type = ctypes.WinDLL('kernel32', use_last_error=True).GetDriveTypeW
        drive_type.argtypes, drive_type.restype = [ctypes.c_wchar_p], ctypes.c_uint
        if drive_type(root.anchor) not in (2, 3):
            raise ValueError('Network, optical and unknown drive types are excluded.')
    else:
        partitions = [partition for partition in psutil.disk_partitions(all=True)
                      if Path(partition.mountpoint) == root or Path(partition.mountpoint) in root.parents]
        if not partitions:
            raise ValueError('The local filesystem identity is unknown.')
        filesystem = max(partitions, key=lambda item: len(Path(item.mountpoint).parts)).fstype.casefold()
        if any(token in filesystem for token in ('nfs', 'cifs', 'smb', 'sshfs', 'fuse', '9p', 'afs', 'davfs')):
            raise ValueError('Network and user-space cloud filesystems are excluded.')
    return root


def _identity(path, root, *, cutoff=None, opened=()):
    info = path.lstat()
    if (not Cleaner._safe_path(path, root) or not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
            or getattr(info, 'st_file_attributes', 0) & _BAD_FLAGS
            or os.name != 'nt' and info.st_uid != os.getuid()
            or Cleaner._coordination_file(path)
            or cutoff is not None and max(info.st_mtime, info.st_ctime) >= cutoff
            or os.path.normcase(os.path.realpath(path)) in opened):
        return None
    return {'path': str(path), 'root': str(root), 'size': info.st_size,
            'modified_ns': info.st_mtime_ns, 'created_ns': info.st_ctime_ns,
            'inode': info.st_ino, 'device': info.st_dev}


def _emit(callback, identity, phase, percent, description, **totals):
    if callback is None:
        return
    try:
        callback({'item_progress': {'key': identity, 'label': 'Application web caches', 'phase': phase, 'percent': percent},
                  'description': description[:1500], 'progress_only': True, **totals})
    except Exception:
        pass


def _audit(path, value):
    from .speed_enhancements import _private_json
    _ordinary_data_path(path.parent)
    _ordinary_data_path(path, file=True)
    _private_json(path, value)


class ApplicationCacheCleaner:
    def __init__(self, data_dir, *, recipe_provider=discover_recipes, process_reader=process_state,
                 open_reader=None, scope_reader=None, delete=None, clock=time.time):
        self.data_dir, self.recipe_provider, self.process_reader = Path(data_dir), recipe_provider, process_reader
        self.open_reader, self.scope_reader = open_reader or Cleaner._open_files_snapshot, scope_reader
        self.require_open_check = os.name != 'nt' or open_reader is not None
        self.delete, self.clock = delete or Cleaner._delete_exact, clock
        self._plans, self._lock = {}, threading.RLock()

    def categories(self):
        return [{'id': recipe.id, 'app': recipe.app, 'name': APP_NAMES.get(recipe.app, recipe.app),
                 'path': str(recipe.root), 'available': recipe.root.is_dir(), 'description': recipe.description}
                for recipe in self.recipe_provider()]

    def _choices_current(self, config):
        try:
            return self.scope_reader is None or validate_configuration(self.scope_reader()) == config
        except Exception:
            return False

    def _safe(self, recipe):
        try:
            state = self.process_reader(recipe)
            return isinstance(state, dict) and state.get('complete') is True and state.get('active') is False
        except Exception:
            return False

    def scan(self, config, *, progress_callback=None, cancel_event=None):
        config = validate_configuration(config)
        started = self.clock()
        if type(started) not in (int, float) or not math.isfinite(started):
            raise ValueError('The cleanup clock is unavailable.')
        plan = {'schema': 1, 'id': uuid.uuid4().hex, 'created_at': started, 'config': config,
                'files': [], 'total_bytes': 0, 'examined': 0, 'complete': True, 'skipped': [], 'roots': [],
                'note': 'Exact rebuildable web cache entries only. Accounts, history databases, downloads, game files and shader caches are excluded.'}
        if not config['enabled'] or not self._choices_current(config):
            return dict(plan, disabled=not config['enabled'], complete=False, reason='Cleanup is disabled or saved choices changed.')
        cutoff, deadline = started - config['min_age_days'] * 86400, time.monotonic() + config['max_seconds']
        try:
            recipes = self.recipe_provider()
            if (not isinstance(recipes, list) or len(recipes) > 64 or any(not isinstance(item, CacheRecipe) for item in recipes)
                    or len({recipe.id for recipe in recipes}) != len(recipes)):
                raise ValueError('Unsupported application-cache recipe inventory.')
        except (OSError, ValueError, TypeError) as error:
            return dict(plan, complete=False, reason=str(error))
        try:
            opened, open_complete = self.open_reader() if self.require_open_check else (set(), True)
        except (OSError, psutil.Error):
            opened, open_complete = set(), False
        if not open_complete:
            return dict(plan, complete=False, reason='Open-file state could not be verified; nothing was approved.')
        seen = set()
        _emit(progress_callback, plan['id'], 'scan', None, 'Checking exact application web-cache recipes.')
        for recipe in recipes:
            if recipe.app not in config['apps']:
                continue
            if cancel_event is not None and cancel_event.is_set():
                plan.update(complete=False, cancelled=True)
                break
            if not self._safe(recipe):
                plan['skipped'].append({'app': recipe.app, 'reason': 'Application is active or process state is unknown.'})
                continue
            try:
                root = _ordinary_root(recipe.root)
            except FileNotFoundError:
                continue
            except (OSError, ValueError) as error:
                plan['skipped'].append({'app': recipe.app, 'reason': str(error)[:500]})
                continue
            plan['roots'].append(str(root))
            stack = [root]
            while stack:
                if plan['examined'] >= config['max_entries'] or time.monotonic() >= deadline:
                    plan['complete'] = False
                    break
                directory = stack.pop()
                try:
                    if directory != root and not Cleaner._safe_path(directory, root):
                        continue
                    with os.scandir(directory) as entries:
                        for entry in entries:
                            plan['examined'] += 1
                            if plan['examined'] > config['max_entries'] or time.monotonic() >= deadline:
                                plan['complete'] = False
                                break
                            if cancel_event is not None and cancel_event.is_set():
                                plan.update(complete=False, cancelled=True)
                                break
                            path = Path(entry.path)
                            info = entry.stat(follow_symlinks=False)
                            if entry.is_symlink() or getattr(info, 'st_file_attributes', 0) & _BAD_FLAGS:
                                continue
                            if entry.is_dir(follow_symlinks=False):
                                if entry.name.casefold() in _CACHE_DIRS and len(path.relative_to(root).parts) <= 2:
                                    stack.append(path)
                                continue
                            if not _CACHE_NAME.fullmatch(entry.name):
                                continue
                            record = _identity(path, root, cutoff=cutoff, opened=opened)
                            key = os.path.normcase(os.path.abspath(path))
                            if record is None or key in seen:
                                continue
                            if plan['total_bytes'] + record['size'] > config['max_bytes']:
                                plan['complete'] = False
                                continue
                            seen.add(key)
                            plan['files'].append(dict(record, recipe_id=recipe.id, app=recipe.app))
                            plan['total_bytes'] += record['size']
                except (OSError, ValueError) as error:
                    plan['complete'] = False
                    plan['skipped'].append({'path': str(directory), 'reason': str(error)[:500]})
            if not plan['complete']:
                break
        with self._lock:
            for old in list(self._plans):
                if started - self._plans[old]['created_at'] > 900:
                    self._plans.pop(old, None)
            while len(self._plans) >= 2:
                self._plans.pop(next(iter(self._plans)))
            self._plans[plan['id']] = copy.deepcopy(plan)
        plan['availability'] = {app: {'supported': any(recipe.app == app for recipe in recipes),
                                      'available': any(recipe.app == app and str(recipe.root) in plan['roots'] for recipe in recipes)}
                                for app in config['apps']}
        _emit(progress_callback, plan['id'], 'scan', 100 if plan['complete'] else None,
              f'{len(plan["files"])} exact cache entries eligible; {plan["total_bytes"]} bytes previewed.')
        return plan

    def apply(self, displayed_plan, config, *, progress_callback=None, cancel_event=None):
        config = validate_configuration(config)
        with self._lock:
            plan = self._plans.pop(str(displayed_plan.get('id', '')), None)
        if plan is None or self.clock() - plan['created_at'] > 900 or self.clock() < plan['created_at']:
            raise ValueError('The application-cache preview expired; scan again.')
        if config != plan['config'] or not config['enabled'] or not self._choices_current(config):
            raise ValueError('Saved application-cache choices changed; scan again.')
        result = {'schema': 1, 'id': plan['id'], 'deleted_count': 0, 'deleted_bytes': 0,
                  'skipped_count': 0, 'complete': plan['complete'], 'items': [], 'files': [], 'errors': []}
        journal = self.data_dir / 'application-cache-cleanup' / (plan['id'] + '.json')
        _audit(journal, {'schema': 1, 'state': 'prepared', 'plan': plan})
        deadline, cutoff = time.monotonic() + config['max_seconds'], self.clock() - config['min_age_days'] * 86400
        total = len(plan['files'])
        last_progress_at = None
        for index, record in enumerate(plan['files']):
            if (time.monotonic() >= deadline or not self._choices_current(config)
                    or cancel_event is not None and cancel_event.is_set()):
                result.update(complete=False, cancelled=bool(cancel_event is not None and cancel_event.is_set()))
                result['skipped_count'] += total - index
                break
            try:
                # Re-discover native authority and process state for every exact removal.
                choices = {recipe.id: recipe for recipe in self.recipe_provider()}
                recipe = choices.get(record['recipe_id'])
                if recipe is None or recipe.app not in config['apps'] or str(recipe.root) != record['root']:
                    raise ValueError('The application-cache recipe changed.')
                _ordinary_root(recipe.root)
                if not self._safe(recipe):
                    raise ValueError('The application started or process state became unknown.')
                opened, complete = self.open_reader() if self.require_open_check else (set(), True)
                if not complete or _identity(Path(record['path']), recipe.root, cutoff=cutoff, opened=opened) is None:
                    raise ValueError('The file became recent, open or protected.')
                self.delete(record)
                result['deleted_count'] += 1
                result['deleted_bytes'] += record['size']
                result['items'].append({'path': record['path'], 'status': 'deleted', 'bytes': record['size']})
                result['files'].append({'path': record['path'], 'status': 'deleted',
                    'identity': {key: record[key] for key in ('device', 'inode', 'size', 'modified_ns', 'created_ns')}})
            except (OSError, ValueError, TypeError) as error:
                result['skipped_count'] += 1
                result['items'].append({'path': record['path'], 'status': 'preserved', 'reason': str(error)[:500]})
            now = time.monotonic()
            if last_progress_at is None or now - last_progress_at >= 0.1 or index + 1 == total:
                last_progress_at = now
                _emit(progress_callback, plan['id'], 'cleanup', (index + 1) * 100 / max(total, 1),
                      f'{index + 1}/{total} cache entries checked.', deleted_count=result['deleted_count'], deleted_bytes=result['deleted_bytes'])
        try:
            _audit(journal, {'schema': 1, 'state': 'completed', 'result': result})
        except (OSError, ValueError) as error:
            result.update(complete=False, journal_error=str(error)[:500])
        result['log_path'] = str(journal)
        return result

    def run(self, config, *, progress_callback=None, cancel_event=None):
        plan = self.scan(config, progress_callback=progress_callback, cancel_event=cancel_event)
        if plan.get('disabled') or 'reason' in plan or plan.get('cancelled'):
            return dict(plan, deleted_count=0, deleted_bytes=0, skipped=True)
        return self.apply(plan, config, progress_callback=progress_callback, cancel_event=cancel_event)
