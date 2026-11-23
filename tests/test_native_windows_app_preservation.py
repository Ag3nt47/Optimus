"""Windows App is immutable; package/process/file providers are fixture-only."""
from __future__ import annotations

import copy
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from optimus import app_presence, audio_trackers, background_apps, cleaner, file_utilities, performance
from optimus.app_removal_protection import WINDOWS_APP_FAMILY, WINDOWS_APP_NAME, WINDOWS_APP_STORE_ID
from optimus.automated_software import fresh_candidates, run_software
from optimus.process_harness import _protected_executable
from optimus.software_app_rules import apply_update_rules, app_rules_summary
from optimus.workflow import DEFAULTS


IDENTITIES = ('Microsoft.WindowsApp', WINDOWS_APP_STORE_ID, WINDOWS_APP_NAME, WINDOWS_APP_FAMILY)
PACKAGE_EXE = r'C:\Program Files\WindowsApps\MicrosoftCorporationII.Windows365_2.0.0.0_x64__8wekyb3d8bbwe\WindowsApp.exe'


def offer(identity, name=None):
    return {'id': identity, 'package_id': identity, 'source': 'winget',
            'name': name or identity, 'version': '1.0', 'available': '2.0'}


def rule(identity, presence='keep_current'):
    return {'id': identity, 'source': 'winget', 'update_policy': 'always_ignore'
            if presence == 'keep_uninstalled' else 'always_update', 'presence': presence}


def test_windows_app_offers_are_inventoried_but_never_update_candidates():
    rows = [offer(identity) for identity in IDENTITIES]
    rows += [offer('Microsoft.WindowsAppSDK'), offer('Other.RemoteClient', name='Windows App')]
    scan = {'structured': True, 'exitCode': 0, 'source': 'winget', 'updates': rows}
    before = copy.deepcopy(scan)
    assert set(fresh_candidates(scan)) == {'Microsoft.WindowsAppSDK', 'Other.RemoteClient'}
    assert scan == before  # Inventory is preserved, including the protected app.


@pytest.mark.parametrize('identity', IDENTITIES)
def test_all_supported_software_updates_never_launch_windows_app(identity):
    provider = SimpleNamespace(updates_scan=Mock(return_value={
        'structured': True, 'exitCode': 0, 'source': 'winget', 'updates': [offer(identity)]}),
        update_app_unattended=Mock(side_effect=AssertionError('No Windows App updater may launch.')))
    settings = dict(DEFAULTS, software_updates_enabled=True, software_update_policy='all_supported',
                    software_update_all_approved=True, software_update_agreements_accepted=True)
    result = run_software(provider, settings)
    assert result['items'] == []
    provider.update_app_unattended.assert_not_called()


@pytest.mark.parametrize('presence', ['keep_installed', 'keep_uninstalled'])
@pytest.mark.parametrize('identity', IDENTITIES)
def test_saved_presence_never_installs_or_uninstalls_windows_app(identity, presence):
    backend = SimpleNamespace(system='win32')
    settings = {'software_updates_enabled': True, 'software_app_rules': [rule(identity, presence)]}
    result = app_presence.run_app_presence(backend, settings)
    assert result['items'][0]['status'] == 'skipped'
    assert result['items'][0]['result']['protected'] is True
    assert result['installer_started'] is False and result['outcomeKnown'] is True
    assert result['running'] is False


@pytest.mark.parametrize('method', ['readiness', 'execute'])
@pytest.mark.parametrize('action', ['install', 'uninstall'])
def test_direct_presence_entry_points_refuse_protected_identity_before_probing(method, action):
    backend = SimpleNamespace(maintenance=Mock(), is_admin=Mock(), security_status=Mock())
    provider = app_presence.WindowsAppPresence(backend)
    provider.inspect = Mock(side_effect=AssertionError('No provider query needed for immutable target.'))
    snapshot = {'id': 'Microsoft.WindowsApp', 'source': 'winget', 'protected': False}
    with pytest.raises(app_presence.PresencePreflightError, match='Windows App'):
        getattr(provider, method)(snapshot, action)
    provider.inspect.assert_not_called()
    backend.is_admin.assert_not_called()
    backend.security_status.assert_not_called()


def test_effective_rules_ignore_windows_app_without_rewriting_saved_choices():
    settings = dict(DEFAULTS, software_update_policy='selected',
        software_update_ids=['Microsoft.WindowsApp', 'Other.RemoteClient'],
        software_app_rules=[rule('Microsoft.WindowsApp', 'keep_uninstalled'), rule('Other.RemoteClient')])
    before = copy.deepcopy(settings)
    effective = apply_update_rules(settings)
    assert effective['software_update_ids'] == ['Other.RemoteClient']
    assert [row['id'] for row in effective['software_app_rules']] == ['Other.RemoteClient']
    summary = app_rules_summary(settings)
    assert summary['keep_uninstalled'] == []
    assert settings == before


@pytest.mark.parametrize('action', ['sleep', 'priority', 'affinity'])
def test_direct_process_mutations_refuse_windows_app(tmp_path, monkeypatch, action):
    process = SimpleNamespace(pid=200, name=Mock(return_value='WindowsApp.exe'),
        exe=Mock(return_value=PACKAGE_EXE), username=Mock(return_value='fixture\\user'),
        suspend=Mock(), nice=Mock(), cpu_affinity=Mock())
    monkeypatch.setattr(performance, '_PLATFORM', 'win32')
    monkeypatch.setattr(performance, '_fresh', Mock(return_value=process))
    monkeypatch.setattr(performance, '_service_ids', Mock(return_value=set()))
    controller = performance.PerformanceController(tmp_path / 'process-state.json')
    record = {'pid': 200, 'create_time': 1.0, 'exe': PACKAGE_EXE}
    with pytest.raises(performance.PerformanceError, match='Windows App'):
        if action == 'sleep':
            controller.sleep_process(record)
        elif action == 'priority':
            controller.set_process_priority(record, 'High')
        else:
            controller.set_affinity(record, [0])
    process.suspend.assert_not_called()
    process.nice.assert_not_called()
    process.cpu_affinity.assert_not_called()
    assert not controller.state_path.exists()
    assert 'Windows App' in _protected_executable(PACKAGE_EXE, 'WindowsApp.exe')


def package_data(tmp_path):
    root = tmp_path / 'Packages' / WINDOWS_APP_FAMILY / 'LocalState'
    root.mkdir(parents=True)
    target = root / 'remote-session.tmp'
    target.write_bytes(b'keep remote session data')
    return root, target


def test_cleanup_skips_windows_app_data_even_under_a_broad_custom_root(tmp_path, monkeypatch):
    data_root, target = package_data(tmp_path)
    unrelated = tmp_path / 'ordinary-cache.tmp'
    unrelated.write_bytes(b'ordinary fixture')
    monkeypatch.setattr(cleaner, 'current_wallpaper_paths', lambda **kwargs: ())
    monkeypatch.setattr(cleaner.Cleaner, '_open_files_snapshot', staticmethod(lambda: (set(), True)))
    engine = cleaner.Cleaner(tmp_path / 'optimus', categories=[
        cleaner.Category('fixture', 'Fixture cache', tmp_path, 'Fixture only')])
    plan = engine.scan(['fixture'], min_age_days=0)
    assert [Path(row['path']) for row in plan['files']] == [unrelated]
    assert target.read_bytes() == b'keep remote session data'
    assert not cleaner.Cleaner._safe_path(target, tmp_path)
    assert not cleaner.Cleaner._safe_path(target, data_root)


@pytest.mark.parametrize('method', ['_delete_exact', '_delete_posix'])
def test_stale_or_forged_cleanup_record_cannot_delete_package_data(tmp_path, monkeypatch, method):
    root, target = package_data(tmp_path)
    monkeypatch.setattr(cleaner, 'current_wallpaper_paths', lambda **kwargs: ())
    with pytest.raises(ValueError, match='Windows App'):
        getattr(cleaner.Cleaner, method)({'path': str(target), 'root': str(root)})
    assert target.read_bytes() == b'keep remote session data'


def test_file_tools_cannot_create_or_overwrite_windows_app_data(tmp_path, monkeypatch):
    root, target = package_data(tmp_path)
    destination = root / 'new-file.bin'
    with pytest.raises(ValueError, match='Windows App'):
        file_utilities._Output(destination)
    assert not destination.exists() and list(root.iterdir()) == [target]
    monkeypatch.setattr('optimus.wallpaper_preservation.current_wallpaper_paths', lambda **kwargs: ())
    service = file_utilities.FileUtilities(SimpleNamespace(data_dir=tmp_path / 'optimus'))
    with pytest.raises(ValueError, match='Windows App'):
        service._shred_allowed(target, folder=False)
    assert target.read_bytes() == b'keep remote session data'


def test_tracker_and_background_mutators_preserve_windows_app(monkeypatch):
    service = object.__new__(audio_trackers.AudioTrackers)
    service._stop_processes = Mock(side_effect=AssertionError('No process mutation.'))
    result = service._block_one({'app': PACKAGE_EXE, 'verdict': 'block', 'reason': 'Fixture'}, 'a' * 32)
    assert result['protected'] and result['status'] == 'skipped' and result['steps'] == {}
    service._stop_processes.assert_not_called()
    assert audio_trackers.classify(PACKAGE_EXE, signed=False, exists=True)[0] == 'ok'
    native = Mock(side_effect=AssertionError('No firewall command.'))
    monkeypatch.setattr(audio_trackers, '_powershell', native)
    with pytest.raises(ValueError, match='Windows App'):
        audio_trackers.add_firewall_block(PACKAGE_EXE, 'fixture rule')
    native.assert_not_called()
    background = object.__new__(background_apps.BackgroundApps)
    background.process_factory = Mock(side_effect=AssertionError('No process may be opened for mutation.'))
    with pytest.raises(ValueError, match='Windows App'):
        background._verify({'exe': PACKAGE_EXE})
    background.process_factory.assert_not_called()
