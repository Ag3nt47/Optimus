"""Fixture-only legacy Setup preparation and exact conditional recovery."""
from __future__ import annotations

import copy
import json
import os
from pathlib import Path
import re
import threading
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from optimus.automation import CURRENT_SETUP_VERSION
from optimus.speed_verification import normalized_speed_checks
from optimus.windows_compatibility import (DESIRED, REGISTRY_PATH, TARGET, VALUE_NAME,
                                           WindowsCompatibility, _WindowsRegistry)


ABSENT = {'exists': False}
DISABLED = {'exists': True, 'type': 'DWord', 'value': 0}
SETTINGS = {'setup_version': CURRENT_SETUP_VERSION, 'windows_cpu_compatibility_enabled': True}


class RegistryFixture:
    def __init__(self, value=None):
        self.value = copy.deepcopy(ABSENT if value is None else value)
        self.writes = []
        self.read_count = 0
        self.before_read = None
        self.before_write = None
        self.write_exception = None
        self.write_result = True
        self.apply_write = True

    def read(self):
        self.read_count += 1
        if self.before_read:
            self.before_read(self.read_count)
        return copy.deepcopy(self.value)

    def write(self, expected, desired):
        if self.before_write:
            self.before_write(expected, desired)
        self.writes.append((copy.deepcopy(expected), copy.deepcopy(desired)))
        if self.write_exception:
            raise self.write_exception
        if self.value != expected:
            raise ValueError('Fixture external value changed.')
        if self.apply_write:
            self.value = copy.deepcopy(desired)
        return self.write_result


def service(tmp_path, value=None, *, admin=True, system='win32', owner='sid:fixture-owner'):
    registry = RegistryFixture(value)
    backend = SimpleNamespace(data_dir=tmp_path, is_admin=Mock(return_value=admin))
    return WindowsCompatibility(backend, registry=registry, system=system, owner=owner), registry


def load_report(result):
    return json.loads(Path(result['log_path']).read_text(encoding='utf-8'))


@pytest.mark.parametrize('before', (ABSENT, DISABLED, DESIRED))
def test_status_and_history_are_read_only_without_ownership_anchor(tmp_path, before):
    tool, registry = service(tmp_path, before)
    status = tool.status()
    assert status['read_only'] is True and status['state'] == before
    assert status['configured'] is (before == DESIRED)
    assert status['target'] == TARGET
    assert 'does not install an update' in status['summary']
    assert tool.history() == []
    assert not list(tmp_path.iterdir())
    assert registry.writes == []
    tool.backend.is_admin.assert_not_called()


@pytest.mark.parametrize('before', (ABSENT, DISABLED))
def test_apply_backs_up_exact_original_before_the_one_fixed_write(tmp_path, before):
    tool, registry = service(tmp_path, before)

    def inspect_backup(expected, desired):
        records = [path for path in tool.directory.glob('*.json') if path.name != 'owner.json']
        assert len(records) == 1
        prepared = json.loads(records[0].read_text(encoding='utf-8'))
        assert prepared['status'] == 'prepared'
        assert prepared['before'] == expected == before
        assert prepared['desired'] == desired == DESIRED
        assert prepared['target'] == TARGET
        assert prepared['owner'] == 'sid:fixture-owner' and len(prepared['signature']) == 64

    registry.before_write = inspect_backup
    result = tool.apply(SETTINGS)
    assert result['status'] == 'completed' and result['failed_items'] == 0
    assert result['items'][0]['status'] == 'applied' and result['restore_available'] is True
    assert registry.writes == [(before, DESIRED)]
    assert load_report(result) == result
    checked = tool.verify(result)
    assert checked['status'] == 'confirmed' and checked['read_only'] is True
    assert 'not eligibility' in checked['summary']
    assert normalized_speed_checks(checked) == checked['checks']


@pytest.mark.parametrize('settings', ({}, {'windows_cpu_compatibility_enabled': False},
    {'windows_cpu_compatibility_enabled': True, 'setup_version': 0},
    {'windows_cpu_compatibility_enabled': True, 'setup_version': CURRENT_SETUP_VERSION - 1}))
def test_disabled_or_unsaved_choice_never_reads_registry_or_creates_backup(tmp_path, settings):
    tool, registry = service(tmp_path)
    result = tool.apply(settings)
    assert result['status'] == 'skipped'
    assert registry.read_count == 0 and registry.writes == []
    assert not list(tmp_path.iterdir())
    assert tool.verify(result)['status'] == 'not_applicable'


@pytest.mark.parametrize('settings', (None, [], {'windows_cpu_compatibility_enabled': 'yes'},
                                    {'windows_cpu_compatibility_enabled': 1}))
def test_nonboolean_approval_is_rejected(tmp_path, settings):
    tool, registry = service(tmp_path)
    with pytest.raises(ValueError):
        tool.apply(settings)
    assert registry.writes == []


def test_already_configured_is_no_new_change_or_recovery_claim(tmp_path):
    tool, registry = service(tmp_path, DESIRED)
    result = tool.apply(SETTINGS)
    assert result['status'] == 'already_configured'
    assert result['restore_available'] is False and result['log_path'] is None
    assert tool.verify(result)['status'] == 'not_applicable'
    assert registry.writes == [] and not list(tmp_path.iterdir())


@pytest.mark.parametrize('before', (
    {'exists': True, 'type': 'String', 'value': '1'},
    {'exists': True, 'type': 'DWord', 'value': 2},
    {'exists': True, 'type': 'DWord', 'value': True},
    {'exists': True, 'type': 'DWord', 'value': -1},
    {'exists': 'yes'},
))
def test_unknown_existing_preferences_are_preserved(tmp_path, before):
    tool, registry = service(tmp_path, before)
    assert tool.status()['status'] == 'unavailable'
    assert tool.apply(SETTINGS)['status'] == 'deferred'
    assert registry.writes == [] and not list(tmp_path.iterdir())


@pytest.mark.parametrize('admin,system', ((False, 'win32'), (True, 'linux'), (True, 'darwin')))
def test_platform_and_admin_gates_never_trigger_elevation(tmp_path, admin, system):
    tool, registry = service(tmp_path, admin=admin, system=system)
    result = tool.apply(SETTINGS)
    assert result['status'] in ('deferred', 'unsupported')
    assert registry.read_count == 0 and registry.writes == []
    assert not list(tmp_path.iterdir())


def test_early_cancellation_prevents_any_registry_read(tmp_path):
    tool, registry = service(tmp_path)
    cancel = threading.Event()
    cancel.set()
    result = tool.apply(SETTINGS, cancel_event=cancel)
    assert result['status'] == 'cancelled' and result['cancelled'] is True
    assert registry.read_count == 0 and registry.writes == []
    assert not list(tmp_path.iterdir())


def test_cancellation_after_backup_prevents_the_setter(tmp_path, monkeypatch):
    tool, registry = service(tmp_path)
    cancel = threading.Event()
    original = tool._write

    def write(value):
        original(value)
        cancel.set()

    monkeypatch.setattr(tool, '_write', write)
    result = tool.apply(SETTINGS, cancel_event=cancel)
    assert result['status'] == 'cancelled' and result['restore_available'] is False
    assert registry.writes == []
    assert load_report(result)['status'] == 'cancelled'


def test_value_change_between_backup_and_setter_is_preserved(tmp_path):
    tool, registry = service(tmp_path)

    def change(count):
        if count == 2:
            registry.value = copy.deepcopy(DISABLED)

    registry.before_read = change
    result = tool.apply(SETTINGS)
    assert result['status'] == 'deferred' and result['restore_available'] is False
    assert registry.writes == [] and registry.value == DISABLED


def test_backup_failure_prevents_the_setter(tmp_path, monkeypatch):
    tool, registry = service(tmp_path)
    monkeypatch.setattr('optimus.windows_compatibility.atomic_json', Mock(side_effect=OSError('fixture disk full')))
    with pytest.raises(OSError, match='disk full'):
        tool.apply(SETTINGS)
    assert registry.writes == [] and registry.value == ABSENT


@pytest.mark.parametrize('failure', (PermissionError('fixture ACL'), RuntimeError('fixture refusal')))
def test_setter_failure_never_reports_an_applied_upgrade(tmp_path, failure):
    tool, registry = service(tmp_path)
    registry.write_exception = failure
    result = tool.apply(SETTINGS)
    assert result['status'] == 'failed' and result['failed_items'] == 1
    assert result['restore_available'] is False and result['items'][0]['status'] == 'failed'
    assert tool.verify(result)['status'] == 'needs_review'


def test_success_return_without_matching_readback_is_failure(tmp_path):
    tool, registry = service(tmp_path)
    registry.apply_write = False
    result = tool.apply(SETTINGS)
    assert result['status'] == 'failed' and result['failed_items'] == 1
    assert result['items'][0]['status'] == 'failed'


def test_uncertain_result_retains_exact_recovery_and_blocks_new_apply(tmp_path):
    tool, registry = service(tmp_path)
    registry.write_result = False  # Setter may have run despite its result.
    result = tool.apply(SETTINGS)
    assert result['status'] == 'uncertain' and result['restore_available'] is True
    assert len(registry.writes) == 1
    assert tool.apply(SETTINGS)['status'] == 'deferred'
    assert len(registry.writes) == 1
    assert tool.verify(result)['status'] == 'needs_review'
    registry.write_result = True
    assert tool.restore(result['id'])['status'] == 'restored'


def test_readback_failure_is_uncertain_and_recoverable(tmp_path):
    tool, registry = service(tmp_path)

    def deny(count):
        if count == 3:
            raise OSError('fixture readback denied')

    registry.before_read = deny
    result = tool.apply(SETTINGS)
    assert result['status'] == 'uncertain' and result['failed_items'] == 1
    assert result['restore_available'] is True
    registry.before_read = None
    assert tool.restore(result['id'])['status'] == 'restored'


@pytest.mark.parametrize('before', (ABSENT, DISABLED))
def test_restore_reinstates_exact_absence_or_original_dword(tmp_path, before):
    tool, registry = service(tmp_path, before)
    original = tool.apply(SETTINGS)
    restored = tool.restore(original['id'])
    assert registry.writes == [(before, DESIRED), (DESIRED, before)]
    assert registry.value == before
    assert restored['id'] == original['id'] and restored['status'] == 'restored'
    assert restored['restore_available'] is False
    assert tool.verify(restored)['status'] == 'confirmed'
    with pytest.raises(ValueError, match='no compatibility change'):
        tool.restore(original['id'])


@pytest.mark.parametrize('current', (ABSENT, DISABLED, {'exists': True, 'type': 'DWord', 'value': 2}))
def test_restore_preserves_later_external_preferences(tmp_path, current):
    tool, registry = service(tmp_path)
    applied = tool.apply(SETTINGS)
    registry.value = copy.deepcopy(current)
    if current.get('value') == 2:
        with pytest.raises(ValueError, match='unrecognized'):
            tool.restore(applied['id'])
    else:
        assert tool.restore(applied['id'])['status'] == 'deferred'
    assert len(registry.writes) == 1 and registry.value == current
    assert tool.verify(applied)['status'] != 'confirmed'


def test_restore_rechecks_value_after_its_prepared_backup(tmp_path, monkeypatch):
    tool, registry = service(tmp_path)
    applied = tool.apply(SETTINGS)
    original = tool._write

    def write(value):
        original(value)
        if value['status'] == 'restore_prepared':
            registry.value = copy.deepcopy(DISABLED)

    monkeypatch.setattr(tool, '_write', write)
    result = tool.restore(applied['id'])
    assert result['status'] == 'deferred'
    assert len(registry.writes) == 1 and registry.value == DISABLED


def test_interrupted_prepared_record_can_be_recovered(tmp_path, monkeypatch):
    tool, registry = service(tmp_path)
    original = tool._write

    def write(value):
        if value['status'] != 'prepared':
            raise OSError('fixture interrupted after setter')
        original(value)

    monkeypatch.setattr(tool, '_write', write)
    with pytest.raises(OSError, match='interrupted'):
        tool.apply(SETTINGS)
    row = tool.history()[0]
    assert row['status'] == 'prepared' and row['restore_available'] is True
    assert tool.apply(SETTINGS)['status'] == 'deferred'
    monkeypatch.setattr(tool, '_write', original)
    assert tool.restore(row['id'])['status'] == 'restored'


def test_history_exposes_restore_table_fields_only_for_owned_records(tmp_path):
    tool, registry = service(tmp_path)
    applied = tool.apply(SETTINGS)
    row = tool.history()[0]
    assert row['Id'] == row['id'] == applied['id']
    assert row['source'] == 'python-compatibility'
    assert row['Kind'] == row['Type'] == 'Windows compatibility preparation'
    assert row['Target'] == 'HKLM\\' + REGISTRY_PATH + '\\' + VALUE_NAME
    assert row['Status'] == row['status'] == 'completed'
    assert row['restore_available'] is True and isinstance(row['TimeUtc'], str)
    assert 'before' not in row and 'signature' not in row


def test_older_uncertain_restore_cannot_hide_behind_display_history_limit(tmp_path):
    tool, registry = service(tmp_path)
    original = tool.apply(SETTINGS)
    saved = load_report(original)
    saved.update(status='restore_uncertain', created_at=1)
    tool._write(saved)
    for index in range(101):
        newer = copy.deepcopy(saved)
        newer.update(id=f'{index + 1:032x}', status='restored', restore_available=False, created_at=index + 2)
        tool._write(newer)
    displayed = tool.history()
    assert len(displayed) == 100 and original['id'] not in {row['id'] for row in displayed}
    registry.value = copy.deepcopy(ABSENT)
    result = tool.apply(SETTINGS)
    assert result['status'] == 'deferred'
    assert 'earlier interrupted' in result['summary']
    assert len(registry.writes) == 1 and registry.value == ABSENT


@pytest.mark.parametrize('identity', ('../owner', '', 'A' * 32, 'a' * 33))
def test_restore_rejects_nonexact_record_ids(tmp_path, identity):
    tool, registry = service(tmp_path)
    with pytest.raises(ValueError):
        tool.restore(identity)
    assert registry.writes == []


@pytest.mark.parametrize('field,value', (('owner', 'sid:another-account'),
    ('target', dict(TARGET, path=r'SOFTWARE\Unrelated')), ('desired', DISABLED),
    ('before', DESIRED), ('status', 'invented'), ('id', 'f' * 32)))
def test_tampered_recovery_metadata_is_never_authority(tmp_path, field, value):
    tool, registry = service(tmp_path)
    applied = tool.apply(SETTINGS)
    path = Path(applied['log_path'])
    saved = load_report(applied)
    saved[field] = value
    path.write_text(json.dumps(saved), encoding='utf-8')
    with pytest.raises(ValueError):
        tool.restore(applied['id'])
    assert len(registry.writes) == 1
    assert tool.verify(applied)['status'] == 'unverified'


def test_other_account_cannot_restore_record(tmp_path):
    tool, registry = service(tmp_path)
    applied = tool.apply(SETTINGS)
    other = WindowsCompatibility(tool.backend, registry=registry, system='win32', owner='sid:other-account')
    with pytest.raises(ValueError, match='another identity'):
        other.restore(applied['id'])
    assert len(registry.writes) == 1


def test_hardlinked_recovery_record_is_rejected(tmp_path):
    tool, registry = service(tmp_path)
    applied = tool.apply(SETTINGS)
    os.link(applied['log_path'], tmp_path / 'linked.json')
    with pytest.raises(ValueError, match='hardlinks'):
        tool.restore(applied['id'])
    assert len(registry.writes) == 1


def test_oversized_recovery_record_is_rejected(tmp_path):
    tool, registry = service(tmp_path)
    applied = tool.apply(SETTINGS)
    Path(applied['log_path']).write_bytes(b' ' * 65537)
    with pytest.raises(ValueError, match='oversized'):
        tool.restore(applied['id'])
    assert len(registry.writes) == 1


def test_indirect_recovery_directory_is_rejected(tmp_path, monkeypatch):
    tool, registry = service(tmp_path)
    applied = tool.apply(SETTINGS)
    original = Path.lstat

    def lstat(path, *args, **kwargs):
        info = original(path, *args, **kwargs)
        if path == tool.directory:
            return SimpleNamespace(st_mode=info.st_mode, st_file_attributes=0x400)
        return info

    monkeypatch.setattr(Path, 'lstat', lstat)
    with pytest.raises(ValueError, match='Indirect'):
        tool.restore(applied['id'])
    assert len(registry.writes) == 1


def test_record_lock_prevents_overlap(tmp_path):
    tool, registry = service(tmp_path)
    tool.lock.acquire()
    try:
        with pytest.raises(RuntimeError, match='already active'):
            tool.apply(SETTINGS)
        with pytest.raises(RuntimeError, match='already active'):
            tool.restore('f' * 32)
    finally:
        tool.lock.release()
    assert registry.writes == []


@pytest.mark.parametrize('timeout', (0, -1, float('nan'), float('inf'), 121, True, '30'))
def test_verification_rejects_unbounded_or_invalid_budget(tmp_path, timeout):
    tool, registry = service(tmp_path)
    assert tool.verify({}, timeout=timeout)['status'] == 'unverified'
    assert registry.read_count == 0


def test_verification_cancellation_never_reads_registry(tmp_path):
    tool, registry = service(tmp_path)
    cancel = threading.Event()
    cancel.set()
    checked = tool.verify({}, cancel_event=cancel)
    assert checked['status'] == 'unverified' and checked['read_only'] is True
    assert registry.read_count == 0


@pytest.mark.skipif(os.name != 'nt', reason='Read-only Windows token ABI regression.')
def test_live_windows_owner_uses_full_native_handle_without_writing_state(tmp_path):
    tool, registry = service(tmp_path)
    tool.owner = None
    identity = tool._owner()
    assert re.fullmatch(r'sid:S-1-\d+(?:-\d+){1,15}', identity)
    assert tool._owner() == identity
    assert not list(tmp_path.iterdir()) and registry.writes == []


class WinregFixture:
    HKEY_LOCAL_MACHINE = 123
    KEY_READ = 1
    KEY_SET_VALUE = 2
    KEY_QUERY_VALUE = 4
    KEY_WOW64_64KEY = 256
    REG_DWORD = 4

    def __init__(self, value=None):
        self.value = value
        self.calls = []

    def OpenKey(self, hive, path, reserved, access):
        self.calls.append(('open', hive, path, reserved, access))
        assert hive == self.HKEY_LOCAL_MACHINE and path == REGISTRY_PATH
        return self

    def CreateKeyEx(self, *args):
        self.calls.append(('create', *args))
        return self.OpenKey(*args)

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return None

    def QueryValueEx(self, key, name):
        assert key is self and name == VALUE_NAME
        if self.value is None:
            raise FileNotFoundError
        return self.value

    def SetValueEx(self, key, name, reserved, kind, value):
        self.calls.append(('set', name, kind, value))
        assert key is self and name == VALUE_NAME and kind == self.REG_DWORD
        self.value = value, kind

    def DeleteValue(self, key, name):
        self.calls.append(('delete_value', name))
        assert key is self and name == VALUE_NAME
        self.value = None

    def FlushKey(self, key):
        assert key is self
        self.calls.append(('flush',))


def test_native_adapter_writes_only_fixed_dword_and_deletes_only_its_value():
    registry = WinregFixture()
    adapter = _WindowsRegistry(registry)
    assert adapter.read() == ABSENT
    assert adapter.write(ABSENT, DESIRED) is True
    assert adapter.read() == DESIRED
    assert adapter.write(DESIRED, ABSENT) is True
    assert adapter.read() == ABSENT
    writes = [call for call in registry.calls if call[0] in ('set', 'delete_value')]
    assert writes == [('set', VALUE_NAME, registry.REG_DWORD, 1), ('delete_value', VALUE_NAME)]
    assert all(call[-1] & registry.KEY_WOW64_64KEY for call in registry.calls if call[0] in ('open', 'create'))


def test_native_adapter_preserves_concurrent_value_changes():
    registry = WinregFixture((0, WinregFixture.REG_DWORD))
    adapter = _WindowsRegistry(registry)
    with pytest.raises(ValueError, match='changed'):
        adapter.write(ABSENT, DESIRED)
    assert not any(call[0] in ('set', 'delete_value') for call in registry.calls)


def test_native_adapter_reports_unknown_registry_type_without_returning_its_contents():
    registry = WinregFixture(('opaque existing contents', 1))
    assert _WindowsRegistry(registry).read() == {'exists': True, 'type': 'Other', 'value': None}
    assert not any(call[0] in ('create', 'set', 'delete_value') for call in registry.calls)
