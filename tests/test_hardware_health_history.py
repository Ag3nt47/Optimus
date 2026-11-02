import copy
import json
import time

import pytest

from optimus.hardware_health_history import HardwareHealthHistory


def report(index=1, *, identity='a' * 64, quality='stable', value=90):
    return {'kind': 'hardware_health', 'schema': 1, 'read_only': True, 'status': 'completed',
        'scan_id': f'{index:032x}', 'captured_at': time.time(), 'drives': [],
        'batteries': [{'name': 'Battery', 'identity_hash': identity, 'identity_quality': quality,
            'identity_source': 'fixture hardware serial',
            'capacity_retained_percent': value, 'cycle_count': index, 'temperature_c': None}]}


def test_baseline_and_every_scan_retained_unknown_is_gap(tmp_path):
    history = HardwareHealthHistory(tmp_path)
    for index, value in enumerate((95, None, 92), 1):
        assert history.record(report(index, value=value))['saved']
    result = history.series()
    points = result['series'][0]['points']
    assert [point['value'] for point in points] == [95, None, 92]
    assert [point['baseline'] for point in points] == [True, False, False]
    assert [point['scan_index'] for point in points] == [1, 2, 3]
    assert len(json.loads(history.path.read_text())['scans']) == 3


def test_replacement_battery_starts_its_own_baseline(tmp_path):
    history = HardwareHealthHistory(tmp_path)
    history.record(report(1)); history.record(report(2, identity='b' * 64, value=100))
    series = [item for item in history.series()['series'] if item['metric'] == 'capacity_retained_percent']
    assert len(series) == 2 and series[0]['id'] != series[1]['id']
    assert series[0]['points'][-1]['value'] is None
    assert series[1]['points'][0]['baseline'] is True


def test_weak_identity_is_explicitly_isolated(tmp_path):
    history = HardwareHealthHistory(tmp_path)
    history.record(report(1, identity=None, quality='unavailable'))
    history.record(report(2, identity=None, quality='unavailable'))
    assert all(item['identity_quality'] == 'weak' for item in history.series()['series'])


def test_rerender_and_duplicate_scan_do_not_append_or_reset_baseline(tmp_path):
    history = HardwareHealthHistory(tmp_path); source = report()
    history.record(source); raw = history.path.read_bytes()
    assert history.record(source)['status'] == 'already_recorded'
    history.series(); history.series()
    assert history.path.read_bytes() == raw


def test_graph_limit_retains_originals_baseline_and_marks_omitted_gap(tmp_path):
    history = HardwareHealthHistory(tmp_path)
    for index in range(1, 8):
        history.record(report(index))
    points = history.series(max_points=4)['series'][0]['points']
    assert points[0]['scan_index'] == 1 and points[0]['baseline']
    assert points[-1]['scan_index'] == 7
    assert points[1]['gap_before'] is True
    assert len(json.loads(history.path.read_text())['scans']) == 7


def test_corrupted_history_preserved_and_never_overwritten(tmp_path):
    history = HardwareHealthHistory(tmp_path)
    history.directory.mkdir(); history.path.write_bytes(b'corrupt history')
    assert history.record(report())['saved'] is False
    assert history.path.read_bytes() == b'corrupt history'
    assert history.series()['status'] == 'unavailable'


def test_history_limit_preserves_first_and_all_existing_scans(tmp_path, monkeypatch):
    from optimus import hardware_health_history as module
    history = HardwareHealthHistory(tmp_path); monkeypatch.setattr(module, '_MAX_SCANS', 2)
    history.record(report(1)); history.record(report(2)); raw = history.path.read_bytes()
    assert history.record(report(3))['status'] == 'limit_reached'
    assert history.path.read_bytes() == raw


def test_only_allowlisted_measurements_stored_no_serials_or_unrelated_report(tmp_path):
    history = HardwareHealthHistory(tmp_path); value = report()
    value['batteries'][0]['SerialNumber'] = 'secret'; value['unrelated'] = 'private data'
    history.record(value)
    assert 'secret' not in history.path.read_text() and 'private data' not in history.path.read_text()


def test_unavailable_scan_creates_real_gap_for_prior_device(tmp_path):
    history = HardwareHealthHistory(tmp_path); history.record(report(1))
    value = report(2); value.update(status='unavailable', batteries=[])
    history.record(value)
    assert history.series()['series'][0]['points'][-1]['value'] is None


def test_two_visible_points_include_baseline_and_latest(tmp_path):
    history = HardwareHealthHistory(tmp_path)
    for index in range(1, 5):
        history.record(report(index))
    assert [p['scan_index'] for p in history.series(max_points=2)['series'][0]['points']] == [1, 4]


@pytest.mark.parametrize('raw', [b'{"schema":1,"schema":1,"devices":{},"scans":[]}',
    b'{"schema":1,"devices":{},"scans":[],"SerialNumber":"hidden"}'])
def test_unknown_or_duplicate_json_fields_are_preserved_without_rewriting(tmp_path, raw):
    history = HardwareHealthHistory(tmp_path); history.directory.mkdir(); history.path.write_bytes(raw)
    assert history.record(report())['saved'] is False
    assert history.path.read_bytes() == raw
