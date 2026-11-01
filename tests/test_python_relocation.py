"""Only isolated read probes run natively; all reparse writes use an API fake."""
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import PureWindowsPath
import struct
from types import SimpleNamespace

import pytest

from optimus import python_relocation as repair


SOURCE = r'C:\Users\TestUser\AppData\Local\Programs\Python'
TARGET = r'D:\ApprovedApps\Python'
SID = 'S-1-5-21-1-2-3-1001'
PACKAGE = 'Python.Python.3.14'


def mount_point(target=TARGET):
    text = ('\\??\\' + target).encode('utf-16-le')
    payload = struct.pack('<HHHH', 0, len(text), len(text) + 2, 0) + text + b'\0\0\0\0'
    return struct.pack('<IHH', repair._MOUNT_POINT, len(payload), 0) + payload


def approval(**changes):
    value = dict(package_id=PACKAGE, source=SOURCE, target=TARGET,
                 reparse_sha256=hashlib.sha256(mount_point()).hexdigest())
    value.update(changes)
    return repair.ApprovedPythonRelocation(**value)


class FakeAPI:
    def __init__(self, *, probes=(448, 0), elevated=True):
        self.raw = mount_point()
        self.probes = list(probes)
        self.elevated = elevated
        self.events = []
        self.active = set()
        self.attributes = {}
        self.securities = {}
        self.volumes = {}
        self.lock_error = None
        self.set_error = None
        self.change_identity = False
        self.corrupt_bytes = False

    def current_user_sid(self):
        return SID

    def is_elevated(self):
        return self.elevated

    def filesystem(self, root):
        return self.volumes.get(root, (3, 'NTFS'))

    @contextmanager
    def guard_path(self, path, *, write):
        if path == self.lock_error:
            raise OSError('The approved directory is busy.')
        self.events.append(('open', path, write))
        self.active.add(path)
        try:
            yield path
        finally:
            self.events.append(('close', path))
            self.active.remove(path)

    def info(self, path):
        attrs = repair._DIRECTORY
        if path == SOURCE:
            attrs |= repair._REPARSE
        elif path.lower().endswith('python3.dll'):
            attrs = 0x20
        return dict(attributes=self.attributes.get(path, attrs),
                    identity=(1, path, bool(self.change_identity and self.writes)))

    def security(self, path):
        return self.securities.get(path, dict(owner=SID, aces=[
            dict(type=0, flags=0, sid=sid, mask=0x1F01FF)
            for sid in ('S-1-5-18', 'S-1-5-32-544', SID)]))

    def get_reparse(self, handle):
        assert handle == SOURCE and handle in self.active
        return self.raw

    @property
    def writes(self):
        return [event for event in self.events if event[0] == 'set']

    def set_reparse(self, handle, raw):
        assert self.elevated and handle == SOURCE and handle in self.active
        assert set(repair._path_chain(SOURCE) + repair._path_chain(TARGET)) <= self.active
        assert str(PureWindowsPath(approval().target_dll).parent) in self.active
        assert approval().target_dll in self.active
        self.events.append(('set', handle, raw))
        if self.set_error:
            raise self.set_error
        assert raw is self.raw  # Same bytes read from the same held handle.
        if self.corrupt_bytes:
            self.raw += b'\0\0'

    def probe(self, path):
        self.events.append(('probe', path))
        assert path == approval().dll
        value = self.probes.pop(0)
        return value if isinstance(value, dict) else dict(supported=True, error=value)


def log_file(tmp_path, text, *, name='winget_000_core_JustForMe.log', encoding='utf-16'):
    path = tmp_path / name
    path.write_text(text, encoding=encoding)
    return path


def error_line(dll=None, error=448):
    return 'MSI (s): Error 1326. Error getting file security: ' + (dll or approval().dll) + f' GetLastError: {error}\n'


def test_approval_round_trip_and_derived_dll_paths():
    value = approval()
    assert repair.ApprovedPythonRelocation.from_dict(value.as_dict()) == value
    assert repair.validate_python_relocation_approval(**value.as_dict()) == value
    assert value.dll == SOURCE + r'\Python314\python3.dll'
    assert value.target_dll == TARGET + r'\Python314\python3.dll'
    assert approval(package_id='python.python.3.12').package_id == 'Python.Python.3.12'


@pytest.mark.parametrize('changes', [
    {'package_id': 'Other.App'}, {'package_id': 'Python.Python.3.14.evil'},
    {'package_id': None}, {'source': r'C:\Users\TestUser\Python'},
    {'source': r'C:\Users\TestUser\AppData\Local\Programs\Python\..\Python'},
    {'source': SOURCE + '\\'}, {'target': r'\\server\share\Python'},
    {'target': r'\\?\D:\ApprovedApps\Python'}, {'target': r'C:\ApprovedApps\Python'},
    {'target': r'D:\ApprovedApps\Python:stream'}, {'target': r'D:\AUX\Python'},
    {'target': r'D:\ApprovedApps.\Python'}, {'target': 'D:/ApprovedApps/Python'},
    {'target': r'D:\Python'}, {'target': r'D:\ApprovedApps\Different'},
    {'reparse_sha256': '0' * 63}, {'reparse_sha256': 'A' * 64}, {'reparse_sha256': True},
])
def test_unapproved_or_ambiguous_mapping_rejected(changes):
    with pytest.raises(ValueError):
        approval(**changes)


@pytest.mark.parametrize('value', [None, [], {}, {'package_id': PACKAGE},
    dict(approval().as_dict(), auto_approve=True)])
def test_persisted_approval_has_exact_schema(value):
    with pytest.raises(ValueError):
        repair.ApprovedPythonRelocation.from_dict(value)


@pytest.mark.parametrize('encoding', ['utf-16', 'utf-16-be', 'utf-8-sig'])
def test_only_exact_native_child_error_is_diagnosed(tmp_path, encoding):
    # UTF16-BE without a BOM intentionally remains unsupported.
    path = log_file(tmp_path, error_line() * 2, encoding=encoding)
    result = repair.diagnose_python_relocation_failure(PACKAGE, path, owned_log_root=tmp_path)
    if encoding == 'utf-16-be':
        assert result is None
    else:
        assert result['source'] == SOURCE and result['dll'] == approval().dll
        assert result['error'] == 1326 and result['win32_error'] == 448
        assert result['log_sha256'] == hashlib.sha256(path.read_bytes()).hexdigest()


@pytest.mark.parametrize('text', ['Error 1326', 'GetLastError: 448',
    'Error 1326.\nError getting file security: ' + approval().dll + ' GetLastError: 448',
    error_line(error=5), error_line(dll=SOURCE + r'\Python312\python3.dll'),
    error_line(dll=SOURCE + r'\Python314\python.exe'),
    error_line() + error_line(dll=r'C:\Other\Python314\python3.dll'),
    error_line(dll=r'\\server\share\Python314\python3.dll')])
def test_unrelated_or_ambiguous_log_text_cannot_authorize_repair(tmp_path, text):
    assert repair.diagnose_python_relocation_failure(PACKAGE, log_file(tmp_path, text), owned_log_root=tmp_path) is None


def test_logs_outside_authenticated_attempt_and_wrong_names_are_rejected(tmp_path):
    root = tmp_path / 'owned'
    root.mkdir()
    outside = log_file(tmp_path, error_line())
    assert repair.diagnose_python_relocation_failure(PACKAGE, outside, owned_log_root=root) is None
    assert repair.diagnose_python_relocation_failure('Other.App', outside, owned_log_root=tmp_path) is None
    wrong = log_file(root, error_line(), name='winget.log')
    assert repair.diagnose_python_relocation_failure(PACKAGE, wrong, owned_log_root=root) is None


def test_oversized_hardlinked_or_changing_logs_are_rejected(tmp_path, monkeypatch):
    path = log_file(tmp_path, error_line())
    monkeypatch.setattr(repair, '_MAX_LOG', 10)
    assert repair.diagnose_python_relocation_failure(PACKAGE, path, owned_log_root=tmp_path) is None
    monkeypatch.setattr(repair, '_MAX_LOG', 4 * 1024**2)
    try:
        os.link(path, tmp_path / 'copy')
    except OSError:
        pytest.skip('Hard links unavailable on this test volume.')
    assert repair.diagnose_python_relocation_failure(PACKAGE, path, owned_log_root=tmp_path) is None


@pytest.mark.parametrize('raw', [b'', b'0' * 16, mount_point()[:-2],
    struct.pack('<I', 0xA000000C) + mount_point()[4:],
    mount_point()[:6] + b'\1\0' + mount_point()[8:],
    mount_point()[:8] + b'\1\0' + mount_point()[10:]])
def test_malformed_or_symbolic_link_buffers_are_rejected(raw):
    with pytest.raises((ValueError, UnicodeError)):
        repair._parse_mount_point(raw)


def test_preflight_reproduces_exact_native_failure_without_write_or_elevation():
    api = FakeAPI(elevated=False, probes=(448,))
    result = repair.preflight_python_relocation(approval(), api=api)
    assert result['status'] == 'ready' and result['probe_error'] == 448
    assert result['mutated'] is False and not api.writes and not api.active
    assert all(not event[2] for event in api.events if event[0] == 'open')


def test_repair_holds_all_paths_and_writes_identical_bytes_on_same_handle():
    api = FakeAPI()
    result = repair.repair_python_relocation(approval(), api=api)
    assert result['status'] == 'repaired' and result['mutated'] is True
    assert result['probe_error'] == 0 and len(api.writes) == 1 and not api.active
    assert [event[2] for event in api.events if event[0] == 'open'].count(True) == 1
    assert [event[0] for event in api.events].count('probe') == 2


def test_true_administrator_token_is_required_before_any_native_path_write():
    api = FakeAPI(elevated=False)
    result = repair.repair_python_relocation(approval(), api=api)
    assert result['code'] == 'administrator_required'
    assert api.events == []


@pytest.mark.parametrize('function', [repair.preflight_python_relocation, repair.repair_python_relocation])
def test_already_trusted_is_a_verified_noop(function):
    api = FakeAPI(probes=(0,))
    result = function(approval(), api=api)
    assert result['status'] == 'already_trusted' and not result['mutated'] and not api.writes


@pytest.mark.parametrize('probe', [5, 2, 32, {'supported': False, 'error': 448},
    {'supported': 'yes', 'error': 448}, {'supported': True, 'error': True},
    {'supported': True, 'error': None}])
def test_different_or_unavailable_native_probe_never_mutates(probe):
    api = FakeAPI(probes=(probe,))
    result = repair.repair_python_relocation(approval(), api=api)
    assert result['status'] == 'review_required' and not api.writes


@pytest.mark.parametrize('after', [448, 5, {'supported': False, 'error': 0},
    {'supported': True, 'error': False}, {'supported': True, 'error': 0.0}])
def test_same_buffer_write_does_not_claim_success_without_strict_post_probe(after):
    api = FakeAPI(probes=(448, after))
    result = repair.repair_python_relocation(approval(), api=api)
    assert result['status'] == 'failed' and result['code'] == 'repair_not_verified'
    assert result['mutated'] is True and len(api.writes) == 1


@pytest.mark.parametrize('path', [r'C:\Users', r'D:\ApprovedApps', TARGET,
    str(PureWindowsPath(approval().target_dll).parent), approval().target_dll])
def test_additional_reparse_point_anywhere_in_mapping_is_rejected(path):
    api = FakeAPI()
    api.attributes[path] = repair._REPARSE | (0 if path.endswith('.dll') else repair._DIRECTORY)
    result = repair.repair_python_relocation(approval(), api=api)
    assert result['status'] == 'review_required' and not api.writes and not api.active


@pytest.mark.parametrize('field,value', [('owner', 'S-1-5-21-UNKNOWN'), ('aces', []),
    ('aces', [dict(type=0, flags=0, mask=0x40000000, sid='S-1-1-0')]),
    ('aces', [dict(type=5, flags=0)])])
def test_unknown_owner_null_or_unsafe_dacl_cannot_be_auto_trusted(field, value):
    api = FakeAPI()
    policy = dict(api.security(TARGET))
    policy[field] = value
    api.securities[TARGET] = policy
    result = repair.repair_python_relocation(approval(), api=api)
    assert result['status'] == 'review_required' and not api.writes


def test_inherit_only_acl_entries_do_not_apply_to_this_object():
    api = FakeAPI()
    policy = api.security(TARGET)
    policy['aces'].append(dict(type=0, flags=8, mask=0x1F01FF, sid='S-1-1-0'))
    api.securities[TARGET] = policy
    assert repair.repair_python_relocation(approval(), api=api)['status'] == 'repaired'


@pytest.mark.parametrize('volume', [(4, 'NTFS'), (3, 'ReFS'), (2, 'NTFS'), (3, 'FAT32')])
def test_network_removable_or_non_ntfs_volumes_are_rejected(volume):
    api = FakeAPI()
    api.volumes['D:\\'] = volume
    assert repair.repair_python_relocation(approval(), api=api)['status'] == 'review_required'
    assert not api.writes


def test_busy_ancestor_fails_closed_without_unlinking_or_replacing_anything():
    api = FakeAPI()
    api.lock_error = r'D:\ApprovedApps'
    assert repair.repair_python_relocation(approval(), api=api)['status'] == 'review_required'
    assert not api.writes and not api.active


def test_changed_approved_bytes_or_target_are_rejected():
    for raw in (mount_point(r'D:\Different\Python'), mount_point() + b'\0\0'):
        api = FakeAPI()
        api.raw = raw
        assert repair.repair_python_relocation(approval(), api=api)['status'] == 'review_required'
        assert not api.writes
    api = FakeAPI()
    assert repair.repair_python_relocation(approval(reparse_sha256='0' * 64), api=api)['status'] == 'review_required'
    assert not api.writes


@pytest.mark.parametrize('failure', ['identity', 'bytes', 'native_error'])
def test_unexpected_native_changes_never_report_repair_success(failure):
    api = FakeAPI()
    if failure == 'identity':
        api.change_identity = True
    elif failure == 'bytes':
        api.corrupt_bytes = True
    else:
        api.set_error = OSError('Native operation denied.')
    result = repair.repair_python_relocation(approval(), api=api)
    assert result['status'] != 'repaired' and not api.active and len(api.writes) == 1


@pytest.mark.skipif(os.name != 'nt', reason='Isolated Windows read-only mitigation probe.')
def test_actual_redirection_guard_child_reads_existing_system_file_only():
    target = str(PureWindowsPath(os.environ.get('SystemRoot', r'C:\Windows')) / 'System32/kernel32.dll')
    result = repair.probe_redirection_trust(target)
    assert result == {'supported': True, 'error': 0}


@pytest.mark.skipif(os.name != 'nt', reason='Frozen release uses Windows PowerShell read-only probe.')
def test_actual_frozen_probe_needs_no_external_python(monkeypatch):
    monkeypatch.setattr(repair.sys, 'frozen', True, raising=False)
    target = str(PureWindowsPath(os.environ.get('SystemRoot', r'C:\Windows')) / 'System32/kernel32.dll')
    assert repair.probe_redirection_trust(target) == {'supported': True, 'error': 0}
