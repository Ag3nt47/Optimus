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
from optimus.widgets.compact_worker_ring import CompactWorkerActivityRing
from optimus.widgets.theme import build_qss
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


@pytest.mark.parametrize('size', [(900, 650), (480, 340)])
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
    dialog.setStyleSheet(build_qss('cyber'))
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
        assert sum(dialog.table.columnWidth(index) for index in range(dialog.table.columnCount())) <= dialog.table.viewport().width()
        assert dialog.table.columnWidth(2) >= 150
        for row, (key, _title, _setting) in enumerate(JOBS):
            ring = dialog.rings[key]
            assert isinstance(ring, CompactWorkerActivityRing)
            assert dialog.table.rowHeight(row) >= ring.height()
            if size == (900, 650):
                rectangle = QRect(ring.mapTo(dialog.table.viewport(), ring.rect().topLeft()), ring.size())
                assert dialog.table.viewport().rect().contains(rectangle)
                assert ring.visibleRegion().boundingRect() == ring.rect()
                button = dialog.skip_buttons[key]
                assert button.isVisible() and button.visibleRegion().boundingRect() == button.rect()
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


@pytest.fixture
def ring_dialog(monkeypatch):
    monkeypatch.setenv('OPTIMUS_REDUCE_MOTION', '0')
    monkeypatch.setattr('optimus.widgets.header_controls._windows_client_animations_enabled', lambda: True)
    app = QApplication.instance() or QApplication([])
    dialog = StartupProgressDialog(JOBS)
    dialog.setProperty('_optimus_no_screen_fit', True)
    dialog.show()
    app.processEvents()
    yield SimpleNamespace(value=dialog, app=app)
    dialog.finish('cancelled')
    dialog.hide()
    dialog.deleteLater()
    app.processEvents()


def item_progress(key='fixture:app', label='Fixture app', phase='download', percent=37, **changes):
    return dict(key=key, label=label, phase=phase, percent=percent, **changes)


@pytest.mark.parametrize('job', ['drivers', 'apps', 'defender'])
@pytest.mark.parametrize('estimated', [False, True])
def test_startup_wheel_displays_only_explicit_normalized_current_item_progress(ring_dialog, job, estimated):
    dialog = ring_dialog.value
    item = item_progress(key='security:defender:quick_scan' if job == 'defender' else 'fixture:app',
                         phase='scan' if job == 'defender' else 'download', estimated=estimated)
    dialog.update_progress(dict(job=job, status='running', current=4, total=5,
                                detail={'item_progress': item, 'current': 9, 'total': 10}))
    ring = dialog.rings[job]
    assert ring.value_text() == ('~37%' if estimated else '37%')
    assert ring.active and not ring.timer.isActive()
    assert item['label'] in ring.accessibleDescription()
    assert ring.phase_caption in ring.accessibleDescription()
    assert 'Job and package counts are separate' in ring.toolTip()


@pytest.mark.parametrize('event', [
    dict(current=37, total=100, percentage=37),
    dict(detail={'current': 37, 'total': 100, 'percentage': 37, 'phase': 'download'}),
    dict(detail={'cpu_percent': 37, 'percent': 37}),
    dict(detail={'item_progress': item_progress(percent=True)}),
    dict(detail={'item_progress': item_progress(phase='install', percent=None)}),
    dict(detail={'item_progress': item_progress(key='fixture:next', label='Next fixture app', percent=None)}),
])
def test_counts_unknown_new_phase_and_new_item_clear_stale_wheel_percentage(ring_dialog, event):
    dialog = ring_dialog.value
    dialog.update_progress(dict(job='apps', status='running', detail={'item_progress': item_progress()}))
    assert dialog.rings['apps'].value_text() == '37%'
    dialog.update_progress(dict(event, job='apps', status='running'))
    ring = dialog.rings['apps']
    assert ring.value_text() == '—' and ring.value_percent is None
    assert ring.active and ring.timer.isActive()


@pytest.mark.parametrize('job', ['zero_breach', 'trackers'])
def test_quiet_native_jobs_spin_without_inventing_a_percentage(ring_dialog, job):
    dialog, app = ring_dialog.value, ring_dialog.app
    dialog.update_progress(dict(job=job, status='running', current=4, total=5,
                                detail={'current': 100, 'total': 100, 'phase': 'scan'}))
    ring = dialog.rings[job]
    assert ring.value_text() == '—' and ring.active and ring.timer.isActive()
    angle = ring.angle
    QTest.qWait(90)
    app.processEvents()
    assert ring.angle != angle


@pytest.mark.parametrize('status', ['pending', 'completed', 'failed', 'skipped', 'cancelled'])
def test_queued_and_terminal_job_wheels_stop_and_drop_unverified_percentages(ring_dialog, status):
    dialog = ring_dialog.value
    dialog.update_progress(dict(job='apps', status='running', detail={'item_progress': item_progress()}))
    dialog.update_progress(dict(job='apps', status=status, result={'completion_verified': True}))
    ring = dialog.rings['apps']
    assert ring.value_text() == '—'
    assert not ring.active and not ring.timer.isActive()


@pytest.mark.parametrize('verified', [False, True])
def test_only_verified_defender_completion_retains_a_terminal_hundred_percent(ring_dialog, verified):
    dialog = ring_dialog.value
    dialog.update_progress(dict(job='defender', status='running',
                                detail={'item_progress': item_progress(phase='scan', percent=99, estimated=True)}))
    dialog.update_progress(dict(job='defender', status='completed', result={'completion_verified': verified}))
    ring = dialog.rings['defender']
    assert ring.value_text() == ('100%' if verified else '—')
    assert not ring.active and not ring.timer.isActive() and not ring.estimated
    if verified:
        assert ring.phase_caption == 'Scan'
    dialog.update_progress(dict(job='defender', status='skipped', result={'completion_verified': True}))
    assert ring.value_text() == '—'  # Reusing a prior scan does not claim a new completed scan.


def test_quiet_wheel_resumes_after_hide_minimize_and_motion_preferences(ring_dialog, monkeypatch):
    dialog, app = ring_dialog.value, ring_dialog.app
    dialog.update_progress(dict(job='trackers', status='running'))
    ring = dialog.rings['trackers']
    assert ring.timer.isActive()
    dialog.hide()
    app.processEvents()
    assert not ring.timer.isActive()
    dialog.show()
    app.processEvents()
    assert ring.timer.isActive()  # No new provider event is needed to resume.
    dialog.showMinimized()
    app.processEvents()
    assert not ring.timer.isActive()
    dialog.showNormal()
    app.processEvents()
    assert ring.timer.isActive()
    monkeypatch.setenv('OPTIMUS_REDUCE_MOTION', '1')
    dialog._sync_rings()
    assert ring.active and not ring.timer.isActive() and ring.value_text() == '—'
    monkeypatch.setenv('OPTIMUS_REDUCE_MOTION', '0')
    dialog.config = SimpleNamespace(ui_motion_enabled=False)
    dialog._sync_rings()
    assert not ring.timer.isActive()
    dialog.config.ui_motion_enabled = True
    dialog._sync_rings()
    assert ring.timer.isActive()
    dialog.finish('failed')
    assert not ring.active and not ring.timer.isActive()


def test_queued_skip_is_requested_once_and_does_not_stop_other_jobs(ring_dialog):
    dialog = ring_dialog.value
    requests, stops = [], Mock()
    dialog.skipRequested.connect(requests.append)
    dialog.cancelRequested.connect(stops)
    dialog.skip_buttons['drivers'].click()
    dialog.request_skip('drivers')
    assert requests == ['drivers'] and not dialog.skip_buttons['drivers'].isEnabled()
    assert dialog._states['drivers'] == 'pending'  # Wait for the controller's accepted outcome.
    dialog.update_progress(dict(job='drivers', status='skipped', skip_requested=True))
    assert dialog.table.item(0, 1).text() == 'Skipped'
    assert not dialog.rings['drivers'].active
    assert dialog.skip_buttons['apps'].isEnabled()
    assert stops.call_count == 0


@pytest.mark.parametrize('outcome', ['completed', 'failed', 'skipped', 'uncertain'])
def test_active_skip_keeps_wheel_live_until_actual_provider_outcome(ring_dialog, outcome):
    dialog = ring_dialog.value
    requests = []
    dialog.skipRequested.connect(requests.append)
    dialog.update_progress(dict(job='apps', status='running'))
    dialog.skip_buttons['apps'].click()
    dialog.update_progress(dict(job='apps', status='running', skip_requested=True))
    assert requests == ['apps'] and dialog._states['apps'] == 'running'
    assert dialog.table.item(1, 1).text() == 'Skipping safely…'
    assert dialog.rings['apps'].active and dialog.rings['apps'].timer.isActive()
    assert not dialog.skip_buttons['apps'].isEnabled()
    dialog.update_progress(dict(job='apps', status='running', description='Provider is finishing safely.'))
    assert dialog.table.item(1, 1).text() == 'Skipping safely…'
    dialog.update_progress(dict(job='apps', status=outcome, result={'status': outcome}))
    assert dialog.table.item(1, 1).text() == outcome.capitalize()
    assert not dialog.rings['apps'].active and not dialog.rings['apps'].timer.isActive()
    assert not dialog.skip_buttons['apps'].isEnabled()
    if outcome == 'uncertain':
        assert dialog.rings['apps'].health == 'error'


def test_finished_and_stopping_dashboards_cannot_request_more_skips(ring_dialog):
    dialog = ring_dialog.value
    requests = []
    dialog.skipRequested.connect(requests.append)
    dialog.mark_stopping()
    for key, button in dialog.skip_buttons.items():
        assert not button.isEnabled()
        dialog.request_skip(key)
    assert requests == []
    dialog.finish('cancelled')
    assert all(not ring.timer.isActive() for ring in dialog.rings.values())


def test_row_skip_routes_to_its_controller_without_global_cancel_or_new_work(window, monkeypatch):
    begin(window)
    value = window.value
    wait_until(window.app, window.work.entered.is_set)
    skip = Mock(return_value=True)
    monkeypatch.setattr(value._startup_jobs, 'skip', skip, raising=False)
    value._startup_progress_window.skip_buttons['apps'].click()
    value._startup_progress_window.request_skip('apps')
    skip.assert_called_once_with('apps')
    assert not value._startup_jobs.cancel.is_set()
    assert window.work.calls == 1 and value.backend.optimize.call_count == 0


def test_motion_setting_changes_resync_quiet_startup_wheel_on_existing_gui_poll(window, monkeypatch):
    monkeypatch.setenv('OPTIMUS_REDUCE_MOTION', '0')
    monkeypatch.setattr('optimus.widgets.header_controls._windows_client_animations_enabled', lambda: True)
    value = window.value
    value.config.ui_motion_enabled = True
    begin(window)
    wait_until(window.app, lambda: window.work.entered.is_set()
               and value._startup_progress_window.rings['apps'].timer.isActive())
    ring = value._startup_progress_window.rings['apps']
    value.config.ui_motion_enabled = False
    value._startup_jobs_tick()  # The existing GUI gate runs this every two seconds.
    assert ring.active and not ring.timer.isActive()
    value.config.ui_motion_enabled = True
    value._startup_jobs_tick()
    assert ring.active and ring.timer.isActive()
    assert window.work.calls == 1  # No new provider event or job was needed.


@pytest.mark.parametrize('terminal', ['completed', 'failed', 'skipped', 'cancelled', 'uncertain'])
def test_late_skip_ack_cannot_revive_finished_job_while_next_job_runs(ring_dialog, terminal):
    dialog = ring_dialog.value
    dialog.update_progress(dict(job='drivers', status='running'))
    dialog.update_progress(dict(job='drivers', status=terminal))
    dialog.update_progress(dict(job='apps', status='running'))
    dialog.update_progress(dict(job='drivers', status='running', skip_requested=True))
    assert dialog.running and dialog._states['drivers'] == terminal
    assert dialog.table.item(0, 1).text() == terminal.capitalize()
    assert not dialog.rings['drivers'].active and not dialog.rings['drivers'].timer.isActive()
    assert dialog.rings['apps'].active and dialog.rings['apps'].timer.isActive()
    assert not dialog.skip_buttons['drivers'].isEnabled()
    dialog.begin(JOBS)  # A genuine new pass explicitly resets the terminal gate.
    dialog.update_progress(dict(job='drivers', status='running'))
    assert dialog.rings['drivers'].active and dialog.rings['drivers'].timer.isActive()
