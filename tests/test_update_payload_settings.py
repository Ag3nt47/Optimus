"""The requested default-on choice remains revocable and launch-scope bound."""
import json

import pytest

from optimus.launch_review import launch_warning
from optimus.native_settings import NativePreferences
from optimus.unattended_configuration import approve_unattended_settings, matches_unattended
from optimus.workflow import DEFAULTS, Preferences


def test_missing_old_flag_enables_requested_default_without_writing_preferences(tmp_path):
    old = dict(DEFAULTS)
    old.pop('update_payload_cleanup_enabled')
    path = tmp_path / 'preferences.json'
    path.write_text(json.dumps(old), encoding='utf-8')
    before = path.read_bytes()
    assert Preferences(tmp_path).load()['update_payload_cleanup_enabled'] is True
    assert path.read_bytes() == before
    assert NativePreferences.validate(old)['update_payload_cleanup_enabled'] is True


def test_explicit_disable_and_corrupt_preferences_never_authorize_cleanup(tmp_path):
    assert Preferences.validate(dict(DEFAULTS, update_payload_cleanup_enabled=False))['update_payload_cleanup_enabled'] is False
    (tmp_path / 'preferences.json').write_text('{invalid', encoding='utf-8')
    assert Preferences(tmp_path).load()['update_payload_cleanup_enabled'] is False
    assert NativePreferences(tmp_path).load()['update_payload_cleanup_enabled'] is False
    with pytest.raises(ValueError):
        Preferences.validate(dict(DEFAULTS, update_payload_cleanup_enabled=1))


def test_changed_cleanup_choice_invalidates_unattended_approval_and_is_described():
    approved = approve_unattended_settings(Preferences.validate(dict(DEFAULTS,
        setup_version=2, unattended_auto_clean_enabled=True)))
    assert matches_unattended(approved)
    approved['update_payload_cleanup_enabled'] = False
    assert not matches_unattended(approved)
    warning = launch_warning(DEFAULTS)
    assert 'tracked Optimus-owned installer downloads' in warning
    assert 'Personal Downloads' in warning and 'Windows-managed' in warning
    assert 'tracked Optimus-owned installer downloads' not in launch_warning(approved)
