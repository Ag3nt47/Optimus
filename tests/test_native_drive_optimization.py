"""Drive tests use fake inventories/processes; native defrag is never launched."""
import copy
import json
import os
from pathlib import Path
import threading
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from optimus import drive_optimization as drive
from optimus.automation import CURRENT_SETUP_VERSION, RunLease


def volume(letter='C', **changes):
    value = {'letter': letter, 'volume_id': 'fixture-volume-' + letter, 'disk_id': 'fixture-disk-' + letter,
             'filesystem': 'NTFS', 'drive_type': 'Fixed', 'health': 'Healthy', 'volume_status': ['OK'],
             'size_bytes': 500 * 1024 ** 3, 'free_bytes': 3 * 1024 ** 3, 'complete': True,
             'media_type': 'SSD', 'disk_number': 0, 'bus': 'NVMe', 'disk_health': 'Healthy',
             'disk_status': ['Online'], 'read_only': False, 'offline': False, 'virtual': False,
             'locked': False, 'accessible': True}
    value.update(changes)
    return value


def inventory(rows=None, **changes):
    value = {'complete': True, 'volumes': [volume()] if rows is None else rows, 'process_inventory_complete': True,
             'busy': [], 'update_busy': False, 'reboot_required': False, 'ac': True,
             'cpu_percent': 10, 'memory_percent': 70, 'available_memory_bytes': 4 * 1024 ** 3}
    value.update(changes)
    return value


class Process:
    def __init__(self, code=0, on_poll=None):
        self.pid = 76543
        self.code = code
        self.on_poll = on_poll
        self.kill = Mock(side_effect=AssertionError('The optimizer must never be killed.'))
        self.terminate = Mock(side_effect=AssertionError('The optimizer must never be terminated.'))

    def poll(self):
        if self.on_poll:
            self.on_poll()
        return self.code


class Adapter:
    def __init__(self, state=None):
        self.state = state or inventory()
        self.process = Process()
        self.launches = []
        self.on_start = None

    def inspect(self, timeout=30):
        return copy.deepcopy(self.state)

    def start(self, letter, log_path):
        self.launches.append(letter)
        if self.on_start:
            self.on_start(letter, log_path)
        Path(log_path).write_text('Fixture native optimizer output, not executed.')
        return self.process


def service(tmp_path, state=None, *, system='win32', admin=True, timeout=1800):
    backend = SimpleNamespace(data_dir=tmp_path, is_admin=Mock(return_value=admin),
        preference_store=SimpleNamespace(validate=lambda config: copy.deepcopy(config)))
    adapter = Adapter(state)
    now = {'value': 2_000_000_000}
    engine = drive.DriveOptimization(backend, system=system, adapter=adapter, owner='fixture-owner',
                                    clock=lambda: now['value'], timeout=timeout)
    return engine, adapter, now


def settings(**changes):
    return dict({'drive_optimization_enabled': True, 'setup_version': CURRENT_SETUP_VERSION}, **changes)


def test_scan_is_readonly_and_low_free_ssd_is_eligible_without_partition_or_cleanup(tmp_path):
    engine, adapter, _ = service(tmp_path)
    result = engine.scan()
    assert result['read_only'] and result['items'][0]['status'] == 'available'
    assert not adapter.launches and not list(tmp_path.iterdir())
    assert 'partition' in result['summary']


def test_windows_filesystem_warning_survives_busy_scope_exclusion_and_noop_final_verification(tmp_path):
    warning = volume('F', filesystem='exFAT', bus='USB', health='Warning',
                     volume_status=['Full Repair Needed'], complete=False, disk_id='')
    engine, adapter, _ = service(tmp_path, inventory([warning], busy=[{'Name': 'defrag.exe', 'ProcessId': 12}]))
    scan = engine.scan()
    reason = scan['items'][0]['reason']
    assert 'Warning' in reason and 'Full Repair Needed' in reason
    assert 'USB' in reason and 'identity' in reason and 'optimizer' in reason
    assert len(scan['health_findings']) == 1 and '1 Windows-reported volume health' in scan['summary']
    result = engine.apply(settings())
    assert result['status'] == 'deferred' and not adapter.launches
    assert result['health_findings'] == scan['health_findings']
    assert 'Full Repair Needed' in result['items'][0]['reason']
    assert engine.verify(result)['status'] == 'needs_review'
    assert 'no repair' in engine.verify(result)['checks'][0]['reason']


@pytest.mark.parametrize('changes', [
    {'bus': 'USB'}, {'bus': 'RAID'}, {'bus': 'Spaces'}, {'drive_type': 'Removable'},
    {'filesystem': 'FAT32'}, {'virtual': True}, {'read_only': True}, {'offline': True},
    {'locked': True}, {'accessible': False}, {'health': 'Warning'}, {'disk_health': 'Unhealthy'},
    {'complete': False}, {'disk_id': ''}, {'letter': 'C & calc'}, {'free_bytes': 1024 ** 3},
    {'media_type': 'HDD'}, {'media_type': 'Unknown'},
])
def test_excluded_or_ambiguous_volumes_never_start_optimizer(tmp_path, changes):
    engine, adapter, _ = service(tmp_path, inventory([volume(**changes)]))
    assert engine.apply(settings())['status'] == 'deferred'
    assert not adapter.launches


@pytest.mark.parametrize('changes', [
    {'ac': False}, {'ac': None}, {'reboot_required': True}, {'reboot_required': None},
    {'update_busy': True}, {'busy': [{'Name': 'defrag.exe', 'ProcessId': 12}]},
    {'process_inventory_complete': False}, {'cpu_percent': 85}, {'memory_percent': 90},
    {'available_memory_bytes': 1024 ** 3 - 1}, {'cpu_percent': None},
])
def test_busy_battery_pending_restart_and_pressure_guards_defer_all_volumes(tmp_path, changes):
    engine, adapter, _ = service(tmp_path, inventory(**changes))
    result = engine.apply(settings())
    assert result['status'] == 'deferred' and not adapter.launches
    assert engine.verify(result)['status'] == 'not_applicable'


def test_known_hdd_and_unclassified_media_use_conservative_free_space_floor(tmp_path):
    rows = [volume('C', media_type='HDD', size_bytes=100 * 1024 ** 3, free_bytes=16 * 1024 ** 3),
            volume('D', media_type='Unknown', size_bytes=100 * 1024 ** 3, free_bytes=16 * 1024 ** 3)]
    engine, adapter, _ = service(tmp_path, inventory(rows))
    result = engine.apply(settings())
    assert adapter.launches == ['C', 'D']
    assert result['counts']['reported_completion'] == 2


def test_prepared_journal_and_speed_specific_lease_exist_before_each_optimizer(tmp_path):
    engine, adapter, _ = service(tmp_path, inventory([volume(), volume('D')]))
    def before_start(letter, log_path):
        paths = [path for path in (tmp_path / 'drive-optimization').glob('*.json') if path.name != 'owner.json']
        saved = json.loads(paths[0].read_text())
        item = next(item for item in saved['items'] if item['before']['letter'] == letter)
        assert item['status'] == 'prepared' and item['native_output_path'] == str(log_path)
        with pytest.raises(RuntimeError):
            RunLease(tmp_path / 'drive-optimization').acquire()
    adapter.on_start = before_start
    result = engine.apply(settings())
    assert result['status'] == 'completed' and result['counts']['reported_completion'] == 2
    assert 'not proof' in result['summary'] and not result['restore_available']
    assert engine.verify(result)['status'] == 'confirmed'


def test_seven_day_cadence_keys_volume_identity_not_drive_letter_and_preserves_history(tmp_path):
    engine, adapter, clock = service(tmp_path)
    first = engine.apply(settings())
    clock['value'] += 3600
    adapter.state['volumes'][0]['letter'] = 'E'
    assert engine.apply(settings())['status'] == 'deferred'
    assert adapter.launches == ['C']
    assert engine.history()[0]['id'] == first['id']
    clock['value'] += drive.CADENCE_SECONDS
    assert engine.apply(settings())['status'] == 'completed'
    assert adapter.launches == ['C', 'E']


def test_timeout_retains_pid_never_kills_and_defers_later_volumes_and_immediate_replay(tmp_path):
    engine, adapter, _ = service(tmp_path, inventory([volume(), volume('D')]), timeout=0)
    adapter.process = Process(code=None)
    result = engine.apply(settings())
    assert adapter.launches == ['C']
    assert result['items'][0]['status'] == 'uncertain' and result['items'][0]['pid'] == adapter.process.pid
    assert result['items'][1]['status'] == 'skipped'
    assert result['counts']['uncertain'] == 1
    adapter.process.kill.assert_not_called()
    adapter.process.terminate.assert_not_called()
    assert engine.verify(result)['status'] == 'needs_review'
    # A live optimizer is an all-volume barrier, independently of cadence.
    adapter.state['busy'] = [{'Name': 'defrag.exe', 'ProcessId': adapter.process.pid}]
    assert engine.apply(settings())['status'] == 'deferred'
    assert adapter.launches == ['C']


def test_cancellation_before_start_or_during_optimizer_never_kills_child(tmp_path):
    engine, adapter, _ = service(tmp_path, inventory([volume(), volume('D')]))
    cancel = threading.Event()
    result = engine.apply(settings(), cancel, progress_callback=lambda _: cancel.set())
    assert result['cancelled'] and not adapter.launches
    cancel.clear()
    adapter.process = Process(code=None, on_poll=lambda: cancel.set())
    result = engine.apply(settings(), cancel)
    assert result['cancelled'] and result['items'][0]['status'] == 'uncertain'
    assert adapter.launches == ['C']
    adapter.process.kill.assert_not_called()
    adapter.process.terminate.assert_not_called()


def test_native_failure_or_launch_exception_stops_remaining_volumes(tmp_path):
    engine, adapter, _ = service(tmp_path, inventory([volume(), volume('D')]))
    adapter.process = Process(code=5)
    result = engine.apply(settings())
    assert result['failed_items'] == 1 and adapter.launches == ['C']
    assert result['items'][1]['status'] == 'skipped'


def test_disabled_setup_nonwindows_and_nonadmin_never_launch(tmp_path):
    engine, adapter, _ = service(tmp_path, admin=False)
    assert engine.apply(settings())['status'] == 'deferred'
    engine.backend.is_admin.return_value = True
    assert engine.apply(settings(drive_optimization_enabled=False))['status'] == 'skipped'
    assert engine.apply(settings(setup_version=0))['status'] == 'skipped'
    engine.system = 'linux'
    assert engine.apply(settings())['status'] == 'skipped'
    assert not adapter.launches


def test_owned_journal_tampering_hardlink_or_forged_input_never_confirms(tmp_path):
    engine, adapter, _ = service(tmp_path)
    assert engine.verify({'id': 'a' * 32})['status'] == 'unverified'
    result = engine.apply(settings())
    path = Path(result['log_path'])
    raw = path.read_bytes()
    forged = json.loads(raw)
    forged['items'][0]['before']['letter'] = 'Z'
    path.write_text(json.dumps(forged))
    assert engine.verify(result)['status'] == 'unverified'
    with pytest.raises(ValueError):
        engine.apply(settings())
    path.write_bytes(raw)
    try:
        os.link(path, tmp_path / 'linked.json')
    except OSError:
        return
    assert engine.verify(result)['status'] == 'unverified'
    assert adapter.launches == ['C']


def test_journal_failure_prevents_process_launch(tmp_path, monkeypatch):
    engine, adapter, _ = service(tmp_path)
    monkeypatch.setattr(drive, '_private_json', Mock(side_effect=OSError('Fixture disk full')))
    with pytest.raises(OSError):
        engine.apply(settings())
    assert not adapter.launches


def test_fresh_volume_identity_and_native_exit_code_are_required_for_confirmation(tmp_path):
    engine, adapter, _ = service(tmp_path)
    result = engine.apply(settings())
    adapter.state['volumes'][0]['disk_id'] = 'replacement-disk'
    assert engine.verify(result)['status'] == 'unverified'
    assert len(adapter.launches) == 1


def test_native_command_scope_is_only_exact_volume_and_media_appropriate_optimization():
    source = Path(drive.__file__).read_text()
    assert "letter + ':', '/O', '/U', '/V'" in source
    assert '/C' not in source and '/X' not in source
    assert 'format.exe' not in source and 'diskpart' not in source
