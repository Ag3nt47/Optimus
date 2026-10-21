# Native sink/flags fixtures and exactly one newly created disposable-file recycle.
# No personal file or existing fixture is selected. No permanent-delete fallback.
[CmdletBinding()]
param([switch]$SkipLive)
$ErrorActionPreference='Stop'
$ProgressPreference='SilentlyContinue'
$engine=Join-Path $PSScriptRoot '..\src\optimus\engine\SafeRecycle.ps1'
. $engine
Initialize-SCMSafeRecycle
$checks=New-Object 'System.Collections.Generic.List[object]'
function Check([string]$Name,[bool]$Passed){$checks.Add([pscustomobject]@{Check=$Name;Pass=$Passed});if(-not $Passed){throw $Name}}
try {
    Check 'Native C# helper compiles on PS5' ($null -ne ('OptimusSafeRecycle' -as [type]))
    $flags=[OptimusSafeRecycle]::OperationFlags
    Check 'Windows recycle-only flag is mandatory' (($flags -band 0x80000) -ne 0)
    Check 'Silent no-confirm no-error-UI flags enabled' (($flags -band 0x414) -eq 0x414)
    Check 'Early failure prevents ignore/continue fallback' (($flags -band 0x100000) -ne 0)
    Check 'No elevation prompt flags are enabled' (($flags -band 0x10040000) -eq 0)
    Check 'Permanent-delete pre-operation flags are refused' (-not [OptimusRecycleSink]::IsRecycle(0))
    $sink=New-Object OptimusRecycleSink((Join-Path $env:TEMP 'not-a-selected-user-file.fixture'))
    Check 'PreDelete cancels a permanent operation before file access' ($sink.PreDeleteItem(0,$null) -lt 0)
    Check 'PostDelete without Recycle Bin destination is never success' ($sink.PostDeleteItem(0,$null,0,$null) -lt 0 -and -not $sink.RecycleDestinationConfirmed)
    Check 'An invalid path is rejected without native operation' $( $blocked=$false; try{[void](Invoke-SCMSafeRecycleFile -Path '\\invalid\share\file')}catch{$blocked=$true};$blocked )
    $branch=Join-Path $PSScriptRoot 'Test-AutomaticRecycleBranch.ps1'
    & (Join-Path $PSHOME 'powershell.exe') -NoLogo -NoProfile -NonInteractive -STA -ExecutionPolicy Bypass -File $branch
    Check 'Automatic branch failure/no-fallback fixture passed' ($LASTEXITCODE -eq 0)
    if(-not $SkipLive) {
        $repoRoot=[IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..')).TrimEnd('\')
        $workRoot=Join-Path $repoRoot 'test-artifacts\SafeRecycle'
        $fixtureRoot=Join-Path $workRoot ([guid]::NewGuid().ToString('N'))
        if(-not ([IO.Path]::GetFullPath($fixtureRoot)).StartsWith(($repoRoot+'\'),[StringComparison]::OrdinalIgnoreCase)){throw 'Fixture escaped the checkout.'}
        [void](New-Item -ItemType Directory -Path $fixtureRoot -Force)
        $target=Join-Path $fixtureRoot 'recycle-only.fixture'
        $keeper=Join-Path $fixtureRoot 'retained.fixture'
        if(-not $target.StartsWith(($workRoot+'\'),[StringComparison]::OrdinalIgnoreCase) -or [IO.File]::Exists($target)){throw 'Fixture path did not remain in owned work directory.'}
        [IO.File]::WriteAllText($target,'Owned disposable Optimus recycle proof.')
        [IO.File]::WriteAllText($keeper,'Owned disposable Optimus recycle proof.')
        $before=(Get-FileHash -LiteralPath $keeper -Algorithm SHA256).Hash
        $result=Invoke-SCMSafeRecycleFile -Path $target
        Check 'Exactly one owned fixture was silently recycled' ($result.Recycled -and $result.Silent -and -not [IO.File]::Exists($target))
        Check 'Native PostDelete returned an actual Recycle Bin item' $result.RecycleDestinationConfirmed
        Check 'Pre and post notifications both retain recycle flag' (($result.PreDeleteFlags -band 0x80) -ne 0 -and ($result.PostDeleteFlags -band 0x80) -ne 0)
        Check 'Unselected keeper fixture remains unchanged' ([IO.File]::Exists($keeper) -and (Get-FileHash -LiteralPath $keeper -Algorithm SHA256).Hash -eq $before)
        [pscustomobject]@{OwnedFixtureRoot=$fixtureRoot;PreDeleteFlags=$result.PreDeleteFlags;PostDeleteFlags=$result.PostDeleteFlags;RecycleDestinationConfirmed=$result.RecycleDestinationConfirmed} | ConvertTo-Json -Compress | Write-Output
    } else {
        Check 'Live recycle proof skipped for CI' $true
    }
} catch {
    $checks.Add([pscustomobject]@{Check=$_.Exception.Message;Pass=$false})
}
$checks.ToArray() | Format-Table -AutoSize | Out-String | Write-Output
if(@($checks | Where-Object {-not $_.Pass}).Count){exit 1}
exit 0
