"""Speed-package controls use only fixture backends and disposable UI settings."""
import copy
import os
import threading
import time
from unittest.mock import Mock

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

import pytest
from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QCheckBox, QPushButton

from optimus.automation import CURRENT_SETUP_VERSION
from optimus.native_settings import full_native_settings
from optimus.native_ui import NativeOptimusWindow, NativeSetupWizard
from optimus.onboarding import InitialSetupWizard
from optimus.ui import OptimusWindow
from optimus.workflow import full_optimization_settings
from test_onboarding import FixtureBackend


def report(**changes):
    value = {'id': 'a' * 32, 'status': 'completed', 'summary': '1 applied; 1 deferred. No measured speed gain is claimed.',
        'counts': {'applied': 1, 'deferred': 1}, 'restore_available': True,
        'items': [{'id': 'power', 'title': 'Supported AC power policy', 'status': 'applied',
            'reason': 'The recorded AC policy was verified.', 'before': 'balanced', 'desired': 'performance'},
            {'id': 'managed', 'title': 'Managed profile', 'status': 'deferred', 'reason': 'Current policy is managed.'}],
        'log_path': 'fixture-speed-report.json'}
    value.update(changes)
    return value


def wait_until(app, predicate):
    deadline = time.monotonic() + 5
    while not predicate() and time.monotonic() < deadline:
        app.processEvents()
        QTest.qWait(5)
    app.processEvents()
    assert predicate(), 'The fixture speed worker did not reach the expected state.'


@pytest.fixture(scope='module')
def app():
    return QApplication.instance() or QApplication([])


@pytest.fixture(params=[OptimusWindow, NativeOptimusWindow], ids=['windows', 'native'])
def window(request, tmp_path, app):
    backend = FixtureBackend(tmp_path, {'setup_version': CURRENT_SETUP_VERSION, 'speed_enhancements_enabled': True})
    backend.speed_enhancements_scan = Mock(return_value=report(status='review', restore_available=False))
    backend.apply_speed_enhancements = Mock(return_value=report())
    backend.speed_enhancements_history = Mock(return_value=[report()])
    backend.restore_speed_enhancements = Mock(return_value=report(status='restored',
        summary='Original recorded AC policy restored.', items=[{'id': 'power', 'title': 'Supported AC power policy',
            'status': 'restored', 'reason': 'Original value restored after fresh comparison.'}], restore_available=False))
    value = request.param(backend, smoke_test=True)
    value.open_settings(page='Settings')
    settings = value._settings_window
    settings.smoke_test = False
    value._test_panel = settings.speed_enhancements_panel
    yield value
    value._test_panel.shutdown()
    settings.pool.waitForDone(5000)
    app.processEvents()
    settings.close()
    value.shutdown()
    value.close()
    settings.deleteLater()
    value.deleteLater()
    app.processEvents()


def test_opening_settings_honors_saved_choice_and_never_scans_or_applies(window):
    panel = window._test_panel
    assert panel.enabled.isChecked()
    assert panel.apply_button.isEnabled()
    assert not panel.save_button.isEnabled()
    assert panel.details.isReadOnly()
    assert len(window.main_buttons) == 4
    for name in ('speed_enhancements_scan', 'apply_speed_enhancements', 'speed_enhancements_history', 'restore_speed_enhancements'):
        getattr(window.backend, name).assert_not_called()
    assert window.backend.saved == []
    window.backend.optimize.assert_not_called()


def test_save_changes_only_speed_choice_without_applying_it(window, app):
    panel = window._test_panel
    window.backend.settings['cleanup_age_days'] = 29
    panel.enabled.setChecked(False)
    assert not panel.apply_button.isEnabled()
    QTest.mouseClick(panel.save_button, Qt.LeftButton)
    wait_until(app, lambda: not panel.window.busy)
    assert window.backend.settings['speed_enhancements_enabled'] is False
    assert window.backend.settings['cleanup_age_days'] == 29
    assert not panel.apply_button.isEnabled()
    assert not panel.save_button.isEnabled()
    window.backend.apply_speed_enhancements.assert_not_called()
    window.backend.optimize.assert_not_called()
    assert 'no speed policy was changed now' in panel.status.text()
    if hasattr(panel.window, 'workflow_control_baseline'):
        assert panel.window.workflow_control_baseline['speed_enhancements_enabled'] is False


def test_review_and_apply_are_explicit_workers_with_readable_outcomes(window, app):
    panel = window._test_panel
    QTest.mouseClick(panel.review_button, Qt.LeftButton)
    wait_until(app, lambda: not panel.window.busy)
    window.backend.speed_enhancements_scan.assert_called_once_with()
    window.backend.apply_speed_enhancements.assert_not_called()
    assert 'Managed profile — deferred' in panel.details.toPlainText()
    assert 'Current policy is managed.' in panel.details.toPlainText()
    worker_threads = []
    def apply(*, progress_callback, cancel_event):
        worker_threads.append(threading.get_ident())
        progress_callback({'percentage': 50, 'description': 'Reading the current AC policy', 'log': 'Fixture policy readback'})
        return report()
    window.backend.apply_speed_enhancements.side_effect = apply
    QTest.mouseClick(panel.apply_button, Qt.LeftButton)
    wait_until(app, lambda: not panel.window.busy)
    window.backend.apply_speed_enhancements.assert_called_once()
    assert len(worker_threads) == 1 and worker_threads[0] != threading.get_ident()
    assert '1 applied; 1 deferred' in panel.status.text()
    assert 'Before: balanced' in panel.details.toPlainText()
    assert 'Target: performance' in panel.details.toPlainText()
    assert 'fixture-speed-report.json' in panel.details.toPlainText()
    assert panel.restore_button.isEnabled()
    window.backend.optimize.assert_not_called()


def test_busy_duplicate_click_cancel_and_settings_close_share_lifecycle(window, app):
    panel = window._test_panel
    entered = threading.Event()
    def apply(*, progress_callback, cancel_event):
        entered.set()
        assert cancel_event.wait(3)
        return report(status='cancelled', summary='Cancelled after the current item.', restore_available=False)
    window.backend.apply_speed_enhancements.side_effect = apply
    QTest.mouseClick(panel.apply_button, Qt.LeftButton)
    wait_until(app, entered.is_set)
    assert panel.window.busy
    assert not panel.enabled.isEnabled() and not panel.apply_button.isEnabled()
    panel.apply_now()
    panel.review()
    assert panel.window.close() is False
    QTest.mouseClick(panel.cancel_button, Qt.LeftButton)
    wait_until(app, lambda: not panel.window.busy)
    window.backend.apply_speed_enhancements.assert_called_once()
    window.backend.speed_enhancements_scan.assert_not_called()
    assert panel._cancel.is_set()
    assert 'Cancelled' in panel.status.text()
    assert panel.cancel_button.isHidden()


@pytest.mark.parametrize('approve', [False, True])
def test_restore_requires_exact_recorded_selection_and_confirmation(window, app, approve):
    panel = window._test_panel
    assert not panel.restore_button.isEnabled()
    panel.restore_selected()
    QTest.mouseClick(panel.history_button, Qt.LeftButton)
    wait_until(app, lambda: not panel.window.busy)
    assert panel.history.currentData()['id'] == 'a' * 32
    panel._approve_restore = Mock(return_value=approve)
    QTest.mouseClick(panel.restore_button, Qt.LeftButton)
    wait_until(app, lambda: not panel.window.busy)
    panel._approve_restore.assert_called_once_with(panel.history.currentData() if not approve else report())
    if approve:
        window.backend.restore_speed_enhancements.assert_called_once()
        assert window.backend.restore_speed_enhancements.call_args.args == ('a' * 32,)
        assert 'restored' in panel.details.toPlainText()
        assert panel.history.count() == 0 and not panel.restore_button.isEnabled()
    else:
        window.backend.restore_speed_enhancements.assert_not_called()
    window.backend.apply_speed_enhancements.assert_not_called()


def test_disabled_or_unsaved_choice_cannot_apply_and_errors_unblock_settings(window, app):
    panel = window._test_panel
    panel.sync_settings({'speed_enhancements_enabled': False})
    panel.enabled.setChecked(True)
    assert not panel.apply_button.isEnabled()
    panel.apply_now()
    window.backend.apply_speed_enhancements.assert_not_called()
    assert 'save' in panel.status.text()
    panel.sync_settings({'speed_enhancements_enabled': True})
    window.backend.apply_speed_enhancements.side_effect = RuntimeError('Fixture policy declined')
    QTest.mouseClick(panel.apply_button, Qt.LeftButton)
    wait_until(app, lambda: not panel.window.busy)
    assert 'Fixture policy declined' in panel.status.text()
    assert panel.apply_button.isEnabled() and panel.cancel_button.isHidden()


@pytest.mark.parametrize('native', [False, True], ids=['windows', 'native'])
def test_setup_full_choice_and_reopen_keep_speed_selection(app, tmp_path, native):
    settings = (full_native_settings if native else full_optimization_settings)()
    settings.update(setup_version=CURRENT_SETUP_VERSION, speed_enhancements_enabled=False)
    backend = FixtureBackend(tmp_path, settings)
    wizard = NativeSetupWizard(backend, preferences=copy.deepcopy(settings)) if native else InitialSetupWizard(backend, preferences=copy.deepcopy(settings))
    try:
        assert not wizard.controls['speed_enhancements_enabled'].isChecked()
        mode = wizard.mode if native else wizard.optimization_mode
        mode.setCurrentIndex(mode.findData('custom'))
        mode.setCurrentIndex(mode.findData('full'))
        assert wizard.controls['speed_enhancements_enabled'].isChecked()
        choices = wizard.collect() if native else wizard.collect_choices()
        assert choices['speed_enhancements_enabled'] is True
        if native:
            assert 'enabled' in wizard.speed_summary.text()
        else:
            wizard.refresh_summary()
            assert 'Automatically apply supported reversible speed enhancements' in wizard.summary.toPlainText()
        assert not backend.saved
        backend.optimize.assert_not_called()
    finally:
        wizard.reject()
        wizard.deleteLater()
        app.processEvents()


def test_windows_compatibility_choices_are_explicit_saved_scope_without_running_upgrades(window, app):
    settings = window._settings_window
    if isinstance(window, NativeOptimusWindow):
        labels = [control.text() for control in settings.findChildren(QCheckBox)]
        assert not any('WinGet compatibility' in label or 'MoSetup' in label for label in labels)
        return
    controls = settings.workflow_controls
    keys = ('update_compatibility_mode_enabled', 'windows_cpu_compatibility_enabled')
    for key in keys:
        assert not controls[key].isChecked()
        controls[key].setChecked(True)
    before = window.backend.preferences()
    save = next(button for button in settings.findChildren(QPushButton) if button.text() == 'Save one-click configuration')
    save.click()
    wait_until(app, lambda: not settings.busy)
    assert all(window.backend.settings[key] is True for key in keys)
    for key in ('software_update_policy', 'software_update_agreements_accepted', 'windows_security_updates_install_approved',
                'driver_policy', 'driver_changes_approved'):
        assert window.backend.settings[key] == before[key]
    window.backend.optimize.assert_not_called()
    window.backend.apply_speed_enhancements.assert_not_called()


@pytest.mark.parametrize('native', [False, True], ids=['windows', 'native'])
def test_compatibility_setup_choices_explain_limited_media_and_application_scope(app, tmp_path, native):
    settings = (full_native_settings if native else full_optimization_settings)()
    backend = FixtureBackend(tmp_path, settings)
    wizard = NativeSetupWizard(backend, preferences=settings) if native else InitialSetupWizard(backend, preferences=settings)
    try:
        keys = ('update_compatibility_mode_enabled', 'windows_cpu_compatibility_enabled')
        if native:
            assert all(key not in wizard.controls for key in keys)
            return
        assert all(not wizard.controls[key].isChecked() for key in keys)
        for key in keys:
            wizard.controls[key].setChecked(True)
        wizard.refresh_summary()
        summary = wizard.summary.toPlainText()
        assert 'WinGet compatibility mode (--force, non-security)' in summary
        assert 'do not retry unknown installer outcomes' in summary
        assert 'MoSetup with administrator access' in summary
        assert 'Installation Assistant may still block' in summary
        assert 'the OS upgrade itself is not automatic cleanup' in summary
        assert all(wizard.collect_choices()[key] is True for key in keys)
        assert not backend.saved
        backend.optimize.assert_not_called()
    finally:
        wizard.reject()
        wizard.deleteLater()
        app.processEvents()
