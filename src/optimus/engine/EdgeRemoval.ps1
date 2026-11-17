#requires -Version 5.1
# Exact Edge browser, Copilot app and unused EdgeCore removal. The shared
# WebView2 runtime, Edge updater, browser profiles and region settings are
# never removed or edited. Folder deletion is limited to the exact product
# folders below Program Files (x86)\Microsoft once nothing registered or
# running uses them; no ownership or ACL changes are made.
function Get-OptimusEdgeMicrosoftRoot {
    $base=[Environment]::GetFolderPath('ProgramFilesX86')
    if(-not $base){return $null}
    return [IO.Path]::GetFullPath((Join-Path $base 'Microsoft')).TrimEnd('\')
}
function Get-OptimusEdgeUpdateClients {
    # Every product the Microsoft Edge updater still services (Edge, Copilot,
    # WebView2, ...). A registered location is never treated as leftover.
    $rows=@()
    foreach($key in @(Get-ChildItem -LiteralPath 'HKLM:\SOFTWARE\WOW6432Node\Microsoft\EdgeUpdate\Clients' -ErrorAction SilentlyContinue)) {
        $value=Get-ItemProperty -LiteralPath $key.PSPath -ErrorAction SilentlyContinue
        if(-not $value){continue}
        $rows+=[pscustomobject]@{guid=[string]$key.PSChildName;name=[string]$value.name;version=[string]$value.pv;location=[string]$value.location}
    }
    return @($rows)
}
function Get-OptimusEdgeRunningUnder {
    param([string]$Path)
    $prefix=[IO.Path]::GetFullPath($Path).TrimEnd('\')+'\'
    return @(Get-CimInstance Win32_Process -ErrorAction Stop | Where-Object {$_.ExecutablePath -and ([string]$_.ExecutablePath).StartsWith($prefix,[StringComparison]::OrdinalIgnoreCase)} |
        ForEach-Object {[pscustomobject]@{pid=[int]$_.ProcessId;name=[string]$_.Name;path=[string]$_.ExecutablePath}})
}
function Test-OptimusEdgePathWithin {
    param([string]$Child,[string]$Parent)
    if(-not $Child -or -not $Parent){return $false}
    try{$c=[IO.Path]::GetFullPath($Child).TrimEnd('\');$p=[IO.Path]::GetFullPath($Parent).TrimEnd('\')}catch{return $false}
    return ($c -ieq $p -or $c.StartsWith($p+'\',[StringComparison]::OrdinalIgnoreCase))
}
function Get-OptimusEdgeLeftoverBlocker {
    # Returns $null when the exact product folder is safe to delete.
    param([string]$Path)
    $microsoft=Get-OptimusEdgeMicrosoftRoot
    $full=[IO.Path]::GetFullPath($Path).TrimEnd('\')
    if(-not $microsoft -or [IO.Path]::GetDirectoryName($full) -ine $microsoft -or (Split-Path $full -Leaf) -cnotin @('Edge','Copilot','EdgeCore')){return 'not_an_exact_product_folder'}
    if(-not(Test-OptimusEdgeOrdinaryPath $full)){return 'indirect_path'}
    foreach($client in @(Get-OptimusEdgeUpdateClients)) {
        if($client.location -and (Test-OptimusEdgePathWithin $client.location $full)){return ('registered_product:'+$client.name)}
    }
    if(@(Get-OptimusEdgeRunningUnder $full).Count){return 'in_use'}
    $entries=@(Get-ChildItem -LiteralPath $full -Recurse -Force -ErrorAction Stop)
    if($entries.Count -gt 200000){return 'unexpected_inventory_size'}
    if(@($entries|Where-Object {$_.Attributes -band [IO.FileAttributes]::ReparsePoint}).Count){return 'contains_links'}
    return $null
}
function Remove-OptimusEdgeLeftoverFolder {
    param([string]$Path)
    $full=[IO.Path]::GetFullPath($Path).TrimEnd('\')
    if(-not(Test-Path -LiteralPath $full -PathType Container)){return [pscustomobject]@{path=$full;removed=$true;reason='already_absent'}}
    $blocker=Get-OptimusEdgeLeftoverBlocker $full
    if($blocker){return [pscustomobject]@{path=$full;removed=$false;reason=$blocker}}
    $size=(@(Get-ChildItem -LiteralPath $full -Recurse -Force -File -ErrorAction SilentlyContinue)|Measure-Object Length -Sum).Sum
    try{Remove-Item -LiteralPath $full -Recurse -Force -ErrorAction Stop}catch{return [pscustomobject]@{path=$full;removed=$false;reason='delete_failed';error=$_.Exception.Message}}
    $gone=-not(Test-Path -LiteralPath $full)
    return [pscustomobject]@{path=$full;removed=$gone;reason=$(if($gone){'removed'}else{'partially_removed'});bytes=[int64]$size}
}
function Invoke-OptimusEdgeLeftoverCleanup {
    # Cleanup after a verified uninstall is best effort and never changes
    # whether the product itself was removed.
    param([string]$Path)
    try{return Remove-OptimusEdgeLeftoverFolder $Path}
    catch{return [pscustomobject]@{path=$Path;removed=$false;reason='cleanup_error';error=$_.Exception.Message}}
}
function Get-OptimusEdgeAllowUninstall {
    $key=Get-Item -LiteralPath 'HKLM:\SOFTWARE\WOW6432Node\Microsoft\EdgeUpdateDev' -ErrorAction SilentlyContinue
    $present=($null -ne $key -and @($key.GetValueNames()) -ccontains 'AllowUninstall')
    return [pscustomobject]@{keyPresent=($null -ne $key);present=$present;value=$(if($present){$key.GetValue('AllowUninstall')}else{$null});kind=$(if($present){[string]$key.GetValueKind('AllowUninstall')}else{$null})}
}
function Set-OptimusEdgeAllowUninstall {
    # Microsoft's Edge setup honours this updater value when Windows would
    # otherwise refuse browser uninstall (exit 93). Region settings are untouched.
    param($Restore)
    $path='HKLM:\SOFTWARE\WOW6432Node\Microsoft\EdgeUpdateDev'
    if($null -eq $Restore) {
        if(-not(Test-Path -LiteralPath $path)){$null=New-Item -Path $path -Force -ErrorAction Stop}
        $null=New-ItemProperty -LiteralPath $path -Name 'AllowUninstall' -Value 1 -PropertyType DWord -Force -ErrorAction Stop
        return
    }
    if($Restore.present){$null=New-ItemProperty -LiteralPath $path -Name 'AllowUninstall' -Value $Restore.value -PropertyType $Restore.kind -Force -ErrorAction Stop}
    elseif(Test-Path -LiteralPath $path){Remove-ItemProperty -LiteralPath $path -Name 'AllowUninstall' -Force -ErrorAction SilentlyContinue}
}
function Get-OptimusEdgeRemovalHash {
    param([string]$Text)
    $hash=[Security.Cryptography.SHA256]::Create()
    try { return ([BitConverter]::ToString($hash.ComputeHash([Text.Encoding]::UTF8.GetBytes($Text)))).Replace('-','').ToLowerInvariant() }
    finally { $hash.Dispose() }
}
function Test-OptimusEdgeRemovalAdmin {
    return ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
}
function Test-OptimusEdgeOrdinaryPath {
    param([string]$Path)
    try {
        for($part=[IO.Path]::GetFullPath($Path);$part;$part=[IO.Path]::GetDirectoryName($part)) {
            if((Get-Item -LiteralPath $part -Force -ErrorAction Stop).Attributes -band [IO.FileAttributes]::ReparsePoint){return $false}
        }
        return $true
    } catch { return $false }
}
function Get-OptimusEdgeSignedFile {
    param([string]$Path)
    if(-not(Test-Path -LiteralPath $Path -PathType Leaf) -or -not(Test-OptimusEdgeOrdinaryPath $Path)){return $null}
    $signature=Get-AuthenticodeSignature -LiteralPath $Path -ErrorAction Stop
    if($signature.Status -ne 'Valid' -or $signature.SignerCertificate.Subject -notmatch '(?i)(?:^|,\s*)CN=Microsoft Corporation(?:,|$)'){return $null}
    $file=Get-Item -LiteralPath $Path -Force -ErrorAction Stop
    $version=[regex]::Match([string]$file.VersionInfo.ProductVersion,'^\d+(?:\.\d+){2,3}').Value
    return [pscustomobject]@{path=$file.FullName;sha256=(Get-FileHash -LiteralPath $file.FullName -Algorithm SHA256 -ErrorAction Stop).Hash.ToLowerInvariant();version=$version}
}
function Get-OptimusEdgeKnownRoots {
    $seen=@{}
    foreach($row in @(
        @([Environment]::GetFolderPath('ProgramFilesX86'),'machine'),
        @([Environment]::GetFolderPath('ProgramFiles'),'machine'),
        @([Environment]::GetFolderPath('LocalApplicationData'),'current-user'))) {
        if(-not $row[0]){continue}
        $root=[IO.Path]::GetFullPath((Join-Path $row[0] 'Microsoft\Edge\Application')).TrimEnd('\')
        if($seen.ContainsKey($root.ToLowerInvariant())){continue};$seen[$root.ToLowerInvariant()]=$true
        [pscustomobject]@{root=$root;scope=$row[1]}
    }
}
function Test-OptimusExactEdgePackage {
    param($Package,[switch]$Provisioned)
    $name=if($Provisioned){[string]$Package.DisplayName}else{[string]$Package.Name}
    $full=if($Provisioned){[string]$Package.PackageName}else{[string]$Package.PackageFullName}
    if($name -cne 'Microsoft.MicrosoftEdge.Stable'){return $false}
    $parts=$full.Split('_')
    if($parts.Count -ne 5 -or $parts[0] -cne $name -or $parts[1] -cnotmatch '^\d+(\.\d+){1,3}$' -or
        $parts[2] -cnotin @('x86','x64','arm','arm64','neutral') -or $parts[3] -cnotmatch '^[A-Za-z0-9.~]*$' -or $parts[4] -cne '8wekyb3d8bbwe'){return $false}
    if(-not $Provisioned -and ([string]$Package.PackageFamilyName -cne 'Microsoft.MicrosoftEdge.Stable_8wekyb3d8bbwe' -or $Package.IsFramework -eq $true -or $Package.IsResourcePackage -eq $true)){return $false}
    return $true
}
function Get-OptimusEdgePackages {
    return @(Get-AppxPackage -Name 'Microsoft.MicrosoftEdge.Stable' -ErrorAction Stop | Where-Object {Test-OptimusExactEdgePackage $_} |
        ForEach-Object {[pscustomobject]@{name=$_.Name;fullName=$_.PackageFullName;family=$_.PackageFamilyName;installLocation=[string]$_.InstallLocation;removable=($_.NonRemovable -eq $false)}} | Sort-Object fullName)
}
function Get-OptimusEdgeProvisionedPackages {
    return @(Get-AppxProvisionedPackage -Online -ErrorAction Stop | Where-Object {Test-OptimusExactEdgePackage $_ -Provisioned} |
        ForEach-Object {[pscustomobject]@{name=$_.DisplayName;fullName=$_.PackageName}} | Sort-Object fullName)
}
function Get-OptimusEdgeWinget {
    # Resolve the real, signed executable from the exact installed package,
    # rather than trusting an arbitrary PATH executable or an AppExecLink.
    foreach($package in @(Get-AppxPackage -Name 'Microsoft.DesktopAppInstaller' -ErrorAction SilentlyContinue)) {
        if([string]$package.PackageFamilyName -cne 'Microsoft.DesktopAppInstaller_8wekyb3d8bbwe'){continue}
        $file=Get-OptimusEdgeSignedFile (Join-Path ([string]$package.InstallLocation) 'winget.exe')
        if($file){return $file}
    }
    return $null
}
function Get-OptimusEdgeSetups {
    param([string]$Root,[string]$BrowserVersion)
    if($BrowserVersion -cnotmatch '^\d+(\.\d+){2,3}$'){return @()}
    $setups=@()
    $directories=@(Get-ChildItem -LiteralPath $Root -Directory -ErrorAction Stop | Where-Object {$_.Name -cmatch '^\d+(\.\d+){2,3}$'})
    if($directories.Count -gt 100){throw 'Unexpected Edge installer inventory size.'}
    foreach($directory in $directories) {
        if($BrowserVersion -and $directory.Name -cne $BrowserVersion){continue}
        $file=Get-OptimusEdgeSignedFile (Join-Path $directory.FullName 'Installer\setup.exe')
        if($file){$setups+=@([pscustomobject]@{path=$file.path;sha256=$file.sha256;version=$directory.Name})}
    }
    return @($setups|Sort-Object {[version]$_.version} -Descending)
}
function New-OptimusEdgeRemovalTarget {
    param([string]$Identity,[string]$Scope,$Payload,[string]$Name='Microsoft Edge browser')
    $fingerprint=Get-OptimusEdgeRemovalHash (ConvertTo-Json -InputObject ([ordered]@{scope=$Scope;payload=$Payload}) -Depth 12 -Compress)
    $recovery=switch([string]$Payload.type){
        'copilot'{'Reinstall Copilot from Microsoft. Shared WebView2 is preserved.'}
        'edgecore'{'Microsoft Edge Update downloads EdgeCore again if a product needs it. Shared WebView2 is preserved.'}
        default{'Reinstall the Edge browser from Microsoft. Browser profile data and shared WebView2 are preserved.'}
    }
    return [pscustomobject]@{id=('edge-'+(Get-OptimusEdgeRemovalHash $Identity).Substring(0,20));kind='edge';name=$Name;scope=$Scope;appIdentity='Microsoft.Edge';fingerprint=$fingerprint;payload=$Payload;method='edge-scoped';admin=($Scope -in @('machine','future-users'));selected=$false;recovery=$recovery}
}
function Get-OptimusCopilotKnownRoots {
    $microsoft=Get-OptimusEdgeMicrosoftRoot
    $rows=@()
    if($microsoft){$rows+=[pscustomobject]@{root=(Join-Path $microsoft 'Copilot\Application');scope='machine'}}
    $local=[Environment]::GetFolderPath('LocalApplicationData')
    if($local){$rows+=[pscustomobject]@{root=[IO.Path]::GetFullPath((Join-Path $local 'Microsoft\Copilot\Application')).TrimEnd('\');scope='current-user'}}
    return @($rows)
}
function Get-OptimusCopilotTargets {
    $targets=@()
    foreach($known in @(Get-OptimusCopilotKnownRoots)) {
        $root=[IO.Path]::GetFullPath([string]$known.root).TrimEnd('\')
        $app=Join-Path $root 'mscopilot.exe'
        if(-not(Test-Path -LiteralPath $app -PathType Leaf)){continue}
        $ordinary=Test-OptimusEdgeOrdinaryPath $app
        $binary=if($ordinary){Get-OptimusEdgeSignedFile $app}else{$null}
        $setup=$null
        if($binary -and $binary.version -cmatch '^\d+(\.\d+){2,3}$') {
            $setup=Get-OptimusEdgeSignedFile (Join-Path $root ($binary.version+'\Installer\copilot_setup.exe'))
            if(-not $setup){$setup=Get-OptimusEdgeSignedFile (Join-Path $root ($binary.version+'\Installer\setup.exe'))}
        }
        $payload=[ordered]@{type='copilot';root=$root;app=$app;scope=$known.scope;appFile=$binary;setup=$setup;safe=($ordinary -and $null -ne $binary -and $null -ne $setup)}
        $targets+=@(New-OptimusEdgeRemovalTarget ('copilot|'+$root.ToLowerInvariant()) $known.scope $payload 'Microsoft Copilot app')
    }
    return @($targets)
}
function Get-OptimusEdgeCoreTarget {
    # EdgeCore is a staged Edge engine payload. It is removable only while no
    # updater-registered product lives in it and each registered product
    # (including WebView2) still has its own versioned binaries.
    $microsoft=Get-OptimusEdgeMicrosoftRoot
    if(-not $microsoft){return @()}
    $core=Join-Path $microsoft 'EdgeCore'
    if(-not(Test-Path -LiteralPath $core -PathType Container)){return @()}
    $clients=@(Get-OptimusEdgeUpdateClients)
    $dependent=@($clients|Where-Object {$_.location -and $_.version -cmatch '^\d+(\.\d+){2,3}$' -and (Test-Path -LiteralPath $_.location -PathType Container) -and
        -not(Test-Path -LiteralPath (Join-Path $_.location $_.version) -PathType Container)} | ForEach-Object {$_.name})
    $children=@(Get-ChildItem -LiteralPath $core -Force -ErrorAction Stop | ForEach-Object {$_.Name} | Sort-Object)
    $payload=[ordered]@{type='edgecore';root=$core;children=$children;dependentProducts=$dependent;safe=($dependent.Count -eq 0)}
    return @(New-OptimusEdgeRemovalTarget ('edgecore|'+$core.ToLowerInvariant()) 'machine' $payload 'Unused Edge engine files (EdgeCore)')
}
function Get-OptimusEdgeRemovalTargets {
    param([bool]$IncludeProvisioned=$false)
    $packages=@(Get-OptimusEdgePackages)
    $assigned=@{}
    $winget=Get-OptimusEdgeWinget
    $targets=@()
    foreach($known in @(Get-OptimusEdgeKnownRoots)) {
        $root=[IO.Path]::GetFullPath([string]$known.root).TrimEnd('\')
        $browser=Join-Path $root 'msedge.exe'
        if(-not(Test-Path -LiteralPath $browser -PathType Leaf)){continue} # Stale installer folders are not browser targets.
        $ordinary=Test-OptimusEdgeOrdinaryPath $browser
        $binary=if($ordinary){Get-OptimusEdgeSignedFile $browser}else{$null}
        $setups=@()
        if($binary){$setups=@(Get-OptimusEdgeSetups $root $binary.version)}
        $bound=@($packages|Where-Object {$_.installLocation -and ($_.installLocation.TrimEnd('\') -ieq $root -or $_.installLocation.StartsWith($root+'\',[StringComparison]::OrdinalIgnoreCase))})
        foreach($package in $bound){$assigned[$package.fullName]=$true}
        $payload=[ordered]@{root=$root;browser=$browser;scope=$known.scope;browserFile=$binary;setup=$(if($setups.Count){$setups[0]}else{$null});packages=$bound;winget=$winget;safe=($ordinary -and $null -ne $binary)}
        $targets+=@(New-OptimusEdgeRemovalTarget ('browser|'+$root.ToLowerInvariant()) $known.scope $payload)
    }
    foreach($package in $packages) {
        if($assigned.ContainsKey($package.fullName)){continue}
        $targets+=@(New-OptimusEdgeRemovalTarget ('appx|'+$package.fullName) 'current-user' ([ordered]@{package=$package;type='appx'}) 'Microsoft Edge Store registration')
    }
    $targets+=@(Get-OptimusCopilotTargets)
    $targets+=@(Get-OptimusEdgeCoreTarget)
    if($IncludeProvisioned) {
        if(-not(Test-OptimusEdgeRemovalAdmin)){throw 'Administrator access is required for Edge provisioning removal.'}
        foreach($package in @(Get-OptimusEdgeProvisionedPackages)) {
            $targets+=@(New-OptimusEdgeRemovalTarget ('provisioned|'+$package.fullName) 'future-users' ([ordered]@{package=$package;type='provisioned'}) 'Microsoft Edge provisioning for future users')
        }
    }
    return @($targets)
}
function Publish-OptimusEdgeRemovalProgress {
    param([scriptblock]$ProgressCallback,$Target,[string]$Phase,$Percent)
    if($ProgressCallback){try{$null=&$ProgressCallback ([pscustomobject]@{key=('debloat:edge:'+$Target.id);label=$Target.name;phase=$Phase;percent=$Percent})}catch{}}
}
function Invoke-OptimusEdgeProcess {
    param([string]$Path,[string[]]$Arguments)
    $process=Start-Process -FilePath $Path -ArgumentList $Arguments -WindowStyle Hidden -PassThru -ErrorAction Stop
    try{$null=$process.Handle}finally{$process.WaitForExit()}
    $process.Refresh()
    if($null -eq $process.ExitCode){throw 'The Edge uninstaller did not report a terminal exit code.'}
    return [int]$process.ExitCode
}
function Remove-OptimusExactEdgePackage {
    param($Package,[switch]$Provisioned)
    if($Provisioned) {
        $current=@(Get-OptimusEdgeProvisionedPackages|Where-Object {$_.fullName -ceq $Package.fullName})
        if($current.Count -ne 1){throw 'The exact Edge provisioning selection changed.'}
        if(-not(Test-OptimusEdgeRemovalAdmin)){throw 'Administrator access is required for provisioning removal.'}
        $result=@(Remove-AppxProvisionedPackage -Online -PackageName $Package.fullName -ErrorAction Stop)
        if(@(Get-OptimusEdgeProvisionedPackages|Where-Object {$_.fullName -ceq $Package.fullName}).Count){throw 'The exact Edge package remains provisioned.'}
        $restart=if($result.Count -eq 1 -and $result[0].RestartNeeded -is [bool]){$result[0].RestartNeeded}else{$null}
        return [pscustomobject]@{method='appx-provisioning';removed=$true;restartNeeded=$restart}
    }
    $current=@(Get-OptimusEdgePackages|Where-Object {$_.fullName -ceq $Package.fullName -and $_.family -ceq 'Microsoft.MicrosoftEdge.Stable_8wekyb3d8bbwe'})
    if($current.Count -ne 1 -or $current[0].removable -ne $true){throw 'The exact Edge Store package is changed or nonremovable.'}
    Remove-AppxPackage -Package $Package.fullName -Confirm:$false -ErrorAction Stop
    if(@(Get-OptimusEdgePackages|Where-Object {$_.fullName -ceq $Package.fullName}).Count){throw 'The exact Edge Store package remains registered.'}
    return [pscustomobject]@{method='appx';removed=$true;restartNeeded=$false}
}
function Invoke-OptimusEdgeRemovalTarget {
    param([Parameter(Mandatory=$true)]$Target,[scriptblock]$ProgressCallback)
    $attempts=New-Object Collections.ArrayList
    $restart=$false
    try {
        $fresh=@(Get-OptimusEdgeRemovalTargets -IncludeProvisioned:($Target.scope -ceq 'future-users')|Where-Object {$_.id -ceq $Target.id})
        if($fresh.Count -ne 1 -or $fresh[0].fingerprint -cne $Target.fingerprint){throw 'The exact Edge installation changed; create a fresh removal preview.'}
        $target=$fresh[0];$payload=$target.payload
        if($payload.type -ceq 'edgecore') {
            if($payload.safe -ne $true){throw ('EdgeCore is still used by: '+(@($payload.dependentProducts) -join ', '))}
            if(-not(Test-OptimusEdgeRemovalAdmin)){throw 'Administrator access is required to remove EdgeCore.'}
            Publish-OptimusEdgeRemovalProgress $ProgressCallback $target 'uninstall' $null
            $folder=Remove-OptimusEdgeLeftoverFolder $payload.root
            $null=$attempts.Add([pscustomobject]@{method='leftover-folder';path=$folder.path;removed=$folder.removed;reason=$folder.reason})
            Publish-OptimusEdgeRemovalProgress $ProgressCallback $target 'verify' $null
            if($folder.removed){Publish-OptimusEdgeRemovalProgress $ProgressCallback $target 'uninstall' 100}
            return [pscustomobject]@{id=$target.id;name=$target.name;removed=$folder.removed;reason=$(if($folder.removed){'removal_verified'}else{'edgecore_'+$folder.reason});status=$(if($folder.removed){'Unused EdgeCore files removed'}else{'EdgeCore kept: '+$folder.reason});restartNeeded=$false;requiresReview=(-not $folder.removed);attempts=$attempts.ToArray();leftoverFolders=@($folder);sharedComponentsPreserved=$true}
        }
        if($payload.type -ceq 'copilot') {
            if($payload.safe -ne $true){throw 'The Copilot app or its signed Microsoft uninstaller could not be verified.'}
            if($target.scope -ceq 'machine' -and -not(Test-OptimusEdgeRemovalAdmin)){throw 'Administrator access is required for this machine Copilot installation.'}
            Publish-OptimusEdgeRemovalProgress $ProgressCallback $target 'uninstall' $null
            $app=Get-OptimusEdgeSignedFile $payload.app
            $setup=Get-OptimusEdgeSignedFile $payload.setup.path
            if(-not $app -or $app.sha256 -cne $payload.appFile.sha256 -or -not $setup -or $setup.sha256 -cne $payload.setup.sha256 -or
                -not $setup.path.StartsWith($payload.root+'\',[StringComparison]::OrdinalIgnoreCase)){throw 'The same-root signed Copilot uninstaller changed.'}
            $arguments=@('--uninstall','--mscopilot','--channel=stable','--force-uninstall','--verbose-logging')
            if($target.scope -ceq 'machine'){$arguments+='--system-level'}
            $exit=Invoke-OptimusEdgeProcess $setup.path $arguments
            $null=$attempts.Add([pscustomobject]@{method='microsoft-setup';exitCode=$exit})
            $restart=($exit -eq 3010)
            Publish-OptimusEdgeRemovalProgress $ProgressCallback $target 'verify' $null
            $present=Test-Path -LiteralPath $payload.app -PathType Leaf
            $folders=@()
            if(-not $present -and $target.scope -ceq 'machine'){$folders+=@(Invoke-OptimusEdgeLeftoverCleanup (Split-Path $payload.root -Parent))}
            $success=-not $present
            if($success -and -not $restart){Publish-OptimusEdgeRemovalProgress $ProgressCallback $target 'uninstall' 100}
            return [pscustomobject]@{id=$target.id;name=$target.name;removed=$success;reason=$(if($success){'removal_verified'}elseif($restart){'restart_required'}else{'copilot_still_detected'});status=$(if($success){'Copilot app removed'}elseif($restart){'Restart required; Copilot removal is not yet confirmed'}else{'Copilot remains installed; the Microsoft uninstaller returned '+$exit});remainingDetected=$present;restartNeeded=$restart;requiresReview=(-not $success -or $restart);attempts=$attempts.ToArray();leftoverFolders=$folders;sharedComponentsPreserved=$true}
        }
        if($payload.type -in @('appx','provisioned')) {
            Publish-OptimusEdgeRemovalProgress $ProgressCallback $target 'uninstall' $null
            $result=Remove-OptimusExactEdgePackage $payload.package -Provisioned:($payload.type -ceq 'provisioned')
            $null=$attempts.Add($result);$restart=$result.restartNeeded
            Publish-OptimusEdgeRemovalProgress $ProgressCallback $target 'verify' $null
            if($restart -is [bool] -and -not $restart){Publish-OptimusEdgeRemovalProgress $ProgressCallback $target 'uninstall' 100}
            return [pscustomobject]@{id=$target.id;name=$target.name;removed=$true;reason='removal_verified';status='Exact Edge registration removed';restartNeeded=$restart;requiresReview=($restart -ne $false);attempts=$attempts.ToArray();sharedComponentsPreserved=$true}
        }
        if($payload.safe -ne $true){throw 'Edge browser path or Microsoft signature could not be verified.'}
        if($target.scope -ceq 'machine' -and -not(Test-OptimusEdgeRemovalAdmin)){throw 'Administrator access is required for this machine Edge installation.'}
        Publish-OptimusEdgeRemovalProgress $ProgressCallback $target 'uninstall' $null
        $canFallback=$true;$regionBlocked=$false;$exit=$null
        if($payload.winget) {
            $manager=Get-OptimusEdgeWinget
            if(-not $manager -or $manager.path -cne $payload.winget.path -or $manager.sha256 -cne $payload.winget.sha256){throw 'The signed package manager changed before Edge removal.'}
            $scope=if($target.scope -ceq 'machine'){'machine'}else{'user'}
            $exit=Invoke-OptimusEdgeProcess $manager.path @('uninstall','--id','Microsoft.Edge','--exact','--scope',$scope,'--silent','--disable-interactivity','--accept-source-agreements')
            $hex='0x'+([BitConverter]::ToUInt32([BitConverter]::GetBytes([int]$exit),0)).ToString('X8')
            $null=$attempts.Add([pscustomobject]@{method='winget';exitCode=$exit;exitCodeHex=$hex})
            $restart=($exit -eq 3010)
            $canFallback=($exit -eq 0 -or $hex -ceq '0x8A150014')
        }
        if((Test-Path -LiteralPath $payload.browser -PathType Leaf) -and $canFallback -and $payload.setup) {
            $browser=Get-OptimusEdgeSignedFile $payload.browser
            $setup=Get-OptimusEdgeSignedFile $payload.setup.path
            if(-not $browser -or $browser.sha256 -cne $payload.browserFile.sha256 -or -not $setup -or $setup.sha256 -cne $payload.setup.sha256 -or
                $setup.path -cne $payload.setup.path -or -not $setup.path.StartsWith($payload.root+'\',[StringComparison]::OrdinalIgnoreCase)){throw 'The same-root signed Edge uninstaller changed before fallback.'}
            $arguments=@('--uninstall','--force-uninstall','--msedge','--channel=stable','--verbose-logging')
            if($target.scope -ceq 'machine'){$arguments+='--system-level'}
            $exit=Invoke-OptimusEdgeProcess $setup.path $arguments
            $null=$attempts.Add([pscustomobject]@{method='microsoft-setup';exitCode=$exit})
            if($exit -eq 93 -and $target.scope -ceq 'machine' -and (Test-OptimusEdgeRemovalAdmin)) {
                # 93: Windows refused browser uninstall for this region. Retry
                # once with the updater's AllowUninstall value, then restore it.
                $regionBlocked=$true
                $prior=Get-OptimusEdgeAllowUninstall
                try {
                    Set-OptimusEdgeAllowUninstall
                    $exit=Invoke-OptimusEdgeProcess $setup.path $arguments
                    $null=$attempts.Add([pscustomobject]@{method='microsoft-setup-allow-uninstall';exitCode=$exit})
                } finally { try{Set-OptimusEdgeAllowUninstall -Restore $prior}catch{} }
            }
            $restart=($restart -or $exit -eq 3010)
            $canFallback=($exit -eq 0)
        }
        # Windows' exact removable Store registration is an independent native
        # fallback; no wildcard or shared WebView package is ever passed to it.
        if($canFallback) {
            foreach($package in @($payload.packages)) {
                if($package.removable -ne $true){continue}
                try{$null=$attempts.Add((Remove-OptimusExactEdgePackage $package))}catch{$null=$attempts.Add([pscustomobject]@{method='appx';error=$_.Exception.Message;removed=$false})}
            }
        }
        Publish-OptimusEdgeRemovalProgress $ProgressCallback $target 'verify' $null
        $present=Test-Path -LiteralPath $payload.browser -PathType Leaf
        $registrations=@(Get-OptimusEdgePackages|Where-Object {$_.installLocation -and ($_.installLocation.TrimEnd('\') -ieq $payload.root -or $_.installLocation.StartsWith($payload.root+'\',[StringComparison]::OrdinalIgnoreCase))})
        $success=($attempts.Count -gt 0 -and -not $present)
        $folders=@()
        if($success -and -not $restart -and $target.scope -ceq 'machine'){$folders+=@(Invoke-OptimusEdgeLeftoverCleanup (Split-Path $payload.root -Parent))}
        if($success -and -not $restart){Publish-OptimusEdgeRemovalProgress $ProgressCallback $target 'uninstall' 100}
        $blocked=(-not $success -and $regionBlocked -and $exit -eq 93)
        return [pscustomobject]@{id=$target.id;name=$target.name;removed=$success;reason=$(if($success){'removal_verified'}elseif($restart){'restart_required'}elseif($blocked){'edge_uninstall_region_blocked'}else{'edge_still_detected'});status=$(if($success){'Edge browser executable absent after native removal'}elseif($restart){'Restart required; Edge removal is not yet confirmed'}elseif($blocked){'Windows blocks Edge browser uninstall in this region (setup exit 93), even with AllowUninstall'}else{'Edge remains installed; native removal did not establish absence'});remainingDetected=$present;registrationStubsRemaining=$registrations.Count;restartNeeded=$restart;requiresReview=(-not $success -or $restart);attempts=$attempts.ToArray();leftoverFolders=$folders;sharedComponentsPreserved=$true;note='Shared WebView2, the Edge updater, browser profiles and region settings are untouched.'}
    } catch {
        Publish-OptimusEdgeRemovalProgress $ProgressCallback $Target 'failed' $null
        return [pscustomobject]@{id=$Target.id;name=$Target.name;removed=$false;reason='edge_removal_refused';status='Edge removal requires review';error=$_.Exception.Message;requiresReview=$true;restartNeeded=$restart;attempts=$attempts.ToArray();sharedComponentsPreserved=$true}
    }
}
function Get-OptimusEdgeInstallPolicyContext {
    # https://learn.microsoft.com/deployedge/microsoft-edge-update-policies#install
    $domain=(Get-CimInstance Win32_ComputerSystem -ErrorAction Stop).PartOfDomain
    if($domain -ne $true){return [pscustomobject]@{supported=$false;reason='active_directory_domain_required';note='Microsoft documents the Edge Update installation policy for Active Directory domain-joined devices. No policy was written on this device.'}}
    $updater=$null
    foreach($known in @(Get-OptimusEdgeKnownRoots)) {
        if($known.scope -cne 'machine'){continue}
        $microsoft=Split-Path (Split-Path ([string]$known.root) -Parent) -Parent
        $candidate=Get-OptimusEdgeSignedFile (Join-Path $microsoft 'EdgeUpdate\MicrosoftEdgeUpdate.exe')
        if($candidate -and $candidate.version -cmatch '^\d+(\.\d+){2,3}$' -and [version]$candidate.version -ge [version]'1.2.145.5'){$updater=$candidate;break}
    }
    if(-not $updater){return [pscustomobject]@{supported=$false;reason='supported_edge_updater_not_verified';note='The signed Microsoft updater and a policy-capable version could not be verified. No policy was written.'}}
    return [pscustomobject]@{supported=$true;reason='documented_edge_update_scope';updater=$updater;note='This product-specific policy blocks installation through the supported Edge updater. It is not a universal Windows servicing or manual-installer prohibition.'}
}
function Set-OptimusEdgeInstallBlock {
    param([Parameter(Mandatory=$true)][string]$BackupDirectory)
    $context=Get-OptimusEdgeInstallPolicyContext
    if($context.supported -ne $true){return [pscustomobject]@{applied=$false;verified=$false;supported=$false;reason=$context.reason;note=$context.note}}
    if(-not(Test-OptimusEdgeRemovalAdmin)){throw 'Administrator access is required for the Edge installation policy.'}
    if(-not(Test-OptimusEdgeOrdinaryPath $BackupDirectory) -or -not(Test-Path -LiteralPath $BackupDirectory -PathType Container)){throw 'An existing ordinary recovery directory is required.'}
    $path='HKLM:\SOFTWARE\Policies\Microsoft\EdgeUpdate'
    $name='Install{56EB18F8-B008-4CBD-B6D2-8C97FE7E9062}'
    $key=Get-Item -LiteralPath $path -ErrorAction SilentlyContinue
    $present=($null -ne $key -and @($key.GetValueNames()) -ccontains $name)
    $before=[ordered]@{schema=1;path=$path;name=$name;present=$present;value=$(if($present){$key.GetValue($name,$null,[Microsoft.Win32.RegistryValueOptions]::DoNotExpandEnvironmentNames)}else{$null});kind=$(if($present){[string]$key.GetValueKind($name)}else{$null});desired=0}
    if($present -and $before.kind -ceq 'DWord' -and $before.value -eq 0){return [pscustomobject]@{applied=$true;verified=$true;supported=$true;changed=$false;reason='edge_update_install_disabled';note=$context.note}}
    $backup=Join-Path $BackupDirectory ('edge-install-policy-'+[guid]::NewGuid().ToString('N')+'.json')
    $bytes=(New-Object Text.UTF8Encoding($false)).GetBytes((ConvertTo-Json -InputObject $before -Depth 5))
    $stream=[IO.File]::Open($backup,[IO.FileMode]::CreateNew,[IO.FileAccess]::Write,[IO.FileShare]::None)
    try{$stream.Write($bytes,0,$bytes.Length)}finally{$stream.Dispose()}
    $null=New-Item -Path $path -Force -ErrorAction Stop
    $null=New-ItemProperty -LiteralPath $path -Name $name -Value 0 -PropertyType DWord -Force -ErrorAction Stop
    $after=Get-Item -LiteralPath $path -ErrorAction Stop
    if($after.GetValueKind($name) -ne [Microsoft.Win32.RegistryValueKind]::DWord -or $after.GetValue($name) -ne 0){throw 'Edge installation policy readback did not match.'}
    return [pscustomobject]@{applied=$true;verified=$true;supported=$true;changed=$true;reason='edge_update_install_disabled';backup=$backup;note=$context.note}
}
