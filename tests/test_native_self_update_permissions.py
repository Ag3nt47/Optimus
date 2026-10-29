"""Private updater ACL policy; native calls affect only test temporary paths."""
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from optimus import self_update_permissions as module

SID = 'S-1-5-21-111-222-333-1001'
LEGACY = 'O:BAG:S-1-5-21-111-222-333-513D:P(A;OICI;FA;;;OW)(A;OICI;FA;;;SY)(A;OICI;FA;;;BA)'


class Native:
    def __init__(self, sddl=LEGACY, owner='S-1-5-32-544'):
        self.security = {'owner': owner, 'sddl': sddl}
        self.created, self.replaced = [], []

    def owner_sid(self): return SID

    def create(self, path, sddl):
        path.mkdir()
        self.created.append((path, sddl))
        self.security = {'owner': SID, 'sddl': 'O:' + SID + sddl}

    def read(self, path): return dict(self.security)

    def replace(self, path, sddl, *, expected_identity):
        assert module._identity(path) == expected_identity
        self.replaced.append((path, sddl))
        self.security = {'owner': self.security['owner'], 'sddl': sddl}


def test_new_private_directory_names_token_user_instead_of_owner_rights(tmp_path):
    native = Native()
    path = tmp_path / 'workspace'
    assert module.create_private_directory(path, 'sid:' + SID, native=native)['status'] == 'created'
    assert len(native.created) == 1
    assert SID in native.created[0][1] and ';;;OW)' not in native.created[0][1]
    assert module._principals(native.security) == frozenset((SID, 'SY', 'BA'))


def test_exclusive_private_creation_preserves_an_existing_directory(tmp_path):
    path = tmp_path / 'workspace'
    path.mkdir()
    sentinel = path / 'record.json'
    sentinel.write_bytes(b'private-record')
    native = Native()
    with pytest.raises(FileExistsError): module.create_private_directory(path, 'sid:' + SID, native=native)
    assert sentinel.read_bytes() == b'private-record' and not native.replaced


@pytest.mark.parametrize('owner', ['sid:S-1-5-21-999-888-777-1001', 'BA', None, 1, 'sid:S-1-5-21-111;D:'])
def test_creation_requires_current_token_identity_before_native_write(tmp_path, owner):
    native = Native()
    with pytest.raises(ValueError): module.create_private_directory(tmp_path / 'workspace', owner, native=native)
    assert not native.created and not native.replaced


def test_exact_legacy_admin_owner_policy_repair_preserves_files_and_owner(tmp_path):
    path = tmp_path / 'workspace'
    path.mkdir()
    (path / 'owner.key').write_bytes(b'fixture-dpapi-envelope')
    (path / 'attempt').mkdir()
    (path / 'attempt' / 'revoked.json').write_bytes(b'unchanged-private-evidence')
    native = Native()
    result = module.repair_legacy_private_directory(path, 'sid:' + SID,
        authenticated_owner='sid:' + SID, native=native)
    assert result == {'status': 'repaired', 'changed': True, 'recursive_reset': False}
    assert len(native.replaced) == 1 and native.replaced[0][0] == path
    assert native.security['owner'] == 'S-1-5-32-544'
    assert (path / 'owner.key').read_bytes() == b'fixture-dpapi-envelope'
    assert (path / 'attempt' / 'revoked.json').read_bytes() == b'unchanged-private-evidence'


@pytest.mark.parametrize('authenticated', [None, 'sid:S-1-5-21-1-2-3-1002', 'sid:BA'])
def test_repair_requires_matching_authenticated_key_owner(tmp_path, authenticated):
    path = tmp_path / 'workspace'
    path.mkdir()
    native = Native()
    with pytest.raises(ValueError): module.repair_legacy_private_directory(path, 'sid:' + SID,
        authenticated_owner=authenticated, native=native)
    assert not native.replaced


@pytest.mark.parametrize('patch', [
    {'owner': 'S-1-5-21-4-5-6-1001'},
    {'sddl': LEGACY.replace('D:P', 'D:')},
    {'sddl': LEGACY.replace(';;;OW)', ';;;WD)')},
    {'sddl': LEGACY + '(A;OICI;FA;;;WD)'},
    {'sddl': LEGACY.replace('OICI;FA', 'OICI;FR', 1)},
    {'sddl': LEGACY.replace('A;OICI', 'D;OICI', 1)},
    {'sddl': LEGACY.replace('A;OICI', 'A;OICIID', 1)},
])
def test_unknown_owner_or_acl_never_gets_adopted(tmp_path, patch):
    path = tmp_path / 'workspace'
    path.mkdir()
    native = Native()
    native.security.update(patch)
    with pytest.raises(PermissionError): module.repair_legacy_private_directory(path, 'sid:' + SID,
        authenticated_owner='sid:' + SID, native=native)
    assert not native.replaced


def test_already_correct_policy_is_not_rewritten(tmp_path):
    path = tmp_path / 'workspace'
    path.mkdir()
    native = Native('O:BA' + module._policy(SID))
    assert module.repair_legacy_private_directory(path, SID, authenticated_owner=SID, native=native) == {
        'status': 'already_configured', 'changed': False}
    assert not native.replaced


def test_changed_directory_identity_prevents_native_acl_write(tmp_path, monkeypatch):
    path = tmp_path / 'workspace'
    path.mkdir()
    native = Native()
    identities = iter(((1, 1), (1, 2)))
    monkeypatch.setattr(module, '_identity', lambda path: next(identities))
    with pytest.raises(ValueError, match='changed before repair'):
        module.repair_legacy_private_directory(path, SID, authenticated_owner=SID, native=native)
    assert not native.replaced


def test_indirect_directory_is_rejected_before_native_acl_write(tmp_path, monkeypatch):
    path = tmp_path / 'workspace'
    path.mkdir()
    native = Native()
    original = Path.lstat
    def indirect(value, *args, **kwargs):
        info = original(value, *args, **kwargs)
        return SimpleNamespace(st_mode=info.st_mode, st_file_attributes=0x400) if value == path else info
    monkeypatch.setattr(Path, 'lstat', indirect)
    with pytest.raises(ValueError, match='Indirect'):
        module.repair_legacy_private_directory(path, SID, authenticated_owner=SID, native=native)
    assert not native.replaced


@pytest.mark.skipif(os.name != 'nt', reason='Windows native ACL fixture')
def test_native_directory_creation_and_legacy_repair_on_temporary_directory(tmp_path):
    native = module.WindowsDirectoryACL()
    owner = native.owner_sid()
    created = tmp_path / 'new-private'
    module.create_private_directory(created, owner)
    assert module._principals(native.read(created)) == frozenset((owner, 'SY', 'BA'))
    # Simulate only the known old directory policy, preserving TokenUser owner.
    legacy = tmp_path / 'legacy-private'
    legacy.mkdir()
    identity = module._identity(legacy)
    native.replace(legacy, 'D:P(A;OICI;FA;;;OW)(A;OICI;FA;;;SY)(A;OICI;FA;;;BA)', expected_identity=identity)
    marker = legacy / 'unchanged.json'
    marker.write_bytes(b'unchanged-evidence')
    module.repair_legacy_private_directory(legacy, owner, authenticated_owner=owner)
    assert module._principals(native.read(legacy)) == frozenset((owner, 'SY', 'BA'))
    assert marker.read_bytes() == b'unchanged-evidence'
