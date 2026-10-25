"""Managed launcher retention uses private fixtures only, no native COM/task calls."""
from __future__ import annotations

import copy
import json
import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest

from optimus import self_update_launchers as module


class FakeAdapter:
    def __init__(self, value):
        self.value = value
        self.links = {}
        self.calls = []
        self.startup_reads = 0
        self.on_write = None
        self.on_startup = None
        self.warnings = []
        manifest = {'schema': 1, 'owner_sid': 'S-1-5-21-1-2-3-1001', 'owner_token': 'a' * 32,
                    'launch': {'executable': str(value.old), 'arguments': [], 'argument_line': '',
                               'working_directory': str(value.old.parent)}, 'previous_launch': None}
        self.startup = {'manifest': manifest, 'manifest_bytes': json.dumps(manifest).encode(), 'xml': '<fixture>owned old task</fixture>',
                        'owner_sid': manifest['owner_sid'], 'status': {'owned': True, 'available': True, 'enabled': True,
                        'state': 'enabled', 'target': str(value.old)}}

    def add_link(self, name='Optimus.lnk', **changes):
        path = self.value.root / 'desktop' / name
        path.parent.mkdir(exist_ok=True)
        path.write_bytes(('original link fixture ' + name).encode())
        row = {'path': str(path), 'target': str(self.value.old), 'arguments': '', 'working_directory': str(self.value.old.parent),
            'icon': str(self.value.old) + ',0', 'description': 'Optimus fixture', 'hotkey': '', 'window_style': 1,
            'app_properties': dict.fromkeys(module._PROPERTIES, '')}
        row.update(changes)
        self.links[str(path)] = row
        return path

    def inventory_links(self, oldexe):
        return {'links': copy.deepcopy(list(self.links.values())), 'warnings': self.warnings}

    def read_link(self, path):
        return copy.deepcopy(self.links[str(path)])

    def _assert_preserved(self):
        backup = self.value.backup
        assert (backup / 'launchers.json').is_file()
        journal = json.loads((backup / 'launchers.json').read_bytes())
        assert journal['outcomeKnown'] is False
        for row in journal['backups']:
            if row['kind'] == 'shortcut':
                assert module._sha(Path(row['backup']).read_bytes()) == row['before_sha256']
            else:
                assert Path(row['manifest_backup']).is_file() and Path(row['xml_backup']).is_file()

    def write_link(self, path, oldexe, newexe, digest):
        self._assert_preserved()
        assert module._sha(path.read_bytes()) == digest
        self.calls.append(('link', str(path)))
        if self.on_write:
            return self.on_write(path, oldexe, newexe, digest)
        self.links[str(path)].update(target=str(newexe), working_directory=str(newexe.parent), icon=str(newexe) + ',0')
        path.write_bytes(b'fixture retargeted link bytes')
        return self.read_link(path)

    def startup_snapshot(self):
        self.startup_reads += 1
        return copy.deepcopy(self.startup)

    def retarget_startup(self, newexe, expected):
        self._assert_preserved()
        assert expected == self.startup
        self.calls.append(('startup', str(newexe)))
        if self.on_startup:
            return self.on_startup(newexe, expected)
        before = copy.deepcopy(self.startup)
        self.startup['manifest']['launch'].update(executable=str(newexe), working_directory=str(newexe.parent))
        self.startup['manifest']['previous_launch'] = copy.deepcopy(before['manifest']['launch'])
        self.startup['manifest_bytes'] = json.dumps(self.startup['manifest']).encode()
        self.startup['xml'] = '<fixture>owned NEW task</fixture>'
        self.startup['status']['target'] = str(newexe)
        return {'before': before, 'after': self.startup_snapshot()}


@pytest.fixture
def fixture(tmp_path):
    value = SimpleNamespace(root=tmp_path, data=tmp_path / 'data', old=tmp_path / 'old' / 'Optimus.exe',
        new=tmp_path / 'new' / 'Optimus.exe', backup=tmp_path / 'data' / 'self-updates' / 'fixture' / 'launchers')
    value.data.mkdir()
    value.old.parent.mkdir(); value.new.parent.mkdir()
    value.old.write_bytes(b'old verified executable fixture, never run')
    value.new.write_bytes(b'new verified executable fixture, never run')
    value.settings = {'launch_at_sign_in': True, 'unattended_auto_clean_enabled': False, 'unattended_scope_hash': '',
                      'fixture_other_choice': ['unchanged']}
    value.raw = (json.dumps(value.settings, indent=4) + '\n').encode()
    value.preferences = value.data / 'preferences.json'; value.preferences.write_bytes(value.raw)
    value.adapter = FakeAdapter(value)
    value.service = module.ManagedLaunchers(value.data, value.adapter)
    value.run = lambda: value.service.retarget(value.old, value.new, value.backup, value.settings)
    return value


def test_constructors_and_unsupported_platform_are_inert(tmp_path):
    with patch.object(module.subprocess, 'run', side_effect=AssertionError('No native calls')) as native:
        module.ManagedLaunchers(tmp_path)
        module.WindowsLauncherAdapter(tmp_path)
        with patch.object(module.sys, 'platform', 'linux'):
            result = module.ManagedLaunchers(tmp_path).retarget('old', 'new', 'backup', {})
    assert result['status'] == 'unsupported' and result['manual_action_required']
    native.assert_not_called()
    assert list(tmp_path.iterdir()) == []


def test_all_originals_and_xml_are_durable_before_any_mutation_and_readback_matches(fixture):
    f = fixture; link = f.adapter.add_link()
    original, original_manifest, original_xml = link.read_bytes(), f.adapter.startup['manifest_bytes'], f.adapter.startup['xml'].encode()
    result = f.run()
    assert result['status'] == 'completed' and result['outcomeKnown'] is True and not result['manual_action_required']
    assert f.preferences.read_bytes() == f.raw
    assert (f.backup / 'link-000.lnk').read_bytes() == original
    assert (f.backup / 'startup-manifest.json').read_bytes() == original_manifest
    assert (f.backup / 'startup-task.xml').read_bytes() == original_xml
    assert f.adapter.links[str(link)]['target'] == str(f.new)
    assert f.adapter.startup['status']['target'] == str(f.new)
    assert [row['kind'] for row in result['changed']] == ['shortcut', 'startup']
    assert result['preferences_written'] is False and result['app_launched'] is False


@pytest.mark.parametrize('change', [{'arguments': '--source-workspace sandbox'}, {'working_directory': 'relative'},
    {'window_style': 2}, {'target': 'other.exe'}, {'app_properties': dict.fromkeys(module._PROPERTIES, 'complex')}])
def test_non_plain_or_unrelated_links_are_never_retargeted(fixture, change):
    f = fixture; path = f.adapter.add_link(**change); original = path.read_bytes()
    result = f.run()
    assert result['status'] == 'completed_with_manual_items' and result['manual_action_required']
    assert not any(kind == 'link' for kind, _ in f.adapter.calls)
    assert path.read_bytes() == original


@pytest.mark.parametrize('status', [
    {'owned': False, 'available': True, 'enabled': None, 'state': 'foreign', 'target': None},
    {'owned': False, 'available': True, 'enabled': False, 'state': 'absent', 'target': None},
    {'owned': True, 'available': True, 'enabled': False, 'state': 'disabled', 'target': 'old'},
    {'owned': True, 'available': True, 'enabled': True, 'state': 'enabled', 'target': 'other.exe'}])
def test_disabled_missing_foreign_or_wrong_startup_is_not_created_enabled_or_adopted(fixture, status):
    f = fixture; f.adapter.startup['status'] = status
    result = f.run()
    assert result['manual_action_required'] and not f.adapter.calls
    assert f.preferences.read_bytes() == f.raw


def test_saved_disabled_startup_is_not_touched(fixture):
    f = fixture; f.settings['launch_at_sign_in'] = False
    f.preferences.write_text(json.dumps(f.settings)); original = f.preferences.read_bytes()
    result = f.run()
    assert result['status'] == 'completed' and not f.adapter.calls and f.adapter.startup_reads == 0
    assert f.preferences.read_bytes() == original


def test_preference_changes_before_entry_refuse_without_backup_or_native_call(fixture):
    f = fixture; f.preferences.write_text('{}')
    with pytest.raises(ValueError, match='Saved choices changed'):
        f.run()
    assert not f.backup.exists() and not f.adapter.calls


def test_backup_failure_never_retargets_any_launcher(fixture):
    f = fixture; path = f.adapter.add_link(); original = path.read_bytes()
    with patch.object(module, '_write_new', side_effect=OSError('Fixture disk full')):
        result = f.run()
    assert result['status'] == 'needs_review' and result['outcomeKnown'] is True
    assert not f.adapter.calls and path.read_bytes() == original


def test_unknown_native_link_outcome_is_durably_unverified_and_not_retried(fixture):
    f = fixture; f.adapter.add_link()
    f.adapter.on_write = Mock(side_effect=TimeoutError('Fixture native result unknown'))
    result = f.run()
    assert result['status'] == 'needs_review' and result['outcomeKnown'] is False
    assert f.adapter.on_write.call_count == 1
    assert json.loads(Path(result['receipt']).read_bytes())['outcomeKnown'] is False


def test_contradictory_link_readback_stops_startup_and_keeps_unknown_outcome(fixture):
    f = fixture; path = f.adapter.add_link()
    f.adapter.on_write = lambda *args: dict(f.adapter.read_link(path), target='wrong.exe')
    result = f.run()
    assert result['status'] == 'needs_review' and result['outcomeKnown'] is False
    assert f.adapter.calls == [('link', str(path))]


def test_unknown_native_startup_result_preserves_exact_original_xml(fixture):
    f = fixture; original = f.adapter.startup['xml'].encode()
    f.adapter.on_startup = Mock(side_effect=TimeoutError('Fixture registration outcome unknown'))
    result = f.run()
    assert result['status'] == 'needs_review' and result['outcomeKnown'] is False
    assert (f.backup / 'startup-task.xml').read_bytes() == original
    f.adapter.on_startup.assert_called_once()


def test_existing_backup_directory_is_never_overwritten(fixture):
    f = fixture; f.backup.mkdir(parents=True); marker = f.backup / 'preserve'; marker.write_bytes(b'old')
    with pytest.raises(FileExistsError):
        f.run()
    assert marker.read_bytes() == b'old' and not f.adapter.calls


def test_backup_path_cannot_escape_data_root(fixture):
    f = fixture; f.backup = f.root / 'outside'
    with pytest.raises(ValueError, match='inside the configured data root'):
        f.run()
    assert not f.backup.exists() and not f.adapter.calls


def test_hardlinked_existing_shortcut_is_not_backed_up_or_modified(fixture):
    f = fixture; path = f.adapter.add_link()
    try:
        os.link(path, f.root / 'link-alias')
    except OSError:
        pytest.skip('Fixture filesystem does not support hardlinks.')
    result = f.run()
    assert result['status'] == 'needs_review' and not f.adapter.calls


def test_duplicate_inventory_is_refused_before_mutation(fixture):
    f = fixture; f.adapter.add_link(); original = f.adapter.inventory_links
    f.adapter.inventory_links = lambda old: {'links': original(old)['links'] * 2, 'warnings': []}
    result = f.run()
    assert result['status'] == 'needs_review' and not f.adapter.calls


def test_user_preferences_changed_during_retention_are_preserved_not_restored(fixture):
    f = fixture; path = f.adapter.add_link()
    def write(path, old, new, digest):
        f.adapter.links[str(path)].update(target=str(new), working_directory=str(new.parent), icon=str(new) + ',0')
        path.write_bytes(b'new fixture bytes')
        f.preferences.write_text('{"user_changed":true}')
        return f.adapter.read_link(path)
    f.adapter.on_write = write
    result = f.run()
    assert result['status'] == 'needs_review' and result['outcomeKnown'] is True
    assert json.loads(f.preferences.read_bytes()) == {'user_changed': True}
    assert not any(kind == 'startup' for kind, _ in f.adapter.calls)


def test_native_task_mutation_reuses_existing_startup_ownership_lock_and_provider_only():
    value = module.WindowsLauncherAdapter(Path('fixture-data'))
    assert isinstance(value.registration, module.StartupRegistration)
    # The actual helper stays embedded and cannot be expanded into arbitrary commands.
    assert "'inventory','read','write'" in module._LINK_PS
    assert 'RunAs' not in module._LINK_PS and 'Start-Process' not in module._LINK_PS
    assert '.Save()' in module._LINK_PS and "$v.arguments -cne ''" in module._LINK_PS



def test_native_startup_script_is_update_only_and_preserves_definition_and_acl():
    text = module._STARTUP_PS
    assert 'RegisterTaskDefinition($name,$definition,20,$sid,$null,3,$null)' in text
    assert 'TASK_UPDATE (4) | TASK_DONT_ADD_PRINCIPAL_ACE (16)' in text
    assert "'update_existing'" in text and '$folder.GetTask($name)' in text
    assert 'UnchangedDefinition $oldXml' in text and 'UnchangedDefinition $newXml' in text
    assert "'Command','Arguments','WorkingDirectory'" in text
    assert 'NewTask' not in text and 'DeleteTask' not in text and 'RunAs' not in text
    assert 'Start-Process' not in text and '.Run(' not in text
    assert 'before_xml_sha256' in text and 'new_executable_sha256' in text


def native_adapter(f):
    adapter = module.WindowsLauncherAdapter(f.data, headroom_checker=lambda: None)
    original = copy.deepcopy(f.adapter.startup)
    adapter.registration.path.write_bytes(original['manifest_bytes'])
    state = {'xml': original['xml']}
    def status(request):
        assert request['operation'] == 'status'
        manifest = request['manifest']
        return {'status': dict(original['status'], target=manifest['launch']['executable']),
            'owner_sid': manifest['owner_sid'], 'before_xml': state['xml'], 'persisted_task': {'verified': True}}
    adapter.registration.runner = status
    expected = adapter.startup_snapshot()
    def update(script, request):
        assert script == module._STARTUP_PS and request['operation'] == 'update_existing'
        assert request['before_xml_sha256'] == module._sha(original['xml'].encode())
        assert request['new_executable_sha256'] == module._sha(f.new.read_bytes())
        # New/old ownership is persisted before the native UPDATE, never afterward.
        saved = json.loads(adapter.registration.path.read_bytes())
        assert saved == request['manifest']
        assert saved['owner_token'] == original['manifest']['owner_token']
        assert saved['previous_launch'] == original['manifest']['launch']
        assert adapter.registration.launch_provider() == saved['launch']
        state['xml'] = '<fixture>updated existing task only</fixture>'
        return {'updated_existing': True, 'owner_sid': original['owner_sid'],
            'before_xml': original['xml'], 'after_xml': state['xml'],
            'before_xml_sha256': module._sha(original['xml'].encode()),
            'after_xml_sha256': module._sha(state['xml'].encode()), 'persisted_task': {'verified': True}}
    adapter._native = Mock(side_effect=update)
    return adapter, expected, state


def test_native_startup_adapter_uses_lock_provider_durable_identity_and_exact_readback(fixture):
    adapter, expected, state = native_adapter(fixture)
    result = adapter.retarget_startup(fixture.new, expected)
    assert result['before'] == expected
    assert result['after']['status']['target'] == str(fixture.new)
    assert result['after']['xml'] == state['xml']
    assert result['native_receipt']['updated_existing'] is True
    adapter._native.assert_called_once()
    assert fixture.preferences.read_bytes() == fixture.raw


def test_retarget_second_headroom_refusal_preserves_manifest_and_task(fixture):
    from optimus.startup_safety import StartupPreflightError
    adapter, expected, state = native_adapter(fixture)
    before = adapter.registration.path.read_bytes()
    adapter.headroom_checker = Mock(side_effect=[None, StartupPreflightError('Fixture space fell below 2 GiB.')])
    with pytest.raises(StartupPreflightError): adapter.retarget_startup(fixture.new, expected)
    assert adapter.registration.path.read_bytes() == before
    adapter._native.assert_not_called()


def test_snapshot_requires_verified_physical_task_before_manifest_change(fixture):
    adapter, expected, state = native_adapter(fixture)
    before = adapter.registration.path.read_bytes()
    original = adapter.registration.runner
    adapter.registration.runner = lambda request: dict(original(request), persisted_task={'verified': False})
    with pytest.raises(ValueError, match='persisted native startup'):
        adapter.retarget_startup(fixture.new, expected)
    assert adapter.registration.path.read_bytes() == before
    adapter._native.assert_not_called()


def test_native_startup_adapter_refuses_task_changed_after_backup_before_manifest_mutation(fixture):
    adapter, expected, state = native_adapter(fixture)
    original = adapter.registration.path.read_bytes()
    state['xml'] = '<fixture>somebody changed task</fixture>'
    with pytest.raises(ValueError, match='changed before retarget'):
        adapter.retarget_startup(fixture.new, expected)
    assert adapter.registration.path.read_bytes() == original
    adapter._native.assert_not_called()


@pytest.mark.parametrize('change', [
    {'updated_existing': False}, {'owner_sid': 'S-1-5-18'}, {'before_xml': '<changed/>'},
    {'before_xml_sha256': '0' * 64}, {'after_xml': '<wrong/>'}, {'after_xml_sha256': '0' * 64}])
def test_native_startup_adapter_requires_complete_exact_native_readback(fixture, change):
    adapter, expected, state = native_adapter(fixture)
    actual = adapter._native.side_effect
    adapter._native.side_effect = lambda script, request: dict(actual(script, request), **change)
    with pytest.raises(ValueError, match='could not be verified|did not establish'):
        adapter.retarget_startup(fixture.new, expected)
    adapter._native.assert_called_once()


def test_reused_startup_runner_cannot_mutate_tasks(fixture):
    adapter = module.WindowsLauncherAdapter(fixture.data)
    with patch.object(adapter, '_native', side_effect=AssertionError('No native mutation')) as native:
        for operation in ('set', 'restore', 'unknown'):
            with pytest.raises(ValueError, match='status-only'):
                adapter._startup_status({'operation': operation})
    native.assert_not_called()


def test_native_power_shell_is_fixed_not_inherited_systemroot_and_modules_are_protected(fixture, monkeypatch):
    adapter = module.WindowsLauncherAdapter(fixture.data)
    executable = fixture.root / 'native-windows' / 'System32' / 'WindowsPowerShell' / 'v1.0' / 'powershell.exe'
    executable.parent.mkdir(parents=True); executable.write_bytes(b'fixture only; never execute')
    monkeypatch.setenv('SystemRoot', str(fixture.root / 'untrusted'))
    monkeypatch.setenv('PSModulePath', str(fixture.root / 'untrusted-modules'))
    with patch.object(module, '_system_powershell', return_value=executable), patch.object(module.subprocess, 'run',
        return_value=SimpleNamespace(stdout='{"ok":true}', returncode=0)) as native:
        assert adapter._native('fixture script only', {'operation': 'fixture'}) == {'ok': True}
    args, kwargs = native.call_args
    assert args[0][0] == str(executable)
    assert kwargs['env']['SystemRoot'] == str(executable.parents[3])
    assert kwargs['env']['windir'] == str(executable.parents[3])
    assert kwargs['env']['PSModulePath'] == str(executable.parent / 'Modules')
    assert kwargs['timeout'] == 20 and '-NonInteractive' in args[0]


def test_system_directory_is_lazy_native_and_does_not_consult_environment(fixture, monkeypatch):
    system = fixture.root / 'native-system'
    executable = system / 'WindowsPowerShell' / 'v1.0' / 'powershell.exe'
    executable.parent.mkdir(parents=True); executable.write_bytes(b'fixture only; never execute')
    def query(buffer, size):
        buffer.value = str(system)
        return len(str(system))
    kernel = SimpleNamespace(GetSystemDirectoryW=Mock(side_effect=query))
    monkeypatch.setenv('SystemRoot', str(fixture.root / 'evil'))
    with patch.object(module.ctypes, 'WinDLL', return_value=kernel, create=True):
        assert module._system_powershell() == executable
    kernel.GetSystemDirectoryW.assert_called_once()



def test_relocated_known_folder_binding_is_preserved_and_rechecked_for_each_link(fixture):
    f = fixture; link = f.adapter.add_link()
    adapter = module.WindowsLauncherAdapter(f.data)
    roots = [{'logical': str(f.root / 'logical-desktop-junction'), 'resolved': str(link.parent)}]
    calls = []
    def native(script, request):
        assert script == module._LINK_PS
        calls.append(copy.deepcopy(request))
        if request['operation'] == 'inventory':
            return {'ok': True, 'links': [f.adapter.read_link(link)], 'warnings': [], 'root_bindings': roots}
        assert request['expected_roots'] == roots
        return {'ok': True, 'link': f.adapter.read_link(link)}
    adapter._native = native
    result = adapter.inventory_links(f.old)
    assert result['root_bindings'] == roots and result['links'][0]['path'] == str(link)
    adapter.read_link(link)
    adapter.write_link(link, f.old, f.new, module._sha(link.read_bytes()))
    assert [row['operation'] for row in calls] == ['inventory', 'read', 'write']
    assert 'GetFinalPathNameByHandleW' in module._LINK_PS and 'CreateFileW(p,0,7' in module._LINK_PS
    assert 'Ordinary $resolved' in module._LINK_PS and 'Ordinary $p' in module._LINK_PS
    assert 'Known launcher folder identity changed after inventory.' in module._LINK_PS


def test_unavailable_known_folder_is_manual_but_other_links_and_owned_startup_continue(fixture):
    f = fixture; f.adapter.add_link()
    f.adapter.warnings = ['A current-user launcher folder could not be resolved safely and was preserved: fixture-unavailable']
    result = f.run()
    assert result['status'] == 'completed_with_manual_items' and result['outcomeKnown'] is True
    assert [row['kind'] for row in result['changed']] == ['shortcut', 'startup']
    assert any('fixture-unavailable' in row['reason'] for row in result['manual_items'])
    assert '$rootWarnings+=' in module._LINK_PS and 'warnings=@($rootWarnings)' in module._LINK_PS
    assert 'A launcher subfolder was unavailable and was preserved:' in module._LINK_PS


def test_native_link_access_requires_prior_known_folder_inventory(fixture):
    adapter = module.WindowsLauncherAdapter(fixture.data)
    with patch.object(adapter, '_native') as native:
        with pytest.raises(ValueError, match='Inspect known launcher roots'):
            adapter.read_link(fixture.root / 'not-inspected.lnk')
    native.assert_not_called()
