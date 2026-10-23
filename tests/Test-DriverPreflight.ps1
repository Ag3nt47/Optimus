# Every object below is a fake. No real Windows Update, device, driver or registry action.
param([string]$ReceiptsPath='')
$ErrorActionPreference='Stop'
. (Join-Path $PSScriptRoot 'Test-AutomatedSystem.ps1') | Out-Null
$baseAssertions=$script:Assertions
$receipts=@{}
function Save-PreflightFixture {
 param([string]$Name,$Record,[string]$Kind)
 $value=Invoke-SCMDriverUpdateApply -Record $Record -Unattended $true -AcceptEula $true
 Assert-Test ($script:Installs -eq 0 -and $value.installer_started -eq $false -and
  $value.outcomeKnown -eq $true -and $value.running -eq $false -and $value.requiresReview -eq $true -and
  $value.failure_scope -eq 'package' -and $value.failureKind -eq $Kind -and
  $value.resultCode -is [int] -and $value.resultCode -in @(2,3,4,5) -and
  $value.aggregateResultCode -is [int] -and $value.aggregateResultCode -in @(2,3,4,5) -and
  $value.hresult -is [int] -and $value.aggregateHresult -is [int]) ('exact known pre-install refusal: '+$Name)
 $receipts[$Name]=$value
 return $value
}
Reset-Fixture;$record=Record-Fixture;$script:SearchResult.Updates=@()
$null=Save-PreflightFixture 'missing' $record 'package_unavailable'
Reset-Fixture;$record=Record-Fixture;$script:SearchResult.Updates=@($script:Update,$script:Update)
$null=Save-PreflightFixture 'ambiguous' $record 'package_unavailable'
foreach($field in @('title','hardwareId','provider','driverClass','manufacturer','model','driverDate')){
 Reset-Fixture;$record=Record-Fixture;$record.$field='changed exact approval'
 $null=Save-PreflightFixture ('changed-'+$field) $record 'version_changed'
}
Reset-Fixture;$record=Record-Fixture;$script:Update.InstallationBehavior.CanRequestUserInput=$null
$null=Save-PreflightFixture 'interaction' $record 'unsupported_installer'
Reset-Fixture;$record=Record-Fixture;$script:Installer.PSObject.Properties.Remove('ForceQuiet')
$null=Save-PreflightFixture 'quiet' $record 'quiet_mode_unavailable'
foreach($code in @(3,4,5)){
 Reset-Fixture;$record=Record-Fixture
 $script:DownloadItem.ResultCode=[int]$code;$script:DownloadResult.ResultCode=[int]$code
 $script:DownloadItem.HResult=[int]0x80246007
 $value=Save-PreflightFixture ('download-'+$code) $record 'download_failed'
 Assert-Test ($script:Downloads -eq 1 -and $value.hresult -eq [int]0x80246007) 'native terminal download result retained without Install'
}
foreach($shared in @([int]0x80070005,[int]0x80070070,[int]0x80240016,[int]0x8024001F)){
 Reset-Fixture;$record=Record-Fixture
 $script:DownloadItem.ResultCode=4;$script:DownloadResult.ResultCode=4
 $script:DownloadItem.HResult=$shared;$script:DownloadResult.HResult=$shared
 $value=Save-PreflightFixture ('shared-download-'+$shared) $record 'download_failed'
 Assert-Test ($value.hresult -eq $shared -and $value.aggregateHresult -eq $shared) 'shared download HRESULT is never hidden by package scope'
}
Reset-Fixture;$record=Record-Fixture
$script:Installer.PSObject.Properties.Remove('ForceQuiet')
$script:Installer | Add-Member ScriptProperty ForceQuiet {$false} {param($value) throw (New-Object Runtime.InteropServices.COMException('Fixture access denied',[int]0x80070005))}
$value=Save-PreflightFixture 'shared-quiet' $record 'quiet_mode_unavailable'
Assert-Test ($value.hresult -eq [int]0x80070005) 'wrapped native quiet authorization HRESULT retained for shared gate'
foreach($malformed in @('download-null','aggregate-in-progress','item-in-progress','aggregate-missing-hresult','item-missing-hresult','string-code','throws')){
 Reset-Fixture;$record=Record-Fixture
 switch($malformed){
  'download-null' {$script:Downloader|Add-Member -Force ScriptMethod Download {$script:Downloads++;return $null}}
  'aggregate-in-progress' {$script:DownloadResult.ResultCode=1}
  'item-in-progress' {$script:DownloadItem.ResultCode=1}
  'aggregate-missing-hresult' {$script:DownloadResult.HResult=$null}
  'item-missing-hresult' {$script:DownloadItem.HResult=$null}
  'string-code' {$script:DownloadItem.ResultCode='4'}
  'throws' {$script:Downloader|Add-Member -Force ScriptMethod Download {$script:Downloads++;throw (New-Object Runtime.InteropServices.COMException('Fixture interrupted download',[int]0x80004005))}}
 }
 $caught=$false
 try {Invoke-SCMDriverUpdateApply -Record $record -Unattended $true -AcceptEula $true | Out-Null}catch{$caught=$true}
 Assert-Test ($caught -and $script:Installs -eq 0) ('unproven download remains closed: '+$malformed)
}
foreach($shared in @('administrator','approval','source','restart','busy','reboot-metadata','inventory')){
 Reset-Fixture;$record=Record-Fixture
 switch($shared){
  'administrator' {$script:Admin=$false}
  'approval' {$record=$null}
  'source' {$script:SearchResult.ResultCode=3}
  'restart' {$script:Pending=$true}
  'busy' {$script:Installer.IsBusy=$true}
  'reboot-metadata' {$script:Update.InstallationBehavior.RebootBehavior=$null}
  'inventory' {$record|Add-Member NoteProperty approvalMode 'matching';$record|Add-Member NoteProperty notOlderThanDriverDate '2026-10-01';$script:InstalledDate='unverified native inventory'}
 }
 $caught=$false
 try {Invoke-SCMDriverUpdateApply -Record $record -Unattended $true -AcceptEula ($shared -ne 'approval') | Out-Null}catch{$caught=$true}
 Assert-Test ($caught -and $script:Installs -eq 0) ('shared guard stays closed: '+$shared)
}
if($ReceiptsPath){$receipts|ConvertTo-Json -Depth 12|Set-Content -LiteralPath $ReceiptsPath -Encoding UTF8}
[pscustomobject]@{Result='PASS';Assertions=($script:Assertions-$baseAssertions);BaseAssertions=$baseAssertions;
 RealScans=0;RealDownloads=0;RealInstalls=0;PSVersion=$PSVersionTable.PSVersion.ToString()}|ConvertTo-Json -Compress
