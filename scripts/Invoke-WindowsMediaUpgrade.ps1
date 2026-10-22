#requires -Version 5.1
<#
.SYNOPSIS
Scan selected Windows installation media; optionally start a reviewed media upgrade.
.EXAMPLE
.\Invoke-WindowsMediaUpgrade.ps1 -SetupPath 'X:\setup.exe' -AcceptLicense -OutputDirectory 'D:\WindowsUpgradeLogs'
.EXAMPLE
.\Invoke-WindowsMediaUpgrade.ps1 -SetupPath 'X:\setup.exe' -Mode Upgrade -AcceptLicense -OutputDirectory 'D:\WindowsUpgradeLogs'
.NOTES
Run in an existing administrator session. The MoSetup policy must already be
prepared through Optimus. This script never downloads or modifies media or policy.
Selected-media trust remains the caller's responsibility; signing setup.exe does
not authenticate every file in the selected image. Setup retains its own checks.
/NoReboot suppresses the first reboot only. Review pending Setup before a later
restart; subsequent upgrade phases can restart the computer.
Primary reference:
https://learn.microsoft.com/windows-hardware/manufacture/desktop/windows-setup-command-line-options
#>
[CmdletBinding()]
param(
    [ValidateSet('Scan','Upgrade')][string]$Mode='Scan',
    [string]$SetupPath,
    [switch]$AcceptLicense,
    [string]$OutputDirectory
)

function Get-OptimusOrdinaryMediaPath {
    param([string]$Path,[switch]$Directory)
    if ($Path -notmatch '^[A-Za-z]:\\' -or $Path -match '[\x00-\x1f"*?]' -or $Path.Contains('..')) {
        throw 'Supply an ordinary absolute local drive path without wildcards or parent traversal.'
    }
    $item=Get-Item -LiteralPath $Path -Force -ErrorAction Stop
    if ($item.PSIsContainer -ne [bool]$Directory) { throw 'The selected path has the wrong file/directory type.' }
    $part=$item
    while ($null -ne $part) {
        if ($part.Attributes -band [IO.FileAttributes]::ReparsePoint) { throw 'Reparse points are excluded from media and report paths.' }
        $part=if ($part.PSIsContainer) {$part.Parent} else {$part.Directory}
    }
    return $item
}

function Get-OptimusMediaSignature {
    param([string]$Path)
    $signed=Get-AuthenticodeSignature -LiteralPath $Path -ErrorAction Stop
    $version=[Diagnostics.FileVersionInfo]::GetVersionInfo($Path)
    $subject=if ($signed.SignerCertificate) {$signed.SignerCertificate.Subject} else {''}
    if ($signed.Status -ne 'Valid' -or $subject -notmatch '(^|,\s*)O=Microsoft Corporation(,|$)' -or
        $subject -notmatch '(^|,\s*)CN=Microsoft (Windows|Corporation|Windows Publisher)(,|$)' -or
        $version.CompanyName -ne 'Microsoft Corporation' -or $version.OriginalFilename -ine 'setup.exe') {
        throw 'The selected setup.exe does not have a valid Microsoft Windows Authenticode identity.'
    }
    return @{sha256=(Get-FileHash -LiteralPath $Path -Algorithm SHA256 -ErrorAction Stop).Hash;
        signer=$subject;thumbprint=$signed.SignerCertificate.Thumbprint}
}

function Get-OptimusMediaHost {
    $identity=[Security.Principal.WindowsIdentity]::GetCurrent()
    $admin=([Security.Principal.WindowsPrincipal]::new($identity)).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
    if (-not $admin) { throw 'Use an existing administrator session; no elevation was requested.' }
    $registry=[Microsoft.Win32.RegistryKey]::OpenBaseKey([Microsoft.Win32.RegistryHive]::LocalMachine,[Microsoft.Win32.RegistryView]::Registry64)
    try {
        $current=$registry.OpenSubKey('SOFTWARE\Microsoft\Windows NT\CurrentVersion',$false)
        $language=$registry.OpenSubKey('SYSTEM\CurrentControlSet\Control\Nls\Language',$false)
        $policy=$registry.OpenSubKey('SYSTEM\Setup\MoSetup',$false)
        $session=$registry.OpenSubKey('SYSTEM\CurrentControlSet\Control\Session Manager',$false)
        try {
            if (-not $current -or -not $language -or -not $session) { throw 'Current Windows metadata is unavailable.' }
            $policyType='Missing';$policyValue=$null
            if ($policy -and $policy.GetValueNames() -contains 'AllowUpgradesWithUnsupportedTPMOrCPU') {
                $policyType=[string]$policy.GetValueKind('AllowUpgradesWithUnsupportedTPMOrCPU')
                $policyValue=$policy.GetValue('AllowUpgradesWithUnsupportedTPMOrCPU')
            }
            $pending=[bool]$session.GetValue('PendingFileRenameOperations')
            foreach ($key in @('SOFTWARE\Microsoft\Windows\CurrentVersion\Component Based Servicing\RebootPending',
                              'SOFTWARE\Microsoft\Windows\CurrentVersion\WindowsUpdate\Auto Update\RebootRequired')) {
                $entry=$registry.OpenSubKey($key,$false)
                if ($entry) {$pending=$true;$entry.Dispose()}
            }
            $architectures=@(Get-CimInstance -ClassName Win32_Processor -ErrorAction Stop | Select-Object -ExpandProperty Architecture -Unique)
            if ($architectures.Count -ne 1 -or $architectures[0] -notin @(0,9,12)) { throw 'The native CPU architecture cannot be verified.' }
            $processes=@(Get-CimInstance -ClassName Win32_Process -ErrorAction Stop)
            if (-not $processes.Count -or -not @($processes | Where-Object {$_.ProcessId -eq $PID}).Count) {throw 'The current process is absent from the process inventory; installer state is unverifiable.'}
            $conflicts=@($processes | Where-Object {$_.Name -match '^(setup|setuphost|setupprep|msiexec|winget|AppInstaller|AppInstallerCLI|Windows10UpgraderApp|Windows11UpgraderApp|Windows11InstallationAssistant|UpdateAssistant|WindowsUpdateBox|TiWorker|TrustedInstaller|dism|pkgmgr)\.exe$'} | Select-Object Name,ProcessId)
            $disk=@(Get-CimInstance -ClassName Win32_LogicalDisk -Filter "DeviceID='C:'" -ErrorAction Stop)
            if ($disk.Count -ne 1 -or $null -eq $disk[0].FreeSpace) { throw 'C: free-space evidence is unavailable.' }
            return @{admin=$admin;edition=[string]$current.GetValue('EditionID');build=[int]$current.GetValue('CurrentBuildNumber');
                installation_type=[string]$current.GetValue('InstallationType');architecture=[int]$architectures[0];
                language=[Globalization.CultureInfo]::GetCultureInfo([Convert]::ToInt32([string]$language.GetValue('InstallLanguage'),16)).Name;
                system_drive=$env:SystemDrive;free_bytes=[long]$disk[0].FreeSpace;pending_restart=$pending;
                policy_type=$policyType;policy_value=$policyValue;process_inventory_complete=$true;conflicts=$conflicts}
        } finally {foreach ($key in @($current,$language,$policy,$session)) {if ($key) {$key.Dispose()}}}
    } finally {$registry.Dispose()}
}

function Get-OptimusMediaImages {
    param([string]$Path)
    if (-not (Get-Command Get-WindowsImage -ErrorAction SilentlyContinue)) { throw 'Get-WindowsImage is unavailable; image metadata must be reviewed through supported Windows tools.' }
    $records=@(Get-WindowsImage -ImagePath $Path -ErrorAction Stop)
    if (-not $records.Count -or $records.Count -gt 100) { throw 'The media image list is missing or ambiguous.' }
    foreach ($record in $records) {
        Get-WindowsImage -ImagePath $Path -Index $record.ImageIndex -ErrorAction Stop
    }
}

function Get-OptimusMediaPreflight {
    param([string]$Path)
    $file=Get-OptimusOrdinaryMediaPath $Path
    if ($file.Name -ine 'setup.exe') { throw 'Select setup.exe at the root of existing Windows installation media.' }
    $hostState=Get-OptimusMediaHost
    if (-not $hostState.admin -or $hostState.installation_type -ne 'Client' -or $hostState.build -lt 10240 -or $hostState.system_drive -ine 'C:') { throw 'This tool requires an administrator on a supported current Windows client installed on C:.' }
    if ($hostState.policy_type -ne 'DWord' -or $hostState.policy_value -ne 1) { throw 'The existing MoSetup compatibility policy is not DWORD 1. Prepare it explicitly through Optimus first.' }
    if ($hostState.free_bytes -lt 25GB) { throw 'At least 25 GiB free on C: is required before scanning; Windows Setup can require more.' }
    if ($hostState.process_inventory_complete -ne $true -or @($hostState.conflicts).Count -ne 0 -or $hostState.pending_restart) { throw 'An installer, servicing operation, pending restart or unknown process inventory requires review first.' }
    $signed=Get-OptimusMediaSignature $file.FullName
    $images=@()
    foreach ($name in @('install.wim','install.esd')) {
        $candidate=Join-Path $file.DirectoryName ('sources\'+$name)
        if (Test-Path -LiteralPath $candidate -ErrorAction Stop) {$images+=Get-OptimusOrdinaryMediaPath $candidate}
    }
    if ($images.Count -ne 1) { throw 'Select ordinary media with exactly one sources\install.wim or install.esd.' }
    $matches=@(Get-OptimusMediaImages $images[0].FullName | Where-Object {
        $_.EditionId -ceq $hostState.edition -and [string]$_.InstallationType -eq 'Client' -and
        [int]$_.Architecture -eq $hostState.architecture -and
        ([version]$_.Version).Major -eq 10 -and ([version]$_.Version).Build -ge 22000 -and
        ([version]$_.Version).Build -gt $hostState.build -and
        [string]$_.DefaultLanguage -ieq $hostState.language
    })
    if ($matches.Count -ne 1 -or [int]$matches[0].ImageIndex -lt 1) { throw 'A unique newer Windows image with the exact current edition, architecture and default UI language could not be verified.' }
    return @{setup_path=$file.FullName;setup_sha256=$signed.sha256;signer=$signed.signer;host=$hostState;
        image_path=$images[0].FullName;image_size=$images[0].Length;image_write_time=$images[0].LastWriteTimeUtc.Ticks;
        image_index=[int]$matches[0].ImageIndex;target_version=[string]$matches[0].Version;
        selected_media_trust='Selected-media trust is the caller responsibility; the setup signature does not authenticate the complete image.'}
}

function New-OptimusMediaReportDirectory {
    param([string]$Parent)
    $base=Get-OptimusOrdinaryMediaPath $Parent -Directory
    $directory=Join-Path $base.FullName ([guid]::NewGuid().ToString('N'))
    $acl=[Security.AccessControl.DirectorySecurity]::new()
    $acl.SetAccessRuleProtection($true,$false)
    foreach ($sid in @([Security.Principal.WindowsIdentity]::GetCurrent().User,
        [Security.Principal.SecurityIdentifier]::new('S-1-5-18'),[Security.Principal.SecurityIdentifier]::new('S-1-5-32-544'))) {
        $acl.AddAccessRule([Security.AccessControl.FileSystemAccessRule]::new($sid,'FullControl','ContainerInherit,ObjectInherit','None','Allow'))
    }
    # The explicit existing parent may be on the user's relocated data drive.
    [IO.Directory]::CreateDirectory($directory,$acl) | Out-Null
    return $directory
}

function Write-OptimusMediaReport {
    param($Report,[string]$Path)
    $temporary=$Path+'.'+[guid]::NewGuid().ToString('N')+'.tmp'
    [IO.File]::WriteAllText($temporary,($Report | ConvertTo-Json -Depth 14),[Text.UTF8Encoding]::new($false))
    if ([IO.File]::Exists($Path)) {
        # PowerShell 5.1 converts a null backup string to an invalid empty path.
        [IO.File]::Replace($temporary,$Path,($Path+'.previous.'+[guid]::NewGuid().ToString('N')))
    } else {[IO.File]::Move($temporary,$Path)}
}

function ConvertTo-OptimusMediaArgument {
    param([string]$Value)
    if ([string]::IsNullOrEmpty($Value)) { return '""' }
    if ($Value -notmatch '[\s"]') { return $Value }
    return '"'+[regex]::Replace([regex]::Replace($Value,'(\\*)"','$1$1\"'),'(\\+)$','$1$1')+'"'
}

function Start-OptimusMediaSetup {
    param([string]$Path,[string[]]$Arguments,[switch]$ScanOnly)
    $line=(@($Arguments | ForEach-Object {ConvertTo-OptimusMediaArgument $_}) -join ' ')
    if ($ScanOnly) {return Start-Process -FilePath $Path -ArgumentList $line -PassThru -WindowStyle Hidden -ErrorAction Stop}
    return Start-Process -FilePath $Path -ArgumentList $line -PassThru -WindowStyle Normal -ErrorAction Stop
}

function New-OptimusMediaGuard {
    $guard=[Threading.Mutex]::new($false,'Global\Optimus.WindowsMediaUpgrade')
    try {
        if (-not $guard.WaitOne(0)) {throw 'Another media-upgrade tool invocation is active.'}
        return $guard
    } catch {$guard.Dispose();throw}
}

function Get-OptimusMediaScanOutcome {
    param([long]$ExitCode)
    $hex='0x{0:X8}' -f ($ExitCode -band 4294967295L)
    $reasons=@{'0xC1900210'='Compatibility scan found no concerns.';
        '0xC1900208'='Actionable application or compatibility concerns require review.';
        '0xC1900204'='The selected migration choice cannot preserve this installation.';
        '0xC1900200'='Windows Setup reports an unmet system requirement.';
        '0xC190020E'='Windows Setup reports insufficient installation space.'}
    return @{exit_code=$ExitCode;hex=$hex;can_upgrade=($hex -eq '0xC1900210');
        status=if ($hex -eq '0xC1900210') {'scan_ready'} elseif ($reasons.ContainsKey($hex)) {'blocked'} else {'uncertain'};
        reason=if ($reasons.ContainsKey($hex)) {$reasons[$hex]} else {'The scan exit code is not a documented ScanOnly success/block result. Review Setup logs; no upgrade or retry was started.'}}
}

function Invoke-OptimusWindowsMediaUpgrade {
    param([string]$SetupPath,[ValidateSet('Scan','Upgrade')][string]$Mode='Scan',[switch]$AcceptLicense,[string]$OutputDirectory)
    $ErrorActionPreference='Stop'
    $report=@{schema=1;id=[guid]::NewGuid().ToString('N');mode=$Mode;status='deferred';started_at=[DateTime]::UtcNow.ToString('o');
        upgrade_started=$false;no_reboot_requested=$true;reboot_limit='NoReboot suppresses only the first down-level reboot. Review pending Setup before a later restart; later upgrade phases can reboot.';
        documentation='https://learn.microsoft.com/windows-hardware/manufacture/desktop/windows-setup-command-line-options'}
    $reportPath=$null;$attempted=$false;$guard=$null
    try {
        if (-not $AcceptLicense) {throw 'Explicit -AcceptLicense is required for Scan and Upgrade before the Windows EULA can be accepted.'}
        if ([string]::IsNullOrWhiteSpace($SetupPath) -or [string]::IsNullOrWhiteSpace($OutputDirectory)) {throw 'Supply explicit -SetupPath and an existing absolute -OutputDirectory on your chosen data drive.'}
        $guard=New-OptimusMediaGuard
        $context=Get-OptimusMediaPreflight $SetupPath
        $report.preflight=$context
        $directory=New-OptimusMediaReportDirectory $OutputDirectory
        $reportPath=Join-Path $directory 'report.json';$report.log_path=$reportPath
        $common=@('/Auto','Upgrade','/EULA','accept','/NoReboot','/ImageIndex',[string]$context.image_index)
        $scanArgs=$common+@('/Quiet','/Compat','ScanOnly','/Compat','IgnoreWarning','/CopyLogs',(Join-Path $directory 'scan-logs.zip'))
        $report.scan_arguments=$scanArgs;$report.status='scan_prepared'
        Write-OptimusMediaReport $report $reportPath
        $attempted=$true
        $scan=Start-OptimusMediaSetup $context.setup_path $scanArgs -ScanOnly
        $report.scan_pid=$scan.Id
        if (-not $scan.WaitForExit(1800000)) {
            $report.status='pending';$report.reason='The compatibility scan exceeded 30 minutes. It was not killed. Review the recorded PID and Setup logs; no upgrade or automatic retry was started.'
        } else {
            $report.scan=Get-OptimusMediaScanOutcome $scan.ExitCode
            $report.status=$report.scan.status;$report.reason=$report.scan.reason
            if ($Mode -eq 'Upgrade' -and $report.scan.can_upgrade) {
                $fresh=Get-OptimusMediaPreflight $SetupPath
                foreach ($key in @('setup_path','setup_sha256','image_path','image_size','image_write_time','image_index','target_version')) {
                    if ($fresh[$key] -cne $context[$key]) {throw 'Selected media or target metadata changed after the compatibility scan. No upgrade was started.'}
                }
                $upgradeArgs=$common+@('/Compat','IgnoreWarning','/DynamicUpdate','Enable','/CopyLogs',(Join-Path $directory 'upgrade-logs.zip'))
                $report.upgrade_arguments=$upgradeArgs;$report.status='upgrade_prepared'
                Write-OptimusMediaReport $report $reportPath
                $upgrade=Start-OptimusMediaSetup $context.setup_path $upgradeArgs
                $report.upgrade_started=$true;$report.upgrade_pid=$upgrade.Id;$report.status='pending'
                $report.reason='Visible Windows Setup was started. Installation completion is not verified. Review Setup and this report before a later restart; no automatic retry was requested.'
            }
        }
    } catch {
        $report.status=if ($attempted) {'uncertain'} else {'deferred'}
        $report.reason=$_.Exception.Message
    } finally {
        if ($guard) {$guard.ReleaseMutex();$guard.Dispose()}
    }
    $report.finished_at=[DateTime]::UtcNow.ToString('o')
    if ($reportPath) {try {Write-OptimusMediaReport $report $reportPath} catch {$report.report_write_error=$_.Exception.Message}}
    return $report
}

# Dot-sourcing exposes the function library only; fixture tests replace every
# host reader and process launcher before calling the entry function.
if ($MyInvocation.InvocationName -ne '.') {
    $result=Invoke-OptimusWindowsMediaUpgrade -SetupPath $SetupPath -Mode $Mode -AcceptLicense:$AcceptLicense -OutputDirectory $OutputDirectory
    $result | ConvertTo-Json -Depth 14
    if ($result.status -in @('scan_ready','pending')) {exit 0}
    exit 2
}
