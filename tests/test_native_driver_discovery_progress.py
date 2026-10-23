"""Driver discovery progress uses returned records, never elapsed search time."""
import os
from pathlib import Path
import subprocess
import json
from unittest.mock import Mock
import pytest

from optimus.automated_system import run_drivers
from optimus.backend import Backend
from test_driver_coverage import offer, device


def service(request):
    value=Backend.__new__(Backend)
    value.bridge=Mock(request=Mock(side_effect=request))
    return value


def inventory(*,complete=True):
    return {'complete':complete,'devices':[device()],'errors':[]}


def test_driver_scan_observer_is_optional_and_does_not_change_native_arguments():
    calls=[]
    def request(operation,**options):
        calls.append((operation,options))
        return {'resultCode':2,'updates':[offer()]} if operation=='driver_updates_scan' else inventory()
    result=service(request).driver_updates_scan()
    assert result['resultCode']==2 and result['coverage']['inventoryComplete']
    assert calls==[('driver_updates_scan',{'timeout':300}),('driver_device_inventory',{'timeout':120})]


@pytest.mark.parametrize('invalid',[False,True,7,'callback',{},[]])
def test_invalid_discovery_observer_is_rejected_before_native_queries(invalid):
    value=service(lambda *_a,**_k:None)
    with pytest.raises(ValueError):
        value.driver_updates_scan(progress_callback=invalid)
    value.bridge.request.assert_not_called()


def test_backend_opt_in_uses_exact_native_callback_and_final_validated_completion():
    events=[];calls=[]
    def request(operation,**options):
        calls.append((operation,dict(options)))
        assert options['reportProgress'] is True and callable(options['progress_callback'])
        callback=options['progress_callback']
        if operation=='driver_updates_scan':
            callback({'phase':'scan','percent':None})
            callback({'phase':'driver_offers','percent':50})
            return {'resultCode':2,'updates':[offer()]}
        callback({'phase':'device_query','percent':None})
        callback({'phase':'device_records','percent':50})
        return inventory()
    result=service(request).driver_updates_scan(progress_callback=events.append)
    assert result['coverage']['inventoryComplete']
    assert calls[0][0]=='driver_updates_scan' and calls[1][0]=='driver_device_inventory'
    assert all(options['progress_callback']==events.append for _,options in calls)
    assert {'phase':'device_records','percent':50} in events
    assert events[-1]=={'phase':'verify','percent':100}
    assert all(event['percent'] is None for event in events if event['phase'] in ('scan','device_query'))


@pytest.mark.parametrize('scan,complete',[
    ({'resultCode':4,'updates':[],'error':'source unavailable'},True),
    ({'resultCode':2,'updates':[],'warnings':[{'message':'partial source'}]},True),
    ({'resultCode':2,'updates':[]},False),
    ({'resultCode':2,'updates':'malformed'},True),
])
def test_incomplete_discovery_never_receives_final_verify_100(scan,complete):
    events=[]
    def request(operation,**_options):
        return scan if operation=='driver_updates_scan' else inventory(complete=complete)
    service(request).driver_updates_scan(progress_callback=events.append)
    assert events[-1]=={'phase':'verify','percent':None}
    assert not any(event['phase']=='verify' and event['percent']==100 for event in events)


def test_observer_failure_does_not_replace_scan_or_inventory_receipts():
    def request(operation,**_options):
        return {'resultCode':2,'updates':[offer()]} if operation=='driver_updates_scan' else inventory()
    def observer(_event):
        raise RuntimeError('display-only failure')
    result=service(request).driver_updates_scan(progress_callback=observer)
    assert result['resultCode']==2 and result['coverage']['inventoryComplete']


def test_scan_exception_keeps_original_failure_and_still_collects_inventory():
    calls=[];events=[]
    def request(operation,**_options):
        calls.append(operation)
        if operation=='driver_updates_scan':
            raise TimeoutError('native read-only scan still unresolved')
        return inventory()
    result=service(request).driver_updates_scan(progress_callback=events.append)
    assert calls==['driver_updates_scan','driver_device_inventory']
    assert result['resultCode']==4 and result['error']=='native read-only scan still unresolved'
    assert result['coverage']['inventoryComplete']
    assert events[-1]['percent'] is None


def test_runner_binds_discovery_events_and_closes_late_callback_without_installs():
    events=[];callbacks=[]
    def request(operation,**options):
        callback=options['progress_callback'];callbacks.append(callback)
        if operation=='driver_updates_scan':
            callback({'phase':'scan','percent':None})
            callback({'phase':'driver_offers','percent':50})
            return {'resultCode':2,'updates':[offer()]}
        callback({'phase':'installed_driver_query','percent':None})
        callback({'phase':'device_records','percent':50})
        return inventory()
    value=service(request)
    result=run_drivers(value,{'driver_updates_enabled':True,'driver_policy':'review'},item_callback=events.append)
    assert result['review_only'] and result['driver_scan']['complete']
    assert events[0]['discovery_progress'] is True and not events[0].get('progress_only')
    assert all(event['item_progress']['key']=='driver:discovery' for event in events)
    assert any(event['item_progress']['phase']=='device_records' and event['item_progress']['percent']==50 for event in events)
    assert all(event['discovery_progress'] is True for event in events)
    count=len(events)
    for callback in callbacks:
        callback({'phase':'verify','percent':75})
    assert len(events)==count
    assert [call.args[0] for call in value.bridge.request.call_args_list]==['driver_updates_scan','driver_device_inventory']


def test_runner_does_not_retry_a_scan_after_observer_enabled_method_raises():
    events=[];callbacks=[];count=0
    def scan(*,progress_callback=None):
        nonlocal count
        count+=1;callbacks.append(progress_callback)
        progress_callback({'phase':'scan','percent':None})
        raise TypeError('native fixture query failed after start')
    value=Mock()
    value.driver_updates_scan=scan
    with pytest.raises(TypeError) as raised:
        run_drivers(value,{'driver_updates_enabled':True,'driver_policy':'review'},item_callback=events.append)
    assert count==1 and raised.value.driver_scan['complete'] is False
    before=len(events);callbacks[0]({'phase':'verify','percent':100})
    assert len(events)==before and all(event['item_progress']['percent'] is None for event in events)


def test_legacy_scan_signature_is_not_given_new_callback_arguments():
    events=[];calls=[]
    class Legacy:
        def driver_updates_scan(self):
            calls.append('scan')
            return {'resultCode':2,'updates':[]}
    result=run_drivers(Legacy(),{'driver_updates_enabled':True,'driver_policy':'review'},item_callback=events.append)
    assert calls==['scan'] and result['review_only']
    assert len(events)==1 and events[0]['item_progress']['percent'] is None


@pytest.mark.skipif(os.name!='nt',reason='Native Windows PowerShell 5.1 offline WUA/CIM fixtures.')
def test_offline_native_driver_discovery_reports_only_actual_query_and_record_phases():
    executable=Path(os.environ['SystemRoot'])/'System32/WindowsPowerShell/v1.0/powershell.exe'
    script=Path(__file__).with_name('Test-DriverDiscoveryProgress.ps1')
    result=subprocess.run([str(executable),'-NoLogo','-NoProfile','-NonInteractive','-ExecutionPolicy','Bypass','-File',str(script)],
                          capture_output=True,text=True,timeout=45)
    assert result.returncode==0,result.stdout+result.stderr
    report=json.loads(result.stdout)
    assert report['Result']=='PASS' and report['Assertions']>=50
    assert report['RealScans']==report['RealInstalls']==report['RealDeviceChanges']==0
