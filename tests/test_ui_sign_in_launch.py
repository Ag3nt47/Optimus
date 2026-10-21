"""Sign-in scope is explicit and is handed to transactional backend saves."""
from unittest.mock import Mock

import pytest
from PySide6.QtWidgets import QApplication, QPushButton

from optimus.onboarding import InitialSetupWizard
from optimus.ui import OptimusWindow
from optimus.workflow import DEFAULTS, Preferences, full_optimization_settings
from test_onboarding import FixtureBackend


@pytest.fixture(scope='module')
def qapp():
    return QApplication.instance() or QApplication([])


def dispose(value, qapp):
    if hasattr(value, 'shutdown'):
        value.shutdown()
    value.close()
    value.deleteLater()
    qapp.processEvents()


def test_public_and_full_defaults_do_not_invent_sign_in_launch_scope():
    assert DEFAULTS['launch_at_sign_in'] is False
    assert Preferences.validate({})['launch_at_sign_in'] is False
    assert full_optimization_settings()['launch_at_sign_in'] is False
    assert full_optimization_settings({'launch_at_sign_in': True})['launch_at_sign_in'] is True
    for value in (None, 0, 1, 'yes'):
        with pytest.raises(ValueError):
            Preferences.validate({'launch_at_sign_in': value})


def test_setup_preserves_explicit_sign_in_choice_separately_from_cadence(qapp, tmp_path):
    backend = FixtureBackend(tmp_path, {'launch_at_sign_in': True, 'automation_mode': 'manual'})
    wizard = InitialSetupWizard(backend)
    try:
        assert wizard.controls['launch_at_sign_in'].isChecked()
        assert wizard.collect_choices()['automation_mode'] == 'manual'
        wizard.refresh_summary()
        assert 'Launch Optimus at Windows sign-in' in wizard.summary.toPlainText()
        assert 'UAC' in wizard.summary.toPlainText()
        wizard.controls['launch_at_sign_in'].setChecked(False)
        selected = wizard.collect_choices()
        assert selected['launch_at_sign_in'] is False and selected['automation_mode'] == 'manual'
        assert backend.saved == []
        backend.optimize.assert_not_called()
    finally:
        wizard.reject()
        dispose(wizard, qapp)


def test_selecting_full_in_setup_keeps_sign_in_disabled_unless_user_selected_it(qapp, tmp_path):
    backend = FixtureBackend(tmp_path)
    wizard = InitialSetupWizard(backend)
    try:
        wizard.optimization_mode.setCurrentIndex(wizard.optimization_mode.findData('full'))
        assert not wizard.controls['launch_at_sign_in'].isChecked()
        wizard.controls['launch_at_sign_in'].setChecked(True)
        wizard.optimization_mode.setCurrentIndex(wizard.optimization_mode.findData('custom'))
        wizard.optimization_mode.setCurrentIndex(wizard.optimization_mode.findData('full'))
        assert wizard.controls['launch_at_sign_in'].isChecked()
        assert backend.saved == []
    finally:
        wizard.reject()
        dispose(wizard, qapp)


@pytest.mark.parametrize('enabled', [True, False])
def test_settings_forwards_sign_in_enable_disable_only_through_save(qapp, tmp_path, enabled):
    backend = FixtureBackend(tmp_path, {'launch_at_sign_in': not enabled, 'automation_mode': 'manual'})
    backend.set_startup_launch = Mock(side_effect=AssertionError('UI must use the transactional settings save.'))
    main = OptimusWindow(backend, smoke_test=True)
    try:
        main.open_settings(page='Settings')
        settings = main._settings_window
        settings._run = Mock()
        settings.workflow_controls['launch_at_sign_in'].setChecked(enabled)
        assert backend.saved == []
        backend.set_startup_launch.assert_not_called()
        save = next(button for button in settings.findChildren(QPushButton) if button.text() == 'Save one-click configuration')
        save.click()
        assert settings._run.call_args.args[0] == 'save_preferences'
        selected = settings._run.call_args.args[1]
        assert selected['launch_at_sign_in'] is enabled
        assert selected['automation_mode'] == 'manual'
        backend.set_startup_launch.assert_not_called()
    finally:
        dispose(main, qapp)
