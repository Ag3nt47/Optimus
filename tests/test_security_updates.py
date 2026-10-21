"""Security-update tests mock WUA; only parsing and owned journal files are real."""
import copy
import json
import os
from pathlib import Path
import subprocess
import threading
from unittest.mock import Mock, patch

import pytest

from optimus import security_updates as security


def row(number=1, **changes):
    value={'id':f'00000000-0000-0000-0000-{number:012d}','revision':1,
           'title':'Fixture security update','kb':['1234567'],
           'categories':[sorted(security.CLASSIFICATIONS)[0]],'interactive':False,'reboot_behavior':2}
    value.update(changes)
    return value


def scan(rows=None, **changes):
    value={'ok':True,'complete':True,'resultCode':2,'reboot_required':False,
           'updates':[row()] if rows is None else rows}
    value.update(changes)
    return value


def success(**changes):
    value={'ok':True,'resultCode':2,'itemResultCode':2,'hresult':0,'aggregate_hresult':0,
           'reboot_required':False,'may_continue':False}
    value.update(changes)
    return value


def settings(**changes):
    value={'windows_security_updates_enabled':True,'windows_security_updates_install_approved':True,
           'windows_security_update_agreements_accepted':True}
    value.update(changes)
    return value


def service(tmp_path, metadata=None, outcomes=None):
    runner=Mock(side_effect=[scan() if metadata is None else metadata, *(outcomes or [success()])])
    return security.SecurityUpdates(tmp_path,runner),runner


@pytest.mark.parametrize('changes',[{'ok':False},{'complete':False},{'resultCode':3},{'resultCode':True},
    {'reboot_required':None},{'reboot_required':0},{'updates':None},{'updates':[row()]*1001}])
def test_invalid_or_incomplete_scan_never_supplies_install_candidates(changes):
    assert security.candidates(scan(**changes)) is None


@pytest.mark.parametrize('changes',[{'id':'bad'},{'revision':True},{'revision':0},{'title':''},
    {'categories':['not-a-guid']},{'categories':['00000000-0000-0000-0000-000000000001']},
    {'interactive':None},{'reboot_behavior':None},{'reboot_behavior':True},{'reboot_behavior':3},
    {'installed':True}])
def test_bad_or_inapplicable_native_row_invalidates_entire_scan(changes):
    assert security.candidates(scan([row(),row(2,**changes)])) is None


def test_duplicate_ids_even_different_revisions_are_ambiguous_and_never_apply(tmp_path):
    tool,runner=service(tmp_path,scan([row(),row(revision=2)]))
    result=tool.run(settings())
    assert result['failed_items']==1
    assert runner.call_count==1


def test_candidates_are_isolated_from_mutating_caller_metadata():
    original=scan()
    selected=security.candidates(original)
    selected[0]['id']='caller-changed'
    assert original['updates'][0]['id']==row()['id']


def test_already_installed_up_to_date_scan_starts_no_installer(tmp_path):
    tool,runner=service(tmp_path,scan([]))
    result=tool.run(settings())
    assert result['items']==[] and result['failed_items']==0
    assert 'No applicable' in result['note']
    assert runner.call_count==1
    assert not list(tool.directory.glob('*.json'))


@pytest.mark.parametrize('changes',[{'windows_security_updates_enabled':False},
    {'windows_security_updates_install_approved':False},
    {'windows_security_updates_install_approved':1},
    {'windows_security_update_agreements_accepted':False}])
def test_disabled_or_missing_scope_and_agreement_never_install(tmp_path,changes):
    tool,runner=service(tmp_path)
    result=tool.run(settings(**changes))
    assert result['items']==[]
    assert not any(call.args[0]=='install' for call in runner.call_args_list)


def test_pending_restart_and_interactive_updates_are_deferred(tmp_path):
    tool,runner=service(tmp_path,scan(reboot_required=True))
    result=tool.run(settings())
    assert result['skipped'] is True and result['failed_items']==0
    assert runner.call_count==1
    tool,runner=service(tmp_path,scan([row(interactive=True)]))
    result=tool.run(settings())
    assert result['skipped_items']==1
    assert result['items'][0]['status']=='skipped'
    assert runner.call_count==1


def test_successful_exact_revision_is_journaled_before_mock_install(tmp_path):
    tool,runner=service(tmp_path)
    def operation(kind,payload,**options):
        if kind=='scan':return scan()
        journal=json.loads(next(tool.directory.glob('*.json')).read_text())
        assert journal['status']=='installing'
        assert payload=={'id':row()['id'],'revision':1,'approved':True,'accept_agreements':True}
        return success()
    runner.side_effect=operation
    result=tool.run(settings())
    assert result['failed_items']==0 and result['items'][0]['status']=='completed'
    assert json.loads(next(tool.directory.glob('*.json')).read_text())['status']=='completed'


@pytest.mark.parametrize('outcome',[success(reboot_required=None),success(hresult=None),
    success(aggregate_hresult=-1),{'ok':True,'resultCode':2,'itemResultCode':2},None,
    {'ok':False,'may_continue':True,'error':'Fixture timed out'},
    {'ok':False,'cancelled':True,'may_continue':True}])
def test_unknown_or_inflight_install_results_block_remaining_updates_and_replay(tmp_path,outcome):
    tool,runner=service(tmp_path,scan([row(),row(2)]),[outcome])
    result=tool.run(settings())
    assert result['failed_items']==1 and result['status']=='uncertain'
    assert result['deferred_ids']==[row(2)['id']]
    assert runner.call_count==2
    tool.runner=Mock(return_value=scan())
    retry=tool.run(settings())
    assert retry['requiresReview'] is True and retry['failed_items']==1
    assert tool.runner.call_count==1


def test_confirmed_failure_stops_batch_and_restart_stops_without_automatic_reboot(tmp_path):
    tool,runner=service(tmp_path,scan([row(),row(2)]),[{'ok':False,'may_continue':False,'error':'Fixture refused before install'}])
    result=tool.run(settings())
    assert result['failed_items']==1 and result['status']=='failed'
    assert runner.call_count==2
    tool,runner=service(tmp_path/'restart',scan([row(),row(2)]),[success(reboot_required=True)])
    result=tool.run(settings())
    assert result['reboot_required'] is True and result['failed_items']==0
    assert result['deferred_ids']==[row(2)['id']]
    assert runner.call_count==2


def test_no_longer_offered_exact_revision_is_a_safe_skip(tmp_path):
    tool,runner=service(tmp_path,outcomes=[{'ok':True,'skipped':True,'reason':'No longer offered'}])
    result=tool.run(settings())
    assert result['failed_items']==0 and result['items'][0]['status']=='skipped'


def test_verified_low_system_disk_skip_defers_entire_remaining_batch(tmp_path):
    tool,runner=service(tmp_path,scan([row(),row(2)]),[{'ok':True,'skipped':True,'reason':'low_system_disk',
        'free_bytes':240*1024*1024,'minimum_free_bytes':2*1024**3}])
    result=tool.run(settings())
    assert result['low_system_disk'] is True and result['failed_items']==0
    assert result['deferred_ids']==[row(2)['id']]
    assert runner.call_count==2


def test_cancellation_before_scan_and_from_progress_cannot_start_install(tmp_path):
    tool,runner=service(tmp_path)
    stop=threading.Event();stop.set()
    assert tool.run(settings(),stop)['cancelled'] is True
    runner.assert_not_called()
    stop.clear()
    result=tool.run(settings(),stop,lambda _:stop.set())
    assert result['cancelled'] is True and result['status']=='cancelled'
    assert result['failed_items']==0 and runner.call_count==1


def test_confirmed_prelaunch_cancellation_is_not_counted_as_failed_install(tmp_path):
    tool,runner=service(tmp_path,outcomes=[{'ok':False,'cancelled':True,'may_continue':False}])
    result=tool.run(settings())
    assert result['cancelled'] is True and result['failed_items']==0
    assert result['items'][0]['status']=='cancelled'


def test_runner_errors_count_scan_failure_and_unknown_install_is_preserved(tmp_path):
    tool,runner=service(tmp_path)
    runner.side_effect=TimeoutError('Fixture scan timeout')
    result=tool.run(settings())
    assert result['failed_items']==1 and result['items']==[]
    runner.side_effect=[scan(),TimeoutError('Fixture installer unknown')]
    result=tool.run(settings())
    assert result['failed_items']==1 and result['status']=='uncertain'


@pytest.mark.parametrize('contents',[{'schema':1,'status':'installing'},{'schema':1,'status':'uncertain'},
    {'schema':1,'status':'unknown'},{'status':'completed'},[],False,'malformed'])
def test_unverifiable_prior_journal_blocks_installation(tmp_path,contents):
    tool,runner=service(tmp_path)
    tool.directory.mkdir()
    identity='a'*32
    if isinstance(contents,dict):contents=dict(contents,id=identity)
    (tool.directory/(identity+'.json')).write_text('{broken' if contents=='malformed' else json.dumps(contents))
    result=tool.run(settings())
    assert result['requiresReview'] is True and result['failed_items']==1
    assert runner.call_count==1


def test_hardlinked_journal_cannot_supply_install_authority(tmp_path):
    tool,runner=service(tmp_path)
    tool.directory.mkdir()
    identity='b'*32
    original=tmp_path/'outside.json';original.write_text(json.dumps({'schema':1,'id':identity,'status':'completed'}))
    os.link(original,tool.directory/(identity+'.json'))
    result=tool.run(settings())
    assert result['requiresReview'] is True
    assert runner.call_count==1


def test_journal_write_failure_never_starts_installer(tmp_path):
    tool,runner=service(tmp_path)
    with patch.object(security,'atomic_json',side_effect=OSError('Fixture disk full')):
        with pytest.raises(OSError):tool.run(settings())
    assert runner.call_count==1


def test_active_request_cannot_start_a_second_scan_or_install(tmp_path):
    tool,runner=service(tmp_path)
    tool._lock.acquire()
    try:
        assert tool.run(settings())['failed_items']==1
    finally:
        tool._lock.release()
    runner.assert_not_called()


def test_separate_service_instance_cannot_race_an_active_apply_lease(tmp_path):
    from optimus.automation import RunLease
    tool,runner=service(tmp_path)
    with RunLease(tool.directory):
        result=tool.run(settings())
    assert result['requiresReview'] is True and result['failed_items']==1
    runner.assert_not_called()


@pytest.mark.skipif(os.name!='nt',reason='Windows-only native preflight.')
def test_native_cancellation_preflight_does_not_spawn_powershell(monkeypatch):
    stop=threading.Event();stop.set()
    with patch.object(security.subprocess,'Popen') as process:
        result=security.native_request('install',{},cancel_event=stop)
    assert result['cancelled'] is True and result['may_continue'] is False
    process.assert_not_called()


@pytest.mark.skipif(os.name!='nt',reason='Windows PowerShell syntax-only parser.')
def test_native_script_parses_without_executing_any_update_api(tmp_path):
    script=tmp_path/'parse-only.ps1';script.write_text(security._NATIVE.replace('__PAYLOAD__','e30='),encoding='utf-8')
    parser="$tokens=$null;$errors=$null;[void][Management.Automation.Language.Parser]::ParseFile($env:OPTIMUS_SYNTAX_ONLY,[ref]$tokens,[ref]$errors);if($errors.Count){$errors|ForEach-Object{$_.Message};exit 1}"
    powershell=Path(os.environ['SystemRoot'])/'System32/WindowsPowerShell/v1.0/powershell.exe'
    result=subprocess.run([str(powershell),'-NoLogo','-NoProfile','-NonInteractive','-Command',parser],
        env=dict(os.environ,OPTIMUS_SYNTAX_ONLY=str(script)),capture_output=True,text=True,timeout=30)
    assert result.returncode==0,result.stdout+result.stderr
    assert 'RebootRequiredBeforeInstallation' in security._NATIVE
    assert 'ForceQuiet=$true' in security._NATIVE and 'AllowSourcePrompts=$false' in security._NATIVE
    assert 'AvailableFreeSpace -lt 2147483648' in security._NATIVE
    assert 'Restart-Computer' not in security._NATIVE and 'shutdown.exe' not in security._NATIVE
