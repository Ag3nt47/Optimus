#requires -Version 5.1
# Disposable fixtures only. No Windows registrations, services or tasks change.
$ErrorActionPreference='Stop'
$fixture=Join-Path ([IO.Path]::GetTempPath()) ('OptimusCoverage-'+[guid]::NewGuid().ToString('N'))
$checks=New-Object 'System.Collections.Generic.List[object]'
function Check([string]$Name,[bool]$Pass) {
    $checks.Add([pscustomobject]@{Check=$Name;Pass=$Pass})
    if (-not $Pass) { throw $Name }
}
try {
    [void][IO.Directory]::CreateDirectory($fixture)
    . (Join-Path $PSScriptRoot '../src/optimus/engine/ComputerManager.ps1') -LibraryOnly -OutputPath (Join-Path $fixture 'data')
    $script:CoverageFixture=$fixture
    foreach ($name in @('User','Machine')) { [void][IO.Directory]::CreateDirectory((Join-Path $fixture $name)) }
    function Get-SCMStartupFolders {
        @('User','Machine') | ForEach-Object { [pscustomobject]@{Scope=$_;Path=(Join-Path $script:CoverageFixture $_)} }
    }
    $script:RefuseFixtureRegistry=$false
    function Open-SCMStartupRegistryKey {
        param($Entry,[bool]$Writable=$false)
        if ($Writable) { throw 'This fixture never allows registry writes.' }
        if ($script:RefuseFixtureRegistry) { throw 'Fixture registry read denied.' }
        return $null
    }
    $coverage=$null
    $rows=@(Get-SCMStartupEntries -Coverage ([ref]$coverage))
    Check 'Empty supported sources can establish coverage without registering anything' ($rows.Count -eq 0 -and $coverage.Complete -eq $true -and @($coverage.Errors).Count -eq 0)
    $script:RefuseFixtureRegistry=$true
    $coverage=$null
    $rows=@(Get-SCMStartupEntries -Coverage ([ref]$coverage))
    Check 'Registry read failures remain visible in coverage' (-not $coverage.Complete -and @($coverage.Errors).Count -gt 0 -and ($coverage.Errors -join ' ') -match 'Fixture registry read denied')
    $script:RefuseFixtureRegistry=$false
    $shortcut=Join-Path $fixture 'User/LockedFixture.lnk'
    [IO.File]::WriteAllText($shortcut,'Not an actual application shortcut.')
    $handle=[IO.File]::Open($shortcut,[IO.FileMode]::Open,[IO.FileAccess]::ReadWrite,[IO.FileShare]::None)
    try {
        $coverage=$null
        $rows=@(Get-SCMStartupEntries -Coverage ([ref]$coverage))
        Check 'Unreadable shortcut fingerprint prevents a complete baseline' (-not $coverage.Complete -and ($coverage.Errors -join ' ') -match 'Cannot identify startup shortcut')
    } finally { $handle.Dispose() }
    Remove-Item -LiteralPath $shortcut
    function Get-CimInstance {
        param([string]$ClassName,[string]$Filter,$ErrorAction)
        switch ($ClassName) {
            'Win32_OperatingSystem' { [pscustomobject]@{TotalVisibleMemorySize=1000;FreePhysicalMemory=500;Caption='Fixture Windows';LastBootUpTime=[datetime]::UtcNow} }
            'Win32_Processor' { if ($script:MissingCounters) { throw 'Fixture CPU counters denied.' }; [pscustomobject]@{Name='Fixture CPU';LoadPercentage=10} }
            'Win32_LogicalDisk' { if ($script:MissingCounters) { throw 'Fixture disk counters denied.' }; [pscustomobject]@{DeviceID='Z:';VolumeName='Fixture';Size=9876543210L;FreeSpace=1234567890L} }
            default { throw ('Unrequested fixture inventory: '+$ClassName) }
        }
    }
    $system=Get-SCMSystemSummary
    Check 'Native resource audit retains exact free and total disk bytes' ($system.Disks[0].FreeBytes -eq 1234567890L -and $system.Disks[0].SizeBytes -eq 9876543210L)
    Check 'Native resource audit retains exact available and total memory bytes' ($system.MemoryAvailableBytes -eq 512000L -and $system.MemoryTotalBytes -eq 1024000L)
    Check 'Available counters explicitly establish measurement coverage' ($system.CpuMeasurementAvailable -and $system.DiskInventoryComplete -and @($system.MeasurementErrors).Count -eq 0)
    $script:MissingCounters=$true
    $unavailable=Get-SCMSystemSummary
    Check 'Unavailable counters are not represented as zero CPU or complete empty disks' ($null -eq $unavailable.CpuPercent -and -not $unavailable.CpuMeasurementAvailable -and -not $unavailable.DiskInventoryComplete -and @($unavailable.MeasurementErrors).Count -eq 2)
    $script:MissingCounters=$false
    function Get-SCMServiceEntries { @() }
    function Get-SCMScheduledEntries { @() }
    function Get-SCMInstalledApps { @() }
    function Get-SCMPowerPlans { @() }
    function Get-SCMProcessSnapshot { @() }
    function Add-SCMProcessEvidence { param($Processes,$Startup,$Tasks) @() }
    $snapshot=Get-SCMFullAudit
    Check 'Full audit carries explicitly verified supported-startup coverage' ($snapshot.StartupInventoryComplete -eq $true -and @($snapshot.StartupInventoryErrors).Count -eq 0)
    $script:RefuseFixtureRegistry=$true
    $snapshot=Get-SCMFullAudit
    Check 'Full audit preserves startup enumeration errors without false completeness' ($snapshot.StartupInventoryComplete -eq $false -and @($snapshot.InventoryErrors).Count -gt 0 -and @($snapshot.StartupInventoryErrors).Count -gt 0)
} catch {
    $checks.Add([pscustomobject]@{Check=$_.Exception.Message;Pass=$false})
} finally {
    $resolved=[IO.Path]::GetFullPath($fixture)
    $temporary=[IO.Path]::GetFullPath([IO.Path]::GetTempPath()).TrimEnd('\')+'\'
    if ($resolved.StartsWith($temporary,[StringComparison]::OrdinalIgnoreCase) -and [IO.Path]::GetFileName($resolved) -match '^OptimusCoverage-[a-f0-9]{32}$') {
        Remove-Item -LiteralPath $resolved -Recurse -Force -ErrorAction SilentlyContinue
    }
}
$checks.ToArray() | ConvertTo-Json -Depth 3
if (@($checks | Where-Object {-not $_.Pass}).Count) { exit 1 }
exit 0
