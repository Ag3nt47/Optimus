#requires -Version 5.1
# Inert PowerShell jobs only: no WUA sessions, host scans, downloads or installs.
$ErrorActionPreference='Stop'
. (Join-Path (Split-Path $PSScriptRoot -Parent) 'src/optimus/engine/UpdateProgress.ps1')
. (Join-Path (Split-Path $PSScriptRoot -Parent) 'src/optimus/engine/FileTools.ps1')
$script:Assertions=0
function Assert-Test {param([bool]$Condition,[string]$Message)
    $script:Assertions++
    if(-not $Condition){throw ('Fixture assertion failed: '+$Message)}
}
function Start-Sleep {param([int]$Milliseconds)
    Assert-Test ($Milliseconds -eq 200) 'bounded owned-job polling interval'
    if($script:Mode -eq 'not_completed' -and $script:Reads -ge 2){throw 'INERT_POLL_TIMEOUT'}
}
function Reset-Fixture {param([string]$Mode='cancelled')
    $script:Mode=$Mode;$script:Reads=0;$script:Aborts=0;$script:Ends=0;$script:Begins=0;$script:Cancel=$false
    $script:Job=[pscustomobject]@{}
    $script:Job|Add-Member ScriptProperty IsCompleted {
        $script:Reads++
        if($script:Mode -ne 'no_abort'){$script:Cancel=$true}
        if($script:Mode -eq 'not_completed'){return $false}
        return ($script:Reads -ge 3)
    }
    $script:Job|Add-Member ScriptMethod RequestAbort {
        $script:Aborts++
        if($script:Mode -eq 'abort_failed'){throw [UnauthorizedAccessException]::new('Inert abort failure')}
    }
    $script:Job|Add-Member ScriptMethod GetProgress {
        return [pscustomobject]@{CurrentUpdatePercentComplete=[int]25}
    }
    $script:Owner=[pscustomobject]@{Online=$true;ServerSelection=[int]1;ServiceID='inert-fixture'}
    $begin={param($first,$second,$third)
        $script:Begins++
        return $script:Job
    }
    $end={param($job)
        $script:Ends++
        Assert-Test ([object]::ReferenceEquals($job,$script:Job)) 'exact matching owned End job'
        Assert-Test ($script:Reads -ge 3) 'strict completion precedes End call'
        $hr=if($script:Mode -eq 'wrong_hresult'){[int]-2147024891}else{[int]-2145124341}
        $failure=[Runtime.InteropServices.COMException]::new('Inert native HRESULT fixture',$hr)
        if($script:Mode -eq 'wrapped'){
            throw [InvalidOperationException]::new('Inert wrapper', $failure)
        }
        throw $failure
    }
    foreach($method in @('BeginSearch','BeginDownload','BeginInstall')){$script:Owner|Add-Member ScriptMethod $method $begin}
    foreach($method in @('EndSearch','EndDownload','EndInstall')){$script:Owner|Add-Member ScriptMethod $method $end}
    $script:Session=[pscustomobject]@{}
    $script:Session|Add-Member ScriptMethod CreateUpdateSearcher {return $script:Owner}
}
function New-SCMWuaSession {return $script:Session}
function Run-Fixture {param([string]$Phase)
    if($Phase -eq 'Search'){
        return Invoke-SCMUpdateSearch -Owner $script:Owner -Criteria 'INERT_ONLY' -CancelRequested {$script:Cancel}
    }
    return Invoke-SCMUpdateProgress -Owner $script:Owner -Phase $Phase -CancelRequested {$script:Cancel}
}

foreach($phase in @('Search','Download')){
    foreach($mode in @('cancelled','wrapped')){
        Reset-Fixture $mode
        $result=Run-Fixture $phase
        Assert-Test ($result.ResultCode -eq 5 -and $result.HResult -eq -2145124341) 'native cancellation HRESULT is retained as aborted, never success'
        Assert-Test ($result.native_cancelled -and $result.job_completed -and $result.abort_requested) 'exact completed abort facts are explicit'
        Assert-Test ($result.outcomeKnown -and -not $result.running -and -not $result.installer_started) 'terminal preinstall cancellation is known idle'
        Assert-Test ($script:Aborts -eq 1 -and $script:Ends -eq 1 -and $script:Reads -eq 3) 'one abort, strict completion, one matching End'
        if($phase -eq 'Download'){
            Assert-Test ($result.GetUpdateResult(0).ResultCode -eq 5) 'single download item remains aborted'
            $refused=$false;try{$result.GetUpdateResult(1)|Out-Null}catch{$refused=$true}
            Assert-Test $refused 'no invented second download item'
        }
    }
    foreach($mode in @('no_abort','abort_failed','wrong_hresult','not_completed')){
        Reset-Fixture $mode
        $caught=$false;try{Run-Fixture $phase|Out-Null}catch{$caught=$true}
        Assert-Test $caught 'unproven cancellation and other HRESULTs remain exceptions'
        if($mode -eq 'no_abort'){Assert-Test ($script:Aborts -eq 0) 'no unrequested abort'}
        if($mode -eq 'not_completed'){Assert-Test ($script:Ends -eq 0) 'false completion cannot call End or establish cancellation'}
    }
}

Reset-Fixture
$caught=$false;try{Run-Fixture 'Install'|Out-Null}catch{$caught=$true}
Assert-Test ($caught -and $script:Aborts -eq 0 -and $script:Ends -eq 1) 'installation is never aborted or converted into preinstall cancellation'

foreach($mode in @('cancelled','wrong_hresult','abort_failed','not_completed')){
    Reset-Fixture $mode
    $result=Get-SCMDriverUpdateScan -CancelRequested {$script:Cancel}
    if($mode -eq 'cancelled'){
        Assert-Test ($result.outcomeKnown -and -not $result.running -and -not $result.uncertain) 'owned terminal cancelled search stays known through scan facade'
    }else{
        Assert-Test (-not $result.outcomeKnown -and $result.running -and $result.uncertain) 'unknown async search cannot be swallowed into a retired scan'
    }
    Assert-Test (-not $result.installer_started -and $result.returned_offer_count -eq 0) 'scan facade never authorizes an installer or offers after cancellation'
}
[pscustomobject]@{Result='PASS';Assertions=$script:Assertions;RealScans=0;RealDownloads=0;RealInstalls=0}|ConvertTo-Json -Compress
