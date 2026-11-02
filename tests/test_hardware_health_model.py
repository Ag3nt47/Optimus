import copy
import math
import threading
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from optimus import hardware_health as module


def raw(**changes):
    value = {'schema': 1, 'read_only': True, 'platform': 'Windows', 'status': 'completed',
        'storage': [{'name': 'Disk', 'device_id': '0', 'health_status': 0,
            'wear_percent_used': 12, 'temperature_c': 35, 'identity_hash': 'a' * 64,
            'identity_quality': 'stable', 'identity_source': 'native unique hardware ID'}],
        'batteries': [{'name': 'Battery', 'full_charge_capacity': 40000, 'design_capacity': 50000,
            'capacity_units': 'mWh', 'cycle_count': 150}], 'errors': []}
    value.update(changes)
    return value


def test_real_health_units_not_charge_or_a_fabricated_disk_score():
    source = raw(); before = copy.deepcopy(source)
    source['batteries'][0]['charge_percent'] = 5
    result = module.normalize_hardware_health(source)
    assert result['batteries'][0]['capacity_retained_percent'] == 80
    assert result['batteries'][0]['capacity_unit'] == 'mWh'
    assert result['drives'][0]['health_status'] == 'Healthy'
    assert result['drives'][0]['wear_percent_used'] == 12
    assert result['drives'][0]['provider_condition_code'] == 0
    assert 'health_percent' not in result['drives'][0]
    assert before['batteries'][0] == {k: v for k, v in source['batteries'][0].items() if k != 'charge_percent'}


@pytest.mark.parametrize('bad', [None, True, False, '80', math.nan, math.inf, -1])
def test_missing_invalid_capacity_is_unknown(bad):
    value = raw(batteries=[{'name': 'Battery', 'design_capacity': bad, 'full_charge_capacity': 50}])
    assert module.normalize_hardware_health(value)['batteries'][0]['capacity_retained_percent'] is None


def test_zero_design_is_unknown_and_above_design_is_retained_without_clamping():
    value = raw(batteries=[{'design_capacity': 0, 'full_charge_capacity': 50},
                          {'design_capacity': 100, 'full_charge_capacity': 105}])
    batteries = module.normalize_hardware_health(value)['batteries']
    assert batteries[0]['capacity_retained_percent'] is None
    assert batteries[1]['capacity_retained_percent'] == 105


def test_zero_full_capacity_is_unavailable_not_zero_health():
    result = module.normalize_hardware_health(raw(batteries=[{'design_capacity': 100, 'full_charge_capacity': 0}]))
    assert result['batteries'][0]['capacity_retained_percent'] is None


def test_battery_numeric_status_is_not_the_msft_disk_health_enum():
    result = module.normalize_hardware_health(raw(batteries=[{'condition': 0, 'health_status': 0}]))
    assert result['batteries'][0]['condition'] == 'Unknown'
    assert result['batteries'][0]['provider_condition_code'] is None


def test_huge_provider_number_is_unknown_without_losing_other_devices():
    value = raw(storage=[{'wear_percent_used': 10**1000}, {'health_status': 0}])
    result = module.normalize_hardware_health(value)
    assert result['drives'][0]['wear_percent_used'] is None
    assert result['drives'][1]['health_status'] == 'Healthy'


def test_native_macos_percent_and_temperature_aliases_do_not_need_fake_capacities():
    value = raw(batteries=[{'reported_health_capacity_percent': 89, 'temp_c': 24}],
        storage=[{'temp_c': 36, 'health_status': 'Verified'}])
    result = module.normalize_hardware_health(value)
    assert result['batteries'][0]['capacity_retained_percent'] == 89
    assert result['batteries'][0]['full_charge_capacity'] is None
    assert result['batteries'][0]['temperature_c'] == 24
    assert result['drives'][0]['temperature_c'] == 36


@pytest.mark.parametrize('value', [None, True, '0', -1, 255, math.inf])
def test_missing_or_sentinel_wear_not_reported_zero(value):
    result = module.normalize_hardware_health(raw(storage=[{'wear_percent_used': value}]))
    assert result['drives'][0]['wear_percent_used'] is None


def test_serials_raw_identity_and_unknown_fields_never_exported():
    value = raw(storage=[{'name': 'Disk', 'SerialNumber': 'secret serial', 'UniqueId': 'secret id',
        'identity_hash': 'b' * 64, 'identity_quality': 'stable'}])
    assert 'secret' not in str(module.normalize_hardware_health(value))


def test_duplicate_identity_never_connects_two_devices():
    value = raw(); value['storage'].append(dict(value['storage'][0], name='Second disk'))
    result = module.normalize_hardware_health(value)
    assert all(row['identity_quality'] == 'unavailable' for row in result['drives'])
    assert result['status'] == 'partial'


def test_physical_warnings_remain_observations_not_repair_or_failure_predictions():
    result = module.normalize_hardware_health(raw(storage=[{'health_status': 'Failing',
        'wear_percent_used': 100, 'uncorrected_read_errors': 1}]))
    assert len(result['warnings']) == 3
    assert result['drives'][0]['provider_condition_code'] == 2
    assert result['read_only'] is True and result['status'] == 'completed'
    assert not any(key in result for key in ('repaired', 'installer_started'))


@pytest.mark.parametrize('value', [None, [], {'schema': True, 'read_only': True}, {'schema': 1, 'read_only': False}])
def test_invalid_provider_response_is_not_healthy(value):
    result = module.normalize_hardware_health(value)
    assert result['status'] == 'unavailable' and result['errors']


def test_cancel_before_native_probe_and_history_does_nothing(monkeypatch, tmp_path):
    event = threading.Event(); event.set()
    backend = SimpleNamespace(data_dir=tmp_path, bridge=Mock())
    report = module.HardwareHealth(backend).scan(cancel_event=event)
    assert report['status'] == 'cancelled'
    backend.bridge.request.assert_not_called()
    assert not (tmp_path / 'hardware-health').exists()


def test_provider_exception_is_local_unavailable_not_an_install_barrier(monkeypatch):
    monkeypatch.setattr(module.platform, 'system', lambda: 'Windows')
    backend = SimpleNamespace(bridge=Mock())
    backend.bridge.request.side_effect = RuntimeError('fixture unavailable')
    report = module.collect_hardware_health(backend)
    assert report['status'] == 'unavailable'
    assert not report.get('installation_blocked') and not report.get('may_continue')
def test_worker_summary_keeps_measurement_before_long_name_and_handles_missing_inventory():
    from optimus.hardware_health import health_summaries
    battery, drives = health_summaries({'batteries': [{'name': 'Long battery name ' * 20,
        'capacity_retained_percent': 85}], 'drives': [{'name': 'Long drive name ' * 20,
        'health_status': 'Warning'}]})
    assert battery.startswith('Battery: 85.0% of design')
    assert drives.startswith('Drive health: Warning')
    assert 'unavailable' in health_summaries({'batteries': None, 'drives': [None]})[0]

