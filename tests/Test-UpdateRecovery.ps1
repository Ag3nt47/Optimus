$ErrorActionPreference='Stop'
. (Join-Path $PSScriptRoot '..\src\optimus\engine\UpdateRecovery.ps1')

# Exercise the real native collector with unavailable COM values. Neither an
# unknown busy flag nor an unknown restart flag may become a false/clear proof.
function New-Object {
    param([string]$ComObject)
    switch ($ComObject) {
        'Microsoft.Update.Session' {
            $session=[pscustomobject]@{}
            $session | Add-Member -MemberType ScriptMethod -Name CreateUpdateInstaller -Value { $script:fixtureInstaller }
            return $session
        }
        'Microsoft.Update.SystemInfo' { return $script:fixtureSystem }
        'System.Collections.Generic.List[object]' { return ,([System.Collections.Generic.List[object]]::new()) }
        default { throw 'The fixture attempted an unexpected native operation.' }
    }
}
function Get-CimInstance { throw 'An unavailable native flag must be rejected before collecting processes.' }

$cases=@(
    @{Busy=$null;Restart=$false},
    @{Busy=$false;Restart=$null},
    @{Busy='False';Restart=$false},
    @{Busy=$false;Restart=0}
)
foreach($case in $cases) {
    $script:fixtureInstaller=[pscustomobject]@{IsBusy=$case.Busy}
    $script:fixtureSystem=[pscustomobject]@{RebootRequired=$case.Restart}
    $rejected=$false
    try { Get-OptimusUpdateRecoveryProbe | Out-Null }
    catch {
        if($_.Exception.Message -ne 'Windows Update busy/restart state is unavailable.') { throw }
        $rejected=$true
    }
    if(-not $rejected) { throw 'An unavailable native flag was incorrectly treated as clear.' }
}
Write-Output 'Update recovery native evidence: 4 unknown-state refusal cases passed.'

. (Join-Path $PSScriptRoot '..\src\optimus\engine\ProcessInventory.ps1')
$script:fixtureBoot=[datetime]::Parse('2026-11-04T15:00:00Z').ToUniversalTime()
function Test-Path { return $false }
function Get-CimInstance {
    param([string]$ClassName,[int]$OperationTimeoutSec)
    if($ClassName -eq 'Win32_OperatingSystem'){return [pscustomobject]@{LastBootUpTime=$script:fixtureBoot}}
    if($ClassName -eq 'Win32_Process'){
        return [pscustomobject]@{ProcessId=100;Name='fixture.exe';ExecutablePath='C:\fixture.exe';CreationDate=$script:fixtureBoot.AddSeconds(2)}
    }
    throw 'Unexpected CIM request.'
}
function Get-WinEvent {
    param($FilterHashtable,[int]$MaxEvents)
    if($FilterHashtable.LogName -ne 'System' -or $FilterHashtable.ProviderName -ne 'Microsoft-Windows-Kernel-General' -or
        $FilterHashtable.Id -ne 12 -or $MaxEvents -ne 1){throw 'Unexpected boot event request.'}
    if($script:fixtureBootEventUnavailable){throw 'Boot event unavailable.'}
    return [pscustomobject]@{TimeCreated=$script:fixtureBoot.AddSeconds(1)}
}
$script:fixtureInstaller=[pscustomobject]@{IsBusy=$false}
$script:fixtureSystem=[pscustomobject]@{RebootRequired=$false}
$result=Get-OptimusUpdateRecoveryProbe
if($result.boot_utc -ne $script:fixtureBoot.ToString('o') -or
    $result.boot_event_utc -ne $script:fixtureBoot.AddSeconds(1).ToString('o')){throw 'Boot evidence was not returned.'}
$script:fixtureBootEventUnavailable=$true
$result=Get-OptimusUpdateRecoveryProbe
if($null -ne $result.boot_event_utc -or $result.complete -ne $true){throw 'Missing boot evidence was not isolated.'}
Write-Output 'Update recovery boot evidence: 2 native fixture cases passed.'
