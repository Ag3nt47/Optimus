"""Reviewed Windows restore points; no automatic system restore or reboot.

Primary contracts:
https://learn.microsoft.com/windows/win32/sr/systemrestore
https://learn.microsoft.com/windows/win32/sr/createrestorepoint-systemrestore
https://learn.microsoft.com/windows/win32/api/srrestoreptapi/nf-srrestoreptapi-srremoverestorepoint
https://support.microsoft.com/windows/experience/backup-recovery/system-restore
"""
from __future__ import annotations

import base64
import copy
import ctypes
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import threading
import time
import uuid

from .automation import RunLease
from .filesystem import _ordinary_data_path
from .parallel_updates import pending_updates_barrier
from .speed_enhancements import _current_owner, _private_json

_ID = re.compile(r'[0-9a-f]{32}\Z')
_TTL = 600
_MAX_RECORD = 1024 * 1024


class RestoreUnavailableError(ValueError):
    """The native provider explicitly reports an unsupported environment."""


def _utc():
    return datetime.now(timezone.utc).isoformat()


def _cancelled(event):
    return event is not None and event.is_set()


def _point(value):
    if not isinstance(value, dict):
        raise ValueError('A restore-point record is malformed.')
    result = {}
    for name in ('sequence_number', 'restore_point_type', 'event_type'):
        number = value.get(name)
        if type(number) is not int or not (1 if name == 'sequence_number' else 0) <= number <= 0xffffffff:
            raise ValueError('A restore-point identity or type is invalid.')
        result[name] = number
    for name, limit in (('description', 1024), ('created_at', 128)):
        text = value.get(name)
        if not isinstance(text, str) or len(text) > limit or '\x00' in text or name == 'created_at' and not text:
            raise ValueError('A restore-point description or timestamp is invalid.')
        result[name] = text
    return result


def _fingerprint(value):
    return hashlib.sha256(json.dumps(_point(value), sort_keys=True, separators=(',', ':'),
                                     ensure_ascii=True).encode('utf-8')).hexdigest()


_POWERSHELL = r'''
$ErrorActionPreference='Stop'
$ProgressPreference='SilentlyContinue'
[Console]::InputEncoding=New-Object Text.UTF8Encoding($false)
[Console]::OutputEncoding=New-Object Text.UTF8Encoding($false)
try {
    $request=ConvertFrom-Json ([Console]::In.ReadToEnd())
    $os=Get-CimInstance -ClassName Win32_OperatingSystem -ErrorAction Stop
    if([int]$os.ProductType -ne 1){
        @{supported=$false;complete=$false;points=@();reason='System Restore is supported on Windows client editions, not Windows Server.'}|ConvertTo-Json -Compress
        exit 0
    }
    if($request.operation -ceq 'list'){
        $points=@(Get-CimInstance -Namespace root/default -ClassName SystemRestore -ErrorAction Stop | ForEach-Object {
            @{sequence_number=[long]$_.SequenceNumber;description=[string]$_.Description;
              created_at=[string]$_.CreationTime;restore_point_type=[long]$_.RestorePointType;event_type=[long]$_.EventType}
        })
        @{supported=$true;complete=$true;points=$points;protection_state='unknown'}|ConvertTo-Json -Depth 5 -Compress
    } elseif($request.operation -ceq 'create'){
        $arguments=@{Description=[string]$request.description;RestorePointType=[uint32]12;EventType=[uint32]100}
        $result=Invoke-CimMethod -Namespace root/default -ClassName SystemRestore -MethodName CreateRestorePoint -Arguments $arguments -ErrorAction Stop
        @{return_code=[long]$result.ReturnValue}|ConvertTo-Json -Compress
    } else { throw 'Unsupported System Restore operation.' }
} catch {
    @{error=[string]$_.Exception.Message}|ConvertTo-Json -Compress
    exit 2
}
'''


class WindowsSystemRestoreAdapter:
    """Native adapter; construction does not load a DLL, start a process or scan."""
    def _request(self, operation, **values):
        if sys.platform != 'win32':
            raise RuntimeError('System Restore is available only on Windows client systems.')
        native = Path(os.environ.get('SystemRoot', r'C:\Windows')) / 'System32'
        powershell = native / 'WindowsPowerShell/v1.0/powershell.exe'
        environment = {key: value for key, value in os.environ.items() if key.casefold() != 'psmodulepath'}
        environment['PSModulePath'] = str(powershell.parent / 'Modules')
        encoded = base64.b64encode(_POWERSHELL.encode('utf-16-le')).decode('ascii')
        process = subprocess.Popen([str(powershell), '-NoLogo', '-NoProfile', '-NonInteractive',
            '-EncodedCommand', encoded], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True, encoding='utf-8', errors='replace',
            env=environment, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        try:
            stdout, stderr = process.communicate(json.dumps(dict(values, operation=operation)), timeout=180)
        except subprocess.TimeoutExpired:
            # A timed-out creation may still be active in Windows. Preserve its
            # prepared journal and let the process settle instead of killing it.
            def drain():
                try:
                    process.communicate()
                except (OSError, ValueError):
                    pass
            threading.Thread(target=drain, name='Optimus restore-point receipt drain', daemon=True).start()
            raise TimeoutError('System Restore did not return in time; its Windows operation may still be running.')
        if len(stdout) > _MAX_RECORD or process.returncode != 0:
            raise RuntimeError('System Restore could not return a complete result. ' + (stdout or stderr)[-2000:])
        try:
            result = json.loads(stdout.lstrip('\ufeff'))
        except (ValueError, TypeError) as error:
            raise RuntimeError('System Restore returned an unreadable native receipt.') from error
        if not isinstance(result, dict) or result.get('error'):
            raise RuntimeError('System Restore returned an invalid result.')
        return result

    def list_points(self):
        return self._request('list')

    def create_point(self, description):
        return self._request('create', description=description)

    def delete_point(self, sequence_number):
        if sys.platform != 'win32' or type(sequence_number) is not int or not 1 <= sequence_number <= 0xffffffff:
            raise ValueError('Choose one exact native restore-point sequence number.')
        from ctypes import wintypes
        native = Path(os.environ.get('SystemRoot', r'C:\Windows')) / 'System32' / 'SrClient.dll'
        # Documented dynamic loading avoids importing an unavailable DLL at
        # application startup. This API returns a DWORD error, not GetLastError.
        library = ctypes.WinDLL(str(native), use_last_error=True)
        remove = library.SRRemoveRestorePoint
        remove.argtypes = [wintypes.DWORD]
        remove.restype = wintypes.DWORD
        return {'return_code': int(remove(sequence_number))}

    def open_restore(self):
        if sys.platform != 'win32':
            raise RuntimeError('The Windows System Restore wizard is unavailable on this platform.')
        executable = Path(os.environ.get('SystemRoot', r'C:\Windows')) / 'System32' / 'rstrui.exe'
        # No selected-point, silent-restore or reboot arguments. The user reviews
        # affected programs and confirms Finish in the native Windows wizard.
        process = subprocess.Popen([str(executable)], shell=False)
        return {'started': True, 'pid': process.pid, 'native_route': str(executable)}


class SystemRestore:
    def __init__(self, backend, *, adapter=None, owner=None, lease_factory=RunLease):
        self.backend, self.adapter, self.owner = backend, adapter, owner
        self.system = getattr(backend, 'system', None) or sys.platform
        self.directory = Path(backend.data_dir) / 'recovery' / 'system-restore'
        self.lease_factory = lease_factory
        self.lock = threading.Lock()
        self._plans = {}

    def _adapter(self):
        if self.adapter is None:
            self.adapter = WindowsSystemRestoreAdapter()
        return self.adapter

    def _owner(self):
        if self.owner is None:
            self.owner = _current_owner()
        if not isinstance(self.owner, str) or not self.owner or len(self.owner) > 256:
            raise ValueError('The local recovery owner identity is unavailable.')
        return self.owner

    def _inventory(self):
        value = self._adapter().list_points()
        if isinstance(value, dict) and value.get('supported') is False:
            raise RestoreUnavailableError(str(value.get('reason') or 'Classic System Restore is unavailable.'))
        if not isinstance(value, dict) or value.get('supported') is not True or value.get('complete') is not True:
            raise ValueError(str(value.get('reason') or 'A complete supported System Restore inventory is required.')
                             if isinstance(value, dict) else 'System Restore returned an unknown inventory.')
        rows = value.get('points')
        if not isinstance(rows, list) or len(rows) > 1000:
            raise ValueError('The restore-point inventory is malformed or exceeds its limit.')
        points = [_point(row) for row in rows]
        if len({row['sequence_number'] for row in points}) != len(points):
            raise ValueError('The restore-point inventory has ambiguous sequence numbers.')
        return points

    def scan(self):
        result = {'status': 'unsupported', 'read_only': True, 'complete': False, 'plan_id': None,
                  'points': [], 'protection_state': 'unknown', 'source': 'Windows SystemRestore WMI'}
        if self.system != 'win32':
            return dict(result, reason='Classic System Restore is available only on Windows client systems.')
        try:
            points, owner = self._inventory(), self._owner()
            now = time.monotonic()
            self._plans = {key: value for key, value in self._plans.items() if value['expires'] > now}
            if len(self._plans) >= 20:
                self._plans.pop(next(iter(self._plans)))
            identity = uuid.uuid4().hex
            selections = {uuid.uuid4().hex: {'point': row, 'fingerprint': _fingerprint(row)} for row in points}
            self._plans[identity] = {'owner': owner, 'expires': now + _TTL, 'selections': selections}
            result.update(status='completed', complete=True, plan_id=identity, expires_in_seconds=_TTL,
                points=[dict(copy.deepcopy(value['point']), selection_id=key)
                        for key, value in selections.items()],
                reason='Restore points were listed. Their presence or absence does not establish whether protection is enabled.')
        except RestoreUnavailableError as error:
            result.update(status='unsupported', reason=str(error)[:2000])
        except Exception as error:
            result.update(status='failed', reason=str(error)[:2000])
        return result

    def _selected(self, plan_id, selection_id):
        if not isinstance(plan_id, str) or not _ID.fullmatch(plan_id) or not isinstance(selection_id, str) or not _ID.fullmatch(selection_id):
            raise ValueError('Select one exact restore point from a current scan.')
        plan = self._plans.get(plan_id)
        if plan is None or plan['owner'] != self._owner() or time.monotonic() >= plan['expires']:
            raise ValueError('The restore-point selection expired or belongs to another owner. Scan again.')
        selected = plan['selections'].get(selection_id)
        if selected is None:
            raise ValueError('The restore-point selection does not belong to this scan.')
        matches = [row for row in self._inventory() if row['sequence_number'] == selected['point']['sequence_number']]
        if len(matches) != 1 or _fingerprint(matches[0]) != selected['fingerprint']:
            raise ValueError('The selected restore point changed or is no longer available. Scan again.')
        return copy.deepcopy(matches[0])

    def _pending(self):
        _ordinary_data_path(self.directory)
        if not self.directory.exists():
            return
        paths = list(self.directory.glob('*.json'))
        if len(paths) > 1000:
            raise ValueError('System Restore recovery history requires review before another operation.')
        for path in paths:
            before = _ordinary_data_path(path, file=True)
            if before is None or before.st_size > _MAX_RECORD:
                raise ValueError('A System Restore recovery record is unavailable or oversized.')
            with path.open('rb') as stream:
                opened = os.fstat(stream.fileno())
                if (opened.st_dev, opened.st_ino, opened.st_nlink) != (before.st_dev, before.st_ino, 1):
                    raise ValueError('A System Restore recovery record changed identity.')
                raw = stream.read(_MAX_RECORD + 1)
            after = _ordinary_data_path(path, file=True)
            if (after is None or len(raw) > _MAX_RECORD or
                    (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns) !=
                    (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)):
                raise ValueError('A System Restore recovery record changed while being read.')
            value = json.loads(raw)
            if (not isinstance(value, dict) or type(value.get('schema')) is not int or value['schema'] != 1
                    or value.get('id') != path.stem or not _ID.fullmatch(path.stem)
                    or value.get('owner') != self._owner()
                    or value.get('action') not in ('create', 'delete', 'open_restore')
                    or value.get('status') not in ('prepared', 'uncertain', 'completed', 'deferred', 'failed', 'blocked', 'unsupported', 'manual_action_required')):
                raise ValueError('A System Restore recovery record has an unverified owner or schema.')
            if value.get('status') in ('prepared', 'uncertain'):
                raise ValueError('Review an earlier interrupted System Restore operation before continuing: ' + str(path))

    def _guarded(self, action, confirmed, cancel_event, operation):
        if self.system != 'win32':
            return self._result('unsupported', 'Classic System Restore is unavailable on this platform.')
        if confirmed is not True:
            return self._result('blocked', 'Explicit confirmation is required for this recovery action.')
        if _cancelled(cancel_event):
            return self._result('deferred', 'Cancelled before the recovery operation.', cancelled=True)
        if not self.lock.acquire(blocking=False):
            return self._result('blocked', 'Another System Restore operation is already active.')
        try:
            with self.lease_factory(self.backend.data_dir):
                self._pending()
                if not callable(getattr(self.backend, 'is_admin', None)) or self.backend.is_admin() is not True:
                    return self._result('blocked', 'Administrator access is required; no elevation was requested.')
                barrier = pending_updates_barrier(self.backend)
                if barrier:
                    return self._result('blocked', str(barrier))
                security = self.backend.security_status()
                if not isinstance(security, dict) or security.get('reboot_required') is not False:
                    return self._result('blocked', 'A clear Windows restart state is required before recovery management.')
                if _cancelled(cancel_event):
                    return self._result('deferred', 'Cancelled before the recovery operation.', cancelled=True)
                return operation()
        except RestoreUnavailableError as error:
            return self._result('unsupported', str(error))
        except Exception as error:
            return self._result('blocked', str(error))
        finally:
            self.lock.release()

    @staticmethod
    def _result(status, reason, **values):
        return dict(status=status, reason=str(reason)[:2000], outcomeKnown=status != 'uncertain',
                    operation_started=False, **values)

    def _write(self, value):
        _ordinary_data_path(self.directory)
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        path = self.directory / (value['id'] + '.json')
        _ordinary_data_path(path, file=True)
        _private_json(path, value)
        _ordinary_data_path(path, file=True)
        return path

    def _perform(self, action, target, cancel_event, operation):
        identifier = uuid.uuid4().hex
        record = {'schema': 1, 'id': identifier, 'owner': self._owner(), 'action': action,
                  'status': 'prepared', 'started_at': _utc(), 'target': copy.deepcopy(target),
                  'note': 'A prepared or uncertain record never authorizes automatic retry.'}
        path = self._write(record)  # Durable intent precedes every native action.
        if _cancelled(cancel_event):
            result = self._result('deferred', 'Cancelled after recording intent and before the native action.', cancelled=True)
        else:
            try:
                result = operation()
                if not isinstance(result, dict):
                    raise ValueError('The native recovery operation returned an unknown result.')
            except Exception as error:
                result = self._result('uncertain', 'The recovery outcome could not be established. Preserve this record and review Windows state. ' + str(error))
                result['operation_started'] = True
        record.update(status=result['status'], finished_at=_utc(), result=copy.deepcopy(result))
        try:
            self._write(record)
        except Exception as error:
            result = self._result('uncertain', 'The final recovery receipt could not be saved. Review the prepared record before retrying.', journal_error=str(error)[:1000])
            result['operation_started'] = True
        return dict(result, id=identifier, journal_path=str(path))

    def create(self, description, confirmed=False, cancel_event=None):
        if (not isinstance(description, str) or not description.strip() or any(ord(char) < 32 for char in description)
                or len(description.encode('utf-16-le', errors='replace')) > 320):
            return self._result('blocked', 'Use a nonempty description of at most 160 UTF-16 characters without control characters.')
        def prepare():
            before = self._inventory()
            marked = description.strip() + ' [Optimus ' + uuid.uuid4().hex + ']'
            def apply():
                receipt = self._adapter().create_point(marked)
                if not isinstance(receipt, dict) or type(receipt.get('return_code')) is not int:
                    raise ValueError('The creation result has no explicit native return code.')
                if receipt['return_code'] != 0:
                    return dict(self._result('failed', 'Windows refused restore-point creation.', native_return_code=receipt['return_code']), operation_started=True)
                after = self._inventory()
                old_ids = {row['sequence_number'] for row in before}
                created = [row for row in after if row['sequence_number'] not in old_ids and row['description'] == marked]
                if len(created) != 1:
                    # S_OK can mean a point from the preceding 24 hours was
                    # reused. Never change the global creation-frequency policy.
                    return dict(self._result('deferred', 'Windows acknowledged creation but no new matching restore point was verified. A recent point may satisfy the Windows creation-frequency limit.', native_return_code=0), operation_started=True)
                return dict(self._result('completed', 'A new uniquely marked restore point was created and listed again.',
                    created_point=created[0], native_return_code=0), operation_started=True)
            return self._perform('create', {'description': marked, 'previous_sequences': [row['sequence_number'] for row in before]}, cancel_event, apply)
        return self._guarded('create', confirmed, cancel_event, prepare)

    def delete(self, plan_id, selection_id, confirmed=False, cancel_event=None):
        def prepare():
            selected = self._selected(plan_id, selection_id)
            def apply():
                # Revalidate after the intent write too; other tools do not hold
                # Optimus's lease and may remove or replace points meanwhile.
                try:
                    current = self._selected(plan_id, selection_id)
                except (ValueError, RestoreUnavailableError) as error:
                    return self._result('blocked', str(error))
                if _cancelled(cancel_event):
                    return self._result('deferred', 'Cancelled before deleting the selected restore point.', cancelled=True)
                receipt = self._adapter().delete_point(current['sequence_number'])
                if not isinstance(receipt, dict) or type(receipt.get('return_code')) is not int:
                    raise ValueError('The deletion result has no explicit native return code.')
                code = receipt['return_code']
                if code != 0:
                    return dict(self._result('failed', 'Windows did not confirm deletion of the exact restore point.', native_return_code=code), operation_started=True)
                after = self._inventory()
                if any(row['sequence_number'] == selected['sequence_number'] for row in after):
                    raise ValueError('Windows returned success but the selected restore point remains listed.')
                return dict(self._result('completed', 'The exact selected restore point was removed and its absence was verified.',
                                        deleted_point=selected, native_return_code=0), operation_started=True)
            return self._perform('delete', selected, cancel_event, apply)
        return self._guarded('delete', confirmed, cancel_event, prepare)

    def open_restore(self, plan_id, selection_id, confirmed=False, restart_acknowledged=False):
        if restart_acknowledged is not True:
            return self._result('blocked', 'Acknowledge that completing System Restore in Windows restarts the computer.')
        def prepare():
            selected = self._selected(plan_id, selection_id)
            def launch():
                try:
                    self._selected(plan_id, selection_id)
                except (ValueError, RestoreUnavailableError) as error:
                    return self._result('blocked', str(error))
                receipt = self._adapter().open_restore()
                if isinstance(receipt, dict) and receipt.get('started') is False:
                    return self._result('failed', 'Windows did not open the System Restore wizard.')
                if not isinstance(receipt, dict) or receipt.get('started') is not True:
                    raise ValueError('The native System Restore wizard did not acknowledge opening.')
                return dict(self._result('manual_action_required', 'The Windows System Restore wizard was opened. Select the reviewed point and inspect affected programs there. Optimus has not applied a restore or restarted Windows.',
                    selected_point=selected, native_receipt=receipt, restored=False, restart_started=False), operation_started=True)
            return self._perform('open_restore', selected, None, launch)
        return self._guarded('open_restore', confirmed, None, prepare)
