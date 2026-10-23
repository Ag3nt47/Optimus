"""An existing installer barrier skips cleanup without delaying safe reviews."""
import json
import threading
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from optimus.automation import RunLease
from optimus.bridge import PowerShellBridge
from optimus.parallel_updates import ParallelInstallerQueue
from test_native_parallel_updates import (Backend, driver_settings, enable_fixture_driver,
    settings, start_workflow)


@pytest.fixture(autouse=True)
def fixture_owner(monkeypatch):
    monkeypatch.setattr('optimus.speed_enhancements._current_owner', lambda: 'fixture-owner')
    monkeypatch.setattr('optimus.runtime_priority.lower_worker_priority', lambda: lambda: None)


def prior_receipt(tmp_path):
    directory = tmp_path / 'parallel-updates'
    directory.mkdir()
    identity = '1' * 32
    path = directory / (identity + '.json')
    raw = json.dumps({'schema': 1, 'id': identity, 'owner': 'fixture-owner',
        'status': 'uncertain', 'installer_prepared': True,
        'result': {'installer_started': True, 'outcomeKnown': False, 'running': True}},
        indent=2).encode('utf-8')
    path.write_bytes(raw)
    return path, raw


@pytest.mark.parametrize('native', [False, True])
@pytest.mark.parametrize('cancel', [False, True])
def test_prior_installer_allows_independent_review_before_slow_discovery_but_never_cleanup_or_install(
        tmp_path, monkeypatch, native, cancel):
    path, original = prior_receipt(tmp_path)
    service = Backend(tmp_path, native=native)
    service.release.set()
    discovery_started, discovery_release, independent = (threading.Event() for _ in range(3))
    cancel_event = threading.Event()
    forbidden = lambda: Mock(side_effect=AssertionError('An unresolved installer forbids this mutation.'))
    service.cleanup_categories = Mock(return_value=[{'id': 'fixture-temp', 'available': True}])
    service.cleanup_scan, service.cleanup_apply = forbidden(), forbidden()
    service.update_app_unattended = forbidden()
    browser = forbidden()
    monkeypatch.setattr('optimus.browser_cleanup.run_browser_cleanup', browser)
    if native:
        original_scan = service.updates_scan
    else:
        enable_fixture_driver(service, forbidden())
        original_scan = service.driver_updates_scan
    def delayed_scan():
        discovery_started.set()
        assert discovery_release.wait(15), 'The fixture did not release its read-only discovery.'
        return original_scan()
    if native:
        service.updates_scan = delayed_scan
    else:
        service.driver_updates_scan = delayed_scan
    def review():
        assert discovery_started.wait(15)
        service.calls.append('independent')
        independent.set()
        return []
    service.installed_apps = review
    configuration = (settings if native else driver_settings)(applications_review_enabled=True,
        cleanup_enabled=True, cleanup_category_ids=['fixture-temp'], browser_cache_enabled=True)
    workflow, thread, result, errors = start_workflow(service, configuration,
        native=native, cancel=cancel_event)
    try:
        assert independent.wait(15), 'Independent review was delayed by discovery despite forbidden cleanup.'
        assert not discovery_release.is_set() and thread.is_alive()
        assert service.audit_count == 1
        cleanup = [row for row in workflow.last_report['stages'] if row['title'].startswith('Clean ')]
        assert cleanup and all(row['result']['skipped'] is True for row in cleanup)
        assert all(row['result']['stop_reason'] for row in cleanup)
        service.cleanup_scan.assert_not_called()
        service.cleanup_apply.assert_not_called()
        browser.assert_not_called()
        service.update_app_unattended.assert_not_called()
        if not native:
            service.driver_update_apply_unattended.assert_not_called()
            service.driver_backup_automatic.assert_not_called()
        assert path.read_bytes() == original
        if cancel:
            cancel_event.set()
        # Cancellation is cooperative: discovery still owns its worker and the
        # workflow lease until the native/read-only request actually returns.
        with pytest.raises(RuntimeError):
            RunLease(tmp_path).acquire()
    finally:
        discovery_release.set()
        thread.join(20)
    assert not errors and not thread.is_alive()
    assert bool(result[0]['cancelled']) is cancel
    assert path.read_bytes() == original
    service.cleanup_scan.assert_not_called()
    service.cleanup_apply.assert_not_called()
    service.update_app_unattended.assert_not_called()
    if not native:
        service.driver_update_apply_unattended.assert_not_called()
    with RunLease(tmp_path):
        pass


def test_without_prior_barrier_discovery_still_must_finish_before_cleanup():
    queue = object.__new__(ParallelInstallerQueue)
    queue.prior_barrier = None
    ready = threading.Event()
    preparing = threading.Event()
    queue.jobs = [SimpleNamespace(prepared=ready, preparation_bridge=None)]
    queue.mux = SimpleNamespace(wait_for_preparation=Mock(side_effect=preparing.set), heartbeat=Mock())
    done = threading.Event()
    result = []
    def wait():
        result.append(queue.wait_prepared())
        done.set()
    worker = threading.Thread(target=wait)
    worker.start()
    try:
        assert preparing.wait(5)
        assert not done.wait(.05)
        queue.mux.wait_for_preparation.assert_called_once()
    finally:
        ready.set()
        worker.join(5)
    assert not worker.is_alive() and result == [None]


def test_without_prior_barrier_pending_native_discovery_still_blocks_cleanup(tmp_path):
    queue = object.__new__(ParallelInstallerQueue)
    queue.prior_barrier = None
    ready = threading.Event()
    ready.set()
    bridge = PowerShellBridge(tmp_path / 'fixture-engine')
    assert bridge._process is None
    bridge._pending['fixture-request'] = {'read_only': True}
    queue.jobs = [SimpleNamespace(prepared=ready, preparation_bridge=bridge)]
    queue.mux = SimpleNamespace(wait_for_preparation=Mock(), heartbeat=Mock())
    reason = queue.wait_prepared()
    assert 'discovery is still running' in reason and 'cleanup and installers were deferred' in reason
    assert bridge._process is None and 'fixture-request' in bridge._pending
