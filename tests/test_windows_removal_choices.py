"""Removal labels, saved scope and remote-client protection use fixture backends."""
import copy
import json
import os
from unittest.mock import Mock

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

import pytest
from PySide6.QtWidgets import QApplication, QPushButton

from optimus.app_removal_protection import WINDOWS_APP_NAME, WINDOWS_APP_FAMILY, WINDOWS_APP_RECORD
from optimus.launch_review import launch_warning
from optimus.native_settings import NativePreferences, full_native_settings
from optimus.onboarding import InitialSetupWizard, REMOVALS
from optimus.removal_enforcement import approved
from optimus.ui import DetailedSettingsWindow, OptimusWindow
from optimus.unattended_configuration import approve_unattended_settings, matches_unattended
from optimus.workflow import (CONSUMER_PACKAGE_NAMES, DEFAULTS, Preferences,
                              WINDOWS_REMOVAL_KEYS, full_optimization_settings)
from test_onboarding import FixtureBackend


NEW_KEYS = ('office365_desktop_debloat_enabled', 'office_desktop_debloat_enabled',
            'windows_removal_enforcement_enabled')
PRIVACY_KEYS = ('privacy_disable_online_speech_enabled', 'privacy_disable_voice_activation_enabled',
                'privacy_disable_activity_history_enabled')


def test_new_install_defaults_select_requested_removals_and_exclude_remote_client(tmp_path):
    saved = Preferences(tmp_path).load()
    assert all(saved[key] is True for key in WINDOWS_REMOVAL_KEYS)
    assert all(saved[key] is True for key in PRIVACY_KEYS)
    assert WINDOWS_APP_NAME not in CONSUMER_PACKAGE_NAMES
    assert WINDOWS_APP_NAME not in saved['consumer_debloat_package_names']
    assert saved['setup_version'] == 0
    assert not matches_unattended(saved)


def test_existing_false_choices_and_other_preferences_survive_new_defaults(tmp_path):
    saved = dict(DEFAULTS, **{key: False for key in (*WINDOWS_REMOVAL_KEYS, *PRIVACY_KEYS)},
                 cleanup_age_days=23, browser_keep_domains=['example.com'],
                 app_removal_exclusions=[WINDOWS_APP_RECORD])
    store = Preferences(tmp_path)
    store.path.write_text(json.dumps(saved), encoding='utf-8')
    assert store.load() == Preferences.validate(saved)
    assert all(store.load()[key] is False for key in (*WINDOWS_REMOVAL_KEYS, *PRIVACY_KEYS))


def test_invalid_settings_never_turn_on_new_removals_or_privacy_actions(tmp_path):
    store = Preferences(tmp_path)
    store.path.write_text('{broken', encoding='utf-8')
    saved = store.load()
    assert all(saved[key] is False for key in WINDOWS_REMOVAL_KEYS)
    assert not any(value for key, value in saved.items() if key.startswith('privacy_'))


def test_legacy_windows_app_selection_is_removed_without_resetting_other_choices():
    saved = Preferences.validate(dict(DEFAULTS, consumer_debloat_package_names=[
        'Microsoft.BingNews', WINDOWS_APP_NAME], m365_debloat_enabled=False,
        cleanup_age_days=19, app_removal_exclusions=[WINDOWS_APP_RECORD]))
    assert saved['consumer_debloat_package_names'] == ['Microsoft.BingNews']
    assert saved['m365_debloat_enabled'] is False and saved['cleanup_age_days'] == 19
    assert saved['app_removal_exclusions'] == [WINDOWS_APP_RECORD]


@pytest.mark.parametrize('key', NEW_KEYS + PRIVACY_KEYS)
def test_added_action_scopes_are_strict_and_require_fresh_unattended_approval(key):
    saved = approve_unattended_settings(Preferences.validate(dict(DEFAULTS, setup_version=2,
        unattended_auto_clean_enabled=True, **{key: False})))
    assert matches_unattended(saved)
    assert not matches_unattended(dict(saved, **{key: True}))
    for bad in (1, 'yes', None):
        with pytest.raises(ValueError):
            Preferences.validate(dict(DEFAULTS, **{key: bad}))


def test_missing_new_scopes_cannot_inherit_an_old_unattended_hash():
    previous = dict(DEFAULTS, setup_version=2, unattended_auto_clean_enabled=True)
    for key in NEW_KEYS + PRIVACY_KEYS:
        previous.pop(key)
    previous = approve_unattended_settings(previous)
    assert matches_unattended(previous)
    updated = Preferences.validate(previous)
    assert all(updated[key] is True for key in NEW_KEYS + PRIVACY_KEYS)
    assert not matches_unattended(updated)


def test_enforcement_record_is_strict_revocable_and_not_an_extra_unattended_action():
    for bad in (None, 'A' * 64, 'short', 1):
        with pytest.raises(ValueError):
            Preferences.validate(dict(DEFAULTS, windows_removal_enforcement_scope_hash=bad))
    disabled = Preferences.validate(dict(DEFAULTS, windows_removal_enforcement_enabled=False,
                                         windows_removal_enforcement_scope_hash='a' * 64))
    assert disabled['windows_removal_enforcement_scope_hash'] == ''
    saved = approve_unattended_settings(Preferences.validate(dict(DEFAULTS, setup_version=2,
        unattended_auto_clean_enabled=True)))
    assert matches_unattended(dict(saved, windows_removal_enforcement_scope_hash='b' * 64))
    assert not matches_unattended(dict(saved, office_desktop_debloat_enabled=False))


def test_native_platforms_do_not_enable_windows_removal_or_privacy_requests():
    for saved in (full_native_settings(), NativePreferences.validate(DEFAULTS)):
        assert all(saved[key] is False for key in (*WINDOWS_REMOVAL_KEYS, *PRIVACY_KEYS))


def test_launch_warning_distinguishes_hub_and_both_desktop_suites():
    text = launch_warning(DEFAULTS)
    assert 'hub/companion Store apps' in text
    assert 'subscription desktop apps' in text and 'perpetual Microsoft Office' in text
    assert 'Always preserve Windows App for remote desktop' in text
    assert 'Windows security updates remain enabled' in text
    assert 'microphone access and Windows App audio' in text
    assert 'Windows upgrades can restore components' in text
    off = dict(DEFAULTS, **{key: False for key in WINDOWS_REMOVAL_KEYS})
    text = launch_warning(off)
    assert 'subscription desktop apps' not in text and 'perpetual Microsoft Office' not in text


@pytest.fixture(scope='module')
def app():
    return QApplication.instance() or QApplication([])


def test_setup_has_individual_uninstall_controls_and_an_uncheckable_keep_row(app, tmp_path):
    backend = FixtureBackend(tmp_path)
    wizard = InitialSetupWizard(backend, first_run=True)
    try:
        for key, _ in REMOVALS:
            assert wizard.controls[key].isChecked()
            assert wizard.controls[key].toolTip()
        assert 'desktop apps' in wizard.controls['office365_desktop_debloat_enabled'].text()
        assert 'perpetual' in wizard.controls['office_desktop_debloat_enabled'].text()
        assert wizard.keep_windows_app.isChecked() and not wizard.keep_windows_app.isEnabled()
        wizard._sync_selection_actions()
        wizard.selection_actions['uninstaller']['clear'].click()
        assert wizard.keep_windows_app.isChecked()
        assert all(not wizard.controls[key].isChecked() for key, _ in REMOVALS)
        wizard.selection_actions['uninstaller']['select'].click()
        assert wizard.keep_windows_app.isChecked()
        assert WINDOWS_APP_NAME not in wizard.consumer_package_controls
        assert all(control.text().startswith('Fully uninstall ') for control in wizard.consumer_package_controls.values())
        assert not backend.saved
        backend.optimize.assert_not_called()
    finally:
        wizard.reject(); wizard.deleteLater(); app.processEvents()


def test_settings_save_preserves_individual_off_choices_and_windows_app(app, tmp_path):
    backend = FixtureBackend(tmp_path, {'setup_version': 2})
    backend.source_root = lambda: __import__('pathlib').Path(__file__).parents[1]
    main = OptimusWindow(backend, smoke_test=True, require_launch_review=True)
    detail = DetailedSettingsWindow(backend, smoke_test=True)
    main._settings_window = detail
    main._install_workflow_preferences(detail)
    captured = []
    detail._run = lambda *args, **kwargs: captured.append((args, kwargs))
    detail._error = Mock()
    try:
        controls = detail.workflow_controls
        assert detail.keep_windows_app.isChecked() and not detail.keep_windows_app.isEnabled()
        assert all(controls[key].isChecked() for key in NEW_KEYS)
        controls['office_desktop_debloat_enabled'].setChecked(False)
        for name, checkbox in detail.consumer_package_controls.items():
            if name == 'Microsoft.BingWeather':
                checkbox.setChecked(False)
        next(button for button in detail.findChildren(QPushButton)
             if button.text() == 'Save one-click configuration').click()
        assert len(captured) == 1
        args, _ = captured[0]
        saved = args[1]
        assert saved['office_desktop_debloat_enabled'] is False
        assert saved['office365_desktop_debloat_enabled'] is True
        assert 'Microsoft.BingWeather' not in saved['consumer_debloat_package_names']
        assert WINDOWS_APP_NAME not in saved['consumer_debloat_package_names']
        assert approved(saved)
        assert backend.saved == []
        backend.optimize.assert_not_called()
        backend.last_removal_guard_registration = {'status': 'needs_review', 'error': 'Fixture administrator approval was declined'}
        captured[0][1]['done'](saved)
        assert 'Choices saved; scheduled re-removal is unavailable' in detail.status.text()
        assert 'approval was declined' in detail.status.text()
        backend.settings = copy.deepcopy(saved)
        backend.settings['privacy_disable_online_speech_enabled'] = False
        next(button for button in detail.findChildren(QPushButton)
             if button.text() == 'Save one-click configuration').click()
        assert len(captured) == 1
        detail._error.assert_called_once()
        assert 'changed outside this form' in detail._error.call_args.args[0]
    finally:
        main.smoke_test=True;main.shutdown();detail.shutdown();main.close();detail.close()
        main.deleteLater();detail.deleteLater();app.processEvents()
