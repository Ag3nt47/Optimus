# Every native inventory, mutation and process launch in this fixture is mocked.
#requires -Version 5.1
$ErrorActionPreference='Stop'
$ProgressPreference='SilentlyContinue'
. (Join-Path $PSScriptRoot '..\src\optimus\engine\OfficeRemoval.ps1')
. (Join-Path $PSScriptRoot '..\src\optimus\engine\EdgeRemoval.ps1')
. (Join-Path $PSScriptRoot '..\src\optimus\engine\MaintenanceOperations.ps1')
$script:Assertions=0
function Assert-Fixture {param([bool]$Condition,[string]$Message) if(-not $Condition){throw ('FAIL: '+$Message)};$script:Assertions++}
function Test-OptimusMaintenanceAdmin {return $true}
$script:Packages=@()
$script:Calls=@()
$script:Events=@()
$script:Readback='normal'
$script:ExitCode=0
function Get-AppxPackage {param($Name,$ErrorAction) if($Name){return @($script:Packages|Where-Object{$_.Name -ceq $Name})};return $script:Packages}
function Get-AppxProvisionedPackage {param([switch]$Online,$ErrorAction) return @()}
function Remove-AppxProvisionedPackage {throw 'No provisioning mutation is permitted in this fixture.'}
function Remove-AppxPackage {
    param($Package,$ErrorAction)
    $script:Calls+=@($Package)
    if($script:Readback -eq 'leave-first' -and $Package -like 'Microsoft.BingWeather_*'){return}
    $script:Packages=@($script:Packages|Where-Object{$_.PackageFullName -cne $Package})
}
function Start-Process {throw 'A real native child must never be launched by this fixture.'}
function New-FixturePackages {
    $script:Packages=@(
        [pscustomobject]@{Name='Microsoft.BingWeather';PackageFullName='Microsoft.BingWeather_1.0_x64__fixture';Version='1.0';NonRemovable=$false;IsFramework=$false;IsResourcePackage=$false},
        [pscustomobject]@{Name='Microsoft.BingNews';PackageFullName='Microsoft.BingNews_1.0_x64__fixture';Version='1.0';NonRemovable=$false;IsFramework=$false;IsResourcePackage=$false}
    )
    $script:Calls=@();$script:Events=@()
}
$observer={param($value) $script:Events+=@($value)}
New-FixturePackages
$script:Readback='leave-first'
$preview=Invoke-OptimusMaintenanceRequest maintenance_preview @{action='consumer-apps'}
$preview.targets=@($preview.targets|Sort-Object name -Descending)
$result=Invoke-OptimusMaintenanceRequest maintenance_apply @{action='consumer-apps';targets=$preview.targets;confirmation='REMOVE'} -ProgressCallback $observer
Assert-Fixture ($script:Calls.Count -eq 2 -and $result.results.Count -eq 2) 'failed removal does not stop next reviewed app'
$first=@($script:Events|Where-Object{$_.label -ceq 'Microsoft.BingWeather'})
$second=@($script:Events|Where-Object{$_.label -ceq 'Microsoft.BingNews'})
Assert-Fixture (($first.phase -join '|') -ceq 'uninstall|verify|failed') 'failed readback emits failure and no completion'
Assert-Fixture (@($first|Where-Object{$null -ne $_.percent}).Count -eq 0) 'failed opaque removal never receives a percent'
Assert-Fixture (($second.phase -join '|') -ceq 'uninstall|verify|uninstall') 'next identity starts a fresh removal phase'
Assert-Fixture ($null -eq $second[0].percent -and $null -eq $second[1].percent -and $second[2].percent -eq 100) 'only verified target gets terminal100'
Assert-Fixture ($first[0].key -cne $second[0].key) 'each selected removal retains its exact item identity'
Assert-Fixture (-not @($result.results|Where-Object{$_.name -ceq 'Microsoft.BingWeather'})[0].removed -and @($result.results|Where-Object{$_.name -ceq 'Microsoft.BingNews'})[0].removed) 'native results preserve each actual outcome'
New-FixturePackages
$script:Readback='normal'
$preview=Invoke-OptimusMaintenanceRequest maintenance_preview @{action='consumer-apps'}
$result=Invoke-OptimusMaintenanceRequest maintenance_apply @{action='consumer-apps';targets=$preview.targets;confirmation='REMOVE'} -ProgressCallback {throw 'Display observer failed'}
Assert-Fixture ($script:Calls.Count -eq 2 -and @($result.results|Where-Object{$_.removed -eq $true}).Count -eq 2) 'observer exceptions cannot abandon or replay removals'
New-FixturePackages
$preview=Invoke-OptimusMaintenanceRequest maintenance_preview @{action='consumer-apps'}
$blocked=$false
try{Invoke-OptimusMaintenanceRequest maintenance_apply @{action='consumer-apps';targets=$preview.targets;confirmation='no'} -ProgressCallback $observer|Out-Null}catch{$blocked=$true}
Assert-Fixture ($blocked -and $script:Calls.Count -eq 0 -and $script:Events.Count -eq 0) 'invalid approval emits no progress or mutation'
$script:EdgeRoot='D:\OptimusFixture\Microsoft\Edge\Application'
$script:EdgeBrowser=$script:EdgeRoot+'\msedge.exe';$script:EdgeSetup=$script:EdgeRoot+'\157.0.0.0\Installer\setup.exe'
$script:EdgePresent=$true
$script:Edge=[pscustomobject]@{id='edge-browser';fingerprint=('a'*64);name='Microsoft Edge';kind='edge';scope='machine';admin=$true;payload=[pscustomobject]@{
    root=$script:EdgeRoot;browser=$script:EdgeBrowser;browserFile=[pscustomobject]@{path=$script:EdgeBrowser;sha256=('b'*64);version='157.0.0.0'};
    setup=[pscustomobject]@{path=$script:EdgeSetup;sha256=('c'*64);version='157.0.0.0'};winget=$null;packages=@();safe=$true}}
function Get-OptimusMaintenanceTargets {param($Action,[bool]$IncludeProvisioned) return @($script:Edge)}
function Get-OptimusEdgeRemovalTargets {param([bool]$IncludeProvisioned=$false) return @($script:Edge)}
function Get-OptimusEdgePackages {return @()}
function Test-OptimusEdgeRemovalAdmin {return $true}
function Get-OptimusEdgeSignedFile {
    param($Path)
    if($Path -ceq $script:EdgeBrowser){return $script:Edge.payload.browserFile}
    if($Path -ceq $script:EdgeSetup){return $script:Edge.payload.setup}
    throw 'Unexpected executable proof request in inert fixture.'
}
function Test-Path {
    param($LiteralPath,$PathType)
    if($LiteralPath -ceq $script:EdgeBrowser){return $script:EdgePresent}
    throw 'Unexpected filesystem probe in inert fixture.'
}
function Invoke-OptimusEdgeProcess {
    param($Path,$Arguments)
    $script:Calls+=@([pscustomobject]@{path=$Path;arguments=@($Arguments)})
    if($script:ExitCode -in @(0,3010)){$script:EdgePresent=$false}
    return $script:ExitCode
}
foreach($code in @(0,3010,1603)){
    $script:ExitCode=$code;$script:Events=@();$script:Calls=@();$script:EdgePresent=$true
    $result=Invoke-OptimusMaintenanceRequest maintenance_apply @{action='edge';targets=@($script:Edge);confirmation='REMOVE'} -ProgressCallback $observer
    Assert-Fixture ($script:Calls.Count -eq 1) ('exactly one mocked registered remover for '+$code)
    Assert-Fixture ($script:Calls[0].path -ceq $script:EdgeSetup -and $script:Calls[0].arguments -contains '--system-level' -and $script:Calls[0].arguments -contains '--msedge') ('exact current native remover and scope for '+$code)
    $complete=@($script:Events|Where-Object{$_.percent -eq 100})
    Assert-Fixture ($complete.Count -eq $(if($code -eq 0){2}else{0})) ('restart/error never produces terminal100; verified native and workflow terminal events '+$code)
    Assert-Fixture ($script:Events[0].phase -ceq 'uninstall' -and $null -eq $script:Events[0].percent) ('registered remover begins indeterminate '+$code)
    Assert-Fixture ($result.results[0].removed -eq ($code -in @(0,3010)) -and $result.results[0].restartNeeded -eq ($code -eq 3010)) ('actual absence and restart retain their separate meanings '+$code)
}
$script:Edge.payload.safe=$false;$script:Events=@();$script:Calls=@();$script:EdgePresent=$true
$result=Invoke-OptimusMaintenanceRequest maintenance_apply @{action='edge';targets=@($script:Edge);confirmation='REMOVE'} -ProgressCallback $observer
Assert-Fixture ($script:Calls.Count -eq 0 -and @($script:Events|Where-Object{$_.percent -eq 100}).Count -eq 0) 'unverified native Edge installation never launches a remover or receives completion percent'
Assert-Fixture (-not $result.results[0].removed -and $result.results[0].requiresReview -and $result.results[0].reason -ceq 'edge_removal_refused') 'unverified native installation is an explicit review finding'
$script:Edge=[pscustomobject]@{id='provisioned-fixture';fingerprint=('b'*64);name='Microsoft.BingWeather';kind='provisioned';scope='future-users';admin=$true;packageFullName='Microsoft.BingWeather_1.0_neutral__fixture'}
$script:NativeRemoval=@()
$script:ProvisionedReadback='absent'
function Remove-AppxProvisionedPackage {
    param([switch]$Online,$PackageName,$ErrorAction)
    $script:Calls+=@([pscustomobject]@{Online=$Online.IsPresent;PackageName=$PackageName;ErrorAction=$ErrorAction})
    return $script:NativeRemoval
}
function Get-AppxProvisionedPackage {
    param([switch]$Online,$ErrorAction)
    if($script:ProvisionedReadback -eq 'error'){throw 'Fixture post-removal provisioning inventory failed.'}
    if($script:ProvisionedReadback -eq 'present'){return @([pscustomobject]@{PackageName=$script:Edge.packageFullName})}
    return @()
}
foreach($case in @('false','true','missing','mistyped','multiple','none','true-readback-error','true-remains')){
    $script:Calls=@();$script:Events=@();$script:ProvisionedReadback='absent'
    $script:NativeRemoval=@([pscustomobject]@{Online=$true;RestartNeeded=$false})
    if($case -in @('true','true-readback-error','true-remains')){$script:NativeRemoval=@([pscustomobject]@{Online=$true;RestartNeeded=$true})}
    elseif($case -eq 'missing'){$script:NativeRemoval=@([pscustomobject]@{Online=$true})}
    elseif($case -eq 'mistyped'){$script:NativeRemoval=@([pscustomobject]@{Online=$true;RestartNeeded='false'})}
    elseif($case -eq 'multiple'){$script:NativeRemoval=@([pscustomobject]@{Online=$true;RestartNeeded=$false},[pscustomobject]@{Online=$true;RestartNeeded=$true})}
    elseif($case -eq 'none'){$script:NativeRemoval=@()}
    if($case -eq 'true-readback-error'){$script:ProvisionedReadback='error'}
    elseif($case -eq 'true-remains'){$script:ProvisionedReadback='present'}
    $result=Invoke-OptimusMaintenanceRequest maintenance_apply @{action='consumer-apps';includeProvisioned=$true;targets=@($script:Edge);confirmation='REMOVE'} -ProgressCallback $observer
    Assert-Fixture ($script:Calls.Count -eq 1 -and $script:Calls[0].Online -and $script:Calls[0].PackageName -ceq $script:Edge.packageFullName -and $script:Calls[0].ErrorAction -ceq 'Stop') ('original exact provisioning command preserved '+$case)
    Assert-Fixture (@($script:Events|Where-Object{$_.percent -eq 100}).Count -eq $(if($case -eq 'false'){1}else{0})) ('provisioning100 needs absent readback and known no-restart '+$case)
    if($case -in @('true','true-readback-error','true-remains')){
        Assert-Fixture ($result.results[0].restartNeeded -is [bool] -and $result.results[0].restartNeeded -eq $true -and $result.restartNeeded -eq $true) ('native restart retained including failed readback '+$case)
    }elseif($case -eq 'false'){
        Assert-Fixture ($result.results[0].restartNeeded -is [bool] -and $result.results[0].restartNeeded -eq $false -and $result.restartNeeded -is [bool] -and $result.restartNeeded -eq $false) 'native no-restart retained'
    }else{
        Assert-Fixture ($null -eq $result.results[0].restartNeeded -and $null -eq $result.restartNeeded) ('unknown native restart is never converted to false '+$case)
    }
    Assert-Fixture ($result.requiresReview -eq ($case -ne 'false') -and $result.results[0].requiresReview -eq ($case -ne 'false')) ('restart/unknown/error needs review '+$case)
    Assert-Fixture ($result.results[0].removed -eq ($case -notin @('true-readback-error','true-remains'))) ('only fresh absent inventory confirms provisioning removal '+$case)
}
[pscustomobject]@{Result='PASS';Assertions=$script:Assertions;RealAppRemovals=0;RealExternalProgramsLaunched=0;PSVersion=$PSVersionTable.PSVersion.ToString()}|ConvertTo-Json -Compress
