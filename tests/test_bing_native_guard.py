"""Extracted native guards and fake snapshots; no real process may be opened."""
import json
import os
from pathlib import Path
import subprocess

import pytest


@pytest.mark.skipif(os.name != 'nt', reason='Windows PowerShell guard harness')
def test_standalone_process_actions_preserve_bing_even_with_forged_inventory(tmp_path):
    project = Path(__file__).parents[1]
    harness = tmp_path / 'bing-guard.ps1'
    harness.write_text(r'''
param([string]$SourceRoot)
$ErrorActionPreference='Stop'
function Import-OnlyFunction {
 param([string]$File,[string[]]$Names)
 $tokens=$null;$parseErrors=$null
 $ast=[Management.Automation.Language.Parser]::ParseFile($File,[ref]$tokens,[ref]$parseErrors)
 if($parseErrors.Count){throw 'Native source has parse errors.'}
 foreach($name in $Names){
  $definition=$ast.Find({param($node) $node -is [Management.Automation.Language.FunctionDefinitionAst] -and $node.Name -ceq $name},$true)
  if(-not $definition){throw 'Expected guard function is missing.'}
  . ([scriptblock]::Create($definition.Extent.Text.Replace(('function '+$name+' '),('function global:'+$name+' '))))
 }
}
Import-OnlyFunction (Join-Path $SourceRoot 'src/optimus/engine/ProcessInventory.ps1') @('Get-SCMProtectionReason')
Import-OnlyFunction (Join-Path $SourceRoot 'src/optimus/engine/ComputerManager.ps1') @('Assert-SCMProcessAction','Stop-SCMSelectedProcess','Set-SCMProcessPriority','Set-SCMProcessAffinity')
$script:NativeCalls=0;$script:IdentityChecks=0;$script:Assertions=0
function Get-Process { $script:NativeCalls++;throw 'A real process must never be opened.' }
function Test-SCMProcessIdentity { param($Record) $script:IdentityChecks++;return $Record }
function Get-SCMProcessSnapshot { return $script:Current }
$variants=@(
 @('BingWallpaper.exe','C:\Fixture\Wallpaper\BingWallpaper.exe'),
 @('wallpaper.exe','C:\Fixture\Bing Wallpaper\wallpaper.exe'),
 @('update.exe','C:\Fixture\BingWallpaper\update.exe')
)
foreach($variant in $variants){
 $reason=Get-SCMProtectionReason -ProcessId 6000 -SessionId 1 -Name $variant[0] -Path $variant[1] -AncestorIds @{}
 if($reason -notlike '*Bing Wallpaper*'){throw 'Bing inventory was not protected.'};$script:Assertions++
 $script:Current=[pscustomobject]@{ProcessId=6000;Name=$variant[0];Path=$variant[1];IsProtected=$true;ProtectionReason=$reason}
 $forged=[pscustomobject]@{ProcessId=6000;Name=$variant[0];Path=$variant[1];IsProtected=$false;ProtectionReason=''}
 foreach($action in @('close','force','priority','affinity')){
  $blocked=$false
  try {
   switch($action){
    'close' {Stop-SCMSelectedProcess -Record $forged}
    'force' {Stop-SCMSelectedProcess -Record $forged -Force}
    'priority' {Set-SCMProcessPriority -Record $forged -Priority Normal}
    'affinity' {Set-SCMProcessAffinity -Record $forged -Cores '0'}
   }
  } catch {if($_.Exception.Message -like '*Process is now protected*'){$blocked=$true}else{throw}}
  if(-not $blocked){throw 'A forged Bing record reached a process mutation.'};$script:Assertions++
 }
}
$ordinary=Get-SCMProtectionReason -ProcessId 6000 -SessionId 1 -Name 'ordinary.exe' -Path 'C:\Fixture\ordinary.exe' -AncestorIds @{}
if($ordinary){throw 'An ordinary fixture process was unexpectedly classified as Bing.'};$script:Assertions++
if($script:NativeCalls -ne 0 -or $script:IdentityChecks -ne 12){throw 'Unexpected native process calls or missing fresh validation.'}
@{result='PASS';assertions=$script:Assertions;native_process_calls=$script:NativeCalls}|ConvertTo-Json -Compress
''', encoding='utf-8')
    executable = Path(os.environ.get('SystemRoot', r'C:\Windows')) / 'System32/WindowsPowerShell/v1.0/powershell.exe'
    result = subprocess.run([str(executable), '-NoLogo', '-NoProfile', '-NonInteractive', '-File', str(harness),
                             '-SourceRoot', str(project)], capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr
    summary = json.loads(result.stdout)
    assert summary == {'result': 'PASS', 'assertions': 16, 'native_process_calls': 0}
