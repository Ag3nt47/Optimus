"""Window activation and guarded UAC handoff use only fixture backends."""
import os
from types import SimpleNamespace
from unittest.mock import Mock, patch

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
import pytest
from PySide6.QtWidgets import QApplication, QDialog

from optimus.native_ui import NativeOptimusWindow
from optimus.ui import OptimusWindow
from optimus.onboarding import InitialSetupWizard


@pytest.fixture(params=[OptimusWindow, NativeOptimusWindow], ids=['windows', 'native'])
def window(request, tmp_path, monkeypatch):
    qapp = QApplication.instance() or QApplication([])
    backend = SimpleNamespace(data_dir=tmp_path, preferences=Mock(return_value={}))
    value = request.param(backend, smoke_test=True)
    value.smoke_test = False
    quit_app = Mock()
    monkeypatch.setattr(qapp, 'quit', quit_app)
    value._test_quit = quit_app
    yield value
    value._settings_window = None
    value._setup_wizard = None
    value._sandbox_window = None
    value._progress_window = None
    value.busy = False
    value._quitting = True
    value.shutdown()
    value.close()
    value.deleteLater()
    qapp.processEvents()


def test_duplicate_optimize_restores_existing_progress_without_a_new_worker(window):
    window.busy = True
    progress = QDialog(window)
    window._progress_window = progress
    original_worker = window._worker = object()
    with patch('optimus.ui.OptimizationProgressWindow') as factory:
        window.start_optimization()
    assert progress.isVisible()
    assert window._worker is original_worker
    assert window._progress_window is progress
    factory.assert_not_called()
    progress.close()
    progress.deleteLater()


def test_activation_restores_active_question_before_progress(window):
    progress = QDialog(window)
    window._progress_window = progress
    window.busy = True
    question = QDialog(progress)
    with patch.object(QApplication, 'activeModalWidget', return_value=question):
        assert window.activate_existing_window()
    assert question.isVisible()
    question.close()
    progress.close()
    question.deleteLater()
    progress.deleteLater()


@pytest.mark.parametrize('started', [True, False])
def test_explicit_uac_handoff_exits_current_gui_only_when_windows_accepts(window, started):
    with patch('optimus.elevation.request_administrator', return_value={'started':started, 'note':'Fixture UAC outcome.'}) as request:
        assert window.restart_as_administrator() is started
    request.assert_called_once_with()
    assert window._quitting is started
    assert window._test_quit.call_count == int(started)


@pytest.mark.parametrize('active', ['workflow', 'pending_bridge', 'settings', 'setup', 'post_review'])
def test_uac_restart_waits_for_operations_and_questions(window, active):
    if active == 'workflow':
        window.backend.workflow = SimpleNamespace(active=True)
    elif active == 'pending_bridge':
        window.backend.bridge = SimpleNamespace(_pending={'fixture':object()})
    elif active == 'settings':
        window._settings_window = Mock(busy=True)
    elif active == 'setup':
        window._setup_wizard = Mock()
    else:
        window._post_review_active = True
    with patch('optimus.elevation.request_administrator') as request, \
         patch.object(window, 'activate_existing_window'):
        assert not window.restart_as_administrator()
    request.assert_not_called()
    window._test_quit.assert_not_called()


def test_uac_restart_keeps_cancelled_unsaved_sandbox_edits(window):
    window._sandbox_window = SimpleNamespace(_unsaved=Mock(return_value=False))
    with patch('optimus.elevation.request_administrator') as request:
        assert not window.restart_as_administrator()
    request.assert_not_called()
    window._test_quit.assert_not_called()


def test_uac_restart_does_not_prompt_an_already_elevated_session(window):
    window.backend.is_admin = Mock(return_value=True)
    with patch('optimus.elevation.request_administrator') as request:
        assert not window.restart_as_administrator()
    request.assert_not_called()
    window._test_quit.assert_not_called()


def test_setup_administrator_guidance_preserves_draft_without_launching_a_child():
    wizard = SimpleNamespace(driver_status=Mock(), backend=SimpleNamespace(elevate=Mock()),
                             _start_probe=Mock(), saved_preferences=None)
    InitialSetupWizard.request_administrator(wizard)
    wizard.backend.elevate.assert_not_called()
    wizard._start_probe.assert_not_called()
    assert wizard.saved_preferences is None
    guidance = wizard.driver_status.setText.call_args.args[0]
    assert 'Save your setup choices' in guidance
    assert 'Settings' in guidance
