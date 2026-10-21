"""Read-only sampled evidence of application resource pressure, with lifetime checks."""
from __future__ import annotations
import math
import ntpath
import threading
import time
import psutil

_MIB = 1024 * 1024


def _number(value, default=0.0):
    try:
        number = float(value)
        return number if math.isfinite(number) and number >= 0 else default
    except (TypeError, ValueError, OverflowError):
        return default


def _capture():
    records, inaccessible = {}, 0
    for process in psutil.process_iter(['pid', 'name', 'exe', 'create_time', 'memory_info', 'cpu_times', 'io_counters'], ad_value=None):
        try:
            info = process.info
            executable, created = info.get('exe'), info.get('create_time')
            if not executable or created is None:
                inaccessible += 1
                continue
            memory, cpu, counters = info.get('memory_info'), info.get('cpu_times'), info.get('io_counters')
            identity = (int(info['pid']), float(created), ntpath.normcase(ntpath.normpath(executable)))
            records[identity] = {
                'pid': int(info['pid']), 'name': info.get('name') or ntpath.basename(executable), 'exe': executable,
                'memory_rss': int(_number(getattr(memory, 'rss', 0))),
                'cpu': _number(cpu.user) + _number(cpu.system) if cpu is not None else None,
                'read': _number(counters.read_bytes) if counters is not None else None,
                'write': _number(counters.write_bytes) if counters is not None else None,
            }
        except (psutil.Error, OSError, ValueError, TypeError, KeyError, AttributeError):
            inaccessible += 1
    cpu = psutil.cpu_times()
    total = sum(_number(value) for key, value in cpu._asdict().items() if key not in ('guest', 'guest_nice'))
    idle = _number(getattr(cpu, 'idle', 0)) + _number(getattr(cpu, 'iowait', 0))
    memory = psutil.virtual_memory()
    return {'time': time.monotonic(), 'processes': records, 'inaccessible': inaccessible,
            'cpu_total': total, 'cpu_idle': idle,
            'memory': {'total': int(memory.total), 'available': int(memory.available), 'percent': float(memory.percent)}}


def _summarize(snapshots, *, expected_samples, interval_seconds, cancelled=False):
    """Build evidence from exact-lifetime deltas; no mutation APIs are called."""
    final = snapshots[-1]
    duration = max(0.0, final['time'] - snapshots[0]['time'])
    logical_cores = max(1, psutil.cpu_count(logical=True) or 1)
    memory = final['memory']
    groups = {}
    for identity, record in final['processes'].items():
        key = identity[2]
        group = groups.setdefault(key, {'name': record['name'], 'exe': record['exe'], 'pids': [],
                                        'memory_rss': 0, '_cpu': [], '_read': [], '_write': [], '_samples': [], '_io_samples': []})
        group['pids'].append(record['pid'])
        group['memory_rss'] += record['memory_rss']
    for before, after in zip(snapshots, snapshots[1:]):
        elapsed = after['time'] - before['time']
        if elapsed <= 0:
            continue
        interval = {key: [0.0, 0.0, 0.0, 0, 0] for key in groups}
        for identity, current in after['processes'].items():
            previous = before['processes'].get(identity)
            if previous is None or identity not in final['processes']:
                continue
            values = interval[identity[2]]
            if current['cpu'] is not None and previous['cpu'] is not None and current['cpu'] >= previous['cpu']:
                values[0] += (current['cpu'] - previous['cpu']) / elapsed / logical_cores * 100
                values[3] += 1
            for index, field in ((1, 'read'), (2, 'write')):
                if current[field] is not None and previous[field] is not None and current[field] >= previous[field]:
                    values[index] += (current[field] - previous[field]) / elapsed
                    values[4] += 1
        for key, values in interval.items():
            group = groups[key]
            for field, value in zip(('_cpu', '_read', '_write', '_samples', '_io_samples'), values):
                group[field].append(min(100.0, value) if field == '_cpu' else value)
    total_delta = final['cpu_total'] - snapshots[0]['cpu_total']
    idle_delta = final['cpu_idle'] - snapshots[0]['cpu_idle']
    system_cpu = max(0.0, min(100.0, (total_delta - idle_delta) / total_delta * 100)) if total_delta > 0 else None
    applications = []
    for group in groups.values():
        cpu, read, write, samples, io_samples = (group.pop(key) for key in ('_cpu', '_read', '_write', '_samples', '_io_samples'))
        avg = lambda values: sum(values) / len(values) if values else 0.0
        group.update(process_count=len(group['pids']), cpu_percent=round(avg(cpu), 2), peak_cpu_percent=round(max(cpu, default=0), 2),
                     memory_percent=round(group['memory_rss'] / max(1, memory['total']) * 100, 2),
                     read_bytes_per_second=round(avg(read)), write_bytes_per_second=round(avg(write)),
                     measured_intervals=sum(value > 0 for value in samples), io_measured_intervals=sum(value > 0 for value in io_samples))
        sustained_cpu = sum(value >= 10.0 for value in cpu) >= 2
        sustained_io = sum(r + w >= 5 * _MIB for r, w in zip(read, write)) >= 2
        memory_pressure = memory['percent'] >= 85 and group['memory_rss'] >= 1024 * _MIB and group['memory_percent'] >= 5
        reasons = []
        if sustained_cpu:
            reasons.append(f"Used {group['cpu_percent']:.1f}% of total CPU on average; at least 10% in two measured intervals.")
        elif group['cpu_percent'] >= 10:
            reasons.append(f"CPU activity averaged {group['cpu_percent']:.1f}% during this short sample; measure again for persistence.")
        if sustained_io:
            reasons.append(f"Process I/O averaged {(group['read_bytes_per_second'] + group['write_bytes_per_second']) / _MIB:.1f} MiB/s in two busy intervals; cached/device I/O does not prove disk saturation.")
        if memory_pressure:
            reasons.append(f"Holds {group['memory_rss'] / _MIB:.0f} MiB while system RAM is {memory['percent']:.0f}% used. Shared pages may be counted in multiple processes.")
        group.update(severity='high' if sustained_cpu or memory_pressure else 'review' if reasons else 'normal', reasons=reasons,
                     recommendation='Review its process and launch details. If the work is unnecessary, save your work and close it normally; disable an approved startup entry to prevent automatic relaunch.' if reasons else 'No threshold exceeded in this short sample. This does not establish that the program is necessary or always idle.')
        applications.append(group)
    order = {'high': 0, 'review': 1, 'normal': 2}
    applications.sort(key=lambda app: (order[app['severity']], -app['cpu_percent'], -app['memory_rss'], app['exe'].casefold()))
    count = len(applications)
    return {'sample_count': len(snapshots), 'interval_seconds': interval_seconds, 'duration_seconds': round(duration, 3),
            'complete': len(snapshots) == expected_samples and not cancelled, 'cancelled': cancelled,
            'memory': memory, 'system_cpu_percent': round(system_cpu, 2) if system_cpu is not None else None,
            'applications': applications[:100], 'application_count': count, 'truncated': count > 100,
            'inaccessible_process_count': max(snapshot['inaccessible'] for snapshot in snapshots),
            'notes': ['Read-only measurement; no application was stopped, suspended, reprioritized or deleted.',
                      'CPU is normalized across all logical processors. Only matching PID, creation time and executable lifetimes are compared.',
                      'This short sample shows potential resource contributors, not proof of a slowdown or obsolete software. Repeat during the slowdown.',
                      'I/O includes cached file and device activity. RAM totals can count shared pages repeatedly. Inaccessible processes are excluded.',
                      'Zero CPU/I/O with no measured intervals means unavailable or a new process, not proven idle.',
                      'Protection and current process identity are checked separately before any manual process action.']}


def analyze_slowdowns(*, sample_count=3, interval_seconds=0.75, cancel_event=None):
    if type(sample_count) is not int or not 3 <= sample_count <= 10:
        raise ValueError('Choose 3 to 10 diagnostic samples.')
    if type(interval_seconds) not in (int, float) or not math.isfinite(interval_seconds) or not 0.25 <= interval_seconds <= 2:
        raise ValueError('Diagnostic sample intervals must be 0.25 to 2 seconds.')
    stop = cancel_event if cancel_event is not None else threading.Event()
    snapshots = [_capture()]
    for _ in range(sample_count - 1):
        if stop.wait(interval_seconds):
            break
        snapshots.append(_capture())
    return _summarize(snapshots, expected_samples=sample_count, interval_seconds=interval_seconds, cancelled=stop.is_set())
