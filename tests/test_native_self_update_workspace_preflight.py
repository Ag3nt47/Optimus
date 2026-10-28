"""Private-workspace readability is checked before automatic update consent."""
from pathlib import Path
import stat
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from optimus import self_update as module
from test_native_self_update import updater


class Scan:
    def __init__(self, *, empty=False, denied=False):
        self.empty, self.denied, self.calls, self.closed = empty, denied, 0, False
    def __enter__(self): return self
    def __exit__(self, *args): self.closed = True
    def __iter__(self): return self
    def __next__(self):
        self.calls += 1
        if self.calls > 1: raise AssertionError('Only the first directory entry may be inspected.')
        if self.denied: raise PermissionError('Fixture enumeration denied')
        if self.empty: raise StopIteration
        return object()


def forbid_authority_writes(p, monkeypatch):
    p.service._key = Mock(side_effect=AssertionError('Checking may not access the owner key.'))
    p.service._write = Mock(side_effect=AssertionError('Checking may not write authority records.'))
    p.transport.download = Mock(side_effect=AssertionError('Checking may not stage an update.'))
    mkdir = Mock(side_effect=AssertionError('Checking may not create update state.'))
    monkeypatch.setattr(module.os, 'mkdir', mkdir)
    return mkdir


def assert_preserved(p, mkdir):
    assert (p.data / 'preferences.json').read_bytes() == p.raw
    assert (p.installation / 'Optimus.location.json').read_bytes() == p.locator
    assert not p.native.launches
    p.service._key.assert_not_called()
    p.service._write.assert_not_called()
    p.transport.download.assert_not_called()
    mkdir.assert_not_called()


def test_absent_private_workspace_remains_automatic_without_creation(updater, monkeypatch):
    p = updater
    mkdir = forbid_authority_writes(p, monkeypatch)
    scan = Mock(side_effect=AssertionError('An absent workspace must not be enumerated.'))
    monkeypatch.setattr(module.os, 'scandir', scan)
    result = p.service.check()
    assert result['status'] == 'available' and result['automatic_install_supported'] is True
    assert not p.service.directory.exists()
    scan.assert_not_called()
    assert_preserved(p, mkdir)


@pytest.mark.parametrize('empty', [False, True])
def test_existing_readable_workspace_checks_one_entry_and_closes(updater, monkeypatch, empty):
    p = updater
    p.service.directory.mkdir()
    sentinel = p.service.directory / 'preserved.json'
    sentinel.write_bytes(b'preserved fixture record')
    mkdir = forbid_authority_writes(p, monkeypatch)
    entries = Scan(empty=empty)
    scan = Mock(return_value=entries)
    monkeypatch.setattr(module.os, 'scandir', scan)
    result = p.service.check()
    assert result['automatic_install_supported'] is True
    scan.assert_called_once_with(p.service.directory)
    assert entries.calls == 1 and entries.closed
    assert sentinel.read_bytes() == b'preserved fixture record'
    assert_preserved(p, mkdir)


@pytest.mark.parametrize('phase', ['open', 'first_entry'])
def test_inaccessible_private_workspace_offers_manual_route_before_approval(updater, monkeypatch, phase):
    p = updater
    p.service.directory.mkdir()
    sentinel = p.service.directory / 'preserved.json'
    sentinel.write_bytes(b'preserved fixture record')
    mkdir = forbid_authority_writes(p, monkeypatch)
    entries = Scan(denied=True)
    scan = Mock(side_effect=PermissionError('Fixture access denied')) if phase == 'open' else Mock(return_value=entries)
    monkeypatch.setattr(module.os, 'scandir', scan)
    result = p.service.check()
    assert result['status'] == 'available' and result['automatic_install_supported'] is False
    assert 'current Windows privilege context cannot read' in result['reason']
    assert 'same administrator level' in result['reason'] and 'verified manual download' in result['reason']
    assert 'Existing update records are preserved' in result['reason']
    assert 'owner' not in result['reason'] and 'account' not in result['reason']
    scan.assert_called_once_with(p.service.directory)
    if phase == 'first_entry': assert entries.calls == 1 and entries.closed
    assert sentinel.read_bytes() == b'preserved fixture record'
    assert_preserved(p, mkdir)


@pytest.mark.parametrize('kind', ['file', 'reparse'])
def test_invalid_workspace_type_fails_closed_without_enumeration(updater, monkeypatch, kind):
    p = updater
    if kind == 'file':
        p.service.directory.write_bytes(b'preserved fixture file')
    else:
        original = Path.lstat
        def lstat(path, *args, **kwargs):
            return SimpleNamespace(st_mode=stat.S_IFDIR, st_file_attributes=0x400) if path == p.service.directory else original(path, *args, **kwargs)
        monkeypatch.setattr(Path, 'lstat', lstat)
    mkdir = forbid_authority_writes(p, monkeypatch)
    scan = Mock(side_effect=AssertionError('An invalid workspace must not be enumerated.'))
    monkeypatch.setattr(module.os, 'scandir', scan)
    result = p.service.check()
    assert result['status'] == 'available' and result['automatic_install_supported'] is False
    scan.assert_not_called()
    if kind == 'file': assert p.service.directory.read_bytes() == b'preserved fixture file'
    assert_preserved(p, mkdir)
