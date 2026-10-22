"""Heavy Qt workers restore their thread priority before becoming reusable."""
import threading
from types import SimpleNamespace

import pytest
from PySide6.QtWidgets import QPushButton

from optimus import runtime_priority
from optimus.onboarding import InventoryProbe
from optimus.ui import AvailableFixesWorker, BackendWorker, WorkflowWorker
from optimus.widgets.speed_enhancements_panel import _Worker
from test_native_speed_enhancements_ui import app, wait_until, window


def make_worker(kind, operation):
    if kind == 'backend':
        return BackendWorker(operation, (), {})
    if kind == 'inventory':
        return InventoryProbe(operation, (), {})
    if kind == 'speed':
        return _Worker(operation, threading.Event())
    if kind == 'workflow':
        return WorkflowWorker(SimpleNamespace(optimize=operation),
            {'browser_cache_enabled': False}, threading.Event())
    return AvailableFixesWorker(SimpleNamespace(apply_available_fixes=operation), {}, {}, threading.Event())


@pytest.mark.parametrize('kind', ['backend', 'inventory', 'speed', 'workflow', 'fixes'])
@pytest.mark.parametrize('failed', [False, True])
def test_worker_priority_is_restored_on_success_and_error_before_finished(app, monkeypatch, kind, failed):
    observed = []
    def lower():
        observed.append('worker priority')
        return lambda: observed.append('restore priority')
    def operation(*args, **kwargs):
        observed.append('operation')
        if failed:
            raise RuntimeError('Fixture operation failed')
        return {'fixture': True}
    monkeypatch.setattr(runtime_priority, 'lower_worker_priority', lower)
    worker = make_worker(kind, operation)
    results, errors = [], []
    worker.signals.result.connect(results.append)
    worker.signals.error.connect(errors.append)
    worker.signals.finished.connect(lambda: observed.append('finished'))
    worker.run()
    assert observed == ['worker priority', 'operation', 'restore priority', 'finished']
    assert results == ([] if failed else [{'fixture': True}])
    assert errors == (['Fixture operation failed'] if failed else [])


def test_interface_priority_choice_is_explicit_and_platform_accurate(window, app, monkeypatch):
    settings = window._settings_window
    applied = []
    monkeypatch.setattr(runtime_priority.InterfacePriority, 'apply', lambda *args: applied.append(args))
    if hasattr(settings, 'workflow_controls'):
        control = settings.workflow_controls['interface_priority_boost_enabled']
        assert control.isEnabled() and 'Above Normal' in control.text()
        assert 'next time Optimus opens' in control.text()
        control.setChecked(False)
        button = next(item for item in settings.findChildren(QPushButton)
                      if item.text() == 'Save one-click configuration')
        button.click()
        wait_until(app, lambda: not settings.busy)
        assert window.backend.saved[-1]['interface_priority_boost_enabled'] is False
    else:
        assert not settings.interface_priority_choice.isEnabled()
        assert not settings.interface_priority_choice.isChecked()
        assert 'Windows' in settings.interface_priority_choice.text()
        assert window.backend.saved == []
    assert applied == []  # Saving the preference takes effect next launch.

