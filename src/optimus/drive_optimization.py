"""Conservative native Windows drive optimization, never partition editing.

The native /O optimizer chooses SSD retrim or HDD optimization. Exit zero is a
reported completion, not proof of bytes reclaimed, lower fragmentation or a
speed gain. Resource limits below are operating guards, not benchmark claims.
"""
from __future__ import annotations

import copy
import ctypes
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import threading
import time
import uuid

import psutil

from .automation import CURRENT_SETUP_VERSION, RunLease
from .filesystem import _ordinary_data_path
from .speed_enhancements import _WindowsAPI, _current_owner, _private_json
from .workflow import Preferences

CADENCE_SECONDS = 7 * 86400
MINIMUM_FREE_BYTES = 2 * 1024 ** 3
HDD_MINIMUM_FREE_RATIO = .15
MAX_CPU_PERCENT = 85
MAX_MEMORY_PERCENT = 90
_ID = re.compile(r'[0-9a-f]{32}\Z')
_BUSES = {'ATA', 'SATA', 'NVMe', 'SAS'}
_QUERY = r'''
$ErrorActionPreference='Stop';$ProgressPreference='SilentlyContinue'
[Console]::OutputEncoding=New-Object Text.UTF8Encoding($false)
try {
 $processes=@(Get-CimInstance Win32_Process -ErrorAction Stop)
 if(-not $processes.Count -or -not @($processes|Where-Object{$_.ProcessId -eq $PID}).Count){throw 'Process inventory is incomplete.'}
 $busy=@($processes|Where-Object{$_.Name -match '^(defrag|dfrgui|msiexec|setup|setuphost|setupprep|TiWorker|TrustedInstaller|dism|winget|Windows11InstallationAssistant|Windows10UpgraderApp)\.exe$'}|Select-Object Name,ProcessId)
 $update=New-Object -ComObject Microsoft.Update.Session
 $installer=$update.CreateUpdateInstaller();$restart=New-Object -ComObject Microsoft.Update.SystemInfo
 $physical=@(Get-PhysicalDisk -ErrorAction Stop)
 $rows=@()
 foreach($v in @(Get-Volume -ErrorAction Stop|Where-Object{$_.DriveLetter})){
  $row=@{letter=[string]$v.DriveLetter;volume_id=[string]$v.UniqueId;filesystem=[string]$v.FileSystem;
   drive_type=[string]$v.DriveType;health=[string]$v.HealthStatus;volume_status=@($v.OperationalStatus|ForEach-Object{[string]$_});
   size_bytes=[long]$v.Size;free_bytes=[long]$v.SizeRemaining;complete=$false;media_type='Unknown'}
  try {
   $parts=@(Get-Partition -DriveLetter $v.DriveLetter -ErrorAction Stop)
   if($parts.Count -ne 1){throw 'Partition mapping is ambiguous.'}
   $disks=@(Get-Disk -Number $parts[0].DiskNumber -ErrorAction Stop)
   if($disks.Count -ne 1){throw 'Disk mapping is ambiguous.'};$d=$disks[0]
   $row.disk_id=[string]$d.UniqueId;$row.disk_number=[int]$d.Number;$row.bus=[string]$d.BusType
   $row.disk_health=[string]$d.HealthStatus;$row.disk_status=@($d.OperationalStatus|ForEach-Object{[string]$_})
   $row.read_only=([bool]$d.IsReadOnly -or [bool]$parts[0].IsReadOnly);$row.offline=[bool]$d.IsOffline
   $row.virtual=([string]$d.FriendlyName -match 'Virtual|Storage Space|VHD')
   $root=([string]$v.DriveLetter)+':\';$row.accessible=[bool](Test-Path -LiteralPath $root -ErrorAction Stop)
   $row.locked=(-not $row.accessible)
   if(Get-Command Get-BitLockerVolume -ErrorAction SilentlyContinue){
    $bitlocker=@(Get-BitLockerVolume -MountPoint $root -ErrorAction Stop)
    if($bitlocker.Count -ne 1){throw 'Encryption-lock state is ambiguous.'}
    $row.locked=([string]$bitlocker[0].LockStatus -ne 'Unlocked')
   }
   $matches=@($physical|Where-Object{
    ([string]$_.UniqueId -and [string]$_.UniqueId -eq [string]$d.UniqueId) -or
    ([string]$_.DeviceId -eq [string]$d.Number -and [string]$_.SerialNumber -and [string]$d.SerialNumber -and
     ([string]$_.SerialNumber).Trim() -eq ([string]$d.SerialNumber).Trim())})
   if($matches.Count -eq 1 -and [string]$matches[0].MediaType -in @('SSD','HDD')){$row.media_type=[string]$matches[0].MediaType}
   $row.complete=$true
  }catch{$row.reason='The volume/disk/lock mapping could not be verified.'}
  $rows+=,$row
 }
 @{complete=$true;volumes=@($rows);process_inventory_complete=$true;busy=@($busy);update_busy=[bool]$installer.IsBusy;reboot_required=[bool]$restart.RebootRequired}|ConvertTo-Json -Depth 8 -Compress
}catch{@{complete=$false;volumes=@();reason='Native drive and maintenance state could not be verified.'}|ConvertTo-Json -Compress;exit 1}
'''


def _system_directory():
    kernel = ctypes.WinDLL('kernel32.dll', use_last_error=True)
    kernel.GetSystemDirectoryW.argtypes = [ctypes.c_wchar_p, ctypes.c_uint32]
    kernel.GetSystemDirectoryW.restype = ctypes.c_uint32
    buffer = ctypes.create_unicode_buffer(32768)
    count = kernel.GetSystemDirectoryW(buffer, len(buffer))
    if not 0 < count < len(buffer):
        raise OSError('The Windows system directory is unavailable.')
    return Path(buffer.value)


class _NativeDrives:
    def __init__(self):
        self.system = _system_directory()

    def inspect(self, timeout=30):
        executable = self.system / 'WindowsPowerShell/v1.0/powershell.exe'
        environment = {key: value for key, value in os.environ.items() if key.casefold() != 'psmodulepath'}
        environment['PSModulePath'] = str(executable.parent / 'Modules')
        command = subprocess.run([str(executable), '-NoLogo', '-NoProfile', '-NonInteractive', '-Command', _QUERY],
            stdin=subprocess.DEVNULL, capture_output=True, text=True, encoding='utf-8', errors='replace',
            timeout=timeout, env=environment, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        if len(command.stdout) > 256 * 1024:
            raise ValueError('Drive inventory exceeded its bound.')
        result = json.loads(command.stdout)
        if command.returncode != 0 or not isinstance(result, dict):
            raise ValueError('Drive inventory was not completed.')
        memory = psutil.virtual_memory()
        result.update(ac=_WindowsAPI().source()['ac'], cpu_percent=psutil.cpu_percent(interval=.15),
                      memory_percent=float(memory.percent), available_memory_bytes=int(memory.available))
        return result

    def start(self, letter, log_path):
        if not re.fullmatch('[A-Z]', letter):
            raise ValueError('Select one exact drive letter.')
        with log_path.open('xb') as output:
            return subprocess.Popen([str(self.system / 'defrag.exe'), letter + ':', '/O', '/U', '/V'],
                stdin=subprocess.DEVNULL, stdout=output, stderr=subprocess.STDOUT,
                creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))


def _identity(row):
    fields = (row.get('volume_id'), row.get('disk_id'))
    if any(not isinstance(part, str) or not part.strip() or len(part) > 1024 for part in fields):
        return None
    return hashlib.sha256(json.dumps(fields, separators=(',', ':')).encode()).hexdigest()[:32]


def _eligible(row):
    if not isinstance(row, dict) or row.get('complete') is not True or _identity(row) is None:
        return 'The volume and its physical disk identity could not be verified.'
    if not isinstance(row.get('letter'), str) or not re.fullmatch('[A-Z]', row['letter']):
        return 'A single unambiguous drive letter is required.'
    if row.get('drive_type') != 'Fixed' or row.get('filesystem') not in ('NTFS', 'ReFS') or row.get('bus') not in _BUSES:
        return 'Only joined internal fixed NTFS/ReFS volumes on ATA/SATA/NVMe/SAS disks are eligible. Removable, USB, virtual and storage-pool paths are excluded.'
    if (row.get('health') != 'Healthy' or row.get('disk_health') != 'Healthy'
            or row.get('volume_status') != ['OK'] or row.get('disk_status') != ['Online']
            or row.get('read_only') is not False or row.get('offline') is not False
            or row.get('virtual') is not False or row.get('locked') is not False or row.get('accessible') is not True):
        return 'The volume must be healthy, online, accessible, unlocked and writable, with a verified nonvirtual disk.'
    free, size = row.get('free_bytes'), row.get('size_bytes')
    if type(free) is not int or type(size) is not int or not 0 <= free <= size or size <= 0:
        return 'Free-space metadata could not be verified.'
    if free < MINIMUM_FREE_BYTES:
        return 'At least 2 GiB free is required by the Optimus operating guard.'
    if row.get('media_type') != 'SSD' and free / size < HDD_MINIMUM_FREE_RATIO:
        return 'HDD or unclassified media needs at least 15% free space; the media type is never guessed.'
    return ''


def _guard(state):
    if not isinstance(state, dict) or state.get('complete') is not True or state.get('process_inventory_complete') is not True:
        return 'The native inventory or process state is incomplete.'
    if state.get('ac') is not True:
        return 'Known AC power is required; battery or unknown power was deferred.'
    if state.get('reboot_required') is not False or state.get('update_busy') is not False or state.get('busy') != []:
        return 'An optimizer, installer, servicing operation, pending restart or unknown busy state requires review.'
    cpu, memory, available = state.get('cpu_percent'), state.get('memory_percent'), state.get('available_memory_bytes')
    if (type(cpu) not in (int, float) or type(memory) not in (int, float) or type(available) is not int
            or not 0 <= cpu <= 100 or not 0 <= memory <= 100 or available < 0):
        return 'Current resource-pressure evidence is unavailable.'
    if cpu >= MAX_CPU_PERCENT or memory >= MAX_MEMORY_PERCENT or available < 1024 ** 3:
        return 'The operating guard deferred extra disk work: CPU is at least 85%, memory is at least 90%, or available memory is below 1 GiB. This is not a benchmark.'
    return ''


def _health_finding(row):
    if not isinstance(row, dict):
        return None
    states = row.get('volume_status', [])
    disk_states = row.get('disk_status', [])
    health = row.get('health')
    disk_health = row.get('disk_health')
    status_text = ', '.join(str(part) for part in states) if isinstance(states, list) else 'unavailable'
    disk_text = ', '.join(str(part) for part in disk_states) if isinstance(disk_states, list) else 'unavailable'
    adverse = health in ('Warning', 'Unhealthy') or disk_health in ('Warning', 'Unhealthy') or any(
        word in (status_text + ' ' + disk_text).casefold() for word in ('repair', 'error', 'failure', 'degraded'))
    if not adverse:
        return None
    return {'target': str(row.get('letter', '?')) + ': Windows volume status', 'status': 'needs_review',
            'reason': f'Windows reported volume health {health}; volume status {status_text}; disk health {disk_health}; disk status {disk_text}. Review the reported filesystem/volume state. This does not diagnose physical disk failure, and no repair was requested.'}


def _volume_reasons(row):
    finding = _health_finding(row)
    reasons = [finding['reason']] if finding else []
    if not isinstance(row, dict):
        return reasons + ['The volume record is invalid.']
    if row.get('bus') and row['bus'] not in _BUSES:
        reasons.append('Scope exclusion: ' + str(row['bus']) + ' is not an eligible internal disk bus.')
    if row.get('drive_type') and row['drive_type'] != 'Fixed':
        reasons.append('Scope exclusion: the volume is not a fixed local drive.')
    if row.get('filesystem') and row['filesystem'] not in ('NTFS', 'ReFS'):
        reasons.append('Scope exclusion: ' + str(row['filesystem']) + ' is outside NTFS/ReFS optimization.')
    for field, label in (('read_only', 'read-only'), ('offline', 'offline'), ('virtual', 'virtual/storage-pool'), ('locked', 'encryption-locked')):
        if row.get(field) is True:
            reasons.append('Eligibility exclusion: the volume/disk is ' + label + '.')
    specific = _eligible(row)
    if specific and specific not in reasons:
        reasons.append(specific)
    return reasons


class DriveOptimization:
    def __init__(self, backend, *, system=None, adapter=None, owner=None, clock=None, timeout=1800):
        self.backend = backend
        self.system = system or getattr(backend, 'system', None) or sys.platform
        self.directory = Path(backend.data_dir) / 'drive-optimization'
        self.adapter = adapter
        self.owner = owner or _current_owner()
        self.clock = clock or time.time
        self.timeout = timeout
        self.lock = threading.Lock()
        self._noops = {}

    def _adapter(self):
        if self.system != 'win32':
            raise ValueError('Native Windows drive optimization is unavailable on this platform.')
        if self.adapter is None:
            self.adapter = _NativeDrives()
        return self.adapter

    def _key(self, create=False):
        _ordinary_data_path(self.directory)
        path = self.directory / 'owner.json'
        if _ordinary_data_path(path, file=True) is None:
            if not create:
                return None
            _private_json(path, {'schema': 1, 'owner': self.owner, 'key': os.urandom(32).hex()})
        value = self._read(path)
        if (not isinstance(value, dict) or value.get('schema') != 1 or value.get('owner') != self.owner
                or not re.fullmatch('[a-f0-9]{64}', str(value.get('key', '')))):
            raise ValueError('Drive history has an invalid ownership anchor.')
        return bytes.fromhex(value['key'])

    def _read(self, path):
        info = _ordinary_data_path(path, file=True)
        if info is None or info.st_size > 1024 * 1024:
            raise ValueError('Drive history is missing or oversized.')
        descriptor = os.open(path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0))
        with os.fdopen(descriptor, 'rb') as stream:
            opened = os.fstat(stream.fileno())
            if (opened.st_dev, opened.st_ino, opened.st_nlink) != (info.st_dev, info.st_ino, 1):
                raise ValueError('Drive history changed identity.')
            raw = stream.read(1024 * 1024 + 1)
        if len(raw) > 1024 * 1024:
            raise ValueError('Drive history is oversized.')
        return json.loads(raw)

    @staticmethod
    def _sign(value, key):
        return hmac.new(key, json.dumps({k: v for k, v in value.items() if k != 'signature'},
            sort_keys=True, separators=(',', ':'), allow_nan=False).encode(), hashlib.sha256).hexdigest()

    def _write(self, value):
        _ordinary_data_path(self.directory)
        path = self.directory / (value['id'] + '.json')
        _ordinary_data_path(path, file=True)
        value['signature'] = self._sign(value, self._key(create=True))
        _private_json(path, value)

    def _load(self, identity):
        if not isinstance(identity, str) or not _ID.fullmatch(identity):
            raise ValueError('Choose one owned drive-operation ID.')
        key = self._key()
        if key is None:
            raise ValueError('No owned drive journal is available.')
        value = self._read(self.directory / (identity + '.json'))
        if (not isinstance(value, dict) or value.get('schema') != 1 or value.get('id') != identity
                or value.get('owner') != self.owner or value.get('platform') != self.system
                or not isinstance(value.get('items'), list) or len(value['items']) > 32
                or not hmac.compare_digest(str(value.get('signature', '')), self._sign(value, key))):
            raise ValueError('Drive history is not a valid owned operation.')
        return value

    def _records(self):
        _ordinary_data_path(self.directory)
        if not self.directory.exists():
            return []
        paths = list(self.directory.glob('*.json'))
        if len(paths) > 1001:
            raise ValueError('Drive history exceeds its bounded inventory.')
        return [self._load(path.stem) for path in paths if path.name != 'owner.json']

    def history(self):
        return [{key: row.get(key) for key in ('id', 'created_at', 'status', 'summary', 'log_path')}
                for row in sorted(self._records(), key=lambda row: row['created_at'], reverse=True)[:100]]

    def _recent(self):
        attempts = {}
        for record in self._records():
            for item in record['items']:
                if item.get('status') in ('reported_completion', 'uncertain', 'running', 'prepared') and isinstance(item.get('volume_id'), str):
                    attempts[item['volume_id']] = max(attempts.get(item['volume_id'], 0), item.get('started_at', record['created_at']))
        return attempts

    def _plan(self, state):
        reason = _guard(state)
        rows = state.get('volumes', []) if isinstance(state, dict) else []
        if not isinstance(rows, list) or len(rows) > 32:
            raise ValueError('Drive inventory exceeds its volume bound.')
        recent, seen, items = self._recent(), set(), []
        for row in rows:
            identity = _identity(row) if isinstance(row, dict) else None
            specific = _volume_reasons(row)
            if identity in seen:
                specific.append('Duplicate volume identities are ambiguous.')
            seen.add(identity)
            age = self.clock() - recent.get(identity, -CADENCE_SECONDS)
            cadence = 'A native optimizer attempt was recorded for this volume within seven days; no repeat was started.' if age < CADENCE_SECONDS else ''
            reasons = specific + ([reason] if reason else []) + ([cadence] if cadence else [])
            items.append({'id': identity or uuid.uuid4().hex, 'volume_id': identity,
                'title': 'Optimize ' + str(row.get('letter', '?')) + ': using the native media policy',
                'status': 'deferred' if reasons else 'available', 'reasons': reasons,
                'health_review': _health_finding(row) is not None,
                'reason': ' '.join(reasons) or 'Eligible internal volume. Windows /O selects SSD retrim or HDD optimization; no partition or format operation is used.',
                'before': copy.deepcopy(row), 'desired': 'Windows defrag /O'})
        if not items:
            items = [{'id': 'drive-optimization', 'title': 'Native drive optimization', 'status': 'deferred',
                      'reason': reason or 'No eligible drive-letter volumes were discovered.', 'before': None, 'desired': None}]
        return items

    def scan(self):
        try:
            state = self._adapter().inspect()
            items = self._plan(state)
        except Exception as error:
            items = [{'id': 'drive-optimization', 'title': 'Native drive optimization', 'status': 'unsupported' if self.system != 'win32' else 'deferred',
                      'reason': 'Native drive state is unavailable (' + type(error).__name__ + '). No drive change was requested.', 'before': None, 'desired': None}]
        findings = [finding for row in items if (finding := _health_finding(row.get('before'))) is not None]
        return {'schema': 1, 'platform': self.system, 'read_only': True, 'items': items, 'health_findings': findings,
                'summary': f'{sum(row["status"] == "available" for row in items)} eligible volume(s); {len(findings)} Windows-reported volume health finding(s) need review. Native optimization is separate from cleanup and partition editing.'}

    def _finish(self, report, status):
        report['status'] = status
        report['counts'] = {state: sum(item['status'] == state for item in report['items'])
                            for state in ('reported_completion', 'deferred', 'skipped', 'failed', 'uncertain')}
        report['failed_items'] = report['counts']['failed'] + report['counts']['uncertain']
        report['finished_at'] = self.clock()
        report['summary'] = ('Native drive optimization: ' + '; '.join(f'{number} {state.replace("_", " ")}' for state, number in report['counts'].items() if number)
                             + f'. {len(report.get("health_findings", []))} Windows-reported volume health finding(s) need review. Completion is an OS report, not proof of space reclaimed, defragmentation percentage or a speed gain.')
        return report

    def apply(self, settings, cancel_event=None, progress_callback=None):
        validator = getattr(getattr(self.backend, 'preference_store', None), 'validate', Preferences.validate)
        config = validator(settings)
        report = {'schema': 1, 'id': uuid.uuid4().hex, 'owner': self.owner, 'platform': self.system,
                  'created_at': self.clock(), 'status': 'running', 'items': [], 'cancelled': False,
                  'log_path': None, 'failed_items': 0, 'restore_available': False}
        def no_change(reason, status='deferred'):
            report['items'] = [{'id': 'drive-optimization', 'title': 'Native drive optimization', 'status': status, 'reason': reason}]
            self._noops[report['id']] = []
            return self._finish(report, status)
        if config.get('drive_optimization_enabled') is not True or config.get('setup_version') != CURRENT_SETUP_VERSION:
            return no_change('Drive optimization is off or current setup has not been saved.', 'skipped')
        if self.system != 'win32':
            return no_change('Only the supported Windows native optimizer is included; no fstrim or partition operation is attempted.', 'skipped')
        if not self.lock.acquire(blocking=False):
            raise RuntimeError('A drive optimization is already active.')
        lease = None
        try:
            _ordinary_data_path(self.directory)
            lease = RunLease(self.directory).acquire()
            state = self._adapter().inspect()
            report['items'] = self._plan(state)
            report['health_findings'] = [finding for item in report['items'] if (finding := _health_finding(item.get('before'))) is not None]
            if self.backend.is_admin() is not True:
                for item in report['items']:
                    item.update(status='deferred', reason=item['reason'] + ' Administrator access is required; no elevation attempt was started.')
            if not any(item['status'] == 'available' for item in report['items']):
                self._noops[report['id']] = copy.deepcopy(report['health_findings'])
                return self._finish(report, 'deferred')
            report['log_path'] = str(self.directory / (report['id'] + '.json'))
            self._write(report)
            halted = False
            for item in report['items']:
                if item['status'] != 'available':
                    continue
                if halted or cancel_event is not None and cancel_event.is_set():
                    item.update(status='skipped', reason='A previous operation or cancellation deferred the remaining volumes.')
                    report['cancelled'] = report['cancelled'] or cancel_event is not None and cancel_event.is_set()
                    continue
                fresh = self._adapter().inspect()
                matches = [row for row in fresh.get('volumes', []) if _identity(row) == item['volume_id']]
                if (_guard(fresh) or len(matches) != 1 or _eligible(matches[0])
                        or matches[0]['letter'] != item['before']['letter']):
                    item.update(status='deferred', reason='Fresh power, maintenance, identity or volume eligibility changed; no optimizer was started.')
                    continue
                if progress_callback:
                    progress_callback({'stage': item['title'], 'description': item['reason'], 'percentage': 5,
                                       'processes': [item['before']['letter'] + ': native optimizer preparation']})
                if cancel_event is not None and cancel_event.is_set():
                    item.update(status='skipped', reason='Cancelled before the native optimizer started.')
                    report['cancelled'] = True
                    halted = True
                    continue
                item.update(status='prepared', started_at=self.clock())
                log = self.directory / (report['id'] + '-' + item['before']['letter'] + '.log')
                _ordinary_data_path(log, file=True)
                item['native_output_path'] = str(log)
                self._write(report)  # Durable exact target before process launch.
                process = None
                try:
                    process = self._adapter().start(item['before']['letter'], log)
                    item.update(status='running', pid=int(process.pid))
                    self._write(report)
                    deadline = time.monotonic() + self.timeout
                    last_progress = 0
                    while process.poll() is None:
                        if cancel_event is not None and cancel_event.is_set() or time.monotonic() >= deadline:
                            item.update(status='uncertain', reason='Stopped waiting without terminating the native optimizer. Its recorded PID may still be running; later volumes were deferred.')
                            report['cancelled'] = cancel_event is not None and cancel_event.is_set()
                            halted = True
                            break
                        if progress_callback and time.monotonic() - last_progress >= 2:
                            progress_callback({'stage': item['title'], 'description': 'Waiting for Windows native optimization; elapsed time is not percent complete.',
                                               'percentage': 20, 'processes': [f'Native optimizer PID {process.pid}: {item["before"]["letter"]}:']})
                            last_progress = time.monotonic()
                        time.sleep(.1)
                    if item['status'] == 'running':
                        item['exit_code'] = process.poll()
                        item['status'] = 'reported_completion' if item['exit_code'] == 0 else 'failed'
                        item['reason'] = ('Windows native optimizer reported completion. Storage effects and speed were not independently measured.'
                                          if item['exit_code'] == 0 else 'Windows native optimizer reported failure. Inspect its output; later volumes were deferred.')
                        halted = item['exit_code'] != 0
                except Exception as error:
                    item.update(status='uncertain', reason='The optimizer outcome is unverified (' + type(error).__name__ + '); no automatic retry or termination was attempted.')
                    halted = True
                item['finished_at'] = self.clock()
                self._write(report)
            self._finish(report, 'cancelled' if report['cancelled'] else 'completed_with_errors' if any(item['status'] in ('failed', 'uncertain') for item in report['items']) else 'completed')
            self._write(report)
            return report
        finally:
            if lease is not None:
                lease.release()
            self.lock.release()

    def verify(self, report, cancel_event=None, timeout=30):
        checks = []
        try:
            if type(timeout) not in (int, float) or not 0 < timeout <= 120:
                raise ValueError('A bounded verification timeout is required.')
            if cancel_event is not None and cancel_event.is_set():
                raise InterruptedError('Verification cancelled.')
            if isinstance(report, dict) and report.get('id') in self._noops and report.get('log_path') is None:
                checks.extend(copy.deepcopy(self._noops[report['id']]))
                if not checks:
                    checks.append({'target': 'Native drive optimization', 'status': 'not_applicable', 'reason': 'No optimizer was started; skipped or deferred work is not a confirmed storage enhancement.'})
            else:
                saved = self._load(report.get('id') if isinstance(report, dict) else None)
                checks.extend(copy.deepcopy(saved.get('health_findings', [])))
                state = self._adapter().inspect(timeout=timeout)
                for item in saved['items']:
                    matches = [row for row in state.get('volumes', []) if _identity(row) == item.get('volume_id')]
                    if item['status'] == 'reported_completion':
                        good = state.get('complete') is True and len(matches) == 1 and not _eligible(matches[0]) and item.get('exit_code') == 0
                        checks.append({'target': item['title'], 'status': 'confirmed' if good else 'unverified',
                            'reason': 'The owned native process reported exit zero and the same healthy internal volume is present. This confirms the completion report and identity only; trim effectiveness, fragmentation and speed were not measured.' if good
                            else 'The native completion report or fresh volume identity could not be independently checked.'})
                    else:
                        checks.append({'target': item['title'], 'status': 'needs_review' if item['status'] in ('uncertain', 'running', 'failed', 'prepared') else 'not_applicable',
                                       'reason': item['reason']})
        except Exception as error:
            checks = [{'target': 'Native drive optimization', 'status': 'unverified', 'reason': 'Owned local drive evidence is unavailable (' + type(error).__name__ + ').'}]
        if not checks:
            checks = [{'target': 'Native drive optimization', 'status': 'not_applicable', 'reason': 'No drive operation was recorded.'}]
        status = next((state for state in ('needs_review', 'unverified', 'confirmed') if any(row['status'] == state for row in checks)), 'not_applicable')
        return {'status': status, 'checks': checks[:32], 'summary': 'Verification covers native completion reports and current volume identity only; it is not a storage-effect or speed benchmark.', 'read_only': True}
