"""Exact unattended scope approvals never grant new package/device authority."""
import copy
import os
from pathlib import Path
from unittest.mock import Mock

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

import pytest
from PySide6.QtWidgets import QApplication

from optimus.native_settings import NativePreferences, full_native_settings
from optimus.native_ui import NativeSetupWizard
from optimus.onboarding import InitialSetupWizard
from optimus.unattended_configuration import (UNATTENDED_DEFAULTS, approve_unattended_settings,
                                             matches_unattended, validate_unattended_configuration)
from optimus.workflow import DEFAULTS, Preferences, full_optimization_settings


def approved(**changes):
    return approve_unattended_settings(Preferences.validate(dict(DEFAULTS, setup_version=2,
        unattended_auto_clean_enabled=True, **changes)))


def test_new_and_old_preferences_never_opt_in_implicitly():
    old = {key: copy.deepcopy(value) for key, value in DEFAULTS.items() if key not in UNATTENDED_DEFAULTS}
    for settings in (DEFAULTS, Preferences.validate(old), Preferences.safe_defaults(),
                     full_optimization_settings(), full_native_settings()):
        assert {key: settings[key] for key in UNATTENDED_DEFAULTS} == UNATTENDED_DEFAULTS
        assert not matches_unattended(settings)


def test_approval_uses_exact_validated_scope_and_changes_only_startup_fields():
    settings = Preferences.validate(dict(DEFAULTS, setup_version=2, unattended_auto_clean_enabled=True,
        automation_mode='monthly', browser_keep_domains=['.EXAMPLE.COM.']))
    before = copy.deepcopy(settings)
    result = approve_unattended_settings(settings)
    assert settings == before
    assert result['launch_at_sign_in'] is True and result['automation_mode'] == 'startup'
    assert len(result['unattended_scope_hash']) == 64 and matches_unattended(result)
    assert matches_unattended(Preferences.validate(result))
    assert result['browser_keep_domains'] == ['example.com']
    assert all(result[key] == value for key, value in before.items()
               if key not in ('launch_at_sign_in', 'automation_mode', 'unattended_scope_hash'))
    assert not result['driver_changes_approved'] and not result['software_update_agreements_accepted']


@pytest.mark.parametrize('changes', [dict(cleanup_age_days=0), dict(browser_cookies_enabled=True),
    dict(browser_cleanup_browsers=['opera']), dict(browser_keep_domains=['example.org']),
    dict(consumer_debloat_enabled=True), dict(m365_debloat_enabled=True),
    dict(privacy_disable_recall_snapshots_enabled=True), dict(software_update_all_approved=True),
    dict(driver_changes_approved=True), dict(smart_cleaning_enabled=True),
    dict(unattended_browser_policy='close_gracefully'), dict(launch_at_sign_in=False),
    dict(automation_mode='daily'), dict(setup_version=1), dict(unattended_auto_clean_enabled=False)])
def test_every_action_scope_change_revokes_unattended_approval(changes):
    saved = approved()
    saved.update(changes)
    assert not matches_unattended(saved)


def test_cosmetic_changes_and_key_order_do_not_change_approved_actions():
    saved = approved()
    saved.update(reduce_motion=True, theme='new theme', motion=False)
    assert matches_unattended(dict(reversed(list(saved.items()))))


@pytest.mark.parametrize('change', [dict(drive_space_guard_enabled=False),
    dict(drive_space_guard_target_gib=1.5), dict(drive_space_guard_target_gib=2.0)])
def test_low_space_cleanup_approval_binds_guard_and_target(change):
    saved = approved(toolkit_settings={'low_space_cleaning': {'enabled': True}},
        drive_space_guard_enabled=True, drive_space_guard_target_gib=1.0)
    assert matches_unattended(saved)
    changed = copy.deepcopy(saved)
    changed.update(change)
    assert not matches_unattended(changed)


@pytest.mark.parametrize('enabled', [True, False])
def test_low_space_guard_toggle_is_scoped_even_when_initially_disabled(enabled):
    saved = approved(toolkit_settings={'low_space_cleaning': {'enabled': True}},
        drive_space_guard_enabled=enabled)
    assert matches_unattended(saved)
    saved['drive_space_guard_enabled'] = not enabled
    assert not matches_unattended(saved)


@pytest.mark.parametrize('legacy', [False, True])
def test_drive_guard_remains_read_only_exclusion_when_low_space_cleaning_is_off(legacy):
    settings = Preferences.validate(dict(DEFAULTS, setup_version=2, unattended_auto_clean_enabled=True))
    if legacy:
        settings['toolkit_settings'].pop('low_space_cleaning')
    saved = approve_unattended_settings(settings)
    saved.update(drive_space_guard_enabled=False, drive_space_guard_target_gib=1.25)
    assert matches_unattended(saved)


@pytest.mark.parametrize('key,value', [('drive_space_guard_enabled', 1),
    ('drive_space_guard_target_gib', None), ('drive_space_guard_target_gib', True),
    ('drive_space_guard_target_gib', float('nan')), ('drive_space_guard_target_gib', 3)])
def test_invalid_low_space_guard_cannot_keep_or_gain_unattended_approval(key, value):
    saved = approved(toolkit_settings={'low_space_cleaning': {'enabled': True}})
    saved[key] = value
    assert not matches_unattended(saved)
    with pytest.raises(ValueError):
        approve_unattended_settings(saved)


@pytest.mark.parametrize('key', ['drive_space_guard_enabled', 'drive_space_guard_target_gib'])
def test_missing_low_space_guard_cannot_keep_automatic_deletion_approval(key):
    saved = approved(toolkit_settings={'low_space_cleaning': {'enabled': True}})
    del saved[key]
    assert not matches_unattended(saved)


def test_turning_off_then_on_cannot_revive_a_previous_approval():
    saved = approved()
    disabled = Preferences.validate(dict(saved, unattended_auto_clean_enabled=False))
    assert disabled['unattended_scope_hash'] == ''
    enabled_again = Preferences.validate(dict(disabled, unattended_auto_clean_enabled=True))
    assert not matches_unattended(enabled_again)


@pytest.mark.parametrize('version', [0, 1, True, 3, '2', None])
def test_approval_requires_completed_current_setup(version):
    with pytest.raises(ValueError):
        approve_unattended_settings(dict(DEFAULTS, unattended_auto_clean_enabled=True, setup_version=version))


def test_approval_requires_explicit_selected_toggle_and_never_turns_it_on():
    with pytest.raises(ValueError):
        approve_unattended_settings(dict(DEFAULTS, setup_version=2))


@pytest.mark.parametrize('key,invalid', [('unattended_auto_clean_enabled', 1),
    ('unattended_auto_clean_enabled', 'yes'), ('unattended_scope_hash', None),
    ('unattended_scope_hash', 'A' * 64), ('unattended_scope_hash', '../approval'),
    ('unattended_browser_policy', None), ('unattended_browser_policy', 'force_kill'),
    ('unattended_browser_policy', [])])
def test_unattended_configuration_is_strict_and_never_coerces(key, invalid):
    for validate in (validate_unattended_configuration, Preferences.validate, NativePreferences.validate):
        with pytest.raises(ValueError):
            validate({key: invalid})
    assert not matches_unattended({key: invalid})


def test_bad_hash_missing_scope_and_nonfinite_settings_never_bypass_review():
    assert not matches_unattended(None)
    saved = approved()
    assert not matches_unattended(dict(saved, unattended_scope_hash='0' * 64))
    assert not matches_unattended(dict(saved, analysis_interval=float('nan')))
    missing = copy.deepcopy(saved)
    missing.pop('cleanup_age_days')
    assert not matches_unattended(missing)


class SetupBackend:
    def __init__(self, directory, native):
        self.data_dir, self.native = directory, native
        self.validate = NativePreferences.validate if native else Preferences.validate
        self.settings = self.validate(full_native_settings() if native else DEFAULTS)
        self.saved = []
        self.optimize, self.analysis_start, self.analysis_stop = Mock(), Mock(), Mock()
    def preferences(self):
        return copy.deepcopy(self.settings)
    def cleanup_categories(self):
        return []
    def source_root(self):
        return Path(__file__).parents[1]
    def save_preferences(self, settings):
        self.settings = self.validate(settings)
        self.saved.append(copy.deepcopy(self.settings))
        return self.preferences()


@pytest.mark.parametrize('native', [False, True])
@pytest.mark.parametrize('policy', ['skip_open', 'close_gracefully'])
def test_wizards_only_stamp_after_explicit_save_and_preserve_browser_policy(tmp_path, native, policy):
    app = QApplication.instance() or QApplication([])
    backend = SetupBackend(tmp_path, native)
    wizard = NativeSetupWizard(backend) if native else InitialSetupWizard(backend)
    try:
        assert not wizard.unattended_setup.enabled.isChecked()
        wizard.unattended_setup.enabled.setChecked(True)
        widget = wizard.unattended_setup.browser_policy
        widget.setCurrentIndex(widget.findData(policy))
        collected = wizard.collect() if native else wizard.collect_choices()
        assert collected['unattended_scope_hash'] == '' and not matches_unattended(collected)
        assert collected['launch_at_sign_in'] is True
        assert collected['automation_mode'] == 'startup'
        assert not backend.saved
        wizard.accept()
        assert len(backend.saved) == 1
        saved = backend.saved[0]
        assert matches_unattended(saved)
        assert saved['unattended_browser_policy'] == policy
        backend.optimize.assert_not_called()
        backend.analysis_start.assert_not_called()
    finally:
        wizard.deleteLater()
        app.processEvents()


@pytest.mark.parametrize('native', [False, True])
def test_cancelling_unsaved_unattended_choice_preserves_disabled_preferences(tmp_path, native):
    app = QApplication.instance() or QApplication([])
    backend = SetupBackend(tmp_path, native)
    initial = backend.preferences()
    wizard = NativeSetupWizard(backend) if native else InitialSetupWizard(backend)
    try:
        wizard.unattended_setup.enabled.setChecked(True)
        wizard.reject()
        assert backend.preferences() == initial and not backend.saved
        assert not matches_unattended(backend.preferences())
    finally:
        wizard.deleteLater()
        app.processEvents()
