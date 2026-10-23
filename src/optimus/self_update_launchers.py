"""Retarget existing owned Windows launchers after a verified managed self-update.

The update worker owns the maintenance lease and proves the old GUI has exited.
This service never launches/elevates an app, kills a process, writes preferences,
creates a shortcut/task, or adopts an unrelated launcher. Complex links are left
for manual review. Constructors and cached attributes perform no native work.
"""
from __future__ import annotations

import base64
import copy
import ctypes
import hashlib
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import time
import uuid

from .filesystem import _ordinary_data_path
from .startup_registration import StartupRegistration
from .workflow import atomic_json

_MAX_LINKS = 512
_MAX_LINK = 1024 ** 2
_MAX_XML = 65536
_FIELDS = {'path', 'target', 'arguments', 'working_directory', 'icon', 'description', 'hotkey', 'window_style', 'app_properties'}
_PROPERTIES = ('System.AppUserModel.ID', 'System.AppUserModel.RelaunchCommand',
    'System.AppUserModel.RelaunchIconResource', 'System.AppUserModel.RelaunchDisplayNameResource')


def _sha(raw):
    return hashlib.sha256(raw).hexdigest()


def _read(path, maximum):
    path = Path(path)
    before = _ordinary_data_path(path, file=True)
    if before is None or before.st_size > maximum:
        raise ValueError('A bounded existing ordinary launcher file is required.')
    with path.open('rb') as stream:
        opened = os.fstat(stream.fileno())
        if not stat.S_ISREG(opened.st_mode) or opened.st_nlink != 1:
            raise ValueError('The launcher file is not a private regular file.')
        raw = stream.read(maximum + 1)
    after = _ordinary_data_path(path, file=True)
    fields = lambda value: (value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns)
    if after is None or len(raw) > maximum or any(fields(row) != fields(before) for row in (opened, after)):
        raise ValueError('The launcher file changed during inspection.')
    return raw


def _write_new(path, raw):
    path = Path(path)
    _ordinary_data_path(path, file=True)
    with path.open('xb') as stream:
        stream.write(raw)
        stream.flush()
        os.fsync(stream.fileno())
    if _read(path, len(raw) + 1) != raw:
        raise ValueError('The launcher backup failed byte readback.')


def _canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=True, allow_nan=False)


def _plain(value, oldexe):
    if not isinstance(value, dict) or set(value) != _FIELDS:
        return False
    if any(not isinstance(value[key], str) or len(value[key]) > 4096 or '\x00' in value[key]
           for key in _FIELDS - {'window_style', 'app_properties'}):
        return False
    properties = value['app_properties']
    return (Path(value['target']) == oldexe and value['arguments'] == ''
        and (not value['working_directory'] or Path(value['working_directory']) == oldexe.parent)
        and type(value['window_style']) is int and value['window_style'] in (1, 3, 7)
        and isinstance(properties, dict) and set(properties) == set(_PROPERTIES)
        and properties['System.AppUserModel.ID'] in ('', 'Ag3nt47.Optimus.Desktop')
        and all(properties[key] == '' for key in _PROPERTIES if key != 'System.AppUserModel.ID'))


_LINK_PS = r"""
$ErrorActionPreference='Stop'
[Console]::OutputEncoding=[Text.UTF8Encoding]::new($false)
[Console]::InputEncoding=[Text.UTF8Encoding]::new($false)
function Ordinary([string]$p){$at=[IO.Path]::GetFullPath($p);while($at){$i=Get-Item -LiteralPath $at -Force;if($i.Attributes -band [IO.FileAttributes]::ReparsePoint){throw 'Indirect shortcut path refused.'};$at=[IO.Path]::GetDirectoryName($at)}}
try{
 $raw=[Console]::In.ReadToEnd();if($raw.Length -gt 131072){throw 'Launcher request too large.'};$r=$raw|ConvertFrom-Json
 if($r.operation -notin @('inventory','read','write')){throw 'Unknown launcher request.'}
 Add-Type -TypeDefinition 'using System;using System.Text;using System.ComponentModel;using System.Runtime.InteropServices;using Microsoft.Win32.SafeHandles;public static class OptimusKnownRoot{[DllImport("kernel32.dll",CharSet=CharSet.Unicode,SetLastError=true)]static extern SafeFileHandle CreateFileW(string p,uint a,uint s,IntPtr x,uint c,uint f,IntPtr t);[DllImport("kernel32.dll",CharSet=CharSet.Unicode,SetLastError=true)]static extern uint GetFinalPathNameByHandleW(SafeFileHandle h,StringBuilder b,uint n,uint f);public static string Resolve(string p){using(var h=CreateFileW(p,0,7,IntPtr.Zero,3,0x02000000,IntPtr.Zero)){if(h.IsInvalid)throw new Win32Exception(Marshal.GetLastWin32Error());var b=new StringBuilder(32768);uint n=GetFinalPathNameByHandleW(h,b,(uint)b.Capacity,0);if(n==0||n>=b.Capacity)throw new Win32Exception(Marshal.GetLastWin32Error());return b.ToString();}}}'
 $known=@([Environment]::GetFolderPath('DesktopDirectory'),[Environment]::GetFolderPath('Programs'),(Join-Path ([Environment]::GetFolderPath('ApplicationData')) 'Microsoft\Internet Explorer\Quick Launch\User Pinned\TaskBar'))|Select-Object -Unique
 $roots=@();$rootWarnings=@();foreach($logical in $known){try{if(-not $logical -or -not [IO.Directory]::Exists($logical)){throw 'Known folder is unavailable.'};$resolved=[OptimusKnownRoot]::Resolve($logical);if($resolved.StartsWith('\\?\')){$resolved=$resolved.Substring(4)};if($resolved -notmatch '^[A-Za-z]:\\'){throw 'Only local known folders are supported.'};Ordinary $resolved;$roots+=,[ordered]@{logical=$logical;resolved=$resolved}}catch{$rootWarnings+='A current-user launcher folder could not be resolved safely and was preserved: '+[string]$logical}}
 if($r.operation -ne 'inventory'){$actual=ConvertTo-Json -InputObject @($roots) -Depth 4 -Compress;$expected=ConvertTo-Json -InputObject @($r.expected_roots) -Depth 4 -Compress;if($actual -cne $expected){throw 'Known launcher folder identity changed after inventory.'}}
 $wsh=New-Object -ComObject WScript.Shell;$shell=New-Object -ComObject Shell.Application
 function Allowed([string]$p){Ordinary $p;if(-not [IO.File]::Exists($p) -or [IO.Path]::GetExtension($p) -ine '.lnk'){throw 'Existing ordinary shortcut required.'};$full=[IO.Path]::GetFullPath($p);$yes=$false;foreach($root in $roots){if($full.StartsWith($root.resolved.TrimEnd('\')+'\',[StringComparison]::OrdinalIgnoreCase)){$yes=$true}};if(-not $yes){throw 'Shortcut is outside standard current-user launcher folders.'}}
 function Snapshot([string]$p){
  Allowed $p;$s=$wsh.CreateShortcut($p);$f=$shell.NameSpace([IO.Path]::GetDirectoryName($p));if($null -eq $f){throw 'Shell folder unavailable.'};$item=$f.ParseName([IO.Path]::GetFileName($p));if($null -eq $item){throw 'Shell shortcut unavailable.'}
  $props=[ordered]@{};foreach($key in @('System.AppUserModel.ID','System.AppUserModel.RelaunchCommand','System.AppUserModel.RelaunchIconResource','System.AppUserModel.RelaunchDisplayNameResource')){$props[$key]=[string]$item.ExtendedProperty($key)}
  return @{path=[IO.Path]::GetFullPath($p);target=[string]$s.TargetPath;arguments=[string]$s.Arguments;working_directory=[string]$s.WorkingDirectory;icon=[string]$s.IconLocation;description=[string]$s.Description;hotkey=[string]$s.Hotkey;window_style=[int]$s.WindowStyle;app_properties=$props}
 }
 if($r.operation -eq 'inventory'){
  $links=@();$warnings=@($rootWarnings);$queue=[Collections.Queue]::new();foreach($root in $roots){$queue.Enqueue(@{path=$root.resolved;depth=0;desktop=($root.logical -ieq [Environment]::GetFolderPath('DesktopDirectory'))})};$dirs=0;$files=0
  while($queue.Count){$node=$queue.Dequeue();$dirs++;if($dirs -gt 512){throw 'Shortcut directory bound exceeded.'};try{Ordinary $node.path
   foreach($f in [IO.Directory]::EnumerateFiles($node.path,'*.lnk')){$files++;if($files -gt 512){throw 'Shortcut inventory bound exceeded.'};try{$v=Snapshot $f;if($v.target -ieq $r.oldexe){$links+=,$v}}catch{if([IO.Path]::GetFileName($f) -match '(?i)optimus'){$warnings+='An Optimus-named shortcut could not be inspected and was preserved.'}}}
   if($node.depth -lt 8 -and -not $node.desktop){foreach($d in [IO.Directory]::EnumerateDirectories($node.path)){$i=Get-Item -LiteralPath $d -Force;if(-not ($i.Attributes -band [IO.FileAttributes]::ReparsePoint)){$queue.Enqueue(@{path=$d;depth=$node.depth+1;desktop=$false})}}}
   }catch{if($_.Exception.Message -match 'bound exceeded'){throw};$warnings+='A launcher subfolder was unavailable and was preserved: '+[string]$node.path}
  }
  @{ok=$true;links=$links;warnings=$warnings;root_bindings=@($roots)}|ConvertTo-Json -Depth 12 -Compress
 }elseif($r.operation -eq 'read'){@{ok=$true;link=(Snapshot $r.path)}|ConvertTo-Json -Depth 12 -Compress}
 else{
  $v=Snapshot $r.path;$hash=(Get-FileHash -LiteralPath $r.path -Algorithm SHA256).Hash.ToLowerInvariant()
  if($hash -cne $r.before_sha256 -or $v.target -ine $r.oldexe -or $v.arguments -cne ''){throw 'Shortcut changed before retarget.'}
  if($v.app_properties['System.AppUserModel.RelaunchCommand'] -or $v.app_properties['System.AppUserModel.RelaunchIconResource'] -or $v.app_properties['System.AppUserModel.RelaunchDisplayNameResource']){throw 'Complex relaunch metadata is preserved for manual review.'}
  Ordinary $r.newexe;if([IO.Path]::GetFileName($r.newexe) -ine 'Optimus.exe'){throw 'Installed Optimus executable required.'}
  $s=$wsh.CreateShortcut($r.path);$s.TargetPath=$r.newexe;$s.WorkingDirectory=[IO.Path]::GetDirectoryName($r.newexe)
  if(-not $v.icon -or $v.icon -ieq ($r.oldexe+',0')){$s.IconLocation=$r.newexe+',0'}
  $s.Save();@{ok=$true;link=(Snapshot $r.path)}|ConvertTo-Json -Depth 12 -Compress
 }
}catch{@{ok=$false;error=$_.Exception.Message}|ConvertTo-Json -Compress;exit 1}
"""


_STARTUP_PS = r"""
$ErrorActionPreference='Stop'
[Console]::OutputEncoding=[Text.UTF8Encoding]::new($false)
[Console]::InputEncoding=[Text.UTF8Encoding]::new($false)
function Ordinary([string]$p){if($p -notmatch '^[A-Za-z]:\\' -or $p -match '[\x00-\x1f%]'){throw 'A local installed path is required.'};$at=[IO.Path]::GetFullPath($p);while($at){$i=Get-Item -LiteralPath $at -Force;if($i.Attributes -band [IO.FileAttributes]::ReparsePoint){throw 'Indirect launch path refused.'};$at=[IO.Path]::GetDirectoryName($at)};if(-not [IO.File]::Exists($p)){throw 'Installed launch executable is absent.'}}
function Digest([string]$s){$h=[Security.Cryptography.SHA256]::Create();try{return ([BitConverter]::ToString($h.ComputeHash([Text.Encoding]::UTF8.GetBytes($s)))).Replace('-','').ToLowerInvariant()}finally{$h.Dispose()}}
function SameUser([string]$user,[string]$sid){if($user -ceq $sid){return $true};try{return ([Security.Principal.NTAccount]::new($user)).Translate([Security.Principal.SecurityIdentifier]).Value -ceq $sid}catch{return $false}}
function Owned($task,$manifest,[string]$sid){
 if($null -eq $task -or -not $task.Enabled){throw 'Existing enabled startup task required.'};$d=$task.Definition
 if($d.RegistrationInfo.Description -cne ('Optimus sign-in launcher v1 '+$manifest.owner_token) -or $d.RegistrationInfo.Source -cne 'Optimus/SignIn/1' -or -not (SameUser $d.Principal.UserId $sid) -or $d.Principal.RunLevel -ne 0 -or $d.Principal.LogonType -ne 3 -or $d.Actions.Count -ne 1 -or $d.Triggers.Count -ne 1){throw 'Existing startup ownership is not exact.'}
 $t=$d.Triggers.Item(1);$a=$d.Actions.Item(1)
 if($t.Type -ne 9 -or -not $t.Enabled -or -not (SameUser $t.UserId $sid) -or $t.Delay -cne 'PT1M' -or $a.Type -ne 0 -or $a.Path -ine $manifest.launch.executable -or [string]$a.Arguments -cne '' -or $a.WorkingDirectory -ine $manifest.launch.working_directory){throw 'Existing startup action or trigger changed.'}
 return $d
}
function UnchangedDefinition([string]$xml){
 $d=[Xml.XmlDocument]::new();$d.PreserveWhitespace=$false;$d.XmlResolver=$null;$d.LoadXml($xml)
 $ns=[Xml.XmlNamespaceManager]::new($d.NameTable);$ns.AddNamespace('t','http://schemas.microsoft.com/windows/2004/02/mit/task')
 foreach($name in @('Command','Arguments','WorkingDirectory')){foreach($node in @($d.SelectNodes('/t:Task/t:Actions/t:Exec/t:'+$name,$ns))){[void]$node.ParentNode.RemoveChild($node)}}
 return $d.DocumentElement.OuterXml
}
try{
 $raw=[Console]::In.ReadToEnd();if($raw.Length -gt 131072){throw 'Startup request exceeds bound.'};$r=$raw|ConvertFrom-Json
 $sid=[Security.Principal.WindowsIdentity]::GetCurrent().User.Value;$before=$r.before_manifest;$after=$r.manifest
 if($r.operation -cne 'update_existing' -or $before.schema -ne 1 -or $after.schema -ne 1 -or $before.owner_sid -cne $sid -or $after.owner_sid -cne $sid -or $before.owner_token -notmatch '^[0-9a-f]{32}$' -or $after.owner_token -cne $before.owner_token){throw 'Owned startup identity required.'}
 foreach($m in @($before,$after)){if($m.launch.arguments -isnot [Array] -or $m.launch.arguments.Count -ne 0 -or $m.launch.argument_line -cne '' -or [IO.Path]::GetFileName($m.launch.executable) -ine 'Optimus.exe' -or [IO.Path]::GetDirectoryName($m.launch.executable) -ine $m.launch.working_directory){throw 'Only an exact installed no-argument launcher is allowed.'};Ordinary $m.launch.executable}
 if($before.launch.executable -ieq $after.launch.executable -or $r.before_xml_sha256 -notmatch '^[0-9a-f]{64}$' -or $r.new_executable_sha256 -notmatch '^[0-9a-f]{64}$' -or (Get-FileHash -LiteralPath $after.launch.executable -Algorithm SHA256).Hash.ToLowerInvariant() -cne $r.new_executable_sha256){throw 'Verified distinct new executable required.'}
 $scheduler=New-Object -ComObject 'Schedule.Service';$scheduler.Connect();$folder=$scheduler.GetFolder('\');$name='Optimus-SignIn-'+$sid
 # GetTask must succeed: no absent-task path ever creates or adopts a task.
 $task=$folder.GetTask($name);$definition=Owned $task $before $sid;$oldXml=[string]$definition.XmlText
 if($oldXml.Length -gt 65536 -or (Digest $oldXml) -cne $r.before_xml_sha256){throw 'Existing native task changed after backup.'}
 $action=$definition.Actions.Item(1);$action.Path=$after.launch.executable;$action.Arguments='';$action.WorkingDirectory=$after.launch.working_directory
 # TASK_UPDATE (4) | TASK_DONT_ADD_PRINCIPAL_ACE (16): never CREATE; retain ACL.
 $folder.RegisterTaskDefinition($name,$definition,20,$sid,$null,3,$null)|Out-Null
 $readback=$folder.GetTask($name);$verified=Owned $readback $after $sid;$newXml=[string]$verified.XmlText
 if($newXml.Length -gt 65536 -or (UnchangedDefinition $oldXml) -cne (UnchangedDefinition $newXml)){throw 'Native task changed beyond the exact launch action.'}
 @{ok=$true;owner_sid=$sid;updated_existing=$true;before_xml=$oldXml;after_xml=$newXml;before_xml_sha256=(Digest $oldXml);after_xml_sha256=(Digest $newXml)}|ConvertTo-Json -Depth 12 -Compress
}catch{@{ok=$false;error=$_.Exception.Message}|ConvertTo-Json -Compress;exit 1}
"""


def _system_powershell():
    """Resolve the OS-owned native tool without trusting inherited SystemRoot."""
    from ctypes import wintypes
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    query = kernel.GetSystemDirectoryW
    query.argtypes = [wintypes.LPWSTR, wintypes.UINT]
    query.restype = wintypes.UINT
    buffer = ctypes.create_unicode_buffer(32768)
    size = query(buffer, len(buffer))
    if not 0 < size < len(buffer):
        raise OSError('The native Windows system directory is unavailable.')
    executable = Path(buffer.value) / 'WindowsPowerShell/v1.0/powershell.exe'
    if not executable.is_absolute():
        raise ValueError('The protected Windows PowerShell executable is unavailable.')
    # Windows servicing legitimately hardlinks protected executables into WinSxS.
    for index, part in enumerate((executable, *executable.parents)):
        info = part.lstat()
        if stat.S_ISLNK(info.st_mode) or getattr(info, 'st_file_attributes', 0) & 0x400:
            raise ValueError('Indirect protected Windows executable path refused.')
        if not (stat.S_ISREG(info.st_mode) if index == 0 else stat.S_ISDIR(info.st_mode)):
            raise ValueError('The protected Windows PowerShell path is not ordinary.')
    return executable


class WindowsLauncherAdapter:
    system = 'win32'

    def __init__(self, data_dir):
        self.data_dir = Path(data_dir)
        self.registration = StartupRegistration(self.data_dir, runner=self._startup_status)
        self._root_bindings = None

    def _native(self, script, request):
        executable = _system_powershell()
        environment = {key: value for key, value in os.environ.items() if key.casefold() not in ('psmodulepath', 'systemroot', 'windir')}
        environment.update(PSModulePath=str(executable.parent / 'Modules'), SystemRoot=str(executable.parents[3]), windir=str(executable.parents[3]))
        encoded = base64.b64encode(script.encode('utf-16-le')).decode()
        result = subprocess.run([str(executable), '-NoLogo', '-NoProfile', '-NonInteractive', '-EncodedCommand', encoded],
            input=json.dumps(request, separators=(',', ':')), capture_output=True, text=True, encoding='utf-8', errors='replace',
            timeout=20, env=environment, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0), check=False)
        if len(result.stdout) > 2 * 1024 ** 2:
            raise ValueError('The native launcher result exceeded its bound.')
        value = json.loads(result.stdout.lstrip('\ufeff'))
        if result.returncode != 0 or not isinstance(value, dict) or value.get('ok') is not True:
            raise ValueError('The native launcher operation failed; inspect its actual state before retrying.')
        return value

    def _startup_status(self, request):
        if request.get('operation') != 'status':
            raise ValueError('The reused sign-in helper is status-only during self-update.')
        script = _read(Path(__file__).with_name('engine') / 'OptimusStartup.ps1', 65536).decode('utf-8-sig')
        return self._native(script, request)

    def _links(self, request):
        if request.get('operation') != 'inventory':
            if self._root_bindings is None:
                raise ValueError('Inspect known launcher roots before reading or changing a link.')
            request = dict(request, expected_roots=copy.deepcopy(self._root_bindings))
        return self._native(_LINK_PS, request)

    def inventory_links(self, oldexe):
        value = self._links({'operation': 'inventory', 'oldexe': str(oldexe)})
        roots = value.get('root_bindings')
        if (not isinstance(roots, list) or len(roots) > 3
                or any(not isinstance(root, dict) or set(root) != {'logical', 'resolved'}
                    or any(not isinstance(root[key], str) or not root[key] or len(root[key]) > 32768 for key in root) for root in roots)):
            raise ValueError('Native known-folder bindings are invalid.')
        self._root_bindings = copy.deepcopy(roots)
        return value

    def read_link(self, path):
        return self._links({'operation': 'read', 'path': str(path)})['link']

    def write_link(self, path, oldexe, newexe, before_sha256):
        return self._links({'operation': 'write', 'path': str(path), 'oldexe': str(oldexe),
                            'newexe': str(newexe), 'before_sha256': before_sha256})['link']

    def startup_snapshot(self):
        registration = self.registration
        before = registration._manifest()
        native = registration.runner({'operation': 'status', 'manifest': before})
        raw = _read(registration.path, 16384) if before is not None else None
        if raw is not None and json.loads(raw) != before:
            raise ValueError('Startup ownership changed while reading.')
        xml = native.get('before_xml')
        if xml is not None and (not isinstance(xml, str) or len(xml) > _MAX_XML):
            raise ValueError('Native startup XML exceeded its bound.')
        return {'status': native['status'], 'owner_sid': native.get('owner_sid'), 'manifest': before,
                'manifest_bytes': raw, 'xml': xml}

    def retarget_startup(self, newexe, expected):
        registration = self.registration
        with registration._mutation():
            current = self.startup_snapshot()
            if current != expected:
                raise ValueError('Startup ownership or native task changed before retarget.')
            before = current['manifest']
            launch = {'executable': str(newexe), 'arguments': [], 'argument_line': '', 'working_directory': str(newexe.parent)}
            registration.launch_provider = lambda: dict(launch)
            manifest = {'schema': 1, 'owner_token': before['owner_token'], 'owner_sid': current['owner_sid'],
                        'launch': registration.launch_provider(), 'previous_launch': copy.deepcopy(before['launch'])}
            atomic_json(registration.path, manifest)
            native = self._native(_STARTUP_PS, {'operation': 'update_existing', 'before_manifest': before,
                'manifest': manifest, 'before_xml_sha256': _sha(current['xml'].encode('utf-8')),
                'new_executable_sha256': _sha(_read(newexe, 256 * 1024 ** 2))})
            if (native.get('updated_existing') is not True or native.get('owner_sid') != current['owner_sid']
                    or native.get('before_xml') != current['xml']
                    or native.get('before_xml_sha256') != _sha(current['xml'].encode('utf-8'))):
                raise ValueError('Native startup update did not establish exact existing ownership.')
            after = self.startup_snapshot()
            if (after['manifest'] != manifest or after['status'].get('owned') is not True
                    or after['status'].get('enabled') is not True or Path(after['status'].get('target', '')) != newexe
                    or after['xml'] != native.get('after_xml')
                    or _sha(after['xml'].encode('utf-8')) != native.get('after_xml_sha256')):
                raise ValueError('Native sign-in retarget could not be verified.')
            return {'before': expected, 'after': after, 'native_receipt': dict(native,
                _before_manifest=before, _after_manifest=manifest, _after_status=after['status'])}


class ManagedLaunchers:
    def __init__(self, data_dir, adapter=None):
        self.data_dir = Path(data_dir)
        self.adapter = adapter

    def _adapter(self):
        if self.adapter is None:
            self.adapter = WindowsLauncherAdapter(self.data_dir)
        return self.adapter

    def retarget(self, oldexe, newexe, backup_dir, saved_settings):
        result = {'schema': 1, 'kind': 'managed-self-update-launchers', 'status': 'not_started',
            'changed': [], 'backups': [], 'manual_items': [], 'manual_action_required': False,
            'outcomeKnown': True, 'running': False, 'preferences_written': False,
            'elevation_requested': False, 'app_launched': False, 'processes_stopped': False}
        if sys.platform != 'win32' and self.adapter is None:
            return dict(result, status='unsupported', manual_action_required=True,
                        reason='Managed launcher retention is currently supported only on Windows.')
        oldexe, newexe, backup_dir = Path(oldexe), Path(newexe), Path(backup_dir)
        preferences = self.data_dir / 'preferences.json'
        if (not oldexe.is_absolute() or not newexe.is_absolute() or oldexe == newexe
                or oldexe.name.casefold() != 'optimus.exe' or newexe.name.casefold() != 'optimus.exe'):
            raise ValueError('Two distinct absolute installed Optimus executables are required.')
        oldhash, newhash = _sha(_read(oldexe, 256 * 1024 ** 2)), _sha(_read(newexe, 256 * 1024 ** 2))
        raw_preferences = _read(preferences, 2 * 1024 ** 2)
        if not isinstance(saved_settings, dict) or _canonical(json.loads(raw_preferences)) != _canonical(saved_settings):
            raise ValueError('Saved choices changed before launcher retention.')
        root = self.data_dir.resolve(strict=True)
        _ordinary_data_path(root)
        _ordinary_data_path(backup_dir)
        if not backup_dir.is_absolute() or backup_dir == root or not backup_dir.resolve().is_relative_to(root):
            raise ValueError('A new private launcher backup directory inside the configured data root is required.')
        backup_dir.mkdir(mode=0o700, parents=True)
        _ordinary_data_path(backup_dir)
        path = backup_dir / 'launchers.json'
        result.update(old_executable=str(oldexe), new_executable=str(newexe),
            old_executable_sha256=oldhash, new_executable_sha256=newhash,
            preferences_sha256=_sha(raw_preferences), backup_directory=str(backup_dir), receipt=str(path), started_at=time.time())
        adapter = self._adapter()
        deadline = time.monotonic() + 60
        def save():
            _ordinary_data_path(path, file=True)
            atomic_json(path, result)
        def unchanged_preferences():
            if time.monotonic() >= deadline:
                raise RuntimeError('Managed launcher retention reached its time budget; no new mutation was started.')
            if _read(preferences, 2 * 1024 ** 2) != raw_preferences:
                raise ValueError('Saved choices changed during launcher retention; nothing is restored or restamped.')
            if _sha(_read(oldexe, 256 * 1024 ** 2)) != oldhash or _sha(_read(newexe, 256 * 1024 ** 2)) != newhash:
                raise ValueError('A verified launcher executable changed during retention.')
        save()
        prepared_links = []
        startup = None
        try:
            inventory = adapter.inventory_links(oldexe)
            if (not isinstance(inventory, dict) or not isinstance(inventory.get('links'), list)
                    or len(inventory['links']) > _MAX_LINKS or not isinstance(inventory.get('warnings', []), list)):
                raise ValueError('Native shortcut inventory is malformed or exceeds its bound.')
            for warning in inventory.get('warnings', [])[:32]:
                result['manual_items'].append({'kind': 'shortcut', 'reason': str(warning)[:1000]})
            seen = set()
            for value in inventory['links']:
                if not isinstance(value, dict) or not isinstance(value.get('path'), str):
                    raise ValueError('Native shortcut inventory lacks an exact path.')
                target = Path(value['path'])
                if not target.is_absolute() or target.suffix.casefold() != '.lnk' or target in seen:
                    raise ValueError('Shortcut inventory has a relative, duplicate, or unsupported identity.')
                seen.add(target)
                if not _plain(value, oldexe):
                    result['manual_items'].append({'kind': 'shortcut', 'path': str(target),
                        'reason': 'Non-plain arguments, working directory or relaunch metadata were preserved for manual review.'})
                    continue
                original = _read(target, _MAX_LINK)
                if adapter.read_link(target) != value:
                    raise ValueError('A discovered shortcut changed before backup.')
                backup = backup_dir / f'link-{len(prepared_links):03d}.lnk'
                _write_new(backup, original)
                entry = {'kind': 'shortcut', 'path': str(target), 'before': copy.deepcopy(value),
                    'before_sha256': _sha(original), 'backup': str(backup), 'backup_sha256': _sha(original),
                    'status': 'backed_up'}
                result['backups'].append(entry)
                prepared_links.append(entry)
            if saved_settings.get('launch_at_sign_in') is True:
                current = adapter.startup_snapshot()
                status = current.get('status') if isinstance(current, dict) else None
                manifest = current.get('manifest') if isinstance(current, dict) else None
                launch = manifest.get('launch') if isinstance(manifest, dict) else None
                valid = (isinstance(status, dict) and status.get('available') is True and status.get('owned') is True
                    and status.get('enabled') is True and status.get('state') == 'enabled'
                    and Path(status.get('target') or '') == oldexe and isinstance(launch, dict)
                    and Path(launch.get('executable') or '') == oldexe and launch.get('arguments') == []
                    and launch.get('argument_line') == '' and Path(launch.get('working_directory') or '') == oldexe.parent
                    and isinstance(current.get('manifest_bytes'), bytes) and isinstance(current.get('xml'), str)
                    and 0 < len(current['xml']) <= _MAX_XML and current.get('owner_sid') == manifest.get('owner_sid')
                    and json.loads(current['manifest_bytes']) == manifest)
                if not valid:
                    result['manual_items'].append({'kind': 'startup',
                        'reason': 'The existing enabled exact owned sign-in task was not established; no task is created, enabled or adopted.'})
                else:
                    _write_new(backup_dir / 'startup-manifest.json', current['manifest_bytes'])
                    xml = current['xml'].encode('utf-8')
                    _write_new(backup_dir / 'startup-task.xml', xml)
                    startup = current
                    result['backups'].append({'kind': 'startup', 'manifest_backup': str(backup_dir / 'startup-manifest.json'),
                        'manifest_sha256': _sha(current['manifest_bytes']), 'xml_backup': str(backup_dir / 'startup-task.xml'),
                        'xml_sha256': _sha(xml), 'status': 'backed_up'})
            # Every selected original is durably preserved before the FIRST mutation.
            result['status'] = 'prepared'
            save()
            for entry in prepared_links:
                unchanged_preferences()
                target = Path(entry['path'])
                if (_sha(_read(target, _MAX_LINK)) != entry['before_sha256']
                        or adapter.read_link(target) != entry['before']):
                    raise ValueError('A shortcut changed after backup; it was preserved.')
                entry['status'] = 'retarget_prepared'
                result['outcomeKnown'] = False
                save()
                after = adapter.write_link(target, oldexe, newexe, entry['before_sha256'])
                fresh = adapter.read_link(target)
                expected = copy.deepcopy(entry['before'])
                expected.update(target=str(newexe), working_directory=str(newexe.parent))
                if expected['icon'] in ('', str(oldexe) + ',0'):
                    expected['icon'] = str(newexe) + ',0'
                if after != fresh or fresh != expected:
                    raise ValueError('The existing shortcut retarget did not preserve its exact metadata/readback.')
                entry.update(status='retargeted', after=copy.deepcopy(fresh), after_sha256=_sha(_read(target, _MAX_LINK)))
                result['changed'].append({'kind': 'shortcut', 'path': str(target), 'target': str(newexe)})
                result['outcomeKnown'] = True
                unchanged_preferences()
                save()
            if startup is not None:
                unchanged_preferences()
                if adapter.startup_snapshot() != startup:
                    raise ValueError('Native sign-in ownership changed after backup; it was preserved.')
                result['startup_status'] = 'retarget_prepared'
                result['outcomeKnown'] = False
                save()
                updated = adapter.retarget_startup(newexe, startup)
                final = adapter.startup_snapshot()
                status = final['status']
                if (not isinstance(updated, dict) or updated.get('after') != final
                        or status.get('owned') is not True or status.get('enabled') is not True
                        or Path(status.get('target') or '') != newexe):
                    raise ValueError('The owned native sign-in launcher retarget could not be verified.')
                result['startup_status'] = 'retargeted'
                # Exact original manifest/XML are in the backups; normalized readbacks are bounded metadata.
                result['startup_after_manifest_sha256'] = _sha(final['manifest_bytes'])
                result['startup_after_xml_sha256'] = _sha(final['xml'].encode('utf-8'))
                result['changed'].append({'kind': 'startup', 'target': str(newexe)})
                result['outcomeKnown'] = True
                unchanged_preferences()
                save()
            result['manual_action_required'] = bool(result['manual_items'])
            result['status'] = 'completed_with_manual_items' if result['manual_items'] else 'completed'
            result['finished_at'] = time.time()
            unchanged_preferences()
            save()
            return result
        except Exception as error:
            result.update(status='needs_review', manual_action_required=True, error=str(error)[:2000], finished_at=time.time())
            result['manual_items'].append({'kind': 'retention', 'reason': 'Inspect the exact preserved backups and actual launcher state; no blind retry or preference restoration is attempted.'})
            save()
            return result
