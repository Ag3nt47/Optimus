"""Expanded updater tests use fake metadata/installers and private temporary logs."""
import copy
import threading
from types import SimpleNamespace
from unittest.mock import Mock
import pytest
from optimus.automated_software import fresh_candidates, run_software, is_newer_version
from optimus.workflow import DEFAULTS, Preferences, Workflow
from test_workflow import mock_verification


def package(identity='Vendor.One', **overrides):
    return dict({'id': identity, 'package_id': identity, 'source': 'winget',
                 'name': identity, 'version': '1.0', 'available': '2.0'}, **overrides)


def configuration(**overrides):
    return dict(copy.deepcopy(DEFAULTS), **dict({'software_updates_enabled': True,
        'software_update_policy': 'all_supported', 'software_update_all_approved': True,
        'software_update_agreements_accepted': True}, **overrides))


def backend(*packages):
    return SimpleNamespace(updates_scan=Mock(return_value={'structured': True, 'exitCode': 0,
        'source': 'winget', 'updates': list(packages) or [package(), package('Vendor.Two')]}),
        update_app_unattended=Mock(return_value={'exitCode': 0}),
        verify_optimization=Mock(side_effect=mock_verification))


@pytest.mark.parametrize('installed,available,newer', [
    ('2.0','1.0',False), ('1.0','1.0.0',False), ('v1.0.0','1.0',False),
    ('1.00.0','1.0',False), ('1.0+build1','1.0+build2',False),
    ('1.0','1.0-beta1',False), ('1.0-rc2','1.0-rc1',False),
    ('1.0-preview2','1.0-rc1',False), ('1.0-vendor1','2.0-vendor2',False),
    ('2025.10','2025.2',False), ('Unknown','2.0',False), ('1.0','Latest',False),
    ('2.0','10.0',True), ('1.0','1.0.0.1',True), ('V1.2.3','v1.2.4',True),
    ('1.0-beta1','1.0-beta2',True), ('1.0-alpha2','1.0-beta1',True),
    ('1.0-rc1','1.0',True), ('1.0-preview1','1.0-preview2',True),
    ('2025.2','2025.10',True), ('1_0_2','1_0_3',True),
])
def test_only_provably_newer_versions_have_forward_order(installed,available,newer):
    assert is_newer_version(installed,available) is newer
    scan={'structured':True,'exitCode':0,'updates':[package(version=installed,available=available)]}
    assert bool(fresh_candidates(scan)) is newer


def test_equal_older_or_incomparable_catalog_rows_never_reinstall_in_all_mode():
    service=backend(package('Vendor.Equal',version='1.0',available='1.0.0'),
                    package('Vendor.Older',version='2.0',available='1.0'),
                    package('Vendor.Opaque',version='1.0-vendor',available='2.0-vendor'),
                    package('Vendor.Newer',version='1.9',available='1.10'))
    result=run_software(service,configuration())
    service.update_app_unattended.assert_called_once_with('Vendor.Newer')
    assert len(result['items'])==1


def test_selected_ids_use_case_insensitive_approval_and_current_canonical_id():
    service=backend(package('Vendor.One'))
    result=run_software(service,configuration(software_update_policy='selected',software_update_ids=['vendor.one']))
    service.update_app_unattended.assert_called_once_with('Vendor.One')
    assert result['not_available_ids']==[]


def test_disabled_updater_does_not_even_scan():
    service=backend()
    result=run_software(service,configuration(software_updates_enabled=False))
    assert result['skipped'] is True
    service.updates_scan.assert_not_called()
    service.update_app_unattended.assert_not_called()


def test_unrecognized_success_exit_review_is_partial_instead_of_clean_success():
    service=backend()
    service.updates_scan.return_value={'structured':False,'exitCode':0,'updates':[]}
    result=run_software(service,configuration(software_update_policy='review'))
    assert result['failed_items']==1
    service.update_app_unattended.assert_not_called()


def test_old_selected_approval_does_not_expand_to_all_supported():
    old = Preferences.validate({'software_updates_enabled': True, 'software_update_policy': 'selected',
        'software_update_ids': ['Vendor.One'], 'software_update_agreements_accepted': True})
    assert old['software_update_policy'] == 'selected'
    assert old['software_update_all_approved'] is False and old['software_update_exclude_ids'] == []
    with pytest.raises(ValueError, match='separate explicit scope'):
        Preferences.validate(dict(old, software_update_policy='all_supported'))


@pytest.mark.parametrize('overrides', [
    {'software_update_all_approved': 'yes'}, {'software_update_all_approved': False},
    {'software_update_agreements_accepted': False}, {'software_update_exclude_ids': ['--all']},
    {'software_update_exclude_ids': ['Vendor.One', 'vendor.one']},
    {'software_update_exclude_ids': 'Vendor.One'}, {'software_update_exclude_ids': ['Vendor.*']},
    {'software_update_exclude_ids': [f'Vendor.App{number}' for number in range(101)]},
])
def test_all_supported_requires_typed_scope_consent_and_exact_exclusions(overrides):
    with pytest.raises(ValueError):
        Preferences.validate(configuration(**overrides))


def test_disabled_all_scope_can_be_saved_without_granting_permission():
    result = Preferences.validate(configuration(software_updates_enabled=False,
        software_update_all_approved=False, software_update_agreements_accepted=False))
    assert not result['software_update_all_approved']
    service = backend()
    with pytest.raises(ValueError):
        run_software(service, dict(result, software_updates_enabled=True))
    service.update_app_unattended.assert_not_called()


def test_all_supported_updates_fresh_candidates_and_respects_case_insensitive_exclusions():
    service = backend(package(), package('Vendor.Two'), package('Vendor.NewlyInstalled'))
    events = []
    outcome = run_software(service, configuration(software_update_exclude_ids=['vendor.two']), item_callback=events.append)
    assert [call.args[0] for call in service.update_app_unattended.call_args_list] == ['Vendor.One', 'Vendor.NewlyInstalled']
    assert outcome['excluded_ids'] == ['Vendor.Two'] and outcome['failed_items'] == 0
    assert [item['status'] for item in outcome['items']] == ['completed', 'completed']
    assert all(event['items'] == 2 and '1.0 to 2.0' in event['description'] for event in events)


def test_selected_policy_never_updates_new_unselected_application():
    service = backend(package(), package('Vendor.NewlyInstalled'))
    result = run_software(service, configuration(software_update_policy='selected',
        software_update_all_approved=False, software_update_ids=['Vendor.One', 'Vendor.Absent']))
    service.update_app_unattended.assert_called_once_with('Vendor.One')
    assert result['not_available_ids'] == ['Vendor.Absent']


def test_review_scan_never_starts_installer_even_with_old_consents():
    service = backend()
    result = run_software(service, configuration(software_update_policy='review'))
    assert result['review_only'] and result['failed_items'] == 0
    service.update_app_unattended.assert_not_called()


@pytest.mark.parametrize('bad', [
    {'source': 'msstore'}, {'version': 'Unknown'}, {'available': 'Unknown'},
    {'version': '< 1.0'}, {'available': '2.0...'}, {'available': '2.0\u2026'},
    {'version': '1.0', 'available': '1.0'}, {'version': 'V1.0', 'available': 'v1.0'},
    {'id': 'Vendor.App...'}, {'id': '--all'}, {'package_id': 'Other.App'}, {'pinned': True},
])
def test_unknown_truncated_wrong_source_and_pinned_candidates_are_excluded(bad):
    scan = {'structured': True, 'exitCode': 0, 'updates': [package(**bad)]}
    assert not fresh_candidates(scan)


def test_duplicate_identity_is_excluded_even_when_duplicate_has_unsupported_metadata():
    scan = {'structured': True, 'exitCode': 0, 'updates': [package(), package('vendor.one', version='Unknown')]}
    assert fresh_candidates(scan) == {}


@pytest.mark.parametrize('scan', [
    {'structured': False, 'exitCode': 0, 'updates': [package()]},
    {'structured': True, 'exitCode': 5, 'updates': [package()]},
    {'structured': True, 'exitCode': True, 'updates': [package()]},
    {'structured': True, 'exitCode': 0, 'source': 'msstore', 'updates': [package()]},
    {'structured': True, 'exitCode': 0, 'updates': [package()] * 1001},
])
def test_scan_validation_precedes_all_supported_installers(scan):
    service = backend()
    service.updates_scan.return_value = scan
    run_software(service, configuration())
    service.update_app_unattended.assert_not_called()


def test_cancellation_from_progress_callback_prevents_first_mutation():
    service = backend()
    event = threading.Event()
    result = run_software(service, configuration(), cancel_event=event, item_callback=lambda state: event.set())
    assert result['cancelled'] and result['deferred_ids'] == ['Vendor.One', 'Vendor.Two']
    service.update_app_unattended.assert_not_called()


def test_preexisting_cancellation_does_not_scan_or_install():
    service = backend()
    event = threading.Event(); event.set()
    assert run_software(service, configuration(), cancel_event=event)['cancelled']
    service.updates_scan.assert_not_called()
    service.update_app_unattended.assert_not_called()


@pytest.mark.parametrize('first', [
    {'exitCode': 3010}, {'exitCode': 1641}, {'exitCode': 0, 'restartNeeded': True},
    {'exitCode': 0, 'running': True}, {'exitCode': 0, 'outcomeKnown': False},
    {'exitCode': 5, 'status': 'timeout'}, {'unexpected': 'unknown'}, None,
])
def test_restart_or_uncertain_installer_defers_remaining_packages(first):
    service = backend()
    service.update_app_unattended.return_value = first
    result = run_software(service, configuration())
    service.update_app_unattended.assert_called_once_with('Vendor.One')
    assert result['deferred_ids'] == ['Vendor.Two'] and result['stop_reason']


def test_any_exception_defers_remaining_packages_without_replay():
    service = backend()
    service.update_app_unattended.side_effect = RuntimeError('fixture uncertain vendor failure')
    result = run_software(service, configuration())
    service.update_app_unattended.assert_called_once_with('Vendor.One')
    assert result['failed_items'] == 1 and result['deferred_ids'] == ['Vendor.Two']


def test_known_preflight_failure_retains_error_without_orphan_installer_claim():
    from optimus.maintenance import UpdatePreflightError
    service = backend()
    service.update_app_unattended.side_effect = UpdatePreflightError('Fixture exact candidate changed before installation')
    result = run_software(service, configuration())
    assert result['failed_items'] == 1 and result['deferred_ids'] == ['Vendor.Two']
    assert result['items'][0]['result']['installer_started'] is False
    assert result['outcomeKnown'] is True and result['installer_started'] is False
    assert 'no installer was started' in result['stop_reason']
    assert 'verify installer state' not in result['stop_reason']
    service.update_app_unattended.assert_called_once_with('Vendor.One')


def test_completed_update_then_preflight_failure_keeps_both_outcomes_known():
    from optimus.maintenance import UpdatePreflightError
    service = backend()
    service.update_app_unattended.side_effect = [
        {'exitCode': 0, 'installer_started': True, 'outcomeKnown': True},
        UpdatePreflightError('Fixture safety check refused the next package')]
    result = run_software(service, configuration())
    assert [row['status'] for row in result['items']] == ['completed', 'failed']
    assert result['installer_started'] is True and result['outcomeKnown'] is True


@pytest.mark.parametrize('typed', [False, True])
def test_real_timeout_and_unclassified_exceptions_remain_uncertain(typed):
    from optimus.maintenance import UpdateOutcomeError
    service = backend()
    service.update_app_unattended.side_effect = (UpdateOutcomeError('Fixture vendor still running') if typed else
                                               RuntimeError('Unclassified fixture failure'))
    result = run_software(service, configuration())
    assert result['outcomeKnown'] is False
    assert result['items'][0]['result']['outcomeKnown'] is False
    assert 'verify installer state' in result['stop_reason']
    assert result['deferred_ids'] == ['Vendor.Two']


def test_pin_or_changed_version_skip_does_not_block_other_candidates():
    service = backend()
    service.update_app_unattended.side_effect = [
        {'status': 'skipped', 'skipped': True, 'exitCode': None, 'note': 'Fixture pinned'}, {'exitCode': 0}]
    result = run_software(service, configuration())
    assert result['failed_items'] == 0 and len(result['items']) == 2
    assert result['items'][0]['status'] == 'skipped' and result['items'][1]['status'] == 'completed'


def test_workflow_uses_all_supported_scope_and_journals_per_application_outcomes(tmp_path):
    service = backend()
    service.data_dir = tmp_path
    service.bridge = SimpleNamespace(_process=None)
    service.audit = Mock(return_value={})
    events = []
    outcome = Workflow(service).run(configuration(cleanup_enabled=False, browser_cache_enabled=False), events.append)
    stage = next(stage for stage in outcome['stages'] if 'software' in stage['title'])
    assert stage['result']['policy'] == 'all_supported' and len(stage['result']['items']) == 2
    assert outcome['status'] == 'completed' and any(event.get('items') == 2 for event in events)
    assert outcome['log_path']

def test_unknown_pin_safety_state_is_visible_and_defers_remaining_applications():
    service = backend()
    service.update_app_unattended.return_value = {'skipped': True, 'exitCode': None,
        'requiresReview': True, 'failureKind': 'pin_inventory_untrusted', 'note': 'Pin output needs review'}
    result = run_software(service, configuration())
    assert result['failed_items'] == result['skipped_items'] == result['requires_review_items'] == 1
    assert result['deferred_ids'] == ['Vendor.Two'] and result['stop_reason']
    assert result['items'][0]['result']['note'] == 'Pin output needs review'
    service.update_app_unattended.assert_called_once_with('Vendor.One')


def test_journal_uses_native_revalidated_version_pair_when_different_from_initial_scan():
    service = backend(package())
    service.update_app_unattended.return_value = {'exitCode': 0, 'from_version': '1.1', 'to_version': '2.1'}
    result = run_software(service, configuration())
    assert result['items'][0]['from_version'] == '1.1' and result['items'][0]['to_version'] == '2.1'

def test_native_cancellation_result_defers_remaining_and_is_not_a_failed_install():
    service = backend()
    stop = threading.Event()
    def cancelled_native(identity, *, cancel_event=None):
        assert cancel_event is stop
        stop.set()
        return {'id': identity, 'cancelled': True, 'skipped': True, 'exitCode': None, 'status': 'cancelled'}
    service.update_app_unattended.side_effect = cancelled_native
    result = run_software(service, configuration(), cancel_event=stop)
    service.update_app_unattended.assert_called_once_with('Vendor.One', cancel_event=stop)
    assert result['cancelled'] and result['failed_items'] == 0
    assert result['items'][0]['status'] == 'cancelled' and result['deferred_ids'] == ['Vendor.Two']


@pytest.mark.parametrize('policy', ['review', 'selected', 'all_supported'])
def test_successful_recognized_empty_scan_completes_without_installer_or_false_failure(policy):
    service = backend()
    service.updates_scan.return_value = {'structured': True, 'exitCode': 0,
        'source': 'winget', 'updates': [], 'no_applicable_upgrades': True,
        'text': 'No installed package found matching input criteria.\n'}
    result = run_software(service, configuration(software_update_policy=policy,
        software_update_ids=['Vendor.One']))
    assert result['review_only'] is True and result['failed_items'] == 0
    service.update_app_unattended.assert_not_called()


def test_empty_scan_sentinel_cannot_bypass_unsuccessful_native_scan():
    service = backend()
    service.updates_scan.return_value = {'structured': False, 'exitCode': 0x8A15002B,
        'source': 'winget', 'updates': [], 'no_applicable_upgrades': True}
    result = run_software(service, configuration())
    assert result['failed_items'] == 1
    service.update_app_unattended.assert_not_called()
