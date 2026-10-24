"""Short, comparable resource observations and a bounded, local-only history.

Rates describe traffic during the observation, not disk latency or Internet
capacity. Changes between runs do not establish that cleanup caused a change.
"""
from __future__ import annotations

import copy
import json
import math
import os
from pathlib import Path
import platform
import re
import statistics
import tempfile
import threading
import time
import uuid

import psutil

from .filesystem import _ordinary_data_path

METRICS = ('cpu_percent', 'memory_percent', 'memory_available_bytes', 'disk_read_bytes_per_second',
           'disk_write_bytes_per_second', 'network_bytes_recv_per_second', 'network_bytes_sent_per_second')


def _number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _counter(provider, name):
    try:
        value = getattr(provider, name)()
        return dict(value._asdict()) if value is not None else None
    except (psutil.Error, OSError, AttributeError, NotImplementedError):
        return None


def collect_resource_window(duration=5, interval=0.5, cancel_event=None, *, provider=psutil,
                            clock=time.monotonic, wall_clock=time.time, wait=None):
    """Observe 1–30 seconds without a busy loop or a benchmark workload.

    Injected clocks/providers are for deterministic tests. Counter resets produce
    unavailable rates rather than a negative rate or a fabricated zero.
    """
    if not _number(duration) or not 1 <= duration <= 30:
        raise ValueError('Observation duration must be 1 to 30 seconds.')
    if not _number(interval) or not 0.1 <= interval <= 5 or interval > duration:
        raise ValueError('Sample interval must be 0.1 to 5 seconds and within the duration.')
    stop = cancel_event or threading.Event()
    wait = wait or stop.wait
    started = clock()
    previous_at = started
    previous_disk = _counter(provider, 'disk_io_counters')
    previous_network = _counter(provider, 'net_io_counters')
    samples = []
    errors = set()
    try:
        provider.cpu_percent(interval=None)  # The initial instantaneous CPU value is a baseline only.
    except (psutil.Error, OSError, AttributeError, NotImplementedError):
        errors.add('The initial CPU baseline is unavailable.')
    while not stop.is_set() and clock() - started < duration and len(samples) < 300:
        remaining = duration - (clock() - started)
        if wait(min(interval, max(0, remaining))) or stop.is_set():
            break
        now = clock()
        elapsed = now - previous_at
        if elapsed <= 0:
            errors.add('The monotonic clock did not advance.')
            break
        row = {'elapsed_seconds': round(now - started, 6), 'interval_seconds': elapsed}
        try:
            cpu = provider.cpu_percent(interval=None)
            row['cpu_percent'] = float(cpu) if _number(cpu) and 0 <= cpu <= 100 else None
        except (psutil.Error, OSError, AttributeError, NotImplementedError, ValueError):
            errors.add('CPU observation is unavailable for one or more intervals.')
        try:
            memory = provider.virtual_memory()
            if (_number(memory.percent) and 0 <= memory.percent <= 100 and _number(memory.available)
                    and _number(memory.total) and 0 <= memory.available <= memory.total):
                row.update(memory_percent=float(memory.percent), memory_available_bytes=int(memory.available),
                           memory_total_bytes=int(memory.total))
            else:
                errors.add('Invalid memory counters were excluded.')
        except (psutil.Error, OSError, AttributeError, NotImplementedError, ValueError):
            errors.add('Memory observation is unavailable for one or more intervals.')
        for kind, counter_name, previous, fields in (
            ('disk', 'disk_io_counters', previous_disk, ('read_bytes', 'write_bytes')),
            ('network', 'net_io_counters', previous_network, ('bytes_recv', 'bytes_sent')),
        ):
            current = _counter(provider, counter_name)
            for field in fields:
                first = previous.get(field) if previous else None
                last = current.get(field) if current else None
                row[kind + '_' + field + '_per_second'] = (
                    (last - first) / elapsed if _number(first) and _number(last) and last >= first else None)
            if kind == 'disk':
                previous_disk = current
            else:
                previous_network = current
            if current is None:
                errors.add(kind.title() + ' activity counters are unavailable.')
        samples.append(row)
        previous_at = now
    summary = {}
    for key in METRICS:
        valid = [row for row in samples if _number(row.get(key))]
        weight = sum(row['interval_seconds'] for row in valid)
        summary[key] = {'mean': (sum(row[key] * row['interval_seconds'] for row in valid) / weight if weight else None),
                        'maximum': max((row[key] for row in valid), default=None), 'observed_seconds': weight}
    summary['memory_pressure_seconds'] = sum(row['interval_seconds'] for row in samples if row.get('memory_percent', 0) >= 90)
    summary['cpu_pressure_seconds'] = sum(row['interval_seconds'] for row in samples if (row.get('cpu_percent') or 0) >= 90)
    memory_total = next((row.get('memory_total_bytes') for row in samples if row.get('memory_total_bytes')), None)
    return {'schema': 1, 'id': uuid.uuid4().hex, 'captured_at': wall_clock(), 'read_only': True,
            'platform': platform.system(), 'platform_release': platform.release(),
            'logical_cpus': provider.cpu_count() or None, 'memory_total_bytes': memory_total,
            'requested_seconds': duration, 'observed_seconds': max(0, clock() - started),
            'complete': not stop.is_set() and bool(samples) and clock() - started >= duration,
            'cancelled': stop.is_set(), 'samples': samples, 'summary': summary, 'errors': sorted(errors),
            'note': 'System-wide activity includes Optimus. Network traffic is not a connection speed test; disk throughput is not latency. Workload changes affect comparisons.'}


class PerformanceHistory:
    """At most 180 compact windows / 90 days; contains no process names or paths."""
    def __init__(self, data_dir, *, max_entries=180, retention_days=90, clock=time.time, owner=None):
        if not isinstance(max_entries, int) or isinstance(max_entries, bool) or not 2 <= max_entries <= 1000:
            raise ValueError('History entry bound must be 2 to 1000.')
        if not isinstance(retention_days, int) or isinstance(retention_days, bool) or not 1 <= retention_days <= 365:
            raise ValueError('History retention must be 1 to 365 days.')
        logical = Path(os.path.abspath(data_dir))
        _ordinary_data_path(logical)
        self.data_dir = logical.resolve()
        _ordinary_data_path(self.data_dir)
        self.path = self.data_dir / 'performance-history' / 'observations.json'
        self.max_entries = max_entries
        self.retention_days = retention_days
        self.clock = clock
        self._lock = threading.RLock()
        if owner is None:
            from .speed_enhancements import _current_owner
            owner = _current_owner()
        if not isinstance(owner, str) or not re.fullmatch(r'(?:uid:\d+|sid:S-\d+(?:-\d+)+)', owner):
            raise ValueError('The local history owner identity is unavailable.')
        self.owner = owner

    @staticmethod
    def _identity(info):
        return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns

    def _guard(self):
        _ordinary_data_path(self.data_dir)
        _ordinary_data_path(self.path.parent)
        return _ordinary_data_path(self.path, file=True)

    def _validate_row(self, row):
        required = {'id', 'captured_at', 'platform', 'platform_release', 'logical_cpus', 'memory_total_bytes',
                    'requested_seconds', 'observed_seconds', 'summary', 'phase', 'context'}
        if not isinstance(row, dict) or set(row) != required or not isinstance(row.get('id'), str) or not re.fullmatch('[a-f0-9]{32}', row['id']):
            raise ValueError('Performance history contains an invalid observation identity or schema.')
        if (not _number(row['captured_at']) or row['captured_at'] < 0 or row['captured_at'] > self.clock() + 300
                or not _number(row['requested_seconds']) or not 1 <= row['requested_seconds'] <= 30
                or not _number(row['observed_seconds']) or not row['requested_seconds'] - .001 <= row['observed_seconds'] <= 3600
                or type(row['logical_cpus']) is not int or not 1 <= row['logical_cpus'] <= 65536
                or type(row['memory_total_bytes']) is not int or not 1 <= row['memory_total_bytes'] <= 1024 ** 6):
            raise ValueError('Performance history contains invalid observation bounds.')
        if (row['phase'] not in ('before', 'after', 'manual') or row['platform'] not in ('Windows', 'Linux', 'Darwin')
                or not isinstance(row['platform_release'], str) or len(row['platform_release']) > 256
                or not isinstance(row['context'], dict) or set(row['context']) - {'power_source', 'activity_mode'}):
            raise ValueError('Performance history contains unsupported comparison context.')
        for key, allowed in (('power_source', ('battery', 'ac', 'unknown')), ('activity_mode', ('idle', 'active', 'manual', 'unknown'))):
            if key in row['context'] and row['context'][key] not in allowed:
                raise ValueError('Performance history contains invalid power or activity context.')
        summary = row['summary']
        if not isinstance(summary, dict) or set(summary) != set(METRICS) | {'memory_pressure_seconds', 'cpu_pressure_seconds'}:
            raise ValueError('Performance history contains unsupported statistics.')
        for key in METRICS:
            item = summary[key]
            if not isinstance(item, dict) or set(item) != {'mean', 'maximum', 'observed_seconds'}:
                raise ValueError('Performance history contains invalid metric statistics.')
            upper = 100 if key in ('cpu_percent', 'memory_percent') else row['memory_total_bytes'] if key == 'memory_available_bytes' else 1024 ** 6
            if (not _number(item['observed_seconds']) or not 0 <= item['observed_seconds'] <= row['observed_seconds'] + .001
                    or any(value is not None and (not _number(value) or not 0 <= value <= upper) for value in (item['mean'], item['maximum']))
                    or (item['mean'] is None) != (item['maximum'] is None)
                    or (item['mean'] is None) != (item['observed_seconds'] == 0)
                    or (item['mean'] is not None and item['mean'] > item['maximum'] + .000001)):
                raise ValueError('Performance history contains non-finite or inconsistent metrics.')
        if any(not _number(summary[key]) or not 0 <= summary[key] <= row['observed_seconds'] + .001
               for key in ('memory_pressure_seconds', 'cpu_pressure_seconds')):
            raise ValueError('Performance history contains invalid pressure durations.')
        return row

    def _load(self):
        before = self._guard()
        if before is None:
            return []
        if before.st_size > 2 * 1024 * 1024:
            raise ValueError('Performance history is not a bounded regular file.')
        with self.path.open('rb') as stream:
            if self._identity(os.fstat(stream.fileno())) != self._identity(before):
                raise ValueError('Performance history changed while opening it.')
            raw = stream.read(2 * 1024 * 1024 + 1)
            after_open = os.fstat(stream.fileno())
        after = self._guard()
        if (after is None or len(raw) > 2 * 1024 * 1024 or self._identity(after_open) != self._identity(before)
                or self._identity(after) != self._identity(before)):
            raise ValueError('Performance history changed during the read.')
        result = json.loads(raw.decode('utf-8'), parse_constant=lambda value: (_ for _ in ()).throw(ValueError('Non-finite history JSON is unsupported.')))
        if (not isinstance(result, dict) or set(result) != {'schema', 'owner', 'observations'} or type(result.get('schema')) is not int
                or result['schema'] != 1 or result['owner'] != self.owner or not isinstance(result.get('observations'), list)
                or len(result['observations']) > 1000):
            raise ValueError('Performance history has an unsupported schema; original data was preserved.')
        return [self._validate_row(row) for row in result['observations']]

    def record(self, window, phase='manual', context=None):
        if phase not in ('before', 'after', 'manual') or not isinstance(window, dict) or window.get('schema') != 1:
            raise ValueError('A current observation and a before/after/manual phase are required.')
        if window.get('complete') is not True or not _number(window.get('captured_at')):
            raise ValueError('Incomplete observations are displayed but are not comparison baselines.')
        # Context is deliberately allowlisted: exports must not retain caller paths or secrets.
        supplied = context or {}
        context = {key: supplied[key] for key, allowed in (
            ('power_source', ('battery', 'ac', 'unknown')), ('activity_mode', ('idle', 'active', 'manual', 'unknown')))
            if supplied.get(key) in allowed}
        row = {key: copy.deepcopy(window.get(key)) for key in ('id', 'captured_at', 'platform', 'platform_release',
               'logical_cpus', 'memory_total_bytes', 'requested_seconds', 'observed_seconds', 'summary')}
        row.update(phase=phase, context=context)
        self._validate_row(row)
        with self._lock:
            rows = self._load()
            cutoff = self.clock() - self.retention_days * 86400
            rows = [item for item in rows if cutoff <= item['captured_at'] <= self.clock() + 300 and item.get('id') != row['id']]
            rows.append(row)
            rows.sort(key=lambda item: item['captured_at'])
            rows = rows[-self.max_entries:]
            self._guard()
            self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            self._guard()
            expected = self._guard()
            handle, temporary = tempfile.mkstemp(prefix='observations-', suffix='.tmp', dir=self.path.parent)
            try:
                temporary_path = Path(temporary)
                temporary_info = _ordinary_data_path(temporary_path, file=True)
                if temporary_info is None or self._identity(os.fstat(handle)) != self._identity(temporary_info):
                    raise ValueError('The temporary history file changed.')
                with os.fdopen(handle, 'w', encoding='utf-8') as stream:
                    handle = None
                    json.dump({'schema': 1, 'owner': self.owner, 'observations': rows}, stream, allow_nan=False)
                    stream.flush()
                    os.fsync(stream.fileno())
                    written_identity = self._identity(os.fstat(stream.fileno()))
                current = self._guard()
                written_info = _ordinary_data_path(temporary_path, file=True)
                if ((expected is None) != (current is None) or (expected is not None and self._identity(expected) != self._identity(current))
                        or written_info is None or self._identity(written_info) != written_identity):
                    raise ValueError('History paths changed before the atomic write.')
                os.replace(temporary, self.path)
                final = self._guard()
                if final is None or self._identity(final) != written_identity:
                    raise ValueError('History identity could not be verified after writing.')
            finally:
                if handle is not None:
                    os.close(handle)
                if Path(temporary).exists():
                    final_temporary = _ordinary_data_path(Path(temporary), file=True)
                    if final_temporary is not None:
                        Path(temporary).unlink()
            return self._report(rows)

    def report(self):
        with self._lock:
            return self._report(self._load())

    def _report(self, rows):
        latest = rows[-1] if rows else None
        compatible = []
        if latest:
            compatible = [row for row in rows[:-1] if all(row.get(key) == latest.get(key) for key in
                          ('platform', 'platform_release', 'logical_cpus', 'memory_total_bytes', 'requested_seconds', 'phase', 'context'))]
        changes = {}
        for key, value in (latest or {}).get('summary', {}).items():
            if not isinstance(value, dict) or not _number(value.get('mean')):
                continue
            baseline = [row.get('summary', {}).get(key, {}).get('mean') for row in compatible[-10:]]
            baseline = [item for item in baseline if _number(item)]
            if baseline:
                median = statistics.median(baseline)
                changes[key] = {'latest': value['mean'], 'baseline_median': median,
                                'difference': value['mean'] - median, 'baseline_windows': len(baseline)}
        return {'schema': 1, 'read_only': True, 'path': str(self.path), 'observations': copy.deepcopy(rows),
                'latest': copy.deepcopy(latest), 'comparable_windows': len(compatible), 'changes': changes,
                'note': 'Only matching platform, hardware, phase, duration and supplied power/activity context are compared. Changes reflect observations, not a causal speed score.'}
