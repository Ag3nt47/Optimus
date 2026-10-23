"""Per-launch approval uses saved fixture settings, never host maintenance."""
import copy
import os
from types import SimpleNamespace
from unittest.mock import Mock

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

import pytest
from PySide6.QtWidgets import QApplication, QDialog, QPushButton

from optimus.launch_review import LaunchConsent, ask_launch_review, launch_warning, scope_fingerprint
from optimus.native_ui import NativeOptimusWindow
from optimus.ui import OptimusWindow
from optimus.workflow import DEFAULTS
from optimus.automation import CURRENT_SETUP_VERSION
from test_onboarding import FixtureBackend


def settings(**changes):
    result = copy.deepcopy(DEFAULTS)
    result.update(setup_version=CURRENT_SETUP_VERSION, cleanup_enabled=False, browser_cache_enabled=False)
    result.update(changes)
    return result


def test_approval_is_local_to_one_launch_and_order_independent():
    value = settings(); first, second = LaunchConsent(), LaunchConsent()
    assert not first.allows(value)
    first.approve(value)
    assert first.allows(dict(reversed(list(value.items())))) and not second.allows(value)


@pytest.mark.parametrize('change', [dict(m365_debloat_enabled=True), dict(cleanup_age_days=0),
    dict(consumer_debloat_package_names=['Microsoft.Copilot']), dict(browser_keep_domains=[]),
    dict(software_update_ids=['Fixture.NewPackage']), dict(smart_cleaning_enabled=True)])
def test_changed_saved_scopes_invalidate_approval(change):
    original = settings(browser_keep_domains=['example.com'])
    gate = LaunchConsent(); gate.approve(original)
    changed = dict(original, **change)
    assert not gate.allows(changed)
    assert not gate.allows(original)  # Returning to an older scope does not revive approval.


def test_invalid_scopes_do_not_generate_approval():
    gate = LaunchConsent()
    for value in (None, [], {'invalid': float('nan')}):
        with pytest.raises((ValueError, TypeError)):
            gate.approve(value)


def test_warning_names_actual_selected_app_and_browser_data_risks():
    text = launch_warning(settings(cleanup_enabled=True, cleanup_age_days=0,
        consumer_debloat_enabled=True, consumer_debloat_package_names=['Microsoft.Copilot'],
        m365_debloat_enabled=True, browser_cookies_enabled=True, browser_history_enabled=True,
        browser_cleanup_browsers=['opera'], browser_keep_domains=['example.com']))
    assert 'Microsoft.Copilot' in text and 'Microsoft 365 hub/companion' in text
    assert 'cookies' in text and 'history' in text and 'opera' in text and 'example.com' in text
    assert 'sign you out' in text and 'irreversible' in text and '0 day(s)' in text
    assert 'Adjust settings is the default' in text


def test_warning_does_not_invent_removal_or_install_scopes():
    text = launch_warning(settings(software_updates_enabled=True, driver_updates_enabled=True))
    assert 'Microsoft 365 hub/companion' not in text and 'Uninstall' not in text
    assert 'Software update discovery only' in text and 'Driver update discovery only' in text
    assert 'Install applicable Windows driver' not in text


def test_warning_explains_missing_setup_and_low_intensity_age():
    text = launch_warning(settings(setup_version=0, cleanup_enabled=True, intensity='Low', cleanup_age_days=0))
    assert '30 day(s)' in text and 'shows a fresh review' in text


def test_privacy_warning_discloses_recall_deletion_and_clipboard_scope_only_when_selected():
    saved = settings(privacy_disable_recall_snapshots_enabled=False,
                     privacy_disable_clipboard_sync_enabled=False)
    text = launch_warning(saved)
    assert 'Recall' not in text and 'cross-device clipboard' not in text
    text = launch_warning(dict(saved, privacy_disable_recall_snapshots_enabled=True,
                               privacy_disable_clipboard_sync_enabled=True))
    assert 'permanently deletes previous Recall snapshots' in text
    assert 'cannot recover deleted snapshots' in text and 'irreversible' in text
    assert 'local clipboard history is unchanged' in text
    assert 'edition/build limits' in text


@pytest.mark.parametrize('key', ['privacy_block_silent_consumer_apps_enabled',
    'privacy_disable_cloud_promotions_enabled', 'privacy_disable_clipboard_sync_enabled',
    'privacy_disable_recall_snapshots_enabled'])
def test_enabling_a_new_privacy_scope_revokes_launch_approval(key):
    saved = settings(**{key: False})
    gate = LaunchConsent(); gate.approve(saved)
    assert not gate.allows(dict(saved, **{key: True}))


def test_dialog_default_is_adjust_settings_and_close_grants_nothing(monkeypatch):
    app = QApplication.instance() or QApplication([])
    parent = SimpleNamespace()
    def inspect(dialog):
        buttons = {button.text():button for button in dialog.findChildren(QPushButton)}
        assert set(buttons) == {'Auto Clean','Adjust settings'}
        assert buttons['Adjust settings'].isDefault() and not buttons['Auto Clean'].isDefault()
        return QDialog.Rejected
    monkeypatch.setattr(QDialog, 'exec', inspect)
    assert ask_launch_review(None, settings()) is None
    app.processEvents()


@pytest.fixture(params=[OptimusWindow, NativeOptimusWindow], ids=['windows','native'])
def window(request, tmp_path):
    app = QApplication.instance() or QApplication([])
    backend = FixtureBackend(tmp_path, settings())
    result = request.param(backend, smoke_test=True)
    result.smoke_test = False
    result._launch_consent.required = True
    result._session_approved_callback = Mock()
    result._session_invalidated_callback = Mock()
    yield result, backend
    result._quitting = True
    result._settings_window = result._setup_wizard = None
    result.shutdown(); result.close(); result.deleteLater(); app.processEvents()


def test_adjust_choice_never_starts_clean_or_automatic_runtime(window, monkeypatch):
    owner, backend = window
    monkeypatch.setattr(owner, '_ask_launch_review', Mock(return_value='adjust_settings'))
    monkeypatch.setattr(owner, 'open_settings', Mock())
    assert not owner.review_launch()
    owner.open_settings.assert_called_once_with(page='Settings')
    assert not owner.session_approved()
    backend.optimize.assert_not_called(); owner._session_approved_callback.assert_not_called()


def test_auto_choice_approves_current_snapshot_then_starts_once(window, monkeypatch):
    owner, backend = window
    monkeypatch.setattr(owner, '_ask_launch_review', Mock(return_value='auto_clean'))
    monkeypatch.setattr(owner, 'start_optimization', Mock())
    assert owner.review_launch() and owner.session_approved()
    owner.start_optimization.assert_called_once_with()
    owner._session_approved_callback.assert_called_once_with()
    backend.optimize.assert_not_called()


def test_invalidation_remains_fail_closed_if_runtime_stop_callback_fails(window):
    owner, backend = window
    owner._launch_consent.approve(backend.preferences())
    owner._session_invalidated_callback = Mock(side_effect=RuntimeError('fixture stop unavailable'))
    owner.invalidate_session_review('Automatic maintenance is paused.')
    assert not owner.session_approved()
    assert 'fixture stop unavailable' in owner.status.text()
    backend.optimize.assert_not_called()


def test_original_optimize_entrypoint_cannot_bypass_launch_review(window, monkeypatch):
    owner, backend = window
    monkeypatch.setattr(owner, '_ask_launch_review', Mock(return_value='adjust_settings'))
    monkeypatch.setattr(owner, 'open_settings', Mock())
    owner.start_optimization()
    backend.optimize.assert_not_called()
    assert not owner.session_approved()


def test_changed_scope_during_dialog_gets_refreshed_review_before_start(window, monkeypatch):
    owner, backend = window
    seen=[]
    def answer(value):
        seen.append(copy.deepcopy(value))
        if len(seen) == 1: backend.settings['m365_debloat_enabled'] = True
        return 'auto_clean'
    monkeypatch.setattr(owner, '_ask_launch_review', answer)
    monkeypatch.setattr(owner, 'start_optimization', Mock())
    assert owner.review_launch() and len(seen) == 2
    assert not seen[0]['m365_debloat_enabled'] and seen[1]['m365_debloat_enabled']
    owner.start_optimization.assert_called_once_with()


def test_incomplete_setup_must_finish_then_get_second_review(window, monkeypatch):
    owner, backend = window
    backend.settings['setup_version'] = 0
    seen=[]
    monkeypatch.setattr(owner, '_ask_launch_review', lambda value:seen.append(copy.deepcopy(value)) or 'auto_clean')
    def setup(**kwargs):
        assert not owner.session_approved()
        backend.settings.update(setup_version=CURRENT_SETUP_VERSION, m365_debloat_enabled=True)
        return True
    monkeypatch.setattr(owner, 'open_initial_setup', setup)
    monkeypatch.setattr(owner, 'start_optimization', Mock())
    assert owner.review_launch() and len(seen) == 2
    assert seen[0]['setup_version'] == 0 and seen[1]['m365_debloat_enabled']
    backend.analysis_start.assert_not_called()


def test_scope_change_pauses_session_and_blocks_queued_fixes(window):
    owner, backend = window
    owner._launch_consent.approve(backend.preferences())
    owner._automation_controller = SimpleNamespace(stop=Mock())
    owner._automation_timer = SimpleNamespace(stop=Mock())
    backend.settings['cleanup_enabled'] = True
    assert not owner.session_approved()
    owner.invalidate_session_review()
    owner._automation_controller.stop.assert_called_once()
    owner._automation_timer.stop.assert_called_once()
    owner._session_invalidated_callback.assert_called_once()


def test_safe_stop_cooperatively_cancels_optional_settings_operations(window, monkeypatch):
    owner, backend = window
    import threading
    backend.request_optional_stop = Mock()
    panel = SimpleNamespace(_cancel=threading.Event())
    owner._settings_window = SimpleNamespace(busy=True, recovery_panel=panel)
    monkeypatch.setattr(owner, 'quit_application', Mock(return_value=True))
    assert owner.request_safe_stop() and panel._cancel.is_set()
    backend.request_optional_stop.assert_called_once_with()
    owner.quit_application.assert_not_called()
    owner._settings_window.busy=False
    owner._finish_optional_stop()
    owner.quit_application.assert_called_once_with()
