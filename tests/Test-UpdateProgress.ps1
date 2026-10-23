#requires -Version 5.1
# Offline fake-job fixtures. This script never creates a Windows Update session,
# scans devices, downloads content, starts an installer, or changes host state.
$ErrorActionPreference = 'Stop'
. (Join-Path (Split-Path $PSScriptRoot -Parent) 'src/optimus/engine/UpdateProgress.ps1')
$script:Assertions = 0
function Assert-Fixture { param([bool]$Condition,[string]$Message)
    $script:Assertions++
    if (-not $Condition) { throw ('Fixture assertion failed: ' + $Message) }
}
function Start-Sleep { param([int]$Milliseconds)
    Assert-Fixture ($Milliseconds -eq 1000) 'polling interval is bounded to one second'
    $script:Sleeps++
}
function New-Result { param([int]$Code=2)
    $script:Item = [pscustomobject]@{ResultCode=$Code;HResult=0;RebootRequired=$false}
    $value = [pscustomobject]@{ResultCode=$Code;HResult=0;RebootRequired=$false}
    $value | Add-Member ScriptMethod GetUpdateResult { param($Index)
        Assert-Fixture ($Index -eq 0) 'single exact update result index'
        return $script:Item
    }
    return $value
}
function Reset-Fixture {
    $script:Begins=0; $script:Ends=0; $script:Sleeps=0; $script:Polls=0; $script:Reads=0
    $script:BeginFailure=$false; $script:EndFailure=$false; $script:NullJob=$false
    $script:Completion=@($false,$false,$true); $script:Percent=@(12,67,100)
    $script:Events = New-Object 'System.Collections.Generic.List[object]'
    $script:Result = New-Result
    $script:Job = [pscustomobject]@{}
    $script:Job | Add-Member ScriptProperty IsCompleted {
        $index=$script:Reads; $script:Reads++
        $value=$script:Completion[[Math]::Min($index,$script:Completion.Count-1)]
        if ($value -is [Exception]) { throw $value }
        return $value
    }
    $script:Job | Add-Member ScriptMethod GetProgress {
        $index=$script:Polls; $script:Polls++
        $value=$script:Percent[[Math]::Min($index,$script:Percent.Count-1)]
        if ($value -is [Exception]) { throw $value }
        if ($value -ceq 'missing') { return [pscustomobject]@{} }
        return [pscustomobject]@{CurrentUpdatePercentComplete=$value}
    }
    $script:Owner = [pscustomobject]@{}
    $script:Owner | Add-Member ScriptMethod BeginDownload { param($Changed,$Completed,$State)
        $script:Begins++
        Assert-Fixture ($null -ne $Changed -and $null -ne $Completed) 'non-null typed callbacks'
        Assert-Fixture ($Changed -is [Optimus.UpdateProgress.IDownloadProgressChangedCallback] -and $Completed -is [Optimus.UpdateProgress.IDownloadCompletedCallback]) 'download callback interfaces'
        Assert-Fixture ($null -eq $State) 'caller state has no job/callback ownership cycle'
        if ($script:BeginFailure) { throw 'fake BeginDownload failure' }
        if ($script:NullJob) { return $null }
        return $script:Job
    }
    $script:Owner | Add-Member ScriptMethod BeginInstall { param($Changed,$Completed,$State)
        $script:Begins++
        Assert-Fixture ($Changed -is [Optimus.UpdateProgress.IInstallationProgressChangedCallback] -and $Completed -is [Optimus.UpdateProgress.IInstallationCompletedCallback]) 'install callback interfaces'
        Assert-Fixture ($null -eq $State) 'install state has no reference cycle'
        if ($script:BeginFailure) { throw 'fake BeginInstall failure' }
        if ($script:NullJob) { return $null }
        return $script:Job
    }
    $script:Owner | Add-Member ScriptMethod EndDownload { param($Job)
        $script:Ends++
        Assert-Fixture ([object]::ReferenceEquals($Job,$script:Job)) 'EndDownload uses exact begun job'
        Assert-Fixture ($script:Reads -ge $script:Completion.Count) 'no End before strict completion'
        if ($script:EndFailure) { throw 'fake EndDownload failure' }
        return $script:Result
    }
    $script:Owner | Add-Member ScriptMethod EndInstall { param($Job)
        $script:Ends++
        Assert-Fixture ([object]::ReferenceEquals($Job,$script:Job)) 'EndInstall uses exact begun job'
        Assert-Fixture ($script:Reads -ge $script:Completion.Count) 'no install End before strict completion'
        if ($script:EndFailure) { throw 'fake EndInstall failure' }
        return $script:Result
    }
}
function Run-Fixture { param([string]$Phase='Download',[scriptblock]$Callback={param($Value) $script:Events.Add($Value)})
    Invoke-SCMUpdateProgress -Owner $script:Owner -Phase $Phase -ProgressCallback $Callback
}

Initialize-SCMUpdateProgressCallbacks
$callbacks=New-Object Optimus.UpdateProgress.NoOpCallbacks
$interfaces=@(
    @('Optimus.UpdateProgress.IDownloadProgressChangedCallback','8c3f1cdd-6173-4591-aebd-a56a53ca77c1'),
    @('Optimus.UpdateProgress.IDownloadCompletedCallback','77254866-9f5b-4c8e-b9e2-c77a8530d64b'),
    @('Optimus.UpdateProgress.IInstallationProgressChangedCallback','e01402d5-f8da-43ba-a012-38894bd048f1'),
    @('Optimus.UpdateProgress.IInstallationCompletedCallback','45f4f6f3-d602-4f98-9a8a-3efa152ad2d3'))
$unknown=[Runtime.InteropServices.Marshal]::GetIUnknownForObject($callbacks)
try {
    foreach($entry in $interfaces) {
        $type=$entry[0] -as [type]; $guid=[guid]$entry[1]; $pointer=[IntPtr]::Zero
        Assert-Fixture ($type.GUID -eq $guid) ('exact SDK callback GUID '+$entry[0])
        $hr=[Runtime.InteropServices.Marshal]::QueryInterface($unknown,[ref]$guid,[ref]$pointer)
        try { Assert-Fixture ($hr -eq 0 -and $pointer -ne [IntPtr]::Zero) 'native CCW exposes required IUnknown callback interface' }
        finally { if($pointer -ne [IntPtr]::Zero) { $null=[Runtime.InteropServices.Marshal]::Release($pointer) } }
        Assert-Fixture ($type.GetMethod('Invoke').ReturnType -eq [int]) 'SDK HRESULT return type'
        $parameters=$type.GetMethod('Invoke').GetParameters()
        Assert-Fixture ($parameters.Count -eq 2 -and $parameters[0].ParameterType -eq [IntPtr] -and $parameters[1].ParameterType -eq [IntPtr]) 'borrowed interface pointers retain native ABI'
        Assert-Fixture (($type.GetMethod('Invoke').GetMethodImplementationFlags() -band [Reflection.MethodImplAttributes]::PreserveSig) -ne 0) 'HRESULT PreserveSig'
    }
} finally { $null=[Runtime.InteropServices.Marshal]::Release($unknown) }
Assert-Fixture ($callbacks.Invoke([IntPtr]::Zero,[IntPtr]::Zero) -eq 0) 'callbacks are no-op S_OK without dereferencing parameters'

foreach($phase in @('Download','Install')) {
    Reset-Fixture
    $result=Run-Fixture $phase
    Assert-Fixture ([object]::ReferenceEquals($result,$script:Result)) 'exact native End result returned unchanged'
    Assert-Fixture ($script:Begins -eq 1 -and $script:Ends -eq 1) 'one Begin and matching End without replay'
    Assert-Fixture ($script:Polls -eq 3 -and $script:Sleeps -eq 2) 'fresh progress snapshot every poll'
    Assert-Fixture ($script:Events.Count -eq 5) 'initial unknown, three polls, verified completion'
    Assert-Fixture ($null -eq $script:Events[0].percent -and $null -eq $script:Events[3].percent) 'initial and pre-End 100 are unknown'
    Assert-Fixture ($script:Events[1].percent -eq 12 -and $script:Events[2].percent -eq 67 -and $script:Events[4].percent -eq 100) 'actual percentages followed by verified terminal100'
    Assert-Fixture (@($script:Events|Where-Object phase -ne $(if($phase -eq 'Download'){'downloading'}else{'installing'})).Count -eq 0) 'phase binding'
}
foreach($invalid in @($true,'78',-1,101,[double]::NaN,[double]::PositiveInfinity,'missing',$null)) {
    Reset-Fixture; $script:Completion=@($true); $script:Percent=@($invalid)
    $result=Run-Fixture
    Assert-Fixture (@($script:Events|Where-Object {$null -ne $_.percent}).Count -eq 0) 'malformed progress never becomes a measured percentage'
    Assert-Fixture ([object]::ReferenceEquals($result,$script:Result)) 'invalid display progress does not change final native result'
}
Reset-Fixture; $script:Percent=@((New-Object Exception('fake GetProgress failure')),45,100)
$result=Run-Fixture
Assert-Fixture ($null -eq $script:Events[1].percent -and $script:Events[2].percent -eq 45) 'progress read failure is unknown and later snapshots recover'
Reset-Fixture; $script:Completion=@((New-Object Exception('fake IsCompleted failure')),'true',$true)
$result=Run-Fixture
Assert-Fixture ($script:Reads -eq 3 -and $script:Sleeps -eq 2 -and $script:Ends -eq 1) 'completion-read errors/non-bools retain job until strict native true'
Reset-Fixture
$result=Run-Fixture Download {param($Value) throw 'fake observer failure'}
Assert-Fixture ([object]::ReferenceEquals($result,$script:Result) -and $script:Ends -eq 1) 'observer failure never abandons native job'
Reset-Fixture
$result=Run-Fixture Download {param($Value) 'untrusted display output'}
Assert-Fixture ([object]::ReferenceEquals($result,$script:Result)) 'observer output does not contaminate returned receipt'
foreach($bad in @('begin','null-job','end')) {
    Reset-Fixture
    if($bad -eq 'begin'){$script:BeginFailure=$true}
    if($bad -eq 'null-job'){$script:NullJob=$true}
    if($bad -eq 'end'){$script:EndFailure=$true}
    $caught=$false;try{Run-Fixture|Out-Null}catch{$caught=$true}
    Assert-Fixture ($caught -and $script:Begins -eq 1) 'unknown native error propagates without replay'
    Assert-Fixture (@($script:Events|Where-Object percent -eq 100).Count -eq 0) 'unknown native error never reports100'
    Assert-Fixture ($script:Ends -eq $(if($bad -eq 'end'){1}else{0})) 'no unmatched End'
}
foreach($invalidResult in @('failed','partial','missing-hresult','item-failed','missing-reboot')) {
    Reset-Fixture
    switch($invalidResult) {
        'failed' {$script:Result.ResultCode=4}
        'partial' {$script:Result.ResultCode=3}
        'missing-hresult' {$script:Result.HResult=$null}
        'item-failed' {$script:Item.ResultCode=4}
        'missing-reboot' {$script:Result.RebootRequired=$null}
    }
    $result=Run-Fixture Install
    Assert-Fixture ([object]::ReferenceEquals($result,$script:Result)) 'terminal result is returned without manufacturing success'
    Assert-Fixture (@($script:Events|Where-Object percent -eq 100).Count -eq 0) 'failed/partial/malformed terminal result does not report100'
}
Reset-Fixture; $script:Percent=@(10,20,90)
$result=Run-Fixture
Assert-Fixture (@($script:Events|Where-Object percent -eq 100).Count -eq 0) 'successful End does not invent an unobserved percentage'
[pscustomobject]@{Result='PASS';Assertions=$script:Assertions;RealScans=0;RealDownloads=0;RealInstalls=0;
    CallbackInterfaces=4;PSVersion=$PSVersionTable.PSVersion.ToString()}|ConvertTo-Json -Compress
