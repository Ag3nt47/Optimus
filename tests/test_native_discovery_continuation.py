"""Independent maintenance uses slow catalog time without touching installer staging."""
import threading
from unittest.mock import Mock

import pytest

from optimus.automation import RunLease
from test_native_parallel_updates import Backend, settings, start_workflow


@pytest.mark.parametrize('native', [False, True])
@pytest.mark.parametrize('cancel', [False, True])
def test_slow_discovery_runs_independent_work_then_cleanup_and_updates_in_same_session(
        tmp_path, monkeypatch, native, cancel):
    monkeypatch.setattr('optimus.speed_enhancements._current_owner', lambda: 'fixture-owner')
    monkeypatch.setattr('optimus.runtime_priority.lower_worker_priority', lambda: lambda: None)
    service = Backend(tmp_path, native=native)
    service.release.set()
    scanning, scanned, independent = (threading.Event() for _ in range(3))
    cancel_event = threading.Event()
    original_scan = service.updates_scan
    def delayed_scan():
        scanning.set()
        assert scanned.wait(15)
        return original_scan()
    service.updates_scan = delayed_scan
    def review():
        assert scanning.wait(15)
        assert service.audit_count == 1
        service.calls.append('independent')
        independent.set()
        return []
    service.installed_apps = review
    service.cleanup_categories = Mock(return_value=[{'id': 'fixture-temp', 'available': True}])
    def preview(*args):
        assert scanned.is_set() and not service.started.is_set()
        service.calls.append('cleanup_scan')
        return {'files': [{'fixture': True}], 'complete': True, 'total_bytes': 1, 'errors': []}
    def clean(*args):
        assert scanned.is_set() and not service.started.is_set()
        service.calls.append('cleanup_apply')
        return {'deleted_count': 1, 'deleted_bytes': 1, 'skipped_count': 0, 'errors': []}
    service.cleanup_scan = Mock(side_effect=preview)
    service.cleanup_apply = Mock(side_effect=clean)
    workflow, thread, results, errors = start_workflow(service, settings(
        applications_review_enabled=True, cleanup_enabled=True,
        cleanup_category_ids=['fixture-temp']), native=native, cancel=cancel_event)
    try:
        assert independent.wait(15), 'Foreground maintenance did not use the slow discovery interval.'
        assert thread.is_alive() and not scanned.is_set()
        service.cleanup_scan.assert_not_called()
        service.cleanup_apply.assert_not_called()
        assert not service.started.is_set()
        with pytest.raises(RuntimeError):
            RunLease(tmp_path).acquire()
        if cancel:
            cancel_event.set()
    finally:
        scanned.set()
        thread.join(20)
    assert not thread.is_alive() and not errors
    assert bool(results[0]['cancelled']) is cancel
    if cancel:
        service.cleanup_scan.assert_not_called()
        service.cleanup_apply.assert_not_called()
        assert not service.started.is_set()
    else:
        # Windows has user-temp plus the separately selected category stage.
        assert service.cleanup_scan.call_count == (1 if native else 2)
        assert service.cleanup_apply.call_count == (1 if native else 2)
        assert service.calls.index('audit') < service.calls.index('independent') < service.calls.index('cleanup_scan')
        assert service.calls.index('cleanup_apply') < service.calls.index('update:Fixture.One') < service.calls.index('update:Fixture.Two')
        assert service.calls[-1] == 'verify'
        assert results[0]['status'] == 'completed'
    with RunLease(tmp_path):
        pass
