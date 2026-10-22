"""Bounded local outcome checks. This module never repeats a maintenance action.

Evidence describes the current postcondition, not proof of causation or a speed
benchmark. Windows Update history is local COM history; Search is never called.
"""
from __future__ import annotations
import base64
from collections import Counter
from contextlib import closing
from datetime import datetime
import json
import math
import ntpath
import os
from pathlib import Path
import re
import sqlite3
import stat
import subprocess
import time
import uuid

import psutil
from .filesystem import _ordinary_data_path

_ID = re.compile(r'^[a-f0-9]{32}$')
_GUID = re.compile(r'^[a-f0-9]{8}(?:-[a-f0-9]{4}){3}-[a-f0-9]{12}$', re.I)
_STATES = ('confirmed', 'needs_review', 'unverified', 'not_applicable')
_MAX_FILES = 20000
_NATIVE = r'''
$ErrorActionPreference='Stop';$ProgressPreference='SilentlyContinue'
[Console]::OutputEncoding=New-Object Text.UTF8Encoding($false)
$p=[Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('__PAYLOAD__'))|ConvertFrom-Json
$out=@{}
if($p.applications){try{
 $items=@();foreach($location in @('HKCU:\Software\Microsoft\Windows\CurrentVersion\Uninstall','HKLM:\Software\Microsoft\Windows\CurrentVersion\Uninstall','HKLM:\Software\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall')){
  if(Test-Path -LiteralPath $location){$items+=@(Get-ItemProperty -Path ($location+'\*') -ErrorAction Stop|Where-Object DisplayName|ForEach-Object{@{name=[string]$_.DisplayName;version=[string]$_.DisplayVersion;identity=[string]$_.PSChildName;publisher=[string]$_.Publisher;type='Desktop'}})}
 };$out.applications=@{complete=$true;items=@($items)}
}catch{$out.applications=@{complete=$false;error=$_.Exception.Message}}}
if($p.appx){try{$out.appx=@{complete=$true;items=@(Get-AppxPackage -ErrorAction Stop|ForEach-Object{@{name=[string]$_.Name;full_name=[string]$_.PackageFullName;version=[string]$_.Version}})}}catch{$out.appx=@{complete=$false;error=$_.Exception.Message}}}
if($p.defender){try{$d=Get-MpComputerStatus -ErrorAction Stop;$out.defender=@{complete=$true;available=$true;realtime_enabled=[bool]$d.RealTimeProtectionEnabled;antivirus_enabled=[bool]$d.AntivirusEnabled;quick_scan_end=if($d.QuickScanEndTime){([datetime]$d.QuickScanEndTime).ToUniversalTime().ToString('o')}else{''}}}catch{$out.defender=@{complete=$false;error=$_.Exception.Message}}}
if(@($p.privacy).Count){$items=@();foreach($spec in $p.privacy){$base=$null;$key=$null;try{
 $view=if([Environment]::Is64BitOperatingSystem){[Microsoft.Win32.RegistryView]::Registry64}else{[Microsoft.Win32.RegistryView]::Registry32}
 $base=[Microsoft.Win32.RegistryKey]::OpenBaseKey([Microsoft.Win32.RegistryHive]([string]$spec.hive),$view);$key=$base.OpenSubKey([string]$spec.path,$false)
 $exists=$key -and ($key.GetValueNames() -contains [string]$spec.name)
 $items+=@{id=[string]$spec.id;complete=$true;exists=[bool]$exists;type=if($exists){$key.GetValueKind([string]$spec.name).ToString()}else{''};value=if($exists){$key.GetValue([string]$spec.name,$null,[Microsoft.Win32.RegistryValueOptions]::DoNotExpandEnvironmentNames)}else{$null}}
}catch{$items+=@{id=[string]$spec.id;complete=$false;error=$_.Exception.Message}}finally{if($key){$key.Dispose()};if($base){$base.Dispose()}}};$out.privacy=@{complete=$true;items=@($items)}}
if(@($p.startup).Count){$items=@();foreach($spec in $p.startup){$base=$null;$key=$null;try{
 $base=[Microsoft.Win32.RegistryKey]::OpenBaseKey([Microsoft.Win32.RegistryHive]([string]$spec.hive),[Microsoft.Win32.RegistryView]([string]$spec.view));$key=$base.OpenSubKey('Software\Microsoft\Windows\CurrentVersion\Run',$false)
 $exists=$key -and ($key.GetValueNames() -contains [string]$spec.name)
 $items+=@{id=[string]$spec.id;complete=$true;exists=[bool]$exists}
}catch{$items+=@{id=[string]$spec.id;complete=$false;error=$_.Exception.Message}}finally{if($key){$key.Dispose()};if($base){$base.Dispose()}}};$out.startup=@{complete=$true;items=@($items)}}
if(@($p.history).Count){try{
 $wanted=@{};foreach($id in $p.history){$wanted[[string]$id]=$true}
 $session=New-Object -ComObject Microsoft.Update.Session;$searcher=$session.CreateUpdateSearcher();$total=$searcher.GetTotalHistoryCount();$items=@()
 if($total -gt 0){foreach($entry in $searcher.QueryHistory(0,[Math]::Min(500,$total))){if($wanted.ContainsKey([string]$entry.UpdateIdentity.UpdateID)){$items+=@{id=[string]$entry.UpdateIdentity.UpdateID;revision=[int]$entry.UpdateIdentity.RevisionNumber;operation=[int]$entry.Operation;result_code=[int]$entry.ResultCode;hresult=[int]$entry.HResult;date=([datetime]::SpecifyKind([datetime]$entry.Date,[DateTimeKind]::Utc)).ToString('o')}}}}
 $out.history=@{complete=$true;items=@($items);examined=[Math]::Min(500,$total);truncated=($total -gt 500)}
}catch{$out.history=@{complete=$false;error=$_.Exception.Message}}}
$out|ConvertTo-Json -Depth 8 -Compress
'''


def local_snapshot(request, *, timeout, cancel_event=None):
    """One isolated local-only reader; terminating it cannot interrupt an install."""
    if os.name != 'nt':
        return {'error': 'Local Windows verification is unavailable on this platform.'}
    executable = Path(os.environ.get('SystemRoot', r'C:\Windows')) / 'System32/WindowsPowerShell/v1.0/powershell.exe'
    payload = base64.b64encode(json.dumps(request, allow_nan=False).encode()).decode()
    environment = {key: value for key, value in os.environ.items() if key.casefold() != 'psmodulepath'}
    environment['PSModulePath'] = str(executable.parent / 'Modules')
    process = subprocess.Popen([str(executable), '-NoLogo', '-NoProfile', '-NonInteractive', '-Command',
        _NATIVE.replace('__PAYLOAD__', payload)], stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, encoding='utf-8', errors='replace', env=environment,
        creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    deadline = time.monotonic() + timeout
    try:
        while True:
            if cancel_event is not None and cancel_event.is_set():
                return {'cancelled': True, 'error': 'The bounded local verification reader was cancelled.'}
            if time.monotonic() >= deadline:
                return {'deadline_reached': True, 'error': 'The bounded local verification reader reached its time limit.'}
            try:
                output, _ = process.communicate(timeout=min(0.1, max(0.01, deadline-time.monotonic())))
                if process.returncode or len(output) > 4 * 1024 * 1024:
                    return {'error': 'The local verification reader returned no usable bounded result.'}
                value = json.loads(output)
                return value if isinstance(value, dict) else {'error': 'Invalid local verification result.'}
            except subprocess.TimeoutExpired:
                continue
    finally:
        if process.poll() is None:
            process.kill()
            process.communicate(timeout=1)


def _process_state(pid):
    process = psutil.Process(pid)
    birth = process.create_time()
    result = {'pid': pid, 'create_time': birth, 'exe': process.exe(), 'priority': int(process.nice())}
    if process.create_time() != birth:
        raise ValueError('The process lifetime changed during the read.')
    return result


def _epoch(value):
    try:
        if isinstance(value, str):
            return datetime.fromisoformat(value.replace('Z', '+00:00')).timestamp()
        return float(value) if type(value) in (int, float) and math.isfinite(value) else None
    except (ValueError, TypeError, OverflowError):
        return None


def _status(checks):
    states = {item['status'] for item in checks}
    return next((state for state in ('needs_review', 'unverified', 'confirmed') if state in states), 'not_applicable')


def _no_change_claimed(value):
    """A skipped/review label cannot suppress an actual mutation receipt."""
    if value.get('skipped') is not True and value.get('review_only') is not True:
        return False
    pending, inspected = [(value, 0)], 0
    while pending:
        item, depth = pending.pop()
        inspected += 1
        if inspected > 10000 or depth > 16:
            return False  # Incomplete inspection cannot prove a no-op.
        if isinstance(item, dict):
            if (item.get('status') in ('completed', 'applied', 'recycled', 'deleted', 'failed', 'cancelled', 'uncertain')
                    or item.get('errors') or type(item.get('failed_items')) is int and item['failed_items'] > 0
                    or any(item.get(key) is True for key in ('applied', 'changed', 'removed', 'Recycled', 'completion_verified'))
                    or any(type(item.get(key)) is int and item[key] > 0
                           for key in ('deleted_count', 'recycled_count', 'changed_count'))):
                return False
            pending.extend((child, depth + 1) for child in item.values() if isinstance(child, (dict, list)))
        elif isinstance(item, list):
            pending.extend((child, depth + 1) for child in item)
    return True


class OutcomeVerification:
    def __init__(self, data_dir, *, native_reader=None, process_reader=None, sampler_status=None, speed_reader=None, compatibility_reader=None, drive_reader=None, clock=None):
        self.data_dir = Path(data_dir)
        self.native_reader = native_reader or local_snapshot
        self.process_reader = process_reader or _process_state
        self.sampler_status = sampler_status
        self.speed_reader = speed_reader
        self.compatibility_reader = compatibility_reader
        self.drive_reader = drive_reader
        self.clock = clock or time.monotonic

    def _owned_json(self, relative, maximum=12*1024*1024):
        path = self.data_dir / relative
        if not path.is_relative_to(self.data_dir) or '..' in Path(relative).parts:
            raise ValueError('The verification record escaped private data.')
        info = _ordinary_data_path(path, file=True)
        if info is None or info.st_size > maximum:
            raise ValueError('The private verification evidence is missing or oversized.')
        with path.open('rb') as stream:
            current = os.fstat(stream.fileno())
            if (current.st_dev, current.st_ino, current.st_nlink) != (info.st_dev, info.st_ino, 1):
                raise ValueError('The private evidence file changed identity.')
            raw = stream.read(maximum+1)
        if len(raw) > maximum:
            raise ValueError('The private evidence file exceeds the read bound.')
        final = _ordinary_data_path(path, file=True)
        if final is None or (final.st_dev, final.st_ino, final.st_size, final.st_mtime_ns) != (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns):
            raise ValueError('The private evidence changed while it was read.')
        return json.loads(raw)

    def verify(self, report, *, cancel_event=None, timeout=30):
        if not isinstance(report, dict) or not isinstance(report.get('stages'), list):
            raise ValueError('Choose an optimization report with recorded stages.')
        if type(timeout) not in (int, float) or not math.isfinite(timeout) or timeout <= 0:
            raise ValueError('Verification requires a positive bounded time budget.')
        self.deadline = self.clock() + min(timeout, 30)
        self.cancel_event = cancel_event
        self.files_checked = 0
        started = self.clock()
        result = {'schema': 1, 'id': uuid.uuid4().hex, 'run_id': report.get('id'), 'read_only': True, 'stages': [], 'errors': [],
                  'coverage_complete': True, 'deadline_reached': False, 'cancelled': False}
        source = report['stages'][:256]
        if len(source) != len(report['stages']):
            result['coverage_complete'] = False
            result['errors'].append('The 256-stage verification coverage bound was reached.')
        request = self._request(source, report)
        self.native = {}
        native_failures = 0
        if any(request.values()) and not self._stopped():
            try:
                self.native = self.native_reader(request, timeout=max(.01, min(18, self.deadline-self.clock())), cancel_event=cancel_event)
                if not isinstance(self.native, dict):
                    self.native = {'error': 'The local evidence reader returned invalid metadata.'}
            except Exception as error:
                self.native = {'error': 'Local evidence could not be read: '+type(error).__name__}
            if self.native.get('error'):
                result['errors'].append(str(self.native['error']))
                native_failures += int(self.native.get('cancelled') is not True and self.native.get('deadline_reached') is not True)
            for name, channel in self.native.items():
                if isinstance(channel, dict) and channel.get('complete') is False:
                    result['errors'].append('Fresh local '+name+' evidence was unavailable.')
                    native_failures += 1
        for stage in source:
            title = str(stage.get('title', 'Unknown stage')) if isinstance(stage, dict) else 'Invalid stage'
            entry = {'title': title, 'checks': []}
            result['stages'].append(entry)
            checks = entry['checks']
            if not isinstance(stage, dict):
                self._add(checks, 'coverage', title, 'unverified', 'The recorded stage is malformed.')
            elif self._stopped():
                self._add(checks, 'coverage', title, 'unverified', 'The verification time budget or cancellation prevented this stage readback.')
            else:
                if stage.get('status') in ('failed', 'cancelled'):
                    self._add(checks, 'recorded_outcome', title, 'needs_review', 'The original stage reported an error or cancellation; final verification does not erase that outcome.')
                try:
                    self._stage(stage, report, checks)
                except Exception as error:
                    self._add(checks, 'local_readback', title, 'unverified', 'The local readback could not finish: '+type(error).__name__)
                if not checks:
                    self._add(checks, 'coverage', title, 'unverified', 'No independent local postcondition is implemented for this recorded operation.')
            entry['status'] = _status(checks)
        result['cancelled'] = cancel_event is not None and cancel_event.is_set()
        result['deadline_reached'] = self.clock() >= self.deadline
        counts = Counter(item['status'] for entry in result['stages'] for item in entry['checks'])
        result['counts'] = {state: counts[state] for state in _STATES}
        result['failed_items'] = native_failures + sum(item.get('failure') is True for entry in result['stages'] for item in entry['checks'])
        result['status'] = _status([{'status': entry['status']} for entry in result['stages']])
        result['duration_seconds'] = round(self.clock()-started, 3)
        result['summary'] = (f"Final local verification: {counts['confirmed']} confirmed, {counts['needs_review']} need review, "
                             f"{counts['unverified']} unverified, {counts['not_applicable']} not applicable. No maintenance action was repeated.")
        result['note'] = 'Coverage lists every recorded stage; confirmation applies only to the stated local evidence. Recreated files, later browser activity, exited processes, pending restarts and bounded history can limit proof. These checks do not establish a speed or memory improvement.'
        return result

    def _stopped(self):
        return self.clock() >= self.deadline or self.cancel_event is not None and self.cancel_event.is_set()

    @staticmethod
    def _add(checks, kind, target, status, reason, *, failure=False, **evidence):
        checks.append({'kind': kind, 'target': str(target), 'status': status, 'reason': reason,
                       'failure': failure, 'evidence': evidence})

    def _request(self, stages, report):
        from .system_privacy import _CONTROLS
        request = {'applications': False, 'appx': False, 'defender': False, 'privacy': [], 'history': [], 'startup': []}
        controls, history, startups = set(), set(), {}
        settings = report.get('settings') if isinstance(report.get('settings'), dict) else {}
        for stage in stages:
            if not isinstance(stage, dict): continue
            title, value = str(stage.get('title', '')), stage.get('result')
            if not isinstance(value, dict): continue
            items = value.get('items') if isinstance(value.get('items'), list) else []
            items = [item for item in items if isinstance(item, dict)]
            if title == 'Review or update configured software':
                request['applications'] |= any(item.get('status') == 'completed' for item in items)
            if title == 'Apply saved privacy controls':
                controls.update(item.get('id') for item in items if isinstance(item.get('id'), str) and item.get('status') in ('applied', 'already_configured'))
            if title == 'Scan for spyware and malware' and value.get('status') == 'completed': request['defender'] = True
            if title in ('Install approved driver updates', 'Check or install Windows security updates'):
                for item in items:
                    if isinstance(item, dict) and item.get('status') in ('applied', 'completed'):
                        binding = item.get('binding') if isinstance(item.get('binding'), dict) else {}
                        identity = binding.get('id', item.get('id'))
                        if isinstance(identity, str) and _GUID.fullmatch(identity): history.add(identity.casefold())
            if title.startswith(('Remove ', 'Uninstall ')):
                removal = value.get('results') if isinstance(value.get('results'), list) else []
                request['appx'] |= any(item.get('removed') is True and item.get('scope') == 'current-user' for item in removal if isinstance(item, dict))
            if title == 'Disable approved startup entries':
                applied = {item['binding'].get('id') for item in items if isinstance(item.get('binding'), dict) and isinstance(item['binding'].get('id'), str) and item.get('status') == 'applied'}
                approved = settings.get('startup_approved_entries') if isinstance(settings.get('startup_approved_entries'), list) else []
                for item in approved:
                    if isinstance(item, dict) and item.get('Id') in applied and item.get('EntryType') == 'Registry' and item.get('Hive') in ('CurrentUser', 'LocalMachine') and item.get('RegistryView') in ('Registry32', 'Registry64'):
                        startups[item['Id']] = {'id': item['Id'], 'hive': item['Hive'], 'view': item['RegistryView'], 'name': item.get('ValueName')}
        request['privacy'] = [dict(id=key, **{field: _CONTROLS[key][field] for field in ('hive', 'path', 'name')}) for key in sorted(controls) if key in _CONTROLS]
        request['history'] = sorted(history)[:200]
        request['startup'] = list(startups.values())[:100]
        return request

    def _channel(self, name):
        value = self.native.get(name)
        return value if isinstance(value, dict) and value.get('complete') is True else None

    def _stage(self, stage, report, checks):
        title, value = stage.get('title', ''), stage.get('result')
        if not isinstance(value, dict):
            if isinstance(value, list): self._add(checks, 'review', title, 'not_applicable', 'This stage collected records and claimed no maintenance change.')
            else: self._add(checks, 'stage_result', title, 'unverified', 'The original stage has no usable outcome record.')
            return
        if title in ('Clean temporary files', 'Clean selected additional categories', 'Clean selected browser data',
                     'Scan for spyware and malware', 'Optimize eligible internal drives') and _no_change_claimed(value):
            self._add(checks, 'review', title, 'not_applicable',
                      'This operation was skipped or review-only; no applied maintenance change is claimed.',
                      note=str(value.get('note', value.get('reason', value.get('stop_reason', ''))))[:500])
            return
        if title in ('Clean temporary files', 'Clean selected additional categories'):
            self._clean(value.get('result', {}), checks, title)
        elif title == 'Clean selected browser data': self._browsers(value, report, checks)
        elif title == 'Apply supported speed enhancements':
            if self.speed_reader is None:
                self._add(checks, 'speed_configuration', title, 'unverified', 'No fresh speed-configuration reader was supplied.')
            else:
                from .speed_verification import normalized_speed_checks
                checked = self.speed_reader(value, cancel_event=self.cancel_event,
                                            timeout=max(.01, self.deadline - self.clock()))
                for check in normalized_speed_checks(checked):
                    self._add(checks, 'speed_configuration', check['target'], check['status'], check['reason'])
        elif title == 'Prepare approved Windows CPU upgrade workaround':
            if self.compatibility_reader is None:
                self._add(checks, 'windows_compatibility', title, 'unverified', 'No fresh compatibility-policy reader was supplied.')
            else:
                from .speed_verification import normalized_speed_checks
                checked = self.compatibility_reader(value, cancel_event=self.cancel_event,
                                                     timeout=max(.01, self.deadline - self.clock()))
                for check in normalized_speed_checks(checked):
                    self._add(checks, 'windows_compatibility', check['target'], check['status'], check['reason'])
        elif title == 'Optimize eligible internal drives':
            if self.drive_reader is None:
                self._add(checks, 'drive_optimization', title, 'unverified', 'No independent drive-maintenance reader was supplied.')
            else:
                from .speed_verification import normalized_speed_checks
                checked = self.drive_reader(value, cancel_event=self.cancel_event,
                                            timeout=max(.01, self.deadline - self.clock()))
                for check in normalized_speed_checks(checked):
                    self._add(checks, 'drive_optimization', check['target'], check['status'], check['reason'])
        elif title == 'Apply Codex performance profile': self._priorities(value, checks)
        elif title == 'Apply saved privacy controls': self._privacy(value, checks)
        elif title == 'Review or update configured software': self._software(value, report, checks)
        elif title in ('Install approved driver updates', 'Check or install Windows security updates'): self._updates(value, stage, checks)
        elif title == 'Scan for spyware and malware':
            current = self._channel('defender'); end = _epoch(current.get('quick_scan_end')) if current else None
            started, finished = _epoch(stage.get('started_at')), _epoch(stage.get('finished_at'))
            if value.get('status') != 'completed': self._add(checks, 'defender_scan', title, 'needs_review', 'The scan was not recorded as verified complete.')
            elif current and end is not None and started is not None:
                good = end >= started and finished is not None and end <= finished+60
                later = finished is not None and end > finished+60
                self._add(checks, 'defender_scan', title, 'confirmed' if good else 'unverified' if later else 'needs_review', 'Fresh Defender status reports a completed scan within this recorded stage.' if good else 'A later scan replaced the last-completion timestamp; it cannot independently identify this older scan.' if later else 'Fresh Defender status does not confirm completion of this run.', failure=not good and not later, quick_scan_end=current.get('quick_scan_end'))
            else: self._add(checks, 'defender_scan', title, 'unverified', 'Fresh Defender completion metadata was unavailable; the earlier return is not independent proof.')
        elif title.startswith(('Remove ', 'Uninstall ')): self._debloat(value, checks)
        elif title == 'Disable approved startup entries': self._startup(value, report, checks)
        elif title == 'Enable resource analysis':
            if self.sampler_status:
                current = self.sampler_status(); good = isinstance(current, dict) and current.get('enabled') is True
                self._add(checks, 'resource_sampler', title, 'confirmed' if good else 'needs_review', 'The local sampler currently reports enabled.' if good else 'The local sampler is no longer enabled.', failure=not good)
            else: self._add(checks, 'resource_sampler', title, 'unverified', 'A fresh sampler-state reader was not supplied.')
        elif title in ('Inspect current resource usage', 'Capture final resource report'):
            valid = isinstance(value.get('Processes'), list) and isinstance(value.get('System'), dict) and value.get('InventoryErrors') == []
            self._add(checks, 'measurement_artifact', title, 'confirmed' if valid else 'unverified', 'The recorded read-only measurement has process/system metadata and no inventory errors; this is not a performance-gain test.' if valid else 'Measurement inventory completeness could not be confirmed.', processes=len(value.get('Processes', [])))
        elif title == 'Prepare background-program recommendations':
            plan = value.get('result', {}); good = isinstance(plan, dict) and plan.get('complete') is True and not plan.get('errors')
            self._add(checks, 'recommendation_coverage', title, 'confirmed' if good else 'unverified', 'The saved local recommendation inventory is complete; proposed actions were not applied by this stage.' if good else 'The recommendation inventory is incomplete.')
        elif title == 'Verify optimization outcomes' and value.get('read_only') is True:
            self._add(checks, 'prior_verification', title, 'not_applicable', 'This is a previous read-only verification artifact. This pass independently rechecks the underlying operation stages; the earlier verification is not a maintenance action.')
        elif title == 'Recycle approved local duplicates':
            for item in value.get('items', []):
                if item.get('status') == 'recycled':
                    self._clean({'deleted_count': 1, 'files': [{'path': item.get('path'), 'status': 'deleted'}]}, checks, 'Recycled duplicate')
                    self._add(checks, 'duplicate_recovery', item.get('keeper'), 'unverified', 'Absence can be checked locally, but recycle-bin recovery and keeper-content preservation were not independently rehashed.')
        elif title == 'Recycle approved cloud duplicates' and value.get('recycled_count', 0):
            self._add(checks, 'cloud_recycle', title, 'unverified', 'Independent cloud confirmation would require a new network request; this final check is local-only.')
        elif value.get('review_only') is True or value.get('skipped') is True or title in ('Check basic system security', 'Review vendor-only and legacy patch sources'):
            self._add(checks, 'review', title, 'not_applicable', 'This was review-only, skipped, or informational; no applied maintenance change is claimed.', note=str(value.get('note', value.get('reason', '')))[:500])
        else:
            self._add(checks, 'coverage', title, 'unverified', 'This operation has no independently supported local readback.')

    def _clean(self, value, checks, target):
        records = value.get('files') if isinstance(value, dict) else None
        expected = value.get('deleted_count') if isinstance(value, dict) else None
        if type(expected) is int and expected == 0:
            self._add(checks, 'file_absence', target, 'not_applicable', 'No file deletion was claimed.'); return
        if not isinstance(records, list) or type(expected) is not int or expected < 0:
            self._add(checks, 'file_absence', target, 'unverified', 'The cleanup summary lacks individual deleted-file evidence.'); return
        counts = Counter(); examples = []; checked = 0
        for record in records:
            if self._stopped() or self.files_checked >= _MAX_FILES: break
            checked += 1; self.files_checked += 1
            try:
                if not isinstance(record, dict) or record.get('status') != 'deleted': raise ValueError('Malformed deleted-file evidence.')
                path = Path(record['path'])
                if not path.is_absolute() or len(str(path)) > 4096 or os.name == 'nt' and not re.fullmatch('[A-Za-z]:', ntpath.splitdrive(str(path))[0]): raise ValueError('An ordinary absolute local path is required.')
                info = _ordinary_data_path(path, file=True)
                if info is None: state = 'absent'
                else:
                    old = record.get('identity', {})
                    if not isinstance(old, dict) or any(type(old.get(key)) is not int for key in ('device','inode')) or old['inode'] <= 0:
                        state = 'identity_unverified'
                    elif (info.st_dev, info.st_ino) != (old['device'], old['inode']) or type(old.get('created_ns')) is int and info.st_ctime_ns != old['created_ns']:
                        state = 'recreated'
                    else: state = 'original_present'
                counts[state] += 1
                if state != 'absent' and len(examples) < 20: examples.append({'path': str(path), 'observation': state})
            except (OSError, ValueError, TypeError, KeyError):
                counts['unreadable'] += 1
        omitted = max(0, expected-checked)
        if counts['absent'] or counts['recreated']:
            self._add(checks, 'file_absence', target, 'confirmed', 'Recorded paths are absent or now contain a different file identity. Recreated files are not classified as failed deletions.', absent=counts['absent'], recreated=counts['recreated'])
        if counts['original_present']:
            self._add(checks, 'file_absence', target, 'needs_review', 'A file with the recorded original identity is still present at a supposedly deleted path.', failure=True, count=counts['original_present'], examples=examples)
        unknown = counts['identity_unverified']+counts['unreadable']+omitted
        if unknown or len(records) != expected:
            self._add(checks, 'file_absence', target, 'unverified', 'Some paths lack original identities, are unreadable, or were outside the bounded check. A present path alone does not prove deletion failed.', count=unknown, checked=checked, recorded=expected, evidence_records=len(records), examples=examples)

    def _priorities(self, value, checks):
        history = None
        for item in value.get('results', []):
            if self._stopped(): self._add(checks, 'priority', 'Remaining processes', 'unverified', 'The bounded priority verification stopped.'); break
            if item.get('applied') is not True: continue
            target = str(item.get('name', 'Process'))+' / '+str(item.get('pid'))
            try:
                if history is None: history = self._owned_json('suspended-processes.json').get('allocation_history', [])
                matches = [row for row in history if isinstance(row, dict) and row.get('id') == item.get('change_id') and row.get('kind') == 'priority']
                if len(matches) != 1: raise ValueError('No unique recorded process birth identity.')
                record = matches[0]['record']; current = self.process_reader(record['pid'])
                if not isinstance(current, dict) or not math.isclose(float(current['create_time']), float(record['create_time']), abs_tol=.000001, rel_tol=0) or ntpath.normcase(current['exe']) != ntpath.normcase(record['exe']):
                    self._add(checks, 'priority', target, 'unverified', 'The PID lifetime or executable changed; a replacement process was not interpreted as the original.'); continue
                good = type(current.get('priority')) is int and current['priority'] == matches[0].get('selected')
                self._add(checks, 'priority', target, 'confirmed' if good else 'needs_review', 'The same PID, birth time and executable currently have the recorded selected priority.' if good else 'The original process now reports a different priority; another manager may have changed it.', failure=not good, priority=current.get('priority'))
            except psutil.NoSuchProcess:
                self._add(checks, 'priority', target, 'not_applicable', 'The process exited; its temporary priority setting no longer applies.')
            except (OSError, ValueError, TypeError, KeyError, psutil.Error):
                self._add(checks, 'priority', target, 'unverified', 'The saved process identity or current priority could not be independently read.')

    def _privacy(self, value, checks):
        channel = self._channel('privacy')
        for item in value.get('items', []):
            if item.get('status') not in ('applied', 'already_configured'):
                self._add(checks, 'privacy_registry', item.get('id'), 'not_applicable', 'This control was not applied: '+str(item.get('status'))); continue
            matches = [row for row in channel.get('items', []) if row.get('id') == item.get('id')] if channel else []
            if len(matches) != 1 or matches[0].get('complete') is not True:
                self._add(checks, 'privacy_registry', item.get('id'), 'unverified', 'Fresh registry readback is unavailable.'); continue
            expected = 0 if item.get('id') == 'optional_diagnostics' and item.get('before', {}).get('value') == 0 else item.get('desired')
            current = matches[0]; good = current.get('exists') is True and current.get('type') == 'DWord' and type(current.get('value')) is int and current['value'] == expected
            self._add(checks, 'privacy_registry', item.get('id'), 'confirmed' if good else 'needs_review', 'The exact local DWORD matches the selected preference; this does not prove all applications obey it.' if good else 'The current registry preference does not match the recorded selected value.', failure=not good, expected=expected, current=current.get('value'))

    def _software(self, value, report, checks):
        from .automated_software import is_newer_version
        applied = [item for item in value.get('items', []) if item.get('status') == 'completed' and item.get('result', {}).get('skipped') is not True]
        if not applied:
            self._add(checks, 'installed_software', 'Software updates', 'not_applicable', 'No application update was claimed as completed; deferred/skipped/review candidates were not treated as installed.'); return
        channel = self._channel('applications')
        initial = next((stage.get('result', {}).get('InstalledApps', []) for stage in report['stages'] if isinstance(stage, dict) and stage.get('title') == 'Inspect current resource usage'), [])
        for item in applied:
            target = item.get('id'); before = [row for row in initial if row.get('Name') == item.get('name') and row.get('Version') == item.get('from_version') and row.get('Type') == 'Desktop']
            matches = [row for row in channel.get('items', []) if len(before) == 1 and row.get('identity') == before[0].get('Identity') and row.get('name') == before[0].get('Name') and row.get('publisher') == before[0].get('Publisher')] if channel else []
            if len(matches) != 1:
                self._add(checks, 'installed_software', target, 'unverified', 'No unique unchanged local uninstall-registration identity could be linked to this exact recorded application.'); continue
            current, expected = matches[0].get('version'), item.get('to_version')
            good = isinstance(current, str) and isinstance(expected, str) and (current.casefold() == expected.casefold() or is_newer_version(expected, current))
            pending = item.get('result', {}).get('restartNeeded') is True or value.get('restartNeeded') is True
            self._add(checks, 'installed_software', target, 'confirmed' if good else 'unverified' if pending else 'needs_review', 'The same local application registration reports the requested version or a provably newer one.' if good else 'The local version does not yet confirm the requested update; a pending restart may affect registration.' if pending else 'The exact original local application registration still does not report the requested version.', failure=not good and not pending, expected=expected, current=current)

    def _updates(self, value, stage, checks):
        applied = [item for item in value.get('items', []) if item.get('status') in ('completed', 'applied')]
        if not applied:
            self._add(checks, 'local_update_history', stage['title'], 'not_applicable', 'No update installation was claimed; a successful scan or offer is not an installed update.'); return
        channel = self._channel('history')
        for item in applied:
            binding = item.get('binding') or next((row for row in value.get('scan', {}).get('updates', []) if row.get('id') == item.get('id')), {})
            identity, revision = binding.get('id', item.get('id')), binding.get('revision')
            lower, upper = _epoch(stage.get('started_at')), _epoch(stage.get('finished_at'))
            rows = [row for row in channel.get('items', []) if str(row.get('id', '')).casefold() == str(identity).casefold() and type(row.get('revision')) is int and row['revision'] == revision and row.get('operation') == 1 and lower is not None and upper is not None and lower <= (_epoch(row.get('date')) or 0) <= upper+60] if channel else []
            if not rows:
                self._add(checks, 'local_update_history', identity, 'unverified', 'No matching post-stage exact revision was found in the bounded local installation history. No online update scan was repeated.'); continue
            latest = max(rows, key=lambda row: _epoch(row.get('date')) or 0)
            good = type(latest.get('result_code')) is int and latest['result_code'] == 2 and type(latest.get('hresult')) is int and latest['hresult'] >= 0
            self._add(checks, 'local_update_history', identity, 'confirmed' if good else 'needs_review', 'Local Windows installation history confirms this exact update revision; runtime/device effects can still require a restart.' if good else 'The latest matching local update-history entry does not report successful installation.', failure=not good, revision=revision, date=latest.get('date'), result_code=latest.get('result_code'))

    def _debloat(self, value, checks):
        claimed = [item for item in value.get('results', []) if item.get('removed') is True]
        if not claimed:
            self._add(checks, 'package_absence', value.get('action', 'Optional applications'), 'not_applicable', 'No verified removal was claimed; missing targets and refused/unverified uninstallers are not treated as removals.'); return
        channel = self._channel('appx')
        for item in claimed:
            if item.get('scope') != 'current-user' or not channel or not isinstance(item.get('name'), str):
                self._add(checks, 'package_absence', item.get('name'), 'unverified', 'This removal scope has no supported independent local registration readback.'); continue
            present = [row for row in channel.get('items', []) if row.get('name') == item['name']]
            self._add(checks, 'package_absence', item['name'], 'confirmed' if not present else 'needs_review', 'No current-user AppX registration with this exact package name remains.' if not present else 'An exact-name package is currently registered; it may have been reinstalled after removal.', failure=False, registrations=len(present))

    def _startup(self, value, report, checks):
        channel = self._channel('startup')
        for item in value.get('items', []):
            if item.get('status') != 'applied': continue
            identity = item.get('binding', {}).get('id')
            saved = [row for row in report.get('settings', {}).get('startup_approved_entries', []) if row.get('Id') == identity]
            if len(saved) == 1 and saved[0].get('EntryType') == 'Shortcut':
                self._clean({'deleted_count': 1, 'files': [{'path': saved[0].get('FilePath'), 'status': 'deleted'}]}, checks, 'Disabled startup shortcut'); continue
            rows = [row for row in channel.get('items', []) if row.get('id') == identity] if channel else []
            if len(rows) == 1 and rows[0].get('complete') is True and rows[0].get('exists') is False:
                self._add(checks, 'startup_registration', identity, 'confirmed', 'The exact approved Run value is absent from its saved hive and registry view.')
            elif len(rows) == 1 and rows[0].get('complete') is True and rows[0].get('exists') is True:
                self._add(checks, 'startup_registration', identity, 'needs_review', 'A Run registration with the approved name is currently present. It may have reappeared after disabling; this does not prove the original action failed.')
            else: self._add(checks, 'startup_registration', identity, 'unverified', 'The registration is unreadable or has reappeared; local proof of the originally disabled entry is insufficient.')
        if not any(item.get('status') == 'applied' for item in value.get('items', [])):
            self._add(checks, 'startup_registration', 'Startup', 'not_applicable', 'No startup registration change was claimed as applied.')

    def _browsers(self, value, report, checks):
        for browser in value.get('items', []):
            if self._stopped():
                self._add(checks, 'browser_coverage', 'Remaining browsers', 'unverified', 'The bounded browser readback stopped.'); break
            if browser.get('status') == 'skipped':
                self._add(checks, 'browser_coverage', browser.get('browser'), 'not_applicable', 'This browser was skipped; no cleanup was claimed.'); continue
            cache = browser.get('cache')
            if isinstance(cache, dict):
                log_id = cache.get('cleanup_log_id')
                if type(cache.get('deleted_count')) is int and cache['deleted_count'] > 0 and isinstance(log_id, str) and _ID.fullmatch(log_id):
                    try:
                        journal = self._owned_json(Path('cleanup')/(log_id+'.json'))
                        if journal.get('state') != 'completed' or journal.get('plan_id') != log_id: raise ValueError('Mismatched cache evidence.')
                        self._clean(journal.get('result', {}), checks, browser.get('browser', 'Browser')+' cache')
                    except (OSError, ValueError, TypeError): self._add(checks, 'file_absence', browser.get('browser'), 'unverified', 'The private cache evidence record could not be read.')
                else: self._clean(cache, checks, browser.get('browser', 'Browser')+' cache')
            for profile in browser.get('profiles', []):
                if profile.get('status') == 'applied': self._browser_profile(profile, report, checks)
            if browser.get('unsupported_categories'):
                self._add(checks, 'browser_coverage', browser.get('browser'), 'not_applicable', 'Unsupported categories were not cleaned.', categories=browser['unsupported_categories'])

    def _browser_profile(self, item, report, checks):
        from .browser_privacy import BrowserPrivacy, _domains
        identity, backup = item.get('profile_id'), item.get('backup_id')
        if not isinstance(identity, str) or not _ID.fullmatch(identity) or not isinstance(backup, str) or not _ID.fullmatch(backup):
            self._add(checks, 'browser_records', identity, 'unverified', 'The browser backup/profile identity is invalid.'); return
        try:
            manifest = self._owned_json(Path('BrowserPrivacyBackups')/backup/'manifest.json', 1024*1024)
            if manifest.get('state') != 'applied' or manifest.get('backupId') != backup or manifest.get('profileId') != identity: raise ValueError('The backup manifest does not confirm this apply.')
            engine = BrowserPrivacy(self.data_dir); profile = engine._profile(identity)
            if manifest.get('profilePath') != profile['path']: raise ValueError('The browser profile path changed.')
            if any((process.info.get('name') or '').casefold() == profile['process'].casefold() for process in psutil.process_iter(['name'])):
                self._add(checks, 'browser_records', identity, 'unverified', 'The browser has reopened; later activity prevents attributing current records to this cleanup.'); return
            categories = item.get('categories', [])
            if not isinstance(categories, list) or not categories or any(value not in ('history','cookies','downloads') for value in categories): raise ValueError('Invalid browser category evidence.')
            keep = _domains(report.get('settings', {}).get('browser_keep_domains', [])); totals = {}
            specs = engine._specs(profile, categories)
            if not specs: raise ValueError('The recorded browser databases no longer exist.')
            for spec in specs:
                if self._stopped(): raise TimeoutError('Browser verification budget exhausted.')
                path = engine._path(profile, spec['relative']); _ordinary_data_path(path, file=True)
                records = [row for row in manifest.get('databases', []) if row.get('relative') == spec['relative'] and row.get('committed') is True]
                if len(records) != 1 or engine._files(path) != records[0].get('after'):
                    self._add(checks, 'browser_records', identity, 'unverified', 'Database metadata changed after the committed cleanup; recreation or later activity cannot be distinguished.'); return
                engine._no_pending_journal(path)
                # Immutable is safe only after the exact committed state and no
                # pending WAL/journal are checked. It creates no SQLite sidecar.
                with closing(sqlite3.connect(path.resolve().as_uri()+'?mode=ro&immutable=1', uri=True, timeout=.1)) as connection:
                    connection.execute('PRAGMA query_only=ON');connection.execute('PRAGMA trusted_schema=OFF')
                    connection.set_progress_handler(lambda: int(self._stopped()), 1000)
                    engine._schema(connection, profile['family'], spec)
                    totals.update(engine._counts(connection, profile['family'], spec, keep))
                if engine._files(path) != records[0].get('after'):
                    self._add(checks, 'browser_records', identity, 'unverified', 'The browser database changed during readback; later activity cannot be classified as a failed cleanup.'); return
            missing = set(categories)-totals.keys()
            if missing:
                self._add(checks, 'browser_records', identity, 'unverified', 'Not every claimed category has a supported current database.', categories=sorted(missing))
            remaining = {key: totals[key] for key in categories if key in totals}
            good = not any(remaining.values())
            self._add(checks, 'browser_records', identity, 'confirmed' if good else 'needs_review', 'Read-only SQLite counts are zero for the cleaned categories, excluding explicitly kept cookie domains.' if good else 'The unchanged committed browser databases still contain selected records.', failure=not good, remaining=remaining, backup_id=backup)
        except (OSError, ValueError, TypeError, KeyError, RuntimeError, sqlite3.Error, TimeoutError, psutil.Error):
            self._add(checks, 'browser_records', identity, 'unverified', 'The closed, unchanged browser database could not be independently read within the local verification limits.')
