"""Post-run fixes use disposable journals and mocked maintenance services only."""
import copy
import json
from pathlib import Path
import threading
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from optimus.automation import CURRENT_SETUP_VERSION, RunLease
from optimus.backend import Backend
from optimus.emergency_stop import controller_for
from optimus.native_backend import NativeBackend
from optimus.native_settings import NativePreferences
from optimus.post_run_fixes import SECURITY_FIXES
from optimus.verification import OutcomeVerification
from optimus.workflow import DEFAULTS, FINAL_VERIFICATION_STAGE, Preferences


def configuration(**changes):
    value = dict(copy.deepcopy(DEFAULTS), setup_version=CURRENT_SETUP_VERSION,
                 cleanup_enabled=False, browser_cache_enabled=False)
    value.update(changes)
    return value


def software_settings(**changes):
    value = configuration(software_updates_enabled=True, software_update_policy='selected',
                          software_update_ids=['Fixture.One'], software_update_agreements_accepted=True)
    value.update(changes)
    return value


def package(identity='Fixture.One'):
    return {'id': identity, 'package_id': identity, 'source': 'winget', 'version': '1.0', 'available': '2.0'}


def software_report(rows=None, **changes):
    value = {'scan': {'structured': True, 'exitCode': 0, 'source': 'winget',
                       'updates': [package()] if rows is None else rows}, 'items': [], 'failed_items': 0}
    value.update(changes)
    return value


def parent(settings, *, security=(), software=None, windows=None, drivers=None, process=None):
    value = {'id': 'a' * 32, 'status': 'completed', 'stages': [], 'cancelled': False,
             'settings': copy.deepcopy(settings),
             'security_review': {'status': 'completed', 'recommendations': [{'id': identity, 'title': 'Untrusted caller title'} for identity in security]}}
    for title, result in (('Review or update configured software', software),
                          ('Check or install Windows security updates', windows),
                          ('Review available driver updates', drivers)):
        if result is not None:
            value['stages'].append({'title': title, 'status': 'completed', 'result': copy.deepcopy(result)})
    if process is not None:
        value['process_review'] = copy.deepcopy(process)
    return value


def verification(report, **kwargs):
    return {'schema': 1, 'read_only': True, 'status': 'not_applicable',
            'counts': {'confirmed': 0, 'needs_review': 0, 'unverified': 0, 'not_applicable': len(report['stages'])},
            'coverage_complete': True, 'failed_items': 0, 'stages': []}


def backend(tmp_path, settings, report, *, native=False):
    service = SimpleNamespace(
        data_dir=tmp_path,
        preference_store=SimpleNamespace(validate=NativePreferences.validate if native else Preferences.validate),
        preferences=Mock(return_value=copy.deepcopy(settings)),
        workflow=SimpleNamespace(lock=threading.Lock(), active=False, last_report=report),
        apply_security_recommendation=Mock(return_value={'status': 'completed', 'failed_items': 0}),
        security_status=Mock(return_value={'status': 'completed', 'recommendations': [], 'reboot_required': False}),
        audit=Mock(return_value={'Processes': [], 'System': {}, 'InventoryErrors': []}),
        verify_optimization=Mock(side_effect=verification),
        updates_scan=Mock(return_value={'structured': True, 'exitCode': 0, 'source': 'winget', 'updates': [package()]}),
        update_app_unattended=Mock(return_value={'exitCode': 0}),
        windows_security_updates=Mock(return_value={'items': [], 'failed_items': 0}),
        driver_updates_scan=Mock(return_value={'resultCode': 2, 'updates': []}),
        driver_backup_automatic=Mock(side_effect=AssertionError('No fixture driver offer should need backup.')),
        process_harness_apply=Mock(side_effect=AssertionError('Batch must not pause processes.')),
        startup_set=Mock(side_effect=AssertionError('Batch must not change startup.')),
        cleanup_apply=Mock(side_effect=AssertionError('Batch must not repeat cleanup.')),
        uninstall=Mock(side_effect=AssertionError('Batch must not uninstall applications.')),
    )
    if native:
        service.system = 'linux'
    service.available_fixes = Backend.available_fixes.__get__(service, Backend)
    service.apply_available_fixes = Backend.apply_available_fixes.__get__(service, Backend)
    return service


def assert_stop_bound_call(service, operation, *args, **kwargs):
    """Keep exact action/scope checks and prove its token receives STOP."""
    token = operation.call_args.kwargs.get('cancel_event')
    assert token is not None and callable(getattr(token, 'is_set', None))
    assert not token.is_set()
    operation.assert_called_once_with(*args, cancel_event=token, **kwargs)
    controller_for(service).request_stop()
    assert token.is_set()


def test_preview_is_pure_canonical_and_excludes_optional_process_actions(tmp_path):
    settings = configuration()
    source = parent(settings, security=[*SECURITY_FIXES, 'review_security', 'restart_required', 'arbitrary-command'],
                    process={'id': 'b' * 32, 'actions': [{'id': 'c' * 32, 'kind': 'sleep_process', 'selected': False}]})
    service = backend(tmp_path, settings, source)
    before = copy.deepcopy(source)
    preview = service.available_fixes(source, settings)
    assert {row['id'] for row in preview['actions']} == set(SECURITY_FIXES)
    assert {row['title'] for row in preview['actions']} == set(SECURITY_FIXES.values())
    assert {row['id'] for row in preview['manual_items']} == {'review_security', 'restart_required', 'process_review'}
    assert source == before and not list(tmp_path.iterdir())
    for name in ('preferences', 'security_status', 'updates_scan', 'audit', 'apply_security_recommendation'):
        getattr(service, name).assert_not_called()


def test_saved_automatic_continuation_never_approves_defender_threat_removal(tmp_path):
    settings = configuration(auto_continue_fixes_enabled=True)
    source = parent(settings, security=['remediate_defender_threats'])
    service = backend(tmp_path, settings, source)
    preview = service.available_fixes(source, settings)
    assert preview['actions'] == []
    assert [item['id'] for item in preview['manual_items']] == ['remediate_defender_threats']
    service.apply_available_fixes(source, settings)
    service.apply_security_recommendation.assert_not_called()


@pytest.mark.parametrize('flags', [
    {'navigation_cancelled': True}, {'navigation_cancelled_items': 1},
    {'items': [{'result': {'navigation_cancelled': True}}]},
    {'app_presence': {'navigation_cancelled_items': 1}},
])
def test_explicit_worker_skip_cannot_be_retried_by_auto_continue(tmp_path, flags):
    settings = software_settings()
    outcome = software_report(**flags)
    source = parent(settings, software=outcome)
    service = backend(tmp_path, settings, source)
    preview = service.available_fixes(source, settings)
    assert not any(row['id'] == 'software_updates' for row in preview['actions'])
    assert any(row['id'] == 'software_updates' and 'skipped or cancelled' in row['reason'] for row in preview['manual_items'])
    service.update_app_unattended.assert_not_called()


def test_followup_preserves_exact_update_rules_but_never_repeats_presence(tmp_path):
    from optimus.post_run_fixes import _narrow_settings, _authorized
    rule = {'id': 'Fixture.One', 'source': 'winget', 'update_policy': 'always_update', 'presence': 'keep_installed'}
    settings = software_settings(software_update_policy='review', software_app_rules=[rule])
    service = backend(tmp_path, settings, parent(settings))
    narrowed = _narrow_settings(service, settings, copy.deepcopy(settings))
    assert _authorized(narrowed, 'software_updates')
    assert narrowed['software_app_rules'] == [dict(rule, presence='keep_current')]
    assert settings['software_app_rules'][0]['presence'] == 'keep_installed'
    twice = _narrow_settings(service, narrowed, copy.deepcopy(settings))
    assert twice['software_updates_enabled'] and _authorized(twice, 'software_updates')
    assert twice['software_app_rules'] == narrowed['software_app_rules']


def test_apply_followup_updates_unchanged_app_rule_without_executing_presence(tmp_path, monkeypatch):
    rule = {'id': 'Fixture.One', 'source': 'winget', 'update_policy': 'always_update', 'presence': 'keep_installed'}
    settings = software_settings(software_update_policy='review', software_app_rules=[rule])
    source = parent(settings, software=software_report())
    service = backend(tmp_path, settings, source)
    presence = Mock(return_value={'items': [], 'failed_items': 0})
    monkeypatch.setattr('optimus.app_presence.run_app_presence', presence)
    result = service.apply_available_fixes(source, settings)
    assert_stop_bound_call(service, service.update_app_unattended, 'Fixture.One')
    assert result['counts']['completed'] == 1
    assert presence.call_args.args[1]['software_app_rules'] == [dict(rule, presence='keep_current')]
    service.uninstall.assert_not_called()


def test_new_or_changed_app_rule_cannot_widen_a_followup(tmp_path):
    from optimus.post_run_fixes import _narrow_settings
    settings = software_settings()
    current = copy.deepcopy(settings)
    current['software_app_rules'] = [{'id': 'Fixture.New', 'source': 'winget',
        'update_policy': 'always_ignore', 'presence': 'keep_uninstalled'}]
    service = backend(tmp_path, settings, parent(settings))
    narrowed = _narrow_settings(service, settings, current)
    assert narrowed['software_updates_enabled'] is False
    assert narrowed['software_app_rules'] == []


def test_exclusion_union_overflow_cannot_reenable_an_old_excluded_app_via_rule(tmp_path):
    from optimus.post_run_fixes import _narrow_settings, _authorized
    from optimus.software_app_rules import apply_update_rules
    rule = {'id': 'Fixture.Old0', 'source': 'winget', 'update_policy': 'always_update', 'presence': 'keep_current'}
    settings = software_settings(software_update_policy='all_supported', software_update_all_approved=True,
        software_update_exclude_ids=[f'Fixture.Old{index}' for index in range(100)], software_app_rules=[rule])
    current = dict(copy.deepcopy(settings), software_update_exclude_ids=[f'Fixture.New{index}' for index in range(100)])
    service = backend(tmp_path, settings, parent(settings))
    narrowed = _narrow_settings(service, settings, current)
    assert narrowed['software_updates_enabled'] is False
    assert not _authorized(narrowed, 'software_updates')
    assert apply_update_rules(narrowed)['software_updates_enabled'] is False
    assert settings['software_update_exclude_ids'][0] == 'Fixture.Old0'
    service.update_app_unattended.assert_not_called()


def test_followup_has_its_own_live_navigation_session_and_ends_it(tmp_path):
    from optimus.navigation import NavigationHub
    settings = software_settings()
    source = parent(settings, software=software_report())
    service = backend(tmp_path, settings, source)
    service.security_status.return_value = {'reboot_required': False, 'recommendations': []}
    service.workflow.navigation = NavigationHub()
    sessions = []
    def observe(event):
        snapshot = service.workflow.navigation.snapshot()
        if snapshot['active']:
            sessions.append(snapshot['session_id'])
    service.apply_available_fixes(source, settings, progress_callback=observe)
    assert sessions and len(set(sessions)) == 1
    assert service.workflow.navigation.snapshot()['active'] is False
    assert service.workflow.active is False


@pytest.mark.parametrize('process, expected', [
    ({'actions': [], 'complete': True, 'errors': []}, False),
    ({'actions': [], 'complete': False, 'errors': []}, True),
    ({'actions': [], 'complete': True, 'errors': ['Fixture inventory unavailable']}, True),
])
def test_empty_complete_process_inventory_is_optional_not_a_remaining_fix(tmp_path, process, expected):
    settings = configuration()
    source = parent(settings, process=process)
    preview = backend(tmp_path, settings, source).available_fixes(source, settings)
    assert any(item['id'] == 'process_review' for item in preview['manual_items']) is expected


@pytest.mark.parametrize('changes', [
    {'id': '../outside'}, {'id': 'b' * 32, 'cancelled': True}, {'status': 'running'},
    {'status': 'cancelled'}, {'stages': [None] * 257}, {'stages': None},
])
def test_invalid_parent_reports_have_no_authority(tmp_path, changes):
    settings = configuration()
    source = parent(settings, security=['enable_firewall'])
    service = backend(tmp_path, settings, source)
    forged = dict(source, **changes)
    with pytest.raises(ValueError):
        service.available_fixes(forged, settings)
    with pytest.raises(ValueError):
        service.apply_available_fixes(forged, settings)
    service.apply_security_recommendation.assert_not_called()
    assert not list(tmp_path.iterdir())


def test_exported_different_parent_and_nested_oversized_inputs_are_rejected(tmp_path):
    settings = configuration()
    source = parent(settings, security=['enable_firewall'])
    service = backend(tmp_path, settings, source)
    with pytest.raises(ValueError, match='current Optimus session'):
        service.apply_available_fixes(dict(source, id='b' * 32), settings)
    nested = []
    for _ in range(30):
        nested = [nested]
    with pytest.raises(ValueError, match='bounds'):
        service.available_fixes(dict(source, extra=nested), settings)
    with pytest.raises(ValueError, match='size limit'):
        service.available_fixes(dict(source, extra='x' * (8 * 1024 * 1024 + 1)), settings)
    service.apply_security_recommendation.assert_not_called()


def test_security_batch_is_one_shot_durable_linked_and_preserves_parent(tmp_path):
    settings = configuration()
    source = parent(settings, security=list(SECURITY_FIXES), process={'id': 'b' * 32, 'actions': []})
    before = copy.deepcopy(source)
    service = backend(tmp_path, settings, source)
    def apply(identity, **kwargs):
        files = list((tmp_path / 'post-run-fixes').glob('*.json'))
        saved = json.loads(files[0].read_text())
        assert saved['parent_run_id'] == source['id']
        assert saved['stages'][-1]['action_id'] == identity
        assert saved['stages'][-1]['status'] == 'prepared'
        assert service.workflow.active
        with pytest.raises(RuntimeError):
            RunLease(tmp_path).acquire()
        return {'status': 'completed', 'failed_items': 0}
    service.apply_security_recommendation.side_effect = apply
    events = []
    result = service.apply_available_fixes(source, settings, progress_callback=events.append)
    assert result['status'] == 'completed'
    assert result['counts']['completed'] == len(SECURITY_FIXES)
    assert result['parent_run_id'] == source['id'] and source == before
    assert service.workflow.last_report is source and not service.workflow.active
    assert result['process_review'] == source['process_review']
    assert result['final_verification']['counts']['confirmed'] == len(SECURITY_FIXES)
    assert result['stages'][-1]['title'] == FINAL_VERIFICATION_STAGE
    assert events[-1]['percentage'] == 100
    assert events[-1]['processes'] == []
    assert all(event['processes'] for event in events[:-1])
    assert json.loads(Path(result['log_path']).read_text())['summary'] == result['summary']
    with pytest.raises(ValueError, match='already attempted'):
        service.apply_available_fixes(source, settings)
    assert service.apply_security_recommendation.call_count == len(SECURITY_FIXES)
    for name in ('process_harness_apply', 'startup_set', 'uninstall', 'cleanup_apply'):
        getattr(service, name).assert_not_called()


def test_completed_update_offers_are_not_blindly_repeated_and_missing_scope_is_manual(tmp_path):
    settings = software_settings()
    completed = software_report(items=[{'id': 'Fixture.One', 'status': 'completed'}])
    source = parent(settings, software=completed)
    service = backend(tmp_path, settings, source)
    assert not service.available_fixes(source, settings)['actions']
    remaining = parent(settings, software=software_report())
    assert service.available_fixes(remaining, settings)['actions'][0]['id'] == 'software_updates'
    review = configuration(software_updates_enabled=True)
    preview = service.available_fixes(parent(review, software=software_report()), review)
    assert not preview['actions'] and preview['manual_items'][0]['id'] == 'software_updates'
    service.updates_scan.assert_not_called()


def test_software_uses_fresh_helper_and_current_exclusions_without_adding_new_scope(tmp_path):
    settings = software_settings(software_update_ids=['Fixture.One', 'Fixture.Two'])
    source = parent(settings, software=software_report([package(), package('Fixture.Two')]))
    service = backend(tmp_path, settings, source)
    service.preferences.return_value = software_settings(software_update_policy='all_supported', software_update_all_approved=True,
                                                         software_update_exclude_ids=['Fixture.Two'])
    service.updates_scan.return_value['updates'] = [package(), package('Fixture.Two'), package('Fixture.Three')]
    result = service.apply_available_fixes(source, settings)
    service.updates_scan.assert_called_once()
    assert_stop_bound_call(service, service.update_app_unattended, 'Fixture.One')
    assert result['counts']['completed'] == 1
    assert result['stages'][0]['authorization']['software_update_policy'] == 'selected'
    assert 'actions/checks' in result['summary']


def test_preferences_are_reloaded_between_actions_and_revocation_blocks_installation(tmp_path):
    settings = software_settings()
    source = parent(settings, security=['enable_firewall'], software=software_report())
    service = backend(tmp_path, settings, source)
    revoked = configuration(software_updates_enabled=True, software_update_policy='review')
    service.preferences.side_effect = [settings, revoked]
    result = service.apply_available_fixes(source, settings)
    assert result['counts']['completed'] == 1 and result['counts']['blocked'] == 1
    service.updates_scan.assert_not_called()
    assert result['status'] == 'completed_with_errors'


def test_supplied_snapshot_cannot_broaden_completed_run_approval(tmp_path):
    settings = software_settings()
    source = parent(settings, software=software_report([package(), package('Fixture.Two')]))
    service = backend(tmp_path, settings, source)
    broadened = software_settings(software_update_policy='all_supported', software_update_all_approved=True)
    service.preferences.return_value = broadened
    service.updates_scan.return_value['updates'] = [package(), package('Fixture.Two')]
    result = service.apply_available_fixes(source, broadened)
    assert_stop_bound_call(service, service.update_app_unattended, 'Fixture.One')
    assert result['reviewed_plan']['actions'][0]['id'] == 'software_updates'


@pytest.mark.parametrize('stop', [
    {'restartNeeded': True}, {'reboot_required': True}, {'may_continue': True},
    {'running': True}, {'outcomeKnown': False}, {'requiresReview': True},
    {'status': 'uncertain'}, {'cancelled': True},
])
def test_stop_flags_in_one_channel_defer_every_later_installer(tmp_path, stop):
    settings = software_settings(windows_security_updates_enabled=True,
                                 windows_security_updates_install_approved=True,
                                 windows_security_update_agreements_accepted=True)
    source = parent(settings, software=software_report(), windows={'scan': {'updates': [{'id': 'fixture-wua'}]}})
    service = backend(tmp_path, settings, source)
    service.update_app_unattended.return_value = dict({'exitCode': 0}, **stop)
    result = service.apply_available_fixes(source, settings)
    service.windows_security_updates.assert_not_called()
    assert result['counts']['deferred'] == 1 and result['stop_reason']
    assert result['final_verification']['status'] in ('needs_review', 'unverified')


@pytest.mark.parametrize('outcome', [
    {'items': [], 'failed_items': 1},
    {'items': [{'id': 'Fixture.One', 'status': 'failed'}], 'failed_items': 0},
])
def test_installer_failure_stops_later_channels(tmp_path, monkeypatch, outcome):
    settings = software_settings(windows_security_updates_enabled=True,
                                 windows_security_updates_install_approved=True,
                                 windows_security_update_agreements_accepted=True)
    source = parent(settings, software=software_report(), windows={'scan': {'updates': [{'id': 'fixture-wua'}]}})
    service = backend(tmp_path, settings, source)
    monkeypatch.setattr('optimus.automated_software.run_software', Mock(return_value=outcome))
    result = service.apply_available_fixes(source, settings)
    service.windows_security_updates.assert_not_called()
    assert result['counts']['deferred'] == 1


def terminal_failure(**changes):
    receipt = {'exitCode': 1603, 'installer_started': True, 'outcomeKnown': True, 'running': False,
               'requiresReview': True, 'failure_scope': 'package'}
    receipt.update(changes)
    return {'failed_items': 1, 'requiresReview': True, 'installer_started': False,
            'items': [{'id': 'Fixture.One', 'status': 'failed', 'result': receipt}]}


@pytest.mark.parametrize('unsafe', [None, {'failureKind': 'source_untrusted'}, {'outcomeKnown': False},
    {'restartNeeded': True}, {'installer_started': None}])
def test_followup_known_package_failure_continues_to_driver_but_shared_unknown_restart_do_not(tmp_path, monkeypatch, unsafe):
    driver = {'id': '00000000-0000-0000-0000-000000000001', 'revision': 1, 'title': 'Fixture driver'}
    settings = software_settings(driver_updates_enabled=True, driver_policy='install_approved',
        driver_changes_approved=True, driver_update_agreements_accepted=True, driver_approved_updates=[driver])
    source = parent(settings, software=software_report(), drivers={'scan': {'updates': [driver]}})
    service = backend(tmp_path, settings, source)
    monkeypatch.setattr('optimus.automated_software.run_software', Mock(return_value=terminal_failure(**(unsafe or {}))))
    drivers = Mock(return_value={'items': [], 'failed_items': 0, 'installer_started': False, 'outcomeKnown': True, 'running': False})
    monkeypatch.setattr('optimus.automated_system.run_drivers', drivers)
    result = service.apply_available_fixes(source, settings)
    assert result['counts']['failed'] + result['counts']['uncertain'] >= 1
    if unsafe is None:
        drivers.assert_called_once()
        assert result['counts']['deferred'] == 0 and not result.get('stop_reason')
    else:
        drivers.assert_not_called()
        assert result['counts']['deferred'] == 1 and result['stop_reason']


def test_prior_native_proven_terminal_package_failure_is_allowed_but_missing_receipt_is_not(tmp_path):
    from optimus.post_run_fixes import _prior_installer_barrier
    settings = software_settings(software_update_ids=['apt:fixture'])
    source = parent(settings, software=terminal_failure())
    service = backend(tmp_path, settings, source, native=True)
    assert _prior_installer_barrier(service, source) == ''
    source['stages'][0]['result']['items'][0]['result'].pop('outcomeKnown')
    assert _prior_installer_barrier(service, source)


def test_wrapper_terminal_flags_cannot_substitute_for_actual_failed_item_receipts():
    from optimus.post_run_fixes import _known_terminal_failure
    wrapper = {'status': 'failed', 'result': {'installer_started': True, 'outcomeKnown': True, 'running': False,
        'failed_items': 1, 'items': [{'status': 'failed', 'result': {'exitCode': 1603}}]}}
    assert not _known_terminal_failure(wrapper)


@pytest.mark.parametrize('old_outcome', [
    {'items': [{'result': {'may_continue': True}}]},
    {'items': [{'result': {'outcomeKnown': False}}]},
    {'reboot_required': True},
])
def test_initial_uncertain_or_restart_state_blocks_installers_but_allows_supported_security(tmp_path, old_outcome):
    settings = software_settings()
    old_outcome['scan'] = {'updates': [package()]}
    source = parent(settings, security=['enable_firewall'], software=old_outcome)
    service = backend(tmp_path, settings, source)
    result = service.apply_available_fixes(source, settings)
    service.apply_security_recommendation.assert_called_once()
    service.updates_scan.assert_not_called()
    assert result['counts']['completed'] == 1 and result['counts']['deferred'] == 0
    assert any(item['id'] == 'software_updates' and item['reason'] for item in result['manual_items'])
    assert not any(stage.get('action_id') == 'software_updates' for stage in result['stages'])


@pytest.mark.parametrize('fresh_restart', [True, None])
def test_fresh_pending_or_unknown_restart_blocks_first_installer(tmp_path, fresh_restart):
    settings = software_settings()
    source = parent(settings, software=software_report())
    service = backend(tmp_path, settings, source)
    service.security_status.return_value['reboot_required'] = fresh_restart
    result = service.apply_available_fixes(source, settings)
    service.updates_scan.assert_not_called()
    assert result['counts']['deferred'] == 1


def test_same_id_modified_preview_cannot_add_unseen_active_actions(tmp_path):
    settings = configuration()
    source = parent(settings, security=['enable_firewall', 'update_defender_signatures'])
    service = backend(tmp_path, settings, source)
    shown = copy.deepcopy(source)
    shown['security_review']['recommendations'] = [{'id': 'enable_firewall'}]
    result = service.apply_available_fixes(shown, settings)
    assert_stop_bound_call(service, service.apply_security_recommendation, 'enable_firewall')
    assert len(result['reviewed_plan']['actions']) == 1


def test_exception_stops_later_channels_and_does_not_expose_exception_payload(tmp_path):
    settings = software_settings(windows_security_updates_enabled=True,
                                 windows_security_updates_install_approved=True,
                                 windows_security_update_agreements_accepted=True)
    source = parent(settings, security=['enable_firewall'], software=software_report(),
                    windows={'scan': {'updates': [{'id': 'fixture-wua'}]}})
    service = backend(tmp_path, settings, source)
    service.apply_security_recommendation.side_effect = RuntimeError('private fixture payload')
    result = service.apply_available_fixes(source, settings)
    assert result['counts']['uncertain'] == 1 and result['counts']['deferred'] == 2
    assert 'private fixture payload' not in json.dumps(result)
    service.updates_scan.assert_not_called()
    service.windows_security_updates.assert_not_called()


def test_cancel_from_progress_prevents_first_mutation_but_saves_truthful_results(tmp_path):
    settings = configuration()
    source = parent(settings, security=['enable_firewall', 'update_defender_signatures'])
    service = backend(tmp_path, settings, source)
    cancel = threading.Event()
    result = service.apply_available_fixes(source, settings, progress_callback=lambda _: cancel.set(), cancel_event=cancel)
    service.apply_security_recommendation.assert_not_called()
    assert result['cancelled'] and result['status'] == 'cancelled'
    assert Path(result['log_path']).is_file()


def test_no_remaining_fresh_offers_is_skipped_not_a_fix_applied(tmp_path):
    settings = software_settings()
    source = parent(settings, software=software_report())
    service = backend(tmp_path, settings, source)
    service.updates_scan.return_value['updates'] = []
    result = service.apply_available_fixes(source, settings)
    assert result['counts']['completed'] == 0 and result['counts']['skipped'] == 1
    service.update_app_unattended.assert_not_called()


def test_each_software_installer_has_a_linked_prepared_item_journal(tmp_path):
    settings = software_settings(software_update_ids=['Fixture.One', 'Fixture.Two'])
    source = parent(settings, software=software_report([package(), package('Fixture.Two')]))
    service = backend(tmp_path, settings, source)
    service.updates_scan.return_value['updates'] = [package(), package('Fixture.Two')]
    captured = []
    def install(identity, *, cancel_event=None):
        assert identity in ('Fixture.One', 'Fixture.Two')
        assert cancel_event is not None and not cancel_event.is_set()
        captured.append((identity, cancel_event))
        paths = list((tmp_path / 'post-run-fixes').glob('*.json'))
        assert len(paths) == 1 and paths[0].parent == tmp_path / 'post-run-fixes'
        saved = json.loads(paths[0].read_text())
        assert paths[0].stem == saved['id']
        assert saved['parent_run_id'] == source['id']
        assert saved['stages'][0]['current_item']['id'] == identity
        assert saved['stages'][0]['authorization']['software_update_ids'] == ['Fixture.One', 'Fixture.Two']
        return {'exitCode': 0}
    service.update_app_unattended.side_effect = install
    result = service.apply_available_fixes(source, settings)
    assert service.update_app_unattended.call_count == 2
    assert len(result['stages'][0]['result']['items']) == 2
    assert [identity for identity, _ in captured] == ['Fixture.One', 'Fixture.Two']
    assert all(not token.is_set() for _, token in captured)
    controller_for(service).request_stop()
    assert all(token.is_set() for _, token in captured)


def test_windows_security_channel_uses_existing_service_with_saved_agreements(tmp_path):
    settings = configuration(windows_security_updates_enabled=True,
                             windows_security_updates_install_approved=True,
                             windows_security_update_agreements_accepted=True)
    source = parent(settings, windows={'scan': {'updates': [{'id': 'fixture-wua'}]}})
    service = backend(tmp_path, settings, source)
    service.windows_security_updates.return_value = {'failed_items': 0,
        'items': [{'id': 'fixture-wua', 'status': 'completed', 'result': {'ok': True}}]}
    result = service.apply_available_fixes(source, settings)
    args, kwargs = service.windows_security_updates.call_args
    assert args[0]['windows_security_updates_install_approved'] is True
    assert args[0]['windows_security_update_agreements_accepted'] is True
    assert callable(kwargs['item_callback'])
    assert_stop_bound_call(service, service.windows_security_updates, args[0], item_callback=kwargs['item_callback'])
    assert result['counts']['completed'] == 1
    assert result['stages'][0]['title'] == 'Check or install Windows security updates'


def test_driver_channel_keeps_exact_approval_fresh_scan_and_required_backup(tmp_path):
    offered = {'id': '00000000-0000-0000-0000-000000000001', 'revision': 1,
               'title': 'Fixture device driver', 'hardwareId': 'FIXTURE\\DEVICE',
               'provider': 'Fixture Vendor', 'driverClass': 'Fixture Class',
               'driverDate': '2026-01-01', 'canRequestUserInput': False, 'rebootBehavior': 2}
    settings = configuration(driver_updates_enabled=True, driver_policy='install_approved',
                             driver_changes_approved=True, driver_update_agreements_accepted=True,
                             driver_approved_updates=[offered])
    source = parent(settings, drivers={'scan': {'updates': [offered]}})
    service = backend(tmp_path, settings, source)
    service.is_admin = Mock(return_value=True)
    service.update_disk_readiness = Mock(return_value={'ready': True})
    service.driver_updates_scan.return_value = {'resultCode': 2, 'updates': [offered]}
    service.driver_backup_automatic.side_effect = None
    service.driver_backup_automatic.return_value = {'exit_code': 0, 'destination': str(tmp_path / 'driver-backup-fixture')}
    service.driver_update_apply_unattended = Mock(return_value={'resultCode': 2, 'hresult': 0, 'aggregateResultCode': 2, 'aggregateHresult': 0,
        'installer_started': True, 'outcomeKnown': True, 'running': False})
    result = service.apply_available_fixes(source, settings)
    service.driver_updates_scan.assert_called_once()
    service.driver_backup_automatic.assert_called_once()
    service.driver_update_apply_unattended.assert_called_once_with(offered, accept_eula=True)
    assert result['counts']['completed'] == 1


def test_final_unknown_security_and_verifier_failure_remain_unverified(tmp_path):
    settings = configuration()
    source = parent(settings, security=['enable_firewall'])
    service = backend(tmp_path, settings, source)
    service.security_status.side_effect = RuntimeError('secret')
    service.verify_optimization.side_effect = RuntimeError('secret')
    result = service.apply_available_fixes(source, settings)
    assert result['final_verification']['status'] == 'unverified'
    assert not result['final_verification']['coverage_complete']
    assert result['status'] == 'completed_with_errors'
    assert 'secret' not in json.dumps(result)


def test_synthetic_manual_routes_survive_fresh_security_readback_for_continue_dialog(tmp_path):
    settings = configuration()
    source = parent(settings, security=['enable_firewall', 'review_windows_update', 'review_legacy_patches'])
    before = copy.deepcopy(source)
    service = backend(tmp_path, settings, source)
    fresh = {'status': 'partial', 'failed_items': 1, 'recommendations': [],
             'errors': ['Fixture provider inventory unavailable'], 'reboot_required': False}
    service.security_status.return_value = fresh
    result = service.apply_available_fixes(source, settings)
    rows = {row['id']: row for row in result['security_review']['recommendations']}
    assert set(rows) == {'review_windows_update', 'review_legacy_patches'}
    assert rows['review_windows_update']['title'] == 'Review Windows Update'
    assert rows['review_legacy_patches']['title'] == 'Review vendor-only and legacy patch coverage'
    for identity, row in rows.items():
        assert row['action'] == identity and row['review_only'] is True
        assert row['source'] == 'completed_report' and row['requires_admin'] is False
    assert result['security_review']['status'] == 'partial'
    assert result['security_review']['errors'] == fresh['errors']
    assert fresh['recommendations'] == [] and source == before
    assert not set(rows).intersection(SECURITY_FIXES)


def test_global_lock_and_journal_failure_prevent_mutation(tmp_path, monkeypatch):
    settings = configuration()
    source = parent(settings, security=['enable_firewall'])
    service = backend(tmp_path, settings, source)
    service.workflow.lock.acquire()
    with pytest.raises(RuntimeError, match='already active'):
        service.apply_available_fixes(source, settings)
    service.workflow.lock.release()
    monkeypatch.setattr('optimus.post_run_fixes.atomic_json', Mock(side_effect=OSError('fixture disk full')))
    with pytest.raises(OSError):
        service.apply_available_fixes(source, settings)
    service.apply_security_recommendation.assert_not_called()
    assert not service.workflow.active and not service.workflow.lock.locked()


def test_native_inherited_api_routes_only_to_scoped_native_package_helper(tmp_path):
    settings = software_settings(software_update_ids=['apt:fixture'])
    source = parent(settings, security=['native-system-updates'], software=software_report([{'id': 'apt:fixture'}]))
    source['stages'][0]['title'] = 'Review or install approved native package updates'
    service = backend(tmp_path, settings, source, native=True)
    service.updates_scan.return_value = {'complete': True, 'structured': True, 'exitCode': 0,
                                         'updates': [{'id': 'apt:fixture', 'version': '1.0'}]}
    service.update_app_unattended.return_value = {'exitCode': 0, 'verified': True}
    assert NativeBackend.available_fixes is Backend.available_fixes
    preview = service.available_fixes(source, settings)
    assert [row['id'] for row in preview['actions']] == ['software_updates']
    result = service.apply_available_fixes(source, settings)
    assert_stop_bound_call(service, service.update_app_unattended, 'apt:fixture')
    service.apply_security_recommendation.assert_not_called()
    assert result['counts']['completed'] == 1
    assert result['stages'][0]['title'] == 'Review or install approved native package updates'


@pytest.mark.parametrize('current_identity, before_available, expected', [
    ('exact-fixture-key', True, 'confirmed'),
    ('different-fixture-key', True, 'unverified'),
    ('exact-fixture-key', False, 'unverified'),
])
def test_real_software_verifier_uses_fresh_followup_before_identity_not_stale_parent(
        tmp_path, current_identity, before_available, expected):
    settings = software_settings()
    source = parent(settings, software=software_report())
    # The original optimization may have changed this version already. Its
    # initial snapshot cannot establish the follow-up install's from_version.
    source['stages'].insert(0, {'title': 'Inspect current resource usage', 'status': 'completed',
        'result': {'InstalledApps': [{'Name': 'Fixture.One', 'Version': '0.5', 'Type': 'Desktop',
                                      'Identity': 'stale-parent-key', 'Publisher': 'Fixture'}]}})
    service = backend(tmp_path, settings, source)
    fresh_before = {'Processes': [], 'System': {}, 'InventoryErrors': [], 'InstalledApps': [
        {'Name': 'Fixture.One', 'Version': '1.0', 'Type': 'Desktop',
         'Identity': 'exact-fixture-key', 'Publisher': 'Fixture'}]}
    after = {'Processes': [], 'System': {}, 'InventoryErrors': []}
    service.audit.side_effect = [fresh_before if before_available else OSError('fixture inventory unavailable'), after]
    native_reader = Mock(return_value={'applications': {'complete': True, 'items': [
        {'name': 'Fixture.One', 'version': '2.0', 'identity': current_identity, 'publisher': 'Fixture'}]}})
    tool = OutcomeVerification(tmp_path, native_reader=native_reader)
    service.verify_optimization = Mock(side_effect=lambda report, **kwargs: tool.verify(report, **kwargs))
    result = service.apply_available_fixes(source, settings)
    checks = [check for stage in result['final_verification']['stages'] for check in stage.get('checks', [])
              if check.get('kind') == 'installed_software']
    assert len(checks) == 1 and checks[0]['status'] == expected
    assert service.audit.call_count == 2
    service.updates_scan.assert_called_once()
    native_reader.assert_called_once()
    assert native_reader.call_args.args[0]['applications'] is True
    assert source['stages'][0]['result']['InstalledApps'][0]['Version'] == '0.5'
    if before_available:
        assert result['software_before_snapshot']['InstalledApps'][0]['Version'] == '1.0'


def test_prior_native_timeout_failure_is_not_retried_without_verified_termination(tmp_path):
    settings = software_settings(software_update_ids=['apt:fixture'])
    old = {'scan': {'updates': [{'id': 'apt:fixture'}]}, 'failed_items': 1,
           'items': [{'id': 'apt:fixture', 'status': 'failed',
                      'error': 'The native command timed out. Installer descendants may continue.'}]}
    source = parent(settings, software=old)
    source['stages'][0]['title'] = 'Review or install approved native package updates'
    service = backend(tmp_path, settings, source, native=True)
    result = service.apply_available_fixes(source, settings)
    service.updates_scan.assert_not_called()
    service.update_app_unattended.assert_not_called()
    assert result['counts']['deferred'] == 0
    assert any(item['id'] == 'software_updates' and 'termination is unverified' in item['reason']
               for item in result['manual_items'])
    assert result['final_verification']['status'] == 'needs_review'


@pytest.mark.parametrize('native', [False, True])
def test_prior_escaped_updater_exception_without_result_cannot_authorize_retry(tmp_path, native):
    settings = software_settings(software_update_ids=['apt:fixture'] if native else ['Fixture.One'])
    source = parent(settings, software={})
    source['stages'][0].update(status='failed', result=None, error='Fixture escaped updater exception')
    if native:
        source['stages'][0]['title'] = 'Review or install approved native package updates'
    service = backend(tmp_path, settings, source, native=native)
    result = service.apply_available_fixes(source, settings)
    service.updates_scan.assert_not_called()
    service.update_app_unattended.assert_not_called()
    assert result['counts']['deferred'] == 0
    assert any(item['id'] == 'software_updates' and 'without a verified operation result' in item['reason']
               for item in result['manual_items'])
    assert not result['reviewed_plan']['actions']


def test_prior_structured_known_terminated_windows_failure_can_recheck_fresh_offer(tmp_path):
    settings = software_settings()
    old = software_report(failed_items=1, items=[{'id': 'Fixture.One', 'status': 'failed',
        'result': {'exitCode': 1603, 'running': False, 'outcomeKnown': True}}])
    source = parent(settings, software=old)
    source['stages'][0]['status'] = 'failed'
    service = backend(tmp_path, settings, source)
    result = service.apply_available_fixes(source, settings)
    service.updates_scan.assert_called_once()
    service.update_app_unattended.assert_called_once()
    assert result['counts']['completed'] == 1
