# Offline fake WUA objects only. No real scan, download, install or registry mutation.
$ErrorActionPreference='Stop'
. (Join-Path $PSScriptRoot '..\src\optimus\engine\FileTools.ps1')
. (Join-Path $PSScriptRoot '..\src\optimus\engine\PersistenceInventory.ps1')
$script:Assertions=0
function Assert-Test {param([bool]$Condition,[string]$Message) if(-not $Condition){throw ('FAIL: '+$Message)};$script:Assertions++}
function Test-SCMAdministrator {return $script:Admin}
function New-SCMWuaSession {return $script:Session}
function New-SCMWuaCollection {return ,(New-Object Collections.ArrayList)}
function Test-SCMWuaRebootPending {return $script:Pending}
function Get-SCMExecutableFromCommand {return 'C:\Fixture\app.exe'}
function Get-CimInstance {
 param($ClassName,$ErrorAction)
 if($ClassName -eq 'Win32_PnPEntity'){return [pscustomobject]@{PNPDeviceID='fixture-device';HardwareID=@('PCI\VEN_1234&DEV_5678');CompatibleID=@()}}
 if($ClassName -eq 'Win32_PnPSignedDriver'){return [pscustomobject]@{DeviceID='fixture-device';DriverDate=$script:InstalledDate}}
 throw 'Unexpected hardware query'
}
function Reset-Fixture {
 $script:Admin=$true;$script:Pending=$false;$script:Accepts=0;$script:Downloads=0;$script:Installs=0;$script:InstalledDate=[datetime]'2026-10-10'
 $script:Update=[pscustomobject]@{Identity=[pscustomobject]@{UpdateID='00000000-0000-0000-0000-000000000001';RevisionNumber=[int]1};Title='Fixture driver';IsDownloaded=$false;EulaAccepted=$false;DriverHardwareID='PCI\VEN_1234&DEV_5678';DriverProvider='Fixture provider';DriverClass='Display';DriverManufacturer='Fixture';DriverModel='Fixture model';DriverVerDate=[datetime]'2026-10-20';InstallationBehavior=[pscustomobject]@{CanRequestUserInput=$false;RebootBehavior=[int]2}}
 $script:Update|Add-Member ScriptMethod AcceptEula {$script:Accepts++;$this.EulaAccepted=$true}
 $script:SearchResult=[pscustomobject]@{ResultCode=[int]2;Updates=@($script:Update)}
 $script:Searcher=New-Object PSObject;$script:Searcher|Add-Member ScriptMethod Search {param($query) if($query -cne "IsInstalled=0 and Type='Driver' and IsHidden=0"){throw 'Unexpected search'};return $script:SearchResult}
 $script:DownloadItem=[pscustomobject]@{ResultCode=[int]2;HResult=0}
 $script:DownloadResult=[pscustomobject]@{ResultCode=[int]2};$script:DownloadResult|Add-Member ScriptMethod GetUpdateResult {param($index) if($index -ne 0){throw 'Wrong index'};return $script:DownloadItem}
 $script:Downloader=[pscustomobject]@{Updates=$null;IsForced=$true};$script:Downloader|Add-Member ScriptMethod Download {$script:Downloads++;return $script:DownloadResult}
 $script:InstallItem=[pscustomobject]@{ResultCode=[int]2;HResult=0;RebootRequired=$false}
 $script:InstallResult=[pscustomobject]@{ResultCode=[int]2;HResult=0;RebootRequired=$false};$script:InstallResult|Add-Member ScriptMethod GetUpdateResult {param($index) return $script:InstallItem}
 $script:Installer=[pscustomobject]@{Updates=$null;IsForced=$true;ForceQuiet=$false;AllowSourcePrompts=$true;RebootRequiredBeforeInstallation=$false;IsBusy=$false}
 $script:Installer|Add-Member ScriptMethod Install {
  Assert-Test ($this.ForceQuiet -and -not $this.AllowSourcePrompts -and -not $this.IsForced) 'quiet/no-source-prompt/no-force before install'
  $script:Installs++;return $script:InstallResult
 }
 $script:Session=New-Object PSObject
 $script:Session|Add-Member ScriptMethod CreateUpdateSearcher {return $script:Searcher}
 $script:Session|Add-Member ScriptMethod CreateUpdateDownloader {return $script:Downloader}
 $script:Session|Add-Member ScriptMethod CreateUpdateInstaller {return $script:Installer}
}
function Record-Fixture {
 $value=ConvertTo-SCMDriverUpdateDescriptor $script:Update
 return $value
}
function Assert-Blocked {
 param($Record,[string]$Reason,[bool]$Accept=$true)
 $blocked=$false
 try{Invoke-SCMDriverUpdateApply -Record $Record -Unattended $true -AcceptEula $Accept|Out-Null}catch{$blocked=$true}
 Assert-Test ($blocked -and $script:Installs -eq 0) $Reason
}
Reset-Fixture
$scan=Get-SCMDriverUpdateScan
Assert-Test ($scan.resultCode -eq 2 -and $scan.updates[0].hardwareId -eq 'PCI\VEN_1234&DEV_5678' -and $scan.updates[0].provider -eq 'Fixture provider' -and $scan.updates[0].driverClass -eq 'Display') 'complete device metadata from fake WUA'
Assert-Test ($script:Downloads -eq 0 -and $script:Installs -eq 0) 'scan does not download/install'
$result=Invoke-SCMDriverUpdateApply -Record (Record-Fixture) -Unattended $true -AcceptEula $true
Assert-Test ($script:Accepts -eq 1 -and $script:Downloads -eq 1 -and $script:Installs -eq 1 -and $result.resultCode -eq 2) 'selected driver license/download/install once'
Reset-Fixture;$script:Admin=$false;Assert-Blocked (Record-Fixture) 'non-admin blocked';Assert-Test ($script:Accepts -eq 0 -and $script:Downloads -eq 0) 'non-admin no mutation'
Reset-Fixture;Assert-Blocked (Record-Fixture) 'missing saved agreement blocked' $false
Reset-Fixture;$script:Pending=$true;Assert-Blocked (Record-Fixture) 'pending reboot blocked';Assert-Test ($script:Accepts -eq 0 -and $script:Downloads -eq 0) 'pending reboot before acceptance/download'
Reset-Fixture;$script:Update.InstallationBehavior.CanRequestUserInput=$true;Assert-Blocked (Record-Fixture) 'interactive blocked'
Reset-Fixture;$script:Update.InstallationBehavior.RebootBehavior=1;Assert-Blocked (Record-Fixture) 'mandatory restart blocked'
Reset-Fixture;$record=Record-Fixture;$record.title='changed';Assert-Blocked $record 'changed title binding blocked'
Reset-Fixture;$script:SearchResult.ResultCode=3;Assert-Blocked (Record-Fixture) 'incomplete search blocked'
Reset-Fixture;$script:DownloadItem.ResultCode=3;Assert-Blocked (Record-Fixture) 'partial download blocked'
Reset-Fixture;$record=Record-Fixture;$record|Add-Member NoteProperty approvalMode 'matching';$record|Add-Member NoteProperty notOlderThanDriverDate '2026-10-01';$script:InstalledDate=[datetime]'2026-10-21';Assert-Blocked $record 'installed newer driver blocks downgrade';Assert-Test ($script:Accepts -eq 0 -and $script:Downloads -eq 0) 'downgrade guard before acceptance/download'
Reset-Fixture;$record=Record-Fixture;$record|Add-Member NoteProperty approvalMode 'matching';$record|Add-Member NoteProperty notOlderThanDriverDate '2026-10-22';Assert-Blocked $record 'approved date floor blocks older candidate'
Reset-Fixture;$record=Record-Fixture;$record|Add-Member NoteProperty approvalMode 'matching';$record|Add-Member NoteProperty notOlderThanDriverDate '2026-10-01';$result=Invoke-SCMDriverUpdateApply -Record $record -Unattended $true -AcceptEula $true
Assert-Test ($script:Installs -eq 1) 'verified newer matching driver installs quietly'
Assert-Test ([bool](Get-SCMStartupProtection -Name 'Bing Wallpaper' -Command 'C:\Fixture\app.exe')) 'Bing name startup protected'
Assert-Test ([bool](Get-SCMStartupProtection -Name 'Generic' -Command 'C:\Microsoft\Bing-Wallpaper\app.exe')) 'Bing command startup protected'
[pscustomobject]@{Result='PASS';Assertions=$script:Assertions;RealScans=0;RealDownloads=0;RealInstalls=0;PSVersion=$PSVersionTable.PSVersion.ToString()}|ConvertTo-Json -Compress
