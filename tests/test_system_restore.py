"""System Restore uses injected stateful adapters and temporary private data only."""
import copy
import base64
import json
from pathlib import Path
import threading
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest

from optimus import system_restore as restore
from optimus.automation import RunLease


def point(sequence=10, **changes):
    result = {'sequence_number': sequence, 'description': 'Fixture checkpoint',
              'created_at': '20261023020000.000000-000', 'restore_point_type': 12, 'event_type': 100}
    result.update(changes)
    return result


class Adapter:
    def __init__(self):
        self.points = [point()]
        self.calls = []
        self.create_new = True
        self.return_code = 0
        self.opened = True
        self.before_create = self.before_delete = None
        self.inventory = None

    def list_points(self):
        self.calls.append('list')
        if self.inventory is not None:
            return copy.deepcopy(self.inventory)
        return {'supported': True, 'complete': True, 'points': copy.deepcopy(self.points), 'protection_state': 'unknown'}

    def create_point(self, description):
        self.calls.append(('create', description))
        if self.before_create:
            self.before_create()
        if self.create_new and type(self.return_code) is int and self.return_code == 0:
            self.points.append(point(max([row['sequence_number'] for row in self.points] or [0]) + 1,
                                     description=description))
        return {'return_code': self.return_code}

    def delete_point(self, sequence_number):
        self.calls.append(('delete', sequence_number))
        if self.before_delete:
            self.before_delete()
        if type(self.return_code) is int and self.return_code == 0:
            self.points = [row for row in self.points if row['sequence_number'] != sequence_number]
        return {'return_code': self.return_code}

    def open_restore(self):
        self.calls.append('open')
        return {'started': self.opened, 'pid': 42, 'native_route': 'fixture-rstrui.exe'}


@pytest.fixture
def service(tmp_path, monkeypatch):
    monkeypatch.setattr(restore, 'pending_updates_barrier', Mock(return_value=None))
    monkeypatch.setattr(restore, '_current_owner', Mock(side_effect=AssertionError('Fixture owner must be explicit.')))
    backend = SimpleNamespace(data_dir=tmp_path, system='win32', is_admin=Mock(return_value=True),
                              security_status=Mock(return_value={'reboot_required': False}))
    return restore.SystemRestore(backend, adapter=Adapter(), owner='fixture-owner')


def selection(service):
    result = service.scan()
    assert result['status'] == 'completed', result
    return result['plan_id'], result['points'][0]['selection_id']


def records(service):
    return [json.loads(path.read_text(encoding='utf-8')) for path in service.directory.glob('*.json')]


def test_construction_is_lazy_without_native_queries_or_owner_lookup(tmp_path):
    backend = SimpleNamespace(data_dir=tmp_path)
    with patch.object(restore, 'WindowsSystemRestoreAdapter') as native, \
         patch.object(restore, '_current_owner') as owner, \
         patch.object(restore.subprocess, 'Popen') as process:
        restore.SystemRestore(backend)
    native.assert_not_called()
    owner.assert_not_called()
    process.assert_not_called()
    assert list(tmp_path.iterdir()) == []


def test_scan_is_read_only_opaque_and_does_not_infer_enabled_state(service):
    result = service.scan()
    assert result['read_only'] and result['complete'] and result['expires_in_seconds'] == 600
    assert restore._ID.fullmatch(result['plan_id'])
    assert restore._ID.fullmatch(result['points'][0]['selection_id'])
    assert result['points'][0]['sequence_number'] == 10
    assert result['protection_state'] == 'unknown'
    assert service.adapter.calls == ['list']
    assert not service.directory.exists()
    service.adapter.points = []
    assert service.scan()['protection_state'] == 'unknown'


@pytest.mark.parametrize('system', ['linux', 'darwin'])
def test_non_windows_is_explicitly_unsupported_without_adapter_calls(service, system):
    service.system = system
    assert service.scan()['status'] == 'unsupported'
    assert service.create('Fixture', confirmed=True)['status'] == 'unsupported'
    assert service.adapter.calls == []


@pytest.mark.parametrize('inventory', [None, {}, {'supported': True, 'complete': False, 'points': []},
    {'supported': True, 'complete': True, 'points': [point(), point()]},
    {'supported': True, 'complete': True, 'points': [point(sequence_number=True)]}])
def test_unknown_partial_or_ambiguous_inventory_never_becomes_empty_success(service, inventory):
    service.adapter.list_points = Mock(return_value=inventory)
    result = service.scan()
    assert result['status'] == 'failed' and not result['complete'] and result['plan_id'] is None


def test_provider_explicit_unsupported_is_preserved(service):
    service.adapter.inventory = {'supported': False, 'complete': False, 'points': [], 'reason': 'Windows Server unsupported.'}
    assert service.scan()['status'] == 'unsupported'
    assert service.create('Fixture', confirmed=True)['status'] == 'unsupported'


@pytest.mark.parametrize('ack', [False, None, 1, 'yes'])
def test_confirmation_must_be_explicit_true(service, ack):
    assert service.create('Fixture', confirmed=ack)['status'] == 'blocked'
    assert service.adapter.calls == [] and not service.directory.exists()


def test_admin_restart_and_installer_gates_prevent_every_mutation(service):
    service.backend.is_admin.return_value = False
    assert service.create('Fixture', confirmed=True)['status'] == 'blocked'
    service.backend.is_admin.return_value = True
    for state in ({'reboot_required': True}, {}, None):
        service.backend.security_status.return_value = state
        assert service.create('Fixture', confirmed=True)['status'] == 'blocked'
    service.backend.security_status.return_value = {'reboot_required': False}
    restore.pending_updates_barrier.return_value = 'Fixture unresolved installer receipt.'
    assert 'unresolved installer' in service.create('Fixture', confirmed=True)['reason']
    assert service.adapter.calls == []
    assert not service.directory.exists()


def test_run_lease_is_required_and_owned_until_receipt_written(service):
    with RunLease(service.backend.data_dir):
        assert service.create('Fixture', confirmed=True)['status'] == 'blocked'
    def while_creating():
        pending = records(service)
        assert len(pending) == 1 and pending[0]['status'] == 'prepared'
        with pytest.raises(RuntimeError):
            RunLease(service.backend.data_dir).acquire()
    service.adapter.before_create = while_creating
    result = service.create('Fixture', confirmed=True)
    assert result['status'] == 'completed'
    assert records(service)[0]['status'] == 'completed'
    with RunLease(service.backend.data_dir):
        pass


def test_create_requires_new_sequence_and_unique_marker_proof(service):
    result = service.create('Before fixture update', confirmed=True)
    assert result['status'] == 'completed' and result['outcomeKnown']
    created = result['created_point']
    assert created['sequence_number'] == 11
    assert created['description'].startswith('Before fixture update [Optimus ')
    assert created in service.adapter.points
    record = records(service)[0]
    assert record['owner'] == 'fixture-owner' and record['action'] == 'create'
    assert record['target']['previous_sequences'] == [10]


def test_s_ok_within_creation_frequency_window_is_deferred_not_created(service):
    service.adapter.create_new = False
    result = service.create('Fixture', confirmed=True)
    assert result['status'] == 'deferred' and result['outcomeKnown']
    assert 'created_point' not in result and 'frequency limit' in result['reason']
    assert service.adapter.points == [point()]
    assert len([row for row in service.adapter.calls if isinstance(row, tuple)]) == 1


@pytest.mark.parametrize('code', [True, False, None, '0'])
def test_malformed_creation_ack_is_uncertain_and_blocks_retry(service, code):
    service.adapter.return_code = code
    result = service.create('Fixture', confirmed=True)
    assert result['status'] == 'uncertain' and not result['outcomeKnown']
    before = copy.deepcopy(service.adapter.calls)
    assert service.create('Again', confirmed=True)['status'] == 'blocked'
    assert service.adapter.calls == before


def test_known_native_refusal_is_failed_without_success_claim(service):
    service.adapter.return_code = 1058
    result = service.create('Fixture', confirmed=True)
    assert result['status'] == 'failed' and result['native_return_code'] == 1058
    assert 'created_point' not in result


def test_timeout_is_uncertain_and_preserves_prepared_intent(service):
    service.adapter.create_point = Mock(side_effect=TimeoutError('fixture operation may still be active'))
    result = service.create('Fixture', confirmed=True)
    assert result['status'] == 'uncertain' and result['operation_started']
    assert records(service)[0]['status'] == 'uncertain'
    assert service.create('Again', confirmed=True)['status'] == 'blocked'
    assert service.adapter.create_point.call_count == 1


def test_terminal_journal_failure_preserves_prepared_barrier(service, monkeypatch):
    original = service._write
    count = [0]
    def write(value):
        count[0] += 1
        if count[0] == 2:
            raise OSError('fixture final disk error')
        return original(value)
    monkeypatch.setattr(service, '_write', write)
    result = service.create('Fixture', confirmed=True)
    assert result['status'] == 'uncertain' and 'journal_error' in result
    assert records(service)[0]['status'] == 'prepared'
    assert service.create('Again', confirmed=True)['status'] == 'blocked'


def test_expired_wrong_or_changed_selection_cannot_delete(service, monkeypatch):
    plan, selected = selection(service)
    assert service.delete('f' * 32, selected, confirmed=True)['status'] == 'blocked'
    assert service.delete(plan, 'f' * 32, confirmed=True)['status'] == 'blocked'
    service.adapter.points[0]['description'] = 'Changed after review'
    assert service.delete(plan, selected, confirmed=True)['status'] == 'blocked'
    service.adapter.points[0] = point()
    service._plans[plan]['expires'] = 0
    assert service.delete(plan, selected, confirmed=True)['status'] == 'blocked'
    assert not any(isinstance(row, tuple) for row in service.adapter.calls)


def test_point_owner_binding_cannot_be_reused_by_other_owner(service):
    plan, selected = selection(service)
    service.owner = 'other-owner'
    assert service.delete(plan, selected, confirmed=True)['status'] == 'blocked'


def test_delete_removes_only_exact_selected_point_after_intent(service):
    service.adapter.points.append(point(11, description='Keep this newer checkpoint'))
    plan, selected = selection(service)
    def verify_intent():
        record = records(service)[0]
        assert record['status'] == 'prepared' and record['target'] == point()
    service.adapter.before_delete = verify_intent
    result = service.delete(plan, selected, confirmed=True)
    assert result['status'] == 'completed' and result['deleted_point'] == point()
    assert [row['sequence_number'] for row in service.adapter.points] == [11]
    assert ('delete', 10) in service.adapter.calls


def test_no_silent_mass_delete_or_fallback_when_native_refuses(service):
    plan, selected = selection(service)
    service.adapter.return_code = 13
    result = service.delete(plan, selected, confirmed=True)
    assert result['status'] == 'failed' and result['native_return_code'] == 13
    assert service.adapter.points == [point()]
    assert service.adapter.calls.count(('delete', 10)) == 1


def test_success_return_without_deleted_point_is_uncertain(service):
    plan, selected = selection(service)
    service.adapter.delete_point = Mock(return_value={'return_code': 0})
    result = service.delete(plan, selected, confirmed=True)
    assert result['status'] == 'uncertain' and 'remains listed' in result['reason']


def test_cancellation_before_native_action_never_deletes(service, monkeypatch):
    plan, selected = selection(service)
    cancel = threading.Event()
    original = service._write
    def write(value):
        path = original(value)
        if value['status'] == 'prepared':
            cancel.set()
        return path
    monkeypatch.setattr(service, '_write', write)
    result = service.delete(plan, selected, confirmed=True, cancel_event=cancel)
    assert result['status'] == 'deferred' and result['cancelled']
    assert ('delete', 10) not in service.adapter.calls
    assert result['operation_started'] is False


def test_changed_selection_after_intent_does_not_call_native_delete(service, monkeypatch):
    plan, selected = selection(service)
    original = service._write
    def write(value):
        path = original(value)
        if value['status'] == 'prepared':
            service.adapter.points[0]['description'] = 'Changed concurrently'
        return path
    monkeypatch.setattr(service, '_write', write)
    result = service.delete(plan, selected, confirmed=True)
    assert result['status'] == 'blocked' and result['operation_started'] is False
    assert ('delete', 10) not in service.adapter.calls


def test_restore_wizard_needs_restart_ack_but_never_restores_or_reboots(service):
    plan, selected = selection(service)
    assert service.open_restore(plan, selected, confirmed=True)['status'] == 'blocked'
    assert 'open' not in service.adapter.calls
    result = service.open_restore(plan, selected, confirmed=True, restart_acknowledged=True)
    assert result['status'] == 'manual_action_required'
    assert result['restored'] is False and result['restart_started'] is False
    assert result['selected_point'] == point()
    assert service.adapter.calls.count('open') == 1
    assert not any(isinstance(row, tuple) for row in service.adapter.calls)


def test_wizard_false_ack_is_not_claimed_opened(service):
    plan, selected = selection(service)
    service.adapter.opened = False
    result = service.open_restore(plan, selected, confirmed=True, restart_acknowledged=True)
    assert result['status'] == 'failed' and result['operation_started'] is False


def test_linked_recovery_path_is_refused_before_mutation(service, tmp_path):
    outside = tmp_path / 'outside'
    outside.mkdir()
    service.directory.parent.mkdir()
    try:
        service.directory.symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip('This fixture account cannot create directory links.')
    result = service.create('Fixture', confirmed=True)
    assert result['status'] == 'blocked'
    assert not any(isinstance(row, tuple) for row in service.adapter.calls)
    assert list(outside.iterdir()) == []


def test_pending_corrupt_or_foreign_recovery_evidence_blocks_before_native_call(service):
    service.directory.mkdir(parents=True)
    path = service.directory / ('a' * 32 + '.json')
    for value in (b'not-json', json.dumps({'schema': True, 'owner': 'fixture-owner'}).encode(),
                  json.dumps({'schema': 1, 'owner': 'other-owner'}).encode()):
        path.write_bytes(value)
        assert service.create('Fixture', confirmed=True)['status'] == 'blocked'
    assert not any(isinstance(row, tuple) for row in service.adapter.calls)


def test_intent_write_failure_never_calls_native_mutation(service, monkeypatch):
    monkeypatch.setattr(service, '_write', Mock(side_effect=OSError('fixture disk unavailable')))
    result = service.create('Fixture', confirmed=True)
    assert result['status'] == 'blocked' and result['operation_started'] is False
    assert not any(isinstance(row, tuple) for row in service.adapter.calls)


def test_cancellation_during_final_revalidation_prevents_delete(service):
    plan, selected = selection(service)
    cancel = threading.Event()
    original = service.adapter.list_points
    reads = [0]
    def inventory():
        reads[0] += 1
        if reads[0] == 2:
            cancel.set()
        return original()
    service.adapter.list_points = inventory
    result = service.delete(plan, selected, confirmed=True, cancel_event=cancel)
    assert result['status'] == 'deferred' and not result['operation_started']
    assert ('delete', 10) not in service.adapter.calls


def test_native_query_uses_fixed_ps51_and_literal_json_stdin(monkeypatch, tmp_path):
    monkeypatch.setattr(restore.sys, 'platform', 'win32')
    monkeypatch.setenv('SystemRoot', str(tmp_path / 'Windows'))
    native = restore.WindowsSystemRestoreAdapter()
    process = Mock(returncode=0)
    process.communicate.return_value = ('{"return_code":0}', '')
    description = "Fixture '; $(must-not-run) & `arbitrary`"
    with patch.object(restore.subprocess, 'Popen', return_value=process) as launch:
        assert native.create_point(description) == {'return_code': 0}
    args, options = launch.call_args
    assert args[0][0].endswith('WindowsPowerShell\\v1.0\\powershell.exe') or args[0][0].endswith('WindowsPowerShell/v1.0/powershell.exe')
    assert description not in ' '.join(args[0])
    script = base64.b64decode(args[0][-1]).decode('utf-16-le')
    assert script == restore._POWERSHELL
    assert json.loads(process.communicate.call_args.args[0]) == {'operation': 'create', 'description': description}
    assert options['creationflags'] == getattr(restore.subprocess, 'CREATE_NO_WINDOW', 0)
    assert 'powershell.exe' in args[0][0] and 'pwsh' not in args[0][0]


def test_native_timeout_does_not_kill_active_windows_operation(monkeypatch):
    monkeypatch.setattr(restore.sys, 'platform', 'win32')
    process = Mock()
    process.communicate.side_effect = restore.subprocess.TimeoutExpired('fixture', 180)
    with patch.object(restore.subprocess, 'Popen', return_value=process), \
         patch.object(restore.threading, 'Thread') as drain:
        with pytest.raises(TimeoutError):
            restore.WindowsSystemRestoreAdapter().create_point('Fixture')
    process.kill.assert_not_called()
    process.terminate.assert_not_called()
    drain.return_value.start.assert_called_once_with()
    assert drain.call_args.kwargs['daemon'] is True


def test_native_delete_calls_exact_documented_dword_api(monkeypatch, tmp_path):
    monkeypatch.setattr(restore.sys, 'platform', 'win32')
    monkeypatch.setenv('SystemRoot', str(tmp_path / 'Windows'))
    library = SimpleNamespace(SRRemoveRestorePoint=Mock(return_value=13))
    win_dll = Mock(return_value=library)
    monkeypatch.setattr(restore.ctypes, 'WinDLL', win_dll, raising=False)
    result = restore.WindowsSystemRestoreAdapter().delete_point(42)
    assert result == {'return_code': 13}
    library.SRRemoveRestorePoint.assert_called_once_with(42)
    assert Path(win_dll.call_args.args[0]) == tmp_path / 'Windows' / 'System32' / 'SrClient.dll'
    assert len(library.SRRemoveRestorePoint.argtypes) == 1


def test_native_wizard_has_no_silent_restore_or_reboot_arguments(monkeypatch, tmp_path):
    monkeypatch.setattr(restore.sys, 'platform', 'win32')
    monkeypatch.setenv('SystemRoot', str(tmp_path / 'Windows'))
    with patch.object(restore.subprocess, 'Popen', return_value=SimpleNamespace(pid=42)) as launch:
        result = restore.WindowsSystemRestoreAdapter().open_restore()
    assert result['started'] is True and result['pid'] == 42
    launch.assert_called_once_with([str(tmp_path / 'Windows' / 'System32' / 'rstrui.exe')], shell=False)
