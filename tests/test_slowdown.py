"""Deterministic diagnostic evidence; no real process control or updates."""
import copy
import threading
from unittest.mock import patch
import pytest
from optimus import slowdown

MIB = 1024 * 1024


def process(pid=10, exe=r'C:\Apps\Browser.exe', created=10, cpu=0, read=0, write=0, rss=100*MIB):
    identity = (pid, created, slowdown.ntpath.normcase(exe))
    return identity, {'pid':pid, 'name':'Browser.exe', 'exe':exe, 'memory_rss':rss, 'cpu':cpu, 'read':read, 'write':write}


def snapshot(t, *records, percent=50, inaccessible=0):
    return {'time':t, 'processes':dict(records), 'inaccessible':inaccessible, 'cpu_total':t*4,
            'cpu_idle':t*2, 'memory':{'total':8*1024*MIB, 'available':4*1024*MIB, 'percent':percent}}


def report(*snapshots):
    with patch.object(slowdown.psutil,'cpu_count',return_value=4):
        return slowdown._summarize(snapshots,expected_samples=3,interval_seconds=1)


def test_sustained_cpu_is_delta_normalized_not_lifetime():
    result = report(*(snapshot(t,process(cpu=1000+t*2)) for t in (1,2,3)))
    app = result['applications'][0]
    assert app['cpu_percent'] == 50
    assert app['severity'] == 'high'
    assert result['system_cpu_percent'] == 50
    assert app['measured_intervals'] == 2


def test_memory_usage_alone_does_not_mark_unneeded_program():
    result = report(*(snapshot(t,process(cpu=1000,rss=3*1024*MIB)) for t in (1,2,3)))
    app = result['applications'][0]
    assert app['severity'] == 'normal'
    assert app['cpu_percent'] == 0
    assert app['reasons'] == []


def test_memory_pressure_explains_potential_contribution():
    result = report(*(snapshot(t,process(rss=2*1024*MIB),percent=90) for t in (1,2,3)))
    assert result['applications'][0]['severity'] == 'high'
    assert 'system RAM is 90%' in result['applications'][0]['reasons'][0]


def test_process_reuse_is_not_cross_lifetime_cpu_or_io():
    result = report(snapshot(1,process(created=1,cpu=0)),snapshot(2,process(created=1,cpu=1)),snapshot(3,process(created=2,cpu=5000,read=10**12)))
    app = result['applications'][0]
    assert app['cpu_percent'] == 0
    assert app['read_bytes_per_second'] == 0
    assert app['measured_intervals'] == 0


def test_executable_change_and_counter_reset_not_compared():
    result = report(snapshot(1,process(exe=r'C:\Other\Browser.exe',cpu=0)),snapshot(2,process(cpu=500)),snapshot(3,process(cpu=1)))
    assert result['applications'][0]['cpu_percent'] == 0


def test_same_executable_workers_grouped_and_names_alone_never_grouped():
    frames=[]
    for t in (1,2,3):
        frames.append(snapshot(t,process(pid=10,cpu=t),process(pid=11,cpu=t),process(pid=12,exe=r'C:\Other\Browser.exe',cpu=0)))
    result=report(*frames)
    assert len(result['applications']) == 2
    assert result['applications'][0]['process_count'] == 2
    assert result['applications'][0]['cpu_percent'] == 50
    assert set(result['applications'][0]['pids']) == {10,11}


def test_large_io_reports_rate_without_claiming_physical_disk_saturation():
    result=report(*(snapshot(t,process(read=t*10*MIB)) for t in (1,2,3)))
    app=result['applications'][0]
    assert app['read_bytes_per_second'] == 10*MIB
    assert app['severity'] == 'review'
    assert 'does not prove disk saturation' in app['reasons'][0]


def test_transient_cpu_spike_is_review_not_sustained():
    result=report(snapshot(1,process(cpu=0)),snapshot(2,process(cpu=2)),snapshot(3,process(cpu=2)))
    assert result['applications'][0]['severity'] == 'review'


def test_unreadable_counters_are_reported_as_unavailable():
    result=report(*(snapshot(t,process(cpu=None,read=None,write=None),inaccessible=7) for t in (1,2,3)))
    assert result['applications'][0]['measured_intervals'] == 0
    assert result['applications'][0]['io_measured_intervals'] == 0
    assert result['inaccessible_process_count'] == 7
    assert any('unavailable' in note for note in result['notes'])


def test_ended_processes_do_not_appear_as_current_offenders():
    result=report(snapshot(1,process(cpu=0)),snapshot(2,process(cpu=100)),snapshot(3))
    assert result['applications'] == []


def test_cancellation_is_bounded_and_never_claims_complete():
    stop=threading.Event()
    stop.set()
    with patch.object(slowdown,'_capture',return_value=snapshot(1)) as capture:
        result=slowdown.analyze_slowdowns(cancel_event=stop)
    assert capture.call_count == 1
    assert result['cancelled'] is True and result['complete'] is False
    assert result['sample_count'] == 1


def test_sample_inputs_have_no_side_effect_on_report_generation():
    frames=[snapshot(t,process(cpu=t)) for t in (1,2,3)]
    before=copy.deepcopy(frames)
    report(*frames)
    assert frames == before


@pytest.mark.parametrize('kwargs',[{'sample_count':True},{'sample_count':2},{'sample_count':11},{'interval_seconds':float('nan')},{'interval_seconds':True},{'interval_seconds':0},{'interval_seconds':3}])
def test_invalid_sampling_inputs_rejected_before_inventory(kwargs):
    with patch.object(slowdown,'_capture') as capture, pytest.raises(ValueError):
        slowdown.analyze_slowdowns(**kwargs)
    capture.assert_not_called()


def test_report_size_bound_is_explicit():
    result=report(*(snapshot(t,*(process(pid=i,exe=fr'C:\Apps\{i}.exe') for i in range(101))) for t in (1,2,3)))
    assert result['application_count'] == 101
    assert len(result['applications']) == 100
    assert result['truncated'] is True
