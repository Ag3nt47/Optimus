"""Explicit expiry of completed browser undo groups and exported report copies.

Live journals, driver exports, system restore, partial browser recovery and
pre-restore safety snapshots are never retention targets.
"""
from __future__ import annotations

from contextlib import contextmanager
import copy
from datetime import datetime, timezone
from functools import wraps
import hashlib
import json
import math
import os
from pathlib import Path
import re
import stat
import threading
import time
import uuid

from .application_caches import _audit, _identity, _ordinary_root
from .cleaner import Cleaner
from .filesystem import _ordinary_data_path
from .browser_paths import browser_definitions


DEFAULTS = {'enabled': False, 'min_age_days': 30, 'browser_backups': False,
            'lose_browser_undo_approved': False, 'exported_reports': True,
            'max_entries': 5000, 'max_bytes': 2 * 1024**3, 'max_seconds': 10}
_HEX = re.compile('[a-f0-9]{32}')
_HASH = re.compile('[a-f0-9]{64}')
_LOCKS, _LOCKS_GUARD = {}, threading.Lock()


class BrowserBackupBusy(ValueError):
    """A browser cleanup, restore or retention operation already owns the lock."""


@contextmanager
def browser_backup_lease(data_dir):
    root = Path(data_dir)
    _ordinary_data_path(root)
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    _ordinary_root(root)
    path = root / 'browser-backups.lock'
    with _LOCKS_GUARD:
        mutex = _LOCKS.setdefault(os.path.normcase(os.path.abspath(path)), threading.Lock())
    if not mutex.acquire(blocking=False):
        raise BrowserBackupBusy('A browser backup operation is already active; retry after it finishes.')
    descriptor, locked = None, False
    try:
        before = _ordinary_data_path(path, file=True)
        flags = os.O_RDWR | getattr(os, 'O_NOFOLLOW', 0)
        if before is None:
            try:
                descriptor = os.open(path, flags | os.O_CREAT | os.O_EXCL, 0o600)
                os.write(descriptor, b'0')
            except FileExistsError:
                before = _ordinary_data_path(path, file=True)
        if descriptor is None:
            descriptor = os.open(path, flags)
        current, opened = _ordinary_data_path(path, file=True), os.fstat(descriptor)
        if (current is None or not stat.S_ISREG(opened.st_mode) or opened.st_nlink != 1
                or (opened.st_dev, opened.st_ino) != (current.st_dev, current.st_ino)):
            raise ValueError('The browser backup lock identity changed.')
        if os.name == 'nt':
            import msvcrt
            os.lseek(descriptor, 0, os.SEEK_SET)
            try:
                msvcrt.locking(descriptor, msvcrt.LK_NBLCK, 1)
            except OSError as error:
                raise BrowserBackupBusy('Another browser backup operation is active; this operation made no changes.') from error
        else:
            import fcntl
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as error:
                raise BrowserBackupBusy('Another browser backup operation is active; this operation made no changes.') from error
        locked = True
        yield
    finally:
        if descriptor is not None:
            if locked:
                if os.name == 'nt':
                    import msvcrt
                    os.lseek(descriptor, 0, os.SEEK_SET)
                    msvcrt.locking(descriptor, msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(descriptor, fcntl.LOCK_UN)
            os.close(descriptor)
        mutex.release()


def browser_backup_operation(method):
    @wraps(method)
    def guarded(self, *args, **kwargs):
        with browser_backup_lease(self.data_dir):
            return method(self, *args, **kwargs)
    return guarded


def validate_configuration(value):
    if not isinstance(value, dict) or set(value) - DEFAULTS.keys():
        raise ValueError('Use the supported recovery-retention settings.')
    result = copy.deepcopy(DEFAULTS)
    result.update(copy.deepcopy(value))
    for key in ('enabled', 'browser_backups', 'lose_browser_undo_approved', 'exported_reports'):
        if type(result[key]) is not bool:
            raise ValueError('Recovery-retention choices must be explicit toggles.')
    if result['enabled'] and result['browser_backups'] and not result['lose_browser_undo_approved']:
        raise ValueError('Approve losing expired browser undo before enabling browser-backup expiry.')
    for key, low, high in (('min_age_days', 1, 3650), ('max_entries', 1, 50000),
                            ('max_bytes', 1, 8 * 1024**3), ('max_seconds', 1, 60)):
        if type(result[key]) is not int or not low <= result[key] <= high:
            raise ValueError('Recovery-retention ' + key + ' is outside its safe bound.')
    return result


def _read_json(path, *, maximum=1024**2):
    info = _ordinary_data_path(path, file=True)
    if info is None or info.st_size > maximum:
        raise ValueError('The recovery metadata is missing or exceeds its bound.')
    raw = path.read_bytes()
    after = _ordinary_data_path(path, file=True)
    if after is None or len(raw) > maximum or (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns) != (
            after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns):
        raise ValueError('The recovery metadata changed while reading.')
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise ValueError('The recovery metadata is unsupported.')
    return value


def _digest(path, deadline):
    result = hashlib.sha256()
    with path.open('rb') as stream:
        while True:
            if time.monotonic() >= deadline:
                raise ValueError('The bounded recovery verification timed out; the group was preserved.')
            block = stream.read(1024**2)
            if not block:
                return result.hexdigest()
            result.update(block)


def _validated_completed_report(report, kind):
    if (not isinstance(report, dict) or type(report.get('schema')) is not int or report['schema'] != 1
            or report.get('read_only') is not True or not isinstance(report.get('id'), str)
            or _HEX.fullmatch(report['id']) is None or report.get('cancelled') is True):
        raise ValueError('Only recognized completed read-only report copies can be archived.')
    if kind == 'storage_growth':
        roots = report.get('roots')
        if (report.get('complete') is not True or type(report.get('examined')) is not int or report['examined'] < 0
                or type(report.get('scanned_bytes')) is not int or report['scanned_bytes'] < 0
                or not isinstance(roots, list) or not 1 <= len(roots) <= 16
                or any(not isinstance(root, dict) or root.get('complete') is not True or root.get('error')
                       or not isinstance(root.get('root'), str) or type(root.get('bytes')) is not int
                       or root['bytes'] < 0 or type(root.get('file_count')) is not int or root['file_count'] < 0 for root in roots)):
            raise ValueError('Only complete selected-folder growth reports can be archived.')
    elif kind == 'health_check':
        window, diagnostics, headroom = report.get('resource_window'), report.get('diagnostics'), report.get('installer_headroom')
        if (type(report.get('maintenance_actions_executed')) is not int or report['maintenance_actions_executed'] != 0
                or not isinstance(window, dict) or window.get('complete') is not True
                or window.get('read_only') is not True or window.get('cancelled') is True
                or not isinstance(window.get('samples'), list) or not 1 <= len(window['samples']) <= 300
                or any(not isinstance(row, dict) for row in window['samples'])
                or not isinstance(diagnostics, dict) or diagnostics.get('read_only') is not True
                or diagnostics.get('status') != 'completed' or diagnostics.get('errors')
                or not isinstance(headroom, dict) or headroom.get('read_only') is not True or headroom.get('errors')):
            raise ValueError('Only completed read-only health checks without unresolved collection errors can be archived.')
    else:
        raise ValueError('Choose a supported completed read-only report kind.')
    return copy.deepcopy(report)


class RecoveryRetention:
    def __init__(self, data_dir, *, scope_reader=None, clock=time.time, delete=None):
        self.data_dir, self.scope_reader, self.clock = Path(data_dir), scope_reader, clock
        self.delete, self._plans, self._lock = delete or Cleaner._delete_exact, {}, threading.RLock()

    def _current(self, config):
        try:
            return self.scope_reader is None or validate_configuration(self.scope_reader()) == config
        except Exception:
            return False

    def archive_completed_report(self, report, *, kind=None):
        """Create an explicitly disposable copy; never move or expire its journal."""
        report = _validated_completed_report(report, kind)
        identity = uuid.uuid4().hex
        payload = {'schema': 1, 'kind': 'optimus-completed-report-export', 'complete': True,
                   'id': identity, 'created_at': self.clock(), 'report_kind': kind, 'report': report}
        if len(json.dumps(payload, allow_nan=False).encode()) > 1024**2:
            raise ValueError('The completed report exceeds the disposable export bound.')
        path = self.data_dir / 'ExportedReports' / (identity + '.json')
        _audit(path, payload)
        return str(path)

    def _browser_group(self, folder, cutoff, deadline, max_bytes):
        _ordinary_root(folder)
        if _HEX.fullmatch(folder.name) is None:
            raise ValueError('Safety snapshots and unknown folder identities are protected.')
        manifest_path = folder / 'manifest.json'
        manifest = _read_json(manifest_path)
        if (manifest.get('backupId') != folder.name or manifest.get('state') != 'applied'
                or manifest.get('restoreSafetyFolder') or manifest.get('restoredDatabases')
                or manifest.get('browser') not in {item['name'] for item in browser_definitions().values()}):
            raise ValueError('Only complete applied browser backups without restore activity are eligible.')
        try:
            created = datetime.strptime(manifest['createdUtc'], '%Y-%m-%dT%H:%M:%SZ').replace(tzinfo=timezone.utc).timestamp()
        except (KeyError, TypeError, ValueError) as error:
            raise ValueError('The browser-backup creation time is unverified.') from error
        if created >= cutoff:
            raise ValueError('The browser backup is within the selected retention period.')
        databases = manifest.get('databases')
        if not isinstance(databases, list) or not 1 <= len(databases) <= 32:
            raise ValueError('The browser-backup database inventory is unsupported.')
        filenames, records, total = set(), [], 0
        for database in databases:
            if (not isinstance(database, dict) or database.get('committed') is not True
                    or not isinstance(database.get('file'), str)
                    or re.fullmatch(r'database-\d+\.sqlite', database['file']) is None
                    or not isinstance(database.get('sha256'), str) or _HASH.fullmatch(database['sha256']) is None
                    or database['file'] in filenames):
                raise ValueError('A browser-backup database identity is unverified.')
            filenames.add(database['file'])
            path = folder / database['file']
            record = _identity(path, folder)
            if record is None or total + record['size'] > max_bytes:
                raise ValueError('The browser-backup group is protected or exceeds its byte bound.')
            if _digest(path, deadline) != database['sha256'] or _identity(path, folder) != record:
                raise ValueError('A browser-backup database changed; the whole group was preserved.')
            records.append(dict(record, sha256=database['sha256']))
            total += record['size']
        observed_names = set()
        with os.scandir(folder) as entries:
            for entry in entries:
                if time.monotonic() >= deadline or len(observed_names) >= 33 or entry.name not in filenames | {'manifest.json'}:
                    raise ValueError('Unexpected or unbounded files in a browser-backup group are protected.')
                observed_names.add(entry.name)
        if observed_names != filenames | {'manifest.json'}:
            raise ValueError('Unexpected files in a browser-backup group are protected.')
        manifest_record = _identity(manifest_path, folder)
        if manifest_record is None or total + manifest_record['size'] > max_bytes:
            raise ValueError('The browser-backup manifest is protected or exceeds its byte bound.')
        records.append(dict(manifest_record, sha256=_digest(manifest_path, deadline)))
        info = folder.lstat()
        return {'kind': 'browser_backup', 'id': folder.name, 'path': str(folder),
                'device': info.st_dev, 'inode': info.st_ino, 'files': records,
                'bytes': total + manifest_record['size'], 'created_at': created,
                'undo_loss': 'Permanently removes the original ' + ', '.join(str(value) for value in manifest.get('categories', []))
                             + ' database backup for this browser cleanup; that cleanup can no longer be undone.'}

    def preview(self, config, *, progress_callback=None, cancel_event=None):
        config = validate_configuration(config)
        plan = {'schema': 1, 'id': uuid.uuid4().hex, 'created_at': self.clock(), 'config': config,
                'eligible': [], 'protected': [], 'total_bytes': 0, 'protected_bytes': 0, 'examined': 0, 'complete': True,
                'note': 'Expiry permanently removes selected old undo backups. Active/partial recovery, pre-restore safety snapshots, driver exports, system restore points and update journals remain protected.'}
        cutoff, deadline = self.clock() - config['min_age_days'] * 86400, time.monotonic() + config['max_seconds']
        with browser_backup_lease(self.data_dir):
            for directory, kind, enabled in ((self.data_dir / 'BrowserPrivacyBackups', 'browser_backup', config['browser_backups']),
                                               (self.data_dir / 'ExportedReports', 'report_copy', config['exported_reports'])):
                try:
                    _ordinary_root(directory)
                except FileNotFoundError:
                    continue
                except (OSError, ValueError) as error:
                    plan['protected'].append({'path': str(directory), 'reason': str(error)[:500]})
                    continue
                with os.scandir(directory) as entries:
                    for entry in entries:
                        plan['examined'] += 1
                        if (plan['examined'] > config['max_entries'] or time.monotonic() >= deadline
                                or cancel_event is not None and cancel_event.is_set()):
                            plan['complete'] = False
                            break
                        path = Path(entry.path)
                        group = None
                        try:
                            if kind == 'browser_backup':
                                group = self._browser_group(path, cutoff, deadline, config['max_bytes'] - plan['total_bytes'])
                                plan['examined'] += len(group['files'])
                                if plan['examined'] > config['max_entries']:
                                    plan['complete'] = False
                                    raise ValueError('The recovery file-entry bound was reached; this group was preserved.')
                            else:
                                payload = _read_json(path)
                                if (_HEX.fullmatch(path.stem) is None or path.suffix != '.json'
                                        or payload.get('schema') != 1 or payload.get('kind') != 'optimus-completed-report-export'
                                        or payload.get('id') != path.stem or payload.get('complete') is not True
                                        or type(payload.get('created_at')) not in (int, float)
                                        or not math.isfinite(payload['created_at']) or payload['created_at'] >= cutoff):
                                    raise ValueError('Only old explicitly completed exported report copies are eligible.')
                                _validated_completed_report(payload.get('report'), payload.get('report_kind'))
                                record = _identity(path, directory, cutoff=cutoff)
                                if record is None:
                                    raise ValueError('The report copy is recent, linked or protected.')
                                group = {'kind': kind, 'id': path.stem, 'path': str(path), 'files': [record],
                                         'bytes': record['size'], 'undo_loss': 'Removes this disposable exported report copy; original maintenance and recovery journals are preserved.'}
                            if not enabled or not config['enabled']:
                                raise ValueError('This recovery-retention category is not approved.')
                            if plan['total_bytes'] + group['bytes'] > config['max_bytes']:
                                raise ValueError('The group exceeds the remaining byte bound.')
                            plan['eligible'].append(group)
                            plan['total_bytes'] += group['bytes']
                        except (OSError, ValueError, TypeError, KeyError) as error:
                            protected = {'path': str(path), 'reason': str(error)[:500]}
                            if group is not None:
                                protected.update(bytes=group['bytes'], undo_loss=group['undo_loss'])
                                plan['protected_bytes'] += group['bytes']
                            plan['protected'].append(protected)
            for name in ('parallel-updates', 'driver-backups', 'runs', 'recovery', 'cleanup', 'retention-cleanup'):
                plan['protected'].append({'path': str(self.data_dir / name), 'reason': 'Recovery and operation journals are never retention-cleaned by this feature.'})
        with self._lock:
            for old in list(self._plans):
                if plan['created_at'] - self._plans[old]['created_at'] > 900:
                    self._plans.pop(old, None)
            while len(self._plans) >= 2:
                self._plans.pop(next(iter(self._plans)))
            self._plans[plan['id']] = copy.deepcopy(plan)
        return plan

    scan = preview

    def apply(self, displayed_plan, config, *, confirmed=False, progress_callback=None, cancel_event=None):
        config = validate_configuration(config)
        if confirmed is not True:
            raise ValueError('Confirm permanent expiry and the displayed loss of browser undo.')
        with self._lock:
            plan = self._plans.pop(str(displayed_plan.get('id', '')), None)
        if (plan is None or self.clock() - plan['created_at'] > 900 or self.clock() < plan['created_at']
                or plan['config'] != config or not config['enabled'] or not self._current(config)):
            raise ValueError('Recovery-retention approval expired or saved choices changed; preview again.')
        result = {'schema': 1, 'id': plan['id'], 'deleted_bytes': 0, 'deleted_count': 0,
                  'expired_groups': 0, 'skipped_count': 0, 'complete': plan['complete'], 'items': [], 'files': []}
        journal = self.data_dir / 'recovery-retention' / (plan['id'] + '.json')
        deadline, cutoff = time.monotonic() + config['max_seconds'], self.clock() - config['min_age_days'] * 86400
        last_progress_at = None
        with browser_backup_lease(self.data_dir):
            _audit(journal, {'schema': 1, 'state': 'prepared', 'plan': plan})
            for index, group in enumerate(plan['eligible']):
                if (time.monotonic() >= deadline or not self._current(config)
                        or cancel_event is not None and cancel_event.is_set()):
                    result.update(complete=False, cancelled=bool(cancel_event is not None and cancel_event.is_set()))
                    result['skipped_count'] += len(plan['eligible']) - index
                    break
                try:
                    if group['kind'] == 'browser_backup':
                        fresh = self._browser_group(Path(group['path']), cutoff, deadline, config['max_bytes'])
                        if fresh != group:
                            raise ValueError('The browser-backup group changed after preview.')
                    for record in group['files']:
                        if not self._current(config) or time.monotonic() >= deadline:
                            raise ValueError('Saved choices changed or the verification time bound was reached.')
                        if cancel_event is not None and cancel_event.is_set():
                            raise ValueError('Cancellation requested; remaining backup files were preserved.')
                        if 'sha256' in record and _digest(Path(record['path']), deadline) != record['sha256']:
                            raise ValueError('A browser-backup database changed before expiry; remaining files were preserved.')
                        self.delete(record)
                        result['deleted_count'] += 1
                        result['deleted_bytes'] += record['size']
                        result['files'].append({'path': record['path'], 'status': 'deleted',
                            'identity': {key: record[key] for key in ('device', 'inode', 'size', 'modified_ns', 'created_ns')}})
                    if group['kind'] == 'browser_backup':
                        folder = Path(group['path'])
                        info = folder.lstat()
                        if (info.st_dev, info.st_ino) != (group['device'], group['inode']):
                            raise ValueError('The empty browser-backup directory identity changed.')
                        _ordinary_root(folder)
                        folder.rmdir()  # Empty verified group only; never recursive deletion.
                    result['expired_groups'] += 1
                    result['items'].append({'id': group['id'], 'status': 'expired', 'bytes': group['bytes'], 'undo_loss': group['undo_loss']})
                except (OSError, ValueError, TypeError) as error:
                    result['complete'] = False
                    result['skipped_count'] += 1
                    result['items'].append({'id': group['id'], 'status': 'preserved_or_partial', 'reason': str(error)[:500]})
                now = time.monotonic()
                if callable(progress_callback) and (last_progress_at is None or now - last_progress_at >= 0.1 or index + 1 == len(plan['eligible'])):
                    last_progress_at = now
                    try:
                        progress_callback({'progress_only': True, 'deleted_bytes': result['deleted_bytes'], 'deleted_count': result['deleted_count'],
                            'item_progress': {'key': 'recovery-retention:' + plan['id'], 'label': 'Expired recovery copies', 'phase': 'cleanup',
                                              'percent': (index + 1) * 100 / max(1, len(plan['eligible']))},
                            'description': f'{result["expired_groups"]} recovery groups expired; original operation journals remain protected.'})
                    except Exception:
                        pass
            try:
                _audit(journal, {'schema': 1, 'state': 'completed', 'result': result})
            except (OSError, ValueError) as error:
                result.update(complete=False, journal_error=str(error)[:500])
        result['log_path'] = str(journal)
        return result

    def run(self, config, *, progress_callback=None, cancel_event=None):
        config = validate_configuration(config)
        if not config['enabled']:
            return {'disabled': True, 'skipped': True, 'deleted_bytes': 0, 'deleted_count': 0}
        return self.apply(self.preview(config, progress_callback=progress_callback, cancel_event=cancel_event),
                          config, confirmed=True, progress_callback=progress_callback, cancel_event=cancel_event)
