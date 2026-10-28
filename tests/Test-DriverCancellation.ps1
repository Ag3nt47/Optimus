# Offline WUA jobs only; no host search, download, install or process control.
#requires -Version 5.1
param([Parameter(Mandatory=$true)][string]$FixtureRoot)
$ErrorActionPreference='Stop'
. (Join-Path $PSScriptRoot 'Test-UpdateProgressIntegration.ps1') | Out-Null
$baseAssertions=$script:Assertions
function Start-Sleep {param([int]$Milliseconds) Assert-Test ($Milliseconds -eq 200) 'cooperative poll is bounded'}
function Add-CancellableSearchFixture {
    $script:SearchEnds=0;$script:SearchAborts=0
    $script:SearchJob=[pscustomobject]@{IsCompleted=$true}
    $script:SearchJob | Add-Member ScriptMethod RequestAbort {$script:SearchAborts++}
    $script:Searcher | Add-Member ScriptMethod BeginSearch {param($criteria,$callback,$state)
        Assert-Test ($callback -is [Optimus.UpdateProgress.ISearchCompletedCallback]) 'search callback has exact native interface'
        return $script:SearchJob
    }
    $script:Searcher | Add-Member ScriptMethod EndSearch {param($job)
        Assert-Test ([object]::ReferenceEquals($job,$script:SearchJob)) 'EndSearch uses the exact begun job'
        $script:SearchEnds++;return $script:SearchResult
    }
}

Initialize-SCMUpdateProgressCallbacks
$searchCallback=New-Object Optimus.UpdateProgress.NoOpSearchCallback
$unknown=[Runtime.InteropServices.Marshal]::GetIUnknownForObject($searchCallback)
$pointer=[IntPtr]::Zero;$iid=[guid]'88aee058-d4b0-4725-a2f1-814a67ae964c'
try {
    $hr=[Runtime.InteropServices.Marshal]::QueryInterface($unknown,[ref]$iid,[ref]$pointer)
    Assert-Test ($hr -eq 0 -and $pointer -ne [IntPtr]::Zero) 'CCW exposes the documented search callback IID'
    Assert-Test ($searchCallback.Invoke([IntPtr]::Zero,[IntPtr]::Zero) -eq 0) 'native callback is no-op S_OK without dereferencing pointers'
} finally {
    if($pointer -ne [IntPtr]::Zero){$null=[Runtime.InteropServices.Marshal]::Release($pointer)}
    $null=[Runtime.InteropServices.Marshal]::Release($unknown)
}

Reset-Fixture
$result=Invoke-SCMDriverUpdateApply -Record (Record-Fixture) -Unattended $true -AcceptEula $true -CancelRequested {$true}
Assert-Test ($result.navigation_cancelled -and -not $result.installer_started -and $result.outcomeKnown -and -not $result.running) 'prelaunch cancellation has known idle outcome'
Assert-Test ($script:Downloads -eq 0 -and $script:Installs -eq 0 -and $script:Accepts -eq 0) 'prelaunch skip does not accept or launch anything'

Reset-Fixture;Add-AsyncFixture;Add-CancellableSearchFixture
$script:Cancel=$false;$script:Aborts=0;$script:Reads=0
$script:DownloadJob.PSObject.Properties.Remove('IsCompleted')
$script:DownloadJob | Add-Member ScriptProperty IsCompleted {$script:Reads++;return ($script:Reads -ge 3)}
$script:DownloadJob | Add-Member -Force ScriptMethod GetProgress {$script:Cancel=$true;return [pscustomobject]@{CurrentUpdatePercentComplete=36}}
$script:DownloadJob | Add-Member ScriptMethod RequestAbort {$script:Aborts++;return $null}
$script:DownloadItem.ResultCode=5;$script:DownloadResult.ResultCode=5
$result=Invoke-SCMDriverUpdateApply -Record (Record-Fixture) -Unattended $true -AcceptEula $true -CancelRequested {$script:Cancel}
Assert-Test ($script:Aborts -eq 1 -and $script:Reads -eq 3 -and $script:AsyncDownloadEnds -eq 1) 'download abort requested once, completion and matching End still awaited'
Assert-Test ($result.navigation_cancelled -and -not $result.installer_started -and $result.outcomeKnown -and -not $result.running) 'terminal aborted download can safely skip before installation'
Assert-Test ($script:AsyncInstalls -eq 0 -and $script:Installs -eq 0) 'cancelled download never starts installer'

Reset-Fixture;Add-AsyncFixture;Add-CancellableSearchFixture
$script:Cancel=$false;$script:Aborts=0;$script:Reads=0
$script:InstallJob.PSObject.Properties.Remove('IsCompleted')
$script:InstallJob | Add-Member ScriptProperty IsCompleted {$script:Reads++;$script:Cancel=$true;return ($script:Reads -ge 3)}
$script:InstallJob | Add-Member ScriptMethod RequestAbort {$script:Aborts++;throw 'installation must never be aborted'}
$result=Invoke-SCMDriverUpdateApply -Record (Record-Fixture) -Unattended $true -AcceptEula $true -ProgressCallback {param($p)} -CancelRequested {$script:Cancel}
Assert-Test ($script:Aborts -eq 0 -and $script:Reads -eq 3 -and $script:AsyncInstallEnds -eq 1) 'installation cancellation waits matching End without aborting the installer'
Assert-Test ($result.installer_started -and $result.resultCode -eq 2 -and $result.outcomeKnown -and -not $result.running -and -not $result.cancelled) 'actual completed installation outcome is retained'

Reset-Fixture;Add-AsyncFixture;Add-CancellableSearchFixture
$script:Cancel=$false
$script:DownloadJob | Add-Member -Force ScriptMethod GetProgress {$script:Cancel=$true;return [pscustomobject]@{CurrentUpdatePercentComplete=36}}
$script:Downloader | Add-Member -Force ScriptMethod EndDownload {param($job) throw 'unknown terminal download state'}
$caught=$false;try{Invoke-SCMDriverUpdateApply -Record (Record-Fixture) -Unattended $true -AcceptEula $true -CancelRequested {$script:Cancel}|Out-Null}catch{$caught=$true}
Assert-Test ($caught -and $script:AsyncInstalls -eq 0) 'unknown EndDownload cannot become a successful cancellation receipt'

Reset-Fixture;Add-AsyncFixture;Add-CancellableSearchFixture
$script:Cancel=$false;$script:SearchReads=0
$script:SearchJob.PSObject.Properties.Remove('IsCompleted')
$script:SearchJob | Add-Member ScriptProperty IsCompleted {$script:SearchReads++;$script:Cancel=$true;return ($script:SearchReads -ge 3)}
$result=Invoke-SCMDriverUpdateApply -Record (Record-Fixture) -Unattended $true -AcceptEula $true -CancelRequested {$script:Cancel}
Assert-Test ($script:SearchAborts -eq 1 -and $script:SearchReads -eq 3 -and $script:SearchEnds -eq 1) 'search abort requested once, exact terminal End awaited'
Assert-Test ($result.navigation_cancelled -and $script:Downloads -eq 0 -and $script:Installs -eq 0) 'cancelled search cannot authorize download or install'

$DataRoot=$FixtureRoot
$request=[pscustomobject]@{id=('a'*32);arguments=@{cooperativeCancellation=$true}}
$callback=New-OptimusCancellationCallback $request
Assert-Test (-not (& $callback)) 'absent exact marker does not cancel'
$other=Join-Path (Join-Path (Join-Path $DataRoot 'request-cancellation') ('b'*32)) 'cancel.request'
$null=New-Item -ItemType Directory -Path (Split-Path $other -Parent) -Force
[IO.File]::WriteAllText($other,'cancel')
Assert-Test (-not (& $callback)) 'another native request marker cannot cancel this job'
$own=Join-Path (Join-Path (Join-Path $DataRoot 'request-cancellation') ('a'*32)) 'cancel.request'
$null=New-Item -ItemType Directory -Path (Split-Path $own -Parent) -Force
[IO.File]::WriteAllText($own,'cancel')
Assert-Test (& $callback) 'only the exact request-owned marker cancels this job'
[pscustomobject]@{Result='PASS';Assertions=($script:Assertions-$baseAssertions);RealScans=0;RealDownloads=0;RealInstalls=0}|ConvertTo-Json -Compress
