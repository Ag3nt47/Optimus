"""Reversible Windows privacy choices and native Defender security controls.

Primary references (policy scope/edition restrictions are intentional):
https://learn.microsoft.com/windows/client-management/mdm/policy-csp-privacy
https://learn.microsoft.com/windows/client-management/mdm/policy-csp-experience
https://learn.microsoft.com/windows/client-management/mdm/policy-csp-system
https://support.microsoft.com/windows/privacy/general-privacy-settings-in-windows
https://support.microsoft.com/windows/privacy/privacy-settings-for-recommendations-offers-in-windows-11
https://learn.microsoft.com/powershell/module/defender/start-mpscan
https://learn.microsoft.com/powershell/module/defender/remove-mpthreat
https://learn.microsoft.com/powershell/module/defender/set-mppreference
https://learn.microsoft.com/powershell/module/netsecurity/set-netfirewallprofile

AdvertisingInfo/ContentDeliveryManager preference values are not a published
stable policy API: only existing recognizable DWORD preferences are changed.
Missing preferences are reported unsupported. A registry readback verifies the
written preference, not that every application obeys it or stops all sharing.
No Defender exclusion, threat-action preference, service, hosts entry, network
blocklist, SmartScreen setting or Windows Update protection is weakened.
"""
from __future__ import annotations

import base64
import copy
import json
import math
import os
from pathlib import Path
import re
import stat
import subprocess
import threading
import time
import uuid

from .workflow import atomic_json

SOURCES = [
    'https://learn.microsoft.com/windows/client-management/mdm/policy-csp-privacy',
    'https://learn.microsoft.com/windows/client-management/mdm/policy-csp-experience',
    'https://learn.microsoft.com/windows/client-management/mdm/policy-csp-system',
    'https://support.microsoft.com/windows/privacy/general-privacy-settings-in-windows',
    'https://support.microsoft.com/windows/privacy/privacy-settings-for-recommendations-offers-in-windows-11',
    'https://learn.microsoft.com/powershell/module/defender/start-mpscan',
    'https://learn.microsoft.com/powershell/module/defender/remove-mpthreat',
    'https://learn.microsoft.com/powershell/module/defender/set-mppreference',
    'https://learn.microsoft.com/powershell/module/netsecurity/set-netfirewallprofile',
]
_CONTROLS = {
    'advertising_id': dict(hive='CurrentUser', path=r'Software\Microsoft\Windows\CurrentVersion\AdvertisingInfo',
        name='Enabled', desired=0, existing_only=True, scope='user', master='privacy_advertising_enabled', flag='privacy_disable_advertising_id', minimum_build=10240),
    'tailored_experiences': dict(hive='CurrentUser', path=r'Software\Policies\Microsoft\Windows\CloudContent',
        name='DisableTailoredExperiencesWithDiagnosticData', desired=1, scope='user', policy=True, master='privacy_advertising_enabled', flag='privacy_disable_tailored_experiences', minimum_build=15063),
    'optional_diagnostics': dict(hive='LocalMachine', path=r'Software\Policies\Microsoft\Windows\DataCollection',
        name='AllowTelemetry', desired=1, scope='device', policy=True, master='privacy_diagnostics_enabled', flag='privacy_disable_optional_diagnostics', minimum_build=10240),
    'app_diagnostics': dict(hive='LocalMachine', path=r'Software\Policies\Microsoft\Windows\AppPrivacy',
        name='LetAppsGetDiagnosticInfo', desired=2, scope='device', policy=True, master='privacy_diagnostics_enabled', flag='privacy_disable_app_diagnostics', minimum_build=15063),
}
for _name in ('SilentInstalledAppsEnabled', 'SubscribedContent-338388Enabled', 'SubscribedContent-338389Enabled',
              'SubscribedContent-353694Enabled', 'SubscribedContent-353696Enabled'):
    _CONTROLS['suggestions_' + _name] = dict(hive='CurrentUser', path=r'Software\Microsoft\Windows\CurrentVersion\ContentDeliveryManager',
        name=_name, desired=0, existing_only=True, scope='user', master='privacy_advertising_enabled', flag='privacy_disable_suggestions', minimum_build=10240)
_ID = re.compile(r'^[a-f0-9]{32}$')
MITRE_MITIGATIONS = {
    'M1049': {'id': 'M1049', 'name': 'Antivirus/Antimalware', 'url': 'https://attack.mitre.org/mitigations/M1049/'},
    'M1037': {'id': 'M1037', 'name': 'Filter Network Traffic', 'url': 'https://attack.mitre.org/mitigations/M1037/'},
    'M1051': {'id': 'M1051', 'name': 'Update Software', 'url': 'https://attack.mitre.org/mitigations/M1051/'},
}
_ACTIONS = {'enable_defender_realtime', 'enable_firewall', 'update_defender_signatures',
            'remediate_defender_threats', 'review_security', 'review_windows_update', 'restart_required'}

_POWERSHELL = r'''
$ErrorActionPreference='Stop';$ProgressPreference='SilentlyContinue'
[Console]::OutputEncoding=New-Object Text.UTF8Encoding($false)
$P=[Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('__PAYLOAD__'))|ConvertFrom-Json
$C=[Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('__CONTROLS__'))|ConvertFrom-Json
function Is-Admin {([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)}
function Context {
 $v=Get-ItemProperty 'HKLM:\SOFTWARE\Microsoft\Windows NT\CurrentVersion'
 [pscustomobject]@{platform='Windows';build=[int]$v.CurrentBuildNumber;edition=[string]$v.EditionID;is_admin=(Is-Admin)}
}
function Read-Control($id) {
 $s=$C.$id;if($null -eq $s){throw 'Unknown privacy control.'}
 $h=[Microsoft.Win32.RegistryHive]([Enum]::Parse([Microsoft.Win32.RegistryHive],[string]$s.hive))
 $view=if([Environment]::Is64BitOperatingSystem){[Microsoft.Win32.RegistryView]::Registry64}else{[Microsoft.Win32.RegistryView]::Default}
 $base=[Microsoft.Win32.RegistryKey]::OpenBaseKey($h,$view)
 try{$key=$base.OpenSubKey([string]$s.path,$false);if($null -eq $key){return @{exists=$false}}
  try{if($key.GetValueNames() -cnotcontains [string]$s.name){return @{exists=$false}}
   $kind=[string]$key.GetValueKind([string]$s.name)
   $value=if($kind -eq 'DWord'){[int64]$key.GetValue([string]$s.name)}else{$null}
   return @{exists=$true;type=$kind;value=$value}
  }finally{$key.Dispose()}
 }finally{$base.Dispose()}
}
function Same-Value($a,$b) {
 if($a.exists -ne $b.exists){return $false}
 if(-not $a.exists){return $true}
 return $a.type -ceq $b.type -and $a.value -eq $b.value
}
function Defender {
 try{$d=Get-MpComputerStatus -ErrorAction Stop
  return @{available=$true;service_enabled=[bool]$d.AMServiceEnabled;antivirus_enabled=[bool]$d.AntivirusEnabled;
   realtime_enabled=[bool]$d.RealTimeProtectionEnabled;running_mode=[string]$d.AMRunningMode;
   signature_age_days=[int]$d.AntivirusSignatureAge;tamper_protected=[bool]$d.IsTamperProtected;
   quick_scan_end=if($d.QuickScanEndTime){([datetime]$d.QuickScanEndTime).ToUniversalTime().ToString('o')}else{''}}
 }catch{return @{available=$false;error='Defender status is unavailable.'}}
}
function Providers($class) {
 try{return @{complete=$true;items=@(Get-CimInstance -Namespace root/SecurityCenter2 -ClassName $class -ErrorAction Stop|ForEach-Object{[string]$_.displayName})}}
 catch{return @{complete=$false;items=@()}}
}
function Active-Defender {
 $d=Defender
 return $d.available -eq $true -and $d.service_enabled -eq $true -and $d.antivirus_enabled -eq $true -and $d.running_mode -ceq 'Normal'
}
function No-OtherProvider($class) {
 $p=Providers $class
 return $p.complete -eq $true -and @($p.items|Where-Object{$_ -notmatch '^((Microsoft Defender|Windows Defender|Windows Security)( Antivirus| Firewall)?|Windows Firewall)$'}).Count -eq 0
}
function Threats {
 $active=@(Get-MpThreat -ErrorAction Stop|Where-Object{$_.IsActive -eq $true})
 $items=@($active|Select-Object -First 200|ForEach-Object{
  @{id=[int64]$_.ThreatID;name=[string]$_.ThreatName;active=$true}})
 return @{complete=$true;items=$items;active_count=$active.Count;truncated=($active.Count -gt 200)}
}
function Security-Status {
 $d=Defender;$av=Providers 'AntiVirusProduct';$fwProviders=Providers 'FirewallProduct'
 $f=@{complete=$false;profiles=@()}
 try{$profiles=@(Get-NetFirewallProfile -PolicyStore ActiveStore -ErrorAction Stop|ForEach-Object{
  @{name=[string]$_.Name;enabled=([string]$_.Enabled -eq 'True');default_inbound=[string]$_.DefaultInboundAction;default_outbound=[string]$_.DefaultOutboundAction}})
  $f=@{complete=($profiles.Count -eq 3);profiles=$profiles}
 }catch{}
 $pending=$null
 try{$u=New-Object -ComObject Microsoft.Update.SystemInfo;$pending=[bool]$u.RebootRequired}catch{}
 $t=@{complete=$false;items=@();active_count=$null};if($d.available){try{$t=Threats}catch{}}
 return @{context=(Context);defender=$d;antivirus_providers=$av;firewall_providers=$fwProviders;firewall=$f;reboot_required=$pending;threats=$t}
}
try {
 $result=switch('__OPERATION__'){
 'context' {Context;break}
 'registry_read' {Read-Control ([string]$P.control);break}
 'registry_write' {
  $s=$C.([string]$P.control);if($null -eq $s){throw 'Unknown privacy control.'}
  if($s.scope -eq 'device' -and -not(Is-Admin)){throw [UnauthorizedAccessException]::new('Administrator access is required.')}
  $before=Read-Control ([string]$P.control)
  if(-not(Same-Value $before $P.expected)){throw 'The privacy value changed since its snapshot.'}
  $h=[Microsoft.Win32.RegistryHive]([Enum]::Parse([Microsoft.Win32.RegistryHive],[string]$s.hive))
  $view=if([Environment]::Is64BitOperatingSystem){[Microsoft.Win32.RegistryView]::Registry64}else{[Microsoft.Win32.RegistryView]::Default}
  $base=[Microsoft.Win32.RegistryKey]::OpenBaseKey($h,$view)
  try{$key=$base.CreateSubKey([string]$s.path)
   try{if($P.remove -eq $true){$key.DeleteValue([string]$s.name,$false)}else{$key.SetValue([string]$s.name,[int]$P.value,[Microsoft.Win32.RegistryValueKind]::DWord)}}finally{$key.Dispose()}
  }finally{$base.Dispose()}
  @{ok=$true;current=(Read-Control ([string]$P.control))};break
 }
 'security_status' {Security-Status;break}
 'quick_scan' {
  if(-not(Is-Admin)){throw [UnauthorizedAccessException]::new('Administrator access is required.')}
  if(-not(Active-Defender)){throw 'Defender is not the active antivirus provider.'}
  $started=[datetime]::UtcNow
  Start-MpScan -ScanType QuickScan -ErrorAction Stop|Out-Null
  $d=Defender;$end=[datetime]::MinValue
  if($d.quick_scan_end){$end=[datetime]::Parse($d.quick_scan_end).ToUniversalTime()}
  @{ok=$true;completion_verified=($end -ge $started);defender=$d;threats=(Threats)};break
 }
 'enable_defender_realtime' {
  if(-not(Is-Admin)){throw [UnauthorizedAccessException]::new('Administrator access is required.')}
  if(-not(Active-Defender) -or -not(No-OtherProvider 'AntiVirusProduct')){throw 'The active antivirus provider changed or another provider is registered.'}
  Set-MpPreference -DisableRealtimeMonitoring $false -ErrorAction Stop
  @{ok=$true;defender=(Defender)};break
 }
 'enable_firewall' {
  if(-not(Is-Admin)){throw [UnauthorizedAccessException]::new('Administrator access is required.')}
  if(-not(No-OtherProvider 'FirewallProduct')){throw 'Another firewall provider is registered or provider inventory is unknown.'}
  $current=@(Get-NetFirewallProfile -PolicyStore ActiveStore -ErrorAction Stop)
  foreach($fwItem in $current){if([string]$fwItem.Name -notin @('Domain','Private','Public')){throw 'Unknown firewall profile.'}}
  if($current.Count -ne 3){throw 'Incomplete firewall inventory.'}
  foreach($fwItem in $current){if([string]$fwItem.Enabled -eq 'False'){Set-NetFirewallProfile -Name $fwItem.Name -Enabled True -ErrorAction Stop}}
  @{ok=$true;security=(Security-Status)};break
 }
 'update_defender_signatures' {
  if(-not(Is-Admin)){throw [UnauthorizedAccessException]::new('Administrator access is required.')}
  if(-not(Active-Defender)){throw 'Defender is not the active antivirus provider.'}
  Update-MpSignature -ErrorAction Stop|Out-Null
  @{ok=$true;defender=(Defender)};break
 }
 'remediate_defender_threats' {
  if(-not(Is-Admin)){throw [UnauthorizedAccessException]::new('Administrator access is required.')}
  if(-not(Active-Defender)){throw 'Defender is not the active antivirus provider.'}
  $before=Threats
  if($before.active_count -gt 0){Remove-MpThreat -ErrorAction Stop|Out-Null}
  @{ok=$true;before_count=$before.active_count;threats=(Threats)};break
 }
 'review_security' {Start-Process 'windowsdefender:';@{ok=$true;opened='windowsdefender:'};break}
 'review_windows_update' {Start-Process 'ms-settings:windowsupdate';@{ok=$true;opened='ms-settings:windowsupdate'};break}
 'restart_required' {Start-Process 'ms-settings:windowsupdate';@{ok=$true;opened='ms-settings:windowsupdate'};break}
 default {throw 'Unsupported system privacy operation.'}
 }
 $result|ConvertTo-Json -Depth 12 -Compress
} catch {
 @{ok=$false;error_type=$_.Exception.GetType().Name;permission_denied=($_.Exception -is [UnauthorizedAccessException]);error='Windows refused or could not complete the requested operation.'}|ConvertTo-Json -Compress
 exit 1
}
'''


def _cancelled(event):
    return event is not None and event.is_set()


def _script(operation, payload):
    if operation not in {'context', 'registry_read', 'registry_write', 'security_status', 'quick_scan'} | _ACTIONS:
        raise ValueError('Unknown native operation.')
    encode = lambda value: base64.b64encode(json.dumps(value, separators=(',', ':'), allow_nan=False).encode('utf-8')).decode('ascii')
    return _POWERSHELL.replace('__OPERATION__', operation).replace('__PAYLOAD__', encode(payload or {})).replace('__CONTROLS__', encode(_CONTROLS))


def _native_request(operation, payload=None, *, timeout=45, cancel_event=None):
    if _cancelled(cancel_event):
        return {'ok': False, 'cancelled': True, 'may_continue': False}
    if os.name != 'nt':
        return {'ok': False, 'unsupported': True, 'error': 'Windows is required.'}
    executable = Path(os.environ.get('SystemRoot', r'C:\Windows')) / 'System32/WindowsPowerShell/v1.0/powershell.exe'
    process = subprocess.Popen([str(executable), '-NoLogo', '-NoProfile', '-NonInteractive', '-Command', _script(operation, payload)],
        stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding='utf-8', errors='replace',
        creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    deadline = time.monotonic() + timeout
    while True:
        cancelled, expired = _cancelled(cancel_event), time.monotonic() >= deadline
        if cancelled or expired:
            if process.poll() is None:
                process.terminate()  # Only this PowerShell host, never Defender/service processes.
            try:
                process.communicate(timeout=3)
            except subprocess.TimeoutExpired:
                process.kill()
                process.communicate(timeout=3)
            return {'ok': False, 'cancelled': cancelled, 'timed_out': expired, 'may_continue': operation not in ('context', 'registry_read', 'security_status'),
                    'error': 'The request stopped waiting. A Windows antivirus operation may continue under Windows control.'}
        try:
            output, _stderr = process.communicate(timeout=min(0.2, max(0.01, deadline - time.monotonic())))
            if len(output) > 1024 * 1024:
                raise ValueError('The native result exceeded its report limit.')
            value = json.loads(output.strip())
            if not isinstance(value, dict):
                raise ValueError('Windows returned an invalid result.')
            if process.returncode != 0 and value.get('ok') is not False:
                return {'ok': False, 'error': 'Windows returned an unsuccessful native status.'}
            return value
        except subprocess.TimeoutExpired:
            continue


def _snapshot(value):
    if not isinstance(value, dict) or type(value.get('exists')) is not bool:
        raise ValueError('The registry snapshot is invalid.')
    if value['exists'] is False:
        return {'exists': False}
    if value.get('type') != 'DWord' or type(value.get('value')) is not int or not 0 <= value['value'] <= 3:
        raise ValueError('The existing privacy preference has an unsupported type or value.')
    return {'exists': True, 'type': 'DWord', 'value': value['value']}


def _ordinary(path):
    for part in (path, *path.parents):
        try:
            info = part.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(info.st_mode) or getattr(info, 'st_file_attributes', 0) & 0x400:
            raise ValueError('Indirect system privacy history paths are not supported.')


def _active(status):
    d = status.get('defender', {})
    return isinstance(d, dict) and all(d.get(key) is True for key in ('available', 'service_enabled', 'antivirus_enabled')) and d.get('running_mode') == 'Normal'


def _other_providers(status, field):
    providers = status.get(field)
    if not isinstance(providers, dict) or providers.get('complete') is not True or not isinstance(providers.get('items'), list):
        return None
    return [name for name in providers['items'] if not isinstance(name, str) or not re.fullmatch(r'(?:(Microsoft Defender|Windows Defender|Windows Security)( Antivirus| Firewall)?|Windows Firewall)', name, re.I)]


def _recommendations(status):
    result = []
    def add(identity, title, reason, admin=False):
        mitigation = 'M1037' if identity == 'enable_firewall' else 'M1051' if identity in ('review_windows_update', 'restart_required') else 'M1049'
        result.append({'id': identity, 'title': title, 'reason': reason, 'action': identity, 'requires_admin': admin,
                       'mitigations': [copy.deepcopy(MITRE_MITIGATIONS[mitigation])]})
    defender = status.get('defender') if isinstance(status.get('defender'), dict) else {}
    if _active(status) and defender.get('realtime_enabled') is False and _other_providers(status, 'antivirus_providers') == []:
        add('enable_defender_realtime', 'Enable Microsoft Defender real-time protection', 'Defender is active but its real-time protection is off. Existing third-party antivirus providers will not be replaced.', True)
    elif not _active(status) or defender.get('realtime_enabled') is not True:
        add('review_security', 'Review antivirus protection in Windows Security', 'Protection is disabled, provided by another product, or cannot be fully verified. Review the active provider without overriding it.')
    age = defender.get('signature_age_days')
    if _active(status) and type(age) is int and age >= 2:
        add('update_defender_signatures', 'Update Microsoft Defender security intelligence', 'The reported antivirus signatures are at least two days old. Download current signatures using Defender.', True)
    firewall = status.get('firewall', {})
    if isinstance(firewall, dict) and firewall.get('complete') is True and isinstance(firewall.get('profiles'), list):
        disabled = [row for row in firewall['profiles'] if isinstance(row, dict) and row.get('enabled') is False]
        if disabled and _other_providers(status, 'firewall_providers') == []:
            add('enable_firewall', 'Enable disabled Windows Firewall profiles', 'One or more Domain, Private or Public profiles are off. Existing rules and inbound/outbound defaults remain unchanged.', True)
        elif disabled:
            add('review_security', 'Review firewall protection in Windows Security', 'Windows Firewall is off and a different provider is registered or provider status is unknown. Review the current provider before changing protection.')
    else:
        add('review_security', 'Review firewall protection in Windows Security', 'Firewall protection could not be fully verified.')
    count = status.get('active_threat_count')
    if _active(status) and type(count) is int and count > 0:
        add('remediate_defender_threats', 'Remediate Defender-detected active threats', str(count) + ' active threat(s) were identified by Defender. Use its standard remediation; Optimus will not delete arbitrary files or change threat-action preferences.', True)
    if status.get('reboot_required') is True:
        add('restart_required', 'Review the pending Windows restart', 'Windows Update reports a pending restart. Open Windows Update and choose when to restart; Optimus will not reboot automatically.')
    unique = {}
    for item in result:
        unique.setdefault(item['id'], item)
    return list(unique.values())


class SystemPrivacy:
    """Native operations are injectable for tests; no work runs on construction."""
    def __init__(self, data_dir, runner=None):
        self.directory = Path(data_dir) / 'system-privacy'
        self.runner = runner or _native_request
        self.lock = threading.RLock()

    def _call(self, operation, payload=None, *, timeout=45, cancel_event=None):
        if _cancelled(cancel_event):
            return {'ok': False, 'cancelled': True}
        result = self.runner(operation, payload or {}, timeout=timeout, cancel_event=cancel_event)
        if not isinstance(result, dict):
            raise ValueError('The Windows operation returned invalid metadata.')
        return result

    def _journal(self, report):
        _ordinary(self.directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        path = self.directory / (report['id'] + '.json')
        _ordinary(path)
        if path.exists() and path.lstat().st_nlink != 1:
            raise ValueError('Linked system privacy history is not supported.')
        atomic_json(path, report)
        return path

    def history(self):
        """Bounded, read-only summaries; restore independently revalidates records."""
        report = {'items': [], 'errors': [], 'complete': True, 'failed_items': 0}
        try:
            _ordinary(self.directory)
            if not self.directory.exists():
                return report
            if not self.directory.is_dir():
                raise ValueError('The private history location is not a directory.')
            remaining = 20 * 1024 * 1024
            with self.lock, os.scandir(self.directory) as entries:
                for index, entry in enumerate(entries):
                    if index >= 1000:
                        report['errors'].append('The private history review limit was reached; additional records were not read.')
                        break
                    if not entry.name.endswith('.json'):
                        continue
                    try:
                        path = self.directory / entry.name
                        _ordinary(path)
                        info = path.lstat()
                        if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
                                or not _ID.fullmatch(path.stem) or info.st_size > 1024 * 1024):
                            raise ValueError('Unsupported history record.')
                        if info.st_size > remaining:
                            report['errors'].append('The private history size limit was reached; additional records were not read.')
                            break
                        with path.open('rb') as stream:
                            opened = os.fstat(stream.fileno())
                            if (opened.st_dev, opened.st_ino, opened.st_nlink) != (info.st_dev, info.st_ino, 1):
                                raise ValueError('The history record identity changed.')
                            raw = stream.read(min(1024 * 1024 + 1, remaining + 1))
                        _ordinary(path)
                        current = path.lstat()
                        if (current.st_dev, current.st_ino, current.st_nlink) != (info.st_dev, info.st_ino, 1) or len(raw) > 1024 * 1024:
                            raise ValueError('The history record identity or size changed.')
                        remaining -= len(raw)
                        saved = json.loads(raw)
                        if not isinstance(saved, dict) or saved.get('id') != path.stem:
                            raise ValueError('Invalid history identity.')
                        if saved.get('kind') != 'privacy':
                            continue
                        count, created = saved.get('changed_count'), saved.get('created_at')
                        if (type(saved.get('version')) is not int or saved['version'] != 1
                                or type(count) is not int or not 0 <= count <= len(_CONTROLS)
                                or type(created) not in (int, float) or not 0 <= created <= 253402300799 or not math.isfinite(created)
                                or saved.get('status') not in ('running', 'completed', 'completed_with_errors', 'cancelled')):
                            raise ValueError('Invalid privacy history summary.')
                        if count:
                            report['items'].append({key: saved[key] for key in ('id', 'kind', 'status', 'created_at', 'changed_count')})
                    except (OSError, ValueError, TypeError):
                        report['errors'].append('A private history record could not be verified and was omitted.')
        except (OSError, ValueError):
            report['errors'].append('The private history location could not be verified; no records were read.')
        report['items'].sort(key=lambda item: (item['created_at'], item['id']), reverse=True)
        report['failed_items'] = len(report['errors'])
        report['complete'] = not report['errors']
        return report

    def apply_privacy(self, settings, cancel_event=None):
        keys = {'privacy_advertising_enabled', 'privacy_diagnostics_enabled'} | {control['flag'] for control in _CONTROLS.values()}
        if not isinstance(settings, dict) or any(key in settings and type(settings[key]) is not bool for key in keys):
            raise ValueError('Privacy choices must be explicit booleans.')
        selected = [key for key, control in _CONTROLS.items() if settings.get(control['master'], False) and settings.get(control['flag'], True)]
        report = {'id': uuid.uuid4().hex, 'version': 1, 'kind': 'privacy', 'status': 'running', 'created_at': time.time(),
                  'items': [], 'errors': [], 'failed_items': 0, 'changed_count': 0, 'cancelled': False, 'sources': SOURCES,
                  'note': 'These Windows privacy controls reduce selected advertising/diagnostic uses. Required Windows diagnostic data, security protection and third-party app/account privacy choices remain separate. Existing content-delivery preferences are version dependent and do not guarantee that all suggested apps or reinstallations are prevented.'}
        if not selected or _cancelled(cancel_event):
            report.update(status='cancelled' if _cancelled(cancel_event) else 'completed', cancelled=_cancelled(cancel_event))
            return report
        with self.lock:
            context = self._call('context', cancel_event=cancel_event)
            self._journal(report)
            for key in selected:
                if _cancelled(cancel_event):
                    report['cancelled'] = True
                    break
                control = _CONTROLS[key]
                item = {'id': key, 'scope': control['scope'], 'status': 'prepared', 'desired': control['desired']}
                report['items'].append(item)
                try:
                    if context.get('platform') != 'Windows' or type(context.get('build')) is not int or context['build'] < control['minimum_build']:
                        item.update(status='unsupported', reason='This Windows version does not expose the selected supported control.')
                    elif control.get('policy') and not str(context.get('edition', '')).startswith(('Professional', 'Enterprise', 'Education', 'IoTEnterprise')):
                        item.update(status='unsupported', reason='This policy is not documented for the installed Windows edition; use Windows privacy Settings.')
                    elif control['scope'] == 'device' and context.get('is_admin') is not True:
                        item.update(status='requires_admin', reason='This is a device-wide Windows policy and needs an administrator session. No elevation was requested.')
                    else:
                        before = _snapshot(self._call('registry_read', {'control': key}, cancel_event=cancel_event))
                        allowed = {0, 1, 2, 3} if key == 'optional_diagnostics' else {0, 1, 2} if key == 'app_diagnostics' else {0, 1}
                        if before['exists'] and before['value'] not in allowed:
                            raise ValueError('The existing preference value is not a recognized setting.')
                        item['before'] = before
                        if control.get('existing_only') and not before['exists']:
                            item.update(status='unsupported', reason='This version-dependent user preference is absent. No undocumented value was invented; review Windows Recommendations and offers.')
                        elif key == 'optional_diagnostics' and before.get('value') == 0:
                            item.update(status='already_configured', changed=False, reason='An existing more restrictive diagnostic-data policy was preserved; Optimus never increases collection from Off/Security to Required.')
                        elif before.get('value') == control['desired']:
                            item.update(status='already_configured', changed=False)
                        else:
                            self._journal(report)  # Original value is durable before any registry write.
                            if _cancelled(cancel_event):
                                report['cancelled'] = True
                                item['status'] = 'cancelled'
                                break
                            outcome = self._call('registry_write', {'control': key, 'expected': before, 'value': control['desired'], 'remove': False}, cancel_event=cancel_event)
                            if outcome.get('cancelled'):
                                report['cancelled'] = True
                                item.update(status='uncertain', reason='The request stopped waiting; the journal retains the original value for guarded restore.')
                                break
                            if outcome.get('ok') is not True or _snapshot(outcome.get('current')) != {'exists': True, 'type': 'DWord', 'value': control['desired']}:
                                raise RuntimeError('Windows did not verify the selected privacy value.')
                            item.update(status='applied', changed=True)
                            report['changed_count'] += 1
                except Exception as error:
                    item.update(status='failed', error_type=type(error).__name__, reason='Windows refused, changed or could not verify this privacy control. The original value remains in private history when available.')
                if item['status'] in ('failed', 'requires_admin', 'unsupported', 'uncertain'):
                    report['failed_items'] += 1
                self._journal(report)
            report['status'] = 'cancelled' if report['cancelled'] else 'completed_with_errors' if report['failed_items'] else 'completed'
            report['finished_at'] = time.time()
            report['log_path'] = str(self._journal(report))
            return report

    def restore_privacy(self, change_id, cancel_event=None):
        if not isinstance(change_id, str) or not _ID.fullmatch(change_id):
            raise ValueError('Choose an exact privacy history identity.')
        path = self.directory / (change_id + '.json')
        _ordinary(path)
        if not path.is_file() or path.stat().st_size > 1024 * 1024 or path.stat().st_nlink != 1:
            raise ValueError('The privacy history is unavailable or unsupported.')
        saved = json.loads(path.read_text(encoding='utf-8'))
        if saved.get('id') != change_id or saved.get('kind') != 'privacy' or saved.get('version') != 1 or not isinstance(saved.get('items'), list):
            raise ValueError('The privacy history does not describe a supported change.')
        # Validate every entry before the first restore write; history never
        # supplies registry paths, arbitrary value names or executable commands.
        entries = []
        for item in saved['items']:
            if not isinstance(item, dict) or item.get('id') not in _CONTROLS or item.get('desired') != _CONTROLS[item['id']]['desired']:
                raise ValueError('The privacy history contains an unknown control.')
            if 'before' in item:
                before = _snapshot(item['before'])
                allowed = {0, 1, 2, 3} if item['id'] == 'optional_diagnostics' else {0, 1, 2} if item['id'] == 'app_diagnostics' else {0, 1}
                if before['exists'] and before['value'] not in allowed:
                    raise ValueError('The saved preference value is not a recognized setting.')
                entries.append((item['id'], before))
        report = {'id': uuid.uuid4().hex, 'kind': 'privacy-restore', 'source_id': change_id, 'status': 'running', 'items': [], 'failed_items': 0, 'cancelled': False}
        with self.lock:
            self._journal(report)
            for key, before in entries:
                if _cancelled(cancel_event):
                    report['cancelled'] = True
                    break
                expected = {'exists': True, 'type': 'DWord', 'value': _CONTROLS[key]['desired']}
                item = {'id': key, 'status': 'skipped'}
                report['items'].append(item)
                try:
                    current = _snapshot(self._call('registry_read', {'control': key}, cancel_event=cancel_event))
                    if _cancelled(cancel_event):
                        report['cancelled'] = True
                        item['status'] = 'cancelled'
                        break
                    if current == before:
                        item['reason'] = 'The original value is already present.'
                    elif current != expected:
                        item['reason'] = 'The setting changed after Optimus; newer choices were preserved.'
                    else:
                        self._journal(report)
                        outcome = self._call('registry_write', {'control': key, 'expected': expected, 'remove': not before['exists'], 'value': before.get('value', 0)}, cancel_event=cancel_event)
                        if outcome.get('cancelled'):
                            report['cancelled'] = True
                            item['status'] = 'uncertain'
                            break
                        if outcome.get('ok') is True and _snapshot(outcome.get('current')) == before:
                            item['status'] = 'restored'
                        else:
                            raise RuntimeError('Windows did not verify the restored preference.')
                except Exception as error:
                    item.update(status='failed', error_type=type(error).__name__, reason='The original value could not be restored or verified; review Windows permissions and private history.')
                    report['failed_items'] += 1
                self._journal(report)
            report['status'] = 'cancelled' if report['cancelled'] else 'completed_with_errors' if report['failed_items'] else 'completed'
            report['log_path'] = str(self._journal(report))
            return report

    def security_status(self):
        """Read local state only; no scan, signature update or online update search."""
        try:
            native = self._call('security_status')
            if native.get('ok') is False:
                raise ValueError('The local Windows security report is unavailable.')
            report = {key: copy.deepcopy(native.get(key)) for key in ('context', 'defender', 'antivirus_providers', 'firewall_providers', 'firewall', 'reboot_required')}
            threats = native.get('threats', {})
            report['active_threat_count'] = threats.get('active_count') if isinstance(threats, dict) and threats.get('complete') is True and type(threats.get('active_count')) is int else None
            report['threat_status_complete'] = (isinstance(threats, dict) and threats.get('complete') is True
                                                and type(threats.get('active_count')) is int and threats['active_count'] >= 0)
            report['errors'] = []
            for key in ('defender', 'antivirus_providers', 'firewall_providers', 'firewall'):
                value = report.get(key)
                if not isinstance(value, dict) or value.get('available', value.get('complete')) is not True:
                    report['errors'].append(key + ' status could not be fully verified.')
            if not report['threat_status_complete']:
                report['errors'].append('Defender active-threat status could not be verified; an unknown result is not zero threats.')
            report.update(status='partial' if report['errors'] else 'completed', failed_items=len(report['errors']), sources=SOURCES)
        except Exception as error:
            report = {'status': 'partial', 'errors': ['Local Windows security state could not be verified (' + type(error).__name__ + ').'],
                      'failed_items': 1, 'defender': {}, 'firewall': {}, 'antivirus_providers': {}, 'firewall_providers': {}, 'active_threat_count': None, 'sources': SOURCES}
        report['recommendations'] = _recommendations(report)
        defender = report.get('defender') if isinstance(report.get('defender'), dict) else {}
        firewall = report.get('firewall') if isinstance(report.get('firewall'), dict) else {}
        firewall_rows = firewall.get('profiles') if isinstance(firewall.get('profiles'), list) else []
        report['checks'] = [
            {'id': 'antivirus', 'status': 'enabled' if _active(report) and defender.get('realtime_enabled') is True else 'review',
             'mitigations': [copy.deepcopy(MITRE_MITIGATIONS['M1049'])],
             'description': 'Local antivirus-provider and Defender real-time state; this does not establish that every threat is detected.'},
            {'id': 'firewall', 'status': 'enabled' if firewall.get('complete') is True and len(firewall_rows) == 3 and all(isinstance(row, dict) and row.get('enabled') is True for row in firewall_rows) else 'review',
             'mitigations': [copy.deepcopy(MITRE_MITIGATIONS['M1037'])],
             'description': 'Windows Firewall profile enablement only; this does not audit every rule or prove outbound data sharing is blocked.'},
            {'id': 'windows_updates', 'status': 'pending_restart' if report.get('reboot_required') is True else 'not_scanned',
             'mitigations': [copy.deepcopy(MITRE_MITIGATIONS['M1051'])],
             'description': 'This local check does not scan for missing patches. Use the separate approved Windows security-update stage or Windows Update.'},
        ]
        report['mitre_note'] = 'These limited control mappings provide context, not complete ATT&CK coverage, an attack simulation, vulnerability assessment or compliance certification.'
        report['note'] = 'Local security checks do not prove that no spyware or data sharing exists. Windows privacy permissions and app/account choices are separate. No protection was disabled.'
        return report

    def scan_spyware(self, cancel_event=None):
        report = {'id': uuid.uuid4().hex, 'kind': 'defender-scan', 'status': 'running', 'items': [], 'errors': [], 'failed_items': 0, 'cancelled': False, 'sources': SOURCES}
        if _cancelled(cancel_event):
            return dict(report, status='cancelled', cancelled=True)
        before = self.security_status()
        report['recommendations'] = before['recommendations']
        if not _active(before) or not isinstance(before.get('context'), dict) or before['context'].get('is_admin') is not True:
            return dict(report, status='blocked', failed_items=1, reason='A verified active Microsoft Defender provider and administrator session are required. Other antivirus products are not replaced or disabled.')
        with self.lock:
            self._journal(report)
            try:
                outcome = self._call('quick_scan', timeout=900, cancel_event=cancel_event)
                report['cancelled'] = _cancelled(cancel_event) or outcome.get('cancelled') is True
                report['scan_may_continue'] = outcome.get('may_continue') is True
                if report['cancelled']:
                    report['status'] = 'cancelled'
                elif outcome.get('ok') is not True or outcome.get('completion_verified') is not True:
                    report.update(status='incomplete', failed_items=1, reason='Defender quick-scan completion was not verified. Check Windows Security; a scan may still be running.')
                else:
                    threats = outcome.get('threats', {})
                    report.update(status='completed', active_threat_count=threats.get('active_count') if isinstance(threats, dict) and threats.get('complete') is True else None,
                                  note='Defender completed a quick scan of its standard scan areas using existing Windows remediation policy. This is not a full-system scan or a guarantee that every spyware program was found. No arbitrary file was deleted by Optimus.')
                    after = self.security_status()
                    report['recommendations'] = after['recommendations']
            except Exception as error:
                report.update(status='failed', failed_items=1, error_type=type(error).__name__, reason='Windows Defender could not complete or verify the scan. Review Windows Security.')
            report['log_path'] = str(self._journal(report))
            return report

    def apply_recommendation(self, recommendation_id, cancel_event=None):
        if recommendation_id not in _ACTIONS:
            raise ValueError('Choose a supported security recommendation.')
        report = {'id': recommendation_id, 'status': 'running', 'failed_items': 0}
        if _cancelled(cancel_event):
            return dict(report, status='cancelled', cancelled=True)
        current = self.security_status()
        permitted = {item['id'] for item in current['recommendations']}
        if recommendation_id not in permitted and recommendation_id != 'review_windows_update':
            return dict(report, status='blocked', failed_items=1, reason='The recommendation no longer matches current Windows security state; no change was requested.')
        manual = recommendation_id in ('review_security', 'review_windows_update', 'restart_required')
        if not manual and (not isinstance(current.get('context'), dict) or current['context'].get('is_admin') is not True):
            return dict(report, status='blocked', failed_items=1, reason='Administrator access is required. No elevation or permission bypass was requested.')
        journal = {'id': uuid.uuid4().hex, 'kind': 'security-recommendation', 'recommendation_id': recommendation_id,
                   'status': 'prepared', 'before': current, 'items': [], 'failed_items': 0}
        with self.lock:
            self._journal(journal)
            try:
                outcome = self._call(recommendation_id, timeout=300, cancel_event=cancel_event)
                if outcome.get('cancelled') or _cancelled(cancel_event):
                    report.update(status='cancelled', cancelled=True, may_continue=outcome.get('may_continue') is True)
                elif outcome.get('ok') is not True:
                    report.update(status='failed', failed_items=1, reason='Windows declined or could not verify the selected action; policy or tamper protection may apply.')
                elif manual:
                    report.update(status='manual_action_required', opened=outcome.get('opened'), reason='Windows Settings was opened for review. No automatic reboot or provider replacement was requested.')
                else:
                    after = self.security_status()
                    remaining = {item['id'] for item in after['recommendations']}
                    if after['status'] != 'completed' or recommendation_id in remaining:
                        report.update(status='failed', failed_items=1, reason='The action returned but its intended security outcome was not verified.', after=after)
                    else:
                        report.update(status='completed', after=after)
            except Exception as error:
                report.update(status='failed', failed_items=1, error_type=type(error).__name__, reason='Windows could not complete the selected security action.')
            journal.update(status=report['status'], result=report, failed_items=report['failed_items'])
            report['log_path'] = str(self._journal(journal))
            return report
