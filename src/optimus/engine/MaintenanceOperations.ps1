#requires -Version 5.1
# Independent optional-app maintenance. Dot-sourcing defines functions only.
# Never edits region policy, takes ownership, removes WebView2, or deletes app folders.
function Test-OptimusMaintenanceAdmin {
    $id=[Security.Principal.WindowsIdentity]::GetCurrent()
    return (New-Object Security.Principal.WindowsPrincipal($id)).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
}
function Get-OptimusMaintenanceHash {
    param([string]$Text)
    $hash=[Security.Cryptography.SHA256]::Create()
    try { return ([BitConverter]::ToString($hash.ComputeHash([Text.Encoding]::UTF8.GetBytes($Text)))).Replace('-','').ToLowerInvariant() }
    finally { $hash.Dispose() }
}
function Test-OptimusOptionalPackage {
    param([string]$Name,[string]$Action)
    if ($Name -match '[*?\\/]') { return $false }
    if ($Action -eq 'm365') { return ($Name -in @('Microsoft.MicrosoftOfficeHub','Microsoft.M365Companions') -or $Name -like 'Microsoft.Microsoft365*') }
    if ($Action -eq 'outlook') { return $Name -eq 'Microsoft.OutlookForWindows' }
    if ($Action -ne 'consumer-apps') { return $false }
    return $Name -in @(
        'Microsoft.BingNews','Microsoft.BingWeather','Microsoft.BingWallpaper','Microsoft.Getstarted',
        'Microsoft.MicrosoftSolitaireCollection','Microsoft.Microsoft3DViewer','Microsoft.Print3D',
        'Microsoft.MixedReality.Portal','Microsoft.People','Microsoft.WindowsMaps',
        'Microsoft.WindowsFeedbackHub','Microsoft.549981C3F5F10','Microsoft.SkypeApp',
        'Microsoft.Windows.DevHome','Microsoft.MicrosoftPCManager','MicrosoftCorporationII.Windows365',
        'MicrosoftCorporationII.MicrosoftFamily','Clipchamp.Clipchamp','MicrosoftTeams','MSTeams','Microsoft.Copilot'
    )
}
function Get-OptimusEdgeTarget {
    $browserLocations=@()
    $setups=@()
    foreach ($base in @(${env:ProgramFiles(x86)},$env:ProgramFiles,$env:LOCALAPPDATA) | Where-Object { $_ } | Select-Object -Unique) {
        $root=Join-Path $base 'Microsoft\Edge\Application'
        $browser=Join-Path $root 'msedge.exe'
        if (Test-Path -LiteralPath $browser -PathType Leaf) { $browserLocations+=$browser }
        if (-not (Test-Path -LiteralPath $root -PathType Container)) { continue }
        foreach ($version in @(Get-ChildItem -LiteralPath $root -Directory -ErrorAction SilentlyContinue | Where-Object { $_.Name -match '^\d+(\.\d+){2,3}$' })) {
            $setup=Join-Path $version.FullName 'Installer\setup.exe'
            if (-not (Test-Path -LiteralPath $setup -PathType Leaf)) { continue }
            $file=Get-Item -LiteralPath $setup -Force
            if ($file.Attributes -band [IO.FileAttributes]::ReparsePoint) { continue }
            $signature=Get-AuthenticodeSignature -LiteralPath $setup -ErrorAction SilentlyContinue
            if ($signature.Status -ne 'Valid' -or $signature.SignerCertificate.Subject -notmatch '(?i)CN=Microsoft Corporation(?:,|$)') { continue }
            $setups+=[pscustomobject]@{path=$setup;version=$version.Name;systemLevel=(-not $root.StartsWith(($env:LOCALAPPDATA.TrimEnd('\')+'\'),[StringComparison]::OrdinalIgnoreCase));sha256=(Get-FileHash -LiteralPath $setup -Algorithm SHA256).Hash}
        }
    }
    $appx=@(Get-AppxPackage -Name 'Microsoft.MicrosoftEdge.Stable' -ErrorAction SilentlyContinue)
    if (-not $browserLocations.Count -and -not $appx.Count) { return @() }
    $winget=Get-Command winget.exe -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1
    $chosen=@($setups | Sort-Object { [version]$_.version } -Descending | Select-Object -First 1)
    $method=if ($winget) { 'winget' } elseif ($chosen.Count) { 'microsoft-setup' } else { 'windows-dialog' }
    $payload=if ($method -eq 'winget') { [pscustomobject]@{executable=$winget.Source;packageId='Microsoft.Edge'} } elseif ($method -eq 'microsoft-setup') { $chosen[0] } else { [pscustomobject]@{} }
    $fingerprint=Get-OptimusMaintenanceHash (ConvertTo-Json -InputObject ([ordered]@{method=$method;payload=$payload;browsers=@($browserLocations|Sort-Object);packages=@($appx.PackageFullName|Sort-Object)}) -Depth 6 -Compress)
    return [pscustomobject]@{id='edge-browser';name='Microsoft Edge browser';scope=if($chosen.Count -and -not $chosen[0].systemLevel){'current-user'}else{'machine'};kind='edge';fingerprint=$fingerprint;method=$method;payload=$payload;selected=$false;admin=($method -eq 'microsoft-setup' -and $chosen[0].systemLevel);recovery='Reinstall Edge from Microsoft. Browser profile data is not separately backed up by Optimus.'}
}
function Get-OptimusMaintenanceTargets {
    param([Parameter(Mandatory=$true)][ValidateSet('consumer-apps','m365','outlook','edge','office-desktop','desktop-edge-script')][string]$Action,[bool]$IncludeProvisioned=$false)
    if ($Action -eq 'edge') { return @(Get-OptimusEdgeTarget) }
    if ($Action -eq 'desktop-edge-script') { return @(Get-OptimusDesktopEdgeIntegration) }
    if ($Action -eq 'office-desktop') {
        return @(Get-SCMInstalledApps | Where-Object { $_.Type -eq 'Desktop' -and $_.Publisher -match '(?i)Microsoft' -and $_.Name -match '(?i)^(Microsoft 365|Microsoft Office)' -and $_.Name -notmatch '(?i)update|proof|language|extensibility|runtime|access database' } | ForEach-Object {
            $fingerprint=Get-OptimusMaintenanceHash ($_.Identity+'|'+$_.Name+'|'+$_.Version)
            [pscustomobject]@{id=('office-'+$fingerprint.Substring(0,16));name=$_.Name;version=$_.Version;scope='desktop';kind='office-dialog';fingerprint=$fingerprint;selected=$false;admin=$false;recovery='Reinstall Office from its original Microsoft account or organization deployment. This action opens Windows uninstall controls.'}
        })
    }
    $targets=@()
    foreach ($package in @(Get-AppxPackage -ErrorAction Stop)) {
        if (-not (Test-OptimusOptionalPackage -Name $package.Name -Action $Action) -or $package.NonRemovable -or $package.IsFramework -or $package.IsResourcePackage) { continue }
        $fingerprint=Get-OptimusMaintenanceHash ($package.Name+'|'+$package.PackageFullName+'|current-user')
        $targets += [pscustomobject]@{id=('appx-'+$fingerprint.Substring(0,20));name=$package.Name;version=[string]$package.Version;scope='current-user';kind='appx';packageFullName=$package.PackageFullName;fingerprint=$fingerprint;selected=$false;admin=$false;recovery='Reinstall this app from Microsoft Store. Removed app-local data is not restored by Optimus.'}
    }
    if ($IncludeProvisioned) {
        if (-not (Test-OptimusMaintenanceAdmin)) { throw 'Administrator rights are required to review and remove provisioning for future new accounts.' }
        foreach ($package in @(Get-AppxProvisionedPackage -Online -ErrorAction Stop)) {
            if (-not (Test-OptimusOptionalPackage -Name $package.DisplayName -Action $Action)) { continue }
            $fingerprint=Get-OptimusMaintenanceHash ($package.DisplayName+'|'+$package.PackageName+'|future-users')
            $targets += [pscustomobject]@{id=('provisioned-'+$fingerprint.Substring(0,20));name=$package.DisplayName;version=[string]$package.Version;scope='future-users';kind='provisioned';packageFullName=$package.PackageName;fingerprint=$fingerprint;selected=$false;admin=$true;recovery='Requires the original signed package, dependencies and license to re-provision; a normal Store reinstall affects the current user only.'}
        }
    }
    return @($targets | Sort-Object name,scope)
}
function Get-OptimusDesktopEdgeIntegration {
    $desktop=[Environment]::GetFolderPath('Desktop')
    if (-not $desktop) { return @() }
    $directory=Join-Path $desktop 'Specialized Computer Management'
    $path=Join-Path $directory 'Remove-Edge.ps1'
    if (-not (Test-Path -LiteralPath $path -PathType Leaf)) { return @() }
    $file=Get-Item -LiteralPath $path -Force -ErrorAction Stop
    $folder=Get-Item -LiteralPath $directory -Force -ErrorAction Stop
    if (($file.Attributes -band [IO.FileAttributes]::ReparsePoint) -or ($folder.Attributes -band [IO.FileAttributes]::ReparsePoint)) { return @() }
    $hash=(Get-FileHash -LiteralPath $path -Algorithm SHA256 -ErrorAction Stop).Hash
    return [pscustomobject]@{id='desktop-edge-script';name='Existing Desktop Edge removal script';scope='external-script';kind='desktop-edge-script';path=$file.FullName;sha256=$hash;fingerprint=(Get-OptimusMaintenanceHash ($file.FullName+'|'+$hash));selected=$false;admin=$false;requiresElevation=$true;recovery='The external script backs up its Windows region-policy JSON and ACL, but has no complete undo for policies, uninstalls, removed folders or app-local data.';effects='External administrator script: changes Windows region and Edge update policies, stops Edge, calls its forced uninstaller and deletes residual Edge browser folders. WebView2/bloatware options are not passed. Its own typed APPLY confirmation remains required.'}
}
function Get-OptimusDesktopEdgePreview {
    param([Parameter(Mandatory=$true)]$Target)
    $exe=Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe'
    $before=(Get-FileHash -LiteralPath $Target.path -Algorithm SHA256 -ErrorAction Stop).Hash
    if ($before -cne $Target.sha256) { throw 'The external Edge script changed before preview. Preview again.' }
    $savedPreference=$ErrorActionPreference
    try {
        $ErrorActionPreference='Continue'
        $output=@(& $exe -NoLogo -NoProfile -NonInteractive -File ([string]$Target.path) -Preview 2>&1)
        $exitCode=$LASTEXITCODE
    } finally { $ErrorActionPreference=$savedPreference }
    $after=(Get-FileHash -LiteralPath $Target.path -Algorithm SHA256 -ErrorAction Stop).Hash
    if ($before -cne $after) { throw 'The external Edge script changed while previewing. No apply plan was created.' }
    return [pscustomobject]@{scriptPath=$Target.path;sha256=$before;exitCode=$exitCode;text=(($output | Out-String).Trim());effects=$Target.effects}
}
function Start-OptimusDesktopEdgeScript {
    param([Parameter(Mandatory=$true)]$Target)
    if ((Get-FileHash -LiteralPath $Target.path -Algorithm SHA256 -ErrorAction Stop).Hash -cne $Target.sha256) { throw 'The reviewed external Edge script changed. Preview again.' }
    $options=@{FilePath=(Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe');ArgumentList=('-NoLogo -NoProfile -File "'+$Target.path+'"');WorkingDirectory=[IO.Path]::GetDirectoryName($Target.path);PassThru=$true;ErrorAction='Stop'}
    if (-not (Test-OptimusMaintenanceAdmin)) { $options.Verb='RunAs' }
    # A visible console is intentional: the original script requires typed APPLY.
    $process=Start-Process @options
    return [pscustomobject]@{id=$Target.id;name=$Target.name;status='Opened original script in a visible administrator console; its APPLY gate controls execution';removed=$false;processId=$process.Id;restartNeeded=$false;requiresUserInteraction=$true;sha256=$Target.sha256}
}

function Invoke-OptimusEdgeUninstall {
    param([Parameter(Mandatory=$true)]$Target)
    if ($Target.method -eq 'windows-dialog') {
        Start-Process 'ms-settings:appsfeatures' -ErrorAction Stop
        return [pscustomobject]@{id=$Target.id;name=$Target.name;status='Windows uninstall settings opened';removed=$false;restartNeeded=$false}
    }
    if ($Target.method -eq 'winget') {
        $exe=[string]$Target.payload.executable
        $arguments=@('uninstall','--id','Microsoft.Edge','--exact','--disable-interactivity')
    } elseif ($Target.method -eq 'microsoft-setup') {
        if ($Target.payload.systemLevel -and -not (Test-OptimusMaintenanceAdmin)) { throw 'Administrator rights are required for this machine Edge installation.' }
        $exe=[string]$Target.payload.path
        $arguments=@('--uninstall','--force-uninstall','--verbose-logging')
        if ($Target.payload.systemLevel) { $arguments+='--system-level' }
    } else { throw 'Unsupported Edge uninstall method.' }
    $process=Start-Process -FilePath $exe -ArgumentList $arguments -WindowStyle Hidden -Wait -PassThru -ErrorAction Stop
    $remaining=@(Get-OptimusEdgeTarget)
    return [pscustomobject]@{id=$Target.id;name=$Target.name;status=if($process.ExitCode -in @(0,3010) -and -not $remaining.Count){'Removed'}elseif($process.ExitCode -eq 3010){'Restart required; verify after reboot'}else{'Removal was not verified; Windows may restrict Edge removal'};removed=($process.ExitCode -in @(0,3010) -and -not $remaining.Count);exitCode=$process.ExitCode;restartNeeded=($process.ExitCode -eq 3010);note='Windows controls uninstall eligibility. Optimus does not edit region policies, remove shared runtimes, or force-delete browser folders.'}
}
function Invoke-OptimusMaintenanceRequest {
    param([Parameter(Mandatory=$true)][string]$Operation,$Arguments)
    if ($Operation -notin @('maintenance_preview','maintenance_apply')) { throw 'Unknown maintenance operation.' }
    $action=[string]$Arguments.action
    if ($action -notin @('consumer-apps','m365','outlook','edge','office-desktop','desktop-edge-script')) { throw 'Unknown optional-app action.' }
    $includeProvisioned=($Arguments.includeProvisioned -eq $true)
    if ($includeProvisioned -and $action -in @('edge','office-desktop','desktop-edge-script')) { throw 'Provisioned-package removal is only available for optional Store apps.' }
    $fresh=@(Get-OptimusMaintenanceTargets -Action $action -IncludeProvisioned $includeProvisioned)
    if ($Operation -eq 'maintenance_preview') {
        if ($action -eq 'desktop-edge-script') {
            $external=if($fresh.Count){Get-OptimusDesktopEdgePreview -Target $fresh[0]}else{$null}
            return [pscustomobject]@{action=$action;targets=$fresh;summary='Review the original external script and its effects. Applying opens its own visible typed confirmation; removal has no complete automatic undo.';externalPreview=$external;restartNeeded=$true;commands=@('Run original Remove-Edge.ps1 -Preview; apply launches it visibly without optional flags.');recovery=if($fresh.Count){$fresh[0].recovery}else{'External script not installed in the known Desktop location.'}}
        }
        return [pscustomobject]@{action=$action;targets=$fresh;summary=('{0} matching targets; select only the apps and scopes you want to remove.' -f $fresh.Count);restartNeeded=$false;commands=@('Only selected, revalidated targets are processed.');recovery='Uninstallation is not a data backup. Reinstall through Microsoft Store, Microsoft Edge or the original Office deployment. App-local data may be removed.'}
    }
    if ([string]$Arguments.confirmation -cne 'REMOVE') { throw 'An explicit user removal confirmation is required.' }
    $requested=@($Arguments.targets)
    if (-not $requested.Count -or $requested.Count -gt 100) { throw 'Select between 1 and 100 reviewed targets.' }
    $selected=@()
    $seen=@{}
    foreach ($expected in $requested) {
        $id=[string]$expected.id
        if ($seen.ContainsKey($id)) { throw 'A target was selected more than once.' }
        $seen[$id]=$true
        $current=@($fresh | Where-Object { $_.id -ceq $id -and $_.fingerprint -ceq [string]$expected.fingerprint })
        if ($current.Count -ne 1) { throw 'An installed package, publisher, version or uninstall method changed. Preview again; nothing was removed.' }
        if ($current[0].admin -and -not (Test-OptimusMaintenanceAdmin)) { throw 'Administrator rights are required for the selected target.' }
        $selected+=$current[0]
    }
    # Revalidation for every selected target completes before the first mutation.
    $results=@()
    foreach ($target in $selected) {
        try {
            switch ($target.kind) {
                'appx' {
                    Remove-AppxPackage -Package ([string]$target.packageFullName) -ErrorAction Stop
                    if (@(Get-AppxPackage -Name $target.name | Where-Object { $_.PackageFullName -ceq $target.packageFullName }).Count) { throw 'The app remains registered for this account.' }
                    $results+=[pscustomobject]@{id=$target.id;name=$target.name;scope=$target.scope;status='Removed';removed=$true;restartNeeded=$false}
                }
                'provisioned' {
                    Remove-AppxProvisionedPackage -Online -PackageName ([string]$target.packageFullName) -ErrorAction Stop | Out-Null
                    if (@(Get-AppxProvisionedPackage -Online | Where-Object { $_.PackageName -ceq $target.packageFullName }).Count) { throw 'The package remains provisioned.' }
                    $results+=[pscustomobject]@{id=$target.id;name=$target.name;scope=$target.scope;status='Provisioning removed for future new accounts';removed=$true;restartNeeded=$false}
                }
                'edge' { $results+=Invoke-OptimusEdgeUninstall -Target $target }
                'desktop-edge-script' { $results+=Start-OptimusDesktopEdgeScript -Target $target }
                'office-dialog' {
                    Start-Process -FilePath (Join-Path $env:SystemRoot 'System32\control.exe') -ArgumentList 'appwiz.cpl' -ErrorAction Stop
                    $results+=[pscustomobject]@{id=$target.id;name=$target.name;status='Windows Programs and Features opened; select this Office product to uninstall';removed=$false;restartNeeded=$false}
                }
                default { throw 'Unsupported target kind.' }
            }
        } catch { $results+=[pscustomobject]@{id=$target.id;name=$target.name;status='Failed or partially removed';error=$_.Exception.Message;removed=$false;restartNeeded=$false} }
    }
    return [pscustomobject]@{action=$action;results=$results;restartNeeded=(@($results | Where-Object restartNeeded).Count -gt 0);note='Only selected scopes were processed. Store reinstalls do not restore deleted app-local data.'}
}
