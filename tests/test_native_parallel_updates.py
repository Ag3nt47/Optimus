"""Parallel scheduling is exercised with latches and injected services only."""
import copy
import json
import threading
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest

from optimus.automation import CURRENT_SETUP_VERSION, RunLease
from optimus.workflow import DEFAULTS, Preferences, Workflow
from optimus.native_workflow import NativeWorkflow
from optimus.parallel_updates import (ParallelAppUpdates, ProgressMux, WINDOWS_SOFTWARE,
                                      NATIVE_SOFTWARE, WAITING_FOR_UPDATES, isolated_backend,
                                      installer_barrier, parallel_stages)
from optimus.maintenance import Maintenance
from optimus.native_operations import NativePackages
from test_workflow import mock_verification


@pytest.fixture(autouse=True)
def fixture_owner():
    with patch('optimus.speed_enhancements._current_owner', return_value='fixture-owner'):
        yield


def settings(**changes):
    value = dict(DEFAULTS, cleanup_enabled=False, browser_cache_enabled=False,
                 software_updates_enabled=True, parallel_app_updates_enabled=True,
                 setup_version=CURRENT_SETUP_VERSION, software_update_policy='selected',
                 software_update_ids=['Fixture.One', 'Fixture.Two'],
                 software_update_agreements_accepted=True)
    value.update(changes)
    return Preferences.validate(value)


class Backend:
    def __init__(self, directory, *, native=False):
        self.data_dir = directory
        self.bridge = SimpleNamespace(_process=None)
        self.calls = []
        self.started, self.release, self.independent = (threading.Event() for _ in range(3))
        self.native = native
        self.system = 'linux' if native else 'win32'
        self.preference_store = SimpleNamespace(validate=Preferences.validate)
        self.outcome = {'exitCode': 0, 'verified': True}
        self.audit_count = 0

    def audit(self):
        self.audit_count += 1
        self.calls.append('audit')
        if self.audit_count > 1:
            assert self.release.is_set()
        return {'fixture': True}

    def updates_scan(self):
        self.calls.append('updates_scan')
        return {'structured': True, 'complete': True, 'exitCode': 0,
                'updates': [{'id': identity, 'package_id': identity, 'source': 'winget',
                             'version': '1.0', 'available': '2.0'}
                            for identity in ['Fixture.One', 'Fixture.Two']]}

    def update_app_unattended(self, identity, **kwargs):
        self.calls.append('update:' + identity)
        # Every mutation has durable prepared/running evidence first.
        records = [json.loads(path.read_text()) for path in (self.data_dir / 'parallel-updates').glob('*.json')]
        record = next(row for row in records if row['status'] == 'running')
        assert record['status'] == 'running'
        assert record['current_item']['description']
        self.started.set()
        assert self.release.wait(5), 'Test did not release fixture installer.'
        if isinstance(self.outcome, Exception):
            raise self.outcome
        return dict(self.outcome)

    def installed_apps(self):
        assert self.started.wait(5)
        self.calls.append('independent')
        self.independent.set()
        return []

    def _apply_drive_optimization_stage(self, *args, **kwargs):
        assert self.release.is_set()
        self.calls.append('drive')
        return {'items': [], 'failed_items': 0}

    def driver_updates_scan(self):
        self.calls.append('drivers')
        return {'resultCode': 2, 'updates': []}

    def windows_security_updates(self, *args, **kwargs):
        assert self.release.is_set()
        self.calls.append('windows_updates')
        return {'failed_items': 0, 'review_only': True, 'scan': {'updates': []}}

    def verify_optimization(self, report, **kwargs):
        assert self.release.is_set()
        self.calls.append('verify')
        software = next(row for row in report['stages'] if row['title'] in (WINDOWS_SOFTWARE, NATIVE_SOFTWARE))
        assert software['status'] != 'running'
        assert 'result' in software
        return mock_verification(report)


def start_workflow(service, configuration, *, native=False, callback=None, cancel=None):
    result, errors = [], []
    workflow = (NativeWorkflow if native else Workflow)(service)
    def run():
        try:
            result.append(workflow.run(configuration, callback or (lambda _: None), cancel))
        except BaseException as error:
            errors.append(error)
    thread = threading.Thread(target=run)
    thread.start()
    return workflow, thread, result, errors


@pytest.mark.parametrize('native', [False, True])
def test_independent_stage_overlaps_but_queued_stages_and_final_readback_wait(tmp_path, native):
    service = Backend(tmp_path, native=native)
    events = []
    waiting = threading.Event()
    def progress(event):
        events.append(event)
        if event.get('waiting_for') == 'software_updates':
            waiting.set()
    configuration = settings(applications_review_enabled=True, driver_updates_enabled=True,
                             drive_optimization_enabled=not native, windows_security_updates_enabled=not native)
    workflow, thread, result, errors = start_workflow(service, configuration, native=native, callback=progress)
    try:
        assert service.independent.wait(5)
        assert waiting.wait(5)
        assert thread.is_alive()
        assert service.calls.count('audit') == 1
        assert 'drivers' in service.calls  # Read-only metadata remains independent.
        assert not any(row in service.calls for row in ('drive', 'windows_updates', 'verify'))
        assert workflow.last_report['stages'][1]['status'] == 'running'
        assert all(event.get('percentage', 0) < 100 for event in events)
        assert any(event.get('jobs', [{}])[0].get('status') == 'running' for event in events)
    finally:
        service.release.set()
        thread.join(8)
    assert not errors
    assert not thread.is_alive()
    assert result[0]['status'] == 'completed'
    assert service.calls.index('verify') > service.calls.index('update:Fixture.Two')
    assert service.calls.count('audit') == 2
    assert events[-1]['percentage'] == 100
    assert events[-1]['jobs'][0]['status'] == 'completed'
    software = [row for row in result[0]['stages'] if row['title'] in (WINDOWS_SOFTWARE, NATIVE_SOFTWARE)]
    assert len(software) == 1 and len(software[0]['result']['items']) == 2


@pytest.mark.parametrize('outcome', [
    {'exitCode': 1}, {'exitCode': 0, 'restartNeeded': True},
    {'exitCode': None, 'running': True, 'outcomeKnown': False}, RuntimeError('Vendor timed out'),
])
def test_late_update_failure_or_restart_defers_other_installer_channels(tmp_path, outcome):
    service = Backend(tmp_path)
    service.outcome = outcome
    workflow, thread, result, errors = start_workflow(service, settings(
        applications_review_enabled=True, drive_optimization_enabled=True,
        windows_security_updates_enabled=True, consumer_debloat_enabled=False))
    try:
        assert service.independent.wait(5)
    finally:
        service.release.set()
        thread.join(8)
    assert not errors
    assert 'drive' not in service.calls and 'windows_updates' not in service.calls
    stages = {row['title']: row for row in result[0]['stages']}
    assert stages['Optimize eligible internal drives']['result']['skipped']
    assert stages['Check or install Windows security updates']['result']['stop_reason']
    assert 'verify' in service.calls
    if isinstance(outcome, Exception) or outcome.get('exitCode') not in (0,):
        assert result[0]['failed_stages'] >= 1


@pytest.mark.parametrize('native', [False, True])
def test_cancel_waits_current_installer_and_retains_lease_then_skips_next_package(tmp_path, native):
    service = Backend(tmp_path, native=native)
    cancel = threading.Event()
    workflow, thread, result, errors = start_workflow(service, settings(applications_review_enabled=True),
                                                     native=native, cancel=cancel)
    try:
        assert service.independent.wait(5)
        cancel.set()
        assert thread.is_alive()
        with pytest.raises(RuntimeError):
            RunLease(tmp_path).acquire()
    finally:
        service.release.set()
        thread.join(8)
    assert not errors
    assert result[0]['status'] == 'cancelled'
    assert service.calls.count('update:Fixture.One') == 1
    assert 'update:Fixture.Two' not in service.calls
    assert 'verify' not in service.calls
    with RunLease(tmp_path):
        pass


def test_foreground_callback_exception_still_joins_and_saves_worker_result(tmp_path):
    service = Backend(tmp_path)
    raised = threading.Event()
    def callback(event):
        if (event.get('stage') == 'Review installed applications' and not raised.is_set()
                and event.get('description') == 'Review installed applications: completed'):
            raised.set()
            raise RuntimeError('Fixture display failure')
    workflow, thread, result, errors = start_workflow(service, settings(applications_review_enabled=True), callback=callback)
    try:
        assert raised.wait(5)
        assert thread.is_alive()
        with pytest.raises(RuntimeError):
            RunLease(tmp_path).acquire()
    finally:
        service.release.set()
        thread.join(8)
    assert isinstance(errors[0], RuntimeError)
    saved = json.loads(next((tmp_path / 'runs').glob('*.json')).read_text())
    software = next(row for row in saved['stages'] if row['title'] == WINDOWS_SOFTWARE)
    assert software['status'] == 'cancelled'
    assert len(software['result']['items']) == 1
    with RunLease(tmp_path):
        pass


def test_pending_journal_never_replays_and_still_allows_final_readonly_checks(tmp_path):
    directory = tmp_path / 'parallel-updates'
    directory.mkdir()
    identifier = 'a' * 32
    (directory / (identifier + '.json')).write_text(json.dumps(
        {'id': identifier, 'schema': 1, 'owner': 'fixture-owner', 'status': 'uncertain'}))
    service = Backend(tmp_path)
    service.release.set()
    result = Workflow(service).run(settings(), lambda _: None)
    assert not any(row.startswith('update:') for row in service.calls)
    stage = next(row for row in result['stages'] if row['title'] == WINDOWS_SOFTWARE)
    assert stage['result']['uncertain'] and stage['result']['skipped']
    assert 'verify' in service.calls


def test_prepared_journal_failure_cannot_start_worker(tmp_path):
    service = Backend(tmp_path)
    operation = Mock()
    job = ParallelAppUpdates(service, 'b' * 32, WINDOWS_SOFTWARE, operation, ProgressMux(lambda _: None))
    with patch('optimus.speed_enhancements._private_json', side_effect=OSError('Fixture disk full')):
        with pytest.raises(OSError):
            job.start()
    operation.assert_not_called()
    assert job.thread is None


def test_thread_launch_exception_records_no_installer_and_allows_later_attempt(tmp_path):
    service = Backend(tmp_path)
    operation = Mock(return_value={'items': [], 'failed_items': 0})
    job = ParallelAppUpdates(service, 'd' * 32, WINDOWS_SOFTWARE, operation, ProgressMux(lambda _: None))
    with patch('optimus.parallel_updates.threading.Thread.start', side_effect=RuntimeError('Fixture thread unavailable')):
        with pytest.raises(RuntimeError):
            job.start()
    operation.assert_not_called()
    saved = json.loads(job.path.read_text())
    assert saved['status'] == 'deferred' and saved['installer_prepared'] is False
    retry = ParallelAppUpdates(service, 'e' * 32, WINDOWS_SOFTWARE, operation, ProgressMux(lambda _: None))
    retry.start()
    assert retry.join()['status'] == 'completed'
    operation.assert_called_once()


def test_scan_exception_before_any_installer_does_not_create_liveness_uncertainty(tmp_path):
    service = Backend(tmp_path)
    operation = Mock(side_effect=RuntimeError('Fixture catalog unavailable'))
    job = ParallelAppUpdates(service, 'f' * 32, WINDOWS_SOFTWARE, operation, ProgressMux(lambda _: None))
    job.start()
    assert job.join()['status'] == 'failed'
    saved = json.loads(job.path.read_text())
    assert saved['status'] == 'needs_review' and saved['installer_prepared'] is False
    operation.side_effect = None
    operation.return_value = {'items': [], 'failed_items': 0}
    retry = ParallelAppUpdates(service, '1' * 32, WINDOWS_SOFTWARE, operation, ProgressMux(lambda _: None))
    retry.start()
    assert retry.join()['status'] == 'completed'


@pytest.mark.parametrize('parallel', [False, True])
def test_persistent_unknown_blocks_serial_and_parallel_file_mutations(tmp_path, parallel):
    directory = tmp_path / 'parallel-updates'
    directory.mkdir()
    identifier = 'c' * 32
    (directory / (identifier + '.json')).write_text(json.dumps(
        {'id': identifier, 'schema': 1, 'owner': 'fixture-owner', 'status': 'uncertain'}))
    service = Backend(tmp_path)
    service.release.set()
    service.cleanup_scan = Mock(side_effect=AssertionError('Unknown child may still own staging files'))
    report = Workflow(service).run(settings(parallel_app_updates_enabled=parallel,
                                            cleanup_enabled=True, cleanup_age_days=0), lambda _: None)
    service.cleanup_scan.assert_not_called()
    assert not any(row.startswith('update:') for row in service.calls)
    cleanup = next(row for row in report['stages'] if row['title'] == 'Clean temporary files')
    assert cleanup['result']['skipped'] and cleanup['result']['stop_reason']


def test_known_no_installer_low_disk_deferral_does_not_poison_future_run(tmp_path):
    service = Backend(tmp_path)
    service.release.set()
    service.outcome = {'exitCode': 0, 'skipped': True, 'requiresReview': True, 'note': 'Fixture low disk space'}
    first = Workflow(service).run(settings(), lambda _: None)
    assert first['failed_stages'] == 1
    record = json.loads(next((tmp_path / 'parallel-updates').glob('*.json')).read_text())
    assert record['status'] == 'needs_review'
    service.outcome = {'exitCode': 0, 'verified': True}
    second = Workflow(service).run(settings(), lambda _: None)
    assert second['status'] == 'completed'
    assert service.calls.count('update:Fixture.One') == 2


def test_known_restart_blocks_until_fresh_readback_clears_it(tmp_path):
    service = Backend(tmp_path)
    service.release.set()
    service.outcome = {'exitCode': 0, 'restartNeeded': True}
    service.security_status = Mock(return_value={'reboot_required': True})
    Workflow(service).run(settings(), lambda _: None)
    record = json.loads(next((tmp_path / 'parallel-updates').glob('*.json')).read_text())
    assert record['status'] == 'needs_review'
    Workflow(service).run(settings(), lambda _: None)
    assert service.calls.count('update:Fixture.One') == 1
    service.security_status.return_value = {'reboot_required': False}
    service.outcome = {'exitCode': 0}
    third = Workflow(service).run(settings(), lambda _: None)
    assert third['status'] == 'completed'
    assert service.calls.count('update:Fixture.One') == 2


def test_serial_application_failure_defers_later_windows_installer(tmp_path):
    service = Backend(tmp_path)
    service.release.set()
    service.outcome = RuntimeError('Unknown fixture vendor child')
    report = Workflow(service).run(settings(parallel_app_updates_enabled=False,
                                           windows_security_updates_enabled=True), lambda _: None)
    assert 'windows_updates' not in service.calls
    row = next(row for row in report['stages'] if row['title'] == 'Check or install Windows security updates')
    assert row['result']['skipped']


def test_session_copy_isolates_known_caches_without_running_constructors(tmp_path):
    maintenance = object.__new__(Maintenance)
    maintenance._last_update_ids = {'Foreground.Package'}
    maintenance._last_update_packages = {'Foreground.Package': {'id': 'Foreground.Package'}}
    maintenance._last_update_scan = 123
    packages = object.__new__(NativePackages)
    packages._last_scan = {'foreground': True}
    packages._lock = threading.RLock()
    service = SimpleNamespace(maintenance=maintenance, native_packages=packages)
    with (patch.object(Maintenance, '__init__', side_effect=AssertionError('No constructor')),
          patch.object(NativePackages, '__init__', side_effect=AssertionError('No discovery'))):
        worker = isolated_backend(service)
    assert worker.maintenance is not maintenance
    assert worker.maintenance._last_update_ids == set()
    assert worker.maintenance._last_update_packages == {}
    assert worker.maintenance._last_update_scan == 0
    worker.maintenance._last_update_ids.add('Worker.Package')
    assert maintenance._last_update_ids == {'Foreground.Package'}
    assert worker.native_packages is not packages
    assert worker.native_packages._last_scan is None
    assert worker.native_packages._lock is packages._lock
    injected = Mock()
    fake = SimpleNamespace(maintenance=Mock(), native_packages=Mock(), updates_scan=injected)
    assert isolated_backend(fake).updates_scan is injected


def test_progress_mux_keeps_foreground_and_unions_processes():
    events = []
    mux = ProgressMux(events.append)
    mux({'stage': 'Independent review', 'percentage': 40, 'processes': ['Optimus worker'], 'description': 'Reviewing', 'log': 'One foreground log'})
    mux.background('running', {'description': 'Updating Fixture.One', 'current': 1, 'total': 3})
    assert events[-1]['stage'] == 'Independent review'
    assert events[-1]['description'] == 'Reviewing'
    assert events[-1]['processes'] == ['Optimus worker', 'Updating Fixture.One']
    assert 'log' not in events[-1]
    mux({'stage': 'Next review', 'percentage': 100, 'processes': ['Optimus worker']})
    assert events[-1]['percentage'] == 99
    assert events[-1]['jobs'][0]['current'] == 1
    mux.background('completed')
    mux({'stage': 'Final verified result', 'percentage': 100})
    assert events[-1]['percentage'] == 100
    assert events[-1]['jobs'][0]['status'] == 'completed'
    assert 'elapsed_seconds' not in events[-1]['jobs'][0]


@pytest.mark.parametrize('value', [None, {'failed_items': 1}, {'running': True},
                                  {'restartNeeded': True}, {'stop_reason': 'Review'},
                                  {'items': [{'status': 'failed'}]}])
def test_barrier_handles_all_material_unknown_or_failed_shapes(value):
    assert installer_barrier(value)


@pytest.mark.parametrize('native', [False, True])
def test_all_cleanup_finishes_before_updater_and_independent_work_overlaps(tmp_path, native):
    service = Backend(tmp_path, native=native)
    def categories():
        return [{'id': 'fixture-temp', 'available': True}]
    def scan(categories, age):
        assert not service.started.is_set()
        service.calls.append('cleanup_scan')
        return {'files': [{'fixture': True}], 'complete': True, 'total_bytes': 1, 'errors': []}
    def apply(plan):
        assert not service.started.is_set()
        service.calls.append('cleanup_apply')
        return {'deleted_count': 1, 'deleted_bytes': 1, 'skipped_count': 0, 'errors': []}
    def browser(*args, **kwargs):
        assert not service.started.is_set()
        service.calls.append('browser_cleanup')
        return {'items': [], 'failed_items': 0}
    service.cleanup_categories, service.cleanup_scan, service.cleanup_apply = categories, scan, apply
    with patch('optimus.browser_cleanup.run_browser_cleanup', side_effect=browser):
        workflow, thread, result, errors = start_workflow(service, settings(
            applications_review_enabled=True, cleanup_enabled=True,
            cleanup_category_ids=['fixture-temp'], browser_cache_enabled=True), native=native)
        try:
            assert service.independent.wait(5)
            assert 'cleanup_apply' in service.calls and 'browser_cleanup' in service.calls
            assert service.calls.index('browser_cleanup') < service.calls.index('update:Fixture.One')
            assert thread.is_alive() and 'verify' not in service.calls
        finally:
            service.release.set()
            thread.join(8)
    assert not errors and result[0]['status'] == 'completed'


def test_startup_duplicate_cloud_and_driver_reviews_continue_before_wait(tmp_path):
    service = Backend(tmp_path)
    reviews_done = threading.Event()
    events = []
    service.startup_list = Mock(side_effect=lambda: service.calls.append('startup_review') or [])
    service.duplicate_scan = Mock(side_effect=lambda root: service.calls.append('duplicate_review') or {})
    service.cloud_status = Mock(return_value={'connected': True})
    service.cloud_scan = Mock(side_effect=lambda: service.calls.append('cloud_review') or {})
    service.apply_system_privacy = Mock(return_value={'failed_items': 1})
    def progress(event):
        events.append(event)
        if event.get('waiting_for') == 'software_updates':
            reviews_done.set()
    workflow, thread, result, errors = start_workflow(service, settings(
        applications_review_enabled=True, startup_review_enabled=True,
        duplicates_enabled=True, duplicate_roots=[r'D:\fixture-duplicates'], cloud_review_enabled=True,
        driver_updates_enabled=True, privacy_advertising_enabled=True), callback=progress)
    try:
        assert reviews_done.wait(5)
        assert all(call in service.calls for call in ('startup_review', 'duplicate_review', 'cloud_review', 'drivers'))
        wait = events[-1]
        assert wait['stage'] == WAITING_FOR_UPDATES
        assert 'Apply saved privacy controls' not in wait['stage']
        assert wait['jobs'][0]['current_package'] == 'Fixture.One'
        assert wait['jobs'][0]['total'] == 2 and wait['jobs'][0]['current'] == 0
        assert 'verify' not in service.calls
        with pytest.raises(RuntimeError):
            RunLease(tmp_path).acquire()
    finally:
        service.release.set()
        thread.join(8)
    assert not errors and result[0]['failed_stages'] == 1  # Privacy failure remains in its own result.


def test_waiting_heartbeat_preserves_package_and_refreshes_elapsed_without_logs():
    events = []
    with patch('optimus.parallel_updates.time.monotonic', side_effect=[100, 100, 101, 161]):
        mux = ProgressMux(events.append)
        mux({'stage': 'Apply saved privacy controls', 'percentage': 29, 'completed': 8, 'total': 28,
             'description': 'Privacy failed; review its items.', 'log': 'FAILED: Apply saved privacy controls'})
        mux.background('running', {'id': 'Ollama.Ollama', 'description': 'Updating package 2/7: Ollama.Ollama',
                                   'current': 1, 'total': 7})
        mux.wait_for_updates('Install approved driver updates')
        mux.heartbeat()
    assert events[-1]['stage'] == WAITING_FOR_UPDATES and events[-1]['percentage'] == 29
    assert events[-1]['waiting_for'] == 'software_updates'
    assert events[-1]['jobs'][0]['current_package'] == 'Ollama.Ollama'
    assert events[-1]['jobs'][0]['status'] == 'running'  # Foreground wait does not stop the updater.
    assert events[-1]['jobs'][0]['current'] == 1 and events[-1]['jobs'][0]['total'] == 7
    assert events[-1]['jobs'][0]['elapsed_seconds'] > events[-2]['jobs'][0]['elapsed_seconds']
    assert 'log' not in events[-1]


def test_parallel_schedule_with_no_software_is_unchanged():
    stages = [('Inspect resources', lambda: None), ('Clean temporary files', lambda: None)]
    planned, software, launch_after = parallel_stages(stages, WINDOWS_SOFTWARE)
    assert planned is stages and software is None and launch_after is None


@pytest.mark.parametrize('native', [False, True])
@pytest.mark.parametrize('audit_failure', [False, True])
def test_audit_failure_not_last_cleanup_result_controls_updater_launch(tmp_path, native, audit_failure):
    service = Backend(tmp_path, native=native)
    service.release.set()
    original_audit = service.audit
    def audit():
        value = original_audit()
        if service.audit_count == 1 and audit_failure:
            value['failed_items'] = 1
        return value
    def scan(categories, age):
        service.calls.append('cleanup_scan')
        # Audit failure followed by successful cleanup must not authorize updates.
        # Cleanup failure after a successful audit must not masquerade as audit failure.
        return {'files': [], 'complete': audit_failure, 'total_bytes': 0,
                'errors': [] if audit_failure else ['Fixture incomplete cleanup preview']}
    service.audit = audit
    service.cleanup_scan = scan
    service.cleanup_categories = lambda: [{'id': 'fixture-temp', 'available': True}]
    report = (NativeWorkflow if native else Workflow)(service).run(settings(
        cleanup_enabled=True, cleanup_category_ids=['fixture-temp']), lambda _: None)
    software = next(row for row in report['stages'] if row['title'] in (WINDOWS_SOFTWARE, NATIVE_SOFTWARE))
    if audit_failure:
        assert not any(call.startswith('update:') for call in service.calls)
        assert 'initial before-inventory failed' in software['result']['stop_reason']
    else:
        assert 'update:Fixture.One' in service.calls
        assert service.calls.index('cleanup_scan') < service.calls.index('update:Fixture.One')
        assert any('Clean ' in row['title'] and row['status'] == 'failed' for row in report['stages'])
