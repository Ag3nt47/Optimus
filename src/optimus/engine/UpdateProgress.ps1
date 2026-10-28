#requires -Version 5.1
# Progress and supported download cancellation. The caller owns approval, update identity, quiet
# mode, backup, pending-restart checks and final native result validation.
# SDK GUIDs/signatures: microsoft/win32metadata, um/wuapi.h.
# https://learn.microsoft.com/windows/win32/wua_sdk/guidelines-for-asynchronous-wua-operations
function Initialize-SCMUpdateProgressCallbacks {
    if ('Optimus.UpdateProgress.NoOpCallbacks' -as [type]) { return }
    Add-Type -TypeDefinition @'
using System;
using System.Runtime.InteropServices;
namespace Optimus.UpdateProgress {
    [ComVisible(true), Guid("8c3f1cdd-6173-4591-aebd-a56a53ca77c1"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
    public interface IDownloadProgressChangedCallback {
        [PreserveSig] int Invoke(IntPtr downloadJob, IntPtr callbackArgs);
    }
    [ComVisible(true), Guid("77254866-9f5b-4c8e-b9e2-c77a8530d64b"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
    public interface IDownloadCompletedCallback {
        [PreserveSig] int Invoke(IntPtr downloadJob, IntPtr callbackArgs);
    }
    [ComVisible(true), Guid("e01402d5-f8da-43ba-a012-38894bd048f1"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
    public interface IInstallationProgressChangedCallback {
        [PreserveSig] int Invoke(IntPtr installationJob, IntPtr callbackArgs);
    }
    [ComVisible(true), Guid("45f4f6f3-d602-4f98-9a8a-3efa152ad2d3"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
    public interface IInstallationCompletedCallback {
        [PreserveSig] int Invoke(IntPtr installationJob, IntPtr callbackArgs);
    }
    // Microsoft win32metadata, RecompiledIdlHeaders/um/wuapi.h.
    [ComVisible(true), Guid("88aee058-d4b0-4725-a2f1-814a67ae964c"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
    public interface ISearchCompletedCallback {
        [PreserveSig] int Invoke(IntPtr searchJob, IntPtr callbackArgs);
    }
    [ComVisible(true), ClassInterface(ClassInterfaceType.None)]
    public sealed class NoOpSearchCallback : ISearchCompletedCallback {
        public int Invoke(IntPtr job, IntPtr callbackArgs) { return 0; }
    }
    // WUA may call on a foreign thread. Never execute PowerShell, retain a job,
    // dereference the borrowed pointers, or depend on a caller apartment here.
    [ComVisible(true), ClassInterface(ClassInterfaceType.None)]
    public sealed class NoOpCallbacks : IDownloadProgressChangedCallback,
        IDownloadCompletedCallback, IInstallationProgressChangedCallback,
        IInstallationCompletedCallback {
        public int Invoke(IntPtr job, IntPtr callbackArgs) { return 0; }
    }
}
'@
}

function Invoke-SCMUpdateSearch {
    param([Parameter(Mandatory=$true)]$Owner,[Parameter(Mandatory=$true)][string]$Criteria,
        [Parameter(Mandatory=$true)][scriptblock]$CancelRequested)
    Initialize-SCMUpdateProgressCallbacks
    $callback=New-Object Optimus.UpdateProgress.NoOpSearchCallback
    # https://learn.microsoft.com/windows/win32/api/wuapi/nf-wuapi-iupdatesearcher-beginsearch
    $job=$Owner.BeginSearch($Criteria,$callback,$null)
    if($null -eq $job){throw 'Windows Update did not return its asynchronous search job; no retry was attempted.'}
    $abortRequested=$false;$abortIssued=$false
    while($true){
        $complete=$false
        try{$observed=$job.IsCompleted;$complete=$observed -is [bool] -and $observed -eq $true}catch{}
        if($complete){break}
        if(-not $abortRequested -and (Test-SCMUpdateCancellation -CancelRequested $CancelRequested)){
            $abortRequested=$true
            # RequestAbort does not prove completion. Preserve job ownership
            # until IsCompleted and the one matching EndSearch below.
            # https://learn.microsoft.com/windows/win32/api/wuapi/nf-wuapi-isearchjob-requestabort
            try{$null=$job.RequestAbort();$abortIssued=$true}catch{}
        }
        Start-Sleep -Milliseconds 200
    }
    try{return $Owner.EndSearch($job)}
    catch{
        if(Test-SCMTerminalAbortError -Failure $_.Exception -AbortIssued $abortIssued -JobComplete $complete){
            return New-SCMTerminalAbortResult -Phase Search
        }
        throw
    }
    finally{[GC]::KeepAlive($callback);[GC]::KeepAlive($job)}
}

function Test-SCMTerminalAbortError {
    param([Exception]$Failure,[bool]$AbortIssued,[bool]$JobComplete)
    if(-not $AbortIssued -or -not $JobComplete){return $false}
    # Only WU_E_CALL_CANCELLED (0x8024000B) after our successful abort request
    # and the owned job's strict IsCompleted observation proves this refusal.
    # https://learn.microsoft.com/windows/win32/wua_sdk/wua-success-and-error-codes-
    for($depth=0;$null -ne $Failure -and $depth -lt 8;$depth++){
        if($Failure.HResult -eq -2145124341){return $true}
        $Failure=$Failure.InnerException
    }
    return $false
}

function New-SCMTerminalAbortResult {
    param([ValidateSet('Search','Download')][string]$Phase)
    $result=[pscustomobject]@{ResultCode=[int]5;HResult=[int]-2145124341;
        native_cancelled=$true;abort_requested=$true;job_completed=$true;
        cancelled=$true;installer_started=$false;outcomeKnown=$true;running=$false}
    if($Phase -eq 'Search'){
        $result|Add-Member -NotePropertyName Updates -NotePropertyValue @()
        $result|Add-Member -NotePropertyName Warnings -NotePropertyValue @()
    }else{
        $result|Add-Member ScriptMethod GetUpdateResult {param($Index)
            if($Index -ne 0){throw 'Only the caller-owned single update has a cancellation result.'}
            return [pscustomobject]@{ResultCode=[int]5;HResult=[int]-2145124341}
        }
    }
    return $result
}

function Test-SCMUpdateCancellation {
    param([scriptblock]$CancelRequested)
    if($null -eq $CancelRequested){return $false}
    try{$requested=& $CancelRequested;return ($requested -is [bool] -and $requested -eq $true)}
    catch{return $false} # A failed cancellation observer must not abandon a native job.
}

function ConvertTo-SCMProgressByteCount {
    param($Value)
    # WUA exposes DECIMAL counters (some COM projections return a string).
    # Reject fractions, booleans, nonfinite values and oversized observations.
    if ($Value -is [string]) {
        if ($Value -notmatch '^[0-9]{1,16}$') { return $null }
        $Value = [decimal]::Parse($Value, [Globalization.CultureInfo]::InvariantCulture)
    }
    if ($Value -isnot [int] -and $Value -isnot [long] -and $Value -isnot [double] -and
        $Value -isnot [single] -and $Value -isnot [decimal]) { return $null }
    try {
        if ([double]::IsNaN([double]$Value) -or [double]::IsInfinity([double]$Value) -or
            $Value -lt 0 -or $Value -gt 1125899906842624 -or [decimal]$Value -ne [decimal]::Truncate([decimal]$Value)) { return $null }
        return [long]$Value
    } catch { return $null }
}

function Publish-SCMUpdateProgress {
    param([scriptblock]$ProgressCallback,
        [Parameter(Mandatory=$true)][ValidateSet('downloading','installing')][string]$Phase,
        $Percent, $DownloadedBytes, $DownloadTotalBytes)
    if ($null -eq $ProgressCallback) { return }
    $value = $null
    if (($Percent -is [int] -or $Percent -is [long] -or $Percent -is [double] -or $Percent -is [single] -or $Percent -is [decimal]) -and
        -not [double]::IsNaN([double]$Percent) -and -not [double]::IsInfinity([double]$Percent) -and
        $Percent -ge 0 -and $Percent -le 100) {
        $value = [double]$Percent
    }
    try {
        # Discard observer output as well as observer errors. A display callback
        # must neither replace the result nor abandon an update operation.
        $payload = [ordered]@{phase=$Phase;percent=$value}
        if ($Phase -eq 'downloading') {
            $done = ConvertTo-SCMProgressByteCount $DownloadedBytes
            $total = ConvertTo-SCMProgressByteCount $DownloadTotalBytes
            if ($null -ne $done -and ($null -eq $DownloadTotalBytes -or ($null -ne $total -and $total -ge $done))) {
                $payload.downloaded_bytes = $done
                $payload.bytes_source = 'windows_update'
                $payload.bytes_estimated = $false
                if ($null -ne $total) { $payload.download_total_bytes = $total }
            }
        }
        $null = & $ProgressCallback ([pscustomobject]$payload)
    } catch { }
}

function Invoke-SCMUpdateProgress {
    [CmdletBinding()]
    param([Parameter(Mandatory=$true)]$Owner,
        [Parameter(Mandatory=$true)][ValidateSet('Download','Install')][string]$Phase,
        [scriptblock]$ProgressCallback,[scriptblock]$CancelRequested)
    Initialize-SCMUpdateProgressCallbacks
    $callbacks = New-Object Optimus.UpdateProgress.NoOpCallbacks
    $progressPhase = if ($Phase -eq 'Download') { 'downloading' } else { 'installing' }
    Publish-SCMUpdateProgress -ProgressCallback $ProgressCallback -Phase $progressPhase -Percent $null
    # There is exactly one Begin and one matching End. Never retry through the
    # synchronous API or infer success from a percentage. Download cancellation
    # uses documented IDownloadJob::RequestAbort once, then still awaits End.
    # Installation is never interrupted or killed by navigation.
    $job = if ($Phase -eq 'Download') {
        $Owner.BeginDownload($callbacks, $callbacks, $null)
    } else {
        $Owner.BeginInstall($callbacks, $callbacks, $null)
    }
    if ($null -eq $job) { throw 'Windows Update did not return its asynchronous job. Verify its outcome before continuing.' }
    $reportedComplete = $false
    $downloadedBytes = $null
    $downloadTotalBytes = $null
    $abortRequested = $false
    $abortIssued = $false
    while ($true) {
        $complete = $false
        try {
            $observed = $job.IsCompleted
            $complete = $observed -is [bool] -and $observed -eq $true
        } catch {
            # Keep the strong job/callback references and await the real native
            # outcome. Caller timeouts retain the bridge request as in-flight.
        }
        if(-not $complete -and $Phase -eq 'Download' -and -not $abortRequested -and
            (Test-SCMUpdateCancellation -CancelRequested $CancelRequested)){
            $abortRequested=$true
            # https://learn.microsoft.com/windows/win32/api/wuapi/nf-wuapi-idownloadjob-requestabort
            # S_OK is not completion. Keep strong ownership and await the real
            # completed job plus EndDownload, even if abort itself fails.
            try { $null=$job.RequestAbort();$abortIssued=$true } catch { }
        }
        $percent = $null
        try {
            # GetProgress returns a snapshot: obtain a fresh one every poll.
            $snapshot = $job.GetProgress()
            if ($null -ne $snapshot) {
                $percent = $snapshot.CurrentUpdatePercentComplete
                if ($Phase -eq 'Download') {
                    # Current-update counters belong to this exact offer;
                    # never turn catalog maximum size into transferred bytes.
                    try {
                        $downloadedBytes = $snapshot.CurrentUpdateBytesDownloaded
                        $downloadTotalBytes = $snapshot.CurrentUpdateBytesToDownload
                    } catch { $downloadedBytes = $null; $downloadTotalBytes = $null }
                }
            }
        } catch { }
        if (($percent -is [int] -or $percent -is [long] -or $percent -is [double] -or $percent -is [single] -or $percent -is [decimal]) -and $percent -eq 100) {
            $reportedComplete = $true
            # A handler may report 100 before returning a failed End result.
            # Await the terminal native receipt rather than displaying success.
            $percent = $null
        }
        Publish-SCMUpdateProgress -ProgressCallback $ProgressCallback -Phase $progressPhase -Percent $percent -DownloadedBytes $downloadedBytes -DownloadTotalBytes $downloadTotalBytes
        if ($complete) { break }
        Start-Sleep -Milliseconds 200
    }
    try {
        try{
            $result = if ($Phase -eq 'Download') { $Owner.EndDownload($job) } else { $Owner.EndInstall($job) }
        }catch{
            if($Phase -eq 'Download' -and
                (Test-SCMTerminalAbortError -Failure $_.Exception -AbortIssued $abortIssued -JobComplete $complete)){
                return New-SCMTerminalAbortResult -Phase Download
            }
            throw
        }
        $verified = $false
        try {
            # The calling driver operation contains one exact approved update.
            # This is only a display check: FileTools still validates and saves
            # the full native result independently before another item begins.
            $item = $result.GetUpdateResult(0)
            $verified = ($result.ResultCode -is [int] -and $result.ResultCode -eq 2 -and
                $result.HResult -is [int] -and $result.HResult -eq 0 -and
                $item.ResultCode -is [int] -and $item.ResultCode -eq 2 -and
                $item.HResult -is [int] -and $item.HResult -eq 0)
            if ($Phase -eq 'Install') {
                $verified = $verified -and $result.RebootRequired -is [bool] -and $item.RebootRequired -is [bool]
            }
        } catch { }
        if ($verified -and $reportedComplete) {
            Publish-SCMUpdateProgress -ProgressCallback $ProgressCallback -Phase $progressPhase -Percent 100 -DownloadedBytes $downloadedBytes -DownloadTotalBytes $downloadTotalBytes
        }
        return $result
    } finally {
        # WUA completion is established before releasing the managed references.
        # Unexpected End errors propagate. The sole translated outcome is the
        # exact terminal pre-install cancellation; it never authorizes success.
        [GC]::KeepAlive($callbacks)
        [GC]::KeepAlive($job)
    }
}
