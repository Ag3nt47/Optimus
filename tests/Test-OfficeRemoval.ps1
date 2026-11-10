$ErrorActionPreference='Stop'
. (Join-Path $PSScriptRoot '..\src\optimus\engine\OfficeRemoval.ps1')
$script:Assertions=0
function Assert-OfficeFixture {param([bool]$Condition,[string]$Message) if(-not $Condition){throw ('FAIL: '+$Message)};$script:Assertions++}
function Assert-OfficeRefused {param([scriptblock]$Action,[string]$Message) $refused=$false;try{$null=& $Action}catch{$refused=$true};Assert-OfficeFixture $refused $Message}
$nativeTrusted=(Get-Command Get-OptimusOfficeTrustedBinary).ScriptBlock
$script:BinaryHash='a'*64
function Get-OptimusOfficeTrustedBinary {param($Path,$Method) return [pscustomobject]@{path=$Path;sha256=$script:BinaryHash;signer='CN=Microsoft Corporation, O=Microsoft Corporation'}}
function New-OfficeFixtureRow {
 param([string]$Product='O365ProPlusRetail',[string]$Culture='en-us',[string]$Name='Microsoft 365 Apps for enterprise - en-us',[string]$Version='16.0.1.0')
 $identity=$Product+' - '+$Culture
 return [pscustomobject]@{path=('HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\'+$identity);identity=$identity;name=$Name;publisher='Microsoft Corporation';version=$Version;windowsInstaller=$false;
  uninstall=('"C:\Program Files\Common Files\Microsoft Shared\ClickToRun\OfficeClickToRun.exe" scenario=install scenariosubtype=ARP sourcetype=None productstoremove='+$Product+'.16_'+$Culture+'_x-none culture='+$Culture+' version.16=16.0')}
}
$script:Rows=@();$script:Products=@();$script:Calls=@();$script:Active=@();$script:Admin=$true;$script:NativeMode='success'
function Get-OptimusOfficeRegistryRows {return @($script:Rows)}
function Get-OptimusOfficeInstalledProducts {return @($script:Products)}
function Test-OptimusOfficeRemovalAdmin {return $script:Admin}
function Get-OptimusOfficeActivePrograms {return @($script:Active)}
function Invoke-OptimusOfficeNativeUninstall {
 param($Payload)
 $script:Calls+=@($Payload)
 if($script:NativeMode -eq 'error'){return 1603}
 if($script:NativeMode -eq 'noop'){return 0}
 $script:Rows=@($script:Rows|Where-Object {$_.path -cne $Payload.registration})
 $remaining=@($script:Rows|Where-Object {$_.uninstall -like ('*productstoremove='+$Payload.productId+'.*')})
 if(-not $remaining.Count){$script:Products=@($script:Products|Where-Object {$_ -cne $Payload.productId})}
 return 0
}
function Reset-OfficeFixture {
 $script:Rows=@((New-OfficeFixtureRow),(New-OfficeFixtureRow -Product O365HomePremRetail -Name 'Microsoft 365 - en-us'),
  (New-OfficeFixtureRow -Product ProPlus2024Volume -Name 'Microsoft Office Professional Plus 2024'))
 $script:Products=@('O365ProPlusRetail','O365HomePremRetail','ProPlus2024Volume','VisioProRetail','ProjectProRetail')
 $script:Calls=@();$script:Active=@();$script:Admin=$true;$script:NativeMode='success';$script:BinaryHash='a'*64
}

Assert-OfficeFixture ((Get-OptimusOfficeProductFamily O365HomePremRetail) -ceq 'm365') 'consumer Microsoft365 is an exact subscription suite'
Assert-OfficeFixture ((Get-OptimusOfficeProductFamily O365ProPlusRetail) -ceq 'm365') 'enterprise Microsoft365 is an exact subscription suite'
Assert-OfficeFixture ((Get-OptimusOfficeProductFamily ProPlus2024Volume) -ceq 'office') 'perpetual Office has a separate family'
foreach($identity in @('VisioProRetail','ProjectProRetail','AccessRuntimeRetail','MicrosoftCorporationII.Windows365','Microsoft.EdgeWebView')) {
 Assert-OfficeFixture (-not(Get-OptimusOfficeProductFamily $identity)) ('non-suite product excluded '+$identity)
}
Reset-OfficeFixture
$all=@(Get-OptimusOfficeRemovalTargets)
Assert-OfficeFixture ($all.Count -eq 3) 'three exact registered suite targets'
Assert-OfficeFixture (@(Get-OptimusOfficeRemovalTargets -Family m365).Count -eq 2) 'Microsoft365 family selection excludes perpetual Office'
Assert-OfficeFixture (@(Get-OptimusOfficeRemovalTargets -Family office).Count -eq 1) 'Office family selection excludes Microsoft365'
Assert-OfficeFixture (@($all|Where-Object {$_.selected -or -not $_.admin}).Count -eq 0) 'preview has no preselection and declares administrator need'
Assert-OfficeFixture ($script:Calls.Count -eq 0) 'inventory launches no uninstaller'
Assert-OfficeFixture (($all[0].payload.arguments -join ' ') -match 'DisplayLevel=False ForceAppShutdown=False$') 'quiet uninstall never forces unsaved Office programs closed'

$script:Rows+=@([pscustomobject]@{path='HKCU:\fixture\windowsapp';identity='MicrosoftCorporationII.Windows365_8wekyb3d8bbwe';name='Microsoft 365 Windows App';publisher='Microsoft Corporation';version='2.0';windowsInstaller=$false;uninstall=$script:Rows[0].uninstall})
$script:Rows+=@(New-OfficeFixtureRow -Product VisioProRetail -Name 'Microsoft Office Visio Professional 2016')
$script:Rows+=@(New-OfficeFixtureRow -Product AccessRuntimeRetail -Name 'Microsoft Office Access Runtime')
Assert-OfficeFixture (@(Get-OptimusOfficeRemovalTargets).Count -eq 3) 'Windows App, standalone Visio and shared Access runtime do not become suite targets'
$protected=[pscustomobject]@{id='foreign';name='Microsoft Windows App';appIdentity='MicrosoftCorporationII.Windows365_8wekyb3d8bbwe';kind='office-clicktorun';fingerprint='x'}
$result=Invoke-OptimusOfficeRemovalTarget $protected -VerificationSeconds 0
Assert-OfficeFixture ($result.protected -and -not $result.removed -and $script:Calls.Count -eq 0) 'Windows App is protected at the native apply boundary even with a forged Office kind'

Reset-OfficeFixture
$m365=@(Get-OptimusOfficeRemovalTargets -Family m365)
$beforeSecond=$m365[1].fingerprint
$events=New-Object Collections.Generic.List[object]
$observe={param($event) $events.Add($event);'discard observer output'}
$first=Invoke-OptimusOfficeRemovalTarget -Target $m365[0] -ProgressCallback $observe -Action office365-desktop -VerificationSeconds 0
Assert-OfficeFixture ($first.removed -and $first.reason -ceq 'removal_verified' -and $first.outcomeKnown) 'real registration and product absence prove selected suite removal'
$remaining=@(Get-OptimusOfficeRemovalTargets -Family m365)
Assert-OfficeFixture ($remaining.Count -eq 1 -and $remaining[0].fingerprint -ceq $beforeSecond) 'removing one suite does not stale the next exact product approval'
$second=Invoke-OptimusOfficeRemovalTarget -Target $m365[1] -VerificationSeconds 0
Assert-OfficeFixture ($second.removed -and $script:Calls.Count -eq 2) 'both Microsoft365 suites remove in one queue'
Assert-OfficeFixture (@(Get-OptimusOfficeRemovalTargets -Family office).Count -eq 1) 'perpetual Office remains when only Microsoft365 selected'
Assert-OfficeFixture (($events.phase -join '|') -ceq 'uninstall|verify|uninstall') 'native progress distinguishes uninstall and independent verification'
Assert-OfficeFixture ($null -eq $events[0].percent -and $events[2].percent -eq 100) 'no invented intermediate uninstall percent'
Assert-OfficeFixture ($events[0].key -ceq ('debloat:office365-desktop:'+$m365[0].id)) 'progress key binds exact action and target'

Reset-OfficeFixture
$chosen=@(Get-OptimusOfficeRemovalTargets -Family m365)[0]
$script:NativeMode='noop'
$result=Invoke-OptimusOfficeRemovalTarget $chosen -VerificationSeconds 0
Assert-OfficeFixture (-not $result.removed -and $result.reason -ceq 'office_uninstall_not_verified' -and -not $result.outcomeKnown) 'exit0 with surviving product is not a successful uninstall'
$script:NativeMode='error'
$result=Invoke-OptimusOfficeRemovalTarget $chosen -VerificationSeconds 0
Assert-OfficeFixture (-not $result.removed -and $result.exitCode -eq 1603 -and $result.reason -ceq 'office_uninstaller_error') 'known uninstaller failure remains visible'
$script:NativeMode='success'
$other=@(Get-OptimusOfficeRemovalTargets -Family m365)[1]
$result=Invoke-OptimusOfficeRemovalTarget $other -VerificationSeconds 0
Assert-OfficeFixture $result.removed 'one failed suite does not prevent independent next target removal'

Reset-OfficeFixture
$chosen=@(Get-OptimusOfficeRemovalTargets -Family m365)[0]
$script:Active=@([pscustomobject]@{Id=42;ProcessName='WINWORD'})
$result=Invoke-OptimusOfficeRemovalTarget $chosen -VerificationSeconds 0
Assert-OfficeFixture ($result.reason -ceq 'office_programs_open' -and $script:Calls.Count -eq 0) 'open documents prevent this uninstall without killing programs'
$script:Active=@();$script:Admin=$false
Assert-OfficeRefused {Invoke-OptimusOfficeRemovalTarget $chosen -VerificationSeconds 0} 'administrator requirement enforced before native launch'
$script:Admin=$true
$exclusion=@([pscustomobject]@{kind='desktop';id=$chosen.appIdentity;name=$chosen.name})
$result=Invoke-OptimusOfficeRemovalTarget $chosen -Exclusions $exclusion -VerificationSeconds 0
Assert-OfficeFixture ($result.protected -and $script:Calls.Count -eq 0) 'saved exact desktop exclusion reaches native boundary'
$script:BinaryHash='b'*64
Assert-OfficeRefused {Invoke-OptimusOfficeRemovalTarget $chosen -VerificationSeconds 0} 'changed executable hash invalidates old target'
Assert-OfficeFixture ($script:Calls.Count -eq 0) 'stale executable is never launched'

Reset-OfficeFixture
$bad=New-OfficeFixtureRow
$bad.uninstall=$bad.uninstall+' evil=arbitrary'
Assert-OfficeRefused {ConvertTo-OptimusOfficeRemovalCommand -Row $bad -InstalledProducts $script:Products} 'unknown uninstall command argument rejected'
$bad.uninstall=$script:Rows[0].uninstall.Replace('culture=en-us','culture=fr-fr')
Assert-OfficeRefused {ConvertTo-OptimusOfficeRemovalCommand -Row $bad -InstalledProducts $script:Products} 'mismatched uninstall culture rejected'
$bad.uninstall=$script:Rows[0].uninstall.Replace('productstoremove=O365ProPlusRetail','productstoremove=VisioProRetail')
Assert-OfficeRefused {ConvertTo-OptimusOfficeRemovalCommand -Row $bad -InstalledProducts $script:Products} 'non-suite product cannot be requested through Office toggle'
$script:Rows=@((New-OfficeFixtureRow));$script:Rows[0].uninstall='malformed command'
$review=@(Get-OptimusOfficeRemovalTargets)[0]
$result=Invoke-OptimusOfficeRemovalTarget $review -VerificationSeconds 0
Assert-OfficeFixture ($review.kind -ceq 'office-review' -and $result.requiresReview -and $script:Calls.Count -eq 0) 'unsupported registered command is explicit review, never shell execution'

Reset-OfficeFixture
$guid='{90160000-0011-0000-1000-0000000FF1CE}'
$msi=[pscustomobject]@{path=('HKLM:\fixture\'+$guid);identity=$guid;name='Microsoft Office Professional Plus 2016';publisher='Microsoft Corporation';version='16.0';windowsInstaller=$true;uninstall=('MsiExec.exe /I'+$guid)}
$script:Rows=@($msi);$script:Products=@()
$target=@(Get-OptimusOfficeRemovalTargets -Family office)[0]
Assert-OfficeFixture ($target.kind -ceq 'office-msi' -and ($target.payload.arguments -join ' ') -ceq ('/x '+$guid+' /qn /norestart')) 'MSI removes only exact registered product and suppresses automatic restart'
$msi.uninstall='MsiExec.exe /I{00000000-0000-0000-0000-000000000000}'
Assert-OfficeRefused {ConvertTo-OptimusOfficeRemovalCommand -Row $msi} 'MSI command cannot identify a different product'

# Exercise the actual executable provenance function with mocked filesystem/trust.
$script:Signature='Valid';$script:Subject='CN=Microsoft Corporation, O=Microsoft Corporation';$script:Reparse=$false
function Get-Item {param($LiteralPath,[switch]$Force,$ErrorAction) return [pscustomobject]@{Attributes=$(if($script:Reparse){[IO.FileAttributes]::ReparsePoint}else{[IO.FileAttributes]::Normal});PSIsContainer=$false}}
function Get-AuthenticodeSignature {param($LiteralPath,$ErrorAction) return [pscustomobject]@{Status=$script:Signature;SignerCertificate=[pscustomobject]@{Subject=$script:Subject}}}
function Get-FileHash {param($LiteralPath,$Algorithm,$ErrorAction) return [pscustomobject]@{Hash=('a'*64)}}
$binary=Join-Path ([Environment]::GetFolderPath('CommonProgramFiles')) 'Microsoft Shared\ClickToRun\OfficeClickToRun.exe'
$proof=& $nativeTrusted -Path $binary -Method clicktorun
Assert-OfficeFixture ($proof.sha256 -ceq ('a'*64)) 'valid Microsoft binary proof includes exact SHA'
Assert-OfficeRefused {& $nativeTrusted -Path 'C:\Untrusted\OfficeClickToRun.exe' -Method clicktorun} 'signed executable name outside supported location rejected'
$script:Subject='CN=Different Publisher'
Assert-OfficeRefused {& $nativeTrusted -Path $binary -Method clicktorun} 'wrong publisher rejected'
$script:Subject='CN=Microsoft Corporation';$script:Signature='HashMismatch'
Assert-OfficeRefused {& $nativeTrusted -Path $binary -Method clicktorun} 'invalid signature rejected'
$script:Signature='Valid';$script:Reparse=$true
Assert-OfficeRefused {& $nativeTrusted -Path $binary -Method clicktorun} 'reparse executable or ancestor refused'
$script:Reparse=$false;$script:Subject='CN=Microsoft Windows, O=Microsoft Corporation'
$proof=& $nativeTrusted -Path (Join-Path $env:SystemRoot 'System32\msiexec.exe') -Method msi
Assert-OfficeFixture ($proof.sha256 -ceq ('a'*64)) 'System32 MSI engine accepts valid Microsoft Windows signer'
Assert-OfficeRefused {& $nativeTrusted -Path $binary -Method clicktorun} 'Office vendor executable cannot impersonate the Windows-signed MSI exception'
Write-Output ('PASS: '+$script:Assertions+' Office removal assertions; no host uninstall or registry mutation')
