"""Current-item transport fixtures; no updater, cleanup or host change is run."""
import copy
import io
import subprocess
import sys
import threading
import time
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from optimus import item_progress as items
from optimus import winget_progress as winget
from optimus.parallel_updates import ProgressMux, ParallelAppUpdates, WINDOWS_SOFTWARE, WINDOWS_DRIVERS


def progress(key='app:A', phase='download', percent=25, **fields):
    return dict(key=key, label=key, phase=phase, percent=percent, **fields)


@pytest.mark.parametrize('value', [True, '25', float('nan'), float('inf'), -1, 101, 10**1000, None],
    ids=['bool','string','nan','inf','negative','over100','hugeint','missing'])
def test_invalid_percent_stays_unknown_without_cpu_or_queue_fallback(value):
    result = items.normalize_item_progress(dict(progress(percent=value), current=1,total=2,cpu_percent=80))
    assert result == progress(percent=None)


@pytest.mark.parametrize('prepared', [False, True])
def test_driver_discovery_never_creates_or_clears_installer_intent(tmp_path, monkeypatch, prepared):
    from optimus import speed_enhancements
    monkeypatch.setattr(speed_enhancements, '_current_owner', lambda: 'fixture')
    events = []
    job = ParallelAppUpdates(SimpleNamespace(data_dir=tmp_path), 'a'*32,
                             WINDOWS_DRIVERS, lambda *_: None, ProgressMux(events.append))
    job._write = Mock()
    job.record.update(status='running', installer_prepared=prepared)
    boundary = {'operation': 'driver', 'discovery_progress': True, 'phase': 'scan',
        'description': 'Inspect exact driver offers',
        'item_progress': progress('driver:discovery', 'scan', None)}
    job.item_progress(boundary)
    before = copy.deepcopy(job.record)
    for phase in ('scan', 'driver_offers', 'device_query', 'installed_driver_query', 'driver_records',
                  'device_records', 'system_query', 'verify'):
        job.item_progress(dict(boundary, phase=phase, progress_only=True,
            item_progress=progress('driver:discovery', phase, 25)))
    assert job.record == before and job._write.call_count == 1
    assert job.record['installer_prepared'] is prepared
    assert events[-1]['jobs'][0]['status'] == 'scanning'
    count = len(events)
    job.item_progress(dict(boundary, progress_only=True,
        item_progress=progress('driver:other', 'scan', 50)))
    assert len(events) == count
    job.item_progress({'id': 'exact-offer', 'phase': 'install',
        'item_progress': progress('driver:exact-offer', 'install', None)})
    assert job.record['installer_prepared'] is True
    assert job._write.call_count == 2


@pytest.mark.parametrize('changes', [dict(key=''),dict(key=[]),dict(key='x'*513),dict(phase=''),
    dict(phase='bad phase'),dict(phase=[]),dict(label=''),dict(label=None)])
def test_invalid_identity_or_phase_rejects_the_payload(changes):
    assert items.normalize_item_progress(dict(progress(), **changes)) is None


def test_estimated_flag_is_explicit_and_exact_values_are_not_relabelled():
    assert items.normalize_item_progress(progress(estimated=True))['estimated'] is True
    for value in (False, 1, 'true', None):
        assert 'estimated' not in items.normalize_item_progress(progress(estimated=value))
    assert items.normalize_item_progress({'current':1,'total':2,'percentage':50}) is None


def test_bound_item_phase_changes_estimate_changes_close_and_wrong_keys(monkeypatch):
    events=[]
    monkeypatch.setattr(items.time,'monotonic',lambda:1)
    callback=items.BoundItemProgress(events.append,'app:A','A',{'id':'A','current':0,'total':2})
    callback({'phase':'download','percent':25})
    callback({'phase':'download','percent':25,'estimated':True})
    callback({'phase':'install','percent':None})
    callback({'key':'app:B','phase':'download','percent':90})
    assert [e['item_progress']['percent'] for e in events]==[25.0,25.0,None]
    assert events[1]['item_progress']['estimated'] is True
    assert all(e['progress_only'] is True for e in events)
    callback({'phase':'install','percent':100,'item_terminal':True})
    callback({'phase':'install','percent':4})
    callback.close();callback({'phase':'download','percent':80})
    assert len(events)==4 and events[-1]['item_progress']['percent']==100


def test_observer_failure_is_display_only_and_signature_check_never_retries():
    callback=items.BoundItemProgress(Mock(side_effect=RuntimeError('display')), 'a','A')
    callback({'phase':'install','percent':40})
    assert items.accepts_progress_callback(lambda *,progress_callback=None:None)
    assert not items.accepts_progress_callback(lambda **options:None)


def test_mux_keeps_overall_and_current_percent_independent_and_copies_snapshots():
    events=[];mux=ProgressMux(events.append)
    mux({'stage':'Maintenance','percentage':42,'item_progress':progress('maintenance','scan',12)})
    mux.background('installing',{'id':'A','current':1,'total':2,'item_progress':progress()})
    first=events[-1]
    assert first['percentage']==42 and first['item_progress']['percent']==12
    assert first['jobs'][0]['item_progress']['percent']==25
    first['item_progress']['percent']=99;first['jobs'][0]['item_progress']['percent']=99
    mux.heartbeat()
    assert events[-1]['item_progress']['percent']==12 and events[-1]['jobs'][0]['item_progress']['percent']==25
    mux.background('installing',{'id':'A','current':1,'total':2,'item_progress':progress(phase='install',percent=None)})
    assert events[-1]['jobs'][0]['item_progress']['percent'] is None
    mux.background('installing',{'id':'B','current':1,'total':2,'item_progress':progress('app:B',percent=0)})
    assert events[-1]['jobs'][0]['item_progress']['percent']==0
    mux.background('completed',{'current':2,'total':2})
    assert events[-1]['jobs'][0]['item_progress'] is None
    mux.wait_for_updates()
    assert events[-1]['item_progress'] is None


def test_progress_only_ticks_never_persist_or_create_intent_and_cannot_revive_terminal_job(tmp_path,monkeypatch):
    from optimus import speed_enhancements
    monkeypatch.setattr(speed_enhancements,'_current_owner',lambda:'fixture')
    events=[];mux=ProgressMux(events.append)
    job=ParallelAppUpdates(SimpleNamespace(data_dir=tmp_path),'a'*32,WINDOWS_SOFTWARE,lambda *_:None,mux)
    job._write=Mock();job.record['status']='running'
    job.item_progress({'id':'A','current':0,'total':2,'item_progress':progress()})
    before=copy.deepcopy(job.record)
    for value in range(200):
        job.item_progress({'id':'A','current':0,'total':2,'progress_only':True,'item_progress':progress(percent=value%100)})
    assert job._write.call_count==1 and job.record==before
    count=len(events)
    job.item_progress({'progress_only':True,'item_progress':progress('app:other')})
    job.record['status']='completed'
    job.item_progress({'progress_only':True,'item_progress':progress()})
    assert len(events)==count and job._write.call_count==1


def test_native_percent_parsing_is_phase_bound_and_never_infers_terminal_success():
    events=[];parser=winget.WinGetProgressParser(events.append)
    parser.feed('80%\rDownloading https://example.invalid/app\r\x1b[32m████ 25%\x1b[0m\r')
    parser.feed('Successfully verified installer hash\nStarting package install...\n████ 75%\r100%\r')
    assert events[0]=={'phase':'working','percent':None}
    assert {'phase':'download','percent':25.0} in events
    assert {'phase':'install','percent':75.0} in events
    assert not any(e['percent']==100 for e in events)
    parser.feed('unrelated installer log says 90%\n')
    assert events[-1]=={'phase':'working','percent':None}


@pytest.mark.parametrize('line,expected', [('12.5 MB / 25.0 MB',50.0),('0 KB / 25 KB',0.0),
    ('25 MB / 25 MB',99.0),('25 KB / 0 KB',None),('26 MB / 25 MB',None),
    ('512 KB / 1 MB',50.0),('12,5 MB / 25,0 MB',None),('-1 MB / 25 MB',None)])
def test_native_byte_counters_are_marked_approximate(line,expected):
    events=[];parser=winget.WinGetProgressParser(events.append)
    parser.feed('Downloading https://example.invalid/app\n'+line+'\r')
    assert events[-1]['percent']==expected
    assert (events[-1].get('estimated') is True)==(expected is not None)


def test_inconsistent_totals_stay_unknown_until_a_new_native_download_starts():
    events=[];parser=winget.WinGetProgressParser(events.append)
    parser.feed('Downloading A\n5 MB / 10 MB\r5 MB / 20 MB\r6 MB / 20 MB\r')
    assert events[-1]['percent'] is None and events[-2]['percent'] is None
    parser.feed('Downloading B\n5 MB / 20 MB\r')
    assert events[-1]['phase']=='download' and events[-1]['percent']==25.0 and events[-1]['estimated'] is True
    assert events[-1]['downloaded_bytes']==5*1024**2
    assert events[-1]['download_total_bytes']==20*1024**2
    parser.feed('x'*9000+'80%\r')
    assert events[-1]['percent']==25.0


def test_stream_runner_reports_before_process_exit_and_preserves_complete_output():
    observed=[]
    script="import sys,time;sys.stdout.write('Downloading fixture\\n');sys.stdout.flush();time.sleep(.15);sys.stdout.write('12.5 MB / 25.0 MB\\r');sys.stdout.flush();time.sleep(.2);sys.stdout.write('Starting package install...\\n');sys.stderr.write('fixture stderr');sys.stdout.flush();time.sleep(.2)"
    result=winget.run_winget_with_progress([sys.executable,'-c',script],timeout=5,
        progress_callback=lambda value:observed.append((time.monotonic(),value)))
    assert result.returncode==0 and result.capture_complete is True
    assert '12.5 MB / 25.0 MB' in result.stdout and result.stderr=='fixture stderr'
    assert any(value['phase']=='download' and value['percent']==50.0 and value.get('estimated') is True
               and value.get('downloaded_bytes')==int(12.5*1024**2) for _,value in observed)
    assert observed and observed[0][0]<time.monotonic()-.1


def test_runner_timeout_never_kills_or_terminates_the_exact_child(monkeypatch):
    original=winget.subprocess.Popen;created=[]
    def launch(*args,**kwargs):
        child=original(*args,**kwargs);child.kill=Mock(side_effect=AssertionError('Do not kill.'))
        child.terminate=Mock(side_effect=AssertionError('Do not terminate.'));created.append(child);return child
    monkeypatch.setattr(winget.subprocess,'Popen',launch)
    with pytest.raises(subprocess.TimeoutExpired):
        winget.run_winget_with_progress([sys.executable,'-c','import time;time.sleep(.25)'],timeout=.02,progress_callback=lambda _:None)
    child=created[0]
    assert child.poll() is None
    child.wait(timeout=3)
    child.kill.assert_not_called();child.terminate.assert_not_called()


def test_truncated_transcript_is_explicitly_incomplete_without_unbounded_capture():
    result=winget.run_winget_with_progress([sys.executable,'-c',"import sys;sys.stdout.write('x'*(3*1024**2))"],
        timeout=5,progress_callback=lambda _:None)
    assert result.returncode==0 and result.capture_complete is False
    assert len(result.stdout)<2*1024**2+100


def test_read_failure_is_incomplete_even_when_the_native_exit_is_zero(monkeypatch):
    class Fault(io.BytesIO):
        def read1(self,_):raise OSError('fixture read fault')
    child=SimpleNamespace(stdout=Fault(),stderr=io.BytesIO(),poll=lambda:0)
    monkeypatch.setattr(winget.subprocess,'Popen',lambda *args,**kwargs:child)
    result=winget.run_winget_with_progress(['fixture-only'],timeout=1,progress_callback=lambda _:None)
    assert result.returncode==0 and result.capture_complete is False


def test_two_application_items_reset_and_late_old_callback_is_inert(monkeypatch):
    from optimus.automated_software import run_software
    from test_automated_software import backend,configuration,package
    value=backend(package('Vendor.One'),package('Vendor.Two'));events=[];callbacks=[]
    def update(identity,*,cancel_event=None,progress_callback=None):
        callbacks.append(progress_callback)
        progress_callback({'phase':'download','percent':25})
        progress_callback({'phase':'install','percent':None})
        return {'exitCode':0,'installer_started':True,'outcomeKnown':True,'running':False}
    value.update_app_unattended=update
    result=run_software(value,configuration(),item_callback=events.append)
    starts=[e['item_progress'] for e in events if not e.get('progress_only')]
    assert [e['key'] for e in starts]==['software:update:Vendor.One','software:update:Vendor.Two']
    assert all(e['percent'] is None for e in starts)
    count=len(events);callbacks[0]({'phase':'install','percent':95})
    assert len(events)==count and result['failed_items']==0


def test_driver_native_phases_are_bound_to_the_exact_update_and_revision():
    from optimus.automated_system import run_drivers
    from test_automated_system import backend,driver_config
    value=backend();events=[];callbacks=[]
    def install(record,accept_eula=False,*,progress_callback=None):
        callbacks.append(progress_callback)
        progress_callback({'phase':'downloading','percent':30})
        progress_callback({'phase':'installing','percent':None})
        return {'resultCode':2,'hresult':0,'restartNeeded':False,'installer_started':True,'outcomeKnown':True,'running':False}
    value.driver_update_apply_unattended=install
    result=run_drivers(value,driver_config(),item_callback=events.append)
    actual=[e['item_progress'] for e in events if isinstance(e.get('item_progress'),dict)
            and e['item_progress']['key'].startswith('driver:update:')]
    assert actual[0]['percent'] is None and actual[1]['percent']==30
    assert actual[-1]['phase']=='installing' and actual[-1]['percent'] is None
    assert all(e['key'].endswith('000000000001:1') for e in actual)
    count=len(events);callbacks[0]({'phase':'installing','percent':100})
    assert len(events)==count and result['failed_items']==0


@pytest.mark.parametrize('code,text,incomplete,complete', [
    (0,'Successfully installed',False,True), (1,'failed',False,False),
    (0,'Restart your PC to finish installation.',False,False),
    (1618,'Another installation is already in progress.',False,False),
    (0,'Successfully installed',True,False)])
def test_only_complete_successful_native_app_end_reports_100(tmp_path,code,text,incomplete,complete):
    from optimus.maintenance import Maintenance,UpdateOutcomeError
    tool=Maintenance(None,tmp_path)
    tool._last_update_ids={'Vendor.App'}
    tool._last_update_packages={'Vendor.App':{'id':'Vendor.App','name':'Fixture','version':'1.0','available':'2.0','source':'winget'}}
    tool._last_update_scan=time.time()
    tool.updates_scan=Mock(return_value={'structured':True,'exitCode':0})
    tool._pin_status=Mock(return_value=(True,'',False));tool._winget=Mock(return_value='fixture-winget.exe')
    receipt=subprocess.CompletedProcess([],code,text,'');receipt.capture_complete=not incomplete
    tool._run=Mock(return_value=receipt);events=[]
    if incomplete:
        with pytest.raises(UpdateOutcomeError):
            tool.updates_apply('Vendor.App',confirmed=True,progress_callback=events.append)
    else:
        tool.updates_apply('Vendor.App',confirmed=True,progress_callback=events.append)
    assert (any(e['percent']==100 for e in events))==complete
    if complete:assert events[-1]['item_terminal'] is True
    assert tool._run.call_count==1


def test_foreground_workflow_passes_actual_item_progress_but_not_stage_placeholder_percent(tmp_path):
    from optimus.workflow import Workflow,DEFAULTS
    from test_workflow import MockBackend
    value=MockBackend(tmp_path);events=[]
    def speed(settings,*,cancel_event=None,progress_callback=None):
        progress_callback({'description':'Real fixture item','current':1,'total':2,
            'item_progress':progress('maintenance:fixture','verify',37.5)})
        progress_callback({'description':'Opaque fixture step','percentage':88})
        return {'items':[],'failed_items':0}
    value._apply_speed_enhancements_stage=speed
    settings=copy.deepcopy(DEFAULTS)
    for key,current in settings.items():
        if type(current) is bool:settings[key]=False
    settings['speed_enhancements_enabled']=True
    Workflow(value).run(settings,events.append)
    actual=[e['item_progress'] for e in events if isinstance(e.get('item_progress'),dict)]
    assert any(e['key']=='maintenance:fixture' and e['percent']==37.5 for e in actual)
    assert any(e['label']=='Opaque fixture step' and e['percent'] is None for e in actual)
    assert not any(e['percent']==88 for e in actual)
