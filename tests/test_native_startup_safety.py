"""Native startup-write guards use mocked Windows space and native results only."""
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from optimus import startup_safety as safety
from optimus.self_update_launchers import WindowsLauncherAdapter, _STARTUP_PS
from optimus.startup_registration import StartupRegistration


@pytest.mark.parametrize('free', [0, 1, 2147483647, -1, None, True, 2147483648.0])
def test_system_volume_refusal_is_known_and_never_queries_temp_or_destination(monkeypatch, free):
    monkeypatch.setenv('SystemRoot', 'D:/untrusted')
    monkeypatch.setenv('TEMP', 'F:/ample-space')
    monkeypatch.setattr(safety, 'windows_directory', lambda: SimpleNamespace(anchor='C:\\'))
    usage = Mock(return_value=SimpleNamespace(free=free))
    monkeypatch.setattr(safety.shutil, 'disk_usage', usage)
    with pytest.raises(safety.StartupPreflightError) as caught:
        safety.require_windows_startup_headroom()
    usage.assert_called_once_with(Path('C:\\'))
    assert caught.value.outcomeKnown and caught.value.preflight_refused
    assert not caught.value.manifest_written and not caught.value.task_registration_started


def test_exact_two_gib_and_unknown_space_provider(monkeypatch):
    monkeypatch.setattr(safety, 'windows_directory', lambda: SimpleNamespace(anchor='C:\\'))
    monkeypatch.setattr(safety.shutil, 'disk_usage', lambda _: SimpleNamespace(free=2147483648))
    assert safety.require_windows_startup_headroom()['free_bytes'] == 2147483648
    monkeypatch.setattr(safety.shutil, 'disk_usage', Mock(side_effect=OSError('Opaque provider error')))
    with pytest.raises(safety.StartupPreflightError, match='unknown'):
        safety.require_windows_startup_headroom()


def test_initial_refusal_creates_no_lock_manifest_or_native_task(tmp_path):
    def refuse():
        raise safety.StartupPreflightError('Fixture system volume is full.')
    runner = Mock(side_effect=AssertionError('No native calls'))
    service = StartupRegistration(tmp_path / 'ordinary', runner=runner, headroom_checker=refuse)
    adapter = WindowsLauncherAdapter(tmp_path / 'retarget', headroom_checker=refuse)
    adapter._native = runner
    for operation in (lambda: service.set_enabled(True), lambda: adapter.retarget_startup(tmp_path / 'new.exe', {}),
                      lambda: service.rollback({'before_xml': '<owned/>', '_before_manifest': {}}),
                      lambda: service.rollback({'before_xml': None, '_before_manifest': {}})):
        with pytest.raises(safety.StartupPreflightError): operation()
    assert not list(tmp_path.iterdir())
    runner.assert_not_called()


@pytest.mark.parametrize('entry', ['status', 'retarget', 'oversized'])
def test_native_status_uses_trusted_file_and_all_commands_respect_windows_limit(tmp_path, monkeypatch, entry):
    import optimus.self_update_launchers as module
    adapter = WindowsLauncherAdapter(tmp_path, headroom_checker=lambda: None)
    host = tmp_path / 'Windows/System32/WindowsPowerShell/v1.0/powershell.exe'
    monkeypatch.setattr(module, '_system_powershell', lambda: host)
    native = Mock(return_value=SimpleNamespace(stdout='{"ok":true}', returncode=0))
    monkeypatch.setattr(module.subprocess, 'run', native)
    if entry == 'status':
        adapter._startup_status({'operation': 'status', 'manifest': None})
        arguments = native.call_args.args[0]
        assert '-File' in arguments and '-EncodedCommand' not in arguments
        assert Path(arguments[arguments.index('-File')+1]).name == 'OptimusStartup.ps1'
    elif entry == 'retarget':
        adapter._native(_STARTUP_PS, {'operation': 'update_existing'})
        arguments = native.call_args.args[0]
        assert '-EncodedCommand' in arguments
    else:
        with pytest.raises(ValueError, match='command-line limit'):
            adapter._native('x' * 32767, {'operation': 'fixture'})
        native.assert_not_called()
        return
    assert len(module.subprocess.list2cmdline(arguments)) < 32767
    assert native.call_args.kwargs['env']['SystemRoot'] == str(host.parents[3])


def test_native_failure_retains_bounded_numeric_facts_without_message_or_credentials():
    value = {'task_registration_started': True, 'failure_kind': 'persisted_task_unverified',
        'error': 'password=never-echo-this', 'native_error_chain': [
            {'type': 'System.IO.IOException', 'hresult': '0x80070070', 'win32_code': 112,
             'message': 'token=never-echo-this'}] * 20}
    error = safety.native_failure(ValueError, value, 1)
    assert len(error.native_diagnostics['native_error_chain']) == 8
    assert '0x80070070' in str(error) and '112' in str(error)
    assert 'password' not in str(error) and 'token' not in str(error)
    assert error.native_diagnostics['task_registration_started'] is True
