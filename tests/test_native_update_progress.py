"""Compile real callback interfaces, but exercise WUA through offline jobs only."""
import json
import os
from pathlib import Path
import subprocess
from unittest.mock import Mock

import pytest


@pytest.mark.skipif(os.name != 'nt', reason='Windows PowerShell 5.1 COM callback ABI fixtures.')
def test_offline_wua_progress_and_callback_interfaces():
    script = Path(__file__).with_name('Test-UpdateProgress.ps1')
    executable = Path(os.environ['SystemRoot']) / 'System32/WindowsPowerShell/v1.0/powershell.exe'
    completed = subprocess.run([str(executable), '-NoLogo', '-NoProfile', '-NonInteractive',
        '-ExecutionPolicy', 'Bypass', '-File', str(script)], capture_output=True,
        text=True, timeout=45)
    assert completed.returncode == 0, completed.stdout + completed.stderr
    report = json.loads(completed.stdout)
    assert report['Result'] == 'PASS' and report['Assertions'] >= 100
    assert report['CallbackInterfaces'] == 4
    assert report['RealScans'] == report['RealDownloads'] == report['RealInstalls'] == 0
    assert report['PSVersion'].startswith('5.1.')


@pytest.mark.skipif(os.name != 'nt', reason='Windows PowerShell 5.1 fake-WUA dispatcher integration.')
def test_offline_driver_progress_bridge_keeps_preflight_and_terminal_receipts():
    script = Path(__file__).with_name('Test-UpdateProgressIntegration.ps1')
    executable = Path(os.environ['SystemRoot']) / 'System32/WindowsPowerShell/v1.0/powershell.exe'
    completed = subprocess.run([str(executable), '-NoLogo', '-NoProfile', '-NonInteractive',
        '-ExecutionPolicy', 'Bypass', '-File', str(script)], capture_output=True,
        text=True, timeout=45)
    assert completed.returncode == 0, completed.stdout + completed.stderr
    report = json.loads(completed.stdout)
    assert report['Result'] == 'PASS' and report['Assertions'] >= 60
    assert report['BaseAssertions'] >= 60
    assert report['RealScans'] == report['RealDownloads'] == report['RealInstalls'] == 0


def test_driver_backend_progress_is_opt_in_and_never_serializes_callable():
    from optimus.backend import Backend
    backend = Backend.__new__(Backend)
    backend.bridge = Mock()
    backend.preferences = Mock(return_value={})
    record = {'id': '00000000-0000-0000-0000-000000000001', 'revision': 1}
    callback = Mock()
    result = backend.driver_update_apply_unattended(record, accept_eula=True, progress_callback=callback)
    assert result is backend.bridge.request.return_value
    backend.bridge.request.assert_called_once_with('driver_update_apply', id=record['id'],
        revision=1, unattended=True, acceptEula=True, record=record, timeout=900,
        progress_callback=callback, reportProgress=True, cleanupDownloads=True)
    backend.bridge.reset_mock()
    backend.driver_update_apply_unattended(record, accept_eula=True)
    assert 'progress_callback' not in backend.bridge.request.call_args.kwargs
    assert 'reportProgress' not in backend.bridge.request.call_args.kwargs


@pytest.mark.parametrize('agreement,callback', [(False, None), (1, Mock()), (True, 42)])
def test_driver_backend_rejects_invalid_approval_or_observer_before_bridge(agreement, callback):
    from optimus.backend import Backend
    backend = Backend.__new__(Backend)
    backend.bridge = Mock()
    with pytest.raises(ValueError):
        backend.driver_update_apply_unattended({'id': 'fixture', 'revision': 1},
            accept_eula=agreement, progress_callback=callback)
    backend.bridge.request.assert_not_called()
