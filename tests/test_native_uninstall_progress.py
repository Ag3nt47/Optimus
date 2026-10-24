"""Exercise native removal status through offline PowerShell fixtures only."""
import json
import os
from pathlib import Path
import subprocess

import pytest


@pytest.mark.skipif(os.name != 'nt', reason='Actual inbox PowerShell5.1 mocked uninstall progress.')
def test_offline_native_uninstall_progress_is_verified_and_fail_forward():
    executable = Path(os.environ['SystemRoot']) / 'System32/WindowsPowerShell/v1.0/powershell.exe'
    script = Path(__file__).with_name('Test-UninstallProgress.ps1')
    result = subprocess.run([str(executable), '-NoLogo', '-NoProfile', '-NonInteractive',
        '-ExecutionPolicy', 'Bypass', '-File', str(script)], capture_output=True, text=True, timeout=45)
    assert result.returncode == 0, result.stdout + result.stderr
    report = json.loads(result.stdout)
    assert report['Result'] == 'PASS' and report['Assertions'] >= 60
    assert report['RealAppRemovals'] == report['RealExternalProgramsLaunched'] == 0
    assert report['PSVersion'].startswith('5.1.')
