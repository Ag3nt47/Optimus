import copy
import os
import threading
from types import SimpleNamespace
from unittest.mock import Mock

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
import pytest
from PySide6.QtWidgets import QApplication
from optimus.ui import OptimusWindow, WorkflowWorker
from optimus.unattended_configuration import approve_unattended_settings
from optimus.workflow import Preferences


@pytest.fixture
def saved():
    return approve_unattended_settings(Preferences.validate(dict(Preferences.safe_defaults(),
        setup_version=2, unattended_auto_clean_enabled=True, browser_cache_enabled=True)))


@pytest.fixture
def app():
    return QApplication.instance() or QApplication([])


@pytest.mark.parametrize('policy,expected', [('skip_open', False), ('close_gracefully', True)])
def test_unattended_worker_uses_saved_choice_without_constructing_question_bridge(app, saved, policy, expected):
    saved = approve_unattended_settings(dict(saved, unattended_browser_policy=policy))
    answers = []
    def optimize(settings, **kwargs):
        answers.append(kwargs['browser_prompt']({'browser': 'Fixture'}))
        return {'status': 'completed'}
    backend = SimpleNamespace(preferences=lambda:copy.deepcopy(saved), optimize=optimize)
    worker = WorkflowWorker(backend, saved, threading.Event())
    assert worker._browser_prompts is None
    worker.run()
    assert answers == [expected]


def test_changed_saved_scope_revokes_automatic_browser_close(app, saved):
    saved = approve_unattended_settings(dict(saved, unattended_browser_policy='close_gracefully'))
    current = dict(saved, browser_keep_domains=['example.com'])
    answer = []
    def optimize(settings, **kwargs):
        answer.append(kwargs['browser_prompt']({}))
        return {'status': 'completed'}
    worker = WorkflowWorker(SimpleNamespace(preferences=lambda:current, optimize=optimize), saved, threading.Event())
    worker.run()
    assert answer == [False]


def test_valid_saved_unattended_launch_approves_exact_scope_without_dialog(app, tmp_path, saved):
    backend = SimpleNamespace(data_dir=tmp_path, preferences=lambda:copy.deepcopy(saved), close=Mock())
    window = OptimusWindow(backend, smoke_test=True, require_launch_review=True)
    window.smoke_test = False
    window._launch_consent.required = True
    window._ask_launch_review = Mock(side_effect=AssertionError('No launch question in approved unattended mode.'))
    window.start_optimization = Mock()
    window._session_approved_callback = Mock()
    try:
        assert window.review_launch() is True
        assert window.session_approved() is True
        window._ask_launch_review.assert_not_called()
        window.start_optimization.assert_called_once()
        window._session_approved_callback.assert_called_once()
    finally:
        window.smoke_test = True
        window.shutdown()
        window.close()


def test_unattended_results_retain_findings_without_questions(app, tmp_path, saved):
    backend = SimpleNamespace(data_dir=tmp_path, preferences=lambda:copy.deepcopy(saved), close=Mock())
    window = OptimusWindow(backend, smoke_test=True)
    report = {'id': 'a' * 32, 'status': 'completed', 'cancelled': False,
              'security_review': {'recommendations': [{'id': 'manual-only'}]},
              'process_review': {'actions': [{'id': 'unapproved-process'}]}}
    try:
        window._prepare_post_reviews(report, saved)
        assert window._pending_post_review is None
        assert report['security_review']['recommendations'] == [{'id': 'manual-only'}]
        assert report['process_review']['actions'] == [{'id': 'unapproved-process'}]
    finally:
        window.shutdown()
        window.close()
