"""Only exact proven Ollama runtimes can release an installer observation slot."""
import copy
import hashlib
import subprocess
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from optimus import resident_update_completion as resident
from optimus.winget_console import _digest


@pytest.fixture
def case(monkeypatch):
    arguments = [r'C:\WindowsApps\winget.exe', 'upgrade', '--id', 'Ollama.Ollama', '--exact',
        '--source', 'winget', '--version', '2.0', '--silent', '--disable-interactivity']
    raw = b'''2026-01-02 03:05:00.100   -- File entry --
2026-01-02 03:05:00.101   Dest filename: D:\\Ollama\\ollama app.exe
2026-01-02 03:05:00.102   Successfully installed the file.
2026-01-02 03:05:00.103   -- File entry --
2026-01-02 03:05:00.104   Dest filename: D:\\Ollama\\ollama.exe
2026-01-02 03:05:00.105   Successfully installed the file.
2026-01-02 03:05:00.106   -- Run entry --
2026-01-02 03:05:00.107   Parameters: /C set PATH=D:\\Ollama;%PATH% & "D:\\Ollama\\ollama app.exe"
'''
    evidence = {'native_pid': 1234, 'native_arguments': arguments[1:], 'application_name': 'Ollama version 2.0',
        'publisher': 'Ollama', 'version': '2.0', 'started_at': 1000, 'finished_at': 1100,
        'installer_log': r'D:\logs\Ollama.log', 'winget_log_sha256': 'a' * 64,
        'installer_log_sha256': 'b' * 64}
    completed = Mock(return_value=evidence)
    monkeypatch.setattr(resident, '_completed_evidence', completed)
    monkeypatch.setattr(resident, '_ordinary_data_path', lambda *_args, **_kwargs: None)
    monkeypatch.setattr(resident, '_image_digest', lambda _path: hashlib.sha256(b'MZfixture').hexdigest())
    monkeypatch.setattr(resident, '_read', lambda path, _limit: raw if str(path).endswith('Ollama.log') else b'MZfixture')
    apps = [{'name': evidence['application_name'], 'publisher': 'Ollama', 'version': '2.0', 'location': r'D:\Ollama'}]
    rows = [{'pid': 456, 'path': r'D:\Ollama\ollama app.exe', 'created_at': 1090}]
    return SimpleNamespace(arguments=arguments, raw=raw, evidence=evidence, apps=apps, rows=rows, completed=completed)


def verify(case):
    return resident.verify_resident_completion(case.arguments, 1234, 1000, case.rows,
        folder='fixture-log-folder', applications=case.apps, now=1200)


def test_complete_known_native_command_and_installed_runtime_yield_distinct_proof(case):
    proof = verify(case)
    assert proof['kind'] == resident.KIND and proof['target_pid'] == 1234
    assert proof['processes'][0]['pid'] == 456
    assert proof['processes'][0]['sha256'] == hashlib.sha256(b'MZfixture').hexdigest()
    assert 'quiescent' not in proof  # Resident completion never invents console EOF.


@pytest.mark.parametrize('change', [
    lambda c: c.arguments.__setitem__(3, 'Other.Package'),
    lambda c: c.arguments.__setitem__(0, r'C:\fixtures\choco.exe'),
    lambda c: c.evidence.update(native_pid=4321),
    lambda c: c.evidence.update(native_arguments=['different', 'command']),
    lambda c: c.apps[0].update(version='1.0'),
    lambda c: c.apps[0].update(location=r'D:\Other'),
    lambda c: c.apps.append(copy.deepcopy(c.apps[0])),
    lambda c: c.rows[0].update(path=r'D:\Temp\Setup.exe'),
    lambda c: c.rows[0].update(path=r'D:\Ollama\unins000.exe'),
    lambda c: c.rows[0].update(created_at=900),
    lambda c: c.rows[0].update(pid=1234),
    lambda c: c.rows.append(copy.deepcopy(c.rows[0])),
    lambda c: c.rows.append({'pid': 789, 'path': r'D:\Ollama\setup.exe', 'created_at': 1095}),
])
def test_unknown_descendant_or_partial_native_match_never_hands_off(case, change):
    # The captured native arguments are a distinct provider receipt, not the
    # same mutable object as the caller's command vector.
    case.evidence['native_arguments'] = list(case.evidence['native_arguments'])
    change(case)
    with pytest.raises(ValueError):
        verify(case)


@pytest.mark.parametrize('old,new', [
    (b'Successfully installed the file.', b'Leaving the existing file.'),
    (b'Parameters: /C set PATH=', b'Parameters: /C unknown-command '),
    (b'Dest filename: D:\\Ollama\\ollama app.exe', b'Dest filename: D:\\Other\\ollama app.exe'),
])
def test_postinstall_launch_and_installed_files_must_be_exact(case, monkeypatch, old, new):
    raw = case.raw.replace(old, new)
    monkeypatch.setattr(resident, '_read', lambda path, _limit: raw if str(path).endswith('Ollama.log') else b'MZfixture')
    with pytest.raises(ValueError):
        verify(case)


def result(case):
    proof = verify(case)
    outcome = subprocess.CompletedProcess(case.arguments, 0, 'native installer succeeded', '')
    outcome.native_completion_verified, outcome.capture_complete = True, False
    outcome.console_receipt = {'schema': 1, 'event': 'resident_handoff', 'nonce': 'c' * 32,
        'supervisor_pid': 2345, 'resident_handoff_verified': True, 'quiescent': False,
        'output_eof': False, 'job_verified': True, 'console_client_count': 1,
        'supervisor_exit_code': None, 'target_exit_code': 0, 'target_pid': 1234,
        'job_active_processes': 1, 'argv_sha256': _digest(case.arguments), 'resident_proof': proof}
    return outcome


def test_maintenance_accepts_only_distinct_authenticated_transport_result(case):
    assert resident.verified_resident_handoff(result(case), case.arguments)
    assert not resident.verified_resident_handoff({'console_receipt': result(case).console_receipt}, case.arguments)


@pytest.mark.parametrize('field,value', [
    ('schema', True), ('event', 'quiescent'), ('nonce', 'forged'), ('supervisor_pid', True),
    ('quiescent', True), ('output_eof', True), ('job_verified', 1), ('resident_handoff_verified', False),
    ('console_client_count', 2), ('supervisor_exit_code', 0), ('target_exit_code', True),
    ('job_active_processes', 0), ('argv_sha256', '0' * 64),
])
def test_contradictory_or_forged_transport_fields_are_rejected(case, field, value):
    outcome = result(case)
    outcome.console_receipt[field] = value
    assert not resident.verified_resident_handoff(outcome, case.arguments)


def test_native_image_hashing_is_streamed_and_rejects_non_images(tmp_path):
    path = tmp_path / 'fixture.exe'
    raw = b'MZ' + b'fixture' * (300 * 1024)
    path.write_bytes(raw)
    assert resident._image_digest(path) == hashlib.sha256(raw).hexdigest()
    path.write_bytes(b'not-a-native-image')
    with pytest.raises(ValueError):
        resident._image_digest(path)
