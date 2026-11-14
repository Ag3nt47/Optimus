"""macOS App Translocation must never become a dead sign-in entry."""
from __future__ import annotations

import plistlib
import sys

import pytest

from optimus import native_startup_registration as registration
from optimus.native_startup_registration import NativeStartupRegistration

TRANSLOCATED = ('/private/var/folders/hl/x/T/AppTranslocation/'
                'A51FD3E9-0F90-485D-B311-6C1BC3B1B607/d/Optimus 2.app/Contents/MacOS/Optimus')


def _service(tmp_path, command):
    home = tmp_path / 'home'
    home.mkdir(exist_ok=True)
    return NativeStartupRegistration(tmp_path / 'data', home=home, platform='darwin',
                                     launch_provider=lambda: list(command))


def test_translocation_paths_are_recognized():
    assert registration.is_translocated(TRANSLOCATED)
    assert registration.is_translocated(
        r'C:\Users\fixture\AppTranslocation\ID\d\Optimus.app\Contents\MacOS\Optimus')
    assert not registration.is_translocated('/Applications/Optimus.app/Contents/MacOS/Optimus')
    assert not registration.is_translocated('/Users/me/Applications/Optimus.app/Contents/MacOS/Optimus')


def test_frozen_translocated_app_refuses_to_register(monkeypatch, tmp_path):
    executable = tmp_path / 'AppTranslocation' / 'ID' / 'd' / 'Optimus.app' / 'Contents' / 'MacOS' / 'Optimus'
    executable.parent.mkdir(parents=True)
    executable.write_bytes(b'')
    monkeypatch.setattr(sys, 'executable', str(executable))
    monkeypatch.setattr(sys, 'frozen', True, raising=False)
    monkeypatch.setattr(sys, 'platform', 'darwin')
    with pytest.raises(ValueError, match='Move Optimus.app to Applications'):
        registration.launch_specification()


def test_frozen_app_outside_translocation_registers_itself(monkeypatch, tmp_path):
    executable = tmp_path / 'Applications' / 'Optimus.app' / 'Contents' / 'MacOS' / 'Optimus'
    executable.parent.mkdir(parents=True)
    executable.write_bytes(b'')
    monkeypatch.setattr(sys, 'executable', str(executable))
    monkeypatch.setattr(sys, 'frozen', True, raising=False)
    monkeypatch.setattr(sys, 'platform', 'darwin')
    assert registration.launch_specification() == [str(executable.resolve())]


def test_existing_translocated_entry_is_reported_stale_and_repairable(tmp_path):
    stale = _service(tmp_path, [TRANSLOCATED])
    stale.set_enabled(True)
    state = stale.status()
    assert state['state'] == 'stale' and state['enabled'] is False and state['owned'] is True
    assert 'Applications' in state['reason']

    repaired = _service(tmp_path, ['/Applications/Optimus.app/Contents/MacOS/Optimus'])
    repaired.set_enabled(True)
    assert repaired.status()['state'] == 'enabled'
    assert plistlib.loads(repaired.target.read_bytes())['ProgramArguments'] == [
        '/Applications/Optimus.app/Contents/MacOS/Optimus']


def test_stale_entry_can_be_turned_off(tmp_path):
    stale = _service(tmp_path, [TRANSLOCATED])
    stale.set_enabled(True)
    stale.set_enabled(False)
    assert stale.status()['state'] == 'absent' and not stale.target.exists()


def test_translocation_check_is_macos_only(tmp_path):
    home = tmp_path / 'home'
    home.mkdir()
    service = NativeStartupRegistration(tmp_path / 'data', home=home, platform='linux',
                                        launch_provider=lambda: [TRANSLOCATED])
    service.set_enabled(True)
    assert service.status()['state'] == 'enabled'
