"""Workflow progress is opt-in without retrying mutation-shaped callbacks."""
import copy
import threading
from types import SimpleNamespace

import pytest

from optimus.backend import Backend
from optimus.workflow import DEFAULTS, Workflow
from test_workflow import MockBackend


def config(**changes):
    return dict(copy.deepcopy(DEFAULTS), browser_cache_enabled=False, **changes)


class ProgressBackend(MockBackend):
    def cleanup_scan(self, category_ids, age, *, progress_callback=None):
        self.calls.append(('scan-progress',))
        assert callable(progress_callback)
        progress_callback({'item_progress': {'key': 'cleanup:fixture', 'label': 'Cleanup preview',
            'phase': 'scan', 'percent': None}, 'description': 'Fixture discovery; total unknown.'})
        return {'files': [{'path': 'mock-selected'}], 'complete': True, 'total_bytes': 4}

    def cleanup_apply(self, plan, *, progress_callback=None):
        self.calls.append(('apply-progress',))
        progress_callback({'item_progress': {'key': 'cleanup:fixture', 'label': 'Cleanup file checks',
            'phase': 'cleanup', 'percent': 50}, 'description': '1/2 fixture files checked.'})
        return {'deleted_count': 1, 'deleted_bytes': 4, 'skipped_count': 0}

    def apply_system_privacy(self, settings, cancel_event=None, *, progress_callback=None):
        self.calls.append(('privacy-progress',))
        progress_callback({'item_progress': {'key': 'privacy:controls', 'label': 'Privacy controls (3/9 checked)',
            'phase': 'verify', 'percent': 100 / 3}, 'current': 3, 'total': 9,
            'description': '3/9 privacy policies checked; unavailable policies were not applied.'})
        return {'status': 'completed', 'failed_items': 0, 'changed_count': 0, 'not_applicable_count': 3}


def test_workflow_routes_explicit_cleanup_and_policy_progress_without_inventing_source_fraction(tmp_path):
    backend = ProgressBackend(tmp_path)
    events = []
    workflow = Workflow(backend)
    report = workflow.run(config(privacy_advertising_enabled=True), events.append)
    assert report['failed_stages'] == 0
    current = [e for e in events if e.get('item_progress')]
    assert [(e['stage'], e['item_progress']['phase']) for e in current] == [
        ('Clean temporary files', 'scan'), ('Clean temporary files', 'cleanup'),
        ('Apply saved privacy controls', 'verify')]
    assert [e['item_progress']['percent'] for e in current] == [None, 50, 100 / 3]
    assert current[0]['percentage'] == current[1]['percentage']
    assert current[1]['percentage'] != current[1]['item_progress']['percent']
    assert not workflow.active


def test_legacy_backend_signature_still_runs_once_without_progress_kwargs(tmp_path):
    backend = MockBackend(tmp_path)
    report = Workflow(backend).run(config(), lambda _: None)
    assert report['failed_stages'] == 0
    assert sum(x[0] == 'cleanup_apply' for x in backend.calls) == 1
    assert sum(x[0] == 'cleanup_scan' for x in backend.calls) == 1


def test_typeerror_after_operation_started_is_not_retried_without_callback(tmp_path):
    backend = ProgressBackend(tmp_path)
    def fail(plan, *, progress_callback=None):
        backend.calls.append(('mutation-attempt',))
        raise TypeError('Fixture error after entry')
    backend.cleanup_apply = fail
    report = Workflow(backend).run(config(), lambda _: None)
    assert sum(x[0] == 'mutation-attempt' for x in backend.calls) == 1
    assert next(s for s in report['stages'] if s['title'] == 'Clean temporary files')['status'] == 'failed'


def test_cancel_after_progress_preview_never_enters_apply(tmp_path):
    backend = ProgressBackend(tmp_path)
    cancel = threading.Event()
    scan = backend.cleanup_scan
    def cancelled_scan(*args, progress_callback=None):
        value = scan(*args, progress_callback=progress_callback)
        cancel.set()
        return value
    backend.cleanup_scan = cancelled_scan
    report = Workflow(backend).run(config(), lambda _: None, cancel)
    assert report['cancelled'] and not any(x[0] == 'apply-progress' for x in backend.calls)


def test_backend_wrappers_forward_the_same_callback_without_constructing_services():
    calls = []
    observer = lambda event: None
    def method(*args, **kwargs):
        calls.append((args, kwargs))
        return {'fixture': True}
    fixture = SimpleNamespace(cleaner=SimpleNamespace(scan=method, apply=method),
        _privacy_service=lambda: SimpleNamespace(apply_privacy=method))
    Backend.cleanup_scan(fixture, ['fixture-cache'], 3, progress_callback=observer)
    Backend.cleanup_apply(fixture, {'id': 'fixture'}, progress_callback=observer)
    Backend.apply_system_privacy(fixture, {}, progress_callback=observer)
    assert len(calls) == 3 and all(options['progress_callback'] is observer for _, options in calls)


def test_workflow_routes_exact_debloat_item_progress_and_does_not_retry_errors(tmp_path):
    backend = MockBackend(tmp_path)
    calls, events = [], []
    def apply(plan, *, progress_callback=None):
        calls.append(plan)
        progress_callback({'current': 0, 'total': 2, 'item': 1, 'id': 'mock-target',
            'item_progress': {'key': 'debloat:consumer-apps:mock-target', 'label': 'Fixture app',
                              'phase': 'uninstall', 'percent': 37}, 'description': 'Uninstall: Fixture app'})
        raise TypeError('Fixture error after removal entry')
    backend.debloat_apply = apply
    report = Workflow(backend).run(config(cleanup_enabled=False, consumer_debloat_enabled=True), events.append)
    assert len(calls) == 1
    item = next(event for event in events if (event.get('item_progress') or {}).get('phase') == 'uninstall')
    assert item['item_progress']['percent'] == 37
    assert item['stage'] == 'Remove selected consumer app group'
    assert item['percentage'] != item['item_progress']['percent']
    assert next(stage for stage in report['stages'] if stage['title'] == item['stage'])['status'] == 'failed'
