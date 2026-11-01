"""Own-thread telemetry with injected native failures and isolated live reads."""
import copy
import os
import sys
import threading
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from optimus import live_metrics
from optimus import thread_metrics


class NativeAPI:
    def __init__(self):
        self.events = []
        self.handles = set()
        self.failures = {}
        self.owners = {}
        self.times = {}
        self.active = {}

    def event(self, operation, tid):
        self.events.append((operation, tid))
        error = self.failures.get((operation, tid))
        if error:
            raise error

    def open_thread(self, tid):
        self.event('open', tid)
        self.handles.add(tid)
        return tid

    def thread_process_id(self, handle):
        self.event('owner', handle)
        return self.owners.get(handle, os.getpid())

    def thread_active(self, handle):
        self.event('active', handle)
        values = self.active.get(handle, [True])
        return values.pop(0) if len(values) > 1 else values[0]

    def thread_times(self, handle):
        self.event('times', handle)
        return self.times.get(handle, (123456, 1_000_000, 2_000_000))

    def close_handle(self, handle):
        self.handles.remove(handle)
        self.event('close', handle)


def test_only_bounded_requested_own_threads_are_read_and_every_handle_closes():
    api = NativeAPI()
    result = thread_metrics.read_own_thread_times([103, 101, 101], api=api)
    assert result['threads'] == {101: 0.3, 103: 0.3}
    assert result['identities'] == {tid: (os.getpid(), 123456) for tid in (101, 103)}
    assert result['unavailable'] == [] and not api.handles
    assert [tid for operation, tid in api.events if operation == 'open'] == [101, 103]
    assert [tid for operation, tid in api.events if operation == 'close'] == [101, 103]


def test_empty_selection_does_not_load_native_api_or_open_a_handle(monkeypatch):
    monkeypatch.setattr(thread_metrics, '_native_api', Mock(side_effect=AssertionError('No query needed')))
    assert thread_metrics.read_own_thread_times(()) == dict(
        threads={}, identities={}, unavailable=[], source='windows_thread_query')


@pytest.mark.parametrize('value', [None, '101', 101, [True], [0], [-1], [2**32],
    [101.0], [float('nan')], [101] * 13, (i for i in range(2))])
def test_unbounded_or_invalid_thread_selection_never_reaches_native_api(value):
    api = NativeAPI()
    with pytest.raises(ValueError):
        thread_metrics.read_own_thread_times(value, api=api)
    assert api.events == []


@pytest.mark.parametrize('owner', [0, True, None, '101', -1, 2**40])
def test_foreign_or_unverified_pid_is_not_read_as_an_optimus_thread(owner):
    api = NativeAPI()
    api.owners[101] = owner
    result = thread_metrics.read_own_thread_times([101, 102], api=api)
    assert result['unavailable'] == [101] and 101 not in result['threads']
    assert ('times', 101) not in api.events
    assert 102 in result['threads'] and not api.handles


@pytest.mark.parametrize('operation', ['open', 'owner', 'active', 'times'])
@pytest.mark.parametrize('error', [OSError('denied'), ValueError('invalid'), AttributeError('unavailable')])
def test_one_failed_thread_does_not_hide_other_worker_counters(operation, error):
    api = NativeAPI()
    api.failures[(operation, 101)] = error
    result = thread_metrics.read_own_thread_times([101, 102], api=api)
    assert result['unavailable'] == [101] and 102 in result['threads']
    assert not api.handles
    assert ('close', 101) in api.events if operation != 'open' else ('close', 101) not in api.events


def test_null_open_handle_is_not_closed_or_read():
    api = NativeAPI()
    api.open_thread = Mock(return_value=None)
    result = thread_metrics.read_own_thread_times([101], api=api)
    assert result['threads'] == {} and result['unavailable'] == [101]
    assert api.events == []


@pytest.mark.parametrize('states', [[False], [True, False], [1], [True, 'active']])
def test_exited_or_racing_thread_does_not_publish_a_zero_cpu_claim(states):
    api = NativeAPI()
    api.active[101] = states
    result = thread_metrics.read_own_thread_times([101], api=api)
    assert result['threads'] == result['identities'] == {} and result['unavailable'] == [101]
    assert not api.handles


@pytest.mark.parametrize('times', [None, [], (1, 2), (True, 2, 3), (0, 2, 3),
    (1, True, 3), (1, -1, 2), (1, 2, float('inf')), (2**64, 0, 0), (1, 2**64, 0)])
def test_invalid_native_birth_or_counter_fields_are_unavailable(times):
    api = NativeAPI()
    api.times[101] = times
    result = thread_metrics.read_own_thread_times([101], api=api)
    assert result['threads'] == result['identities'] == {} and not api.handles


def test_native_load_failure_is_unknown_without_an_unverified_global_fallback(monkeypatch):
    monkeypatch.setattr(thread_metrics, '_native_api', Mock(side_effect=OSError('API unavailable')))
    assert thread_metrics.read_own_thread_times([101]) == dict(
        threads={}, identities={}, unavailable=[101], source='windows_thread_query')


def test_close_failure_discards_counters_and_does_not_open_further_handles():
    api = NativeAPI()
    api.failures[('close', 101)] = OSError('Handle state could not be confirmed')
    result = thread_metrics.read_own_thread_times([101, 102], api=api)
    assert result['threads'] == result['identities'] == {}
    assert result['unavailable'] == [101, 102] and not api.handles
    assert ('open', 102) not in api.events


def test_native_bindings_use_system32_and_only_noninherited_limited_query_access(monkeypatch):
    library = SimpleNamespace(**{name: Mock(return_value=1) for name in (
        'OpenThread', 'GetProcessIdOfThread', 'GetThreadTimes', 'GetExitCodeThread', 'CloseHandle')})
    loader = Mock(return_value=library)
    monkeypatch.setattr(thread_metrics, 'os', SimpleNamespace(name='nt'))
    monkeypatch.setattr(thread_metrics.ctypes, 'WinDLL', loader, raising=False)
    api = thread_metrics._WindowsThreadAPI()
    api.open_thread(101)
    api.close_handle(1)
    loader.assert_called_once_with('kernel32.dll', use_last_error=True, winmode=0x800)
    library.OpenThread.assert_called_once_with(0x0800, False, 101)
    library.CloseHandle.assert_called_once_with(1)


def system_fixture(monkeypatch, *, platform='win32'):
    import psutil
    monkeypatch.setattr(live_metrics, 'sys', SimpleNamespace(platform=platform))
    monkeypatch.setattr(psutil, 'cpu_times', lambda: SimpleNamespace(_asdict=lambda: dict(user=10, idle=10)))
    monkeypatch.setattr(psutil, 'cpu_count', lambda **options: 8)
    monkeypatch.setattr(psutil, 'cpu_freq', lambda: SimpleNamespace(current=3200), raising=False)
    monkeypatch.setattr(psutil, 'virtual_memory', lambda: SimpleNamespace(percent=30))
    monkeypatch.setattr(psutil, 'net_io_counters', lambda **options: {})
    monkeypatch.setattr(psutil, 'net_if_stats', lambda: {})
    process = Mock(side_effect=AssertionError('A Windows process-wide thread census is forbidden'))
    monkeypatch.setattr(psutil, 'Process', process)
    return psutil, process


def test_windows_system_reader_uses_only_selected_tids_and_requires_birth_identity(monkeypatch):
    _, process = system_fixture(monkeypatch)
    native = Mock(return_value=dict(threads={101: 0.3}, identities={101: (os.getpid(), 123)}, unavailable=[]))
    monkeypatch.setattr(thread_metrics, 'read_own_thread_times', native)
    raw = live_metrics._read_system((101,))
    native.assert_called_once_with((101,))
    assert raw['threads'] == {101: 0.3} and raw['thread_identity_required'] is True
    assert raw['thread_identities'] == {101: (os.getpid(), 123)}
    assert raw['memory_percent'] == 30 and raw['errors'] == []
    process.assert_not_called()


def test_windows_thread_failure_leaves_other_system_metrics_available(monkeypatch):
    _, process = system_fixture(monkeypatch)
    monkeypatch.setattr(thread_metrics, 'read_own_thread_times', lambda ids: dict(
        threads={}, identities={}, unavailable=list(ids)))
    raw = live_metrics._read_system((101,))
    assert raw['threads'] == {} and raw['thread_identity_required'] is True
    assert raw['cpu_count'] == 8 and raw['memory_percent'] == 30 and raw['errors'] == ['threads']
    process.assert_not_called()


def test_portable_psutil_reader_keeps_selected_thread_filter_and_skips_empty_census(monkeypatch):
    psutil, _ = system_fixture(monkeypatch, platform='linux')
    threads = Mock(return_value=[SimpleNamespace(id=101, user_time=.1, system_time=.2),
                                SimpleNamespace(id=102, user_time=20, system_time=30)])
    process = Mock(return_value=SimpleNamespace(threads=threads))
    monkeypatch.setattr(psutil, 'Process', process)
    assert live_metrics._read_system()['threads'] == {}
    process.assert_not_called()
    raw = live_metrics._read_system((101,))
    assert raw['threads'] == {101: pytest.approx(.3)} and 'thread_identity_required' not in raw
    threads.assert_called_once_with()


@pytest.mark.parametrize('platform', ['darwin', 'linux', 'win32'])
@pytest.mark.parametrize('capability', ['missing', 'non_callable', 'attribute_error', 'not_implemented', 'system_error'])
def test_optional_cpu_frequency_never_discards_cpu_memory_or_network(monkeypatch, platform, capability):
    psutil, process = system_fixture(monkeypatch, platform=platform)
    if capability == 'missing':
        monkeypatch.delattr(psutil, 'cpu_freq', raising=False)
    elif capability == 'non_callable':
        monkeypatch.setattr(psutil, 'cpu_freq', None, raising=False)
    else:
        error = {'attribute_error': AttributeError, 'not_implemented': NotImplementedError,
                 'system_error': SystemError}[capability]('Unsupported hardware frequency')
        monkeypatch.setattr(psutil, 'cpu_freq', Mock(side_effect=error), raising=False)
    state = {'now': 100.0, 'cpu': 10, 'received': 1000, 'sent': 500}
    monkeypatch.setattr(psutil, 'cpu_times', lambda: SimpleNamespace(
        _asdict=lambda: dict(user=state['cpu'], idle=state['cpu'])))
    monkeypatch.setattr(psutil, 'net_io_counters', lambda **options: {
        'Fixture network': SimpleNamespace(bytes_recv=state['received'], bytes_sent=state['sent'])})
    monkeypatch.setattr(psutil, 'net_if_stats', lambda: {
        'Fixture network': SimpleNamespace(isup=True, speed=100)})
    metrics = live_metrics.LiveMetrics(SimpleNamespace(), wifi_provider=lambda: {}, clock=lambda: state['now'])
    metrics._sample()
    state.update(now=102.0, cpu=20, received=3000, sent=1500)
    metrics._sample()
    result = metrics.status()
    assert result['computer']['cpu_frequency_mhz'] is None
    assert result['computer']['cpu_percent'] == 50.0
    assert result['computer']['memory_percent'] == 30
    assert result['network']['is_up'] is True and result['network']['link_mbps'] == 100
    assert result['network']['receive_mbps'] == .008 and result['network']['send_mbps'] == .004
    process.assert_not_called()


def navigation(tid=101):
    return dict(active=True, session_id='session', jobs=[dict(id='software_updates', active=True,
        current=dict(key='current-package', thread_id=tid))])


def metric_sample(cpu=1, birth=123, *, pid=None):
    return dict(cpu_times={'total': 100, 'idle': 50}, cpu_count=8, cpu_frequency_mhz=3200,
        memory_percent=30, threads={101: cpu}, thread_identity_required=True,
        thread_identities={101: (os.getpid() if pid is None else pid, birth)},
        network_counters={}, network_stats={})


def sample_worker(metrics, data, now):
    metrics._reader = lambda: copy.deepcopy(data)
    metrics._clock = lambda: now
    metrics._sample()
    return metrics.status()['workers']['software_updates']


def test_reused_tid_creation_time_resets_baseline_even_if_cpu_counter_increases():
    metrics = live_metrics.LiveMetrics(SimpleNamespace(workflow_navigation_status=navigation),
        wifi_provider=lambda: {})
    assert sample_worker(metrics, metric_sample(1, 123), 100)['cpu_percent'] is None
    assert sample_worker(metrics, metric_sample(2, 123), 102)['cpu_percent'] == 6.25
    assert sample_worker(metrics, metric_sample(3, 456), 104)['cpu_percent'] is None
    assert sample_worker(metrics, metric_sample(4, 456), 106)['cpu_percent'] == 6.25


@pytest.mark.parametrize('identity', [None, (0, 123), (True, 123), (os.getpid(), True),
    (os.getpid(), 0), (os.getpid(), 2**64), [os.getpid(), 123]])
def test_missing_foreign_or_invalid_birth_identity_is_unknown_and_clears_old_baseline(identity):
    metrics = live_metrics.LiveMetrics(SimpleNamespace(workflow_navigation_status=navigation), wifi_provider=lambda: {})
    sample_worker(metrics, metric_sample(1), 100)
    data = metric_sample(2)
    data['thread_identities'] = {101: identity}
    assert sample_worker(metrics, data, 102)['cpu_percent'] is None
    assert sample_worker(metrics, metric_sample(3), 104)['cpu_percent'] is None
    assert sample_worker(metrics, metric_sample(4), 106)['cpu_percent'] == 6.25


def test_default_reader_receives_only_bounded_before_navigation_worker_ids(monkeypatch):
    backend = SimpleNamespace(workflow_navigation_status=lambda: dict(active=True, session_id='session', jobs=[
        dict(id='maintenance', active=True, current=dict(key='a', thread_id=102)),
        dict(id='software_updates', active=True, current=dict(key='b', thread_id=101)),
        dict(id='driver_updates', active=True, current=dict(key='c', thread_id=101))]))
    reader = Mock(return_value=metric_sample())
    monkeypatch.setattr(live_metrics, '_read_system', reader)
    metrics = live_metrics.LiveMetrics(backend, wifi_provider=lambda: {}, clock=lambda: 100)
    reader.assert_not_called()
    metrics._sample()
    reader.assert_called_once_with((101, 102))


@pytest.mark.parametrize('platform', ['linux', 'darwin'])
def test_portable_native_ids_keep_existing_64bit_support(monkeypatch, platform):
    monkeypatch.setattr(live_metrics, 'sys', SimpleNamespace(platform=platform))
    assert live_metrics.LiveMetrics._bindings(navigation(2**40))['software_updates'][2] == 2**40


def test_windows_native_ids_must_fit_the_documented_dword(monkeypatch):
    monkeypatch.setattr(live_metrics, 'sys', SimpleNamespace(platform='win32'))
    assert live_metrics.LiveMetrics._bindings(navigation(2**40))['software_updates'] is None


@pytest.mark.skipif(os.name != 'nt', reason='Read-only own Windows thread query.')
def test_real_current_thread_has_own_pid_birth_time_and_no_handle_growth():
    import psutil
    tid = threading.get_native_id()
    first = thread_metrics.read_own_thread_times([tid])
    assert first['unavailable'] == [] and first['identities'][tid][0] == os.getpid()
    assert first['identities'][tid][1] > 0
    process = psutil.Process()
    before = process.num_handles()
    for _ in range(40):
        current = thread_metrics.read_own_thread_times([tid])
        assert current['identities'] == first['identities'] and current['threads'][tid] >= first['threads'][tid]
    assert process.num_handles() <= before
