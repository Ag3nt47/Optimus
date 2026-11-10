$ErrorActionPreference='Stop'
. (Join-Path $PSScriptRoot '..\src\optimus\engine\RemovalGuardTasks.ps1')
$script:Assertions=0
function Assert-GuardFixture {param([bool]$Condition,[string]$Message)if(-not $Condition){throw ('FAIL: '+$Message)};$script:Assertions++}
function Assert-GuardRefused {param([scriptblock]$Action,[string]$Message)$blocked=$false;try{$null=& $Action}catch{$blocked=$true};Assert-GuardFixture $blocked $Message}
function New-GuardFixtureCollection {
 param([string]$Kind)
 $value=[pscustomobject]@{Rows=(New-Object Collections.Generic.List[object]);Kind=$Kind}
 $value|Add-Member -MemberType ScriptProperty -Name Count -Value {$this.Rows.Count}
 $value|Add-Member -MemberType ScriptMethod -Name Item -Value {param($index)return $this.Rows[$index-1]}
 $value|Add-Member -MemberType ScriptMethod -Name Create -Value {
  param($type)
  if($this.Kind -ceq 'trigger'){$row=[pscustomobject]@{Type=$type;UserId='';Delay='';Enabled=$true;StartBoundary='';EndBoundary='';Subscription='';Repetition=[pscustomobject]@{Interval='';Duration='';StopAtDurationEnd=$false}}}
  else{$row=[pscustomobject]@{Type=$type;Path='';Arguments='';WorkingDirectory=''}}
  $this.Rows.Add($row);return $row
 }
 return $value
}
function New-GuardFixtureDefinition {
 [pscustomobject]@{RegistrationInfo=[pscustomobject]@{Source='';Description=''};Principal=[pscustomobject]@{UserId='';LogonType=0;RunLevel=0};
  Settings=[pscustomobject]@{Enabled=$true;MultipleInstances=0;ExecutionTimeLimit='';AllowHardTerminate=$true;DisallowStartIfOnBatteries=$true;StopIfGoingOnBatteries=$true;StartWhenAvailable=$false;RunOnlyIfIdle=$true};
  Actions=(New-GuardFixtureCollection action);Triggers=(New-GuardFixtureCollection trigger)}
}
$script:TaskMap=@{};$script:Registered=@();$script:Deleted=@();$script:Launches=0;$script:IsAdmin=$true
$script:Folder=[pscustomobject]@{}
$script:Folder|Add-Member -MemberType ScriptMethod -Name GetTask -Value {param($name)if($script:TaskMap.ContainsKey($name)){return $script:TaskMap[$name]};throw (New-Object IO.FileNotFoundException)}
$script:Folder|Add-Member -MemberType ScriptMethod -Name RegisterTaskDefinition -Value {
 param($name,$definition,$flags,$user,$password,$logon,$sddl)
 if($password -or $user -ceq 'SYSTEM' -or $logon -ne 3){throw 'Fixture forbids service credentials.'}
 $script:Registered+=@([pscustomobject]@{name=$name;flags=$flags;user=$user;password=$password;logon=$logon;sddl=$sddl})
 $task=[pscustomobject]@{Definition=$definition;Enabled=$true;LastTaskResult=0;Sddl=$sddl}
 $task|Add-Member -MemberType ScriptMethod -Name GetSecurityDescriptor -Value {param($flags)return $this.Sddl}
 $task|Add-Member -MemberType ScriptMethod -Name Stop -Value {throw 'A running guard/installer must never be stopped'}
 $script:TaskMap[$name]=$task;return $task
}
$script:Folder|Add-Member -MemberType ScriptMethod -Name DeleteTask -Value {param($name,$flags)$script:Deleted+=@($name);$script:TaskMap.Remove($name)}
$script:Scheduler=[pscustomobject]@{}
$script:Scheduler|Add-Member -MemberType ScriptMethod -Name GetFolder -Value {param($name)return $script:Folder}
$script:Scheduler|Add-Member -MemberType ScriptMethod -Name NewTask -Value {param($unused)return New-GuardFixtureDefinition}
function Get-OptimusRemovalGuardScheduler {return $script:Scheduler}
function Test-OptimusRemovalGuardAdmin {return $script:IsAdmin}
function Start-Process {$script:Launches++;throw 'Fixtures must never launch a process.'}
$script:Sid=Get-OptimusRemovalGuardSid
$fixture=Join-Path ([IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))) ('test-artifacts\removal-guard-v2-'+[guid]::NewGuid().ToString('N'))
if(-not $fixture.StartsWith('D:\',[StringComparison]::OrdinalIgnoreCase)){throw 'Fixtures must stay on D.'}
$data=Join-Path $fixture 'Data';$null=New-Item -ItemType Directory -Path $data -Force
$prefs=[pscustomobject]@{setup_version=2;windows_removal_enforcement_enabled=$true;windows_removal_enforcement_scope_hash='';consumer_debloat_enabled=$false;m365_debloat_enabled=$true;office365_desktop_debloat_enabled=$true;office_desktop_debloat_enabled=$false;outlook_debloat_enabled=$false;edge_debloat_enabled=$true;privacy_advertising_id_disabled=$true;consumer_debloat_package_names=@('Microsoft.BingWeather');app_removal_exclusions=@([pscustomobject]@{kind='store';id='MicrosoftCorporationII.Windows365_8wekyb3d8bbwe';name='MicrosoftCorporationII.Windows365';label='Microsoft Windows App'})}
$scope=Get-OptimusGuardPreferenceScope $prefs
$hash=Get-OptimusRemovalGuardDigest ([Text.Encoding]::UTF8.GetBytes((ConvertTo-OptimusGuardCanonicalJson $scope)))
$prefs.windows_removal_enforcement_scope_hash=$hash
$prefPath=Join-Path $data 'preferences.json';[IO.File]::WriteAllText($prefPath,($prefs|ConvertTo-Json -Depth 20))
[IO.File]::WriteAllText((Join-Path $data 'maintenance-run.lock'),'')
$request=@{operation='status';dataRoot=$data;launchManifest=(Join-Path $data 'startup-launch.json')}
$status=Invoke-OptimusRemovalGuardRequest $request
Assert-GuardFixture (-not $status.status.enabled -and -not $status.status.owned -and $script:Registered.Count -eq 0) 'absent status creates no task'
$request.operation='enable';$request.approvedScope=$scope;$request.scopeHash=$hash
$enabled=Invoke-OptimusRemovalGuardRequest $request
Assert-GuardFixture ($enabled.status.enabled -and $enabled.status.owned -and $script:Registered.Count -eq 1) 'one protected task registered in fake scheduler'
$name=$enabled.status.task_name;$task=$script:TaskMap[$name];$bundle=Read-OptimusGuardTaskBundle $task $script:Sid $data
Assert-GuardFixture ($script:Registered[0].flags -eq 18 -and $script:Registered[0].logon -eq 3 -and -not $script:Registered[0].password -and $script:Registered[0].user -ceq $script:Sid) 'interactive same user and DONT_ADD_PRINCIPAL_ACE'
Assert-GuardFixture ($script:Registered[0].sddl -ceq ('O:BAG:BAD:P(A;;GA;;;SY)(A;;GA;;;BA)(A;;GRGX;;;'+$script:Sid+')')) 'administrator owner with protected read-execute-only user ACL'
Assert-GuardFixture ($task.Definition.Principal.RunLevel -eq 1 -and (Test-OptimusGuardProtectedTask $task $script:Sid)) 'approved highest token has verified protected task security'
Assert-GuardFixture ($task.Definition.Triggers.Count -eq 3 -and $task.Definition.Triggers.Item(2).Repetition.Interval -ceq 'PT15M' -and $task.Definition.Triggers.Item(3).Subscription -match 'EventID=19') 'logon, 15-minute and Windows Update triggers'
Assert-GuardFixture ($task.Definition.Settings.MultipleInstances -eq 2 -and $task.Definition.Settings.ExecutionTimeLimit -ceq 'PT0S' -and -not $task.Definition.Settings.AllowHardTerminate -and -not $task.Definition.Settings.StopIfGoingOnBatteries) 'active uninstallers are never terminated by schedule or power transitions'
Assert-GuardFixture ($task.Definition.Actions.Item(1).Arguments.Length -lt 30000 -and $task.Definition.Actions.Item(1).Arguments -notmatch ' -File |OptimusConsole|run.ps1') 'native code is embedded within Windows command-line limit'
Assert-GuardFixture ($task.Definition.Actions.Item(1).WorkingDirectory -ieq [Environment]::GetFolderPath('System') -and $bundle.context.scope_hash -ceq $hash) 'system working directory and fixed approved scope'
$errors=$null;[Management.Automation.Language.Parser]::ParseInput($bundle.body,[ref]$null,[ref]$errors)|Out-Null
Assert-GuardFixture (@($errors).Count -eq 0) 'complete compressed native action parses without executing it'
Assert-GuardFixture ($bundle.body -match "function Get-OptimusEdgeWinget \{ return \`$null \}" -and $bundle.body -match "scope -ceq 'machine'") 'per-user and winget native paths are disabled'
Assert-GuardFixture ($bundle.body -match "path.StartsWith\('HKLM:" -and $bundle.body -match '\[IO.FileMode\]::Open,\[IO.FileAccess\]::Read,\[IO.FileShare\]::None') 'inner Office inventory is HKLM-only and lease is existing/read-only'
Assert-GuardFixture ($bundle.body -match 'Appx\\Remove-AppxPackage -Package \$Package -Confirm:\$false -ErrorAction Stop' -and $bundle.body -match 'Dism\\Remove-AppxProvisionedPackage -Online -PackageName \$PackageName -ErrorAction Stop') 'fixed module-qualified AppX and DISM wrappers recheck approval immediately at native mutation boundary'
Assert-GuardFixture (-not(Test-Path -LiteralPath (Join-Path $data 'removal-guard')) -and @(Get-ChildItem -LiteralPath $data).Count -eq 2) 'no elevated code, owner record or output is written to writable Data'
Assert-GuardFixture (Test-OptimusGuardCurrentConsent $bundle.context) 'saved full effective approval matches protected scope'
$script:OptimusGuardEmbeddedBody=$bundle.body
Assert-GuardFixture (Test-OptimusGuardLiveApproval $bundle.context) 'current exact protected registered action is live'
$prefs.office365_desktop_debloat_enabled=$false;[IO.File]::WriteAllText($prefPath,($prefs|ConvertTo-Json -Depth 20))
Assert-GuardFixture (-not(Test-OptimusGuardCurrentConsent $bundle.context)) 'changed flag revokes even when stored old hash remains'
$prefs.office365_desktop_debloat_enabled=$true;$prefs.app_removal_exclusions+=@([pscustomobject]@{kind='desktop';id='new';name='new';label='new'})
[IO.File]::WriteAllText($prefPath,($prefs|ConvertTo-Json -Depth 20))
Assert-GuardFixture (-not(Test-OptimusGuardCurrentConsent $bundle.context)) 'new exclusion revokes old task immediately'
$prefs.app_removal_exclusions=@($prefs.app_removal_exclusions[0]);[IO.File]::WriteAllText($prefPath,($prefs|ConvertTo-Json -Depth 20))
$task.Enabled=$false;Assert-GuardFixture (-not(Test-OptimusGuardLiveApproval $bundle.context)) 'disabled task cannot use stale queued approval';$task.Enabled=$true
$original=$task.Sddl
foreach($sddl in @(('O:'+$script:Sid+'G:BAD:P(A;;GA;;;SY)(A;;GA;;;BA)(A;;GRGX;;;'+$script:Sid+')'),('O:BAG:BAD:P(A;;GA;;;SY)(A;;GA;;;BA)(A;;GA;;;'+$script:Sid+')'),('O:BAG:BAD:P(A;;GA;;;SY)(A;;GA;;;BA)(A;;GRGXWD;;;'+$script:Sid+')'))){$task.Sddl=$sddl;Assert-GuardFixture (-not(Test-OptimusGuardProtectedTask $task $script:Sid)) 'user ownership/write access never qualifies as protected';Assert-GuardRefused {Invoke-OptimusRemovalGuardRequest @{operation='disable';dataRoot=$data;launchManifest=$request.launchManifest}} 'foreign task cannot be disabled'}
$task.Sddl=$original
$old=$task.Definition.Actions.Item(1).Arguments;$task.Definition.Actions.Item(1).Arguments+='; evil'
Assert-GuardFixture (-not(Test-OptimusGuardLiveApproval $bundle.context)) 'extra action code revokes queued guard';$task.Definition.Actions.Item(1).Arguments=$old
$old=$task.Definition.RegistrationInfo.Source;$task.Definition.RegistrationInfo.Source='Optimus/RemovalGuard/1'
Assert-GuardRefused {Invoke-OptimusRemovalGuardRequest $request} 'old writable-code highest task is never upgraded as owned';$task.Definition.RegistrationInfo.Source=$old
$old=$task.Definition.Triggers.Item(2).StartBoundary;$task.Definition.Triggers.Item(2).StartBoundary='2099-01-01T00:00:00'
Assert-GuardRefused {Read-OptimusGuardTaskBundle $task $script:Sid $data} 'schedule drift is rejected';$task.Definition.Triggers.Item(2).StartBoundary=$old
$script:IsAdmin=$false;Assert-GuardRefused {Invoke-OptimusRemovalGuardRequest $request} 'registration requires elevation';$script:IsAdmin=$true
$request.extraScript='code';Assert-GuardRefused {Invoke-OptimusRemovalGuardRequest $request} 'caller cannot insert script code';$request.Remove('extraScript')
$old=$request.scopeHash;$request.scopeHash=('0'*64);Assert-GuardRefused {Invoke-OptimusRemovalGuardRequest $request} 'scope hash tampering is refused';$request.scopeHash=$old
$again=Invoke-OptimusRemovalGuardRequest $request
Assert-GuardFixture ($again.status.enabled -and $script:Registered[-1].flags -eq 20) 'owned update preserves DONT_ADD_PRINCIPAL_ACE'
Assert-GuardFixture (-not(Test-OptimusGuardLiveApproval $bundle.context)) 'old queued action cannot run after approved task replacement'
$request=@{operation='disable';dataRoot=$data;launchManifest=$request.launchManifest};$disabled=Invoke-OptimusRemovalGuardRequest $request
Assert-GuardFixture (-not $disabled.status.enabled -and $script:Deleted.Count -eq 1 -and $script:Launches -eq 0) 'disable only unregisters, never launches or stops a process'
Assert-GuardFixture ((ConvertTo-OptimusGuardCanonicalJson ([pscustomobject]@{a=@();b=@('one');c=@('one','two');d=([string][char]233)})) -ceq '{"a":[],"b":["one"],"c":["one","two"],"d":"\u00e9"}') 'canonical scope JSON matches Python ASCII arrays and escaping'
Assert-GuardRefused {Assert-OptimusGuardMachineExecutable $prefPath} 'user Data file cannot become an elevated executable'
Assert-OptimusGuardMachineExecutable (Join-Path ([Environment]::GetFolderPath('System')) 'msiexec.exe')
Assert-GuardFixture $true 'normal read-only Windows MSI servicing hardlinks pass protected-machine ACL checks'
$script:NativeMutationMask=0
function Get-Acl {
 param($LiteralPath,$ErrorAction)
 $acl=[pscustomobject]@{}
 $acl|Add-Member -MemberType ScriptMethod -Name GetOwner -Value {param($unused)[Security.Principal.SecurityIdentifier]::new('S-1-5-18')}
 $acl|Add-Member -MemberType ScriptMethod -Name GetAccessRules -Value {param($a,$b,$t)@([pscustomobject]@{AccessControlType='Allow';PropagationFlags=0;IdentityReference=[Security.Principal.SecurityIdentifier]::new('S-1-1-0');FileSystemRights=$script:NativeMutationMask})}
 return $acl
}
foreach($mask in @(1073741824,268435456,262144,65536,2,4,16,64,256,524288)){
 $script:NativeMutationMask=$mask
 Assert-GuardRefused {Assert-OptimusGuardMachineExecutable (Join-Path ([Environment]::GetFolderPath('System')) 'msiexec.exe')} ('untrusted native ACL mutation mask '+$mask+' is refused')
}
Remove-Item Function:Get-Acl
$runtimeAst=[Management.Automation.Language.Parser]::ParseInput((Get-OptimusRemovalGuardRuntimeText),[ref]$null,[ref]$null)
$oldLiveApproval=${function:Test-OptimusGuardLiveApproval}
function Test-OptimusGuardLiveApproval {return $false}
foreach($wrapperName in @('Remove-AppxPackage','Remove-AppxProvisionedPackage')){
 $wrapper=@($runtimeAst.EndBlock.Statements|Where-Object {$_ -is [Management.Automation.Language.FunctionDefinitionAst] -and $_.Name -ceq $wrapperName})
 Assert-GuardFixture ($wrapper.Count -eq 1) ('one fixed native wrapper for '+$wrapperName)
 . ([scriptblock]::Create($wrapper[0].Extent.Text))
}
$blocked=$false;try{Remove-AppxPackage -Package 'INERT NEVER REMOVE' -Confirm:$false -ErrorAction Stop}catch{$blocked=$_.Exception.Message -ceq 'Removal approval was revoked.'}
Assert-GuardFixture $blocked 'revoked AppX scope throws before resolving native removal cmdlet'
$blocked=$false;try{Remove-AppxProvisionedPackage -Online -PackageName 'INERT NEVER REMOVE' -ErrorAction Stop}catch{$blocked=$_.Exception.Message -ceq 'Removal approval was revoked.'}
Assert-GuardFixture $blocked 'revoked provisioned scope throws before resolving native DISM cmdlet'
Remove-Item Function:Remove-AppxPackage,Function:Remove-AppxProvisionedPackage
Set-Item Function:Test-OptimusGuardLiveApproval $oldLiveApproval
Write-Output ('PASS: '+$script:Assertions+' protected guard assertions; action '+$task.Definition.Actions.Item(1).Arguments.Length+' chars; fake scheduler only; fixture '+$fixture)
