"""Payload cleanup mutates only freshly created per-test workspaces."""
import json
import os
from pathlib import Path
import pickle

import pytest

from optimus import update_payloads as module
from optimus.update_payloads import PayloadWorkspace


SUCCESS = dict(exitCode=0, installer_started=True, outcomeKnown=True, running=False)


@pytest.fixture
def workspace(tmp_path):
    data = tmp_path / 'Data'
    data.mkdir()
    service = PayloadWorkspace.create(data, 'winget', 'Fixture.Package')
    return service, Path(service.env_overrides()['TEMP']), data


def finish(service, **options):
    return service.finish(dict(SUCCESS), verified=True, quiescent=True, **options)


def test_verified_success_deletes_only_recognized_private_payloads(workspace):
    service, root, data = workspace
    files = {'setup.exe': b'exe', 'package.msi': b'msi', 'archive.zip': b'zip', 'a' * 64: b'hash'}
    for name, raw in files.items():
        (root / name).write_bytes(raw)
    for name in ('install.log', 'receipt.json', 'personal.txt', 'unknown.tmp', 'rollback.zip', 'uninstall.exe'):
        (root / name).write_bytes(b'preserve')
    backup = root / 'driver-backups'
    backup.mkdir()
    (backup / 'driver.cab').write_bytes(b'preserve backup')
    outside = data.parent / 'Downloads'
    outside.mkdir()
    (outside / 'setup.exe').write_bytes(b'personal installer')
    result = finish(service)
    assert result['status'] == 'completed'
    assert result['ownership'] == 'optimus_private_update_workspace' and result['workspace_owned'] is True
    assert result['removed_files'] == len(files) and result['removed_bytes'] == sum(map(len, files.values()))
    assert all(not (root / name).exists() for name in files)
    assert (outside / 'setup.exe').read_bytes() == b'personal installer'
    assert (backup / 'driver.cab').read_bytes() == b'preserve backup'
    assert (root / 'install.log').exists() and (root / 'receipt.json').exists()
    manifest = root.parent / 'manifest.json'
    assert manifest.exists() and manifest.parent != root
    assert json.loads((root.parent / 'cleanup-result.json').read_text()) == result


@pytest.mark.parametrize('changes', [
    {'exitCode': 1}, {'exitCode': 3010}, {'exitCode': True}, {'outcomeKnown': False},
    {'running': True}, {'installer_started': False}, {'requiresReview': True},
    {'restartNeeded': True}, {'reboot_required': True}, {'cancelled': True}, {'skipped': True},
])
def test_failed_active_unknown_or_restart_result_preserves_every_byte(workspace, changes):
    service, root, _ = workspace
    path = root / 'installer.exe'
    path.write_bytes(b'needed for review')
    result = service.finish(dict(SUCCESS, **changes), verified=True, quiescent=True)
    assert result['status'] == 'preserved' and result['removed_files'] == result['removed_bytes'] == 0
    assert path.read_bytes() == b'needed for review'
    assert not (root.parent / 'cleanup-intent.json').exists()


@pytest.mark.parametrize('options', [dict(), dict(verified=True), dict(quiescent=True),
    dict(verified=True, quiescent=True, restart_pending=True), dict(verified=1, quiescent=True)])
def test_explicit_verified_and_quiescent_proof_is_required(workspace, options):
    service, root, _ = workspace
    (root / 'setup.exe').write_bytes(b'preserve')
    result = service.finish(SUCCESS, **options)
    assert result['removed_bytes'] == 0 and (root / 'setup.exe').exists()


def test_disabled_never_creates_or_inspects_data(tmp_path):
    missing = tmp_path / 'absent'
    service = PayloadWorkspace.create(missing, 'fixture', 'Fixture.Package', enabled=False)
    assert service.env_overrides() == {} and not missing.exists()
    assert finish(service)['status'] == 'disabled'
    assert not missing.exists()


def test_env_is_only_the_private_payload_and_provider_cannot_choose_paths(workspace):
    service, root, data = workspace
    assert service.env_overrides() == dict(TEMP=str(root), TMP=str(root), TMPDIR=str(root))
    assert root.name == 'payload' and root.parent.parent == data / 'update-payloads'
    value = PayloadWorkspace.create(data, '../../Downloads', 'C:/personal/setup.exe')
    other = Path(value.env_overrides()['TEMP'])
    assert other.parent.parent == root.parent.parent and other != root


def test_finish_is_idempotent_and_does_not_reclaim_files_added_afterward(workspace):
    service, root, _ = workspace
    (root / 'setup.exe').write_bytes(b'first')
    first = finish(service)
    (root / 'new.exe').write_bytes(b'new bytes')
    first['removed_bytes'] = 999
    second = finish(service)
    assert second['removed_bytes'] == 5 and second['removed_files'] == 1
    assert (root / 'new.exe').exists() and service.env_overrides() == {}


def test_serialized_manifest_is_not_cleanup_authority(workspace):
    service, root, _ = workspace
    with pytest.raises(TypeError):
        PayloadWorkspace()
    with pytest.raises(TypeError):
        pickle.dumps(service)
    (root / 'setup.exe').write_bytes(b'preserve')
    manifest = root.parent / 'manifest.json'
    manifest.write_bytes(b'{"schema":1}')
    result = finish(service)
    assert result['removed_bytes'] == 0 and (root / 'setup.exe').exists()


def test_replaced_root_is_not_owned_even_with_same_names(workspace):
    service, root, _ = workspace
    (root / 'setup.exe').write_bytes(b'original')
    old = root.with_name('preserved-original')
    root.rename(old)
    root.mkdir()
    (root / 'setup.exe').write_bytes(b'unowned replacement')
    result = finish(service)
    assert result['removed_bytes'] == 0 and (root / 'setup.exe').read_bytes() == b'unowned replacement'
    assert result['ownership'] == 'none' and result['workspace_owned'] is False
    assert (old / 'setup.exe').read_bytes() == b'original'


def test_changed_file_identity_is_preserved_at_the_delete_boundary(workspace, monkeypatch):
    service, root, _ = workspace
    path = root / 'setup.exe'
    path.write_bytes(b'original')
    delete = module.Cleaner._delete_exact
    def race(record):
        path.unlink()
        path.write_bytes(b'replacement which was not scanned')
        return delete(record)
    monkeypatch.setattr(module.Cleaner, '_delete_exact', race)
    result = finish(service)
    assert result['removed_bytes'] == 0 and result['status'] == 'partial'
    assert path.read_bytes() == b'replacement which was not scanned'


def test_hardlinked_payload_is_retained(workspace):
    service, root, data = workspace
    original = data.parent / 'personal.exe'
    original.write_bytes(b'linked')
    try:
        os.link(original, root / 'setup.exe')
    except OSError:
        pytest.skip('Hardlink creation is unavailable for this fixture filesystem.')
    result = finish(service)
    assert result['removed_bytes'] == 0 and original.read_bytes() == b'linked'
    assert (root / 'setup.exe').exists()


def test_entry_bound_preserves_payloads_without_partial_scan_authority(workspace, monkeypatch):
    service, root, _ = workspace
    for name in ('one.exe', 'two.exe'):
        (root / name).write_bytes(b'preserve')
    monkeypatch.setattr(module, '_MAX_ENTRIES', 1)
    result = finish(service)
    assert result['status'] == 'preserved' and result['removed_bytes'] == 0
    assert (root / 'one.exe').exists() and (root / 'two.exe').exists()


def test_nested_payloads_reclaim_only_empty_owned_children(workspace):
    service, root, _ = workspace
    nested = root / 'download' / 'package'
    nested.mkdir(parents=True)
    (nested / 'setup.exe').write_bytes(b'nested')
    result = finish(service)
    assert result['removed_files'] == 1 and result['removed_bytes'] == 6
    assert root.exists() and (root.parent / 'manifest.json').exists()
    assert result['removed_directories'] == (2 if os.name == 'nt' else 0)
    assert nested.exists() is (os.name != 'nt')


@pytest.mark.parametrize('nested', [{'restartNeeded': True}, {'running': True}, {'outcomeKnown': False}])
def test_nested_contradictory_lifecycle_preserves_payloads(workspace, nested):
    service, root, _ = workspace
    (root / 'setup.exe').write_bytes(b'preserve')
    result = service.finish(dict(SUCCESS, native_receipt=nested), verified=True, quiescent=True)
    assert result['removed_bytes'] == 0 and (root / 'setup.exe').exists()


@pytest.mark.skipif(os.name != 'nt', reason='Actual Windows sharing/handle guards.')
def test_open_payload_is_preserved_by_exclusive_delete_handle(workspace):
    service, root, _ = workspace
    path = root / 'setup.exe'
    path.write_bytes(b'open payload')
    with path.open('rb'):
        result = finish(service)
    assert result['removed_bytes'] == 0 and result['status'] == 'partial'
    assert path.read_bytes() == b'open payload'


@pytest.mark.skipif(os.name != 'nt', reason='Actual Windows directory pin prevents ancestor rename.')
def test_directory_pin_blocks_payload_root_swap_during_leaf_deletion(workspace, monkeypatch):
    service, root, _ = workspace
    (root / 'setup.exe').write_bytes(b'owned')
    delete = module.Cleaner._delete_exact
    attempted = []
    def attempt_swap(record):
        with pytest.raises(OSError):
            root.rename(root.with_name('renamed-by-race'))
        attempted.append(True)
        delete(record)
    monkeypatch.setattr(module.Cleaner, '_delete_exact', attempt_swap)
    result = finish(service)
    assert attempted == [True] and result['removed_bytes'] == 5


def test_link_to_external_payload_is_never_followed(workspace):
    service, root, data = workspace
    external = data.parent / 'external'
    external.mkdir()
    (external / 'personal.exe').write_bytes(b'personal')
    try:
        (root / 'linked').symlink_to(external, target_is_directory=True)
    except OSError:
        pytest.skip('Symlink creation is unavailable for this fixture token.')
    result = finish(service)
    assert result['removed_bytes'] == 0 and (external / 'personal.exe').read_bytes() == b'personal'


def test_noop_delete_cannot_report_removed_bytes(workspace, monkeypatch):
    service, root, _ = workspace
    path = root / 'setup.exe'
    path.write_bytes(b'not removed')
    monkeypatch.setattr(module.Cleaner, '_delete_exact', lambda record: None)
    result = finish(service)
    assert result['status'] == 'partial' and result['removed_files'] == result['removed_bytes'] == 0
    assert path.read_bytes() == b'not removed'


def test_reparse_metadata_is_preserved_without_following(workspace, monkeypatch):
    from types import SimpleNamespace
    service, root, _ = workspace
    path = root / 'setup.exe'
    path.write_bytes(b'preserve reparse candidate')
    lstat = Path.lstat
    def flagged(candidate, *args, **kwargs):
        info = lstat(candidate, *args, **kwargs)
        if candidate == path:
            return SimpleNamespace(st_mode=info.st_mode, st_nlink=info.st_nlink,
                st_file_attributes=module.REPARSE, st_dev=info.st_dev)
        return info
    monkeypatch.setattr(Path, 'lstat', flagged)
    result = finish(service)
    assert result['removed_bytes'] == 0 and path.read_bytes() == b'preserve reparse candidate'
