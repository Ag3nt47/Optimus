"""Native settings overrides obey the shared per-launch consent gate."""
import copy
import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

import pytest
from PySide6.QtWidgets import QApplication, QDialog

from optimus.native_settings import NativePreferences, full_native_settings
from optimus.native_ui import NativeOptimusWindow


@pytest.fixture(scope='module')
def app():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def owner(app, tmp_path):
    settings = NativePreferences.validate(dict(full_native_settings(), setup_version=2,
                                               background_analysis_enabled=True))
    backend = SimpleNamespace(data_dir=tmp_path, preferences=lambda: copy.deepcopy(settings),
        cleanup_categories=lambda: [], source_root=lambda: Path(__file__).parents[1],
        analysis_start=Mock(), analysis_stop=Mock(), close=Mock(), optimize=Mock())
    window = NativeOptimusWindow(backend, require_launch_review=True)
    window._session_invalidated_callback = Mock(return_value='')
    try:
        yield window, backend, settings
    finally:
        window._settings_window = None
        window.shutdown()
        window.close()


def test_native_open_settings_revokes_prior_approval_and_stops_runtime(owner):
    window, backend, settings = owner
    window._launch_consent.approve(settings)
    stub = SimpleNamespace(busy=False, show=Mock(), raise_=Mock(), activateWindow=Mock(),
                           select_page=Mock(), shutdown=Mock())
    window._settings_window = stub
    window.open_settings()
    assert not window.session_approved()
    window._session_invalidated_callback.assert_called_once_with()
    stub.show.assert_called_once_with()
    backend.analysis_start.assert_not_called()
    backend.optimize.assert_not_called()


def test_native_setup_save_revokes_approval_without_resuming_analysis(owner, monkeypatch):
    window, backend, settings = owner
    window._launch_consent.approve(settings)
    saved = copy.deepcopy(settings)
    saved['analysis_interval'] = 7
    settings.update(saved)
    wizard = SimpleNamespace(saved_preferences=saved, run_now=False,
                             exec=Mock(return_value=QDialog.Accepted), deleteLater=Mock())
    monkeypatch.setattr('optimus.native_ui.NativeSetupWizard', lambda *_, **__: wizard)
    assert window.open_initial_setup()
    assert not window.session_approved()
    window._session_invalidated_callback.assert_called_once_with()
    backend.analysis_start.assert_not_called()
    backend.analysis_stop.assert_not_called()
    backend.optimize.assert_not_called()


def test_unapproved_native_setup_still_does_not_resume_saved_analysis(owner, monkeypatch):
    window, backend, settings = owner
    wizard = SimpleNamespace(saved_preferences=copy.deepcopy(settings), run_now=False,
                             exec=Mock(return_value=QDialog.Accepted), deleteLater=Mock())
    monkeypatch.setattr('optimus.native_ui.NativeSetupWizard', lambda *_, **__: wizard)
    assert window.open_initial_setup()
    backend.analysis_start.assert_not_called()
    backend.optimize.assert_not_called()


def test_unattended_native_findings_stay_in_results_without_post_run_dialog(owner, monkeypatch):
    from optimus.unattended_configuration import approve_unattended_settings
    window, backend, settings = owner
    saved = approve_unattended_settings(NativePreferences.validate(dict(settings,
        unattended_auto_clean_enabled=True)))
    window._post_review_active = True
    make_dialog = Mock(side_effect=AssertionError('Unattended run cannot open review prompts.'))
    monkeypatch.setattr('optimus.native_ui.make_yes_no_dialog', make_dialog)
    window._ask_process_review = Mock(side_effect=AssertionError('Unattended process findings remain manual results.'))
    result = {'security_review': {'recommendations': [{'id': 'review'}]}, 'process_review': {'fixture': True}}
    window._offer_post_optimization_reviews(result, saved)
    assert result['security_review']['recommendations'] == [{'id': 'review'}]
    assert not window._post_review_active
    make_dialog.assert_not_called()
