"""Recommendation installation uses mocked Windows APIs and private fixtures."""
from __future__ import annotations

import copy
from contextlib import nullcontext
import json
from pathlib import Path
import threading
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from optimus.automation import RunLease
from optimus.backend import Backend
from optimus.workflow import DEFAULTS, Preferences


UPDATE = '00000000-0000-0000-0000-000000000001'


def service(tmp_path, *, approved=True):
    settings = dict(copy.deepcopy(DEFAULTS), windows_security_updates_enabled=True,
                    windows_security_updates_install_approved=approved,
                    windows_security_update_agreements_accepted=approved)
    verified = {'schema': 1, 'read_only': True, 'status': 'confirmed', 'coverage_complete': True,
        'cancelled': False, 'deadline_reached': False, 'errors': [],
        'failed_items': 0, 'counts': {'confirmed': 1, 'needs_review': 0, 'unverified': 0, 'not_applicable': 0},
        'stages': [{'title': 'Check or install Windows security updates',
                    'checks': [{'kind': 'local_update_history', 'target': UPDATE, 'status': 'confirmed',
                                'evidence': {'revision': 1}}]}]}
    tool = SimpleNamespace(data_dir=tmp_path,
        preference_store=SimpleNamespace(validate=Preferences.validate),
        preferences=Mock(return_value=settings),
        workflow=SimpleNamespace(lock=threading.Lock(), active=False),
        windows_security_updates=Mock(return_value={
            'scan': {'updates': [{'id': UPDATE, 'revision': 1}]}, 'failed_items': 0,
            'items': [{'id': UPDATE, 'status': 'completed', 'result': {'ok': True}}]}),
        verify_optimization=Mock(return_value=verified),
        _privacy_service=Mock(return_value=SimpleNamespace(apply_recommendation=Mock(
            return_value={'status': 'manual_action_required', 'opened': 'ms-settings:windowsupdate'}))))
    tool._speed_maintenance = Backend._speed_maintenance.__get__(tool, Backend)
    tool.apply_security_recommendation = Backend.apply_security_recommendation.__get__(tool, Backend)
    tool.verify_optimization.side_effect = lambda report, **kwargs: dict(
        copy.deepcopy(tool.verify_optimization.return_value), run_id=report['id'])
    return tool, settings


def test_approved_windows_recommendation_installs_under_global_lease_then_verifies_and_journals(tmp_path):
    tool, settings = service(tmp_path)
    original = copy.deepcopy(settings)
    def install(configuration, **kwargs):
        assert configuration['windows_security_updates_install_approved'] is True
        assert configuration['windows_security_update_agreements_accepted'] is True
        assert tool.workflow.active and tool.workflow.lock.locked()
        with pytest.raises(RuntimeError):
            RunLease(tmp_path).acquire()
        prepared = json.loads(next((tmp_path / 'security-recommendations').glob('*.json')).read_text())
        assert prepared['status'] == 'running' and prepared['stages'][0]['status'] == 'prepared'
        return {'scan': {'updates': [{'id': UPDATE, 'revision': 1}]}, 'failed_items': 0,
                'items': [{'id': UPDATE, 'status': 'completed', 'result': {'ok': True}}]}
    tool.windows_security_updates.side_effect = install
    result = tool.apply_security_recommendation('review_windows_update', allow_saved_update_install=True)
    assert result['status'] == 'completed' and result['verified_installs'] == 1
    assert result['installed_count'] == 1 and result['verified'] is True
    assert result['preferences_saved'] is False and settings == original
    assert json.loads(Path(result['log_path']).read_text())['status'] == 'completed'
    tool._privacy_service.assert_not_called()
    tool.verify_optimization.assert_called_once()
    assert not tool.workflow.active and not tool.workflow.lock.locked()


def test_legacy_or_open_windows_recommendation_cannot_turn_into_install_when_preferences_change(tmp_path):
    tool, settings = service(tmp_path)
    result = tool.apply_security_recommendation('review_windows_update')
    assert result['status'] == 'manual_action_required'
    tool.windows_security_updates.assert_not_called()
    tool.preferences.assert_not_called()


@pytest.mark.parametrize('flag', [1, 'true', None])
def test_install_route_rejects_nonboolean_presentation_authorization(tmp_path, flag):
    tool, settings = service(tmp_path)
    with pytest.raises(ValueError, match='explicitly'):
        tool.apply_security_recommendation('review_windows_update', allow_saved_update_install=flag)
    tool.windows_security_updates.assert_not_called()
    tool._privacy_service.assert_not_called()


@pytest.mark.parametrize('field', ['windows_security_updates_enabled',
    'windows_security_updates_install_approved', 'windows_security_update_agreements_accepted'])
def test_unapproved_recommendation_only_opens_manual_windows_controls(tmp_path, field):
    tool, settings = service(tmp_path)
    settings[field] = False
    if field == 'windows_security_update_agreements_accepted':
        settings['windows_security_updates_install_approved'] = False
    result = tool.apply_security_recommendation('review_windows_update', allow_saved_update_install=True)
    assert result['status'] == 'manual_action_required'
    tool.windows_security_updates.assert_not_called()
    tool.verify_optimization.assert_not_called()
    assert not (tmp_path / 'security-recommendations').exists()


def test_recommendation_rechecks_revoked_saved_approval_after_acquiring_lease(tmp_path):
    tool, settings = service(tmp_path)
    revoked = dict(settings, windows_security_updates_install_approved=False)
    tool.preferences.side_effect = [settings, revoked]
    result = tool.apply_security_recommendation('review_windows_update', allow_saved_update_install=True)
    assert result['status'] == 'blocked'
    tool.windows_security_updates.assert_not_called()
    assert 'revoked' in result['reason']


def test_recommendation_does_not_start_installer_over_unresolved_owned_update(tmp_path, monkeypatch):
    tool, settings = service(tmp_path)
    monkeypatch.setattr('optimus.parallel_updates.pending_updates_barrier',
                        Mock(return_value='An original installer is still observed.'))
    result = tool.apply_security_recommendation('review_windows_update', allow_saved_update_install=True)
    assert result['status'] == 'blocked' and result['installation_blocked'] is True
    tool.windows_security_updates.assert_not_called()


def test_recommendation_preflight_exception_records_known_no_launch(tmp_path, monkeypatch):
    tool, settings = service(tmp_path)
    monkeypatch.setattr('optimus.parallel_updates.pending_updates_barrier',
                        Mock(side_effect=ValueError('private preflight fixture')))
    result = tool.apply_security_recommendation('review_windows_update', allow_saved_update_install=True)
    assert result['status'] == 'blocked' and result['operation_started'] is False
    assert result['outcomeKnown'] is True and result['running'] is False and result['may_continue'] is False
    tool.windows_security_updates.assert_not_called()
    assert 'private preflight fixture' not in json.dumps(result)


@pytest.mark.parametrize('status', ['needs_review', 'unverified'])
def test_native_success_without_exact_history_confirmation_is_not_reported_installed(tmp_path, status):
    tool, settings = service(tmp_path)
    verified = tool.verify_optimization.return_value
    verified.update(status=status, counts={'confirmed': 0, 'needs_review': int(status == 'needs_review'),
                                          'unverified': int(status == 'unverified'), 'not_applicable': 0})
    verified['stages'][0]['checks'][0]['status'] = status
    result = tool.apply_security_recommendation('review_windows_update', allow_saved_update_install=True)
    assert result['status'] == 'needs_review' and result['verified_installs'] == 0
    assert result['installed_count'] == 1 and result['verified'] is False
    assert result['final_verification']['status'] == status


@pytest.mark.parametrize('issue', ['stale_run', 'wrong_revision', 'wrong_stage', 'duplicate_check',
    'cancelled', 'deadline_reached', 'wrong_counts'])
def test_history_confirmation_is_bound_to_this_run_exact_revision_and_complete_readback(tmp_path, issue):
    tool, settings = service(tmp_path)
    verified = tool.verify_optimization.return_value
    if issue == 'stale_run':
        tool.verify_optimization.side_effect = lambda *args, **kwargs: dict(verified, run_id='a' * 32)
    elif issue == 'wrong_revision':
        verified['stages'][0]['checks'][0]['evidence']['revision'] = 2
    elif issue == 'wrong_stage':
        verified['stages'][0]['title'] = 'Install approved driver updates'
    elif issue == 'duplicate_check':
        verified['stages'][0]['checks'] *= 2
    elif issue == 'wrong_counts':
        verified['counts']['confirmed'] = 0
    else:
        verified[issue] = True
    result = tool.apply_security_recommendation('review_windows_update', allow_saved_update_install=True)
    assert result['status'] == 'needs_review' and result['verified'] is False


def test_native_recommendation_exception_is_durable_uncertain_and_never_retried(tmp_path):
    tool, settings = service(tmp_path)
    tool.windows_security_updates.side_effect = RuntimeError('private fixture payload')
    result = tool.apply_security_recommendation('review_windows_update', allow_saved_update_install=True)
    assert result['status'] == 'uncertain' and result['outcomeKnown'] is False
    assert result['may_continue'] is True and result['running'] is None
    tool.windows_security_updates.assert_called_once()
    tool.verify_optimization.assert_not_called()
    assert 'private fixture payload' not in json.dumps(result)
    assert json.loads(Path(result['log_path']).read_text())['status'] == 'uncertain'


def test_no_applicable_windows_updates_remains_skipped_without_an_install_claim(tmp_path):
    tool, settings = service(tmp_path)
    tool.windows_security_updates.return_value = {'scan': {'updates': []}, 'items': [],
        'failed_items': 0, 'note': 'No applicable Security/Critical update revisions are offered.'}
    result = tool.apply_security_recommendation('review_windows_update', allow_saved_update_install=True)
    assert result['status'] == 'skipped' and result['installed_count'] == 0
    assert result['verified'] is False and result['stages'][0]['status'] == 'skipped'


def test_cancelled_recommendation_never_calls_native_installer(tmp_path):
    tool, settings = service(tmp_path)
    cancelled = threading.Event()
    cancelled.set()
    result = tool.apply_security_recommendation('review_windows_update', cancel_event=cancelled, allow_saved_update_install=True)
    assert result['status'] == 'cancelled' and result['stages'] == []
    tool.windows_security_updates.assert_not_called()


@pytest.mark.parametrize('identity', ['enable_defender_realtime', 'enable_firewall',
    'update_defender_signatures', 'remediate_defender_threats'])
def test_security_adapter_adds_terminal_lifecycle_only_after_observed_host_exit(tmp_path, monkeypatch, identity):
    from optimus import system_privacy
    process = Mock(returncode=1)
    process.communicate.return_value = (json.dumps({'ok': False}), '')
    controller = Mock()
    controller.bind_cancel.return_value = threading.Event()
    controller.launch_gate.return_value = nullcontext()
    ticket = controller.begin_native.return_value
    monkeypatch.setattr(system_privacy, 'os', SimpleNamespace(name='nt', environ={}))
    monkeypatch.setattr(system_privacy.subprocess, 'Popen', Mock(return_value=process))
    result = system_privacy._native_request(identity, stop_controller=controller)
    assert result['ok'] is False and result['outcomeKnown'] is True
    assert result['running'] is False and result['may_continue'] is False
    ticket.finish.assert_called_once_with()
    controller.observe_native_child.assert_not_called()
