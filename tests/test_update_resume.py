"""Reboot intent cannot broaden approvals, replay unknown installers or reboot."""
import copy
import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from optimus.automation import RunLease
from optimus.update_resume import UpdateResumeService, boot_identity


@pytest.fixture
def case(tmp_path):
    backend = SimpleNamespace(data_dir=tmp_path, pending_updates_barrier=Mock(return_value=None))
    boot = Mock(return_value=1000)
    service = UpdateResumeService(backend, boot=boot, now=lambda: 2000)
    config = {'setup_version': 2, 'toolkit_settings': {'resume': {'enabled': True}},
        'software_updates_enabled': True, 'software_update_policy': 'selected',
        'software_update_ids': ['Fixture.App'], 'software_update_agreements_accepted': True,
        'driver_updates_enabled': False}
    report = {'status': 'completed', 'stages': [{'result': {'restartNeeded': True,
        'installer_started': True, 'outcomeKnown': True, 'running': False}}]}
    return SimpleNamespace(service=service, backend=backend, config=config, report=report, boot=boot, data=tmp_path)


def capture(case):
    with RunLease(case.data) as lease:
        return case.service.capture(case.config, case.report, lease=lease)


def resume(case, runner):
    with RunLease(case.data) as lease:
        return case.service.resume(case.config, runner, lease=lease)


def test_one_resume_after_user_reboot_fresh_scope_and_no_preferences_write(case):
    preference = case.data / 'preferences.json'
    preference.write_bytes(b'{"original":true}\n')
    original = preference.read_bytes()
    saved = capture(case)
    assert saved['status'] == 'pending_reboot'
    runner = Mock(return_value={'status': 'completed', 'report': 'fixture-report.json'})
    assert resume(case, runner)['status'] == 'waiting_for_reboot'
    runner.assert_not_called()
    case.boot.return_value = 3000
    assert resume(case, runner)['status'] == 'completed'
    runner.assert_called_once_with(case.config)
    assert resume(case, runner)['status'] == 'no_pending_plan'
    assert preference.read_bytes() == original
    assert json.loads(next((case.data / 'update-resume').glob('*.json')).read_text())['restart_requested'] is False


def test_repeated_capture_same_boot_is_idempotent(case):
    first, second = capture(case), capture(case)
    assert first['plan'] == second['plan'] and second['status'] == 'already_pending'
    assert len(list((case.data / 'update-resume').glob('*.json'))) == 1


def test_native_software_batch_uses_terminal_leaf_receipts_not_missing_aggregate_flag(case):
    leaf = case.report['stages'][0]['result']
    case.report = {'status': 'blocked', 'stages': [{'result': {
        'installer_started': True, 'outcomeKnown': True, 'restartNeeded': True,
        'deferred_ids': ['Fixture.Next'], 'items': [{'id': 'Fixture.App', 'status': 'completed', 'result': leaf}]}}]}
    assert capture(case)['status'] == 'pending_reboot'


def test_boot_clock_sampling_drift_is_not_a_user_reboot(case):
    capture(case)
    case.boot.return_value = 1002
    runner = Mock()
    assert resume(case, runner)['status'] == 'waiting_for_reboot'
    runner.assert_not_called()


@pytest.mark.parametrize('change', [
    {'outcomeKnown': False}, {'running': True}, {'outcomeKnown': None},
    {'status': 'uncertain'}, {'status': 'running'}, {'status': 'prepared'},
])
def test_unknown_or_live_installer_cannot_create_resume_intent(case, change):
    case.report['stages'][0]['result'].update(change)
    assert capture(case)['status'] == 'unresolved_installer'
    assert not (case.data / 'update-resume').exists()


def test_no_plan_without_explicit_native_restart_evidence(case):
    case.report = {'status': 'blocked', 'note': 'Restart required, but only a message.'}
    assert capture(case)['status'] == 'no_restart_required'


@pytest.mark.parametrize('key,value', [
    ('software_update_ids', ['Other.App']), ('software_update_policy', 'all_supported'),
    ('software_updates_enabled', False), ('driver_updates_enabled', True),
    ('software_update_agreements_accepted', False),
])
def test_changed_approvals_do_not_resume(case, key, value):
    capture(case)
    case.boot.return_value = 3000
    case.config[key] = value
    runner = Mock()
    assert resume(case, runner)['status'] == 'approval_changed'
    runner.assert_not_called()


def test_live_barrier_after_reboot_preserves_pending_plan(case):
    capture(case)
    case.boot.return_value = 3000
    case.backend.pending_updates_barrier.return_value = 'Another installer is still active.'
    runner = Mock()
    assert resume(case, runner)['status'] == 'blocked'
    runner.assert_not_called()
    case.backend.pending_updates_barrier.return_value = None
    runner.return_value = {'status': 'completed'}
    assert resume(case, runner)['status'] == 'completed'


def test_new_package_manager_agreement_cannot_broaden_saved_reboot_plan(case):
    capture(case)
    case.boot.return_value = 3000
    case.config['toolkit_settings']['package_managers'] = {'chocolatey_enabled': True, 'agreements_accepted': True}
    runner = Mock()
    assert resume(case, runner)['status'] == 'approval_changed'
    runner.assert_not_called()


def test_exception_after_started_receipt_never_replays(case):
    capture(case)
    case.boot.return_value = 3000
    runner = Mock(side_effect=RuntimeError('Installer may have started before callback failed.'))
    with pytest.raises(RuntimeError):
        resume(case, runner)
    runner.reset_mock()
    assert resume(case, runner)['status'] == 'needs_review'
    runner.assert_not_called()


def test_new_pending_plan_cannot_hide_an_older_started_continuation(case):
    from pathlib import Path
    saved = capture(case)
    path = Path(saved['plan'])
    old = json.loads(path.read_text())
    old.update(id='0' * 32, status='started')
    (path.parent / ('0' * 32 + '.json')).write_text(json.dumps(old))
    case.boot.return_value = 3000
    runner = Mock()
    assert resume(case, runner)['status'] == 'needs_review'
    runner.assert_not_called()


def test_terminal_unknown_result_never_becomes_replayable(case):
    capture(case)
    case.boot.return_value = 3000
    runner = Mock(return_value={'status': 'blocked', 'outcomeKnown': False, 'running': True})
    assert resume(case, runner)['status'] == 'needs_review'
    assert resume(case, runner)['status'] == 'no_pending_plan'
    runner.assert_called_once()


@pytest.mark.parametrize('result', [None, {}, {'status': 'running'}])
def test_missing_or_unfinished_runner_report_cannot_complete_intent(case, result):
    capture(case)
    case.boot.return_value = 3000
    assert resume(case, Mock(return_value=result))['status'] == 'needs_review'


def test_cancelled_resume_is_reported_as_cancelled_and_never_replayed(case):
    capture(case)
    case.boot.return_value = 3000
    runner = Mock(return_value={'status': 'cancelled', 'installer_started': False,
                              'outcomeKnown': True, 'running': False})
    assert resume(case, runner)['status'] == 'cancelled'
    assert resume(case, runner)['status'] == 'no_pending_plan'
    runner.assert_called_once()


@pytest.mark.parametrize('field,value', [('owner', 'another-owner'), ('kind', 'unknown'), ('scope_sha256', '0' * 64), ('schema', True)])
def test_tampered_plan_never_calls_runner(case, field, value):
    saved = capture(case)
    from pathlib import Path
    path = Path(saved['plan'])
    record = json.loads(path.read_text())
    record[field] = value
    path.write_text(json.dumps(record))
    case.boot.return_value = 3000
    runner = Mock()
    with pytest.raises(ValueError):
        resume(case, runner)
    runner.assert_not_called()


def test_configuration_opt_in_and_lease_are_both_required(case):
    case.config['toolkit_settings']['resume']['enabled'] = False
    assert capture(case)['status'] == 'disabled'
    assert resume(case, Mock())['status'] == 'disabled'
    with pytest.raises(ValueError):
        case.service.capture(case.config, case.report, lease=None)
    assert not (case.data / 'update-resume').exists()


def test_runner_receives_copy_not_mutable_saved_approval(case):
    capture(case)
    case.boot.return_value = 3000
    original = copy.deepcopy(case.config)
    def runner(config):
        config['software_update_ids'].append('Never.Persist')
        return {'status': 'completed'}
    assert resume(case, runner)['status'] == 'completed'
    assert case.config == original


def test_boot_identity_is_native_and_bounded():
    assert type(boot_identity()) is int and boot_identity() > 0
