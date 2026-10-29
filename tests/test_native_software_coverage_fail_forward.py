"""Coverage findings cannot hold unrelated installers; every service is a fixture."""
import copy
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from optimus.automated_software import UNKNOWN_VERSION_COVERAGE_WARNING, run_software
from optimus.maintenance import Maintenance
from optimus.post_run_fixes import _known_terminal_failure, _stop_reason
from optimus.update_scan_evidence import with_app_scan
from test_automated_software import configuration, package
from test_native_post_run_fixes import backend, parent, software_report, software_settings
from test_updates import table


def coverage_scan(*rows):
    return {'source': 'winget', 'structured': True, 'exitCode': 0,
        'updates': list(rows), 'source_reported_offers_count': len(rows),
        'excluded': [UNKNOWN_VERSION_COVERAGE_WARNING]}


def terminal_receipt(**changes):
    value = {'exitCode': 0, 'installer_started': True, 'outcomeKnown': True,
        'running': False, 'verified': True}
    value.update(changes)
    return value


def coverage_group(*, held=True, completed=True):
    items = []
    if held:
        for identity in ('Fixture.HeldOne', 'Fixture.HeldTwo'):
            items.append({'id': identity, 'status': 'skipped', 'result': terminal_receipt(
                exitCode=None, installer_started=False, verified=False, skipped=True,
                status='skipped', requiresReview=True, failureKind='automatic_update_hold')})
    if completed:
        items.append({'id': 'Fixture.Completed', 'status': 'completed', 'result': terminal_receipt()})
        items.append({'id': 'Fixture.Client', 'status': 'skipped', 'result': terminal_receipt(
            exitCode=None, installer_started=False, verified=False, skipped=True,
            failureKind='version_changed')})
    failures = sum(row['result'].get('requiresReview') is True for row in items)
    value = {'scan': coverage_scan(*(package(row['id']) for row in items)),
        'items': items, 'failed_items': failures + 1, 'scan_failed_items': 1,
        'installer_started': completed, 'outcomeKnown': True, 'running': False,
        'cancelled': False, 'restartNeeded': False, 'stop_reason': '', 'deferred_ids': []}
    return with_app_scan(value)


def test_unknown_version_advisory_uses_the_native_parser_constant():
    text = table(('Fixture', 'Fixture.One', '1.0', '2.0', 'winget'))
    text += '1 package(s) have version numbers that cannot be determined. Use --include-unknown to see all results.\n'
    rows, structured, excluded = Maintenance._parse_update_table(text)
    assert structured and [row['id'] for row in rows] == ['Fixture.One']
    assert excluded == [UNKNOWN_VERSION_COVERAGE_WARNING]


@pytest.mark.parametrize('mode', ['review', 'defer', 'install'])
def test_generated_groups_separate_scan_counter_without_hiding_total_failures(mode):
    service = SimpleNamespace(updates_scan=Mock(return_value=coverage_scan(package())),
        update_app_unattended=Mock(return_value=terminal_receipt()))
    settings = configuration(software_update_policy='review' if mode == 'review' else 'all_supported')
    kwargs = {'before_install': lambda state: False} if mode == 'defer' else {}
    result = run_software(service, settings, **kwargs)
    assert result['scan_failed_items'] == 1 and result['failed_items'] == 1
    assert result['app_scan']['complete'] is False
    assert result['app_scan']['errors'] == [{'source': 'winget', 'error': UNKNOWN_VERSION_COVERAGE_WARNING}]
    assert bool(service.update_app_unattended.called) is (mode == 'install')


@pytest.mark.parametrize('held,completed', [(True, True), (False, True), (False, False)])
def test_only_proven_terminal_groups_can_continue_past_coverage(held, completed):
    result = coverage_group(held=held, completed=completed)
    before = copy.deepcopy(result)
    assert _known_terminal_failure(result)
    assert _stop_reason(result) == ''
    assert result == before
    assert result['app_scan']['complete'] is False
    assert result['failed_items'] == (3 if held else 1)


def test_changed_client_version_is_proven_not_started_after_revalidation():
    service = SimpleNamespace(updates_scan=Mock(side_effect=[
        coverage_scan(package('Fixture.Completed'), package('Fixture.Client')),
        coverage_scan(package('Fixture.Completed'))]),
        update_app_unattended=Mock(return_value=terminal_receipt()))
    result = run_software(service, configuration(), before_install=lambda state: True)
    skipped = next(row['result'] for row in result['items'] if row['id'] == 'Fixture.Client')
    assert skipped['failureKind'] == 'version_changed'
    assert skipped['installer_started'] is False and skipped['outcomeKnown'] is True
    assert skipped['running'] is False
    service.update_app_unattended.assert_called_once_with('Fixture.Completed')
    assert result['scan_failed_items'] == result['failed_items'] == 1
    assert result['running'] is False and _stop_reason(result) == ''


@pytest.mark.parametrize('change', [
    lambda v: v.pop('scan_failed_items'),
    lambda v: v.update(scan_failed_items=True),
    lambda v: v.update(scan_failed_items=2),
    lambda v: v.update(scan_failed_items='1'),
    lambda v: v.update(failed_items=True),
    lambda v: v.update(failed_items=99),
    lambda v: v['scan'].update(excluded=[]),
    lambda v: v['scan'].update(excluded=['Unrecognized source output.']),
    lambda v: v['scan'].update(source='msstore'),
    lambda v: v['scan'].update(structured=False),
    lambda v: v['scan'].update(exitCode=True),
    lambda v: v['scan'].update(exitCode=5),
    lambda v: v['scan'].update(source_reported_offers_count=99),
    lambda v: v['scan']['updates'][0].update(source='msstore'),
    lambda v: v['scan']['updates'][0].update(version='Unknown'),
    lambda v: v['scan'].update(stderr='Source trust could not be established.'),
    lambda v: v.pop('app_scan'),
    lambda v: v['app_scan'].update(failed_count=99),
    lambda v: v['app_scan'].update(failed_count=False),
    lambda v: v['app_scan'].update(errors=[]),
    lambda v: v.pop('running'),
    lambda v: v.update(outcomeKnown=False),
    lambda v: v.update(installer_started=False),
    lambda v: v.update(failureKind='source_untrusted'),
    lambda v: v.update(restartNeeded=True),
    lambda v: v.update(exitCode=1618),
    lambda v: v['items'][-1]['result'].pop('running'),
    lambda v: v['items'][-1]['result'].pop('installer_started'),
    lambda v: v['items'][-1]['result'].update(outcomeKnown=False),
    lambda v: v['items'][-1]['result'].update(running=True),
    lambda v: v['items'][-1]['result'].update(may_continue=True),
    lambda v: v['items'][-1]['result'].update(restartNeeded=True),
    lambda v: v['items'][-1]['result'].update(exitCode=1618),
    lambda v: v['items'][-1]['result'].update(failure_scope='shared'),
])
def test_scan_counter_cannot_forge_source_or_lifecycle_authority(change):
    value = coverage_group()
    change(value)
    assert not _known_terminal_failure(value)
    assert _stop_reason(value)


@pytest.mark.parametrize('unsafe', [None, 'source_untrusted', 'unknown', 'missing_running', 'restart', 'concurrent'])
def test_real_software_followup_then_exact_driver_apply_only_after_terminal_coverage(tmp_path, monkeypatch, unsafe):
    driver = {'id': '00000000-0000-0000-0000-000000000001', 'revision': 1,
        'title': 'Fixture device driver', 'hardwareId': 'FIXTURE\\DEVICE',
        'provider': 'Fixture Vendor', 'driverClass': 'Fixture Class',
        'driverDate': '2026-01-01', 'canRequestUserInput': False, 'rebootBehavior': 2}
    settings = software_settings(software_update_policy='all_supported', software_update_all_approved=True,
        driver_updates_enabled=True, driver_policy='install_approved', driver_changes_approved=True,
        driver_update_agreements_accepted=True, driver_approved_updates=[driver])
    source = parent(settings, software=software_report(), drivers={'scan': {'updates': [driver]}})
    original = copy.deepcopy(source)
    service = backend(tmp_path, settings, source)
    rows = [package(identity) for identity in ('Fixture.HeldOne', 'Fixture.HeldTwo', 'Fixture.Client', 'Fixture.Completed')]
    service.updates_scan.return_value = coverage_scan(*rows)
    holds = [{'id': row['id'], 'available_version': row['available'], 'reason': 'Fixture exact-offer hold.'} for row in rows[:2]]
    monkeypatch.setattr('optimus.automated_software.load_update_holds', lambda path: copy.deepcopy(holds))
    def update(identity, **kwargs):
        if identity == 'Fixture.Client':
            return terminal_receipt(exitCode=None, installer_started=False, verified=False,
                skipped=True, failureKind='version_changed')
        receipt = terminal_receipt()
        if unsafe == 'source_untrusted':
            receipt.update(requiresReview=True, failureKind='source_untrusted')
        elif unsafe == 'unknown':
            receipt['outcomeKnown'] = False
        elif unsafe == 'missing_running':
            receipt.pop('running')
        elif unsafe == 'restart':
            receipt['restartNeeded'] = True
        elif unsafe == 'concurrent':
            receipt.update(exitCode=1618, requiresReview=True)
        return receipt
    service.update_app_unattended.side_effect = update
    service.is_admin = Mock(return_value=True)
    service.update_disk_readiness = Mock(return_value={'ready': True})
    service.driver_updates_scan.return_value = {'resultCode': 2, 'updates': [driver]}
    service.driver_backup_automatic.side_effect = None
    service.driver_backup_automatic.return_value = {'exit_code': 0, 'destination': str(tmp_path / 'fixture-backup')}
    service.driver_update_apply_unattended = Mock(return_value={'resultCode': 2, 'hresult': 0,
        'aggregateResultCode': 2, 'aggregateHresult': 0,
        'installer_started': True, 'outcomeKnown': True, 'running': False})
    result = service.apply_available_fixes(source, settings)
    apps = next(stage['result'] for stage in result['stages'] if stage.get('action_id') == 'software_updates')
    assert source == original and holds[0]['id'] == 'Fixture.HeldOne'
    assert apps['scan_failed_items'] == 1 and apps['failed_items'] >= 3
    assert apps['app_scan']['complete'] is False and apps['held_count'] == 2
    assert [call.args[0] for call in service.update_app_unattended.call_args_list] == ['Fixture.Client', 'Fixture.Completed']
    if unsafe is None:
        assert apps['failed_items'] == 3
        assert apps['app_scan']['failed_count'] == 0
        service.driver_backup_automatic.assert_called_once()
        service.driver_update_apply_unattended.assert_called_once_with(driver, accept_eula=True)
        assert result['counts']['deferred'] == 0 and not result.get('stop_reason')
        assert result['status'] == 'completed_with_errors'  # Coverage and held offers remain honest findings.
    else:
        service.driver_updates_scan.assert_not_called()
        service.driver_backup_automatic.assert_not_called()
        service.driver_update_apply_unattended.assert_not_called()
        assert result['counts']['deferred'] == 1 and result['stop_reason']
