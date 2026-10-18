# Fake registry + fake history only; transport dispatch definitions are extracted without executing the server.
$ErrorActionPreference='Stop'
$engine=Join-Path $PSScriptRoot '..\src\optimus\engine'
. (Join-Path $engine 'RegistryTools.ps1')
$tokens=$null;$errors=$null
$ast=[Management.Automation.Language.Parser]::ParseFile((Join-Path $engine 'OptimusBridge.ps1'),[ref]$tokens,[ref]$errors)
if($errors.Count){throw 'Bridge syntax failed'}
foreach($name in @('Invoke-OptimusRequest','Restore-OptimusRegistryChange','Test-OptimusSameExecutableFile')) {
 $node=$ast.Find({param($n)$n -is [Management.Automation.Language.FunctionDefinitionAst] -and $n.Name -eq $name},$false)
 if(-not $node){throw ('Missing bridge function '+$name)}
 Invoke-Expression $node.Extent.Text
}
$script:SCMDataRoot=Join-Path $env:TEMP ('OptimusRegistryIntegration-'+[guid]::NewGuid().ToString('N'))
[void](New-Item -ItemType Directory -Path $script:SCMDataRoot -Force)
$checks=New-Object 'System.Collections.Generic.List[object]'
function Check([string]$Name,[bool]$Pass){$checks.Add([pscustomobject]@{Check=$Name;Pass=$Pass});if(-not $Pass){throw $Name}}
function Copy-RegistryIntegrationFixture($Value){ConvertFrom-Json -InputObject (ConvertTo-Json -InputObject $Value -Depth 15)}
$script:FakeRegistry=@{}
$script:FakeChanges=@()
$script:Deleted=0
$script:Restored=0
$script:FailJournal=$false
$base=[pscustomobject]@{Name='OptimusFixture.exe';Scope=(Get-OptimusRegistryScope);UserSid=(Get-OptimusRegistrySid);ValueBytes=100;AclSddl='O:BAG:BAD:(A;;KA;;;BA)';Values=@([pscustomobject][ordered]@{Name='';Kind='String';Data='C:\MissingFixture\OptimusFixture.exe'})}
function Get-OptimusRegistryAppPathNames {@($script:FakeRegistry.Keys)}
function Read-OptimusRegistryAppPath([string]$Name){if(-not $script:FakeRegistry.ContainsKey($Name)){throw 'Missing fake registration'};Copy-RegistryIntegrationFixture $script:FakeRegistry[$Name]}
function Get-OptimusRegistryMissingTarget($Snapshot){$Snapshot.Values[0].Data}
function Get-SCMChanges {@($script:FakeChanges)}
function New-SCMChange {param($Kind,$Target,$Undo,$Status)
 if($script:FailJournal){throw 'Fixture journal failure'}
 if(-not(Test-Path -LiteralPath $Undo.BackupPath)){throw 'Journal preceded verified backup'}
 $change=[pscustomobject]@{Id=[guid]::NewGuid().ToString('N');Kind=$Kind;Target=$Target;Undo=(Copy-RegistryIntegrationFixture $Undo);Status=$Status}
 $script:FakeChanges+=,$change
 return $change
}
function Set-SCMChangeStatus {param($Change,$Status)$Change.Status=$Status}
function Remove-OptimusRegistryAppPathKey([string]$Name){
 if(@($script:FakeChanges | Where-Object {$_.Undo.Name -eq $Name -and $_.Status -eq 'Pending'}).Count -ne 1){throw 'Removal lacked pending journal'}
 [void]$script:FakeRegistry.Remove($Name);$script:Deleted++
}
function Restore-OptimusRegistryAppPathKey($Snapshot){if($script:FakeRegistry.ContainsKey($Snapshot.Name)){throw 'Fake conflict'};$script:FakeRegistry[$Snapshot.Name]=Copy-RegistryIntegrationFixture $Snapshot;$script:Restored++}
try {
 Check 'Bridge PS5 syntax and dispatch definitions' $true
 $selfPath=(Get-Process -Id $PID).Path
 Check 'Native file identity helper accepts same path' (Test-OptimusSameExecutableFile $selfPath $selfPath)
 Check 'Native file identity helper rejects another executable' (-not(Test-OptimusSameExecutableFile $selfPath (Join-Path $env:SystemRoot 'System32/notepad.exe')))
 $script:FakeRegistry[$base.Name]=Copy-RegistryIntegrationFixture $base
 $scan=Invoke-OptimusRequest ([pscustomobject]@{operation='registry_scan';arguments=@{}})
 Check 'Scan dispatch reviews fake current-user registration without mutation' ($scan.issues.Count -eq 1 -and $script:Deleted -eq 0)
 $record=$scan.issues[0]
 $script:FailJournal=$true;$blocked=$false
 try{[void](Invoke-OptimusRequest ([pscustomobject]@{operation='registry_remove';arguments=@{record=$record}}))}catch{$blocked=$true}
 Check 'Failed journal prevents fake deletion' ($blocked -and $script:Deleted -eq 0)
 $script:FailJournal=$false
 $change=Invoke-OptimusRequest ([pscustomobject]@{operation='registry_remove';arguments=@{record=$record}})
 Check 'Selective removal backs up then journals then deletes then marks Applied' ($script:Deleted -eq 1 -and $script:FakeChanges.Count -eq 1 -and $change.Status -eq 'Applied' -and $change.Undo.Removed)
 $forged=Copy-RegistryIntegrationFixture $change;$forged.Id='unknown';$blocked=$false
 try{[void](Invoke-OptimusRequest ([pscustomobject]@{operation='restore';arguments=@{change=$forged}}))}catch{$blocked=$true}
 Check 'Restore refuses unsaved caller identity' ($blocked -and $script:Restored -eq 0)
 $forged=Copy-RegistryIntegrationFixture $change;$forged.Undo.Name='Attacker.exe';$forged.Undo.BackupPath='C:\caller-selected.json'
 $result=Invoke-OptimusRequest ([pscustomobject]@{operation='restore';arguments=@{change=$forged}})
 Check 'Restore resolves saved undo and ignores caller substitution' ($result.restored -and $script:Restored -eq 1 -and $script:FakeRegistry.ContainsKey($base.Name) -and $script:FakeChanges[0].Status -eq 'Restored')
 $blocked=$false;try{[void](Invoke-OptimusRequest ([pscustomobject]@{operation='registry_restore';arguments=@{change=$change}}))}catch{$blocked=$true}
 Check 'Already restored change cannot be replayed' ($blocked -and $script:Restored -eq 1)
} catch {$checks.Add([pscustomobject]@{Check=$_.Exception.Message;Pass=$false})}
$checks.ToArray() | Format-Table -AutoSize | Out-String | Write-Output
if(@($checks | Where-Object {-not $_.Pass}).Count){exit 1}
exit 0
