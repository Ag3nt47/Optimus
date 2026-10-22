"""Conditional recovery for one legacy Windows Setup compatibility preference.

This prepares the historical MoSetup CPU/TPM upgrade opt-in. It does not make
unsupported hardware supported, guarantee that Installation Assistant accepts
it, install Windows or provide patches that a vendor no longer publishes.
Current Microsoft installation guidance does not document this legacy opt-in:
https://support.microsoft.com/en-us/windows/deployment/install-upgrade/ways-to-install-windows-11
Setup /Compat IgnoreWarning only ignores dismissible compatibility messages:
https://learn.microsoft.com/windows-hardware/manufacture/desktop/windows-setup-command-line-options

No LabConfig setting, TPM, Secure Boot, signature check or Windows protection is
changed. A fresh DWORD readback verifies this preference only, never an upgrade.
"""
from __future__ import annotations

import copy
from datetime import datetime, timezone
import hashlib
import hmac
import json
import math
import os
from pathlib import Path
import re
import sys
import threading
import time
import uuid

from .automation import CURRENT_SETUP_VERSION
from .filesystem import _ordinary_data_path
from .workflow import atomic_json


REGISTRY_PATH = r'SYSTEM\Setup\MoSetup'
VALUE_NAME = 'AllowUpgradesWithUnsupportedTPMOrCPU'
TARGET = {'hive': 'HKEY_LOCAL_MACHINE', 'path': REGISTRY_PATH,
          'name': VALUE_NAME, 'view': '64-bit'}
DESIRED = {'exists': True, 'type': 'DWord', 'value': 1}
_ID = re.compile(r'[0-9a-f]{32}\Z')
_LIMIT = 65536
_KIND = 'legacy-windows-setup-compatibility'
_LIMITATION = ('Legacy Windows Setup preparation only. It may not bypass current CPU or TPM eligibility checks, '
               'does not guarantee Installation Assistant or Windows Update acceptance, and does not install an update. '
               'Genuine instruction-set, storage, driver, signature and security requirements remain in force.')
_STATES = {'prepared', 'completed', 'failed', 'uncertain', 'deferred', 'cancelled',
           'restore_prepared', 'restored', 'restore_failed', 'restore_uncertain'}


def _snapshot(value):
    if not isinstance(value, dict) or type(value.get('exists')) is not bool:
        raise ValueError('The legacy compatibility preference has no valid snapshot.')
    if value['exists'] is False:
        return {'exists': False}
    if value.get('type') != 'DWord' or type(value.get('value')) is not int or value['value'] not in (0, 1):
        raise ValueError('An unrecognized compatibility preference type or value was preserved.')
    return {'exists': True, 'type': 'DWord', 'value': value['value']}


def _cancelled(event):
    return event is not None and event.is_set()


class _WindowsRegistry:
    """One fixed machine value; caller data never supplies registry locations."""
    def __init__(self, registry=None):
        if registry is None:
            import winreg as registry
        self.registry = registry

    def _read_key(self, key):
        try:
            value, kind = self.registry.QueryValueEx(key, VALUE_NAME)
        except FileNotFoundError:
            return {'exists': False}
        return {'exists': True, 'type': 'DWord' if kind == self.registry.REG_DWORD else 'Other',
                'value': value if kind == self.registry.REG_DWORD else None}

    def read(self):
        registry = self.registry
        try:
            with registry.OpenKey(registry.HKEY_LOCAL_MACHINE, REGISTRY_PATH, 0,
                                  registry.KEY_READ | registry.KEY_WOW64_64KEY) as key:
                return self._read_key(key)
        except FileNotFoundError:
            return {'exists': False}

    def write(self, expected, desired):
        expected, desired = _snapshot(expected), _snapshot(desired)
        if _snapshot(self.read()) != expected:
            raise ValueError('The compatibility preference changed before its write.')
        registry = self.registry
        access = registry.KEY_SET_VALUE | registry.KEY_QUERY_VALUE | registry.KEY_WOW64_64KEY
        opener = registry.CreateKeyEx if desired['exists'] else registry.OpenKey
        with opener(registry.HKEY_LOCAL_MACHINE, REGISTRY_PATH, 0, access) as key:
            if _snapshot(self._read_key(key)) != expected:
                raise ValueError('The compatibility preference changed while opening its key.')
            if desired['exists']:
                registry.SetValueEx(key, VALUE_NAME, 0, registry.REG_DWORD, desired['value'])
            else:
                registry.DeleteValue(key, VALUE_NAME)  # Never delete a key or another value.
            registry.FlushKey(key)
        return True


class WindowsCompatibility:
    def __init__(self, backend, *, registry=None, system=None, owner=None):
        self.backend = backend
        self.system = system or getattr(backend, 'system', None) or sys.platform
        self.directory = Path(backend.data_dir) / 'windows-compatibility'
        self.registry = registry
        self.owner = owner
        self.lock = threading.Lock()

    def _adapter(self):
        if self.system != 'win32':
            raise ValueError('Legacy Windows Setup preparation is available only on Windows.')
        if self.registry is None:
            self.registry = _WindowsRegistry()
        return self.registry

    def _owner(self):
        if self.owner is None:
            # Reuse the token-SID helper without importing GUI/Qt or relying on
            # a profile path that can differ across an administrator relaunch.
            from .speed_enhancements import _current_owner
            self.owner = _current_owner()
        return self.owner

    def status(self):
        result = {'read_only': True, 'platform': self.system, 'target': copy.deepcopy(TARGET),
                  'state': None, 'configured': None, 'status': 'unavailable', 'summary': _LIMITATION}
        if self.system != 'win32':
            result.update(status='unsupported', reason='This is a Windows Setup preference.')
            return result
        try:
            state = _snapshot(self._adapter().read())
            result.update(state=state, configured=state == DESIRED,
                          status='already_prepared' if state == DESIRED else 'available',
                          reason='The legacy opt-in is already set.' if state == DESIRED
                          else 'The legacy opt-in can be prepared with an owned recovery record.')
        except (OSError, ValueError) as error:
            result['reason'] = str(error)
        return result

    def _read_file(self, path):
        original = _ordinary_data_path(path, file=True)
        if original is None or original.st_size > _LIMIT:
            raise ValueError('Compatibility recovery evidence is missing or oversized.')
        descriptor = os.open(path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0))
        with os.fdopen(descriptor, 'rb') as stream:
            opened = os.fstat(stream.fileno())
            if (opened.st_dev, opened.st_ino, opened.st_nlink) != (original.st_dev, original.st_ino, 1):
                raise ValueError('Compatibility evidence changed identity while opening.')
            raw = stream.read(_LIMIT + 1)
        current = _ordinary_data_path(path, file=True)
        if (len(raw) > _LIMIT or current is None
                or (current.st_dev, current.st_ino, current.st_size, current.st_mtime_ns)
                != (original.st_dev, original.st_ino, original.st_size, original.st_mtime_ns)):
            raise ValueError('Compatibility evidence changed during its bounded read.')
        return json.loads(raw)

    def _key(self, create=False):
        _ordinary_data_path(self.directory)
        path = self.directory / 'owner.json'
        if _ordinary_data_path(path, file=True) is None:
            if not create:
                return None
            self.directory.mkdir(parents=True, exist_ok=True)
            value = {'schema': 1, 'owner': self._owner(), 'key': os.urandom(32).hex()}
            # Exclusive creation preserves another writer's ownership anchor.
            try:
                with path.open('xb') as stream:
                    stream.write(json.dumps(value, separators=(',', ':')).encode('utf-8'))
                    stream.flush()
                    os.fsync(stream.fileno())
            except FileExistsError:
                pass
        value = self._read_file(path)
        if (not isinstance(value, dict) or set(value) != {'schema', 'owner', 'key'}
                or type(value.get('schema')) is not int or value['schema'] != 1
                or value.get('owner') != self._owner()
                or not isinstance(value.get('key'), str) or not re.fullmatch('[0-9a-f]{64}', value['key'])):
            raise ValueError('Compatibility history belongs to another identity or is invalid.')
        return bytes.fromhex(value['key'])

    @staticmethod
    def _signature(value, key):
        unsigned = {name: item for name, item in value.items() if name != 'signature'}
        payload = json.dumps(unsigned, sort_keys=True, separators=(',', ':'), allow_nan=False).encode('utf-8')
        return hmac.new(key, payload, hashlib.sha256).hexdigest()

    def _write(self, value):
        _ordinary_data_path(self.directory)
        key = self._key(create=True)
        path = self.directory / (value['id'] + '.json')
        _ordinary_data_path(path, file=True)
        value['signature'] = self._signature(value, key)
        atomic_json(path, value)

    def _load(self, identity):
        if not isinstance(identity, str) or not _ID.fullmatch(identity):
            raise ValueError('Choose one exact owned compatibility history ID.')
        key = self._key()
        if key is None:
            raise ValueError('No owned compatibility recovery evidence is available.')
        value = self._read_file(self.directory / (identity + '.json'))
        if (not isinstance(value, dict) or type(value.get('schema')) is not int or value['schema'] != 1
                or value.get('id') != identity or value.get('owner') != self._owner()
                or value.get('kind') != _KIND or value.get('platform') != 'win32'
                or value.get('target') != TARGET or value.get('desired') != DESIRED
                or value.get('status') not in _STATES or type(value.get('restore_available')) is not bool
                or type(value.get('created_at')) not in (int, float)
                or not math.isfinite(value['created_at']) or not 0 <= value['created_at'] <= 253402300799
                or not isinstance(value.get('signature'), str)
                or not hmac.compare_digest(value['signature'], self._signature(value, key))):
            raise ValueError('The compatibility history identity, target or signature is invalid.')
        before = _snapshot(value.get('before'))
        if before == DESIRED:
            raise ValueError('This record contains no owned compatibility change to restore.')
        return value

    def _records(self):
        """Validate all bounded evidence; display truncation is never authority."""
        _ordinary_data_path(self.directory)
        if not self.directory.exists():
            return []
        result = []
        with os.scandir(self.directory) as entries:
            for index, entry in enumerate(entries):
                if index >= 1001:
                    raise ValueError('Compatibility history exceeds its bounded inventory.')
                if entry.name == 'owner.json' or not entry.name.endswith('.json'):
                    continue
                result.append(self._load(Path(entry.name).stem))
        return result

    def history(self):
        result = []
        for saved in self._records():
            identity, status = saved['id'], saved['status']
            created = datetime.fromtimestamp(saved['created_at'], timezone.utc).isoformat()
            result.append({'source': 'python-compatibility', 'Id': identity, 'id': identity,
                           'Type': 'Windows compatibility preparation', 'Kind': 'Windows compatibility preparation',
                           'Target': 'HKLM\\' + REGISTRY_PATH + '\\' + VALUE_NAME,
                           'Status': status, 'status': status, 'TimeUtc': created, 'Created': created,
                           'created_at': saved['created_at'], 'restore_available': saved['restore_available'],
                           'summary': saved['summary']})
        return sorted(result, key=lambda row: row['created_at'], reverse=True)[:100]

    def _result(self):
        return {'schema': 1, 'id': uuid.uuid4().hex, 'platform': self.system, 'kind': _KIND,
                'created_at': time.time(), 'status': 'running', 'items': [], 'failed_items': 0,
                'cancelled': False, 'summary': _LIMITATION, 'log_path': None,
                'restore_available': False, 'target': copy.deepcopy(TARGET)}

    @staticmethod
    def _finish(value, state, item_state, reason):
        value.update(status=state, finished_at=time.time(), summary=reason + ' ' + _LIMITATION)
        value['items'] = [{'id': 'legacy-mosetup-opt-in', 'title': 'Legacy Windows Setup compatibility preparation',
                           'status': item_state, 'reason': reason}]
        value['failed_items'] = int(item_state in ('failed', 'uncertain'))
        return value

    def apply(self, settings, *, cancel_event=None):
        if (not isinstance(settings, dict)
                or 'windows_cpu_compatibility_enabled' in settings
                and type(settings['windows_cpu_compatibility_enabled']) is not bool):
            raise ValueError('The Windows CPU compatibility choice must be an explicit boolean.')
        value = self._result()
        if settings.get('windows_cpu_compatibility_enabled') is not True or settings.get('setup_version') != CURRENT_SETUP_VERSION:
            return self._finish(value, 'skipped', 'skipped', 'The legacy compatibility option is off or current setup is not saved.')
        if self.system != 'win32':
            return self._finish(value, 'unsupported', 'deferred', 'This preparation is available only on Windows.')
        if _cancelled(cancel_event):
            value['cancelled'] = True
            return self._finish(value, 'cancelled', 'skipped', 'Cancelled before any registry change.')
        if self.backend.is_admin() is not True:
            return self._finish(value, 'deferred', 'deferred', 'Administrator access is required; no permission prompt was triggered.')
        if not self.lock.acquire(blocking=False):
            raise RuntimeError('A compatibility preparation or recovery is already active.')
        try:
            # An unresolved prepared journal may describe a setter that ran
            # before a crash; do not silently layer another automatic change.
            if any(row['status'] in ('prepared', 'uncertain', 'restore_prepared', 'restore_uncertain') for row in self._records()):
                return self._finish(value, 'deferred', 'deferred', 'Review an earlier interrupted compatibility operation before preparing another one.')
            try:
                before = _snapshot(self._adapter().read())
            except (OSError, ValueError) as error:
                return self._finish(value, 'deferred', 'deferred', str(error))
            if before == DESIRED:
                return self._finish(value, 'already_configured', 'already_configured', 'The exact legacy DWORD is already 1; Optimus claims no new change.')
            value.update(owner=self._owner(), before=before, desired=copy.deepcopy(DESIRED),
                         status='prepared', restore_available=True,
                         log_path=str(self.directory / (value['id'] + '.json')))
            value['items'] = [{'id': 'legacy-mosetup-opt-in', 'status': 'prepared'}]
            self._write(value)  # Durable exact undo before the first setter.
            if _cancelled(cancel_event):
                value.update(cancelled=True, restore_available=False)
                self._finish(value, 'cancelled', 'skipped', 'Cancelled after backup and before the registry setter.')
            elif _snapshot(self._adapter().read()) != before:
                value['restore_available'] = False
                self._finish(value, 'deferred', 'deferred', 'The registry value changed after preview; a newer choice was preserved.')
            else:
                self._change(value, before, DESIRED)
            self._write(value)
            return value
        finally:
            self.lock.release()

    def _change(self, value, expected, desired, *, restoring=False):
        succeeded, failure = False, None
        try:
            succeeded = self._adapter().write(expected, desired) is True
        except (OSError, ValueError, RuntimeError) as error:
            failure = type(error).__name__
        try:
            after = _snapshot(self._adapter().read())
            value['restore_after' if restoring else 'after'] = after
        except (OSError, ValueError) as error:
            after, failure = None, failure or type(error).__name__
        if succeeded and after == desired:
            value['restore_available'] = not restoring
            self._finish(value, 'restored' if restoring else 'completed', 'restored' if restoring else 'applied',
                         'The exact original registry preference was restored and read back.' if restoring
                         else 'The legacy compatibility DWORD was set to 1 and read back; no Windows upgrade was run.')
        elif after == expected:
            value['restore_available'] = restoring
            self._finish(value, 'restore_failed' if restoring else 'failed', 'failed',
                         'Windows did not change the preference' + (' (' + failure + ')' if failure else '') + '.')
        else:
            value['restore_available'] = True
            self._finish(value, 'restore_uncertain' if restoring else 'uncertain', 'uncertain',
                         'The preference outcome could not be verified; retain this exact recovery record and review Windows state.')

    def restore(self, identity, *, cancel_event=None):
        if self.system != 'win32' or self.backend.is_admin() is not True:
            raise ValueError('Restoring this machine preference requires an administrator Windows session.')
        if not self.lock.acquire(blocking=False):
            raise RuntimeError('A compatibility preparation or recovery is already active.')
        try:
            value = self._load(identity)
            if not value['restore_available'] or value['status'] == 'restored':
                raise ValueError('This record has no compatibility change available to restore.')
            if _cancelled(cancel_event):
                return dict(value, cancelled=True, status='cancelled', summary='Recovery cancelled before any registry change. ' + _LIMITATION)
            current = _snapshot(self._adapter().read())
            if current != DESIRED:
                return dict(value, status='deferred', summary='The current value no longer equals Optimus\'s DWORD 1; later choices were preserved. ' + _LIMITATION)
            value.update(status='restore_prepared', restore_started_at=time.time())
            self._write(value)
            if _cancelled(cancel_event) or _snapshot(self._adapter().read()) != DESIRED:
                value['cancelled'] = _cancelled(cancel_event)
                self._finish(value, 'cancelled' if value['cancelled'] else 'deferred', 'skipped', 'Recovery stopped before the setter because cancellation or the current preference changed.')
            else:
                self._change(value, DESIRED, _snapshot(value['before']), restoring=True)
            self._write(value)
            return value
        finally:
            self.lock.release()

    def verify(self, report, *, cancel_event=None, timeout=30):
        check = {'target': 'Legacy Windows Setup compatibility preference', 'status': 'unverified',
                 'reason': 'Owned legacy-preference evidence could not be verified.'}
        try:
            if type(timeout) not in (int, float) or not math.isfinite(timeout) or not 0 < timeout <= 120:
                raise ValueError('Use a finite bounded verification timeout.')
            deadline = time.monotonic() + timeout
            if _cancelled(cancel_event):
                check['reason'] = 'Legacy-preference verification was cancelled.'
            elif isinstance(report, dict) and report.get('log_path') is None and report.get('status') in ('skipped', 'unsupported', 'deferred', 'already_configured', 'cancelled'):
                check.update(status='not_applicable', reason='No registry change was claimed; no Windows upgrade or patch was installed.')
            else:
                value = self._load(report.get('id') if isinstance(report, dict) else None)
                if value['status'] not in ('completed', 'restored'):
                    check.update(status='needs_review', reason='The owned legacy-preference operation did not finish with a verified result.')
                else:
                    desired = _snapshot(value['before']) if value['status'] == 'restored' else DESIRED
                    current = _snapshot(self._adapter().read())
                    if _cancelled(cancel_event) or time.monotonic() > deadline:
                        check['reason'] = 'The bounded legacy-preference verification was cancelled or exceeded its budget.'
                    elif current == desired:
                        check.update(status='confirmed', reason='The exact owned registry preference is still present. This verifies legacy preparation only, not eligibility, Windows installation or patch coverage.')
                    else:
                        check.update(status='needs_review', reason='The preference changed after Optimus; current Windows or user choices were preserved.')
        except (OSError, ValueError, TypeError, RuntimeError) as error:
            check['reason'] += ' (' + type(error).__name__ + ').'
        return {'status': check['status'], 'checks': [check], 'summary': check['reason'], 'read_only': True}
