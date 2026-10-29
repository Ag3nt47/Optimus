# Fake WUA and PnP objects only; never query or change the real host.
$ErrorActionPreference='Stop'
. (Join-Path $PSScriptRoot '..\src\optimus\engine\FileTools.ps1')
$script:Assertions=0
function Assert-Test {param([bool]$Condition,[string]$Note) if(-not $Condition){throw "FAIL: $Note"};$script:Assertions++}
function Test-SCMAdministrator {return $true}
function Test-SCMWuaRebootPending {return $false}
function New-SCMWuaCollection {return ,(New-Object Collections.ArrayList)}
function New-SCMWuaSession {return $script:Session}
function Get-CimInstance {
 param($ClassName,$ErrorAction)
 if($ClassName -eq 'Win32_PnPEntity'){
  if($script:Absent){return @()}
  return [pscustomobject]@{Present=$true;PNPDeviceID='fixture-device';HardwareID=@('PCI\FIXTURE');CompatibleID=@()}
 }
 if($ClassName -eq 'Win32_PnPSignedDriver'){return [pscustomobject]@{DeviceID='fixture-device';DriverProviderName='Fixture provider';DriverVersion=$script:Version;DriverDate=[datetime]'2026-10-02';IsSigned=$script:Signed}}
 throw 'Unexpected fixture inventory request'
}
function Reset-Fixture {
 $script:Downloads=0;$script:Installs=0;$script:Queries=0;$script:Version='1.0.0.0';$script:Absent=$false;$script:Signed=$true
 $script:StillOffered=$false;$script:UnchangedVersion=$false;$script:BadSearch=$false;$script:Restart=$false
 $script:Update=[pscustomobject]@{Identity=[pscustomobject]@{UpdateID='00000000-0000-0000-0000-000000000001';RevisionNumber=[int]1}
  Title='Fixture provider - Display - 2.0.0.0';IsDownloaded=$false;EulaAccepted=$true;DriverHardwareID='PCI\FIXTURE';DriverProvider='Fixture provider'
  DriverClass='Display';DriverManufacturer='Fixture';DriverModel='Fixture';DriverVerDate=[datetime]'2026-10-02'
  InstallationBehavior=[pscustomobject]@{CanRequestUserInput=$true;RebootBehavior=[int]2}}
 $script:Searcher=[pscustomobject]@{Online=$false}
 $script:Searcher|Add-Member ScriptMethod Search {param($criteria)
  $script:Queries++;if($criteria -cne "IsInstalled=0 and Type='Driver' and IsHidden=0"){throw 'Wrong fixture search'}
  if($script:Queries -gt 1 -and $script:BadSearch){throw 'Fixture post-search failed'}
  $updates=if($script:Installs -eq 0 -or $script:StillOffered){@($script:Update)}else{@()}
  return [pscustomobject]@{ResultCode=[int]2;Updates=@($updates)}
 }
 $script:DownloadItem=[pscustomobject]@{ResultCode=[int]2;HResult=[int]0}
 $script:Download=[pscustomobject]@{ResultCode=[int]2;HResult=[int]0}
 $script:Download|Add-Member ScriptMethod GetUpdateResult {param($index) return $script:DownloadItem}
 $script:Downloader=[pscustomobject]@{Updates=$null;IsForced=$true}
 $script:Downloader|Add-Member ScriptMethod Download {$script:Downloads++;return $script:Download}
 $script:InstallItem=[pscustomobject]@{ResultCode=[int]2;HResult=[int]0;RebootRequired=$false}
 $script:Installed=[pscustomobject]@{ResultCode=[int]2;HResult=[int]0;RebootRequired=$false}
 $script:Installed|Add-Member ScriptMethod GetUpdateResult {param($index) return $script:InstallItem}
 $script:Installer=[pscustomobject]@{Updates=$null;IsForced=$true;ForceQuiet=$false;AllowSourcePrompts=$true;RebootRequiredBeforeInstallation=$false;IsBusy=$false}
 $script:Installer|Add-Member ScriptMethod Install {
  $script:Installs++;if(-not $script:UnchangedVersion){$script:Version='2.0.0.0'}
  $script:Installed.RebootRequired=$script:Restart;$script:InstallItem.RebootRequired=$script:Restart
  return $script:Installed
 }
 $script:Session=New-Object PSObject
 $script:Session|Add-Member ScriptMethod CreateUpdateSearcher {return $script:Searcher}
 $script:Session|Add-Member ScriptMethod CreateUpdateDownloader {return $script:Downloader}
 $script:Session|Add-Member ScriptMethod CreateUpdateInstaller {return $script:Installer}
}
function Apply-Fixture {Invoke-SCMDriverUpdateApply -Record (ConvertTo-SCMDriverUpdateDescriptor $script:Update) -Unattended $true -AcceptEula $true}
Reset-Fixture
$result=Apply-Fixture
Assert-Test ($result.resultCode -eq 2 -and $result.aggregateResultCode -eq 2 -and $result.hresult -eq 0 -and $result.aggregateHresult -eq 0) 'all native success codes retained'
Assert-Test ($result.verified -and -not $result.requiresReview -and $result.post_install_verification.status -eq 'confirmed') 'fresh offer disappearance and installed version confirmed'
Assert-Test ($script:Queries -eq 2 -and $script:Searcher.Online -and $script:Downloads -eq 1 -and $script:Installs -eq 1) 'one install and fresh online post-query'
Assert-Test ($result.post_install_verification.device.drivers[0].version -eq '2.0.0.0') 'fresh actual PnP version saved'
Reset-Fixture;$script:StillOffered=$true
$result=Apply-Fixture
Assert-Test ($result.requiresReview -and -not $result.verified -and $result.post_install_verification.status -eq 'still_offered') 'same exact offer remains unverified after reported success'
Assert-Test ($result.outcomeKnown -and -not $result.running -and $result.installer_started) 'verification failure does not invent a running installer'
Reset-Fixture;$script:UnchangedVersion=$true
$result=Apply-Fixture
Assert-Test ($result.requiresReview -and -not $result.verified -and -not $result.post_install_verification.device.versionMatches) 'offer absence alone cannot confirm unchanged driver version'
Reset-Fixture;$script:BadSearch=$true
$result=Apply-Fixture
Assert-Test ($result.requiresReview -and -not $result.verified -and -not $result.post_install_verification.searchComplete) 'post-search failure stays explicit'
Reset-Fixture;$script:Restart=$true;$script:UnchangedVersion=$true;$script:StillOffered=$true
$result=Apply-Fixture
Assert-Test ($result.restartNeeded -and $result.installation_reported -and -not $result.post_install_verification.verified -and $result.post_install_verification.status -eq 'pending_restart') 'pending restart does not claim device activation'
Reset-Fixture;$script:Version='2.0.0.0'
$result=Apply-Fixture
Assert-Test ($result.skipped -and $result.alreadyCurrent -and $result.verified -and -not $result.installer_started) 'already-installed version is a known skip'
Assert-Test ($script:Installs -eq 0 -and $script:Downloads -eq 0) 'repeated current offer causes no install/download'
Reset-Fixture;$script:Version='3.0.0.0'
$result=Apply-Fixture
Assert-Test ($result.skipped -and $result.alreadyCurrent -and $script:Installs -eq 0) 'newer installed version is not downgraded by repeated offer'
Reset-Fixture;$script:Absent=$true
$observation=Get-SCMDriverInstalledObservation -Record (ConvertTo-SCMDriverUpdateDescriptor $script:Update)
Assert-Test (-not $observation.complete -and -not $observation.alreadyCurrent) 'absent hardware has no present-device proof'
Reset-Fixture;$script:Version='2.0.0.0';$script:Signed=$false
$observation=Get-SCMDriverInstalledObservation -Record (ConvertTo-SCMDriverUpdateDescriptor $script:Update)
Assert-Test (-not $observation.complete -and -not $observation.alreadyCurrent) 'unsigned or unknown driver state never suppresses an offered update'
Reset-Fixture;$script:Version='2.0.0.0';$script:Update.Title='Fixture driver (2.0.0.0)'
$observation=Get-SCMDriverInstalledObservation -Record (ConvertTo-SCMDriverUpdateDescriptor $script:Update)
Assert-Test ($observation.alreadyCurrent -and $observation.offeredVersion -eq '2.0.0.0') 'parenthesized WUA title version is verified explicitly'
[pscustomobject]@{Result='PASS';Assertions=$script:Assertions;RealScans=0;RealDownloads=0;RealInstalls=0}|ConvertTo-Json -Compress
