from pathlib import Path
from types import SimpleNamespace
import threading

import pytest

from optimus.system_diagnostics import (GIB, check_installer_headroom, collect_system_diagnostics,
                                        integrity_check_plan, run_windows_integrity)


class Bridge:
    def __init__(self, response=None, error=None): self.response=response; self.error=error; self.calls=[]
    def request(self, operation, **kwargs):
        self.calls.append((operation, kwargs))
        if self.error: raise self.error
        if callable(self.response):return self.response(operation,kwargs)
        return self.response


def test_windows_native_evidence_recommendations():
    bridge = Bridge({'schema':1,'read_only':True,'memory':{'percent':95},'pagefile':{'automatic_managed':False},
                     'storage':[{'wear_percent_used':95}], 'errors':[]})
    report = collect_system_diagnostics(SimpleNamespace(bridge=bridge), platform_name='Windows')
    assert bridge.calls == [('system_diagnostics', {'timeout':90})]
    assert report['read_only'] and len(report['recommendations']) == 2
    assert report['storage'][0]['wear_percent_used'] == 95
    assert all(row['automatic'] is False for row in report['recommendations'])


@pytest.mark.parametrize('response', [None, [], {'schema':1,'read_only':False}, {'schema':2,'read_only':True}])
def test_invalid_native_schema_is_partial(response):
    report = collect_system_diagnostics(SimpleNamespace(bridge=Bridge(response)), platform_name='Windows')
    assert report['status'] == 'partial'
    assert report['capabilities']['native_health'] == 'unavailable'


def test_cancel_does_not_start_native_scan():
    event = threading.Event(); event.set()
    bridge = Bridge()
    assert collect_system_diagnostics(SimpleNamespace(bridge=bridge), event, platform_name='Windows')['status'] == 'cancelled'
    assert not bridge.calls


def test_linux_battery_health_uses_compatible_units(tmp_path, monkeypatch):
    battery = tmp_path / 'BAT0'; battery.mkdir()
    for key, value in {'type':'Battery','energy_full':'800','energy_full_design':'1000','cycle_count':'50'}.items():
        (battery/key).write_text(value)
    monkeypatch.setattr('optimus.system_diagnostics.shutil.which', lambda name: None)
    report = collect_system_diagnostics(platform_name='Linux', battery_root=tmp_path)
    assert report['batteries'][0]['full_charge_vs_design_percent'] == 80
    assert report['batteries'][0]['cycle_count'] == 50
    assert report['capabilities']['storage_wear_temperature'] == 'not_exposed_by_inbox_collector'


def test_linux_missing_health_is_unknown(tmp_path, monkeypatch):
    monkeypatch.setattr('optimus.system_diagnostics.shutil.which', lambda name: None)
    report = collect_system_diagnostics(platform_name='Linux', battery_root=tmp_path)
    assert report['batteries'] == []
    assert report['capabilities']['battery_health'] == 'not_exposed'


def test_macos_battery_serial_is_not_exported(monkeypatch):
    monkeypatch.setattr('optimus.system_diagnostics.shutil.which', lambda name:'/usr/sbin/system_profiler')
    payload = b'{"SPPowerDataType":[{"serial":"private","sppower_battery_health_info":{"sppower_battery_cycle_count":12,"serial":"secret"}}]}'
    report = collect_system_diagnostics(platform_name='Darwin', command=lambda *args,**kwargs:payload)
    assert report['batteries'][0]['health'] == {'sppower_battery_cycle_count':12}
    assert 'secret' not in str(report)


def test_headroom_deduplicates_volumes_and_sums_distinct_allocations(tmp_path):
    usage_calls=[]
    def usage(mount): usage_calls.append(mount); return SimpleNamespace(free=8*GIB,total=100*GIB)
    result = check_installer_headroom({'system':tmp_path,'temp':tmp_path/'cache','destination':tmp_path/'apps'},
        {'system':GIB,'temp':2*GIB,'destination':2*GIB}, disk_usage=usage, volume_resolver=lambda path:('same',str(tmp_path)))
    assert len(usage_calls) == 1
    assert len(result['volumes']) == 1
    assert result['volumes'][0]['known_required_bytes'] == 5*GIB
    assert result['status'] == 'sufficient_for_estimate'


def test_unknown_sizes_never_claim_sufficient(tmp_path):
    result=check_installer_headroom({'temp':tmp_path},disk_usage=lambda path:SimpleNamespace(free=10*GIB,total=20*GIB),
                                    volume_resolver=lambda path:('same',str(tmp_path)))
    assert result['status'] == 'requirement_unknown'
    assert result['volumes'][0]['unknown_size_labels'] == ['temp']


def test_insufficient_space_is_explicit(tmp_path):
    result=check_installer_headroom({'temp':tmp_path},{'temp':3*GIB},disk_usage=lambda path:SimpleNamespace(free=GIB,total=10*GIB),
                                    volume_resolver=lambda path:('same',str(tmp_path)))
    assert result['status'] == 'insufficient_known_space'
    assert result['volumes'][0]['known_shortfall_bytes'] == 4*GIB


@pytest.mark.parametrize('required,reserve', [({'temp':-1},0),({'temp':True},0),({'unknown':1},0),({},-1),({},True)])
def test_invalid_requirements(required,reserve,tmp_path):
    with pytest.raises(ValueError): check_installer_headroom({'temp':tmp_path},required,reserve)


def test_headroom_errors_are_not_sufficient():
    assert check_installer_headroom({'temp':'relative'})['status'] == 'requirement_unknown'


def test_no_symptom_or_request_never_runs_integrity():
    bridge=Bridge()
    assert run_windows_integrity(SimpleNamespace(bridge=bridge),integrity_check_plan(),platform_name='Windows')['status']=='not_needed'
    assert not bridge.calls


@pytest.mark.parametrize('repair,approved', [('yes',True),(1,True),(True,1),(False,'yes')])
def test_integrity_boolean_choices_are_strict(repair,approved):
    bridge=Bridge()
    with pytest.raises(ValueError):
        run_windows_integrity(SimpleNamespace(bridge=bridge),integrity_check_plan(requested=True),repair=repair,approved=approved,platform_name='Windows')
    assert not bridge.calls


def test_repair_requires_distinct_approval():
    bridge=Bridge()
    with pytest.raises(PermissionError):
        run_windows_integrity(SimpleNamespace(bridge=bridge),integrity_check_plan(requested=True),repair=True,platform_name='Windows')
    assert not bridge.calls


def test_approved_repair_delegates_only_allowlisted_mode(tmp_path):
    from test_integrity_lifecycle import write_native_completion
    def response(operation,choices):return write_native_completion(tmp_path,choices['operation_id'])
    bridge=Bridge(response)
    result=run_windows_integrity(SimpleNamespace(bridge=bridge,data_dir=tmp_path),integrity_check_plan(['windows_update_errors']),
                                 repair=True,approved=True,platform_name='Windows')
    assert result['status']=='completed_review_native_logs'
    operation,choices=bridge.calls[0]
    assert operation=='windows_integrity' and choices['timeout']==1800 and choices['repair'] is True and choices['approved'] is True
    assert len(choices['operation_id'])==32


def test_timeout_keeps_native_operation_unverified_no_retry(tmp_path):
    bridge=Bridge(error=TimeoutError())
    result=run_windows_integrity(SimpleNamespace(bridge=bridge,data_dir=tmp_path),integrity_check_plan(requested=True),platform_name='Windows')
    assert result['status']=='running_outcome_unverified'
    assert result['retry_allowed'] is False
    assert len(bridge.calls)==1


def test_diagnostics_ignore_forged_eligible_flag():
    bridge=Bridge()
    plan=integrity_check_plan(); plan['eligible']=True
    result=run_windows_integrity(SimpleNamespace(bridge=bridge),plan,platform_name='Windows')
    assert result['status']=='not_needed' and not bridge.calls


def test_no_windows_servicing_on_linux():
    bridge=Bridge()
    result=run_windows_integrity(SimpleNamespace(bridge=bridge),integrity_check_plan(requested=True),platform_name='Linux')
    assert result['status']=='unsupported_platform' and not bridge.calls


def test_cancel_before_servicing():
    event=threading.Event();event.set();bridge=Bridge()
    result=run_windows_integrity(SimpleNamespace(bridge=bridge),integrity_check_plan(requested=True),cancel_event=event,platform_name='Windows')
    assert result['status']=='cancelled_before_start' and not bridge.calls


@pytest.mark.parametrize('symptoms', ['windows_update_errors',['slow pc'],[None],[';format c:']])
def test_unknown_integrity_symptoms_refused(symptoms):
    with pytest.raises(ValueError):integrity_check_plan(symptoms)


def test_ps_collector_has_no_mutating_storage_or_pagefile_commands():
    source=(Path(__file__).parents[1]/'src/optimus/engine/SystemDiagnostics.ps1').read_text()
    collector=source.split('function Invoke-OptimusWindowsIntegrity')[0]
    assert 'Get-CimInstance' in collector and 'Get-WinEvent' in collector
    assert not any(item in collector for item in ('Set-CimInstance','Remove-CimInstance','Reset-PhysicalDisk','Restart-Computer'))
    assert '/NoRestart' in source and 'Stop-Process' not in source
