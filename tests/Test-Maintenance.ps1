$ErrorActionPreference='Stop'
$ProgressPreference='SilentlyContinue'
. (Join-Path $PSScriptRoot '..\src\optimus\engine\EdgeRemoval.ps1')
. (Join-Path $PSScriptRoot '..\src\optimus\engine\OfficeRemoval.ps1')
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
# Windows App is preserved even before a saved exclusion has been created.
$windowsApp=[pscustomobject]@{Name='MicrosoftCorporationII.Windows365';PackageFullName='MicrosoftCorporationII.Windows365_7.5.9.0_x64__8wekyb3d8bbwe';PackageFamilyName='MicrosoftCorporationII.Windows365_8wekyb3d8bbwe';Version='7.5.9.0';NonRemovable=$false;IsFramework=$false;IsResourcePackage=$false}
$weather=[pscustomobject]@{Name='Microsoft.BingWeather';PackageFullName='Microsoft.BingWeather_1.0.0.0_x64__8wekyb3d8bbwe';Version='1.0.0.0';NonRemovable=$false;IsFramework=$false;IsResourcePackage=$false}
$script:Packages=@($windowsApp,$weather)
$exclusion=[pscustomobject]@{kind='store';id=$windowsApp.PackageFamilyName;name=$windowsApp.Name;label='Microsoft Windows App'}
$unprotectedPreview=Invoke-OptimusMaintenanceRequest maintenance_preview @{action='consumer-apps'}
$protectedPreview=Invoke-OptimusMaintenanceRequest maintenance_preview @{action='consumer-apps';removalExclusions=@($exclusion)}
Assert-OptimusTest ($unprotectedPreview.targets.Count -eq 1 -and $unprotectedPreview.targets[0].name -ceq $weather.Name) 'Windows App never enters the consumer removal candidates'
Assert-OptimusTest ($protectedPreview.targets.Count -eq 1 -and $protectedPreview.targets[0].name -ceq $weather.Name) 'saved Windows App protection retains unrelated removable targets'
$beforeCalls=$script:Calls.Count
$protectedResult=Invoke-OptimusMaintenanceRequest maintenance_apply @{action='consumer-apps';targets=$unprotectedPreview.targets;confirmation='REMOVE';removalExclusions=@($exclusion)}
Assert-OptimusTest ($script:Calls.Count -eq ($beforeCalls+1) -and $script:Calls[-1].package -ceq $weather.PackageFullName) 'stale mixed plan removes only the unprotected fixture'
Assert-OptimusTest ($protectedResult.results.Count -eq 1 -and $protectedResult.results[0].removed -eq $true) 'unrelated removable package continues while Windows App remains excluded'
Assert-OptimusTest ($script:Packages.Count -eq 1 -and $script:Packages[0].PackageFamilyName -ceq $windowsApp.PackageFamilyName) 'Windows App survives native apply'
$provisioned=[pscustomobject]@{kind='provisioned';name=$windowsApp.Name;packageFullName='MicrosoftCorporationII.Windows365_8.0.0.0_neutral_~_8wekyb3d8bbwe'}
Assert-OptimusTest (Test-OptimusExcludedRemovalTarget $provisioned @($exclusion)) 'provisioned release matches the same stable family'
Assert-OptimusTest (Test-OptimusExcludedRemovalTarget $provisioned @()) 'mandatory Windows App preservation does not depend on saved configuration'
$provisioned.packageFullName='MicrosoftCorporationII.Windows365_8.0.0.0_neutral_~_otherpub00000'
Assert-OptimusTest (Test-OptimusExcludedRemovalTarget $provisioned @()) 'Windows App name is conservatively preserved for any publisher metadata'

# New module dispatch is exercised with strict fixture signatures: unknown
# parameters, accidental direct launches or changed selections cannot pass.
$script:EdgeCalls=@();$script:OfficeCalls=@();$script:EdgeProvisionedFlags=@()
$script:EdgeFixture=[pscustomobject]@{id='edge-fixture';name='Microsoft Edge browser';kind='edge';method='edge-scoped';fingerprint=('e'*64);admin=$false;scope='current-user'}
function Get-OptimusEdgeRemovalTargets {param([bool]$IncludeProvisioned=$false)$script:EdgeProvisionedFlags+=@($IncludeProvisioned);return @($script:EdgeFixture)}
function Invoke-OptimusEdgeRemovalTarget {[CmdletBinding()]param($Target,[scriptblock]$ProgressCallback)$script:EdgeCalls+=@($Target.id);return [pscustomobject]@{id=$Target.id;name=$Target.name;removed=$true;restartNeeded=$false;sharedComponentsPreserved=$true}}
function Start-Process {param($FilePath,$ArgumentList,$ErrorAction,[switch]$PassThru,$Verb,$WorkingDirectory,$WindowStyle,[switch]$Wait)throw 'Unexpected real program launch in integrated maintenance fixture.'}
$edgePreview=Invoke-OptimusMaintenanceRequest maintenance_preview @{action='edge';includeProvisioned=$true}
$edgeResult=Invoke-OptimusMaintenanceRequest maintenance_apply @{action='edge';includeProvisioned=$true;targets=$edgePreview.targets;confirmation='REMOVE'}
Assert-OptimusTest ($edgeResult.results.Count -eq 1 -and $edgeResult.results[0].removed -and $script:EdgeCalls.Count -eq 1) 'new Edge module receives reviewed target without unsupported parameters'
Assert-OptimusTest ($script:EdgeProvisionedFlags.Count -eq 2 -and $script:EdgeProvisionedFlags[0] -and $script:EdgeProvisionedFlags[1]) 'explicit Edge future-user scope is retained across preview/apply'
$edgeExclusion=[pscustomobject]@{kind='desktop';id='Microsoft.Edge';name='Microsoft Edge';label='Microsoft Edge'}
$protectedEdge=Invoke-OptimusMaintenanceRequest maintenance_preview @{action='edge';removalExclusions=@($edgeExclusion)}
Assert-OptimusTest ($protectedEdge.targets.Count -eq 0 -and $protectedEdge.protectedCount -eq 1) 'saved Edge exclusion remains effective at the native boundary'
$stale=$script:EdgeFixture|ConvertTo-Json|ConvertFrom-Json;$stale.fingerprint='f'*64;$before=$script:EdgeCalls.Count;$blocked=$false
try{Invoke-OptimusMaintenanceRequest maintenance_apply @{action='edge';targets=@($stale);confirmation='REMOVE'}|Out-Null}catch{$blocked=$true}
Assert-OptimusTest ($blocked -and $script:EdgeCalls.Count -eq $before) 'stale Edge plan cannot reach removal module'

$script:OfficeFixture=@([pscustomobject]@{id='office-first';name='Microsoft 365 Fixture';kind='office-clicktorun';appIdentity='Office.First';method='clicktorun';fingerprint=('a'*64);admin=$false;scope='desktop'},[pscustomobject]@{id='office-second';name='Microsoft Office Fixture';kind='office-msi';appIdentity='Office.Second';method='msi';fingerprint=('b'*64);admin=$false;scope='desktop'})
function Get-OptimusOfficeRemovalTargets {param($Family)$script:OfficeFamily=$Family;return @($script:OfficeFixture)}
function Invoke-OptimusOfficeRemovalTarget {[CmdletBinding()]param($Target,$Exclusions,[scriptblock]$ProgressCallback,$Action)$script:OfficeCalls+=@([pscustomobject]@{id=$Target.id;action=$Action});if($Target.id -ceq 'office-first'){return [pscustomobject]@{id=$Target.id;name=$Target.name;removed=$false;error='Fixture first-product failure';requiresReview=$true;restartNeeded=$false}};return [pscustomobject]@{id=$Target.id;name=$Target.name;removed=$true;restartNeeded=$false}}
$officePreview=Invoke-OptimusMaintenanceRequest maintenance_preview @{action='office365-desktop'}
Assert-OptimusTest ($script:OfficeFamily -ceq 'm365' -and $officePreview.targets.Count -eq 2) 'desktop Microsoft 365 preview is dispatched to native suite inventory'
$officeResult=Invoke-OptimusMaintenanceRequest maintenance_apply @{action='office365-desktop';targets=$officePreview.targets;confirmation='REMOVE'}
Assert-OptimusTest ($script:OfficeCalls.Count -eq 2 -and -not $officeResult.results[0].removed -and $officeResult.results[1].removed -and $script:OfficeCalls[1].action -ceq 'office365-desktop') 'one Office product failure does not block the next reviewed product'
$officeExclusion=[pscustomobject]@{kind='desktop';id='Office.Second';name='Microsoft Office Fixture';label='Keep Office Fixture'}
$officeProtected=Invoke-OptimusMaintenanceRequest maintenance_preview @{action='office-desktop';removalExclusions=@($officeExclusion)}
Assert-OptimusTest ($script:OfficeFamily -ceq 'office' -and $officeProtected.targets.Count -eq 1 -and $officeProtected.protectedCount -eq 1) 'desktop Office uses exact saved removal protections'
[pscustomobject]@{Result='PASS';Assertions=$script:Assertions;RealAppRemovals=0;RealExternalProgramsLaunched=0;PSVersion=$PSVersionTable.PSVersion.ToString()}|ConvertTo-Json -Compress

