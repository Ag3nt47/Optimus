"""Durable servicing lifecycle fixtures; no DISM/SFC/native repair is launched."""
import copy
from datetime import datetime,timezone
import hashlib
import json
from pathlib import Path
import time
from types import SimpleNamespace
import uuid

import pytest

from optimus.extended_backend import ExtendedMaintenanceBackend
from optimus.system_diagnostics import (integrity_check_plan,integrity_mutation_barrier,run_windows_integrity,
                                       _INTEGRITY_STEPS)


def iso(value):return datetime.fromtimestamp(value,timezone.utc).isoformat()


def write_intent(root,*,repair=False,owner='uid:1000',identity=None):
    identity=identity or uuid.uuid4().hex
    path=Path(root)/'integrity-operations'/(identity+'.json');path.parent.mkdir(parents=True,exist_ok=True)
    value={'schema':1,'kind':'windows-integrity-intent-v1','id':identity,'owner':owner,'repair':repair,'status':'prepared','started_at':time.time()-10}
    path.write_text(json.dumps(value),encoding='utf-8')
    return path,value


def write_native_completion(root,identity,*,status='completed_review_native_logs',code=0,known=True):
    intent=json.loads((Path(root)/'integrity-operations'/(identity+'.json')).read_text())
    directory=Path(root)/'integrity-diagnostics'/identity;directory.mkdir(parents=True,exist_ok=True)
    names=['restore_health','repair_files'] if intent['repair'] else ['check_health','scan_health','verify_files']
    begun=max(intent['started_at'],time.time()-5)
    # Mock ordered timestamps fit between the already prepared intent and now.
    finish=time.time()
    span=max(.000001,(finish-begun)/(len(names)*3+1))
    items=[]
    if status not in ('requires_administrator','preflight_failed'):
        for index,name in enumerate(names):
            executable,args=_INTEGRITY_STEPS[name]
            items.append({'schema':1,'step':name,'repair':intent['repair'],'status':'exited','exit_code':code,
                          'started_utc':iso(begun+index*span*3),'finished_utc':iso(begun+index*span*3+span),
                          'executable':str((Path(root)/'System32'/executable).absolute()),'arguments':args,
                          'stdout':str(directory/(name+'.stdout.log')),'stderr':str(directory/(name+'.stderr.log'))})
            if code!=0:break
    value={'schema':1,'kind':'windows-integrity-native-v1','id':identity,'owner':intent['owner'],
           'repair':intent['repair'],'read_only':not intent['repair'],'outcomeKnown':known,
           'native_started':bool(items),'restart_needed':code==3010,'status':status,'items':items,
           'report_directory':str(directory),'started_utc':iso(begun),'finished_utc':iso(finish)}
    (directory/'completion.json').write_text(json.dumps(value),encoding='utf-8')
    return value


def test_prepared_intent_is_cross_process_barrier(tmp_path):
    path,intent=write_intent(tmp_path)
    assert 'no verified native completion' in integrity_mutation_barrier(tmp_path,owner=intent['owner'])
    assert path.exists()


def test_late_verified_native_completion_releases_barrier_without_replay(tmp_path):
    path,intent=write_intent(tmp_path)
    before=path.read_bytes()
    write_native_completion(tmp_path,intent['id'])
    assert integrity_mutation_barrier(tmp_path,owner=intent['owner']) is None
    assert path.read_bytes()==before  # Read-only reconciliation, no receipt reset.


@pytest.mark.parametrize('status,code',[('needs_review',50),('preflight_failed',0),('requires_administrator',0)])
def test_known_failed_or_unlaunched_native_outcomes_do_not_block_others(tmp_path,status,code):
    path,intent=write_intent(tmp_path)
    write_native_completion(tmp_path,intent['id'],status=status,code=code)
    assert integrity_mutation_barrier(tmp_path,owner=intent['owner']) is None


def test_restart_request_retains_gate_until_native_restart_state_clear(tmp_path):
    path,intent=write_intent(tmp_path)
    write_native_completion(tmp_path,intent['id'],status='restart_required',code=3010)
    assert integrity_mutation_barrier(tmp_path,owner=intent['owner'])
    assert integrity_mutation_barrier(tmp_path,owner=intent['owner'],reboot_reader=lambda:{'reboot_required':True})
    assert integrity_mutation_barrier(tmp_path,owner=intent['owner'],reboot_reader=lambda:{'reboot_required':False}) is None


@pytest.mark.parametrize('field,value',[('owner','uid:2000'),('id','b'*32),('repair',True),('outcomeKnown',False),('schema',True)])
def test_mismatched_native_completion_stays_blocked(tmp_path,field,value):
    path,intent=write_intent(tmp_path)
    completion=write_native_completion(tmp_path,intent['id']);completion[field]=value
    (tmp_path/'integrity-diagnostics'/intent['id']/'completion.json').write_text(json.dumps(completion))
    assert integrity_mutation_barrier(tmp_path,owner=intent['owner'])


def test_modified_exact_command_cannot_clear_servicing_gate(tmp_path):
    path,intent=write_intent(tmp_path)
    completion=write_native_completion(tmp_path,intent['id']);completion['items'][0]['arguments']='/Online /Cleanup-Image /ResetBase'
    (tmp_path/'integrity-diagnostics'/intent['id']/'completion.json').write_text(json.dumps(completion))
    assert integrity_mutation_barrier(tmp_path,owner=intent['owner'])


def test_acknowledged_native_receipt_cannot_change(tmp_path):
    path,intent=write_intent(tmp_path)
    write_native_completion(tmp_path,intent['id'])
    completion=tmp_path/'integrity-diagnostics'/intent['id']/'completion.json'
    intent.update(status='finished',completion_sha256=hashlib.sha256(completion.read_bytes()).hexdigest())
    path.write_text(json.dumps(intent))
    assert integrity_mutation_barrier(tmp_path,owner=intent['owner']) is None
    completion.write_bytes(completion.read_bytes()+b' ')
    assert integrity_mutation_barrier(tmp_path,owner=intent['owner'])


def test_orphan_native_directory_cannot_be_ignored(tmp_path):
    (tmp_path/'integrity-diagnostics'/('a'*32)).mkdir(parents=True)
    assert integrity_mutation_barrier(tmp_path,owner='uid:1000')


def test_foreign_owned_intent_is_preserved_and_blocks(tmp_path):
    path,intent=write_intent(tmp_path)
    before=path.read_bytes()
    assert integrity_mutation_barrier(tmp_path,owner='uid:2000')
    assert path.read_bytes()==before


def test_oversized_intent_never_reads_as_known_completion(tmp_path):
    path,intent=write_intent(tmp_path);path.write_bytes(b' '*(512*1024+1))
    assert integrity_mutation_barrier(tmp_path,owner=intent['owner'])


def test_global_recovery_guard_checks_integrity_before_other_recovery(tmp_path):
    path,intent=write_intent(tmp_path)
    backend=ExtendedMaintenanceBackend();backend.data_dir=tmp_path
    assert 'integrity' in backend.recovery_mutation_barrier().lower()


def test_bridge_success_without_durable_native_completion_is_not_enough(tmp_path):
    calls=[]
    def request(operation,**kwargs):calls.append((operation,kwargs));return {'outcomeKnown':True,'status':'completed'}
    backend=SimpleNamespace(data_dir=tmp_path,bridge=SimpleNamespace(request=request))
    result=run_windows_integrity(backend,integrity_check_plan(requested=True),platform_name='Windows')
    assert result['status']=='running_outcome_unverified' and len(calls)==1
    again=run_windows_integrity(backend,integrity_check_plan(requested=True),platform_name='Windows')
    assert again['status']=='deferred' and len(calls)==1


def test_timeout_then_late_native_receipt_clears_gate_without_second_dispatch(tmp_path):
    calls=[]
    def request(operation,**kwargs):calls.append(kwargs);raise TimeoutError()
    backend=SimpleNamespace(data_dir=tmp_path,bridge=SimpleNamespace(request=request))
    result=run_windows_integrity(backend,integrity_check_plan(requested=True),platform_name='Windows')
    assert result['retry_allowed'] is False
    intent=json.loads(Path(result['journal']).read_text())
    write_native_completion(tmp_path,intent['id'])
    assert integrity_mutation_barrier(tmp_path,owner=intent['owner']) is None
    assert len(calls)==1
