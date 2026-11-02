#requires -Version 5.1
# No actual provider, hardware test, repair, installer or process launch runs here.
param([Parameter(Mandatory=$true)][string]$ReceiptsPath)
$ErrorActionPreference='Stop'
. (Join-Path $PSScriptRoot '../src/optimus/engine/SystemDiagnostics.ps1')
$script:NativeInboxBatteryReport=(Get-Command Get-OptimusInboxBatteryReport).ScriptBlock
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
        'Drive-Unique-1234'=@([pscustomobject]@{DeviceId='12';Wear=[byte]0;Temperature=[byte]31;TemperatureMax=[byte]70;PowerOnHours=[uint16]120;
            ReadErrorsUncorrected=[uint64]0;WriteErrorsUncorrected=[uint64]0;ReadErrorsTotal=[uint64]2;ReadErrorsCorrected=[uint64]2;
            WriteErrorsTotal=[uint64]0;WriteErrorsCorrected=[uint64]0;StartStopCycleCount=[uint32]20;StartStopCycleCountMax=[uint32]600000;
            LoadUnloadCycleCount=[uint32]30;LoadUnloadCycleCountMax=[uint32]600000;ReadLatencyMax=[uint64]300;WriteLatencyMax=[uint64]0;
            FlushLatencyMax=[uint64]25;ManufactureDate='2018-W42'})
        'Drive-Unique-5678'=@([pscustomobject]@{DeviceId='2';Wear=[byte]120;Temperature=[byte]45;PowerOnHours=[uint16]240;ReadErrorsUncorrected=[uint64]2})}
    $script:Static=@([pscustomobject]@{InstanceName='FixtureBattery-instance-0';Active=$true;DesignedCapacity=[uint32]1000;
        Capabilities=[uint32]0;SerialNumber='do-not-export-battery-serial';DeviceName='Fixture Battery';ManufactureName='Fixture Manufacturer'})
    $script:Full=@([pscustomobject]@{InstanceName='FixtureBattery-instance-0';FullChargedCapacity=[uint32]800})
    $script:Cycles=@([pscustomobject]@{InstanceName='FixtureBattery-instance-0';CycleCount=[uint32]20})
    $script:BatteryFallback=@()
    $script:InboxBatteries=@();$script:InboxFailure=$false
}
function Get-OptimusInboxBatteryReport {
    param($CancelRequested,$TimeoutSeconds)
    if($null -ne $CancelRequested -and (& $CancelRequested)){throw [OperationCanceledException]::new('Fake inbox cancelled.')}
    $script:Calls.Add([pscustomobject]@{operation='inbox-battery-fixture';class='powercfg_batteryreport';timeout=$TimeoutSeconds})
    if($script:InboxFailure){throw [TimeoutException]::new('Fake inbox timeout.')}
    return $script:InboxBatteries
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
Assert-True ($good.storage[0].read_errors_total -eq 2 -and $good.storage[0].corrected_read_errors -eq 2 -and $good.storage[0].corrected_write_errors -eq 0) 'Corrected and total errors are independent counters, not physical health percentages.'
Assert-True ($good.storage[0].start_stop_cycles -eq 20 -and $good.storage[0].rated_start_stop_cycles -eq 600000 -and $good.storage[0].load_unload_cycles -eq 30) 'Mechanical cycle counters and their native rated limits remain distinct.'
Assert-True ($good.storage[0].maximum_read_latency_ms -eq 300 -and $good.storage[0].maximum_write_latency_ms -eq 0 -and $good.storage[0].maximum_flush_latency_ms -eq 25) 'Native maximum latency units remain milliseconds, including real zero.'
Assert-True ($good.storage[0].manufacture_year_week -ceq '2018-W42') 'A reported manufacture year/week is retained without inventing age.'
Assert-True ($good.storage[0].wear_percent_used_source -ceq 'MSFT_PhysicalDiskToStorageReliabilityCounter.Wear' -and $good.storage[0].counter_capabilities.wear_percent_used -ceq 'reported') 'Raw wear counters retain exact provenance without claiming verified endurance.'
Assert-True ($good.storage[1].counter_capabilities.wear_percent_used -ceq 'not_applicable' -and $good.storage[1].wear_percent_used -eq 120) 'HDD generic wear is explicitly not an applicable life metric while the raw provider fact remains visible.'
Assert-True ($good.storage[1].counter_capabilities.uncorrected_write_errors -ceq 'not_reported') 'Missing write errors cannot become zero or a healthy result.'
Assert-True (@($script:Calls | Where-Object operation -eq 'inbox-battery-fixture').Count -eq 0) 'Complete native battery capacities must avoid an extra inbox report process.'
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
$counter.ReadErrorsTotal=[uint64]::MaxValue;$counter.ReadErrorsCorrected=[uint64]::MaxValue
$counter.WriteErrorsTotal=[uint64]::MaxValue;$counter.WriteErrorsCorrected=[uint64]::MaxValue
$counter.StartStopCycleCount=[uint32]::MaxValue;$counter.StartStopCycleCountMax=[uint32]::MaxValue
$counter.LoadUnloadCycleCount=[uint32]::MaxValue;$counter.LoadUnloadCycleCountMax=[uint32]::MaxValue
$counter.ReadLatencyMax=[uint64]::MaxValue;$counter.WriteLatencyMax=[uint64]::MaxValue;$counter.FlushLatencyMax=[uint64]::MaxValue
$counter.ManufactureDate="private`ninvalid"
$script:Disks[0].HealthStatus=$true
$sentinel=Get-OptimusHardwareHealth
$receipts.sentinels=$sentinel
Assert-True (-not $sentinel.storage[0].reliability_available) 'Missing and all-ones counters must stay unavailable.'
foreach($field in @('temperature_c','maximum_operating_temperature_c','wear_percent_used','power_on_hours','uncorrected_read_errors','health_status')){
    Assert-True ($sentinel.storage[0].PSObject.Properties.Name -notcontains $field) ('Unsupported or malformed fact cannot become zero: '+$field)
}
Assert-True ($sentinel.storage[0].counter_capabilities.temperature_c -ceq 'invalid_or_sentinel' -and $sentinel.storage[0].counter_capabilities.uncorrected_write_errors -ceq 'not_reported') 'Per-sensor capability separates unsupported values from reported zero.'
Assert-True ($sentinel.storage[0].PSObject.Properties.Name -notcontains 'manufacture_year_week') 'Malformed manufacture strings are omitted without age guesses.'

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

$batteryXml=@'
<BatteryReport><Batteries><Battery><Id>Fixture battery model</Id><Manufacturer>Fixture cells</Manufacturer><SerialNumber>private-report-serial</SerialNumber><DesignCapacity>1000</DesignCapacity><FullChargeCapacity>700</FullChargeCapacity><CycleCount>30</CycleCount><RelativeCapacity>false</RelativeCapacity></Battery></Batteries><History><Battery><DesignCapacity>1</DesignCapacity><FullChargeCapacity>999</FullChargeCapacity></Battery></History><ComputerName>private-computer-name</ComputerName></BatteryReport>
'@
$inbox=@(ConvertFrom-OptimusBatteryReport $batteryXml)
Assert-True ($inbox.Count -eq 1 -and $inbox[0].full_charge_vs_design_percent -eq 70 -and $inbox[0].cycle_count -eq 30) 'Only installed-battery native capacity fields supply a ratio, never historical usage.'
Assert-True ($inbox[0].capacity_units -ceq 'mWh' -and $inbox[0].identity_quality -ceq 'stable') 'A confirmed absolute-unit flag and unique firmware identity remain factual.'
Assert-True (($inbox | ConvertTo-Json -Depth 10 -Compress) -notmatch 'private-report-serial|private-computer-name|Fixture battery model') 'Private report serials, host details and raw battery IDs are never exported.'
$receipts.inbox=$inbox
New-FixtureState;$script:Failures=@('BatteryStaticData');$script:InboxBatteries=$inbox
$inboxFallback=Get-OptimusHardwareHealth
Assert-True ($inboxFallback.batteries.Count -eq 1 -and $inboxFallback.batteries[0].source -ceq 'powercfg_batteryreport' -and $inboxFallback.capabilities.battery_inbox_report -ceq 'used') 'An independent inbox inventory can recover capacities without joining by WMI slot.'
$normalInboxCall=@($script:Calls | Where-Object operation -eq 'inbox-battery-fixture')[0]
Assert-True ($normalInboxCall.timeout -gt 5 -and $normalInboxCall.timeout -le 12 -and $inboxFallback.force_refresh -eq $false) 'Automatic inbox fallback allows12seconds inside the remaining30second total budget.'
$receipts.inbox_fallback=$inboxFallback
$relativeInbox=@(ConvertFrom-OptimusBatteryReport ($batteryXml.Replace('<RelativeCapacity>false</RelativeCapacity>','<RelativeCapacity>true</RelativeCapacity>')))
Assert-True ($relativeInbox[0].capacity_units -ceq 'relative provider units' -and $relativeInbox[0].full_charge_vs_design_percent -eq 70) 'Relative report capacities retain their ratio without a false mWh label.'
$unknownUnit=@(ConvertFrom-OptimusBatteryReport ($batteryXml.Replace('<RelativeCapacity>false</RelativeCapacity>','')))
Assert-True ($unknownUnit[0].capacity_units -ceq 'native provider units') 'An absent relative-capacity flag cannot establish absolute units.'
$zeroInbox=@(ConvertFrom-OptimusBatteryReport ($batteryXml.Replace('<DesignCapacity>1000</DesignCapacity>','<DesignCapacity>0</DesignCapacity>').Replace('<CycleCount>30</CycleCount>','<CycleCount>0</CycleCount>')))
Assert-True ($zeroInbox[0].PSObject.Properties.Name -notcontains 'full_charge_vs_design_percent' -and $zeroInbox[0].PSObject.Properties.Name -notcontains 'cycle_count') 'Zero unsupported report capacities and cycles remain unknown.'
foreach($xml in @('<!DOCTYPE x [<!ENTITY leak SYSTEM "file:///private">]><BatteryReport>&leak;</BatteryReport>',
    $batteryXml.Replace('<DesignCapacity>1000</DesignCapacity>','<DesignCapacity>1000</DesignCapacity><DesignCapacity>2000</DesignCapacity>'),'<Other/>')){
    $refused=$false;try {ConvertFrom-OptimusBatteryReport $xml | Out-Null}catch{$refused=$true}
    Assert-True $refused 'DTD, ambiguous capacities and unrelated XML cannot supply battery observations.'
}
New-FixtureState;$script:Failures=@('BatteryStaticData');$script:InboxFailure=$true
$inboxFailed=Get-OptimusHardwareHealth
Assert-True ($inboxFailed.capabilities.battery_inbox_report -ceq 'unavailable' -and $inboxFailed.storage.Count -eq 2) 'A timed-out inbox fallback cannot block independent physical-drive observations.'
New-FixtureState;$script:Failures=@('BatteryStaticData');$script:InboxBatteries=$inbox
$forceInbox=Get-OptimusHardwareHealth -ForceRefresh
$forceCall=@($script:Calls | Where-Object operation -eq 'inbox-battery-fixture')[0]
Assert-True ($forceCall.timeout -gt 8 -and $forceCall.timeout -le 20 -and $forceInbox.capabilities.battery_inbox_report -ceq 'used') 'Forced inbox fallback can use20seconds within its remaining60second budget.'
New-FixtureState;$script:Failures=@('BatteryStaticData');$script:InboxBatteries=$inbox
$shortNormal=Get-OptimusHardwareHealth -BudgetSeconds 2
$shortCall=@($script:Calls | Where-Object operation -eq 'inbox-battery-fixture')[0]
Assert-True ($shortCall.timeout -ge 1 -and $shortCall.timeout -le 2) 'A12second automatic allowance cannot exceed the remaining total budget.'
New-FixtureState;$script:Failures=@('BatteryStaticData');$script:InboxBatteries=$inbox
$shortForce=Get-OptimusHardwareHealth -ForceRefresh -BudgetSeconds 3
$shortCall=@($script:Calls | Where-Object operation -eq 'inbox-battery-fixture')[0]
Assert-True ($shortCall.timeout -ge 1 -and $shortCall.timeout -le 3) 'A20second forced allowance cannot exceed an explicit remaining total budget.'
New-FixtureState
$forced=Get-OptimusHardwareHealth -ForceRefresh
Assert-True ($forced.force_refresh -eq $true -and @($script:Calls | Where-Object {$_.timeout -gt 8}).Count -eq 0) 'Explicit force refresh expands only bounded native query budgets.'
New-FixtureState;$script:Static=@()
$noNativeBattery=Get-OptimusHardwareHealth
Assert-True (@($script:Calls | Where-Object operation -eq 'inbox-battery-fixture').Count -eq 0) 'A normal completed empty battery inventory avoids an unnecessary process on desktops.'
$forcedEmptyBattery=Get-OptimusHardwareHealth -ForceRefresh
Assert-True (@($script:Calls | Where-Object operation -eq 'inbox-battery-fixture').Count -eq 1) 'Explicit force refresh can check an otherwise empty provider through the independent inbox report.'

# Exercise the real inbox wrapper with an inert process adapter. Only newly
# created fixture XML is written; no powercfg or other child process is started.
$script:OwnedReports=New-Object 'System.Collections.Generic.List[string]'
$script:ReportProcesses=New-Object 'System.Collections.Generic.List[object]'
$script:ReportMode='success';$script:ReportCancelChecks=0
function New-OptimusBatteryReportProcess {
    param([string]$ReportPath)
    $script:OwnedReports.Add($ReportPath)
    $fake=[pscustomobject]@{ReportPath=$ReportPath;HasExited=$false;ExitCode=0;Killed=$false;Disposed=$false}
    $fake | Add-Member ScriptMethod Start {
        if($script:ReportMode -eq 'start_failure'){throw [IO.IOException]::new('Fake process could not start.')}
        [IO.File]::WriteAllText($this.ReportPath,$batteryXml,[Text.UTF8Encoding]::new($false))
        return $true
    }
    $fake | Add-Member ScriptMethod WaitForExit {param($Milliseconds)
        if($script:ReportMode -in @('timeout','cancel') -and -not $this.Killed){return $false}
        $this.HasExited=$true;return $true
    }
    $fake | Add-Member ScriptMethod Kill {$this.Killed=$true;$this.HasExited=$true}
    $fake | Add-Member ScriptMethod Dispose {$this.Disposed=$true}
    $script:ReportProcesses.Add($fake)
    return $fake
}
$wrapped=@(& $script:NativeInboxBatteryReport -CancelRequested {$false} -TimeoutSeconds 1)
Assert-True ($wrapped.Count -eq 1 -and $wrapped[0].full_charge_vs_design_percent -eq 70) 'The inbox wrapper parses its own completed private report.'
foreach($mode in @('timeout','cancel','start_failure')){
    $script:ReportMode=$mode;$script:ReportCancelChecks=0;$failed=$false
    $callback={
        $script:ReportCancelChecks++
        return ($script:ReportMode -ceq 'cancel' -and $script:ReportCancelChecks -gt 1)
    }
    $watch=[Diagnostics.Stopwatch]::StartNew()
    try {& $script:NativeInboxBatteryReport -CancelRequested $callback -TimeoutSeconds 1 | Out-Null}catch{$failed=$true}
    Assert-True ($failed -and $watch.Elapsed.TotalSeconds -lt 4) 'Mocked timeout, cancellation and start failures are bounded.'
    $last=$script:ReportProcesses[$script:ReportProcesses.Count-1]
    Assert-True ($last.Disposed -and ($mode -ceq 'start_failure' -or $last.Killed)) 'Only the owned read-only report process is stopped and disposed on cancellation/timeout.'
}
foreach($path in $script:OwnedReports){
    Assert-True (-not [IO.File]::Exists($path) -and -not [IO.Directory]::Exists([IO.Path]::GetDirectoryName($path))) 'Every owned private report and empty scratch directory is cleaned on success or failure.'
}
$before=$script:OwnedReports.Count;$failed=$false
try {& $script:NativeInboxBatteryReport -CancelRequested {$true} -TimeoutSeconds 1 | Out-Null}catch{$failed=$true}
Assert-True ($failed -and $script:OwnedReports.Count -eq $before) 'Pre-cancelled inbox fallback starts no report process or scratch allocation.'

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
