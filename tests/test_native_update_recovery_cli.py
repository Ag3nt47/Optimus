"""Recovery commands never fall through to GUI maintenance or native installers."""
import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from optimus import app
from optimus.automation import RunLease


IDENTITY = 'a' * 32


@pytest.fixture
def service(monkeypatch, tmp_path):
    import optimus.backend
    backend = SimpleNamespace(data_dir=tmp_path, close=Mock(), optimize=Mock(),
                              update_app_unattended=Mock(), set_startup_launch=Mock())
    monkeypatch.setattr(optimus.backend, 'Backend', lambda: backend)
    monkeypatch.setattr(app.sys, 'platform', 'win32')
    monkeypatch.setattr(app, '_run_gui', Mock(side_effect=AssertionError('Recovery cannot open a normal session')))
    return backend


def test_status_only_inspects_one_record_without_recovery_lease_or_maintenance(service, monkeypatch, capsys):
    inspect = Mock(return_value={'status': 'blocked', 'reason': 'Historical runtime proof is missing.'})
    reconcile = Mock(side_effect=AssertionError('Status cannot repair a record'))
    monkeypatch.setattr('optimus.update_recovery.inspect_legacy_cache_guard', inspect)
    monkeypatch.setattr('optimus.update_recovery.reconcile_legacy_cache_guard', reconcile)
    with RunLease(service.data_dir):
        assert app.main(['--update-recovery', 'status', '--update-record', IDENTITY]) == 0
    inspect.assert_called_once_with(service.data_dir, IDENTITY)
    reconcile.assert_not_called()
    service.close.assert_called_once_with()
    service.optimize.assert_not_called()
    service.update_app_unattended.assert_not_called()
    service.set_startup_launch.assert_not_called()
    result = json.loads(capsys.readouterr().out)
    assert result['status'] == 'blocked' and result['maintenance_actions_executed'] == 0
    assert result['installers_started_by_recovery'] == 0


def test_reconciliation_holds_global_lease_and_forwards_explicit_review_file(service, monkeypatch, tmp_path, capsys):
    evidence = tmp_path / 'reviewed-evidence.json'
    evidence.write_text('{}', encoding='utf-8')
    observed = []
    def reconcile(data, identity, *, lease, reviewed_evidence):
        assert data == service.data_dir and identity == IDENTITY
        assert lease.handle is not None and not lease.handle.closed
        assert reviewed_evidence == evidence
        with pytest.raises(RuntimeError, match='already running maintenance'):
            RunLease(service.data_dir).acquire()
        observed.append(identity)
        return {'status': 'reconciled', 'new_run_required': True}
    monkeypatch.setattr('optimus.update_recovery.reconcile_legacy_cache_guard', reconcile)
    assert app.main(['--update-recovery', 'reconcile', '--update-record', IDENTITY,
                     '--recovery-evidence', str(evidence)]) == 0
    assert observed == [IDENTITY]
    with RunLease(service.data_dir):
        pass
    service.optimize.assert_not_called()
    service.update_app_unattended.assert_not_called()
    service.close.assert_called_once_with()
    assert json.loads(capsys.readouterr().out)['new_run_required'] is True


def test_busy_workflow_prevents_reconciliation_without_touching_the_record(service, monkeypatch, capsys):
    reconcile = Mock(side_effect=AssertionError('Active maintenance owns the journal'))
    monkeypatch.setattr('optimus.update_recovery.reconcile_legacy_cache_guard', reconcile)
    with RunLease(service.data_dir):
        assert app.main(['--update-recovery', 'reconcile', '--update-record', IDENTITY]) == 2
    reconcile.assert_not_called()
    service.close.assert_called_once_with()
    assert 'already running maintenance' in capsys.readouterr().err


@pytest.mark.parametrize('arguments', [
    ['--update-recovery', 'status'],
    ['--update-recovery', 'reconcile'],
    ['--update-recovery', 'status', '--update-record', IDENTITY, '--recovery-evidence', 'ignored.json'],
])
def test_missing_scope_or_unrequested_evidence_fails_without_gui_or_native_actions(service, monkeypatch, arguments):
    inspect = Mock(side_effect=AssertionError('No implicit journal selection'))
    reconcile = Mock(side_effect=AssertionError('No implicit recovery'))
    monkeypatch.setattr('optimus.update_recovery.inspect_legacy_cache_guard', inspect)
    monkeypatch.setattr('optimus.update_recovery.reconcile_legacy_cache_guard', reconcile)
    assert app.main(arguments) == 2
    inspect.assert_not_called()
    reconcile.assert_not_called()
    service.close.assert_called_once_with()


def test_recovery_options_require_an_explicit_recovery_command(monkeypatch):
    gui = Mock(side_effect=AssertionError('Invalid flags cannot open the GUI'))
    monkeypatch.setattr(app, '_run_gui', gui)
    with pytest.raises(SystemExit) as result:
        app.main(['--update-record', IDENTITY])
    assert result.value.code == 2
    gui.assert_not_called()


def test_recovery_exception_releases_lease_and_preserves_the_error(service, monkeypatch, capsys):
    monkeypatch.setattr('optimus.update_recovery.reconcile_legacy_cache_guard',
                        Mock(side_effect=ValueError('The reviewed journal changed.')))
    assert app.main(['--update-recovery', 'reconcile', '--update-record', IDENTITY]) == 2
    with RunLease(service.data_dir):
        pass
    assert 'reviewed journal changed' in capsys.readouterr().err
    service.close.assert_called_once_with()


@pytest.mark.parametrize('status', ['blocked', 'audit_pending'])
def test_incomplete_reconciliation_is_reported_with_a_failure_exit(service, monkeypatch, capsys, status):
    monkeypatch.setattr('optimus.update_recovery.reconcile_legacy_cache_guard',
                        Mock(return_value={'status': status, 'changed': status == 'audit_pending'}))
    assert app.main(['--update-recovery', 'reconcile', '--update-record', IDENTITY]) == 2
    assert json.loads(capsys.readouterr().out)['status'] == status
    with RunLease(service.data_dir):
        pass
