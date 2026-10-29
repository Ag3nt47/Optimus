"""Independent STOP race/geometry checks using inert worker and native states."""
from __future__ import annotations

import copy
import os
import threading
import time
from types import SimpleNamespace
from unittest.mock import Mock

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

import pytest
from PySide6.QtCore import QPoint, QRect, QTimer
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QDialog

from optimus.automation import CURRENT_SETUP_VERSION
from optimus.launch_review import LaunchConsent
from optimus.native_ui import NativeOptimusWindow
from optimus.ui import OptimusWindow, OptimizationProgressWindow
from optimus.workflow import DEFAULTS


def wait_until(app, predicate):
    deadline = time.monotonic() + 5
    while not predicate() and time.monotonic() < deadline:
        app.processEvents()
        QTest.qWait(5)
    app.processEvents()
    assert predicate()


class NativeState:
    """A native observer, not a process to terminate or a fake success result."""
    def __init__(self):
        self.native_pending = 0
        self.request_count = 0
        self.events = []

    def bind_cancel(self, event):
        self.events.append(event)

    def request_stop(self):
        self.request_count += 1
        for event in self.events:
            event.set()

    def snapshot(self):
        return {'native_pending': self.native_pending,
                'drained': self.request_count > 0 and self.native_pending == 0}


@pytest.fixture(params=[OptimusWindow, NativeOptimusWindow], ids=['windows', 'native'])
def owner(request, tmp_path, monkeypatch):
    from optimus import emergency_stop, live_metrics
    app = QApplication.instance() or QApplication([])
    monkeypatch.setattr(live_metrics, 'LiveMetrics', lambda *_args, **_kwargs: None)
    settings = dict(copy.deepcopy(DEFAULTS), setup_version=CURRENT_SETUP_VERSION,
        browser_cache_enabled=False, browser_history_enabled=False,
        browser_cookies_enabled=False, browser_download_history_enabled=False,
        auto_continue_fixes_enabled=True)
    backend = SimpleNamespace(data_dir=tmp_path, preferences=lambda: copy.deepcopy(settings),
        optimize=Mock(), available_fixes=Mock(return_value={'actions': [{'id': 'never-after-stop'}]}),
        apply_available_fixes=Mock(), request_optional_stop=Mock())
    value = request.param(backend, smoke_test=True)
    value.smoke_test = False
    value._launch_consent = LaunchConsent(required=True)
    value._launch_consent.approve(settings)
    value._automation_controller = SimpleNamespace(stop=Mock())
    value._automation_timer = SimpleNamespace(stop=Mock())
    value._critical_quit = Mock()
    value._critical_releases = []
    monkeypatch.setattr(app, 'quit', value._critical_quit)
    state = NativeState()
    monkeypatch.setattr(emergency_stop, 'controller_for', lambda _backend: state)
    value.show()
    app.processEvents()
    yield value, app, state, settings
    for event in value._critical_releases:
        event.set()
    value._cancel.set()
    value.pool.waitForDone(5000)
    app.processEvents()
    state.native_pending = 0
    value._quitting = True
    value._settings_window = value._setup_wizard = value._sandbox_window = None
    value._self_update_controller = value._security_review_dialog = value._revert_dialog = None
    if value._progress_window is not None:
        value._progress_window.finish({'status': 'cancelled', 'cancelled': True, 'stages': []})
        value._progress_window.close()
    value.shutdown()
    value.close()
    value.deleteLater()
    app.processEvents()


def test_stop_latches_before_queued_startup_review_and_is_idempotent(owner):
    window, app, state, _ = owner
    window._startup_update_pending = True
    window._self_update_controller = None  # Earliest visible window, before controller creation.
    ask = Mock(side_effect=AssertionError('STOP cannot open a queued launch prompt.'))
    window._ask_launch_review = ask
    QTimer.singleShot(0, window.review_launch)
    assert window.request_emergency_stop()
    assert window._emergency_stopping and window._close_when_idle and window._cancel.is_set()
    assert all(not button.isEnabled() for button in window.main_buttons)
    assert not window.stop_button.isEnabled()
    assert not window.request_emergency_stop()
    assert state.request_count == 1
    window.start_optimization()
    wait_until(app, lambda: window._critical_quit.called)
    ask.assert_not_called()
    window.backend.optimize.assert_not_called()
    window.backend.available_fixes.assert_not_called()
    window._critical_quit.assert_called_once_with()


def test_idle_window_and_alternate_quit_paths_wait_for_native_observer(owner):
    window, app, state, _ = owner
    state.native_pending = 1
    assert window.request_emergency_stop()
    for _ in range(3):
        assert window.quit_application() is False
        app.processEvents()
    assert not window._quitting
    window._critical_quit.assert_not_called()
    window._finish_emergency_stop()
    assert 'finishing safely' in window.status.text()
    state.native_pending = 0
    wait_until(app, lambda: window._critical_quit.called)
    window._critical_quit.assert_called_once_with()


def test_stop_rejects_questions_and_drains_blocked_settings_without_new_actions(owner):
    window, app, state, _ = owner
    wizard, launch = QDialog(window), QDialog(window)
    wizard.show()
    launch.show()
    window._setup_wizard, window._launch_review_dialog = wizard, launch
    window._post_review_active = True
    cancel = threading.Event()
    settings = SimpleNamespace(busy=True, shutdown=Mock(), recovery_panel=SimpleNamespace(_cancel=cancel))
    window._settings_window = settings
    assert window.request_emergency_stop()
    assert cancel.is_set()
    assert not wizard.isVisible() and not launch.isVisible()
    assert window._post_review_active is False
    window.open_settings()
    window.open_initial_setup()
    app.processEvents()
    window._critical_quit.assert_not_called()
    window.backend.optimize.assert_not_called()
    # A real modal exec() clears these fields in its finally block.
    window._setup_wizard = window._launch_review_dialog = None
    settings.busy = False
    wait_until(app, lambda: window._critical_quit.called)
    settings.shutdown.assert_called_once_with()


def test_stop_waits_for_update_controller_and_does_not_continue_launch(owner):
    window, app, state, _ = owner
    controller = SimpleNamespace(pending=True, request_safe_stop=Mock(return_value=True), shutdown=Mock())
    window._startup_update_pending = True
    window._self_update_controller = controller
    assert window.request_emergency_stop()
    app.processEvents()
    assert controller.request_safe_stop.called
    window._critical_quit.assert_not_called()
    window.review_launch()
    window.start_optimization()
    window.backend.optimize.assert_not_called()
    controller.pending = False
    window._startup_update_pending = False
    wait_until(app, lambda: window._critical_quit.called)
    controller.shutdown.assert_called_once_with()


def test_idle_stop_blocks_setup_and_settings_overrides_without_a_busy_window(owner, monkeypatch):
    from optimus import native_ui, onboarding, ui
    window, app, state, _ = owner
    state.native_pending = 1  # Keep the stopped GUI alive while probing late entrypoints.
    factories = []
    for module, name in ((ui, 'DetailedSettingsWindow'), (native_ui, 'NativeSettingsWindow'),
                         (onboarding, 'InitialSetupWizard'), (native_ui, 'NativeSetupWizard')):
        factory = Mock(side_effect=AssertionError('STOP must gate a fresh settings/setup constructor.'))
        monkeypatch.setattr(module, name, factory)
        factories.append(factory)
    assert window._settings_window is None and window._setup_wizard is None
    assert window.request_emergency_stop()
    window.open_settings()
    assert window.open_initial_setup() is False
    assert window._settings_window is None and window._setup_wizard is None
    for factory in factories:
        factory.assert_not_called()
    window.backend.optimize.assert_not_called()
    state.native_pending = 0
    wait_until(app, lambda: window._critical_quit.called)


def test_late_completed_result_saves_receipt_but_cannot_queue_followups(owner):
    window, app, state, _ = owner
    started, release = threading.Event(), threading.Event()
    window._critical_releases.append(release)
    receipt = window.backend.data_dir / 'completed-after-stop.txt'

    def operation(settings, *, progress_callback, cancel_event):
        started.set()
        assert release.wait(10)
        assert cancel_event.is_set()
        receipt.write_text('native result accounted for', encoding='utf-8')
        return {'status': 'completed', 'stages': [],
                'security_review': {'recommendations': [{'id': 'must-not-open'}]}}

    window.backend.optimize.side_effect = operation
    window.start_optimization()
    wait_until(app, started.is_set)
    assert window.request_emergency_stop()
    assert not window._progress_window.stop_button.isEnabled()
    window._critical_quit.assert_not_called()
    release.set()
    wait_until(app, lambda: window._critical_quit.called)
    assert receipt.read_text() == 'native result accounted for'
    assert window._pending_auto_fixes is None and window._pending_post_review is None
    window.backend.available_fixes.assert_not_called()
    window.backend.apply_available_fixes.assert_not_called()
    window._critical_quit.assert_called_once_with()


@pytest.mark.parametrize('size', [(640, 480), (960, 540), (1280, 820), (1920, 1080)])
def test_stop_label_and_hitbox_remain_visible_when_resizing(owner, size):
    window, app, _, _ = owner
    for surface in (window, OptimizationProgressWindow(window, threading.Event())):
        surface.resize(*size)
        surface.show()
        app.processEvents()
        button = surface.stop_button
        bounds = QRect(button.mapTo(surface, QPoint()), button.size())
        assert button.isVisible() and surface.rect().contains(bounds)
        assert button.text() == 'STOP'
        assert button.width() >= button.fontMetrics().horizontalAdvance('STOP') + 20
        assert 'exit' in button.accessibleName().lower()
        assert 'installer' in button.toolTip().lower()
        assert '#b32636' in button.styleSheet()
        if surface is not window:
            surface.finish({'status': 'cancelled', 'cancelled': True, 'stages': []})
            surface.close()
            surface.deleteLater()
