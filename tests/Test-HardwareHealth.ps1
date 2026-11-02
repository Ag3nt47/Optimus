#requires -Version 5.1
# No actual provider, hardware test, repair, installer or process launch runs here.
param([Parameter(Mandatory=$true)][string]$ReceiptsPath)
$ErrorActionPreference='Stop'
. (Join-Path $PSScriptRoot '../src/optimus/engine/SystemDiagnostics.ps1')
$script:Assertions=0
function Assert-True {param($Value,[string]$Message) $script:Assertions++;if(-not $Value){throw $Message}}
function New-FixtureState {
    $script:Calls=New-Object 'System.Collections.Generic.List[object]'
    $script:Failures=@()
    $script:Disks=@(
        [pscustomobject]@{FriendlyName='Fixture SSD';DeviceId='12';UniqueId='Drive-Unique-1234';Size=[long]1000000;MediaType=[uint16]4;BusType=[uint16]17;HealthStatus=[uint16]0;OperationalStatus=@([uint16]2);SerialNumber='do-not-export-drive-serial'},
        [pscustomobject]@{FriendlyName='Fixture HDD';DeviceId='2';UniqueId='Drive-Unique-5678';Size=[long]2000000;MediaType=[uint16]3;BusType=[uint16]11;HealthStatus=[uint16]1;OperationalStatus=@([uint16]3)})
    $script:Mapped=@([pscustomobject]@{UniqueId='Drive-Unique-1234';Number=[uint32]2;Size=[long]1000000},
        [pscustomobject]@{UniqueId='Drive-Unique-5678';Number=[uint32]12;Size=[long]2000000})
    $script:Partitions=@([pscustomobject]@{DiskNumber=[uint32]2;DriveLetter=[char]'C'},
        [pscustomobject]@{DiskNumber=[uint32]2;DriveLetter=[char]'D'},[pscustomobject]@{DiskNumber=[uint32]12;DriveLetter=[char]'F'})
    $script:Counters=@{
        'Drive-Unique-1234'=@([pscustomobject]@{DeviceId='12';Wear=[byte]0;Temperature=[byte]31;TemperatureMax=[byte]70;PowerOnHours=[uint16]120;ReadErrorsUncorrected=[uint64]0;WriteErrorsUncorrected=[uint64]0})
        'Drive-Unique-5678'=@([pscustomobject]@{DeviceId='2';Wear=[byte]120;Temperature=[byte]45;PowerOnHours=[uint16]240;ReadErrorsUncorrected=[uint64]2})}
    $script:Static=@([pscustomobject]@{InstanceName='FixtureBattery-instance-0';Active=$true;DesignedCapacity=[uint32]1000;
        Capabilities=[uint32]0;SerialNumber='do-not-export-battery-serial';DeviceName='Fixture Battery';ManufactureName='Fixture Manufacturer'})
    $script:Full=@([pscustomobject]@{InstanceName='FixtureBattery-instance-0';FullChargedCapacity=[uint32]800})
    $script:Cycles=@([pscustomobject]@{InstanceName='FixtureBattery-instance-0';CycleCount=[uint32]20})
    $script:BatteryFallback=@()
}
function Get-CimInstance {
    param($ClassName,$Namespace,$OperationTimeoutSec,$ErrorAction)
    $script:Calls.Add([pscustomobject]@{operation='query';class=$ClassName;timeout=$OperationTimeoutSec})
    if($ClassName -in $script:Failures){throw 'Private fixture error; this text must not be exported.'}
    switch($ClassName){
        'MSFT_PhysicalDisk' {return $script:Disks}
        'MSFT_Disk' {return $script:Mapped}
        'MSFT_Partition' {return $script:Partitions}
        'BatteryStaticData' {return $script:Static}
        'BatteryFullChargedCapacity' {return $script:Full}
        'BatteryCycleCount' {return $script:Cycles}
        'Win32_Battery' {return $script:BatteryFallback}
        'Win32_OperatingSystem' {return [pscustomobject]@{TotalVisibleMemorySize=1000;FreePhysicalMemory=250;TotalVirtualMemorySize=2000;FreeVirtualMemory=500}}
        'Win32_ComputerSystem' {return [pscustomobject]@{AutomaticManagedPagefile=$false}}
        'Win32_PageFileUsage' {return [pscustomobject]@{AllocatedBaseSize=2048;CurrentUsage=512;PeakUsage=1024}}
        'Win32_PageFileSetting' {return [pscustomobject]@{InitialSize=2048;MaximumSize=4096}}
        default {throw 'Unexpected fake CIM query.'}
    }
}
function Get-CimAssociatedInstance {
    param($InputObject,$Association,$ResultClassName,$OperationTimeoutSec,$ErrorAction)
    Assert-True ($Association -ceq 'MSFT_PhysicalDiskToStorageReliabilityCounter') 'Only the exact physical-disk association is permitted.'
    Assert-True ($ResultClassName -ceq 'MSFT_StorageReliabilityCounter') 'Only reliability counters can be associated.'
    $script:Calls.Add([pscustomobject]@{operation='association';class=$Association;timeout=$OperationTimeoutSec})
    if('association' -in $script:Failures){throw 'Fake counter is inaccessible.'}
    return $script:Counters[$InputObject.UniqueId]
}
function Get-WinEvent {param($FilterHashtable,$MaxEvents,$ErrorAction) throw 'No fixture events.'}
$receipts=[ordered]@{}
New-FixtureState
$good=Get-OptimusHardwareHealth
$receipts.good=$good
Assert-True ($good.schema -eq 1 -and $good.read_only -eq $true -and $good.platform -ceq 'Windows') 'The observation schema must be read-only Windows facts.'
Assert-True ($good.storage.Count -eq 2 -and $good.batteries.Count -eq 1) 'Physical disks and batteries are separate device inventories.'
Assert-True (($good.storage[0].volumes -join ',') -ceq 'C:,D:' -and ($good.storage[1].volumes -join ',') -ceq 'F:') 'Partitions must map through exact unique disk IDs, never matching DeviceId to disk number.'
Assert-True ($good.storage[0].disk_number -eq 2 -and $good.storage[0].device_id -ceq '12') 'Physical and logical numbering must remain distinct.'
Assert-True ($good.storage[0].wear_percent_used -eq 0 -and $good.storage[0].uncorrected_read_errors -eq 0) 'Native zero wear/error counters are valid.'
Assert-True ($good.storage[1].wear_percent_used -eq 120) 'Wear past its estimated limit is not clamped to a fake remaining-life percentage.'
Assert-True ($good.storage[0].temperature_c -eq 31 -and $good.storage[0].maximum_operating_temperature_c -eq 70) 'TemperatureMax is the maximum normal operating temperature.'
Assert-True ($good.batteries[0].full_charge_vs_design_percent -eq 80 -and $good.batteries[0].capacity_units -ceq 'mWh') 'Known absolute capacities must retain exact native units and capacity ratio.'
Assert-True ($good.batteries[0].cycle_count -eq 20) 'An exposed positive cycle counter is retained.'
Assert-True ($good.storage[0].identity_quality -ceq 'stable' -and $good.storage[0].identity_hash -cmatch '^[a-f0-9]{64}$') 'Stable physical hardware IDs are opaque hashes.'
Assert-True ($good.batteries[0].identity_quality -ceq 'stable' -and $good.batteries[0].identity_hash -cmatch '^[a-f0-9]{64}$') 'A unique battery serial plus instance produces an opaque identity.'
$json=$good | ConvertTo-Json -Depth 10 -Compress
Assert-True ($json -notmatch 'do-not-export|Drive-Unique|FixtureBattery-instance') 'Raw serials, unique IDs and battery instance paths cannot leave the collector.'
Assert-True (@($script:Calls | Where-Object {$_.timeout -lt 1 -or $_.timeout -gt 5}).Count -eq 0) 'Every focused native query has a bounded timeout.'
Assert-True (@($script:Calls | Where-Object class -eq 'MSFT_StorageReliabilityCounter').Count -eq 0) 'A bulk counter DeviceId inventory cannot be guessed as physical-disk identity.'

New-FixtureState
$script:Counters['Drive-Unique-1234'][0].DeviceId='2'
$wrong=Get-OptimusHardwareHealth
Assert-True (-not $wrong.storage[0].reliability_available) 'A contradictory associated counter identity stays unknown.'

New-FixtureState
$script:Counters['Drive-Unique-1234']+= $script:Counters['Drive-Unique-1234'][0]
$duplicate=Get-OptimusHardwareHealth
Assert-True (-not $duplicate.storage[0].reliability_available) 'Ambiguous reliability counter associations stay unknown.'

New-FixtureState
$counter=$script:Counters['Drive-Unique-1234'][0]
$counter.Temperature=[byte]255;$counter.TemperatureMax=[byte]0;$counter.Wear=[byte]255
$counter.PowerOnHours=[uint16]65535;$counter.ReadErrorsUncorrected=[uint64]::MaxValue;$counter.WriteErrorsUncorrected=$null
$script:Disks[0].HealthStatus=$true
$sentinel=Get-OptimusHardwareHealth
$receipts.sentinels=$sentinel
Assert-True (-not $sentinel.storage[0].reliability_available) 'Missing and all-ones counters must stay unavailable.'
foreach($field in @('temperature_c','maximum_operating_temperature_c','wear_percent_used','power_on_hours','uncorrected_read_errors','health_status')){
    Assert-True ($sentinel.storage[0].PSObject.Properties.Name -notcontains $field) ('Unsupported or malformed fact cannot become zero: '+$field)
}

New-FixtureState
$script:Static[0].Capabilities=[uint32]1073741824;$script:Full[0].FullChargedCapacity=[uint32]1050
$relative=Get-OptimusHardwareHealth
Assert-True ($relative.batteries[0].capacity_units -ceq 'relative provider units' -and $relative.batteries[0].full_charge_vs_design_percent -eq 105) 'Relative capacities must not be labelled mWh or clamped at 100 percent.'

New-FixtureState
$script:Static[0].PSObject.Properties.Remove('Capabilities');$script:Static[0].PSObject.Properties.Remove('SerialNumber')
$script:Cycles[0].CycleCount=[uint32]0
$unknown=Get-OptimusHardwareHealth
Assert-True ($unknown.batteries[0].capacity_units -ceq 'native provider units') 'Missing capacity units cannot become mWh.'
Assert-True ($unknown.batteries[0].identity_quality -ceq 'unavailable' -and $unknown.batteries[0].PSObject.Properties.Name -notcontains 'identity_hash') 'A generic ACPI battery slot alone is not stable identity across replacement.'
Assert-True ($unknown.batteries[0].PSObject.Properties.Name -notcontains 'cycle_count') 'An unsupported zero battery cycle counter is unknown.'

New-FixtureState
$script:Full[0].InstanceName='Another battery'
$mismatch=Get-OptimusHardwareHealth
Assert-True ($mismatch.batteries[0].PSObject.Properties.Name -notcontains 'full_charge_vs_design_percent') 'Mismatched battery instances cannot supply a ratio.'

foreach($invalid in @([uint32]0,[uint32]::MaxValue,-1,$true,'1000',1000.0)){
    New-FixtureState;$script:Static[0].DesignedCapacity=$invalid
    $invalidCapacity=Get-OptimusHardwareHealth
    Assert-True ($invalidCapacity.batteries[0].PSObject.Properties.Name -notcontains 'full_charge_vs_design_percent') 'Zero, sentinel, negative, boolean and nonintegral capacities stay unknown.'
}

New-FixtureState
$script:Full+= $script:Full[0];$script:Static+= $script:Static[0]
$ambiguous=Get-OptimusHardwareHealth
Assert-True (@($ambiguous.batteries | Where-Object {$_.PSObject.Properties.Name -contains 'full_charge_vs_design_percent' -or $_.identity_quality -ceq 'stable'}).Count -eq 0) 'Duplicate battery identities cannot be paired or used in history.'

New-FixtureState
$script:Disks[1].UniqueId=$script:Disks[0].UniqueId
$ambiguousDrive=Get-OptimusHardwareHealth
Assert-True (@($ambiguousDrive.storage | Where-Object {$_.identity_quality -ceq 'stable' -or $_.volumes.Count -gt 0}).Count -eq 0) 'Duplicate drive UniqueIds cannot identify history devices or partitions.'

New-FixtureState
$script:Disks[0].UniqueId='00000000-0000-0000-0000-000000000000'
$placeholder=Get-OptimusHardwareHealth
Assert-True ($placeholder.storage[0].identity_quality -ceq 'unavailable') 'Placeholder drive identities cannot merge baseline history.'

New-FixtureState
$script:Failures=@('MSFT_PhysicalDisk')
$storageFailure=Get-OptimusHardwareHealth
Assert-True ($storageFailure.storage.Count -eq 0 -and $storageFailure.capabilities.storage_health -ceq 'unavailable' -and $storageFailure.batteries.Count -eq 1) 'A failed drive query must not prevent battery observation or claim zero installed drives.'
Assert-True (($storageFailure | ConvertTo-Json -Depth 10 -Compress) -notmatch 'Private fixture error') 'Provider exceptions cannot expose private diagnostic strings.'

New-FixtureState
$script:Failures=@('BatteryStaticData')
$batteryFailure=Get-OptimusHardwareHealth
Assert-True ($batteryFailure.batteries.Count -eq 0 -and $batteryFailure.capabilities.battery_health -ceq 'unavailable' -and $batteryFailure.storage.Count -eq 2) 'A failed battery query is unavailable, not no battery or healthy.'

New-FixtureState
$script:Failures=@('BatteryStaticData')
$script:BatteryFallback=@([pscustomobject]@{Name='Fixture CIM battery';DeviceID='private-fallback-device-id';DesignCapacity=[uint32]1000;FullChargeCapacity=[uint32]750;EstimatedChargeRemaining=100})
$fallback=Get-OptimusHardwareHealth
$receipts.fallback=$fallback
Assert-True ($fallback.batteries[0].source -ceq 'Win32_Battery' -and $fallback.batteries[0].full_charge_vs_design_percent -eq 75 -and $fallback.batteries[0].capacity_units -ceq 'mWh') 'A separate native battery fallback can report exact mWh capacities, not current charge.'
Assert-True ($fallback.batteries[0].identity_quality -ceq 'unavailable' -and ($fallback | ConvertTo-Json -Depth 10 -Compress) -notmatch 'private-fallback') 'Fallback device slots cannot be treated as stable physical battery identities.'

New-FixtureState
$script:Failures=@('BatteryStaticData')
$script:BatteryFallback=@([pscustomobject]@{Name='Fixture CIM battery';DesignCapacity=[uint32]0;FullChargeCapacity=[uint32]0;EstimatedChargeRemaining=100})
$unsupportedFallback=Get-OptimusHardwareHealth
$receipts.unsupported_fallback=$unsupportedFallback
Assert-True ($unsupportedFallback.batteries.Count -eq 1 -and $unsupportedFallback.batteries[0].PSObject.Properties.Name -notcontains 'full_charge_vs_design_percent') 'Unsupported zero CIM capacities must not turn current charge into a fake health percentage.'

New-FixtureState
$cancelled=Get-OptimusHardwareHealth -CancelRequested {$true}
Assert-True ($cancelled.status -ceq 'cancelled' -and $script:Calls.Count -eq 0) 'Cancellation before scanning must start no native query.'

New-FixtureState
$budget=Get-OptimusHardwareHealth -BudgetSeconds 2
Assert-True ($budget.capabilities.storage_health -ceq 'unavailable' -and $budget.batteries.Count -eq 1) 'A small storage budget must fail forward to the independent battery channel.'

New-FixtureState
$diagnostics=Get-OptimusSystemDiagnostics
Assert-True (@($script:Calls | Where-Object class -eq 'MSFT_PhysicalDisk').Count -eq 1 -and @($script:Calls | Where-Object class -eq 'BatteryStaticData').Count -eq 1) 'General system diagnostics must reuse exactly one focused health collection.'
Assert-True ($diagnostics.memory.percent -eq 75 -and $diagnostics.batteries[0].full_charge_vs_design_percent -eq 80) 'Existing diagnostic fields remain compatible.'

[IO.File]::WriteAllText($ReceiptsPath,($receipts | ConvertTo-Json -Depth 12),[Text.UTF8Encoding]::new($false))
[pscustomobject]@{Result='PASS';Assertions=$script:Assertions;RealQueries=0;RealTests=0;RealRepairs=0;RawSerialsExported=0} | ConvertTo-Json -Compress
