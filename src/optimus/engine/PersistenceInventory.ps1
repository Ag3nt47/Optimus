# Windows PowerShell 5.1. Functions only: loading this file performs no changes.

function Test-SCMAdministrator {
    $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
    $principal = New-Object Security.Principal.WindowsPrincipal($identity)
    return $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
}

function Get-SCMStableId {
    param([Parameter(Mandatory=$true)][string]$Text)
    $sha = [Security.Cryptography.SHA256]::Create()
    try { return ([BitConverter]::ToString($sha.ComputeHash([Text.Encoding]::UTF8.GetBytes($Text)))).Replace('-', '') }
    finally { $sha.Dispose() }
}

function Get-SCMStartupFolders {
    $result = @()
    foreach ($spec in @(@('User', 'Startup'), @('Machine', 'CommonStartup'))) {
        $path = [Environment]::GetFolderPath([Environment+SpecialFolder]$spec[1])
        if ($path) { $result += [pscustomobject]@{ Scope=$spec[0]; Path=[IO.Path]::GetFullPath($path) } }
    }
    return $result
}

function Get-SCMStartupBackupRoot {
    $dataVariable = Get-Variable -Name SCMDataRoot -Scope Script -ErrorAction SilentlyContinue
    if (-not $dataVariable -or -not $dataVariable.Value) { throw 'SCMDataRoot must be set before using startup backups.' }
    return [IO.Path]::GetFullPath((Join-Path ([string]$dataVariable.Value) 'DisabledStartup'))
}

function Open-SCMStartupRegistryKey {
    param([Parameter(Mandatory=$true)]$Entry, [bool]$Writable=$false)
    if ([string]$Entry.RegistryPath -ne 'Software\Microsoft\Windows\CurrentVersion\Run') { throw 'Only the Windows Run startup key is supported.' }
    if ([string]$Entry.Hive -notin @('CurrentUser','LocalMachine')) { throw 'Unsupported registry hive.' }
    if ([string]$Entry.RegistryView -notin @('Registry32','Registry64')) { throw 'Unsupported registry view.' }
    if (-not [Environment]::Is64BitOperatingSystem -and $Entry.RegistryView -eq 'Registry64') { throw 'This registry view is unavailable.' }
    if ($Writable -and $Entry.Hive -eq 'LocalMachine' -and -not (Test-SCMAdministrator)) { throw 'Administrator rights are required for machine startup entries.' }
    $baseKey = [Microsoft.Win32.RegistryKey]::OpenBaseKey([Microsoft.Win32.RegistryHive]$Entry.Hive, [Microsoft.Win32.RegistryView]$Entry.RegistryView)
    try { return $baseKey.OpenSubKey([string]$Entry.RegistryPath, $Writable) }
    finally { $baseKey.Dispose() }
}

function Get-SCMRegistryValueToken {
    param([string]$Kind, $Value)
    switch ($Kind) {
        'Binary' { return [Convert]::ToBase64String([byte[]]$Value) }
        'None' { return [Convert]::ToBase64String([byte[]]$Value) }
        'MultiString' { return (ConvertTo-Json -InputObject @([string[]]$Value) -Compress) }
        default { return [string]$Value }
    }
}

function Get-SCMStartupRegistryFingerprint {
    param([string]$Kind, $Value)
    return Get-SCMStableId ($Kind + '|' + (Get-SCMRegistryValueToken -Kind $Kind -Value $Value))
}

function Convert-SCMRegistryRawValue {
    param([string]$Kind, $Value)
    switch ($Kind) {
        'String' { return [string]$Value }
        'ExpandString' { return [string]$Value }
        'MultiString' { return ,([string[]]$Value) }
        'Binary' { return ,([byte[]]$Value) }
        'None' { return ,([byte[]]$Value) }
        'DWord' { return [int]$Value }
        'QWord' { return [long]$Value }
        default { throw 'Unsupported saved registry value kind.' }
    }
}

function Assert-SCMStartupShortcutPath {
    param([Parameter(Mandatory=$true)][string]$Path, [bool]$Writable=$false)
    $fullPath = [IO.Path]::GetFullPath($Path)
    if ([IO.Path]::GetExtension($fullPath) -ine '.lnk') { throw 'Only Startup folder shortcut files can be managed.' }
    foreach ($folder in @(Get-SCMStartupFolders)) {
        if ([IO.Path]::GetDirectoryName($fullPath).TrimEnd('\') -ieq $folder.Path.TrimEnd('\')) {
            if ($Writable -and $folder.Scope -eq 'Machine' -and -not (Test-SCMAdministrator)) { throw 'Administrator rights are required for shared Startup folder entries.' }
            if (Test-Path -LiteralPath $fullPath) {
                $item = Get-Item -LiteralPath $fullPath -Force -ErrorAction Stop
                if ($item.PSIsContainer -or ($item.Attributes -band [IO.FileAttributes]::ReparsePoint)) { throw 'Directory and reparse-point startup entries cannot be changed.' }
            }
            return $fullPath
        }
    }
    throw 'Shortcut is outside the supported Windows Startup folders.'
}

function Get-SCMStartupRecords {
    $root = Get-SCMStartupBackupRoot
    if (-not (Test-Path -LiteralPath $root -PathType Container)) { return @() }
    $records = @()
    foreach ($file in @(Get-ChildItem -LiteralPath $root -Filter '*.json' -File -ErrorAction SilentlyContinue)) {
        try {
            $record = Get-Content -LiteralPath $file.FullName -Raw -ErrorAction Stop | ConvertFrom-Json -ErrorAction Stop
            if ($record.Version -eq 1 -and $record.RecordId -match '^[a-f0-9]{32}$' -and $file.BaseName -ceq $record.RecordId -and $record.Original -and $record.State -in @('Disabled','Removed','Enabled','Undone')) { $records += $record }
        } catch { Write-Verbose ('Unreadable startup backup retained: ' + $file.FullName) }
    }
    return $records
}

function Save-SCMStartupRecord {
    param([Parameter(Mandatory=$true)]$Record)
    if ($Record.RecordId -notmatch '^[a-f0-9]{32}$') { throw 'Invalid startup backup identifier.' }
    $root = Get-SCMStartupBackupRoot
    [void][IO.Directory]::CreateDirectory($root)
    $path = Join-Path $root ($Record.RecordId + '.json')
    $temporary = Join-Path $root ([guid]::NewGuid().ToString('N') + '.tmp')
    try {
        [IO.File]::WriteAllText($temporary, (ConvertTo-Json -InputObject $Record -Depth 16), (New-Object Text.UTF8Encoding($false)))
        if (Test-Path -LiteralPath $path) { [IO.File]::Replace($temporary, $path, [System.Management.Automation.Language.NullString]::Value) }
        else { [IO.File]::Move($temporary, $path) }
    } finally { if (Test-Path -LiteralPath $temporary) { Remove-Item -LiteralPath $temporary -Force -ErrorAction SilentlyContinue } }
}

function Get-SCMStartupRecordById {
    param([Parameter(Mandatory=$true)][string]$RecordId)
    if ($RecordId -notmatch '^[a-f0-9]{32}$') { throw 'Invalid startup backup identifier.' }
    $record = @(Get-SCMStartupRecords | Where-Object { $_.RecordId -ceq $RecordId })
    if ($record.Count -ne 1) { throw 'The saved startup backup could not be read.' }
    return $record[0]
}

function Get-SCMStartupProtection {
    param([string]$Name, [string]$Command)
    $text=$Name+' '+$Command
    if (($text -replace '[^A-Za-z0-9]','') -match '(?i)BingWallpaper') { return 'Bing Wallpaper is preserved and its startup entry is protected.' }
    if ($text -match '(?i)anti.?virus|anti.?malware|securityhealth|defend|crowdstrike|sentinel|sophos|eset|kaspersky|bitdefender|malwarebytes|avast|avira|trend.?micro|mcafee|trellix|symantec|norton|cylance|forticlient|check.?point|withsecure|webroot|watchguard|huntress|recovery|backup|bitlocker|encrypt|credential|password|firewall') { return 'Security, recovery, backup, or credential related startup entries are protected.' }
    $exe=Get-SCMExecutableFromCommand -Command $Command
    if ($exe -and $exe.StartsWith(([IO.Path]::GetFullPath($env:SystemRoot).TrimEnd('\')+'\'),[StringComparison]::OrdinalIgnoreCase)) { return 'Startup commands supplied from the Windows directory are protected.' }
    return ''
}

function Get-SCMStartupEntries {
    $entries = @()
    $admin = Test-SCMAdministrator
    $views = @('Registry32')
    if ([Environment]::Is64BitOperatingSystem) { $views = @('Registry64','Registry32') }
    foreach ($hive in @('CurrentUser','LocalMachine')) {
        $hiveViews=$views
        if ($hive -eq 'CurrentUser') { $hiveViews=@($views[0]) } # HKCU Run is shared between WOW64 views.
        foreach ($view in $hiveViews) {
            $spec = [pscustomobject]@{ Hive=$hive; RegistryView=$view; RegistryPath='Software\Microsoft\Windows\CurrentVersion\Run' }
            $key = $null
            try {
                $key = Open-SCMStartupRegistryKey -Entry $spec
                if ($key) {
                    foreach ($name in $key.GetValueNames()) {
                        $kind = $key.GetValueKind($name).ToString()
                        $raw = $key.GetValue($name, $null, [Microsoft.Win32.RegistryValueOptions]::DoNotExpandEnvironmentNames)
                        $fingerprint = Get-SCMStartupRegistryFingerprint -Kind $kind -Value $raw
                        $canManage = ($hive -eq 'CurrentUser' -or $admin)
                        $reason = 'Windows launches this entry when you sign in; the entry does not prove a process is currently running.'
                        if (-not $canManage) { $reason += ' Administrator rights are required to change machine startup.' }
                        $protection=Get-SCMStartupProtection -Name $name -Command ([string]$raw)
                        if ($protection) { $canManage=$false; $reason=$protection }
                        $entries += [pscustomobject]@{
                            Id=('Registry-' + (Get-SCMStableId (($hive+'|'+$view+'|'+$spec.RegistryPath+'|'+$name).ToLowerInvariant())))
                            Name=$name; Source=('Run registry ('+$hive+', '+$view+')'); Command=([string]$raw)
                            Location=($hive+'\'+$spec.RegistryPath); Enabled=$true; CanManage=$canManage; Reason=$reason; Protected=[bool]$protection
                            EntryType='Registry'; Hive=$hive; RegistryView=$view; RegistryPath=$spec.RegistryPath
                            ValueName=$name; ValueKind=$kind; RawValue=$raw; FilePath=''; Fingerprint=$fingerprint; BackupId=''
                        }
                    }
                }
            } catch { Write-Verbose ('Cannot inspect '+$hive+' '+$view+': '+$_.Exception.Message) }
            finally { if ($key) { $key.Dispose() } }
        }
    }
    $shell = $null
    try { $shell = New-Object -ComObject WScript.Shell } catch { }
    try {
        foreach ($folder in @(Get-SCMStartupFolders)) {
            if (-not (Test-Path -LiteralPath $folder.Path -PathType Container)) { continue }
            foreach ($file in @(Get-ChildItem -LiteralPath $folder.Path -Filter '*.lnk' -File -Force -ErrorAction SilentlyContinue)) {
                $command = $file.FullName
                if ($shell) {
                    $shortcut = $null
                    try { $shortcut = $shell.CreateShortcut($file.FullName); $command = ('"'+$shortcut.TargetPath+'" '+$shortcut.Arguments).Trim() }
                    catch { } finally { if ($shortcut) { [void][Runtime.InteropServices.Marshal]::ReleaseComObject($shortcut) } }
                }
                $fingerprint = ''
                try { $fingerprint = (Get-FileHash -LiteralPath $file.FullName -Algorithm SHA256 -ErrorAction Stop).Hash } catch { }
                $canManage = ($folder.Scope -eq 'User' -or $admin) -and -not ($file.Attributes -band [IO.FileAttributes]::ReparsePoint) -and [bool]$fingerprint
                $reason = 'Windows opens this shortcut when you sign in; its target may create additional background processes.'
                if (-not $canManage) { $reason += ' Administrator access, a readable file, and a regular shortcut are required to change this entry.' }
                $protection=Get-SCMStartupProtection -Name $file.BaseName -Command $command
                if ($protection) { $canManage=$false; $reason=$protection }
                $entries += [pscustomobject]@{
                    Id=('Shortcut-'+(Get-SCMStableId $file.FullName.ToLowerInvariant())); Name=$file.BaseName; Source=('Startup folder ('+$folder.Scope+')')
                    Command=$command; Location=$folder.Path; Enabled=$true; CanManage=[bool]$canManage; Reason=$reason; Protected=[bool]$protection
                    EntryType='Shortcut'; FilePath=$file.FullName; Fingerprint=$fingerprint; BackupId=''
                }
            }
        }
    } finally { if ($shell) { [void][Runtime.InteropServices.Marshal]::ReleaseComObject($shell) } }
    foreach ($record in @(Get-SCMStartupRecords | Where-Object { $_.State -in @('Disabled','Removed') })) {
        $active = @($entries | Where-Object { $_.Id -ceq $record.Original.Id })
        if ($active.Count) {
            foreach ($item in $active) { $item.CanManage=$false; $item.Reason='This startup entry reappeared while a disabled backup exists. It is left unchanged to avoid overwriting either version.' }
            continue
        }
        $copy = $record.Original | ConvertTo-Json -Depth 12 | ConvertFrom-Json
        $copy.Enabled = $false
        $copy.BackupId = $record.RecordId
        $copy.CanManage = ($copy.EntryType -eq 'Shortcut' -and (@(Get-SCMStartupFolders | Where-Object { $_.Scope -eq 'User' -and $_.Path -ieq $copy.Location }).Count -gt 0)) -or ($copy.EntryType -eq 'Registry' -and $copy.Hive -eq 'CurrentUser') -or $admin
        $copy.Reason = 'Disabled here with a saved backup. Enable restores the original startup entry.'
        if ($record.State -eq 'Removed') { $copy.Source += ' [removed; backup]'; $copy.Reason='Startup registration removed, with a backup retained. Enable restores the registration; the application remains installed.' }
        $copy | Add-Member -NotePropertyName BackupState -NotePropertyValue $record.State -Force
        $entries += $copy
    }
    return @($entries | Sort-Object Name, Source)
}

function Resolve-SCMCurrentStartupEntry {
    param([Parameter(Mandatory=$true)]$Entry)
    $current = @(Get-SCMStartupEntries | Where-Object { $_.Id -ceq $Entry.Id })
    if ($current.Count -ne 1) { throw 'Startup entries changed. Refresh the list and select the item again.' }
    $fresh = $current[0]
    if ([bool]$fresh.Enabled -ne [bool]$Entry.Enabled -or $fresh.Fingerprint -cne $Entry.Fingerprint -or $fresh.BackupId -cne $Entry.BackupId) { throw 'This startup entry changed. Refresh the list before changing it.' }
    if (-not $fresh.CanManage) { throw $fresh.Reason }
    return $fresh
}

function Test-SCMStartupTargetPresent {
    param([Parameter(Mandatory=$true)]$Entry)
    if ($Entry.EntryType -eq 'Registry') {
        $key = $null
        try { $key=Open-SCMStartupRegistryKey -Entry $Entry; return ($null -ne $key -and @($key.GetValueNames()) -icontains [string]$Entry.ValueName) }
        finally { if ($key) { $key.Dispose() } }
    }
    if ($Entry.EntryType -eq 'Shortcut') { $path=Assert-SCMStartupShortcutPath -Path $Entry.FilePath; return Test-Path -LiteralPath $path }
    throw 'Unsupported startup entry type.'
}

function Add-SCMStartupTargetFromRecord {
    param([Parameter(Mandatory=$true)]$Record)
    $entry = $Record.Original
    if (Test-SCMStartupTargetPresent -Entry $entry) { throw 'A startup entry already exists at this location; nothing was overwritten.' }
    if ($entry.EntryType -eq 'Registry') {
        $key = $null
        try {
            $key=Open-SCMStartupRegistryKey -Entry $entry -Writable $true
            if (-not $key) { throw 'The startup registry key no longer exists. Restore was stopped without recreating other registry structure.' }
            if (@($key.GetValueNames()) -icontains [string]$entry.ValueName) { throw 'The startup value reappeared; nothing was overwritten.' }
            $raw = Convert-SCMRegistryRawValue -Kind $entry.ValueKind -Value $entry.RawValue
            if ((Get-SCMStartupRegistryFingerprint -Kind $entry.ValueKind -Value $raw) -cne $entry.Fingerprint) { throw 'The saved startup value did not pass its integrity check.' }
            $key.SetValue([string]$entry.ValueName, $raw, [Microsoft.Win32.RegistryValueKind]$entry.ValueKind)
        } finally { if ($key) { $key.Dispose() } }
    } elseif ($entry.EntryType -eq 'Shortcut') {
        $path=Assert-SCMStartupShortcutPath -Path $entry.FilePath -Writable $true
        $backup=Join-Path (Get-SCMStartupBackupRoot) ($Record.RecordId+'.lnk')
        if (-not (Test-Path -LiteralPath $backup -PathType Leaf) -or (Get-FileHash -LiteralPath $backup -Algorithm SHA256).Hash -cne $entry.Fingerprint) { throw 'The saved shortcut is missing or changed; restore was stopped.' }
        $backupItem=Get-Item -LiteralPath $backup -Force -ErrorAction Stop
        if ($backupItem.Attributes -band [IO.FileAttributes]::ReparsePoint) { throw 'The saved shortcut is a reparse point and cannot be restored.' }
        [IO.File]::Move($backup, $path)
    } else { throw 'Unsupported startup entry type.' }
}

function Remove-SCMStartupTargetToRecord {
    param([Parameter(Mandatory=$true)]$Record)
    $entry=$Record.Original
    if ($entry.EntryType -eq 'Registry') {
        $key=$null
        try {
            $key=Open-SCMStartupRegistryKey -Entry $entry -Writable $true
            if (-not $key -or @($key.GetValueNames()) -inotcontains [string]$entry.ValueName) { throw 'The startup value is already absent. Refresh before changing it.' }
            $kind=$key.GetValueKind([string]$entry.ValueName).ToString()
            $raw=$key.GetValue([string]$entry.ValueName,$null,[Microsoft.Win32.RegistryValueOptions]::DoNotExpandEnvironmentNames)
            if ((Get-SCMStartupRegistryFingerprint -Kind $kind -Value $raw) -cne $entry.Fingerprint) { throw 'The startup registry value changed. Nothing was removed.' }
            $key.DeleteValue([string]$entry.ValueName,$true)
        } finally { if ($key) { $key.Dispose() } }
    } elseif ($entry.EntryType -eq 'Shortcut') {
        $path=Assert-SCMStartupShortcutPath -Path $entry.FilePath -Writable $true
        if (-not (Test-Path -LiteralPath $path -PathType Leaf) -or (Get-FileHash -LiteralPath $path -Algorithm SHA256).Hash -cne $entry.Fingerprint) { throw 'The startup shortcut changed or disappeared. Nothing was moved.' }
        $backup=Join-Path (Get-SCMStartupBackupRoot) ($Record.RecordId+'.lnk')
        if (Test-Path -LiteralPath $backup) { throw 'A saved shortcut already occupies the backup location.' }
        [IO.File]::Move($path,$backup)
    } else { throw 'Unsupported startup entry type.' }
}

function New-SCMStartupRemoval {
    param([Parameter(Mandatory=$true)]$Entry, [ValidateSet('Disabled','Removed')][string]$State)
    $record=[pscustomobject]@{ Version=1; RecordId=[guid]::NewGuid().ToString('N'); State=$State; CreatedUtc=[DateTime]::UtcNow.ToString('o'); Original=$Entry }
    Save-SCMStartupRecord -Record $record
    try { Remove-SCMStartupTargetToRecord -Record $record }
    catch { $record.State='Undone'; try { Save-SCMStartupRecord -Record $record } catch { }; throw }
    return [pscustomobject]@{ Kind='Startup'; Version=1; Action='RemoveActive'; RecordId=$record.RecordId; ExpectedState=$State; Id=$Entry.Id; Name=$Entry.Name }
}

function Set-SCMStartupState {
    param([Parameter(Mandatory=$true)]$Entry, [Parameter(Mandatory=$true)][bool]$Enabled)
    $fresh=Resolve-SCMCurrentStartupEntry -Entry $Entry
    if ([bool]$fresh.Enabled -eq $Enabled) { throw 'The startup entry is already in the requested state.' }
    if (-not $Enabled) { return New-SCMStartupRemoval -Entry $fresh -State Disabled }
    $record=Get-SCMStartupRecordById -RecordId $fresh.BackupId
    if ($record.State -notin @('Disabled','Removed')) { throw 'This backup changed; refresh before restoring it.' }
    $previousState=$record.State
    Add-SCMStartupTargetFromRecord -Record $record
    $record.State='Enabled'
    try { Save-SCMStartupRecord -Record $record }
    catch { try { Remove-SCMStartupTargetToRecord -Record $record } catch { }; throw 'The startup restore could not be journaled. Inspect the startup entry before retrying.' }
    return [pscustomobject]@{ Kind='Startup'; Version=1; Action='Enable'; RecordId=$record.RecordId; ExpectedState='Enabled'; PreviousState=$previousState; Id=$fresh.Id; Name=$fresh.Name }
}

function Remove-SCMStartupEntry {
    param([Parameter(Mandatory=$true)]$Entry)
    $fresh=Resolve-SCMCurrentStartupEntry -Entry $Entry
    if ($fresh.Enabled) { return New-SCMStartupRemoval -Entry $fresh -State Removed }
    $record=Get-SCMStartupRecordById -RecordId $fresh.BackupId
    if ($record.State -ne 'Disabled') { throw 'This backup changed; refresh before removing it.' }
    $record.State='Removed'
    Save-SCMStartupRecord -Record $record
    return [pscustomobject]@{ Kind='Startup'; Version=1; Action='MarkRemoved'; RecordId=$record.RecordId; ExpectedState='Removed'; Id=$fresh.Id; Name=$fresh.Name }
}

function Restore-SCMStartupChange {
    param([Parameter(Mandatory=$true)]$Undo)
    if ($Undo.Kind -ne 'Startup' -or $Undo.Version -ne 1) { throw 'Unsupported startup undo record.' }
    $record=Get-SCMStartupRecordById -RecordId $Undo.RecordId
    if ($record.State -cne $Undo.ExpectedState -or $record.Original.Id -cne $Undo.Id) { throw 'A later startup change prevents this undo. Undo newer changes first.' }
    if ($Undo.Action -eq 'RemoveActive') {
        $other=@(Get-SCMStartupRecords | Where-Object { $_.RecordId -cne $record.RecordId -and $_.Original.Id -ceq $Undo.Id -and $_.State -eq 'Disabled' })
        if ($other.Count) { throw 'A newer disabled backup exists for this entry. Undo it first.' }
        Add-SCMStartupTargetFromRecord -Record $record
        $record.State='Undone'
        Save-SCMStartupRecord -Record $record
    } elseif ($Undo.Action -eq 'Enable') {
        Remove-SCMStartupTargetToRecord -Record $record
        $record.State=if ($Undo.PreviousState -in @('Disabled','Removed')) { $Undo.PreviousState } else { 'Disabled' }
        Save-SCMStartupRecord -Record $record
    } elseif ($Undo.Action -eq 'MarkRemoved') {
        if (Test-SCMStartupTargetPresent -Entry $record.Original) { throw 'The startup entry reappeared; undo was stopped.' }
        $record.State='Disabled'
        Save-SCMStartupRecord -Record $record
    } else { throw 'Unsupported startup undo action.' }
}

function Get-SCMExecutableFromCommand {
    param([string]$Command)
    $expanded=[Environment]::ExpandEnvironmentVariables($Command).Trim()
    $candidate=''
    if ($expanded -match '^"([^"]+)"') { $candidate=$Matches[1] }
    elseif ($expanded -match '^(.+?\.exe)(?:\s|$)') { $candidate=$Matches[1] }
    if (-not $candidate -or -not [IO.Path]::IsPathRooted($candidate)) { return '' }
    try { return [IO.Path]::GetFullPath($candidate) } catch { return '' }
}

function Get-SCMServiceProtection {
    param([Parameter(Mandatory=$true)]$Service)
    $critical='^(RpcSs|RpcEptMapper|DcomLaunch|LSM|SamSs|EventLog|Schedule|PlugPlay|Power|ProfSvc|UserManager|Winmgmt|W32Time|Dhcp|Dnscache|NlaSvc|nsi|BFE|MpsSvc|WinDefend|WdNisSvc|SecurityHealthService|Sense|CryptSvc|KeyIso|VaultSvc|TrustedInstaller|wuauserv|UsoSvc|WaaSMedicSvc|msiserver|Appinfo|gpsvc|LanmanWorkstation|LanmanServer|WSearch|WpnService|StateRepository|AppXSvc|ClipSVC|BrokerInfrastructure|SystemEventsBroker|ShellHWDetection|SgrmBroker|sppsvc|NgcSvc|NgcCtnrSvc|WinRM|Wecsvc|VSS|swprv|vmcompute|hns|vmms|TermService|UmRdpService|SessionEnv|Audiosrv|AudioEndpointBuilder|MMCSS|hidserv|SENS|TokenBroker|WebAccountManager)(_|$)'
    if ([string]$Service.Name -match $critical) { return 'Windows, sign-in, security, networking, or recovery infrastructure is protected.' }
    if ([string]$Service.ServiceType -match 'Driver|Kernel|File System') { return 'Device and system drivers are protected.' }
    $description=([string]$Service.Name+' '+[string]$Service.DisplayName+' '+[string]$Service.PathName+' '+[string]$Service.Description)
    if ($description -match '(?i)anti.?virus|anti.?malware|defend|security|endpoint|crowdstrike|falcon|sentinel|sophos|eset|kaspersky|bitdefender|malwarebytes|avast|avgsvc|avg[ ]|avira|trend.?micro|mcafee|trellix|symantec|norton|carbon.?black|cylance|cortex|palo.?alto|fortinet|forticlient|check.?point|withsecure|f.?secure|webroot|watchguard|huntress|elastic.?agent|wazuh|osquery|tanium|rapid7|qualys|tenable|anyconnect|secure.?client|zscaler|netskope|globalprotect|wireguard|openvpn|tailscale|zerotier|backup|recovery|restore|credential|password|encrypt|bitlocker|firewall|anticheat|anti.?cheat') { return 'Security, access, recovery, or backup related services are protected.' }
    $exe=Get-SCMExecutableFromCommand -Command $Service.PathName
    if (-not $exe -or -not (Test-Path -LiteralPath $exe -PathType Leaf)) { return 'The service executable could not be identified and verified; it is protected.' }
    $windows=[IO.Path]::GetFullPath($env:SystemRoot).TrimEnd('\')+'\'
    if ($exe.StartsWith($windows,[StringComparison]::OrdinalIgnoreCase)) { return 'Services supplied from the Windows directory are protected.' }
    try {
        $file=Get-Item -LiteralPath $exe -Force -ErrorAction Stop
        if ($file.Attributes -band [IO.FileAttributes]::ReparsePoint) { return 'A service executable with an indirect file target is protected.' }
        $company=[string]$file.VersionInfo.CompanyName
        if (-not $company.Trim()) { return 'The service publisher is unknown; the service is protected.' }
        if ($company -match '(?i)Microsoft') { return 'Microsoft system and application services are protected.' }
        if ($company -match '(?i)security|crowdstrike|sentinel|sophos|eset|kaspersky|bitdefender|malwarebytes|avast|avira|trend.?micro|mcafee|trellix|symantec|norton|carbon.?black|cylance|palo.?alto|fortinet|check.?point|withsecure|f.?secure|webroot|watchguard|huntress|tanium|rapid7|qualys|tenable|acronis|veeam') { return 'This service is provided by a security, management, or backup publisher and is protected.' }
    } catch { return 'The service publisher could not be inspected; the service is protected.' }
    return ''
}

function Get-SCMServiceEntries {
    $result=@()
    $admin=Test-SCMAdministrator
    foreach ($service in @(Get-CimInstance -ClassName Win32_Service -ErrorAction Stop)) {
        $reason=Get-SCMServiceProtection -Service $service
        $protected=[bool]$reason
        if (-not $protected) { $reason='Third-party service. Stopping can interrupt the associated program; Manual changes future startup only, while Disabled prevents starts.' }
        $result += [pscustomobject]@{
            Name=$service.Name; DisplayName=$service.DisplayName; State=$service.State; StartMode=$service.StartMode
            PathName=$service.PathName; ProcessId=$service.ProcessId; Protected=$protected; Reason=$reason
            CanManage=($admin -and -not $protected); StartName=$service.StartName; Description=$service.Description; ServiceType=$service.ServiceType
        }
    }
    return @($result | Sort-Object DisplayName)
}

function Get-SCMServiceDelayedInfo {
    param([Parameter(Mandatory=$true)][string]$Name)
    if ($Name -match '[\\/\x00]') { throw 'Invalid service name.' }
    $base=[Microsoft.Win32.RegistryKey]::OpenBaseKey([Microsoft.Win32.RegistryHive]::LocalMachine,[Microsoft.Win32.RegistryView]::Default)
    $key=$null
    try {
        $key=$base.OpenSubKey(('SYSTEM\CurrentControlSet\Services\'+$Name),$false)
        if (-not $key) { throw 'The service registry configuration is unavailable.' }
        $present=@($key.GetValueNames()) -icontains 'DelayedAutoStart'
        if (-not $present) { return [pscustomobject]@{ Present=$false; Kind=''; Value=$null; Fingerprint='Absent' } }
        $kind=$key.GetValueKind('DelayedAutoStart').ToString()
        $value=$key.GetValue('DelayedAutoStart',$null,[Microsoft.Win32.RegistryValueOptions]::DoNotExpandEnvironmentNames)
        return [pscustomobject]@{ Present=$true; Kind=$kind; Value=$value; Fingerprint=(Get-SCMStartupRegistryFingerprint -Kind $kind -Value $value) }
    } finally { if ($key) { $key.Dispose() }; $base.Dispose() }
}

function Set-SCMServiceDelayedInfo {
    param([Parameter(Mandatory=$true)][string]$Name,[Parameter(Mandatory=$true)]$Info)
    if (-not (Test-SCMAdministrator)) { throw 'Administrator rights are required to restore delayed startup configuration.' }
    if ($Name -match '[\\/\x00]') { throw 'Invalid service name.' }
    $base=[Microsoft.Win32.RegistryKey]::OpenBaseKey([Microsoft.Win32.RegistryHive]::LocalMachine,[Microsoft.Win32.RegistryView]::Default)
    $key=$null
    try {
        $key=$base.OpenSubKey(('SYSTEM\CurrentControlSet\Services\'+$Name),$true)
        if (-not $key) { throw 'The service registry configuration is unavailable.' }
        if ($Info.Present) { $key.SetValue('DelayedAutoStart',(Convert-SCMRegistryRawValue -Kind $Info.Kind -Value $Info.Value),[Microsoft.Win32.RegistryValueKind]$Info.Kind) }
        else { $key.DeleteValue('DelayedAutoStart',$false) }
    } finally { if ($key) { $key.Dispose() }; $base.Dispose() }
}

function Save-SCMServiceBackup {
    param([Parameter(Mandatory=$true)]$Record)
    if ($Record.Undo.BackupId -notmatch '^[a-f0-9]{32}$') { throw 'Invalid service backup identifier.' }
    $root=Join-Path ([IO.Path]::GetDirectoryName((Get-SCMStartupBackupRoot))) 'ServiceBackups'
    [void][IO.Directory]::CreateDirectory($root)
    $path=Join-Path $root ($Record.Undo.BackupId+'.json')
    $temporary=Join-Path $root ([guid]::NewGuid().ToString('N')+'.tmp')
    try {
        [IO.File]::WriteAllText($temporary,(ConvertTo-Json -InputObject $Record -Depth 12),(New-Object Text.UTF8Encoding($false)))
        if (Test-Path -LiteralPath $path) { [IO.File]::Replace($temporary,$path,[System.Management.Automation.Language.NullString]::Value) } else { [IO.File]::Move($temporary,$path) }
    } finally { if (Test-Path -LiteralPath $temporary) { Remove-Item -LiteralPath $temporary -Force -ErrorAction SilentlyContinue } }
}

function Set-SCMServiceState {
    param([Parameter(Mandatory=$true)]$Entry, [Parameter(Mandatory=$true)][ValidateSet('Stop','Manual','Disabled')][string]$Action)
    if (-not (Test-SCMAdministrator)) { throw 'Administrator rights are required to change services.' }
    $fresh=@(Get-SCMServiceEntries | Where-Object { $_.Name -ceq $Entry.Name })
    if ($fresh.Count -ne 1) { throw 'This service no longer exists. Refresh the list.' }
    $service=$fresh[0]
    if ($service.Protected) { throw $service.Reason }
    if ($service.PathName -cne $Entry.PathName -or $service.StartMode -cne $Entry.StartMode -or $service.State -cne $Entry.State) { throw 'The service changed. Refresh the list before applying this action.' }
    Add-Type -AssemblyName System.ServiceProcess -ErrorAction Stop
    $controller=Get-Service -Name $service.Name -ErrorAction Stop
    if ($Action -in @('Stop','Disabled')) {
        $dependents=@($controller.DependentServices | Where-Object { $_.Status -ne 'Stopped' })
        if ($dependents.Count) { throw ('Running services depend on this service: '+(($dependents | ForEach-Object { $_.DisplayName }) -join ', ')+'. No change was made.') }
    }
    if ($Action -eq 'Stop' -and ($service.State -ne 'Running' -or -not $controller.CanStop)) { throw 'The service is not running or does not support stopping.' }
    if ($Action -ne 'Stop' -and $service.StartMode -eq $Action) { throw 'The service already has the requested startup mode.' }
    $delayed=Get-SCMServiceDelayedInfo -Name $service.Name
    $undo=[pscustomobject]@{
        Kind='Service'; Version=1; BackupId=[guid]::NewGuid().ToString('N'); Name=$service.Name; DisplayName=$service.DisplayName
        PathName=$service.PathName; PreviousStartMode=$service.StartMode; PreviousState=$service.State
        ExpectedStartMode=$(if ($Action -eq 'Stop') { $service.StartMode } else { $Action }); ExpectedState=$(if ($Action -eq 'Stop') { 'Stopped' } else { $service.State })
        PreviousDelayed=$delayed; ExpectedDelayed=$delayed; Action=$Action; Warning=''
    }
    $backup=[pscustomobject]@{ Version=1; State='Prepared'; CreatedUtc=[DateTime]::UtcNow.ToString('o'); Undo=$undo }
    Save-SCMServiceBackup -Record $backup
    try {
        if ($Action -eq 'Stop') {
            $controller.Stop()
            try { $controller.WaitForStatus([ServiceProcess.ServiceControllerStatus]::Stopped,[TimeSpan]::FromSeconds(20)) }
            catch [System.ServiceProcess.TimeoutException] { $undo.Warning='The stop request was accepted, but the service is still stopping. Refresh its state before another action.' }
        } else {
            Set-Service -Name $service.Name -StartupType $Action -ErrorAction Stop
            $afterDelayed=Get-SCMServiceDelayedInfo -Name $service.Name
            if ($afterDelayed.Fingerprint -cne $delayed.Fingerprint) { Set-SCMServiceDelayedInfo -Name $service.Name -Info $delayed }
        }
    } catch {
        $failure=$_.Exception.Message
        $after=@(Get-CimInstance -ClassName Win32_Service -ErrorAction SilentlyContinue | Where-Object { $_.Name -ceq $service.Name })
        $afterDelayed=Get-SCMServiceDelayedInfo -Name $service.Name
        if ($after.Count -eq 1 -and ($after[0].State -cne $service.State -or $after[0].StartMode -cne $service.StartMode -or $afterDelayed.Fingerprint -cne $delayed.Fingerprint)) {
            $undo.ExpectedState=$after[0].State; $undo.ExpectedStartMode=$after[0].StartMode; $undo.ExpectedDelayed=$afterDelayed
            $undo.Warning='The action was only partly completed: '+$failure+'. The change is recorded so it can be undone.'
        } else {
            $backup.State='Failed'
            try { Save-SCMServiceBackup -Record $backup } catch { }
            throw $failure
        }
    }
    $backup.State='Applied'
    try { Save-SCMServiceBackup -Record $backup } catch { $undo.Warning += ' The prepared rollback backup was retained, but its status could not be updated.' }
    return $undo
}

function Restore-SCMServiceChange {
    param([Parameter(Mandatory=$true)]$Undo)
    if ($Undo.Kind -ne 'Service' -or $Undo.Version -ne 1) { throw 'Unsupported service undo record.' }
    if (-not (Test-SCMAdministrator)) { throw 'Administrator rights are required to restore a service.' }
    $fresh=@(Get-SCMServiceEntries | Where-Object { $_.Name -ceq $Undo.Name })
    if ($fresh.Count -ne 1) { throw 'The service no longer exists.' }
    $service=$fresh[0]
    if ($service.Protected) { throw $service.Reason }
    if ($service.PathName -cne $Undo.PathName -or $service.StartMode -cne $Undo.ExpectedStartMode -or $service.State -cne $Undo.ExpectedState) { throw 'The service changed after this action or is still stopping. Refresh and undo newer changes first; no service was modified.' }
    $delayed=Get-SCMServiceDelayedInfo -Name $Undo.Name
    if ($Undo.ExpectedDelayed -and $delayed.Fingerprint -cne $Undo.ExpectedDelayed.Fingerprint) { throw 'The delayed-start configuration changed after this action. It was left unchanged.' }
    if ($Undo.Action -eq 'Stop') {
        if ($Undo.PreviousState -eq 'Running') { Start-Service -Name $Undo.Name -ErrorAction Stop }
    } elseif ($Undo.Action -in @('Manual','Disabled')) {
        $mode=switch ($Undo.PreviousStartMode) { 'Auto' { 'Automatic' }; 'Manual' { 'Manual' }; 'Disabled' { 'Disabled' }; default { throw 'The previous startup mode is unsupported.' } }
        Set-Service -Name $Undo.Name -StartupType $mode -ErrorAction Stop
        if ($Undo.PreviousDelayed) { Set-SCMServiceDelayedInfo -Name $Undo.Name -Info $Undo.PreviousDelayed }
    } else { throw 'Unsupported service undo action.' }
    if ($Undo.BackupId) {
        $backup=[pscustomobject]@{ Version=1; State='Undone'; UpdatedUtc=[DateTime]::UtcNow.ToString('o'); Undo=$Undo }
        try { Save-SCMServiceBackup -Record $backup } catch { Write-Warning 'The service was restored, but its standalone backup status could not be updated.' }
    }
}

function Get-SCMScheduledEntries {
    if (-not (Get-Command Get-ScheduledTask -ErrorAction SilentlyContinue)) { return @() }
    $result=@()
    foreach ($task in @(Get-ScheduledTask -ErrorAction Stop)) {
        $commands=@()
        $protected=([string]$task.TaskPath -like '\Microsoft\*' -or [string]$task.Author -match '(?i)Microsoft')
        $reason='Read-only scheduled task inventory. Windows starts this task according to its saved triggers and conditions.'
        foreach ($action in @($task.Actions)) {
            $command=([string]$action.Execute+' '+[string]$action.Arguments).Trim()
            if (-not $command) { $command='Non-executable or unavailable task action'; $protected=$true }
            $commands += $command
            $exe=Get-SCMExecutableFromCommand -Command ('"'+[string]$action.Execute+'"')
            if (-not $exe -or -not (Test-Path -LiteralPath $exe -PathType Leaf)) { $protected=$true }
            elseif ($exe.StartsWith(([IO.Path]::GetFullPath($env:SystemRoot).TrimEnd('\')+'\'),[StringComparison]::OrdinalIgnoreCase)) { $protected=$true }
            if ($command -match '(?i)defend|antivirus|security|crowdstrike|sentinel|sophos|recovery|backup|encrypt|bitlocker') { $protected=$true }
        }
        if ($protected) { $reason='Protected Windows, security, recovery, or unverified task. Inventory is read-only.' }
        $result += [pscustomobject]@{ TaskName=$task.TaskName; TaskPath=$task.TaskPath; State=[string]$task.State; Author=$task.Author; Command=($commands -join ' | '); Hidden=[bool]$task.Settings.Hidden; Protected=[bool]$protected; Reason=$reason }
    }
    return @($result | Sort-Object TaskPath,TaskName)
}
