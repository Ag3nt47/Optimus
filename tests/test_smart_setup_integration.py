"""Opt-in Smart/Opera/calendar integration; all backend actions are fixtures."""
import copy
from datetime import datetime, timezone
import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

import pytest
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication

from optimus import automation
from optimus.calendar_cadence import next_month_timestamp
from optimus.gui_startup import SetupAutomationController
from optimus.native_settings import NativePreferences, full_native_settings
from optimus.native_ui import NativeSetupWizard
from optimus.onboarding import InitialSetupWizard
from optimus.smart_configuration import SMART_DEFAULTS, SMART_BOUNDS
from optimus.workflow import DEFAULTS, Preferences, full_optimization_settings


@pytest.fixture(scope='module')
def app():
    return QApplication.instance() or QApplication([])


def fixture_backend(tmp_path, settings):
    return SimpleNamespace(data_dir=tmp_path, preferences=lambda: copy.deepcopy(settings),
        cleanup_categories=lambda: [], source_root=lambda: Path(__file__).parents[1],
        save_preferences=Mock(), optimize=Mock(), analysis_start=Mock(), analysis_stop=Mock())


def test_old_settings_gain_only_disabled_smart_options_and_retain_approvals():
    old = copy.deepcopy(DEFAULTS)
    for key in SMART_DEFAULTS:
        old.pop(key)
    old.update(setup_version=2, automation_mode='weekly', browser_cleanup_browsers=['firefox'],
        driver_changes_approved=True, driver_update_agreements_accepted=True,
        software_update_agreements_accepted=True, software_update_ids=['Fixture.App'])
    migrated = Preferences.validate(old)
    assert {key: migrated[key] for key in SMART_DEFAULTS} == SMART_DEFAULTS
    assert all(migrated[key] == value for key, value in old.items())
    assert len(migrated) == len(old) + 6
    assert not migrated['smart_cleaning_enabled'] and not migrated['browser_exit_cleaning_enabled']


def test_opera_is_supported_only_when_explicitly_selected():
    for settings in (DEFAULTS, Preferences.safe_defaults(), full_optimization_settings(), full_native_settings()):
        assert 'opera' not in settings['browser_cleanup_browsers']
    for validator in (Preferences.validate, NativePreferences.validate):
        assert validator({'browser_cleanup_browsers': ['opera']})['browser_cleanup_browsers'] == ['opera']
    prior = {'browser_cleanup_browsers': ['firefox']}
    assert full_optimization_settings(prior)['browser_cleanup_browsers'] == ['firefox']


@pytest.mark.parametrize('validator', [Preferences.validate, NativePreferences.validate])
@pytest.mark.parametrize('key', list(SMART_DEFAULTS))
def test_shared_validators_reject_bool_number_coercion(validator, key):
    invalid = 1 if isinstance(SMART_DEFAULTS[key], bool) else True
    with pytest.raises(ValueError):
        validator({key: invalid})


@pytest.mark.parametrize('key', list(SMART_BOUNDS))
def test_smart_bounds_are_enforced_by_preferences(key):
    low, high = SMART_BOUNDS[key]
    for invalid in (low - 1, high + 1, float(low), '60'):
        with pytest.raises(ValueError):
            Preferences.validate({key: invalid})
    assert Preferences.validate({key: low})[key] == low
    assert Preferences.validate({key: high})[key] == high


def test_monthly_dispatch_uses_calendar_deadline_and_counts_failure(tmp_path):
    config = dict(DEFAULTS, setup_version=2, automation_mode='monthly')
    previous = datetime(2024, 1, 31, 12, 45, tzinfo=timezone.utc).timestamp()
    attempt = automation.record_attempt(tmp_path, config, now=previous)
    failed = automation.record_attempt(tmp_path, config, 'failed', now=previous + 1, run_id=attempt['run_id'])
    deadline = next_month_timestamp(previous)
    assert not automation.due(config, failed, now=deadline - 1)
    assert automation.due(config, failed, now=deadline)
    assert not automation.due(config, failed, now=previous - 1)
    assert 'calendar' in automation.cadence_note('monthly')
    assert 'while Optimus is open' in automation.cadence_note('monthly')


def test_monthly_controller_uses_existing_workflow_only_when_due(tmp_path, monkeypatch):
    config = dict(DEFAULTS, setup_version=2, automation_mode='monthly')
    previous = 1706702400
    attempt = automation.record_attempt(tmp_path, config, status='configured', now=previous)
    backend = fixture_backend(tmp_path, config)
    backend.workflow = SimpleNamespace(active=False)
    window = SimpleNamespace(smoke_test=False, busy=False, status=SimpleNamespace(setText=Mock()),
        open_initial_setup=Mock(), start_optimization=Mock())
    deadline = next_month_timestamp(previous)
    controller = SetupAutomationController(backend, window, clock=lambda: deadline - 1)
    controller.tick()
    window.start_optimization.assert_not_called()
    controller.clock = lambda: deadline
    controller.tick()
    window.start_optimization.assert_called_once_with()
    assert automation.load_attempt(tmp_path) == attempt


@pytest.mark.parametrize('native', [False, True])
def test_wizards_preserve_saved_smart_values_and_optional_opera(app, tmp_path, native):
    config = (full_native_settings() if native else copy.deepcopy(DEFAULTS))
    config.update(setup_version=2, automation_mode='monthly', browser_cleanup_browsers=['firefox'],
        smart_cleaning_enabled=True, smart_cleaning_threshold_bytes=500_000_001,
        smart_cleaning_cooldown_seconds=7200, browser_exit_cleaning_enabled=False,
        browser_exit_grace_seconds=23, smart_maintenance_poll_seconds=71)
    backend = fixture_backend(tmp_path, config)
    wizard = NativeSetupWizard(backend) if native else InitialSetupWizard(backend)
    try:
        collected = wizard.collect() if native else wizard.collect_choices()
        assert all(collected[key] == config[key] for key in SMART_DEFAULTS)
        assert collected['automation_mode'] == 'monthly'
        assert collected['browser_cleanup_browsers'] == ['firefox']
        if native:
            opera = next(wizard.browsers.item(index) for index in range(wizard.browsers.count())
                         if wizard.browsers.item(index).data(Qt.UserRole) == 'opera')
            assert opera.checkState() == Qt.Unchecked
            opera.setCheckState(Qt.Checked)
        else:
            assert not wizard.browser_controls['opera'].isChecked()
            wizard.browser_controls['opera'].setChecked(True)
        updated = wizard.collect() if native else wizard.collect_choices()
        assert set(updated['browser_cleanup_browsers']) == {'firefox', 'opera'}
        wizard.reject()
        backend.save_preferences.assert_not_called()
        backend.optimize.assert_not_called()
        backend.analysis_start.assert_not_called()
        assert backend.preferences() == config
    finally:
        wizard.deleteLater()
        app.processEvents()


@pytest.mark.parametrize('native', [False, True])
def test_wizards_smart_defaults_off_and_monthly_choice_available(app, tmp_path, native):
    config = full_native_settings() if native else copy.deepcopy(DEFAULTS)
    backend = fixture_backend(tmp_path, config)
    wizard = NativeSetupWizard(backend) if native else InitialSetupWizard(backend)
    try:
        assert not wizard.controls['smart_cleaning_enabled'].isChecked()
        assert not wizard.controls['browser_exit_cleaning_enabled'].isChecked()
        if native:
            assert wizard.cadence.findData('monthly') >= 0
        else:
            assert 'monthly' in wizard.cadence_buttons
    finally:
        wizard.reject()
        wizard.deleteLater()
        app.processEvents()
