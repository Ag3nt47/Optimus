"""Native preference and sign-in transactions use owned disposable fixtures."""
import copy
import json
import os
from pathlib import Path
import plistlib
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from optimus import automation, filesystem
from optimus.native_settings import NativePreferences, WINDOWS_ONLY, full_native_settings
from optimus.native_startup_registration import NativeStartupRegistration


def config(**changes):
    value = full_native_settings()
    value.update(setup_version=automation.CURRENT_SETUP_VERSION)
    value.update(changes)
    return value


def test_native_full_defaults_enable_discovery_without_windows_actions_or_install_consent():
    value = NativePreferences.validate(full_native_settings())
    assert value['optimization_mode'] == 'full' and value['automation_mode'] == 'startup'
    assert value['software_updates_enabled'] and value['software_update_policy'] == 'review'
    assert not value['software_update_agreements_accepted']
    assert all(value[key] is False for key in WINDOWS_ONLY)
    assert not {'windows-temp', 'canonical-user-temp'} & set(value['cleanup_category_ids'])


@pytest.mark.parametrize('package', ['apt:libssl3:amd64', 'brew:homebrew/core/wget', 'dnf:kernel.x86_64', 'pacman:firefox'])
def test_native_package_identity_survives_validation(package):
    value = NativePreferences.validate(config(software_update_policy='selected',
        software_update_ids=[package], software_update_agreements_accepted=True))
    assert value['software_update_ids'] == [package]


@pytest.mark.parametrize('package', ['--force', 'apt:foo;touch', 'brew:../evil', 'apt:foo\nbar', 'apt:$(id)'])
def test_native_package_argument_injection_is_rejected(package):
    with pytest.raises(ValueError):
        NativePreferences.validate(config(software_update_ids=[package]))


def test_native_installation_requires_setup_scope_and_agreements():
    for changes in ({'software_update_agreements_accepted': False}, {'software_update_ids': []}, {'setup_version': 0}):
        value = config(software_update_policy='selected', software_update_ids=['apt:firefox'],
                       software_update_agreements_accepted=True)
        value.update(changes)
        with pytest.raises(ValueError):
            NativePreferences.validate(value)
    with pytest.raises(ValueError):
        NativePreferences.validate(config(software_update_policy='all_supported', software_update_agreements_accepted=True))


def test_native_duplicates_keep_posix_paths_and_require_scope_approval():
    settings = config(duplicates_enabled=True, duplicate_policy='keep_oldest', duplicate_changes_approved=True,
                      duplicate_roots=['/home/fixture/Documents'])
    assert NativePreferences.validate(settings)['duplicate_roots'] == ['/home/fixture/Documents']
    for changes in ({'duplicate_changes_approved': False}, {'setup_version': 0}, {'duplicate_roots': []},
                    {'duplicate_roots': ['/']}, {'duplicate_roots': ['/home/fixture/../other']}):
        with pytest.raises(ValueError):
            NativePreferences.validate(dict(settings, **changes))


def test_native_startup_hash_scope_and_consent_survive_without_windows_descriptors():
    record = {'Source': 'native-startup', 'Name': 'Fixture helper',
              'Path': '/home/fixture/.config/autostart/fixture.desktop', 'SHA256': 'a' * 64, 'Enabled': True}
    value = config(startup_review_enabled=True, startup_policy='disable_selected',
                   startup_changes_approved=True, startup_approved_entries=[record])
    assert NativePreferences.validate(value)['startup_approved_entries'] == [record]
    for changes in ({'startup_changes_approved': False}, {'startup_approved_entries': []},
                    {'startup_approved_entries': [dict(record, SHA256='bad')]}, {'setup_version': 0}):
        with pytest.raises(ValueError):
            NativePreferences.validate(dict(value, **changes))


def test_native_cadence_transactions_use_injected_native_validator(tmp_path):
    store = NativePreferences(tmp_path)
    value = config(duplicates_enabled=True, duplicate_roots=['/home/fixture/Documents'])
    saved = automation.save_setup(tmp_path, value, store.save, validator=store.validate)
    assert saved['duplicate_roots'] == value['duplicate_roots']
    attempt = automation.record_attempt(tmp_path, saved, validator=store.validate)
    assert not automation.due(saved, attempt, validator=store.validate)
    automation.record_attempt(tmp_path, saved, status='completed', run_id=attempt['run_id'], validator=store.validate)


@pytest.mark.parametrize('platform', ['darwin', 'linux'])
def test_native_signin_file_is_owned_and_can_be_rolled_back(tmp_path, platform):
    home = tmp_path / 'home'
    home.mkdir()
    command = ['/opt/Fixture App/Optimus', '--literal=%name']
    service = NativeStartupRegistration(tmp_path / 'data', home=home, platform=platform, launch_provider=lambda: command)
    assert service.status()['state'] == 'absent'
    receipt = service.set_enabled(True)
    assert service.status()['state'] == 'enabled'
    assert service.status()['owned'] is True
    raw = service.target.read_bytes()
    if platform == 'darwin':
        value = plistlib.loads(raw)
        assert value['ProgramArguments'] == command and value['KeepAlive'] is False
    else:
        assert 'Exec="/opt/Fixture App/Optimus" "--literal=%%name"' in raw.decode()
    service.rollback(receipt)
    assert not service.target.exists() and not service.manifest.exists()


def test_native_signin_preserves_unrecognized_changed_file(tmp_path):
    service = NativeStartupRegistration(tmp_path / 'data', home=tmp_path / 'home', platform='linux',
                                        launch_provider=lambda: ['/opt/Optimus'])
    receipt = service.set_enabled(True)
    service.target.write_text('[Desktop Entry]\nExec=fixture foreign program\n')
    assert service.status()['state'] == 'foreign'
    with pytest.raises(ValueError):
        service.set_enabled(False)
    with pytest.raises(ValueError):
        service.rollback(receipt)
    assert 'foreign' in service.target.read_text()


def test_native_signin_rejects_symlink_target_and_invalid_launch_arguments(tmp_path):
    service = NativeStartupRegistration(tmp_path / 'data', home=tmp_path / 'home', platform='linux',
                                        launch_provider=lambda: ['/opt/Optimus\nExec=evil'])
    with pytest.raises(ValueError):
        service.set_enabled(True)
    assert not service.target.exists()


@pytest.mark.parametrize('platform', ['darwin', 'linux'])
def test_native_data_uses_os_standard_location(tmp_path, monkeypatch, platform):
    install = tmp_path / 'install'
    install.mkdir()
    monkeypatch.setattr(filesystem.sys, 'platform', platform)
    monkeypatch.setattr(Path, 'home', classmethod(lambda cls: tmp_path / 'home'))
    monkeypatch.delenv('XDG_STATE_HOME', raising=False)
    expected = tmp_path / 'home' / ('Library/Application Support/Optimus' if platform == 'darwin' else '.local/state/optimus')
    assert filesystem.default_data_root(install) == expected.resolve()


def test_native_cli_audit_selects_native_backend_without_admin_request(tmp_path, monkeypatch):
    import optimus.app as app
    import optimus.native_backend as native_backend
    backend = SimpleNamespace(data_dir=tmp_path, export_report=Mock(return_value={'path': 'fixture'}), close=Mock())
    monkeypatch.setattr(app.sys, 'platform', 'linux')
    monkeypatch.setattr(native_backend, 'NativeBackend', lambda: backend)
    assert app.main(['--audit', '--output', str(tmp_path / 'report')]) == 0
    backend.export_report.assert_called_once_with(tmp_path / 'report')
    backend.close.assert_called_once_with()
