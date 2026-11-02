"""Counter meaning, provenance and supported estimates use only local fixtures."""
import copy
import threading
import time
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from optimus import hardware_health as health


def report(**row):
    return health.normalize_hardware_health({'schema': 1, 'read_only': True,
        'status': 'completed', 'storage': [row], 'batteries': [], 'errors': []})


@pytest.mark.parametrize('media', ['HDD', 'Unknown', 'Unspecified', None])
def test_zero_from_hdd_or_unknown_media_is_never_ssd_endurance(media):
    result = report(media_type=media, wear_percent_used=0, power_on_hours=6402)
    row = result['drives'][0]
    assert row['wear_percent_used'] is None
    assert row['reported_wear_percent_used'] == 0
    assert row['powered_on_days'] == 266.75
    assert row['wear_evidence'] == ('not_applicable' if media == 'HDD' else 'unavailable')
    assert 'remaining' in row['assessment']['summary']
    assert not any(key in row for key in ('health_percent', 'remaining_life_percent', 'years_remaining'))


def test_generic_ssd_zero_is_kept_as_raw_evidence_not_an_unused_drive_claim():
    row = report(media_type='SSD', wear_percent_used=0,
        wear_percent_used_source='MSFT_PhysicalDiskToStorageReliabilityCounter.Wear')['drives'][0]
    assert row['reported_wear_percent_used'] == 0 and row['wear_percent_used'] is None
    assert row['wear_evidence'] == 'unverified_zero'
    assert '0%' in row['wear_percent_used_note'] and 'unsupported' in row['wear_percent_used_note'].casefold()
    assert row['additional_readings'][0]['confidence'] == 'unverified zero'


@pytest.mark.parametrize('value', [0, 12, 100, 140, 255])
def test_real_nvme_estimates_keep_source_and_worn_beyond_design_values(value):
    row = report(media_type='SSD', wear_percent_used=value,
        wear_counter_kind='nvme_percentage_used', wear_percent_used_source='NVMe SMART percentage_used')['drives'][0]
    assert row['wear_percent_used'] == value and row['wear_evidence'] == 'nvme_estimate'
    assert row['wear_percent_used_source'] == 'NVMe SMART percentage_used'
    assert 'failure probability' in row['wear_percent_used_note']
    assert '255 means more than 254%' in row['wear_percent_used_note']


def test_temperature_warning_uses_device_reported_limit_not_an_age_guess():
    result = report(media_type='HDD', temperature_c=54, maximum_operating_temperature_c=55)
    assert not result['warnings']
    result = report(media_type='HDD', temperature_c=55, maximum_operating_temperature_c=55)
    assert 'provider-rated' in result['warnings'][0]
    assert report(temperature_c=65)['warnings'] == []


def test_more_supported_counters_keep_real_zero_and_absence_distinct():
    row = report(media_type='HDD', corrected_read_errors=0, read_errors_total=5,
        start_stop_cycles=123, maximum_read_latency_ms=12)['drives'][0]
    assert row['corrected_read_errors'] == 0 and row['read_errors_total'] == 5
    fields = {entry['label']: entry for entry in row['additional_readings']}
    assert fields['Corrected read errors']['value'] == '0'
    assert fields['Corrected write errors']['value'] == 'Unavailable'
    assert fields['Start/stop cycles']['unit'] == 'cycles'


def test_native_manufacture_week_supports_only_explicit_calendar_age_estimate():
    row = report(manufacture_year_week='201001')['drives'][0]
    assert row['manufacture_year_week'] == '2010-W01'
    age = next(entry for entry in row['additional_readings'] if entry['label'].startswith('Approximate calendar age'))
    assert float(age['value']) > 10 and 'provider date' in age['confidence']
    for invalid in ('209901', '201054', 'installed 2010', '000000', ''):
        assert report(manufacture_year_week=invalid)['drives'][0]['manufacture_year_week'] is None


def test_nvme_units_scope_bounds_and_raw_identity_allowlist():
    raw = {'protocol': 'NVMe', 'source': 'NVMe SMART', 'scope': 'namespace', 'namespace_id': 1,
        'critical_warning': 10**100, 'data_units_read': 0, 'data_units_written': 123,
        'data_unit_bytes': 512000, 'data_units_note': 'Reported in rounded-up groups.',
        'serial_number': 'must not survive'}
    before = copy.deepcopy(raw)
    row = report(media_type='SSD', smart_evidence=raw)['drives'][0]
    assert row['smart_evidence']['critical_warning'] is None
    assert row['smart_evidence']['data_units_read'] is None
    assert row['smart_evidence']['data_units_written'] == 123
    assert row['smart_evidence']['scope'] == 'namespace'
    assert 'serial_number' not in row['smart_evidence'] and raw == before


def test_force_refresh_is_passed_to_owned_windows_probe_with_longer_bound(monkeypatch):
    monkeypatch.setattr(health.platform, 'system', lambda: 'Windows')
    token = threading.Event()
    bridge = SimpleNamespace(request=Mock(return_value={'schema': 1, 'read_only': True,
        'status': 'completed', 'batteries': [], 'storage': []}))
    health.collect_hardware_health(SimpleNamespace(bridge=bridge), cancel_event=token, force_refresh=True)
    bridge.request.assert_called_once_with('hardware_health', timeout=75, cancel_event=token, force_refresh=True)
    with pytest.raises(ValueError):
        health.collect_hardware_health(force_refresh='yes')


def test_waiting_second_hardware_scan_can_cancel_without_waiting_for_first(tmp_path):
    service = health.HardwareHealth(SimpleNamespace(data_dir=tmp_path))
    service._lock.acquire()
    token = threading.Event(); token.set()
    started = time.monotonic()
    try:
        result = service.scan(cancel_event=token)
        assert result['status'] == 'cancelled' and time.monotonic() - started < .5
        assert not (tmp_path / 'hardware-health').exists()
    finally:
        service._lock.release()


def test_service_close_never_waits_for_reader_starting_on_other_thread(tmp_path):
    entered, release = threading.Event(), threading.Event()
    lock = threading.RLock()
    def hold():
        with lock:
            entered.set(); release.wait(2)
    thread = threading.Thread(target=hold); thread.start(); assert entered.wait(1)
    service = health.HardwareHealth(SimpleNamespace(data_dir=tmp_path))
    service._reader_bridge = SimpleNamespace(_lock=lock, close=Mock())
    try:
        started = time.monotonic(); service.close()
        assert time.monotonic() - started < .5
        service._reader_bridge.close.assert_not_called()
        assert service._closed.is_set()
    finally:
        release.set(); thread.join(2)
