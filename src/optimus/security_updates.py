"""Windows Security/Critical updates through the configured native WUA service.

Supported API: https://learn.microsoft.com/windows/win32/wua_sdk/portal-client
Quiet mode: https://learn.microsoft.com/windows/win32/wua_sdk/iupdateinstaller2-properties
Restart metadata: https://learn.microsoft.com/windows/win32/api/wuapi/ne-wuapi-installationrebootbehavior
Partial ATT&CK mapping: https://attack.mitre.org/mitigations/M1051/
No feature upgrades, third-party patch downloads, policy bypass or reboot.
"""
from __future__ import annotations
import base64
import copy
import json
import os
from pathlib import Path
import re
import subprocess
import threading
import time
import uuid
from functools import partial
from .workflow import atomic_json

CLASSIFICATIONS = frozenset(('0fa1201d-4330-4fa8-8ae9-b877473b6441',
                            'e6cf1350-c01b-414d-a61f-263d14d133b4'))
MITIGATION = {'id': 'M1051', 'name': 'Update Software', 'url': 'https://attack.mitre.org/mitigations/M1051/'}
_GUID = re.compile(r'^[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}$', re.I)

_NATIVE = r'''
$ErrorActionPreference='Stop';$ProgressPreference='SilentlyContinue'
[Console]::OutputEncoding=New-Object Text.UTF8Encoding($false)
$P=[Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('__PAYLOAD__'))|ConvertFrom-Json
function Pending-Restart { $required=(New-Object -ComObject Microsoft.Update.SystemInfo).RebootRequired;if($required -isnot [bool]){throw 'Restart metadata is unavailable.'};return ($required -or (Test-Path 'HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Component Based Servicing\RebootPending') -or (Test-Path 'HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\WindowsUpdate\Auto Update\RebootRequired')) }
function Eligible($u) {
 if($u.IsInstalled -or $u.IsHidden -or $u.Type -ne 1){return $false}
 foreach($c in $u.Categories){if([string]$c.CategoryID -in @('0fa1201d-4330-4fa8-8ae9-b877473b6441','e6cf1350-c01b-414d-a61f-263d14d133b4')){return $true}}
 return $false
}
$installationStarted=$false
try {
 if($P.operation -notin @('scan','install')){throw 'Unknown security-update operation.'}
 if($P.operation -eq 'install'){
  if($P.approved -isnot [bool] -or $P.approved -ne $true -or $P.accept_agreements -isnot [bool] -or $P.accept_agreements -ne $true){throw 'Saved scope and agreement acceptance are required.'}
  if(-not ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)){throw 'Administrator access is required.'}
  if(Pending-Restart){throw 'A pending restart must be resolved before installing security updates.'}
  if($env:SystemDrive -notmatch '^[A-Za-z]:$'){throw 'The system drive could not be verified.'}
  $systemDrive=New-Object IO.DriveInfo($env:SystemDrive+'\')
  if(-not $systemDrive.IsReady){throw 'The system drive is unavailable.'}
  if($systemDrive.AvailableFreeSpace -lt 2147483648){@{ok=$true;skipped=$true;reason='low_system_disk';free_bytes=[long]$systemDrive.AvailableFreeSpace;minimum_free_bytes=[long]2147483648;note='At least 2 GiB free on the system drive is required before an Optimus security-update installation.'}|ConvertTo-Json -Compress;exit 0}
  if([string]$P.id -notmatch '^[0-9a-fA-F]{8}(-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}$' -or $P.revision -isnot [int] -or $P.revision -lt 1){throw 'Invalid exact update identity.'}
 }
 $s=New-Object -ComObject Microsoft.Update.Session;$s.ClientApplicationID='Optimus Security Updates'
 $found=$s.CreateUpdateSearcher().Search("IsInstalled=0 and IsHidden=0 and Type='Software'")
 if([int]$found.ResultCode -ne 2){throw 'Windows Update did not complete a successful security-update scan.'}
 $items=@();$matches=@()
 foreach($u in $found.Updates){
  if(-not(Eligible $u)){continue}
  $categories=@($u.Categories|ForEach-Object{[string]$_.CategoryID})
  $interactive=$u.InstallationBehavior.CanRequestUserInput;$reboot=$u.InstallationBehavior.RebootBehavior
  $items+=@{id=[string]$u.Identity.UpdateID;revision=[int]$u.Identity.RevisionNumber;title=[string]$u.Title;kb=@($u.KBArticleIDs);categories=$categories;interactive=$interactive;reboot_behavior=$reboot}
  if($P.operation -eq 'install' -and [string]$u.Identity.UpdateID -ieq [string]$P.id -and [int]$u.Identity.RevisionNumber -eq [int]$P.revision){$matches+=,$u}
 }
 if($P.operation -eq 'scan'){@{ok=$true;complete=$true;resultCode=2;updates=@($items);reboot_required=[bool](Pending-Restart)}|ConvertTo-Json -Depth 8 -Compress;exit 0}
 if($matches.Count -eq 0){@{ok=$true;skipped=$true;reason='This exact update revision is no longer offered.'}|ConvertTo-Json -Compress;exit 0}
 if($matches.Count -ne 1){throw 'Ambiguous update identity.'}
 $u=$matches[0]
 if($u.InstallationBehavior.CanRequestUserInput -ne $false){throw 'This update requires interactive review.'}
 if($null -eq $u.InstallationBehavior.RebootBehavior -or [int]$u.InstallationBehavior.RebootBehavior -notin @(0,1,2)){throw 'This update has unknown restart behavior.'}
 $installer=$s.CreateUpdateInstaller()
 if($installer.IsBusy -ne $false){throw 'Windows Update is busy or its state is unknown.'}
 $installer.ForceQuiet=$true;$installer.AllowSourcePrompts=$false
 if($installer.ForceQuiet -ne $true -or $installer.AllowSourcePrompts -ne $false){throw 'Quiet installation is unavailable.'}
 $collection=New-Object -ComObject Microsoft.Update.UpdateColl;[void]$collection.Add($u)
 if(-not $u.EulaAccepted){[void]$u.AcceptEula()}
 $downloader=$s.CreateUpdateDownloader();$downloader.Updates=$collection
 $download=$downloader.Download()
 if([int]$download.ResultCode -ne 2 -or $u.IsDownloaded -ne $true){throw 'The security update did not download successfully.'}
 if(Pending-Restart){throw 'A restart became pending; installation was deferred.'}
 if($installer.IsBusy -ne $false){throw 'Another Windows Update installation is active or its state is unknown.'}
 $installer.Updates=$collection
 if($installer.RebootRequiredBeforeInstallation -ne $false){throw 'A restart is required before installation, or that state is unknown.'}
 $installationStarted=$true;$outcome=$installer.Install();$item=$outcome.GetUpdateResult(0)
 if($outcome.RebootRequired -isnot [bool] -or $item.RebootRequired -isnot [bool]){throw 'The installation restart result is unavailable.'}
 @{ok=([int]$outcome.ResultCode -eq 2 -and [int]$item.ResultCode -eq 2 -and [int]$outcome.HResult -ge 0 -and [int]$item.HResult -ge 0);resultCode=[int]$outcome.ResultCode;itemResultCode=[int]$item.ResultCode;aggregate_hresult=[int]$outcome.HResult;hresult=[int]$item.HResult;may_continue=$false;reboot_required=($outcome.RebootRequired -or $item.RebootRequired -or (Pending-Restart))}|ConvertTo-Json -Depth 5 -Compress
} catch {@{ok=$false;may_continue=$installationStarted;error=$_.Exception.Message}|ConvertTo-Json -Compress;exit 1}
'''


def native_request(operation, payload, *, timeout=900, cancel_event=None, stop_controller=None):
    if os.name != 'nt':
        return {'ok': False, 'error': 'Windows is required.'}
    if cancel_event is not None and cancel_event.is_set():
        return {'ok': False, 'cancelled': True, 'may_continue': False}
    from .emergency_stop import EmergencyStop
    controller = stop_controller if stop_controller is not None else EmergencyStop()
    controller.check()
    cancel_event = controller.bind_cancel(cancel_event)
    encoded = base64.b64encode(json.dumps(dict(payload, operation=operation), allow_nan=False).encode()).decode('ascii')
    executable = Path(os.environ.get('SystemRoot', r'C:\Windows')) / 'System32/WindowsPowerShell/v1.0/powershell.exe'
    environment = {k: v for k, v in os.environ.items() if k.casefold() != 'psmodulepath'}
    environment['PSModulePath'] = str(executable.parent / 'Modules')
    arguments = [str(executable), '-NoLogo', '-NoProfile', '-NonInteractive', '-Command',
                 _NATIVE.replace('__PAYLOAD__', encoded)]
    options = dict(stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, encoding='utf-8', errors='replace', env=environment,
        creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    ticket = None
    if operation == 'scan':
        process = controller.spawn_disposable(arguments, label='Read-only Windows security update search', **options)
    else:
        with controller.launch_gate():
            ticket = controller.begin_native('Windows security update installer')
            try:
                process = subprocess.Popen(arguments, **options)
                ticket.started = True
            except Exception:
                ticket.finish()
                raise
    deadline = time.monotonic() + timeout
    while True:
        cancelled = cancel_event is not None and cancel_event.is_set()
        if cancelled or time.monotonic() >= deadline:
            if ticket is not None:
                # WUA Install() is synchronous in this owned host. Closing it
                # mid-call cannot safely abort Windows servicing. Retain and
                # drain the original process until its genuine exit; the caller
                # saves an unresolved exact-update receipt and starts no retry.
                ticket.finish(known=False, error='Stopped waiting for native security installation.')
                controller.observe_native_child(process, ticket)
                return {'ok': False, 'cancelled': cancelled, 'may_continue': True,
                        'error': 'Cancellation requested. The Windows security update is still observed; its final installation outcome needs verification.'}
            if process.poll() is None:
                process.terminate()
            try:
                process.communicate(timeout=3)
            except subprocess.TimeoutExpired:
                process.kill()
                process.communicate(timeout=3)
            return {'ok': False, 'cancelled': cancelled, 'may_continue': operation == 'install',
                    'error': 'Stopped waiting; an update may continue under Windows control. Verify Windows Update before retrying.'}
        try:
            output, _ = process.communicate(timeout=0.2)
            if len(output) > 2 * 1024 * 1024:
                raise ValueError('Windows Update returned an oversized result.')
            result = json.loads(output.strip())
            if not isinstance(result, dict) or process.returncode != 0 and result.get('ok') is not False:
                raise ValueError('Windows Update returned an invalid result.')
            if ticket is not None:
                ticket.finish()
            return result
        except subprocess.TimeoutExpired:
            continue
        except Exception as error:
            if ticket is not None:
                # Malformed output after a real exit is not an installed claim.
                # Preserve the caller's error and continue observing if live.
                if process.poll() is not None:
                    ticket.finish()
                else:
                    ticket.finish(known=False, error=error)
                    controller.observe_native_child(process, ticket)
            raise


def candidates(scan):
    if (not isinstance(scan, dict) or scan.get('ok') is not True or scan.get('complete') is not True
            or type(scan.get('resultCode')) is not int or scan['resultCode'] != 2
            or type(scan.get('reboot_required')) is not bool
            or not isinstance(scan.get('updates'), list) or len(scan['updates']) > 1000):
        return None
    rows, seen = [], set()
    for row in scan['updates']:
        if (not isinstance(row, dict) or not isinstance(row.get('id'), str) or not _GUID.fullmatch(row['id'])
                or type(row.get('revision')) is not int or not 1 <= row['revision'] <= 2147483647
                or not isinstance(row.get('title'), str) or not row['title'].strip() or len(row['title']) > 4096
                or type(row.get('interactive')) is not bool or not isinstance(row.get('categories'), list)
                or type(row.get('reboot_behavior')) is not int or row['reboot_behavior'] not in (0,1,2)
                or len(row['categories']) > 100 or row.get('installed', False) is not False
                or any(not isinstance(item, str) or not _GUID.fullmatch(item) for item in row['categories'])
                or not CLASSIFICATIONS.intersection(item.casefold() for item in row['categories'])):
            return None
        identity = row['id'].casefold()
        if identity in seen:
            return None
        seen.add(identity)
        rows.append(copy.deepcopy(row))
    return rows


class SecurityUpdates:
    def __init__(self, data_dir, runner=None, *, stop_controller=None):
        self.directory = Path(data_dir) / 'security-updates'
        self.stop_controller = stop_controller
        self.runner = runner if runner is not None else partial(native_request, stop_controller=stop_controller)
        self._lock = threading.Lock()

    def run(self, settings, cancel_event=None, item_callback=None):
        if self.stop_controller is not None:
            self.stop_controller.check()
            cancel_event = self.stop_controller.bind_cancel(cancel_event)
        if not self._lock.acquire(blocking=False):
            return {'skipped': True, 'failed_items': 1, 'items': [], 'note': 'Another security-update request is active.'}
        lease = None
        try:
            if all(settings.get(key) is True for key in ('windows_security_updates_enabled',
                   'windows_security_updates_install_approved','windows_security_update_agreements_accepted')) and not (cancel_event is not None and cancel_event.is_set()):
                from .filesystem import _ordinary_data_path
                from .automation import RunLease
                try:
                    _ordinary_data_path(self.directory)
                    lease = RunLease(self.directory).acquire()
                except (OSError, ValueError, RuntimeError):
                    return {'skipped': True, 'failed_items': 1, 'items': [], 'requiresReview': True,
                            'note': 'The security-update location is unverifiable or another process is already applying updates.'}
            return self._run(settings, cancel_event, item_callback)
        finally:
            try:
                if lease is not None:
                    lease.release()
            finally:
                self._lock.release()

    def _run(self, settings, cancel_event=None, item_callback=None):
        if settings.get('windows_security_updates_enabled') is not True:
            return {'skipped': True, 'failed_items': 0, 'items': []}
        stop = lambda: cancel_event is not None and cancel_event.is_set()
        if stop():
            return {'cancelled': True, 'items': [], 'failed_items': 0}
        report = {'schema': 1, 'id': uuid.uuid4().hex, 'items': [], 'failed_items': 0,
                  'mitigations': [MITIGATION], 'review_only': True,
                  'coverage': 'Applicable Security/Critical software updates from the Windows-configured update service. No feature upgrade or universal vulnerability-coverage claim.'}
        try:
            scan = self.runner('scan', {}, timeout=600, cancel_event=cancel_event)
        except Exception as error:
            report.update(failed_items=0 if stop() else 1, cancelled=stop(), note='The available-update scan failed (' + type(error).__name__ + '); no installation was attempted.')
            return report
        report['scan'] = scan
        rows = candidates(scan)
        if rows is None:
            report.update(failed_items=0 if stop() else 1, cancelled=stop(),
                          note='The available-update scan could not be verified; no installation was attempted.')
            return report
        approved = settings.get('windows_security_updates_install_approved') is True
        agreements = settings.get('windows_security_update_agreements_accepted') is True
        if not (approved and agreements) or stop():
            report.update(cancelled=stop(), note='Review only. Automatic installation requires saved Security/Critical update scope and Microsoft agreement acceptance.')
            return report
        if not rows:
            report.update(note='No applicable Security/Critical update revisions are offered; no installer was started.')
            return report
        if scan.get('reboot_required') is not False:
            report.update(skipped=True, note='Resolve or verify the pending restart before installing security updates. No automatic reboot.')
            return report
        from .filesystem import _ordinary_data_path
        def write_journal(path):
            _ordinary_data_path(self.directory)
            _ordinary_data_path(path, file=True)
            atomic_json(path, report)
        # A previous unknown installer must be reviewed rather than replayed.
        try:
            _ordinary_data_path(self.directory)
            self.directory.mkdir(parents=True, exist_ok=True)
            prior_paths = list(self.directory.glob('*.json'))
            if len(prior_paths) > 1000:
                raise ValueError('Too many security-update operation records.')
            for path in prior_paths:
                info = _ordinary_data_path(path, file=True)
                if info is None or info.st_size > 2 * 1024 * 1024:
                    raise ValueError('An operation record is missing or oversized.')
                prior = json.loads(path.read_text(encoding='utf-8'))
                if (not isinstance(prior, dict) or type(prior.get('schema')) is not int or prior['schema'] != 1
                        or not re.fullmatch('[0-9a-f]{32}', path.stem) or prior.get('id') != path.stem
                        or not isinstance(prior.get('items'), list) or type(prior.get('failed_items')) is not int
                        or prior['failed_items'] < 0
                        or prior.get('status') not in ('completed','cancelled','failed')):
                    raise ValueError('A previous update outcome is unknown or uncertain.')
        except (OSError, ValueError):
            report.update(skipped=True, failed_items=1, requiresReview=True, note='A previous operation record or its location is unverifiable or uncertain. Verify Windows Update and preserve the history before retrying.')
            return report
        report['review_only'] = False
        journal = self.directory / (report['id'] + '.json')
        for index, row in enumerate(rows):
            if stop():
                report['cancelled'] = True
                break
            if row['interactive']:
                report['items'].append({'id': row['id'], 'status': 'skipped', 'reason': 'Interactive update requires review.'})
                continue
            if item_callback:
                item_callback({'current': index, 'total': len(rows), 'description': row['title']})
            if stop():
                report['cancelled'] = True
                break
            report.update(status='installing', current_update={'id': row['id'], 'revision': row['revision']})
            write_journal(journal)
            item_index = len(report['items'])
            failure_recorded = False
            try:
                outcome = self.runner('install', {'id': row['id'], 'revision': row['revision'],
                    'approved': True, 'accept_agreements': True}, timeout=3600, cancel_event=cancel_event)
                if not isinstance(outcome, dict):
                    raise ValueError('The installer returned invalid metadata.')
                skipped = outcome.get('ok') is True and outcome.get('skipped') is True
                cancelled_cleanly = outcome.get('cancelled') is True and outcome.get('may_continue') is False
                success = (outcome.get('ok') is True and type(outcome.get('resultCode')) is int
                           and outcome['resultCode'] == 2 and type(outcome.get('itemResultCode')) is int
                           and outcome['itemResultCode'] == 2 and type(outcome.get('hresult')) is int and outcome['hresult'] >= 0
                           and type(outcome.get('aggregate_hresult')) is int and outcome['aggregate_hresult'] >= 0
                           and type(outcome.get('reboot_required')) is bool)
                known_failure = outcome.get('ok') is False and outcome.get('may_continue') is False
                uncertain = (outcome.get('may_continue') is True or
                             not (success or skipped or cancelled_cleanly or known_failure))
                report['items'].append({'id': row['id'], 'title': row['title'],
                    'status': 'uncertain' if uncertain else 'completed' if success else 'skipped' if skipped else 'cancelled' if cancelled_cleanly else 'failed', 'result': outcome})
                failure_recorded = bool(uncertain or not (success or skipped or cancelled_cleanly))
                report['failed_items'] += int(failure_recorded)
                report['status'] = 'uncertain' if uncertain else 'cancelled' if cancelled_cleanly else 'failed' if known_failure else 'completed'
                report['reboot_required'] = outcome.get('reboot_required') is True
                if outcome.get('cancelled') is True:
                    report['cancelled'] = True
                if skipped and outcome.get('reason') == 'low_system_disk':
                    report.update(low_system_disk=True, skipped=True)
                write_journal(journal)
                if uncertain or not success and not skipped or report['reboot_required'] or report.get('low_system_disk') or stop():
                    break
            except Exception as error:
                if len(report['items']) == item_index:
                    report['items'].append({'id': row['id'], 'title': row['title'], 'status': 'uncertain',
                                            'error': 'The installation outcome could not be verified (' + type(error).__name__ + ').'})
                else:
                    report['items'][item_index]['status'] = 'uncertain'
                if not failure_recorded:
                    report['failed_items'] += 1
                report.update(status='uncertain', error=str(error))
                write_journal(journal)
                break
        report.setdefault('status', 'completed')
        report['cancelled'] = report.get('cancelled', False) or stop()
        if report['cancelled'] and report['status'] != 'uncertain':
            report['status'] = 'cancelled'
        report['deferred_ids'] = [row['id'] for row in rows[len(report['items']):]]
        report['skipped_items'] = sum(item['status'] == 'skipped' for item in report['items'])
        report['note'] = 'Only approved fresh exact revisions are eligible; inspect per-item outcomes for completed, skipped or uncertain updates. Reboot, cancellation, uncertainty or failure defers remaining updates; Optimus never requests an automatic reboot.'
        if report.get('low_system_disk'):
            report['note'] = 'At least 2 GiB free on the Windows system drive is required. Remaining updates were deferred; no installation was started for the skipped update.'
        if journal.exists():
            write_journal(journal)
        return report
