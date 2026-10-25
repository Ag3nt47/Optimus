"""Owned sign-in registration uses fixtures; never creates a scheduled task."""
import copy
import json
import os
from pathlib import Path
import subprocess
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from optimus import startup_registration as startup
from optimus.backend import Backend
from optimus.workflow import Preferences

SID = 'S-1-5-21-100-200-300-1001'


@pytest.mark.skipif(os.name!='nt',reason='Native scheduler read-only absence regression')
def test_native_missing_task_wrappers_are_absent_and_other_errors_still_propagate(tmp_path):
    helper=Path(__file__).parents[1]/'src/optimus/engine/OptimusStartup.ps1'
    harness=tmp_path/'task-absence.ps1'
    harness.write_text(r'''
param([string]$Helper)
$ErrorActionPreference='Stop';Set-StrictMode -Version 2
$tokens=$null;$errors=$null
$ast=[Management.Automation.Language.Parser]::ParseFile($Helper,[ref]$tokens,[ref]$errors)
if($errors.Count){throw 'The native helper has parse errors.'}
$definition=$ast.Find({param($node) $node -is [Management.Automation.Language.FunctionDefinitionAst] -and $node.Name -ceq 'Read-Task'},$true)
if(-not $definition){throw 'Read-Task is missing.'}
. ([scriptblock]::Create($definition.Extent.Text))
$script:Injected=$null;$script:Checks=0
$folder=New-Object PSObject
$folder|Add-Member ScriptMethod GetTask {param($Name) if($script:Injected){throw $script:Injected};return 'fixture-found'}
foreach($exception in @([IO.FileNotFoundException]::new('fixture missing'),
 [Reflection.TargetInvocationException]::new('wrapped',[IO.FileNotFoundException]::new('nested missing')),
 [Runtime.InteropServices.COMException]::new('COM missing',-2147024894))){
 $script:Injected=$exception
 if($null -ne (Read-Task $folder 'fixture')){throw 'A missing task was not interpreted as absent.'};$script:Checks++
}
foreach($exception in @([UnauthorizedAccessException]::new('fixture access denied'),
 [Reflection.TargetInvocationException]::new('wrapped',[Runtime.InteropServices.COMException]::new('denied',-2147024891)),
 [Runtime.InteropServices.COMException]::new('Different HRESULT facility',-2147287038))){
 $script:Injected=$exception;$blocked=$false
 try{Read-Task $folder 'fixture'|Out-Null}catch{$blocked=$true}
 if(-not $blocked){throw 'A non-Win32-missing error was hidden as absence.'};$script:Checks++
}
$script:Injected=$null
if((Read-Task $folder 'fixture') -cne 'fixture-found'){throw 'An existing task was lost.'};$script:Checks++
$scheduler=New-Object -ComObject 'Schedule.Service';$scheduler.Connect();$native=$scheduler.GetFolder('\')
$unique='Optimus-ReadOnly-Missing-'+[guid]::NewGuid().ToString('N')
if($null -ne (Read-Task $native $unique)){throw 'The unique absent native task was not reported as absent.'};$script:Checks++
@{result='PASS';checks=$script:Checks;registrations=0;deletions=0}|ConvertTo-Json -Compress
''',encoding='utf-8')
    executable=Path(os.environ.get('SystemRoot',r'C:\Windows'))/'System32/WindowsPowerShell/v1.0/powershell.exe'
    environment={key:value for key,value in os.environ.items() if key.casefold()!='psmodulepath'}
    environment['PSModulePath']=str(executable.parent/'Modules')
    result=subprocess.run([str(executable),'-NoLogo','-NoProfile','-NonInteractive','-File',str(harness),'-Helper',str(helper)],
        env=environment,capture_output=True,text=True,timeout=20)
    assert result.returncode==0,result.stdout+result.stderr
    assert json.loads(result.stdout)=={'result':'PASS','checks':8,'registrations':0,'deletions':0}


@pytest.mark.skipif(os.name!='nt',reason='Native unregistered scheduler ownership regression')
def test_native_normalized_task_accounts_resolve_to_current_sid_without_registration(tmp_path):
    helper=Path(__file__).parents[1]/'src/optimus/engine/OptimusStartup.ps1'
    harness=tmp_path/'task-user-normalization.ps1'
    harness.write_text(r'''
param([string]$Helper)
$ErrorActionPreference='Stop';Set-StrictMode -Version 2
$tokens=$null;$errors=$null
$ast=[Management.Automation.Language.Parser]::ParseFile($Helper,[ref]$tokens,[ref]$errors)
if($errors.Count){throw 'The native helper has parse errors.'}
foreach($name in @('Test-TaskUser','Test-Owned')){
 $definition=$ast.Find({param($node) $node -is [Management.Automation.Language.FunctionDefinitionAst] -and $node.Name -ceq $name},$true)
 if(-not $definition){throw ('Missing native function: '+$name)}
 . ([scriptblock]::Create($definition.Extent.Text))
}
$identity=[Security.Principal.WindowsIdentity]::GetCurrent();$sid=$identity.User.Value
$short=$identity.Name.Split('\')[-1];$users=@($sid,$identity.Name)
try{if((([Security.Principal.NTAccount]::new($short)).Translate([Security.Principal.SecurityIdentifier])).Value -ceq $sid){$users+=$short}}catch{}
$users=@($users|Select-Object -Unique)
$scheduler=New-Object -ComObject 'Schedule.Service';$scheduler.Connect()
$definition=$scheduler.NewTask(0)
$launch=@{executable='D:\Optimus ownership fixture\Optimus.exe';argument_line='';working_directory='D:\Optimus ownership fixture'}
$manifest=@{owner_token=('a'*32);launch=$launch;previous_launch=$null}
$definition.RegistrationInfo.Description='Optimus sign-in launcher v1 '+$manifest.owner_token
$definition.RegistrationInfo.Source='Optimus/SignIn/1'
$definition.Principal.UserId=$sid;$definition.Principal.LogonType=3;$definition.Principal.RunLevel=0
$nativePrincipal=$definition.Principal.UserId
$trigger=$definition.Triggers.Create(9);$trigger.Delay='PT1M';$trigger.Enabled=$true
$action=$definition.Actions.Create(0);$action.Path=$launch.executable;$action.Arguments='';$action.WorkingDirectory=$launch.working_directory
$checks=0
foreach($principal in $users){foreach($triggerUser in $users){
 $definition.Principal.UserId=$principal;$trigger.UserId=$triggerUser
 if($null -eq (Test-Owned $definition $manifest $sid)){throw 'A verified current-user account representation was rejected.'}
 $checks++
}}
$script:fixtureAction=[pscustomobject]@{Type=0;Path=$launch.executable;Arguments=$null;WorkingDirectory=$launch.working_directory}
$fixtureActions=[pscustomobject]@{Count=1}
$fixtureActions|Add-Member ScriptMethod Item {param($Index) return $script:fixtureAction}
$fixtureDefinition=[pscustomobject]@{RegistrationInfo=$definition.RegistrationInfo;Principal=$definition.Principal;Triggers=$definition.Triggers;Actions=$fixtureActions}
if($null -eq (Test-Owned $fixtureDefinition $manifest $sid)){throw 'Registered COM null arguments did not match the canonical empty line.'};$checks++
$script:fixtureAction.Arguments='unexpected'
if($null -ne (Test-Owned $fixtureDefinition $manifest $sid)){throw 'Unexpected nonempty arguments were accepted.'};$checks++
$launch.argument_line='-B "D:\Optimus ownership fixture\launch.py"'
$script:fixtureAction.Arguments=$null
if($null -ne (Test-Owned $fixtureDefinition $manifest $sid)){throw 'Null arguments matched a nonempty source argument line.'};$checks++
$script:fixtureAction.Arguments=$launch.argument_line
if($null -eq (Test-Owned $fixtureDefinition $manifest $sid)){throw 'Exact nonempty arguments were rejected.'};$checks++
$script:fixtureAction.Arguments=$launch.argument_line+' '
if($null -ne (Test-Owned $fixtureDefinition $manifest $sid)){throw 'Changed nonempty argument whitespace was accepted.'};$checks++
$launch.argument_line=''
$definition.Principal.UserId='NT AUTHORITY\SYSTEM';$trigger.UserId=$sid
if($null -ne (Test-Owned $definition $manifest $sid)){throw 'A foreign principal was accepted.'};$checks++
$definition.Principal.UserId=$sid;$trigger.UserId='NT AUTHORITY\SYSTEM'
if($null -ne (Test-Owned $definition $manifest $sid)){throw 'A foreign trigger user was accepted.'};$checks++
if(Test-TaskUser ('OptimusNoSuchAccount-'+[guid]::NewGuid().ToString('N')) $sid){throw 'An unresolved username was accepted.'};$checks++
if(Test-TaskUser $short 'S-1-5-18'){throw 'Short username text was accepted without matching the owner SID.'};$checks++
$definition.Principal.UserId=$sid;$trigger.UserId=$sid
$definition.Principal.RunLevel=1
if($null -ne (Test-Owned $definition $manifest $sid)){throw 'A highest-privilege task was accepted as the least-privilege launcher.'};$checks++
$definition.Principal.RunLevel=0;$definition.Principal.LogonType=1
if($null -ne (Test-Owned $definition $manifest $sid)){throw 'A password-backed task was accepted as an interactive launcher.'};$checks++
$definition.Principal.LogonType=3;$trigger.Delay='PT2M'
if($null -ne (Test-Owned $definition $manifest $sid)){throw 'An altered startup delay was accepted.'};$checks++
$trigger.Delay='PT1M'
$extra=$definition.Actions.Create(0);$extra.Path='C:\Windows\System32\cmd.exe'
if($null -ne (Test-Owned $definition $manifest $sid)){throw 'An extra task action was accepted.'};$checks++
$definition.Actions.Remove(2)
$definition.RegistrationInfo.Source='Unrecognized owner'
if($null -ne (Test-Owned $definition $manifest $sid)){throw 'An unrecognized task owner was accepted.'};$checks++
$definition.RegistrationInfo.Source='Optimus/SignIn/1'
if($null -eq (Test-Owned $definition $manifest $sid)){throw 'Restoring the original least-privilege definition did not restore ownership.'};$checks++
@{result='PASS';checks=$checks;principal_readback=$nativePrincipal;registrations=0;deletions=0}|ConvertTo-Json -Compress
''',encoding='utf-8')
    executable=Path(os.environ.get('SystemRoot',r'C:\Windows'))/'System32/WindowsPowerShell/v1.0/powershell.exe'
    environment={key:value for key,value in os.environ.items() if key.casefold()!='psmodulepath'}
    environment['PSModulePath']=str(executable.parent/'Modules')
    result=subprocess.run([str(executable),'-NoLogo','-NoProfile','-NonInteractive','-File',str(harness),'-Helper',str(helper)],
        env=environment,capture_output=True,text=True,timeout=25)
    assert result.returncode==0,result.stdout+result.stderr
    proof=json.loads(result.stdout)
    assert proof['result']=='PASS' and proof['checks']>=8 and proof['principal_readback']
    assert proof['registrations']==proof['deletions']==0


class SchedulerFixture:
    def __init__(self):
        self.task = None
        self.calls = []

    def __call__(self, request):
        self.calls.append(copy.deepcopy(request))
        manifest = request['manifest']
        owned = self.task is not None and manifest is not None and self.task['token'] == manifest['owner_token'] and self.task['launch'] in (manifest['launch'], manifest['previous_launch'])
        foreign = self.task is not None and not owned
        before = json.dumps(self.task) if self.task else None
        if request['operation'] == 'restore':
            if foreign:
                raise startup.StartupRegistrationError('Changed task preserved.')
            self.task = json.loads(request['before_xml']) if request['before_xml'] else None
        elif request['operation'] == 'set':
            if foreign:
                raise startup.StartupRegistrationError('Unrecognized task preserved.')
            self.task = {'token': manifest['owner_token'], 'launch': manifest['launch'], 'enabled': True} if request['enabled'] else None
        owned = self.task is not None and manifest is not None and self.task['token'] == manifest['owner_token'] and self.task['launch'] in (manifest['launch'], manifest['previous_launch'])
        foreign = self.task is not None and not owned
        return {'ok': True, 'owner_sid': SID, 'before_xml': before,
                'matched_launch': copy.deepcopy(self.task['launch']) if owned else None,
                'status': {'enabled': None if foreign else self.task['enabled'] if self.task else False,
                           'owned': owned, 'available': True,
                           'state': 'foreign' if foreign else 'enabled' if self.task and self.task['enabled'] else 'disabled' if self.task else 'absent',
                           'reason': 'Fixture foreign task' if foreign else 'Fixture status',
                           'task_name': 'Optimus-SignIn-'+SID,
                           'target': self.task['launch']['executable'] if owned else None}}


def fixture_service(tmp_path):
    runner = SchedulerFixture()
    launch = {'executable': r'D:\Optimus space\Optimus.exe', 'arguments': [],
              'argument_line': '', 'working_directory': r'D:\Optimus space'}
    return startup.StartupRegistration(tmp_path, runner=runner, launch_provider=lambda: copy.deepcopy(launch), headroom_checker=lambda: None), runner, launch


def test_space_changed_after_ownership_read_prevents_manifest_and_registration(tmp_path):
    from optimus.startup_safety import StartupPreflightError
    service, runner, _ = fixture_service(tmp_path)
    service.headroom_checker = Mock(side_effect=[None, StartupPreflightError('Fixture free-space changed.')])
    with pytest.raises(StartupPreflightError):
        service.set_enabled(True)
    assert not service.path.exists() and runner.task is None
    assert [request['operation'] for request in runner.calls] == ['status']


def test_rollback_space_changed_after_native_return_preserves_manifest_with_unknown_total_outcome(tmp_path):
    from optimus.startup_safety import StartupPreflightError
    service, runner, launch = fixture_service(tmp_path)
    service.set_enabled(True)
    launch['executable'] = r'D:\Optimus space\Updated.exe'
    receipt = service.set_enabled(True)
    before = service.path.read_bytes()
    runner.calls.clear()
    service.headroom_checker = Mock(side_effect=[None, StartupPreflightError('Fixture free-space changed.')])
    with pytest.raises(startup.StartupRegistrationError, match='native rollback returned') as caught:
        service.rollback(receipt)
    assert not isinstance(caught.value, StartupPreflightError)
    assert service.path.read_bytes() == before
    assert [request['operation'] for request in runner.calls] == ['status', 'restore']


def fixture_backend(tmp_path, service):
    backend = object.__new__(Backend)
    backend.data_dir = tmp_path
    backend.preference_store = Preferences(tmp_path)
    backend._startup_registration = service
    backend.preference_store.save({'launch_at_sign_in': False})
    return backend


def test_status_never_mutates_or_creates_state(tmp_path):
    service, runner, _ = fixture_service(tmp_path)
    assert service.status()['state'] == 'absent'
    assert not service.path.exists()
    assert [item['operation'] for item in runner.calls] == ['status']


def test_explicit_enable_stores_bounded_owned_launcher_then_disable(tmp_path):
    service, runner, launch = fixture_service(tmp_path)
    receipt = service.set_enabled(True)
    manifest = json.loads(service.path.read_text())
    assert manifest['launch'] == launch and manifest['owner_sid'] == SID
    assert service.path.stat().st_size < 16384
    assert receipt['status']['enabled'] is True and service.status()['owned'] is True
    assert service.set_enabled(False)['status']['state'] == 'absent'
    assert runner.task is None


def test_enable_updates_only_owned_registration_and_keeps_previous_launcher(tmp_path):
    service, runner, launch = fixture_service(tmp_path)
    service.set_enabled(True)
    old_token = runner.task['token']
    launch['executable'] = r'D:\New Optimus\Optimus.exe'
    launch['working_directory'] = r'D:\New Optimus'
    receipt = service.set_enabled(True)
    assert runner.task['token'] == old_token
    assert runner.task['launch'] == launch
    assert json.loads(service.path.read_text())['previous_launch']['executable'] == r'D:\Optimus space\Optimus.exe'
    service.rollback(receipt)
    assert runner.task['launch']['executable'] == r'D:\Optimus space\Optimus.exe'
    assert service.status()['owned'] is True


@pytest.mark.parametrize('enabled', [True, False])
def test_foreign_task_never_overwritten_removed_or_claimed(tmp_path, enabled):
    service, runner, launch = fixture_service(tmp_path)
    runner.task = {'token': 'foreign', 'launch': launch, 'enabled': True}
    assert service.status()['enabled'] is None and service.status()['state'] == 'foreign'
    before = copy.deepcopy(runner.task)
    with pytest.raises(startup.StartupRegistrationError, match='foreign'):
        service.set_enabled(enabled)
    assert runner.task == before and not service.path.exists()


def test_manual_action_change_blocks_later_disable(tmp_path):
    service, runner, _ = fixture_service(tmp_path)
    service.set_enabled(True)
    runner.task['launch'] = dict(runner.task['launch'], argument_line='--unexpected')
    before = copy.deepcopy(runner.task)
    with pytest.raises(startup.StartupRegistrationError):
        service.set_enabled(False)
    assert runner.task == before


def test_backend_ordinary_save_preserves_manual_disable_and_other_preferences(tmp_path):
    service, runner, _ = fixture_service(tmp_path)
    backend = fixture_backend(tmp_path, service)
    backend.set_startup_launch(True)
    runner.task['enabled'] = False
    runner.calls.clear()
    settings = backend.preferences()
    settings['analysis_interval'] = 9
    backend.save_preferences(settings)
    assert runner.calls == [] and runner.task['enabled'] is False
    assert backend.preferences()['analysis_interval'] == 9
    assert backend.preferences()['launch_at_sign_in'] is True


def test_explicit_backend_on_repairs_own_manually_disabled_task(tmp_path):
    service, runner, _ = fixture_service(tmp_path)
    backend = fixture_backend(tmp_path, service)
    backend.set_startup_launch(True)
    runner.task['enabled'] = False
    assert backend.set_startup_launch(True)['enabled'] is True
    assert runner.task['enabled'] is True


def test_backend_registration_error_preserves_all_saved_preferences(tmp_path):
    service, runner, launch = fixture_service(tmp_path)
    backend = fixture_backend(tmp_path, service)
    before = backend.preference_store.path.read_bytes()
    runner.task = {'token': 'foreign', 'launch': launch, 'enabled': False}
    with pytest.raises(startup.StartupRegistrationError):
        backend.save_preferences(dict(backend.preferences(), launch_at_sign_in=True, analysis_interval=11))
    assert backend.preference_store.path.read_bytes() == before


def test_failed_preferences_write_rolls_back_new_task_and_manifest(tmp_path, monkeypatch):
    service, runner, _ = fixture_service(tmp_path)
    backend = fixture_backend(tmp_path, service)
    before = backend.preference_store.path.read_bytes()
    monkeypatch.setattr(backend.preference_store, 'save', Mock(side_effect=OSError('fixture full disk')))
    with pytest.raises(OSError, match='full disk'):
        backend.save_preferences(dict(backend.preferences(), launch_at_sign_in=True))
    assert runner.task is None and not service.path.exists()
    assert backend.preference_store.path.read_bytes() == before


def test_failed_preferences_write_restores_exact_manual_task_enabled_state(tmp_path, monkeypatch):
    service, runner, _ = fixture_service(tmp_path)
    backend = fixture_backend(tmp_path, service)
    backend.set_startup_launch(True)
    runner.task['enabled'] = False
    before = copy.deepcopy(runner.task)
    monkeypatch.setattr(backend.preference_store, 'save', Mock(side_effect=OSError('fixture write error')))
    with pytest.raises(OSError):
        backend.save_preferences(dict(backend.preferences(), launch_at_sign_in=False))
    assert runner.task == before


def test_failed_rollback_is_reported_as_unverified(tmp_path, monkeypatch):
    service, _, _ = fixture_service(tmp_path)
    backend = fixture_backend(tmp_path, service)
    monkeypatch.setattr(backend.preference_store, 'save', Mock(side_effect=OSError('write error')))
    monkeypatch.setattr(service, 'rollback', Mock(side_effect=OSError('task changed')))
    with pytest.raises(RuntimeError, match='rollback could not be verified'):
        backend.save_preferences(dict(backend.preferences(), launch_at_sign_in=True))


def test_rollback_preserves_a_later_manual_task_change(tmp_path):
    service, runner, _ = fixture_service(tmp_path)
    receipt=service.set_enabled(True)
    runner.task['enabled']=False
    with pytest.raises(startup.StartupRegistrationError,match='changed before rollback'):
        service.rollback(receipt)
    assert runner.task['enabled'] is False


@pytest.mark.skipif(os.name!='nt',reason='Windows byte-range lease fixture')
def test_another_window_registration_lease_prevents_replay(tmp_path):
    import msvcrt
    service, runner, _ = fixture_service(tmp_path)
    lock=tmp_path/'startup-launch.lock';lock.write_bytes(b'0')
    with lock.open('r+b') as stream:
        msvcrt.locking(stream.fileno(),msvcrt.LK_NBLCK,1)
        try:
            with pytest.raises(startup.StartupRegistrationError,match='Another Optimus window'):
                service.set_enabled(True)
        finally:
            stream.seek(0);msvcrt.locking(stream.fileno(),msvcrt.LK_UNLCK,1)
    assert runner.calls==[] and not service.path.exists()


@pytest.mark.parametrize('value', [1, 0, 'yes', None, [], {}])
def test_explicit_setting_is_strict_bool_without_windows_calls(tmp_path, value):
    service, runner, _ = fixture_service(tmp_path)
    with pytest.raises(ValueError):
        service.set_enabled(value)
    assert runner.calls == []


@pytest.mark.parametrize('raw', ['{}', 'null', '[]', '{"schema":true}', 'x'*16385])
def test_bad_manifest_fails_closed_without_windows_calls(tmp_path, raw):
    service, runner, _ = fixture_service(tmp_path)
    service.path.write_text(raw)
    status = service.status()
    assert status['enabled'] is None and status['available'] is False
    assert runner.calls == []


def test_timeout_keeps_ownership_of_prior_and_new_launcher(tmp_path):
    service, runner, launch = fixture_service(tmp_path)
    service.set_enabled(True)
    launch['executable'] = r'D:\Updated\Optimus.exe'
    launch['working_directory'] = r'D:\Updated'
    original = service.runner
    def delayed(request):
        if request['operation'] == 'set':
            raise startup.StartupRegistrationError('outcome is unknown')
        return original(request)
    service.runner = delayed
    with pytest.raises(startup.StartupRegistrationError, match='unknown'):
        service.set_enabled(True)
    assert service.status()['owned'] is True
    assert json.loads(service.path.read_text())['launch']['executable'] == launch['executable']


def test_frozen_launch_uses_exact_installed_file_and_no_session_flags(tmp_path, monkeypatch):
    executable = tmp_path / 'Optimus.exe'; executable.write_bytes(b'fixture')
    monkeypatch.setattr(startup.sys, 'frozen', True, raising=False)
    monkeypatch.setattr(startup.sys, 'executable', str(executable))
    monkeypatch.setattr(startup.sys, 'argv', ['other', '--source-workspace', 'untrusted', '--admin-relaunch'])
    launch = startup.launch_specification()
    assert launch['executable'] == str(executable) and launch['arguments'] == []
    assert launch['argument_line'] == '' and launch['working_directory'] == str(tmp_path)


def test_source_launch_preserves_only_original_launcher_and_quotes_spaces(tmp_path, monkeypatch):
    directory = tmp_path / 'original source'; directory.mkdir()
    (directory / 'launch.py').write_text('# fixture')
    executable = tmp_path / 'python.exe'; executable.write_bytes(b'fixture')
    monkeypatch.setattr(startup.sys, 'frozen', False, raising=False)
    monkeypatch.setattr(startup.sys, 'executable', str(executable))
    monkeypatch.setenv('OPTIMUS_ORIGINAL_SOURCE', str(directory))
    launch = startup.launch_specification()
    assert launch['arguments'] == ['-B', str(directory/'launch.py')]
    assert launch['argument_line'] == '-B "'+str(directory/'launch.py')+'"'


def test_hardlinked_launcher_rejected(tmp_path, monkeypatch):
    original = tmp_path/'old.exe'; original.write_bytes(b'fixture')
    executable = tmp_path/'Optimus.exe'; os.link(original, executable)
    monkeypatch.setattr(startup.sys, 'frozen', True, raising=False)
    monkeypatch.setattr(startup.sys, 'executable', str(executable))
    with pytest.raises(ValueError, match='hardlinks'):
        startup.launch_specification()


def test_native_request_bounded_timeout_and_no_shell_or_uac(tmp_path, monkeypatch):
    service, _, _ = fixture_service(tmp_path)
    run = Mock(return_value=SimpleNamespace(returncode=0, stdout='{"ok":true}'))
    monkeypatch.setattr(startup.subprocess, 'run', run)
    assert service._native({'operation':'status','manifest':None})['ok'] is True
    assert run.call_args.kwargs['timeout'] == 10
    assert 'shell' not in run.call_args.kwargs
    assert '-NonInteractive' in run.call_args.args[0]
    assert not any('runas' in str(item).lower() for item in run.call_args.args[0])


def test_native_clear_failure_is_preserved(tmp_path, monkeypatch):
    service, _, _ = fixture_service(tmp_path)
    monkeypatch.setattr(startup.subprocess, 'run', Mock(return_value=SimpleNamespace(returncode=1,stdout='{"ok":false,"native_error_chain":[{"type":"System.UnauthorizedAccessException","hresult":"0x80070005"}]}')))
    with pytest.raises(startup.StartupRegistrationError, match='0x80070005'):
        service._native({'operation':'status','manifest':None})


@pytest.fixture(scope='module')
def native_validator():
    if os.name != 'nt':
        pytest.skip('Native validation requires Windows; never registers tasks.')
    host = Path(os.environ.get('SystemRoot', r'C:\Windows'))/'System32/WindowsPowerShell/v1.0/powershell.exe'
    helper = Path(startup.__file__).with_name('engine')/'OptimusStartup.ps1'
    def validate(manifest):
        response = subprocess.run([str(host),'-NoLogo','-NoProfile','-NonInteractive','-ExecutionPolicy','RemoteSigned',
            '-File',str(helper),'-ValidateOnly'], input=json.dumps({'operation':'status','manifest':manifest}),
            capture_output=True,text=True,encoding='utf-8',errors='replace',timeout=10,
            creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0),check=False)
        return response, json.loads(response.stdout.lstrip('\ufeff'))
    _, identity = validate(None)
    return validate, identity['owner_sid']


def native_manifest(tmp_path, sid, source=False):
    directory=tmp_path/'original source with spaces'; directory.mkdir(exist_ok=True)
    executable=directory/('python.exe' if source else 'Optimus.exe'); executable.write_bytes(b'owned fixture')
    arguments=['-B',str(directory/'launch.py')] if source else []
    if source:
        (directory/'launch.py').write_text('# owned fixture')
    return {'schema':1,'owner_token':'a'*32,'owner_sid':sid,
            'launch':{'executable':str(executable),'arguments':arguments,
                      'argument_line':subprocess.list2cmdline(arguments),'working_directory':str(directory)},
            'previous_launch':None}


@pytest.mark.parametrize('source',[False,True])
def test_native_manifest_and_windows_quoting_validation_never_calls_scheduler(tmp_path,native_validator,source):
    validate,sid=native_validator
    response,value=validate(native_manifest(tmp_path,sid,source))
    assert response.returncode==0 and value['ok'] is True and value['validated'] is True


@pytest.mark.parametrize('change',['sid','token','quoting','argument','target','working_directory'])
def test_native_validation_fails_closed_before_scheduler(tmp_path,native_validator,change):
    validate,sid=native_validator
    manifest=native_manifest(tmp_path,sid,True)
    if change=='sid':manifest['owner_sid']=SID
    elif change=='token':manifest['owner_token']='foreign'
    elif change=='quoting':manifest['launch']['argument_line']='-B unquoted wrong source'
    elif change=='argument':manifest['launch']['arguments'][0]='-Command'
    elif change=='target':manifest['launch']['executable']=str(tmp_path/'cmd.exe')
    elif change=='working_directory':manifest['launch']['working_directory']=str(tmp_path)
    response,value=validate(manifest)
    assert response.returncode!=0 and value['ok'] is False
