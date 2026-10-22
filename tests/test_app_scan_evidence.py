"""Discovery proof uses fixture sources and never starts a real updater."""
from types import SimpleNamespace
from unittest.mock import Mock
import subprocess
import threading

import pytest

from optimus.automated_software import run_software
from optimus.automation import CURRENT_SETUP_VERSION
from optimus.maintenance import Maintenance
from optimus.native_operations import NativePackages
from optimus.native_workflow import run_native_software
from optimus.update_scan_evidence import app_scan_evidence
from test_automated_software import configuration, package


def scan(*rows, **overrides):
    return dict({'source': 'winget', 'structured': True, 'exitCode': 0,
                 'updates': list(rows)}, **overrides)


def service(metadata):
    return SimpleNamespace(updates_scan=Mock(return_value=metadata),
                           update_app_unattended=Mock(return_value={'exitCode': 0}))


def test_successful_zero_offer_scan_is_distinct_from_unknown_and_has_coverage():
    summary = app_scan_evidence(scan(), selected=[], started=1000, finished=1046)
    assert summary['complete'] and summary['no_more_offered']
    assert summary['offers_count'] == summary['eligible_count'] == summary['remaining_count'] == 0
    assert summary['duration_seconds'] == 46
    assert 'No more updates were offered by this source at scan time' in summary['summary']
    assert 'publisher-only' in summary['coverage_limit']
    assert summary['installed_packages_checked'] is None  # WinGet upgrade is not installed-app inventory.


@pytest.mark.parametrize('metadata', [
    scan(structured=False), scan(exitCode=5), scan(source='msstore'),
    scan(complete=False), scan(excluded=['A truncated row was excluded.']),
    scan(updates='not a list'), scan(updates=[None]),
])
def test_unrecognized_partial_or_failed_discovery_never_means_zero_updates(metadata):
    summary = app_scan_evidence(metadata)
    assert not summary['complete'] and not summary['no_more_offered']
    assert summary['offers_count'] is None and summary['remaining_count'] is None
    assert summary['errors'] and 'unknown' in summary['summary']


def test_review_discovers_without_installing_and_reports_actual_offers():
    backend = service(scan(package(), package('Vendor.Two')))
    result = run_software(backend, configuration(software_update_policy='review'))
    backend.updates_scan.assert_called_once_with()
    backend.update_app_unattended.assert_not_called()
    assert result['app_scan']['offers_count'] == 2
    assert result['app_scan']['eligible_count'] == 0
    assert not result['app_scan']['no_more_offered']


def test_saved_selection_exclusions_and_completed_results_do_not_claim_all_apps_current():
    backend = service(scan(package(), package('Vendor.Two'), package('Vendor.Third')))
    result = run_software(backend, configuration(software_update_policy='selected',
        software_update_ids=['Vendor.One', 'Vendor.Two'], software_update_exclude_ids=['Vendor.Two']))
    proof = result['app_scan']
    assert proof['offers_count'] == 3 and proof['eligible_count'] == 1
    assert proof['completed_count'] == 1 and proof['verified_count'] == 0
    assert proof['remaining_count'] is None and not proof['no_more_offered']
    backend.update_app_unattended.assert_called_once_with('Vendor.One')


def test_partial_discovery_keeps_recognized_exact_approved_updates_but_flags_coverage():
    backend = service(scan(package(), excluded=['Another offer has an incomparable version.'],
                           source_reported_offers_count=2))
    result = run_software(backend, configuration())
    backend.update_app_unattended.assert_called_once_with('Vendor.One')
    assert result['failed_items'] == 1
    assert result['app_scan']['offers_count'] is None
    assert result['app_scan']['source_reported_offers_count'] == 2
    assert result['app_scan']['recognized_offers_count'] == result['app_scan']['eligible_count'] == 1
    assert result['app_scan']['completed_count'] == 1 and not result['app_scan']['no_more_offered']


def test_install_checkpoint_deferral_retains_discovery_and_approved_count():
    backend = service(scan(package(), package('Vendor.Two')))
    result = run_software(backend, configuration(), before_install=lambda _: False)
    assert result['app_scan']['offers_count'] == result['app_scan']['eligible_count'] == 2
    assert result['app_scan']['deferred_count'] == 2
    assert '2 deferred' in result['app_scan']['summary']
    backend.update_app_unattended.assert_not_called()


def test_app_source_exception_is_a_known_before_installer_failure_not_an_empty_result():
    backend = service(None)
    backend.updates_scan.side_effect = TimeoutError('Fixture catalog timeout')
    with pytest.raises(TimeoutError) as raised:
        run_software(backend, configuration())
    proof = raised.value.app_scan
    assert proof['status'] == 'failed' and proof['offers_count'] is None
    assert 'TimeoutError' in proof['errors'][0]['error']
    backend.update_app_unattended.assert_not_called()


def test_failed_revalidation_cannot_reuse_initial_scan_as_current_success():
    backend = service(None)
    backend.updates_scan.side_effect = [scan(package()), TimeoutError('Fixture revalidation failed')]
    result = run_software(backend, configuration(), before_install=lambda _: True)
    assert result['app_scan']['status'] == 'failed' and result['app_scan']['offers_count'] is None
    assert result['app_scan']['deferred_count'] == 1
    backend.update_app_unattended.assert_not_called()


@pytest.mark.parametrize('cancelled', [False, True])
def test_disabled_or_cancelled_scan_does_not_claim_a_search_was_done(cancelled):
    backend = service(scan())
    event = threading.Event()
    if cancelled:
        event.set()
    result = run_software(backend, configuration(software_updates_enabled=cancelled), cancel_event=event)
    assert result['app_scan']['status'] == 'not_started'
    assert result['app_scan']['offers_count'] is None and not result['app_scan']['no_more_offered']
    backend.updates_scan.assert_not_called()


def test_winget_backend_keeps_exact_source_query_and_actual_search_timestamps(tmp_path):
    backend = Maintenance(None, tmp_path)
    backend._winget = Mock(return_value='fixture-winget.exe')
    backend._run = Mock(return_value=subprocess.CompletedProcess([], 0,
        'No installed package found matching input criteria.', ''))
    metadata = backend.updates_scan()
    backend._run.assert_called_once_with(['fixture-winget.exe', 'upgrade', '--source', 'winget', '--disable-interactivity'])
    proof = app_scan_evidence(metadata)
    assert proof['complete'] and proof['offers_count'] == 0 and proof['started_utc'] and proof['completed_utc']
    assert proof['duration_seconds'] >= 0


def test_truncated_winget_identity_cannot_be_reported_as_zero_updates(tmp_path):
    header = f"{'Name':24}{'Id':32}{'Version':18}{'Available':18}Source"
    row = f"{'Example':24}{'Vendor.App...':32}{'1.0':18}{'2.0':18}winget"
    backend = Maintenance(None, tmp_path)
    backend._winget = Mock(return_value='fixture-winget.exe')
    backend._run = Mock(return_value=subprocess.CompletedProcess([], 0, header + '\n' + '-' * len(header) + '\n' + row, ''))
    proof = app_scan_evidence(backend.updates_scan())
    assert proof['status'] == 'incomplete' and proof['offers_count'] is None


@pytest.mark.parametrize('row,stderr', [('Warning', ''), ('2 upgrades available.', ''),
                                      ('', 'Fixture source request failed')])
def test_empty_unrecognized_or_diagnostic_winget_table_cannot_claim_zero_updates(tmp_path, row, stderr):
    header = f"{'Name':24}{'Id':32}{'Version':18}{'Available':18}Source"
    backend = Maintenance(None, tmp_path)
    backend._winget = Mock(return_value='fixture-winget.exe')
    backend._run = Mock(return_value=subprocess.CompletedProcess([], 0,
        header + '\n' + '-' * len(header) + '\n' + row, stderr))
    metadata = backend.updates_scan()
    assert metadata['excluded']
    result = run_software(service(metadata), configuration(software_update_policy='review'))
    proof = result['app_scan']
    assert result['failed_items'] == 1
    assert proof['status'] == 'incomplete' and not proof['complete'] and not proof['no_more_offered']
    assert proof['offers_count'] is None and proof['remaining_count'] is None


@pytest.mark.parametrize('rows,reported', [([], 2), ([package()], 0), ([package()], 2),
    ([], True), ([], -1), ([], '0')])
def test_evidence_independently_rejects_inconsistent_or_invalid_reported_counts(rows, reported):
    proof = app_scan_evidence(scan(updates=rows, source_reported_offers_count=reported))
    assert not proof['complete'] and not proof['no_more_offered']
    assert proof['offers_count'] is None and proof['remaining_count'] is None
    assert any('count' in error['error'] for error in proof['errors'])


def test_evidence_independently_rejects_diagnostic_output_even_with_success_exit():
    proof = app_scan_evidence(scan(stderr='Fixture source request failed'))
    assert not proof['complete'] and proof['offers_count'] is None and not proof['no_more_offered']


def test_matching_zero_footer_without_warnings_remains_a_confirmed_empty_source(tmp_path):
    header = f"{'Name':24}{'Id':32}{'Version':18}{'Available':18}Source"
    backend = Maintenance(None, tmp_path)
    backend._winget = Mock(return_value='fixture-winget.exe')
    backend._run = Mock(return_value=subprocess.CompletedProcess([], 0,
        header + '\n' + '-' * len(header) + '\n0 upgrades available.', ''))
    metadata = backend.updates_scan()
    assert metadata['excluded'] == []
    proof = app_scan_evidence(metadata)
    assert proof['complete'] and proof['offers_count'] == 0 and proof['no_more_offered']


def test_known_winget_footers_are_not_misread_as_truncated_package_rows(tmp_path):
    header = f"{'Name':24}{'Id':32}{'Version':18}{'Available':18}Source"
    row = f"{'Example':24}{'Vendor.App':32}{'1.0':18}{'2.0':18}winget"
    text = (header + '\n' + '-' * len(header) + '\n' + row + '\n1 upgrade available.\n'
            '1 package(s) have version numbers that cannot be determined. Use --include-unknown to see all results.\n')
    backend = Maintenance(None, tmp_path)
    backend._winget = Mock(return_value='fixture-winget.exe')
    backend._run = Mock(return_value=subprocess.CompletedProcess([], 0, text, ''))
    metadata = backend.updates_scan()
    assert not any('truncated update row' in reason for reason in metadata['excluded'])
    assert metadata['source_reported_offers_count'] == 1
    result = run_software(service(metadata), configuration(software_update_policy='review'))
    assert result['failed_items'] == 1 and not result['app_scan']['complete']
    assert result['app_scan']['recognized_offers_count'] == 1
    assert result['app_scan']['source_reported_offers_count'] == 1


def test_native_repository_scan_reports_registered_package_count_and_metadata_limits(tmp_path):
    packages = NativePackages(tmp_path, system='linux', runner=Mock())
    packages.manager, packages.executable = 'apt', '/fixture/apt'
    packages.installed = Mock(return_value=[packages._record('apt', 'editor', '1.0')])
    packages.runner.run.return_value = {'exitCode': 0, 'stdout': 'Listing... Done\n', 'stderr': ''}
    metadata = packages.scan()
    result = run_native_software(service(metadata), {'software_update_policy': 'review'})
    proof = result['app_scan']
    assert proof['source'] == 'apt' and proof['offers_count'] == 0 and proof['installed_packages_checked'] == 1
    assert 'current repository metadata' in proof['coverage_limit']
    assert proof['started_utc'] and proof['completed_utc']


@pytest.mark.parametrize('manager,output', [('apt', 'Unrecognized fixture update row'),
    ('dnf', 'editor.x86_64 malformed'), ('pacman', 'editor 1 ->')])
def test_native_malformed_update_output_is_incomplete_instead_of_zero(tmp_path, manager, output):
    packages = NativePackages(tmp_path, system='linux', runner=Mock())
    packages.manager, packages.executable = manager, '/fixture/' + manager
    packages.installed = Mock(return_value=[packages._record(manager, 'editor', '1.0')])
    packages.runner.run.return_value = {'exitCode': 0, 'stdout': output, 'stderr': ''}
    metadata = packages.scan()
    backend = service(metadata)
    result = run_native_software(backend, {'software_update_policy': 'review'})
    assert result['failed_items'] == 1
    assert result['app_scan']['offers_count'] is None and not result['app_scan']['complete']
    backend.update_app_unattended.assert_not_called()


def test_native_source_failure_has_evidence_without_installer_launch():
    backend = service(None)
    backend.updates_scan.side_effect = RuntimeError('Fixture manager unavailable')
    with pytest.raises(RuntimeError) as raised:
        run_native_software(backend, {'software_update_policy': 'review'})
    assert raised.value.app_scan['status'] == 'failed' and raised.value.app_scan['offers_count'] is None
    backend.update_app_unattended.assert_not_called()


@pytest.mark.parametrize('manager,code,stderr', [('dnf', 100, ''),
    ('pacman', 1, 'error: failed to read database')])
def test_native_update_status_and_diagnostics_cannot_be_discarded_as_zero_updates(tmp_path, manager, code, stderr):
    packages = NativePackages(tmp_path, system='linux', runner=Mock())
    packages.manager, packages.executable = manager, '/fixture/' + manager
    packages.installed = Mock(return_value=[packages._record(manager, 'editor', '1.0')])
    packages.runner.run.return_value = {'exitCode': code, 'stdout': '', 'stderr': stderr}
    metadata = packages.scan()
    assert metadata['manager_exit_code'] == code and metadata['manager_diagnostics'] == stderr
    result = run_native_software(service(metadata), {'software_update_policy': 'review'})
    assert result['failed_items'] == 1 and not result['app_scan']['complete']
    assert result['app_scan']['offers_count'] is None and not result['app_scan']['no_more_offered']


@pytest.mark.parametrize('manager,code', [('dnf', 0), ('pacman', 1)])
def test_native_normal_empty_status_without_diagnostics_remains_confirmed(tmp_path, manager, code):
    packages = NativePackages(tmp_path, system='linux', runner=Mock())
    packages.manager, packages.executable = manager, '/fixture/' + manager
    packages.installed = Mock(return_value=[])
    packages.runner.run.return_value = {'exitCode': code, 'stdout': '', 'stderr': ''}
    proof = app_scan_evidence(packages.scan(), native=True)
    assert proof['complete'] and proof['offers_count'] == 0 and proof['no_more_offered']


def test_native_apt_standard_cli_warning_does_not_invalidate_valid_update_query(tmp_path):
    packages = NativePackages(tmp_path, system='linux', runner=Mock())
    packages.manager, packages.executable = 'apt', '/fixture/apt'
    packages.installed = Mock(return_value=[])
    warning = '\nWARNING: apt does not have a stable CLI interface. Use with caution in scripts.\n\n'
    packages.runner.run.return_value = {'exitCode': 0, 'stdout': 'Listing... Done\n', 'stderr': warning}
    metadata = packages.scan()
    assert metadata['manager_diagnostics'] == warning and metadata['stderr'] == ''
    proof = app_scan_evidence(metadata, native=True)
    assert proof['complete'] and proof['offers_count'] == 0 and proof['no_more_offered']


def post_scan_fixture(native):
    rows = [package(), package('Vendor.Two')]
    metadata = scan(*rows)
    settings = configuration()
    runner = run_software
    if native:
        rows = [package('apt:editor', source='apt'), package('apt:other', source='apt')]
        metadata = scan(*rows, source='apt', complete=True)
        settings['setup_version'] = CURRENT_SETUP_VERSION
        runner = run_native_software
    backend = service(metadata)
    backend.update_app_unattended.return_value = {'exitCode': 0, 'verified': True}
    return runner, backend, settings


@pytest.mark.parametrize('native', [False, True])
@pytest.mark.parametrize('checkpoint', ['prepared_callback', 'before_install', 'item_callback'])
def test_post_scan_checkpoint_exception_retains_discovery_without_claiming_installer_lifecycle(native, checkpoint):
    runner, backend, settings = post_scan_fixture(native)
    callback = Mock(side_effect=RuntimeError('Fixture checkpoint failed'))
    with pytest.raises(RuntimeError) as raised:
        runner(backend, settings, **{checkpoint: callback})
    proof = raised.value.app_scan
    assert proof['complete'] and proof['offers_count'] == proof['eligible_count'] == 2
    assert proof['completed_count'] == 0 and proof['deferred_count'] == 2
    assert not proof['no_more_offered'] and proof['remaining_count'] is None
    assert not hasattr(raised.value, 'installer_started') and not hasattr(raised.value, 'outcomeKnown')
    callback.assert_called_once()
    backend.update_app_unattended.assert_not_called()


@pytest.mark.parametrize('native', [False, True])
def test_callback_failure_after_one_update_retains_completed_count_without_false_no_installer_claim(native):
    runner, backend, settings = post_scan_fixture(native)
    callback = Mock(side_effect=[None, RuntimeError('Fixture next-package display failed')])
    with pytest.raises(RuntimeError) as raised:
        runner(backend, settings, item_callback=callback)
    proof = raised.value.app_scan
    assert proof['complete'] and proof['offers_count'] == proof['eligible_count'] == 2
    assert proof['completed_count'] == proof['verified_count'] == proof['deferred_count'] == 1
    assert proof['remaining_count'] is None and not proof['no_more_offered']
    assert not hasattr(raised.value, 'installer_started') and not hasattr(raised.value, 'outcomeKnown')
    backend.update_app_unattended.assert_called_once()


@pytest.mark.parametrize('native', [False, True])
def test_approval_error_after_discovery_preserves_available_count(native):
    runner, backend, settings = post_scan_fixture(native)
    settings['software_update_agreements_accepted'] = False
    with pytest.raises(ValueError) as raised:
        runner(backend, settings)
    assert raised.value.app_scan['complete'] and raised.value.app_scan['offers_count'] == 2
    assert raised.value.app_scan['eligible_count'] is None
    backend.update_app_unattended.assert_not_called()


def test_post_revalidation_callback_exception_keeps_latest_source_inventory():
    runner, backend, settings = post_scan_fixture(False)
    initial = backend.updates_scan.return_value
    refreshed = scan(package(), package('Vendor.Two'), package('Vendor.New'))
    backend.updates_scan.side_effect = [initial, refreshed]
    with pytest.raises(RuntimeError) as raised:
        runner(backend, settings, before_install=lambda _: True,
               item_callback=Mock(side_effect=RuntimeError('Fixture display failed')))
    assert raised.value.app_scan['offers_count'] == 3
    assert raised.value.app_scan['eligible_count'] == raised.value.app_scan['deferred_count'] == 2
    backend.update_app_unattended.assert_not_called()
