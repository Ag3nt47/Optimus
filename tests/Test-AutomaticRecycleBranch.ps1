# Existing FileTools server branch, fixture files only. Native operation is mocked.
$ErrorActionPreference='Stop'
$ProgressPreference='SilentlyContinue'
. (Join-Path $PSScriptRoot '..\src\optimus\engine\FileTools.ps1')
$checks=New-Object 'System.Collections.Generic.List[object]'
function Check([string]$Name,[bool]$Pass){$checks.Add([pscustomobject]@{Check=$Name;Pass=$Pass});if(-not $Pass){throw $Name}}
try {
    $repoRoot=[IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..')).TrimEnd('\')
    $fixtureRoot=Join-Path $repoRoot ('test-artifacts\SafeRecycleBranch\'+[guid]::NewGuid().ToString('N'))
    if(-not ([IO.Path]::GetFullPath($fixtureRoot)).StartsWith(($repoRoot+'\'),[StringComparison]::OrdinalIgnoreCase)){throw 'Fixture escaped checkout.'}
    [void](New-Item -ItemType Directory -Path $fixtureRoot -Force)
    foreach($name in @('selected.fixture','keeper.fixture')){[IO.File]::WriteAllText((Join-Path $fixtureRoot $name),('Owned duplicate fixture. '*100))}
    $scan=Get-SCMDuplicateFiles -Root $fixtureRoot -MinBytes 1 -MaxFiles 20 -MaxSeconds 10
    Check 'Fresh server scan approved exactly two fixture candidates' ($scan.Complete -and $scan.Candidates.Count -eq 2)
    $record=@($scan.Candidates | Where-Object {[IO.Path]::GetFileName($_.FilePath) -eq 'selected.fixture'})[0]
    $record | Add-Member -NotePropertyName AutomaticApproved -NotePropertyValue $true
    $selected=$record.FilePath
    $keeper=Join-Path $fixtureRoot 'keeper.fixture'
    $before=(Get-FileHash -LiteralPath $selected -Algorithm SHA256).Hash
    # Policy lookups are fixture-only stubs; no Windows settings are modified.
    function Get-ItemProperty {param($LiteralPath,$ErrorAction)return $null}
    $script:NativeCalls=0
    function Invoke-SCMSafeRecycleFile {param([string]$Path)$script:NativeCalls++;if($Path -ne $selected){throw 'Unexpected native path'};throw 'Owned native refusal fixture'}
    $blocked=$false;$message=''
    try{[void](Move-SCMFileToRecycleBin -Record $record)}catch{$blocked=$true;$message=$_.Exception.Message}
    Check 'AutomaticApproved routes through the silent native helper' ($script:NativeCalls -eq 1)
    Check 'Native failure propagates with no legacy VB fallback' ($blocked -and $message -eq 'Owned native refusal fixture' -and [IO.File]::Exists($selected) -and (Get-FileHash -LiteralPath $selected -Algorithm SHA256).Hash -eq $before)
    Check 'Keeper and approved record remain untouched on native failure' ([IO.File]::Exists($keeper) -and -not $script:SCMFileToolApprovals[$scan.ScanId].Records[$selected].Recycled)
    $record.AutomaticApproved='true';$blocked=$false
    try{[void](Move-SCMFileToRecycleBin -Record $record)}catch{$blocked=$true}
    Check 'A truthy string cannot request the automatic branch' ($blocked -and $script:NativeCalls -eq 1 -and [IO.File]::Exists($selected))
    $record.AutomaticApproved=$true
    Remove-Item -LiteralPath 'Function:\Invoke-SCMSafeRecycleFile'
    $blocked=$false;$message=''
    try{[void](Move-SCMFileToRecycleBin -Record $record)}catch{$blocked=$true;$message=$_.Exception.Message}
    Check 'Missing native engine never uses an alternate deletion path' ($blocked -and $message -like '*no alternate delete*' -and [IO.File]::Exists($selected))
    function Invoke-SCMSafeRecycleFile {param([string]$Path)[pscustomobject]@{Recycled=$true}}
    $blocked=$false;$message=''
    try{[void](Move-SCMFileToRecycleBin -Record $record)}catch{$blocked=$true;$message=$_.Exception.Message}
    Check 'An unperformed native result is not reported as recycled' ($blocked -and $message -like '*did not remove*' -and [IO.File]::Exists($selected))
} catch {$checks.Add([pscustomobject]@{Check=$_.Exception.Message;Pass=$false})}
$checks.ToArray() | Format-Table -AutoSize | Out-String | Write-Output
if(@($checks | Where-Object {-not $_.Pass}).Count){exit 1}
exit 0
