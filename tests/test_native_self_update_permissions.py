"""Private updater ACL policy; native calls affect only test temporary paths."""
import ctypes
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


def binary_policy(sid=SID):
    return {'dacl_protected': True, 'aces': [
        {'type': 0, 'flags': 3, 'mask': 0x1f01ff, 'sid': trustee}
        for trustee in (sid, 'S-1-5-18', 'S-1-5-32-544')]}


@pytest.mark.parametrize('other_sid', ['S-1-5-21-444-555-666-500', 'S-1-5-21-444-555-666-1001'])
def test_binary_wrong_domain_trustee_fails_creation_readback_and_is_never_adopted(tmp_path, other_sid):
    native = Native()
    native.read = lambda path: dict(binary_policy(other_sid), owner=SID, sddl=module._policy(other_sid))
    with pytest.raises(OSError, match='readback'):
        module.create_private_directory(tmp_path / 'new-private', SID, native=native)
    existing = tmp_path / 'existing-private'
    existing.mkdir()
    with pytest.raises(PermissionError, match='preserved'):
        module.repair_legacy_private_directory(existing, SID, authenticated_owner=SID, native=native)
    assert not native.replaced


@pytest.mark.parametrize('change', ['unprotected', 'fourth_ace', 'duplicate', 'deny',
    'inherited', 'read_only', 'bool_type', 'sddl_alias', 'unknown_field'])
def test_binary_policy_rejects_any_change_to_exact_private_allow_policy(change):
    policy = binary_policy()
    assert module._principals(policy) == frozenset((SID, 'SY', 'BA'))
    if change == 'unprotected': policy['dacl_protected'] = False
    elif change == 'fourth_ace': policy['aces'].append(dict(policy['aces'][0]))
    elif change == 'duplicate': policy['aces'][0]['sid'] = 'S-1-5-18'
    elif change == 'deny': policy['aces'][0]['type'] = 1
    elif change == 'inherited': policy['aces'][0]['flags'] |= 0x10
    elif change == 'read_only': policy['aces'][0]['mask'] = 0x120089
    elif change == 'bool_type': policy['aces'][0]['type'] = False
    elif change == 'sddl_alias': policy['aces'][0]['sid'] = 'LA'
    elif change == 'unknown_field': policy['aces'][0]['extra'] = True
    assert module._principals(policy) is None


def native_descriptor_policy(native, sddl):
    descriptor = native._descriptor(sddl)
    text = native.w.LPWSTR()
    try:
        present, defaulted, dacl = native.w.BOOL(), native.w.BOOL(), ctypes.c_void_p()
        assert native.api.GetSecurityDescriptorDacl(descriptor, ctypes.byref(present),
            ctypes.byref(dacl), ctypes.byref(defaulted)) and present and dacl
        assert native.api.ConvertSecurityDescriptorToStringSecurityDescriptorW(descriptor, 1,
            4, ctypes.byref(text), None)
        return dict(native._binary_dacl(descriptor, dacl), sddl=text.value)
    finally:
        if text: native.kernel.LocalFree(text)
        native.kernel.LocalFree(descriptor)


@pytest.mark.skipif(os.name != 'nt', reason='Windows native in-memory ACL fixture')
def test_native_local_administrator_alias_reads_back_as_exact_numeric_rid500_sid():
    native = module.WindowsDirectoryACL()
    policy = native_descriptor_policy(native, module._policy('LA'))
    local_sid = policy['aces'][0]['sid']
    assert local_sid.startswith('S-1-5-21-') and local_sid.endswith('-500')
    assert ';;;LA)' in policy['sddl']
    assert module._principals(policy) == frozenset((local_sid, 'SY', 'BA'))
    assert native_descriptor_policy(native, module._policy(local_sid)) == policy


@pytest.mark.skipif(os.name != 'nt', reason='Windows native in-memory ACL fixture')
def test_native_same_rid_in_another_domain_never_matches_local_administrator():
    native = module.WindowsDirectoryACL()
    local = native_descriptor_policy(native, module._policy('LA'))
    local_sid = local['aces'][0]['sid']
    pieces = local_sid.split('-')
    pieces[-2] = str((int(pieces[-2]) + 1) % 0x100000000)
    other_sid = '-'.join(pieces)
    other = native_descriptor_policy(native, module._policy(other_sid))
    assert other['aces'][0]['sid'] == other_sid
    assert ';;;LA)' not in other['sddl']
    assert module._principals(other) == frozenset((other_sid, 'SY', 'BA'))
    assert module._principals(other) != frozenset((local_sid, 'SY', 'BA'))


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
