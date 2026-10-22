"""Media-upgrade script fixtures never read host policy or launch an installer."""
import json
import os
from pathlib import Path
import re
import shutil
import subprocess

import pytest


SCRIPT = Path(__file__).resolve().parents[1] / 'scripts' / 'Invoke-WindowsMediaUpgrade.ps1'


def powershell():
    executable = shutil.which('powershell') or shutil.which('pwsh')
    if executable is None:
        pytest.skip('PowerShell is unavailable.')
    return executable


def test_script_contains_only_media_setup_path_and_readonly_policy_operations():
    source = SCRIPT.read_text(encoding='utf-8')
    assert "[string]$Mode='Scan'" in source
    assert "$MyInvocation.InvocationName -ne '.'" in source
    assert 'Get-AuthenticodeSignature' in source
    assert 'Get-WindowsImage' in source
    assert "'/DynamicUpdate','Enable'" in source
    assert "'/Quiet','/Compat','ScanOnly','/Compat','IgnoreWarning'" in source
    assert 'NoReboot suppresses only the first' in source
    assert not re.search(r'\b(?:Set|New|Remove)-ItemProperty\b|LabConfig|appraiserres|/Product\s+Server|/IgnoreCheck', source, re.I)
    assert not re.search(r'\b(?:Invoke-WebRequest|Invoke-RestMethod|Stop-Process|Restart-Computer|SetValue|DeleteValue)\b', source)


HARNESS = r'''
param([string]$Helper,[string]$Media,[string]$OutputRoot)
$ErrorActionPreference='Stop'
$tokens=$null;$errors=$null
$ast=[Management.Automation.Language.Parser]::ParseFile($Helper,[ref]$tokens,[ref]$errors)
if($errors.Count){throw ($errors|Out-String)}
# A dot-source must only define functions. These trap any accidental host work.
function Get-CimInstance {throw 'Unexpected host CIM call.'}
function Get-AuthenticodeSignature {throw 'Unexpected host signature call.'}
function Start-Process {throw 'Unexpected actual child launch.'}
function Get-WindowsImage {throw 'Unexpected host DISM call.'}
. $Helper
$script:Reads=0;$script:Launches=@();$script:Signatures=0;$script:Case=''
function New-OptimusMediaGuard {
    $guard=[pscustomobject]@{}
    $guard|Add-Member ScriptMethod ReleaseMutex {}
    $guard|Add-Member ScriptMethod Dispose {}
    return $guard
}
function Get-OptimusMediaHost {
    $script:Reads++
    $hostState=@{admin=$true;edition='Professional';build=19045;installation_type='Client';architecture=9;
        language='en-US';system_drive='C:';free_bytes=40GB;pending_restart=$false;policy_type='DWord';
        policy_value=1;process_inventory_complete=$true;conflicts=@()}
    switch($script:Case){
        'no_admin' {$hostState.admin=$false}
        'policy_type' {$hostState.policy_type='String'}
        'policy_off' {$hostState.policy_value=0}
        'low_space' {$hostState.free_bytes=24GB}
        'unknown_processes' {$hostState.process_inventory_complete=$false}
        'installer_active' {$hostState.conflicts=@(@{Name='msiexec.exe';ProcessId=123})}
        'pending_restart' {$hostState.pending_restart=$true}
        'wrong_edition' {$hostState.edition='Core'}
        'wrong_arch' {$hostState.architecture=12}
        'wrong_language' {$hostState.language='fr-FR'}
        'not_newer' {$hostState.build=26100}
    }
    return $hostState
}
function Get-OptimusMediaSignature {
    param([string]$Path)
    $script:Signatures++
    if($script:Case -eq 'bad_signature'){throw 'Fixture signature is invalid.'}
    $hash=if($script:Case -eq 'changed_media' -and $script:Signatures -gt 1){'changed'}else{'fixture-sha256'}
    return @{sha256=$hash;signer='CN=Microsoft Windows, O=Microsoft Corporation'}
}
function Get-OptimusMediaImages {
    param([string]$Path)
    if($script:Case -eq 'metadata_missing'){throw 'Fixture metadata unavailable.'}
    return [pscustomobject]@{EditionId='Professional';InstallationType='Client';Architecture=9;
        Version=[version]'10.0.26100.1';DefaultLanguage='en-US';ImageIndex=2}
}
function New-OptimusMediaReportDirectory {
    param([string]$Parent)
    $target=Join-Path $Parent $script:Case
    New-Item -ItemType Directory -Path $target -ErrorAction Stop|Out-Null
    return $target
}
function Start-OptimusMediaSetup {
    param([string]$Path,[string[]]$Arguments,[switch]$ScanOnly)
    $script:Launches+=@{path=$Path;arguments=$Arguments;scan=[bool]$ScanOnly}
    if($script:Case -eq 'launch_unknown'){throw 'Fixture launch outcome unknown.'}
    $code=switch($script:Case){
        'apps_block' {3247440392L}
        'migration_block' {3247440388L}
        'requirements_block' {3247440384L}
        'space_block' {3247440398L}
        'unknown_exit' {0L}
        default {3247440400L}
    }
    $process=[pscustomobject]@{Id=if($ScanOnly){111}else{222};ExitCode=$code}
    $process|Add-Member ScriptMethod WaitForExit {param($Milliseconds) return $script:Case -ne 'scan_timeout'}
    return $process
}
$cases=@('no_license','scan_ready','upgrade_ready','apps_block','migration_block','requirements_block','space_block',
    'unknown_exit','scan_timeout','no_admin','policy_type','policy_off','low_space','unknown_processes',
    'installer_active','pending_restart','wrong_edition','wrong_arch','wrong_language','not_newer',
    'metadata_missing','bad_signature','changed_media','launch_unknown','ambiguous_media','relative_path')
$results=@()
foreach($case in $cases){
    $script:Case=$case;$script:Reads=0;$script:Launches=@();$script:Signatures=0
    $extra=Join-Path (Split-Path -Parent $Media) 'sources\install.esd'
    if($case -eq 'ambiguous_media'){[IO.File]::WriteAllText($extra,'fixture only')}
    $parameters=@{SetupPath=$Media;OutputDirectory=$OutputRoot}
    if($case -ne 'no_license'){$parameters.AcceptLicense=$true}
    if($case -ne 'scan_ready'){$parameters.Mode='Upgrade'}
    if($case -eq 'relative_path'){$parameters.SetupPath='setup.exe'}
    $result=Invoke-OptimusWindowsMediaUpgrade @parameters
    $results+=@{case=$case;report=$result;reads=$script:Reads;launches=$script:Launches}
    if($case -eq 'ambiguous_media'){Remove-Item -LiteralPath $extra -ErrorAction Stop}
}
$results|ConvertTo-Json -Depth 18 -Compress
'''


@pytest.fixture(scope='module')
def outcomes(tmp_path_factory):
    if os.name != 'nt':
        pytest.skip('Windows-only PowerShell fixture paths; static policy check is portable.')
    root = tmp_path_factory.mktemp('media-script-fixtures')
    media = root / 'selected media & literal'
    (media / 'sources').mkdir(parents=True)
    setup = media / 'setup.exe'
    setup.write_text('fixture only; not an executable', encoding='utf-8')
    (media / 'sources' / 'install.wim').write_text('fixture image metadata is mocked', encoding='utf-8')
    output = root / 'reports & literal'
    output.mkdir()
    harness = root / 'fixture-harness.ps1'
    harness.write_text(HARNESS, encoding='utf-8')
    completed = subprocess.run([powershell(), '-NoLogo', '-NoProfile', '-NonInteractive', '-File', str(harness),
        '-Helper', str(SCRIPT), '-Media', str(setup), '-OutputRoot', str(output)], capture_output=True, text=True, timeout=35)
    assert completed.returncode == 0, completed.stdout + completed.stderr
    return {record['case']: record for record in json.loads(completed.stdout)}


def test_missing_license_defers_before_any_host_read_or_launch(outcomes):
    value = outcomes['no_license']
    assert value['report']['status'] == 'deferred'
    assert value['reads'] == 0 and value['launches'] == []
    assert '-AcceptLicense' in value['report']['reason']


def test_default_scan_only_builds_exact_compatibility_argv(outcomes):
    value = outcomes['scan_ready']
    report = value['report']
    assert report['mode'] == 'Scan' and report['status'] == 'scan_ready'
    assert report['scan']['hex'] == '0xC1900210' and report['scan']['can_upgrade']
    assert not report['upgrade_started']
    assert len(value['launches']) == 1 and value['launches'][0]['scan']
    args = value['launches'][0]['arguments']
    assert args == ['/Auto', 'Upgrade', '/EULA', 'accept', '/NoReboot', '/ImageIndex', '2',
        '/Quiet', '/Compat', 'ScanOnly', '/Compat', 'IgnoreWarning', '/CopyLogs', args[-1]]
    assert Path(args[-1]).parent == Path(report['log_path']).parent
    assert json.loads(Path(report['log_path']).read_text())['status'] == 'scan_ready'


def test_explicit_upgrade_starts_visible_only_after_success_and_fresh_checks(outcomes):
    value = outcomes['upgrade_ready']
    report = value['report']
    assert value['reads'] == 2 and len(value['launches']) == 2
    assert report['status'] == 'pending' and report['upgrade_started']
    assert report['upgrade_pid'] == 222
    assert value['launches'][0]['scan'] and not value['launches'][1]['scan']
    args = value['launches'][1]['arguments']
    assert '/Quiet' not in args and 'ScanOnly' not in args
    assert args == ['/Auto', 'Upgrade', '/EULA', 'accept', '/NoReboot', '/ImageIndex', '2',
        '/Compat', 'IgnoreWarning', '/DynamicUpdate', 'Enable', '/CopyLogs', args[-1]]
    assert 'first down-level reboot' in report['reboot_limit']
    assert 'not verified' in report['reason']
    assert 'caller responsibility' in report['preflight']['selected_media_trust']


@pytest.mark.parametrize('case,code', [('apps_block', '0xC1900208'), ('migration_block', '0xC1900204'),
    ('requirements_block', '0xC1900200'), ('space_block', '0xC190020E')])
def test_documented_scan_blocks_never_start_an_upgrade(outcomes, case, code):
    value = outcomes[case]
    assert value['report']['status'] == 'blocked'
    assert value['report']['scan']['hex'] == code
    assert not value['report']['upgrade_started'] and len(value['launches']) == 1


@pytest.mark.parametrize('case', ['unknown_exit', 'scan_timeout', 'launch_unknown', 'changed_media'])
def test_unknown_timeout_or_changed_media_never_retries_or_upgrades(outcomes, case):
    value = outcomes[case]
    assert value['report']['status'] in ('pending', 'uncertain')
    assert not value['report']['upgrade_started'] and len(value['launches']) == 1


@pytest.mark.parametrize('case', ['no_admin', 'policy_type', 'policy_off', 'low_space', 'unknown_processes',
    'installer_active', 'pending_restart', 'wrong_edition', 'wrong_arch', 'wrong_language', 'not_newer',
    'metadata_missing', 'bad_signature', 'ambiguous_media', 'relative_path'])
def test_failed_preflight_never_launches_any_media_process(outcomes, case):
    value = outcomes[case]
    assert value['report']['status'] == 'deferred'
    assert not value['report']['upgrade_started'] and value['launches'] == []


@pytest.mark.skipif(os.name != 'nt', reason='Windows PowerShell entrypoint fixture.')
def test_real_script_entrypoint_without_license_is_json_only_and_does_not_create_outputs(tmp_path):
    output = tmp_path / 'not-created'
    completed = subprocess.run([powershell(), '-NoLogo', '-NoProfile', '-NonInteractive', '-File', str(SCRIPT),
        '-SetupPath', str(tmp_path / 'setup.exe'), '-OutputDirectory', str(output)], capture_output=True, text=True, timeout=15)
    assert completed.returncode == 2, completed.stdout + completed.stderr
    assert json.loads(completed.stdout)['status'] == 'deferred'
    assert not output.exists()


@pytest.mark.skipif(os.name != 'nt', reason='Windows command-line parser fixture.')
def test_real_launcher_quotes_arguments_and_uses_expected_window_style_without_launching(tmp_path):
    """Exercise the real encoder/launcher through a fake Start-Process only."""
    arguments = ['', '/CopyLogs', 'D:\\reports with spaces\\', 'embedded"quote',
        'backslashes\\\\"quote', 'literal&value', r'D:\ends with slash\\']
    data = tmp_path / 'arguments.json'
    data.write_text(json.dumps(arguments), encoding='utf-8')
    harness = tmp_path / 'launcher-fixture.ps1'
    harness.write_text(r'''
param([string]$Helper,[string]$Data)
$ErrorActionPreference='Stop'
. $Helper
function Start-Process {
    param([string]$FilePath,[string]$ArgumentList,[switch]$PassThru,[string]$WindowStyle,[string]$ErrorAction)
    return @{path=$FilePath;line=$ArgumentList;pass_thru=[bool]$PassThru;style=$WindowStyle;errors=$ErrorAction}
}
$arguments=Get-Content -LiteralPath $Data -Raw | ConvertFrom-Json
@{scan=(Start-OptimusMediaSetup 'X:\selected media\setup.exe' $arguments -ScanOnly);
  upgrade=(Start-OptimusMediaSetup 'X:\selected media\setup.exe' $arguments)} | ConvertTo-Json -Compress
''', encoding='utf-8')
    completed = subprocess.run([powershell(), '-NoLogo', '-NoProfile', '-NonInteractive', '-File', str(harness),
        '-Helper', str(SCRIPT), '-Data', str(data)], capture_output=True, text=True, timeout=15)
    assert completed.returncode == 0, completed.stdout + completed.stderr
    captured = json.loads(completed.stdout)
    import ctypes
    from ctypes import wintypes
    parser = ctypes.WinDLL('shell32', use_last_error=True).CommandLineToArgvW
    parser.argtypes = [wintypes.LPCWSTR, ctypes.POINTER(ctypes.c_int)]
    parser.restype = ctypes.POINTER(wintypes.LPWSTR)
    free = ctypes.WinDLL('kernel32', use_last_error=True).LocalFree
    free.argtypes = [wintypes.HLOCAL]
    free.restype = wintypes.HLOCAL
    for mode, style in [('scan', 'Hidden'), ('upgrade', 'Normal')]:
        value = captured[mode]
        assert value['path'] == r'X:\selected media\setup.exe'
        assert value['style'] == style and value['pass_thru'] and value['errors'] == 'Stop'
        count = ctypes.c_int()
        parsed = parser('fixture.exe ' + value['line'], ctypes.byref(count))
        assert parsed
        try:
            assert [parsed[index] for index in range(1, count.value)] == arguments
        finally:
            free(ctypes.cast(parsed, wintypes.HLOCAL))


@pytest.mark.skipif(os.name != 'nt', reason='PowerShell DISM reader fixture.')
def test_real_image_reader_expands_native_index_records_without_reading_media(tmp_path):
    harness = tmp_path / 'image-reader-fixture.ps1'
    harness.write_text(r'''
param([string]$Helper)
$ErrorActionPreference='Stop'
. $Helper
$script:Calls=@()
function Get-WindowsImage {
    param([string]$ImagePath,[int]$Index,[string]$ErrorAction)
    $script:Calls+=@{path=$ImagePath;index=$Index;errors=$ErrorAction}
    if (-not $Index) {return @([pscustomobject]@{ImageIndex=1},[pscustomobject]@{ImageIndex=3})}
    return [pscustomobject]@{ImageIndex=$Index;EditionId='Professional';Architecture=9;
        InstallationType='Client';DefaultLanguage='en-US';Version=[version]'10.0.26100.1'}
}
$records=@(Get-OptimusMediaImages 'X:\sources\install.wim')
@{records=$records;calls=$script:Calls}|ConvertTo-Json -Depth 5 -Compress
''', encoding='utf-8')
    completed = subprocess.run([powershell(), '-NoLogo', '-NoProfile', '-NonInteractive', '-File', str(harness),
        '-Helper', str(SCRIPT)], capture_output=True, text=True, timeout=15)
    assert completed.returncode == 0, completed.stdout + completed.stderr
    captured = json.loads(completed.stdout)
    assert [record['ImageIndex'] for record in captured['records']] == [1, 3]
    assert [call['index'] for call in captured['calls']] == [0, 1, 3]
    assert all(call['path'] == r'X:\sources\install.wim' and call['errors'] == 'Stop' for call in captured['calls'])
