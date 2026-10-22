"""Explicit completed-result batch actions use fixture workers, never host maintenance."""
import copy
import os
import threading
import time
from types import SimpleNamespace
from unittest.mock import Mock

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

import pytest
from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from optimus.automation import CURRENT_SETUP_VERSION
from optimus.native_ui import NativeOptimusWindow
from optimus.ui import OptimusWindow
from optimus.workflow import DEFAULTS


def report(identifier='original-run', **changes):
    value = {'id': identifier, 'status': 'completed', 'stages': [], 'cancelled': False,
        'security_review': {'recommendations': [{'id': 'manual', 'title': 'Review vendor patch'}]},
        'process_review': {'complete': True, 'actions': []},
        'final_verification': {'status': 'unverified', 'counts': {'unverified': 1}}}
    value.update(changes)
    return value


def plan():
    return {'actions': [{'id': 'firewall', 'title': 'Enable Windows Firewall', 'kind': 'security'}],
        'manual_items': [{'id': 'reboot', 'title': 'Restart Windows',
            'reason': 'A restart requires your manual decision.'}], 'summary': 'Review the listed scope.'}


def wait_until(app, predicate):
    deadline = time.monotonic() + 5
    while not predicate() and time.monotonic() < deadline:
        app.processEvents()
        QTest.qWait(5)
    app.processEvents()
    assert predicate(), 'Fixture UI did not reach the expected state.'


@pytest.fixture(scope='module')
def app():
    return QApplication.instance() or QApplication([])


@pytest.fixture(params=[OptimusWindow, NativeOptimusWindow], ids=['windows', 'native'])
def window(request, tmp_path, app):
    settings = dict(copy.deepcopy(DEFAULTS), setup_version=CURRENT_SETUP_VERSION,
        browser_cache_enabled=False, browser_history_enabled=False, browser_cookies_enabled=False,
        browser_download_history_enabled=False, post_process_review_enabled=True)
    original = report()
    backend = SimpleNamespace(data_dir=tmp_path, preferences=lambda: copy.deepcopy(settings),
        optimize=Mock(return_value=original), available_fixes=Mock(return_value=plan()),
        apply_available_fixes=Mock(return_value=report('fixes-run', parent_run_id=original['id'],
            counts={'completed': 1}, summary='1 action/check completed.')))
    value = request.param(backend, smoke_test=True)
    value.smoke_test = False
    value.show()
    value._offer_post_optimization_reviews = Mock()
    yield value
    value._cancel.set()
    value.pool.waitForDone(5000)
    app.processEvents()
    value._pending_post_review = None
    value._pending_auto_fixes = None
    value._post_review_active = False
    value._quitting = True
    if value._progress_window:
        value._progress_window.close()
    value.shutdown()
    value.close()
    value.deleteLater()
    app.processEvents()


def complete_initial(window, app):
    QTest.mouseClick(window.main_buttons[0], Qt.LeftButton)
    wait_until(app, lambda: window._progress_window is not None and not window.busy)
    return window._progress_window


def test_completed_scan_only_previews_exact_batch_until_explicit_click(window, app):
    progress = complete_initial(window, app)
    assert len(window.main_buttons) == 3
    window.backend.apply_available_fixes.assert_not_called()
    window._offer_post_optimization_reviews.assert_not_called()
    window.backend.optimize.assert_called_once()
    assert progress.auto_continue_button.isVisible() and progress.auto_continue_button.isEnabled()
    assert progress.auto_continue_details.isReadOnly()
    assert 'Enable Windows Firewall' in progress.auto_continue_details.toPlainText()
    assert 'Restart Windows: A restart requires your manual decision.' in progress.auto_continue_details.toPlainText()
    assert '1 automatic action available; 1 item requires manual review' in progress.auto_continue_summary.text()
    assert 'approves the automatic actions listed below' in progress.auto_continue_summary.text()
    assert not progress.auto_continue_button.autoDefault() and not progress.auto_continue_button.isDefault()
    assert progress.cancel_button.text() == 'Continue to recommendations'


def test_click_uses_separate_worker_once_and_keeps_manual_continue(window, app):
    progress = complete_initial(window, app)
    entered, release = threading.Event(), threading.Event()
    worker_threads = []
    fresh = report('fixes-run', parent_run_id='original-run', status='completed_with_errors',
        counts={'completed': 1, 'blocked': 1, 'manual_action_required': 1, 'uncertain': 1},
        summary='1 action/check completed; 1 blocked; 1 manual action required; 1 uncertain.')
    def apply(parent, settings, *, progress_callback, cancel_event):
        worker_threads.append(threading.get_ident())
        entered.set()
        progress_callback({'stage': 'Fixture follow-up', 'percentage': 50,
            'description': 'Fixture actions only', 'log': 'Fixture batch journal entry'})
        assert release.wait(3)
        return fresh
    window.backend.apply_available_fixes.side_effect = apply
    QTest.mouseClick(progress.auto_continue_button, Qt.LeftButton)
    wait_until(app, entered.is_set)
    try:
        assert window.busy and not progress.completed
        assert not progress.auto_continue_button.isVisible()
        assert not window.main_buttons[0].isEnabled()
        assert not window._post_review_active and window._pending_post_review is None
        progress.autoContinueRequested.emit()
        window._start_auto_continue(progress)
        window.start_optimization()
        assert window.backend.apply_available_fixes.call_count == 1
    finally:
        release.set()
    wait_until(app, lambda: not window.busy)
    assert len(worker_threads) == 1 and worker_threads[0] != threading.get_ident()
    window.backend.optimize.assert_called_once()
    window.backend.available_fixes.assert_called_once()
    window.backend.apply_available_fixes.assert_called_once()
    assert window.backend.apply_available_fixes.call_args.args[0]['id'] == 'original-run'
    assert 'Fixture batch journal entry' in progress.log.toPlainText()
    assert '"id": "original-run"' in progress.log.toPlainText()
    assert '"id": "fixes-run"' in progress.log.toPlainText()
    assert '1 blocked' in progress.auto_continue_summary.text()
    assert '1 uncertain' in progress.auto_continue_summary.text()
    assert 'Not fully verified' in progress.verification_summary.text()
    assert progress.cancel_button.text() == 'Continue to recommendations'
    assert progress.auto_continue_button.isHidden()
    window._start_auto_continue(progress)
    window.backend.apply_available_fixes.assert_called_once()
    window._offer_post_optimization_reviews.assert_not_called()
    QTest.mouseClick(progress.cancel_button, Qt.LeftButton)
    wait_until(app, lambda: window._offer_post_optimization_reviews.called)
    window._offer_post_optimization_reviews.assert_called_once_with(fresh,
        window.backend.preferences())


@pytest.mark.parametrize('dismiss', ['close', 'reject'])
def test_dismissal_skips_automatic_actions_and_pending_reviews(window, app, dismiss):
    progress = complete_initial(window, app)
    getattr(progress, dismiss)()
    app.processEvents()
    assert window._pending_auto_fixes is None
    assert window._pending_post_review is None and not window._post_review_active
    assert not progress.cancel.is_set()
    progress.autoContinueRequested.emit()
    window.backend.apply_available_fixes.assert_not_called()
    window._offer_post_optimization_reviews.assert_not_called()


@pytest.mark.parametrize('cancel', ['button', 'close', 'escape'])
def test_active_followup_is_cancellable_without_closing_results(window, app, cancel):
    progress = complete_initial(window, app)
    entered = threading.Event()
    def apply(parent, settings, *, progress_callback, cancel_event):
        entered.set()
        assert cancel_event.wait(3)
        return report('cancelled-fixes', cancelled=True, status='cancelled',
            final_verification=None, summary='Cancelled after the current stage.', counts={'completed': 0})
    window.backend.apply_available_fixes.side_effect = apply
    QTest.mouseClick(progress.auto_continue_button, Qt.LeftButton)
    wait_until(app, entered.is_set)
    if cancel == 'button':
        QTest.mouseClick(progress.cancel_button, Qt.LeftButton)
    elif cancel == 'escape':
        QTest.keyClick(progress, Qt.Key_Escape)
    else:
        progress.close()
    wait_until(app, lambda: not window.busy)
    assert progress.isVisible() and progress.completed
    assert progress.cancel.is_set()
    assert progress.auto_continue_button.isHidden()
    assert 'CANCELLED' in progress.stage.text()
    assert window._pending_post_review is None and not window._post_review_active
    window.backend.apply_available_fixes.assert_called_once()
    window.backend.optimize.assert_called_once()
    window._offer_post_optimization_reviews.assert_not_called()


@pytest.mark.parametrize('mode', ['no_actions', 'legacy_backend', 'preview_error'])
def test_no_available_batch_has_disabled_control_with_visible_reason(window, app, mode):
    if mode == 'no_actions':
        window.backend.available_fixes.return_value = dict(plan(), actions=[])
    elif mode == 'legacy_backend':
        del window.backend.available_fixes
        del window.backend.apply_available_fixes
    else:
        window.backend.available_fixes.side_effect = RuntimeError('Fixture preview unavailable')
    progress = complete_initial(window, app)
    assert progress.auto_continue_button.isVisible() and not progress.auto_continue_button.isEnabled()
    assert 'No automatic fixes are available' in progress.auto_continue_summary.text()
    assert window._pending_auto_fixes is None
    if mode == 'preview_error':
        assert 'Fixture preview unavailable' in progress.auto_continue_summary.text()
    progress.autoContinueRequested.emit()
    if mode != 'legacy_backend':
        window.backend.apply_available_fixes.assert_not_called()


@pytest.mark.parametrize('outcome', ['cancelled', 'failed', 'error'])
def test_initial_failure_or_cancellation_never_offers_batch(window, app, outcome):
    if outcome == 'error':
        window.backend.optimize.side_effect = RuntimeError('Fixture initial failure')
    else:
        window.backend.optimize.return_value = report(status=outcome, cancelled=outcome == 'cancelled')
    progress = complete_initial(window, app)
    assert progress.auto_continue_button.isHidden()
    assert window._pending_auto_fixes is None
    window.backend.available_fixes.assert_not_called()
    window.backend.apply_available_fixes.assert_not_called()


def test_followup_worker_error_cannot_be_retried(window, app):
    progress = complete_initial(window, app)
    window.backend.apply_available_fixes.side_effect = RuntimeError('Fixture follow-up failure')
    QTest.mouseClick(progress.auto_continue_button, Qt.LeftButton)
    wait_until(app, lambda: not window.busy)
    assert 'Fixture follow-up failure' in progress.description.text()
    assert progress.auto_continue_button.isHidden()
    assert window._pending_post_review is None and window._pending_auto_fixes is None
    assert not window._post_review_active
    progress.autoContinueRequested.emit()
    window.backend.apply_available_fixes.assert_called_once()


def test_manual_continue_discards_the_batch_and_dismiss_can_cancel_queued_review(window, app):
    progress = complete_initial(window, app)
    QTest.mouseClick(progress.cancel_button, Qt.LeftButton)
    assert window._pending_auto_fixes is None
    assert progress.auto_continue_button.isHidden()
    progress.close()
    app.processEvents()
    window._offer_post_optimization_reviews.assert_not_called()
    window.backend.apply_available_fixes.assert_not_called()


def test_new_scan_discards_old_result_actions_and_stale_window_signals(window, app):
    old = complete_initial(window, app)
    old.close()
    second = complete_initial(window, app)
    assert second is not old
    pending = window._pending_auto_fixes
    old.autoContinueRequested.emit()
    old.resultsAcknowledged.emit()
    old.resultsDismissed.emit()
    assert window._pending_auto_fixes is pending
    window.backend.apply_available_fixes.assert_not_called()
    window._offer_post_optimization_reviews.assert_not_called()


def test_batch_scope_and_both_controls_fit_completed_results_at_850_by_650(window, app):
    value = plan()
    value['actions'] = [{'id': str(index), 'title': title, 'kind': 'security'} for index, title in enumerate([
        'Enable Windows Firewall', 'Enable Microsoft Defender real-time protection',
        'Update Microsoft Defender security intelligence', 'Run a Microsoft Defender quick scan',
        'Recheck and apply saved software update selections',
        'Recheck and install saved Windows security update selections',
        'Recheck and install saved driver update selections'])]
    window.backend.available_fixes.return_value = value
    progress = complete_initial(window, app)
    progress.resize(850, 650)
    app.processEvents()
    assert progress.height() == 650
    assert progress.auto_continue_details.isVisible()
    assert progress.auto_continue_summary.isVisible()
    assert progress.auto_continue_button.geometry().bottom() < 650
    assert progress.cancel_button.geometry().bottom() < 650
    assert progress.processes.isHidden()
    assert progress.log.isVisible() and progress.verification_details.isVisible()
    for action in value['actions']:
        assert action['title'] in progress.auto_continue_details.toPlainText()


def test_saved_automatic_choice_continues_once_without_a_second_click(window, app):
    settings = dict(window.backend.preferences(), auto_continue_fixes_enabled=True)
    window.backend.preferences = Mock(return_value=settings)
    QTest.mouseClick(window.main_buttons[0], Qt.LeftButton)
    wait_until(app, lambda: window.backend.apply_available_fixes.called and not window.busy)
    app.processEvents()
    window.backend.apply_available_fixes.assert_called_once()
    window.backend.optimize.assert_called_once()
    window.backend.available_fixes.assert_called_once()
    assert window._auto_continue_attempted
    assert 'SAVED AUTOMATIC FIX CHOICE' in window._progress_window.log.toPlainText()
    assert window._progress_window.progress.format() == 'Follow-up %p%'
    window._offer_post_optimization_reviews.assert_not_called()


def test_saved_opt_out_keeps_available_fixes_as_a_button_only(window, app):
    window.backend.preferences = Mock(return_value=dict(window.backend.preferences(), auto_continue_fixes_enabled=False))
    progress = complete_initial(window, app)
    app.processEvents()
    assert progress.auto_continue_button.isEnabled()
    window.backend.apply_available_fixes.assert_not_called()


@pytest.mark.parametrize('dismiss', ['close', 'reject'])
def test_dismiss_before_queued_automatic_continuation_cancels_the_saved_choice(window, app, dismiss):
    from optimus.ui import OptimizationProgressWindow
    settings = dict(window.backend.preferences(), auto_continue_fixes_enabled=True)
    window.backend.preferences = Mock(return_value=settings)
    window.busy = True
    window._worker = SimpleNamespace(settings=settings, _browser_prompts=None)
    progress = window._progress_window = OptimizationProgressWindow(window, threading.Event())
    progress.resultsDismissed.connect(window._dismiss_post_optimization_reviews)
    progress.show()
    window._workflow_result(report())
    window._workflow_finished()
    getattr(progress, dismiss)()
    app.processEvents()
    app.processEvents()
    window.backend.apply_available_fixes.assert_not_called()
    assert window._pending_auto_fixes is None and not window._post_review_active


@pytest.mark.parametrize('failure', ['cancelled', 'error'])
def test_saved_automatic_choice_never_continues_after_cancellation_or_error(window, app, failure):
    window.backend.preferences = Mock(return_value=dict(window.backend.preferences(), auto_continue_fixes_enabled=True))
    if failure == 'cancelled':
        window.backend.optimize.return_value = report(cancelled=True, status='cancelled')
    else:
        window.backend.optimize.side_effect = RuntimeError('Fixture initial scan error')
    complete_initial(window, app)
    app.processEvents()
    window.backend.apply_available_fixes.assert_not_called()
