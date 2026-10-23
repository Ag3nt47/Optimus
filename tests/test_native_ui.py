"""Native presentation renders and stores explicit choices without host mutations."""
import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from PySide6.QtWidgets import QApplication, QDialog, QPushButton

from optimus.native_settings import NativePreferences, full_native_settings
from optimus.native_ui import NativeOptimusWindow, NativeSetupWizard


@pytest.fixture(scope='module')
def app():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def backend(tmp_path):
    return SimpleNamespace(data_dir=tmp_path, preferences=lambda: NativePreferences.validate(full_native_settings()),
        cleanup_categories=lambda: [{'id': 'user-temp', 'name': 'Private user temporary files', 'available': True}],
        source_root=lambda: Path(__file__).parents[1], optimize=Mock(), analysis_stop=Mock(), analysis_start=Mock(),
        save_preferences=Mock(), close=Mock())


def test_native_main_keeps_exact_four_buttons_and_settings_constructs(app, backend):
    window = NativeOptimusWindow(backend, smoke_test=True)
    try:
        assert [button.text() for button in window.main_buttons] == ['1 click Optimize and debloat', 'Settings', 'Sandbox', 'Revert']
        window.start_optimization()
        backend.optimize.assert_not_called()
        window.open_settings()
        settings = window._settings_window
        assert settings.tabs.count() == 12
        assert 'Registry' not in [settings.tabs.tabText(i) for i in range(settings.tabs.count())]
        assert settings.process_harness_panel.actions.count() == 0
        assert settings.privacy_panel._plan is None
    finally:
        window.shutdown()
        if window._settings_window:
            window._settings_window.close()
        window.close()


def test_native_wizard_defaults_do_not_accept_update_or_file_change_approvals(app, backend):
    wizard = NativeSetupWizard(backend, first_run=True)
    try:
        value = wizard.collect()
        assert value['optimization_mode'] == 'full'
        assert value['software_update_policy'] == 'review'
        assert not value['software_update_agreements_accepted']
        assert not value['startup_changes_approved']
        assert not value['duplicate_changes_approved']
        assert wizard.run_after.isChecked()
        assert wizard.pageIds() and len(wizard.pageIds()) == 6
        assert wizard.browsers.count() == 7
        opera = wizard.browsers.item(6)
        assert opera.text().startswith('Opera')
        assert not opera.checkState().value
    finally:
        wizard.reject()


def test_native_wizard_saved_software_selection_is_not_silently_replaced(app, backend):
    value = NativePreferences.validate(dict(full_native_settings(), software_update_ids=['apt:fixture']))
    wizard = NativeSetupWizard(backend, preferences=value)
    try:
        assert wizard.collect()['software_update_ids'] == ['apt:fixture']
        wizard._software_ready({'updates': [{'id': 'apt:new', 'name': 'Fixture', 'installedVersion': '1', 'availableVersion': '2'}]})
        # An empty current offer for a previously selected app does not revoke
        # its future update choice or silently enroll a newly discovered app.
        assert wizard.collect()['software_update_ids'] == ['apt:fixture']
        from optimus.native_ui import checked_data
        assert checked_data(wizard.software) == []
        assert not wizard.controls['software_update_agreements_accepted'].isChecked()
    finally:
        wizard.reject()


def test_native_select_all_respects_approval_limit_without_accepting_agreements(app, backend):
    from optimus.native_ui import checked_data
    wizard = NativeSetupWizard(backend)
    try:
        wizard._software_ready({'updates': [{'id': 'apt:fixture' + str(i)} for i in range(101)]})
        page = wizard.software.parentWidget()
        select = next(button for button in page.findChildren(QPushButton) if button.text() == 'Select all')
        select.click()
        assert len(checked_data(wizard.software)) == 100
        assert not wizard.controls['software_update_agreements_accepted'].isChecked()
        assert not wizard.controls['software_update_all_approved'].isChecked()
    finally:
        wizard.reject()
