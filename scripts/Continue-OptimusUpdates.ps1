#requires -Version 5.1
<#
.SYNOPSIS
Continue only saved Optimus app and driver updates, skipping individual bad offers.
.EXAMPLE
.\Continue-OptimusUpdates.ps1 -Executable 'D:\Optimus\Optimus.exe'
.NOTES
Uses the existing saved selections, agreements and approvals. It never enables
an updater, uninstalls apps, cleans files, clears all journals or kills installers.
If needed, Windows asks for administrator permission once so approved driver
updates can run. -StandardUser explicitly suppresses that request; protected
updates can then be deferred. No UAC dialog is clicked through.

Run after the current maintenance run has finished. A busy maintenance lease or
an actually unresolved native installer remains protected. Exact bad package
offers are skipped while other eligible updates continue. Opaque installers can
still need time to finish. Exit 0 means the eligible queue drained (including
individual skips), not that every offered update was installed. Read the report.
#>
[CmdletBinding()]
param(
    [string]$Executable,
    [string]$ReportDirectory,
    [switch]$StandardUser
)
$ErrorActionPreference='Stop'
try {
    if (-not $PSBoundParameters.ContainsKey('Executable')) {
        if ((Split-Path -Leaf (Split-Path -Parent $PSScriptRoot)) -cne 'source') {
            throw 'Source checkouts must supply the installed Optimus executable using -Executable.'
        }
        $packageRoot=[IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..\..'))
        $Executable=Join-Path $packageRoot 'Optimus.exe'
    }
    if ([string]::IsNullOrWhiteSpace($Executable)) { throw 'Supply the installed Optimus executable using -Executable.' }
    $executableItem=Get-Item -LiteralPath $Executable -Force
    if ($executableItem.PSIsContainer -or ($executableItem.Attributes -band [IO.FileAttributes]::ReparsePoint)) {
        throw 'The Optimus executable must be an ordinary file.'
    }
    if ([string]::IsNullOrWhiteSpace($ReportDirectory)) {
        $ReportDirectory=Join-Path ([IO.Path]::GetTempPath()) 'OptimusUpdateContinuation'
    }
    # Every invocation gets its own folder; an old successful receipt cannot be
    # mistaken for the result of a busy or failed new command.
    $ReportDirectory=Join-Path ([IO.Path]::GetFullPath($ReportDirectory)) ([guid]::NewGuid().ToString('N'))
    if ($ReportDirectory.Contains('"') -or $ReportDirectory.Contains("`r") -or $ReportDirectory.Contains("`n")) {
        throw 'Choose an ordinary local report directory.'
    }
    $identity=[Security.Principal.WindowsIdentity]::GetCurrent()
    try {
        $principal=New-Object Security.Principal.WindowsPrincipal($identity)
        $administrator=$principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
    } finally { $identity.Dispose() }
    # A quoted Windows argument needs special handling for a trailing backslash.
    $quotedDirectory='"'+$ReportDirectory.TrimEnd('\')+'"'
    $launch=@{FilePath=$executableItem.FullName;ArgumentList=@('--continue-updates','--output',$quotedDirectory);
              Wait=$true;PassThru=$true;WindowStyle='Hidden'}
    if (-not $administrator -and -not $StandardUser) {
        Write-Host 'Windows will request administrator permission once for approved protected updates.'
        $launch.Verb='RunAs'
    } elseif (-not $administrator) {
        Write-Warning 'Standard-user mode selected. Approved driver or protected app updates may require administrator access and be deferred.'
    }
    Write-Host ('Live and final JSON reports: '+$ReportDirectory)
    $process=Start-Process @launch
    if ($null -eq $process.ExitCode) { throw 'The Optimus update command did not return a completion status.' }
    $reports=@(Get-ChildItem -LiteralPath $ReportDirectory -Filter 'continue-updates-*.json' -File -ErrorAction SilentlyContinue |
        Where-Object { $_.Name -notlike '*.progress.json' } | Sort-Object LastWriteTimeUtc -Descending)
    if ($reports.Count -gt 0) {
        $receipt=Get-Content -LiteralPath $reports[0].FullName -Raw | ConvertFrom-Json
        Write-Host ('Result: '+$receipt.status)
        Write-Host ('Final report: '+$reports[0].FullName)
        if ($receipt.installation_block_reason) { Write-Warning $receipt.installation_block_reason }
        if ($receipt.error) { Write-Warning $receipt.error }
    } else {
        Write-Warning 'No update report was created. Another maintenance run may still own the updater; wait until it finishes and try again.'
    }
    exit $process.ExitCode
} catch {
    [Console]::Error.WriteLine('Optimus update continuation: '+$_.Exception.Message)
    exit 2
}
