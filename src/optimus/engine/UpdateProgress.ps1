#requires -Version 5.1
# Progress observation only. The caller owns approval, update identity, quiet
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

function Publish-SCMUpdateProgress {
    param([scriptblock]$ProgressCallback,
        [Parameter(Mandatory=$true)][ValidateSet('downloading','installing')][string]$Phase,
        $Percent)
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
        $null = & $ProgressCallback ([pscustomobject]@{phase=$Phase;percent=$value})
    } catch { }
}

function Invoke-SCMUpdateProgress {
    [CmdletBinding()]
    param([Parameter(Mandatory=$true)]$Owner,
        [Parameter(Mandatory=$true)][ValidateSet('Download','Install')][string]$Phase,
        [scriptblock]$ProgressCallback)
    Initialize-SCMUpdateProgressCallbacks
    $callbacks = New-Object Optimus.UpdateProgress.NoOpCallbacks
    $progressPhase = if ($Phase -eq 'Download') { 'downloading' } else { 'installing' }
    Publish-SCMUpdateProgress -ProgressCallback $ProgressCallback -Phase $progressPhase -Percent $null
    # There is exactly one Begin and one matching End. Never retry through the
    # synchronous API, abort/kill the job, or infer success from a percentage.
    $job = if ($Phase -eq 'Download') {
        $Owner.BeginDownload($callbacks, $callbacks, $null)
    } else {
        $Owner.BeginInstall($callbacks, $callbacks, $null)
    }
    if ($null -eq $job) { throw 'Windows Update did not return its asynchronous job. Verify its outcome before continuing.' }
    $reportedComplete = $false
    while ($true) {
        $complete = $false
        try {
            $observed = $job.IsCompleted
            $complete = $observed -is [bool] -and $observed -eq $true
        } catch {
            # Keep the strong job/callback references and await the real native
            # outcome. Caller timeouts retain the bridge request as in-flight.
        }
        $percent = $null
        try {
            # GetProgress returns a snapshot: obtain a fresh one every poll.
            $snapshot = $job.GetProgress()
            if ($null -ne $snapshot) { $percent = $snapshot.CurrentUpdatePercentComplete }
        } catch { }
        if (($percent -is [int] -or $percent -is [long] -or $percent -is [double] -or $percent -is [single] -or $percent -is [decimal]) -and $percent -eq 100) {
            $reportedComplete = $true
            # A handler may report 100 before returning a failed End result.
            # Await the terminal native receipt rather than displaying success.
            $percent = $null
        }
        Publish-SCMUpdateProgress -ProgressCallback $ProgressCallback -Phase $progressPhase -Percent $percent
        if ($complete) { break }
        Start-Sleep -Milliseconds 1000
    }
    try {
        $result = if ($Phase -eq 'Download') { $Owner.EndDownload($job) } else { $Owner.EndInstall($job) }
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
            Publish-SCMUpdateProgress -ProgressCallback $ProgressCallback -Phase $progressPhase -Percent 100
        }
        return $result
    } finally {
        # WUA completion is established before releasing the managed references.
        # End errors still propagate; this helper never manufactures a receipt.
        [GC]::KeepAlive($callbacks)
        [GC]::KeepAlive($job)
    }
}
