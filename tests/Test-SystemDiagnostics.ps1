#requires -Version 5.1
$ErrorActionPreference='Stop'
. (Join-Path $PSScriptRoot '../src/optimus/engine/SystemDiagnostics.ps1')
function Assert-True {param($Value,[string]$Message) if(-not $Value){throw $Message}}
function Get-CimInstance {
    param($ClassName,$Namespace,$OperationTimeoutSec,$ErrorAction)
    switch($ClassName) {
        'Win32_OperatingSystem' {[pscustomobject]@{TotalVisibleMemorySize=1000;FreePhysicalMemory=250;TotalVirtualMemorySize=2000;FreeVirtualMemory=500}}
        'Win32_ComputerSystem' {[pscustomobject]@{AutomaticManagedPagefile=$false}}
        'Win32_PageFileUsage' {[pscustomobject]@{AllocatedBaseSize=2048;CurrentUsage=512;PeakUsage=1024}}
        'Win32_PageFileSetting' {[pscustomobject]@{InitialSize=2048;MaximumSize=4096}}
        'MSFT_PhysicalDisk' {[pscustomobject]@{FriendlyName='Test SSD';DeviceId='0';MediaType=4;BusType=17;HealthStatus=0;OperationalStatus=@(2);Size=10000}}
        'MSFT_StorageReliabilityCounter' {[pscustomobject]@{DeviceId='0';Wear=0;Temperature=$null;ReadErrorsUncorrected=0}}
        'BatteryStaticData' {[pscustomobject]@{InstanceName='TestBattery';DesignedCapacity=1000}}
        'BatteryFullChargedCapacity' {[pscustomobject]@{InstanceName='TestBattery';FullChargedCapacity=800}}
        'BatteryCycleCount' {[pscustomobject]@{InstanceName='TestBattery';CycleCount=20}}
        default {throw 'Unexpected CIM class.'}
    }
}
function Get-WinEvent {param($FilterHashtable,$MaxEvents,$ErrorAction) throw 'No accessible matching events.'}
$result=Get-OptimusSystemDiagnostics
Assert-True ($result.schema -eq 1 -and $result.read_only -eq $true) 'Collector must be read-only.'
Assert-True ($result.memory.percent -eq 75) 'Memory use must be computed from native facts.'
Assert-True ($result.pagefile.automatic_managed -eq $false) 'False pagefile setting must not disappear.'
Assert-True ($result.storage[0].wear_percent_used -eq 0) 'An exposed wear zero is valid evidence.'
Assert-True ($result.storage[0].PSObject.Properties.Name -notcontains 'temperature_c') 'Missing temperature must remain unknown.'
Assert-True ($result.batteries[0].full_charge_vs_design_percent -eq 80) 'Battery capacity ratio must use matching native identities.'
Assert-True ($result.batteries[0].cycle_count -eq 20) 'Exposed cycle count must be retained.'
Assert-True ($result.capabilities.boot_timing -eq 'unavailable_or_no_events') 'Missing event evidence cannot claim a healthy boot.'
$threw=$false
try {Invoke-OptimusWindowsIntegrity -Repair $true -Approved $false -DataRoot $PSScriptRoot}catch {$threw=$true}
Assert-True $threw 'Unapproved repair must stop before launching native commands or creating reports.'
# Native lifecycle protocol uses fake launches and an isolated fixture folder.
# No Windows servicing command is executed, even if CI itself is elevated.
$script:Launches=New-Object 'System.Collections.Generic.List[object]'
$script:ExitCodes=@(0,0,0)
$script:LaunchThrows=$false
$script:Administrator=$true
function Test-OptimusIntegrityAdministrator {return $script:Administrator}
function Start-Process {
    param($FilePath,$ArgumentList,$WindowStyle,[switch]$Wait,[switch]$PassThru,$RedirectStandardOutput,$RedirectStandardError)
    $script:Launches.Add([pscustomobject]@{executable=$FilePath;arguments=$ArgumentList;wait=$Wait;hidden=$WindowStyle})
    if($script:LaunchThrows){throw 'Mocked native launch outcome is unknown.'}
    [IO.File]::WriteAllText($RedirectStandardOutput,'mock only')
    [IO.File]::WriteAllText($RedirectStandardError,'')
    $process=[pscustomobject]@{ExitCode=$script:ExitCodes[$script:Launches.Count-1];HasExited=$true}
    $process | Add-Member -MemberType ScriptMethod -Name Refresh -Value {}
    return $process
}
$temporaryRoot=[IO.Path]::GetFullPath([IO.Path]::GetTempPath())
$scratch=Join-Path $temporaryRoot ('optimus-integrity-fixture-'+[Guid]::NewGuid().ToString('N'))
[void][IO.Directory]::CreateDirectory($scratch)
function New-FixtureIntent {
    param([bool]$Repair=$false)
    $id=[Guid]::NewGuid().ToString('N')
    $directory=Join-Path $scratch 'integrity-operations'
    [void][IO.Directory]::CreateDirectory($directory)
    $value=[ordered]@{schema=1;kind='windows-integrity-intent-v1';id=$id;owner=('sid:'+ [Security.Principal.WindowsIdentity]::GetCurrent().User.Value);repair=$Repair;status='prepared';started_at=1}
    Write-OptimusIntegrityReceipt -Path (Join-Path $directory ($id+'.json')) -Value $value
    return $id
}
try {
    $id=New-FixtureIntent
    $terminal=Invoke-OptimusWindowsIntegrity -Repair $false -Approved $false -DataRoot $scratch -OperationId $id
    Assert-True ($terminal.outcomeKnown -eq $true -and $terminal.items.Count -eq 3) ('Mock native diagnostics require all three exact terminal steps: '+($terminal | ConvertTo-Json -Depth 5 -Compress))
    Assert-True ($terminal.status -eq 'completed_review_native_logs' -and $terminal.automatic_restart -eq $false) 'Native completion must retain status without restarting.'
    Assert-True ($script:Launches[0].arguments -ceq '/Online /Cleanup-Image /CheckHealth /NoRestart' -and $script:Launches[2].arguments -ceq '/verifyonly') 'Only the exact diagnostic command vectors are allowed.'
    Assert-True (Test-Path -LiteralPath (Join-Path $terminal.report_directory 'completion.json')) 'A durable native completion is required.'
    $script:Launches.Clear();$script:ExitCodes=@(3010)
    $id=New-FixtureIntent -Repair $true
    $terminal=Invoke-OptimusWindowsIntegrity -Repair $true -Approved $true -DataRoot $scratch -OperationId $id
    Assert-True ($terminal.status -eq 'restart_required' -and $terminal.restart_needed -eq $true -and $script:Launches.Count -eq 1) 'A required restart must stop further native servicing.'
    $script:Launches.Clear();$script:Administrator=$false
    $id=New-FixtureIntent
    $terminal=Invoke-OptimusWindowsIntegrity -DataRoot $scratch -OperationId $id
    Assert-True ($terminal.status -eq 'requires_administrator' -and $terminal.outcomeKnown -eq $true -and $script:Launches.Count -eq 0) 'No administrator authority means a known no-launch receipt.'
    $script:Administrator=$true;$script:LaunchThrows=$true
    $id=New-FixtureIntent
    $terminal=Invoke-OptimusWindowsIntegrity -DataRoot $scratch -OperationId $id
    Assert-True ($terminal.outcomeKnown -eq $false -and $terminal.status -eq 'outcome_unverified') 'An ambiguous launch must stay unknown for the durable barrier.'
} finally {
    # The only recursive removal is this newly created, checked fixture root.
    $absolute=[IO.Path]::GetFullPath($scratch)
    if([IO.Path]::GetDirectoryName($absolute).TrimEnd('\') -cne $temporaryRoot.TrimEnd('\') -or
        [IO.Path]::GetFileName($absolute) -cnotmatch '^optimus-integrity-fixture-[a-f0-9]{32}$'){
        throw 'Fixture cleanup path failed its workspace boundary check.'
    }
    [IO.Directory]::Delete($absolute,$true)
}
Write-Output 'System diagnostic native fixtures passed: 16 checks; servicing launches were mocked.'
