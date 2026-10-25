"""Actual inbox PS5.1 helper coverage with task files in an owned temp tree."""
import json
import os
from pathlib import Path
import subprocess
import uuid

import pytest


@pytest.mark.skipif(os.name != 'nt', reason='Actual inbox PowerShell5.1 startup safety fixtures.')
def test_offline_native_startup_safety_never_registers_host_tasks(tmp_path):
    executable = Path(os.environ['SystemRoot']) / 'System32/WindowsPowerShell/v1.0/powershell.exe'
    script = Path(__file__).with_name('Test-StartupSafety.ps1')
    fixture = tmp_path / ('optimus-startup-safety-' + uuid.uuid4().hex)
    result = subprocess.run([str(executable), '-NoLogo', '-NoProfile', '-NonInteractive',
        '-ExecutionPolicy', 'Bypass', '-File', str(script), '-FixtureRoot', str(fixture)],
        capture_output=True, text=True, timeout=45)
    assert result.returncode == 0, result.stdout + result.stderr
    report = json.loads(result.stdout)
    assert report['Result'] == 'PASS' and report['Assertions'] == 56
    assert report['RealTaskRegistrations'] == report['RealTaskRemovals'] == 0
    assert report['RealExternalProgramsLaunched'] == 0
    assert report['FixtureTaskFilesOnly'] is True
    assert report['PSVersion'].startswith('5.1.')
