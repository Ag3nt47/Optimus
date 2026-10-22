"""Bounded observations from existing audits; never a claim of a speed gain.

Only the optional startup comparison writes app-private metadata. It never
changes a registration or grants an existing maintenance approval. A producer
must explicitly attest complete startup inventory; a bare list is insufficient.
"""
from __future__ import annotations

import json
import math
import os
from pathlib import Path
import re
import threading
import time
import uuid

from .filesystem import _ordinary_data_path, replace_with_retry, resolve_data_root

_GIB = 1024 ** 3
_STAGE_LIMIT = 256
_MAX_STARTUP = 1000
_MAX_BASELINE_BYTES = 2 * 1024 * 1024
_STARTUP_ID = re.compile(r'^(Registry|Shortcut)-[a-fA-F0-9]{64}$')
_HASH = re.compile(r'^[a-fA-F0-9]{64}$')
_RUN = re.compile(r'^[a-f0-9]{32}$')
_LOCK = threading.RLock()


def _number(value, *, maximum=None):
    try:
        valid = type(value) in (int, float) and math.isfinite(value) and value >= 0
    except OverflowError:
        valid = False
    if not valid:
        return None
    return value if maximum is None or value <= maximum else None


def _change(before, after, *, percentage=False):
    result = {'before': before, 'after': after,
              'status': 'observed' if before is not None and after is not None else 'unavailable'}
    result['delta_percentage_points' if percentage else 'delta_bytes'] = (
        round(after - before, 3) if percentage else after - before
    ) if result['status'] == 'observed' else None
    return result


def _stage(report, title):
    stages = report.get('stages') if isinstance(report, dict) else None
    if not isinstance(stages, list) or len(stages) > _STAGE_LIMIT:
        return None
    matches = [row for row in stages if isinstance(row, dict) and row.get('title') == title]
    return matches[0] if len(matches) == 1 else None


def _audit(stage):
    if not isinstance(stage, dict) or not isinstance(stage.get('result'), dict):
        return {}, False
    value = stage['result']
    return value, stage.get('status') == 'completed' and value.get('InventoryErrors') == []


def _disks(system):
    rows = system.get('Disks')
    if ('DiskInventoryComplete' in system and system['DiskInventoryComplete'] is not True
            or not isinstance(rows, list) or not rows or len(rows) > 26):
        return {}, False
    result = {}
    for row in rows:
        if not isinstance(row, dict) or not isinstance(row.get('Drive'), str) or not re.fullmatch('[A-Za-z]:', row['Drive']):
            return {}, False
        drive = row['Drive'].upper()
        exact = 'SizeBytes' in row or 'FreeBytes' in row
        size = _number(row.get('SizeBytes')) if exact else _number(row.get('SizeGB'))
        free = _number(row.get('FreeBytes')) if exact else _number(row.get('FreeGB'))
        if exact and (type(size) is not int or type(free) is not int):
            return {}, False
        percent = (free / size * 100 if size and free is not None else None) if exact or 'FreePercent' not in row else _number(row['FreePercent'], maximum=100)
        if drive in result or size is None or size == 0 or free is None or free > size or percent is None:
            return {}, False
        result[drive] = {'free_bytes': free if exact else round(free * _GIB),
                         'free_percent': percent, 'approximate': not exact}
    return result, True


def summarize_resources(before, after, *, before_complete=True, after_complete=True):
    """Compare the existing PowerShell System schema, including its rounding."""
    before = before.get('System', {}) if isinstance(before, dict) else {}
    after = after.get('System', {}) if isinstance(after, dict) else {}
    before = before if isinstance(before, dict) else {}
    after = after if isinstance(after, dict) else {}
    def cpu_value(system):
        return (_number(system.get('CpuPercent'), maximum=100)
                if 'CpuMeasurementAvailable' not in system or system['CpuMeasurementAvailable'] is True else None)

    cpu = _change(cpu_value(before), cpu_value(after), percentage=True)
    used = _change(_number(before.get('MemoryUsedPercent'), maximum=100),
                   _number(after.get('MemoryUsedPercent'), maximum=100), percentage=True)

    def available(system):
        exact = 'MemoryAvailableBytes' in system or 'MemoryTotalBytes' in system
        value = _number(system.get('MemoryAvailableBytes')) if exact else _number(system.get('MemoryAvailableGB'))
        total = _number(system.get('MemoryTotalBytes')) if exact else _number(system.get('MemoryTotalGB'))
        if exact and (type(value) is not int or type(total) is not int):
            return None, False
        valid = value is not None and total is not None and total > 0 and value <= total
        return (value if exact else round(value * _GIB), not exact) if valid else (None, False)

    first_memory, first_approximate = available(before)
    last_memory, last_approximate = available(after)
    memory = {**_change(first_memory, last_memory), 'approximate': first_approximate or last_approximate}
    first_disks, first_valid = _disks(before)
    last_disks, last_valid = _disks(after)
    disks = []
    for drive in sorted(first_disks.keys() | last_disks.keys()):
        first, last = first_disks.get(drive, {}), last_disks.get(drive, {})
        row = {'drive': drive, **_change(first.get('free_bytes'), last.get('free_bytes')),
               'after_free_percent': last.get('free_percent'), 'approximate': first.get('approximate', False) or last.get('approximate', False)}
        row['pressure'] = ('unavailable' if not last else 'low' if last['free_bytes'] < 2 * _GIB or last['free_percent'] < 5
                           else 'above_warning_thresholds')
        disks.append(row)
    warnings = []
    measurement_errors = any('MeasurementErrors' in system and system['MeasurementErrors'] != [] for system in (before, after))
    if measurement_errors:
        warnings.append('The native producer reported unavailable measurements; successful fields remain observations, not complete coverage.')
    if not before_complete or not after_complete:
        warnings.append('One or both audit inventories were incomplete; these observations do not establish complete system coverage.')
    if not first_valid or not last_valid:
        warnings.append('A complete unambiguous disk measurement was unavailable; missing drives are not assigned zero free space.')
    if any(row['pressure'] == 'low' for row in disks):
        warnings.append('At least one volume has less than 2 GiB or 5% free. Individual installers and backups can need more space.')
    if used['after'] is not None and used['after'] >= 85:
        warnings.append('The final observation still shows at least 85% memory use; review sustained work before closing applications.')
    observed = [cpu['status'], used['status'], memory['status']] + [row['status'] for row in disks]
    status = ('unavailable' if not any(value == 'observed' for value in observed) else
              'observed' if all(value == 'observed' for value in observed) and first_valid and last_valid and before_complete and after_complete and not measurement_errors else 'partial')
    return {'status': status, 'approximate': memory['approximate'] or any(row['approximate'] for row in disks), 'cpu_percent': cpu, 'memory_used_percent': used,
            'memory_available_bytes': memory, 'disks': disks, 'warnings': warnings,
            'notes': ['These are volatile before/after observations, not proof that Optimus caused a performance or memory gain.',
                      'Exact byte fields are preferred. Older audits round disk GiB to 0.1 and memory GiB to 0.01; those converted byte deltas are approximate.',
                      'Free-space changes can include other programs, caches and updates; they differ from verified deleted-file bytes.']}


def _text(value, label, *, empty=False):
    if not isinstance(value, str) or len(value) > 512 or '\x00' in value or not empty and not value:
        raise ValueError('Invalid startup ' + label + '.')
    return re.sub(r'[\x00-\x1f\x7f]', ' ', value)


def _startup_rows(records, *, saved=False):
    if type(records) is not list or len(records) > _MAX_STARTUP:
        raise ValueError('Startup inventory exceeds its 1000-entry bound or has no list.')
    result = {}
    for row in records:
        if not isinstance(row, dict):
            raise ValueError('Malformed startup inventory row.')
        enabled = row.get('enabled') if saved else row.get('Enabled')
        if type(enabled) is not bool:
            raise ValueError('Startup inventory lacks an explicit enabled state.')
        if enabled is False:
            if saved:
                raise ValueError('Saved startup baseline must contain only live enabled registrations.')
            # Native coverage attests the live sources, not old backup history.
            continue
        identity = row.get('id') if saved else row.get('Id')
        fingerprint = row.get('fingerprint') if saved else row.get('Fingerprint')
        kind = row.get('entry_type') if saved else row.get('EntryType')
        protected = row.get('protected') if saved else row.get('Protected', False)
        if (not isinstance(identity, str) or not _STARTUP_ID.fullmatch(identity)
                or not isinstance(fingerprint, str) or not _HASH.fullmatch(fingerprint)
                or kind not in ('Registry', 'Shortcut') or not identity.startswith(kind + '-')
                or type(enabled) is not bool or type(protected) is not bool):
            raise ValueError('Startup inventory lacks a legitimate stable identity, fingerprint or state.')
        key = identity.casefold()
        if key in result:
            raise ValueError('Startup inventory contains duplicate stable identities.')
        name = _text(row.get('name') if saved else row.get('Name'), 'name', empty=True)
        source = _text(row.get('source') if saved else row.get('Source'), 'source')
        # Protection is informational here. It never changes approval scope.
        clue = name if saved else ' '.join(str(row.get(field, ''))[:32768] for field in ('Name', 'Command', 'FilePath', 'Location'))
        bing = 'bingwallpaper' in re.sub('[^a-z0-9]', '', clue.casefold())
        result[key] = {'id': kind + '-' + identity.split('-', 1)[1].lower(), 'name': name,
                       'source': source, 'entry_type': kind, 'fingerprint': fingerprint.lower(),
                       'enabled': enabled, 'protected': protected or bing}
    return result


def _metadata(run_id, captured_at):
    if run_id is not None and (not isinstance(run_id, str) or not _RUN.fullmatch(run_id)):
        raise ValueError('Invalid startup baseline run identity.')
    if captured_at is not None and (not isinstance(captured_at, str) or len(captured_at) > 128 or '\x00' in captured_at):
        raise ValueError('Invalid startup inventory capture label.')


def _file_identity(info):
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns


class StartupDrift:
    """Compare complete inventories; corrupt or redirected baselines stay intact."""
    def __init__(self, data_dir):
        self.data_dir = Path(data_dir)

    def _read(self, path):
        info = _ordinary_data_path(path, file=True)
        if info is None:
            return None, None
        if info.st_size > _MAX_BASELINE_BYTES:
            raise ValueError('Saved startup baseline exceeds its byte bound.')
        with path.open('rb') as stream:
            if _file_identity(os.fstat(stream.fileno())) != _file_identity(info):
                raise ValueError('Saved startup baseline changed while opening.')
            raw = stream.read(_MAX_BASELINE_BYTES + 1)
        after = _ordinary_data_path(path, file=True)
        if after is None or _file_identity(after) != _file_identity(info) or len(raw) > _MAX_BASELINE_BYTES:
            raise ValueError('Saved startup baseline changed while reading.')
        value = json.loads(raw.decode('utf-8-sig'))
        if (not isinstance(value, dict) or set(value) != {'schema', 'run_id', 'captured_at', 'saved_at', 'records'}
                or type(value.get('schema')) is not int or value['schema'] != 1 or _number(value.get('saved_at')) is None):
            raise ValueError('Saved startup baseline has an unsupported schema.')
        _metadata(value.get('run_id'), value.get('captured_at'))
        return value, info

    def _save(self, path, value, previous):
        raw = json.dumps(value, ensure_ascii=False, allow_nan=False, sort_keys=True).encode('utf-8')
        if len(raw) > _MAX_BASELINE_BYTES:
            raise ValueError('Startup baseline exceeds its byte bound.')
        _ordinary_data_path(path.parent)
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        _ordinary_data_path(path.parent)
        temporary = path.with_name('.startup-baseline-' + uuid.uuid4().hex + '.tmp')
        try:
            with temporary.open('xb') as stream:
                stream.write(raw)
                stream.flush()
                os.fsync(stream.fileno())
            current = _ordinary_data_path(path, file=True)
            if (previous is None) != (current is None) or previous is not None and _file_identity(current) != _file_identity(previous):
                raise ValueError('Startup baseline changed before saving; newer state was preserved.')
            replace_with_retry(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)

    def compare(self, records, *, complete=False, run_id=None, captured_at=None):
        result = {'status': 'unverified', 'baseline_saved': False, 'counts': {'new': 0, 'removed': 0, 'changed': 0},
                  'new': [], 'removed': [], 'changed': [], 'errors': [],
                  'notes': ['Startup differences can come from you, installers, Windows or this maintenance run. They do not classify a program as unwanted.',
                            'Coverage is limited to enabled supported Run registrations and Startup-folder shortcuts; it does not cover every scheduled task, service or other persistence source.',
                            'Bing Wallpaper and other protected entries remain informational; no registration or saved approval is changed.']}
        if complete is not True:
            result['reason'] = 'The producer did not verify a complete startup inventory; no baseline was changed.'
            return result
        with _LOCK:
            try:
                _metadata(run_id, captured_at)
                current = _startup_rows(records)
                path = resolve_data_root(self.data_dir) / 'MaintenanceInsights' / 'startup-baseline.json'
                prior, prior_info = self._read(path)
                old = _startup_rows(prior['records'], saved=True) if prior else None
                if old is not None:
                    result['new'] = [current[key] for key in sorted(current.keys() - old.keys())]
                    result['removed'] = [old[key] for key in sorted(old.keys() - current.keys())]
                    for key in sorted(current.keys() & old.keys()):
                        fields = [name for name in ('fingerprint', 'enabled') if current[key][name] != old[key][name]]
                        if fields:
                            result['changed'].append({'id': current[key]['id'], 'before': old[key], 'after': current[key], 'fields': fields})
                    result['baseline_run_id'] = prior['run_id']
                    result['counts'] = {key: len(result[key]) for key in ('new', 'removed', 'changed')}
                value = {'schema': 1, 'run_id': run_id, 'captured_at': captured_at, 'saved_at': time.time(),
                         'records': [current[key] for key in sorted(current)]}
                self._save(path, value, prior_info)
                result['baseline_saved'] = True
                result['status'] = 'compared' if prior is not None else 'baseline_created'
                result['counts'] = {key: len(result[key]) for key in ('new', 'removed', 'changed')}
                result['reason'] = ('Compared complete exact registrations with the previous complete baseline.' if prior is not None else
                                    'The first complete startup inventory is the baseline; no historical difference is claimed.')
            except (OSError, ValueError, TypeError, KeyError, UnicodeError) as error:
                result['errors'].append('Startup baseline could not be verified or saved (' + type(error).__name__ + '); existing state was preserved.')
                result['reason'] = 'Startup drift remains unverified. No startup entry or maintenance approval was changed.'
        return result


def build_insights(report, data_dir):
    before, before_complete = _audit(_stage(report, 'Inspect current resource usage'))
    after, after_complete = _audit(_stage(report, 'Capture final resource report'))
    resources = summarize_resources(before, after, before_complete=before_complete, after_complete=after_complete)
    startup_complete = (after_complete and after.get('StartupInventoryComplete') is True
                        and after.get('StartupInventoryErrors', []) == [])
    system = after.get('System') if isinstance(after.get('System'), dict) else {}
    startup = StartupDrift(data_dir).compare(after.get('Startup'), complete=startup_complete,
        run_id=report.get('id') if isinstance(report, dict) else None,
        captured_at=system.get('CapturedAtUtc'))
    counts = startup['counts']

    def display(value, *, gib=False):
        return 'unavailable' if value is None else (f'{value / _GIB:.2f} GiB' if gib else f'{value:.1f}%')

    cpu, memory = resources['cpu_percent'], resources['memory_available_bytes']
    observed = [f"CPU {display(cpu['before'])} -> {display(cpu['after'])}",
                f"available RAM {display(memory['before'], gib=True)} -> {display(memory['after'], gib=True)}"]
    for row in resources['disks'][:3]:
        observed.append(f"{row['drive']} free {display(row['before'], gib=True)} -> {display(row['after'], gib=True)}")
    if not resources['disks']:
        observed.append('disk free space unavailable')
    elif len(resources['disks']) > 3:
        extra = len(resources['disks']) - 3
        observed.append(str(extra) + ' additional volume' + ('s' if extra != 1 else '') + ' in resource details')
    summary = ('Resource observations: ' + resources['status'] + '. ' + '; '.join(observed) +
               ('. Legacy byte measurements are approximate' if resources['approximate'] else '') +
               '. Startup comparison: ' + startup['status'].replace('_', ' ') +
               (f" ({counts['new']} new, {counts['removed']} removed, {counts['changed']} changed)." if startup['status'] == 'compared' else '.') +
               ' Observed differences are not proof of an optimization gain; no startup action was applied.')
    return {'schema': 1, 'read_only': True, 'summary': summary,
            'resource_changes': resources, 'startup_changes': startup}
