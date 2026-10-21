"""Read-only outcome verification using owned files, SQLite fixtures and fake OS evidence."""
import copy
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import threading
from types import SimpleNamespace
from unittest.mock import Mock

import psutil
import pytest

from optimus import verification as verify
from optimus.browser_cleanup import _cache_summary
from optimus.browser_privacy import BrowserPrivacy
from optimus.cleaner import Category, Cleaner


def stage(title, result, status='completed', **extra):
    return dict(title=title, result=result, status=status, started_at=100, finished_at=110, **extra)


def report(*stages, **settings):
    return {'id': 'a'*32, 'settings': settings, 'stages': list(stages)}


def tool(tmp_path, native=None, **options):
    return verify.OutcomeVerification(tmp_path, native_reader=native or Mock(return_value={}), **options)


def checks(result, kind=None):
    return [item for entry in result['stages'] for item in entry['checks'] if kind is None or item['kind'] == kind]


def identity(path):
    info=path.lstat()
    return dict(device=info.st_dev,inode=info.st_ino,created_ns=info.st_ctime_ns,size=info.st_size,modified_ns=info.st_mtime_ns)


def test_every_stage_is_covered_without_erasing_original_errors_or_claiming_unknowns(tmp_path):
    source=report(stage('Review startup registrations',{'review_only':True}),
                  stage('Clean temporary files',{'result':{'deleted_count':0,'files':[]}},'failed'),
                  stage('Future unsupported operation',{'applied':True}))
    before=copy.deepcopy(source);reader=Mock()
    result=tool(tmp_path,reader).verify(source)
    assert source==before and result['coverage_complete'] and len(result['stages'])==3
    assert result['counts']==dict(confirmed=0,needs_review=1,unverified=1,not_applicable=2)
    assert result['failed_items']==0 and result['read_only'] and result['run_id']==source['id']
    reader.assert_not_called()


@pytest.mark.parametrize('case,expected',[('absent','confirmed'),('same','needs_review'),
    ('recreated','confirmed'),('legacy_present','unverified'),('invalid_path','unverified')])
def test_deleted_files_use_absence_and_original_identity_without_mislabeling_recreation(tmp_path,case,expected):
    target=tmp_path/'cache.tmp';target.write_text('fixture');old=identity(target)
    record={'path':str(target),'status':'deleted','identity':old}
    if case=='absent':target.unlink()
    elif case=='recreated':record['identity']=dict(old,inode=old['inode']+1)
    elif case=='legacy_present':record.pop('identity')
    elif case=='invalid_path':record['path']=r'\\server\share\cache.tmp'
    result=tool(tmp_path).verify(report(stage('Clean temporary files',{'result':{'deleted_count':1,'files':[record]}})))
    assert checks(result,'file_absence')[0]['status']==expected
    assert result['failed_items']==int(case=='same')
    if case=='recreated':assert checks(result)[0]['evidence']['recreated']==1
    if target.exists():assert target.read_text()=='fixture'


def test_file_bound_is_visible_and_missing_records_are_not_claimed_checked(tmp_path,monkeypatch):
    monkeypatch.setattr(verify,'_MAX_FILES',1)
    records=[dict(path=str(tmp_path/f'{index}.tmp'),status='deleted') for index in range(3)]
    result=tool(tmp_path).verify(report(stage('Clean temporary files',{'result':{'deleted_count':3,'files':records}})))
    assert result['counts']['confirmed']==1 and result['counts']['unverified']==1
    assert checks(result)[-1]['evidence']['count']==2 and result['failed_items']==0


def test_reparse_deleted_path_is_not_followed(tmp_path,monkeypatch):
    target=tmp_path/'cache.tmp';target.write_text('keep')
    original=Path.lstat
    def lstat(path,*args,**kwargs):
        value=original(path,*args,**kwargs)
        return SimpleNamespace(st_mode=value.st_mode,st_file_attributes=0x400) if path==target else value
    monkeypatch.setattr(Path,'lstat',lstat)
    result=tool(tmp_path).verify(report(stage('Clean temporary files',{'result':{'deleted_count':1,'files':[{'path':str(target),'status':'deleted'}]}})))
    assert result['counts']['unverified']==1 and result['failed_items']==0


def test_cleaner_preserves_original_identity_for_later_read_only_verification(tmp_path,monkeypatch):
    root=tmp_path/'cache';root.mkdir();target=root/'old.tmp';target.write_text('fixture')
    cleaner=Cleaner(tmp_path/'data',[Category('fixture','Fixture',root,'Fixture')])
    plan=cleaner.scan(['fixture'],0)
    original=plan['files'][0]
    monkeypatch.setattr(cleaner,'_delete_exact',lambda record:Path(record['path']).unlink())
    outcome=cleaner.apply(plan)
    assert outcome['files'][0]['identity']=={key:original[key] for key in ('device','inode','size','modified_ns','created_ns')}
    result=tool(tmp_path/'data').verify(report(stage('Clean temporary files',{'result':outcome})))
    assert result['status']=='confirmed' and result['failed_items']==0


@pytest.mark.parametrize('log_name,kept',[('a'*32+'.json',True),('invalid.json',False)])
def test_browser_cache_retains_only_opaque_private_evidence_reference(log_name,kept):
    result=_cache_summary({'result':{'deleted_count':1,'log_path':str(Path('D:/Private/cleanup')/log_name)}})
    assert ('cleanup_log_id' in result)==kept
    assert 'log_path' not in result and 'files' not in result


def test_browser_cache_uses_owned_journal_not_caller_path(tmp_path):
    folder=tmp_path/'cleanup';folder.mkdir();key='b'*32
    (folder/(key+'.json')).write_text(json.dumps({'state':'completed','plan_id':key,
        'result':{'deleted_count':1,'files':[{'path':str(tmp_path/'missing.cache'),'status':'deleted'}]}}))
    value={'items':[{'browser':'Google Chrome','status':'completed','profiles':[],
        'cache':{'deleted_count':1,'cleanup_log_id':key,'log_path':r'\\server\never-read.json'}}]}
    result=tool(tmp_path).verify(report(stage('Clean selected browser data',value)))
    assert result['status']=='confirmed'


def test_review_update_stages_do_not_repeat_catalog_or_history_queries(tmp_path):
    reader=Mock()
    value=report(stage('Review available driver updates',{'review_only':True,'result':{'resultCode':2}}),
        stage('Check or install Windows security updates',{'review_only':True,'scan':{'updates':[{'id':'1'*32}]},'items':[]}),
        stage('Review or update configured software',{'items':[{'status':'skipped','result':{'requiresReview':True}}]}))
    result=tool(tmp_path,reader).verify(value)
    reader.assert_not_called();assert result['counts']['not_applicable']==3


@pytest.mark.parametrize('current,state',[(0,'confirmed'),(1,'needs_review'),(False,'needs_review'),(None,'needs_review')])
def test_registry_readback_is_exact_and_typed(tmp_path,current,state):
    native=Mock(return_value={'privacy':{'complete':True,'items':[{'id':'advertising_id','complete':True,'exists':True,'type':'DWord','value':current}]}})
    result=tool(tmp_path,native).verify(report(stage('Apply saved privacy controls',{'items':[{'id':'advertising_id','status':'applied','desired':0}]})))
    assert checks(result)[0]['status']==state and result['failed_items']==int(state=='needs_review')
    assert native.call_count==1 and native.call_args.kwargs['timeout']<=18
    assert native.call_args.args[0]['privacy'][0]['name']=='Enabled'


def test_more_restrictive_diagnostic_preference_is_verified_without_requesting_a_write(tmp_path):
    native=Mock(return_value={'privacy':{'complete':True,'items':[{'id':'optional_diagnostics','complete':True,'exists':True,'type':'DWord','value':0}]}})
    result=tool(tmp_path,native).verify(report(stage('Apply saved privacy controls',{'items':[{'id':'optional_diagnostics','status':'already_configured','desired':1,'before':{'value':0}}]})))
    assert result['status']=='confirmed' and checks(result)[0]['evidence']['expected']==0


@pytest.mark.parametrize('change,state',[({},'confirmed'),({'create_time':222},'unverified'),
    ({'exe':r'C:\Elsewhere\app.exe'},'unverified'),({'priority':32},'needs_review')])
def test_priority_readback_checks_pid_birth_executable_and_actual_class(tmp_path,change,state):
    record={'pid':6000,'create_time':123.25,'exe':r'C:\Apps\codex.exe'}
    (tmp_path/'suspended-processes.json').write_text(json.dumps({'version':1,'entries':{},'allocation_history':[
        {'id':'c'*64,'kind':'priority','record':record,'selected':128,'status':'applied'}]}))
    reader=Mock(return_value=dict(record,priority=128,**change)) if 'priority' not in change else Mock(return_value=dict(record,**change))
    result=tool(tmp_path,process_reader=reader).verify(report(stage('Apply Codex performance profile',{'results':[{'pid':6000,'name':'codex.exe','applied':True,'change_id':'c'*64}]})))
    assert checks(result)[0]['status']==state and result['failed_items']==int(state=='needs_review')


def test_priority_of_an_exited_process_is_not_read_from_a_replacement(tmp_path):
    (tmp_path/'suspended-processes.json').write_text(json.dumps({'allocation_history':[
        {'id':'c'*64,'kind':'priority','record':{'pid':6000},'selected':128}]}))
    reader=Mock(side_effect=psutil.NoSuchProcess(6000))
    result=tool(tmp_path,process_reader=reader).verify(report(stage('Apply Codex performance profile',{'results':[{'applied':True,'change_id':'c'*64}]})))
    assert result['status']=='not_applicable'


@pytest.mark.parametrize('version,restart,state',[('2.0',False,'confirmed'),('2.1',False,'confirmed'),
    ('1.0',False,'needs_review'),('1.0',True,'unverified')])
def test_software_uses_local_registration_version_without_online_rescan(tmp_path,version,restart,state):
    native=Mock(return_value={'applications':{'complete':True,'items':[dict(name='Fixture app',identity='exact-key',publisher='Fixture',version=version)]}})
    initial={'InstalledApps':[dict(Name='Fixture app',Identity='exact-key',Publisher='Fixture',Version='1.0',Type='Desktop')]}
    update={'items':[dict(id='Fixture.App',name='Fixture app',from_version='1.0',to_version='2.0',status='completed',result={})],'restartNeeded':restart}
    result=tool(tmp_path,native).verify(report(stage('Inspect current resource usage',initial),stage('Review or update configured software',update)))
    assert checks(result,'installed_software')[0]['status']==state
    assert native.call_args.args[0]['applications'] is True and not native.call_args.args[0]['history']


@pytest.mark.parametrize('kind',['security','driver'])
@pytest.mark.parametrize('variant,state',[('success','confirmed'),('old','unverified'),('revision','unverified'),('future','unverified'),('failed','needs_review')])
def test_update_history_requires_exact_revision_success_and_this_stage_time(tmp_path,kind,variant,state):
    identity='11111111-2222-3333-4444-555555555555';binding={'id':identity,'revision':4}
    row=dict(id=identity,revision=4,operation=1,result_code=2,hresult=0,date=datetime.fromtimestamp(105,timezone.utc).isoformat())
    if variant=='old':row['date']=datetime.fromtimestamp(90,timezone.utc).isoformat()
    elif variant=='revision':row['revision']=5
    elif variant=='future':row['date']=datetime.fromtimestamp(1000,timezone.utc).isoformat()
    elif variant=='failed':row['result_code']=4
    native=Mock(return_value={'history':{'complete':True,'items':[row]}})
    title='Install approved driver updates' if kind=='driver' else 'Check or install Windows security updates'
    value={'items':[{'binding':binding,'status':'applied'}]} if kind=='driver' else {'items':[{'id':identity,'status':'completed'}],'scan':{'updates':[binding]}}
    result=tool(tmp_path,native).verify(report(stage(title,value)))
    assert checks(result)[0]['status']==state and result['failed_items']==int(state=='needs_review')
    assert native.call_args.args[0]['history']==[identity]


@pytest.mark.parametrize('end,state',[('1970-01-01T00:01:45Z','confirmed'),('1970-01-01T00:01:30Z','needs_review'),
    ('1970-01-01T01:01:45Z','unverified'),('', 'unverified')])
def test_defender_completion_is_fresh_relative_to_this_scan(tmp_path,end,state):
    native=Mock(return_value={'defender':{'complete':True,'quick_scan_end':end}})
    result=tool(tmp_path,native).verify(report(stage('Scan for spyware and malware',{'status':'completed'})))
    assert checks(result)[0]['status']==state


@pytest.mark.parametrize('present,state',[(False,'confirmed'),(True,'needs_review')])
def test_removal_is_checked_against_fresh_current_user_package_metadata(tmp_path,present,state):
    native=Mock(return_value={'appx':{'complete':True,'items':[{'name':'Fixture.Optional'}] if present else []}})
    result=tool(tmp_path,native).verify(report(stage('Remove selected consumer app group',{'results':[{'name':'Fixture.Optional','scope':'current-user','removed':True}]})))
    assert checks(result)[0]['status']==state and result['failed_items']==0


def browser_fixture(tmp_path,monkeypatch,visits):
    local=tmp_path/'local';profile=local/'Google/Chrome/User Data/Default';profile.mkdir(parents=True)
    monkeypatch.setenv('LOCALAPPDATA',str(local));monkeypatch.setenv('APPDATA',str(tmp_path/'roaming'))
    monkeypatch.setattr(verify.psutil,'process_iter',lambda *args:[])
    database=profile/'History'
    with sqlite3.connect(database) as connection:
        connection.executescript('CREATE TABLE urls(id INTEGER PRIMARY KEY,url TEXT);CREATE TABLE visits(id INTEGER PRIMARY KEY,url INTEGER);')
        if visits:connection.execute('INSERT INTO visits VALUES(1,1)')
    engine=BrowserPrivacy(tmp_path);item=engine.profiles()[0];backup='d'*32
    manifest={'state':'applied','backupId':backup,'profileId':item['id'],'profilePath':str(profile),
        'databases':[{'relative':'History','committed':True,'after':engine._files(database)}]}
    folder=tmp_path/'BrowserPrivacyBackups'/backup;folder.mkdir(parents=True)
    (folder/'manifest.json').write_text(json.dumps(manifest))
    source=report(stage('Clean selected browser data',{'items':[{'browser':'Google Chrome','status':'completed',
        'profiles':[{'profile_id':item['id'],'backup_id':backup,'categories':['history'],'status':'applied'}]}]}))
    return source,database


@pytest.mark.parametrize('remaining,state',[(0,'confirmed'),(1,'needs_review')])
def test_browser_privacy_uses_read_only_sqlite_counts_and_creates_no_sidecars(tmp_path,monkeypatch,remaining,state):
    source,database=browser_fixture(tmp_path,monkeypatch,remaining)
    before=hashlib.sha256(database.read_bytes()).hexdigest();files=set(database.parent.iterdir())
    result=tool(tmp_path).verify(source)
    assert checks(result,'browser_records')[0]['status']==state
    assert result['failed_items']==int(state=='needs_review')
    assert hashlib.sha256(database.read_bytes()).hexdigest()==before and set(database.parent.iterdir())==files


def test_later_browser_activity_is_unverified_not_a_failed_cleanup(tmp_path,monkeypatch):
    source,database=browser_fixture(tmp_path,monkeypatch,0)
    with sqlite3.connect(database) as connection:connection.execute('INSERT INTO visits VALUES(1,1)')
    result=tool(tmp_path).verify(source)
    assert result['status']=='unverified' and result['failed_items']==0


def test_cancellation_and_deadline_preserve_explicit_stage_coverage(tmp_path):
    cancelled=threading.Event();cancelled.set();reader=Mock()
    source=report(stage('one',{}),stage('two',{}))
    result=tool(tmp_path,reader).verify(source,cancel_event=cancelled)
    assert result['cancelled'] and len(result['stages'])==2 and result['counts']['unverified']==2
    reader.assert_not_called()
    tick=iter(range(1000));result=tool(tmp_path,clock=lambda:next(tick)).verify(source,timeout=.1)
    assert result['deadline_reached'] and result['counts']['unverified']==2


def test_native_reader_failure_is_a_verification_failure_and_never_an_install_retry(tmp_path):
    reader=Mock(return_value={'error':'Fixture local reader timeout'})
    result=tool(tmp_path,reader).verify(report(stage('Scan for spyware and malware',{'status':'completed'})))
    assert result['failed_items']==1 and result['status']=='unverified' and reader.call_count==1
    assert result['errors']==['Fixture local reader timeout']


def test_prior_verification_is_informational_and_does_not_recurse(tmp_path):
    native=Mock()
    result=tool(tmp_path,native).verify(report(stage('Verify optimization outcomes',{'read_only':True,'status':'unverified'})))
    assert result['status']=='not_applicable' and result['counts']['not_applicable']==1
    native.assert_not_called()


def test_local_reader_budget_expiry_is_visible_without_claiming_an_operation_failed(tmp_path):
    native=Mock(return_value={'deadline_reached':True,'error':'Fixture native read budget exhausted'})
    result=tool(tmp_path,native).verify(report(stage('Scan for spyware and malware',{'status':'completed'})))
    assert result['status']=='unverified' and result['failed_items']==0 and result['errors']


def test_backend_hook_exposes_same_read_only_contract_without_initializing_services(tmp_path):
    from optimus.backend import Backend
    backend=object.__new__(Backend);backend.data_dir=tmp_path;backend.sampler=SimpleNamespace(status=lambda:{'enabled':True})
    source=report(stage('Enable resource analysis',{'enabled':True}));backend.workflow=SimpleNamespace(last_report=source)
    result=backend.verify_optimization(source)
    assert result['status']=='confirmed' and result['read_only']


def test_loaded_report_does_not_confuse_a_new_backend_sampler_with_the_original_session(tmp_path):
    from optimus.backend import Backend
    backend=object.__new__(Backend);backend.data_dir=tmp_path;backend.sampler=SimpleNamespace(status=Mock(return_value={'enabled':False}))
    backend.workflow=SimpleNamespace(last_report=None)
    result=backend.verify_optimization(report(stage('Enable resource analysis',{'enabled':True})))
    assert result['status']=='unverified' and result['failed_items']==0
    backend.sampler.status.assert_not_called()


@pytest.mark.parametrize('exists,state',[(False,'confirmed'),(True,'needs_review'),(None,'unverified')])
def test_startup_readback_distinguishes_absence_reappearance_and_unknown_state(tmp_path,exists,state):
    key='Registry-'+'a'*64
    native=Mock(return_value={'startup':{'complete':True,'items':[{'id':key,'complete':True,'exists':exists}]}})
    source=report(stage('Disable approved startup entries',{'items':[{'binding':{'id':key},'status':'applied'}]}),
        startup_approved_entries=[{'Id':key,'EntryType':'Registry','Hive':'CurrentUser','RegistryView':'Registry64','ValueName':'Fixture'}])
    result=tool(tmp_path,native).verify(source)
    assert checks(result)[0]['status']==state and result['failed_items']==0


@pytest.mark.skipif(os.name!='nt',reason='Windows PowerShell syntax parser only')
def test_native_reader_parses_and_contains_only_local_read_operations(tmp_path):
    script=tmp_path/'reader.ps1';script.write_text(verify._NATIVE.replace('__PAYLOAD__','e30='))
    executable=Path(os.environ.get('SystemRoot',r'C:\Windows'))/'System32/WindowsPowerShell/v1.0/powershell.exe'
    code="param($File) $t=$null;$e=$null;[void][Management.Automation.Language.Parser]::ParseFile($File,[ref]$t,[ref]$e);if($e.Count){$e|Out-String|Write-Output;exit 1}"
    result=subprocess.run([str(executable),'-NoProfile','-NonInteractive','-Command', '& { '+code+' }',str(script)],capture_output=True,text=True,timeout=20)
    assert result.returncode==0,result.stdout+result.stderr
    for forbidden in ('.Search(','.Install(','.Download(','Start-MpScan','Stop-MpScan','Set-MpPreference','Remove-AppxPackage','SetValue','DeleteValue','winget'):
        assert forbidden not in verify._NATIVE
