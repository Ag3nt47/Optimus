"""Injected live metrics tests: no machine probes or network traffic."""
from __future__ import annotations

import copy
import json
import subprocess
import threading
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from optimus.live_metrics import LiveMetrics, identify_wifi


def navigation(*jobs):
    return {'active': True, 'session_id': 'fixture-session', 'jobs': [
        {'id': identity, 'active': True, 'current': {'key': key, 'thread_id': tid}}
        for identity, key, tid in jobs]}


def raw(*, total=100, idle=50, memory=40, received=1000, sent=500, threads=None):
    return {'cpu_times': {'total': total, 'idle': idle}, 'cpu_count': 8,
        'cpu_frequency_mhz': 3200, 'memory_percent': memory,
        'threads': {101: 1.0} if threads is None else threads,
        'network_counters': {'Radio Adapter': {'received': received, 'sent': sent}},
        'network_stats': {'Radio Adapter': {'is_up': True, 'link_mbps': 866}}}


@pytest.fixture
def fixture():
    state = {'now': 100., 'raw': raw(), 'navigation': navigation(('software_updates', 'first', 101))}
    backend = SimpleNamespace(workflow_navigation_status=lambda: copy.deepcopy(state['navigation']))
    reader = Mock(side_effect=lambda: copy.deepcopy(state['raw']))
    wifi = Mock(return_value={'wifi_adapters': ['Radio Adapter'],
        'internet_by_adapter': {'Radio Adapter': 'os_reported_internet'}})
    metrics = LiveMetrics(backend, reader=reader, wifi_provider=wifi, clock=lambda: state['now'])
    return metrics, state, reader, wifi


def sample(fixture, **changes):
    metrics, state, _, _ = fixture
    state.update(changes)
    metrics._sample()
    return metrics.status()


def test_constructor_and_status_are_inert_and_return_independent_copies(fixture):
    metrics, _, reader, wifi = fixture
    status = metrics.status()
    assert status['running'] is False and status['computer']['cpu_percent'] is None
    assert status['network']['receive_mbps'] is None
    status['computer']['cpu_percent'] = 100
    assert metrics.status()['computer']['cpu_percent'] is None
    reader.assert_not_called()
    wifi.assert_not_called()


@pytest.mark.parametrize('value', [True, 0, .1, 31, float('inf'), '2'])
def test_invalid_polling_interval_is_rejected(value):
    with pytest.raises(ValueError): LiveMetrics(SimpleNamespace(), interval=value)


def test_first_sample_never_invents_cpu_or_throughput(fixture):
    report = sample(fixture)
    assert report['computer']['cpu_percent'] is None
    assert report['computer']['cpu_frequency_mhz'] == 3200
    assert report['computer']['memory_percent'] == 40
    assert report['workers']['software_updates']['cpu_percent'] is None
    assert report['network']['receive_mbps'] is None
    assert report['network']['kind'] == 'wifi' and report['network']['is_up'] is True


def test_observed_cpu_thread_and_passive_network_deltas_use_explicit_units(fixture):
    sample(fixture)
    report = sample(fixture, now=102., raw=raw(total=116, idle=58, received=2_001_000,
                                              sent=500_500, threads={101: 2.0}))
    assert report['computer']['cpu_percent'] == 50.
    assert report['workers']['software_updates']['cpu_percent'] == 6.25
    assert 'total computer CPU' in report['workers']['software_updates']['scope']
    assert report['network']['receive_mbps'] == 8.
    assert report['network']['send_mbps'] == 2.
    assert report['network']['link_mbps'] == 866
    assert report['network']['internet_state'] == 'os_reported_internet'
    assert 'not Internet capacity' in report['network']['reason']
    fixture[3].assert_called_once()


def test_observed_zero_worker_cpu_is_truthful_waiting_not_failed(fixture):
    sample(fixture)
    report = sample(fixture, now=102., raw=raw(total=116, idle=66, threads={101: 1.0}))
    assert report['computer']['cpu_percent'] == 0
    worker = report['workers']['software_updates']
    assert worker['cpu_percent'] == 0 and 'Waiting on PowerShell' in worker['reason']


@pytest.mark.parametrize('memory,idle,cpu_high,memory_high', [(95, 65, False, True), (30, 50, True, False)])
def test_resource_pressure_is_observed_not_a_full_speed_claim(fixture, memory, idle, cpu_high, memory_high):
    sample(fixture)
    report = sample(fixture, now=102., raw=raw(total=116, idle=idle, memory=memory))
    assert report['computer']['high_cpu'] is cpu_high
    assert report['computer']['high_memory'] is memory_high
    assert report['computer']['pressure'] == 'high'
    assert 'does not diagnose' in report['computer']['reason']


def test_missing_native_tid_is_unavailable_not_zero(fixture):
    sample(fixture, navigation=navigation(('driver_updates', 'first', None)))
    report = sample(fixture, now=102., navigation=navigation(('driver_updates', 'first', None)))
    assert report['workers']['driver_updates']['cpu_percent'] is None


def test_reused_thread_id_or_new_item_requires_its_own_baseline(fixture):
    sample(fixture)
    report = sample(fixture, now=102., raw=raw(total=116, idle=58, threads={101: 2.0}),
        navigation=navigation(('software_updates', 'second', 101)))
    assert report['workers']['software_updates']['cpu_percent'] is None
    report = sample(fixture, now=104., raw=raw(total=132, idle=66, threads={101: 2.5}))
    assert report['workers']['software_updates']['cpu_percent'] == 3.12


def test_thread_that_exits_or_changes_during_read_is_unavailable(fixture):
    metrics, state, reader, _ = fixture
    sample(fixture)
    def read():
        state['navigation'] = navigation(('software_updates', 'second', 102))
        return raw(total=116, idle=58, threads={102: 8.})
    reader.side_effect = read
    state['now'] = 102
    metrics._sample()
    assert metrics.status()['workers']['software_updates']['cpu_percent'] is None


def test_shared_serial_thread_is_attributed_to_updater_without_double_count(fixture):
    state = navigation(('maintenance', 'delegated', 101), ('software_updates', 'first', 101))
    sample(fixture, navigation=state)
    report = sample(fixture, now=102., raw=raw(total=116, idle=58, threads={101: 2.0}))
    assert report['workers']['maintenance']['cpu_percent'] is None
    assert 'not counted twice' in report['workers']['maintenance']['reason']
    assert report['workers']['software_updates']['cpu_percent'] == 6.25


def test_ambiguous_shared_updater_thread_is_unavailable_for_both(fixture):
    state = navigation(('driver_updates', 'driver', 101), ('software_updates', 'first', 101))
    sample(fixture, navigation=state)
    report = sample(fixture, now=102., raw=raw(total=116, idle=58, threads={101: 2.0}))
    assert all(row['cpu_percent'] is None for row in report['workers'].values())


def test_external_thread_cpu_is_not_added_to_any_worker(fixture):
    sample(fixture)
    report = sample(fixture, now=102., raw=raw(total=116, idle=58, threads={101: 1.0, 900: 9999.}))
    assert report['workers']['software_updates']['cpu_percent'] == 0
    assert len(report['workers']) == 1


@pytest.mark.parametrize('change', ['backwards_clock', 'large_gap', 'cpu_reset', 'thread_reset', 'missing_thread', 'count_changed'])
def test_discontinuous_samples_never_publish_invalid_worker_percentages(fixture, change):
    sample(fixture)
    now = 102.
    data = raw(total=116, idle=58, threads={101: 2.0})
    if change == 'backwards_clock': now = 99
    elif change == 'large_gap': now = 999
    elif change == 'cpu_reset': data['cpu_times'] = {'total': 5, 'idle': 1}
    elif change == 'thread_reset': data['threads'][101] = .1
    elif change == 'missing_thread': data['threads'] = {}
    elif change == 'count_changed': data['cpu_count'] = 4
    report = sample(fixture, now=now, raw=data)
    if change != 'cpu_reset':
        assert report['workers']['software_updates']['cpu_percent'] is None
    if change in ('backwards_clock', 'large_gap', 'cpu_reset'):
        assert report['computer']['cpu_percent'] is None


def test_network_counter_reset_then_new_baseline_recovers(fixture):
    sample(fixture)
    report = sample(fixture, now=102., raw=raw(total=116, idle=58, received=5, sent=2))
    assert report['network']['receive_mbps'] is None and report['network']['send_mbps'] is None
    report = sample(fixture, now=104., raw=raw(total=132, idle=66, received=1_000_005, sent=250_002))
    assert report['network']['receive_mbps'] == 4 and report['network']['send_mbps'] == 1


def test_wifi_identity_failure_labels_network_and_never_guesses_from_adapter_name(fixture):
    fixture[3].return_value = {'wifi_adapters': [], 'internet_by_adapter': {}}
    report = sample(fixture)
    assert report['network']['kind'] == 'network'
    assert report['network']['internet_state'] == 'unverified'
    assert 'Wi-Fi could not be verified' in report['network']['reason']


def test_down_adapter_reports_connection_state_without_fake_rx(fixture):
    data = raw()
    data['network_stats']['Radio Adapter']['is_up'] = False
    report = sample(fixture, raw=data)
    assert report['network']['is_up'] is False
    assert report['network']['receive_mbps'] is None and report['network']['adapter'] is None
    assert report['network']['internet_state'] == 'unverified'


def test_wifi_is_preferred_and_virtual_traffic_is_not_summed(fixture):
    data = raw()
    data['network_counters']['VPN'] = {'received': 99999999, 'sent': 999999}
    data['network_stats']['VPN'] = {'is_up': True, 'link_mbps': 1000}
    sample(fixture, raw=data)
    data['network_counters']['Radio Adapter']['received'] += 2_000_000
    data['network_counters']['VPN']['received'] += 999_000_000
    report = sample(fixture, now=102., raw=data)
    assert report['network']['adapter'] == 'Radio Adapter'
    assert report['network']['receive_mbps'] == 8


def test_metric_probe_failure_clears_stale_values_and_recovers_baseline(fixture):
    sample(fixture)
    fixture[2].side_effect = OSError('Fixture unavailable counters')
    report = sample(fixture, now=102.)
    assert report['computer']['cpu_percent'] is None and report['workers'] == {}
    fixture[2].side_effect = lambda: raw(total=132, idle=66)
    report = sample(fixture, now=104.)
    assert report['computer']['cpu_percent'] is None


def test_invalid_or_nonfinite_os_values_are_unavailable(fixture):
    data = raw(memory=float('nan'))
    data.update(cpu_frequency_mhz=True, cpu_times={'total': float('inf'), 'idle': 0})
    sample(fixture)
    report = sample(fixture, now=102, raw=data)
    assert report['computer']['cpu_percent'] is None
    assert report['computer']['memory_percent'] is None
    assert report['computer']['cpu_frequency_mhz'] is None


def test_linux_wifi_uses_kernel_wireless_identity_and_never_executes(tmp_path):
    (tmp_path / 'strange-radio' / 'wireless').mkdir(parents=True)
    (tmp_path / 'Wi-Fi-looking-name').mkdir()
    runner = Mock(side_effect=AssertionError('No command on Linux'))
    report = identify_wifi(system='linux', linux_root=tmp_path, runner=runner)
    assert report['wifi_adapters'] == ['strange-radio']
    runner.assert_not_called()


def test_windows_wifi_provider_is_bounded_hidden_read_only_and_os_reported():
    runner = Mock(return_value=subprocess.CompletedProcess([], 0, json.dumps({
        'wifi_adapters': ['Wireless Radio'], 'profiles': [{'adapter': 'Wireless Radio', 'internet': True}]}), ''))
    report = identify_wifi(system='win32', runner=runner)
    assert report['wifi_adapters'] == ['Wireless Radio']
    assert report['internet_by_adapter']['Wireless Radio'] == 'os_reported_internet'
    argv = runner.call_args.args[0]
    command = argv[-1]
    assert 'Get-NetAdapter -Physical' in command and 'NdisPhysicalMedium -in @(1,9)' in command
    assert 'Get-NetConnectionProfile' in command
    assert all(term not in command for term in ('Set-Net', 'Restart-', 'Invoke-WebRequest', 'Test-NetConnection'))
    assert runner.call_args.kwargs['timeout'] == 3
    assert runner.call_args.kwargs['stdin'] == subprocess.DEVNULL
    assert runner.call_args.kwargs['env']['PSModulePath'].endswith('Modules')


@pytest.mark.parametrize('output', ['not json', '{}', '{"wifi_adapters":"Wi-Fi"}', '{"wifi_adapters":["bad\\nname"]}'])
def test_unverified_windows_identity_stays_generic_network(output):
    runner = Mock(return_value=subprocess.CompletedProcess([], 0, output, ''))
    assert identify_wifi(system='win32', runner=runner)['wifi_adapters'] == []


def test_macos_hardware_port_mapping_does_not_guess_from_device_name():
    text = 'Hardware Port: Ethernet\nDevice: en0\n\nHardware Port: Wi-Fi\nDevice: en7\n'
    runner = Mock(return_value=subprocess.CompletedProcess([], 0, text, ''))
    assert identify_wifi(system='darwin', runner=runner)['wifi_adapters'] == ['en7']
    assert runner.call_args.args[0] == ['/usr/sbin/networksetup', '-listallhardwareports']


def test_provider_timeout_is_a_passive_unknown_not_a_speed_failure():
    runner = Mock(side_effect=subprocess.TimeoutExpired(['fixture'], 3))
    report = identify_wifi(system='win32', runner=runner)
    assert report['wifi_adapters'] == [] and 'could not be verified' in report['reason']


def test_stop_during_sample_discards_late_result_without_blocking_status():
    entered, release = threading.Event(), threading.Event()
    def reader():
        entered.set()
        assert release.wait(3)
        return raw()
    backend = SimpleNamespace(workflow_navigation_status=lambda: {})
    metrics = LiveMetrics(backend, reader=reader, wifi_provider=lambda: {}, interval=.5)
    try:
        assert metrics.start() is True
        assert entered.wait(2)
        assert metrics.start() is False
        metrics.stop(wait=False)
        assert metrics.status()['running'] is False
        release.set()
        assert metrics.stop(wait=True) is True
        assert metrics.status()['sampled_at'] is None
    finally:
        release.set()
        metrics.stop(wait=True)


def test_restart_during_old_sample_uses_new_generation_without_overlapping_samplers():
    entered, release, second = threading.Event(), threading.Event(), threading.Event()
    calls = [0]
    def reader():
        calls[0] += 1
        if calls[0] == 1:
            entered.set()
            assert release.wait(3)
        else:
            second.set()
        return raw()
    metrics = LiveMetrics(SimpleNamespace(), reader=reader, wifi_provider=lambda: {}, interval=.5)
    try:
        metrics.start()
        assert entered.wait(2)
        original = metrics._thread
        metrics.stop(wait=False)
        assert metrics.start() is True
        assert metrics._thread is original
        release.set()
        assert second.wait(2)
        assert metrics.status()['running'] is True
    finally:
        release.set()
        metrics.stop(wait=True)
