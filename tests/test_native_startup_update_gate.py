"""Serialized startup/STOP races with fake network, installation and cleanup."""
import copy
import threading
from unittest.mock import Mock

import pytest
from PySide6.QtTest import QTest

from optimus.ui import OptimusWindow
from test_native_self_update_ui import owner, controller, updater, wait_for


@pytest.mark.parametrize('phase', ['stage', 'prepare_handoff', 'dispatch_worker'])
def test_accepted_update_failure_never_invokes_real_unattended_launch_review(owner, monkeypatch, phase):
    app, window, backend, settings = owner
    saved = copy.deepcopy(settings)
    monkeypatch.setattr('optimus.unattended_configuration.matches_unattended', lambda current: True)
    window.review_launch = lambda: OptimusWindow.review_launch(window)
    service = updater()
    getattr(service, phase).side_effect = RuntimeError('Fixture accepted update failure')
    value, _ = controller(owner, service, ask=lambda *_: 'yes_update')
    value.start()
    wait_for(app, lambda: not value.pending)
    QTest.qWait(30)
    app.processEvents()
    window.start_optimization.assert_not_called()
    window._session_approved_callback.assert_not_called()
    assert not window.session_approved() and settings == saved
    assert value._progress.completed and not value._progress.isVisible()


def test_delayed_check_blocks_cleanup_until_one_decline_callback(owner):
    app, window, backend, settings = owner
    active, release = threading.Event(), threading.Event()
    def check(**kwargs):
        active.set()
        assert release.wait(5)
        return {'status': 'available', 'automatic_install_supported': True, 'offer': {'version': '0.99.0'}}
    value, service = controller(owner, updater(check=Mock(side_effect=check)), ask=lambda *_: 'no_update')
    value._fixture_release = [release]
    value.start()
    value.start()  # The same GUI cannot begin another check/prompt.
    wait_for(app, active.is_set)
    assert value.pending and window._startup_update_pending
    assert not window.session_approved()
    window.review_launch.assert_not_called()
    window.start_optimization.assert_not_called()
    release.set()
    wait_for(app, lambda: window.review_launch.called)
    service.check.assert_called_once()
    window.review_launch.assert_called_once()


def test_stop_invalidates_already_queued_no_update_continuation(owner):
    app, window, backend, settings = owner
    value, service = controller(owner)
    value._fallback('No update is needed.')  # Queued on the GUI thread, not executed yet.
    window._emergency_stopping = True
    value.request_safe_stop()
    app.processEvents()
    window.review_launch.assert_not_called()
    window.start_optimization.assert_not_called()
    assert value.cancel.is_set()


def test_readiness_ack_that_finishes_after_stop_cannot_start_cleanup(owner):
    app, window, backend, settings = owner
    active, release = threading.Event(), threading.Event()
    def acknowledge(identity):
        active.set()
        assert release.wait(5)
        return {'status': 'ready'}
    service = updater(consume_resume=Mock(return_value={'status': 'accepted', 'cleanup_approved': True,
        'settings': copy.deepcopy(settings)}), acknowledge_resume=Mock(side_effect=acknowledge))
    value, _ = controller(owner, service, resume_id='b' * 32)
    value._fixture_release = [release]
    value.start()
    wait_for(app, active.is_set)
    window._emergency_stopping = True
    value.request_safe_stop()
    release.set()
    wait_for(app, lambda: not value._jobs)
    window.start_optimization.assert_not_called()
    window._session_approved_callback.assert_not_called()
    assert not window.session_approved()


def test_verified_resume_result_cannot_replay_cleanup_in_same_gui(owner):
    app, window, backend, settings = owner
    accepted = {'status': 'accepted', 'cleanup_approved': True, 'settings': copy.deepcopy(settings)}
    value, service = controller(owner, updater(consume_resume=Mock(return_value=accepted)), resume_id='b' * 32)
    value.start()
    wait_for(app, lambda: window.start_optimization.called)
    value._resume_acknowledged(accepted, {'status': 'ready'})
    value._complete_resume(accepted)
    value.start()
    window.start_optimization.assert_called_once()
    service.consume_resume.assert_called_once()
    service.acknowledge_resume.assert_called_once()


def test_emergency_native_drain_cannot_clear_close_latch(owner):
    app, window, backend, settings = owner
    value, _ = controller(owner)
    window._emergency_stopping = True
    window._close_when_idle = True
    window.quit_application.return_value = False
    value._closing = True
    value._exit_after_stop()
    assert value._closing and window._close_when_idle
    window.review_launch.assert_not_called()
