# Fake registry fixtures only. No real App Paths registrations are modified.
$ErrorActionPreference='Stop'
$module=Join-Path $PSScriptRoot '..\src\optimus\engine\RegistryTools.ps1'
. $module
$script:SCMDataRoot=Join-Path $env:TEMP ('OptimusRegistryFixture-'+[guid]::NewGuid().ToString('N'))
[void](New-Item -ItemType Directory -Path $script:SCMDataRoot -Force)
$checks=New-Object 'System.Collections.Generic.List[object]'
function Confirm-RegistryFixture([string]$Name,[bool]$Pass){$checks.Add([pscustomobject]@{Check=$Name;Pass=$Pass});if(-not $Pass){throw $Name}}
function Copy-RegistryFixture($Value){ConvertFrom-Json -InputObject (ConvertTo-Json -InputObject $Value -Depth 15)}
$script:FakeMap=@{}
$script:FakeExists=$false
$script:RemoveCount=0
$script:RestoreCount=0
$script:OriginalTargetFunction=${function:Get-OptimusRegistryMissingTarget}
$script:OriginalWriteBackup=${function:Write-OptimusRegistryBackup}
$script:FixtureSid=Get-OptimusRegistrySid
$base=[pscustomobject]@{Name='FixtureMissing.exe';Scope=(Get-OptimusRegistryScope);UserSid=$script:FixtureSid;ValueBytes=180;AclSddl='O:BAG:BAD:(A;;KA;;;BA)(A;;KA;;;SY)';Values=@(
 [pscustomobject][ordered]@{Name='';Kind='String';Data='C:\NoSuchOptimusFixture\Missing.exe'},
 [pscustomobject][ordered]@{Name='Path';Kind='ExpandString';Data='%TEMP%\fixture'},
 [pscustomobject][ordered]@{Name='Binary';Kind='Binary';Data='AAH/'},
 [pscustomobject][ordered]@{Name='None';Kind='None';Data='AQI='},
 [pscustomobject][ordered]@{Name='Multiple';Kind='MultiString';Data=@('a','b')},
 [pscustomobject][ordered]@{Name='Dword';Kind='DWord';Data='-1'},
 [pscustomobject][ordered]@{Name='Qword';Kind='QWord';Data='9223372036854775807'}
)}
function Get-OptimusRegistryAppPathNames {@($script:FakeMap.Keys)}
function Read-OptimusRegistryAppPath([string]$Name){if(-not $script:FakeMap.ContainsKey($Name)){throw 'Missing fake key'};Copy-RegistryFixture $script:FakeMap[$Name]}
function Get-OptimusRegistryMissingTarget($Snapshot){if($script:FakeExists){return $null};[string]@($Snapshot.Values | Where-Object Name -eq '')[0].Data}
function Remove-OptimusRegistryAppPathKey([string]$Name){if(@(Get-ChildItem -LiteralPath (Get-OptimusRegistryBackupRoot) -Filter '*.json').Count -eq 0){throw 'Deletion attempted without durable fixture backup'};$script:RemoveCount++;[void]$script:FakeMap.Remove($Name)}
function Restore-OptimusRegistryAppPathKey($Snapshot){[void](Test-OptimusRegistrySnapshot $Snapshot);if($script:FakeMap.ContainsKey($Snapshot.Name)){throw 'New fake key conflict'};$script:FakeMap[$Snapshot.Name]=Copy-RegistryFixture $Snapshot;$script:RestoreCount++}
try {
 $tokens=$null;$errors=$null;$ast=[Management.Automation.Language.Parser]::ParseFile((Resolve-Path -LiteralPath $module).Path,[ref]$tokens,[ref]$errors)
 Confirm-RegistryFixture 'PS5 syntax' (@($errors).Count -eq 0)
 $native=$ast.Find({param($node)($node -is [Management.Automation.Language.ExpandableStringExpressionAst] -or $node -is [Management.Automation.Language.StringConstantExpressionAst]) -and $node.Value.Contains('public static class OptimusRegistryNativeKey')},$true)
 Add-Type -TypeDefinition $native.Value
 Confirm-RegistryFixture 'Native create-only helper compiles without invoking registry' ($null -ne ('OptimusRegistryNativeKey' -as [type]))
 [void](Test-OptimusRegistrySnapshot $base)
 Confirm-RegistryFixture 'All supported raw registry value types validate' $true
 $realTarget=& $script:OriginalTargetFunction $base
 Confirm-RegistryFixture 'Missing local absolute exe target recognized' ($realTarget -eq $base.Values[0].Data)
 $script:FakeMap[$base.Name]=Copy-RegistryFixture $base
 $scan=Get-OptimusRegistryIssues
 Confirm-RegistryFixture 'Review creates one approved missing-target issue only' ($scan.issues.Count -eq 1 -and $script:RemoveCount -eq 0)
 $record=$scan.issues[0]
 $forged=$record.PSObject.Copy();$forged.Id='unknown';$blocked=$false
 try{[void](Remove-OptimusRegistryIssue $forged)}catch{$blocked=$true}
 Confirm-RegistryFixture 'Forged approval blocked' ($blocked -and $script:RemoveCount -eq 0)
 $forged=$record.PSObject.Copy();$forged.Name='Other.exe';$blocked=$false
 try{[void](Remove-OptimusRegistryIssue $forged)}catch{$blocked=$true}
 Confirm-RegistryFixture 'Caller cannot redirect key name' ($blocked -and $script:RemoveCount -eq 0)
 $script:FakeMap[$base.Name].Values[1].Data='changed'
 $blocked=$false;try{[void](Remove-OptimusRegistryIssue $record)}catch{$blocked=$true}
 Confirm-RegistryFixture 'Changed values blocked' ($blocked -and $script:RemoveCount -eq 0)
 $script:FakeMap[$base.Name]=Copy-RegistryFixture $base
 $script:FakeExists=$true;$blocked=$false
 try{[void](Remove-OptimusRegistryIssue $record)}catch{$blocked=$true}
 Confirm-RegistryFixture 'Executable appearing after review blocks removal' ($blocked -and $script:RemoveCount -eq 0)
 $script:FakeExists=$false
 $undo=Remove-OptimusRegistryIssue $record
 Confirm-RegistryFixture 'Verified full backup precedes one selected fake deletion' ($script:RemoveCount -eq 1 -and (Test-Path -LiteralPath $undo.BackupPath) -and -not $script:FakeMap.ContainsKey($base.Name))
 $script:FakeMap[$base.Name]=Copy-RegistryFixture $base
 $blocked=$false;try{[void](Restore-OptimusRegistryIssue $undo)}catch{$blocked=$true}
 Confirm-RegistryFixture 'Undo refuses a newer key conflict' ($blocked -and $script:RestoreCount -eq 0)
 [void]$script:FakeMap.Remove($base.Name)
 $restored=Restore-OptimusRegistryIssue $undo
 Confirm-RegistryFixture 'Undo restores all raw kinds and ACL snapshot' ($restored.Restored -and $script:RestoreCount -eq 1 -and (Get-OptimusRegistryFingerprint $script:FakeMap[$base.Name]) -eq (Get-OptimusRegistryFingerprint $base))
 $issue=(Get-OptimusRegistryIssues).issues[0]
 function Write-OptimusRegistryBackup {param($Id,$Snapshot,$Fingerprint)Join-Path $script:SCMDataRoot 'not-created.json'}
 $blocked=$false;try{[void](Remove-OptimusRegistryIssue $issue)}catch{$blocked=$true}
 Confirm-RegistryFixture 'Missing backup blocks deletion' ($blocked -and $script:RemoveCount -eq 1)
 ${function:Write-OptimusRegistryBackup}=$script:OriginalWriteBackup
 $invalid=Copy-RegistryFixture $base;$invalid.Scope='Software\Other';$blocked=$false
 try{[void](Test-OptimusRegistrySnapshot $invalid)}catch{$blocked=$true}
 Confirm-RegistryFixture 'Out-of-scope restore rejected' $blocked
 $invalid=Copy-RegistryFixture $base;$invalid.Values[0].Data='x'*20000;$blocked=$false
 try{[void](Test-OptimusRegistrySnapshot $invalid)}catch{$blocked=$true}
 Confirm-RegistryFixture 'Oversized raw snapshot rejected' $blocked
 $invalid=Copy-RegistryFixture $base;$invalid.UserSid='S-1-5-999';$blocked=$false
 try{[void](Test-OptimusRegistrySnapshot $invalid)}catch{$blocked=$true}
 Confirm-RegistryFixture 'Different user context rejected' $blocked
} catch {$checks.Add([pscustomobject]@{Check=$_.Exception.Message;Pass=$false})}
$checks.ToArray() | ConvertTo-Json -Depth 4 | Set-Content -LiteralPath (Join-Path $PSScriptRoot '..\registry-tools-tests.json') -Encoding UTF8
$checks.ToArray() | Format-Table -AutoSize | Out-String | Write-Output
if(@($checks | Where-Object {-not $_.Pass}).Count){exit 1}
exit 0
