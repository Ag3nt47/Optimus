"""Native battery/drive health uses disposable sysfs and command fixtures only."""
import copy
from functools import partial
import io
import json
from pathlib import Path
import plistlib
import threading
from unittest.mock import Mock

import pytest

from optimus import native_hardware_health as health


@pytest.fixture(autouse=True)
def deterministic_collection_clock(monkeypatch):
    # Windows filesystem fixtures can be delayed by unrelated endpoint tools.
    # Collector deadline tests explicitly supply their own advancing clock.
    monkeypatch.setattr(health, 'collect_native_hardware_health',
                        partial(health.collect_native_hardware_health, clock=lambda: 0.0))


class Commands:
    def __init__(self, *, disks=None, smart=None, power=None, mac_disks=None, info=None):
        self.disks = disks or []
        self.smart = smart or {}
        self.power = [] if power is None else power
        self.mac_disks = [] if mac_disks is None else mac_disks
        self.info = info or {}
        self.calls = []

    def __call__(self, argv, **kwargs):
        self.calls.append((argv, kwargs))
        assert health._allowed_argv(argv)
        assert 0 < kwargs['timeout'] <= 4 and 'cancel_event' in kwargs
        tool = Path(argv[0]).name
        code = 0
        if tool == 'lsblk':
            data = json.dumps({'blockdevices': self.disks}).encode()
        elif tool == 'smartctl':
            value = self.smart.get(argv[-1], {'smartctl': {'exit_status': 2}})
            code = value['smartctl']['exit_status']
            data = json.dumps(value).encode()
        elif tool == 'system_profiler':
            data = json.dumps({'SPPowerDataType': self.power}).encode()
        elif tool == 'diskutil' and argv[1] == 'list':
            data = plistlib.dumps({'AllDisksAndPartitions': self.mac_disks})
        elif tool == 'diskutil':
            data = plistlib.dumps(self.info[argv[-1]])
        else:
            raise AssertionError('Unexpected fixture command.')
        return {'exit_code': code, 'stdout': data, 'stderr': b''}


def sysfs(tmp_path):
    batteries, blocks = tmp_path / 'power_supply', tmp_path / 'block'
    batteries.mkdir()
    blocks.mkdir()
    return batteries, blocks


def battery(root, name='BAT0', **values):
    path = root / name
    path.mkdir()
    for key, value in dict(type='Battery', present='1', **values).items():
        (path / key).write_text(str(value), encoding='utf-8')
    return path


def linux(tmp_path, commands=None, **kwargs):
    batteries, blocks = sysfs(tmp_path)
    return batteries, blocks, commands or Commands(), dict(
        platform_name='Linux', command=commands or Commands(), battery_root=batteries,
        block_root=blocks, **kwargs)


def disk(name='sda', **changes):
    return dict({'name': name, 'kname': name, 'type': 'disk', 'size': 1000000000000,
                 'model': 'Fixture model', 'rota': True, 'rm': False, 'tran': 'sata'}, **changes)


def smart(device='/dev/sda', *, passed=True, **changes):
    return dict({'smartctl': {'exit_status': 0 if passed else 8}, 'device': {'name': device},
                 'smart_status': {'passed': passed}, 'serial_number': 'DO-NOT-EXPORT-SERIAL'}, **changes)


def test_linux_energy_capacity_units_temperature_cycles_and_private_identity(tmp_path):
    batteries, blocks = sysfs(tmp_path)
    battery(batteries, energy_full=42000000, energy_full_design=60000000, charge_full=100,
            charge_full_design=200, capacity=99, cycle_count=123, temp=315, health='Good',
            manufacturer='Fixture Maker', model_name='Fixture Battery', serial_number='PRIVATE-BATTERY-12345')
    result = health.collect_native_hardware_health('Linux', command=Commands(), battery_root=batteries, block_root=blocks)
    row = result['batteries'][0]
    assert row['full_charge_capacity'] == 42000000 and row['design_capacity'] == 60000000
    assert row['capacity_unit'] == 'uWh' and row['cycle_count'] == 123 and row['temp_c'] == 31.5
    assert row['condition'] == 'Good' and row['identity_quality'] == 'stable' and len(row['identity_hash']) == 64
    assert 'PRIVATE-BATTERY' not in json.dumps(result) and 'serial_number' not in row
    assert result['read_only'] is True and result['status'] == 'completed'


def test_linux_never_mixes_energy_with_charge_or_charge_percent(tmp_path):
    batteries, blocks = sysfs(tmp_path)
    battery(batteries, energy_full=40000000, charge_full_design=6000000, capacity=95, cycle_count=0)
    row = health.collect_native_hardware_health('Linux', command=Commands(), battery_root=batteries, block_root=blocks)['batteries'][0]
    assert row['full_charge_capacity'] is None and row['design_capacity'] is None
    assert row['capacity_unit'] is None and row['cycle_count'] is None
    assert row['temp_c'] is None and row['identity_hash'] is None


def test_linux_falls_back_only_to_complete_same_unit_charge_pair(tmp_path):
    batteries, blocks = sysfs(tmp_path)
    battery(batteries, energy_full=0, energy_full_design=0, charge_full=4500000, charge_full_design=5000000)
    row = health.collect_native_hardware_health('Linux', command=Commands(), battery_root=batteries, block_root=blocks)['batteries'][0]
    assert (row['full_charge_capacity'], row['design_capacity'], row['capacity_unit']) == (4500000, 5000000, 'uAh')


@pytest.mark.parametrize('value', ['unknown', '000000000', 'Default string', 'To be filled by O.E.M.', 'PRIVATE\nBAD', 'x' * 513])
def test_placeholder_or_malformed_battery_serial_cannot_join_history(tmp_path, value):
    assert health._strong_identity('Linux/battery', ['Fixture Maker', 'Fixture model', value]) is None


def test_duplicate_native_battery_identity_is_never_treated_unique(tmp_path):
    batteries, blocks = sysfs(tmp_path)
    for name in ('BAT0', 'BAT1'):
        battery(batteries, name, manufacturer='Fixture Maker', model_name='Fixture model', serial_number='SHARED-12345')
    rows = health.collect_native_hardware_health('Linux', command=Commands(), battery_root=batteries, block_root=blocks)['batteries']
    assert len(rows) == 2 and all(row['identity_quality'] == 'unavailable' for row in rows)


@pytest.mark.parametrize('passed', [True, False])
def test_linux_smart_health_is_exact_device_bound_and_failures_are_not_command_errors(tmp_path, passed):
    batteries, blocks = sysfs(tmp_path)
    path = blocks / 'sda'
    path.mkdir()
    (path / 'wwid').write_text('naa.5000c50012345678')
    commands = Commands(disks=[disk()], smart={'/dev/sda': smart(passed=passed, temperature={'current': 33})})
    result = health.collect_native_hardware_health('Linux', command=commands, battery_root=batteries, block_root=blocks)
    row = result['storage'][0]
    assert row['smart_passed'] is passed and row['health_status'] == ('Passed' if passed else 'Failed')
    assert row['temp_c'] == 33 and row['power_on_hours'] is None and row['wear_percent_used'] is None
    assert row['identity_quality'] == 'stable' and '5000c50012345678' not in json.dumps(result)
    assert 'DO-NOT-EXPORT-SERIAL' not in json.dumps(result)
    assert '-q' in commands.calls[-1][0] and 'noserial' in commands.calls[-1][0]


def test_linux_nvme_uses_native_percentage_used_even_over_100(tmp_path):
    batteries, blocks = sysfs(tmp_path)
    commands = Commands(disks=[disk('nvme0n1', tran='nvme')], smart={'/dev/nvme0n1': smart('/dev/nvme0n1',
        nvme_smart_health_information_log={'percentage_used': 127}, power_on_time={'hours': 8400})})
    row = health.collect_native_hardware_health('Linux', command=commands, battery_root=batteries, block_root=blocks)['storage'][0]
    assert row['wear_percent_used'] == 127 and row['power_on_hours'] == 8400


@pytest.mark.parametrize('change', [
    {'device': {'name': '/dev/sdb'}}, {'smartctl': {'exit_status': 3}},
    {'smartctl': {'exit_status': 1}}, {'smart_status': {'passed': 'true'}},
    {'smartctl': {'exit_status': 8}, 'smart_status': {'passed': True}},
])
def test_linux_smart_errors_sleep_or_contradictions_never_claim_healthy(tmp_path, change):
    batteries, blocks = sysfs(tmp_path)
    value = smart(**change)
    commands = Commands(disks=[disk()], smart={'/dev/sda': value})
    result = health.collect_native_hardware_health('Linux', command=commands, battery_root=batteries, block_root=blocks)
    assert result['storage'][0]['smart_passed'] is None and result['status'] == 'partial'


def test_linux_does_not_probe_usb_virtual_or_unsupported_transports(tmp_path):
    batteries, blocks = sysfs(tmp_path)
    commands = Commands(disks=[disk('sda', tran='usb'), disk('vda', tran=None)])
    result = health.collect_native_hardware_health('Linux', command=commands, battery_root=batteries, block_root=blocks)
    assert len(result['storage']) == 2 and len(commands.calls) == 1
    assert all(row['smart_passed'] is None for row in result['storage'])


def test_linux_opaque_wwid_survives_kernel_name_change_but_not_missing_identity(tmp_path):
    batteries, blocks = sysfs(tmp_path)
    ids = []
    for name in ('sda', 'sdb'):
        path = blocks / name
        path.mkdir()
        (path / 'wwid').write_text('eui.1122334455667788')
        commands = Commands(disks=[disk(name, tran='usb')])
        ids.append(health.collect_native_hardware_health('Linux', command=commands, battery_root=batteries, block_root=blocks)['storage'][0]['identity_hash'])
    assert ids[0] is not None and ids[0] == ids[1]


def test_mac_native_health_percentage_is_not_current_charge_or_invented_absolute_capacity():
    power = [{'sppower_battery_health_info': {'sppower_battery_cycle_count': 47,
              'sppower_battery_health': 'Normal', 'sppower_battery_maximum_capacity': '88%'},
              'sppower_battery_charge_info': {'sppower_battery_state_of_charge': 11},
              'sppower_battery_model_info': {'sppower_battery_manufacturer': 'Fixture Maker',
                  'sppower_battery_device_name': 'Fixture Model', 'sppower_battery_serial_number': 'PRIVATE-APPLE-12345'}}]
    result = health.collect_native_hardware_health('Darwin', command=Commands(power=power))
    row = result['batteries'][0]
    assert row['reported_health_capacity_percent'] == 88 and row['cycle_count'] == 47
    assert row['full_charge_capacity'] is None and row['design_capacity'] is None and row['capacity_unit'] is None
    assert row['identity_quality'] == 'stable' and 'PRIVATE-APPLE' not in json.dumps(result)


@pytest.mark.parametrize('status,passed', [('Verified', True), ('Failing', False), ('Not Supported', None), ('unknown', None)])
def test_mac_physical_disk_smart_status_and_unknown_wear_are_preserved(status, passed):
    commands = Commands(mac_disks=[{'DeviceIdentifier': 'disk0'}], info={'disk0': {
        'DeviceIdentifier': 'disk0', 'Whole': True, 'VirtualOrPhysical': 'Physical',
        'MediaName': 'APPLE SSD Fixture', 'TotalSize': 1000000000000, 'BusProtocol': 'PCI-Express',
        'SMARTStatus': status, 'VolumeUUID': 'NOT-A-PHYSICAL-ID', 'SerialNumber': 'NEVER-EXPORT'}})
    result = health.collect_native_hardware_health('Darwin', command=commands)
    row = result['storage'][0]
    assert row['smart_passed'] is passed and row['wear_percent_used'] is None and row['temp_c'] is None
    assert row['identity_quality'] == 'unavailable' and row['identity_hash'] is None
    assert 'NOT-A-PHYSICAL-ID' not in json.dumps(result) and 'NEVER-EXPORT' not in json.dumps(result)


def test_mac_refuses_partition_ids_virtual_or_changed_whole_disk():
    commands = Commands(mac_disks=[{'DeviceIdentifier': 'disk0s1'}, {'DeviceIdentifier': 'disk1'}],
                        info={'disk1': {'DeviceIdentifier': 'disk2', 'Whole': True, 'SMARTStatus': 'Verified'}})
    result = health.collect_native_hardware_health('Darwin', command=commands)
    assert len(result['storage']) == 1 and result['storage'][0]['smart_passed'] is None
    assert len(commands.calls) == 3  # power, list, one whole-disk info


def test_failing_battery_query_does_not_skip_independent_disk_inventory():
    fixture = Commands(mac_disks=[])
    def command(argv, **kwargs):
        if Path(argv[0]).name == 'system_profiler':
            raise TimeoutError('private timeout details')
        return fixture(argv, **kwargs)
    result = health.collect_native_hardware_health('Darwin', command=command)
    assert result['status'] == 'partial' and len(fixture.calls) == 1
    assert 'private timeout' not in json.dumps(result)


def test_cancelled_collection_never_invokes_tools_or_sysfs(tmp_path):
    event = threading.Event()
    event.set()
    command = Mock(side_effect=AssertionError('No command allowed after cancellation.'))
    result = health.collect_native_hardware_health('Linux', event, command=command, battery_root=tmp_path / 'missing')
    assert result['status'] == 'cancelled' and result['storage'] == [] and result['batteries'] == []
    command.assert_not_called()


@pytest.mark.parametrize('value', [0, -1, 31, True, float('nan'), float('inf'), '12'])
def test_collection_budget_is_strict_and_finite(value):
    with pytest.raises(ValueError):
        health.collect_native_hardware_health('Linux', command=Commands(), max_seconds=value)


@pytest.mark.parametrize('argv', [
    ['/bin/sh', '-c', 'true'], ['/usr/sbin/diskutil', 'repairDisk', 'disk0'],
    ['/usr/sbin/diskutil', 'info', '-plist', 'disk0;evil'],
    ['/usr/sbin/system_profiler', 'SPHardwareDataType', '-json'],
    ['/usr/sbin/smartctl', '-t', 'short', '/dev/sda'],
    ['/usr/bin/lsblk', '--json', '--output', 'SERIAL,WWN'],
])
def test_readonly_command_allowlist_rejects_mutations_or_unrequested_hardware_exports(argv):
    assert not health._allowed_argv(argv)
    with pytest.raises(ValueError):
        health._bounded_command(argv, timeout=.1)


def test_oversized_native_output_is_refused_before_json_parsing():
    command = Mock(return_value={'exit_code': 0, 'stdout': b'x' * (health._MAX_STDOUT + 1), 'stderr': b''})
    result = health.collect_native_hardware_health('Darwin', command=command)
    assert result['status'] == 'partial' and result['storage'] == [] and result['batteries'] == []


def test_linux_missing_health_counters_do_not_become_zero(tmp_path):
    batteries, blocks = sysfs(tmp_path)
    commands = Commands(disks=[disk('sda', tran='usb')])
    row = health.collect_native_hardware_health('Linux', command=commands, battery_root=batteries, block_root=blocks)['storage'][0]
    for key in ('temp_c', 'wear_percent_used', 'power_on_hours', 'uncorrected_read_errors', 'uncorrected_write_errors'):
        assert row[key] is None


def test_linux_unreadable_supply_type_cannot_claim_no_battery(tmp_path):
    batteries, blocks = sysfs(tmp_path)
    (batteries / 'BAT0').mkdir()
    result = health.collect_native_hardware_health('Linux', command=Commands(), battery_root=batteries, block_root=blocks)
    assert result['batteries'] == [] and result['capabilities']['battery_health'] == 'unavailable'
    assert result['status'] == 'partial'


def test_mac_unreadable_physical_identity_cannot_claim_no_drives():
    result = health.collect_native_hardware_health('Darwin', command=Commands(mac_disks=[{'DeviceIdentifier': None}]))
    assert result['storage'] == [] and result['capabilities']['storage_health'] == 'unavailable'


@pytest.mark.parametrize('nested', ['smartctl', 'device', 'smart_status', 'temperature', 'power_on_time'])
def test_malformed_nested_smart_fields_are_unknown_not_exceptions(tmp_path, nested):
    batteries, blocks = sysfs(tmp_path)
    value = smart(**{nested: None})
    if nested == 'smartctl':
        command = Commands(disks=[disk()])
        original = command.__call__
        def run(argv, **kwargs):
            if Path(argv[0]).name == 'smartctl':
                return {'exit_code': 0, 'stdout': json.dumps(value).encode(), 'stderr': b''}
            return original(argv, **kwargs)
    else:
        run = Commands(disks=[disk()], smart={'/dev/sda': value})
    result = health.collect_native_hardware_health('Linux', command=run, battery_root=batteries, block_root=blocks)
    assert len(result['storage']) == 1
    if nested in ('smartctl', 'device', 'smart_status'):
        assert result['storage'][0]['smart_passed'] is None
    else:
        assert result['storage'][0]['smart_passed'] is True


def test_mac_battery_presence_without_health_is_unknown_not_no_battery():
    result = health.collect_native_hardware_health('Darwin', command=Commands(power=[{
        'sppower_battery_model_info': {'sppower_battery_manufacturer': 'Fixture Maker'}}]))
    assert len(result['batteries']) == 1
    assert result['capabilities']['battery_health'] == 'not_exposed'
    assert result['batteries'][0]['reported_health_capacity_percent'] is None


@pytest.mark.parametrize('payload', [b'not a plist', b'<plist><dict><key>unfinished'])
def test_mac_malformed_plist_cannot_abort_the_caller(payload):
    normal = Commands()
    def command(argv, **kwargs):
        if Path(argv[0]).name == 'diskutil':
            return {'exit_code': 0, 'stdout': payload, 'stderr': b''}
        return normal(argv, **kwargs)
    result = health.collect_native_hardware_health('Darwin', command=command)
    assert result['status'] == 'partial' and result['storage'] == []


def test_total_collection_deadline_stops_before_a_new_native_operation(tmp_path):
    batteries, blocks = sysfs(tmp_path)
    battery(batteries)
    readings = iter(n / 3 for n in range(30))
    command = Mock(side_effect=AssertionError('No command after collection deadline.'))
    result = health.collect_native_hardware_health('Linux', command=command,
        battery_root=batteries, block_root=blocks, max_seconds=1, clock=lambda: next(readings))
    assert result['status'] == 'partial' and result['errors']
    command.assert_not_called()


@pytest.mark.parametrize('value', [10 ** 1000, str(10 ** 1000), True, float('nan'), float('inf')])
def test_unknown_or_overflowing_native_counter_never_becomes_zero(value):
    assert health._number(value) is None


def test_command_client_cancellation_before_spawn_does_no_work(monkeypatch):
    event = threading.Event()
    event.set()
    popen = Mock(side_effect=AssertionError('No read-only subprocess after cancellation.'))
    monkeypatch.setattr(health.subprocess, 'Popen', popen)
    with pytest.raises(InterruptedError):
        health._bounded_command(['/usr/bin/lsblk', '--json', '--bytes', '--nodeps', '--output', health._LSBLK_FIELDS],
                                timeout=1, cancel_event=event)
    popen.assert_not_called()


@pytest.mark.parametrize('stderr', [False, True])
def test_command_pipe_buffers_have_a_hard_size_limit(monkeypatch, stderr):
    process = Mock(returncode=0)
    process.poll.return_value = 0
    process.stdout = io.BytesIO(b'{}' if stderr else b'x' * (health._MAX_STDOUT + 100))
    process.stderr = io.BytesIO(b'x' * (health._MAX_STDERR + 100) if stderr else b'')
    monkeypatch.setattr(health.subprocess, 'Popen', Mock(return_value=process))
    with pytest.raises(ValueError):
        health._bounded_command(['/usr/bin/lsblk', '--json', '--bytes', '--nodeps', '--output', health._LSBLK_FIELDS], timeout=1)
    process.terminate.assert_not_called()


def test_blocked_sysfs_driver_cannot_hold_collection_or_accumulate_read_threads(monkeypatch):
    entered, release = threading.Event(), threading.Event()
    gate = threading.Semaphore(1)
    monkeypatch.setattr(health, '_ATTRIBUTE_GATE', gate)
    calls = []
    def blocked(path):
        calls.append(path)
        entered.set()
        release.wait(2)
        return b'Battery'
    monkeypatch.setattr(health, '_read_attribute', blocked)
    ticks = iter([0, 0, 2, 2, 2, 2])
    budget = health._Budget(1, None, lambda: next(ticks))
    try:
        with pytest.raises(TimeoutError):
            health._attr(Path('/sys/class/power_supply/BAT0/type'), budget)
        assert entered.wait(.5)
        # A later scan reports an unavailable reading instead of creating a
        # second thread behind an unresolved native read.
        fresh = health._Budget(1, None, lambda: 0)
        assert health._attr(Path('/sys/class/power_supply/BAT1/type'), fresh) is None
        assert len(calls) == 1
    finally:
        release.set()
        assert gate.acquire(timeout=1)
        gate.release()
