"""Real worker orchestration with disposable state and no host maintenance."""
from __future__ import annotations

import json
import threading
from types import SimpleNamespace

import pytest

from optimus.automation import CURRENT_SETUP_VERSION, RunLease
from optimus.automated_system import driver_descriptor
from optimus.automated_system import run_drivers
from optimus.bridge import PowerShellBridge
from optimus.parallel_updates import (ParallelAppUpdates, ParallelInstallerQueue, ProgressMux,
                                      WINDOWS_DRIVERS, WINDOWS_DRIVER_REVIEW, WINDOWS_SOFTWARE)
from optimus.workflow import DEFAULTS, Preferences, Workflow


@pytest.fixture(autouse=True)
def mocked_host(monkeypatch):
    monkeypatch.setattr('optimus.speed_enhancements._current_owner', lambda: 'fixture-owner')
    monkeypatch.setattr('optimus.runtime_priority.lower_worker_priority', lambda: lambda: None)


def driver():
    return {'id': '00000000-0000-0000-0000-000000000001', 'revision': 1,
            'title': 'Disposable fixture display driver', 'hardwareId': r'PCI\VEN_1234&DEV_5678',
            'provider': 'Fixture provider', 'driverClass': 'Display', 'driverDate': '2026-10-01T00:00:00Z',
            'canRequestUserInput': False, 'rebootBehavior': 2, 'eulaAccepted': False}


def configuration(**changes):
    settings = dict(DEFAULTS, setup_version=CURRENT_SETUP_VERSION, cleanup_enabled=True,
        cleanup_age_days=0, browser_cache_enabled=False, parallel_app_updates_enabled=True,
        software_updates_enabled=True, software_update_policy='selected',
        software_update_ids=['Fixture.App'], software_update_agreements_accepted=True,
        driver_updates_enabled=True, driver_policy='install_approved', driver_changes_approved=True,
        driver_update_agreements_accepted=True, driver_approved_updates=[driver_descriptor(driver())])
    settings.update(changes)
    return Preferences.validate(settings)


def verification():
    return {'schema': 1, 'read_only': True, 'status': 'not_applicable',
            'counts': {'confirmed': 0, 'needs_review': 0, 'unverified': 0, 'not_applicable': 1},
            'stages': [], 'coverage_complete': True, 'failed_items': 0,
            'summary': 'Fixture verification inspected completed worker receipts.'}


class FixtureBackend:
    def __init__(self, directory):
        self.data_dir = directory
        self.system = 'win32'
        self.bridge = SimpleNamespace(_process=None)
        self.calls = []
        self.shared = {'audit': 0, 'active': 0, 'maximum': 0}
        self.lock = threading.Lock()
        self.entered = {name: threading.Event() for name in ('audit', 'apps', 'drivers')}
        self.discovery_release = threading.Event()
        self.cleanup_started = threading.Event()
        self.cleanup_release = threading.Event()
        self.cleanup_done = threading.Event()
        self.installer_started = threading.Event()
        self.installer_release = threading.Event()
        self.foreground_review = threading.Event()

    def audit(self):
        with self.lock:
            self.shared['audit'] += 1
            count = self.shared['audit']
        self.calls.append('audit:' + str(count))
        if count == 1:
            self.entered['audit'].set()
            assert self.discovery_release.wait(8), 'Fixture did not release initial audit.'
        else:
            assert self.shared['active'] == 0
            assert {'install:app', 'install:driver'} <= set(self.calls)
        return {'System': {}, 'Startup': []}

    def updates_scan(self):
        self.entered['apps'].set()
        assert self.discovery_release.wait(8), 'Fixture did not release software discovery.'
        self.calls.append('scan:app')
        return {'source': 'winget', 'structured': True, 'exitCode': 0,
                'updates': [{'id': 'Fixture.App', 'source': 'winget', 'version': '1.0', 'available': '2.0'}]}

    def driver_updates_scan(self):
        self.entered['drivers'].set()
        assert self.discovery_release.wait(8), 'Fixture did not release driver discovery.'
        self.calls.append('scan:driver')
        return {'resultCode': 2, 'updates': [driver()]}

    def cleanup_scan(self, ids, age):
        assert all(event.is_set() for event in self.entered.values())
        assert self.discovery_release.is_set()
        assert not self.installer_started.is_set()
        self.calls.append('cleanup:scan')
        self.cleanup_started.set()
        assert self.cleanup_release.wait(8), 'Fixture did not release cleanup.'
        return {'complete': True, 'errors': [], 'files': [{'fixture': True}], 'total_bytes': 1}

    def cleanup_apply(self, plan):
        assert not self.installer_started.is_set()
        self.calls.append('cleanup:done')
        self.cleanup_done.set()
        return {'deleted_count': 1, 'deleted_bytes': 1, 'skipped_count': 0}

    def is_admin(self):
        return True

    def update_disk_readiness(self):
        return {'ready': True}

    def driver_backup_automatic(self):
        assert self.cleanup_done.is_set()
        with self.lock:
            assert self.shared['active'] == 0
        self.calls.append('backup:driver')
        return {'exit_code': 0, 'destination': str(self.data_dir / 'fixture-backup')}

    def _installer(self, kind):
        assert self.cleanup_done.is_set()
        records = [json.loads(path.read_text(encoding='utf-8'))
                   for path in (self.data_dir / 'parallel-updates').glob('*.json')]
        title = WINDOWS_SOFTWARE if kind == 'app' else WINDOWS_DRIVERS
        record = next(value for value in records if value['title'] == title)
        assert record['status'] == 'running' and record['installer_prepared'] is True
        with self.lock:
            self.shared['active'] += 1
            self.shared['maximum'] = max(self.shared['maximum'], self.shared['active'])
            assert self.shared['active'] == 1
        self.calls.append('install:' + kind)
        self.installer_started.set()
        try:
            assert self.installer_release.wait(8), 'Fixture did not release installer.'
        finally:
            with self.lock:
                self.shared['active'] -= 1

    def update_app_unattended(self, identity, **kwargs):
        assert identity == 'Fixture.App'
        self._installer('app')
        return {'exitCode': 0, 'installer_started': True, 'outcomeKnown': True, 'running': False}

    def driver_update_apply_unattended(self, value, **kwargs):
        self._installer('driver')
        return {'resultCode': 2, 'hresult': 0, 'aggregateResultCode': 2, 'aggregateHresult': 0,
                'installer_started': True, 'outcomeKnown': True, 'running': False, 'restartNeeded': False}

    def installed_apps(self):
        assert self.installer_started.wait(8)
        self.foreground_review.set()
        return []

    def verify_optimization(self, report, **kwargs):
        updates = [row for row in report['stages'] if row['title'] in (WINDOWS_SOFTWARE, WINDOWS_DRIVERS)]
        assert len(updates) == 2 and all(row['status'] != 'running' for row in updates)
        assert all('result' in row for row in updates)
        self.calls.append('verify')
        return verification()


def test_three_workers_start_together_then_cleanup_precedes_exclusive_installers(tmp_path):
    backend = FixtureBackend(tmp_path)
    cancel, result, errors = threading.Event(), [], []
    workflow = Workflow(backend)
    def run():
        try:
            result.append(workflow.run(configuration(applications_review_enabled=True), lambda _: None, cancel))
        except BaseException as error:
            errors.append(error)
    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    try:
        # None of the three can finish until all three have actually entered.
        assert all(event.wait(5) for event in backend.entered.values())
        assert not backend.cleanup_started.is_set() and not backend.installer_started.is_set()
        backend.discovery_release.set()
        assert backend.cleanup_started.wait(5)
        assert not backend.installer_started.is_set()
        backend.cleanup_release.set()
        assert backend.installer_started.wait(5)
        assert backend.foreground_review.wait(5)
        assert thread.is_alive()
        with pytest.raises(RuntimeError):
            RunLease(tmp_path).acquire()
    finally:
        backend.discovery_release.set()
        backend.cleanup_release.set()
        backend.installer_release.set()
        thread.join(8)
        if thread.is_alive():
            cancel.set()
            thread.join(8)
    assert not thread.is_alive() and not errors
    assert result[0]['status'] == 'completed'
    assert backend.shared['maximum'] == 1
    assert backend.calls.index('cleanup:done') < backend.calls.index('install:app')
    assert backend.calls.index('cleanup:done') < backend.calls.index('backup:driver')
    assert backend.calls.index('verify') > backend.calls.index('install:driver')
    assert backend.calls.index('verify') > backend.calls.index('install:app')


def test_review_only_apps_and_drivers_still_start_with_audit_without_any_install_consent(tmp_path):
    class ReviewBackend(FixtureBackend):
        def audit(self):
            with self.lock:
                self.shared['audit'] += 1
                first = self.shared['audit'] == 1
            if first:
                self.entered['audit'].set()
                assert self.discovery_release.wait(8)
            return {'System': {}, 'Startup': []}

        def verify_optimization(self, report, **kwargs):
            updates = [row for row in report['stages']
                       if row['title'] in (WINDOWS_SOFTWARE, WINDOWS_DRIVER_REVIEW)]
            assert len(updates) == 2
            assert all(row['status'] == 'completed' and row['result']['review_only'] for row in updates)
            return verification()

    backend, result, errors, cancel = ReviewBackend(tmp_path), [], [], threading.Event()
    settings = configuration(cleanup_enabled=False, software_update_policy='review', software_update_ids=[],
        software_update_agreements_accepted=False, driver_policy='review', driver_changes_approved=False,
        driver_update_agreements_accepted=False, driver_approved_updates=[])
    def run():
        try:
            result.append(Workflow(backend).run(settings, lambda _: None, cancel))
        except BaseException as error:
            errors.append(error)
    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    try:
        assert all(event.wait(5) for event in backend.entered.values())
        assert not backend.installer_started.is_set()
    finally:
        backend.discovery_release.set()
        thread.join(8)
        if thread.is_alive():
            cancel.set()
            thread.join(8)
    assert not thread.is_alive() and not errors
    assert result[0]['status'] == 'completed'
    assert not any(call.startswith(('install:', 'backup:')) for call in backend.calls)
    records = [json.loads(path.read_text(encoding='utf-8'))
               for path in (tmp_path / 'parallel-updates').glob('*.json')]
    assert len(records) == 2
    assert all(record['installer_prepared'] is False for record in records)


def queue(tmp_path, operations, *, cancel=None, backend=None):
    backend = backend or SimpleNamespace(data_dir=tmp_path, system='win32', bridge=SimpleNamespace(_process=None))
    return ParallelInstallerQueue(backend, 'a' * 32, operations, ProgressMux(lambda _: None), cancel_event=cancel)


def operation(started, mutations, *, result=None):
    def run(worker, item, prepared, before_install):
        started.set()
        state = {'candidate_count': 1, 'review_only': False, 'scan_complete': True}
        prepared(state)
        if not before_install(state):
            return {'items': [], 'failed_items': 0, 'cancelled': False,
                    'installer_started': False, 'outcomeKnown': True}
        item({'id': 'Fixture.App', 'current': 0, 'total': 1})
        mutations.append('mutation')
        return result or {'items': [], 'failed_items': 0, 'installer_started': True, 'outcomeKnown': True}
    return run


def test_cancel_while_waiting_maintenance_never_records_installer_intent(tmp_path):
    cancel, started, mutations = threading.Event(), threading.Event(), []
    worker = queue(tmp_path, [(WINDOWS_SOFTWARE, operation(started, mutations))], cancel=cancel)
    worker.start()
    try:
        assert started.wait(5)
        assert worker.wait_prepared() is None
        assert worker.jobs[0].record['installer_prepared'] is False
        cancel.set()
        stages = worker.join()
    finally:
        worker.cancel()
        worker.join()
    assert mutations == []
    assert worker.jobs[0].record['installer_prepared'] is False
    assert stages[0]['result']['installer_started'] is False
    assert stages[0]['result']['outcomeKnown'] is True


def test_cancel_while_second_channel_waits_for_installer_keeps_active_installer_owned(tmp_path):
    cancel, first, second = threading.Event(), threading.Event(), threading.Event()
    active, release, mutations = threading.Event(), threading.Event(), []
    def first_operation(worker, item, prepared, before_install):
        first.set()
        state = {'candidate_count': 1, 'review_only': False, 'scan_complete': True}
        prepared(state)
        assert before_install(state)
        item({'id': 'Fixture.App', 'current': 0, 'total': 1})
        active.set()
        assert release.wait(8)
        mutations.append('first')
        return {'items': [], 'failed_items': 0, 'installer_started': True, 'outcomeKnown': True}
    worker = queue(tmp_path, [(WINDOWS_SOFTWARE, first_operation),
                              (WINDOWS_DRIVERS, operation(second, mutations))], cancel=cancel)
    worker.start()
    try:
        assert first.wait(5) and second.wait(5)
        worker.wait_prepared()
        worker.release_maintenance()
        assert active.wait(5)
        cancel.set()
        assert worker.active_job is worker.jobs[0]
        assert worker.jobs[0].thread.is_alive()
        assert worker.jobs[1].record['installer_prepared'] is False
    finally:
        release.set()
        worker.cancel()
        worker.join()
    assert mutations == ['first']
    assert worker.jobs[1].record['installer_prepared'] is False


def test_failed_driver_discovery_does_not_block_known_independent_application_install(tmp_path):
    backend = SimpleNamespace(data_dir=tmp_path, system='win32', bridge=SimpleNamespace(_process=None),
        driver_updates_scan=lambda: {'resultCode': 4, 'updates': [], 'error': 'Fixture metadata failure.'})
    mutations, started = [], threading.Event()
    def failed_driver(worker, item, prepared, before_install):
        return run_drivers(worker, configuration(), item_callback=item,
                           prepared_callback=prepared, before_install=before_install)
    # Put the failed discovery first to prove the healthy channel need not wait
    # behind or inherit installer uncertainty from a metadata-only failure.
    worker = queue(tmp_path, [(WINDOWS_DRIVERS, failed_driver),
                              (WINDOWS_SOFTWARE, operation(started, mutations))], backend=backend)
    worker.start()
    try:
        assert started.wait(5)
        worker.wait_prepared()
        worker.release_maintenance()
        stages = worker.join()
    finally:
        worker.cancel()
        worker.join()
    assert stages[0]['result']['failed_items'] == 1
    assert worker.jobs[0].record['installer_prepared'] is False
    assert mutations == ['mutation']


def test_final_outcome_save_failure_closes_gate_before_next_installer(tmp_path, monkeypatch):
    mutations, first, second = [], threading.Event(), threading.Event()
    original = ParallelAppUpdates._write
    def fail_terminal(self):
        if self.title == WINDOWS_SOFTWARE and 'finished_at' in self.record and self.record['installer_prepared']:
            raise OSError('Fixture terminal receipt is not durable.')
        return original(self)
    monkeypatch.setattr(ParallelAppUpdates, '_write', fail_terminal)
    worker = queue(tmp_path, [(WINDOWS_SOFTWARE, operation(first, mutations)),
                              (WINDOWS_DRIVERS, operation(second, mutations))])
    worker.start()
    try:
        assert first.wait(5) and second.wait(5)
        assert worker.wait_prepared() is None
        worker.release_maintenance()
        stages = worker.join()
    finally:
        worker.cancel()
        worker.join()
    assert len(mutations) == 1
    assert stages[0]['result']['uncertain'] is True
    assert 'journal' in stages[0]['result']['stop_reason']
    assert stages[1]['result']['blocked_by_prior_installer'] is True
    assert worker.jobs[1].record['installer_prepared'] is False


def test_pending_native_discovery_defers_shared_temp_cleanup_and_all_installers(tmp_path, monkeypatch):
    bridge = PowerShellBridge(tmp_path / 'fixture-engine')
    with bridge._pending_lock:
        bridge._pending['fixture-read-only-scan'] = ('driver_updates_scan', 123456)
    # No bridge subprocess may start in this fixture.
    monkeypatch.setattr(PowerShellBridge, '_start', lambda _: pytest.fail('Native process is forbidden.'))
    backend = SimpleNamespace(data_dir=tmp_path, system='win32', bridge=bridge)
    first, second, mutations = threading.Event(), threading.Event(), []
    worker = queue(tmp_path, [(WINDOWS_SOFTWARE, operation(first, mutations)),
                              (WINDOWS_DRIVERS, operation(second, mutations))], backend=backend)
    # Keep the pending bridge as the injected worker instance, without constructing
    # or starting a real backend/engine or inspecting any host process.
    monkeypatch.setattr('optimus.parallel_updates.isolated_backend', lambda value, **_: value)
    worker.start()
    try:
        assert first.wait(5) and second.wait(5)
        reason = worker.wait_prepared()
        assert reason and 'still running' in reason
        worker.release_maintenance(reason)
        stages = worker.join()
    finally:
        worker.cancel()
        worker.join()
    assert not mutations
    assert all(stage['result'].get('blocked_by_prior_installer') for stage in stages)
    assert all(not job.record['installer_prepared'] for job in worker.jobs)
    assert bridge._process is None


@pytest.mark.parametrize('fault', ['unknown', 'restart'])
def test_first_installer_unknown_or_restart_blocks_already_prepared_other_channel(tmp_path, fault):
    mutations, first, second = [], threading.Event(), threading.Event()
    outcome = {'items': [], 'failed_items': 0, 'installer_started': True,
               'outcomeKnown': fault != 'unknown', 'restartNeeded': fault == 'restart'}
    worker = queue(tmp_path, [(WINDOWS_SOFTWARE, operation(first, mutations, result=outcome)),
                              (WINDOWS_DRIVERS, operation(second, mutations))])
    worker.start()
    try:
        assert first.wait(5) and second.wait(5)
        worker.wait_prepared()
        worker.release_maintenance()
        stages = worker.join()
    finally:
        worker.cancel()
        worker.join()
    assert len(mutations) == 1
    assert stages[1]['result'].get('blocked_by_prior_installer') is True
    assert worker.jobs[1].record['installer_prepared'] is False
