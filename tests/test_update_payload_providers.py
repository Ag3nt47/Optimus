"""Update payload ownership and verification use disposable fake providers only."""
import copy
import hashlib
import json
import os
from pathlib import Path
import subprocess
import time
from unittest.mock import Mock

import pytest

from optimus.maintenance import Maintenance, UpdateOutcomeError
from optimus.update_payload_lifecycle import cleanup_enabled, console_quiescent
from optimus import winget_console
from optimus.parallel_updates import _journal_result
from test_updates import table
from test_app_presence import installed_table, catalog_text, backend as presence_backend


def terminal(code=0, *, quiescent=True):
    value = subprocess.CompletedProcess([], code, 'Fixture native operation returned.', '')
    value.capture_complete = True
    value.console_receipt = {'schema': 1, 'verified': True, 'quiescent': quiescent,
        'job_verified': True, 'job_active_processes': 0 if quiescent else 1,
        'console_client_count': 1, 'supervisor_exit_code': 0, 'target_exit_code': code,
        'output_eof': True}
    return value


@pytest.fixture
def provider(tmp_path):
    value = Maintenance(None, tmp_path, restart_reader=Mock(return_value={'reboot_required': False}))
    value._winget = Mock(return_value='fixture-winget.exe')
    value._pin_status = Mock(return_value=(True, '', False))
    value._last_update_scan = time.time()
    value._last_update_ids = {'Vendor.App'}
    value._last_update_packages = {'Vendor.App': {'id': 'Vendor.App', 'package_id': 'Vendor.App',
        'name': 'Fixture App', 'version': '1.0', 'available': '2.0', 'source': 'winget'}}
    value.fixture_code, value.fixture_after, value.fixture_type, value.fixture_quiescent = 0, '2.0', 'msi', True
    value.fixture_paths = []
    value.fixture_extra = False
    def run(arguments, **options):
        if arguments[1] == 'upgrade' and '--id' not in arguments:
            return subprocess.CompletedProcess([], 0, table(('Fixture App', 'Vendor.App', '1.0', '2.0', 'winget')), '')
        if arguments[1] == 'list':
            return subprocess.CompletedProcess([], 0, installed_table('Vendor.App', value.fixture_after), '')
        if arguments[1] == 'show':
            text = catalog_text('Vendor.App', '2.0', value.fixture_type).replace('a' * 64,
                hashlib.sha256(b'fixture installer bytes').hexdigest())
            return subprocess.CompletedProcess([], 0, text, '')
        assert arguments[1] == 'upgrade' and '--id' in arguments
        environment = options.get('environment')
        root = Path(environment['TEMP']) if environment else tmp_path / 'provider-managed-fixture'
        folder = root / 'WinGet' / 'Vendor.App.2.0'
        folder.mkdir(parents=True, exist_ok=True)
        payload = folder / 'fixture-installer.msi'
        payload.write_bytes(b'fixture installer bytes')
        value.fixture_paths.append(payload)
        if value.fixture_extra:
            (folder / 'installer.log').write_bytes(b'keep diagnostic log')
            (folder / 'receipt.json').write_bytes(b'keep receipt')
            (folder / 'unins000.exe').write_bytes(b'keep future uninstaller')
            (folder / 'renamed-repair.exe').write_bytes(b'keep repair runtime')
        return terminal(value.fixture_code, quiescent=value.fixture_quiescent)
    value._run = Mock(side_effect=run)
    return value


@pytest.mark.parametrize('stored,expected', [
    (None, True), ({}, True), ({'update_payload_cleanup_enabled': True}, True),
    ({'update_payload_cleanup_enabled': False}, False),
    ({'update_payload_cleanup_enabled': 'false'}, False), ([], False),
    ({'cleanup_age_days': -1}, False),
])
def test_cleanup_preference_defaults_and_corruption_never_write(tmp_path, stored, expected):
    path = tmp_path / 'preferences.json'
    raw = None if stored is None else json.dumps(stored).encode()
    if raw is not None: path.write_bytes(raw)
    assert cleanup_enabled(tmp_path) is expected
    assert (path.read_bytes() if path.exists() else None) == raw


def test_duplicate_preference_is_not_cleanup_authority(tmp_path):
    (tmp_path / 'preferences.json').write_text('{"update_payload_cleanup_enabled":false,"update_payload_cleanup_enabled":true}')
    assert cleanup_enabled(tmp_path) is False


def test_verified_exact_update_cleans_only_its_owned_payload(provider, tmp_path):
    personal = tmp_path / 'Downloads' / 'personal-installer.exe'
    personal.parent.mkdir()
    personal.write_bytes(b'personal download')
    provider.fixture_extra = True
    result = provider.updates_apply('Vendor.App', confirmed=True, unattended=True)
    cleanup = result['payload_cleanup']
    assert result['verified'] is True and result['installed_version'] == '2.0'
    assert cleanup['requested_enabled'] is True and cleanup['supported'] is True
    assert cleanup['workspace_owned'] is True and cleanup['removed_files'] == 1
    assert cleanup['removed_bytes'] == len(b'fixture installer bytes')
    assert cleanup['status'] in ('completed', 'partial')
    payload, = provider.fixture_paths
    assert not payload.exists() and personal.read_bytes() == b'personal download'
    assert (payload.parent / 'installer.log').read_bytes() == b'keep diagnostic log'
    assert (payload.parent / 'receipt.json').read_bytes() == b'keep receipt'
    assert (payload.parent / 'unins000.exe').is_file() and (payload.parent / 'renamed-repair.exe').is_file()
    assert list((tmp_path / 'update-payloads').glob('*/manifest.json'))
    assert all('--download-directory' not in call.args[0] for call in provider._run.call_args_list)
    saved = _journal_result({'items': [{'id': 'Vendor.App', 'status': 'completed', 'result': result}]})
    assert saved['items'][0]['result']['payload_cleanup']['workspace_id'] == cleanup['workspace_id']
    assert saved['items'][0]['result']['payload_cleanup']['removed_bytes'] == cleanup['removed_bytes']


@pytest.mark.parametrize('change', ['failed', 'restart', 'old_version', 'live_child', 'unknown_restart', 'portable'])
def test_failure_restart_unverified_and_live_children_preserve_payload(provider, change):
    if change == 'failed': provider.fixture_code = 5
    if change == 'restart': provider.fixture_code = 3010
    if change == 'old_version': provider.fixture_after = '1.0'
    if change == 'live_child': provider.fixture_quiescent = False
    if change == 'unknown_restart': provider._payload_restart_reader.return_value = {'reboot_required': None}
    if change == 'portable': provider.fixture_type = 'portable'
    result = provider.updates_apply('Vendor.App', confirmed=True, unattended=True)
    assert result['payload_cleanup']['removed_files'] == result['payload_cleanup']['removed_bytes'] == 0
    assert provider.fixture_paths[0].read_bytes() == b'fixture installer bytes'


def test_explicit_off_keeps_existing_preferences_and_provider_download(provider, tmp_path):
    path = tmp_path / 'preferences.json'
    raw = b'{"update_payload_cleanup_enabled": false, "retained_old_field": 91}\n'
    path.write_bytes(raw)
    result = provider.updates_apply('Vendor.App', confirmed=True, unattended=True)
    assert result['payload_cleanup']['status'] == 'disabled'
    assert result['payload_cleanup']['removed_bytes'] == 0
    assert path.read_bytes() == raw and not (tmp_path / 'update-payloads').exists()
    assert provider.fixture_paths[0].is_file()
    assert all('environment' not in call.kwargs for call in provider._run.call_args_list)


def test_turning_setting_off_during_installer_preserves_owned_files(provider, tmp_path):
    original = provider._run.side_effect
    def run(arguments, **options):
        value = original(arguments, **options)
        if arguments[1] == 'upgrade' and '--id' in arguments:
            (tmp_path / 'preferences.json').write_text('{"update_payload_cleanup_enabled":false}')
        return value
    provider._run.side_effect = run
    result = provider.updates_apply('Vendor.App', confirmed=True, unattended=True)
    assert result['payload_cleanup']['status'] == 'disabled'
    assert provider.fixture_paths[0].is_file()


@pytest.mark.parametrize('field,value', [('verified', False), ('quiescent', False),
    ('job_active_processes', True), ('target_exit_code', 3010), ('output_eof', False)])
def test_console_quiescence_requires_strict_bound_completion(field, value):
    result = terminal()
    result.console_receipt[field] = value
    assert console_quiescent(result) is False


def test_temp_environment_is_child_only_and_does_not_expand_authority(monkeypatch):
    before = dict(os.environ)
    block = winget_console._environment_block({'TEMP': 'D:\\fixture\\attempt', 'TMP': 'D:\\fixture\\attempt'})
    values = ''.join(block).split('\0')
    assert 'TEMP=D:\\fixture\\attempt' in values and 'TMP=D:\\fixture\\attempt' in values
    assert dict(os.environ) == before
    with pytest.raises(ValueError): winget_console._environment_block({'PATH': 'D:\\fixture'})
    with pytest.raises(ValueError): winget_console._environment_block({'TEMP': 'relative'})


def test_saved_app_presence_uses_same_owned_payload_gate(presence_backend):
    from optimus.app_presence import WindowsAppPresence
    service = presence_backend
    original = service.maintenance._run
    payloads = []
    def run(arguments, *, timeout, progress_callback=None, environment=None):
        value = original(arguments, timeout=timeout, progress_callback=progress_callback)
        if arguments[1] == 'show':
            value.stdout = value.stdout.replace('a' * 64, hashlib.sha256(b'fixture reader installer').hexdigest())
        if arguments[1] == 'install':
            payload = Path(environment['TEMP']) / 'fixture-reader.msi'
            payload.write_bytes(b'fixture reader installer')
            payloads.append(payload)
            return terminal()
        return value
    service.maintenance._run = run
    operation = WindowsAppPresence(service)
    snapshot = operation.inspect('Example.Reader')
    result = operation.execute(snapshot, 'install')
    assert result['verified'] is True and result['payload_cleanup']['removed_files'] == 1
    assert not payloads[0].exists()
