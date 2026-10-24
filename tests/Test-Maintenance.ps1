$ErrorActionPreference='Stop'
$ProgressPreference='SilentlyContinue'
. (Join-Path $PSScriptRoot '..\src\optimus\engine\MaintenanceOperations.ps1')
$script:Assertions=0
function Assert-OptimusTest {param([bool]$Condition,[string]$Message) if(-not $Condition){throw ('FAIL: '+$Message)};$script:Assertions++}
Assert-OptimusTest (Test-OptimusOptionalPackage 'Microsoft.MicrosoftOfficeHub' 'm365') 'M365 hub allowlist'
Assert-OptimusTest (Test-OptimusOptionalPackage 'Microsoft.Microsoft365Copilot' 'm365') 'M365 prefix only'
Assert-OptimusTest (-not(Test-OptimusOptionalPackage 'Microsoft.OutlookForWindows' 'm365')) 'Outlook separate'
Assert-OptimusTest (Test-OptimusOptionalPackage 'Microsoft.OutlookForWindows' 'outlook') 'Outlook opt-in'
foreach($name in @('Microsoft.WindowsStore','Microsoft.DesktopAppInstaller','Microsoft.SecHealthUI','Microsoft.WindowsAppRuntime.1.8','Microsoft.VCLibs.140.00','Microsoft.OneDriveSync','Microsoft.GamingApp','Microsoft.XboxIdentityProvider')){Assert-OptimusTest (-not(Test-OptimusOptionalPackage $name 'consumer-apps')) ('protected '+$name)}
Assert-OptimusTest (-not(Test-OptimusOptionalPackage 'Microsoft.Microsoft365*' 'm365')) 'wildcard name rejected'
$script:Packages=@(
 [pscustomobject]@{Name='Microsoft.BingWeather';PackageFullName='Microsoft.BingWeather_1.0.0.0_x64__8wekyb3d8bbwe';Version='1.0.0.0';NonRemovable=$false;IsFramework=$false;IsResourcePackage=$false},
 [pscustomobject]@{Name='Microsoft.BingNews';PackageFullName='Microsoft.BingNews_1.0.0.0_x64__8wekyb3d8bbwe';Version='1.0.0.0';NonRemovable=$true;IsFramework=$false;IsResourcePackage=$false},
 [pscustomobject]@{Name='Microsoft.Getstarted';PackageFullName='Microsoft.Getstarted_1.0.0.0_x64__8wekyb3d8bbwe';Version='1.0.0.0';NonRemovable=$false;IsFramework=$true;IsResourcePackage=$false},
 [pscustomobject]@{Name='Microsoft.WindowsStore';PackageFullName='Microsoft.WindowsStore_1.0.0.0_x64__8wekyb3d8bbwe';Version='1.0.0.0';NonRemovable=$false;IsFramework=$false;IsResourcePackage=$false}
)
$script:Calls=@()
function Get-AppxPackage {param($Name,$ErrorAction) if($Name){return @($script:Packages|Where-Object{$_.Name -eq $Name})};return $script:Packages}
function Remove-AppxPackage {param($Package,$ErrorAction) $script:Calls+=@{kind='appx';package=$Package};$script:Packages=@($script:Packages|Where-Object{$_.PackageFullName -cne $Package})}
function Get-AppxProvisionedPackage {param([switch]$Online,$ErrorAction) $script:Calls+=@{kind='provisioned-query'};return @()}
function Remove-AppxProvisionedPackage {param([switch]$Online,$PackageName,$ErrorAction) throw 'Unexpected provisioned mutation'}
function Start-Process {param($FilePath,$ArgumentList,$ErrorAction,[switch]$PassThru,$Verb,$WorkingDirectory,$WindowStyle,[switch]$Wait) throw 'No external program may execute in this test'}
function Test-OptimusMaintenanceAdmin {return $false}
$preview=Invoke-OptimusMaintenanceRequest -Operation maintenance_preview -Arguments @{action='consumer-apps';includeProvisioned=$false}
Assert-OptimusTest ($preview.targets.Count -eq 1) 'only removable ordinary app candidate'
Assert-OptimusTest (-not $preview.targets[0].selected) 'default deselected'
Assert-OptimusTest ($script:Calls.Count -eq 0) 'preview has no mutation or provisioning query'
$blocked=$false;try{Invoke-OptimusMaintenanceRequest -Operation maintenance_apply -Arguments @{action='consumer-apps';targets=@($preview.targets[0]);confirmation='yes'}|Out-Null}catch{$blocked=$true}
Assert-OptimusTest ($blocked -and $script:Calls.Count -eq 0) 'explicit confirmation gate'
$good=$preview.targets[0]
$bad=$good|ConvertTo-Json|ConvertFrom-Json;$bad.id='unknown';$blocked=$false
try{Invoke-OptimusMaintenanceRequest -Operation maintenance_apply -Arguments @{action='consumer-apps';targets=@($good,$bad);confirmation='REMOVE'}|Out-Null}catch{$blocked=$true}
Assert-OptimusTest ($blocked -and $script:Calls.Count -eq 0) 'all targets validated before mutation'
$script:Packages[0].PackageFullName='Microsoft.BingWeather_2.0.0.0_x64__8wekyb3d8bbwe';$blocked=$false
try{Invoke-OptimusMaintenanceRequest -Operation maintenance_apply -Arguments @{action='consumer-apps';targets=@($good);confirmation='REMOVE'}|Out-Null}catch{$blocked=$true}
Assert-OptimusTest ($blocked -and $script:Calls.Count -eq 0) 'stale version rejected'
$script:Packages[0].PackageFullName='Microsoft.BingWeather_1.0.0.0_x64__8wekyb3d8bbwe'
$script:ProgressEvents=@()
$observer={param($value) $script:ProgressEvents+=@($value); 'Observer output must not enter the operation result'}
$result=Invoke-OptimusMaintenanceRequest -Operation maintenance_apply -Arguments @{action='consumer-apps';targets=@($good);confirmation='REMOVE'} -ProgressCallback $observer
Assert-OptimusTest ($script:Calls.Count -eq 1 -and $result.results[0].removed) 'only exact selected package removed in fixture'
Assert-OptimusTest ($script:ProgressEvents.Count -eq 3) 'one opaque removal start, readback and verified completion'
Assert-OptimusTest (($script:ProgressEvents.phase -join '|') -ceq 'uninstall|verify|uninstall') 'native uninstall phases remain separate'
Assert-OptimusTest ($null -eq $script:ProgressEvents[0].percent -and $null -eq $script:ProgressEvents[1].percent -and $script:ProgressEvents[2].percent -eq 100) 'no invented intermediate uninstall percent'
Assert-OptimusTest (@($script:ProgressEvents|Where-Object{$_.key -cne ('debloat:consumer-apps:'+$good.id)}).Count -eq 0) 'progress bound to exact removal target'
Assert-OptimusTest ($result -isnot [array]) 'observer pipeline output discarded'
Assert-OptimusTest (@($script:Packages|Where-Object{$_.Name -eq 'Microsoft.WindowsStore'}).Count -eq 1) 'Store preserved in fixture'
$blocked=$false;try{Invoke-OptimusMaintenanceRequest -Operation maintenance_preview -Arguments @{action='consumer-apps';includeProvisioned=$true}|Out-Null}catch{$blocked=$true}
Assert-OptimusTest $blocked 'future-account scope requires admin'
$blocked=$false;try{Invoke-OptimusMaintenanceRequest -Operation maintenance_preview -Arguments @{action='unknown'}|Out-Null}catch{$blocked=$true}
Assert-OptimusTest $blocked 'unknown action rejected'
$script:Launches=@()
$script:FixtureHash='F'*64
function Get-FileHash {param($LiteralPath,$Algorithm,$ErrorAction) return [pscustomobject]@{Hash=$script:FixtureHash}}
function Start-Process {param($FilePath,$ArgumentList,$ErrorAction,[switch]$PassThru,$Verb,$WorkingDirectory,$WindowStyle,[switch]$Wait) $script:Launches+=[pscustomobject]@{File=$FilePath;Arguments=$ArgumentList;Verb=$Verb;WindowStyle=$WindowStyle;Wait=$Wait.IsPresent};return [pscustomobject]@{Id=12345}}
$external=[pscustomobject]@{id='desktop-edge-script';name='Fixture external';path='C:\OptimusFixture\Desktop\Specialized Computer Management\Remove-Edge.ps1';sha256=('F'*64)}
$launch=Start-OptimusDesktopEdgeScript -Target $external
Assert-OptimusTest ($script:Launches.Count -eq 1 -and $script:Launches[0].Verb -eq 'RunAs') 'external original script requests UAC'
Assert-OptimusTest (-not $script:Launches[0].WindowStyle -and -not $script:Launches[0].Wait) 'external console visible and user controlled'
Assert-OptimusTest ($script:Launches[0].Arguments -notmatch 'IncludeWebView2|IncludeBloatware|Preview|Bypass' -and $launch.requiresUserInteraction) 'no optional removal flags or policy bypass'
$script:FixtureHash='A'*64;$blocked=$false
try{Start-OptimusDesktopEdgeScript -Target $external|Out-Null}catch{$blocked=$true}
Assert-OptimusTest ($blocked -and $script:Launches.Count -eq 1) 'changed external script blocks launch'
# Retained Bing Wallpaper must be excluded from every current/future-user group.
foreach($action in @('consumer-apps','m365','outlook')) {
    foreach($name in @('Microsoft.BingWallpaper','Microsoft.BingWallpaper.Stable','Microsoft.Microsoft365BingWallpaper','Microsoft.Bing-Wallpaper')) {
        Assert-OptimusTest (-not(Test-OptimusOptionalPackage $name $action)) ('Bing Wallpaper retained '+$action+' '+$name)
    }
}
Assert-OptimusTest (Test-OptimusOptionalPackage 'Microsoft.BingWeather' 'consumer-apps') 'Weather remains independently optional'
Assert-OptimusTest (Test-OptimusOptionalPackage 'Microsoft.BingNews' 'consumer-apps') 'News remains independently optional'
$script:Packages=@(
 [pscustomobject]@{Name='Microsoft.BingWallpaper';PackageFullName='Microsoft.BingWallpaper_1.1.469.0_x86__8wekyb3d8bbwe';Version='1.1.469.0';NonRemovable=$false;IsFramework=$false;IsResourcePackage=$false},
 [pscustomobject]@{Name='Microsoft.Microsoft365BingWallpaper';PackageFullName='Microsoft.Microsoft365BingWallpaper_1.0_x64__fixture';Version='1.0';NonRemovable=$false;IsFramework=$false;IsResourcePackage=$false}
)
function Test-OptimusMaintenanceAdmin {return $true}
function Get-AppxProvisionedPackage {param([switch]$Online,$ErrorAction) return @([pscustomobject]@{DisplayName='Microsoft.BingWallpaper';PackageName='Microsoft.BingWallpaper_1.1.469.0_neutral_fixture';Version='1.1.469.0'},[pscustomobject]@{DisplayName='Microsoft.Microsoft365BingWallpaper';PackageName='Microsoft.Microsoft365BingWallpaper_1.0_neutral_fixture';Version='1.0'})}
foreach($action in @('consumer-apps','m365','outlook')) {
    Assert-OptimusTest (@(Get-OptimusMaintenanceTargets -Action $action -IncludeProvisioned $true).Count -eq 0) ('retained package absent from installed and provisioned '+$action)
}
$legacyFingerprint=Get-OptimusMaintenanceHash ('Microsoft.BingWallpaper|Microsoft.BingWallpaper_1.1.469.0_x86__8wekyb3d8bbwe|current-user')
$legacy=[pscustomobject]@{id=('appx-'+$legacyFingerprint.Substring(0,20));fingerprint=$legacyFingerprint;name='Microsoft.BingWallpaper'}
$beforeCalls=$script:Calls.Count;$blocked=$false
try{Invoke-OptimusMaintenanceRequest -Operation maintenance_apply -Arguments @{action='consumer-apps';targets=@($legacy);confirmation='REMOVE'}|Out-Null}catch{$blocked=$true}
Assert-OptimusTest ($blocked -and $script:Calls.Count -eq $beforeCalls) 'stale pre-protection Bing plan cannot remove retained package'
# Native Edge regression fixtures: every process launch and installed-state
# query is mocked. These checks never run WinGet or the Microsoft installer.
$script:EdgeExitCode=-1978335212
$script:EdgeRemaining=$true
$script:EdgeAdmin=$true
$script:EdgeLaunches=@()
function Get-OptimusEdgeTarget {if($script:EdgeRemaining){return @([pscustomobject]@{id='edge-browser'})};return @()}
function Test-OptimusMaintenanceAdmin {return $script:EdgeAdmin}
function Start-Process {param($FilePath,$ArgumentList,$ErrorAction,[switch]$PassThru,$Verb,$WorkingDirectory,$WindowStyle,[switch]$Wait) $script:EdgeLaunches+=[pscustomobject]@{File=$FilePath;Arguments=@($ArgumentList);WindowStyle=$WindowStyle;Wait=$Wait.IsPresent};return [pscustomobject]@{Id=12345;ExitCode=$script:EdgeExitCode}}
$edge=[pscustomobject]@{id='edge-browser';name='Microsoft Edge browser';method='winget';payload=[pscustomobject]@{executable='C:\OptimusFixture\winget.exe';packageId='Microsoft.Edge'}}
$edgeResult=Invoke-OptimusEdgeUninstall -Target $edge
Assert-OptimusTest ($edgeResult.exitCode -eq -1978335212 -and $edgeResult.exitCodeHex -ceq '0x8A150014') 'signed native package-not-found exit preserved exactly'
Assert-OptimusTest ($edgeResult.reason -ceq 'winget_no_applications_found' -and $edgeResult.requiresReview -and $edgeResult.skipped -and -not $edgeResult.removed -and $edgeResult.remainingDetected) 'package identity miss accurately requires review and retains Edge'
Assert-OptimusTest ($edgeResult.status -notmatch 'Windows may restrict' -and $script:EdgeLaunches.Count -eq 1 -and $script:EdgeLaunches[0].File -ceq 'C:\OptimusFixture\winget.exe') 'package identity miss does not claim a Windows restriction or launch a fallback'
Assert-OptimusTest (($script:EdgeLaunches[0].Arguments -join '|') -ceq 'uninstall|--id|Microsoft.Edge|--exact|--disable-interactivity') 'only exact stable package uninstall requested in fixture'
$script:EdgeRemaining=$false
$edgeResult=Invoke-OptimusEdgeUninstall -Target $edge
Assert-OptimusTest (-not $edgeResult.removed -and -not $edgeResult.remainingDetected -and $edgeResult.requiresReview) 'package-not-found never claims removal even if a concurrent inventory finds no Edge'
$script:EdgeExitCode=0;$script:EdgeRemaining=$true
$edgeResult=Invoke-OptimusEdgeUninstall -Target $edge
Assert-OptimusTest (-not $edgeResult.removed -and $edgeResult.reason -ceq 'edge_still_detected') 'successful process exit alone cannot claim removal'
$script:EdgeRemaining=$false
$edgeResult=Invoke-OptimusEdgeUninstall -Target $edge
Assert-OptimusTest ($edgeResult.removed -and -not $edgeResult.requiresReview -and $edgeResult.reason -ceq 'removal_verified') 'successful exit plus fresh absent inventory verifies removal'
$script:EdgeExitCode=3010;$script:EdgeRemaining=$true
$edgeResult=Invoke-OptimusEdgeUninstall -Target $edge
Assert-OptimusTest (-not $edgeResult.removed -and $edgeResult.restartNeeded -and $edgeResult.reason -ceq 'restart_required') 'restart result remains unverified until inventory confirms absence'
$edge.method='microsoft-setup';$edge.payload=[pscustomobject]@{path='C:\OptimusFixture\Microsoft\Edge\Installer\setup.exe';systemLevel=$true}
$script:EdgeExitCode=0
$edgeResult=Invoke-OptimusEdgeUninstall -Target $edge
$setupLaunch=$script:EdgeLaunches[-1]
Assert-OptimusTest (($setupLaunch.Arguments -join '|') -ceq '--uninstall|--verbose-logging|--system-level' -and $setupLaunch.WindowStyle -eq 'Hidden' -and $setupLaunch.Wait) 'Microsoft installer route uses normal uninstall without a force flag'
$script:EdgeAdmin=$false;$beforeLaunches=$script:EdgeLaunches.Count;$blocked=$false
try{Invoke-OptimusEdgeUninstall -Target $edge|Out-Null}catch{$blocked=$true}
Assert-OptimusTest ($blocked -and $script:EdgeLaunches.Count -eq $beforeLaunches) 'machine installer remains blocked without administrator permission'
[pscustomobject]@{Result='PASS';Assertions=$script:Assertions;RealAppRemovals=0;RealExternalProgramsLaunched=0;PSVersion=$PSVersionTable.PSVersion.ToString()}|ConvertTo-Json -Compress

