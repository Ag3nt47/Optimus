"""Full-mode setup and recommendation UI exercise fixtures, never host mutations."""
import copy
import threading
import time
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication, QDialog, QLabel, QPushButton, QWidget

from optimus.onboarding import InitialSetupWizard
from optimus.ui import OptimusWindow, PrivacyRestoreDialog, SecurityReviewDialog
from optimus.workflow import CONSUMER_PACKAGE_NAMES, DEFAULTS, full_optimization_settings
from test_onboarding import FixtureBackend
from test_process_harness_panel import fixture_plan


@pytest.fixture(scope='module')
def qapp():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def full_backend(tmp_path, monkeypatch):
    monkeypatch.setenv('LOCALAPPDATA', str(tmp_path))
    return FixtureBackend(tmp_path, full_optimization_settings())


def dispose(value, qapp):
    if hasattr(value, 'shutdown'):
        value.shutdown()
    value.close()
    value.deleteLater()
    qapp.processEvents()


def test_first_full_setup_defaults_to_startup_all_ages_and_run_after_save(qapp, full_backend):
    wizard = InitialSetupWizard(full_backend, first_run=True)
    try:
        assert wizard.optimization_mode.currentData() == 'full'
        assert wizard.cadence_buttons['startup'].isChecked()
        assert wizard.run_now_checkbox.isChecked()
        assert wizard.controls['cleanup_age_days'].value() == 0
        assert wizard.controls['request_admin_on_startup'].isChecked()
        for key in ('browser_cache_enabled', 'browser_history_enabled', 'browser_cookies_enabled',
                    'browser_download_history_enabled', 'privacy_advertising_enabled',
                    'privacy_diagnostics_enabled', 'security_review_enabled', 'spyware_scan_enabled',
                    'post_process_review_enabled', 'consumer_debloat_enabled'):
            assert wizard.controls[key].isChecked()
        assert wizard.software_policy.currentData() == 'review'
        assert not wizard.software_agreements.isChecked()
        assert not wizard.driver_consent.isChecked()
        assert all(control.isChecked() for control in wizard.consumer_package_controls.values())
        assert set(wizard.consumer_package_controls) == set(CONSUMER_PACKAGE_NAMES)
        full_backend.optimize.assert_not_called()
        assert full_backend.saved == []
    finally:
        wizard.reject()
        dispose(wizard, qapp)


def test_saved_full_mode_keeps_user_changes_on_reopen(qapp, full_backend):
    changes = {'cleanup_age_days': 22, 'browser_cookies_enabled': False, 'browser_cleanup_browsers': ['chrome'],
               'browser_keep_domains': ['example.com'], 'privacy_diagnostics_enabled': False,
               'privacy_disable_app_diagnostics': False, 'automation_mode': 'weekly',
               'request_admin_on_startup': False, 'consumer_debloat_package_names': [CONSUMER_PACKAGE_NAMES[0]]}
    full_backend.settings.update(changes)
    wizard = InitialSetupWizard(full_backend, first_run=False)
    try:
        selected = wizard.collect_choices()
        assert selected['optimization_mode'] == 'full'
        for key, value in changes.items():
            assert selected[key] == value
        assert not wizard.run_now_checkbox.isChecked()
        assert full_backend.saved == []
    finally:
        wizard.reject()
        dispose(wizard, qapp)


def test_selecting_full_applies_once_and_preserves_explicit_scopes(qapp, tmp_path):
    backend = FixtureBackend(tmp_path, {'browser_cleanup_browsers': ['firefox'], 'browser_keep_domains': ['example.com'],
                                       'software_update_exclude_ids': ['Fixture.Excluded'],
                                       'duplicate_roots': [r'D:\Fixture\Documents']})
    wizard = InitialSetupWizard(backend)
    try:
        assert wizard.optimization_mode.currentData() == 'custom'
        wizard.optimization_mode.setCurrentIndex(wizard.optimization_mode.findData('full'))
        selected = wizard.collect_choices()
        assert selected['optimization_mode'] == 'full' and selected['cleanup_age_days'] == 0
        assert selected['browser_cleanup_browsers'] == ['firefox']
        assert selected['browser_keep_domains'] == ['example.com']
        assert selected['software_update_exclude_ids'] == ['Fixture.Excluded']
        assert selected['duplicate_roots'] == [r'D:\Fixture\Documents']
        assert wizard.run_now_checkbox.isChecked()
        wizard.controls['browser_cookies_enabled'].setChecked(False)
        wizard.controls['privacy_diagnostics_enabled'].setChecked(False)
        wizard.refresh_summary()
        assert wizard.collect_choices()['browser_cookies_enabled'] is False
        assert wizard.collect_choices()['privacy_diagnostics_enabled'] is False
        assert backend.saved == []
    finally:
        wizard.reject()
        dispose(wizard, qapp)


def test_consumer_package_scope_select_all_clear_all_and_exact_choices(qapp, full_backend):
    wizard = InitialSetupWizard(full_backend)
    try:
        wizard.selection_actions['consumer packages']['clear'].click()
        assert wizard.collect_choices()['consumer_debloat_package_names'] == []
        chosen = CONSUMER_PACKAGE_NAMES[-1]
        wizard.consumer_package_controls[chosen].setChecked(True)
        assert wizard.collect_choices()['consumer_debloat_package_names'] == [chosen]
        wizard.selection_actions['consumer packages']['select'].click()
        assert wizard.collect_choices()['consumer_debloat_package_names'] == list(CONSUMER_PACKAGE_NAMES)
        assert not any('bingwallpaper' in name.casefold() for name in CONSUMER_PACKAGE_NAMES)
        assert full_backend.saved == []
    finally:
        wizard.reject()
        dispose(wizard, qapp)


def test_windows_security_update_approval_needs_agreements(qapp, full_backend):
    wizard = InitialSetupWizard(full_backend)
    try:
        wizard.controls['windows_security_updates_enabled'].setChecked(True)
        wizard.controls['windows_security_updates_install_approved'].setChecked(True)
        with pytest.raises(ValueError, match='Microsoft update agreement'):
            wizard.collect_choices()
        wizard.controls['windows_security_update_agreements_accepted'].setChecked(True)
        assert wizard.collect_choices()['windows_security_updates_install_approved'] is True
        wizard.controls['windows_security_updates_enabled'].setChecked(False)
        assert wizard.collect_choices()['windows_security_updates_install_approved'] is False
        assert wizard.collect_choices()['windows_security_update_agreements_accepted'] is False
    finally:
        wizard.reject()
        dispose(wizard, qapp)


def test_full_summary_explains_privacy_security_consumer_scope_and_post_review(qapp, full_backend):
    wizard = InitialSetupWizard(full_backend)
    try:
        before = wizard.collect_choices()
        wizard.refresh_summary()
        text = wizard.summary.toPlainText()
        for expected in ('Full Optimization', 'All eligible', 'Microsoft Defender quick scan',
                         'Yes/No', 'Post-completion', 'Consumer package scope', CONSUMER_PACKAGE_NAMES[0]):
            assert expected in text
        assert wizard.collect_choices() == before
    finally:
        wizard.reject()
        dispose(wizard, qapp)


def test_settings_keeps_saved_full_overrides_without_running_preset(qapp, full_backend):
    full_backend.settings.update({'cleanup_age_days': 17, 'browser_cookies_enabled': False, 'automation_mode': 'manual',
                                  'consumer_debloat_package_names': [CONSUMER_PACKAGE_NAMES[0]]})
    window = OptimusWindow(full_backend, smoke_test=True)
    try:
        window.open_settings(page='Settings')
        settings = window._settings_window
        assert settings.workflow_controls['optimization_mode'].currentData() == 'full'
        assert settings.workflow_controls['cleanup_age_days'].value() == 17
        assert not settings.workflow_controls['browser_cookies_enabled'].isChecked()
        assert settings.workflow_controls['automation_mode'].currentData() == 'manual'
        assert settings.workflow_mode_overlay == {}
        assert full_backend.saved == []
        full_backend.optimize.assert_not_called()
    finally:
        dispose(window, qapp)


def test_settings_full_selection_saves_mode_keys_and_exact_scope(qapp, tmp_path, monkeypatch):
    monkeypatch.setenv('LOCALAPPDATA', str(tmp_path))
    backend = FixtureBackend(tmp_path, {'browser_cleanup_browsers': ['chrome'], 'browser_keep_domains': ['example.com'],
                                      'software_update_exclude_ids': ['Fixture.Excluded']})
    window = OptimusWindow(backend, smoke_test=True)
    try:
        window.open_settings(page='Settings')
        settings = window._settings_window
        settings._run = Mock()
        settings.workflow_controls['optimization_mode'].setCurrentIndex(0)
        assert settings.workflow_controls['cleanup_age_days'].value() == 0
        settings.workflow_controls['browser_cookies_enabled'].setChecked(False)
        for name, control in settings.consumer_package_controls.items():
            control.setChecked(name == CONSUMER_PACKAGE_NAMES[0])
        save = next(button for button in settings.findChildren(QPushButton) if button.text() == 'Save one-click configuration')
        save.click()
        args = settings._run.call_args.args
        assert args[0] == 'save_preferences'
        chosen = args[1]
        assert chosen['optimization_mode'] == 'full'
        assert chosen['automation_mode'] == 'startup'
        assert chosen['cleanup_age_days'] == 0 and chosen['browser_cookies_enabled'] is False
        assert chosen['browser_cleanup_browsers'] == ['chrome']
        assert chosen['browser_keep_domains'] == ['example.com']
        assert chosen['software_update_exclude_ids'] == ['Fixture.Excluded']
        assert chosen['consumer_debloat_package_names'] == [CONSUMER_PACKAGE_NAMES[0]]
        assert not chosen['software_update_agreements_accepted']
        backend.optimize.assert_not_called()
    finally:
        dispose(window, qapp)


def security_report():
    return {'recommendations': [
        {'id': 'fixture-one', 'title': 'Fixture recommendation one', 'reason': 'Fixture finding', 'action': 'Fixture action one'},
        {'id': 'fixture-two', 'title': 'Fixture recommendation two', 'reason': 'Another finding', 'action': 'Fixture action two', 'requires_admin': True}],
        'errors': [], 'failed_items': []}


def test_privacy_restore_requires_selection_and_yes_then_reports_partial_result(qapp):
    window = QWidget()
    window.busy = False
    window.status = QLabel('Fixture status')
    window._run = Mock()
    dialog = PrivacyRestoreDialog(window, {'items': [{'id': 'fixture-privacy-change', 'kind': 'privacy',
        'status': 'partial', 'created_at': '2026-10-21', 'changed_count': 2}], 'errors': []})
    try:
        assert dialog.choices.currentIndex() == 0
        assert not dialog.restore_button.isEnabled()
        dialog.restore_selected()
        window._run.assert_not_called()
        dialog.choices.setCurrentIndex(1)
        assert dialog.restore_button.isEnabled()
        dialog._approve_restore = Mock(return_value=False)
        dialog.restore_selected()
        window._run.assert_not_called()
        dialog._approve_restore.return_value = True
        dialog.restore_selected()
        assert window._run.call_args.args == ('restore_system_privacy', 'fixture-privacy-change')
        assert not dialog.restore_button.isEnabled()
        cancel = window._run.call_args.kwargs['cancel_event']
        dialog.reject()
        assert cancel.is_set()
        window._run.call_args.kwargs['done']({'status': 'partial', 'items': [{'status': 'blocked', 'reason': 'Fixture current state changed'}]})
        assert 'blocked' in dialog.details.toPlainText()
        assert 'Fixture current state changed' in dialog.details.toPlainText()
        assert not dialog._running
    finally:
        dialog.shutdown()
        dispose(dialog, qapp)
        window.deleteLater()


def finish_security(dialog, qapp):
    deadline = time.monotonic() + 2
    while dialog._worker is not None and time.monotonic() < deadline:
        qapp.processEvents()
        time.sleep(0.005)
    assert dialog._worker is None
    dialog.pool.waitForDone(1000)
    qapp.processEvents()


def test_security_review_starts_unselected_and_does_nothing_without_yes(qapp):
    backend = SimpleNamespace(apply_security_recommendation=Mock())
    dialog = SecurityReviewDialog(backend, security_report())
    try:
        assert not dialog.apply_button.isEnabled()
        assert all(not dialog.selection_control(row).isChecked() for row in range(dialog.table.rowCount()))
        dialog.apply_selected()
        backend.apply_security_recommendation.assert_not_called()
        dialog.selection_control(0).setChecked(True)
        dialog._ask_recommendation = Mock(return_value=False)
        dialog.apply_selected()
        backend.apply_security_recommendation.assert_not_called()
        assert 'Declined' in dialog.table.item(0, 3).text()
    finally:
        dialog.shutdown()
        dispose(dialog, qapp)


def test_security_details_keep_checks_mitre_update_and_legacy_coverage(qapp):
    report = security_report()
    report.update(checks=[{'id': 'fixture-check', 'status': 'review'}], mitre_note='Limited mapping, not a compliance claim.',
                  update_coverage={'source': 'Fixture update source'},
                  legacy_patch_review={'note': 'Provider routes only; no automatic unverified installation.'})
    dialog = SecurityReviewDialog(SimpleNamespace(), report)
    try:
        for expected in ('fixture-check', 'Limited mapping', 'Fixture update source', 'Provider routes only'):
            assert expected in dialog.details.toPlainText()
        dialog._active = 'fixture-one'
        dialog._item_result({'status': 'blocked', 'reason': 'Fixture policy'})
        assert 'fixture-check' in dialog.details.toPlainText()
        assert 'Fixture policy' in dialog.details.toPlainText()
    finally:
        dispose(dialog, qapp)


def test_security_questions_per_item_on_gui_apply_only_yes_on_worker(qapp):
    calls = []
    backend = SimpleNamespace(apply_security_recommendation=lambda identifier, cancel_event=None: calls.append((identifier, threading.get_ident())) or {'status': 'completed'})
    dialog = SecurityReviewDialog(backend, security_report())
    asked = []
    try:
        for row in range(2):
            dialog.selection_control(row).setChecked(True)
        dialog._ask_recommendation = lambda recommendation: asked.append((recommendation['id'], threading.get_ident())) or recommendation['id'] == 'fixture-two'
        dialog.apply_selected()
        finish_security(dialog, qapp)
        assert asked == [('fixture-one', threading.get_ident()), ('fixture-two', threading.get_ident())]
        assert len(calls) == 1 and calls[0][0] == 'fixture-two' and calls[0][1] != threading.get_ident()
        assert dialog.table.item(1, 3).text() == 'completed'
        assert all(not dialog.selection_control(row).isChecked() for row in range(2))
    finally:
        dialog.shutdown()
        dispose(dialog, qapp)


def test_security_permission_failure_is_visible(qapp):
    backend = SimpleNamespace(apply_security_recommendation=Mock(side_effect=PermissionError('Fixture permission denied')))
    dialog = SecurityReviewDialog(backend, security_report())
    try:
        dialog.selection_control(0).setChecked(True)
        dialog._ask_recommendation = Mock(return_value=True)
        dialog.apply_selected()
        finish_security(dialog, qapp)
        assert 'permission denied' in dialog.details.toPlainText()
        assert 'Failed' in dialog.table.item(0, 3).text()
    finally:
        dialog.shutdown()
        dispose(dialog, qapp)


def test_cancelling_security_batch_stops_later_approved_items(qapp):
    entered = threading.Event()
    calls = []

    def apply(identifier, cancel_event=None):
        calls.append(identifier)
        entered.set()
        cancel_event.wait(1)
        return {'status': 'cancelled'}

    dialog = SecurityReviewDialog(SimpleNamespace(apply_security_recommendation=apply), security_report())
    try:
        for row in range(2):
            dialog.selection_control(row).setChecked(True)
        dialog._ask_recommendation = Mock(return_value=True)
        dialog.apply_selected()
        assert entered.wait(1)
        dialog.reject()
        finish_security(dialog, qapp)
        assert calls == ['fixture-one']
        assert dialog._cancel.is_set()
    finally:
        dialog.shutdown()
        dispose(dialog, qapp)


def test_skipping_security_review_still_offers_process_review(qapp, full_backend, monkeypatch):
    window = OptimusWindow(full_backend, smoke_test=True)
    monkeypatch.setattr(SecurityReviewDialog, 'exec', lambda self: QDialog.Rejected)
    window.smoke_test = False
    window._ask_process_review = Mock(return_value=False)
    try:
        report = fixture_plan()
        window._offer_post_optimization_reviews({'security_review': security_report(), 'process_review': report}, {'post_process_review_enabled': True})
        window._ask_process_review.assert_called_once_with(report)
        assert not window._post_review_active
        full_backend.optimize.assert_not_called()
    finally:
        window.smoke_test = True
        dispose(window, qapp)


def test_security_dialog_failure_does_not_suppress_process_review(qapp, full_backend, monkeypatch):
    window = OptimusWindow(full_backend, smoke_test=True)
    monkeypatch.setattr(SecurityReviewDialog, 'exec', Mock(side_effect=RuntimeError('Fixture UI failure')))
    window.smoke_test = False
    window._ask_process_review = Mock(return_value=False)
    try:
        report = fixture_plan()
        window._offer_post_optimization_reviews({'security_review': security_report(), 'process_review': report}, {'post_process_review_enabled': True})
        window._ask_process_review.assert_called_once_with(report)
        assert not window._post_review_active
    finally:
        window.smoke_test = True
        dispose(window, qapp)


def test_accepting_process_review_opens_same_fresh_unselected_report(qapp, full_backend):
    window = OptimusWindow(full_backend, smoke_test=True)
    window.smoke_test = False
    review = SimpleNamespace(performance_tabs=Mock(), process_harness_panel=Mock(), raise_=Mock(), activateWindow=Mock())

    def open_settings(**kwargs):
        window._settings_window = review

    window.open_settings = Mock(side_effect=open_settings)
    window._ask_process_review = Mock(return_value=True)
    try:
        report = fixture_plan()
        window._offer_post_optimization_reviews({'process_review': report}, {'post_process_review_enabled': True})
        window.open_settings.assert_called_once_with(page='Performance')
        review.process_harness_panel._scan_ready.assert_called_once_with(report)
        review.performance_tabs.setCurrentIndex.assert_called_once_with(2)
        assert not window._post_review_active
    finally:
        window._settings_window = None
        window.smoke_test = True
        dispose(window, qapp)
