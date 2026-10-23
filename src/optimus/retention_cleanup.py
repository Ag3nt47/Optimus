"""Independently approved retention for narrow disposable file scopes.

Only exact inactive Angerona diagnostics and recognizable incomplete download
files are eligible. This module never recursively purges Downloads, an Angerona
checkout, security evidence, application state, or a complete download.
"""
from __future__ import annotations

from dataclasses import dataclass
import ctypes
import json
import math
import os
from pathlib import Path
import re
import stat
import sys
import time
import uuid

import psutil

from .cleaner import Cleaner, REPARSE, OFFLINE, RECALL, SYSTEM, READONLY
from .browser_paths import browser_definitions, process_matches
from .filesystem import _ordinary_data_path
from .retention_configuration import validate_retention_scope


_LOG_NAMES = ('startup-preflight.log', 'startup-preflight.previous.log',
              'startup-dashboard.log', 'startup-dashboard.previous.log',
              'blackbox.log', 'crash.log', 'uiwatchdog.log')
_INCOMPLETE_SUFFIXES = ('.crdownload', '.part', '.partial')
_DOWNLOAD_PROCESSES = frozenset(('chrome', 'chrome.exe', 'msedge', 'msedge.exe',
    'firefox', 'firefox.exe', 'brave', 'brave.exe', 'vivaldi', 'vivaldi.exe',
    'opera', 'opera.exe', 'chromium', 'chromium.exe', 'safari', 'curl', 'curl.exe',
    'wget', 'wget.exe', 'aria2c', 'aria2c.exe', 'qbittorrent', 'qbittorrent.exe',
    'transmission', 'deluge', 'idm.exe', 'fdm.exe', 'msedgewebview2.exe',
    'com.apple.webkit.networking', 'safari networking'))
_MAX_FILES, _MAX_BYTES, _MAX_SECONDS = 5000, 2 * 1024 ** 3, 15


@dataclass(frozen=True)
class RetentionScope:
    identity: str
    root: Path
    names: tuple[str, ...] = ()
    suffixes: tuple[str, ...] = ()


def _known_folder(identifier):
    """Use native Shell authority rather than inherited Windows path variables."""
    from ctypes import wintypes
    guid = (ctypes.c_byte * 16).from_buffer_copy(uuid.UUID(identifier).bytes_le)
    output = ctypes.c_void_p()
    shell = ctypes.WinDLL('shell32', use_last_error=True)
    shell.SHGetKnownFolderPath.argtypes = [ctypes.c_void_p, wintypes.DWORD, wintypes.HANDLE,
                                          ctypes.POINTER(ctypes.c_void_p)]
    shell.SHGetKnownFolderPath.restype = ctypes.c_long
    result = shell.SHGetKnownFolderPath(ctypes.byref(guid), 0, None, ctypes.byref(output))
    if result != 0 or not output.value:
        raise OSError('The native user-folder identity could not be verified.')
    try:
        return Path(ctypes.wstring_at(output))
    finally:
        free = ctypes.WinDLL('ole32').CoTaskMemFree
        free.argtypes = [ctypes.c_void_p]
        free(output)


def _read_authority(path):
    info = _ordinary_data_path(path, file=True)
    if info is None:
        return None
    if info.st_size > 4096:
        raise ValueError('The Angerona cleanup scope authority exceeds its bound.')
    raw = path.read_bytes()
    after = _ordinary_data_path(path, file=True)
    if after is None or len(raw) > 4096 or (info.st_dev, info.st_ino, info.st_mtime_ns) != (
            after.st_dev, after.st_ino, after.st_mtime_ns):
        raise ValueError('The Angerona cleanup scope authority changed while reading.')
    value = json.loads(raw.decode('utf-8-sig'))
    from .speed_enhancements import _current_owner
    owner = _current_owner()
    if (not isinstance(value, dict) or type(value.get('schema')) is not int or value['schema'] != 1
            or value.get('owner_sid') != owner.removeprefix('sid:')
            or not isinstance(value.get('installation_id'), str)
            or re.fullmatch('[a-f0-9]{32}', value['installation_id']) is None):
        raise ValueError('The Angerona cleanup scope authority has unknown ownership.')
    installation = Path(value.get('install_directory', ''))
    repository = Path(value.get('repository_root', ''))
    if (not installation.is_absolute() or installation != path.parent
            or installation.name.casefold() != 'angeronacleanup' or not repository.is_absolute()
            or repository.name.casefold() != 'angeronasuite'):
        raise ValueError('The Angerona cleanup scope authority names an unexpected installation.')
    _ordinary_data_path(repository)
    marker = _ordinary_data_path(repository / 'src/angerona/app.py', file=True)
    if marker is None or marker.st_size > 2 * 1024 ** 2:
        raise ValueError('The exact Angerona source installation could not be verified.')
    return repository.parent / 'AngeronaData'


def discover_scopes(kind, data_dir, *, system=None, home=None):
    """Select fixed native/profile roots; settings never supply arbitrary paths."""
    system, home = system or sys.platform, Path(home) if home is not None else Path.home()
    if kind == 'failed_downloads':
        if system == 'win32':
            root = _known_folder('374DE290-123F-4565-9164-39C4925E467B')
        else:
            root = home / 'Downloads'
            configuration = home / '.config/user-dirs.dirs'
            if system == 'linux' and configuration.exists():
                info = _ordinary_data_path(configuration, file=True)
                if info is None or info.st_size > 16384:
                    raise ValueError('The native Downloads folder configuration is unverified.')
                if os.name != 'nt' and (info.st_uid != os.getuid() or info.st_mode & 0o022):
                    raise ValueError('The native Downloads folder configuration has unsafe ownership or write permissions.')
                text = configuration.read_text(encoding='utf-8')
                match = re.search(r'^XDG_DOWNLOAD_DIR="([^"\r\n]+)"\s*$', text, re.M)
                if match:
                    value = match[1]
                    if value.startswith('$HOME/'):
                        root = home / value[6:]
                    elif value.startswith('/') and '$' not in value and '`' not in value:
                        root = Path(value)
                    else:
                        raise ValueError('The native Downloads folder configuration is unsupported.')
                if root == home or '..' in root.parts:
                    raise ValueError('A Downloads cleanup scope cannot be the home directory or use traversal.')
        return [RetentionScope('incomplete-browser-downloads', root, suffixes=_INCOMPLETE_SUFFIXES)]
    if kind != 'angerona_logs':
        raise ValueError('Choose a supported independent file-retention scope.')
    if system != 'win32':
        return []
    local = _known_folder('F1B32785-6FBA-4FCF-9D55-7B8E7F157091')
    profiles = [local / 'Angerona/SourceData', local / 'Angerona/Startup']
    authorities = (Path(data_dir).parent.parent / 'AngeronaCleanup/scope-authority.json',
                   local / 'AngeronaCleanup/scope-authority.json')
    for authority in dict.fromkeys(authorities):
        profile = _read_authority(authority)
        if profile is not None:
            profiles.append(profile)
    # This is Angerona's documented packaged default, never a drive-wide scan.
    profiles.append(Path('D:/AngeronaData'))
    scopes = []
    for index, profile in enumerate(dict.fromkeys(profiles)):
        scopes.extend((RetentionScope('angerona-diagnostics-' + str(index), profile / 'logs', names=_LOG_NAMES),
                       RetentionScope('angerona-crash-' + str(index), profile / 'diagnostics', names=('crash.log',))))
    return scopes


def process_state(kind):
    """An unavailable relevant process inventory preserves the entire scope."""
    complete, active, observed = True, [], 0
    deadline = time.monotonic() + 3
    try:
        for index, process in enumerate(psutil.process_iter(['pid', 'name', 'cmdline', 'exe', 'cwd'], ad_value=None)):
            observed += 1
            if index >= 20000 or time.monotonic() >= deadline:
                complete = False
                break
            name = process.info.get('name')
            if not isinstance(name, str) or not name:
                complete = False
                continue
            original_name, name = name, name.casefold()
            if kind == 'failed_downloads' and (name in _DOWNLOAD_PROCESSES
                    or any(process_matches(browser, original_name) for browser in browser_definitions())):
                active.append(name)
            if kind == 'angerona_logs':
                if name.startswith('angerona'):
                    active.append(name)
                elif name.startswith(('python', 'pypy', 'py.exe', 'pyw.exe')):
                    arguments = process.info.get('cmdline')
                    executable, working = process.info.get('exe'), process.info.get('cwd')
                    if not isinstance(arguments, list) or not arguments:
                        complete = False
                    elif any('angerona' in str(argument).casefold() for argument in arguments[1:]):
                        active.append(name)
                    elif isinstance(working, str) and 'angeronasuite' in working.casefold():
                        active.append(name)
                    elif isinstance(executable, str) and 'angeronasuite' in executable.casefold() and not isinstance(working, str):
                        # Optimus may share this interpreter. Its executable path
                        # alone is not evidence that Angerona is running, while
                        # an unreadable relative-script working directory is not
                        # evidence that the source runtime is inactive.
                        complete = False
                elif name in ('powershell.exe', 'pwsh.exe', 'cmd.exe'):
                    arguments = process.info.get('cmdline')
                    if isinstance(arguments, list) and any('angeronasuite' in str(argument).casefold()
                            or 'start-angerona' in str(argument).casefold() for argument in arguments[1:]):
                        active.append(name)
    except (psutil.Error, OSError, AttributeError, NotImplementedError):
        complete = False
    return {'complete': complete and observed > 0, 'active': bool(active), 'processes': sorted(set(active))}


def _file_record(path, root, cutoff, opened):
    info = path.lstat()
    if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
            or getattr(info, 'st_file_attributes', 0) & (REPARSE | OFFLINE | RECALL | SYSTEM | READONLY)
            or max(info.st_mtime, info.st_ctime) >= cutoff
            or os.name != 'nt' and info.st_uid != os.getuid()
            or os.path.normcase(os.path.realpath(path)) in opened or not Cleaner._safe_path(path, root)):
        return None
    return {'path': str(path), 'root': str(root), 'size': info.st_size,
            'modified_ns': info.st_mtime_ns, 'created_ns': info.st_ctime_ns,
            'inode': info.st_ino, 'device': info.st_dev}


class RetentionCleanup:
    def __init__(self, data_dir, *, scope_provider=discover_scopes, process_reader=process_state,
                 open_reader=None, delete=None, scope_reader=None, clock=time.time):
        self.data_dir = Path(data_dir)
        self.scope_provider, self.process_reader, self.scope_reader = scope_provider, process_reader, scope_reader
        self.open_reader = open_reader or Cleaner._open_files_snapshot
        self.require_open_check = os.name != 'nt' or open_reader is not None
        self.delete, self.clock = delete or Cleaner._delete_exact, clock

    def run(self, kind, settings, *, progress_callback=None, cancel_event=None):
        validate_retention_scope(settings)
        if kind not in ('angerona_logs', 'failed_downloads'):
            raise ValueError('Choose a supported independent file-retention scope.')
        if progress_callback is not None and not callable(progress_callback):
            raise ValueError('An optional retention progress callback must be callable.')
        identity, started = uuid.uuid4().hex, self.clock()
        if type(started) not in (int, float) or not math.isfinite(started):
            raise ValueError('The retention clock is unavailable.')
        days = settings.get(kind + '_retention_days', 7)
        report = {'schema': 1, 'cleanup_id': identity, 'kind': kind, 'retention_days': days,
                  'started_at': started, 'deleted_count': 0, 'deleted_bytes': 0, 'skipped_count': 0,
                  'failed_items': 0, 'items': [], 'roots': [], 'complete': True, 'cancelled': False,
                  'note': 'Only individually verified inactive disposable files are removed. Empty folders, completed downloads, Angerona security evidence and application state are preserved.'}
        if settings.get(kind + '_cleanup_enabled') is not True:
            return dict(report, skipped=True, disabled=True)

        def cancelled():
            return cancel_event is not None and cancel_event.is_set()

        def emit(phase, percent, description):
            if callable(progress_callback):
                try:
                    progress_callback({'item_progress': {'key': 'cleanup:' + identity, 'label': kind.replace('_', ' ').capitalize(),
                        'phase': phase, 'percent': percent}, 'deleted_bytes': report['deleted_bytes'],
                        'deleted_count': report['deleted_count'], 'description': description, 'progress_only': True})
                except Exception:
                    pass

        last_check, last_safe = None, False

        def safe_now(*, force=False):
            nonlocal last_check, last_safe
            if cancelled():
                return False
            now = time.monotonic()
            if self.scope_reader is not None:
                current = self.scope_reader()
                if (not isinstance(current, dict) or current.get(kind + '_cleanup_enabled') is not True
                        or type(current.get(kind + '_retention_days')) is not int
                        or current[kind + '_retention_days'] != days):
                    last_safe = False
                    return False
            if not force and last_check is not None and now - last_check < 1:
                return last_safe
            last_check = now
            try:
                state = self.process_reader(kind)
            except Exception:
                last_safe = False
                return False
            last_safe = isinstance(state, dict) and state.get('complete') is True and state.get('active') is False
            return last_safe

        emit('scan', None, 'Checking supported locations and process inactivity before retention cleanup.')
        if not safe_now():
            return dict(report, skipped=True, complete=False, cancelled=cancelled(),
                        reason='Relevant programs are active, process state is unknown, choices changed, or cancellation was requested; nothing was removed.')
        try:
            scopes = self.scope_provider(kind, self.data_dir)
        except (OSError, ValueError, TypeError) as error:
            return dict(report, skipped=True, complete=False, failed_items=1,
                        reason='Supported retention locations could not be verified: ' + str(error)[:500])
        if not isinstance(scopes, list) or len(scopes) > 16 or any(not isinstance(scope, RetentionScope) for scope in scopes):
            return dict(report, skipped=True, complete=False, failed_items=1,
                        reason='The supported retention scope inventory is invalid; no files were deleted.')
        if not scopes:
            return dict(report, skipped=True, supported=False, reason='No verified retention locations are supported on this platform.')
        try:
            opened, open_complete = self.open_reader() if self.require_open_check else (set(), True)
        except (OSError, psutil.Error):
            opened, open_complete = set(), False
        if not open_complete:
            return dict(report, skipped=True, complete=False, reason='Open-file ownership could not be completely verified; retention cleanup was skipped.')
        open_checked_at = time.monotonic()
        cutoff, deadline, records, examined, total_bytes = started - days * 86400, time.monotonic() + _MAX_SECONDS, [], 0, 0
        seen = set()
        for scope in scopes:
            root = Path(scope.root)
            report['roots'].append(str(root))
            try:
                _ordinary_data_path(root)
                try:
                    info = root.lstat()
                except FileNotFoundError:
                    continue
                if not root.is_absolute() or not stat.S_ISDIR(info.st_mode) or root == root.parent:
                    raise ValueError('The retention root is not an ordinary concrete directory.')
                with os.scandir(root) as entries:
                    for entry in entries:
                        examined += 1
                        if examined > _MAX_FILES or time.monotonic() >= deadline:
                            report['complete'] = False
                            break
                        path = Path(entry.path)
                        if not ((scope.names and path.name.casefold() in scope.names)
                                or (scope.suffixes and path.suffix.casefold() in scope.suffixes and len(path.stem) > 0)):
                            continue
                        try:
                            record = _file_record(path, root, cutoff, opened)
                        except (OSError, ValueError) as error:
                            report['complete'] = False
                            report['failed_items'] += 1
                            report['items'].append({'path': str(path), 'status': 'preserved', 'reason': str(error)[:500]})
                            continue
                        key = os.path.normcase(os.path.abspath(path))
                        if record is None or key in seen:
                            report['skipped_count'] += 1
                            continue
                        if total_bytes + record['size'] > _MAX_BYTES:
                            report['complete'] = False
                            report['skipped_count'] += 1
                            continue
                        records.append(record)
                        seen.add(key)
                        total_bytes += record['size']
            except (OSError, ValueError) as error:
                report['complete'] = False
                report['failed_items'] += 1
                report['items'].append({'root': str(root), 'status': 'skipped', 'reason': str(error)[:500]})
            if examined > _MAX_FILES or time.monotonic() >= deadline:
                break
        from .speed_enhancements import _private_json
        journal = self.data_dir / 'retention-cleanup' / (identity + '.json')
        try:
            _ordinary_data_path(journal.parent)
            _ordinary_data_path(journal, file=True)
            _private_json(journal, {'schema': 1, 'state': 'prepared', 'kind': kind, 'retention_days': days,
                                    'started_at': started, 'files': records})
        except (OSError, ValueError) as error:
            return dict(report, skipped=True, complete=False, failed_items=report['failed_items'] + 1,
                        reason='The retention audit could not be prepared; no file was deleted.', journal_error=str(error)[:500])
        emit('cleanup', 0, f'Checking {len(records)} aged disposable files; no preview size is counted as removed.')
        apply_deadline = time.monotonic() + 35
        for index, record in enumerate(records):
            if time.monotonic() >= apply_deadline or not safe_now(force=index == 0):
                report.update(complete=False, cancelled=cancelled())
                report['items'].append({'status': 'deferred', 'reason': 'Programs started, scope choices changed, or cancellation was requested; remaining files were preserved.'})
                break
            try:
                path = Path(record['path'])
                fresh = path.lstat()
                if max(fresh.st_mtime, fresh.st_ctime) >= cutoff:
                    raise ValueError('The file became too recent for this retention choice.')
                if self.require_open_check:
                    if index == 0 or time.monotonic() - open_checked_at >= 0.5:
                        opened, open_complete = self.open_reader()
                        open_checked_at = time.monotonic()
                    if not open_complete or os.path.normcase(os.path.realpath(path)) in opened:
                        raise ValueError('Open-file state is active or unknown; the file was preserved.')
                self.delete(record)
                report['deleted_count'] += 1
                report['deleted_bytes'] += record['size']
                report['items'].append({'path': record['path'], 'status': 'deleted', 'bytes': record['size']})
            except (OSError, ValueError) as error:
                report['skipped_count'] += 1
                report['items'].append({'path': record['path'], 'status': 'preserved', 'reason': str(error)[:500]})
            emit('cleanup', min(99, (index + 1) * 100 / max(1, len(records))),
                 f'{index + 1}/{len(records)} checked; {report["deleted_count"]} files removed.')
        report.update(finished_at=self.clock(), cancelled=cancelled(), log_path=str(journal))
        try:
            _ordinary_data_path(journal.parent)
            _ordinary_data_path(journal, file=True)
            _private_json(journal, {'schema': 1, 'state': 'completed', 'result': report})
        except (OSError, ValueError) as error:
            report.update(complete=False, failed_items=report['failed_items'] + 1,
                          journal_error=str(error)[:500], note='Verified removals are counted, but their final audit could not be saved; remaining work requires review.')
        emit('cleanup', 100 if report['complete'] else None,
             f'Retention results saved: {report["deleted_count"]} files removed; {report["skipped_count"]} preserved.')
        return report
