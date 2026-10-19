"""GUI initialization and cadence tests never change host settings or run tools."""
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
import optimus.gui_startup as startup


def fixture(tmp_path, settings):
    backend = SimpleNamespace(data_dir=tmp_path, preferences=Mock(return_value=settings), workflow=SimpleNamespace(active=False))
    window = SimpleNamespace(smoke_test=False, _quitting=False, busy=False, status=SimpleNamespace(setText=Mock()),
                             open_initial_setup=Mock(), start_optimization=Mock())
    controller = startup.SetupAutomationController(backend, window, clock=lambda: 1000)
    return controller, backend, window


def test_first_open_asks_once_without_starting_legacy_high_actions(tmp_path):
    controller, _, window = fixture(tmp_path, {'setup_version': 0, 'intensity': 'High', 'edge_debloat_enabled': True})
    controller.tick()
    controller.tick()
    window.open_initial_setup.assert_called_once_with(first_run=True)
    window.start_optimization.assert_not_called()


def test_cancelled_setup_can_be_reopened_manually_but_is_not_repeated_by_timer(tmp_path):
    controller, _, window = fixture(tmp_path, {})
    controller.tick()
    for _ in range(3):
        controller.tick()
    assert window.open_initial_setup.call_count == 1
    window.start_optimization.assert_not_called()


@pytest.mark.parametrize('child', ['_setup_wizard', '_settings_window', '_sandbox_window', '_progress_window'])
def test_open_review_or_editor_defers_automatic_work(tmp_path, child):
    controller, backend, window = fixture(tmp_path, {'setup_version': 1, 'automation_mode': 'startup'})
    setattr(window, child, SimpleNamespace(busy=False, isVisible=lambda: True))
    controller.tick()
    backend.preferences.assert_not_called()
    window.start_optimization.assert_not_called()


def test_modal_confirmation_prevents_automatic_work(tmp_path):
    controller, _, window = fixture(tmp_path, {'setup_version': 1, 'automation_mode': 'startup'})
    controller.modal_active = lambda: True
    controller.tick()
    window.start_optimization.assert_not_called()


def test_due_selection_uses_existing_progress_workflow(tmp_path, monkeypatch):
    controller, backend, window = fixture(tmp_path, {'setup_version': 1, 'automation_mode': 'daily'})
    monkeypatch.setattr(startup, 'load_attempt', Mock(return_value={'attempted_at': 1}))
    check = Mock(return_value=True)
    monkeypatch.setattr(startup, 'due', check)
    controller.tick()
    check.assert_called_once_with(backend.preferences.return_value, {'attempted_at': 1}, now=1000)
    window.start_optimization.assert_called_once_with()


def test_manual_choice_does_not_start_automatic_work(tmp_path):
    controller, _, window = fixture(tmp_path, {'setup_version': 1, 'automation_mode': 'manual'})
    controller.tick()
    window.start_optimization.assert_not_called()


def test_stopped_smoke_and_running_workflows_do_not_initialize(tmp_path):
    for kind in ('stopped', 'smoke', 'busy', 'backend-active'):
        controller, backend, window = fixture(tmp_path, {})
        if kind == 'stopped':
            controller.stop()
        elif kind == 'smoke':
            window.smoke_test = True
        elif kind == 'busy':
            window.busy = True
        else:
            backend.workflow.active = True
        controller.tick()
        window.open_initial_setup.assert_not_called()
        window.start_optimization.assert_not_called()


def test_cadence_read_failure_is_visible_and_does_not_apply(tmp_path, monkeypatch):
    controller, _, window = fixture(tmp_path, {'setup_version': 1, 'automation_mode': 'startup'})
    monkeypatch.setattr(startup, 'load_attempt', Mock(side_effect=OSError('fixture denied')))
    controller.tick()
    window.status.setText.assert_called_once()
    assert 'fixture denied' in window.status.setText.call_args.args[0]
    window.start_optimization.assert_not_called()


def test_unverifiable_cadence_state_pauses_and_explains(tmp_path, monkeypatch):
    controller, _, window = fixture(tmp_path, {'setup_version': 1, 'automation_mode': 'daily'})
    monkeypatch.setattr(startup, 'load_attempt', lambda _: {'status': 'invalid', 'error': 'Fixture state is unreadable; paused.'})
    controller.tick()
    window.status.setText.assert_called_once_with('Fixture state is unreadable; paused.')
    window.start_optimization.assert_not_called()
