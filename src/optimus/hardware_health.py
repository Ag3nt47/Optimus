"""Read-only hardware observations and local, identity-bound health history.

Capacity, charge, endurance used and provider condition are distinct facts.
Unknown sensors never become zeroes or a fabricated physical-health score.
"""
from __future__ import annotations

import copy
from datetime import datetime, timezone
import math
import platform
import re
import threading
import time
import uuid

HEALTH_STAGE = 'Check battery and physical drive health'
_DIGEST = re.compile(r'[a-f0-9]{64}\Z')
_METRICS = {
    'battery': {'capacity_retained_percent': '% of design', 'provider_condition_code': 'provider condition',
                'cycle_count': 'cycles', 'temperature_c': '°C'},
    'drive': {'wear_percent_used': '% estimated SSD endurance used', 'temperature_c': '°C', 'power_on_hours': 'hours',
              'provider_condition_code': 'provider condition',
              'uncorrected_read_errors': 'errors', 'uncorrected_write_errors': 'errors'},
}
_DRIVE_COUNTERS = {
    'read_errors_total': ('Total read errors', 'errors'),
    'corrected_read_errors': ('Corrected read errors', 'errors'),
    'write_errors_total': ('Total write errors', 'errors'),
    'corrected_write_errors': ('Corrected write errors', 'errors'),
    'start_stop_cycles': ('Start/stop cycles', 'cycles'),
    'rated_start_stop_cycles': ('Provider-rated start/stop limit', 'cycles'),
    'load_unload_cycles': ('Load/unload cycles', 'cycles'),
    'rated_load_unload_cycles': ('Provider-rated load/unload limit', 'cycles'),
    'maximum_read_latency_ms': ('Maximum observed read latency', 'ms'),
    'maximum_write_latency_ms': ('Maximum observed write latency', 'ms'),
    'maximum_flush_latency_ms': ('Maximum observed flush latency', 'ms'),
}
_SMART_COUNTERS = {
    'power_cycle_count': ('Power cycles', 'cycles'),
    'critical_warning': ('NVMe critical warning mask', 'bit mask'),
    'available_spare_percent': ('NVMe available spare', '%'),
    'available_spare_threshold_percent': ('NVMe spare threshold', '%'),
    'media_data_integrity_errors': ('NVMe media/data integrity errors', 'errors'),
    'error_information_log_entries': ('NVMe error log entries', 'entries'),
    'unsafe_shutdowns': ('NVMe unsafe shutdowns', 'shutdowns'),
    'data_units_read': ('NVMe data units read', 'units of 512,000 bytes'),
    'data_units_written': ('NVMe data units written', 'units of 512,000 bytes'),
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


def _drive_evidence(row, common, warnings):
    """Keep raw counters separate from applicability and derived observations."""
    media = common['media_type'].casefold()
    nvme = row.get('wear_counter_kind') == 'nvme_percentage_used'
    raw_wear = _number(row.get('reported_wear_percent_used', row.get('wear_percent_used')), 0, 255 if nvme else 254)
    source = _text(row.get('wear_percent_used_source'), 160) or _text(row.get('reliability_source'), 160) or common['source']
    common.update(reported_wear_percent_used=raw_wear, wear_percent_used=None,
        wear_percent_used_source=source, media_type_source=_text(row.get('media_type_source'), 160),
        additional_readings=[], analysis_notes=[])
    if media == 'hdd':
        evidence, note = 'not_applicable', 'Mechanical HDDs have no standardized SSD endurance-used percentage. The generic wear counter is not a remaining-life measurement.'
    elif media != 'ssd':
        evidence, note = 'unavailable', 'The media type or endurance counter could not be verified; remaining life is unavailable.'
    elif raw_wear is None:
        evidence, note = 'unavailable', 'The provider did not expose a usable SSD endurance estimate.'
    elif raw_wear == 0 and not nvme:
        evidence, note = 'unverified_zero', 'The generic provider returned 0%. Unsupported/default or rounded counters can look like zero; this does not establish an unused SSD or 100% remaining life.'
    else:
        evidence = 'nvme_estimate' if nvme else 'provider_estimate'
        common['wear_percent_used'] = raw_wear
        note = ('Firmware-reported NVMe endurance estimate; 255 means more than 254% used. '
            if nvme else 'Provider-reported SSD endurance estimate. ')
        note += 'This is estimated endurance used, not a failure probability or remaining calendar life.'
        if raw_wear == 0:
            note += ' A reported 0% is not proof of an unused drive.'
    common.update(wear_evidence=evidence, wear_percent_used_note=note)
    readings, notes = common['additional_readings'], common['analysis_notes']
    notes.append(note)
    def add(label, value, unit='', origin=None, confidence='provider reported'):
        readings.append({'label': label, 'value': str(value) if value is not None else 'Unavailable',
            'unit': unit if value is not None else '', 'source': origin or common['source'], 'confidence': confidence})
    if raw_wear is not None:
        add('Raw provider wear counter', raw_wear, '%', source,
            'not applicable to HDD endurance' if media == 'hdd' else 'unverified zero' if evidence == 'unverified_zero' else 'provider estimate')
    maximum = _number(row.get('maximum_operating_temperature_c'), 1, 150)
    common['maximum_operating_temperature_c'] = maximum
    add('Provider-rated maximum operating temperature', maximum, '°C', _text(row.get('reliability_source'), 160) or None)
    if maximum is not None and common['temperature_c'] is not None and common['temperature_c'] >= maximum:
        warnings.append(common['name'] + ': reported temperature is at or above its provider-rated normal operating maximum; review cooling and backups.')
    hours = common['power_on_hours']
    common['powered_on_days'] = round(hours / 24, 2) if hours is not None else None
    if hours is not None:
        add('Powered-on equivalent usage', f'{hours / 24:,.1f}', 'days', confidence='unit conversion of reported hours')
        notes.append('Powered-on hours describe recorded use, not calendar age or remaining service life.')
    capability = row.get('counter_capabilities') if isinstance(row.get('counter_capabilities'), dict) else {}
    common['counter_capabilities'] = {key: capability[key]
        for key in (*_DRIVE_COUNTERS, 'wear_percent_used', 'temperature_c', 'power_on_hours',
                    'maximum_operating_temperature_c', 'uncorrected_read_errors', 'uncorrected_write_errors', 'manufacture_year_week')
        if capability.get(key) in ('reported', 'not_reported', 'invalid_or_sentinel', 'not_applicable', 'unavailable')}
    for key, (label, unit) in _DRIVE_COUNTERS.items():
        value = _integer(row.get(key))
        common[key] = value
        add(label, value, unit, _text(row.get('reliability_source'), 160) or None)
    manufactured = _text(row.get('manufacture_year_week'), 20)
    common['manufacture_year_week'] = None
    match = re.fullmatch(r'(19\d{2}|20\d{2})[- /Ww]?(\d{2})', manufactured)
    if match:
        try:
            first_day = datetime.fromisocalendar(int(match[1]), int(match[2]), 1).replace(tzinfo=timezone.utc)
            elapsed = (datetime.now(timezone.utc) - first_day).total_seconds()
            if elapsed >= 0:
                common['manufacture_year_week'] = f'{match[1]}-W{match[2]}'
                add('Reported manufacture week', common['manufacture_year_week'])
                add('Approximate calendar age from manufacture week', f'{elapsed / (365.25 * 86400):.1f}', 'years',
                    confidence='estimate from provider date, approximately one-week resolution')
        except ValueError:
            pass
    smart = row.get('smart_evidence') if isinstance(row.get('smart_evidence'), dict) else {}
    clean_smart = {'protocol': _text(smart.get('protocol'), 20), 'source': _text(smart.get('source'), 160)}
    if clean_smart['protocol'] in ('ATA', 'NVMe'):
        for key, (label, unit) in _SMART_COUNTERS.items():
            if key != 'power_cycle_count' and clean_smart['protocol'] != 'NVMe':
                continue
            value = _number(smart.get(key), 0, 255 if key == 'critical_warning' else 2**128 - 1) if type(smart.get(key)) is int else None
            if key in ('data_units_read', 'data_units_written') and value == 0:
                value = None
            if key.startswith('data_units_') and smart.get('data_unit_bytes') != 512000:
                value = None
            if key.endswith('_percent') and value is not None and value > 100:
                value = None
            clean_smart[key] = value
            add(label, value, unit, clean_smart['source'], 'firmware reported; data-unit counts rounded up' if key.startswith('data_units_') else 'firmware reported')
        if clean_smart['protocol'] == 'NVMe':
            clean_smart['scope'] = _text(smart.get('scope'), 30)
            clean_smart['data_units_note'] = _text(smart.get('data_units_note'), 500)
            clean_smart['namespace_id'] = _number(smart.get('namespace_id'), 0, 2**32 - 1) if type(smart.get('namespace_id')) is int else None
            if clean_smart['scope']:
                add('NVMe log scope', clean_smart['scope'], origin=clean_smart['source'])
            if clean_smart['data_units_note']:
                notes.append(clean_smart['data_units_note'])
        for key in ('current_attribute_threshold_failure', 'past_attribute_threshold_failure'):
            if type(smart.get(key)) is bool:
                clean_smart[key] = smart[key]
                add(key.replace('_', ' ').capitalize(), 'Yes' if smart[key] else 'No', origin=clean_smart['source'])
        if clean_smart.get('critical_warning') or clean_smart.get('media_data_integrity_errors'):
            warnings.append(common['name'] + ': firmware reports NVMe warning/error evidence; review the hardware details and backups.')
    common['smart_evidence'] = clean_smart if clean_smart['protocol'] in ('ATA', 'NVMe') else None
    common['additional_readings'] = readings[:32]
    common['assessment'] = {'status': 'limited',
        'summary': 'Provider condition and available counters are observations. A reliable remaining-life percentage or date cannot be inferred from age alone.'}


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
            'storage_health', 'battery_health', 'battery_inbox_report', 'volume_mapping') if isinstance(capabilities.get(key), str)}
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
                unit = unit if unit in ('mWh', 'uWh', 'mAh', 'uAh', 'relative units', 'relative provider units', 'native provider units') else 'native provider units'
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
                    temperature_c=_number(row.get('temperature_c', row.get('temp_c')), -50, 150),
                    power_on_hours=_integer(row.get('power_on_hours')),
                    uncorrected_read_errors=_integer(row.get('uncorrected_read_errors')),
                    uncorrected_write_errors=_integer(row.get('uncorrected_write_errors')))
                _drive_evidence(row, common, result['warnings'])
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


def collect_hardware_health(backend=None, *, cancel_event=None, force_refresh=False):
    if type(force_refresh) is not bool:
        raise ValueError('Choose a boolean hardware refresh option.')
    if cancel_event is not None and cancel_event.is_set():
        return normalize_hardware_health({'schema': 1, 'read_only': True, 'status': 'cancelled', 'storage': [], 'batteries': []})
    try:
        name = platform.system()
        if getattr(backend, 'system', None) in ('linux', 'darwin'):
            name = 'Linux' if backend.system == 'linux' else 'Darwin'
        if name == 'Windows':
            raw = backend.bridge.request('hardware_health', timeout=75 if force_refresh else 45,
                cancel_event=cancel_event, **({'force_refresh': True} if force_refresh else {}))
        else:
            from .native_hardware_health import collect_native_hardware_health
            raw = collect_native_hardware_health(name, cancel_event=cancel_event,
                **({'force_refresh': True} if force_refresh else {}))
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
        evidence = row.get('wear_evidence')
        wear = _number(row.get('wear_percent_used'), 0, 255)
        if (str(row.get('media_type', '')).casefold() != 'ssd'
                or evidence not in ('provider_estimate', 'nvme_estimate')
                or (evidence != 'nvme_estimate' and wear in (0, 255))):
            wear = None
        if wear is not None:
            text += f' · {wear:g}% endurance used (estimate)'
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
        self._closed = threading.Event()
        self._reader_bridge = None
        self.latest = None

    def _reader(self):
        from .bridge import PowerShellBridge
        from types import SimpleNamespace
        bridge = getattr(self.backend, 'bridge', None)
        if type(bridge) is not PowerShellBridge:
            return self.backend  # Fixture bridges keep their explicit contract.
        if self._reader_bridge is None:
            self._reader_bridge = PowerShellBridge(self.backend.data_dir)
            controller = getattr(self.backend, '_emergency_stop', None)
            if controller is not None:
                self._reader_bridge._bind_emergency_stop(controller)
        return SimpleNamespace(system='win32', bridge=self._reader_bridge)

    def scan(self, *, cancel_event=None, record_history=True, force_refresh=False):
        if type(force_refresh) is not bool:
            raise ValueError('Choose a boolean hardware refresh option.')
        service = self
        class Cancellation:
            def is_set(self):
                return service._closed.is_set() or (cancel_event is not None and cancel_event.is_set())
        cancellation = Cancellation()
        while not self._lock.acquire(timeout=.05):
            if cancellation.is_set():
                return normalize_hardware_health({'schema': 1, 'read_only': True,
                    'status': 'cancelled', 'storage': [], 'batteries': []})
        try:
            options = {'force_refresh': True} if force_refresh else {}
            reader = self._reader() if force_refresh and not cancellation.is_set() else self.backend
            report = collect_hardware_health(reader, cancel_event=cancellation, **options)
            report['scan_id'] = uuid.uuid4().hex
            if record_history and report['status'] not in ('cancelled', 'disabled'):
                from .hardware_health_history import HardwareHealthHistory
                history = HardwareHealthHistory(self.backend.data_dir)
                report['history_status'] = history.record(report)
            self.latest = copy.deepcopy(report)
            return report
        finally:
            try:
                if self._reader_bridge is not None:
                    try:
                        close = getattr(self._reader_bridge, 'close_when_idle', self._reader_bridge.close)
                        close()
                    except (OSError, ValueError, RuntimeError):
                        pass  # Cleanup cannot invalidate a completed read or lock future scans.
            finally:
                self._lock.release()

    def cached(self):
        return copy.deepcopy(self.latest)

    def close(self):
        self._closed.set()
        bridge = self._reader_bridge
        if bridge is not None:
            # Never wait on a request that is still starting its reader host.
            if bridge._lock.acquire(blocking=False):
                try:
                    bridge.close()
                finally:
                    bridge._lock.release()
            # scan() closes the host on completion when this attempt is busy.
