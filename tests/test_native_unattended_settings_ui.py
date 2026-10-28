"""Windows Settings unattended scope is exercised with a copied fixture backend."""
import copy
import os
from pathlib import Path
from unittest.mock import Mock

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

import pytest
from PySide6.QtWidgets import QApplication, QPushButton

from optimus.ui import DetailedSettingsWindow, OptimusWindow
from optimus.unattended_configuration import approve_unattended_settings, matches_unattended
from optimus.workflow import DEFAULTS, Preferences


class CopyBackend:
    def __init__(self, directory, initial=None):
        self.data_dir = directory
        self.settings = Preferences.validate(initial or dict(DEFAULTS, setup_version=2, automation_mode='monthly'))
        self.analysis_start, self.analysis_stop, self.optimize, self.close = Mock(), Mock(), Mock(), Mock()
        self.save_preferences = Mock(side_effect=AssertionError('Settings fixture captures saves without host actions.'))
    def preferences(self):
        return copy.deepcopy(self.settings)
    def cleanup_categories(self):
        return []
    def source_root(self):
        return Path(__file__).parents[1]


@pytest.fixture(scope='module')
def app():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def windows(app, tmp_path, request):
    initial = dict(DEFAULTS, setup_version=request.param) if hasattr(request, 'param') else None
    backend = CopyBackend(tmp_path, initial)
    main = OptimusWindow(backend, smoke_test=True, require_launch_review=True)
    detail = DetailedSettingsWindow(backend, smoke_test=True)
    main._settings_window = detail
    main._install_workflow_preferences(detail)
    captured = []
    detail._run = lambda *args, **kwargs: captured.append((args, kwargs))
    detail._error = Mock()
    save = next(button for button in detail.findChildren(QPushButton)
                if button.text() == 'Save one-click configuration')
    try:
        yield main, detail, backend, captured, save
    finally:
        main.smoke_test = True
        main.shutdown()
        main.close()
        detail.shutdown()
        detail.close()
        main.deleteLater()
        detail.deleteLater()
        app.processEvents()


def test_unattended_toggle_visibly_forces_startup_launch_and_controls(windows):
    main, detail, backend, captured, save = windows
    controls = detail.workflow_controls
    enabled = controls['unattended_auto_clean_enabled']
    assert not enabled.isChecked()
    assert controls['automation_mode'].currentData() == 'monthly'
    assert not detail.workflow_unattended_browser.isEnabled()
    enabled.setChecked(True)
    assert controls['launch_at_sign_in'].isChecked()
    assert not controls['launch_at_sign_in'].isEnabled()
    assert controls['automation_mode'].currentData() == 'startup'
    assert not controls['automation_mode'].isEnabled()
    assert detail.workflow_unattended_browser.isEnabled()
    assert not captured
    backend.save_preferences.assert_not_called()
    enabled.setChecked(False)
    assert controls['launch_at_sign_in'].isEnabled()
    assert controls['automation_mode'].isEnabled()
    assert not detail.workflow_unattended_browser.isEnabled()


@pytest.mark.parametrize('policy', ['skip_open', 'close_gracefully'])
def test_explicit_settings_save_stamps_normalized_complete_scope_only(windows, policy):
    main, detail, backend, captured, save = windows
    detail.workflow_controls['unattended_auto_clean_enabled'].setChecked(True)
    combo = detail.workflow_unattended_browser
    combo.setCurrentIndex(combo.findData(policy))
    before = backend.preferences()
    save.click()
    assert len(captured) == 1
    args, kwargs = captured[0]
    assert args[0] == 'save_preferences'
    payload = args[1]
    assert set(payload) == set(DEFAULTS)
    assert payload['toolkit_settings'] == DEFAULTS['toolkit_settings']
    assert all(section['enabled'] is False for section in payload['toolkit_settings'].values())
    assert payload['software_app_rules'] == []
    assert matches_unattended(payload) and matches_unattended(Preferences.validate(payload))
    assert payload['unattended_browser_policy'] == policy
    assert payload['launch_at_sign_in'] is True and payload['automation_mode'] == 'startup'
    assert not payload['driver_changes_approved']
    assert not payload['driver_update_agreements_accepted']
    assert not payload['software_update_agreements_accepted']
    assert backend.preferences() == before
    kwargs['done'](copy.deepcopy(payload))
    assert main.workflow_settings == payload
    backend.optimize.assert_not_called()
    backend.analysis_start.assert_not_called()


@pytest.mark.parametrize('windows', [0, 1], indirect=True)
def test_incomplete_setup_cannot_be_stamped_by_settings_save(windows):
    main, detail, backend, captured, save = windows
    detail.workflow_controls['unattended_auto_clean_enabled'].setChecked(True)
    save.click()
    assert not captured
    detail._error.assert_called_once()
    assert 'Complete setup' in detail._error.call_args.args[0]
    assert not matches_unattended(backend.preferences())


def test_external_scope_change_with_stale_hash_cannot_skip_launch_review(windows):
    main, detail, backend, captured, save = windows
    saved = approve_unattended_settings(Preferences.validate(dict(backend.settings,
        unattended_auto_clean_enabled=True)))
    backend.settings = dict(saved, m365_debloat_enabled=True)
    assert not matches_unattended(backend.settings)
    main.smoke_test = False
    main._launch_consent.required = True
    main._ask_launch_review = Mock(return_value='adjust_settings')
    main.open_settings = Mock()
    main.start_optimization = Mock()
    assert main.review_launch() is False
    main._ask_launch_review.assert_called_once()
    main.start_optimization.assert_not_called()
    assert not main.session_approved()
    assert not captured


def test_settings_cannot_approve_external_removal_scope_not_shown_in_form(windows):
    main, detail, backend, captured, save = windows
    assert not detail.workflow_controls['m365_debloat_enabled'].isChecked()
    backend.settings['m365_debloat_enabled'] = True
    detail.workflow_controls['unattended_auto_clean_enabled'].setChecked(True)
    save.click()
    assert not captured, 'An external scope change needs a fresh visible review before persistent approval.'
    detail._error.assert_called_once()
    assert not matches_unattended(backend.preferences())


def test_retention_and_drive_settings_save_only_the_explicit_scopes_and_fractional_target(windows):
    main, detail, backend, captured, save = windows
    controls = detail.workflow_controls
    for prefix in ('codex_archives', 'angerona_logs', 'failed_downloads'):
        assert not controls[prefix + '_cleanup_enabled'].isChecked()
        assert controls[prefix + '_retention_days'].value() == 7
    assert controls['drive_space_guard_enabled'].isChecked()
    controls['codex_archives_cleanup_enabled'].setChecked(True)
    controls['codex_archives_retention_days'].setValue(30)
    controls['failed_downloads_cleanup_enabled'].setChecked(True)
    controls['failed_downloads_retention_days'].setValue(14)
    controls['drive_space_guard_target_gib'].setValue(1.25)
    controls['unattended_auto_clean_enabled'].setChecked(True)
    before = backend.preferences()
    save.click()
    assert len(captured) == 1
    payload = captured[0][0][1]
    assert set(payload) == set(DEFAULTS)
    assert payload['toolkit_settings'] == DEFAULTS['toolkit_settings']
    assert all(section['enabled'] is False for section in payload['toolkit_settings'].values())
    assert payload['codex_archives_cleanup_enabled'] is True and payload['codex_archives_retention_days'] == 30
    assert payload['failed_downloads_cleanup_enabled'] is True and payload['failed_downloads_retention_days'] == 14
    assert payload['angerona_logs_cleanup_enabled'] is False
    assert payload['drive_space_guard_target_gib'] == 1.25 and matches_unattended(payload)
    assert backend.preferences() == before
