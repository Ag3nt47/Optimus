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
