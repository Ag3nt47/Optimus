"""Retention choices require explicit scope; these fixtures never delete data."""
import copy
import os
import threading

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

import pytest
from PySide6.QtCore import QCoreApplication, QEvent
from PySide6.QtWidgets import QApplication, QDoubleSpinBox, QWidget

from optimus.launch_review import LaunchConsent, launch_warning
from optimus.native_ui import NativeSetupWizard
from optimus.onboarding import InitialSetupWizard
from optimus.retention_configuration import RETENTION_SCOPES
from optimus.ui import OptimizationProgressWindow
from optimus.unattended_configuration import approve_unattended_settings, matches_unattended
from optimus.workflow import DEFAULTS, Preferences
from test_onboarding import FixtureBackend
from test_unattended_configuration import SetupBackend


@pytest.fixture(scope='module')
def app():
    return QApplication.instance() or QApplication([])


def cleanup(widget, app):
    widget.deleteLater()
    QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)
    app.processEvents()


@pytest.mark.parametrize('native', (False, True))
def test_new_controls_stay_opt_in_with_independent_ages_and_no_constructor_actions(app, tmp_path, native):
    backend = SetupBackend(tmp_path, True) if native else FixtureBackend(tmp_path)
    before = backend.preferences()
    wizard = NativeSetupWizard(backend) if native else InitialSetupWizard(backend)
    try:
        choices = wizard.retention_cleanup
        for prefix, _, _ in RETENTION_SCOPES:
            toggle, age = choices.toggles[prefix + '_cleanup_enabled'], choices.days[prefix + '_retention_days']
            assert not toggle.isChecked()
            assert (age.minimum(), age.maximum(), age.value()) == (1, 3650, 7)
            assert not age.isEnabled()
        assert wizard.drive_guard.enabled.isChecked()
        assert isinstance(wizard.drive_guard.target, QDoubleSpinBox)
        assert (wizard.drive_guard.target.minimum(), wizard.drive_guard.target.maximum(), wizard.drive_guard.target.value()) == (1.0, 2.0, 2.0)
        assert wizard.drive_guard.target.singleStep() == 0.25
        if native:
            assert not choices.toggles['angerona_logs_cleanup_enabled'].isEnabled()
            assert 'Windows' in choices.toggles['angerona_logs_cleanup_enabled'].toolTip()
        else:
            wizard.selection_actions['cleanup']['select'].click()
            assert not any(control.isChecked() for control in choices.toggles.values())
        for index, (prefix, _, _) in enumerate(RETENTION_SCOPES):
            choices.days[prefix + '_retention_days'].setValue((index + 1) * 11)
        if native:
            wizard.intensity.setCurrentText('Low')
        else:
            wizard.controls['intensity'].setCurrentText('Low')
        assert [control.value() for control in choices.days.values()] == [11, 22, 33]
        assert backend.preferences() == before and backend.saved == []
        backend.optimize.assert_not_called()
        backend.analysis_start.assert_not_called()
    finally:
        wizard.reject()
        cleanup(wizard, app)


def test_full_setup_preset_does_not_select_archived_personal_conversations(app, tmp_path):
    backend = FixtureBackend(tmp_path)
    wizard = InitialSetupWizard(backend)
    try:
        wizard.optimization_mode.setCurrentIndex(wizard.optimization_mode.findData('full'))
        assert not wizard.controls['codex_archives_cleanup_enabled'].isChecked()
        assert not wizard.controls['angerona_logs_cleanup_enabled'].isChecked()
        assert not wizard.controls['failed_downloads_cleanup_enabled'].isChecked()
    finally:
        wizard.reject()
        cleanup(wizard, app)


def test_launch_warning_names_each_retention_age_and_irreversible_personal_history():
    settings = dict(DEFAULTS, setup_version=2, codex_archives_cleanup_enabled=True,
        codex_archives_retention_days=11, angerona_logs_cleanup_enabled=True,
        angerona_logs_retention_days=22, failed_downloads_cleanup_enabled=True,
        failed_downloads_retention_days=33, drive_space_guard_target_gib=1.25)
    text = launch_warning(settings)
    assert 'archived Codex conversations older than 11 day(s)' in text
    assert 'personal conversation history with no automatic undo' in text
    assert 'Angerona log files older than 22 day(s)' in text
    assert 'incomplete-download leftovers older than 33 day(s)' in text
    assert 'paused-download fragments are also discarded' in text
    assert 'Completed downloads' in text and '1.25 GiB free' in text
    assert 'does not reserve space or authorize additional deletion' in text
    assert 'File deletion' in text


@pytest.mark.parametrize('prefix,_,__', RETENTION_SCOPES)
@pytest.mark.parametrize('suffix,value', (('_cleanup_enabled', True), ('_retention_days', 14)))
def test_each_of_six_retention_scope_changes_requires_new_approval(prefix, _, __, suffix, value):
    saved = approve_unattended_settings(Preferences.validate(dict(DEFAULTS, setup_version=2,
        unattended_auto_clean_enabled=True)))
    consent = LaunchConsent()
    consent.approve(saved)
    changed = dict(saved, **{prefix + suffix: value})
    assert not matches_unattended(changed)
    assert not consent.allows(changed)
    missing = copy.deepcopy(saved)
    missing.pop(prefix + suffix)
    assert not matches_unattended(missing)
    with pytest.raises(ValueError):
        approve_unattended_settings(missing)


def test_read_only_drive_threshold_does_not_grant_or_revoke_deletion_approval():
    saved = approve_unattended_settings(Preferences.validate(dict(DEFAULTS, setup_version=2,
        unattended_auto_clean_enabled=True)))
    assert matches_unattended(dict(saved, drive_space_guard_target_gib=1.25, drive_space_guard_enabled=False))
    assert not saved['codex_archives_cleanup_enabled']


def test_drive_warning_shows_observed_free_target_and_shortfall_after_completion(app):
    owner = QWidget()
    progress = OptimizationProgressWindow(owner, threading.Event())
    payload = {'schema': 1, 'enabled': True, 'review_only': True, 'target_gib': 2.0,
        'target_bytes': 2147483648, 'complete': False, 'status': 'needs_review',
        'summary': 'One drive could not be measured.', 'warnings': ['C: below target'],
        'volumes': [
            {'name': 'C:', 'mountpoint': 'C:\\', 'free_bytes': 268435456,
             'target_bytes': 2147483648, 'shortfall_bytes': 1879048192, 'below_target': True},
            {'name': 'D:', 'mountpoint': 'D:\\', 'free_bytes': None,
             'target_bytes': 2147483648, 'shortfall_bytes': None, 'below_target': None}]}
    try:
        progress.update_progress({'stage': 'Inspect current resources', 'percentage': 5, 'space_guard': payload})
        text = progress.space_guard_status.text()
        assert 'WARNING: Drive space below target' in text
        assert not progress.space_guard_icon.pixmap().isNull()
        assert not progress.space_guard_icon.isHidden()
        assert 'C: free 0.25 GiB · target 2.00 GiB · shortfall 1.75 GiB' in text
        assert 'D: free unknown · target 2.00 GiB · shortfall unknown' in text
        progress.finish({'status': 'completed_with_errors', 'stages': [], 'space_guard': payload})
        assert progress.space_guard_status.text() == text
        assert not progress.space_guard_status.isHidden()
        assert 'does not reserve disk space' in progress.space_guard_status.accessibleDescription()
        progress._update_space_guard(dict(payload, volumes=[], warnings=[], summary='Volume inventory is unavailable.'))
        assert 'Free-space target: 2.00 GiB' in progress.space_guard_status.text()
        assert 'no verified volume measurement' in progress.space_guard_status.text()
    finally:
        progress.completed = True
        progress.close()
        owner.close()
        cleanup(progress, app)
        cleanup(owner, app)
