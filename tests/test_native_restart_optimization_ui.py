"""Restart uses disposable services and real Qt workers, never host maintenance."""
import copy
import os
from pathlib import Path
import threading
import time
from types import SimpleNamespace
from unittest.mock import Mock

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

import pytest
from PySide6.QtCore import QCoreApplication, QEvent, QPoint, QRect, Qt
from PySide6.QtGui import QFontDatabase
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from optimus.automation import CURRENT_SETUP_VERSION, RunLease
from optimus.emergency_stop import controller_for
from optimus.ui import OptimusWindow, WorkflowWorker
from optimus.workflow import DEFAULTS


def wait_until(app, condition):
    deadline = time.monotonic() + 6
    while not condition() and time.monotonic() < deadline:
        app.processEvents()
        QTest.qWait(5)
    app.processEvents()
    assert condition(), 'Fixture did not reach its expected lifecycle state.'


def completed():
    return {'status': 'completed', 'stages': [], 'security_review': {}, 'process_review': {}}


@pytest.fixture(scope='module')
def app():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def window(app, tmp_path, monkeypatch):
    monkeypatch.setenv('OPTIMUS_REDUCE_MOTION', '1')
    loaded = []
    if os.name == 'nt' and not QFontDatabase.families():
        for name in ('segoeui.ttf', 'seguisb.ttf'):
            source = Path(os.environ.get('SystemRoot', r'C:\Windows')) / 'Fonts' / name
            if source.is_file():
                loaded.append(QFontDatabase.addApplicationFont(str(source)))
    monkeypatch.setattr('optimus.live_metrics.LiveMetrics', lambda _: SimpleNamespace(
        status=lambda: {}, start=lambda: None, stop=lambda **_: None))
    settings = dict(copy.deepcopy(DEFAULTS), setup_version=CURRENT_SETUP_VERSION,
        reduce_motion=True, auto_continue_fixes_enabled=False, post_process_review_enabled=False,
        browser_cache_enabled=False, browser_history_enabled=False, browser_cookies_enabled=False,
        browser_download_history_enabled=False)
    backend = SimpleNamespace(data_dir=tmp_path, preferences=lambda: copy.deepcopy(settings),
        optimize=Mock(return_value=completed()), available_fixes=Mock(return_value={'actions': [], 'manual_items': []}))
    value = OptimusWindow(backend, smoke_test=True)
    value.smoke_test = False
    value._restart_timer.setInterval(15)
    value.show()
    yield value
    value._cancel_optimization_restart()
    value._cancel.set()
    value.pool.waitForDone(5000)
    app.processEvents()
    value._quitting = True
    value._post_review_active = False
    if value._progress_window is not None:
        value._progress_window.completed = True
        value._progress_window.close()
    value.shutdown()
    value.close()
    value.deleteLater()
    QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)
    app.processEvents()
    for identity in loaded:
        if identity >= 0:
            QFontDatabase.removeApplicationFont(identity)


def finish_first(window, app):
    window.start_optimization()
    wait_until(app, lambda: window._progress_window is not None and not window.busy and window.pool.activeThreadCount() == 0)
    return window._progress_window


def test_header_click_starts_exactly_one_new_run_through_existing_entry(window, app):
    previous = finish_first(window, app)
    assert previous.restart_button.text() == 'Restart optimization'
    assert '#16803d' in previous.restart_button.styleSheet()
    assert not previous.restart_button.autoDefault() and not previous.restart_button.isDefault()
    QTest.mouseClick(previous.restart_button, Qt.LeftButton)
    previous.restartRequested.emit()  # A second signal cannot queue a second launch.
    wait_until(app, lambda: window.backend.optimize.call_count == 2 and not window.busy)
    QTest.qWait(40)
    assert window.backend.optimize.call_count == 2
    assert window._progress_window is not previous and window._restart_progress is None


def test_restart_waits_finished_signal_and_actual_worker_pool_drain(window, app, monkeypatch):
    entered, release, tail = (threading.Event() for _ in range(3))
    class DelayedReturnWorker(WorkflowWorker):
        def run(self):
            super().run()
            assert tail.wait(5)
    monkeypatch.setattr('optimus.ui.WorkflowWorker', DelayedReturnWorker)
    def optimize(settings, *, cancel_event, **kwargs):
        if window.backend.optimize.call_count == 1:
            entered.set()
            assert release.wait(5)
            return completed()  # A late cancellation may arrive after work has finished.
        return completed()
    window.backend.optimize.side_effect = optimize
    window.start_optimization()
    wait_until(app, entered.is_set)
    progress, original_cancel = window._progress_window, window._cancel
    progress.restart_button.click()
    try:
        assert original_cancel.is_set()
        assert window._restart_progress is progress and not progress.restart_button.isEnabled()
        release.set()
        wait_until(app, lambda: not window.busy)
        QTest.qWait(50)
        assert window.pool.activeThreadCount() == 1
        assert window.backend.optimize.call_count == 1 and window._cancel is original_cancel
        window.backend.available_fixes.assert_not_called()
    finally:
        release.set()
        tail.set()
    wait_until(app, lambda: window.backend.optimize.call_count == 2 and not window.busy)
    assert window._cancel is not original_cancel


def test_external_lease_error_is_honest_and_one_restart_waits_for_release(window, app):
    window.backend.optimize.side_effect = [RuntimeError('Another Optimus process is already running maintenance.'), completed()]
    progress = finish_first(window, app)
    assert progress.progress.value() == 0
    assert progress.title.text() == 'ANOTHER OPTIMIZATION IS ACTIVE'
    assert 'progress is unavailable here' in progress.description.text()
    assert progress.worker_cards['maintenance'].status.text() == 'Waiting for maintenance'
    assert all(card.status.text() != 'Stopped' for card in progress.worker_cards.values())
    lease = RunLease(window.backend.data_dir).acquire()
    try:
        progress.restart_button.click()
        progress.restartRequested.emit()
        wait_until(app, lambda: 'maintenance lease' in progress.restart_status.text())
        assert window.backend.optimize.call_count == 1 and progress.progress.value() == 0
        window.start_optimization()
        assert window.backend.optimize.call_count == 1
    finally:
        lease.release()
    wait_until(app, lambda: window.backend.optimize.call_count == 2 and not window.busy)


@pytest.mark.parametrize('dismiss', ['close', 'reject'])
def test_dismissal_cancels_queue_even_if_lease_probe_returns_later(window, app, monkeypatch, dismiss):
    progress = finish_first(window, app)
    entered, release = threading.Event(), threading.Event()
    def probe(directory):
        entered.set()
        assert release.wait(5)
        return True
    monkeypatch.setattr('optimus.ui._restart_lease_available', probe)
    progress.restart_button.click()
    wait_until(app, entered.is_set)
    getattr(progress, dismiss)()
    release.set()
    wait_until(app, lambda: window._restart_probe is None and window.pool.activeThreadCount() == 0)
    QTest.qWait(40)
    assert window._restart_progress is None and window.backend.optimize.call_count == 1


def test_owned_native_observer_must_drain_before_cancel_token_changes(window, app, monkeypatch):
    progress = finish_first(window, app)
    controller = controller_for(window.backend)
    snapshot = {'stopped': False, 'drained': False, 'native_pending': 1}
    monkeypatch.setattr(controller, 'snapshot', lambda: dict(snapshot))
    original_cancel = window._cancel
    progress.restart_button.click()
    QTest.qWait(50)
    assert 'owned system or vendor operations' in progress.restart_status.text()
    assert window._restart_probe is None and window.backend.optimize.call_count == 1
    assert window._cancel is original_cancel
    snapshot.update(drained=True, native_pending=0)
    wait_until(app, lambda: window.backend.optimize.call_count == 2 and not window.busy)


def test_closing_queued_restart_does_not_regenerate_late_saved_followup(window, app):
    saved = window.backend.preferences()
    window.backend.preferences = lambda: dict(saved, auto_continue_fixes_enabled=True)
    entered, release = threading.Event(), threading.Event()
    def optimize(*args, **kwargs):
        entered.set()
        assert release.wait(5)
        return completed()
    window.backend.optimize.side_effect = optimize
    window.backend.available_fixes.return_value = {'actions': [{'id': 'fixture', 'title': 'Fixture fix'}]}
    window.backend.apply_available_fixes = Mock()
    window.start_optimization()
    wait_until(app, entered.is_set)
    progress = window._progress_window
    try:
        progress.restart_button.click()
        assert progress.close() is False
        assert window._restart_progress is None
    finally:
        release.set()
    wait_until(app, lambda: not window.busy)
    window.backend.available_fixes.assert_not_called()
    window.backend.apply_available_fixes.assert_not_called()
    assert window.backend.optimize.call_count == 1


def test_shared_stop_latch_cancels_queued_restart(window, app):
    progress = finish_first(window, app)
    controller_for(window.backend).request_stop()
    progress.restart_button.click()
    app.processEvents()
    assert window._restart_progress is None and not progress.restart_button.isEnabled()
    assert 'STOP is active' in window.status.text()
    assert window.backend.optimize.call_count == 1


def test_emergency_stop_button_clears_existing_restart_queue(window, app, monkeypatch):
    progress = finish_first(window, app)
    lease = RunLease(window.backend.data_dir).acquire()
    monkeypatch.setattr(window, '_finish_emergency_stop', Mock())
    monkeypatch.setattr(window, 'request_safe_stop', Mock(return_value=True))
    try:
        progress.restart_button.click()
        assert window._restart_progress is progress
        progress.stop_button.click()
        app.processEvents()
        assert window._emergency_stopping and window._restart_progress is None
        assert not window._restart_timer.isActive() and not progress.restart_button.isEnabled()
        assert window.backend.optimize.call_count == 1
    finally:
        lease.release()


def test_restart_preserves_saved_scope_and_launch_review_gate(window, app, monkeypatch):
    progress = finish_first(window, app)
    monkeypatch.setattr(window, 'session_approved', lambda *args: False)
    review = Mock(return_value=False)
    monkeypatch.setattr(window, 'review_launch', review)
    progress.restart_button.click()
    wait_until(app, lambda: review.called)
    assert window.backend.optimize.call_count == 1
    assert window._restart_progress is None


def test_main_quit_request_cancels_queue_before_normal_exit_guards(window, app):
    progress = finish_first(window, app)
    lease = RunLease(window.backend.data_dir).acquire()
    try:
        progress.restart_button.click()
        window._setup_wizard = SimpleNamespace()  # Existing exit guard keeps the fixture app open.
        assert window.quit_application() is False
        assert window._restart_progress is None and not window._restart_timer.isActive()
        assert window.backend.optimize.call_count == 1
    finally:
        window._setup_wizard = None
        lease.release()


@pytest.mark.parametrize('size', [(850, 650), (640, 480), (360, 400)])
def test_header_restart_fullscreen_and_stop_fit_normal_narrow_and_fullscreen(window, app, size):
    progress = finish_first(window, app)
    progress.finish(None, error='Another Optimus process is already running maintenance.')
    progress.resize(*size)
    for _ in range(5):
        app.processEvents()
    assert progress.size().toTuple() == size
    buttons = (progress.restart_button, progress.fullscreen.button, progress.stop_button)
    for button in buttons:
        assert button.isVisible()
        rectangle = QRect(button.mapTo(progress, QPoint()), button.size())
        assert progress.rect().contains(rectangle)
        assert button.visibleRegion().boundingRect() == button.rect()
    assert progress.restart_button.width() >= progress.restart_button.fontMetrics().horizontalAdvance(progress.restart_button.text()) + 20
    if size[0] >= 640:
        assert progress.title.visible_text == progress.title.text()
    assert all(not left.geometry().intersects(right.geometry()) for index, left in enumerate(buttons) for right in buttons[index + 1:])
    progress.fullscreen.button.click()
    app.processEvents()
    assert progress.isFullScreen()
    assert progress.restart_button.visibleRegion().boundingRect() == progress.restart_button.rect()
    QTest.keyClick(progress, Qt.Key_Escape)
    app.processEvents()
    assert not progress.isFullScreen() and progress.isVisible()
