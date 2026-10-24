#requires -Version 5.1
# Read-only OS facts. Missing provider data remains unavailable, never "healthy".
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
    try {
        $disks=@(Get-CimInstance -Namespace 'root/Microsoft/Windows/Storage' -ClassName MSFT_PhysicalDisk -OperationTimeoutSec 5 -ErrorAction Stop | Select-Object -First 32)
        $counters=@()
        try {$counters=@(Get-CimInstance -Namespace 'root/Microsoft/Windows/Storage' -ClassName MSFT_StorageReliabilityCounter -OperationTimeoutSec 5 -ErrorAction Stop | Select-Object -First 64)}
        catch {$errors.Add('Storage reliability counters: '+$_.Exception.Message)}
        foreach($disk in $disks) {
            $row=[ordered]@{name=[string]$disk.FriendlyName;device_id=[string]$disk.DeviceId;
                media_type=$disk.MediaType;bus_type=$disk.BusType;health_status=$disk.HealthStatus;
                operational_status=@($disk.OperationalStatus);size_bytes=$disk.Size;reliability_available=$false}
            $matches=@($counters | Where-Object {[string]$_.DeviceId -ceq [string]$disk.DeviceId})
            if($matches.Count -eq 1) {
                $counter=$matches[0]
                foreach($entry in @(@('Temperature','temperature_c'),@('TemperatureMax','maximum_operating_temperature_c'),
                    @('Wear','wear_percent_used'),@('PowerOnHours','power_on_hours'),
                    @('ReadErrorsUncorrected','uncorrected_read_errors'),@('WriteErrorsUncorrected','uncorrected_write_errors'))) {
                    $value=$counter.($entry[0])
                    if($null -ne $value){$row[$entry[1]]=$value;$row.reliability_available=$true}
                }
            }
            $row.note='Provider-reported counters only. A missing counter is unknown; wear 100 means the estimated wear limit is reached. No serial number is exported.'
            $storage.Add([pscustomobject]$row)
        }
        $capabilities.storage_health=if($disks.Count -gt 0){'available_provider_dependent'}else{'not_exposed'}
    } catch {$errors.Add('Storage health: '+$_.Exception.Message);$capabilities.storage_health='unavailable'}
    try {
        $static=@(Get-CimInstance -Namespace root/wmi -ClassName BatteryStaticData -OperationTimeoutSec 5 -ErrorAction Stop | Select-Object -First 16)
        $full=@(Get-CimInstance -Namespace root/wmi -ClassName BatteryFullChargedCapacity -OperationTimeoutSec 5 -ErrorAction Stop | Select-Object -First 16)
        $cycles=@()
        try {$cycles=@(Get-CimInstance -Namespace root/wmi -ClassName BatteryCycleCount -OperationTimeoutSec 5 -ErrorAction Stop | Select-Object -First 16)}catch {}
        foreach($battery in $static) {
            $row=[ordered]@{name='Battery';design_capacity=$battery.DesignedCapacity;capacity_units='native provider units'}
            $matches=@($full | Where-Object InstanceName -CEQ $battery.InstanceName)
            if($matches.Count -eq 1){
                $row.full_charge_capacity=$matches[0].FullChargedCapacity
                if($battery.DesignedCapacity -gt 0){$row.full_charge_vs_design_percent=100.0*$matches[0].FullChargedCapacity/$battery.DesignedCapacity}
            }
            $cycle=@($cycles | Where-Object InstanceName -CEQ $battery.InstanceName)
            if($cycle.Count -eq 1 -and $null -ne $cycle[0].CycleCount){$row.cycle_count=$cycle[0].CycleCount}
            $batteries.Add([pscustomobject]$row)
        }
        $capabilities.battery_health=if($static.Count -gt 0){'available_provider_dependent'}else{'not_exposed'}
    } catch {$capabilities.battery_health='not_exposed';$errors.Add('Battery health is not exposed by the native provider: '+$_.Exception.Message)}
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
