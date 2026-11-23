"""Startup scans remain visible and stoppable using fixture work only."""
import copy
import os
import threading
import time
from types import SimpleNamespace
from unittest.mock import Mock
from pathlib import Path

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

import pytest
from PySide6.QtCore import QThread, QRect
from PySide6.QtGui import QFont, QFontDatabase
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from optimus.native_ui import NativeOptimusWindow
from optimus.startup_jobs import JOBS, StartupJobs
from optimus.ui import OptimusWindow
from optimus.widgets.startup_progress import StartupProgressDialog
from optimus.workflow import DEFAULTS


def wait_until(app, predicate):
    deadline = time.monotonic() + 4
    while not predicate() and time.monotonic() < deadline:
        app.processEvents()
        QTest.qWait(5)
    app.processEvents()
    assert predicate(), 'Fixture startup work did not reach the expected state.'


class FixturePass:
    def __init__(self):
        self.entered = threading.Event()
        self.release = threading.Event()
        self.calls = 0
        self.worker_thread = None
        self.hold_after_cancel = False

    def __call__(self, backend, settings, *, cancel_event, progress):
        self.calls += 1
        self.worker_thread = threading.get_ident()
        if not settings.get('startup_jobs_enabled'):
            return {'status': 'disabled', 'jobs': []}
        selected = [job for job in JOBS if settings.get(job[2])]
        if not selected:
            return {'status': 'completed', 'jobs': []}
        key, title, _setting = selected[0]
        event = dict(job=key, title=title, status='running', current=0, total=1,
                     description='Checking the fixture application')
        progress(event)
        self.entered.set()
        deadline = time.monotonic() + 5
        while not self.release.is_set() and time.monotonic() < deadline:
            if cancel_event.is_set() and not self.hold_after_cancel:
                break
            self.release.wait(.01)
        status = 'cancelled' if cancel_event.is_set() else 'completed'
        row = dict(job=key, title=title, status=status, seconds=.1,
                   result={'note': 'Fixture only; no host action was performed.'})
        progress(dict(row, current=1, total=1, description='Fixture finished'))
        progress(dict(status=status, current=1, total=1, jobs=[row], description='Startup jobs finished'))
        return {'status': status, 'jobs': [row]}


@pytest.fixture(params=[OptimusWindow, NativeOptimusWindow], ids=['windows', 'native'])
def window(request, tmp_path, monkeypatch):
    monkeypatch.setenv('OPTIMUS_REDUCE_MOTION', '1')
    app = QApplication.instance() or QApplication([])
    settings = copy.deepcopy(DEFAULTS)
    settings.update(startup_jobs_enabled=True, driver_updates_enabled=False,
                    software_updates_enabled=True, spyware_scan_enabled=False,
                    zero_breach_enabled=False, tracker_blocking_enabled=False)
    backend = SimpleNamespace(data_dir=tmp_path, preferences=lambda: copy.deepcopy(settings), optimize=Mock())
    work = FixturePass()
    monkeypatch.setattr('optimus.startup_jobs.StartupJobs',
                        lambda *args, **kwargs: StartupJobs(*args, runner=work, **kwargs))
    value = request.param(backend, smoke_test=True)
    value.smoke_test = False
    value.show()
    quit_app = Mock()
    monkeypatch.setattr(app, 'quit', quit_app)
    yield SimpleNamespace(value=value, app=app, settings=settings, work=work, quit=quit_app)
    work.release.set()
    if value._startup_jobs is not None:
        value._startup_jobs.stop()
        if value._startup_jobs.thread is not None:
            value._startup_jobs.thread.join(3)
    value._quitting = True
    value.shutdown()
    value.close()
    value.deleteLater()
    app.processEvents()


def begin(fixture):
    fixture.value.begin_startup_jobs()
    fixture.value._startup_jobs_gate.stop()  # Advance the same GUI gate explicitly.
    fixture.value._startup_jobs_tick()


def test_dashboard_starts_after_self_update_and_worker_events_use_gui_thread(window, monkeypatch):
    threads = []
    original = StartupProgressDialog.update_progress

    def record(dialog, event):
        threads.append(QThread.currentThread())
        return original(dialog, event)

    monkeypatch.setattr(StartupProgressDialog, 'update_progress', record)
    value, app = window.value, window.app
    value._startup_update_pending = True
    begin(window)
    assert value._startup_progress_window is None
    assert window.work.calls == 0
    value._startup_update_pending = False
    value._startup_jobs_tick()
    wait_until(app, lambda: window.work.entered.is_set() and bool(threads))
    dashboard = value._startup_progress_window
    assert dashboard.isVisible() and not dashboard.isModal()
    assert dashboard.table.item(0, 1).text() == 'Running'
    assert 'fixture application' in dashboard.status.text()
    assert dashboard.progress.maximum() == 0  # No invented percent for an active provider.
    assert all(thread == app.thread() for thread in threads)
    assert window.work.worker_thread != threading.get_ident()
    assert value.backend.optimize.call_count == 0


@pytest.mark.parametrize('disabled', ['master', 'all_jobs'])
def test_disabled_startup_jobs_do_not_open_a_dashboard(window, disabled):
    if disabled == 'master':
        window.settings['startup_jobs_enabled'] = False
    else:
        for _key, _title, setting in JOBS:
            window.settings[setting] = False
    begin(window)
    wait_until(window.app, lambda: not window.value._startup_jobs.running)
    window.value._startup_jobs_tick()
    assert window.value._startup_progress_window is None
    assert not window.work.entered.is_set()


def test_hide_tray_restore_and_optimize_reuse_existing_scan(window):
    begin(window)
    value, app = window.value, window.app
    wait_until(app, window.work.entered.is_set)
    dashboard = value._startup_progress_window
    dashboard.close()
    assert not dashboard.isVisible()
    assert value._startup_jobs.running and not value._startup_jobs.cancel.is_set()
    value._restore_window()
    assert value._startup_progress_window is dashboard and dashboard.isVisible()
    dashboard.hide()
    value.start_optimization()
    assert dashboard.isVisible()
    assert value._start_after_startup_jobs is True
    assert window.work.calls == 1 and value.backend.optimize.call_count == 0
    dashboard.request_cancel()  # Cancel the queued Auto Clean as well as fixture startup work.
    assert value._start_after_startup_jobs is False


def test_explicit_stop_is_cooperative_and_final_cancelled_result_remains_visible(window):
    window.work.hold_after_cancel = True
    begin(window)
    value, app = window.value, window.app
    wait_until(app, window.work.entered.is_set)
    dashboard = value._startup_progress_window
    dashboard.cancel_button.click()
    assert value._startup_jobs.cancel.is_set()
    assert value._startup_jobs.running and dashboard.isVisible()
    assert 'Stopping startup jobs safely' in dashboard.status.text()
    assert not dashboard.cancel_button.isEnabled()
    assert dashboard.progress.maximum() == 0
    window.work.release.set()
    wait_until(app, lambda: not value._startup_jobs.running)
    value._startup_jobs_tick()
    assert dashboard.table.item(0, 1).text() == 'Cancelled'
    assert not dashboard.running and 'stopped' in dashboard.status.text()
    assert dashboard.isVisible() and window.quit.call_count == 0


def test_queued_progress_cannot_revive_a_cancelled_pass_after_final_report(window):
    begin(window)
    value = window.value
    # Wait without processing Qt events, leaving the worker's running event queued.
    assert window.work.entered.wait(2)
    value._startup_jobs.stop()
    window.work.release.set()
    value._startup_jobs.thread.join(3)
    assert not value._startup_jobs.running
    value._startup_jobs_tick()
    dashboard = value._startup_progress_window
    assert not dashboard.running and dashboard.table.item(0, 1).text() == 'Cancelled'
    window.app.processEvents()
    assert not dashboard.running and dashboard.table.item(0, 1).text() == 'Cancelled'
    value._startup_jobs_tick()
    assert not dashboard.running


def test_quit_stops_startup_jobs_and_waits_for_current_operation(window):
    window.work.hold_after_cancel = True
    begin(window)
    value, app = window.value, window.app
    wait_until(app, window.work.entered.is_set)
    assert value.quit_application() is False
    assert value._close_when_idle and value._startup_jobs.cancel.is_set()
    assert not value._startup_jobs_gate.isActive()
    assert window.quit.call_count == 0 and value._startup_jobs.running
    value._finish_optional_stop()
    assert window.quit.call_count == 0
    window.work.release.set()
    wait_until(app, lambda: not value._startup_jobs.running)
    value._finish_optional_stop()
    assert window.quit.call_count == 1


def test_external_shutdown_stops_and_drains_startup_thread(window):
    begin(window)
    value = window.value
    wait_until(window.app, window.work.entered.is_set)
    value.shutdown()
    assert value._startup_jobs.cancel.is_set()
    assert not value._startup_jobs.running
    assert value._startup_jobs.timer is None
    assert not value._startup_jobs_gate.isActive()


def test_emergency_stop_latches_startup_cancellation_before_backend_stop(window, monkeypatch):
    window.work.hold_after_cancel = True
    begin(window)
    value = window.value
    wait_until(window.app, window.work.entered.is_set)
    observed = []
    controller = SimpleNamespace(request_stop=lambda: observed.append(value._startup_jobs.cancel.is_set()))
    monkeypatch.setattr('optimus.emergency_stop.controller_for', lambda backend: controller)
    monkeypatch.setattr(value, '_finish_emergency_stop', Mock())
    assert value.request_emergency_stop()
    assert observed == [True]
    assert value._startup_jobs.running and window.quit.call_count == 0
    assert value._startup_progress_window.stopping
    assert not value._startup_jobs_gate.isActive()


def test_administrator_restart_waits_for_running_startup_jobs(window, monkeypatch):
    begin(window)
    value = window.value
    wait_until(window.app, window.work.entered.is_set)
    request = Mock()
    monkeypatch.setattr('optimus.elevation.request_administrator', request)
    assert value.restart_as_administrator() is False
    request.assert_not_called()
    assert value._startup_progress_window.isVisible()
    assert not value._quitting and value._startup_jobs.running


@pytest.mark.parametrize('status', ['completed', 'failed', 'skipped', 'cancelled'])
def test_job_outcomes_are_distinct_and_terminal_row_does_not_finish_active_pass(status):
    app = QApplication.instance() or QApplication([])
    dialog = StartupProgressDialog([('apps', 'App updater', 'software_updates_enabled')])
    dialog.update_progress(dict(job='apps', title='App updater', status=status, current=1, total=1,
                                error='Fixture failure' if status == 'failed' else None))
    assert dialog.table.item(0, 1).text() == status.capitalize()
    assert dialog.running and dialog.progress.maximum() == 0
    overall = 'completed' if status == 'skipped' else status
    dialog.update_progress(dict(status=overall, current=1, total=1))
    assert not dialog.running
    assert dialog.progress.maximum() == 1 and dialog.progress.value() == 1
    assert status in dialog.summary.text()
    if status == 'failed':
        assert 'failed' in dialog.status.text() and 'Fixture failure' in dialog.table.item(0, 2).text()
        assert 'retry' not in dialog.status.text().lower()
    dialog.deleteLater()
    app.processEvents()


@pytest.mark.parametrize('size', [(850, 520), (480, 340)])
def test_dashboard_columns_and_controls_fit_normal_and_narrow_windows(size):
    app = QApplication.instance() or QApplication([])
    previous_font = app.font()
    font_ids = []
    # Windows offscreen Qt can expose no system fonts. Register an installed
    # font only in this fixture, as the existing critical-review tool does.
    if os.name == 'nt' and app.platformName() == 'offscreen':
        for name in ('segoeui.ttf', 'segoeuib.ttf', 'seguisb.ttf', 'arial.ttf'):
            candidate = Path(os.environ.get('WINDIR', r'C:\Windows')) / 'Fonts' / name
            if candidate.is_file():
                identity = QFontDatabase.addApplicationFont(str(candidate))
                if identity >= 0:
                    font_ids.append(identity)
        app.setFont(QFont('Segoe UI', 10))
    dialog = StartupProgressDialog(JOBS)
    dialog.setProperty('_optimus_no_screen_fit', True)
    try:
        dialog.update_progress(dict(job='drivers', title='Driver updater', status='completed',
                                    result={'note': 'Driver discovery completed. No approved installations were needed.'}))
        dialog.update_progress(dict(job='apps', title='App updater', status='running',
                                    description='Checking approved application updates.'))
        dialog.resize(*size)
        dialog.show()
        app.processEvents()
        assert dialog.size().width() == size[0] and dialog.size().height() == size[1]
        assert dialog.table.horizontalScrollBar().maximum() == 0
        assert sum(dialog.table.columnWidth(index) for index in range(3)) <= dialog.table.viewport().width()
        assert dialog.table.columnWidth(2) >= 150
        for button in (dialog.hide_button, dialog.cancel_button):
            rectangle = QRect(button.mapTo(dialog, button.rect().topLeft()), button.size())
            assert dialog.rect().contains(rectangle)
            assert button.isVisible() and button.visibleRegion().boundingRect() == button.rect()
    finally:
        dialog.hide()
        dialog.deleteLater()
        app.processEvents()
        app.setFont(previous_font)
        for identity in font_ids:
            QFontDatabase.removeApplicationFont(identity)
