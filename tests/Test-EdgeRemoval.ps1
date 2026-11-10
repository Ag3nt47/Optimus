#requires -Version 5.1
# All external programs, package operations and discovery are fixtures.
$ErrorActionPreference='Stop'
. (Join-Path $PSScriptRoot '..\src\optimus\engine\EdgeRemoval.ps1')
$nativeSignedFile=(Get-Item Function:Get-OptimusEdgeSignedFile).ScriptBlock
$script:Assertions=0
function Assert-Edge([bool]$Value,[string]$Message){$script:Assertions++;if(-not $Value){throw $Message}}
$fixture=Join-Path ([IO.Path]::GetTempPath()) ('optimus-edge-fixture-'+[guid]::NewGuid().ToString('N'))
$null=New-Item -ItemType Directory -Path $fixture
$script:Roots=@();$script:Signatures=@{};$script:Packages=@();$script:Provisioned=@();$script:Winget=$null
$script:Launches=New-Object Collections.ArrayList;$script:PackageRemovals=New-Object Collections.ArrayList
$script:Admin=$true;$script:ExitCodes=New-Object Collections.Queue;$script:RemoveVendor=$false;$script:RemoveAppxBrowser=$false
function Get-OptimusEdgeKnownRoots {return @($script:Roots)}
function Get-OptimusEdgePackages {return @($script:Packages)}
function Get-OptimusEdgeProvisionedPackages {return @($script:Provisioned)}
function Get-OptimusEdgeWinget {return $script:Winget}
function Test-OptimusEdgeRemovalAdmin {return $script:Admin}
function Get-OptimusEdgeSignedFile {param($Path)if((Test-Path -LiteralPath $Path -PathType Leaf)-and $script:Signatures.ContainsKey($Path)){return $script:Signatures[$Path]};return $null}
function Invoke-OptimusEdgeProcess {
 param($Path,$Arguments)
 $null=$script:Launches.Add([pscustomobject]@{path=$Path;arguments=@($Arguments)})
 if($script:RemoveVendor -and $Path -like '*\Installer\setup.exe'){
  $root=Split-Path (Split-Path (Split-Path $Path -Parent) -Parent) -Parent
  $browser=Join-Path $root 'msedge.exe';if(Test-Path -LiteralPath $browser){Remove-Item -LiteralPath $browser -Force}
 }
 if($script:ExitCodes.Count){return [int]$script:ExitCodes.Dequeue()};return 0
}
function Remove-AppxPackage {
 param($Package,[switch]$Confirm,$ErrorAction)
 $selected=@($script:Packages|Where-Object {$_.fullName -ceq $Package})
 $null=$script:PackageRemovals.Add([pscustomobject]@{kind='appx';package=$Package})
 $script:Packages=@($script:Packages|Where-Object {$_.fullName -cne $Package})
 if($script:RemoveAppxBrowser){foreach($root in $script:Roots){if($selected.Count -and $selected[0].installLocation.StartsWith($root.root+'\',[StringComparison]::OrdinalIgnoreCase)){$path=Join-Path $root.root 'msedge.exe';if(Test-Path -LiteralPath $path){Remove-Item -LiteralPath $path -Force}}}}
}
function Remove-AppxProvisionedPackage {
 param([switch]$Online,$PackageName,$ErrorAction)
 $null=$script:PackageRemovals.Add([pscustomobject]@{kind='provisioned';package=$PackageName})
 $script:Provisioned=@($script:Provisioned|Where-Object {$_.fullName -cne $PackageName})
 return [pscustomobject]@{RestartNeeded=$false}
}
function Add-EdgeRoot([string]$Name,[string]$Scope,[string]$Version='157.0.0.0',[switch]$NoBrowser){
 $root=Join-Path $fixture ($Name+'\Microsoft\Edge\Application')
 $installer=Join-Path $root ($Version+'\Installer');$null=New-Item -ItemType Directory -Path $installer -Force
 $setup=Join-Path $installer 'setup.exe';[IO.File]::WriteAllText($setup,'inert fixture setup')
 $script:Signatures[$setup]=[pscustomobject]@{path=$setup;sha256=('a'*64);version=$Version}
 $browser=Join-Path $root 'msedge.exe'
 if(-not $NoBrowser){[IO.File]::WriteAllText($browser,'inert fixture browser');$script:Signatures[$browser]=[pscustomobject]@{path=$browser;sha256=('b'*64);version=$Version}}
 $script:Roots+=@([pscustomobject]@{root=$root;scope=$Scope});return $root
}
function Reset-Calls {$script:Launches.Clear();$script:PackageRemovals.Clear();$script:ExitCodes.Clear();$script:Winget=$null;$script:RemoveVendor=$false;$script:RemoveAppxBrowser=$false}

# A higher stale user installer cannot shadow an installed machine browser.
$machine=Add-EdgeRoot 'machine' 'machine'
$stale=Add-EdgeRoot 'stale-user' 'current-user' '199.0.0.0' -NoBrowser
$targets=@(Get-OptimusEdgeRemovalTargets)
Assert-Edge ($targets.Count -eq 1 -and $targets[0].payload.root -ceq $machine -and $targets[0].payload.setup.version -ceq '157.0.0.0') 'Stale root shadowed actual browser installation.'
$machineTarget=$targets[0]

# No matching WinGet registration advances to this exact root's signed setup.
$script:Winget=[pscustomobject]@{path=(Join-Path $fixture 'winget.exe');sha256=('c'*64);version='1.0.0.0'}
$script:ExitCodes.Enqueue(-1978335212);$script:ExitCodes.Enqueue(0);$script:RemoveVendor=$true
$target=@(Get-OptimusEdgeRemovalTargets)[0]
$events=New-Object Collections.ArrayList
$result=Invoke-OptimusEdgeRemovalTarget $target -ProgressCallback {param($event)$null=$events.Add($event)}
Assert-Edge ($result.removed -and $result.reason -ceq 'removal_verified' -and $script:Launches.Count -eq 2) 'Package identity miss did not advance to verified vendor fallback.'
Assert-Edge ($script:Launches[0].arguments -contains '--scope' -and $script:Launches[0].arguments -contains 'machine') 'WinGet scope was not fixed to the selected machine installation.'
Assert-Edge ($script:Launches[1].path -ceq $machineTarget.payload.setup.path -and $script:Launches[1].arguments -contains '--force-uninstall' -and $script:Launches[1].arguments -contains '--msedge' -and $script:Launches[1].arguments -contains '--system-level') 'Vendor flags or exact setup scope differed.'
Assert-Edge ($events[$events.Count-1].percent -eq 100 -and @($events|Where-Object {$null -ne $_.percent -and $_.percent -ne 100}).Count -eq 0) 'Invented intermediate removal percentage.'

# Exit zero is not removal when the browser is still present.
[IO.File]::WriteAllText((Join-Path $machine 'msedge.exe'),'inert fixture browser')
Reset-Calls
$target=@(Get-OptimusEdgeRemovalTargets)[0]
$result=Invoke-OptimusEdgeRemovalTarget $target
Assert-Edge (-not $result.removed -and $result.remainingDetected -and $result.reason -ceq 'edge_still_detected') 'Exit zero falsely reported browser absence.'

# A reboot-required or unrelated manager failure does not replay uninstallers.
Reset-Calls;$script:Winget=[pscustomobject]@{path=(Join-Path $fixture 'winget.exe');sha256=('c'*64);version='1.0.0.0'}
$script:ExitCodes.Enqueue(3010);$target=@(Get-OptimusEdgeRemovalTargets)[0]
$result=Invoke-OptimusEdgeRemovalTarget $target
Assert-Edge (-not $result.removed -and $result.restartNeeded -and $result.reason -ceq 'restart_required' -and $script:Launches.Count -eq 1) 'Pending restart replayed the vendor uninstaller.'
Reset-Calls;$script:Winget=[pscustomobject]@{path=(Join-Path $fixture 'winget.exe');sha256=('c'*64);version='1.0.0.0'}
$script:ExitCodes.Enqueue(1618);$target=@(Get-OptimusEdgeRemovalTargets)[0]
$result=Invoke-OptimusEdgeRemovalTarget $target
Assert-Edge (-not $result.removed -and $script:Launches.Count -eq 1) 'Busy manager outcome started a conflicting vendor uninstaller.'

# Current-user and machine installations have independent reviewed identities.
$user=Add-EdgeRoot 'active-user' 'current-user' '158.0.0.0'
Reset-Calls;$script:RemoveVendor=$true
$targets=@(Get-OptimusEdgeRemovalTargets)
Assert-Edge ($targets.Count -eq 2 -and $targets[0].id -cne $targets[1].id) 'Installation scopes merged into one target.'
$userTarget=@($targets|Where-Object {$_.scope -ceq 'current-user'})[0]
$result=Invoke-OptimusEdgeRemovalTarget $userTarget
Assert-Edge ($result.removed -and (Test-Path -LiteralPath (Join-Path $machine 'msedge.exe')) -and $script:Launches[0].arguments -notcontains '--system-level') 'User removal touched machine scope.'

# Changes after preview and unsigned/reparse browser paths cannot launch tools.
Reset-Calls
$target=@(Get-OptimusEdgeRemovalTargets)[0]
$script:Signatures[(Join-Path $machine 'msedge.exe')].sha256=('d'*64)
$result=Invoke-OptimusEdgeRemovalTarget $target
Assert-Edge (-not $result.removed -and $result.reason -ceq 'edge_removal_refused' -and $script:Launches.Count -eq 0) 'A changed binary reused a stale approval.'
$script:Signatures.Remove((Join-Path $machine 'msedge.exe'))
$target=@(Get-OptimusEdgeRemovalTargets)[0]
$result=Invoke-OptimusEdgeRemovalTarget $target
Assert-Edge (-not $result.removed -and $script:Launches.Count -eq 0) 'An unverified browser publisher launched removal.'
$script:Signatures[(Join-Path $machine 'msedge.exe')]=[pscustomobject]@{path=(Join-Path $machine 'msedge.exe');sha256=('b'*64);version='157.0.0.0'}
Reset-Calls
$setupUnsigned=Join-Path $machine '157.0.0.0\Installer\setup.exe';$originalSetup=$script:Signatures[$setupUnsigned];$script:Signatures.Remove($setupUnsigned)
$target=@(Get-OptimusEdgeRemovalTargets)[0];$result=Invoke-OptimusEdgeRemovalTarget $target
Assert-Edge (-not $result.removed -and $script:Launches.Count -eq 0) 'An unverified setup was used as fallback.'
$script:Signatures[$setupUnsigned]=$originalSetup
$realOrdinary=(Get-Item Function:Test-OptimusEdgeOrdinaryPath).ScriptBlock
function Test-OptimusEdgeOrdinaryPath {param($Path) return $false}
$target=@(Get-OptimusEdgeRemovalTargets)[0];$result=Invoke-OptimusEdgeRemovalTarget $target
Assert-Edge (-not $result.removed -and $script:Launches.Count -eq 0) 'An indirect browser path launched removal.'
Set-Item Function:Test-OptimusEdgeOrdinaryPath $realOrdinary

# If the signed setup is unavailable, an exact removable package can finish
# the same selected installation without touching another browser/runtime.
Reset-Calls;$script:RemoveAppxBrowser=$true
$boundFull='Microsoft.MicrosoftEdge.Stable_157.0.0.0_x64__8wekyb3d8bbwe'
$script:Packages=@([pscustomobject]@{name='Microsoft.MicrosoftEdge.Stable';fullName=$boundFull;family='Microsoft.MicrosoftEdge.Stable_8wekyb3d8bbwe';installLocation=(Join-Path $machine '157.0.0.0');removable=$true})
$setupPath=Join-Path $machine '157.0.0.0\Installer\setup.exe';$savedSetup=$script:Signatures[$setupPath];$script:Signatures.Remove($setupPath)
$target=@(Get-OptimusEdgeRemovalTargets)[0];$result=Invoke-OptimusEdgeRemovalTarget $target
Assert-Edge ($result.removed -and $script:Launches.Count -eq 0 -and $script:PackageRemovals[0].package -ceq $boundFull) 'Exact AppX fallback did not remove its selected browser.'
$script:Signatures[$setupPath]=$savedSetup

# A browser can be absent while a nonremovable OS registration stub remains.
[IO.File]::WriteAllText((Join-Path $machine 'msedge.exe'),'inert fixture browser');Reset-Calls;$script:RemoveVendor=$true
$script:Packages=@([pscustomobject]@{name='Microsoft.MicrosoftEdge.Stable';fullName=$boundFull;family='Microsoft.MicrosoftEdge.Stable_8wekyb3d8bbwe';installLocation=(Join-Path $machine '157.0.0.0');removable=$false})
$target=@(Get-OptimusEdgeRemovalTargets)[0];$result=Invoke-OptimusEdgeRemovalTarget $target
Assert-Edge ($result.removed -and -not $result.remainingDetected -and $result.registrationStubsRemaining -eq 1 -and $script:PackageRemovals.Count -eq 0) 'An OS registration stub was confused with a launchable browser or force-removed.'

# Exact family validation excludes shared WebView2 and Windows App.
function RawPackage($name,$publisher='8wekyb3d8bbwe') {return [pscustomobject]@{Name=$name;DisplayName=$name;PackageFullName=($name+'_157.0.0.0_x64__'+$publisher);PackageName=($name+'_157.0.0.0_neutral_~_'+$publisher);PackageFamilyName=($name+'_'+$publisher);NonRemovable=$false;IsFramework=$false;IsResourcePackage=$false}}
Assert-Edge (Test-OptimusExactEdgePackage (RawPackage 'Microsoft.MicrosoftEdge.Stable')) 'Exact removable Edge package rejected.'
Assert-Edge (-not(Test-OptimusExactEdgePackage (RawPackage 'Microsoft.MicrosoftEdge.Stable' 'otherpub00000'))) 'Different publisher matched Edge package.'
foreach($name in @('MicrosoftCorporationII.Windows365','Microsoft.EdgeWebView2','Microsoft.MicrosoftEdge.Stable.Preview')){Assert-Edge (-not(Test-OptimusExactEdgePackage (RawPackage $name))) ('Unrelated package matched: '+$name)}

# A standalone removable package uses the native exact package operation only.
Reset-Calls;$script:Roots=@()
$full='Microsoft.MicrosoftEdge.Stable_157.0.0.0_x64__8wekyb3d8bbwe'
$script:Packages=@([pscustomobject]@{name='Microsoft.MicrosoftEdge.Stable';fullName=$full;family='Microsoft.MicrosoftEdge.Stable_8wekyb3d8bbwe';installLocation='';removable=$true})
$target=@(Get-OptimusEdgeRemovalTargets)[0];$result=Invoke-OptimusEdgeRemovalTarget $target
Assert-Edge ($result.removed -and $script:Launches.Count -eq 0 -and $script:PackageRemovals.Count -eq 1 -and $script:PackageRemovals[0].package -ceq $full) 'AppX-only removal did not stay within the exact package.'
$script:Packages=@([pscustomobject]@{name='Microsoft.MicrosoftEdge.Stable';fullName=$full;family='Microsoft.MicrosoftEdge.Stable_8wekyb3d8bbwe';installLocation='';removable=$false})
$target=@(Get-OptimusEdgeRemovalTargets)[0];$before=$script:PackageRemovals.Count;$result=Invoke-OptimusEdgeRemovalTarget $target
Assert-Edge (-not $result.removed -and $script:PackageRemovals.Count -eq $before) 'A nonremovable registration was forced through AppX.'

# Provisioned removal is separate, exact, opt-in, and administrator-only.
$script:Packages=@();$prov='Microsoft.MicrosoftEdge.Stable_157.0.0.0_neutral_~_8wekyb3d8bbwe'
$script:Provisioned=@([pscustomobject]@{name='Microsoft.MicrosoftEdge.Stable';fullName=$prov})
Assert-Edge (@(Get-OptimusEdgeRemovalTargets).Count -eq 0) 'Default preview expanded into future-user provisioning.'
$target=@(Get-OptimusEdgeRemovalTargets -IncludeProvisioned $true)[0];$result=Invoke-OptimusEdgeRemovalTarget $target
Assert-Edge ($result.removed -and $script:PackageRemovals[$script:PackageRemovals.Count-1].kind -ceq 'provisioned' -and $script:PackageRemovals[$script:PackageRemovals.Count-1].package -ceq $prov) 'Provisioned operation selected another package.'
$script:Admin=$false;$blocked=$false
try{Get-OptimusEdgeRemovalTargets -IncludeProvisioned $true|Out-Null}catch{$blocked=$true}
Assert-Edge $blocked 'Provisioned preview bypassed administrator access.'

# Exercise the real trust helper with fixture signatures: a similar publisher
# or invalid signature cannot become a trusted Microsoft vendor executable.
$script:SignatureStatus='Valid';$script:SignatureSubject='CN=Microsoft Corporation Evil, O=Fixture'
function Get-AuthenticodeSignature {param($LiteralPath,$ErrorAction)return [pscustomobject]@{Status=$script:SignatureStatus;SignerCertificate=[pscustomobject]@{Subject=$script:SignatureSubject}}}
$signed=&$nativeSignedFile $setupPath
Assert-Edge ($null -eq $signed) 'Similar publisher CN was accepted as Microsoft Corporation.'
$script:SignatureSubject='CN=Microsoft Corporation, O=Microsoft Corporation';$script:SignatureStatus='HashMismatch'
Assert-Edge ($null -eq (&$nativeSignedFile $setupPath)) 'Invalid signature was accepted.'
$script:SignatureStatus='Valid'
Assert-Edge ($null -ne (&$nativeSignedFile $setupPath)) 'Valid fixture publisher did not reach native fingerprint capture.'

# Unsupported Home/workgroup policy does not write a misleading registry key.
$script:Domain=$false
function Get-CimInstance {param($ClassName,$ErrorAction)return [pscustomobject]@{PartOfDomain=$script:Domain}}
$policy=Set-OptimusEdgeInstallBlock -BackupDirectory $fixture
Assert-Edge (-not $policy.supported -and -not $policy.applied -and $policy.reason -ceq 'active_directory_domain_required') 'Workgroup/Home claimed a reinstall block.'

# Applicable policy changes only the stable Edge product Install value. All
# registry cmdlets below are mocks; the real host registry is never written.
$script:Domain=$true;$script:Admin=$true;$script:Roots=@([pscustomobject]@{root=$machine;scope='machine'})
$updater=Join-Path (Split-Path (Split-Path $machine -Parent) -Parent) 'EdgeUpdate\MicrosoftEdgeUpdate.exe'
$null=New-Item -ItemType Directory -Path (Split-Path $updater -Parent) -Force;[IO.File]::WriteAllText($updater,'inert updater')
$script:Signatures[$updater]=[pscustomobject]@{path=$updater;sha256=('e'*64);version='1.3.193.5'}
$script:PolicyPath='HKLM:\SOFTWARE\Policies\Microsoft\EdgeUpdate';$script:PolicyName='Install{56EB18F8-B008-4CBD-B6D2-8C97FE7E9062}'
$script:PolicyValue=5;$script:PolicyWrites=New-Object Collections.ArrayList
$script:RegistryFixture=[pscustomobject]@{}
$script:RegistryFixture|Add-Member ScriptMethod GetValueNames {return @($script:PolicyName)}
$script:RegistryFixture|Add-Member ScriptMethod GetValueKind {param($Name)return [Microsoft.Win32.RegistryValueKind]::DWord}
$script:RegistryFixture|Add-Member ScriptMethod GetValue {param($Name,$Default,$Options)return $script:PolicyValue}
function Get-Item {param($LiteralPath,[switch]$Force,$ErrorAction)if($LiteralPath -ceq $script:PolicyPath){return $script:RegistryFixture};Microsoft.PowerShell.Management\Get-Item -LiteralPath $LiteralPath -Force:$Force -ErrorAction Stop}
function New-Item {param($Path,[switch]$Force,$ErrorAction)if($Path -cne $script:PolicyPath){throw 'Unexpected registry root.'};return $script:RegistryFixture}
function New-ItemProperty {param($LiteralPath,$Name,$Value,$PropertyType,[switch]$Force,$ErrorAction)if($LiteralPath -cne $script:PolicyPath -or $Name -cne $script:PolicyName -or $Value -ne 0 -or $PropertyType -cne 'DWord'){throw 'Unexpected policy mutation.'};$script:PolicyValue=$Value;$null=$script:PolicyWrites.Add($Name)}
$policy=Set-OptimusEdgeInstallBlock -BackupDirectory $fixture
Assert-Edge ($policy.supported -and $policy.applied -and $policy.verified -and $script:PolicyWrites.Count -eq 1) 'Applicable exact installation policy was not read back.'
$backup=Get-Content -LiteralPath $policy.backup -Raw|ConvertFrom-Json
Assert-Edge ($backup.value -eq 5 -and $backup.kind -ceq 'DWord' -and $backup.name -ceq $script:PolicyName) 'Previous policy metadata was not backed up.'
$again=Set-OptimusEdgeInstallBlock -BackupDirectory $fixture
Assert-Edge (-not $again.changed -and $script:PolicyWrites.Count -eq 1) 'Idempotent policy repeated a registry change.'

# The implementation has no generic folder deletion or shared-updater controls.
$body=Get-Content -LiteralPath (Join-Path $PSScriptRoot '..\src\optimus\engine\EdgeRemoval.ps1') -Raw
Assert-Edge ($body -notmatch '\bRemove-Item\b|\bStop-Service\b|\bSet-Service\b|\btakeown\b|\bicacls\b|InstallDefault|UpdateDefault|IntegratedServicesRegionPolicySet') 'Shared or region-system mutation entered Edge removal module.'
[pscustomobject]@{Result='PASS';Assertions=$script:Assertions;RealBrowserRemovals=0;RealExternalProgramsLaunched=0;PSVersion=$PSVersionTable.PSVersion.ToString();Fixture=$fixture}|ConvertTo-Json -Compress
