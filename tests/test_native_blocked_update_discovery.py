"""An unresolved installer blocks mutations, while fresh searches remain useful."""
import json
from contextlib import nullcontext
from pathlib import Path
from unittest.mock import Mock, patch

import pytest

from optimus.native_workflow import NativeWorkflow
from optimus.parallel_updates import (
    ACTIVE_JOB_STATES, NATIVE_SOFTWARE, WINDOWS_DRIVERS, WINDOWS_SOFTWARE,
    ParallelAppUpdates, ParallelInstallerQueue, ProgressMux, pending_updates_barrier,
    _journal_result, update_completion_state,
)
from optimus.workflow import Workflow
from test_native_parallel_updates import Backend, driver_settings, enable_fixture_driver, settings


@pytest.fixture(autouse=True)
def fixture_services(monkeypatch):
    monkeypatch.setattr('optimus.speed_enhancements._current_owner', lambda: 'fixture-owner')
    monkeypatch.setattr('optimus.runtime_priority.lower_worker_priority', lambda: lambda: None)


def old_receipt(directory, kind='uncertain'):
    directory = directory / 'parallel-updates'
    directory.mkdir()
    path = directory / ('c' * 32 + '.json')
    value = {'id': path.stem, 'schema': 1, 'owner': 'fixture-owner',
             'status': 'uncertain', 'installer_prepared': True}
    if kind == 'foreign_owner':
        value['owner'] = 'some-other-owner'
    elif kind == 'restart':
        value.update(status='needs_review', result={'restartNeeded': True})
    path.write_text('{' if kind == 'unreadable' else json.dumps(value), encoding='utf-8')
    return path, path.read_bytes()


def forbid_mutations(service):
    for name in ('update_app_unattended', 'driver_update_apply_unattended',
                 'driver_backup_automatic', 'cleanup_scan', 'cleanup_apply',
                 'windows_security_updates', '_apply_drive_optimization_stage'):
        setattr(service, name, Mock(side_effect=AssertionError('Blocked installer must not mutate: ' + name)))


@pytest.mark.parametrize('parallel', [False, True])
@pytest.mark.parametrize('kind', ['uncertain', 'foreign_owner', 'unreadable', 'restart'])
def test_old_receipt_allows_both_searches_once_but_no_conflicting_mutation(tmp_path, parallel, kind):
    path, original = old_receipt(tmp_path, kind)
    service = Backend(tmp_path)
    service.release.set()
    service.updates_scan = Mock(wraps=service.updates_scan)
    enable_fixture_driver(service, Mock())
    service.security_status = Mock(return_value={'reboot_required': True})
    forbid_mutations(service)
    events = []
    config = driver_settings(parallel_app_updates_enabled=parallel, cleanup_enabled=True,
                             windows_security_updates_enabled=True, drive_optimization_enabled=True)
    # Serial preference must not silently create updater threads.
    context = patch('optimus.parallel_updates.threading.Thread.start',
                    side_effect=AssertionError('Serial discovery cannot start a thread')) if not parallel else nullcontext()
    with context:
        report = Workflow(service).run(config, events.append)
    service.updates_scan.assert_called_once()
    service.driver_updates_scan.assert_called_once()
    for name in ('update_app_unattended', 'driver_update_apply_unattended', 'driver_backup_automatic',
                 'cleanup_scan', 'cleanup_apply', 'windows_security_updates', '_apply_drive_optimization_stage'):
        getattr(service, name).assert_not_called()
    assert path.read_bytes() == original
    assert pending_updates_barrier(service)
    stages = {stage['title']: stage for stage in report['stages']}
    for title, key, count in ((WINDOWS_SOFTWARE, 'app_scan', 2), (WINDOWS_DRIVERS, 'driver_scan', 1)):
        result = stages[title]['result']
        assert result['installation_blocked'] is True and result['blocked_by_prior_installer'] is True
        assert result['installer_started'] is False and result['outcomeKnown'] is True
        assert result[key]['complete'] is True and result[key]['offers_count'] == count
        assert result['items'] == [] and result['installation_block_reason']
        receipt = json.loads(Path(stages[title]['worker_log_path']).read_text(encoding='utf-8'))
        assert receipt['installer_prepared'] is False and receipt['status'] == 'needs_review'
        assert receipt['result'][key]['offers_count'] == count
    for title in ('Clean temporary files', 'Check or install Windows security updates',
                  'Optimize eligible internal drives'):
        assert stages[title]['result']['skipped'] is True
    terminal = [job for event in events for job in event.get('jobs', []) if job['status'] not in ACTIVE_JOB_STATES]
    assert {job['id'] for job in terminal} == {'software_updates', 'driver_updates'}
    assert all(job['installation_blocked'] and job['scan_complete'] for job in terminal)
    assert all(job['scan_status'] == 'completed' and job['scan_label'] for job in terminal)


@pytest.mark.parametrize('parallel', [False, True])
def test_native_app_search_retains_fresh_counts_and_original_barrier(tmp_path, parallel):
    path, original = old_receipt(tmp_path)
    service = Backend(tmp_path, native=True)
    service.release.set()
    service.cleanup_categories = Mock(return_value=[{'id': 'fixture-temp', 'available': True}])
    service.updates_scan = Mock(wraps=service.updates_scan)
    forbid_mutations(service)
    report = NativeWorkflow(service).run(settings(parallel_app_updates_enabled=parallel,
                                                 cleanup_enabled=True), lambda _: None)
    service.updates_scan.assert_called_once()
    service.update_app_unattended.assert_not_called()
    service.cleanup_scan.assert_not_called()
    result = next(stage['result'] for stage in report['stages'] if stage['title'] == NATIVE_SOFTWARE)
    assert result['installation_blocked'] is True
    assert result['app_scan']['complete'] is True and result['app_scan']['offers_count'] == 2
    assert path.read_bytes() == original and pending_updates_barrier(service)


@pytest.mark.parametrize('review', [False, True])
@pytest.mark.parametrize('empty', [False, True])
def test_queue_discovers_pending_barrier_itself_and_retains_it_even_without_install_candidates(tmp_path, review, empty):
    from optimus.automated_software import run_software
    path, original = old_receipt(tmp_path)
    service = Backend(tmp_path)
    service.release.set()
    scan = service.updates_scan()
    scan['updates'] = [] if empty else scan['updates']
    service.updates_scan = Mock(return_value=scan)
    forbid_mutations(service)
    config = settings(software_update_policy='review' if review else 'selected')
    def operation(worker, item, prepared, before_install):
        return run_software(worker, config, item_callback=item,
                            prepared_callback=prepared, before_install=before_install)
    queue = ParallelInstallerQueue(service, 'a' * 32, [(WINDOWS_SOFTWARE, operation)], ProgressMux(lambda _: None))
    queue.start()
    reason = queue.wait_prepared()
    assert reason and str(path) in reason
    queue.release_maintenance()
    result = queue.join()[0]['result']
    service.updates_scan.assert_called_once()
    service.update_app_unattended.assert_not_called()
    assert result['app_scan']['offers_count'] == (0 if empty else 2)
    assert result['installation_blocked'] is True and result['blocked_by_prior_installer'] is True
    assert queue.barrier == reason and not queue.jobs[0].record['installer_prepared']
    assert path.read_bytes() == original and pending_updates_barrier(service)


def test_unphased_operation_still_cannot_execute_under_prior_barrier(tmp_path):
    old_receipt(tmp_path)
    service = Backend(tmp_path)
    operation, events = Mock(), []
    job = ParallelAppUpdates(service, 'a' * 32, WINDOWS_SOFTWARE, operation, ProgressMux(events.append))
    job.start()
    job.join()
    operation.assert_not_called()
    assert job.result['blocked_by_prior_installer'] is True
    state = events[-1]['jobs'][0]
    assert state['scan_status'] == 'not_started' and state['scan_complete'] is False
    assert state['installation_blocked'] is True and not job.record['installer_prepared']


def test_barrier_first_observed_during_second_job_preparation_applies_to_both_empty_searches(tmp_path):
    from optimus.automated_software import run_software
    from optimus.automated_system import run_drivers
    service = Backend(tmp_path)
    enable_fixture_driver(service, Mock())
    service.updates_scan = Mock(return_value={'structured': True, 'exitCode': 0, 'updates': []})
    service.driver_updates_scan.return_value = {'resultCode': 2, 'updates': []}
    forbid_mutations(service)
    config = driver_settings()
    def operation(updater):
        def run(worker, item, prepared, before_install):
            return updater(worker, config, item_callback=item,
                           prepared_callback=prepared, before_install=before_install)
        return run
    queue = ParallelInstallerQueue(service, 'a' * 32,
        [(WINDOWS_SOFTWARE, operation(run_software)), (WINDOWS_DRIVERS, operation(run_drivers))],
        ProgressMux(lambda _: None))
    reason = 'Fixture unresolved installer became visible while preparing the second job.'
    with patch.object(ParallelAppUpdates, '_pending', side_effect=[None, reason]):
        queue.start()
    assert queue.wait_prepared() == reason
    for stage in queue.join():
        assert stage['result']['installation_block_reason'] == reason
        assert stage['result']['installation_blocked'] is True
        assert stage['result']['installer_started'] is False
    service.updates_scan.assert_called_once()
    service.driver_updates_scan.assert_called_once()
    assert all(not job.record['installer_prepared'] for job in queue.jobs)


def test_failed_search_remains_failed_under_prior_installer_barrier(tmp_path):
    path, original = old_receipt(tmp_path)
    service = Backend(tmp_path)
    service.release.set()
    service.updates_scan = Mock(side_effect=RuntimeError('Fixture app catalog failed'))
    enable_fixture_driver(service, Mock())
    service.driver_updates_scan.return_value = {'resultCode': 4, 'updates': [], 'error': 'Fixture driver catalog failed'}
    forbid_mutations(service)
    report = Workflow(service).run(driver_settings(), lambda _: None)
    for stage in report['stages']:
        if stage['title'] in (WINDOWS_SOFTWARE, WINDOWS_DRIVERS):
            result = stage['result']
            key = 'app_scan' if stage['title'] == WINDOWS_SOFTWARE else 'driver_scan'
            assert stage['status'] == 'failed' and result[key]['complete'] is False
            assert result[key]['status'] == 'failed' and result[key]['errors']
            assert result['installation_blocked'] is True
    service.updates_scan.assert_called_once()
    service.driver_updates_scan.assert_called_once()
    assert path.read_bytes() == original and pending_updates_barrier(service)


def test_automatic_app_hold_reason_survives_completion_and_bounded_receipt():
    result = {'items': [], 'held_count': 1, 'held_ids': ['Fixture.HeldApp'],
              'automatic_update_holds': [{'id': 'Fixture.HeldApp', 'available_version': '2.0',
                  'reason': 'Automatic update held: the previous installer failed its compatibility check.'}],
              'app_scan': {'complete': True, 'status': 'completed', 'offers_count': 2,
                           'summary': 'Fixture catalog found 2 app updates.'}}
    state = update_completion_state(result)
    assert '1 app update held for review.' in state['description']
    assert 'previous installer failed its compatibility check' in state['description']
    assert state['offers_count'] == 2 and state['scan_complete'] is True
    saved = _journal_result(result)
    assert saved['held_count'] == 1 and saved['held_ids'] == ['Fixture.HeldApp']
    assert saved['automatic_update_holds'] == result['automatic_update_holds']
    result['automatic_update_holds'][0]['reason'] = 'x' * 10000
    assert len(_journal_result(result)['automatic_update_holds'][0]['reason']) == 1000
    assert len(update_completion_state(result)['description']) <= 2000
