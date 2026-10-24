"""Native health evidence, installer headroom, and explicitly requested repair.

No diagnostic changes pagefiles, reboots, installs drivers, or resets Windows
Update. Windows servicing is delegated to the tracked native bridge so a caller
timeout cannot kill an unknown repair or cause an automatic replay.
"""
from __future__ import annotations

import ctypes
from datetime import datetime
import hashlib
import json
import math
import os
from pathlib import Path
import platform
import shutil
import subprocess
import time
import uuid

import psutil

from .filesystem import _ordinary_data_path

GIB = 1024 ** 3
SYMPTOMS = frozenset(('windows_update_errors', 'component_corruption', 'system_file_errors', 'repeated_system_crashes'))
_INTEGRITY_STEPS = {'check_health': ('dism.exe', '/Online /Cleanup-Image /CheckHealth /NoRestart'),
                    'scan_health': ('dism.exe', '/Online /Cleanup-Image /ScanHealth /NoRestart'),
                    'verify_files': ('sfc.exe', '/verifyonly'),
                    'restore_health': ('dism.exe', '/Online /Cleanup-Image /RestoreHealth /NoRestart'),
                    'repair_files': ('sfc.exe', '/scannow')}


def _finite(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _command(arguments, timeout=8):
    """Bounded read-only native tools only; never accepts a shell string."""
    result = subprocess.run(arguments, capture_output=True, timeout=timeout, check=False,
                            creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    if len(result.stdout) > 1024 * 1024 or len(result.stderr) > 65536:
        raise ValueError('Native diagnostic output exceeded its bound.')
    if result.returncode:
        raise OSError('Native diagnostic command exited with code ' + str(result.returncode))
    return result.stdout


def _native_batteries(root=Path('/sys/class/power_supply')):
    rows = []
    if not root.is_dir():
        return rows
    for path in sorted(root.iterdir())[:32]:
        try:
            if (path / 'type').read_text().strip() != 'Battery':
                continue
            row = {'name': path.name}
            for field in ('energy_full', 'energy_full_design', 'charge_full', 'charge_full_design', 'cycle_count'):
                try:
                    value = int((path / field).read_text().strip())
                    if value >= 0:
                        row[field] = value
                except (OSError, ValueError):
                    pass
            full, design = ((row.get('energy_full'), row.get('energy_full_design')) if row.get('energy_full_design')
                            else (row.get('charge_full'), row.get('charge_full_design')))
            row['full_charge_vs_design_percent'] = 100 * full / design if full is not None and design else None
            rows.append(row)
        except OSError:
            continue
    return rows


def collect_system_diagnostics(backend=None, cancel_event=None, *, platform_name=None, command=_command,
                               battery_root=Path('/sys/class/power_supply')):
    name = platform_name or platform.system()
    result = {'schema': 1, 'captured_at': time.time(), 'platform': name, 'read_only': True,
              'status': 'completed', 'storage': [], 'batteries': [], 'boot': {}, 'pagefile': {},
              'memory': {}, 'capabilities': {}, 'errors': [], 'recommendations': []}
    if cancel_event is not None and cancel_event.is_set():
        return dict(result, status='cancelled')
    if name == 'Windows':
        if backend is None:
            raise ValueError('Windows health diagnostics require the tracked native backend.')
        try:
            native = backend.bridge.request('system_diagnostics', timeout=90)
            if not isinstance(native, dict) or native.get('schema') != 1 or native.get('read_only') is not True:
                raise ValueError('The native diagnostic returned an unsupported response.')
            for key in ('storage', 'batteries', 'boot', 'pagefile', 'memory', 'capabilities', 'errors'):
                if key in native:
                    result[key] = native[key]
        except Exception as error:
            result.update(status='partial', errors=[str(error)], capabilities={'native_health': 'unavailable'})
    elif name in ('Linux', 'Darwin'):
        try:
            memory = psutil.virtual_memory()
            swap = psutil.swap_memory()
            result['memory'] = {'total_bytes': memory.total, 'available_bytes': memory.available,
                                'percent': memory.percent}
            result['pagefile'] = {'kind': 'swap', 'total_bytes': swap.total, 'used_bytes': swap.used,
                                  'note': 'Swap configuration is observed; Optimus does not resize or disable it.'}
            result['capabilities']['memory'] = 'available'
        except (psutil.Error, OSError, AttributeError) as error:
            result['errors'].append(str(error))
        if name == 'Linux':
            result['batteries'] = _native_batteries(battery_root)
            result['capabilities']['battery_health'] = 'available' if result['batteries'] else 'not_exposed'
            executable = shutil.which('systemd-analyze')
            if executable:
                try:
                    text = command([executable, 'time'], timeout=5).decode('utf-8', 'replace')
                    result['boot'] = {'source': 'systemd-analyze time', 'summary': text[:4096],
                                      'note': 'Current boot only; no per-application Windows event parity is claimed.'}
                except (OSError, ValueError, subprocess.TimeoutExpired) as error:
                    result['errors'].append('Boot timing unavailable: ' + str(error))
            result['capabilities']['storage_wear_temperature'] = 'not_exposed_by_inbox_collector'
        else:
            executable = shutil.which('system_profiler')
            if executable:
                try:
                    payload = json.loads(command([executable, 'SPPowerDataType', '-json'], timeout=8))
                    for source in payload.get('SPPowerDataType', [])[:16]:
                        health = source.get('sppower_battery_health_info', {})
                        if isinstance(health, dict) and health:
                            result['batteries'].append({'name': 'Built-in battery', 'health': {
                                key: health[key] for key in ('sppower_battery_cycle_count', 'sppower_battery_health',
                                'sppower_battery_maximum_capacity') if key in health}})
                except (OSError, ValueError, subprocess.TimeoutExpired) as error:
                    result['errors'].append('Battery health unavailable: ' + str(error))
            result['capabilities'].update(battery_health='available' if result['batteries'] else 'not_exposed',
                                           storage_wear_temperature='not_exposed_by_inbox_collector', boot_timing='not_exposed')
    else:
        result.update(status='unsupported', capabilities={'native_health': 'unsupported_platform'})
    memory = result.get('memory') or {}
    if _finite(memory.get('percent')) and memory['percent'] >= 90:
        result['recommendations'].append({'id': 'memory_pressure', 'automatic': False,
            'reason': 'At least 90% memory use was observed. Compare sustained observations and review high-memory applications; keep a system-managed pagefile unless a specific workload requires otherwise.'})
    if result.get('pagefile', {}).get('automatic_managed') is False:
        result['recommendations'].append({'id': 'review_pagefile', 'automatic': False,
            'reason': 'Windows reports a manually managed pagefile. Review configuration and commit pressure before changing it.'})
    if result['errors'] and result['status'] == 'completed':
        result['status'] = 'partial'
    return result


def _volume(path):
    path = Path(path).resolve()
    while not path.exists() and path != path.parent:
        path = path.parent
    if os.name == 'nt':
        volume = ctypes.create_unicode_buffer(32768)
        if not ctypes.windll.kernel32.GetVolumePathNameW(str(path), volume, len(volume)):
            raise ctypes.WinError()
        return volume.value.casefold(), volume.value
    original = path
    identity = path.stat().st_dev
    while path.parent != path and path.parent.stat().st_dev == identity:
        path = path.parent
    return str(identity), str(path or original)


def check_installer_headroom(paths, required_bytes=None, reserve_bytes=2 * GIB, *, disk_usage=shutil.disk_usage,
                             volume_resolver=_volume):
    """Group labelled system/temp/destination paths by actual volume.

    ``required_bytes`` maps each label to its separate expected allocation. Each
    distinct label is counted once; do not supply the same allocation under two
    labels. Unknown sizes remain unknown and cannot produce a "sufficient" claim.
    """
    if not isinstance(paths, dict) or not 1 <= len(paths) <= 32 or any(not isinstance(key, str) or not key or len(key) > 80 for key in paths):
        raise ValueError('Provide 1 to 32 uniquely labelled installer paths.')
    if not isinstance(reserve_bytes, int) or isinstance(reserve_bytes, bool) or not 0 <= reserve_bytes <= 100 * GIB:
        raise ValueError('The reserve must be 0 to 100 GiB.')
    required_bytes = {} if required_bytes is None else required_bytes
    if not isinstance(required_bytes, dict) or set(required_bytes) - set(paths):
        raise ValueError('Requirement labels must belong to the supplied paths.')
    for value in required_bytes.values():
        if value is not None and (not isinstance(value, int) or isinstance(value, bool) or value < 0 or value > 100000 * GIB):
            raise ValueError('Known installer allocations must be non-negative byte counts.')
    volumes, errors = {}, []
    for label, supplied in paths.items():
        try:
            path = Path(supplied)
            if not path.is_absolute():
                raise ValueError('Installer paths must be absolute.')
            key, mount = volume_resolver(path)
            if key not in volumes:
                usage = disk_usage(mount)
                volumes[key] = {'volume': str(mount), 'free_bytes': int(usage.free), 'total_bytes': int(usage.total),
                                'labels': [], 'known_required_bytes': 0, 'unknown_size_labels': [], 'reserve_bytes': reserve_bytes}
            row = volumes[key]
            row['labels'].append(label)
            size = required_bytes.get(label)
            if size is None:
                row['unknown_size_labels'].append(label)
            else:
                row['known_required_bytes'] += size
        except (OSError, ValueError, TypeError) as error:
            errors.append({'label': label, 'error': str(error)})
    for row in volumes.values():
        row['known_shortfall_bytes'] = max(0, row['known_required_bytes'] + reserve_bytes - row['free_bytes'])
        row['status'] = ('insufficient_known_space' if row['known_shortfall_bytes'] else
                         'requirement_unknown' if row['unknown_size_labels'] else 'sufficient_for_estimate')
    rows = list(volumes.values())
    return {'schema': 1, 'read_only': True, 'volumes': rows, 'errors': errors,
            'status': 'insufficient_known_space' if any(row['known_shortfall_bytes'] for row in rows) else
                      'requirement_unknown' if errors or any(row['unknown_size_labels'] for row in rows) else 'sufficient_for_estimate',
            'note': 'This is an estimate, not a reservation or installation guarantee. Unknown download, extraction or rollback sizes are never treated as zero.'}


def integrity_check_plan(symptoms=(), *, requested=False):
    if isinstance(symptoms, str) or not isinstance(symptoms, (list, tuple, set, frozenset)) or any(item not in SYMPTOMS for item in symptoms):
        raise ValueError('Choose supported Windows integrity symptoms.')
    relevant = sorted(set(symptoms))
    return {'schema': 1, 'kind': 'windows_integrity', 'symptoms': relevant,
            'eligible': bool(relevant or requested is True), 'explicit_request': requested is True,
            'diagnostics': ['check_health', 'scan_health', 'verify_files'] if relevant or requested is True else [],
            'repairs': ['restore_health', 'repair_files'], 'repair_requires_explicit_approval': True,
            'note': 'Checks run only for a reported symptom or explicit request. Repair is separate, may download official components, and never restarts Windows automatically.'}


def _integrity_json(path, maximum=512 * 1024):
    path = Path(path)
    before = _ordinary_data_path(path, file=True)
    if before is None:
        return None
    if before.st_size > maximum:
        raise ValueError('An integrity journal exceeds its bound.')
    identity = lambda info: (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns)
    with path.open('rb') as stream:
        if identity(os.fstat(stream.fileno())) != identity(before):
            raise ValueError('An integrity journal changed while opening.')
        raw = stream.read(maximum + 1)
        final = os.fstat(stream.fileno())
    after = _ordinary_data_path(path, file=True)
    if after is None or len(raw) > maximum or identity(before) != identity(after) or identity(final) != identity(before):
        raise ValueError('An integrity journal changed while reading.')
    value = json.loads(raw.decode('utf-8-sig'), parse_constant=lambda value: (_ for _ in ()).throw(ValueError('Non-finite integrity JSON is unsupported.')))
    if not isinstance(value, dict):
        raise ValueError('An integrity journal must be a JSON object.')
    return value, hashlib.sha256(raw).hexdigest()


def _integrity_time(value):
    if not isinstance(value, str) or len(value) > 64:
        raise ValueError('A native integrity timestamp is unavailable.')
    parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if parsed.tzinfo is None:
        raise ValueError('Native integrity timestamps require an explicit timezone.')
    return parsed.timestamp()


def _native_integrity_completion(data_dir, intent):
    directory = Path(data_dir) / 'integrity-diagnostics' / intent['id']
    loaded = _integrity_json(directory / 'completion.json')
    if loaded is None:
        return None
    value, digest = loaded
    if (type(value.get('schema')) is not int or value['schema'] != 1 or value.get('kind') != 'windows-integrity-native-v1'
            or value.get('id') != intent['id'] or value.get('owner') != intent['owner']
            or type(value.get('repair')) is not bool or value['repair'] != intent['repair']
            or value.get('outcomeKnown') is not True or type(value.get('native_started')) is not bool
            or type(value.get('restart_needed')) is not bool
            or value.get('status') not in ('completed_review_native_logs', 'restart_required', 'needs_review', 'requires_administrator', 'preflight_failed')
            or not isinstance(value.get('items'), list) or len(value['items']) > 3
            or Path(value.get('report_directory', '')) != directory):
        raise ValueError('The native integrity completion does not match its owned intent.')
    start, finish = _integrity_time(value.get('started_utc')), _integrity_time(value.get('finished_utc'))
    if not intent['started_at'] - 5 <= start <= finish <= time.time() + 300:
        raise ValueError('Native integrity completion timing is inconsistent.')
    expected = ['restore_health', 'repair_files'] if intent['repair'] else ['check_health', 'scan_health', 'verify_files']
    items = value['items']
    if value['native_started'] is not bool(items):
        raise ValueError('The native integrity launch evidence is inconsistent.')
    if not items and value['status'] not in ('requires_administrator', 'preflight_failed'):
        raise ValueError('A terminal integrity result is missing native step receipts.')
    if not items and value['restart_needed'] is not False:
        raise ValueError('A no-launch integrity result cannot claim a restart request.')
    prior_end = start
    for index, item in enumerate(items):
        step = expected[index]
        if (not isinstance(item, dict) or type(item.get('schema')) is not int or item['schema'] != 1
                or item.get('step') != step or item.get('status') != 'exited'
                or type(item.get('repair')) is not bool or item['repair'] != intent['repair']
                or type(item.get('exit_code')) is not int or not -2147483648 <= item['exit_code'] <= 0xffffffff
                or not isinstance(item.get('executable'), str) or not Path(item['executable']).is_absolute()
                or Path(item['executable']).name.casefold() != _INTEGRITY_STEPS[step][0]
                or item.get('arguments') != _INTEGRITY_STEPS[step][1]
                or Path(item.get('stdout', '')) != directory / (step + '.stdout.log')
                or Path(item.get('stderr', '')) != directory / (step + '.stderr.log')):
            raise ValueError('An integrity native step does not match its allowlisted command.')
        begun, ended = _integrity_time(item.get('started_utc')), _integrity_time(item.get('finished_utc'))
        if not prior_end <= begun <= ended <= finish:
            raise ValueError('Native integrity step timing is inconsistent.')
        if index < len(items) - 1 and item['exit_code'] != 0:
            raise ValueError('Servicing must not continue past a failed or restart-required step.')
        prior_end = ended
    if items:
        last = items[-1]['exit_code']
        if (value['restart_needed'] != (last == 3010)
                or (value['status'] == 'restart_required') != (last == 3010)
                or value['status'] == 'completed_review_native_logs' and (last != 0 or len(items) != len(expected))
                or value['status'] == 'needs_review' and last in (0, 3010)):
            raise ValueError('Integrity native exit codes and terminal status disagree.')
    return value, digest


def _validate_integrity_intent(path, value, owner):
    if (type(value.get('schema')) is not int or value['schema'] != 1 or value.get('kind') != 'windows-integrity-intent-v1'
            or not isinstance(value.get('id'), str) or len(value['id']) != 32
            or any(char not in '0123456789abcdef' for char in value['id']) or value['id'] != Path(path).stem
            or value.get('owner') != owner or type(value.get('repair')) is not bool
            or not _finite(value.get('started_at')) or not 0 <= value['started_at'] <= time.time() + 300
            or value.get('status') not in ('prepared', 'finished')):
        raise ValueError('Integrity intent ownership, schema or bounds are invalid.')


def integrity_mutation_barrier(data_dir, *, owner=None, reboot_reader=None):
    """Cross-process guard; never delete/reset an interrupted servicing record.

    A late durable native completion may establish that a timed-out request
    finished. Absence of a PID, a timeout, or a fresh app launch cannot do so.
    """
    from .speed_enhancements import _current_owner
    owner = owner or _current_owner()
    root = Path(data_dir)
    directory = root / 'integrity-operations'
    try:
        _ordinary_data_path(directory)
        _ordinary_data_path(root / 'integrity-diagnostics')
        paths = sorted(directory.glob('*.json')) if directory.exists() else []
        if len(paths) > 1000:
            raise ValueError('Integrity operation history exceeds its bound.')
        known = set()
        for path in paths:
            loaded = _integrity_json(path)
            if loaded is None:
                raise ValueError('An integrity intent disappeared.')
            value, _ = loaded
            _validate_integrity_intent(path, value, owner)
            known.add(value['id'])
            terminal = _native_integrity_completion(root, value)
            if terminal is None:
                return 'A Windows integrity operation has no verified native completion. It may still be running; review its retained logs before conflicting maintenance: ' + str(path)
            completion, digest = terminal
            if value['status'] == 'finished' and value.get('completion_sha256') != digest:
                raise ValueError('A completed integrity receipt changed after acknowledgment.')
            if completion['restart_needed']:
                state = reboot_reader() if callable(reboot_reader) else None
                if not isinstance(state, dict) or state.get('reboot_required') is not False:
                    return 'Windows integrity servicing requested a restart; its cleared restart state has not been verified.'
        native_root = root / 'integrity-diagnostics'
        if native_root.exists():
            children = list(native_root.iterdir())
            if len(children) > 1000 or any(child.name not in known for child in children):
                raise ValueError('An integrity native record has no matching owned intent.')
    except Exception as error:
        return 'Windows integrity records require review: ' + str(error)[:1000]
    return None


def run_windows_integrity(backend, plan, *, approved=False, repair=False, cancel_event=None, timeout=1800, platform_name=None):
    """Run one tracked native servicing request; timeout never kills or retries it."""
    if not isinstance(plan, dict) or plan.get('schema') != 1 or plan.get('kind') != 'windows_integrity':
        raise ValueError('A current integrity plan is required.')
    if type(repair) is not bool or type(approved) is not bool:
        raise ValueError('Repair and approval must be explicit Boolean choices.')
    fresh = integrity_check_plan(plan.get('symptoms', ()), requested=plan.get('explicit_request') is True)
    if fresh['eligible'] is not True:
        return {'schema': 1, 'status': 'not_needed', 'items': [], 'read_only': True}
    if repair and approved is not True:
        raise PermissionError('Windows repair requires explicit approval separate from automatic maintenance.')
    if cancel_event is not None and cancel_event.is_set():
        return {'schema': 1, 'status': 'cancelled_before_start', 'items': [], 'read_only': not repair}
    if not _finite(timeout) or not 1 <= timeout <= 7200:
        raise ValueError('The servicing observation timeout must be 1 to 7200 seconds.')
    if (platform_name or platform.system()) not in ('Windows', 'win32'):
        return {'schema': 1, 'status': 'unsupported_platform', 'items': [], 'read_only': not repair}
    from .speed_enhancements import _current_owner, _private_json
    root = Path(backend.data_dir)
    owner = _current_owner()
    reason = integrity_mutation_barrier(root, owner=owner, reboot_reader=getattr(backend, 'security_status', None))
    if reason:
        return {'schema': 1, 'status': 'deferred', 'items': [], 'read_only': not repair, 'retry_allowed': False, 'note': reason}
    identity = uuid.uuid4().hex
    journal = root / 'integrity-operations' / (identity + '.json')
    _ordinary_data_path(journal, file=True)
    intent = {'schema': 1, 'kind': 'windows-integrity-intent-v1', 'id': identity, 'owner': owner,
              'repair': repair, 'status': 'prepared', 'started_at': time.time()}
    _private_json(journal, intent)  # Durable authority precedes native dispatch.
    observation_error = None
    try:
        backend.bridge.request('windows_integrity', timeout=timeout, repair=repair,
                               approved=approved is True if repair else False, operation_id=identity)
    except Exception as error:
        observation_error = error
    try:
        terminal = _native_integrity_completion(root, intent)
        if terminal is not None:
            value, digest = terminal
            intent.update(status='finished', completion_sha256=digest, result_status=value['status'])
            _ordinary_data_path(journal, file=True)
            _private_json(journal, intent)
            return dict(value, read_only=not repair, journal=str(journal), retry_allowed=False,
                        note='Retained native exit codes and logs describe the result; no automatic restart or replay was requested.')
    except Exception as error:
        observation_error = error
    return {'schema': 1, 'status': 'running_outcome_unverified', 'items': [], 'read_only': not repair,
            'retry_allowed': False, 'journal': str(journal),
            'observation_error': type(observation_error).__name__ if observation_error is not None else 'missing_native_completion',
            'note': 'The native servicing outcome is not durably verified. It was not killed or replayed; its owned journal blocks conflicting maintenance across app restarts until a valid native completion or reviewed recovery is available.'}
