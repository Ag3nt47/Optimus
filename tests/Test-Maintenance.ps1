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
$result=Invoke-OptimusMaintenanceRequest -Operation maintenance_apply -Arguments @{action='consumer-apps';targets=@($good);confirmation='REMOVE'}
Assert-OptimusTest ($script:Calls.Count -eq 1 -and $result.results[0].removed) 'only exact selected package removed in fixture'
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
[pscustomobject]@{Result='PASS';Assertions=$script:Assertions;RealAppRemovals=0;RealExternalProgramsLaunched=0;PSVersion=$PSVersionTable.PSVersion.ToString()}|ConvertTo-Json -Compress

