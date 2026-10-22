"""Explicit sign-in repair/status controls use fixture backends, never native tasks."""
from unittest.mock import Mock
import pytest
from PySide6.QtWidgets import QApplication, QLabel, QPushButton
from optimus.ui import OptimusWindow
from test_onboarding import FixtureBackend


@pytest.fixture(scope='module')
def qapp():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def window(qapp, tmp_path, monkeypatch):
    monkeypatch.setenv('LOCALAPPDATA', str(tmp_path))
    backend = FixtureBackend(tmp_path)
    backend.startup_launch_status = Mock()
    backend.set_startup_launch = Mock()
    result = OptimusWindow(backend, smoke_test=True)
    result.open_settings(page='Settings')
    result._settings_window._run = Mock()
    yield result
    result.shutdown()
    result.close()
    result.deleteLater()
    qapp.processEvents()


def test_opening_settings_does_not_query_or_change_tasks(window):
    window.backend.startup_launch_status.assert_not_called()
    window.backend.set_startup_launch.assert_not_called()
    assert [button.text() for button in window.main_buttons] == ['1 click Optimize and debloat','Settings','Sandbox']
    assert not window._settings_window.workflow_controls['launch_at_sign_in'].isChecked()
    assert not window._settings_window.workflow_controls['request_admin_on_startup'].isChecked()


@pytest.mark.parametrize('state,label', [('enabled','Enabled'),('disabled','Disabled'),
    ('absent','Missing'),('foreign','Unrecognized entry'),('unavailable','Unavailable')])
def test_status_check_is_explicit_and_reports_effective_windows_state(window, state, label):
    details = window._settings_window
    details.findChild(QPushButton, 'CheckSignInLaunch').click()
    details._run.assert_called_once()
    assert details._run.call_args.args == ('startup_launch_status',)
    details._run.call_args.kwargs['done']({'state':state,'reason':'Fixture evidence'})
    text = details.findChild(QLabel,'SignInLaunchStatus').text()
    assert label in text and 'Fixture evidence' in text
    window.backend.set_startup_launch.assert_not_called()
    assert window.backend.saved == []


@pytest.mark.parametrize('enabled', [True,False])
def test_explicit_apply_requires_confirmation_and_saves_only_startup_choice(window, enabled):
    details = window._settings_window
    details.workflow_controls['launch_at_sign_in'].setChecked(enabled)
    details.workflow_controls['request_admin_on_startup'].setChecked(True)
    details._confirm = Mock(return_value=True)
    details.findChild(QPushButton,'ApplySignInLaunch').click()
    details._confirm.assert_called_once()
    assert 'normal Windows UAC' in details._confirm.call_args.args[1]
    assert details._run.call_args.args == ('set_startup_launch',enabled)
    details._run.call_args.kwargs['done']({'state':'enabled' if enabled else 'absent'})
    assert details.workflow_control_baseline['launch_at_sign_in'] is enabled
    assert details.workflow_controls['request_admin_on_startup'].isChecked()
    assert window.backend.saved == []


def test_declining_explicit_apply_preserves_task_and_preferences(window):
    details = window._settings_window
    details.workflow_controls['launch_at_sign_in'].setChecked(True)
    details._confirm = Mock(return_value=False)
    details.findChild(QPushButton,'ApplySignInLaunch').click()
    details._run.assert_not_called()
    window.backend.set_startup_launch.assert_not_called()
    assert window.backend.saved == []


def test_choice_changed_during_apply_is_retained_as_unsaved(window):
    details = window._settings_window
    details.workflow_controls['launch_at_sign_in'].setChecked(True)
    details._confirm = Mock(return_value=True)
    details.findChild(QPushButton,'ApplySignInLaunch').click()
    callback = details._run.call_args.kwargs['done']
    details.workflow_controls['launch_at_sign_in'].setChecked(False)
    callback({'state':'enabled'})
    assert not details.workflow_controls['launch_at_sign_in'].isChecked()
    assert details.workflow_control_baseline['launch_at_sign_in'] is True


def test_setup_summary_explains_admin_choice_even_without_sign_in_launch(qapp, tmp_path):
    from optimus.onboarding import InitialSetupWizard
    backend = FixtureBackend(tmp_path, {'launch_at_sign_in':False, 'request_admin_on_startup':True})
    wizard = InitialSetupWizard(backend)
    try:
        wizard.refresh_summary()
        summary = wizard.summary.toPlainText()
        assert 'Request administrator access once at each normal app launch using Windows UAC' in summary
        assert 'declining keeps Optimus open with standard access' in summary
        assert backend.saved == []
    finally:
        wizard.reject()
        wizard.deleteLater()
        qapp.processEvents()
