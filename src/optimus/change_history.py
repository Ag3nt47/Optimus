"""Bounded, reviewed undo of existing Optimus-owned recovery records.

This is an index and dispatcher, never a system-wide time machine. Original
restorers retain their current-state, ownership and identity checks. Plans stay
in this process; durable batch receipts contain identifiers and outcomes only,
not commands, passwords, browser records, registry values or file contents.
"""
from __future__ import annotations

import copy
from datetime import datetime
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

from .automation import RunLease
from .filesystem import _ordinary_data_path
from .speed_enhancements import _current_owner, _private_json

_ID = re.compile(r'(?:[a-f0-9]{32}|[a-f0-9]{64})\Z')
_TOKEN = re.compile(r'[a-f0-9]{32}\Z')
_SUPPORTED = frozenset(('priority', 'affinity', 'startup', 'service', 'powerplan', 'registryentry'))
_FOLDERS = ('runs', 'MaintenanceHistory', 'parallel-updates', 'process-harness/actions',
            'drive-optimization', 'recovery/driver-backups', 'recovery/system-restore',
            'file-utilities', 'smart-maintenance', 'system-privacy', 'change-history')
LIMIT = 500
MAX_SELECTION = 2000


def _text(value, limit=180):
    return ''.join(c for c in str(value or '') if c >= ' ')[:limit]


def _created(record):
    value = next((record[key] for key in ('created_at', 'started_at', 'TimeUtc', 'createdUtc', 'time_utc')
                  if record.get(key) is not None), 0)
    if type(value) in (int, float) and math.isfinite(value) and 0 <= value <= 253402300799:
        return float(value)
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace('Z', '+00:00')).timestamp()
            return parsed if math.isfinite(parsed) and parsed >= 0 else 0
        except (ValueError, OverflowError, OSError):
            pass
    return 0


def _fingerprint(record):
    body = json.dumps(record, sort_keys=True, separators=(',', ':'), allow_nan=False, default=str)
    if len(body) > 2 * 1024 * 1024:
        raise ValueError('An individual recovery record exceeds the review limit.')
    return hashlib.sha256(body.encode('utf-8')).hexdigest()


def _cancelled(event):
    return event is not None and event.is_set()


class ChangeHistory:
    def __init__(self, backend, *, owner=None, lease_factory=RunLease, clock=time.monotonic):
        self.backend, self.root = backend, Path(backend.data_dir)
        self.directory = self.root / 'change-history'
        self.owner, self.lease_factory, self.clock = owner, lease_factory, clock
        self.lock, self._plans = threading.RLock(), {}

    def _owner(self):
        if self.owner is None:
            self.owner = _current_owner()
        if not isinstance(self.owner, str) or not self.owner or len(self.owner) > 256:
            raise ValueError('A stable current account identity is required for undo history.')
        return self.owner

    def _read(self, path, *, maximum=2 * 1024 * 1024):
        info = _ordinary_data_path(path, file=True)
        if info is None or not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > maximum:
            raise ValueError('An ordinary bounded private history record is required.')
        with path.open('rb') as stream:
            opened = os.fstat(stream.fileno())
            raw = stream.read(maximum + 1)
        current = _ordinary_data_path(path, file=True)
        if current is None or len(raw) > maximum or any(
                (v.st_dev, v.st_ino, v.st_nlink, v.st_size, v.st_mtime_ns)
                != (info.st_dev, info.st_ino, 1, info.st_size, info.st_mtime_ns) for v in (opened, current)):
            raise ValueError('The private history file changed during review.')
        return json.loads(raw)

    def _batches(self):
        _ordinary_data_path(self.directory)
        if not self.directory.exists():
            return []
        result, remaining = [], 16 * 1024 * 1024
        with os.scandir(self.directory) as entries:
            for count, entry in enumerate(entries):
                if count >= 1000:
                    raise ValueError('Undo batch history exceeds its bounded review limit.')
                if not entry.name.endswith('.json'):
                    continue
                path = self.directory / entry.name
                if not _TOKEN.fullmatch(path.stem):
                    raise ValueError('An undo batch journal has an invalid identity.')
                value = self._read(path)
                remaining -= path.stat().st_size
                if remaining < 0:
                    raise ValueError('Undo batch history exceeds its size budget.')
                if (not isinstance(value, dict) or value.get('id') != path.stem or value.get('kind') != 'change-history'
                        or value.get('schema') != 1 or value.get('owner') != self._owner()
                        or value.get('status') not in ('prepared', 'running', 'uncertain', 'completed', 'cancelled', 'failed')
                        or not isinstance(value.get('items'), list) or len(value['items']) > MAX_SELECTION
                        or any(not isinstance(row, dict) or not isinstance(row.get('provider'), str)
                               or not isinstance(row.get('record_id'), str) or not _ID.fullmatch(row['record_id'])
                               or row.get('status') not in ('prepared', 'restored', 'not_changed', 'uncertain', 'failed')
                               for row in value['items'])):
                    raise ValueError('An undo batch journal is malformed.')
                if value['status'] in ('completed', 'cancelled', 'failed') and any(
                        row['status'] in ('prepared', 'uncertain') for row in value['items']):
                    raise ValueError('A terminal undo receipt still contains an unresolved item.')
                result.append(value)
        return result

    def pending_barrier(self):
        try:
            for row in self._batches():
                if row.get('status') in ('prepared', 'running', 'uncertain'):
                    return 'A previous undo batch has an unresolved outcome. Review its private saved receipt before further automatic changes.'
        except (OSError, ValueError, TypeError):
            return 'Private undo history could not be verified. Review it before further automatic changes.'
        return None

    def _write(self, record):
        _ordinary_data_path(self.directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(self.directory, 0o700)
        except OSError:
            pass
        path = self.directory / (record['id'] + '.json')
        _ordinary_data_path(path, file=True)
        _private_json(path, record)
        return path

    def _provider(self, provider):
        methods = {'change': 'changes', 'privacy': 'system_privacy_history',
                   'speed': 'speed_enhancements_history', 'browser': 'privacy_backups'}
        if provider == 'sleep':
            method = getattr(getattr(self.backend, 'performance_controller', None), 'list_suspended', None)
            if not callable(method):
                return [], False
            result = method()
            if not isinstance(result, list):
                raise ValueError('Suspended process history is not a usable inventory.')
            return [dict(row, id=_fingerprint(row.get('record', row))) for row in result if isinstance(row, dict)], False
        method = getattr(self.backend, methods[provider], None)
        if not callable(method):
            return [], False
        result = method()
        if provider == 'privacy':
            if not isinstance(result, dict) or not isinstance(result.get('items'), list):
                raise ValueError('Privacy history did not return a usable inventory.')
            return result['items'], result.get('complete') is not True
        if not isinstance(result, list):
            raise ValueError('Recovery history did not return a usable inventory.')
        return result, False

    def _entry(self, provider, record):
        if not isinstance(record, dict):
            raise ValueError('A recovery history row is malformed.')
        identity = record.get('Id') or record.get('id') or record.get('backupId')
        if not isinstance(identity, str) or not _ID.fullmatch(identity):
            raise ValueError('A recovery history identity is invalid.')
        state = _text(record.get('Status') or record.get('status') or record.get('state'), 48).lower()
        restored = state in ('restored', 'superseded', 'unchanged') or record.get('restored') is True
        kind = _text(record.get('Kind') or record.get('kind') or provider, 64)
        target = _text(record.get('Target') or record.get('Name') or record.get('name') or '')
        available, reason = False, 'This history entry has no supported automatic undo. Reinstallation, a Recycle Bin or native recovery may be needed.'
        if provider == 'change':
            source = record.get('source')
            supported = (kind.lower() in _SUPPORTED or source in ('native-startup', 'python-allocation')
                         or source == 'python-compatibility' and record.get('restore_available') is True)
            active = (state in ('applied', 'pending', 'failed', 'restore_pending') if source == 'python-allocation' else
                      record.get('restore_available') is True if source == 'python-compatibility' else
                      not restored if source == 'native-startup' else state in ('applied', 'pending'))
            available = supported and active and not restored and callable(getattr(self.backend, 'restore', None))
            if kind.lower() == 'registryentry':
                available = available and callable(getattr(self.backend, 'registry_restore', None))
        elif provider == 'privacy':
            kind, target = 'Windows privacy preferences', str(record.get('changed_count', '?')) + ' recorded preference(s)'
            available = type(record.get('changed_count')) is int and record['changed_count'] > 0 and callable(getattr(self.backend, 'restore_system_privacy', None))
            reason = 'Restores supported saved preference values only when unchanged since Optimus. Deleted Recall snapshots cannot be recovered.'
        elif provider == 'speed':
            kind, target = 'Power/performance preferences', 'Owned saved power configuration'
            available = record.get('restore_available') is True and not restored and callable(getattr(self.backend, '_speed_enhancements_service', None))
        elif provider == 'browser':
            kind, target = 'Browser privacy backup', _text(record.get('browser')) + ' / ' + _text(record.get('profileName'), 80)
            available = state in ('applied', 'partial') and callable(getattr(self.backend, 'privacy_restore', None))
            reason = 'Restores private browser databases only if the exact profile is closed and newer browser activity is absent.'
        elif provider == 'sleep':
            kind, target = 'Resume Optimus-paused process', _text(record.get('name') or record.get('record', {}).get('name'))
            available = callable(getattr(self.backend, 'wake_process', None))
            reason = 'Resumes only the exact process lifetime recorded by Optimus; an ended or changed process is not replaced.'
        if restored:
            available, reason = False, 'This record is already restored; it is not available for another automatic undo.'
        elif available and provider not in ('privacy', 'browser'):
            reason = 'The existing exact-record restorer will recheck ownership, current state and supported scope before changing it.'
        return {'provider': provider, 'record_id': identity, 'kind': kind, 'target': target,
                'created_at': _created(record), 'status': state or 'recorded', 'undo_available': bool(available),
                'reason': reason, '_record': record, '_fingerprint': _fingerprint(record)}

    def _inventory(self):
        items, errors, done = [], [], set()
        for provider in ('change', 'privacy', 'speed', 'browser', 'sleep'):
            try:
                records, partial = self._provider(provider)
                if partial or len(records) > LIMIT:
                    errors.append(provider + ' history is incomplete or exceeds its review limit.')
                for record in records[:LIMIT]:
                    try:
                        item = self._entry(provider, record)
                        key = (provider, item['record_id'])
                        if key in done:
                            raise ValueError('Duplicate recovery identity.')
                        done.add(key)
                        items.append(item)
                    except (ValueError, TypeError):
                        errors.append(provider + ' has an invalid or duplicate history row.')
            except Exception as error:
                errors.append(provider + ' history could not be read (' + type(error).__name__ + ').')
        # Include other owned receipts, without interpreting their sensitive
        # operation payloads as executable undo instructions.
        remaining = 16 * 1024 * 1024
        for folder in _FOLDERS:
            directory = self.root / folder
            try:
                _ordinary_data_path(directory)
                if not directory.exists():
                    continue
                with os.scandir(directory) as entries:
                    for index, entry in enumerate(entries):
                        if index >= 200 or len(items) >= 2000:
                            errors.append(folder + ' additional history was omitted at its review limit.')
                            break
                        if not entry.name.endswith('.json'):
                            continue
                        path = directory / entry.name
                        info = _ordinary_data_path(path, file=True)
                        if info is None:
                            continue
                        maximum = 8 * 1024 * 1024 if folder == 'runs' else 2 * 1024 * 1024
                        try:
                            if info.st_size > min(maximum, remaining):
                                raise ValueError('History content is above the remaining bounded review budget.')
                            remaining -= info.st_size
                            value = self._read(path, maximum=maximum)
                        except (OSError, ValueError, TypeError):
                            if _ID.fullmatch(path.stem):
                                items.append({'provider': 'history:' + folder, 'record_id': path.stem,
                                    'kind': folder + ' journal metadata', 'target': 'Content was not parsed; review its dedicated tool',
                                    'created_at': 0, 'file_modified_at': info.st_mtime, 'size_bytes': info.st_size,
                                    'status': 'unverified', 'undo_available': False,
                                    'reason': 'Only this journal file identity/size was reviewed. Its content was invalid, unavailable or beyond a bounded review budget. No automatic undo is authorized.'})
                            errors.append(folder + ' has an unparsed receipt shown as unverified metadata only.')
                            continue
                        if not isinstance(value, dict):
                            raise ValueError('History object required.')
                        identity = value.get('id') or value.get('run_id') or value.get('runId') or path.stem
                        if not isinstance(identity, str) or not _ID.fullmatch(identity):
                            continue
                        if folder == 'system-privacy' and ('privacy', identity) in done:
                            continue
                        kind = _text(value.get('kind') or value.get('action') or folder, 80)
                        items.append({'provider': 'history:' + folder, 'record_id': identity, 'kind': kind,
                            'target': 'Recorded operation; see its dedicated tool for supported recovery',
                            'created_at': _created(value), 'status': _text(value.get('status') or value.get('state') or 'recorded', 48),
                            'undo_available': False,
                            'reason': 'No automatic undo is provided here. Deleted files, app removals, driver updates, overwritten data and Recall snapshots are not universally reversible.'})
            except (OSError, ValueError, TypeError):
                errors.append(folder + ' private history could not be verified.')
        try:
            restored = {(row['provider'], row['record_id']) for batch in self._batches()
                        for row in batch.get('items', []) if isinstance(row, dict) and row.get('status') == 'restored'}
            for item in items:
                if (item['provider'], item['record_id']) in restored:
                    item.update(undo_available=False, reason='A completed Optimus undo batch already restored this record.')
        except (OSError, ValueError, TypeError, KeyError):
            errors.append('Undo batch history could not be verified.')
        items.sort(key=lambda item: (item['created_at'], item['record_id']), reverse=True)
        return items[:2000], list(dict.fromkeys(errors))

    def scan(self):
        with self.lock:
            now = self.clock()
            self._plans = {key: value for key, value in self._plans.items() if now < value['expires_at']}
            while len(self._plans) >= 8:
                self._plans.pop(next(iter(self._plans)))
            items, errors = self._inventory()
            identity, choices = uuid.uuid4().hex, {}
            public = []
            for item in items:
                selection = uuid.uuid4().hex
                choices[selection] = copy.deepcopy(item)
                public.append(dict({key: value for key, value in item.items() if not key.startswith('_')}, selection_id=selection))
            self._plans[identity] = dict(owner=self._owner(), expires_at=now + 600, choices=choices)
            return {'status': 'completed' if not errors else 'needs_review', 'plan_id': identity,
                    'expires_at': now + 600, 'items': public, 'complete': not errors, 'errors': errors,
                    'undo_available': sum(item['undo_available'] for item in public),
                    'note': 'Newest changes appear first. Only supported available restorations can be selected. This cannot reverse every cleanup or restore deleted personal data.'}

    def _dispatch(self, item, cancel, progress):
        provider, record, identity = item['provider'], item['_record'], item['record_id']
        if provider == 'privacy':
            return self.backend.restore_system_privacy(identity, cancel_event=cancel)
        if provider == 'browser':
            return self.backend.privacy_restore(identity, confirmed=True)
        if provider == 'speed':
            return self.backend._speed_enhancements_service().restore(identity, cancel_event=cancel, progress_callback=progress)
        if provider == 'sleep':
            return self.backend.wake_process(copy.deepcopy(record.get('record') or record))
        if record.get('source') == 'python-compatibility':
            return self.backend._windows_compatibility_service().restore(identity, cancel_event=cancel)
        if str(record.get('Kind', '')).lower() == 'registryentry':
            return self.backend.registry_restore(record)
        return self.backend.restore(copy.deepcopy(record))

    @staticmethod
    def _outcome(value):
        if not isinstance(value, dict):
            return 'uncertain'
        status = str(value.get('status') or value.get('state') or value.get('Status') or '').lower()
        rows = value.get('items')
        if status in ('uncertain', 'restore_uncertain', 'running', 'prepared') or value.get('outcomeKnown') is False:
            return 'uncertain'
        if isinstance(rows, list) and any(isinstance(row, dict) and row.get('status') in ('uncertain', 'prepared', 'running') for row in rows):
            return 'uncertain'
        if value.get('failed_items', 0) or status in ('failed', 'completed_with_errors', 'restore-partial'):
            return 'failed'
        if value.get('restored') is True or status in ('restored', 'resumed'):
            return 'restored'
        if status == 'completed' and isinstance(rows, list) and rows and all(isinstance(row, dict)
                and row.get('status') in ('restored', 'skipped') for row in rows):
            return 'restored' if all(row.get('status') == 'restored' for row in rows) else 'not_changed'
        if status in ('cancelled', 'deferred', 'skipped', 'unsupported', 'blocked'):
            return 'not_changed'
        return 'uncertain'

    def revert(self, plan_id, selection_ids, *, confirmed=False, cancel_event=None, progress_callback=None):
        if confirmed is not True:
            raise ValueError('Confirm the concrete selected restoration summary first.')
        if not isinstance(plan_id, str) or not _TOKEN.fullmatch(plan_id) or not isinstance(selection_ids, list) or not selection_ids or len(selection_ids) > MAX_SELECTION:
            raise ValueError('Choose a bounded exact undo plan and selection.')
        if any(not isinstance(value, str) or not _TOKEN.fullmatch(value) for value in selection_ids) or len(set(selection_ids)) != len(selection_ids):
            raise ValueError('Choose unique opaque recovery selections.')
        with self.lock:
            plan = self._plans.get(plan_id)
            if plan is None or plan['owner'] != self._owner() or self.clock() >= plan['expires_at']:
                raise ValueError('The recovery review expired or belongs to another session. Refresh it.')
            selected = []
            for identity in selection_ids:
                item = plan['choices'].get(identity)
                if item is None or item.get('undo_available') is not True:
                    raise ValueError('Only exact available restorations can be selected.')
                selected.append(item)
            selected.sort(key=lambda item: item['created_at'], reverse=True)
            if _cancelled(cancel_event):
                return {'status': 'cancelled', 'items': [], 'note': 'Cancelled before any restoration.'}
            lease = self.lease_factory(self.root).acquire()
            try:
                reason = self.pending_barrier()
                from .parallel_updates import pending_updates_barrier
                reason = reason or pending_updates_barrier(self.backend)
                if reason:
                    raise RuntimeError(reason)
                fresh, errors = self._inventory()
                lookup = {(item['provider'], item['record_id']): item for item in fresh}
                for item in selected:
                    current = lookup.get((item['provider'], item['record_id']))
                    if current is None or current.get('undo_available') is not True or current.get('_fingerprint') != item['_fingerprint']:
                        raise ValueError('A selected recovery record changed. Refresh and review again.')
                report = {'schema': 1, 'id': uuid.uuid4().hex, 'kind': 'change-history', 'owner': self._owner(), 'created_at': time.time(),
                          'status': 'prepared', 'items': [], 'total': len(selected), 'failed_items': 0}
                self._write(report)
                self._plans.pop(plan_id, None)  # Approval cannot replay a consumed batch.
                for index, item in enumerate(selected):
                    if _cancelled(cancel_event):
                        report['status'] = 'cancelled'
                        break
                    row = {'provider': item['provider'], 'record_id': item['record_id'], 'status': 'prepared'}
                    report['items'].append(row)
                    self._write(report)
                    if progress_callback:
                        progress_callback({'stage': 'Restore selected Optimus change', 'description': item['kind'] + ': ' + item['target'],
                                           'current': index, 'total': len(selected)})
                    if _cancelled(cancel_event):
                        row['status'], report['status'] = 'not_changed', 'cancelled'
                        self._write(report)
                        break
                    # Re-read this exact provider after durable intent, not
                    # merely once for the whole batch. A preceding restoration
                    # or another settings choice can change a later record.
                    try:
                        rows, partial = self._provider(item['provider'])
                        matches = [self._entry(item['provider'], raw) for raw in rows[:LIMIT]
                                   if (raw.get('Id') or raw.get('id') or raw.get('backupId')) == item['record_id']]
                        if len(matches) != 1 or matches[0].get('undo_available') is not True or matches[0]['_fingerprint'] != item['_fingerprint']:
                            raise ValueError('The selected history identity changed immediately before restoration.')
                    except Exception as error:
                        row.update(status='not_changed', error_type=type(error).__name__)
                        report.update(status='failed', failed_items=report['failed_items'] + 1)
                        self._write(report)
                        break
                    if _cancelled(cancel_event):
                        row['status'], report['status'] = 'not_changed', 'cancelled'
                        self._write(report)
                        break
                    try:
                        # Each native service rechecks the live target itself.
                        # The preview payload is never accepted from a caller.
                        result = self._dispatch(item, cancel_event, progress_callback)
                        row['status'] = self._outcome(result)
                    except Exception as error:
                        row.update(status='uncertain', error_type=type(error).__name__)
                    if row['status'] in ('uncertain', 'failed'):
                        report['status'] = 'uncertain' if row['status'] == 'uncertain' else 'failed'
                        report['failed_items'] += 1
                        self._write(report)
                        break
                    self._write(report)
                    if progress_callback:
                        progress_callback({'stage': 'Restoration result saved',
                                           'description': item['kind'] + ': ' + row['status'].replace('_', ' '),
                                           'current': index + 1, 'total': len(selected)})
                else:
                    report['status'] = 'completed'
                report['finished_at'] = time.time()
                report['restored'] = sum(row['status'] == 'restored' for row in report['items'])
                report['not_processed'] = len(selected) - len(report['items'])
                report['note'] = (f"{report['restored']} supported record(s) restored; {report['not_processed']} not processed. "
                                  'Other irreversible operations are not undone. Review unchanged/conflicting items separately.')
                path = self._write(report)
                return dict(report, log_path=str(path))
            finally:
                lease.release()
