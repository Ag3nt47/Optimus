#requires -Version 5.1
# The production bridge dispatcher and driver gates run with offline fake WUA.
# No real Windows Update session, scan, download, installation or host change.
$ErrorActionPreference='Stop'
. (Join-Path $PSScriptRoot 'Test-AutomatedSystem.ps1') | Out-Null
$baseAssertions=$script:Assertions
$engine=Join-Path (Split-Path $PSScriptRoot -Parent) 'src/optimus/engine'
. (Join-Path $engine 'UpdateProgress.ps1')
$tokens=$null;$errors=$null
$ast=[Management.Automation.Language.Parser]::ParseFile((Join-Path $engine 'OptimusBridge.ps1'),[ref]$tokens,[ref]$errors)
Assert-Test ($errors.Count -eq 0) 'production bridge parses in Windows PowerShell 5.1'
foreach($name in @('New-OptimusProgressCallback','New-OptimusCancellationCallback','Invoke-OptimusRequest')) {
    $node=$ast.Find({param($n)$n -is [Management.Automation.Language.FunctionDefinitionAst] -and $n.Name -eq $name},$false)
    Assert-Test ($null -ne $node) ('production function present: '+$name)
    . ([scriptblock]::Create($node.Extent.Text))
}
function Add-AsyncFixture {
    $script:AsyncDownloads=0;$script:AsyncInstalls=0;$script:AsyncDownloadEnds=0;$script:AsyncInstallEnds=0
    $script:AsyncBeginFailure=$false;$script:AsyncEndFailure=$false;$script:RestartAfterDownload=$false
    $script:DownloadJob=[pscustomobject]@{IsCompleted=$true}
    $script:DownloadJob | Add-Member ScriptMethod GetProgress {return [pscustomobject]@{CurrentUpdatePercentComplete=36}}
    $script:InstallJob=[pscustomobject]@{IsCompleted=$true}
    $script:InstallJob | Add-Member ScriptMethod GetProgress {return [pscustomobject]@{CurrentUpdatePercentComplete=64}}
    $script:Downloader | Add-Member ScriptMethod BeginDownload {param($Changed,$Completed,$State)
        $script:AsyncDownloads++
        Assert-Test ($script:Installer.ForceQuiet -and -not $script:Installer.AllowSourcePrompts -and -not $this.IsForced) 'all quiet/no-source/no-force gates before async download'
        Assert-Test ($Changed -is [Optimus.UpdateProgress.IDownloadProgressChangedCallback] -and $Completed -is [Optimus.UpdateProgress.IDownloadCompletedCallback]) 'download callbacks use documented interfaces'
        if($script:AsyncBeginFailure){throw 'fake async Begin failure'}
        return $script:DownloadJob
    }
    $script:Downloader | Add-Member ScriptMethod EndDownload {param($Job)
        $script:AsyncDownloadEnds++
        Assert-Test ([object]::ReferenceEquals($Job,$script:DownloadJob)) 'matching download job passed to End'
        if($script:RestartAfterDownload){$script:Pending=$true}
        return $this.Download()
    }
    $script:Installer | Add-Member ScriptMethod BeginInstall {param($Changed,$Completed,$State)
        $script:AsyncInstalls++
        Assert-Test ($this.ForceQuiet -and -not $this.AllowSourcePrompts -and -not $this.IsForced -and -not $this.IsBusy) 'all quiet/no-source/no-force/notbusy gates before async install'
        Assert-Test ($Changed -is [Optimus.UpdateProgress.IInstallationProgressChangedCallback] -and $Completed -is [Optimus.UpdateProgress.IInstallationCompletedCallback]) 'install callbacks use documented interfaces'
        return $script:InstallJob
    }
    $script:Installer | Add-Member ScriptMethod EndInstall {param($Job)
        $script:AsyncInstallEnds++
        Assert-Test ([object]::ReferenceEquals($Job,$script:InstallJob)) 'matching install job passed to End'
        if($script:AsyncEndFailure){throw 'fake unknown async End result'}
        return $this.Install()
    }
}
function Invoke-BridgeFixture {param($ProgressFlag=$true,[bool]$IncludeFlag=$true)
    $arguments=@{record=(Record-Fixture);unattended=$true;acceptEula=$true}
    if($IncludeFlag){$arguments['reportProgress']=$ProgressFlag}
    $request=[pscustomobject]@{id=('a'*32);operation='driver_update_apply';arguments=$arguments}
    $original=[Console]::Out;$writer=New-Object IO.StringWriter
    try {[Console]::SetOut($writer);$script:BridgeResult=Invoke-OptimusRequest -Request $request}
    finally {[Console]::SetOut($original);$script:Wire=$writer.ToString()}
    return @($script:Wire -split '\r?\n' | Where-Object {$_} | ForEach-Object {ConvertFrom-Json -InputObject $_})
}

Reset-Fixture
$synchronous=Invoke-SCMDriverUpdateApply -Record (Record-Fixture) -Unattended $true -AcceptEula $true
$expected=$synchronous|ConvertTo-Json -Depth 8 -Compress
Reset-Fixture;Add-AsyncFixture
$frames=@(Invoke-BridgeFixture)
Assert-Test (($script:BridgeResult|ConvertTo-Json -Depth 8 -Compress) -ceq $expected) 'async and original synchronous final receipts are identical'
Assert-Test ($script:AsyncDownloads -eq 1 -and $script:AsyncDownloadEnds -eq 1 -and $script:AsyncInstalls -eq 1 -and $script:AsyncInstallEnds -eq 1) 'exact one matching Begin/End per operation'
Assert-Test ($frames.Count -eq 5) 'download/install native values plus explicit post-install verification'
Assert-Test (@($frames|Where-Object {$_.kind -cne 'progress' -or $_.id -cne ('a'*32)}).Count -eq 0) 'every frame is explicit progress and bound to exact request'
Assert-Test ($frames[0].progress.phase -ceq 'downloading' -and $null -eq $frames[0].progress.percent -and $frames[1].progress.percent -eq 36) 'download phase reset and actual progress'
Assert-Test ($frames[2].progress.phase -ceq 'installing' -and $null -eq $frames[2].progress.percent -and $frames[3].progress.percent -eq 64) 'install phase reset and actual progress'
Assert-Test ($frames[4].progress.phase -ceq 'verify' -and $null -eq $frames[4].progress.percent) 'post-install search has unknown progress until its actual result is known'
Assert-Test (@($frames|Where-Object {$_.PSObject.Properties.Name -contains 'ok' -or $_.PSObject.Properties.Name -contains 'result'}).Count -eq 0) 'progress frame can never masquerade as terminal response'

Reset-Fixture;Add-AsyncFixture
$script:DownloadJob | Add-Member -Force ScriptMethod GetProgress {
    return [pscustomobject]@{CurrentUpdatePercentComplete=36;CurrentUpdateBytesDownloaded=[decimal]360;
        CurrentUpdateBytesToDownload=[decimal]1000}
}
$frames=@(Invoke-BridgeFixture)
Assert-Test ($frames[1].progress.downloaded_bytes -eq 360 -and $frames[1].progress.download_total_bytes -eq 1000) 'exact native download bytes reach progress wire'
Assert-Test ($script:BridgeResult.download_progress.downloaded_bytes -eq 360 -and $script:BridgeResult.download_progress.bytes_source -eq 'windows_update') 'last measured download counter remains in terminal receipt'
Assert-Test ($script:BridgeResult.resultCode -eq 2 -and $script:AsyncInstalls -eq 1) 'download observation does not change native install outcome'
Assert-Test (@($frames | Where-Object {$_.progress.phase -eq 'installing' -and $_.progress.PSObject.Properties.Name -contains 'downloaded_bytes'}).Count -eq 0) 'install phase never relabels download size'

Reset-Fixture;Add-AsyncFixture
$script:DownloadJob | Add-Member -Force ScriptMethod GetProgress {
    return [pscustomobject]@{CurrentUpdatePercentComplete=36;CurrentUpdateBytesDownloaded=[decimal]360;
        CurrentUpdateBytesToDownload=[decimal]1000}
}
$script:DownloadItem.ResultCode=4;$script:DownloadResult.ResultCode=4
$frames=@(Invoke-BridgeFixture)
Assert-Test ($script:BridgeResult.failureKind -eq 'download_failed' -and $script:BridgeResult.download_progress.downloaded_bytes -eq 360 -and $script:AsyncInstalls -eq 0) 'failed download retains observed bytes and never counts an installation'

foreach($flag in @($false,'true',$null)) {
    Reset-Fixture;Add-AsyncFixture
    $frames=@(Invoke-BridgeFixture -ProgressFlag $flag)
    Assert-Test ($frames.Count -eq 0 -and $script:AsyncDownloads -eq 0 -and $script:AsyncInstalls -eq 0 -and $script:Downloads -eq 1 -and $script:Installs -eq 1) 'no observer keeps original sync path, non-boolean flag cannot opt in'
}
Reset-Fixture;Add-AsyncFixture
$frames=@(Invoke-BridgeFixture -IncludeFlag $false)
Assert-Test ($frames.Count -eq 0 -and $script:AsyncDownloads -eq 0 -and $script:AsyncInstalls -eq 0) 'legacy request remains synchronous'

foreach($guard in @('admin','agreement','restart','busy','quiet','missing','unknown-interaction')) {
    Reset-Fixture;Add-AsyncFixture;$accept=$true
    switch($guard) {
        'admin' {$script:Admin=$false}
        'agreement' {$accept=$false}
        'restart' {$script:Pending=$true}
        'busy' {$script:Installer.IsBusy=$true}
        'quiet' {$script:Installer.PSObject.Properties.Remove('ForceQuiet')}
        'missing' {$script:SearchResult.Updates=@()}
        'unknown-interaction' {$script:Update.InstallationBehavior.CanRequestUserInput=$null}
    }
    try {Invoke-SCMDriverUpdateApply -Record (Record-Fixture) -Unattended $true -AcceptEula $accept -ProgressCallback {}|Out-Null}catch{}
    Assert-Test ($script:AsyncDownloads -eq 0 -and $script:AsyncInstalls -eq 0 -and $script:Downloads -eq 0 -and $script:Installs -eq 0) ('existing preflight cannot be bypassed by observer: '+$guard)
}
Reset-Fixture;Add-AsyncFixture;$script:RestartAfterDownload=$true
$caught=$false;try{Invoke-BridgeFixture|Out-Null}catch{$caught=$true}
Assert-Test ($caught -and $script:AsyncDownloads -eq 1 -and $script:AsyncInstalls -eq 0) 'fresh pending restart after download still prevents install'
Reset-Fixture;Add-AsyncFixture;$script:DownloadItem.ResultCode=4;$script:DownloadResult.ResultCode=4
$frames=@(Invoke-BridgeFixture)
Assert-Test ($script:BridgeResult.failureKind -eq 'download_failed' -and $script:BridgeResult.installer_started -eq $false -and $script:AsyncInstalls -eq 0) 'known failed download retains exact preflight refusal without install'
Reset-Fixture;Add-AsyncFixture;$script:AsyncBeginFailure=$true
$caught=$false;try{Invoke-BridgeFixture|Out-Null}catch{$caught=$true}
Assert-Test ($caught -and $script:AsyncDownloads -eq 1 -and $script:AsyncDownloadEnds -eq 0 -and $script:Downloads -eq 0 -and $script:AsyncInstalls -eq 0) 'async Begin failure never retries synchronous method'
Reset-Fixture;Add-AsyncFixture;$script:AsyncEndFailure=$true
$caught=$false;try{Invoke-BridgeFixture|Out-Null}catch{$caught=$true}
Assert-Test ($caught -and $script:AsyncInstalls -eq 1 -and $script:AsyncInstallEnds -eq 1 -and $script:Installs -eq 0) 'unknown End failure propagates without manufacturing terminal receipt or replay'

$first=New-OptimusProgressCallback -RequestId ('b'*32)
$second=New-OptimusProgressCallback -RequestId ('c'*32)
$original=[Console]::Out;$writer=New-Object IO.StringWriter
try {[Console]::SetOut($writer);& $first ([pscustomobject]@{phase='downloading';percent=11});& $second ([pscustomobject]@{phase='installing';percent=22})}
finally {[Console]::SetOut($original)}
$values=@($writer.ToString() -split '\r?\n'|Where-Object {$_}|ForEach-Object {ConvertFrom-Json -InputObject $_})
Assert-Test ($values[0].id -ceq ('b'*32) -and $values[1].id -ceq ('c'*32)) 'closures bind separate immutable request identities'

foreach($asJsonObject in @($false,$true)){
 foreach($cleanupEnabled in @($false,$true)){
  Reset-Fixture
  $arguments=@{record=(Record-Fixture);unattended=$true;acceptEula=$true;cleanupDownloads=$cleanupEnabled}
  if($asJsonObject){$arguments=[pscustomobject]$arguments}
  $request=[pscustomobject]@{id=('d'*32);operation='driver_update_apply';arguments=$arguments}
  $result=Invoke-OptimusRequest -Request $request
  Assert-Test ($result.payload_cleanup.requested_enabled -eq $cleanupEnabled -and $script:Installs -eq 1) 'cleanup choice survives native dispatcher for dictionary and JSON requests'
 }
 foreach($invalid in @('false',1,$null)){
  Reset-Fixture
  $arguments=@{record=(Record-Fixture);unattended=$true;acceptEula=$true;cleanupDownloads=$invalid}
  if($asJsonObject){$arguments=[pscustomobject]$arguments}
  $request=[pscustomobject]@{id=('e'*32);operation='driver_update_apply';arguments=$arguments}
  $caught=$false;try{Invoke-OptimusRequest -Request $request|Out-Null}catch{$caught=$true}
  Assert-Test ($caught -and $script:Downloads -eq 0 -and $script:Installs -eq 0) 'invalid cleanup choice refused before driver download/install'
 }
}
[pscustomobject]@{Result='PASS';Assertions=($script:Assertions-$baseAssertions);BaseAssertions=$baseAssertions;
    RealScans=0;RealDownloads=0;RealInstalls=0;PSVersion=$PSVersionTable.PSVersion.ToString()}|ConvertTo-Json -Compress
