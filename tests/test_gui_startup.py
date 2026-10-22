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
    controller, backend, window = fixture(tmp_path, {'setup_version': startup.CURRENT_SETUP_VERSION, 'automation_mode': 'startup'})
    setattr(window, child, SimpleNamespace(busy=False, isVisible=lambda: True))
    controller.tick()
    backend.preferences.assert_not_called()
    window.start_optimization.assert_not_called()


def test_modal_confirmation_prevents_automatic_work(tmp_path):
    controller, _, window = fixture(tmp_path, {'setup_version': startup.CURRENT_SETUP_VERSION, 'automation_mode': 'startup'})
    controller.modal_active = lambda: True
    controller.tick()
    window.start_optimization.assert_not_called()


def test_due_selection_uses_existing_progress_workflow(tmp_path, monkeypatch):
    controller, backend, window = fixture(tmp_path, {'setup_version': startup.CURRENT_SETUP_VERSION, 'automation_mode': 'daily'})
    monkeypatch.setattr(startup, 'load_attempt', Mock(return_value={'attempted_at': 1}))
    check = Mock(return_value=True)
    monkeypatch.setattr(startup, 'due', check)
    controller.tick()
    check.assert_called_once_with(backend.preferences.return_value, {'attempted_at': 1}, now=1000)
    window.start_optimization.assert_called_once_with()


def test_manual_choice_does_not_start_automatic_work(tmp_path):
    controller, _, window = fixture(tmp_path, {'setup_version': startup.CURRENT_SETUP_VERSION, 'automation_mode': 'manual'})
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
    controller, _, window = fixture(tmp_path, {'setup_version': startup.CURRENT_SETUP_VERSION, 'automation_mode': 'startup'})
    monkeypatch.setattr(startup, 'load_attempt', Mock(side_effect=OSError('fixture denied')))
    controller.tick()
    window.status.setText.assert_called_once()
    assert 'fixture denied' in window.status.setText.call_args.args[0]
    window.start_optimization.assert_not_called()


def test_unverifiable_cadence_state_pauses_and_explains(tmp_path, monkeypatch):
    controller, _, window = fixture(tmp_path, {'setup_version': startup.CURRENT_SETUP_VERSION, 'automation_mode': 'daily'})
    monkeypatch.setattr(startup, 'load_attempt', lambda _: {'status': 'invalid', 'error': 'Fixture state is unreadable; paused.'})
    controller.tick()
    window.status.setText.assert_called_once_with('Fixture state is unreadable; paused.')
    window.start_optimization.assert_not_called()


def test_previous_setup_version_must_approve_new_actions_before_automatic_runs(tmp_path):
    controller, _, window = fixture(tmp_path, {'setup_version': 1, 'automation_mode': 'startup'})
    controller.tick()
    window.open_initial_setup.assert_called_once_with(first_run=True)
    window.start_optimization.assert_not_called()


def test_two_app_sessions_cannot_rearm_each_other_through_shared_state(tmp_path, monkeypatch):
    from optimus import automation
    settings = {'setup_version':startup.CURRENT_SETUP_VERSION,'automation_mode':'startup'}
    sessions = ['a'*32,'b'*32]
    instances = []
    def complete(backend):
        with automation.RunLease(tmp_path):
            started = automation.record_attempt(tmp_path, settings, now=1000)
            finished = automation.record_attempt(tmp_path, settings, status='completed', now=1001,
                                                 run_id=started['run_id'])
        backend.workflow.last_report = {'id':started['run_id'],'status':'completed'}
        return finished
    for session in sessions:
        with monkeypatch.context() as context:
            context.setattr(automation, 'SESSION_ID', session)
            controller, backend, window = fixture(tmp_path, settings)
            window.start_optimization.side_effect = lambda service=backend: complete(service)
            instances.append((session,controller,window))
    for _ in range(5):
        for session, controller, window in instances:
            with monkeypatch.context() as context:
                context.setattr(automation, 'SESSION_ID', session)
                controller.tick()
    assert automation.load_attempt(tmp_path)['session_id'] == sessions[1]
    assert [window.start_optimization.call_count for _,_,window in instances] == [1,1]
    # A genuinely new app session keeps its own one-startup-run entitlement.
    with monkeypatch.context() as context:
        context.setattr(automation, 'SESSION_ID', 'c'*32)
        new_controller, new_backend, new_window = fixture(tmp_path, settings)
        new_window.start_optimization.side_effect = lambda: complete(new_backend)
        new_controller.tick()
        new_controller.tick()
        new_window.start_optimization.assert_called_once_with()


@pytest.mark.parametrize('status', ['configured','completed','failed','cancelled'])
def test_observed_own_attempt_stays_consumed_after_other_session_overwrites_it(tmp_path, monkeypatch, status):
    from optimus import automation
    settings = {'setup_version':startup.CURRENT_SETUP_VERSION,'automation_mode':'startup'}
    controller, _, window = fixture(tmp_path, settings)
    own = {'schema':1,'attempted_at':10,'session_id':controller._session_id,
           'mode':'startup','status':status,'run_id':None if status=='configured' else '1'*32}
    if status != 'configured':
        own['finished_at'] = 11
    states = Mock(side_effect=[own,dict(own, session_id='b'*32)])
    monkeypatch.setattr(startup, 'load_attempt', states)
    controller.tick()
    controller.tick()
    window.start_optimization.assert_not_called()
    assert states.call_count == 1


def test_manual_attempt_in_this_window_counts_even_if_shared_state_was_overwritten(tmp_path, monkeypatch):
    controller, backend, window = fixture(tmp_path, {'setup_version':startup.CURRENT_SETUP_VERSION,'automation_mode':'startup'})
    backend.workflow.last_report = {'status':'failed','error':'Fixture lease was unavailable.'}
    monkeypatch.setattr(startup, 'load_attempt', lambda _: None)
    controller.tick()
    window.start_optimization.assert_not_called()


def test_startup_dispatch_failure_does_not_repeat_but_explicit_retry_remains_available(tmp_path, monkeypatch):
    controller, _, window = fixture(tmp_path, {'setup_version':startup.CURRENT_SETUP_VERSION,'automation_mode':'startup'})
    monkeypatch.setattr(startup, 'load_attempt', lambda _: None)
    window.start_optimization.side_effect = RuntimeError('Fixture launch failed')
    controller.tick()
    controller.tick()
    window.start_optimization.assert_called_once_with()
    assert 'Fixture launch failed' in window.status.setText.call_args.args[0]
    window.start_optimization.side_effect = None
    window.start_optimization()
    assert window.start_optimization.call_count == 2


@pytest.mark.parametrize('mode', ['daily','weekly'])
def test_startup_memory_does_not_change_elapsed_daily_or_weekly_policy(tmp_path, monkeypatch, mode):
    settings = {'setup_version':startup.CURRENT_SETUP_VERSION,'automation_mode':mode}
    controller, _, window = fixture(tmp_path, settings)
    controller._startup_attempted = True
    previous = {'attempted_at':1}
    monkeypatch.setattr(startup, 'load_attempt', lambda _: previous)
    check = Mock(side_effect=[False,True])
    monkeypatch.setattr(startup, 'due', check)
    controller.tick()
    window.start_optimization.assert_not_called()
    controller.tick()
    window.start_optimization.assert_called_once_with()
    assert check.call_count == 2


def test_startup_memory_does_not_enable_manual_mode(tmp_path):
    controller, _, window = fixture(tmp_path, {'setup_version':startup.CURRENT_SETUP_VERSION,'automation_mode':'manual'})
    controller._startup_attempted = True
    controller.tick()
    window.start_optimization.assert_not_called()
