"""Browser questions use fixture callbacks only; no browser is closed by these tests."""
import threading
import time
from unittest.mock import Mock

import pytest
from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication, QMessageBox, QWidget

from optimus.browser_prompts import BrowserPromptBridge
from optimus.ui import WorkflowWorker


@pytest.fixture(scope='module')
def qapp():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def bridges(qapp):
    created = []

    def create(**kwargs):
        bridge = BrowserPromptBridge(**kwargs)
        created.append(bridge)
        return bridge

    yield create
    for bridge in created:
        bridge.shutdown()
        bridge.deleteLater()
    qapp.processEvents()


def start_request(bridge, info=None, cancel=None):
    results = []
    info = info or {'browser_id': 'chrome', 'browser': 'Google Chrome', 'processes': [{'pid': 123, 'name': 'chrome.exe'}]}
    thread = threading.Thread(target=lambda: results.append(bridge.request(info, cancel)))
    thread.start()
    return thread, results


def finish(thread, qapp, timeout=2):
    deadline = time.monotonic() + timeout
    while thread.is_alive() and time.monotonic() < deadline:
        qapp.processEvents()
        thread.join(0.005)
    assert not thread.is_alive(), 'Browser question did not release its worker.'
    qapp.processEvents()


def test_yes_no_routes_each_browser_on_application_thread(qapp, bridges):
    gui_thread = threading.get_ident()
    seen = []

    def ask(info):
        seen.append((info['browser_id'], threading.get_ident()))
        return info['browser_id'] == 'chrome'

    bridge = bridges(ask=ask)
    chrome, yes = start_request(bridge)
    finish(chrome, qapp)
    firefox, no = start_request(bridge, {'browser_id': 'firefox', 'browser': 'Firefox'})
    finish(firefox, qapp)
    assert yes == [True] and no == [False]
    assert seen == [('chrome', gui_thread), ('firefox', gui_thread)]


def test_approval_is_not_remembered_for_later_run(qapp, bridges):
    ask = Mock(side_effect=[True, False])
    bridge = bridges(ask=ask)
    first, approved = start_request(bridge)
    finish(first, qapp)
    second, denied = start_request(bridge)
    finish(second, qapp)
    assert approved == [True] and denied == [False]
    assert ask.call_count == 2


def test_cancelled_before_request_never_asks(qapp, bridges):
    ask = Mock(return_value=True)
    cancel = threading.Event()
    cancel.set()
    thread, results = start_request(bridges(ask=ask), cancel=cancel)
    finish(thread, qapp)
    assert results == [False]
    ask.assert_not_called()


def test_cancellation_before_queued_slot_prevents_dialog(qapp, bridges):
    ask = Mock(return_value=True)
    cancel = threading.Event()
    bridge = bridges(ask=ask)
    thread, results = start_request(bridge, cancel=cancel)
    cancel.set()
    thread.join(0.5)
    finish(thread, qapp)
    assert results == [False]
    ask.assert_not_called()


def test_cancel_during_modal_question_closes_it_and_releases_worker(qapp, bridges):
    bridge = bridges()
    cancel = threading.Event()
    thread, results = start_request(bridge, cancel=cancel)
    QTimer.singleShot(30, cancel.set)
    finish(thread, qapp)
    assert results == [False]
    assert not any(isinstance(widget, QMessageBox) and widget.isVisible()
                   for widget in qapp.topLevelWidgets())


def test_cancel_wins_even_if_question_returns_yes(qapp, bridges):
    cancel = threading.Event()

    def ask(_info):
        cancel.set()
        return True

    thread, results = start_request(bridges(ask=ask), cancel=cancel)
    finish(thread, qapp)
    assert results == [False]


def test_no_gui_response_times_out_without_deadlock_or_late_dialog(qapp, bridges):
    ask = Mock(return_value=True)
    thread, results = start_request(bridges(ask=ask, response_timeout=0.03))
    # Deliberately do not process the GUI queue until the worker has timed out.
    thread.join(0.5)
    assert not thread.is_alive()
    assert results == [False]
    qapp.processEvents()
    ask.assert_not_called()


def test_shutdown_releases_queued_request_without_gui_processing(qapp, bridges):
    ask = Mock(return_value=True)
    bridge = bridges(ask=ask)
    thread, results = start_request(bridge)
    bridge.shutdown()
    thread.join(0.5)
    finish(thread, qapp)
    assert results == [False]
    ask.assert_not_called()


def test_exception_in_dialog_fails_closed(qapp, bridges):
    thread, results = start_request(bridges(ask=Mock(side_effect=RuntimeError('Fixture failure'))))
    finish(thread, qapp)
    assert results == [False]


def test_gui_thread_request_is_refused_without_blocking(qapp, bridges):
    ask = Mock(return_value=True)
    assert bridges(ask=ask).request({'browser': 'Fixture'}) is False
    ask.assert_not_called()


def test_question_defaults_no_and_explains_scope(qapp, bridges, monkeypatch):
    seen = {}

    def exec_fixture(dialog):
        seen.update(thread=threading.get_ident(), default=dialog.standardButton(dialog.defaultButton()),
                    escape=dialog.standardButton(dialog.escapeButton()), text=dialog.informativeText(),
                    details=dialog.detailedText())
        return QMessageBox.No

    monkeypatch.setattr(QMessageBox, 'exec', exec_fixture)
    thread, results = start_request(bridges())
    finish(thread, qapp)
    assert results == [False]
    assert seen['thread'] == threading.get_ident()
    assert seen['default'] == seen['escape'] == QMessageBox.No
    assert 'windows and profiles' in seen['text']
    assert 'Save unfinished work' in seen['text']
    assert 'sign you out' in seen['text']
    assert 'will not force-terminate' in seen['text']
    assert 'PID 123' in seen['details']


def test_workflow_worker_passes_callback_before_backend_runs(qapp):
    cancel = threading.Event()
    seen = {}

    class Backend:
        def optimize(self, settings, *, progress_callback, cancel_event, browser_prompt):
            seen['worker_thread'] = threading.get_ident()
            seen['cancel'] = cancel_event
            seen['answer'] = browser_prompt({'browser_id': 'firefox', 'browser': 'Firefox'})
            return {'completed': True}

    worker = WorkflowWorker(Backend(), {'browser_cache_enabled': True}, cancel)
    worker._browser_prompts._ask = lambda _info: seen.update(gui_thread=threading.get_ident()) or True
    thread = threading.Thread(target=worker.run)
    thread.start()
    try:
        finish(thread, qapp)
        assert seen['worker_thread'] != threading.get_ident()
        assert seen['gui_thread'] == threading.get_ident()
        assert seen['cancel'] is cancel and seen['answer'] is True
    finally:
        worker._browser_prompts.shutdown()
        worker._browser_prompts.deleteLater()


def test_no_callback_kwarg_when_all_browser_features_disabled(qapp):
    seen = {}

    class StrictBackend:
        def optimize(self, settings, *, progress_callback, cancel_event):
            seen['called'] = True
            return {}

    worker = WorkflowWorker(StrictBackend(), {'browser_cache_enabled': False}, threading.Event())
    worker.run()
    assert seen == {'called': True}
    assert worker._browser_prompts is None


@pytest.mark.parametrize('flag', ['browser_history_enabled', 'browser_cookies_enabled', 'browser_download_history_enabled'])
def test_privacy_only_workflow_still_has_prompt_bridge(qapp, flag):
    backend = Mock()
    worker = WorkflowWorker(backend, {'browser_cache_enabled': False, flag: True}, threading.Event())
    try:
        worker.run()
        assert callable(backend.optimize.call_args.kwargs['browser_prompt'])
    finally:
        worker._browser_prompts.shutdown()
        worker._browser_prompts.deleteLater()


@pytest.fixture
def privacy_panel(qapp):
    from optimus.widgets.privacy_panel import PrivacyPanel
    window = QWidget()
    window.busy = False
    window._run = Mock()
    window._error = Mock()
    window._confirm = Mock(return_value=True)
    panel = PrivacyPanel(window)
    panel.profile.addItem('Fixture Chrome', {'id': 'fixture-profile', 'browser': 'Chrome', 'name': 'Default',
                                          'supportedCategories': ['history', 'cookies']})
    yield panel, window
    panel.shutdown()
    window.deleteLater()
    qapp.processEvents()


def test_manual_privacy_preview_queues_close_question_but_does_not_apply(privacy_panel, qapp):
    panel, window = privacy_panel
    panel.categories['history'].setChecked(True)
    panel._browser_prompts._ask = Mock(return_value=False)
    panel._preview()
    args, kwargs = window._run.call_args
    assert args == ('privacy_scan', 'fixture-profile')
    assert kwargs['categories'] == ['history']
    assert not kwargs['cancel_event'].is_set()
    answers = []
    thread = threading.Thread(target=lambda: answers.append(kwargs['browser_prompt']({'browser': 'Chrome'})))
    thread.start()
    finish(thread, qapp)
    assert answers == [False]
    assert window._run.call_count == 1
    window._confirm.assert_not_called()


def test_skipped_manual_preview_has_no_fake_counts_or_apply(privacy_panel):
    panel, _window = privacy_panel
    panel._show_plan({'skipped': True, 'canApply': False, 'note': 'You chose to leave Chrome running.'})
    assert panel._plan is None and not panel.clear_button.isEnabled()
    assert 'leave Chrome running' in panel.details.toPlainText()
    assert 'Preview expires' not in panel.details.toPlainText()
    assert 'backup estimate' not in panel.details.toPlainText()


def test_manual_privacy_still_requires_separate_clear_confirmation(privacy_panel):
    panel, window = privacy_panel
    panel._show_plan({'canApply': True, 'planId': 'fixture-plan', 'categories': ['history'], 'counts': {'history': 3}})
    window._confirm.return_value = False
    panel._apply()
    assert window._confirm.call_args.kwargs['phrase'] == 'CLEAR'
    window._run.assert_not_called()


def test_manual_privacy_shutdown_cancels_pending_close_question(privacy_panel, qapp):
    panel, window = privacy_panel
    panel.categories['history'].setChecked(True)
    panel._preview()
    kwargs = window._run.call_args.kwargs
    answers = []
    thread = threading.Thread(target=lambda: answers.append(kwargs['browser_prompt']({'browser': 'Chrome'})))
    thread.start()
    panel.shutdown()
    finish(thread, qapp)
    assert answers == [False]
    assert kwargs['cancel_event'].is_set()
