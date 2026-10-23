"""Blocked follow-ups are explicit reviews, with no fabricated fresh searches."""
import copy
import json
from pathlib import Path

import pytest

from optimus.parallel_updates import update_completion_state
from optimus.post_run_fixes import CHANNELS, _prior_installer_barrier
from test_native_post_run_fixes import backend, package, parent, software_report, software_settings


DRIVER = {'id': '00000000-0000-0000-0000-000000000001', 'revision': 1, 'title': 'Fixture driver'}


def all_update_settings():
    return software_settings(windows_security_updates_enabled=True,
        windows_security_updates_install_approved=True, windows_security_update_agreements_accepted=True,
        driver_updates_enabled=True, driver_policy='install_approved', driver_changes_approved=True,
        driver_update_agreements_accepted=True, driver_approved_updates=[DRIVER])


def source_report(settings, **software_changes):
    return parent(settings, security=['enable_firewall'],
        software=software_report(**software_changes),
        windows={'scan': {'updates': [{'id': 'Fixture.WindowsSecurity'}]}},
        drivers={'scan': {'updates': [DRIVER]}})


@pytest.mark.parametrize('old', [
    {'outcomeKnown': False, 'running': True},
    {'restartNeeded': True},
    {'installation_blocked': True, 'blocked_by_prior_installer': True,
     'stop_reason': 'Fixture previous driver installer remains unresolved.'},
])
def test_known_prior_barrier_preview_is_pure_manual_for_updates_and_keeps_independent_security(tmp_path, old):
    settings = all_update_settings()
    source = source_report(settings, **old)
    service = backend(tmp_path, settings, source)
    before = copy.deepcopy(source)
    shown = service.available_fixes(source, settings)
    assert [row['id'] for row in shown['actions']] == ['enable_firewall']
    manual = {row['id']: row for row in shown['manual_items']}
    assert set(manual) == set(CHANNELS)
    reason = _prior_installer_barrier(service, source)
    assert reason and all(row['reason'] == reason for row in manual.values())
    assert source == before and list(tmp_path.iterdir()) == []
    for name in ('preferences', 'security_status', 'audit', 'updates_scan', 'update_app_unattended',
                 'windows_security_updates', 'driver_updates_scan', 'apply_security_recommendation'):
        getattr(service, name).assert_not_called()


def test_execution_of_known_blocked_plan_applies_security_only_without_pretending_updates_were_attempted(tmp_path):
    settings = all_update_settings()
    source = source_report(settings, outcomeKnown=False, running=True)
    before = copy.deepcopy(source)
    service = backend(tmp_path, settings, source)
    result = service.apply_available_fixes(source, settings)
    service.apply_security_recommendation.assert_called_once_with('enable_firewall', cancel_event=None)
    assert result['counts']['completed'] == 1 and result['counts']['deferred'] == 0
    assert {row['id'] for row in result['manual_items']} == set(CHANNELS)
    assert not any(row.get('action_id') in CHANNELS for row in result['stages'])
    assert source == before
    for name in ('updates_scan', 'update_app_unattended', 'windows_security_updates', 'driver_updates_scan'):
        getattr(service, name).assert_not_called()
    assert result['final_verification']['counts']['needs_review'] >= 3


@pytest.mark.parametrize('channel', list(CHANNELS))
def test_barrier_discovered_after_preview_has_durable_known_no_launch_receipt_and_unsearched_label(tmp_path, channel):
    settings = all_update_settings()
    outcome = {'scan': {'updates': [package()]}, 'items': [], 'failed_items': 0}
    # Historical source coverage is evidence from the parent, never this run.
    scan_key = {'software_updates': 'app_scan', 'driver_updates': 'driver_scan'}.get(channel)
    if scan_key:
        outcome[scan_key] = {'schema': 1, 'complete': True, 'status': 'completed', 'offers_count': 777}
    source = parent(settings, **{
        {'software_updates': 'software', 'windows_security_updates': 'windows', 'driver_updates': 'drivers'}[channel]: outcome})
    service = backend(tmp_path, settings, source)
    shown = service.available_fixes(source, settings)
    assert [row['id'] for row in shown['actions']] == [channel]
    service.security_status.return_value['reboot_required'] = True
    result = service.apply_available_fixes(source, settings)
    stage = next(row for row in result['stages'] if row.get('action_id') == channel)
    receipt = stage['result']
    assert stage['action_status'] == 'deferred' and result['counts']['deferred'] == 1
    assert receipt['installer_started'] is False and receipt['outcomeKnown'] is True and receipt['running'] is False
    assert receipt['installation_blocked'] is True and receipt['blocked_by_prior_installer'] is False
    assert receipt['stop_reason'] == receipt['installation_block_reason'] == result['stop_reason']
    assert 'restart' in receipt['stop_reason'].lower() and receipt['items'] == []
    assert 'scan' not in receipt
    if scan_key:
        assert receipt[scan_key]['status'] == 'not_started' and receipt[scan_key]['complete'] is False
        state = update_completion_state(receipt)
        assert state['scan_label'] == 'Scan not started' and state['scan_status'] == 'not_started'
        assert state['installation_blocked'] is True
        assert '777' not in state['description']
    saved = json.loads(Path(result['log_path']).read_text(encoding='utf-8'))
    assert next(row['result'] for row in saved['stages'] if row.get('action_id') == channel) == receipt
    for name in ('updates_scan', 'update_app_unattended', 'windows_security_updates', 'driver_updates_scan'):
        getattr(service, name).assert_not_called()


def test_new_unknown_app_outcome_keeps_later_channels_unsearched_and_block_reason_saved(tmp_path):
    settings = all_update_settings()
    source = source_report(settings)
    service = backend(tmp_path, settings, source)
    service.update_app_unattended.return_value = {'exitCode': None,
        'installer_started': True, 'outcomeKnown': False, 'running': True}
    result = service.apply_available_fixes(source, settings)
    assert result['counts']['deferred'] == 2
    later = [row for row in result['stages'] if row.get('action_id') in ('windows_security_updates', 'driver_updates')]
    assert len(later) == 2
    for row in later:
        receipt = row['result']
        assert receipt['installation_blocked'] is True and receipt['installer_started'] is False
        assert receipt['outcomeKnown'] is True and receipt['running'] is False
        assert receipt['stop_reason'] == result['stop_reason'] and 'uncertain' in receipt['stop_reason'].lower()
        assert 'scan' not in receipt
    service.windows_security_updates.assert_not_called()
    service.driver_updates_scan.assert_not_called()
