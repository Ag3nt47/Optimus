"""Exercise the actual native request function through its JSON wire envelope."""
import json
import os
from pathlib import Path
import subprocess

import pytest


@pytest.fixture(scope='module')
def dispatch_receipts(tmp_path_factory):
    if os.name != 'nt':
        pytest.skip('Actual Windows PowerShell bridge dispatch AST fixture.')
    path = tmp_path_factory.mktemp('health-dispatch') / 'receipts.json'
    script = Path(__file__).with_name('Test-HardwareHealthDispatch.ps1')
    executable = Path(os.environ['SystemRoot']) / 'System32/WindowsPowerShell/v1.0/powershell.exe'
    result = subprocess.run([str(executable), '-NoLogo', '-NoProfile', '-NonInteractive',
        '-File', str(script), '-ReceiptsPath', str(path)],
        capture_output=True, text=True, encoding='utf-8', timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr
    summary = json.loads(result.stdout)
    assert summary['Result'] == 'PASS' and summary['Cases'] == 10
    assert summary['ActualDispatchAst'] is True
    assert summary['RealQueries'] == summary['HardwareProcessesStarted'] == summary['FullBridgeBootstraps'] == 0
    return {row['name']: row for row in json.loads(path.read_text(encoding='utf-8-sig'))}


@pytest.mark.parametrize('name,expected', [('missing', False), ('nested_true', True), ('nested_false', False),
    ('root_true_nested_false', False), ('root_only_ignored', False)])
def test_actual_native_dispatch_reads_force_only_from_the_arguments_envelope(dispatch_receipts, name, expected):
    row = dispatch_receipts[name]
    assert not row['rejected'] and row['collector_calls'] == row['callback_factory_calls'] == 1
    assert row['result']['force_refresh'] is expected


@pytest.mark.parametrize('name', ['string_true', 'integer_one', 'explicit_null'])
def test_actual_native_dispatch_rejects_non_boolean_force_before_collector_or_callback(dispatch_receipts, name):
    row = dispatch_receipts[name]
    assert row['rejected'] and row['collector_calls'] == row['callback_factory_calls'] == 0


def test_actual_native_dispatch_preserves_cancellation_callback(dispatch_receipts):
    assert dispatch_receipts['cooperative_cancel']['result']['cancelled'] is True
    assert dispatch_receipts['cooperative_cancel']['result']['callback_present'] is True
    assert dispatch_receipts['no_cooperative_callback']['result']['cancelled'] is False
    assert dispatch_receipts['no_cooperative_callback']['result']['callback_present'] is False


def test_native_fixture_loads_only_the_actual_dispatch_function_without_bootstrap():
    source = Path(__file__).with_name('Test-HardwareHealthDispatch.ps1').read_text()
    assert 'Parser]::ParseFile' in source and 'FunctionDefinitionAst' in source
    assert '[scriptblock]::Create($definitions[0].Extent.Text)' in source
    assert '. (Join-Path' not in source
    assert 'Get-CimInstance' not in source and 'Start-Process' not in source
