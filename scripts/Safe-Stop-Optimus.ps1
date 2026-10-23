#requires -Version 5.1
<#
.SYNOPSIS
Ask the running Optimus window to stop safely and exit after active work returns.
.EXAMPLE
.\Safe-Stop-Optimus.ps1 -Executable 'D:\Optimus\Optimus.exe'
.NOTES
Stops new workflow work. Active operations are allowed to finish; unknown outcomes
remain saved for review and block automatic retries. A successful exit code acknowledges the request only;
it does not claim that all operations have already finished. No process is killed,
no installer is cancelled forcibly, and no administrator prompt is requested.
Unsaved Sandbox edits and active setup/settings questions can still require the
user's decision. If no compatible window responds, this script fails safely.
#>
[CmdletBinding()]
param([string]$Executable)
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
    # Windows GUI executables are not reliably awaited by PowerShell's call
    # operator. Wait explicitly and use the child's actual acknowledgement code.
    $process=Start-Process -FilePath $executableItem.FullName -ArgumentList @('--safe-stop') -Wait -PassThru -WindowStyle Hidden
    if ($null -eq $process.ExitCode) { throw 'The Optimus stop command did not return an acknowledgement status.' }
    exit $process.ExitCode
} catch {
    [Console]::Error.WriteLine('Optimus safe stop: '+$_.Exception.Message)
    exit 2
}
