"""Inbox recovery fixtures never inspect, copy or install actual host drivers."""
import copy
import ctypes
import hashlib
import os
from pathlib import Path
import threading
import time
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from optimus import inbox_driver_backup as module


@pytest.fixture
def package(tmp_path):
    windows = tmp_path / 'Windows'
    system = windows / 'System32'
    store = system / 'DriverStore' / 'FileRepository' / 'monitor.inf_fixture'
    store.mkdir(parents=True)
    published = windows / 'INF' / 'monitor.inf'
    published.parent.mkdir()
    published.write_bytes(b'[Version]\n; Microsoft inbox fixture only')
    inf = store / 'monitor.inf'
    inf.write_bytes(published.read_bytes())
    (store / 'monitor.sys').write_bytes(b'Never execute this fixture')
    catalog = store / 'monitor.cat'
    catalog.write_bytes(b'Fixture catalog; native trust mocked')
    destination = tmp_path / 'private-copy'
    destination.mkdir()
    description = {'kind': 'inbox-driver-store-snapshot', 'published_inf': str(published),
        'store_inf': str(inf), 'package_directory': str(store), 'catalog': str(catalog),
        'digital_signer': 'Microsoft Windows', 'digital_signer_version': 'fixture',
        'signer_score': module._INBOX_SCORE, 'catalog_trust_verified': True}
    adapter = SimpleNamespace(describe=Mock(side_effect=lambda _: copy.deepcopy(description)))
    disk = Mock(return_value=SimpleNamespace(free=16 * 1024 ** 3))
    service = module.InboxDriverSnapshot(adapter, disk_usage=disk)
    return SimpleNamespace(windows=windows, system=system, store=store, published=published,
        inf=inf, catalog=catalog, destination=destination, description=description,
        adapter=adapter, disk=disk, service=service)


def test_constructors_do_not_load_native_libraries_or_query_the_host(monkeypatch):
    loader = Mock(side_effect=AssertionError('No native calls in constructors'))
    monkeypatch.setattr(module.ctypes, 'WinDLL', loader, raising=False)
    assert module.WindowsInboxCatalog().api is None
    assert module.InboxDriverSnapshot().adapter is None
    loader.assert_not_called()


def test_complete_package_and_catalog_are_copied_and_hashed_with_honest_kind(package):
    p = package
    result = p.service.snapshot('monitor.inf', p.destination)
    assert result['verified'] and result['kind'] == 'inbox-driver-store-snapshot'
    assert result['oem_export'] is False and result['automatic_restore_supported'] is False
    assert 'not guaranteed' in result['restore_note'] and 'No protected store file was changed' in result['restore_note']
    assert len(result['files']) == 5 and p.adapter.describe.call_count == 2
    for row in result['files']:
        source, copied = Path(row['source']), p.destination / row['path']
        assert source.read_bytes() == copied.read_bytes()
        assert copied.stat().st_nlink == 1
        assert hashlib.sha256(copied.read_bytes()).hexdigest() == row['sha256']
    assert p.service.verify(result) is True


def test_registered_catalog_outside_package_is_preserved_separately(package):
    p = package
    catalog = p.system / 'CatRoot' / 'fixture' / 'component.cat'
    catalog.parent.mkdir(parents=True)
    catalog.write_bytes(b'registered signed Windows catalog fixture')
    p.description['catalog'] = str(catalog)
    result = p.service.snapshot('monitor.inf', p.destination)
    assert (p.destination / 'trusted-catalog.cat').read_bytes() == catalog.read_bytes()
    assert result['native_binding']['catalog'] == str(catalog)


@pytest.mark.parametrize('change', [
    lambda p: p.description.update(catalog_trust_verified=False),
    lambda p: p.description.update(signer_score=0x0F000000),
    lambda p: p.description.update(kind='oem-pnputil-export'),
])
def test_unverified_or_wrong_kind_never_creates_any_copy(package, change):
    change(package)
    with pytest.raises(ValueError, match='incomplete'):
        package.service.snapshot('monitor.inf', package.destination)
    assert not list(package.destination.iterdir())


def test_low_destination_space_refuses_before_copy(package):
    p = package
    p.disk.return_value.free = module._RESERVE_BYTES
    with pytest.raises(OSError, match='free space'):
        p.service.snapshot('monitor.inf', p.destination)
    assert not list(p.destination.iterdir())


def test_larger_configured_destination_buffer_is_preserved(package):
    p = package
    p.service.reserve_bytes = 8 * 1024 ** 3
    p.disk.return_value.free = p.service.reserve_bytes
    with pytest.raises(OSError, match='reserved buffer'):
        p.service.snapshot('monitor.inf', p.destination)
    assert not list(p.destination.iterdir())


@pytest.mark.parametrize('reserve', [0, 128 * 1024 ** 2, True, None, 32 * 1024 ** 4])
def test_unknown_or_insufficient_buffer_cannot_be_configured(reserve):
    with pytest.raises(ValueError, match='at least 2 GiB'):
        module.InboxDriverSnapshot(reserve_bytes=reserve)


def test_buffer_is_rechecked_before_each_file(package):
    p = package
    p.disk.side_effect = [SimpleNamespace(free=16 * 1024 ** 3), SimpleNamespace(free=module._RESERVE_BYTES)]
    with pytest.raises(OSError, match='reserved buffer'):
        p.service.snapshot('monitor.inf', p.destination)
    assert not list(p.destination.iterdir())


def test_exact_reserve_plus_remaining_copy_bytes_succeeds(package):
    p = package
    files = p.service._files(p.description, None, time.monotonic() + 30)
    total = sum(row['size'] for row in files)
    def remaining_space(_):
        copied = sum(path.stat().st_size for path in p.destination.rglob('*') if path.is_file())
        return SimpleNamespace(free=module._RESERVE_BYTES + total - copied)
    p.disk.side_effect = remaining_space
    result = p.service.snapshot('monitor.inf', p.destination)
    assert result['verified'] and remaining_space(None).free == module._RESERVE_BYTES


@pytest.mark.parametrize('kind', ['source', 'catalog', 'binding', 'private_file', 'extra_file', 'hardlink'])
def test_final_revalidation_rejects_every_source_or_private_change(package, kind, tmp_path):
    p = package
    result = p.service.snapshot('monitor.inf', p.destination)
    if kind == 'source':
        (p.store / 'monitor.sys').write_bytes(b'changed source')
    elif kind == 'catalog':
        p.catalog.write_bytes(b'changed catalog')
    elif kind == 'binding':
        p.description['digital_signer_version'] = 'changed native binding'
    elif kind == 'private_file':
        (p.destination / 'package' / 'monitor.sys').write_bytes(b'changed private copy')
    elif kind == 'extra_file':
        (p.destination / 'unexpected.dll').write_bytes(b'extra')
    else:
        target = p.destination / 'package' / 'monitor.sys'
        os.link(target, tmp_path / 'linked-private.sys')
    with pytest.raises(ValueError):
        p.service.verify(result)


def test_source_hardlinks_are_read_only_but_private_copies_are_single_link(package, tmp_path):
    p = package
    os.link(p.store / 'monitor.sys', tmp_path / 'winsxs-fixture.sys')
    assert (p.store / 'monitor.sys').stat().st_nlink == 2
    result = p.service.snapshot('monitor.inf', p.destination)
    assert result['verified'] and (p.destination / 'package' / 'monitor.sys').stat().st_nlink == 1


@pytest.mark.parametrize('kind', ['file', 'directory'])
def test_indirect_source_is_not_followed(package, tmp_path, kind):
    p = package
    try:
        if kind == 'file':
            (p.store / 'indirect.sys').symlink_to(p.store / 'monitor.sys')
        else:
            (p.store / 'indirect').symlink_to(tmp_path, target_is_directory=True)
    except OSError:
        pytest.skip('Fixture account cannot create symbolic links.')
    with pytest.raises(ValueError, match='Indirect'):
        p.service.snapshot('monitor.inf', p.destination)
    assert not list(p.destination.iterdir())


def test_incomplete_directory_walk_cannot_claim_complete_copy(package, monkeypatch):
    def incomplete(*args, **kwargs):
        kwargs['onerror'](PermissionError('fixture unreadable package subdirectory'))
        return iter(())
    monkeypatch.setattr(module.os, 'walk', incomplete)
    with pytest.raises(PermissionError, match='unreadable'):
        package.service.snapshot('monitor.inf', package.destination)


@pytest.mark.parametrize('limit', ['files', 'bytes'])
def test_full_package_bounds_refuse_before_any_copy(package, monkeypatch, limit):
    monkeypatch.setattr(module, '_MAX_FILES' if limit == 'files' else '_MAX_BYTES', 1)
    with pytest.raises(ValueError, match='bound'):
        package.service.snapshot('monitor.inf', package.destination)
    assert not list(package.destination.iterdir())


def test_cancelled_and_expired_requests_do_not_even_resolve_native_package(package):
    event = threading.Event(); event.set()
    for kwargs in ({'cancel_event': event}, {'deadline': time.monotonic() - 1}):
        with pytest.raises(RuntimeError, match='cancelled'):
            package.service.snapshot('monitor.inf', package.destination, **kwargs)
    package.adapter.describe.assert_not_called()
    assert not list(package.destination.iterdir())


def test_cancellation_during_private_readback_stops_before_next_file(package, monkeypatch):
    from optimus import driver_backups
    event = threading.Event()
    original = driver_backups._hash_file
    calls = []
    def cancel_after_copy(path, **kwargs):
        result = original(path, **kwargs)
        calls.append(path)
        event.set()
        return result
    monkeypatch.setattr(driver_backups, '_hash_file', cancel_after_copy)
    with pytest.raises(RuntimeError, match='cancelled'):
        package.service.snapshot('monitor.inf', package.destination, cancel_event=event)
    assert len(calls) == 1


def native_fixture(package, monkeypatch, *, forward=None, reverse=None, score=None, signer=None, catalog=None, success=True):
    p = package
    monkeypatch.setattr(module.sys, 'platform', 'win32')
    native = module.WindowsInboxCatalog()
    def resolve_store(source, alternate, locale, buffer, capacity, required):
        assert source == str(p.published) and alternate is None and locale is None
        buffer.value = str(forward or p.inf)
        required._obj.value = len(buffer.value) + 1
        return 1
    def resolve_published(source, buffer, capacity, required):
        assert source == str(forward or p.inf)
        buffer.value = str(reverse or p.published)
        required._obj.value = len(buffer.value) + 1
        return 1
    def verify(source, alternate, info):
        assert source == str(forward or p.inf) and alternate is None
        assert info._obj.cbSize == ctypes.sizeof(module._SignerInfo)
        info._obj.CatalogFile = str(catalog or p.catalog)
        info._obj.DigitalSigner = signer or 'Microsoft Windows'
        info._obj.DigitalSignerVersion = 'fixture'
        info._obj.SignerScore = module._INBOX_SCORE if score is None else score
        return 1 if success else 0
    native.api = SimpleNamespace(SetupGetInfDriverStoreLocationW=resolve_store,
        SetupGetInfPublishedNameW=resolve_published, SetupVerifyInfFileW=verify)
    native.system, native.windows = p.system, p.windows
    monkeypatch.setattr(module.ctypes, 'get_last_error', lambda: 5, raising=False)
    return native


def test_native_forward_reverse_and_inbox_catalog_signature_are_required(package, monkeypatch):
    native = native_fixture(package, monkeypatch)
    proof = native.describe('monitor.inf')
    assert proof['catalog_trust_verified'] is True and proof['signer_score'] == module._INBOX_SCORE
    assert proof['store_inf'] == str(package.inf) and proof['published_inf'] == str(package.published)


def test_native_reverse_exact_basename_matches_actual_windows_api_return(package, monkeypatch):
    native = native_fixture(package, monkeypatch, reverse=Path('monitor.inf'))
    assert native.describe('monitor.inf')['published_inf'] == str(package.published)


@pytest.mark.parametrize('reverse', ['different.inf', 'subdirectory/monitor.inf', 'C:monitor.inf', '../monitor.inf'])
def test_native_reverse_relative_or_different_name_is_not_guessed(package, monkeypatch, reverse):
    native = native_fixture(package, monkeypatch, reverse=Path(reverse))
    with pytest.raises(ValueError, match='reverse|binding'):
        native.describe('monitor.inf')


@pytest.mark.parametrize('changes', [
    {'reverse': 'wrong.inf'}, {'forward': 'other-driver-store.inf'},
    {'score': 0x0F000000}, {'signer': 'Untrusted fixture signer'}, {'catalog': 'unknown.cat'}, {'success': False},
])
def test_native_bad_binding_or_catalog_is_refused(package, monkeypatch, changes):
    values = dict(changes)
    for key in ('reverse', 'forward', 'catalog'):
        if key in values:
            values[key] = package.windows / values[key]
    native = native_fixture(package, monkeypatch, **values)
    with pytest.raises((ValueError, OSError)):
        native.describe('monitor.inf')


@pytest.mark.parametrize('inf', ['oem17.inf', '../monitor.inf', r'other\monitor.inf', '', '*', 'monitor.sys'])
def test_native_invalid_inf_is_rejected_before_dll_load(inf, monkeypatch):
    native = module.WindowsInboxCatalog()
    native._load = Mock(side_effect=AssertionError('No native call'))
    with pytest.raises(ValueError):
        native.describe(inf)
    native._load.assert_not_called()


def test_native_adapter_loads_only_absolute_system_setupapi_and_correct_signatures(package, monkeypatch):
    p = package
    (p.system / 'setupapi.dll').write_bytes(b'fixture never loaded')
    def root_function(root):
        def call(buffer, size):
            buffer.value = str(root)
            return len(str(root))
        return Mock(side_effect=call)
    kernel = SimpleNamespace(GetSystemDirectoryW=root_function(p.system), GetWindowsDirectoryW=root_function(p.windows))
    setup = SimpleNamespace(**{name: Mock() for name in ('SetupGetInfDriverStoreLocationW', 'SetupGetInfPublishedNameW', 'SetupVerifyInfFileW')})
    loader = Mock(side_effect=[kernel, setup])
    monkeypatch.setattr(module.ctypes, 'WinDLL', loader, raising=False)
    monkeypatch.setattr(module.sys, 'platform', 'win32')
    native = module.WindowsInboxCatalog(); native._load()
    assert loader.call_args_list[1].args == (str(p.system / 'setupapi.dll'),)
    assert loader.call_args_list[1].kwargs == {'use_last_error': True}
    assert len(setup.SetupGetInfDriverStoreLocationW.argtypes) == 6
    assert len(setup.SetupGetInfPublishedNameW.argtypes) == 4
    assert len(setup.SetupVerifyInfFileW.argtypes) == 3
    assert setup.SetupVerifyInfFileW.restype is ctypes.c_int
    if ctypes.sizeof(ctypes.c_wchar) == 2:
        assert ctypes.sizeof(module._SignerInfo) == 1568
