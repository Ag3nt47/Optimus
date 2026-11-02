import copy
import hashlib
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


def drive_report(index=1, *, media='SSD', evidence='nvme_estimate', wear=0):
    result = report(index)
    result.update(batteries=[], drives=[{'name': 'Fixture drive', 'device_id': '0',
        'identity_hash': 'a' * 64, 'identity_quality': 'stable', 'identity_source': 'fixture physical identity',
        'media_type': media, 'wear_evidence': evidence, 'wear_percent_used': wear,
        'temperature_c': 37}])
    return result


def legacy_drive_history(history, *, media=None, values=(0, 12, 18)):
    identity = hashlib.sha256(('drive\0' + 'a' * 64).encode()).hexdigest()
    device = {'label': 'Legacy fixture drive', 'kind': 'drive', 'identity_quality': 'stable'}
    if media is not None:
        device['media_type'] = media
    value = {'schema': 1, 'devices': {identity: device}, 'scans': [
        {'id': f'{index:032x}', 'captured_at': time.time() - 1000 + index,
            'measurements': {identity: {'wear_percent_used': wear, 'temperature_c': 37}}}
        for index, wear in enumerate(values, 1)]}
    history.directory.mkdir()
    history.path.write_text(json.dumps(value), encoding='utf-8')
    return identity, value


def wear_points(history):
    return next(row['points'] for row in history.series()['series'] if row['metric'] == 'wear_percent_used')


@pytest.mark.parametrize('media', [None, 'HDD', 'SSD'])
def test_legacy_unannotated_wear_is_a_visible_gap_without_rewriting_history(tmp_path, media):
    history = HardwareHealthHistory(tmp_path)
    _, original = legacy_drive_history(history, media=media)
    raw = history.path.read_bytes()
    assert [point['value'] for point in wear_points(history)] == [None, None, None]
    assert [point['baseline'] for point in wear_points(history)] == [True, False, False]
    assert history.path.read_bytes() == raw
    assert json.loads(raw)['scans'] == original['scans']
    assert 'legacy endurance' in history.series()['note'] and 'original measurements are retained' in history.series()['note']


def test_new_trusted_nvme_zero_and_255_append_after_legacy_gaps_without_migration(tmp_path):
    history = HardwareHealthHistory(tmp_path)
    identity, original = legacy_drive_history(history, media='SSD')
    assert history.record(drive_report(4, wear=0))['saved'] is True
    assert history.record(drive_report(5, wear=255))['saved'] is True
    stored = json.loads(history.path.read_text(encoding='utf-8'))
    assert stored['scans'][:3] == original['scans']
    assert [point['value'] for point in wear_points(history)] == [None, None, None, 0, 255]
    assert [point['baseline'] for point in wear_points(history)] == [True, False, False, False, False]
    assert stored['scans'][3]['evidence'] == {identity: {'media_type': 'SSD', 'wear_evidence': 'nvme_estimate'}}
    assert stored['devices'][identity]['media_type'] == 'SSD'


@pytest.mark.parametrize('known_from', ['device', 'scan'])
def test_known_hdd_masks_all_wear_even_if_a_later_provider_claims_ssd(tmp_path, known_from):
    history = HardwareHealthHistory(tmp_path)
    identity, original = legacy_drive_history(history, media='HDD' if known_from == 'device' else 'SSD')
    if known_from == 'scan':
        original['scans'][0]['evidence'] = {identity: {'media_type': 'HDD', 'wear_evidence': 'not_applicable'}}
        history.path.write_text(json.dumps(original), encoding='utf-8')
    assert history.record(drive_report(4, wear=0))['saved'] is True
    assert [point['value'] for point in wear_points(history)] == [None] * 4
    stored = json.loads(history.path.read_text(encoding='utf-8'))
    assert stored['scans'][:3] == original['scans']
    if known_from == 'device':
        assert stored['devices'][identity]['media_type'] == 'HDD'
    assert stored['scans'][3]['measurements'][identity]['wear_percent_used'] == 0


@pytest.mark.parametrize('media,evidence,wear,plotted', [
    ('SSD', 'nvme_estimate', 0, 0), ('SSD', 'nvme_estimate', 255, 255),
    ('SSD', 'provider_estimate', 18, 18), ('SSD', 'provider_estimate', 0, None),
    ('SSD', 'provider_estimate', 255, None), ('SSD', 'unverified_zero', 0, None),
    ('HDD', 'provider_estimate', 20, None), ('Unknown', 'nvme_estimate', 5, None),
])
def test_wear_plot_requires_appropriate_per_scan_evidence(tmp_path, media, evidence, wear, plotted):
    history = HardwareHealthHistory(tmp_path)
    assert history.record(drive_report(media=media, evidence=evidence, wear=wear))['saved'] is True
    assert wear_points(history)[0]['value'] == plotted
    stored = json.loads(history.path.read_text(encoding='utf-8'))
    measurement = next(iter(stored['scans'][0]['measurements'].values()))
    assert measurement['wear_percent_used'] == wear  # Display masking never deletes a measurement.


def test_new_history_evidence_is_a_privacy_allowlist(tmp_path):
    history = HardwareHealthHistory(tmp_path)
    value = drive_report()
    value['drives'][0].update(serial_number='PRIVATE-SERIAL', raw_smart={'private': 'PRIVATE-SMART'},
        evidence={'media_type': 'SSD', 'private': 'PRIVATE-EVIDENCE'})
    assert history.record(value)['saved'] is True
    raw = history.path.read_text(encoding='utf-8')
    assert 'PRIVATE' not in raw
    stored = json.loads(raw)
    assert set(next(iter(stored['devices'].values()))) == {'label', 'kind', 'identity_quality', 'media_type'}
    assert set(next(iter(stored['scans'][0]['evidence'].values()))) == {'media_type', 'wear_evidence'}


@pytest.mark.parametrize('change', ['device_unknown_field', 'device_bad_media', 'scan_unknown_field',
    'evidence_unknown_field', 'evidence_bad_media', 'evidence_bad_kind', 'evidence_unknown_identity', 'evidence_list'])
def test_invalid_optional_evidence_preserves_existing_file(tmp_path, change):
    history = HardwareHealthHistory(tmp_path)
    assert history.record(drive_report())['saved'] is True
    stored = json.loads(history.path.read_text(encoding='utf-8'))
    identity = next(iter(stored['devices']))
    scan = stored['scans'][0]
    if change == 'device_unknown_field':
        stored['devices'][identity]['serial_number'] = 'PRIVATE'
    elif change == 'device_bad_media':
        stored['devices'][identity]['media_type'] = []
    elif change == 'scan_unknown_field':
        scan['raw_inventory'] = 'PRIVATE'
    elif change == 'evidence_unknown_field':
        scan['evidence'][identity]['source_serial'] = 'PRIVATE'
    elif change == 'evidence_bad_media':
        scan['evidence'][identity]['media_type'] = 'Not a known media type'
    elif change == 'evidence_bad_kind':
        scan['evidence'][identity]['wear_evidence'] = 'guessed_remaining_life'
    elif change == 'evidence_unknown_identity':
        scan['evidence']['b' * 64] = scan['evidence'].pop(identity)
    else:
        scan['evidence'] = []
    history.path.write_text(json.dumps(stored), encoding='utf-8')
    raw = history.path.read_bytes()
    assert history.series()['status'] == 'unavailable'
    assert history.record(drive_report(2))['saved'] is False
    assert history.path.read_bytes() == raw


def test_battery_cannot_receive_physical_drive_evidence(tmp_path):
    history = HardwareHealthHistory(tmp_path)
    history.record(report())
    stored = json.loads(history.path.read_text(encoding='utf-8'))
    identity = next(iter(stored['devices']))
    stored['scans'][0]['evidence'] = {identity: {'media_type': 'SSD', 'wear_evidence': 'nvme_estimate'}}
    history.path.write_text(json.dumps(stored), encoding='utf-8')
    raw = history.path.read_bytes()
    assert history.series()['status'] == 'unavailable' and history.path.read_bytes() == raw


def test_failed_atomic_append_preserves_legacy_values_and_baseline(tmp_path, monkeypatch):
    from optimus import workflow
    history = HardwareHealthHistory(tmp_path)
    legacy_drive_history(history, media='SSD')
    raw = history.path.read_bytes()
    def fail(*_args, **_kwargs):
        raise OSError('Fixture atomic write failure')
    monkeypatch.setattr(workflow, 'atomic_json', fail)
    assert history.record(drive_report(4))['saved'] is False
    assert history.path.read_bytes() == raw
    assert [point['value'] for point in wear_points(history)] == [None, None, None]


def test_metadata_file_size_bound_still_preserves_existing_history(tmp_path, monkeypatch):
    from optimus import hardware_health_history as module
    history = HardwareHealthHistory(tmp_path)
    assert history.record(drive_report())['saved'] is True
    raw = history.path.read_bytes()
    monkeypatch.setattr(module, '_MAX_BYTES', len(raw) + 10)
    assert history.record(drive_report(2))['status'] == 'limit_reached'
    assert history.path.read_bytes() == raw
