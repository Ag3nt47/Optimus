#requires -Version 5.1
# Read-only OS facts. Missing provider data remains unavailable, never "healthy".
function Get-OptimusHealthInteger {
    param($Value,[decimal]$Maximum,[switch]$Positive)
    if($Value -isnot [byte] -and $Value -isnot [sbyte] -and $Value -isnot [int16] -and
       $Value -isnot [uint16] -and $Value -isnot [int] -and $Value -isnot [uint32] -and
       $Value -isnot [long] -and $Value -isnot [uint64]){return $null}
    if([decimal]$Value -lt 0 -or [decimal]$Value -gt $Maximum -or ($Positive -and $Value -eq 0)){return $null}
    return $Value
}

function Get-OptimusHealthText {
    param($Value,[int]$Maximum=160)
    if($Value -isnot [string]){return ''}
    $text=($Value -replace '[\x00-\x1f\x7f]',' ').Trim()
    return $text.Substring(0,[Math]::Min($Maximum,$text.Length))
}

function Test-OptimusHealthIdentity {
    param($Value)
    if($Value -isnot [string] -or $Value.Length -lt 4 -or $Value.Length -gt 4096){return $false}
    $compact=($Value.ToLowerInvariant() -replace '[^a-z0-9]','')
    return ($compact.Length -ge 4 -and $compact -notmatch '^(.)\1+$' -and $compact -notin @(
        'unknown','none','null','na','notavailable','notapplicable','defaultstring','serialnumber',
        'tobefilledbyoem','tobefilledbyoemstring') -and $Value -notmatch '[\x00-\x1f\x7f]')
}

function Get-OptimusHealthIdentityHash {
    param([string]$Kind,[string[]]$Parts)
    $sha=[Security.Cryptography.SHA256]::Create()
    try {
        $bytes=[Text.Encoding]::UTF8.GetBytes($Kind+[char]0+($Parts -join [char]0))
        return ([BitConverter]::ToString($sha.ComputeHash($bytes))).Replace('-','').ToLowerInvariant()
    } finally {$sha.Dispose()}
}

function Get-OptimusHardwareHealth {
    param([scriptblock]$CancelRequested,[ValidateRange(2,30)][double]$BudgetSeconds=30)
    $errors=New-Object 'System.Collections.Generic.List[string]'
    $storage=New-Object 'System.Collections.Generic.List[object]'
    $batteries=New-Object 'System.Collections.Generic.List[object]'
    $capabilities=[ordered]@{storage_health='unavailable';battery_health='unavailable';volume_mapping='unavailable'}
    $state=@{cancelled=$false}
    $clock=[Diagnostics.Stopwatch]::StartNew()
    $stopped={
        if($null -ne $CancelRequested){
            try {$requested=& $CancelRequested;if($requested -isnot [bool] -or $requested){$state.cancelled=$true}}
            catch {$state.cancelled=$true}
        }
        return $state.cancelled
    }
    $timeout={param([double]$Until)
        if(& $stopped){throw [OperationCanceledException]::new('Hardware health observation cancelled.')}
        $remaining=$Until-$clock.Elapsed.TotalSeconds
        if($remaining -lt 1){throw [TimeoutException]::new('Hardware health observation budget reached.')}
        return [uint32][Math]::Min(5,[Math]::Floor($remaining))
    }
    $query={param([string]$Class,[string]$Namespace,[int]$Limit,[double]$Until)
        $seconds=& $timeout $Until
        $rows=@(Get-CimInstance -Namespace $Namespace -ClassName $Class -OperationTimeoutSec $seconds -ErrorAction Stop |
            Select-Object -First ($Limit+1))
        if($rows.Count -gt $Limit){throw [IO.InvalidDataException]::new('Hardware provider inventory bound exceeded.')}
        if(& $stopped){throw [OperationCanceledException]::new('Hardware health observation cancelled.')}
        return $rows
    }
    $namespace='root/Microsoft/Windows/Storage'
    $storageUntil=[Math]::Max(0,[Math]::Min(20,$BudgetSeconds-10))
    $disks=@();$mappedDisks=@();$partitions=@();$mappingAvailable=$false
    try {
        $disks=@(& $query 'MSFT_PhysicalDisk' $namespace 32 $storageUntil)
        $capabilities.storage_health=if($disks.Count){'available_provider_dependent'}else{'not_exposed'}
    } catch {$errors.Add('Physical-drive inventory unavailable ('+$_.Exception.GetType().Name+').')}
    if($disks.Count -and -not (& $stopped)){
        try {
            $mappedDisks=@(& $query 'MSFT_Disk' $namespace 64 $storageUntil)
            $partitions=@(& $query 'MSFT_Partition' $namespace 256 $storageUntil)
            $mappingAvailable=$true;$capabilities.volume_mapping='available_provider_dependent'
        } catch {$errors.Add('Physical-drive volume mapping unavailable ('+$_.Exception.GetType().Name+').')}
    }
    foreach($disk in $disks){
        $device=Get-OptimusHealthText $disk.DeviceId 128
        $row=[ordered]@{name=(Get-OptimusHealthText $disk.FriendlyName);device_id=$device;
            operational_status=@();reliability_available=$false;volumes=@();volume_mapping='unavailable';
            identity_quality='unavailable';identity_source='unavailable'}
        foreach($entry in @(@('MediaType','media_type',65534),@('BusType','bus_type',65534),
                @('HealthStatus','health_status',65534),@('Size','size_bytes',[decimal]'18446744073709551614'))){
            $value=Get-OptimusHealthInteger $disk.($entry[0]) $entry[2] -Positive:($entry[0] -ceq 'Size')
            if($null -ne $value){$row[$entry[1]]=$value}
        }
        $row.operational_status=@(foreach($value in @($disk.OperationalStatus)){
            $valid=Get-OptimusHealthInteger $value 65534
            if($null -ne $valid){$valid}
        })
        $unique=$disk.UniqueId
        $uniquePeers=@($disks | Where-Object {$_.UniqueId -is [string] -and $unique -is [string] -and $_.UniqueId.Trim() -ieq $unique.Trim()})
        if((Test-OptimusHealthIdentity $unique) -and $uniquePeers.Count -eq 1){
            $row.identity_hash=Get-OptimusHealthIdentityHash 'windows:physical-disk:MSFT_PhysicalDisk.UniqueId' @($unique.Trim().ToUpperInvariant())
            $row.identity_source='MSFT_PhysicalDisk.UniqueId';$row.identity_quality='stable'
            if($mappingAvailable){
                $matches=@($mappedDisks | Where-Object {$_.UniqueId -is [string] -and $_.UniqueId.Trim() -ieq $unique.Trim()})
                if($matches.Count -eq 1){
                    $number=Get-OptimusHealthInteger $matches[0].Number 4294967294
                    $physicalSize=Get-OptimusHealthInteger $disk.Size ([decimal]'18446744073709551614') -Positive
                    $logicalSize=Get-OptimusHealthInteger $matches[0].Size ([decimal]'18446744073709551614') -Positive
                    if($null -ne $number -and $null -ne $physicalSize -and $physicalSize -eq $logicalSize){
                        $row.disk_number=$number;$row.volume_mapping='verified_unique_id'
                        $row.volumes=@($partitions | Where-Object {
                            $partNumber=Get-OptimusHealthInteger $_.DiskNumber 4294967294
                            $null -ne $partNumber -and $partNumber -eq $number -and
                            ([string]$_.DriveLetter -cmatch '^[A-Za-z]$')
                        } | ForEach-Object {([string]$_.DriveLetter).ToUpperInvariant()+':'} | Sort-Object -Unique)
                    }
                }
            }
        }
        try {
            $seconds=& $timeout $storageUntil
            # The explicit physical-disk association fixes DeviceId context.
            # A bulk counter DeviceId can instead refer to MSFT_Disk.Number.
            $counters=@(Get-CimAssociatedInstance -InputObject $disk -Association MSFT_PhysicalDiskToStorageReliabilityCounter `
                -ResultClassName MSFT_StorageReliabilityCounter -OperationTimeoutSec $seconds -ErrorAction Stop | Select-Object -First 2)
            $devicePeers=@($disks | Where-Object {[string]$_.DeviceId -ceq $device})
            if($counters.Count -eq 1 -and $device -and $devicePeers.Count -eq 1 -and [string]$counters[0].DeviceId -ceq $device){
                $counter=$counters[0]
                foreach($entry in @(@('Temperature','temperature_c',254,$true),@('TemperatureMax','maximum_operating_temperature_c',254,$true),
                    @('Wear','wear_percent_used',254,$false),@('PowerOnHours','power_on_hours',65534,$false),
                    @('ReadErrorsUncorrected','uncorrected_read_errors',[decimal]'18446744073709551614',$false),
                    @('WriteErrorsUncorrected','uncorrected_write_errors',[decimal]'18446744073709551614',$false))){
                    $value=Get-OptimusHealthInteger $counter.($entry[0]) $entry[2] -Positive:($entry[3])
                    if($null -ne $value){$row[$entry[1]]=$value;$row.reliability_available=$true}
                }
                $row.reliability_source='MSFT_PhysicalDiskToStorageReliabilityCounter'
            }
        } catch {$errors.Add('Physical-drive reliability unavailable ('+$_.Exception.GetType().Name+').')}
        $row.note='Physical-device provider observations, not a surface test or guarantee. Missing/sentinel counters stay unknown; wear is percent used, not remaining life. Raw serials and unique identifiers are not exported.'
        $storage.Add([pscustomobject]$row)
    }
    $static=@();$full=@();$cycles=@()
    if(-not (& $stopped)){
        try {
            $static=@(& $query 'BatteryStaticData' 'root/wmi' 16 $BudgetSeconds)
            $capabilities.battery_health=if($static.Count){'available_provider_dependent'}else{'not_exposed'}
        } catch {$errors.Add('Battery inventory unavailable ('+$_.Exception.GetType().Name+').')}
        if($static.Count){
            try {$full=@(& $query 'BatteryFullChargedCapacity' 'root/wmi' 16 $BudgetSeconds)}
            catch {$errors.Add('Battery full-charge capacity unavailable ('+$_.Exception.GetType().Name+').')}
            try {$cycles=@(& $query 'BatteryCycleCount' 'root/wmi' 16 $BudgetSeconds)}
            catch {$errors.Add('Battery cycle count unavailable ('+$_.Exception.GetType().Name+').')}
        }
    }
    foreach($battery in $static){
        if($battery.Active -is [bool] -and -not $battery.Active){continue}
        $row=[ordered]@{name='Battery';capacity_units='native provider units';identity_quality='unavailable';identity_source='unavailable'}
        $name=Get-OptimusHealthText $battery.DeviceName
        if($name){$row.name=$name}
        $design=Get-OptimusHealthInteger $battery.DesignedCapacity 4294967294 -Positive
        if($null -ne $design){$row.design_capacity=$design}
        $flags=Get-OptimusHealthInteger $battery.Capabilities 4294967294
        if($null -ne $flags){$row.capacity_units=if(([long]$flags -band 1073741824) -ne 0){'relative provider units'}else{'mWh'}}
        $instance=$battery.InstanceName
        $peers=@($static | Where-Object {$_.InstanceName -is [string] -and $instance -is [string] -and $_.InstanceName -ceq $instance})
        if($instance -is [string] -and $instance.Length -gt 0 -and $instance.Length -le 4096 -and $peers.Count -eq 1){
            $matches=@($full | Where-Object {$_.InstanceName -is [string] -and $_.InstanceName -ceq $instance})
            if($matches.Count -eq 1){
                $charged=Get-OptimusHealthInteger $matches[0].FullChargedCapacity 4294967294 -Positive
                if($null -ne $charged){$row.full_charge_capacity=$charged}
                if($null -ne $design -and $null -ne $charged){$row.full_charge_vs_design_percent=[Math]::Round(100.0*[double]$charged/[double]$design,2)}
            }
            $cycle=@($cycles | Where-Object {$_.InstanceName -is [string] -and $_.InstanceName -ceq $instance})
            if($cycle.Count -eq 1){
                $count=Get-OptimusHealthInteger $cycle[0].CycleCount 4294967294 -Positive
                if($null -ne $count){$row.cycle_count=$count}
            }
            $serial=$battery.SerialNumber
            $serialPeers=@($static | Where-Object {$_.SerialNumber -is [string] -and $serial -is [string] -and $_.SerialNumber.Trim() -ceq $serial.Trim()})
            if((Test-OptimusHealthIdentity $serial) -and (Test-OptimusHealthIdentity $instance) -and $serialPeers.Count -eq 1){
                $row.identity_hash=Get-OptimusHealthIdentityHash 'windows:battery:instance-and-serial' @(
                    $instance,$serial.Trim(),(Get-OptimusHealthText $battery.ManufactureName),(Get-OptimusHealthText $battery.DeviceName))
                $row.identity_source='BatteryStaticData.InstanceName+SerialNumber';$row.identity_quality='stable'
            }
        }
        $row.note='Full-charge/design is a capacity estimate, not current charge or a runtime guarantee. The ratio uses one exact native battery instance; unsupported capacities/cycles stay unknown.'
        $batteries.Add([pscustomobject]$row)
    }
    if(-not $static.Count -and $capabilities.battery_health -ceq 'unavailable' -and -not (& $stopped)){
        try {
            # The separate CIM_Battery schema explicitly defines these two
            # capacities in mWh. Never mix its rows with unmatched root/WMI
            # battery instances or infer physical identity from a battery slot.
            $fallback=@(& $query 'Win32_Battery' 'root/cimv2' 16 $BudgetSeconds)
            foreach($battery in $fallback){
                $name=Get-OptimusHealthText $battery.Name
                $row=[ordered]@{name=if($name){$name}else{'Battery'};source='Win32_Battery';capacity_units='mWh';
                    identity_quality='unavailable';identity_source='unavailable'}
                $design=Get-OptimusHealthInteger $battery.DesignCapacity 4294967294 -Positive
                $charged=Get-OptimusHealthInteger $battery.FullChargeCapacity 4294967294 -Positive
                if($null -ne $design){$row.design_capacity=$design}
                if($null -ne $charged){$row.full_charge_capacity=$charged}
                if($null -ne $design -and $null -ne $charged){$row.full_charge_vs_design_percent=[Math]::Round(100.0*[double]$charged/[double]$design,2)}
                $row.note='CIM_Battery fallback observations. Zero or unsupported capacities stay unknown; current charge is not battery health. No stable physical battery identity is claimed.'
                $batteries.Add([pscustomobject]$row)
            }
            if($fallback.Count){$capabilities.battery_health='available_provider_dependent'}
        } catch {$errors.Add('CIM battery fallback unavailable ('+$_.Exception.GetType().Name+').')}
    }
    $status=if($state.cancelled){'cancelled'}elseif($errors.Count){if($storage.Count -or $batteries.Count){'partial'}else{'unavailable'}}else{'completed'}
    [pscustomobject]@{schema=1;platform='Windows';read_only=$true;captured_at=[DateTime]::UtcNow.ToString('o');status=$status;
        cancelled=$state.cancelled;storage=@($storage.ToArray());batteries=@($batteries.ToArray());
        capabilities=[pscustomobject]$capabilities;errors=@($errors.ToArray());
        duration_seconds=[Math]::Round($clock.Elapsed.TotalSeconds,3)}
}

function Get-OptimusSystemDiagnostics {
    $errors=New-Object 'System.Collections.Generic.List[string]'
    $capabilities=[ordered]@{}
    $storage=New-Object 'System.Collections.Generic.List[object]'
    $batteries=New-Object 'System.Collections.Generic.List[object]'
    $memory=[ordered]@{}
    $pagefile=[ordered]@{}
    $boot=[ordered]@{source='Microsoft-Windows-Diagnostics-Performance/Operational';events=@()}
    try {
        $os=Get-CimInstance Win32_OperatingSystem -OperationTimeoutSec 5 -ErrorAction Stop
        $memory.total_bytes=[long]$os.TotalVisibleMemorySize*1024
        $memory.available_bytes=[long]$os.FreePhysicalMemory*1024
        if($memory.total_bytes -gt 0){$memory.percent=100.0*(1.0-$memory.available_bytes/[double]$memory.total_bytes)}
        $memory.total_virtual_bytes=[long]$os.TotalVirtualMemorySize*1024
        $memory.free_virtual_bytes=[long]$os.FreeVirtualMemory*1024
        $capabilities.memory='available'
    } catch {$errors.Add('Memory facts: '+$_.Exception.Message);$capabilities.memory='unavailable'}
    try {
        $computer=Get-CimInstance Win32_ComputerSystem -OperationTimeoutSec 5 -ErrorAction Stop
        if($computer.AutomaticManagedPagefile -is [bool]){$pagefile.automatic_managed=$computer.AutomaticManagedPagefile}
        $pagefile.usage=@(Get-CimInstance Win32_PageFileUsage -OperationTimeoutSec 5 -ErrorAction Stop | Select-Object -First 32 | ForEach-Object {
            [pscustomobject]@{allocated_mb=$_.AllocatedBaseSize;current_mb=$_.CurrentUsage;peak_mb=$_.PeakUsage}
        })
        $pagefile.configuration=@(Get-CimInstance Win32_PageFileSetting -OperationTimeoutSec 5 -ErrorAction Stop | Select-Object -First 32 | ForEach-Object {
            [pscustomobject]@{initial_mb=$_.InitialSize;maximum_mb=$_.MaximumSize}
        })
        $pagefile.note='Read-only configuration; no pagefile resizing or disabling.'
        $capabilities.pagefile='available'
    } catch {$errors.Add('Pagefile facts: '+$_.Exception.Message);$capabilities.pagefile='partial_or_unavailable'}
    $health=Get-OptimusHardwareHealth
    foreach($row in $health.storage){$storage.Add($row)}
    foreach($row in $health.batteries){$batteries.Add($row)}
    foreach($message in $health.errors){$errors.Add($message)}
    foreach($entry in $health.capabilities.PSObject.Properties){$capabilities[$entry.Name]=$entry.Value}
    try {
        $events=@(Get-WinEvent -FilterHashtable @{LogName='Microsoft-Windows-Diagnostics-Performance/Operational';Id=100,101,102,103;StartTime=(Get-Date).AddDays(-14)} -MaxEvents 50 -ErrorAction Stop)
        $boot.events=@(foreach($event in $events) {
            $xml=[xml]$event.ToXml()
            $data=[ordered]@{}
            foreach($field in $xml.Event.EventData.Data) {
                if([string]$field.Name -in @('BootTime','MainPathBootTime','BootPostBootTime','BootIsDegradation','TotalTime','DegradationTime','Name','FriendlyName')) {
                    $data[[string]$field.Name]=[string]$field.'#text'
                }
            }
            [pscustomobject]@{id=$event.Id;time_utc=$event.TimeCreated.ToUniversalTime().ToString('o');data=[pscustomobject]$data}
        })
        $boot.note='Recent Windows boot and startup degradation events. Event times are milliseconds; app delay events are evidence to review, not proof an app is unnecessary.'
        $capabilities.boot_timing='available'
    } catch {$capabilities.boot_timing='unavailable_or_no_events';$boot.note='No accessible matching recent events. This does not establish a fast or problem-free boot.'}
    [pscustomobject]@{schema=1;read_only=$true;storage=@($storage.ToArray());batteries=@($batteries.ToArray());
        memory=[pscustomobject]$memory;pagefile=[pscustomobject]$pagefile;boot=[pscustomobject]$boot;
        capabilities=[pscustomobject]$capabilities;errors=@($errors.ToArray())}
}

function Write-OptimusIntegrityReceipt {
    param([Parameter(Mandatory=$true)][string]$Path,[Parameter(Mandatory=$true)]$Value)
    $directory=[IO.Path]::GetDirectoryName($Path)
    for($part=$directory;$part;$part=[IO.Path]::GetDirectoryName($part)) {
        if(([IO.File]::GetAttributes($part) -band [IO.FileAttributes]::ReparsePoint) -ne 0){throw 'Indirect integrity report ancestry is refused.'}
        if($part -eq [IO.Path]::GetPathRoot($part)){break}
    }
    if([IO.File]::Exists($Path) -and ([IO.File]::GetAttributes($Path) -band [IO.FileAttributes]::ReparsePoint) -ne 0){throw 'Indirect integrity report file is refused.'}
    $temporary=$Path+'.'+[Guid]::NewGuid().ToString('N')+'.tmp'
    $raw=[Text.UTF8Encoding]::new($false).GetBytes(($Value | ConvertTo-Json -Depth 10))
    $stream=[IO.FileStream]::new($temporary,[IO.FileMode]::CreateNew,[IO.FileAccess]::Write,[IO.FileShare]::None)
    try {$stream.Write($raw,0,$raw.Length);$stream.Flush($true)}finally {$stream.Dispose()}
    if([IO.File]::Exists($Path)){[IO.File]::Replace($temporary,$Path,[NullString]::Value)}else {[IO.File]::Move($temporary,$Path)}
}

function Test-OptimusIntegrityAdministrator {
    $identity=[Security.Principal.WindowsIdentity]::GetCurrent()
    $principal=New-Object Security.Principal.WindowsPrincipal($identity)
    return $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
}

function Invoke-OptimusWindowsIntegrity {
    param([bool]$Repair=$false,[bool]$Approved=$false,[Parameter(Mandatory=$true)][string]$DataRoot,[string]$OperationId)
    if($Repair -and -not $Approved){throw 'Windows repair requires separate explicit approval.'}
    if($OperationId -cnotmatch '^[a-f0-9]{32}$'){throw 'An exact owned integrity operation identity is required.'}
    $identity=[Security.Principal.WindowsIdentity]::GetCurrent()
    $owner='sid:'+$identity.User.Value
    $intentPath=Join-Path (Join-Path $DataRoot 'integrity-operations') ($OperationId+'.json')
    $intentInfo=Get-Item -LiteralPath $intentPath -Force -ErrorAction Stop
    if($intentInfo.PSIsContainer -or $intentInfo.Length -gt 65536 -or ($intentInfo.Attributes -band [IO.FileAttributes]::ReparsePoint)){
        throw 'A bounded ordinary integrity intent is required.'
    }
    $intent=ConvertFrom-Json -InputObject ([IO.File]::ReadAllText($intentPath))
    if($intent.schema -isnot [int] -or $intent.schema -ne 1 -or $intent.kind -cne 'windows-integrity-intent-v1' -or $intent.id -cne $OperationId -or
        $intent.owner -cne $owner -or $intent.repair -isnot [bool] -or $intent.repair -ne $Repair -or $intent.status -cne 'prepared'){
        throw 'A durable matching user-owned integrity intent is required before dispatch.'
    }
    $directory=Join-Path (Join-Path $DataRoot 'integrity-diagnostics') $OperationId
    [void][IO.Directory]::CreateDirectory($directory)
    $items=New-Object 'System.Collections.Generic.List[object]'
    $started=[DateTime]::UtcNow.ToString('o')
    $inFlight=$false
    $nativeStarted=$false
    $status='completed_review_native_logs'
    $known=$true
    $errorText=$null
    $restart=$false
    try {
        if(-not (Test-OptimusIntegrityAdministrator)) {
            $status='requires_administrator'
        } else {
            $system=[Environment]::GetFolderPath('System')
            $steps=if($Repair){@(@('restore_health','dism.exe','/Online /Cleanup-Image /RestoreHealth /NoRestart'),@('repair_files','sfc.exe','/scannow'))}
                else {@(@('check_health','dism.exe','/Online /Cleanup-Image /CheckHealth /NoRestart'),@('scan_health','dism.exe','/Online /Cleanup-Image /ScanHealth /NoRestart'),@('verify_files','sfc.exe','/verifyonly'))}
            foreach($step in $steps) {
                $tool=Join-Path $system $step[1]
                if(-not(Test-Path -LiteralPath $tool -PathType Leaf)){throw 'The supported Windows servicing executable is unavailable.'}
                $stdout=Join-Path $directory ($step[0]+'.stdout.log')
                $stderr=Join-Path $directory ($step[0]+'.stderr.log')
                $prepared=[ordered]@{schema=1;step=$step[0];repair=$Repair;status='prepared';started_utc=[DateTime]::UtcNow.ToString('o');executable=$tool;arguments=$step[2]}
                $journal=Join-Path $directory ($step[0]+'.json')
                Write-OptimusIntegrityReceipt -Path $journal -Value $prepared
                # Windows Start-Process -Wait observes the launched process
                # tree. Caller timeout/cancel never terminates servicing.
                $inFlight=$true
                $nativeStarted=$true
                $process=Start-Process -FilePath $tool -ArgumentList $step[2] -WindowStyle Hidden -Wait -PassThru -RedirectStandardOutput $stdout -RedirectStandardError $stderr
                $process.Refresh()
                $code=$process.ExitCode
                if(-not $process.HasExited -or $code -isnot [int]){throw 'Native servicing exit could not be established.'}
                $inFlight=$false
                $prepared.status='exited';$prepared.exit_code=$code;$prepared.finished_utc=[DateTime]::UtcNow.ToString('o')
                $prepared.stdout=$stdout;$prepared.stderr=$stderr
                $items.Add([pscustomobject]$prepared)
                Write-OptimusIntegrityReceipt -Path $journal -Value $prepared
                if($code -eq 3010){$status='restart_required';$restart=$true;break}
                if($code -ne 0){$status='needs_review';break}
            }
        }
    } catch {
        $known=-not $inFlight
        $status=if($known){'preflight_failed'}else{'outcome_unverified'}
        $errorText=$_.Exception.Message
    }
    $result=[pscustomobject]@{schema=1;kind='windows-integrity-native-v1';id=$OperationId;owner=$owner;repair=$Repair;
        read_only=(-not $Repair);status=$status;items=@($items.ToArray());report_directory=$directory;
        started_utc=$started;finished_utc=[DateTime]::UtcNow.ToString('o');native_started=$nativeStarted;
        outcomeKnown=$known;restart_needed=$restart;error=$errorText;automatic_restart=$false}
    Write-OptimusIntegrityReceipt -Path (Join-Path $directory 'completion.json') -Value $result
    $result
}
