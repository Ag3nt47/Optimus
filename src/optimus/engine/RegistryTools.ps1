# Narrow current-user App Paths review, approved removal, and reversible backups.
# Windows PowerShell 5.1. Source loading defines functions only.

function Get-OptimusRegistryScope { 'Software\Microsoft\Windows\CurrentVersion\App Paths' }
function Get-OptimusRegistrySid { [Security.Principal.WindowsIdentity]::GetCurrent().User.Value }
function Assert-OptimusRegistryName {
    param([string]$Name)
    if (-not $Name -or $Name.Length -gt 255 -or $Name -match '[\\/\x00]' -or $Name -notmatch '(?i)\.exe$') { throw 'Only one App Paths executable-registration child key is supported.' }
}
function Get-OptimusRegistryAppPathNames {
    $key=[Microsoft.Win32.Registry]::CurrentUser.OpenSubKey((Get-OptimusRegistryScope),$false)
    if (-not $key) { return @() }
    try { @($key.GetSubKeyNames()) } finally { $key.Close() }
}
function Get-OptimusRegistryFingerprint {
    param($Snapshot)
    $canonical=[ordered]@{Scope=(Get-OptimusRegistryScope);Name=[string]$Snapshot.Name;UserSid=[string]$Snapshot.UserSid;Values=@($Snapshot.Values | Sort-Object Name);AclSddl=[string]$Snapshot.AclSddl}
    $bytes=[Text.Encoding]::UTF8.GetBytes((ConvertTo-Json -InputObject $canonical -Depth 12 -Compress))
    $sha=[Security.Cryptography.SHA256]::Create()
    try { ([BitConverter]::ToString($sha.ComputeHash($bytes))).Replace('-','').ToLowerInvariant() } finally { $sha.Dispose() }
}
function Read-OptimusRegistryAppPath {
    param([string]$Name)
    Assert-OptimusRegistryName $Name
    $key=[Microsoft.Win32.Registry]::CurrentUser.OpenSubKey(((Get-OptimusRegistryScope)+'\'+$Name),$false)
    if (-not $key) { throw 'This App Paths registration no longer exists.' }
    try {
        if ($key.SubKeyCount -ne 0) { throw 'Nested App Paths registrations are excluded.' }
        $names=@($key.GetValueNames())
        if ($names.Count -gt 20) { throw 'Registrations with more than 20 values are excluded.' }
        $values=New-Object 'System.Collections.Generic.List[object]'
        [long]$valueBytes=0
        foreach($valueName in ($names | Sort-Object)) {
            $kind=$key.GetValueKind($valueName)
            $raw=$key.GetValue($valueName,$null,[Microsoft.Win32.RegistryValueOptions]::DoNotExpandEnvironmentNames)
            $data=$null
            switch([string]$kind) {
                'String' {$data=[string]$raw;$valueBytes+=[Text.Encoding]::Unicode.GetByteCount($data)+2}
                'ExpandString' {$data=[string]$raw;$valueBytes+=[Text.Encoding]::Unicode.GetByteCount($data)+2}
                'MultiString' {$data=@([string[]]$raw);foreach($part in $data){$valueBytes+=[Text.Encoding]::Unicode.GetByteCount($part)+2};$valueBytes+=2}
                'DWord' {$data=([int]$raw).ToString([Globalization.CultureInfo]::InvariantCulture);$valueBytes+=4}
                'QWord' {$data=([long]$raw).ToString([Globalization.CultureInfo]::InvariantCulture);$valueBytes+=8}
                'Binary' {$data=[Convert]::ToBase64String([byte[]]$raw);$valueBytes+=([byte[]]$raw).Length}
                'None' {$data=[Convert]::ToBase64String([byte[]]$raw);$valueBytes+=([byte[]]$raw).Length}
                default { throw 'This registration contains an unsupported raw value type.' }
            }
            $valueBytes+=[Text.Encoding]::Unicode.GetByteCount($valueName)+2
            if ($valueBytes -gt 32768) { throw 'Registrations larger than 32 KiB are excluded.' }
            $values.Add([pscustomobject][ordered]@{Name=[string]$valueName;Kind=[string]$kind;Data=$data})
        }
        $sections=[Security.AccessControl.AccessControlSections]::Access -bor [Security.AccessControl.AccessControlSections]::Owner -bor [Security.AccessControl.AccessControlSections]::Group
        $acl=$key.GetAccessControl($sections).GetSecurityDescriptorSddlForm($sections)
        [pscustomobject]@{Name=$Name;Scope=(Get-OptimusRegistryScope);UserSid=(Get-OptimusRegistrySid);Values=@($values.ToArray());ValueBytes=$valueBytes;AclSddl=$acl}
    } finally { $key.Close() }
}
function Get-OptimusRegistryMissingTarget {
    param($Snapshot)
    $default=@($Snapshot.Values | Where-Object {$_.Name -eq '' -and $_.Kind -in @('String','ExpandString')})
    if($default.Count -ne 1){return $null}
    $path=[string]$default[0].Data
    if($default[0].Kind -eq 'ExpandString'){$path=[Environment]::ExpandEnvironmentVariables($path)}
    $path=$path.Trim()
    if($path.StartsWith('"') -and $path.EndsWith('"') -and $path.Length -gt 2){$path=$path.Substring(1,$path.Length-2)}
    if($path -notmatch '^[a-zA-Z]:\\[^"*?<>|]+\.exe$' -or $path.Substring(2).Contains(':')){return $null}
    $path=[IO.Path]::GetFullPath($path)
    try {
        [void](Get-Item -LiteralPath $path -Force -ErrorAction Stop)
        return $null
    } catch [System.Management.Automation.ItemNotFoundException] { return $path }
    catch [System.Management.Automation.DriveNotFoundException] { return $null }
    catch { return $null }
}
function Get-OptimusRegistryIssues {
    [CmdletBinding()]
    param()
    if(-not(Get-Variable -Name OptimusRegistryApprovals -Scope Script -ErrorAction SilentlyContinue)){$script:OptimusRegistryApprovals=@{}}
    $now=[datetime]::UtcNow
    foreach($id in @($script:OptimusRegistryApprovals.Keys)) {
        if(($now-([datetimeoffset]$script:OptimusRegistryApprovals[$id].CapturedUtc).UtcDateTime).TotalMinutes -gt 15){[void]$script:OptimusRegistryApprovals.Remove($id)}
    }
    $issues=New-Object 'System.Collections.Generic.List[object]'
    $skipped=0
    foreach($name in @(Get-OptimusRegistryAppPathNames)) {
        try {
            $snapshot=Read-OptimusRegistryAppPath $name
            $target=Get-OptimusRegistryMissingTarget $snapshot
            if(-not $target){continue}
            $fingerprint=Get-OptimusRegistryFingerprint $snapshot
            $id=[guid]::NewGuid().ToString('N')
            $script:OptimusRegistryApprovals[$id]=@{Id=$id;Name=$name;Target=$target;Fingerprint=$fingerprint;UserSid=$snapshot.UserSid;CapturedUtc=$now.ToString('o')}
            $issues.Add([pscustomobject]@{Id=$id;IssueId=$id;Kind='RegistryEntry';Name=$name;Scope=('HKCU\'+(Get-OptimusRegistryScope));KeyPath=('HKCU\'+(Get-OptimusRegistryScope)+'\'+$name);Target=$target;Fingerprint=$fingerprint;CanRemove=$true;Evidence='The current-user App Paths default value refers to a missing local executable. This does not prove the registration is unused.';ValueCount=@($snapshot.Values).Count;ValueBytes=$snapshot.ValueBytes})
        } catch { $skipped++ }
    }
    [pscustomobject]@{issues=@($issues.ToArray());skipped=$skipped;note='Review only. Selective removal supports only bounded HKCU App Paths keys with no children, captured raw value types and ACL, a fresh missing target, and a verified backup. Missing targets are evidence, not proof of an unused registration. No HKLM, COM/CLSID, shared DLL, installer, or wholesale registry cleanup.'}
}
function Get-OptimusRegistryBackupRoot {
    if(-not(Get-Variable -Name SCMDataRoot -Scope Script -ErrorAction SilentlyContinue)){throw 'The Optimus data directory is not configured.'}
    $root=Join-Path ([IO.Path]::GetFullPath($script:SCMDataRoot)) 'RegistryBackups'
    $directory=New-Object IO.DirectoryInfo($root)
    while($directory){if($directory.Exists -and ($directory.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0){throw 'Registry backup folders cannot use reparse paths.'};$directory=$directory.Parent}
    [void][IO.Directory]::CreateDirectory($root)
    $directory=New-Object IO.DirectoryInfo($root)
    while($directory){if(($directory.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0){throw 'Registry backup folders cannot use reparse paths.'};$directory=$directory.Parent}
    return $root
}
function Write-OptimusRegistryBackup {
    param([string]$Id,$Snapshot,[string]$Fingerprint)
    $path=Join-Path (Get-OptimusRegistryBackupRoot) ($Id+'.json')
    $backup=[ordered]@{Schema=1;BackupId=$Id;Kind='RegistryEntry';CapturedUtc=[datetime]::UtcNow.ToString('o');Fingerprint=$Fingerprint;Snapshot=$Snapshot}
    $bytes=[Text.Encoding]::UTF8.GetBytes((ConvertTo-Json -InputObject $backup -Depth 14))
    $temporary=$path+'.pending-'+[guid]::NewGuid().ToString('N')
    $stream=New-Object IO.FileStream($temporary,[IO.FileMode]::CreateNew,[IO.FileAccess]::Write,[IO.FileShare]::None)
    try {$stream.Write($bytes,0,$bytes.Length);$stream.Flush($true)} finally {$stream.Dispose()}
    [IO.File]::Move($temporary,$path)
    $saved=Get-Content -LiteralPath $path -Raw -Encoding UTF8 | ConvertFrom-Json
    if((Get-OptimusRegistryFingerprint $saved.Snapshot) -ne $Fingerprint){throw 'The registry backup could not be verified.'}
    return $path
}
function Remove-OptimusRegistryAppPathKey {
    param([string]$Name)
    Assert-OptimusRegistryName $Name
    $parent=[Microsoft.Win32.Registry]::CurrentUser.OpenSubKey((Get-OptimusRegistryScope),$true)
    if(-not $parent){throw 'The App Paths registration parent disappeared.'}
    try {
        $child=$parent.OpenSubKey($Name,$false)
        if(-not $child){throw 'The selected registration disappeared.'}
        try {if($child.SubKeyCount -ne 0){throw 'Nested registry keys cannot be removed.'}} finally {$child.Close()}
        $parent.DeleteSubKey($Name,$true)
    } finally {$parent.Close()}
}
function Remove-OptimusRegistryIssue {
    [CmdletBinding()]
    param([Parameter(Mandatory=$true)]$Record,[scriptblock]$BeforeRemove)
    $id=[string]$Record.Id
    if(-not(Get-Variable -Name OptimusRegistryApprovals -Scope Script -ErrorAction SilentlyContinue) -or -not $script:OptimusRegistryApprovals.ContainsKey($id)){throw 'This registry review record is unknown. Scan again.'}
    $approved=$script:OptimusRegistryApprovals[$id]
    if($approved.UserSid -ne (Get-OptimusRegistrySid) -or ([datetime]::UtcNow-([datetimeoffset]$approved.CapturedUtc).UtcDateTime).TotalMinutes -gt 15){throw 'This record belongs to another user context or has expired.'}
    foreach($field in @('Name','Target','Fingerprint')){if(-not([string]$approved[$field]).Equals([string]$Record.$field,[StringComparison]::OrdinalIgnoreCase)){throw 'The registry review record changed. Scan again.'}}
    $fresh=Read-OptimusRegistryAppPath $approved.Name
    if((Get-OptimusRegistryFingerprint $fresh) -ne $approved.Fingerprint -or (Get-OptimusRegistryMissingTarget $fresh) -ne $approved.Target){throw 'The registration or executable state changed. Scan again.'}
    $backupId=[guid]::NewGuid().ToString('N')
    $backupPath=Write-OptimusRegistryBackup -Id $backupId -Snapshot $fresh -Fingerprint $approved.Fingerprint
    if(-not(Test-Path -LiteralPath $backupPath -PathType Leaf)){throw 'The required registry backup is missing. Nothing was removed.'}
    $undo=[pscustomobject]@{Kind='RegistryEntry';Schema=1;BackupId=$backupId;Fingerprint=$approved.Fingerprint;Name=$approved.Name;UserSid=$approved.UserSid;BackupPath=$backupPath;Removed=$false;Note='The full registration backup was verified before removal.'}
    if($BeforeRemove){[void](& $BeforeRemove $undo)}
    $final=Read-OptimusRegistryAppPath $approved.Name
    if((Get-OptimusRegistryFingerprint $final) -ne $approved.Fingerprint -or (Get-OptimusRegistryMissingTarget $final) -ne $approved.Target){throw 'The registration changed while its backup was written. Nothing was removed.'}
    Remove-OptimusRegistryAppPathKey $approved.Name
    [void]$script:OptimusRegistryApprovals.Remove($id)
    [pscustomobject]@{Kind='RegistryEntry';Schema=1;BackupId=$backupId;Fingerprint=$approved.Fingerprint;Name=$approved.Name;UserSid=$approved.UserSid;BackupPath=$backupPath;Removed=$true;Note='Only this current-user App Paths registration was removed. Its application files were not changed.'}
}
function Test-OptimusRegistrySnapshot {
    param($Snapshot)
    Assert-OptimusRegistryName ([string]$Snapshot.Name)
    if($Snapshot.Scope -ne (Get-OptimusRegistryScope) -or $Snapshot.UserSid -ne (Get-OptimusRegistrySid) -or @($Snapshot.Values).Count -gt 20){throw 'The registry backup scope, user, or value count is invalid.'}
    [long]$bytes=0
    foreach($value in @($Snapshot.Values)) {
        if(([string]$value.Name).Contains([char]0)){throw 'Invalid registry value name.'}
        $bytes+=[Text.Encoding]::Unicode.GetByteCount([string]$value.Name)+2
        switch([string]$value.Kind) {
            'String' {$bytes+=[Text.Encoding]::Unicode.GetByteCount([string]$value.Data)+2}
            'ExpandString' {$bytes+=[Text.Encoding]::Unicode.GetByteCount([string]$value.Data)+2}
            'MultiString' {foreach($part in @($value.Data)){$bytes+=[Text.Encoding]::Unicode.GetByteCount([string]$part)+2};$bytes+=2}
            'DWord' {[void][int]::Parse([string]$value.Data,[Globalization.CultureInfo]::InvariantCulture);$bytes+=4}
            'QWord' {[void][long]::Parse([string]$value.Data,[Globalization.CultureInfo]::InvariantCulture);$bytes+=8}
            'Binary' {$bytes+=([Convert]::FromBase64String([string]$value.Data)).Length}
            'None' {$bytes+=([Convert]::FromBase64String([string]$value.Data)).Length}
            default {throw 'Unsupported registry backup value type.'}
        }
        if($bytes -gt 32768){throw 'Registry backup values exceed 32 KiB.'}
    }
    if(-not $Snapshot.AclSddl){throw 'The required saved registry ACL is missing.'}
    return $true
}
function Restore-OptimusRegistryAppPathKey {
    param($Snapshot)
    [void](Test-OptimusRegistrySnapshot $Snapshot)
    if(-not('OptimusRegistryNativeKey' -as [type])) {
        Add-Type -TypeDefinition @"
using System;
using System.ComponentModel;
using System.Runtime.InteropServices;
using Microsoft.Win32;
using Microsoft.Win32.SafeHandles;
public static class OptimusRegistryNativeKey {
 [DllImport("advapi32.dll", CharSet=CharSet.Unicode, SetLastError=true)]
 static extern int RegCreateKeyEx(IntPtr root, string name, int reserved, string keyClass, int options, int access, IntPtr security, out IntPtr handle, out int disposition);
 [DllImport("advapi32.dll")] static extern int RegCloseKey(IntPtr handle);
 public static RegistryKey CreateNew(string name) {
  string prefix = @"Software\Microsoft\Windows\CurrentVersion\App Paths\";
  if (!name.StartsWith(prefix, StringComparison.OrdinalIgnoreCase) || name.Substring(prefix.Length).IndexOf('\\') >= 0) throw new ArgumentException("Invalid App Paths scope");
  IntPtr handle; int disposition;
  int result=RegCreateKeyEx(new IntPtr(unchecked((int)0x80000001)),name,0,null,0,0xF003F,IntPtr.Zero,out handle,out disposition);
  if(result!=0) throw new Win32Exception(result);
  if(disposition!=1) { RegCloseKey(handle); throw new InvalidOperationException("A newer registration already exists; it will not be overwritten."); }
  return RegistryKey.FromHandle(new SafeRegistryHandle(handle,true));
 }
}
"@
    }
    $key=[OptimusRegistryNativeKey]::CreateNew(((Get-OptimusRegistryScope)+'\'+$Snapshot.Name))
    try {
        foreach($value in @($Snapshot.Values)) {
            $kind=[Microsoft.Win32.RegistryValueKind]([string]$value.Kind)
            switch([string]$value.Kind) {
                'MultiString' {$raw=[string[]]@($value.Data)}
                'DWord' {$raw=[int]::Parse([string]$value.Data,[Globalization.CultureInfo]::InvariantCulture)}
                'QWord' {$raw=[long]::Parse([string]$value.Data,[Globalization.CultureInfo]::InvariantCulture)}
                'Binary' {$raw=[Convert]::FromBase64String([string]$value.Data)}
                'None' {$raw=[Convert]::FromBase64String([string]$value.Data)}
                default {$raw=[string]$value.Data}
            }
            $key.SetValue([string]$value.Name,$raw,$kind)
        }
        $security=New-Object Security.AccessControl.RegistrySecurity
        $security.SetSecurityDescriptorSddlForm([string]$Snapshot.AclSddl)
        $key.SetAccessControl($security)
        $key.Flush()
    } finally {$key.Close()}
}
function Restore-OptimusRegistryIssue {
    [CmdletBinding()]
    param([Parameter(Mandatory=$true)]$Undo)
    if($Undo.Kind -ne 'RegistryEntry' -or [int]$Undo.Schema -ne 1 -or [string]$Undo.BackupId -notmatch '^[a-f0-9]{32}$' -or [string]$Undo.Fingerprint -notmatch '^[a-f0-9]{64}$'){throw 'This is not a supported registry restore record.'}
    if($Undo.UserSid -ne (Get-OptimusRegistrySid)){throw 'This backup belongs to another Windows user context.'}
    $path=Join-Path (Get-OptimusRegistryBackupRoot) ([string]$Undo.BackupId+'.json')
    $file=Get-Item -LiteralPath $path -Force -ErrorAction Stop
    if($file.Length -gt 262144 -or ($file.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0){throw 'The registry backup file is invalid.'}
    $backup=Get-Content -LiteralPath $path -Raw -Encoding UTF8 | ConvertFrom-Json
    if([int]$backup.Schema -ne 1 -or $backup.BackupId -ne $Undo.BackupId -or $backup.Kind -ne 'RegistryEntry' -or $backup.Snapshot.Name -ne $Undo.Name -or $backup.Fingerprint -ne $Undo.Fingerprint){throw 'The registry backup identity changed.'}
    [void](Test-OptimusRegistrySnapshot $backup.Snapshot)
    if((Get-OptimusRegistryFingerprint $backup.Snapshot) -ne $Undo.Fingerprint){throw 'The registry backup content changed.'}
    # Native CreateNew also checks atomically, avoiding a check/create overwrite race.
    if(@(Get-OptimusRegistryAppPathNames) -contains [string]$backup.Snapshot.Name){throw 'A newer registration already exists; it will not be overwritten.'}
    Restore-OptimusRegistryAppPathKey $backup.Snapshot
    [pscustomobject]@{Kind='RegistryEntry';Name=$backup.Snapshot.Name;Restored=$true;Note='Raw registry value types and the saved access/owner/group security descriptor were restored. Backup retained.'}
}
