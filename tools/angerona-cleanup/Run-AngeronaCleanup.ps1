[CmdletBinding()]
param([ValidateSet('Audit','Apply','Watch')][string]$Mode='Audit',[switch]$AdminRelaunch,[string]$StartupTicket)
$ErrorActionPreference='Stop'
Set-StrictMode -Version 2
Import-Module (Join-Path $PSScriptRoot 'SafeCleanup.psm1') -Force
Import-Module (Join-Path $PSScriptRoot 'AngeronaCleanup.psm1') -Force
Import-Module (Join-Path $PSScriptRoot 'StartupPermission.psm1') -Force
$config=Read-CleanupConfiguration -InstallationRoot $PSScriptRoot
$sid=[Security.Principal.WindowsIdentity]::GetCurrent().User.Value
if($config.schema -ne 1 -or $config.owner_sid -ne $sid -or [IO.Path]::GetFullPath($config.install_directory).TrimEnd('\') -ine [IO.Path]::GetFullPath($PSScriptRoot).TrimEnd('\')){throw 'Cleanup installation identity mismatch.'}
$repo=[IO.Path]::GetFullPath($config.repository_root).TrimEnd('\')
if(-not [IO.File]::Exists((Join-Path $repo 'src\angerona\app.py'))){throw 'The configured Angerona source installation is unavailable.'}
$reports=Join-Path $PSScriptRoot 'Reports';[IO.Directory]::CreateDirectory($reports)|Out-Null
$identity=Get-CleanupIdentity
if(-not $identity){throw 'The watcher token identity could not be read.'}
$isAdministrator=$identity.Administrator -and (New-Object Security.Principal.WindowsPrincipal([Security.Principal.WindowsIdentity]::GetCurrent())).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
$permission=@{state=(Get-CleanupElevationDecision $Mode $isAdministrator ([bool]$AdminRelaunch));administrator=[bool]$isAdministrator;owner_sid=$identity.Sid;session=$identity.Session;startup_prompt_attempted=$false}
$childContext=$null
if($StartupTicket -and -not $AdminRelaunch){throw 'An administrator handoff ticket requires its relaunch guard.'}
if($Mode -eq 'Watch' -and $StartupTicket){$childContext=Read-CleanupStartupTicket -Path $StartupTicket -Configuration $config;$permission.state='approved-at-startup';$permission.startup_prompt_attempted=$true}
$requestAdmin=$true
if($config.PSObject.Properties['request_admin_on_startup']){if($config.request_admin_on_startup -isnot [bool]){throw 'Administrator startup preference must be on or off.'};$requestAdmin=$config.request_admin_on_startup}
if($Mode -eq 'Watch' -and $permission.state -eq 'request-once' -and $requestAdmin){
    $handoff=Request-CleanupStartupElevation -Configuration $config
    if($handoff.action -in @('handed-off','already-running','already-starting','stopped')){$handoff|ConvertTo-Json -Depth 5;exit 0}
    $permission.state=$handoff.state;$permission.startup_prompt_attempted=$true;$permission.reason=$handoff.reason
    # An uncertain child may still be releasing its lease after cancellation.
    # Keep this parent alive without cleaning or asking UAC again until it is free.
    while(-not (Test-CleanupLeaseFree (Join-Path $PSScriptRoot 'watcher.lock'))){
        Write-CleanupJson -Path (Join-Path $PSScriptRoot 'status.json') -Value @{utc=[DateTime]::UtcNow.ToString('o');watcher_pid=$PID;watcher_alive=$true;permission=$permission;deferred=$true;reason='Cancelled administrator handoff is still releasing the watcher lease.'}
        if(Test-CleanupStopRequested $config){exit 0};Start-Sleep -Seconds 1
    }
}elseif($Mode -eq 'Watch' -and -not $requestAdmin -and -not $isAdministrator){$permission.state='standard-startup-request-disabled'}
$watcherLease=$null
if($Mode -eq 'Watch'){
    try{$watcherLease=[IO.File]::Open((Join-Path $PSScriptRoot 'watcher.lock'),[IO.FileMode]::OpenOrCreate,[IO.FileAccess]::ReadWrite,[IO.FileShare]::None)}
    catch [IO.IOException]{@{mode='watcher-already-running';completed=$false;reason='An existing watcher owns this installation.'}|ConvertTo-Json;exit 3}
}
function Invoke-RecordedCleanup([bool]$Apply,[string]$Trigger){
    try{$runLease=[IO.File]::Open((Join-Path $PSScriptRoot 'run.lock'),[IO.FileMode]::OpenOrCreate,[IO.FileAccess]::ReadWrite,[IO.FileShare]::None)}
    catch [IO.IOException]{return [pscustomobject]@{mode='cleanup-busy';completed=$false;busy=$true;reason='Another manual or watcher cleanup pass is already running.'}}
    try{
    $stopReader=(Get-Command Test-CleanupStopRequested -CommandType Function).ScriptBlock
    $stopCheck={return (-not (& $stopReader $config))}.GetNewClosure()
    $report=Invoke-AngeronaCleanup -RepositoryRoot $repo -Apply:$Apply -ContinueCheck $stopCheck
    $report|Add-Member -NotePropertyName trigger -NotePropertyValue $Trigger
    $report|Add-Member -NotePropertyName shutdown_guaranteed -NotePropertyValue $false
    $report|Add-Member -NotePropertyName permission -NotePropertyValue $permission
    $name=[DateTime]::UtcNow.ToString('yyyyMMdd-HHmmss')+'-'+[Guid]::NewGuid().ToString('N')+'.json'
    Write-CleanupJson -Path (Join-Path $reports $name) -Value $report
    Write-CleanupJson -Path (Join-Path $PSScriptRoot 'latest.json') -Value $report
    foreach($file in @(Get-ChildItem -LiteralPath $reports -Filter '*.json' -File|Where-Object{$_.Name -match '^\d{8}-\d{6}-[0-9a-f]{32}\.json$' -and -not ($_.Attributes -band [IO.FileAttributes]::ReparsePoint)}|Sort-Object LastWriteTimeUtc -Descending|Select-Object -Skip 100)){[IO.File]::Delete($file.FullName)}
    return $report
    }finally{$runLease.Dispose()}
}
$subscriptions=@()
try{
    if($Mode -ne 'Watch'){$manual=Invoke-RecordedCleanup ($Mode -eq 'Apply') 'manual';$manual|ConvertTo-Json -Depth 14;if($manual.PSObject.Properties['busy']){exit 3};exit 0}
    if(-not ('AngeronaCleanup.SessionSignal' -as [type])){
        Add-Type -TypeDefinition @'
using System;
using Microsoft.Win32;
namespace AngeronaCleanup {
 public static class SessionSignal {
  public static volatile bool Ending;
  public static bool Listening;
  public static void Start(){try{SystemEvents.SessionEnding+=(s,e)=>{Ending=true;};Listening=true;}catch{Listening=false;}}
 }
}
'@
    }
    [AngeronaCleanup.SessionSignal]::Start()
    foreach($kind in @('Start','Stop')){
        $source='AngeronaCleanup-'+$kind+'-'+[Guid]::NewGuid().ToString('N')
        $query="SELECT * FROM Win32_Process${kind}Trace WHERE ProcessName = 'python.exe' OR ProcessName = 'pythonw.exe' OR ProcessName = 'Angerona.exe' OR ProcessName = 'AngeronaStartup.exe' OR ProcessName = 'powershell.exe'"
        try{Register-WmiEvent -Query $query -SourceIdentifier $source -ErrorAction Stop|Out-Null;$subscriptions+=$source}catch{}
    }
    Write-CleanupJson -Path (Join-Path $PSScriptRoot 'status.json') -Value @{utc=[DateTime]::UtcNow.ToString('o');watcher_pid=$PID;watcher_created=$identity.Created;watcher_alive=$true;permission=$permission;deferred=$true;reason='Watcher initialized; waiting for the first process inventory.'}
    if($childContext -and -not (Confirm-CleanupChildReady $childContext $config)){exit 0}
    # First pass catches missed cleanup from a previous exit/shutdown. No task
    # claims to run after the computer has powered off.
    $armed=$true;$lastScan=[DateTime]::MinValue;$lastStatus=[DateTime]::MinValue;$lastSignature=''
    while($true){
        $event=Wait-Event -Timeout 1
        if(Test-CleanupStopRequested $config){break}
        $eventRelevant=$event -and $event.SourceIdentifier -in $subscriptions
        if($eventRelevant){Remove-Event -EventIdentifier $event.EventIdentifier -ErrorAction SilentlyContinue}
        if([AngeronaCleanup.SessionSignal]::Ending){
            Write-CleanupJson -Path (Join-Path $PSScriptRoot 'shutdown-pending.json') -Value @{utc=[DateTime]::UtcNow.ToString('o');reason='Session ending; cleanup is best effort and will be retried at next sign-in.'}
            Invoke-RecordedCleanup $true 'session-ending-best-effort'|Out-Null
            break
        }
        if(-not $eventRelevant -and ([DateTime]::UtcNow-$lastScan).TotalSeconds -lt 30){continue}
        $lastScan=[DateTime]::UtcNow
        $state=Get-CleanupProcessState -RepositoryRoot $repo
        $signature=([string]$state.safe)+'|'+$state.reason+'|'+$state.matches.Count+'|'+$state.unreadable_processes.Count
        if($signature -ne $lastSignature -or ([DateTime]::UtcNow-$lastStatus).TotalMinutes -ge 5){
            Write-CleanupJson -Path (Join-Path $PSScriptRoot 'status.json') -Value @{utc=[DateTime]::UtcNow.ToString('o');watcher_pid=$PID;watcher_created=$identity.Created;watcher_alive=$true;permission=$permission;process_state=$state;deferred=(-not $state.safe);wmi_events=($subscriptions.Count -gt 0);session_end_notification=[AngeronaCleanup.SessionSignal]::Listening;fallback_scan_seconds=30;shutdown_guaranteed=$false}
            $lastStatus=[DateTime]::UtcNow;$lastSignature=$signature
        }
        if(-not $state.safe){$armed=$true;continue}
        if($armed){
            Start-Sleep -Seconds 2
            $confirmation=Get-CleanupProcessState -RepositoryRoot $repo
            if($confirmation.safe){$pass=Invoke-RecordedCleanup $true 'verified-exit-or-sign-in-catch-up';$armed=[bool]$pass.PSObject.Properties['busy']}
        }
    }
}finally{
    foreach($source in $subscriptions){Unregister-Event -SourceIdentifier $source -ErrorAction SilentlyContinue;Get-Event -SourceIdentifier $source -ErrorAction SilentlyContinue|Remove-Event -ErrorAction SilentlyContinue}
    if($watcherLease){$watcherLease.Dispose()}
    if($Mode -eq 'Watch'){try{Write-CleanupJson -Path (Join-Path $PSScriptRoot 'status.json') -Value @{utc=[DateTime]::UtcNow.ToString('o');watcher_pid=$PID;watcher_created=$identity.Created;watcher_alive=$false;permission=$permission;reason='Watcher exited; no further cleanup is running in this process.'}}catch{}}
}
