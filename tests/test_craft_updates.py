"""Exercise portable updates against local archives; never contact publishers."""
from contextlib import nullcontext
import copy
import hashlib
import io
import json
import os
from pathlib import Path
import threading
from types import SimpleNamespace
from unittest.mock import Mock
import zipfile

import pytest

from optimus import craft_updates as craft

pytestmark = pytest.mark.skipif(os.name != 'nt', reason='Windows portable Craft updater')


def write_json(path, value):
    path.write_text(json.dumps(value), encoding='utf-8')


def read_json(path):
    return json.loads(path.read_text(encoding='utf-8'))


class FixtureClient:
    def __init__(self, repos, *, version='0.2.0', corrupt=False):
        self.release_calls = []
        self.download_calls = []
        self.corrupt = corrupt
        self.releases = {}
        self.payloads = {}
        for repo in repos:
            buffer = io.BytesIO()
            with zipfile.ZipFile(buffer, 'w') as archive:
                archive.writestr(f'{repo}-{version}-windows-x64-portable/{repo}.exe',
                                 b'MZnew-' + repo.encode())
                archive.writestr(f'{repo}-{version}-windows-x64-portable/LICENSE', 'fixture')
                if repo == 'photocraft':
                    archive.writestr(f'{repo}-{version}-windows-x64-portable/portable.txt',
                                     'PhotoCraftData')
            payload = buffer.getvalue()
            filename = f'{repo}-{version}-windows-x64-portable.zip'
            url = f'https://github.com/storytold/{repo}/releases/download/v{version}/{filename}'
            self.payloads[filename] = payload
            self.releases[repo] = {
                'draft': False, 'prerelease': False, 'tag_name': 'v' + version,
                'assets': [{'name': filename, 'browser_download_url': url,
                            'size': len(payload),
                            'digest': 'sha256:' + hashlib.sha256(payload).hexdigest()}],
            }

    def release(self, repo):
        self.release_calls.append(repo)
        return self.releases[repo]

    def download(self, offer, path, cancel_event=None):
        self.download_calls.append(offer['filename'])
        payload = self.payloads[offer['filename']]
        Path(path).write_bytes(b'corrupted' if self.corrupt else payload)


@pytest.fixture
def suite(tmp_path, monkeypatch):
    def create(apps=None):
        apps = apps or {'WordCraft': 'wordcraft'}
        monkeypatch.setattr(craft, 'APPS', apps)
        monkeypatch.setattr(craft, '_lock', lambda path: nullcontext(True))
        monkeypatch.setattr(craft.shutil, 'disk_usage',
                            lambda path: SimpleNamespace(free=10 * 1024 ** 3))
        root = tmp_path / 'Craft Suite'
        data = tmp_path / 'Optimus Data'
        (root / 'Apps').mkdir(parents=True)
        (root / 'Downloads').mkdir()
        data.mkdir()
        manifest = []
        for name, repo in apps.items():
            directory = root / 'Apps' / name
            directory.mkdir()
            (directory / (repo + '.exe')).write_bytes(b'MZold-' + repo.encode())
            (directory / 'LICENSE').write_text('old fixture', encoding='utf-8')
            if repo == 'photocraft':
                (directory / 'portable.txt').write_text('PhotoCraftData', encoding='utf-8')
            archive_path = root / 'Downloads' / f'{repo}-0.1.0-windows-x64-portable.zip'
            with zipfile.ZipFile(archive_path, 'w') as archive:
                for local_file in directory.iterdir():
                    archive.write(local_file, f'{repo}-0.1.0-windows-x64-portable/{local_file.name}')
            initial_digest = 'sha256:' + hashlib.sha256(archive_path.read_bytes()).hexdigest()
            manifest.append({'Name': name, 'Status': 'Ready', 'Version': 'v0.1.0',
                             'AppDirectory': str(directory), 'DownloadPath': str(archive_path),
                             'ExpectedDigest': initial_digest, 'ActualDigest': initial_digest,
                             'Repo': 'storytold/' + repo, 'Size': archive_path.stat().st_size})
        write_json(root / 'download-manifest.json', manifest)
        write_json(data / 'craft-installation.json', {
            'schema': 1, 'root': str(root), 'updates_authorized': True,
            'apps': list(apps),
        })
        return SimpleNamespace(root=root, data=data, apps=apps,
                               client=FixtureClient(apps.values()))
    return create


def execute(suite, **options):
    return craft.run_craft_updates(suite.data, client=suite.client,
                                   running=lambda directory: False, **options)


def test_available_release_replaces_stable_path_and_retains_prior_version(suite):
    installation = suite()
    result = execute(installation)
    row = result['items'][0]
    assert row['status'] == 'updated'
    assert result['updated_count'] == 1 and result['failed_items'] == 0
    assert (installation.root / 'Apps/WordCraft/wordcraft.exe').read_bytes() == b'MZnew-wordcraft'
    assert (Path(row['backup']) / 'wordcraft.exe').read_bytes() == b'MZold-wordcraft'
    saved = read_json(installation.data / 'craft-update-state.json')
    assert saved['apps']['WordCraft']['version'] == '0.2.0'
    assert read_json(installation.data / 'craft-update-transaction.json')['status'] == 'completed'
    assert list((installation.root / '.updates').iterdir()) == []


def test_current_release_does_not_download_or_change_installation(suite):
    installation = suite()
    installation.client = FixtureClient(installation.apps.values(), version='0.1.0')
    result = execute(installation)
    assert result['items'][0]['status'] == 'current'
    assert installation.client.download_calls == []
    assert (installation.root / 'Apps/WordCraft/wordcraft.exe').read_bytes() == b'MZold-wordcraft'
    assert not (installation.data / 'craft-update-state.json').exists()


def test_portable_preferences_presets_and_recovery_survive_update(suite):
    installation = suite({'PhotoCraft': 'photocraft'})
    directory = installation.root / 'Apps/PhotoCraft'
    local_data = directory / 'PhotoCraftData'
    (local_data / 'presets').mkdir(parents=True)
    (local_data / 'recovery').mkdir()
    (local_data / 'preferences.json').write_text('{"theme":"custom"}', encoding='utf-8')
    (local_data / 'presets/user-preset.json').write_text('unique preset', encoding='utf-8')
    (local_data / 'recovery/unsaved-project.bin').write_bytes(b'user recovery data')
    (directory / 'portable.txt').write_text('PhotoCraftData', encoding='utf-8')
    result = execute(installation)
    assert result['items'][0]['status'] == 'updated'
    assert (directory / 'photocraft.exe').read_bytes() == b'MZnew-photocraft'
    assert (local_data / 'preferences.json').read_text(encoding='utf-8') == '{"theme":"custom"}'
    assert (local_data / 'presets/user-preset.json').read_text(encoding='utf-8') == 'unique preset'
    assert (local_data / 'recovery/unsaved-project.bin').read_bytes() == b'user recovery data'
    assert (directory / 'portable.txt').exists()


def test_wrong_download_digest_preserves_installed_app(suite):
    installation = suite()
    installation.client.corrupt = True
    result = execute(installation)
    assert result['items'][0]['status'] == 'failed'
    assert 'checksum or size' in result['items'][0]['error']
    assert (installation.root / 'Apps/WordCraft/wordcraft.exe').read_bytes() == b'MZold-wordcraft'
    assert not (installation.data / 'craft-update-state.json').exists()
    assert not (installation.root / 'Previous Versions').exists()


def test_running_app_is_deferred_without_downloading(suite):
    installation = suite()
    result = craft.run_craft_updates(installation.data, client=installation.client,
                                     running=lambda directory: True)
    assert result['items'][0]['status'] == 'deferred'
    assert installation.client.download_calls == []
    assert (installation.root / 'Apps/WordCraft/wordcraft.exe').read_bytes() == b'MZold-wordcraft'


def test_app_opened_during_download_is_not_replaced(suite):
    installation = suite()
    checks = iter((False, True))
    result = craft.run_craft_updates(installation.data, client=installation.client,
                                     running=lambda directory: next(checks))
    assert result['items'][0]['status'] == 'deferred'
    assert len(installation.client.download_calls) == 1
    assert (installation.root / 'Apps/WordCraft/wordcraft.exe').read_bytes() == b'MZold-wordcraft'
    assert not (installation.data / 'craft-update-state.json').exists()


def test_failed_state_commit_rolls_back_and_cannot_leak_into_later_commit(suite, monkeypatch):
    installation = suite({'WordCraft': 'wordcraft', 'GridCraft': 'gridcraft'})
    atomic = craft._atomic
    failed = False

    def fail_first_state_commit(path, value):
        nonlocal failed
        if Path(path).name == 'craft-update-state.json' and not failed:
            failed = True
            raise OSError('fixture metadata write failure')
        atomic(path, value)

    monkeypatch.setattr(craft, '_atomic', fail_first_state_commit)
    result = execute(installation)
    assert [row['status'] for row in result['items']] == ['failed', 'updated']
    assert (installation.root / 'Apps/WordCraft/wordcraft.exe').read_bytes() == b'MZold-wordcraft'
    assert (installation.root / 'Apps/GridCraft/gridcraft.exe').read_bytes() == b'MZnew-gridcraft'
    state = read_json(installation.data / 'craft-update-state.json')
    assert state['apps'].get('WordCraft', {}).get('version', 'v0.1.0') in ('v0.1.0', '0.1.0')
    assert state['apps']['GridCraft']['version'] == '0.2.0'


def test_failed_rollback_retains_exact_journal_and_stops_later_apps_until_recovery(suite, monkeypatch):
    installation = suite({'WordCraft': 'wordcraft', 'GridCraft': 'gridcraft'})
    live = installation.root / 'Apps/WordCraft'
    backups = installation.root / 'Previous Versions'
    atomic = craft._atomic
    rename = craft.os.rename

    def fail_state_commit(path, value):
        if Path(path).name == 'craft-update-state.json':
            raise OSError('fixture metadata commit failure')
        atomic(path, value)

    def deny_backup_restore(source, destination, *args, **kwargs):
        if Path(source).parent == backups and Path(destination) == live:
            raise OSError('fixture rollback file temporarily locked')
        return rename(source, destination, *args, **kwargs)

    monkeypatch.setattr(craft, '_atomic', fail_state_commit)
    monkeypatch.setattr(craft.os, 'rename', deny_backup_restore)
    result = execute(installation)
    assert len(result['items']) == 1
    assert result['items'][0]['name'] == 'WordCraft'
    assert result['items'][0]['status'] == 'failed'
    assert result['items'][0]['recovery_required'] is True
    journal = read_json(installation.data / 'craft-update-transaction.json')
    assert journal['name'] == 'WordCraft' and journal['status'] == 'prepared'
    assert (Path(journal['backup']) / 'wordcraft.exe').read_bytes() == b'MZold-wordcraft'
    assert (installation.root / 'Apps/GridCraft/gridcraft.exe').read_bytes() == b'MZold-gridcraft'
    assert installation.client.release_calls == ['wordcraft']

    monkeypatch.setattr(craft, '_atomic', atomic)
    monkeypatch.setattr(craft.os, 'rename', rename)
    recovered = execute(installation)
    assert [row['status'] for row in recovered['items']] == ['updated', 'updated']
    assert (live / 'wordcraft.exe').read_bytes() == b'MZnew-wordcraft'
    assert (installation.root / 'Apps/GridCraft/gridcraft.exe').read_bytes() == b'MZnew-gridcraft'
    assert recovered['failed_items'] == 0


@pytest.mark.parametrize('new_payload_installed', [False, True])
def test_interrupted_swap_recovers_prior_app_and_version(suite, new_payload_installed):
    installation = suite()
    live = installation.root / 'Apps/WordCraft'
    backup = installation.root / 'Previous Versions/WordCraft-0.1.0-012345abcdef'
    backup.parent.mkdir()
    live.rename(backup)
    if new_payload_installed:
        live.mkdir()
        (live / 'wordcraft.exe').write_bytes(b'MZinterrupted-new')
    write_json(installation.data / 'craft-update-state.json', {
        'schema': 1, 'apps': {'WordCraft': {'version': '0.2.0'}}})
    write_json(installation.data / 'craft-update-transaction.json', {
        'schema': 1, 'name': 'WordCraft', 'status': 'prepared',
        'directory': str(live), 'backup': str(backup), 'prior_version': 'v0.1.0',
    })
    installation.client = FixtureClient(installation.apps.values(), version='0.1.0')
    result = execute(installation)
    assert result['items'][0]['status'] == 'current'
    assert (live / 'wordcraft.exe').read_bytes() == b'MZold-wordcraft'
    assert not backup.exists()
    assert read_json(installation.data / 'craft-update-state.json')['apps']['WordCraft']['version'] == 'v0.1.0'
    assert read_json(installation.data / 'craft-update-transaction.json')['status'] == 'rolled_back'


def test_recovery_retries_prior_state_write_after_backup_was_restored(suite, monkeypatch):
    installation = suite()
    live = installation.root / 'Apps/WordCraft'
    backup = installation.root / 'Previous Versions/WordCraft-0.1.0-012345abcdef'
    backup.parent.mkdir()
    live.rename(backup)
    original_state = {'schema': 1, 'apps': {'WordCraft': {'version': '0.2.0'}}}
    state_path = installation.data / 'craft-update-state.json'
    write_json(state_path, original_state)
    write_json(installation.data / 'craft-update-transaction.json', {
        'schema': 1, 'name': 'WordCraft', 'status': 'prepared',
        'directory': str(live), 'backup': str(backup), 'prior_version': 'v0.1.0',
    })
    atomic = craft._atomic

    def fail_state_commit(path, value):
        if Path(path) == state_path:
            raise OSError('fixture recovery metadata failure')
        atomic(path, value)

    monkeypatch.setattr(craft, '_atomic', fail_state_commit)
    with pytest.raises(OSError, match='fixture recovery metadata failure'):
        craft._recover(installation.data, installation.root,
                       read_json(state_path), lambda directory: False)
    assert (live / 'wordcraft.exe').read_bytes() == b'MZold-wordcraft'
    assert not backup.exists()
    monkeypatch.setattr(craft, '_atomic', atomic)
    craft._recover(installation.data, installation.root,
                   read_json(state_path), lambda directory: False)
    assert read_json(state_path)['apps']['WordCraft']['version'] == 'v0.1.0'


def test_cancelled_run_never_downloads(suite):
    installation = suite()
    event = threading.Event()
    event.set()
    result = execute(installation, cancel_event=event)
    assert result['cancelled'] is True
    assert installation.client.release_calls == []
    assert installation.client.download_calls == []


def test_failed_publisher_check_does_not_prevent_other_app_updates(suite):
    installation = suite({'WordCraft': 'wordcraft', 'GridCraft': 'gridcraft'})
    release = installation.client.release

    def unavailable_wordcraft(repo):
        if repo == 'wordcraft':
            raise OSError('fixture publisher unavailable')
        return release(repo)

    installation.client.release = unavailable_wordcraft
    result = execute(installation)
    assert [row['status'] for row in result['items']] == ['failed', 'updated']
    assert (installation.root / 'Apps/WordCraft/wordcraft.exe').read_bytes() == b'MZold-wordcraft'
    assert (installation.root / 'Apps/GridCraft/gridcraft.exe').read_bytes() == b'MZnew-gridcraft'


@pytest.mark.parametrize('change', ['foreign_repo', 'missing_digest', 'prerelease'])
def test_unverified_publisher_offer_is_rejected_before_download(suite, change):
    installation = suite()
    release = installation.client.releases['wordcraft']
    if change == 'foreign_repo':
        release['assets'][0]['browser_download_url'] = release['assets'][0]['browser_download_url'].replace(
            '/storytold/', '/different-owner/')
    elif change == 'missing_digest':
        release['assets'][0].pop('digest')
    else:
        release['prerelease'] = True
    result = execute(installation)
    assert result['items'][0]['status'] == 'failed'
    assert installation.client.download_calls == []
    assert (installation.root / 'Apps/WordCraft/wordcraft.exe').read_bytes() == b'MZold-wordcraft'


@pytest.mark.parametrize('software_enabled,craft_enabled,expected', [
    (True, True, True), (True, False, False),
    (False, True, False), (False, False, False),
])
def test_workflow_craft_stage_uses_saved_flags_and_survives_unavailable_winget(
        tmp_path, monkeypatch, software_enabled, craft_enabled, expected):
    from test_workflow import MockBackend, workflow_module

    backend = MockBackend(tmp_path)
    backend.update_scan = {'structured': False, 'exitCode': 1, 'updates': [],
                           'error': 'fixture WinGet unavailable'}
    configuration = copy.deepcopy(workflow_module.DEFAULTS)
    configuration.update({key: False for key, value in configuration.items()
                          if isinstance(value, bool)})
    configuration.update(software_updates_enabled=software_enabled,
                         craft_updates_enabled=craft_enabled)
    check = Mock(return_value={'items': [], 'failed_items': 0,
                               'installer_started': False, 'outcomeKnown': True,
                               'running': False})
    monkeypatch.setattr(craft, 'run_craft_updates', check)
    report = workflow_module.Workflow(backend).run(configuration, lambda event: None)
    stages = [stage for stage in report['stages'] if stage['title'] == 'Update Craft apps']
    assert bool(stages) is expected
    assert check.call_count == int(expected)
    if expected:
        assert stages[0]['status'] == 'completed'
        assert check.call_args.args == (tmp_path,)
        assert ('updates_scan',) in backend.calls
    else:
        check.assert_not_called()


@pytest.mark.parametrize('settings', [
    {'software_updates_enabled': False, 'craft_updates_enabled': True},
    {'software_updates_enabled': True, 'craft_updates_enabled': False},
])
def test_sign_in_command_respects_saved_disable_choices(suite, monkeypatch, settings):
    installation = suite()
    write_json(installation.data / 'preferences.json', settings)
    check = Mock(side_effect=AssertionError('Disabled updater must not run'))
    monkeypatch.setattr(craft, 'run_craft_updates', check)
    result = craft.run_saved_craft_updates(installation.data)
    assert result['skipped'] is True
    check.assert_not_called()


def test_sign_in_command_retains_registered_opt_in_after_upstream_preferences_change(suite, monkeypatch):
    installation = suite()
    write_json(installation.data / 'preferences.json', {'software_updates_enabled': True})
    check = Mock(return_value={'items': [], 'fixture_checked': True})
    monkeypatch.setattr(craft, 'run_craft_updates', check)
    result = craft.run_saved_craft_updates(installation.data)
    assert result['fixture_checked'] is True
    check.assert_called_once_with(installation.data)


@pytest.mark.parametrize('unsafe_name', ['../escaped.exe', '/absolute.exe',
                                         'folder/CON.exe', 'folder/file:stream'])
def test_unsafe_archive_members_are_rejected(tmp_path, unsafe_name):
    archive = tmp_path / 'unsafe.zip'
    with zipfile.ZipFile(archive, 'w') as source:
        source.writestr(unsafe_name, b'MZfixture')
    with pytest.raises(ValueError, match='unsafe path'):
        craft.extract_archive(archive, tmp_path / 'extracted', 'wordcraft')
    assert not (tmp_path / 'escaped.exe').exists()
