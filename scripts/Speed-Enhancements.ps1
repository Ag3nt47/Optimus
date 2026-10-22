#requires -Version 5.1
<#
.SYNOPSIS
Inspect, apply or restore Optimus's saved native speed enhancements.
.EXAMPLE
.\Speed-Enhancements.ps1 -Executable 'D:\Optimus\Optimus.exe'
.EXAMPLE
.\Speed-Enhancements.ps1 -Executable 'D:\Optimus\Optimus.exe' -Mode apply -OutputDirectory 'D:\Optimus-Reports'
.EXAMPLE
.\Speed-Enhancements.ps1 -Executable 'D:\Optimus\Optimus.exe' -Restore 0123456789abcdef0123456789abcdef
.NOTES
Status is read-only and is the default. Apply respects the saved package toggle
and existing native permissions. This wrapper never installs tools, enables
settings, requests elevation or runs the full cleanup workflow.
#>
[CmdletBinding(DefaultParameterSetName='Mode')]
param(
    [Parameter(ParameterSetName='Mode')]
    [ValidateSet('status','apply','history')]
    [string]$Mode='status',
    [Parameter(Mandatory=$true,ParameterSetName='Restore')]
    [ValidateScript({$_ -cmatch '^[0-9a-f]{32}$'})]
    [string]$Restore,
    [string]$Executable,
    [string]$OutputDirectory
)
$ErrorActionPreference='Stop'
try {
    if (-not $PSBoundParameters.ContainsKey('Executable')) {
        # Only the executable beside the containing portable package is
        # discovered. A source checkout must supply -Executable explicitly.
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
    $arguments=@('--speed-enhancements',$Mode.ToLowerInvariant())
    if ($PSCmdlet.ParameterSetName -eq 'Restore') { $arguments=@('--speed-restore',$Restore) }
    if ($PSBoundParameters.ContainsKey('OutputDirectory')) {
        if ([string]::IsNullOrWhiteSpace($OutputDirectory)) { throw 'Supply a concrete report directory.' }
        $arguments+=@('--output',$OutputDirectory)
    }
    # Call operator + argv preserves spaces and metacharacters as literal data.
    & $executableItem.FullName @arguments
    if ($null -eq $LASTEXITCODE) { exit 0 }
    exit $LASTEXITCODE
} catch {
    [Console]::Error.WriteLine('Optimus speed enhancements: '+$_.Exception.Message)
    exit 2
}
