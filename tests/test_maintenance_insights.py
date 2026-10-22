"""Only disposable app metadata and supplied audit fixtures are inspected."""
import copy
import json
import os
from pathlib import Path
from unittest.mock import patch

import pytest

from optimus.maintenance_insights import StartupDrift, build_insights, summarize_resources

GIB = 1024 ** 3


def startup(number=1, **changes):
    value = {'Id': 'Registry-' + format(number, '064x'), 'Fingerprint': 'a' * 64,
             'Name': 'Fixture program ' + str(number), 'Source': 'Run registry (CurrentUser, Registry32)',
             'EntryType': 'Registry', 'Enabled': True, 'Protected': False,
             'Command': 'fixture.exe --token PRIVATE_FIXTURE_COMMAND'}
    value.update(changes)
    return value


def audit(*, exact=True, free=3 * GIB, available=2 * GIB, cpu=20, used=75, startup_rows=None, complete=True):
    system = {'CapturedAtUtc': '2026-10-22T01:00:00Z', 'CpuPercent': cpu, 'MemoryUsedPercent': used}
    disk = {'Drive': 'C:', 'FreePercent': free / (100 * GIB) * 100}
    if exact:
        system.update(MemoryAvailableBytes=available, MemoryTotalBytes=8 * GIB)
        disk.update(FreeBytes=free, SizeBytes=100 * GIB)
    else:
        system.update(MemoryAvailableGB=available / GIB, MemoryTotalGB=8)
        disk.update(FreeGB=free / GIB, SizeGB=100)
    system['Disks'] = [disk]
    return {'System': system, 'Startup': [startup()] if startup_rows is None else startup_rows,
            'InventoryErrors': [], 'StartupInventoryComplete': complete}


def report(before=None, after=None):
    return {'id': 'f' * 32, 'stages': [
        {'title': 'Inspect current resource usage', 'status': 'completed', 'result': before or audit()},
        {'title': 'Capture final resource report', 'status': 'completed', 'result': after or audit()}]}


def baseline_path(root):
    return root / 'MaintenanceInsights/startup-baseline.json'


def test_exact_observations_keep_signed_deltas_without_causal_gain_claim():
    value = summarize_resources(audit(free=3 * GIB, available=GIB, cpu=30, used=87),
                                audit(free=4 * GIB + 23, available=2 * GIB, cpu=10, used=75))
    assert value['status'] == 'observed' and value['approximate'] is False
    assert value['cpu_percent']['delta_percentage_points'] == -20
    assert value['memory_used_percent']['delta_percentage_points'] == -12
    assert value['memory_available_bytes']['delta_bytes'] == GIB
    assert value['disks'][0]['delta_bytes'] == GIB + 23
    assert any('not proof' in note for note in value['notes'])
    assert not {'actions', 'speed_improvement', 'saved_bytes'} & value.keys()


def test_legacy_rounded_gib_observations_are_explicitly_approximate():
    value = summarize_resources(audit(exact=False), audit(exact=False, free=4 * GIB))
    assert value['status'] == 'observed' and value['approximate'] is True
    assert value['memory_available_bytes']['approximate'] is True
    assert value['disks'][0]['approximate'] is True
    assert value['disks'][0]['delta_bytes'] == GIB


def test_missing_before_and_missing_drive_do_not_become_zero_measurements():
    value = summarize_resources({}, audit())
    assert value['status'] == 'unavailable'
    assert value['memory_available_bytes']['before'] is None
    assert value['memory_available_bytes']['delta_bytes'] is None
    assert value['disks'][0]['before'] is None and value['disks'][0]['delta_bytes'] is None
    assert any('unavailable' in note for note in value['warnings'])


@pytest.mark.parametrize('number', [True, None, '1', float('nan'), float('inf'), -1, 10 ** 1000],
                         ids=['boolean', 'missing', 'string', 'nan', 'infinity', 'negative', 'huge'])
def test_invalid_measurements_never_fabricate_a_delta(number):
    final = audit()
    final['System']['MemoryAvailableBytes'] = number
    value = summarize_resources(audit(), final)
    assert value['status'] == 'partial'
    assert value['memory_available_bytes']['after'] is None
    assert value['memory_available_bytes']['delta_bytes'] is None


def test_exact_field_error_does_not_fall_back_to_good_looking_rounded_field():
    final = audit()
    final['System'].update(MemoryAvailableBytes='unknown', MemoryAvailableGB=2, MemoryTotalGB=8)
    final['System']['Disks'][0].update(FreeBytes=True, FreeGB=3, SizeGB=100)
    value = summarize_resources(audit(), final)
    assert value['memory_available_bytes']['after'] is None
    assert value['disks'][0]['after'] is None


def test_duplicate_volume_labels_fail_closed_and_low_space_and_ram_stay_visible():
    final = audit(free=GIB, used=91)
    final['System']['Disks'].append(dict(final['System']['Disks'][0]))
    malformed = summarize_resources(audit(), final)
    assert malformed['status'] == 'partial' and malformed['disks'][0]['after'] is None
    normal = summarize_resources(audit(), audit(free=GIB, used=91))
    assert normal['disks'][0]['pressure'] == 'low'
    assert any('85%' in message for message in normal['warnings'])
    assert any('2 GiB or 5%' in message for message in normal['warnings'])


def test_empty_native_volume_inventory_is_unknown_not_a_successful_zero_disk_sample():
    final = audit()
    final['System']['Disks'] = []
    value = summarize_resources(audit(), final)
    assert value['status'] == 'partial'
    assert value['disks'][0]['after'] is None
    assert any('disk measurement was unavailable' in note for note in value['warnings'])


def test_native_measurement_failures_cannot_impersonate_zero_cpu_or_complete_disks():
    final = audit(cpu=0)
    final['System'].update(CpuMeasurementAvailable=False, DiskInventoryComplete=False,
                           MeasurementErrors=['Fixture CPU and disk provider failure'])
    value = summarize_resources(audit(), final)
    assert value['status'] == 'partial'
    assert value['cpu_percent']['after'] is None
    assert value['cpu_percent']['delta_percentage_points'] is None
    assert value['disks'][0]['after'] is None
    assert any('producer reported unavailable' in text for text in value['warnings'])


def test_complete_native_markers_accept_real_zero_cpu_and_exact_bytes():
    first, final = audit(cpu=0), audit(cpu=0)
    for row in (first, final):
        row['System'].update(CpuMeasurementAvailable=True, DiskInventoryComplete=True, MeasurementErrors=[])
    value = summarize_resources(first, final)
    assert value['status'] == 'observed' and value['cpu_percent']['delta_percentage_points'] == 0


def test_exact_same_volume_bytes_determine_free_percent_over_conflicting_rounded_label():
    final = audit(free=10 * GIB)
    final['System']['Disks'][0].update(SizeBytes=1000 * GIB, FreePercent=99)
    row = summarize_resources(audit(), final)['disks'][0]
    assert row['after_free_percent'] == 1 and row['pressure'] == 'low'


def test_baseline_creation_does_not_claim_historical_changes_or_store_commands(tmp_path):
    result = StartupDrift(tmp_path).compare([startup(Name='Bing Wallpaper')], complete=True,
                                          run_id='a' * 32, captured_at='2026-10-22T01:00:00Z')
    assert result['status'] == 'baseline_created' and result['baseline_saved'] is True
    assert result['counts'] == {'new': 0, 'removed': 0, 'changed': 0}
    raw = baseline_path(tmp_path).read_text()
    stored = json.loads(raw)
    assert stored['records'][0]['protected'] is True
    assert 'PRIVATE_FIXTURE_COMMAND' not in raw and 'Command' not in raw
    assert not {'actions', 'failed_items', 'approvals'} & result.keys()


def test_new_removed_changed_and_disabled_rows_follow_exact_live_identity(tmp_path):
    tracker = StartupDrift(tmp_path)
    tracker.compare([startup(1), startup(2), startup(3)], complete=True, run_id='a' * 32)
    value = tracker.compare([startup(1, Fingerprint='b' * 64), startup(2, Enabled=False), startup(4)],
                            complete=True, run_id='b' * 32)
    assert value['status'] == 'compared'
    assert value['counts'] == {'new': 1, 'removed': 2, 'changed': 1}
    assert value['new'][0]['id'].endswith(format(4, '064x'))
    assert value['changed'][0]['fields'] == ['fingerprint']
    assert value['baseline_run_id'] == 'a' * 32
    assert all(row['enabled'] for row in json.loads(baseline_path(tmp_path).read_text())['records'])
    assert any('do not classify' in text for text in value['notes'])


def test_unreadable_disabled_backup_metadata_does_not_impersonate_live_coverage(tmp_path):
    value = StartupDrift(tmp_path).compare([startup(), {'Enabled': False, 'Fingerprint': ''}], complete=True)
    assert value['status'] == 'baseline_created'
    assert len(json.loads(baseline_path(tmp_path).read_text())['records']) == 1


@pytest.mark.parametrize('complete', [False, None, 1, 'yes'])
def test_no_explicit_complete_inventory_never_creates_or_changes_baseline(tmp_path, complete):
    root = tmp_path / 'not-created'
    result = StartupDrift(root).compare([startup()], complete=complete)
    assert result['status'] == 'unverified' and not result['baseline_saved']
    assert not root.exists() and result['errors'] == []


@pytest.mark.parametrize('records', [[startup(), startup()], [startup(Fingerprint='')],
    [startup(Id='Registry-' + 'z' * 64)], [startup(Enabled='true')], [None], [startup(Source='')],
    [startup(EntryType='Shortcut')], [startup(Protected='false')]])
def test_ambiguous_or_unfingerprinted_inventory_cannot_replace_baseline(tmp_path, records):
    tracker = StartupDrift(tmp_path)
    tracker.compare([startup()], complete=True)
    original = baseline_path(tmp_path).read_bytes()
    value = tracker.compare(records, complete=True)
    assert value['status'] == 'unverified' and value['baseline_saved'] is False
    assert baseline_path(tmp_path).read_bytes() == original


def test_1000_entries_are_supported_but_1001_cannot_overwrite(tmp_path):
    tracker = StartupDrift(tmp_path)
    value = tracker.compare([startup(number) for number in range(1000)], complete=True)
    assert value['status'] == 'baseline_created'
    before = baseline_path(tmp_path).read_bytes()
    refused = tracker.compare([startup(number) for number in range(1001)], complete=True)
    assert refused['status'] == 'unverified' and baseline_path(tmp_path).read_bytes() == before


@pytest.mark.parametrize('raw', [b'{broken', b'{}', b'{"schema":true}', b'x' * (2 * 1024 * 1024 + 1)],
                         ids=['broken', 'empty', 'wrong-schema', 'oversized'])
def test_corrupt_or_oversized_baseline_is_preserved_without_silent_reset(tmp_path, raw):
    path = baseline_path(tmp_path)
    path.parent.mkdir()
    path.write_bytes(raw)
    result = StartupDrift(tmp_path).compare([startup()], complete=True)
    assert result['status'] == 'unverified' and result['errors']
    assert path.read_bytes() == raw


def test_hardlinked_private_baseline_is_never_opened_as_write_authority(tmp_path):
    outside = tmp_path / 'outside.json'
    outside.write_text('private outside fixture')
    path = baseline_path(tmp_path)
    path.parent.mkdir()
    os.link(outside, path)
    result = StartupDrift(tmp_path).compare([startup()], complete=True)
    assert result['status'] == 'unverified' and not result['baseline_saved']
    assert outside.read_text() == 'private outside fixture' and path.stat().st_nlink == 2


def test_linked_ancestor_refuses_all_baseline_writes(tmp_path, monkeypatch):
    folder = tmp_path / 'MaintenanceInsights'
    folder.mkdir()
    original = Path.lstat

    def indirect(path):
        info = original(path)
        if path == folder:
            from types import SimpleNamespace
            return SimpleNamespace(st_mode=info.st_mode, st_file_attributes=0x400)
        return info

    monkeypatch.setattr(Path, 'lstat', indirect)
    result = StartupDrift(tmp_path).compare([startup()], complete=True)
    assert result['status'] == 'unverified' and not baseline_path(tmp_path).exists()


def test_a_newer_baseline_wins_if_it_changes_during_comparison(tmp_path, monkeypatch):
    tracker = StartupDrift(tmp_path)
    tracker.compare([startup()], complete=True)
    path = baseline_path(tmp_path)
    save = tracker._save
    replacement = path.read_text().replace('Fixture program 1', 'A newer caller record')

    def race(target, value, previous):
        temporary = target.with_suffix('.new')
        temporary.write_text(replacement)
        temporary.replace(target)
        return save(target, value, previous)

    monkeypatch.setattr(tracker, '_save', race)
    result = tracker.compare([startup(2)], complete=True)
    assert result['status'] == 'unverified' and result['counts']['new'] == 1
    assert path.read_text() == replacement
    assert not list(path.parent.glob('.startup-baseline-*.tmp'))


def test_replace_failure_keeps_previous_bytes_and_cleans_its_own_temporary(tmp_path):
    tracker = StartupDrift(tmp_path)
    tracker.compare([startup()], complete=True)
    before = baseline_path(tmp_path).read_bytes()
    with patch('optimus.maintenance_insights.replace_with_retry', side_effect=PermissionError('fixture')):
        value = tracker.compare([startup(2)], complete=True)
    assert value['status'] == 'unverified' and baseline_path(tmp_path).read_bytes() == before
    assert not list(baseline_path(tmp_path).parent.glob('.startup-baseline-*.tmp'))


def test_build_requires_explicit_scoped_native_completeness_and_inventory_success(tmp_path):
    first = build_insights(report(), tmp_path)
    assert first['schema'] == 1 and first['read_only'] is True
    assert first['startup_changes']['status'] == 'baseline_created'
    assert first['resource_changes']['status'] == 'observed'
    previous = baseline_path(tmp_path).read_bytes()
    for label in ('absent-marker', 'incomplete-audit', 'startup-errors', 'failed-final-stage', 'duplicate-final'):
        run = report(after=audit(startup_rows=[startup(2)]))
        if label == 'absent-marker':
            del run['stages'][-1]['result']['StartupInventoryComplete']
        elif label == 'incomplete-audit':
            run['stages'][-1]['result']['InventoryErrors'] = ['Startup inventory fixture error']
        elif label == 'startup-errors':
            run['stages'][-1]['result']['StartupInventoryErrors'] = ['Contradictory producer metadata']
        elif label == 'failed-final-stage':
            run['stages'][-1]['status'] = 'failed'
        else:
            run['stages'].append(copy.deepcopy(run['stages'][-1]))
        result = build_insights(run, tmp_path)
        assert result['startup_changes']['status'] == 'unverified'
        assert baseline_path(tmp_path).read_bytes() == previous
        assert not {'failed_items', 'actions'} & result.keys()


def test_empty_or_malformed_report_is_unknown_not_all_clear(tmp_path):
    for run in (None, {}, {'stages': 'unknown'}):
        value = build_insights(run, tmp_path)
        assert value['resource_changes']['status'] == 'unavailable'
        assert value['startup_changes']['status'] == 'unverified'
        assert 'not proof' in value['summary']
    assert not baseline_path(tmp_path).exists()


def test_prominent_summary_includes_observed_metrics_and_retains_unknowns(tmp_path):
    run = report(before=audit(free=3 * GIB, available=GIB, cpu=30),
                 after=audit(free=4 * GIB, available=2 * GIB, cpu=10))
    summary = build_insights(run, tmp_path)['summary']
    assert 'CPU 30.0% -> 10.0%' in summary
    assert 'available RAM 1.00 GiB -> 2.00 GiB' in summary
    assert 'C: free 3.00 GiB -> 4.00 GiB' in summary
    assert 'not proof' in summary
    run['stages'][0]['result']['System'].clear()
    partial = build_insights(run, tmp_path)['summary']
    assert 'CPU unavailable -> 10.0%' in partial
    assert 'available RAM unavailable -> 2.00 GiB' in partial
    assert 'C: free unavailable -> 4.00 GiB' in partial


def test_prominent_summary_bounds_volume_rows_and_points_to_details(tmp_path):
    run = report()
    for stage in run['stages']:
        disk = stage['result']['System']['Disks'][0]
        stage['result']['System']['Disks'] = [dict(disk, Drive=drive) for drive in ('C:', 'D:', 'E:', 'F:')]
    summary = build_insights(run, tmp_path)['summary']
    assert 'C: free' in summary and 'E: free' in summary
    assert 'F: free' not in summary and '1 additional volume in resource details' in summary
