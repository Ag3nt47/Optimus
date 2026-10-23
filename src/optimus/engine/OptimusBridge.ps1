#requires -Version 5.1
[CmdletBinding()]
param([Parameter(Mandatory=$true)][string]$DataRoot)
$ErrorActionPreference='Stop'
$ProgressPreference='SilentlyContinue'
$WarningPreference='SilentlyContinue'
[Console]::InputEncoding=New-Object Text.UTF8Encoding($false)
[Console]::OutputEncoding=New-Object Text.UTF8Encoding($false)
. (Join-Path $PSScriptRoot 'ComputerManager.ps1') -LibraryOnly -OutputPath $DataRoot
. (Join-Path $PSScriptRoot 'SafeRecycle.ps1')
. (Join-Path $PSScriptRoot 'FileTools.ps1')
. (Join-Path $PSScriptRoot 'UpdateProgress.ps1')
. (Join-Path $PSScriptRoot 'MaintenanceOperations.ps1')
. (Join-Path $PSScriptRoot 'RegistryTools.ps1')
function Test-OptimusSameExecutableFile {
    param([string]$First,[string]$Second)
    if(-not $First -or -not $Second){return $false}
    if($First -ieq $Second){return $true}
    if(-not('OptimusExecutableIdentity' -as [type])) {
        Add-Type -TypeDefinition @"
using System;
using System.IO;
using System.Runtime.InteropServices;
using Microsoft.Win32.SafeHandles;
public static class OptimusExecutableIdentity {
 [StructLayout(LayoutKind.Sequential)] struct FileInfo {
  public uint Attributes;
  public System.Runtime.InteropServices.ComTypes.FILETIME Creation, Access, Write;
  public uint Volume, SizeHigh, SizeLow, Links, IndexHigh, IndexLow;
 }
 [DllImport("kernel32.dll", SetLastError=true)] static extern bool GetFileInformationByHandle(SafeFileHandle handle,out FileInfo info);
 public static bool Same(string first,string second) {
  try {
   using(var left=new FileStream(first,FileMode.Open,FileAccess.Read,FileShare.ReadWrite|FileShare.Delete))
   using(var right=new FileStream(second,FileMode.Open,FileAccess.Read,FileShare.ReadWrite|FileShare.Delete)) {
    FileInfo a,b;
    if(!GetFileInformationByHandle(left.SafeFileHandle,out a)||!GetFileInformationByHandle(right.SafeFileHandle,out b)) return false;
    return (a.Attributes&16)==0 && (b.Attributes&16)==0 && (a.IndexHigh!=0||a.IndexLow!=0) && a.Volume==b.Volume && a.IndexHigh==b.IndexHigh && a.IndexLow==b.IndexLow;
   }
  } catch { return false; }
 }
}
"@
    }
    return [OptimusExecutableIdentity]::Same($First,$Second)
}
function Restore-OptimusRegistryChange {
    param($Change)
    $id=[string]$Change.Id
    if($id -notmatch '^[a-f0-9]{32}$'){throw 'Invalid registry change identity.'}
    $saved=@(Get-SCMChanges | Where-Object { $_.Id -ceq $id -and $_.Kind -eq 'RegistryEntry' })
    if($saved.Count -ne 1 -or $saved[0].Status -notin @('Applied','Pending')){throw 'Only a saved, applied or interrupted registry change can be restored.'}
    $result=Restore-OptimusRegistryIssue -Undo $saved[0].Undo
    Set-SCMChangeStatus -Change $saved[0] -Status Restored
    [pscustomobject]@{restored=$true;detail=$result}
}
function New-OptimusProgressCallback {
    param([Parameter(Mandatory=$true)][ValidatePattern('^[a-f0-9]{32}$')][string]$RequestId)
    # Capture the exact request identity, not a mutable outer loop variable.
    $boundRequestId=$RequestId
    return {
        param($Progress)
        $frame=[ordered]@{kind='progress';id=$boundRequestId;progress=$Progress}
        [Console]::WriteLine((ConvertTo-Json -InputObject $frame -Depth 6 -Compress))
    }.GetNewClosure()
}
function Invoke-OptimusRequest {
    param($Request)
    $arg=$Request.arguments
    switch ([string]$Request.operation) {
        'audit' { Get-SCMFullAudit }
        'process_details' {
            $fresh=Get-SCMProcessIdentity -ProcessId ([int]$arg.record.pid)
            $pythonCreation=[DateTimeOffset]::FromUnixTimeMilliseconds([long]([double]$arg.record.create_time*1000))
            if(-not $fresh.Path -or -not(Test-OptimusSameExecutableFile -First $fresh.Path -Second ([string]$arg.record.exe)) -or [Math]::Abs(([DateTimeOffset]::Parse($fresh.StartTimeUtc)-$pythonCreation).TotalMilliseconds) -gt 2){throw 'Process identity changed. Refresh before inspecting.'}
            $record=@(Get-SCMProcessSnapshot | Where-Object ProcessId -eq $fresh.ProcessId)
            if($record.Count -ne 1 -or $record[0].StartTimeUtc -ne $fresh.StartTimeUtc){throw 'Process changed during inspection.'}
            Get-SCMProcessDetails -Record $record[0]
        }
        'startup_list' { @(Get-SCMStartupEntries) }
        'startup_set' { $undo=Set-SCMStartupState -Entry $arg.record -Enabled ([bool]$arg.enabled);New-SCMChange -Kind Startup -Target $arg.record.Name -Undo $undo }
        'startup_remove' { $undo=Remove-SCMStartupEntry -Entry $arg.record;New-SCMChange -Kind Startup -Target $arg.record.Name -Undo $undo }
        'installed_apps' { @(Get-SCMInstalledApps) }
        'services' { @(Get-SCMServiceEntries) }
        'service_set' { $undo=Set-SCMServiceState -Entry $arg.record -Action ([string]$arg.action);New-SCMChange -Kind Service -Target $arg.record.Name -Undo $undo }
        'storage_scan' { Get-SCMStorageAnalysis -Root ([string]$arg.root) -MaxFiles 100000 -MaxSeconds 45 }
        'duplicate_scan' { Get-SCMDuplicateFiles -Root ([string]$arg.root) -MinBytes 1024 -MaxFiles 50000 -MaxSeconds 45 }
        'duplicate_recycle' { Move-SCMFileToRecycleBin -Record $arg.record }
        'drivers_list' { @(Get-SCMDriverInventory) }
        'changes' { @(Get-SCMChanges) }
        'restore' {
            if($arg.change.Kind -eq 'RegistryEntry'){Restore-OptimusRegistryChange -Change $arg.change}
            else {Restore-SCMChange -Change $arg.change;[pscustomobject]@{restored=$true}}
        }
        'registry_scan' { Get-OptimusRegistryIssues }
        'registry_remove' {
            $script:OptimusRegistryPendingChange=$null
            $undo=Remove-OptimusRegistryIssue -Record $arg.record -BeforeRemove {
                param($prepared)
                $script:OptimusRegistryPendingChange=New-SCMChange -Kind RegistryEntry -Target ('HKCU App Paths / '+$prepared.Name) -Undo $prepared -Status Pending
            }
            $script:OptimusRegistryPendingChange.Undo=$undo
            Set-SCMChangeStatus -Change $script:OptimusRegistryPendingChange -Status Applied
            $script:OptimusRegistryPendingChange
        }
        'registry_restore' { Restore-OptimusRegistryChange -Change $arg.change }
        'power_plans' { @(Get-SCMPowerPlans) }
        'set_power_plan' { Set-SCMPowerPlan -Guid ([string]$arg.guid);@(Get-SCMPowerPlans) }
        'registry_audit' { Get-OptimusRegistryIssues }
        'driver_updates_scan' { Get-SCMDriverUpdateScan }
        'driver_device_inventory' { Get-SCMDeviceCoverageInventory }
        'driver_update_apply' {
            $record=if($arg.record){$arg.record}else{[pscustomobject]@{id=[string]$arg.id;revision=[int]$arg.revision}}
            $progressCallback=$null
            if($arg.reportProgress -is [bool] -and $arg.reportProgress -eq $true){
                $progressCallback=New-OptimusProgressCallback -RequestId ([string]$Request.id)
            }
            Invoke-SCMDriverUpdateApply -Record $record -Unattended ($arg.unattended -eq $true) -AcceptEula ($arg.acceptEula -eq $true) -ProgressCallback $progressCallback
        }
        default { Invoke-OptimusMaintenanceRequest -Operation ([string]$Request.operation) -Arguments $arg }
    }
}
while($null -ne ($line=[Console]::ReadLine())) {
    $request=$null
    try {
        if($line.Length -gt 4000000){throw 'Request too large.'}
        $request=ConvertFrom-Json -InputObject $line -ErrorAction Stop
        $result=Invoke-OptimusRequest -Request $request
        $response=[ordered]@{id=$request.id;ok=$true;result=$result}
    } catch { $response=[ordered]@{id=$request.id;ok=$false;error=$_.Exception.Message} }
    [Console]::WriteLine((ConvertTo-Json -InputObject $response -Depth 24 -Compress))
}

