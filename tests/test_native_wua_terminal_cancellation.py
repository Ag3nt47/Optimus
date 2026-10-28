"""Run real PowerShell with inert jobs and exact HRESULT exceptions only."""
import json
import os
from pathlib import Path
import subprocess

import pytest


@pytest.mark.skipif(os.name != 'nt', reason='Inbox PowerShell 5.1 fake-COM jobs.')
def test_owned_terminal_abort_hresult_is_narrow_and_noninstaller():
    script = Path(__file__).with_name('Test-WuaTerminalCancellation.ps1')
    executable = Path(os.environ['SystemRoot']) / 'System32/WindowsPowerShell/v1.0/powershell.exe'
    environment = dict(os.environ)
    environment['PSModulePath'] = str(executable.parent / 'Modules')
    result = subprocess.run([str(executable), '-NoLogo', '-NoProfile', '-NonInteractive',
        '-ExecutionPolicy', 'Bypass', '-File', str(script)], capture_output=True,
        text=True, env=environment, timeout=40)
    assert result.returncode == 0, result.stdout + result.stderr
    report = json.loads(result.stdout)
    assert report['Result'] == 'PASS' and report['Assertions'] >= 60
    assert report['RealScans'] == report['RealDownloads'] == report['RealInstalls'] == 0
