<#
.SYNOPSIS
    Clears stale Optimus update records that block app and driver installs.

.DESCRIPTION
    Optimus refuses to start app or driver installers while an older update
    record is unresolved. That is correct while an installer may still be
    running, but some records stay stuck after Windows has already restarted:

      * "A previous update requested a restart; its cleared restart state has
        not been verified."  The record asked for a restart, Windows restarted
        afterwards, but Windows keeps its own restart-pending flags set.
      * "A previous update installer has an unresolved outcome" / "needs restart
        recovery".  Optimus closed while an installer was being prepared or run
        and the record was never finished.

    Without -Apply the script only reports. With -Apply it moves each stale
    record to Data\parallel-updates-archive\<time>\ (nothing is deleted) and
    holds the exact package version an unfinished record was installing, so
    Optimus does not retry that exact offer automatically. Newer versions of
    the same package stay eligible.

    A record is only treated as stale when it was written before the current
    Windows boot. The script refuses to run while Optimus, WinGet or an MSI
    install is running.

.EXAMPLE
    powershell -NoProfile -ExecutionPolicy Bypass -File Clear-OptimusUpdateBlock.ps1
    powershell -NoProfile -ExecutionPolicy Bypass -File Clear-OptimusUpdateBlock.ps1 -Apply
#>
[CmdletBinding()]
param(
    [string]$DataDir,
    [switch]$Apply
)

Set-StrictMode -Version 2
$ErrorActionPreference = 'Stop'
$BootFenceSeconds = 120
$OrphanHoldReason = 'Optimus closed while this exact offer was being prepared and Windows has since restarted; its outcome was never recorded. Review the offer before retrying.'

function Resolve-OptimusDataDir {
    param([string]$Requested)
    $candidates = @()
    if ($Requested) { $candidates += $Requested }
    if ($PSScriptRoot) {
        $candidates += (Join-Path (Split-Path -Parent $PSScriptRoot) 'Data')
        $candidates += (Join-Path $PSScriptRoot 'Data')
    }
    $candidates += 'D:\Agent47\Optimus\Data'
    foreach ($candidate in $candidates) {
        if ($candidate -and (Test-Path -LiteralPath (Join-Path $candidate 'parallel-updates') -PathType Container)) {
            return (Resolve-Path -LiteralPath $candidate).ProviderPath
        }
    }
    throw 'Optimus Data folder with a parallel-updates folder was not found. Pass -DataDir.'
}

function Get-Number {
    param($Value)
    if ($null -eq $Value) { return $null }
    if ($Value -is [double] -or $Value -is [int] -or $Value -is [long] -or $Value -is [decimal]) {
        $number = [double]$Value
        if (-not [double]::IsNaN($number) -and -not [double]::IsInfinity($number) -and $number -gt 0) { return $number }
    }
    return $null
}

function Get-Property {
    param($Object, [string]$Name)
    if ($null -eq $Object) { return $null }
    $property = $Object.PSObject.Properties[$Name]
    if ($property) { return $property.Value }
    return $null
}

function Get-ActiveInstallers {
    $busy = @()
    foreach ($process in @(Get-CimInstance Win32_Process)) {
        $name = [string]$process.Name
        $line = [string]$process.CommandLine
        if ($name -ieq 'Optimus.exe') { $busy += "Optimus (PID $($process.ProcessId))" }
        elseif ($name -ieq 'winget.exe' -or $name -ieq 'AppInstallerCLI.exe') { $busy += "WinGet (PID $($process.ProcessId))" }
        elseif ($name -ieq 'msiexec.exe' -and $line -notmatch '(?i)\s/V\s*$') { $busy += "Windows Installer (PID $($process.ProcessId))" }
    }
    return $busy
}

function Get-RecordPackages {
    param($Record)
    $rows = @()
    $result = Get-Property $Record 'result'
    foreach ($item in @(Get-Property $result 'items')) {
        $id = Get-Property $item 'id'
        $version = Get-Property $item 'to_version'
        if ($id -and $version) { $rows += [pscustomobject]@{ id = [string]$id; available_version = [string]$version } }
    }
    $current = Get-Property $Record 'current_item'
    $id = Get-Property $current 'id'
    $version = Get-Property $current 'to_version'
    if (-not $version) { $version = Get-Property $current 'available_version' }
    if ($id -and $version) { $rows += [pscustomobject]@{ id = [string]$id; available_version = [string]$version } }
    return $rows
}

$DataDir = Resolve-OptimusDataDir $DataDir
$journalDir = Join-Path $DataDir 'parallel-updates'
$bootTime = (Get-CimInstance Win32_OperatingSystem).LastBootUpTime.ToUniversalTime()
$bootEpoch = ([DateTimeOffset]$bootTime).ToUnixTimeSeconds()
Write-Host "Optimus data: $DataDir"
Write-Host ("Windows last started: {0:yyyy-MM-dd HH:mm} (local)" -f $bootTime.ToLocalTime())

$stale = @()
foreach ($file in @(Get-ChildItem -LiteralPath $journalDir -Filter '*.json' -File)) {
    try { $record = Get-Content -LiteralPath $file.FullName -Raw -Encoding UTF8 | ConvertFrom-Json }
    catch { Write-Warning "Unreadable update record (left in place): $($file.Name)"; continue }
    $status = [string](Get-Property $record 'status')
    $result = Get-Property $record 'result'
    $started = Get-Number (Get-Property $record 'started_at')
    $finished = Get-Number (Get-Property $record 'finished_at')
    $title = [string](Get-Property $record 'title')
    $kind = $null
    if ($status -eq 'needs_review' -and (Get-Property $result 'restartNeeded') -eq $true) {
        if ($finished -and $finished + $BootFenceSeconds -lt $bootEpoch) { $kind = 'restart-already-done' }
        else { Write-Host "Still waiting for a restart (left in place): $($file.Name) $title" }
    }
    elseif ($status -in @('prepared', 'running', 'uncertain') -and (Get-Property $record 'installer_prepared') -ne $false) {
        $stamp = if ($finished) { $finished } else { $started }
        if ($stamp -and $stamp + $BootFenceSeconds -lt $bootEpoch) { $kind = 'unfinished-before-restart' }
        else { Write-Host "Installer record from the current Windows session (left in place): $($file.Name) $title" }
    }
    if ($kind) {
        $stale += [pscustomobject]@{
            File = $file; Kind = $kind; Title = $title; Status = $status
            When = if ($stamp = ($finished, $started | Where-Object { $_ } | Select-Object -First 1)) { [DateTimeOffset]::FromUnixTimeSeconds([long]$stamp).LocalDateTime } else { $null }
            Packages = if ($kind -eq 'unfinished-before-restart') { @(Get-RecordPackages $record) } else { @() }
        }
    }
}

if (-not $stale) {
    Write-Host 'No stale update records found. App and driver installs are not blocked by old records.'
    exit 0
}

Write-Host ''
Write-Host 'Stale update records blocking installs:'
foreach ($row in $stale) {
    $why = if ($row.Kind -eq 'restart-already-done') { 'asked for a restart; Windows has restarted since' } else { 'installer never finished; Windows has restarted since' }
    Write-Host ("  {0}  {1:yyyy-MM-dd HH:mm}  {2}  ({3})" -f $row.File.Name, $row.When, $row.Title, $why)
    foreach ($package in $row.Packages) { Write-Host ("      will hold {0} {1}" -f $package.id, $package.available_version) }
}

if (-not $Apply) {
    Write-Host ''
    Write-Host 'Report only. Run again with -Apply to archive these records.'
    exit 0
}

$busy = @(Get-ActiveInstallers)
if ($busy) {
    Write-Error ("Close these first, then run again: " + ($busy -join ', '))
    exit 2
}

$holdsPath = Join-Path $DataDir 'update-holds.json'
$newHolds = @($stale | ForEach-Object { $_.Packages } | Where-Object { $_ })
if ($newHolds) {
    $owner = [string](Get-Property ($stale | ForEach-Object { Get-Content -LiteralPath $_.File.FullName -Raw -Encoding UTF8 | ConvertFrom-Json } | Select-Object -First 1) 'owner')
    if (Test-Path -LiteralPath $holdsPath) {
        $holds = Get-Content -LiteralPath $holdsPath -Raw -Encoding UTF8 | ConvertFrom-Json
        if ((Get-Property $holds 'schema') -ne 1 -or (Get-Property $holds 'owner') -ne $owner) {
            Write-Error 'update-holds.json belongs to another owner or format; nothing was changed.'
            exit 3
        }
        $rows = [System.Collections.ArrayList]@(@($holds.holds) | ForEach-Object {
            [ordered]@{ id = [string]$_.id; available_version = [string]$_.available_version; reason = [string]$_.reason } })
    }
    else { $rows = [System.Collections.ArrayList]@() }
    foreach ($package in $newHolds) {
        $exists = $rows | Where-Object { $_.id -ieq $package.id -and $_.available_version -ceq $package.available_version }
        if (-not $exists) {
            [void]$rows.Add([ordered]@{ id = $package.id; available_version = $package.available_version; reason = $OrphanHoldReason })
        }
    }
    if ($rows.Count -gt 100) { Write-Error 'The update hold list would exceed 100 entries; nothing was changed.'; exit 3 }
    $json = ConvertTo-Json -Compress -Depth 5 ([ordered]@{ schema = 1; owner = $owner; holds = @($rows) })
    if ($rows.Count -eq 1) { $json = $json -replace '"holds":\{(.*)\}\}$', '"holds":[{$1}]}' }
    $backupDir = Join-Path $DataDir 'parallel-updates-archive'
    New-Item -ItemType Directory -Force -Path $backupDir | Out-Null
    if (Test-Path -LiteralPath $holdsPath) {
        Copy-Item -LiteralPath $holdsPath -Destination (Join-Path $backupDir ('update-holds-' + (Get-Date -Format 'yyyyMMdd-HHmmss') + '.json'))
    }
    [IO.File]::WriteAllText($holdsPath, $json, (New-Object Text.UTF8Encoding $false))
    Write-Host "Saved $($newHolds.Count) exact-version hold(s) in update-holds.json."
}

$archive = Join-Path (Join-Path $DataDir 'parallel-updates-archive') (Get-Date -Format 'yyyyMMdd-HHmmss')
New-Item -ItemType Directory -Force -Path $archive | Out-Null
$receipt = @()
foreach ($row in $stale) {
    Move-Item -LiteralPath $row.File.FullName -Destination (Join-Path $archive $row.File.Name)
    $receipt += [ordered]@{ file = $row.File.Name; kind = $row.Kind; title = $row.Title; status = $row.Status }
}
$receiptJson = ConvertTo-Json -Depth 4 @{ archived_at = (Get-Date).ToUniversalTime().ToString('o'); windows_boot_utc = $bootTime.ToString('o'); records = @($receipt) }
[IO.File]::WriteAllText((Join-Path $archive 'receipt.json'), $receiptJson, (New-Object Text.UTF8Encoding $false))
Write-Host "Archived $($stale.Count) record(s) to $archive"
Write-Host 'Done. Open Optimus and run it again; app and driver installs can continue.'
