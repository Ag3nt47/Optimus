"""An immediate follow-up skips known failed offers and measures its own work."""
import copy
import json
from unittest.mock import Mock

import pytest

from optimus.automated_software import known_failed_offer, run_software
from optimus.worker_totals import WorkerResourceTotals
from test_native_post_run_fixes import backend, configuration, package, parent, software_report, software_settings


def failed_offer(identity='Fixture.One', before='1.0', after='2.0'):
    return {'id': identity, 'from_version': before, 'to_version': after, 'status': 'failed',
        'error': 'The exact native installer failed with result 1603.',
        'result': {'id': identity, 'package_id': identity, 'source': 'winget',
            'from_version': before, 'to_version': after, 'status': 'failed',
            'exitCode': 2316632070, 'installer_started': True,
            'outcomeKnown': True, 'running': False, 'verified': False}}


def scan(*rows):
    return {'structured': True, 'exitCode': 0, 'source': 'winget', 'updates': list(rows)}


def terminal_install(identity='Fixture.Two', before='1.0', after='2.0'):
    return {'id': identity, 'package_id': identity, 'source': 'winget',
        'from_version': before, 'to_version': after, 'status': 'completed',
        'exitCode': 0, 'installer_started': True, 'outcomeKnown': True,
        'running': False, 'verified': True}


def test_exact_failed_parent_offer_is_not_downloaded_twice_and_other_apps_continue(tmp_path):
    settings = software_settings(software_update_ids=['Fixture.One', 'Fixture.Two'])
    failed = failed_offer()
    source = parent(settings, software=software_report(
        rows=[package(), package('Fixture.Two')], items=[failed], failed_items=1))
    original = copy.deepcopy(source)
    service = backend(tmp_path, settings, source)
    service.updates_scan.return_value = scan(package(), package('Fixture.Two'))
    service.update_app_unattended.return_value = terminal_install()
    prefs = tmp_path / 'preferences.json'
    prefs.write_text('{"fixture_saved_choice":true}\n')
    pref_bytes = prefs.read_bytes()

    result = service.apply_available_fixes(source, settings)

    assert [call.args[0] for call in service.update_app_unattended.call_args_list] == ['Fixture.Two']
    rows = result['stages'][0]['result']['items']
    skipped = next(row for row in rows if row['id'] == 'Fixture.One')['result']
    assert skipped['failureKind'] == 'failed_in_parent_run'
    assert skipped['failure_scope'] == 'package' and skipped['requiresReview'] is True
    assert skipped['installer_started'] is False and skipped['outcomeKnown'] is True and skipped['running'] is False
    assert 'exitCode' not in skipped and skipped['previous_exit_code'] == 2316632070
    assert '1603' in skipped['previous_error']
    assert skipped['note'].startswith('The main run already attempted this exact app/version offer')
    assert source == original and prefs.read_bytes() == pref_bytes
    assert result['counts']['failed'] == 1  # The unresolved offer remains explicit.
    checks = [check for stage in result['final_verification']['stages'] for check in stage.get('checks', [])]
    assert any(check['kind'] == 'previous_app_update_failure' and check['target'] == 'Fixture.One'
               and check['status'] == 'needs_review' and check['previous_exit_code'] == 2316632070 for check in checks)
    assert result['final_verification']['status'] == 'needs_review'
    assert result.get('stop_reason', '') == ''


@pytest.mark.parametrize('changed', ['available', 'version', 'identity'])
def test_changed_offer_is_not_suppressed_by_parent_failure(tmp_path, changed):
    settings = software_settings(software_update_ids=['Fixture.One', 'Fixture.Two'])
    source = parent(settings, software=software_report(items=[failed_offer()], failed_items=1))
    service = backend(tmp_path, settings, source)
    row = package('Fixture.Two' if changed == 'identity' else 'Fixture.One')
    row['available' if changed == 'available' else 'version'] = '3.0' if changed == 'available' else '1.5'
    service.updates_scan.return_value = scan(row)
    service.update_app_unattended.return_value = terminal_install(row['id'], row['version'], row['available'])
    result = service.apply_available_fixes(source, settings)
    assert [call.args[0] for call in service.update_app_unattended.call_args_list] == [row['id']]
    assert result['counts']['completed'] == 1


@pytest.mark.parametrize('where,key,value', [
    ('row', 'status', 'completed'), ('row', 'installer_started', False),
    ('row', 'source', 'msstore'), ('row', 'package_id', 'Fixture.Other'),
    ('receipt', 'id', 'Fixture.Other'), ('receipt', 'package_id', 'Fixture.Other'),
    ('receipt', 'source', 'msstore'), ('receipt', 'from_version', '0.9'),
    ('receipt', 'to_version', '3.0'), ('receipt', 'status', 'needs_review'),
    ('receipt', 'installer_started', False), ('receipt', 'outcomeKnown', False),
    ('receipt', 'running', True), ('receipt', 'exitCode', 0), ('receipt', 'exitCode', True),
    ('receipt', 'exitCode', 3010), ('receipt', 'exitCode', 0x8A150109),
    ('receipt', 'exitCode', 0x80070005), ('receipt', 'failure_scope', 'shared'),
    ('receipt', 'failure_scope', 'app_batch'), ('receipt', 'uncertain', True),
    ('receipt', 'restartNeeded', True), ('receipt', 'cancelled', True),
    ('nested', 'outcomeKnown', False), ('nested', 'running', True),
    ('nested', 'restartNeeded', True), ('nested', 'exitCode', 3010),
    ('nested', 'failure_scope', 'shared'),
    ('nested', 'running', 0), ('nested', 'outcomeKnown', 1),
    ('nested', 'uncertain', 'no'), ('nested', 'restartNeeded', 'no'),
    ('nested', 'exitCode', True), ('nested', 'hresult', True),
])
def test_unknown_conflicting_or_shared_parent_evidence_cannot_supply_skip(where, key, value):
    row = failed_offer()
    target = row if where == 'row' else row['result']
    if where == 'nested':
        target['diagnostic_state'] = {}
        target = target['diagnostic_state']
    target[key] = value
    assert known_failed_offer(row) is None


@pytest.mark.parametrize('failure', [
    {'running': True, 'outcomeKnown': False}, {'restartNeeded': True},
    {'exitCode': 0x80070005, 'failure_scope': 'shared'},
])
def test_parent_shared_or_unknown_state_still_blocks_new_installers(tmp_path, failure):
    settings = software_settings()
    row = failed_offer()
    row['result'].update(failure)
    source = parent(settings, software=software_report(items=[row], failed_items=1))
    service = backend(tmp_path, settings, source)
    result = service.apply_available_fixes(source, settings)
    service.update_app_unattended.assert_not_called()
    assert result['reviewed_plan']['actions'] == []
    assert any(item['id'] == 'software_updates' and item['reason'] for item in result['manual_items'])


def test_parent_failure_does_not_install_skip_offer_or_block_driver_scan(tmp_path):
    offered = {'id': '00000000-0000-0000-0000-000000000001', 'revision': 1,
        'title': 'Fixture device driver', 'hardwareId': 'FIXTURE\\DEVICE',
        'provider': 'Fixture Vendor', 'driverClass': 'Fixture Class',
        'driverDate': '2026-01-01', 'canRequestUserInput': False, 'rebootBehavior': 2}
    settings = software_settings(driver_updates_enabled=True, driver_policy='install_approved',
        driver_changes_approved=True, driver_update_agreements_accepted=True,
        driver_approved_updates=[offered])
    source = parent(settings, software=software_report(items=[failed_offer()], failed_items=1),
        drivers={'scan': {'updates': [offered]}})
    service = backend(tmp_path, settings, source)
    service.is_admin = Mock(return_value=True)
    service.update_disk_readiness = Mock(return_value={'ready': True})
    service.driver_updates_scan.return_value = {'resultCode': 2, 'updates': [offered]}
    service.driver_backup_automatic.side_effect = None
    service.driver_backup_automatic.return_value = {'exit_code': 0,
        'destination': str(tmp_path / 'driver-backup-fixture')}
    service.driver_update_apply_unattended = Mock(return_value={
        'resultCode': 2, 'hresult': 0, 'aggregateResultCode': 2, 'aggregateHresult': 0,
        'installer_started': True, 'outcomeKnown': True, 'running': False})
    result = service.apply_available_fixes(source, settings)
    service.update_app_unattended.assert_not_called()
    service.driver_updates_scan.assert_called_once()
    service.driver_backup_automatic.assert_called_once()
    service.driver_update_apply_unattended.assert_called_once_with(offered, accept_eula=True)
    assert [stage['action_id'] for stage in result['stages'] if 'action_id' in stage] == ['software_updates', 'driver_updates']
    assert result['counts']['failed'] == 1 and result['counts']['completed'] == 1
    assert result['worker_resource_totals']['driver_updates']['installed_items'] == 1
    assert result.get('stop_reason', '') == ''


def test_followup_totals_count_only_its_new_receipts_not_parent_or_verification(tmp_path):
    settings = software_settings()
    source = parent(settings, software=software_report())
    source['resource_totals'] = {'downloaded_bytes': 999999}
    source['worker_resource_totals'] = {'software_updates': {'downloaded_bytes': 999999}}
    service = backend(tmp_path, settings, source)
    events = []
    def update(identity, *, progress_callback, **kwargs):
        observed = {'key': 'software:update:' + identity, 'label': identity, 'phase': 'download',
            'percent': 50.0, 'downloaded_bytes': 2048, 'download_total_bytes': 4096,
            'bytes_estimated': False, 'bytes_source': 'winget_native'}
        progress_callback(observed)
        progress_callback(dict(observed, percent=100.0, downloaded_bytes=4096))
        receipt = terminal_install(identity)
        receipt['download_progress'] = dict(observed, percent=100.0, downloaded_bytes=4096)
        return receipt
    service.update_app_unattended = update
    result = service.apply_available_fixes(source, settings, progress_callback=events.append)
    totals = result['worker_resource_totals']['software_updates']
    assert totals['downloaded_bytes'] == 4096 and totals['downloaded_known'] is True
    assert totals['installed_items'] == 1 and totals['installed_known'] is True
    assert result['stages'][0]['result']['resource_totals'] == totals
    assert result['resource_totals'] == WorkerResourceTotals().snapshot()
    assert any(event.get('worker_resource_totals', {}).get('software_updates', {}).get('downloaded_bytes') == 2048
               for event in events)
    persisted = json.loads(next((tmp_path / 'post-run-fixes').glob('*.json')).read_text())
    assert persisted['worker_resource_totals'] == result['worker_resource_totals']
    assert persisted['resource_totals'] == result['resource_totals']


def test_failed_offer_context_is_scoped_to_one_followup_not_future_normal_runs(tmp_path):
    from types import SimpleNamespace
    settings = software_settings()
    service = SimpleNamespace(data_dir=tmp_path, updates_scan=Mock(return_value=scan(package())),
        update_app_unattended=Mock(return_value=terminal_install('Fixture.One')))
    skipped = run_software(service, settings, prior_failed_offers=(failed_offer(),))
    service.update_app_unattended.assert_not_called()
    assert skipped['items'][0]['result']['failureKind'] == 'failed_in_parent_run'
    run_software(service, settings)
    service.update_app_unattended.assert_called_once_with('Fixture.One')


def test_only_failed_offer_skip_stays_unfixed_in_final_verification(tmp_path):
    settings = software_settings()
    source = parent(settings, software=software_report(items=[failed_offer()], failed_items=1))
    service = backend(tmp_path, settings, source)
    result = service.apply_available_fixes(source, settings)
    service.update_app_unattended.assert_not_called()
    assert result['counts']['completed'] == 0 and result['counts']['failed'] == 1
    assert result['worker_resource_totals']['software_updates']['installed_items'] == 0
    assert result['worker_resource_totals']['software_updates']['installed_known'] is True
    assert result['final_verification']['counts']['needs_review'] == 1
    assert result['final_verification']['status'] == 'needs_review'
