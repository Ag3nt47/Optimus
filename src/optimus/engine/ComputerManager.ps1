#requires -Version 5.1
<# Local inventory, individual confirmed changes and restore records. No automatic tuning at launch. #>
[CmdletBinding()]
param([switch]$Audit,[switch]$Monitor,[ValidateRange(2,120)][int]$Samples=6,[ValidateRange(1,60)][int]$IntervalSeconds=5,[switch]$SelfTest,[switch]$UiSmokeTest,[switch]$LibraryOnly,[string]$OutputPath,[string]$ExpectedUserSid)
$ErrorActionPreference='Stop'
if ($ExpectedUserSid -and [Security.Principal.WindowsIdentity]::GetCurrent().User.Value -ne $ExpectedUserSid) { throw 'Relaunch must use the same Windows account; cancelled to preserve startup and restore ownership.' }
$script:SCMVersion='1.0.0'
$script:SCMHome=$PSScriptRoot
$script:SCMDataRoot=Join-Path $env:LOCALAPPDATA 'SpecializedComputerManager'
if ($LibraryOnly -and $OutputPath) { $script:SCMDataRoot=[IO.Path]::GetFullPath($OutputPath) }
$script:ChangeRoot=Join-Path $script:SCMDataRoot 'Changes'
$script:ReportRoot=Join-Path $script:SCMDataRoot 'Reports'
foreach ($folder in @($script:SCMDataRoot,$script:ChangeRoot,$script:ReportRoot)) { [void](New-Item -ItemType Directory -Path $folder -Force) }
. (Join-Path $PSScriptRoot 'ProcessInventory.ps1')
. (Join-Path $PSScriptRoot 'PersistenceInventory.ps1')
. (Join-Path $PSScriptRoot 'Interface.ps1')
function Test-SCMAdministrator {
    $identity=[Security.Principal.WindowsIdentity]::GetCurrent()
    (New-Object Security.Principal.WindowsPrincipal($identity)).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
}
function Write-SCMJson {
    param($Value,[string]$Path)
    $temp=$Path+'.tmp-'+[guid]::NewGuid().ToString('N')
    $Value | ConvertTo-Json -Depth 16 | Set-Content -LiteralPath $temp -Encoding UTF8
    Move-Item -LiteralPath $temp -Destination $Path -Force
}
function New-SCMChange {
    param([string]$Kind,[string]$Target,$Undo,[string]$Status='Applied')
    $change=[pscustomobject]@{Id=[guid]::NewGuid().ToString('N');TimeUtc=[datetime]::UtcNow.ToString('o');Kind=$Kind;Target=$Target;Status=$Status;Undo=$Undo}
    Write-SCMJson $change (Join-Path $script:ChangeRoot ($change.Id+'.json'))
    $change
}
function Set-SCMChangeStatus {
    param($Change,[string]$Status)
    $Change.Status=$Status
    Write-SCMJson $Change (Join-Path $script:ChangeRoot ($Change.Id+'.json'))
}
function Get-SCMChanges {
    $changes=@(Get-ChildItem -LiteralPath $script:ChangeRoot -Filter '*.json' | ForEach-Object { try { Get-Content -LiteralPath $_.FullName -Raw | ConvertFrom-Json } catch {} })
    $serviceBackups=Join-Path $script:SCMDataRoot 'ServiceBackups'
    if (Test-Path -LiteralPath $serviceBackups -PathType Container) {
        foreach ($file in @(Get-ChildItem -LiteralPath $serviceBackups -Filter '*.json' -File)) {
            try {
                $backup=Get-Content -LiteralPath $file.FullName -Raw | ConvertFrom-Json
                if ($backup.Version -ne 1 -or $backup.State -notin @('Prepared','Applied') -or $backup.Undo.BackupId -notmatch '^[a-f0-9]{32}$' -or $file.BaseName -cne $backup.Undo.BackupId) { continue }
                if (@($changes | Where-Object { $_.Kind -eq 'Service' -and $_.Undo.BackupId -eq $backup.Undo.BackupId }).Count) { continue }
                $changes+=[pscustomobject]@{Id=$backup.Undo.BackupId;TimeUtc=$backup.CreatedUtc;Kind='Service';Target=($backup.Undo.Name+' (recovered backup)');Status='Pending';Undo=$backup.Undo}
            } catch {}
        }
    }
    @($changes | Sort-Object TimeUtc -Descending)
}
function Assert-SCMProcessAction {
    param($Record)
    if ($Record.IsProtected) { throw ('Protected: '+$Record.ProtectionReason) }
    [void](Test-SCMProcessIdentity -Record $Record)
    $current=@(Get-SCMProcessSnapshot | Where-Object ProcessId -eq $Record.ProcessId)
    if ($current.Count -ne 1 -or $current[0].IsProtected) { throw 'Process is now protected or has ended. Refresh the list.' }
    Test-SCMProcessIdentity -Record $Record
}
function Set-SCMProcessPriority {
    param($Record,[ValidateSet('Idle','BelowNormal','Normal','AboveNormal')][string]$Priority)
    [void](Assert-SCMProcessAction $Record)
    $process=Get-Process -Id $Record.ProcessId
    $undo=[pscustomobject]@{Record=$Record;Priority=$process.PriorityClass.ToString()}
    $change=New-SCMChange 'Priority' ($Record.Name+' / '+$Record.ProcessId) $undo 'Pending'
    try {
        [void](Test-SCMProcessIdentity -Record $Record)
        $process.PriorityClass=[System.Diagnostics.ProcessPriorityClass]$Priority
        Set-SCMChangeStatus $change 'Applied'
    } catch { Set-SCMChangeStatus $change 'Failed';throw }
}
function Set-SCMProcessAffinity {
    param($Record,[string]$Cores)
    [void](Assert-SCMProcessAction $Record)
    $count=[Environment]::ProcessorCount
    if ($count -gt 63 -or -not [Environment]::Is64BitProcess) { throw 'Affinity editing supports 64-bit PowerShell with at most 63 logical CPUs. Use Windows scheduling on this configuration.' }
    $indices=@($Cores.Split(',') | ForEach-Object {
        $core=0
        if (-not [int]::TryParse($_.Trim(),[ref]$core) -or $core -lt 0 -or $core -ge $count) { throw "Enter comma-separated core numbers 0 through $($count-1)." }
        $core
    } | Sort-Object -Unique)
    if (-not $indices.Count) { throw 'Select at least one core.' }
    [long]$mask=0
    foreach ($core in $indices) { $mask=$mask -bor ([long]1 -shl $core) }
    $process=Get-Process -Id $Record.ProcessId
    $undo=[pscustomobject]@{Record=$Record;Affinity=$process.ProcessorAffinity.ToInt64().ToString()}
    $change=New-SCMChange 'Affinity' ($Record.Name+' / '+$Record.ProcessId) $undo 'Pending'
    try {
        [void](Test-SCMProcessIdentity -Record $Record)
        $process.ProcessorAffinity=[intptr]$mask
        Set-SCMChangeStatus $change 'Applied'
    } catch { Set-SCMChangeStatus $change 'Failed';throw }
}
function Stop-SCMSelectedProcess {
    param($Record,[switch]$Force)
    [void](Assert-SCMProcessAction $Record)
    $process=Get-Process -Id $Record.ProcessId
    [void](Test-SCMProcessIdentity -Record $Record)
    if ($Force) { $process.Kill() }
    elseif (-not $process.CloseMainWindow()) { throw 'No closeable main window. Exit from the app or use End selected process after saving your work.' }
    [void](New-SCMChange 'ProcessClose' ($Record.Name+' / '+$Record.ProcessId) $null 'Logged')
}
function Get-SCMPowerPlans {
    $lines=@(& "$env:SystemRoot\System32\powercfg.exe" /list 2>&1)
    if ($LASTEXITCODE -ne 0) { throw 'Unable to list power plans.' }
    @($lines | ForEach-Object {
        if ([string]$_ -match '([0-9a-fA-F]{8}-(?:[0-9a-fA-F]{4}-){3}[0-9a-fA-F]{12})\s+\((.+)\)') { [pscustomobject]@{Guid=$matches[1];Name=$matches[2];Active=([string]$_).Contains('*')} }
    })
}
function Set-SCMPowerPlan {
    param([string]$Guid)
    $plans=@(Get-SCMPowerPlans)
    if (-not ($plans | Where-Object Guid -eq $Guid)) { throw 'Unknown power plan.' }
    $old=@($plans | Where-Object Active)[0]
    if (-not $old) { throw 'Current plan could not be identified.' }
    $change=New-SCMChange 'PowerPlan' $Guid ([pscustomobject]@{Guid=$old.Guid}) 'Pending'
    try {
        & "$env:SystemRoot\System32\powercfg.exe" /setactive $Guid | Out-Null
        if ($LASTEXITCODE -ne 0) { throw 'Windows rejected the plan change.' }
        Set-SCMChangeStatus $change 'Applied'
    } catch { Set-SCMChangeStatus $change 'Failed';throw }
}
function Restore-SCMChange {
    param($Change)
    if ($Change.Status -notin @('Applied','Pending')) { throw 'Only applied or interrupted changes can be restored.' }
    switch ($Change.Kind) {
        'Priority' {
            [void](Assert-SCMProcessAction $Change.Undo.Record)
            (Get-Process -Id $Change.Undo.Record.ProcessId).PriorityClass=[System.Diagnostics.ProcessPriorityClass]([string]$Change.Undo.Priority)
        }
        'Affinity' {
            [void](Assert-SCMProcessAction $Change.Undo.Record)
            (Get-Process -Id $Change.Undo.Record.ProcessId).ProcessorAffinity=[intptr]([long]$Change.Undo.Affinity)
        }
        'Startup' { Restore-SCMStartupChange -Undo $Change.Undo }
        'Service' { Restore-SCMServiceChange -Undo $Change.Undo }
        'PowerPlan' {
            & "$env:SystemRoot\System32\powercfg.exe" /setactive $Change.Undo.Guid | Out-Null
            if ($LASTEXITCODE -ne 0) { throw 'Windows rejected power-plan restoration.' }
        }
        default { throw 'This entry has no automatic undo.' }
    }
    Set-SCMChangeStatus $Change 'Restored'
}
function Get-SCMInstalledApps {
    $locations=@('HKCU:\Software\Microsoft\Windows\CurrentVersion\Uninstall\*','HKLM:\Software\Microsoft\Windows\CurrentVersion\Uninstall\*','HKLM:\Software\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall\*')
    $result=@(foreach ($location in $locations) {
        Get-ItemProperty -Path $location -ErrorAction SilentlyContinue | Where-Object DisplayName | ForEach-Object {
            [pscustomobject]@{Name=$_.DisplayName;Publisher=$_.Publisher;Version=$_.DisplayVersion;InstallLocation=$_.InstallLocation;Type='Desktop';Identity=$_.PSChildName}
        }
    })
    try {
        $result+=@(Get-AppxPackage -ErrorAction Stop | Where-Object { -not $_.IsFramework -and -not $_.NonRemovable } | ForEach-Object {
            [pscustomobject]@{Name=$_.Name;Publisher=$_.Publisher;Version=$_.Version.ToString();InstallLocation=$_.InstallLocation;Type='Store';Identity=$_.PackageFamilyName}
        })
    } catch {}
    @($result | Sort-Object Name,Version -Unique)
}
function Add-SCMProcessEvidence {
    param([array]$Processes,[array]$Startup,[array]$Tasks)
    foreach ($record in $Processes) {
        $evidence=New-Object 'System.Collections.Generic.List[string]'
        if ($record.Path) {
            $pattern='(?i)(?:^|["\s])'+[regex]::Escape($record.Path)+'(?=["\s]|$)'
            foreach ($entry in $Startup) { if ($entry.Enabled -and [string]$entry.Command -match $pattern) { $evidence.Add('Possible startup source: '+$entry.Name) } }
            foreach ($entry in $Tasks) { if ([string]$entry.Command -match $pattern) { $evidence.Add('Matching task: '+$entry.TaskPath+$entry.TaskName) } }
        }
        $record | Add-Member -NotePropertyName PersistenceEvidence -NotePropertyValue ($evidence -join '; ') -Force
    }
    $Processes
}
function Get-SCMAppTotals {
    param([array]$Processes)
    @($Processes | Group-Object RootProgram | ForEach-Object {
        [pscustomobject]@{Program=$_.Name;Processes=$_.Count;WorkingSetMB=[math]::Round(($_.Group | Measure-Object MemoryMB -Sum).Sum,1);PrivateMB=[math]::Round(($_.Group | Measure-Object PrivateMB -Sum).Sum,1);CPUPercent=[math]::Round(($_.Group | Measure-Object CpuPercent -Sum).Sum,1);Executables=(($_.Group.Name | Sort-Object -Unique) -join ', ')}
    } | Sort-Object WorkingSetMB -Descending)
}
function Get-SCMFullAudit {
    $errors=New-Object 'System.Collections.Generic.List[string]'
    $startup=@();$services=@();$tasks=@();$apps=@();$plans=@()
    $startupCoverage = $null
    try {
        $startup=@(Get-SCMStartupEntries -Coverage ([ref]$startupCoverage))
        if ($startupCoverage) { foreach ($inventoryError in @($startupCoverage.Errors)) { $errors.Add('Startup: '+$inventoryError) } }
    } catch { $errors.Add('Startup: '+$_.Exception.Message) }
    try { $services=@(Get-SCMServiceEntries) } catch { $errors.Add('Services: '+$_.Exception.Message) }
    try { $tasks=@(Get-SCMScheduledEntries) } catch { $errors.Add('Scheduled tasks: '+$_.Exception.Message) }
    try { $apps=@(Get-SCMInstalledApps) } catch { $errors.Add('Apps: '+$_.Exception.Message) }
    try { $plans=@(Get-SCMPowerPlans) } catch { $errors.Add('Power: '+$_.Exception.Message) }
    $processes=@(Add-SCMProcessEvidence @(Get-SCMProcessSnapshot) $startup $tasks)
    [pscustomobject]@{
        ToolVersion=$script:SCMVersion;CapturedUtc=[datetime]::UtcNow.ToString('o');Elevated=(Test-SCMAdministrator)
        System=(Get-SCMSystemSummary);Processes=$processes;AppTotals=@(Get-SCMAppTotals $processes)
        Startup=$startup;Services=$services;ScheduledTasks=$tasks;InstalledApps=$apps;PowerPlans=$plans;InventoryErrors=@($errors.ToArray())
        StartupInventoryComplete=($null -ne $startupCoverage -and $startupCoverage.Complete -eq $true)
        StartupInventoryErrors=$(if ($startupCoverage) { @($startupCoverage.Errors) } else { @('Startup inventory coverage was not established.') })
        Notes=@('CPU is sampled and normalized across all logical CPUs.','Working sets include shared pages; totals can double-count shared RAM. Private bytes are committed memory, not necessarily resident RAM.','Parent and startup/task matches are evidence, not proof of the historical launch source.','Old start time alone does not identify an obsolete or unwanted process.','Protected and inaccessible processes are included in the full audit. Raw process command lines are omitted. Startup and task commands are included; keep reports private.')
    }
}
function Export-SCMAudit {
    param($Snapshot,[string]$Destination)
    if (-not $Destination) { $Destination=Join-Path $script:ReportRoot ('Audit-'+(Get-Date -Format 'yyyyMMdd-HHmmss')) }
    $Destination=[IO.Path]::GetFullPath($Destination)
    [void](New-Item -ItemType Directory -Path $Destination -Force)
    Write-SCMJson $Snapshot (Join-Path $Destination 'audit.json')
    $Snapshot.Processes | Export-Csv -LiteralPath (Join-Path $Destination 'processes.csv') -NoTypeInformation -Encoding UTF8
    $Snapshot.AppTotals | Export-Csv -LiteralPath (Join-Path $Destination 'applications.csv') -NoTypeInformation -Encoding UTF8
    $Snapshot.Startup | Select-Object Name,Source,Enabled,CanManage,Command,Location,Reason | Export-Csv -LiteralPath (Join-Path $Destination 'startup.csv') -NoTypeInformation -Encoding UTF8
    $Snapshot.Services | Export-Csv -LiteralPath (Join-Path $Destination 'services.csv') -NoTypeInformation -Encoding UTF8
    $Snapshot.ScheduledTasks | Export-Csv -LiteralPath (Join-Path $Destination 'scheduled-tasks.csv') -NoTypeInformation -Encoding UTF8
    $html=New-Object Text.StringBuilder
    [void]$html.AppendLine('<!doctype html><html lang="en"><meta charset="utf-8"><title>Computer audit</title><style>body{font:15px Segoe UI,sans-serif;margin:30px;background:#f3f5f8;color:#182333}table{border-collapse:collapse;background:white;width:100%;margin-bottom:30px}td,th{padding:8px;border:1px solid #d5dbe3;text-align:left;word-break:break-word}th{background:#19354e;color:white}h1,h2{color:#19354e}pre{white-space:pre-wrap}</style><h1>Specialized Computer Manager</h1>')
    [void]$html.AppendLine('<p>Captured UTC: '+[Net.WebUtility]::HtmlEncode($Snapshot.CapturedUtc)+'</p><h2>System</h2><pre>'+[Net.WebUtility]::HtmlEncode(($Snapshot.System | ConvertTo-Json -Depth 8))+'</pre><h2>Measurements</h2><ul>')
    foreach ($note in $Snapshot.Notes) { [void]$html.AppendLine('<li>'+[Net.WebUtility]::HtmlEncode($note)+'</li>') }
    [void]$html.AppendLine('</ul><h2>Application totals</h2>')
    [void]$html.AppendLine(($Snapshot.AppTotals | ConvertTo-Html -Fragment | Out-String))
    [void]$html.AppendLine('<h2>All running processes</h2>')
    [void]$html.AppendLine(($Snapshot.Processes | Select-Object ProcessId,Name,AppName,RootProgram,CpuPercent,MemoryMB,PrivateMB,ParentName,LaunchExplanation,PersistenceEvidence,Path,IsProtected,ProtectionReason | ConvertTo-Html -Fragment | Out-String))
    [void]$html.AppendLine('<h2>Startup</h2>')
    [void]$html.AppendLine(($Snapshot.Startup | Select-Object Name,Source,Enabled,CanManage,Command,Reason | ConvertTo-Html -Fragment | Out-String))
    [void]$html.AppendLine('<h2>Services</h2>')
    [void]$html.AppendLine(($Snapshot.Services | Select-Object Name,DisplayName,State,StartMode,ProcessId,Protected,Reason,PathName | ConvertTo-Html -Fragment | Out-String))
    [void]$html.AppendLine('<h2>Scheduled tasks</h2>')
    [void]$html.AppendLine(($Snapshot.ScheduledTasks | Select-Object TaskName,TaskPath,State,Author,Command,Protected | ConvertTo-Html -Fragment | Out-String))
    if ($Snapshot.InventoryErrors.Count) { [void]$html.AppendLine('<h2>Unavailable inventory</h2><pre>'+[Net.WebUtility]::HtmlEncode(($Snapshot.InventoryErrors -join "`r`n"))+'</pre>') }
    [void]$html.AppendLine('</html>')
    $html.ToString() | Set-Content -LiteralPath (Join-Path $Destination 'report.html') -Encoding UTF8
    $Destination
}
function Export-SCMMonitor {
    param([int]$Count=6,[int]$Interval=5,[string]$Destination)
    if (-not $Destination) { $Destination=Join-Path $script:ReportRoot ('Monitor-'+(Get-Date -Format 'yyyyMMdd-HHmmss')) }
    $Destination=[IO.Path]::GetFullPath($Destination)
    [void](New-Item -ItemType Directory -Path $Destination -Force)
    $samples=New-Object 'System.Collections.Generic.List[object]'
    for ($index=0;$index -lt $Count;$index++) {
        $processes=@(Get-SCMProcessSnapshot)
        $samples.Add([pscustomobject]@{Index=$index;CapturedUtc=[datetime]::UtcNow.ToString('o');Processes=$processes})
        Write-Host ('Sample '+($index+1)+'/'+$Count+': '+$processes.Count+' processes')
        if ($index -lt $Count-1) { Start-Sleep -Seconds $Interval }
    }
    $flat=@(foreach ($sample in $samples) {
        foreach ($process in $sample.Processes) {
            [pscustomobject]@{Identity=([string]$process.ProcessId+'|'+$process.StartTimeUtc);Index=$sample.Index;CapturedUtc=$sample.CapturedUtc;ProcessId=$process.ProcessId;Name=$process.Name;RootProgram=$process.RootProgram;Role=$process.Role;CpuPercent=$process.CpuPercent;MemoryMB=$process.MemoryMB;PrivateMB=$process.PrivateMB;Path=$process.Path}
        }
    })
    $trends=@($flat | Group-Object Identity | ForEach-Object {
        $items=@($_.Group | Sort-Object Index);$first=$items[0];$last=$items[-1]
        [pscustomobject]@{ProcessId=$first.ProcessId;Name=$first.Name;Program=$first.RootProgram;Role=$first.Role;Observations=$items.Count;FirstSample=$first.Index;LastSample=$last.Index;WorkingSetStartMB=$first.MemoryMB;WorkingSetEndMB=$last.MemoryMB;WorkingSetChangeMB=[math]::Round(($last.MemoryMB-$first.MemoryMB),1);PrivateChangeMB=[math]::Round(($last.PrivateMB-$first.PrivateMB),1);AverageCpuPercent=[math]::Round(($items | Measure-Object CpuPercent -Average).Average,2);Path=$first.Path}
    } | Sort-Object PrivateChangeMB -Descending)
    $flat | Export-Csv -LiteralPath (Join-Path $Destination 'samples.csv') -NoTypeInformation -Encoding UTF8
    $trends | Export-Csv -LiteralPath (Join-Path $Destination 'trends.csv') -NoTypeInformation -Encoding UTF8
    Write-SCMJson ([pscustomobject]@{ToolVersion=$script:SCMVersion;Samples=$Count;IntervalSeconds=$Interval;Notes='Read-only samples. Growth across a short interval is not proof of a memory leak. A process may start or exit between samples. PID and creation time identify each process.';Trends=$trends}) (Join-Path $Destination 'trends.json')
    $Destination
}
function Invoke-SCMSelfTest {
    $checks=New-Object 'System.Collections.Generic.List[object]'
    foreach ($file in @('ComputerManager.ps1','ProcessInventory.ps1','PersistenceInventory.ps1','Interface.ps1')) {
        $tokens=$null;$parseErrors=$null
        [void][Management.Automation.Language.Parser]::ParseFile((Join-Path $script:SCMHome $file),[ref]$tokens,[ref]$parseErrors)
        $checks.Add([pscustomobject]@{Check=('Parse '+$file);Pass=($parseErrors.Count -eq 0);Detail=($parseErrors.Message -join '; ')})
    }
    $snapshot=Get-SCMFullAudit
    $self=@($snapshot.Processes | Where-Object ProcessId -eq $PID)
    $checks.Add([pscustomobject]@{Check='Manager protected';Pass=($self.Count -eq 1 -and $self[0].IsProtected);Detail='Manager and ancestors must be protected.'})
    $checks.Add([pscustomobject]@{Check='Process inventory';Pass=($snapshot.Processes.Count -gt 0);Detail=($snapshot.Processes.Count.ToString()+' processes')})
    $unprotected=@($snapshot.Processes | Where-Object { $_.Name -match '^(System|Registry|smss|csrss|wininit|services|lsass|winlogon|MsMpEng)(\.exe)?$' -and -not $_.IsProtected })
    $checks.Add([pscustomobject]@{Check='Essential OS protected';Pass=($unprotected.Count -eq 0);Detail='Critical names protected.'})
    $checks.Add([pscustomobject]@{Check='CPU range valid';Pass=(@($snapshot.Processes | Where-Object { $_.CpuPercent -lt 0 -or $_.CpuPercent -gt 100 }).Count -eq 0);Detail='0 to 100 percent of whole machine.'})
    $checks.Add([pscustomobject]@{Check='Inventories complete';Pass=($snapshot.InventoryErrors.Count -eq 0);Detail=($snapshot.InventoryErrors -join '; ')})
    if ($self.Count -eq 1) {
        $valid=$false;try { [void](Test-SCMProcessIdentity -Record $self[0]);$valid=$true } catch {}
        $checks.Add([pscustomobject]@{Check='Live identity accepted';Pass=$valid;Detail='PID, creation time and path.'})
        $fake=$self[0].PSObject.Copy();$fake.StartTimeUtc='2000-01-01T00:00:00.0000000Z'
        $rejected=$false;try { [void](Test-SCMProcessIdentity -Record $fake) } catch { $rejected=$true }
        $checks.Add([pscustomobject]@{Check='Stale identity rejected';Pass=$rejected;Detail='Reused PID must fail closed.'})
        $protected=$false;try { [void](Assert-SCMProcessAction $self[0]) } catch { $protected=$true }
        $checks.Add([pscustomobject]@{Check='Protected action blocked';Pass=$protected;Detail='Read-only guard test; no process changed.'})
    }
    $checks.ToArray()
}
if ($LibraryOnly) { return }
try {
    if ($SelfTest) {
        $results=@(Invoke-SCMSelfTest)
        $results | Format-Table -AutoSize | Out-String | Write-Output
        if ($OutputPath) { Write-SCMJson $results ([IO.Path]::GetFullPath($OutputPath)) }
        if (@($results | Where-Object { -not $_.Pass }).Count) { exit 1 }
        exit 0
    }
    if ($Monitor) { $destination=Export-SCMMonitor -Count $Samples -Interval $IntervalSeconds -Destination $OutputPath;Write-Output ('Monitor saved: '+$destination);exit 0 }
    if ($Audit) { $destination=Export-SCMAudit (Get-SCMFullAudit) $OutputPath;Write-Output ('Audit saved: '+$destination);exit 0 }
    Show-SCMInterface -SmokeTest:$UiSmokeTest
} catch { Write-Error $_;exit 1 }


