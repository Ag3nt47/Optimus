"""Read-only hardware observations and local, identity-bound health history.

Capacity, charge, endurance used and provider condition are distinct facts.
Unknown sensors never become zeroes or a fabricated physical-health score.
"""
from __future__ import annotations

import copy
from datetime import datetime
import math
import platform
import re
import threading
import time
import uuid

from .filesystem import _ordinary_data_path

HEALTH_STAGE = 'Check battery and physical drive health'
_DIGEST = re.compile(r'[a-f0-9]{64}\Z')
_METRICS = {
    'battery': {'capacity_retained_percent': '% of design', 'provider_condition_code': 'provider condition',
                'cycle_count': 'cycles', 'temperature_c': '°C'},
    'drive': {'wear_percent_used': '% life used', 'temperature_c': '°C', 'power_on_hours': 'hours',
              'provider_condition_code': 'provider condition',
              'uncorrected_read_errors': 'errors', 'uncorrected_write_errors': 'errors'},
}


def _text(value, limit=200):
    return ' '.join(''.join(c if c.isprintable() else ' ' for c in value[:4096]).split())[:limit] if isinstance(value, str) else ''


def _number(value, low=0, high=2**63 - 1):
    if type(value) is int:
        return value if low <= value <= high else None
    return value if type(value) is float and math.isfinite(value) and low <= value <= high else None


def _integer(value):
    return value if type(value) is int and 0 <= value <= 2**63 - 1 else None


def _stamp(value):
    if _number(value, 0, time.time() + 300) is not None:
        return float(value)
    if isinstance(value, str) and len(value) <= 64:
        try:
            parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
            if parsed.tzinfo is not None:
                return _stamp(parsed.timestamp())
        except ValueError:
            pass
    return None


def _identity(row):
    digest = row.get('identity_hash')
    stable = (row.get('identity_quality') == 'stable' and isinstance(digest, str) and _DIGEST.fullmatch(digest)
              and bool(_text(row.get('identity_source'), 100)))
    return {'identity_hash': digest if stable else None,
            'identity_quality': 'stable' if stable else 'unavailable',
            'identity_source': _text(row.get('identity_source'), 100) if stable else ''}


def _condition(value):
    # MSFT_PhysicalDisk.HealthStatus is an enum, not a health percentage.
    if type(value) is int:
        return {0: 'Healthy', 1: 'Warning', 2: 'Unhealthy', 5: 'Unknown'}.get(value, 'Unknown')
    text = _text(value, 100)
    return text or 'Unknown'


def _condition_code(value, kind):
    """Categorical provider observation, never a synthetic health percentage."""
    text = value.casefold().replace(' ', '').replace('_', '') if isinstance(value, str) else ''
    groups = ({0: ('healthy', 'verified', 'passed'), 1: ('warning', 'degraded'),
               2: ('unhealthy', 'critical', 'failing', 'failed')} if kind == 'drive' else
              {0: ('normal', 'good', 'healthy'), 1: ('servicerecommended', 'servicebattery', 'replacesoon'),
               2: ('replacenow', 'poor')})
    return next((code for code, names in groups.items() if text in names), None)


def normalize_hardware_health(raw):
    """Allowlist display/history fields; never retain serials or raw inventories."""
    now = time.time()
    result = {'schema': 1, 'read_only': True, 'kind': 'hardware_health', 'captured_at': now,
              'platform': '', 'status': 'unavailable', 'batteries': [], 'drives': [],
              'capabilities': {}, 'errors': [], 'warnings': [],
              'note': 'Provider-reported condition and capacity/endurance estimates; this is not a surface test or a repair.'}
    if (not isinstance(raw, dict) or type(raw.get('schema')) is not int or raw['schema'] != 1
            or raw.get('read_only') is not True):
        result['errors'] = ['The hardware provider returned no usable read-only report.']
        return result
    result['platform'] = _text(raw.get('platform'), 40)
    result['captured_at'] = _stamp(raw.get('captured_at')) or now
    result['status'] = raw.get('status') if raw.get('status') in (
        'completed', 'partial', 'unavailable', 'cancelled', 'disabled') else 'partial'
    errors = raw.get('errors')
    result['errors'] = [_text(item, 400) for item in errors[:32] if isinstance(item, str)] if isinstance(errors, list) else []
    capabilities = raw.get('capabilities')
    if isinstance(capabilities, dict):
        result['capabilities'] = {key: _text(capabilities[key], 100) for key in (
            'storage_health', 'battery_health', 'volume_mapping') if isinstance(capabilities.get(key), str)}
    batteries = raw.get('batteries')
    storage = raw.get('storage', raw.get('drives'))
    for key, rows, limit in (('batteries', batteries, 32), ('drives', storage, 64)):
        if not isinstance(rows, list):
            result['errors'].append(key.title() + ' inventory is unavailable.')
            continue
        if len(rows) > limit:
            result['errors'].append(key.title() + ' inventory exceeded the display bound; remaining devices are unverified.')
        for index, row in enumerate(rows[:limit]):
            if not isinstance(row, dict):
                result['errors'].append('An invalid ' + key + ' record was omitted.')
                continue
            identity = _identity(row)
            common = dict(identity, name=_text(row.get('name'), 120) or ('Battery ' if key == 'batteries' else 'Physical drive ') + str(index + 1),
                          source=_text(row.get('source'), 160) or result['platform'] + ' hardware provider', note=_text(row.get('note'), 500))
            if key == 'batteries':
                full = _number(row.get('full_charge_capacity'), 1)
                design = _number(row.get('design_capacity'), 1)
                unit = row.get('capacity_unit', row.get('capacity_units'))
                unit = unit if unit in ('mWh', 'uWh', 'mAh', 'uAh', 'relative units', 'native provider units') else 'native provider units'
                retained = _number(100.0 * full / design, 0, 200) if full is not None and design else None
                # macOS can expose an explicit native maximum-capacity percent
                # without absolute capacities; current charge is never used.
                if retained is None:
                    retained = _number(row.get('capacity_ratio_percent', row.get('reported_health_capacity_percent')), 0, 200)
                common.update(condition=_text(row.get('condition'), 100) or 'Unknown',
                    full_charge_capacity=full, design_capacity=design, capacity_unit=unit,
                    capacity_retained_percent=retained, cycle_count=_integer(row.get('cycle_count')),
                    temperature_c=_number(row.get('temperature_c', row.get('temp_c')), -50, 150))
                if retained is not None and retained < 80:
                    result['warnings'].append(common['name'] + ': reported full-charge capacity is below 80% of design; review battery condition.')
            else:
                media = row.get('media_type')
                media = {0: 'Unspecified', 3: 'HDD', 4: 'SSD', 5: 'SCM'}.get(media, 'Unknown') if type(media) is int else _text(media, 40) or 'Unknown'
                volumes = row.get('volumes')
                common.update(device_id=_text(row.get('device_id'), 120), media_type=media,
                    health_status=_condition(row.get('health_status', row.get('condition'))),
                    volumes=[_text(item, 200) for item in volumes[:32] if isinstance(item, str)] if isinstance(volumes, list) else [],
                    wear_percent_used=_number(row.get('wear_percent_used'), 0, 254),
                    temperature_c=_number(row.get('temperature_c', row.get('temp_c')), -50, 150),
                    power_on_hours=_integer(row.get('power_on_hours')),
                    uncorrected_read_errors=_integer(row.get('uncorrected_read_errors')),
                    uncorrected_write_errors=_integer(row.get('uncorrected_write_errors')))
                if common['health_status'].casefold() in ('warning', 'unhealthy', 'failing', 'failed', 'critical'):
                    result['warnings'].append(common['name'] + ': the storage provider reports ' + common['health_status'] + '; review backups and device support.')
                if common['wear_percent_used'] is not None and common['wear_percent_used'] >= 100:
                    result['warnings'].append(common['name'] + ': the provider-reported endurance estimate is at or above 100% used.')
                if any((common[key] or 0) > 0 for key in ('uncorrected_read_errors', 'uncorrected_write_errors')):
                    result['warnings'].append(common['name'] + ': historical uncorrected errors are reported; these are not a failure prediction.')
            result[key].append(common)
            common['provider_condition_code'] = _condition_code(
                common.get('condition') if key == 'batteries' else common.get('health_status'),
                'battery' if key == 'batteries' else 'drive')
    # A cloned/generic identity cannot connect histories of separate devices.
    identities = [row['identity_hash'] for key in ('batteries', 'drives') for row in result[key] if row['identity_hash']]
    duplicates = {identity for identity in identities if identities.count(identity) > 1}
    for key in ('batteries', 'drives'):
        for row in result[key]:
            if row['identity_hash'] in duplicates:
                row.update(identity_hash=None, identity_quality='unavailable', identity_source='')
                row['note'] = 'The provider identity is duplicated; history is not joined across scans.'
    if duplicates:
        result['errors'].append('Duplicate hardware identities cannot establish device continuity.')
    if result['errors'] and result['status'] == 'completed':
        result['status'] = 'partial'
    return result


def collect_hardware_health(backend=None, *, cancel_event=None):
    if cancel_event is not None and cancel_event.is_set():
        return normalize_hardware_health({'schema': 1, 'read_only': True, 'status': 'cancelled', 'storage': [], 'batteries': []})
    try:
        name = platform.system()
        if getattr(backend, 'system', None) in ('linux', 'darwin'):
            name = 'Linux' if backend.system == 'linux' else 'Darwin'
        if name == 'Windows':
            raw = backend.bridge.request('hardware_health', timeout=45, cancel_event=cancel_event)
        else:
            from .native_hardware_health import collect_native_hardware_health
            raw = collect_native_hardware_health(name, cancel_event=cancel_event)
        return normalize_hardware_health(raw)
    except Exception as error:
        return normalize_hardware_health({'schema': 1, 'read_only': True, 'status': 'unavailable',
            'platform': platform.system(), 'storage': [], 'batteries': [],
            'errors': ['Hardware health could not be read (' + type(error).__name__ + ').']})


def health_summaries(report):
    """Compact worker labels plus full per-device accessible text."""
    report = report if isinstance(report, dict) else {}
    battery_lines, drive_lines = [], []
    batteries, drives = report.get('batteries'), report.get('drives')
    for row in batteries[:32] if isinstance(batteries, list) else []:
        if not isinstance(row, dict):
            continue
        value = _number(row.get('capacity_retained_percent'), 0, 200)
        text = f'{value:.1f}% of design' if value is not None else 'capacity unavailable'
        battery_lines.append(text + ' · ' + _text(row.get('name'), 120))
    for row in drives[:64] if isinstance(drives, list) else []:
        if not isinstance(row, dict):
            continue
        text = _condition(row.get('health_status'))
        wear = _number(row.get('wear_percent_used'), 0, 254)
        if wear is not None:
            text += f' · {wear:g}% life used'
        drive_lines.append(text + ' · ' + _text(row.get('name'), 120))
    state = report.get('status')
    missing = 'disabled' if state == 'disabled' else 'check cancelled' if state == 'cancelled' else 'not exposed / unavailable'
    return ('Battery: ' + ('; '.join(battery_lines) if battery_lines else missing),
            'Drive health: ' + ('; '.join(drive_lines) if drive_lines else missing))


class HardwareHealth:
    """One explicit scan at a time; no timer, service, or background polling."""
    def __init__(self, backend):
        self.backend = backend
        self._lock = threading.Lock()
        self.latest = None

    def scan(self, *, cancel_event=None, record_history=True):
        with self._lock:
            report = collect_hardware_health(self.backend, cancel_event=cancel_event)
            report['scan_id'] = uuid.uuid4().hex
            if record_history and report['status'] not in ('cancelled', 'disabled'):
                from .hardware_health_history import HardwareHealthHistory
                history = HardwareHealthHistory(self.backend.data_dir)
                report['history_status'] = history.record(report)
            self.latest = copy.deepcopy(report)
            return report

    def cached(self):
        return copy.deepcopy(self.latest)
