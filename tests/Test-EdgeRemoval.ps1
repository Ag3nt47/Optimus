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
$script:MicrosoftRoot=$null;$script:Clients=@();$script:Running=@();$script:CopilotRoots=@();$script:RemoveWhenAllowed=$false
$script:Allow=[pscustomobject]@{keyPresent=$false;present=$false;value=$null;kind=$null};$script:AllowWrites=New-Object Collections.ArrayList
function Get-OptimusEdgeMicrosoftRoot {return $script:MicrosoftRoot}
function Get-OptimusEdgeUpdateClients {return @($script:Clients)}
function Get-OptimusEdgeRunningUnder {param($Path)return @($script:Running|Where-Object {Test-OptimusEdgePathWithin $_.path $Path})}
function Get-OptimusCopilotKnownRoots {return @($script:CopilotRoots)}
function Get-OptimusEdgeAllowUninstall {return $script:Allow}
function Set-OptimusEdgeAllowUninstall {param($Restore)if($null -eq $Restore){$script:Allow=[pscustomobject]@{keyPresent=$true;present=$true;value=1;kind='DWord'};$null=$script:AllowWrites.Add('set')}else{$script:Allow=$Restore;$null=$script:AllowWrites.Add('restore')}}
function Get-OptimusEdgeSignedFile {param($Path)if((Test-Path -LiteralPath $Path -PathType Leaf)-and $script:Signatures.ContainsKey($Path)){return $script:Signatures[$Path]};return $null}
function Invoke-OptimusEdgeProcess {
 param($Path,$Arguments)
 $null=$script:Launches.Add([pscustomobject]@{path=$Path;arguments=@($Arguments)})
 if(($script:RemoveVendor -or ($script:RemoveWhenAllowed -and $script:Allow.value -eq 1)) -and ($Path -like '*\Installer\setup.exe' -or $Path -like '*\Installer\copilot_setup.exe')){
  $root=Split-Path (Split-Path (Split-Path $Path -Parent) -Parent) -Parent
  foreach($leaf in @('msedge.exe','mscopilot.exe')){$browser=Join-Path $root $leaf;if(Test-Path -LiteralPath $browser){Remove-Item -LiteralPath $browser -Force}}
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
function Reset-Calls {$script:RemoveWhenAllowed=$false;$script:Allow=[pscustomobject]@{keyPresent=$false;present=$false;value=$null;kind=$null};$script:AllowWrites.Clear();$script:Launches.Clear();$script:PackageRemovals.Clear();$script:ExitCodes.Clear();$script:Winget=$null;$script:RemoveVendor=$false;$script:RemoveAppxBrowser=$false}

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

# Windows' region refusal (setup exit 93) is retried once with the updater's
# AllowUninstall value, which is then restored. Region settings are untouched.
[IO.File]::WriteAllText((Join-Path $machine 'msedge.exe'),'inert fixture browser');Reset-Calls;$script:Packages=@()
$script:ExitCodes.Enqueue(93);$script:ExitCodes.Enqueue(93)
$target=@(Get-OptimusEdgeRemovalTargets|Where-Object {$_.payload.root -ceq $machine})[0];$result=Invoke-OptimusEdgeRemovalTarget $target
Assert-Edge (-not $result.removed -and $result.reason -ceq 'edge_uninstall_region_blocked' -and $script:Launches.Count -eq 2 -and ($script:AllowWrites -join ',') -ceq 'set,restore' -and -not $script:Allow.present) 'Region-blocked uninstall was not retried once and restored.'
Assert-Edge ($script:Launches[0].arguments -contains '--channel=stable') 'Registered stable channel was not passed to the Edge uninstaller.'
Reset-Calls;$script:ExitCodes.Enqueue(93);$script:ExitCodes.Enqueue(0);$script:RemoveWhenAllowed=$true
$target=@(Get-OptimusEdgeRemovalTargets|Where-Object {$_.payload.root -ceq $machine})[0];$result=Invoke-OptimusEdgeRemovalTarget $target
Assert-Edge ($result.removed -and $result.attempts[1].method -ceq 'microsoft-setup-allow-uninstall' -and -not $script:Allow.present) 'AllowUninstall retry did not complete a verified removal.'
[IO.File]::WriteAllText((Join-Path $user 'msedge.exe'),'inert fixture browser');Reset-Calls;$script:ExitCodes.Enqueue(93)
$target=@(Get-OptimusEdgeRemovalTargets|Where-Object {$_.payload.root -ceq $user})[0];$result=Invoke-OptimusEdgeRemovalTarget $target
Assert-Edge ($script:AllowWrites.Count -eq 0 -and $script:Launches.Count -eq 1) 'A per-user removal wrote machine updater values.'

# After verified browser removal, only the exact unregistered machine product
# folder is deleted; a registered product, link or running process keeps it.
$msRoot=Split-Path (Split-Path $machine -Parent) -Parent;$script:MicrosoftRoot=$msRoot
$edgeFolder=Split-Path $machine -Parent;[IO.File]::WriteAllText((Join-Path $machine '157.0.0.0\leftover.dat'),'x');[IO.File]::WriteAllText((Join-Path $machine 'msedge.exe'),'inert fixture browser')
Reset-Calls;$script:RemoveVendor=$true
$target=@(Get-OptimusEdgeRemovalTargets|Where-Object {$_.payload.root -ceq $machine})[0];$result=Invoke-OptimusEdgeRemovalTarget $target
Assert-Edge ($result.removed -and $result.leftoverFolders[0].removed -and -not(Test-Path -LiteralPath $edgeFolder)) 'Verified Edge removal left its exact product folder.'
$script:Roots=@($script:Roots|Where-Object {$_.root -cne $machine});$machine=Add-EdgeRoot 'machine' 'machine';$edgeFolder=Split-Path $machine -Parent
$script:Clients=@([pscustomobject]@{guid='x';name='Microsoft Edge';version='157.0.0.0';location=$machine})
Assert-Edge ((Get-OptimusEdgeLeftoverBlocker $edgeFolder) -ceq 'registered_product:Microsoft Edge') 'A registered product folder was treated as leftover.'
$script:Clients=@();$script:Running=@([pscustomobject]@{pid=7;name='msedge.exe';path=(Join-Path $machine 'msedge.exe')})
Assert-Edge ((Get-OptimusEdgeLeftoverBlocker $edgeFolder) -ceq 'in_use') 'A running product folder was treated as leftover.'
$script:Running=@()
$webview=Join-Path $msRoot 'EdgeWebView';$null=New-Item -ItemType Directory -Path $webview -Force
Assert-Edge ((Get-OptimusEdgeLeftoverBlocker $webview) -ceq 'not_an_exact_product_folder' -and (Get-OptimusEdgeLeftoverBlocker $msRoot) -ceq 'not_an_exact_product_folder') 'WebView2 or the Microsoft folder was eligible for deletion.'
$deleted=Remove-OptimusEdgeLeftoverFolder $webview
Assert-Edge (-not $deleted.removed -and (Test-Path -LiteralPath $webview)) 'Shared WebView2 folder was deleted.'

# Copilot uses its exact registered Microsoft uninstaller, then its folder.
$copilotRoot=Join-Path $msRoot 'Copilot\Application';$copilotSetup=Join-Path $copilotRoot '157.0.0.0\Installer\copilot_setup.exe'
function New-CopilotFixture {
 $null=New-Item -ItemType Directory -Path (Split-Path $copilotSetup -Parent) -Force
 [IO.File]::WriteAllText($copilotSetup,'inert');[IO.File]::WriteAllText((Join-Path $copilotRoot 'mscopilot.exe'),'inert')
 $script:Signatures[$copilotSetup]=[pscustomobject]@{path=$copilotSetup;sha256=('f'*64);version='157.0.0.0'}
 $script:Signatures[(Join-Path $copilotRoot 'mscopilot.exe')]=[pscustomobject]@{path=(Join-Path $copilotRoot 'mscopilot.exe');sha256=('9'*64);version='157.0.0.0'}
}
New-CopilotFixture
$script:CopilotRoots=@([pscustomobject]@{root=$copilotRoot;scope='machine'});Reset-Calls;$script:RemoveVendor=$true
$target=@(Get-OptimusEdgeRemovalTargets|Where-Object {$_.payload.type -ceq 'copilot'})[0]
Assert-Edge ($target.name -ceq 'Microsoft Copilot app' -and $target.payload.safe) 'Copilot target was not discovered.'
$result=Invoke-OptimusEdgeRemovalTarget $target
Assert-Edge ($result.removed -and $script:Launches[0].path -ceq $copilotSetup -and $script:Launches[0].arguments -contains '--mscopilot' -and $script:Launches[0].arguments -contains '--system-level' -and -not(Test-Path -LiteralPath (Split-Path $copilotRoot -Parent))) 'Copilot was not removed through its exact uninstaller with folder cleanup.'
New-CopilotFixture;Reset-Calls;$script:ExitCodes.Enqueue(1)
$target=@(Get-OptimusEdgeRemovalTargets|Where-Object {$_.payload.type -ceq 'copilot'})[0];$result=Invoke-OptimusEdgeRemovalTarget $target
Assert-Edge (-not $result.removed -and $result.reason -ceq 'copilot_still_detected' -and (Test-Path -LiteralPath $copilotRoot)) 'A failed Copilot uninstall deleted files.'
$script:CopilotRoots=@()

# EdgeCore is deleted only while every registered product keeps its own binaries.
$core=Join-Path $msRoot 'EdgeCore';$null=New-Item -ItemType Directory -Path (Join-Path $core '157.0.0.0') -Force;[IO.File]::WriteAllText((Join-Path $core '157.0.0.0\msedge.dll'),'x')
$wvApp=Join-Path $webview 'Application';$null=New-Item -ItemType Directory -Path (Join-Path $wvApp '157.0.0.0') -Force
$script:Clients=@([pscustomobject]@{guid='w';name='Microsoft Edge WebView2 Runtime';version='157.0.0.0';location=$wvApp},[pscustomobject]@{guid='c';name='Copilot';version='158.0.0.0';location=$copilotRoot})
$target=@(Get-OptimusEdgeRemovalTargets|Where-Object {$_.payload.type -ceq 'edgecore'})[0]
Assert-Edge (-not $target.payload.safe -and @($target.payload.dependentProducts) -ccontains 'Copilot') 'A product without its own binaries did not keep EdgeCore.'
$result=Invoke-OptimusEdgeRemovalTarget $target
Assert-Edge (-not $result.removed -and (Test-Path -LiteralPath $core)) 'EdgeCore was deleted while a product depended on it.'
$script:Clients=@($script:Clients[0]);$script:Running=@([pscustomobject]@{pid=8;name='x.exe';path=(Join-Path $core '157.0.0.0\x.exe')})
$target=@(Get-OptimusEdgeRemovalTargets|Where-Object {$_.payload.type -ceq 'edgecore'})[0];$result=Invoke-OptimusEdgeRemovalTarget $target
Assert-Edge (-not $result.removed -and $result.reason -ceq 'edgecore_in_use' -and (Test-Path -LiteralPath $core)) 'EdgeCore in use was deleted.'
$script:Running=@()
$target=@(Get-OptimusEdgeRemovalTargets|Where-Object {$_.payload.type -ceq 'edgecore'})[0];$result=Invoke-OptimusEdgeRemovalTarget $target
Assert-Edge ($result.removed -and -not(Test-Path -LiteralPath $core) -and (Test-Path -LiteralPath $wvApp)) 'Unused EdgeCore was not removed or WebView2 was touched.'
$script:MicrosoftRoot=$null;$script:Clients=@()

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
$withoutLeftover=[regex]::Replace($body,'(?s)function Remove-OptimusEdgeLeftoverFolder \{.*?\r?\n\}\r?\n','')
Assert-Edge ($withoutLeftover -notmatch '\bRemove-Item\b|\bStop-Service\b|\bSet-Service\b|\btakeown\b|\bicacls\b|InstallDefault|UpdateDefault|IntegratedServicesRegionPolicySet') 'Shared or region-system mutation entered Edge removal module.'
[pscustomobject]@{Result='PASS';Assertions=$script:Assertions;RealBrowserRemovals=0;RealExternalProgramsLaunched=0;PSVersion=$PSVersionTable.PSVersion.ToString();Fixture=$fixture}|ConvertTo-Json -Compress
