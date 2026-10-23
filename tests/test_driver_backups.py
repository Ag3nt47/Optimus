"""Recovery fixtures only: no real device query, export, install or rollback."""
import copy
import ctypes
import hashlib
import json
import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from optimus.automation import RunLease
from optimus import driver_backups as recovery


def device(version='2.0', inf='oem17.inf', *, present=True):
    return {'deviceId': r'PCI\VEN_1234&DEV_5678\FIXTURE', 'displayName': 'Fixture display',
            'present': present, 'installedDrivers': [{'version': version, 'date': '2026-10-20',
            'inf': inf, 'provider': 'Fixture provider', 'deviceClass': 'DISPLAY', 'signed': True}]}


class FakeAdapter:
    def __init__(self):
        self.current = device()
        self.calls = []
        self.outcome = {'supported': True, 'called': True, 'success': True, 'winerror': 0,
                        'restartNeeded': False, 'outcomeKnown': True, 'running': False}
        self.change_after_rollback = True
        self.inf_data = b'[Version]\nSignature="$Windows NT$"\n'
        self.export_exit = 0
        self.mapping = True
        self.before_export = None
        self.during_export = None
        self.before_rollback = None
        self.readback_error = False

    def inventory(self):
        self.calls.append('inventory')
        if self.readback_error and 'rollback' in self.calls:
            raise OSError('Fixture readback unavailable')
        return {'inventoryComplete': True, 'devices': [copy.deepcopy(self.current)]}

    def export(self, destination):
        self.calls.append('export')
        if self.before_export:
            self.before_export(destination)
        folder = Path(destination) / 'fixture.inf_amd64'
        folder.mkdir()
        (folder / 'fixture.inf').write_bytes(self.inf_data)
        (folder / 'fixture.sys').write_bytes(b'fixture driver bytes')
        (folder / 'fixture.cat').write_bytes(b'fixture catalog bytes')
        if self.during_export:
            self.during_export()
        return {'exit_code': self.export_exit, 'output': 'Fixture export only'}

    def installed_inf_hash(self, inf):
        return hashlib.sha256(self.inf_data if self.mapping else b'changed').hexdigest()

    def rollback(self, device_id):
        self.calls.append('rollback')
        if self.before_rollback:
            self.before_rollback(device_id)
        if isinstance(self.outcome, Exception):
            raise self.outcome
        if self.change_after_rollback and self.outcome.get('success'):
            self.current = device('1.0', 'oem9.inf')
        return copy.deepcopy(self.outcome)


@pytest.fixture
def tool(tmp_path):
    backend = SimpleNamespace(data_dir=tmp_path / 'state', system='win32', is_admin=Mock(return_value=True),
        security_status=Mock(return_value={'reboot_required': False}), update_disk_readiness=Mock(return_value={'ready': True}))
    adapter = FakeAdapter()
    service = recovery.DriverBackups(backend, adapter=adapter, owner='fixture-owner', barrier_reader=lambda _:None)
    return service, adapter, backend


def choice(service):
    report = service.scan()
    return report['plan_id'], report['devices'][0]['selection_id']


def test_constructor_and_empty_catalog_are_read_only(tool):
    service, adapter, backend = tool
    assert adapter.calls == [] and not backend.data_dir.exists()
    assert service.history() == [] and not backend.data_dir.exists()
    scan = service.scan()
    assert scan['read_only'] and scan['devices'][0]['rollback_eligible']
    assert adapter.calls == ['inventory'] and not backend.data_dir.exists()


@pytest.mark.parametrize('confirmed', [False, None, 'yes', 1])
def test_export_and_rollback_require_boolean_confirmation_before_host_work(tool, confirmed):
    service, adapter, backend = tool
    with pytest.raises(ValueError):
        service.export(confirmed=confirmed)
    with pytest.raises(ValueError):
        service.rollback('a'*32, 'b'*32, confirmed=confirmed)
    assert adapter.calls == [] and not backend.data_dir.exists()


def test_export_intent_precedes_adapter_and_hashes_map_exact_oem_inf(tool):
    service, adapter, _ = tool
    def prepared(destination):
        value = json.loads((destination.parent / 'manifest.json').read_text())
        assert value['status'] == 'exporting' and value['devices'][0]['drivers'][0]['version'] == '2.0'
    adapter.before_export = prepared
    result = service.export(confirmed=True)
    assert result['status'] == 'completed' and result['verification']['file_count'] == 3
    assert result['packages'][0]['published_inf'] == 'oem17.inf'
    assert result['packages'][0]['mapping_verified'] and result['packages'][0]['exported_infs'] == ['fixture.inf_amd64/fixture.inf']
    assert service.verify(result['id'])['verified']
    assert len(service.history()) == 1 and adapter.calls.count('export') == 1


def test_verify_detects_changed_bytes_and_untracked_files(tool):
    service, _, _ = tool
    result = service.export(confirmed=True)
    base = service.store.directory / result['id'] / 'packages' / 'fixture.inf_amd64'
    (base / 'fixture.sys').write_bytes(b'altered')
    assert service.verify(result['id'])['status'] == 'needs_review'
    (base / 'new.inf').write_bytes(b'untracked')
    assert service.verify(result['id'])['unexpected_files'] == ['fixture.inf_amd64/new.inf']


def test_modified_manifest_or_foreign_owner_cannot_authorize_verification(tool):
    service, _, _ = tool
    result = service.export(confirmed=True)
    path = service.store.directory / result['id'] / 'manifest.json'
    raw = json.loads(path.read_text()); raw['devices'][0]['drivers'][0]['version'] = 'forged'
    path.write_text(json.dumps(raw))
    with pytest.raises(ValueError, match='signature'):
        service.verify(result['id'])
    raw['owner'] = 'foreign'
    path.write_text(json.dumps(raw))
    with pytest.raises(ValueError):
        service.history()


@pytest.mark.parametrize('relative', ['../other.inf', '/outside.inf', r'a\b.inf', 'C:/other.inf', 'a/./b.inf'])
def test_invalid_backup_relative_paths_never_become_read_authority(tool, relative):
    service, _, _ = tool
    result = service.export(confirmed=True)
    saved = service.store.load(result['id']); saved['files'][0]['path'] = relative
    service.store.write(saved)
    with pytest.raises(ValueError, match='relative file path'):
        service.verify(result['id'])


def test_hardlinked_manifest_is_rejected(tool, tmp_path):
    service, _, _ = tool
    result = service.export(confirmed=True)
    original = service.store.directory / result['id'] / 'manifest.json'
    try:
        os.link(original, tmp_path / 'manifest-link.json')
    except OSError:
        pytest.skip('Hardlinks unavailable in this fixture filesystem')
    with pytest.raises(ValueError, match='hardlinks'):
        service.verify(result['id'])


@pytest.mark.parametrize('condition', ['admin', 'barrier', 'restart', 'disk'])
def test_rollback_preflight_blocks_before_backup_or_native_api(tool, condition):
    service, adapter, backend = tool
    selection = choice(service)
    if condition == 'admin': backend.is_admin.return_value = False
    if condition == 'barrier': service.barrier_reader = lambda _:'Fixture installer unresolved'
    if condition == 'restart': backend.security_status.return_value = {'reboot_required': True}
    if condition == 'disk': backend.update_disk_readiness.return_value = {'ready': False}
    result = service.rollback(*selection, confirmed=True)
    assert result['status'] == 'blocked' and result['installer_started'] is False
    assert adapter.calls == ['inventory']


def test_another_maintenance_lease_blocks_all_recovery_mutations(tool):
    service, adapter, backend = tool
    selection = choice(service)
    with RunLease(backend.data_dir):
        with pytest.raises(RuntimeError, match='already running'):
            service.rollback(*selection, confirmed=True)
    assert 'export' not in adapter.calls and 'rollback' not in adapter.calls


def test_device_changed_since_scan_refuses_before_export(tool):
    service, adapter, _ = tool
    selection = choice(service); adapter.current = device('3.0')
    with pytest.raises(ValueError, match='changed'):
        service.rollback(*selection, confirmed=True)
    assert 'export' not in adapter.calls and 'rollback' not in adapter.calls


def test_successful_rollback_exports_first_and_reads_back_changed_version(tool):
    service, adapter, _ = tool
    selection = choice(service)
    def prepared(device_id):
        history = service.history()
        active = next(item for item in history if item['kind'] == 'driver-rollback')
        saved = service.store.load(active['id'])
        assert saved['status'] == 'prepared' and saved['outcomeKnown'] is False
        assert device_id == device()['deviceId'] and saved['preflight_backup_id']
    adapter.before_rollback = prepared
    result = service.rollback(*selection, confirmed=True)
    assert adapter.calls.index('export') < adapter.calls.index('rollback')
    assert result['status'] == 'completed' and result['verified']
    assert result['before']['drivers'][0]['version'] == '2.0' and result['after']['drivers'][0]['version'] == '1.0'
    assert result['outcomeKnown'] is True and result['running'] is False and result['installer_started'] is True
    with pytest.raises(ValueError):
        service.rollback(*selection, confirmed=True)


@pytest.mark.parametrize('failure', ['export', 'mapping', 'changing_inventory'])
def test_failed_or_unmapped_or_unstable_backup_prevents_rollback(tool, failure):
    service, adapter, _ = tool
    selection = choice(service)
    if failure == 'export': adapter.export_exit = 1
    if failure == 'mapping': adapter.mapping = False
    if failure == 'changing_inventory': adapter.during_export = lambda:setattr(adapter, 'current', device('3.0'))
    result = service.rollback(*selection, confirmed=True)
    assert result['status'] == 'blocked' and 'rollback' not in adapter.calls


@pytest.mark.parametrize('mode', ['no_previous', 'unsupported', 'same_version', 'restart', 'native_error', 'unknown', 'readback_error'])
def test_rollback_terminal_states_never_claim_unverified_success(tool, mode):
    service, adapter, _ = tool
    selection = choice(service)
    expected = {'no_previous':'unavailable', 'unsupported':'unsupported', 'same_version':'needs_review',
                'restart':'restart_required', 'native_error':'failed', 'unknown':'uncertain', 'readback_error':'needs_review'}[mode]
    if mode == 'no_previous': adapter.outcome.update(success=False, winerror=259)
    if mode == 'unsupported': adapter.outcome = {'supported':False,'called':False,'success':False,'error':'Fixture API unavailable'}
    if mode == 'same_version': adapter.change_after_rollback = False
    if mode == 'restart': adapter.outcome['restartNeeded'] = True
    if mode == 'native_error': adapter.outcome.update(success=False, winerror=5)
    if mode == 'unknown': adapter.outcome = RuntimeError('Fixture API disconnected')
    if mode == 'readback_error': adapter.readback_error = True
    result = service.rollback(*selection, confirmed=True)
    assert result['status'] == expected and result.get('verified') is not True
    assert result['outcomeKnown'] is (mode != 'unknown')
    if mode == 'unsupported': assert result['installer_started'] is False
    if mode == 'restart': assert result['restartNeeded'] is True


def test_device_inventory_must_be_complete_and_present_identity_known(tool):
    service, adapter, _ = tool
    adapter.inventory = Mock(return_value={'inventoryComplete':False, 'devices':[device()]})
    with pytest.raises(ValueError, match='complete'):
        service.scan()
    adapter.inventory.return_value = {'inventoryComplete':True,'devices':[device(present=None)]}
    report = service.scan()
    assert not report['devices'][0]['rollback_eligible']
    with pytest.raises(ValueError, match='complete rollback identity'):
        service.rollback(report['plan_id'], report['devices'][0]['selection_id'], confirmed=True)


def test_journal_failure_prevents_export_and_native_rollback(tool, monkeypatch):
    service, adapter, _ = tool
    selection = choice(service)
    monkeypatch.setattr(service.store, 'write', Mock(side_effect=OSError('Fixture disk unavailable')))
    with pytest.raises(OSError):
        service.rollback(*selection, confirmed=True)
    assert 'export' not in adapter.calls and 'rollback' not in adapter.calls


def test_unknown_recovery_receipt_prevents_any_later_automatic_retry(tool):
    service, adapter, _ = tool
    selection = choice(service); adapter.outcome = RuntimeError('Fixture unknown native outcome')
    assert service.rollback(*selection, confirmed=True)['status'] == 'uncertain'
    count = adapter.calls.count('export')
    assert service.pending_barrier()
    assert service.export(confirmed=True)['status'] == 'blocked'
    assert adapter.calls.count('export') == count


@pytest.mark.parametrize('metadata', [{'signed':None}, {'deviceClass':'Changed fixture classification'}, {'provider':'FIXTURE PROVIDER'}])
def test_metadata_only_change_does_not_verify_a_different_installed_driver(tool, metadata):
    service, adapter, _ = tool
    selection = choice(service); adapter.change_after_rollback = False
    adapter.before_rollback = lambda _:adapter.current['installedDrivers'][0].update(metadata)
    result = service.rollback(*selection, confirmed=True)
    assert result['status'] == 'needs_review' and result['verified'] is False


@pytest.mark.parametrize('changes', [{'called':False}, {'winerror':5}, {'outcomeKnown':False}, {'running':True}, {'restartNeeded':None}])
def test_contradictory_or_incomplete_native_metadata_remains_uncertain(tool, changes):
    service, adapter, _ = tool
    selection = choice(service); adapter.outcome.update(changes)
    result = service.rollback(*selection, confirmed=True)
    assert result['status'] == 'uncertain' and result['outcomeKnown'] is False
    assert result.get('verified') is not True


@pytest.mark.skipif(os.name != 'nt', reason='Windows ctypes loader fixture')
def test_native_api_uses_exact_instance_no_ui_non_null_reboot_and_releases_handle(monkeypatch):
    calls = []
    class Function:
        def __init__(self, name): self.name = name
        def __call__(self, *arguments):
            calls.append((self.name, arguments))
            if self.name == 'SetupDiCreateDeviceInfoList': return 99
            if self.name == 'SetupDiOpenDeviceInfoW':
                assert arguments[1] == device()['deviceId'] and arguments[3] == 0
                assert arguments[4]._obj.cbSize == ctypes.sizeof(arguments[4]._obj)
            if self.name == 'DiRollbackDriver':
                assert arguments[0] == 99 and arguments[2] is None and arguments[3] == 1
                assert arguments[4] is not None
                arguments[4]._obj.value = True
            return 1
    setup = SimpleNamespace(**{name:Function(name) for name in ('SetupDiCreateDeviceInfoList','SetupDiOpenDeviceInfoW','SetupDiDestroyDeviceInfoList')})
    newdev = SimpleNamespace(DiRollbackDriver=Function('DiRollbackDriver'))
    monkeypatch.setattr(ctypes, 'WinDLL', lambda name, **kwargs:newdev if str(name).endswith('newdev.dll') else setup)
    result = recovery.WindowsDriverRecovery.rollback(device()['deviceId'])
    assert result['success'] and result['restartNeeded'] and result['outcomeKnown']
    assert calls[-1][0] == 'SetupDiDestroyDeviceInfoList'
    assert all('reboot' not in name.casefold() for name, _ in calls)
