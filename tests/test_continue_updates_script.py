"""Execute the public helper with a mocked launcher; no update/native child runs."""
import base64
import json
import os
from pathlib import Path
import subprocess

import pytest


@pytest.mark.skipif(os.name != 'nt', reason='Windows PowerShell helper')
@pytest.mark.parametrize('status,exit_code', [('completed_with_skips', 0), ('blocked', 2)])
def test_helper_awaits_exact_update_only_command_and_reads_unique_final_receipt(tmp_path, status, exit_code):
    script = Path(__file__).parents[1] / 'scripts' / 'Continue-OptimusUpdates.ps1'
    executable = tmp_path / 'Fake Optimus.exe'
    executable.write_bytes(b'not a real executable; Start-Process is mocked')
    directory = tmp_path / 'Report parent'
    directory.mkdir()
    (directory / 'continue-updates-old.json').write_text('{"status":"old success"}', encoding='utf-8')
    launch_receipt = tmp_path / 'launcher.json'
    def literal(value):
        return "'" + str(value).replace("'", "''") + "'"
    # The mock lives only in this disposable PowerShell process. It intercepts
    # Start-Process before the empty fixture .exe could ever be invoked.
    harness = r'''
$ErrorActionPreference='Stop'
function global:Start-Process {
    param([string]$FilePath,[string[]]$ArgumentList,[switch]$Wait,[switch]$PassThru,
          [string]$WindowStyle,[string]$Verb)
    if (-not $Wait -or -not $PassThru -or $WindowStyle -cne 'Hidden' -or $Verb) {
        throw 'The mock requires one explicitly awaited non-elevated hidden child.'
    }
    if ($ArgumentList.Count -ne 3 -or $ArgumentList[0] -cne '--continue-updates' -or
        $ArgumentList[1] -cne '--output') { throw 'Unexpected update arguments.' }
    $folder=$ArgumentList[2].Trim('"')
    [IO.Directory]::CreateDirectory($folder) | Out-Null
    @{file=$FilePath;args=$ArgumentList;waited=[bool]$Wait;window=$WindowStyle;folder=$folder} |
        ConvertTo-Json -Depth 5 | Set-Content -LiteralPath $global:fixtureLaunchReceipt -Encoding UTF8
    @{schema=1;status=$global:fixtureResultStatus;installation_block_reason='Fixture held offer skipped or retained'} |
        ConvertTo-Json | Set-Content -LiteralPath (Join-Path $folder 'continue-updates-fixture.json') -Encoding UTF8
    # Newer progress must not masquerade as the final run receipt.
    @{schema=1;event=@{status='progress only'}} | ConvertTo-Json |
        Set-Content -LiteralPath (Join-Path $folder 'continue-updates-fixture.progress.json') -Encoding UTF8
    return [pscustomobject]@{ExitCode=$global:fixtureResultExitCode}
}
'''
    harness += '\n$global:fixtureLaunchReceipt=' + literal(launch_receipt)
    harness += '\n$global:fixtureResultStatus=' + literal(status)
    harness += '\n$global:fixtureResultExitCode=' + str(exit_code)
    harness += '\n& ' + literal(script) + ' -Executable ' + literal(executable)
    harness += ' -ReportDirectory ' + literal(directory) + ' -StandardUser\nexit $LASTEXITCODE\n'
    inbox = Path(os.environ['SystemRoot']) / 'System32' / 'WindowsPowerShell' / 'v1.0' / 'powershell.exe'
    result = subprocess.run([str(inbox), '-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass',
        '-EncodedCommand', base64.b64encode(harness.encode('utf-16-le')).decode('ascii')],
        capture_output=True, text=True, timeout=30)
    assert result.returncode == exit_code, result.stdout + result.stderr
    launched = json.loads(launch_receipt.read_text(encoding='utf-8-sig'))
    assert Path(launched['file']) == executable
    assert Path(launched['folder']).parent == directory
    assert Path(launched['folder']) != directory
    assert launched['waited'] is True and launched['window'] == 'Hidden'
    assert 'Result: ' + status in result.stdout
    assert 'old success' not in result.stdout and 'Result: progress only' not in result.stdout
