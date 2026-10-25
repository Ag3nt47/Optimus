# Offline helpers only. Every task file is inside a disposable fixture tree.
#requires -Version 5.1
param([Parameter(Mandatory=$true)][string]$FixtureRoot)
$ErrorActionPreference='Stop'
$ProgressPreference='SilentlyContinue'
Set-StrictMode -Version 2
. (Join-Path $PSScriptRoot '..\src\optimus\engine\StartupSafety.ps1')
$script:Assertions=0
function Assert-Fixture([bool]$Condition,[string]$Message){if(-not $Condition){throw ('FAIL: '+$Message)};$script:Assertions++}
function Expect-FixtureFailure([scriptblock]$Action,[string]$Kind=''){
    $failed=$false;$caught=$null
    try{& $Action|Out-Null}catch{$failed=$true;$caught=$_.Exception}
    Assert-Fixture $failed 'expected operation must fail closed'
    if($Kind){Assert-Fixture ($caught.Data['OptimusFailureKind'] -ceq $Kind) 'structured failure category is retained'}
    return $caught
}
$fixture=[IO.Path]::GetFullPath($FixtureRoot)
if(-not [IO.Path]::IsPathRooted($FixtureRoot) -or [IO.Path]::GetFileName($fixture) -notmatch '^optimus-startup-safety-[0-9a-f]{32}$' -or (Test-Path -LiteralPath $fixture)){throw 'A new exact disposable fixture directory is required.'}
$script:WindowsDirectory=Join-Path $fixture 'Windows'
$tasks=Join-Path $script:WindowsDirectory 'System32\Tasks'
$null=New-Item -ItemType Directory -Path $tasks -Force
$script:Available=[long]2147483648
$script:QueryThrows=$false
function Get-OptimusStartupWindowsDirectory {return $script:WindowsDirectory}
function Get-OptimusStartupAvailableFree([string]$Volume){if($script:QueryThrows){throw 'fixture unavailable'};return $script:Available}
# A regression that attempts external launch or COM registration fails here.
function Start-Process {throw 'No external process is permitted in this fixture.'}
function Register-ScheduledTask {throw 'No real task registration is permitted.'}
function Unregister-ScheduledTask {throw 'No real task removal is permitted.'}
function New-Object {param($ComObject) throw 'No COM object is permitted in this fixture.'}

$ready=Assert-OptimusStartupHeadroom
Assert-Fixture ($ready.free_bytes -eq 2147483648) 'exactly 2 GiB passes'
Assert-Fixture ($ready.required_bytes -eq 2147483648) 'threshold is fixed in bytes'
Assert-Fixture ($ready.system_volume -ceq [IO.Path]::GetPathRoot($script:WindowsDirectory)) 'the mocked Windows volume is queried'
foreach($free in @([long]2147483647,[long]0)){
    $script:Available=$free
    $null=Expect-FixtureFailure {Assert-OptimusStartupHeadroom} 'system_volume_space'
}
foreach($unknown in @($null,'2147483648',[int]2048)){
    $script:Available=$unknown
    $null=Expect-FixtureFailure {Assert-OptimusStartupHeadroom}
}
$script:QueryThrows=$true
$null=Expect-FixtureFailure {Assert-OptimusStartupHeadroom}
$script:QueryThrows=$false;$script:Available=[long]2147483648
$originalWindows=$script:WindowsDirectory;$script:WindowsDirectory='not-a-local-Windows-path'
$null=Expect-FixtureFailure {Assert-OptimusStartupHeadroom}
$script:WindowsDirectory=$originalWindows

$name='Optimus-SignIn-S-1-5-21-111-222-333-1001'
$path=Join-Path $tasks $name
$xml='<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task"><RegistrationInfo><Description>Fixture</Description></RegistrationInfo><Actions Context="Fixture"><Exec><Command>D:\fixture\Optimus.exe</Command></Exec></Actions></Task>'
$pretty=@'
<?xml version="1.0" encoding="utf-16"?>
<Task xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task" version="1.2">
  <RegistrationInfo><Description>Fixture</Description></RegistrationInfo>
  <Actions Context="Fixture"><Exec><Command>D:\fixture\Optimus.exe</Command></Exec></Actions>
</Task>
'@
function Write-FixtureTask([string]$Text){[IO.File]::WriteAllText($path,$Text,[Text.UTF8Encoding]::new($false))}
Write-FixtureTask $xml
$verified=Read-OptimusPersistedTask $name $xml
Assert-Fixture ($verified.verified -eq $true) 'actual disposable persisted bytes verify'
Assert-Fixture ($verified.path -ceq $path) 'readback stays inside the fixture Windows directory'
Assert-Fixture ($verified.bytes -gt 0 -and $verified.canonical_sha256 -match '^[0-9a-f]{64}$') 'bounded byte count and canonical hash are returned'
Assert-Fixture ((ConvertTo-OptimusCanonicalTaskXml $xml) -ceq (ConvertTo-OptimusCanonicalTaskXml $pretty)) 'formatting, declaration and attribute order do not alter canonical XML'
[IO.File]::WriteAllText($path,$pretty,[Text.Encoding]::Unicode)
$utf16=Read-OptimusPersistedTask $name $xml
Assert-Fixture ($utf16.verified -eq $true) 'UTF-16 BOM storage is decoded independently'
Assert-Fixture ($utf16.canonical_sha256 -ceq $verified.canonical_sha256) 'UTF-8 and UTF-16 storage preserve the same canonical XML'

[IO.File]::WriteAllBytes($path,[byte[]]@())
$null=Expect-FixtureFailure {Read-OptimusPersistedTask $name $xml} 'persisted_task_unverified'
foreach($bad in @('<Task','<Task xmlns="urn:wrong"/>','<NotTask xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task"/>',($xml.Replace('Fixture</Description>','Changed</Description>')))){
    Write-FixtureTask $bad
    $null=Expect-FixtureFailure {Read-OptimusPersistedTask $name $xml} 'persisted_task_unverified'
}
$dtd='<!DOCTYPE Task [<!ENTITY e SYSTEM "file:///Z:/OptimusFixtureMustNeverRead.txt">]><Task xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task"><Description>&e;</Description></Task>'
Write-FixtureTask $dtd
$null=Expect-FixtureFailure {Read-OptimusPersistedTask $name $xml} 'persisted_task_unverified'
$null=Expect-FixtureFailure {ConvertTo-OptimusCanonicalTaskXml ('x'*65537)}
Write-FixtureTask ('x'*262145)
$null=Expect-FixtureFailure {Read-OptimusPersistedTask $name $xml} 'persisted_task_unverified'
Write-FixtureTask $xml
$null=Expect-FixtureFailure {Read-OptimusPersistedTask '..\foreign-task' $xml} 'persisted_task_unverified'
function Get-Item {
    param([string]$LiteralPath,[switch]$Force,$ErrorAction)
    if($LiteralPath -ceq $tasks){return [pscustomobject]@{Attributes=[IO.FileAttributes]::ReparsePoint}}
    return Microsoft.PowerShell.Management\Get-Item -LiteralPath $LiteralPath -Force -ErrorAction Stop
}
$null=Expect-FixtureFailure {Read-OptimusPersistedTask $name $xml} 'persisted_task_unverified'
Remove-Item -LiteralPath Function:\Get-Item
# Delete only the exact task-shaped fixture file, never the host Tasks folder.
if(-not $path.StartsWith($fixture+'\',[StringComparison]::OrdinalIgnoreCase)){throw 'Fixture path escaped its root.'}
[IO.File]::Delete($path)
$null=Expect-FixtureFailure {Read-OptimusPersistedTask $name $xml} 'persisted_task_unverified'

$sensitive='FIXTURE_ONLY_password_token_path_MUST_NOT_ECHO'
$native=[Runtime.InteropServices.COMException]::new($sensitive,-2147024784)
$wrapped=[IO.IOException]::new($sensitive,$native)
$wrapped.Data['OptimusFailureKind']='system_volume_space'
$failure=Get-OptimusStartupFailure $wrapped $false
$encoded=$failure|ConvertTo-Json -Depth 12 -Compress
Assert-Fixture ($failure.ok -eq $false -and $failure.task_registration_started -eq $false) 'failure retains known prelaunch state'
Assert-Fixture ($failure.failure_kind -ceq 'system_volume_space') 'bounded exception chain retains failure kind'
Assert-Fixture (@($failure.native_error_chain|Where-Object{$_.hresult -ceq '0x80070070'}).Count -eq 1) 'disk-full HRESULT is retained numerically'
Assert-Fixture ($encoded -notmatch [regex]::Escape($sensitive)) 'exception messages, paths and credentials are not echoed'
Assert-Fixture ($failure.native_error_chain.Count -eq 2) 'native and managed exception types are retained'
$win32=[ComponentModel.Win32Exception]::new(112,$sensitive)
$winResult=Get-OptimusStartupFailure $win32 $true
Assert-Fixture ($winResult.native_error_chain[0].win32_code -eq 112) 'Win32 disk-full code is retained'
Assert-Fixture ($winResult.task_registration_started -eq $true) 'post-registration uncertainty stays explicit'
$deep=$win32
foreach($i in 1..20){$deep=[Exception]::new($sensitive,$deep)}
$bounded=Get-OptimusStartupFailure $deep $true
Assert-Fixture ($bounded.native_error_chain.Count -eq 8) 'deep exception chains are capped at eight'
Assert-Fixture (($bounded|ConvertTo-Json -Depth 12 -Compress) -notmatch [regex]::Escape($sensitive)) 'deep exceptions never leak their messages'
foreach($row in $bounded.native_error_chain){Assert-Fixture (@($row.Keys|Where-Object{$_ -notin @('type','hresult','win32_code')}).Count -eq 0) 'only numeric and type fields enter native diagnostics'}

@{Result='PASS';Assertions=$script:Assertions;RealTaskRegistrations=0;RealTaskRemovals=0;RealExternalProgramsLaunched=0;FixtureTaskFilesOnly=$true;PSVersion=$PSVersionTable.PSVersion.ToString()}|ConvertTo-Json -Compress
