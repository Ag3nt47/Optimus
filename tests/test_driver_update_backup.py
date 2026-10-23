"""Targeted backup fixtures never query or export actual host drivers."""
import copy
import hashlib
import json
import os
from pathlib import Path
import threading
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from optimus import driver_update_backup as module


def offer(number=1, hardware=r'PCI\VEN_1234&DEV_5678'):
    return {'id': f'00000000-0000-0000-0000-{number:012d}', 'revision': 3,
            'title': 'Fixture driver', 'hardwareId': hardware}


def device(identity='one', inf='oem17.inf', hardware=r'PCI\VEN_1234&DEV_5678'):
    return {'deviceId': identity, 'displayName': 'Fixture device', 'present': True,
        'hardwareIds': [hardware], 'compatibleIds': [], 'installedDrivers': [
            {'inf': inf, 'version': '1.0', 'provider': 'Fixture', 'date': '2026-01-01',
             'deviceClass': 'System', 'signed': True}]}


class FakeAdapter:
    def __init__(self):
        self.devices = [device()]
        self.calls = []
        self.after_export = None
        self.on_export = None
        self.complete = True
        self.failed = set()
        self.infs = {'oem17.inf': b'[Version]\n; installed OEM17 fixture',
                     'oem23.inf': b'[Version]\n; installed OEM23 fixture'}
        self.changed = set()

    def inventory(self):
        self.calls.append(('inventory',))
        return {'inventoryComplete': self.complete, 'devices': copy.deepcopy(self.devices)}

    def installed_inf_hash(self, inf):
        self.calls.append(('hash', inf))
        return hashlib.sha256(self.infs[inf] + (b'changed' if inf in self.changed else b'')).hexdigest()

    def export(self, inf, destination, **kwargs):
        self.calls.append(('export', inf))
        assert inf != '*' and inf in self.infs
        prepared = json.loads((destination.parent / 'receipt.json').read_text())
        assert prepared['status'] == 'exporting' and not prepared['backed_updates']
        if self.on_export:
            self.on_export(inf, destination, kwargs)
        (destination / 'driver.inf').write_bytes(self.infs[inf])
        (destination / 'driver.sys').write_bytes(b'fixture driver only')
        if self.after_export:
            self.after_export(inf, destination)
        return {'exit_code': 1 if inf in self.failed else 0, 'output': 'fixture only'}


@pytest.fixture
def tool(tmp_path):
    backend = SimpleNamespace(data_dir=tmp_path / 'state', is_admin=Mock(return_value=True))
    adapter = FakeAdapter()
    service = module.DriverUpdateBackup(backend, adapter=adapter, owner='fixture-owner', system='win32')
    return service, adapter, backend


def exact(rows):
    return [{'id': row['id'], 'revision': row['revision']} for row in rows]


def test_constructor_and_unsupported_cancelled_nonadmin_do_no_native_work(tool):
    service, adapter, backend = tool
    assert not adapter.calls and not backend.data_dir.exists()
    service.system = 'linux'
    assert service.backup([offer()])['blocked_updates'] and not adapter.calls
    service.system = 'win32'
    event = threading.Event(); event.set()
    assert service.backup([offer()], cancel_event=event)['backed_updates'] == []
    backend.is_admin.return_value = False
    assert 'Administrator' in service.backup([offer()])['reason']
    assert not adapter.calls and not backend.data_dir.exists()


def test_exact_offer_maps_and_verified_receipt_is_durable_before_export(tool):
    service, adapter, _ = tool
    progress = []
    result = service.backup([offer()], item_callback=progress.append)
    assert result['exit_code'] == 0 and result['backed_updates'] == exact([offer()])
    assert result['blocked_updates'] == [] and result['status'] == 'completed'
    assert [call for call in adapter.calls if call[0] == 'export'] == [('export', 'oem17.inf')]
    receipt = json.loads(Path(result['receipt']).read_text())
    assert receipt['owner'] == 'fixture-owner' and receipt['status'] == 'completed'
    assert receipt['backed_updates'] == result['backed_updates']
    package = receipt['packages'][0]
    assert package['verified'] and len(package['files']) == 2
    assert progress[0]['phase'] == 'backup'


def test_compatible_id_matching_is_exact_casefolded_and_shared_package_exports_once(tool):
    service, adapter, _ = tool
    adapter.devices[0]['hardwareIds'] = ['OTHER']
    adapter.devices[0]['compatibleIds'] = [r'pci\ven_1234&dev_5678']
    result = service.backup([offer(), offer(2)])
    assert result['backed_updates'] == exact([offer(), offer(2)])
    assert len([call for call in adapter.calls if call[0] == 'export']) == 1


def test_five_shared_offers_hash_package_only_initial_and_final_once_per_run(tool, monkeypatch):
    service, adapter, _ = tool
    files = Mock(wraps=service._files)
    monkeypatch.setattr(service, '_files', files)
    offers = [offer(number) for number in range(1, 6)]
    for run in range(2):
        result = service.backup(offers)
        assert result['backed_updates'] == exact(offers)
        assert result['packages'][0]['final_verified'] is True
        assert files.call_count == (run + 1) * 2
        assert adapter.calls.count(('export', 'oem17.inf')) == run + 1
        # Source identity before/after export and one final source check.
        assert adapter.calls.count(('hash', 'oem17.inf')) == (run + 1) * 3


def test_failed_shared_final_verification_blocks_all_references_without_rehashing(tool, monkeypatch):
    service, adapter, _ = tool
    original = service._files
    calls = []
    def verify(*args, **kwargs):
        calls.append(1)
        if len(calls) == 2:
            raise ValueError('Fixture final package changed')
        return original(*args, **kwargs)
    monkeypatch.setattr(service, '_files', verify)
    result = service.backup([offer(number) for number in range(1, 6)])
    assert not result['backed_updates'] and len(result['blocked_updates']) == 5
    assert len(calls) == 2 and result['packages'][0]['final_verified'] is False
    assert all('Fixture final package changed' in row['reason'] for row in result['blocked_updates'])


@pytest.mark.parametrize('change,reason', [
    (lambda adapter: setattr(adapter, 'complete', False), 'complete'),
    (lambda adapter: adapter.devices[0].update(present=None), 'No exact'),
    (lambda adapter: adapter.devices[0].update(hardwareIds=['PCI\\VEN_1234']), 'No exact'),
    (lambda adapter: adapter.devices[0]['installedDrivers'][0].update(inf='inbox.inf'), 'inbox'),
    (lambda adapter: adapter.devices[0].update(installedDrivers=[]), 'identity'),
    (lambda adapter: adapter.devices[0]['installedDrivers'][0].update(provider=''), 'incomplete'),
    (lambda adapter: adapter.devices[0].update(hardwareIds='bad'), 'identifiers'),
])
def test_missing_ambiguous_inbox_or_incomplete_mapping_never_claims_backup(tool, change, reason):
    service, adapter, _ = tool
    change(adapter)
    result = service.backup([offer()])
    assert not result['backed_updates'] and result['exit_code'] != 0
    assert reason.lower() in result['blocked_updates'][0]['reason'].lower()
    assert not any(call[0] == 'export' for call in adapter.calls)


def test_a_failed_oem_export_does_not_block_a_different_verified_offer(tool):
    service, adapter, _ = tool
    adapter.devices.append(device('two', 'oem23.inf', 'SECOND'))
    adapter.failed.add('oem17.inf')
    result = service.backup([offer(), offer(2, 'SECOND')])
    assert result['status'] == 'partial' and result['exit_code'] == 0
    assert result['backed_updates'] == exact([offer(2, 'SECOND')])
    assert result['blocked_updates'][0]['id'] == offer()['id']


def attach_inbox_fixture(adapter, tmp_path):
    from optimus.inbox_driver_backup import InboxDriverSnapshot, _INBOX_SCORE
    folder = tmp_path / 'native-windows-fixture' / 'monitor-package'
    folder.mkdir(parents=True)
    inf = folder / 'monitor.inf'; inf.write_bytes(b'[Version]\n; signed fixture')
    catalog = folder / 'monitor.cat'; catalog.write_bytes(b'fixture catalog')
    description = {'kind': 'inbox-driver-store-snapshot', 'published_inf': str(inf),
        'store_inf': str(inf), 'package_directory': str(folder), 'catalog': str(catalog),
        'catalog_trust_verified': True, 'signer_score': _INBOX_SCORE}
    catalog_adapter = SimpleNamespace(describe=Mock(side_effect=lambda _: copy.deepcopy(description)))
    engine = InboxDriverSnapshot(catalog_adapter, disk_usage=lambda _: SimpleNamespace(free=16 * 1024 ** 3))
    adapter.snapshot_inbox = Mock(side_effect=engine.snapshot)
    adapter.verify_inbox = Mock(side_effect=engine.verify)
    adapter.devices = [device(inf='monitor.inf')]
    adapter.devices[0]['installedDrivers'][0]['provider'] = 'Microsoft'
    return engine, catalog_adapter, description


def test_signed_inbox_package_uses_verified_snapshot_and_never_fake_oem_export(tool, tmp_path):
    service, adapter, backend = tool
    attach_inbox_fixture(adapter, tmp_path)
    result = service.backup([offer()])
    assert result['backed_updates'] == exact([offer()]) and result['status'] == 'completed'
    assert not any(call[0] in ('export', 'hash') for call in adapter.calls)
    package = result['packages'][0]
    assert package['kind'] == 'inbox-driver-store-snapshot' and package['final_verified'] is True
    assert package['oem_export'] is False and package['automatic_restore_supported'] is False
    adapter.snapshot_inbox.assert_called_once(); adapter.verify_inbox.assert_called_once()
    receipt = json.loads(Path(result['receipt']).read_text())
    assert receipt['packages'][0]['snapshot']['verified'] is True
    # Targeted snapshots cannot accidentally enter the separate signed OEM
    # export/revert catalog merely by using a familiar INF/package filename.
    from optimus.driver_backups import DriverBackups
    history = DriverBackups(backend, adapter=Mock(), owner='fixture-owner').history()
    assert history == []


@pytest.mark.parametrize('signed,provider', [(False, 'Microsoft'), (None, 'Microsoft'), (True, 'Other')])
def test_unknown_or_non_microsoft_inbox_identity_never_attempts_snapshot(tool, tmp_path, signed, provider):
    service, adapter, _ = tool
    attach_inbox_fixture(adapter, tmp_path)
    adapter.devices[0]['installedDrivers'][0].update(signed=signed, provider=provider)
    result = service.backup([offer()])
    assert not result['backed_updates'] and 'signed Microsoft' in result['blocked_updates'][0]['reason']
    adapter.snapshot_inbox.assert_not_called()


@pytest.mark.parametrize('changes', [{'version':'unknown'}, {'version':'1.*'}, {'date':'unknown'}, {'date':'2026-13-02'}, {'deviceClass':''}])
def test_inbox_requires_known_installed_version_date_and_class(tool, tmp_path, changes):
    service, adapter, _ = tool
    attach_inbox_fixture(adapter, tmp_path)
    adapter.devices[0]['installedDrivers'][0].update(changes)
    result = service.backup([offer()])
    assert not result['backed_updates']
    adapter.snapshot_inbox.assert_not_called()


def test_untrusted_inbox_catalog_does_not_block_an_independently_backed_oem(tool, tmp_path):
    service, adapter, _ = tool
    _, _, description = attach_inbox_fixture(adapter, tmp_path)
    description['catalog_trust_verified'] = False
    adapter.devices.append(device('two', 'oem23.inf', 'SECOND'))
    result = service.backup([offer(), offer(2, 'SECOND')])
    assert result['status'] == 'partial' and result['backed_updates'] == exact([offer(2, 'SECOND')])
    assert 'incomplete' in result['blocked_updates'][0]['reason']


def test_inbox_device_identity_and_copy_are_revalidated_after_backup(tool, tmp_path):
    service, adapter, _ = tool
    engine, _, _ = attach_inbox_fixture(adapter, tmp_path)
    def change_device(*args, **kwargs):
        result = engine.snapshot(*args, **kwargs)
        adapter.devices[0]['installedDrivers'][0]['version'] = 'changed'
        return result
    adapter.snapshot_inbox.side_effect = change_device
    result = service.backup([offer()])
    assert not result['backed_updates'] and 'identity changed' in result['blocked_updates'][0]['reason']


def test_failed_final_native_inbox_verification_never_authorizes_driver(tool, tmp_path):
    service, adapter, _ = tool
    attach_inbox_fixture(adapter, tmp_path)
    adapter.verify_inbox.return_value = False
    adapter.verify_inbox.side_effect = None
    result = service.backup([offer()])
    assert not result['backed_updates'] and 'reverified' in result['blocked_updates'][0]['reason']


def test_device_change_during_final_inbox_verification_is_not_authorized(tool, tmp_path):
    service, adapter, _ = tool
    engine, _, _ = attach_inbox_fixture(adapter, tmp_path)
    def change_after_verification(*args, **kwargs):
        result = engine.verify(*args, **kwargs)
        adapter.devices[0]['installedDrivers'][0]['version'] = '2.0'
        return result
    adapter.verify_inbox.side_effect = change_after_verification
    result = service.backup([offer()])
    assert not result['backed_updates']
    assert 'identity changed' in result['blocked_updates'][0]['reason']


@pytest.mark.parametrize('kind', ['inf_bytes', 'duplicate_inf', 'changed_installed_inf', 'changed_device', 'hardlink', 'symlink'])
def test_export_identity_hash_and_ordinary_file_checks_prevent_false_backup(tool, kind, tmp_path):
    service, adapter, _ = tool
    def alter(inf, folder):
        if kind == 'inf_bytes':
            (folder / 'driver.inf').write_bytes(b'wrong package')
        elif kind == 'duplicate_inf':
            (folder / 'duplicate.inf').write_bytes(adapter.infs[inf])
        elif kind == 'changed_installed_inf':
            adapter.changed.add(inf)
        elif kind == 'changed_device':
            adapter.devices[0]['installedDrivers'][0]['version'] = 'changed'
        elif kind == 'hardlink':
            os.link(folder / 'driver.sys', folder / 'linked.sys')
        elif kind == 'symlink':
            try:
                (folder / 'linked.sys').symlink_to(folder / 'driver.sys')
            except OSError:
                pytest.skip('Fixture user cannot create symbolic links.')
    adapter.after_export = alter
    result = service.backup([offer()])
    assert not result['backed_updates'] and result['blocked_updates']


def test_cancel_during_readonly_export_stops_new_exports_and_claims_no_backup(tool):
    service, adapter, _ = tool
    adapter.devices.append(device('two', 'oem23.inf', 'SECOND'))
    event = threading.Event()
    adapter.on_export = lambda *args: event.set()
    result = service.backup([offer(), offer(2, 'SECOND')], cancel_event=event)
    assert not result['backed_updates']
    assert len([call for call in adapter.calls if call[0] == 'export']) == 1
    receipt = json.loads(Path(result['receipt']).read_text())
    assert not receipt['backed_updates']


def test_total_deadline_prevents_any_native_export(tool):
    service, adapter, _ = tool
    service.total_timeout = -1
    result = service.backup([offer()])
    assert not result['backed_updates'] and not any(call[0] == 'export' for call in adapter.calls)


def test_receipt_write_failure_before_export_does_not_start_native_work(tool, monkeypatch):
    service, adapter, _ = tool
    monkeypatch.setattr(module, '_private_json', Mock(side_effect=OSError('fixture journal failure')))
    with pytest.raises(OSError, match='journal'):
        service.backup([offer()])
    assert not adapter.calls


@pytest.mark.parametrize('offers', [[], [offer(), offer()], [dict(offer(), id='bad')], [dict(offer(), revision=True)]])
def test_offer_validation_happens_before_any_native_probe(tool, offers):
    service, adapter, _ = tool
    with pytest.raises(ValueError):
        service.backup(offers)
    assert not adapter.calls


def test_native_adapter_builds_only_exact_export_command_and_never_uses_shell(tmp_path, monkeypatch):
    system = tmp_path / 'System32'; system.mkdir()
    (system / 'pnputil.exe').write_bytes(b'fixture placeholder; never execute')
    def system_directory(buffer, length):
        buffer.value = str(system)
        return len(str(system))
    monkeypatch.setattr(module.ctypes, 'windll', SimpleNamespace(kernel32=SimpleNamespace(GetSystemDirectoryW=system_directory)), raising=False)
    monkeypatch.setattr(module.sys, 'platform', 'win32')
    process = SimpleNamespace(poll=lambda: 0, wait=lambda **kwargs: 0)
    popen = Mock(return_value=process)
    monkeypatch.setattr(module.subprocess, 'Popen', popen)
    destination = tmp_path / 'oem17.inf'; destination.mkdir()
    outcome = module.WindowsTargetedExport(None).export('oem17.inf', destination)
    command = popen.call_args.args[0]
    assert command == [str(system / 'pnputil.exe'), '/export-driver', 'oem17.inf', str(destination)]
    assert popen.call_args.kwargs['shell'] is False and outcome['exit_code'] == 0
    with pytest.raises(ValueError):
        module.WindowsTargetedExport(None).export('*', destination)
    assert popen.call_count == 1


def test_backend_hook_uses_service_without_full_export(monkeypatch, tmp_path):
    from optimus.backend import Backend
    service = Mock()
    factory = Mock(return_value=service)
    monkeypatch.setattr(module, 'DriverUpdateBackup', factory)
    backend = SimpleNamespace(data_dir=tmp_path)
    event, callback = threading.Event(), Mock()
    Backend.driver_backup_for_updates(backend, [offer()], event, callback)
    factory.assert_called_once_with(backend)
    service.backup.assert_called_once_with([offer()], cancel_event=event, item_callback=callback)


@pytest.mark.parametrize('mode', ['cancel', 'timeout', 'callback_error'])
def test_native_export_drains_only_owned_readonly_process_on_stop_or_callback_error(tmp_path, monkeypatch, mode):
    system = tmp_path / 'System32'; system.mkdir()
    (system / 'pnputil.exe').write_bytes(b'fixture; never execute')
    def system_directory(buffer, length):
        buffer.value = str(system)
        return len(str(system))
    monkeypatch.setattr(module.ctypes, 'windll', SimpleNamespace(kernel32=SimpleNamespace(GetSystemDirectoryW=system_directory)), raising=False)
    monkeypatch.setattr(module.sys, 'platform', 'win32')
    event = threading.Event()
    state = {'running': True, 'clock': 0}
    def clock():
        state['clock'] += 0.6
        return state['clock']
    def terminate():
        state['running'] = False
    process = SimpleNamespace(poll=lambda: None if state['running'] else -15,
        wait=lambda **kwargs: -15, terminate=Mock(side_effect=terminate), kill=Mock())
    def spawn(*args, **kwargs):
        if mode == 'cancel':
            event.set()
        return process
    popen = Mock(side_effect=spawn)
    monkeypatch.setattr(module.subprocess, 'Popen', popen)
    monkeypatch.setattr(module.time, 'monotonic', clock)
    monkeypatch.setattr(module.time, 'sleep', Mock())
    destination = tmp_path / 'oem17.inf'; destination.mkdir()
    callback = Mock(side_effect=RuntimeError('fixture progress failed')) if mode == 'callback_error' else None
    if mode == 'callback_error':
        with pytest.raises(RuntimeError, match='progress'):
            module.WindowsTargetedExport(None).export('oem17.inf', destination, timeout=180, progress_callback=callback)
    else:
        result = module.WindowsTargetedExport(None).export('oem17.inf', destination, cancel_event=event,
            timeout=0 if mode == 'timeout' else 180)
        assert result['cancelled' if mode == 'cancel' else 'timed_out']
    process.terminate.assert_called_once_with()
    process.kill.assert_not_called()
    assert popen.call_args.args[0][1:3] == ['/export-driver', 'oem17.inf']


def test_native_export_already_cancelled_never_spawns_process(tmp_path, monkeypatch):
    monkeypatch.setattr(module.sys, 'platform', 'win32')
    popen = Mock(side_effect=AssertionError('Cancelled export must not launch.'))
    monkeypatch.setattr(module.subprocess, 'Popen', popen)
    event = threading.Event(); event.set()
    assert module.WindowsTargetedExport(None).export('oem17.inf', tmp_path, cancel_event=event)['cancelled']
    popen.assert_not_called()
