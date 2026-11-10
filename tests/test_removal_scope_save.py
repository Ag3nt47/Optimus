"""Only reviewed settings saves may approve removal scopes; no host services run."""
import copy
import json
from unittest.mock import Mock

import pytest
from PySide6.QtWidgets import QApplication

from optimus.backend import Backend
from optimus.onboarding import InitialSetupWizard, removal_guard_save_warning
from optimus.removal_enforcement import SCOPE_KEY, approved, scope_hash, stamp_approval
from optimus.workflow import DEFAULTS, Preferences
from test_onboarding import FixtureBackend


@pytest.fixture
def backend(tmp_path, monkeypatch):
    result = Backend.__new__(Backend)
    result.data_dir = tmp_path
    result.preference_store = Preferences(tmp_path)
    old = copy.deepcopy(DEFAULTS)
    old.update(setup_version=2, launch_at_sign_in=False)
    # Simulate an older configuration that never displayed the new scopes.
    for key in ('office365_desktop_debloat_enabled', 'office_desktop_debloat_enabled',
                'windows_removal_enforcement_enabled', SCOPE_KEY,
                'privacy_disable_online_speech_enabled', 'privacy_disable_voice_activation_enabled',
                'privacy_disable_activity_history_enabled'):
        old.pop(key, None)
    result.preference_store.path.write_text(json.dumps(old), encoding='utf-8')
    result._test_guard = Mock()
    result._test_guard.configure.return_value = {'ok': True, 'status': {'enabled': False}}
    result._removal_guard_service = Mock(return_value=result._test_guard)
    result._test_startup = Mock()
    result._startup_launch_service = Mock(return_value=result._test_startup)
    monkeypatch.setattr('optimus.backend.sys.frozen', True, raising=False)
    monkeypatch.setattr('optimus.backend.sys.platform', 'win32')
    return result


@pytest.mark.parametrize('operation', ['ordinary', 'startup', 'toolkit', 'smart'])
def test_unrelated_saves_cannot_approve_new_default_on_scopes(backend, operation):
    before = backend.preferences()
    assert before['office365_desktop_debloat_enabled'] is True
    assert not approved(before)
    if operation == 'startup':
        backend.startup_launch_status = Mock(return_value={})
        backend.set_startup_launch(True)
    elif operation == 'toolkit':
        backend._toolkit = Mock(return_value=Mock(status=Mock(return_value={})))
        backend.configure_toolkit(before['toolkit_settings'])
    elif operation == 'smart':
        backend._smart_service = Mock(return_value=Mock(configure=Mock(return_value={})))
        backend.configure_smart_maintenance({})
    else:
        backend.save_preferences(dict(before, analysis_interval=9))
    after = backend.preferences()
    assert after[SCOPE_KEY] == '' and not approved(after)
    assert after['office365_desktop_debloat_enabled'] is True
    assert backend._test_guard.configure.call_args.args[0] is False


def test_generic_save_preserves_valid_approval_but_cannot_restamp_changed_scope(backend):
    reviewed = stamp_approval(backend.preferences())
    saved = backend.save_preferences(reviewed)
    assert approved(saved)
    unchanged = backend.save_preferences(dict(saved, analysis_interval=12))
    assert unchanged[SCOPE_KEY] == reviewed[SCOPE_KEY] and approved(unchanged)
    changed = backend.save_preferences(dict(unchanged, office_desktop_debloat_enabled=False))
    assert changed[SCOPE_KEY] == reviewed[SCOPE_KEY]
    assert changed[SCOPE_KEY] != scope_hash(changed) and not approved(changed)
    assert backend._test_guard.configure.call_args.args[0] is False


def test_guard_registration_failure_preserves_choices_and_exposes_visible_outcome(backend):
    backend._test_guard.configure.side_effect = RuntimeError('Fixture administrator approval was declined')
    reviewed = stamp_approval(backend.preferences())
    saved = backend.save_preferences(reviewed)
    assert approved(saved) and backend.preferences() == saved
    assert backend.last_removal_guard_registration['status'] == 'needs_review'
    assert 'scheduled re-removal is unavailable' in removal_guard_save_warning(backend)
    assert 'approval was declined' in removal_guard_save_warning(backend)
    record = json.loads((backend.data_dir / 'removal-guard-registration-status.json').read_text())
    assert record == backend.last_removal_guard_registration
    backend._test_guard.configure.side_effect = None
    backend.save_preferences(saved)
    assert removal_guard_save_warning(backend) == ''
    assert json.loads((backend.data_dir / 'removal-guard-registration-status.json').read_text())['status'] == 'configured'


def test_setup_save_stamps_only_after_review_without_running_maintenance(tmp_path):
    app = QApplication.instance() or QApplication([])
    fixture = FixtureBackend(tmp_path)
    wizard = InitialSetupWizard(fixture, first_run=True)
    try:
        assert not approved(wizard.collect_choices())
        assert fixture.saved == []
        wizard.run_now_checkbox.setChecked(False)
        wizard.accept()
        assert len(fixture.saved) == 1
        assert approved(fixture.saved[0]) and approved(wizard.saved_preferences)
        assert wizard.run_now is False
        fixture.optimize.assert_not_called()
    finally:
        wizard.deleteLater()
        app.processEvents()
