"""Three persistent worker cards and bounded result rendering use fixture data only."""
import copy
import os
from pathlib import Path
import threading
import time
from types import SimpleNamespace
from unittest.mock import Mock

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

import pytest
from PySide6.QtCore import QCoreApplication, QEvent
from PySide6.QtGui import QFontDatabase
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QWidget

from optimus.automation import CURRENT_SETUP_VERSION
from optimus.ui import OptimizationProgressWindow, OptimusWindow, DetailedSettingsWindow, _report_preview
from optimus.widgets.theme import build_qss
from optimus.widgets.header_controls import PanelRevealOverlay
from optimus.workflow import DEFAULTS


@pytest.fixture(scope='module')
def app():
    return QApplication.instance() or QApplication([])


@pytest.fixture(autouse=True)
def fonts(app):
    loaded = []
    if os.name == 'nt' and not QFontDatabase.families():
        for name in ('segoeui.ttf', 'seguisb.ttf'):
            path = Path(os.environ.get('SystemRoot', r'C:\Windows')) / 'Fonts' / name
            if path.is_file():
                identity = QFontDatabase.addApplicationFont(str(path))
                if identity >= 0:
                    loaded.append(identity)
    yield
    for identity in loaded:
        QFontDatabase.removeApplicationFont(identity)


@pytest.fixture
def progress(app):
    owner = QWidget()
    owner.setStyleSheet(build_qss())
    value = OptimizationProgressWindow(owner, threading.Event())
    value.show()
    app.processEvents()
    yield value
    value.completed = True
    value.spinner.stop()
    value.close()
    owner.close()
    value.deleteLater()
    owner.deleteLater()
    QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)
    app.processEvents()


def jobs(app_status='running', driver_status='pending'):
    return [{'id': 'software_updates', 'status': app_status,
             'description': 'Updating approved app Fixture.App', 'current_package': 'Fixture.App',
             'current': 1, 'total': 7, 'elapsed_seconds': 125},
            {'id': 'driver_updates', 'status': driver_status,
             'description': 'Queued behind application updates' if driver_status == 'pending' else 'Installing approved Fixture GPU driver',
             'current_package': 'Fixture GPU driver', 'current': 0, 'total': 2, 'elapsed_seconds': 0}]


def test_four_worker_cards_exist_before_any_job_and_disabled_modules_are_honest(progress):
    assert set(progress.worker_cards) == {'maintenance', 'software_updates', 'driver_updates', 'security_scan'}
    assert [card.heading.text() for card in progress.worker_cards.values()] == ['Maintenance', 'App updates', 'Driver updates', 'Virus & malware scan']
    assert all(card.isVisible() for card in progress.worker_cards.values())
    assert all(progress.worker_cards[key].status.text() == 'Not started'
        for key in ('maintenance', 'software_updates', 'driver_updates'))
    assert progress.worker_cards['security_scan'].status.text() == 'Not selected'
    settings = {'software_updates_enabled': True, 'driver_updates_enabled': False}
    progress.configure_workers(settings)
    assert progress.worker_cards['software_updates'].status.text() == 'Not started'
    driver = progress.worker_cards['driver_updates']
    assert driver.status.text() == 'Not selected'
    assert driver.progress.maximum() > 0  # No decorative indefinite activity for an idle worker.
    assert settings == {'software_updates_enabled': True, 'driver_updates_enabled': False}


def test_foreground_and_two_update_jobs_show_independent_observed_status(progress):
    progress.update_progress({'stage': 'Review background processes', 'description': 'Read-only fixture review.',
        'percentage': 38, 'jobs': jobs()})
    maintenance, app, driver = (progress.worker_cards[key] for key in ('maintenance', 'software_updates', 'driver_updates'))
    assert maintenance.status.text() == 'Running'
    assert maintenance.details.text() == 'Review background processes'
    assert maintenance.progress.value() == 38
    assert app.status.text() == 'Running' and app.progress.value() == 1 and app.progress.maximum() == 7
    assert 'Fixture.App' in app.details.text() and app.elapsed.text() == 'Elapsed: 2m 05s'
    assert driver.status.text() == 'Queued'
    assert 'Queued behind application updates' in driver.details.text()
    assert driver.progress.value() == 0 and driver.progress.maximum() == 2
    assert progress.title.text() == 'OPTIMIZATION IN PROGRESS'


@pytest.mark.parametrize('identity,noun', [('driver_updates', 'driver'), ('software_updates', 'app')])
def test_completed_discovery_cards_show_zero_offers_and_failed_searches_stay_unknown(progress, identity, noun):
    progress.update_progress({'stage': 'Continue maintenance', 'percentage': 40,
        'jobs': [{'id': identity, 'status': 'completed', 'description': 'Fixture catalog search succeeded; no applicable offers.',
                  'current': 0, 'total': 0, 'scan_label': f'0 {noun} updates found', 'elapsed_seconds': 46}]})
    card = progress.worker_cards[identity]
    assert card.status.text() == 'Completed'
    assert card.progress.format() == f'0 {noun} updates found'
    assert card.elapsed.text() == 'Elapsed: 46s'
    progress.update_progress({'stage': 'Continue maintenance', 'percentage': 41,
        'jobs': [{'id': identity, 'status': 'needs_review', 'description': 'Fixture catalog search failed; availability unknown.',
                  'current': 0, 'total': 0, 'scan_label': 'Availability not confirmed', 'elapsed_seconds': 46}]})
    assert card.status.text() == 'Needs review'
    assert card.progress.format() == 'Availability not confirmed'
    assert 'failed' in card.details.text()


def test_settings_driver_failure_does_not_describe_a_zero_offer_success():
    panel = SimpleNamespace(driver_updates_table=Mock(), driver_tabs=Mock(), driver_update_note=Mock())
    DetailedSettingsWindow._driver_updates_ready(panel, {'resultCode': 4, 'updates': [], 'error': 'Fixture network error'})
    text = panel.driver_update_note.setText.call_args.args[0]
    assert 'incomplete' in text and 'could not be verified' in text
    assert '0 driver candidates' not in text and 'No applicable drivers' not in text


def test_driver_lane_wait_and_handoff_preserve_cards_and_cancel(progress):
    state = {'stage': 'Waiting for background updates', 'waiting_for': 'installer_updates',
        'active_job_id': 'driver_updates', 'next_stage': 'Check Windows security updates', 'percentage': 57,
        'jobs': jobs('completed', 'running')}
    progress.update_progress(state)
    assert progress.title.text() == 'WAITING FOR BACKGROUND UPDATES'
    assert progress.worker_cards['maintenance'].status.text() == 'Waiting for update lane'
    assert 'Check Windows security updates' in progress.worker_cards['maintenance'].details.text()
    assert progress.worker_cards['software_updates'].status.text() == 'Completed'
    assert progress.worker_cards['driver_updates'].status.text() == 'Running'
    assert 'Fixture GPU driver' in progress.worker_cards['driver_updates'].details.text()
    assert progress.progress.value() == 57
    progress.cancel_button.click()
    progress.update_progress(state)
    assert progress.cancel.is_set() and 'current background update must return' in progress.description.text()
    progress.update_progress({'stage': 'Verify maintenance outcomes', 'percentage': 90,
        'jobs': jobs('completed', 'completed')})
    assert progress.title.text() == 'OPTIMIZATION IN PROGRESS'
    assert progress.worker_cards['driver_updates'].status.text() == 'Completed'
    progress.finish({'status': 'completed_with_errors', 'stages': []})
    assert all(card.isVisible() for card in progress.worker_cards.values())
    assert progress.worker_cards['maintenance'].status.text() == 'Completed; review findings'


@pytest.mark.parametrize('dimensions', [(850, 650), (1024, 768)])
def test_three_cards_and_cancel_fit_compact_progress_window(progress, app, dimensions):
    progress.resize(*dimensions)
    progress.update_progress({'stage': 'Waiting for background updates', 'waiting_for': 'installer_updates',
        'percentage': 42, 'description': 'Independent work is complete; conflicting maintenance waits for the installer queue.',
        'jobs': jobs()})
    app.processEvents()
    assert (progress.width(), progress.height()) == dimensions
    for card in progress.worker_cards.values():
        assert not card.visibleRegion().isEmpty()
        assert card.width() >= 180
        assert card.geometry().right() < progress.width()
        assert card.geometry().bottom() < progress.height()
    assert progress.cancel_button.geometry().bottom() < progress.height()


def test_large_report_is_a_bounded_preview_and_keeps_full_journal_path(progress):
    rows = [{'path': 'fixture/' + 'a' * 480 + str(index), 'status': 'locked'} for index in range(10000)]
    report = {'status': 'completed_with_errors', 'stages': [{'title': 'Fixture item outcomes', 'result': {'items': rows}}],
              'log_path': 'D:/fixture/runs/full-report.json',
              'final_verification': {'status': 'needs_review', 'counts': {'needs_review': 1},
                                     'stages': [{'title': 'Fixture stage', 'status': 'needs_review'}]}}
    progress.finish(report)
    shown = progress.log.toPlainText()
    assert len(shown) < 51000 and len(rows) == 10000
    assert 'Display preview shortened' in shown and 'D:/fixture/runs/full-report.json' in shown
    assert 'needs_review' in progress.verification_details.toPlainText()
    assert 'Needs review' in progress.verification_summary.text()
    assert rows[-1]['path'].endswith('9999')  # Rendering did not truncate the saved source data.


def test_sequential_update_events_populate_the_same_cards_without_invented_counts(progress):
    progress.configure_workers({'parallel_app_updates_enabled': False, 'software_updates_enabled': True,
                                'driver_updates_enabled': True})
    progress.update_progress({'stage': 'Review or update configured software', 'stage_status': 'running',
        'description': 'Updating the current approved app.', 'percentage': 30})
    assert progress.worker_cards['software_updates'].status.text() == 'Running'
    assert 'current approved app' in progress.worker_cards['software_updates'].details.text()
    assert progress.worker_cards['software_updates'].progress.maximum() == 1
    progress.update_progress({'stage': 'Install approved driver updates', 'stage_status': 'failed',
        'description': 'Fixture driver install needs review.', 'percentage': 50})
    assert progress.worker_cards['driver_updates'].status.text() == 'Failed'
    progress.finish({'status': 'completed_with_errors', 'stages': [
        {'title': 'Review or update configured software', 'status': 'completed', 'result': {}},
        {'title': 'Install approved driver updates', 'status': 'completed',
         'result': {'skipped': True, 'stop_reason': 'A fixture restart is required.'}}]})
    assert progress.worker_cards['software_updates'].status.text() == 'Completed'
    assert progress.worker_cards['driver_updates'].status.text() == 'Deferred'
    assert 'restart is required' in progress.worker_cards['driver_updates'].details.text()


def test_spinner_stays_inside_progress_window_and_pauses_while_hidden(progress, app, monkeypatch):
    monkeypatch.setattr('optimus.widgets.header_controls.motion_allowed', lambda *args: True)
    assert progress.isAncestorOf(progress.spinner)
    assert progress.spinner.window() is progress
    progress.spinner.start('Fixture activity')
    app.processEvents()
    assert progress.spinner._ring._timer.isActive()
    progress.hide()
    app.processEvents()
    assert not progress.spinner._ring._timer.isActive()
    assert not progress._worker_elapsed_timer.isActive()
    before = progress.spinner._ring._angle
    QTest.qWait(80)
    assert progress.spinner._ring._angle == before
    progress.show()
    app.processEvents()
    assert progress.spinner._ring._timer.isActive()
    assert progress._worker_elapsed_timer.isActive()


def test_elapsed_card_updates_without_host_sampling_and_freezes_after_completion(progress, app):
    state = {'stage': 'Long read-only inventory', 'percentage': 30,
        'jobs': [{'id': 'driver_updates', 'status': 'running', 'description': 'Fixture driver installer',
                  'current': 0, 'total': 2, 'elapsed_seconds': 0}]}
    progress.update_progress(state)
    card = progress.worker_cards['driver_updates']
    QTest.qWait(1200)
    progress._refresh_worker_elapsed()
    assert card.elapsed.text() not in ('Elapsed: 0s', 'Elapsed: —')
    assert card.progress.value() == 0 and card.status.text() == 'Running'
    progress.update_progress(dict(state, jobs=[{'id': 'driver_updates', 'status': 'completed',
        'description': 'Fixture driver checks returned.', 'current': 2, 'total': 2}]))
    frozen = card.elapsed.text()
    QTest.qWait(1100)
    progress._refresh_worker_elapsed()
    assert card.elapsed.text() == frozen and card.status.text() == 'Completed'


def test_reveal_cleanup_accepts_a_frame_already_destroyed_with_its_dialog(app):
    owner = QWidget()
    overlay = PanelRevealOverlay(owner)
    frame = QWidget(owner)
    overlay._frame = frame
    frame.deleteLater()
    QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)
    overlay._finish()
    assert overlay._frame is None and overlay._target is None
    overlay.shutdown()
    owner.close()
    owner.deleteLater()
    app.processEvents()


def test_queued_window_motion_sync_is_cancelled_when_its_canvas_is_deleted(app, tmp_path):
    backend = SimpleNamespace(data_dir=tmp_path, preferences=lambda: dict(DEFAULTS))
    window = OptimusWindow(backend, smoke_test=True)
    callback = Mock()
    window.canvas.sync_motion = callback
    window.changeEvent(QEvent(QEvent.WindowStateChange))
    window.deleteLater()
    QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)
    app.processEvents()
    callback.assert_not_called()


def test_preview_bounds_nested_cyclic_and_long_text_without_mutating_input():
    value = {'status': 'needs_review', 'text': 'x' * 1000000}
    value['cycle'] = value
    shown = _report_preview(value)
    assert len(shown) < 51000 and 'display preview' in shown.lower()
    assert len(value['text']) == 1000000 and value['cycle'] is value


def test_finished_progress_windows_are_retired_between_runs(app, tmp_path):
    settings = dict(DEFAULTS, setup_version=CURRENT_SETUP_VERSION,
                    reduce_motion=True, post_process_review_enabled=False, auto_continue_fixes_enabled=False)
    report = {'status': 'completed', 'stages': [], 'security_review': {}, 'process_review': {}}
    backend = SimpleNamespace(data_dir=tmp_path, preferences=lambda: copy.deepcopy(settings),
        optimize=Mock(return_value=report), available_fixes=Mock(return_value={'actions': [], 'manual_items': []}),
        apply_available_fixes=Mock())
    window = OptimusWindow(backend, smoke_test=True)
    window.smoke_test = False
    try:
        for _ in range(4):
            window.start_optimization()
            deadline = time.monotonic() + 5
            while window.busy and time.monotonic() < deadline:
                app.processEvents()
                QTest.qWait(5)
            assert not window.busy
            window._progress_window.close()
            QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)
            app.processEvents()
            assert len(window.findChildren(OptimizationProgressWindow)) == 1
        assert backend.optimize.call_count == 4
    finally:
        window.pool.waitForDone(5000)
        window.smoke_test = True
        window.shutdown()
        window.close()
        window.deleteLater()
        app.processEvents()


def test_unfinished_progress_is_never_retired_to_start_another_worker(app, tmp_path):
    settings = dict(DEFAULTS, setup_version=CURRENT_SETUP_VERSION)
    backend = SimpleNamespace(data_dir=tmp_path, preferences=lambda: settings, optimize=Mock())
    window = OptimusWindow(backend, smoke_test=True)
    window.smoke_test = False
    current = window._progress_window = OptimizationProgressWindow(window, threading.Event())
    try:
        window.start_optimization()
        assert window._progress_window is current and not current.completed
        backend.optimize.assert_not_called()
        assert 'previous progress window' in window.status.text()
    finally:
        current.completed = True
        current.spinner.stop()
        window.smoke_test = True
        window.shutdown()
        window.close()
        window.deleteLater()
        app.processEvents()
