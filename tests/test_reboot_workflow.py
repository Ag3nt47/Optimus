"""A reboot intent joins one fresh approved workflow, with one global lease."""
import copy
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from optimus.automation import RunLease
from optimus.reboot_workflow import run_with_reboot_resume
from optimus.update_resume import UpdateResumeService
from optimus.workflow import DEFAULTS, Preferences, atomic_json


@pytest.fixture
def case(tmp_path, monkeypatch):
    config = copy.deepcopy(DEFAULTS)
    config.update(setup_version=2, cleanup_enabled=False, software_updates_enabled=False,
                  driver_updates_enabled=False)
    config['toolkit_settings']['resume']['enabled'] = True
    config = Preferences.validate(config)
    backend = SimpleNamespace(data_dir=tmp_path, system='win32',
        preference_store=SimpleNamespace(validate=Preferences.validate),
        preferences=lambda: copy.deepcopy(config), pending_updates_barrier=Mock(return_value=None))
    boot = Mock(return_value=1000)
    monkeypatch.setattr('optimus.update_resume.boot_identity', boot)
    report_path = tmp_path / 'runs' / ('f' * 32 + '.json')
    def fresh(settings, _progress, _cancel, *, browser_prompt=None, _lease=None):
        assert settings == config
        if _lease is not None:
            assert _lease.handle is not None
            with pytest.raises(RuntimeError):
                RunLease(tmp_path).acquire()
        report = {'id': 'f' * 32, 'status': 'completed', 'stages': [], 'log_path': str(report_path)}
        atomic_json(report_path, report)
        return report
    backend.workflow = SimpleNamespace(run=Mock(side_effect=fresh))
    preference = tmp_path / 'preferences.json'
    preference.write_bytes(b'{"fixture preferences": "must remain exact"}\n')
    return SimpleNamespace(config=config, backend=backend, boot=boot, data=tmp_path,
        preference=preference, original=preference.read_bytes(), report_path=report_path)


def pending(case):
    terminal = {'status': 'completed', 'result': {'restartNeeded': True,
        'installer_started': True, 'outcomeKnown': True, 'running': False}}
    with RunLease(case.data) as lease:
        return UpdateResumeService(case.backend).capture(case.config, terminal, lease=lease)


def run(case):
    return run_with_reboot_resume(case.backend, case.config, lambda _: None)


def test_eligible_reboot_intent_runs_full_workflow_once_and_preserves_preferences(case):
    saved = pending(case)
    case.boot.return_value = 3000
    report = run(case)
    case.backend.workflow.run.assert_called_once()
    metadata = report['automatic_reboot_resume']
    assert metadata['status'] == 'completed' and metadata['workflow_runs_started'] == 1
    assert metadata['additional_update_queues_started'] == 0
    receipt = json.loads(Path(saved['plan']).read_text())
    assert receipt['status'] == 'completed' and receipt['execution_mode'] == 'fresh_approved_maintenance'
    assert receipt['updates_only'] is True  # The intent itself grants no cleanup permission.
    assert json.loads(case.report_path.read_text()) == report
    assert case.preference.read_bytes() == case.original
    with RunLease(case.data):
        pass  # Outer owner released its lease after receipt persistence.


@pytest.mark.parametrize('mode,status', [('no_plan', 'no_pending_plan'), ('same_boot', 'waiting_for_reboot'),
    ('changed_scope', 'approval_changed'), ('native_barrier', 'blocked')])
def test_ineligible_intent_still_runs_separately_approved_workflow_exactly_once(case, mode, status):
    if mode != 'no_plan':
        pending(case)
    if mode in ('changed_scope', 'native_barrier'):
        case.boot.return_value = 3000
    if mode == 'changed_scope':
        case.config['toolkit_settings']['history']['sample_seconds'] = 6
    if mode == 'native_barrier':
        case.backend.pending_updates_barrier.return_value = 'Fixture native operation remains unresolved.'
    report = run(case)
    assert report['automatic_reboot_resume']['status'] == status
    case.backend.workflow.run.assert_called_once()
    assert case.preference.read_bytes() == case.original


def test_disabled_resume_does_not_probe_boot_create_plan_or_own_extra_lease(case):
    case.config['toolkit_settings']['resume']['enabled'] = False
    report = run(case)
    case.boot.assert_not_called()
    assert 'automatic_reboot_resume' not in report
    assert '_lease' not in case.backend.workflow.run.call_args.kwargs
    assert not (case.data / 'update-resume').exists()
    assert not (case.data / 'maintenance-run.lock').exists()


def test_runner_failure_after_claim_preserves_started_intent_and_never_restarts_same_request(case):
    saved = pending(case)
    case.boot.return_value = 3000
    case.backend.workflow.run.side_effect = RuntimeError('Fixture vendor callback failed after launch.')
    with pytest.raises(RuntimeError, match='vendor callback'):
        run(case)
    case.backend.workflow.run.assert_called_once()
    assert json.loads(Path(saved['plan']).read_text())['status'] == 'started'
    with RunLease(case.data):
        pass


def test_unreadable_plan_is_reported_and_never_rewritten(case):
    saved = pending(case)
    path = Path(saved['plan'])
    original = b'broken fixture JSON'
    path.write_bytes(original)
    case.boot.return_value = 3000
    report = run(case)
    assert report['automatic_reboot_resume']['status'] == 'unavailable'
    assert path.read_bytes() == original
    case.backend.workflow.run.assert_called_once()


def test_stale_requested_update_scope_cannot_consume_current_saved_reboot_intent(case):
    saved = pending(case)
    case.boot.return_value = 3000
    current_saved = copy.deepcopy(case.config)
    current_saved['toolkit_settings']['history']['sample_seconds'] = 6
    case.backend.preferences = Mock(return_value=current_saved)
    report = run(case)
    assert report['automatic_reboot_resume']['status'] == 'approval_changed'
    assert json.loads(Path(saved['plan']).read_text())['status'] == 'pending'
    case.backend.workflow.run.assert_called_once()


def test_saved_scope_change_during_claim_stops_before_workflow_and_preserves_receipt(case):
    saved = pending(case)
    case.boot.return_value = 3000
    changed = copy.deepcopy(case.config)
    changed['software_updates_enabled'] = True
    case.backend.preferences = Mock(side_effect=[copy.deepcopy(case.config), changed])
    with pytest.raises(ValueError, match='approvals changed'):
        run(case)
    case.backend.workflow.run.assert_not_called()
    assert json.loads(Path(saved['plan']).read_text())['status'] == 'started'


def test_manual_resume_default_runner_remains_update_only(case):
    saved = pending(case)
    case.boot.return_value = 3000
    runner = Mock(return_value={'status': 'completed'})
    with RunLease(case.data) as lease:
        result = UpdateResumeService(case.backend).resume(case.config, runner, lease=lease)
    assert result['status'] == 'completed'
    assert json.loads(Path(saved['plan']).read_text())['execution_mode'] == 'updates_only'
    case.backend.workflow.run.assert_not_called()


@pytest.mark.parametrize('native', [False, True])
def test_real_windows_and_native_workflow_borrow_lease_without_releasing_owner(tmp_path, native):
    from test_workflow import MockBackend
    from optimus.workflow import Workflow
    from optimus.native_workflow import NativeWorkflow
    backend = MockBackend(tmp_path)
    backend.system = 'linux' if native else 'win32'
    backend.preference_store = SimpleNamespace(validate=Preferences.validate)
    config = copy.deepcopy(DEFAULTS)
    config.update(setup_version=2, cleanup_enabled=False, browser_cache_enabled=False,
                  software_updates_enabled=False, driver_updates_enabled=False, drive_space_guard_enabled=False)
    workflow = (NativeWorkflow if native else Workflow)(backend)
    with RunLease(tmp_path) as lease:
        report = workflow.run(config, lambda _: None, _lease=lease)
        assert report['status'] == 'completed'
        assert lease.handle is not None and workflow.active is False
        with pytest.raises(RuntimeError):
            RunLease(tmp_path).acquire()
    assert [row[0] for row in backend.calls] == ['audit', 'audit']


@pytest.mark.parametrize('native', [False, True])
def test_real_workflow_rejects_wrong_root_lease_before_any_host_operation(tmp_path, native):
    from test_workflow import MockBackend
    from optimus.workflow import Workflow
    from optimus.native_workflow import NativeWorkflow
    backend = MockBackend(tmp_path / 'Data')
    backend.system = 'linux' if native else 'win32'
    backend.preference_store = SimpleNamespace(validate=Preferences.validate)
    workflow = (NativeWorkflow if native else Workflow)(backend)
    with RunLease(tmp_path / 'Other') as lease:
        with pytest.raises(ValueError, match='supplied maintenance lease'):
            workflow.run(copy.deepcopy(DEFAULTS), lambda _: None, _lease=lease)
        assert lease.handle is not None
    assert backend.calls == [] and not workflow.active
