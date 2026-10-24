#requires -Version 5.1
<#
.SYNOPSIS
Save sustained resource, hardware, boot, pagefile and installer-space observations.
.NOTES
Read-only: no UAC request, preference save, background monitor, cleanup, repair
or installer is started. Some protected hardware facts can be unavailable.
Source checkouts must specify the installed executable with -Executable.
#>
[CmdletBinding()]
param([string]$Executable, [string]$ReportDirectory)
$ErrorActionPreference='Stop'
try {
    if (-not $PSBoundParameters.ContainsKey('Executable')) {
        if ((Split-Path -Leaf (Split-Path -Parent $PSScriptRoot)) -cne 'source') {
            throw 'Supply the installed Optimus executable using -Executable.'
        }
        $Executable=Join-Path ([IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..\..'))) 'Optimus.exe'
    }
    if ([string]::IsNullOrWhiteSpace($Executable)) { throw 'Supply -Executable.' }
    $item=Get-Item -LiteralPath $Executable -Force
    if ($item.PSIsContainer -or ($item.Attributes -band [IO.FileAttributes]::ReparsePoint)) {
        throw 'The executable must be an ordinary file.'
    }
    if ([string]::IsNullOrWhiteSpace($ReportDirectory)) {
        $ReportDirectory=Join-Path ([IO.Path]::GetTempPath()) 'OptimusPerformanceChecks'
    }
    $ReportDirectory=Join-Path ([IO.Path]::GetFullPath($ReportDirectory)) ([guid]::NewGuid().ToString('N'))
    if ($ReportDirectory.Contains('"') -or $ReportDirectory.Contains("`r") -or $ReportDirectory.Contains("`n")) {
        throw 'Choose an ordinary local report directory.'
    }
    $quoted='"'+$ReportDirectory.TrimEnd('\')+'"'
    $process=Start-Process -FilePath $item.FullName -ArgumentList @('--toolkit','check','--output',$quoted) -WindowStyle Hidden -Wait -PassThru
    if ($null -eq $process.ExitCode) { throw 'The diagnostic command returned no status.' }
    $reports=@(Get-ChildItem -LiteralPath $ReportDirectory -Filter 'toolkit-check-*.json' -File -ErrorAction SilentlyContinue)
    if ($reports.Count -ne 1) { throw 'No unique diagnostic report was returned.' }
    $receipt=Get-Content -LiteralPath $reports[0].FullName -Raw | ConvertFrom-Json
    if ($receipt.command -cne 'toolkit-check' -or $receipt.result.read_only -ne $true -or $receipt.result.maintenance_actions_executed -ne 0) {
        throw 'The diagnostic receipt is invalid.'
    }
    Write-Host ('Read-only performance report: '+$reports[0].FullName)
    exit $process.ExitCode
} catch {
    [Console]::Error.WriteLine('Optimus performance check: '+$_.Exception.Message)
    exit 2
}
