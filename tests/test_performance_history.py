from collections import namedtuple
import json
import threading

import pytest

from optimus.performance_history import PerformanceHistory, collect_resource_window

Counters = namedtuple('Counters', 'read_bytes write_bytes bytes_recv bytes_sent')
Memory = namedtuple('Memory', 'percent available total')


class Clock:
    def __init__(self): self.value = 0
    def __call__(self): return self.value
    def wait(self, value): self.value += value; return False


class Provider:
    def __init__(self, clock): self.clock = clock
    def cpu_percent(self, interval=None): return 99 if self.clock() == 0 else 25
    def virtual_memory(self): return Memory(92, 80, 1000)
    def disk_io_counters(self): return Counters(self.clock() * 100, self.clock() * 40, 0, 0)
    def net_io_counters(self): return Counters(0, 0, self.clock() * 200, self.clock() * 20)
    def cpu_count(self): return 8


def window(duration=2, **kwargs):
    clock = Clock()
    return collect_resource_window(duration=duration, interval=.5, provider=Provider(clock),
                                   clock=clock, wall_clock=lambda: 1000000, wait=clock.wait, **kwargs)


def test_observations_use_deltas_and_exclude_first_cpu_value():
    result = window()
    assert result['complete'] is True
    assert len(result['samples']) == 4
    assert result['summary']['cpu_percent']['mean'] == 25
    assert result['summary']['disk_read_bytes_per_second']['mean'] == 100
    assert result['summary']['network_bytes_recv_per_second']['mean'] == 200
    assert result['summary']['memory_pressure_seconds'] == 2
    assert result['read_only'] is True
    assert 'latency' in result['note']


def test_counter_resets_are_unavailable_not_fake_zero():
    clock = Clock()
    provider = Provider(clock)
    provider.disk_io_counters = lambda: Counters(100 if clock() == 0 else 0, 100, 0, 0)
    result = collect_resource_window(1, .5, provider=provider, clock=clock, wait=clock.wait)
    assert result['samples'][0]['disk_read_bytes_per_second'] is None
    assert result['summary']['disk_read_bytes_per_second']['observed_seconds'] == .5


def test_missing_counter_remains_unknown():
    clock = Clock()
    provider = Provider(clock)
    provider.net_io_counters = lambda: None
    result = collect_resource_window(1, .5, provider=provider, clock=clock, wait=clock.wait)
    assert result['summary']['network_bytes_recv_per_second']['mean'] is None
    assert result['errors'] == ['Network activity counters are unavailable.']


def test_missing_cpu_does_not_hide_memory_evidence():
    clock = Clock()
    provider = Provider(clock)
    def unavailable(interval=None): raise NotImplementedError()
    provider.cpu_percent = unavailable
    result = collect_resource_window(1, .5, provider=provider, clock=clock, wait=clock.wait)
    assert result['summary']['cpu_percent']['mean'] is None
    assert result['summary']['memory_percent']['mean'] == 92


def test_cancel_before_first_interval():
    event = threading.Event(); event.set()
    result = window(cancel_event=event)
    assert result['cancelled'] and not result['complete']
    assert not result['samples']


@pytest.mark.parametrize('duration,interval', [(0,.5),(31,.5),(True,.5),(float('nan'),.5),(1,0),(1,2),(1,True)])
def test_invalid_sampling_bounds(duration, interval):
    with pytest.raises(ValueError): collect_resource_window(duration, interval)


def test_history_bounds_privacy_and_comparison(tmp_path):
    history = PerformanceHistory(tmp_path, max_entries=2, clock=lambda: 1000000)
    for index in range(3):
        current = window()
        current['id'] = format(index, '032x')
        current['summary']['cpu_percent']['mean'] = 20 + index
        report = history.record(current, context={'power_source':'ac', 'secret':'never retain this'})
    assert len(report['observations']) == 2
    assert report['comparable_windows'] == 1
    assert report['changes']['cpu_percent']['difference'] == 1
    assert 'samples' not in report['latest']
    assert 'secret' not in history.path.read_text()


def test_phase_and_duration_must_match(tmp_path):
    history = PerformanceHistory(tmp_path, clock=lambda: 1000000)
    before = window()
    history.record(before, phase='before')
    after = window(); after['id'] = 'a' * 32
    report = history.record(after, phase='after')
    assert report['comparable_windows'] == 0
    manual = window(3); manual['id'] = 'b' * 32
    assert history.record(manual)['comparable_windows'] == 0


def test_bad_history_is_preserved(tmp_path):
    history = PerformanceHistory(tmp_path, clock=lambda: 1000000)
    history.path.parent.mkdir()
    history.path.write_text('broken history')
    with pytest.raises(json.JSONDecodeError): history.record(window())
    assert history.path.read_text() == 'broken history'


def test_incomplete_window_is_not_baseline(tmp_path):
    result = window(); result['complete'] = False
    with pytest.raises(ValueError): PerformanceHistory(tmp_path).record(result)


def test_old_entries_expire_when_recorded(tmp_path):
    history = PerformanceHistory(tmp_path, retention_days=1, clock=lambda: 1000000)
    result = window(); result['captured_at'] = 1000000 - 90000
    history.record(result)
    report = history.record(window())
    assert len(report['observations']) == 1


@pytest.mark.parametrize('kwargs', [{'max_entries':0},{'max_entries':True},{'retention_days':0},{'retention_days':366}])
def test_history_bounds(kwargs, tmp_path):
    with pytest.raises(ValueError): PerformanceHistory(tmp_path, **kwargs)


def test_history_foreign_owner_preserved(tmp_path):
    history = PerformanceHistory(tmp_path, clock=lambda: 1000000, owner='uid:1000')
    history.record(window())
    original = history.path.read_bytes()
    foreign = PerformanceHistory(tmp_path, clock=lambda: 1000000, owner='uid:1001')
    with pytest.raises(ValueError): foreign.record(window())
    assert history.path.read_bytes() == original


@pytest.mark.parametrize('key,value', [('mean',float('nan')),('mean',-1),('maximum',float('inf')),('observed_seconds',True)])
def test_history_rejects_invalid_statistics(tmp_path,key,value):
    history = PerformanceHistory(tmp_path,clock=lambda:1000000)
    current = window(); current['summary']['cpu_percent'][key] = value
    with pytest.raises(ValueError):history.record(current)
    assert not history.path.exists()


def test_hardlinked_history_never_read_or_replaced(tmp_path):
    import os
    history = PerformanceHistory(tmp_path,clock=lambda:1000000)
    history.record(window())
    other = tmp_path/'other.json'
    try: os.link(history.path,other)
    except OSError: pytest.skip('Hardlinks unavailable on this filesystem.')
    original = other.read_bytes()
    with pytest.raises(ValueError):history.record(window())
    assert other.read_bytes() == original


def test_symlink_history_parent_is_refused(tmp_path):
    target = tmp_path/'target';target.mkdir()
    parent = tmp_path/'performance-history'
    try: parent.symlink_to(target,target_is_directory=True)
    except OSError: pytest.skip('Creating symbolic links requires unavailable Windows privilege.')
    history = PerformanceHistory(tmp_path,clock=lambda:1000000)
    with pytest.raises(ValueError):history.record(window())
    assert not list(target.iterdir())


def test_oversized_history_is_preserved(tmp_path):
    history = PerformanceHistory(tmp_path,clock=lambda:1000000)
    history.path.parent.mkdir()
    history.path.write_bytes(b' '*(2*1024*1024+1))
    with pytest.raises(ValueError):history.record(window())
    assert history.path.stat().st_size == 2*1024*1024+1
