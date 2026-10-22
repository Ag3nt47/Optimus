$ErrorActionPreference='Stop';Set-StrictMode -Version 2
Import-Module (Join-Path $PSScriptRoot 'SafeCleanup.psm1') -Force
Import-Module (Join-Path $PSScriptRoot 'AngeronaCleanup.psm1') -Force
Import-Module (Join-Path $PSScriptRoot 'TaskRegistration.psm1') -Force
Import-Module (Join-Path $PSScriptRoot 'StartupPermission.psm1') -Force
$fixture=Join-Path $PSScriptRoot ('fixture-'+[Guid]::NewGuid().ToString('N'))
[IO.Directory]::CreateDirectory($fixture)|Out-Null
$script:checks=0
function Assert-Cleanup($condition,$message){if(-not $condition){throw ('ASSERT: '+$message)};$script:checks++}
function New-OwnedFile([string]$path){[IO.Directory]::CreateDirectory((Split-Path $path -Parent))|Out-Null;[IO.File]::WriteAllText($path,'owned fixture');[IO.File]::SetCreationTimeUtc($path,[DateTime]::UtcNow.AddMinutes(-10));[IO.File]::SetLastWriteTimeUtc($path,[DateTime]::UtcNow.AddMinutes(-10));return $path}
function Process-Record([string]$Name,[string]$Command,[int]$ProcessNumber=100,[string]$Executable='C:\Python\python.exe',[int]$Parent=0,[DateTime]$Created=[DateTime]'2026-10-20T12:00:00Z'){
    return [pscustomobject]@{Name=$Name;CommandLine=$Command;ExecutablePath=$Executable;ProcessId=$ProcessNumber;ParentProcessId=$Parent;CreationDate=$Created}
}
$repo=Join-Path $fixture 'AngeronaSuite';[IO.Directory]::CreateDirectory((Join-Path $repo 'src\angerona'))|Out-Null
[IO.File]::WriteAllText((Join-Path $repo 'src\angerona\app.py'),'# fixture')
$temps=Join-Path $fixture 'runtime-temp';[IO.Directory]::CreateDirectory($temps)|Out-Null
$held=$null
try{
    Assert-Cleanup ([bool](Get-Command Write-CleanupJson -ErrorAction SilentlyContinue)) 'Atomic report writer is exported.'
    $old=New-OwnedFile (Join-Path $temps 'old.tmp')
    $locked=New-OwnedFile (Join-Path $temps 'locked.tmp');$held=[IO.File]::Open($locked,[IO.FileMode]::Open,[IO.FileAccess]::ReadWrite,[IO.FileShare]::None)
    $readOnly=New-OwnedFile (Join-Path $temps 'readonly.tmp');[IO.File]::SetAttributes($readOnly,[IO.FileAttributes]::ReadOnly)
    $outside=New-OwnedFile (Join-Path $fixture 'outside\evidence.txt')
    $hardlink=Join-Path $temps 'linked.tmp';New-Item -ItemType HardLink -Path $hardlink -Target $outside|Out-Null
    $junction=Join-Path $temps 'junction';New-Item -ItemType Junction -Path $junction -Target (Split-Path $outside -Parent)|Out-Null
    $future=New-OwnedFile (Join-Path $temps 'future.tmp');[IO.File]::SetLastWriteTimeUtc($future,[DateTime]::UtcNow.AddMinutes(10))
    $audit=Invoke-SafeCleanup -Root $temps
    Assert-Cleanup ($audit.deleted_files -eq 0 -and [IO.File]::Exists($old)) 'Read-only audit never deletes.'
    $apply=Invoke-SafeCleanup -Root $temps -Apply
    Assert-Cleanup ($apply.deleted_files -eq 1 -and -not [IO.File]::Exists($old)) 'Only verified disposable temp was removed.'
    foreach($file in @($locked,$readOnly,$outside,$hardlink,$future)){Assert-Cleanup ([IO.File]::Exists($file)) ('Protected file retained: '+[IO.Path]::GetFileName($file))}
    Assert-Cleanup ($apply.reasons['multiple-hard-links'] -eq 1) 'Hard links rejected.'
    Assert-Cleanup ($apply.reasons['locked-or-inaccessible'] -eq 1) 'Busy file rejected.'
    $logs=Join-Path $fixture 'logs';$diagnostic=New-OwnedFile (Join-Path $logs 'startup-dashboard.log');$security=New-OwnedFile (Join-Path $logs 'security.log');$nested=New-OwnedFile (Join-Path $logs 'evidence\crash.log')
    $logClean=Invoke-SafeCleanup -Root $logs -Apply -FileNames @('startup-dashboard.log','crash.log')
    Assert-Cleanup (-not [IO.File]::Exists($diagnostic)) 'Exact top-level disposable diagnostic is eligible.'
    Assert-Cleanup ([IO.File]::Exists($security) -and [IO.File]::Exists($nested)) 'Security logs and nested evidence are excluded.'
    $interrupted=New-OwnedFile (Join-Path $fixture 'interrupted\file.tmp')
    $stop=Invoke-SafeCleanup -Root (Split-Path $interrupted -Parent) -Apply -ContinueCheck {$false}
    Assert-Cleanup ([IO.File]::Exists($interrupted) -and $stop.reasons['runtime-started'] -eq 1) 'A restarted runtime stops deletion.'
    $explorer=Process-Record 'explorer.exe' 'C:\Windows\explorer.exe' 1 'C:\Windows\explorer.exe'
    Assert-Cleanup (Get-CleanupProcessState -RepositoryRoot $repo -Records @($explorer)).safe 'Complete unrelated inventory is safe.'
    Assert-Cleanup (-not (Get-CleanupProcessState -RepositoryRoot $repo -Records @()).safe) 'Empty inventory is not proof of exit.'
    foreach($command in @('python.exe -m angerona --chill','python.exe -B -m angerona.startup','python.exe -c "from angerona.app import App"')){
        $state=Get-CleanupProcessState -RepositoryRoot $repo -Records @((Process-Record 'python.exe' $command))
        Assert-Cleanup ($state.active -and -not $state.safe) ('Live module/import blocked: '+$command)
    }
    $frozen=Get-CleanupProcessState -RepositoryRoot $repo -Records @((Process-Record 'Angerona.exe' 'D:\Angerona.exe'))
    Assert-Cleanup ($frozen.active -and -not $frozen.safe) 'Frozen runtime blocks cleanup.'
    $venv=Join-Path $repo 'venv\Scripts\python.exe'
    $opt=Get-CleanupProcessState -RepositoryRoot $repo -Records @((Process-Record 'python.exe' ('"'+$venv+'" -B "D:\Agent47\Optimus\App\source\launch.py"') 2 $venv))
    Assert-Cleanup $opt.safe 'Known Optimus source launcher sharing venv is not Angerona.'
    foreach($argument in @('-m tools.ci_pytest','.tmp\run_selfcheck_after_final.py','-m pytest','-c "from multiprocessing.spawn import spawn_main;spawn_main()"')){
        $state=Get-CleanupProcessState -RepositoryRoot $repo -Records @((Process-Record 'python.exe' ('"'+$venv+'" '+$argument) 2 $venv))
        Assert-Cleanup ($state.uncertain -and -not $state.safe) ('Unknown/shared-venv QA worker defers: '+$argument)
        $base=Get-CleanupProcessState -RepositoryRoot $repo -Records @((Process-Record 'python.exe' ('"'+$venv+'" '+$argument) 2 'C:\Python\python.exe'))
        Assert-Cleanup (-not $base.safe) 'Base interpreter preserving venv command origin remains blocked.'
    }
    Assert-Cleanup (-not (Get-CleanupProcessState -RepositoryRoot $repo -Records @((Process-Record 'python.exe' ''))).safe) 'Restricted/unreadable Python metadata defers.'
    $tracked=@{};$birth=[DateTime]'2026-10-20T12:00:00Z'
    $parent=Process-Record 'python.exe' 'python.exe -m angerona' 200 'C:\Python\python.exe' 0 $birth
    $child=Process-Record 'python.exe' 'python.exe -c "from multiprocessing.spawn import spawn_main;spawn_main()"' 201 'C:\Python\python.exe' 200 ($birth.AddSeconds(1))
    $family=Get-CleanupProcessState -RepositoryRoot $repo -Records @($parent,$child) -TrackedProcesses $tracked
    Assert-Cleanup ($family.matches.Count -eq 2) 'Observed descendant is tracked by identity.'
    $orphan=Get-CleanupProcessState -RepositoryRoot $repo -Records @($child) -TrackedProcesses $tracked
    Assert-Cleanup ($orphan.active -and -not $orphan.safe) 'Child keeps cleanup blocked after parent exits.'
    $reused=Process-Record 'explorer.exe' 'explorer.exe' 201 'C:\Windows\explorer.exe' 0 ($birth.AddSeconds(20))
    Assert-Cleanup (Get-CleanupProcessState -RepositoryRoot $repo -Records @($reused) -TrackedProcesses $tracked).safe 'PID reuse does not inherit old child ownership.'
    $scopes=@(Get-CleanupScopes -RepositoryRoot $repo)
    Assert-Cleanup ($scopes.Count -eq 12) 'Only four explicit profiles with three scope types each.'
    Assert-Cleanup (@($scopes|Where-Object{$_.root.StartsWith($repo+'\',[StringComparison]::OrdinalIgnoreCase)}).Count -eq 0) 'Repository .tmp/build/venv/QA paths are absent.'
    Assert-Cleanup (@($scopes|Where-Object{$_.names -contains 'runtime_alerts.log' -or $_.root -match 'evidence|shared_logs|shadow_cache|repair-wheels'}).Count -eq 0) 'Durable security/evidence/recovery paths are excluded.'
    $flowFile=New-OwnedFile (Join-Path $fixture 'orchestration\owned.tmp')
    $flow=& (Get-Module AngeronaCleanup) {
        param($Repository,$FixtureScope)
        $savedState=(Get-Item function:Get-CleanupProcessState).ScriptBlock
        $savedScopes=(Get-Item function:Get-CleanupScopes).ScriptBlock
        try{
            Set-Item function:script:Get-CleanupProcessState -Value {param($RepositoryRoot) [pscustomobject]@{safe=$true;active=$false;uncertain=$false;reason='Fixture-only no-runtime proof';matches=@();unreadable_processes=@()}}
            Set-Item function:script:Get-CleanupScopes -Value ({param($RepositoryRoot) @([pscustomobject]@{root=$FixtureScope;kind='runtime-temp';names=@()})}.GetNewClosure())
            Invoke-AngeronaCleanup -RepositoryRoot $Repository -Apply
        }finally{Set-Item function:script:Get-CleanupProcessState -Value $savedState;Set-Item function:script:Get-CleanupScopes -Value $savedScopes}
    } $repo (Split-Path $flowFile -Parent)
    Assert-Cleanup ($flow.completed -and $flow.deleted_bytes -gt 0 -and -not [IO.File]::Exists($flowFile)) 'Production orchestration + runtime closure deletes only explicit fixture scope.'
    $blockFile=New-OwnedFile (Join-Path $fixture 'deferred\owned.tmp')
    $deferred=& (Get-Module AngeronaCleanup) {
        param($Repository)
        $savedState=(Get-Item function:Get-CleanupProcessState).ScriptBlock
        try{
            Set-Item function:script:Get-CleanupProcessState -Value {param($RepositoryRoot) [pscustomobject]@{safe=$false;active=$true;uncertain=$false;reason='Fixture runtime active';matches=@();unreadable_processes=@()}}
            Invoke-AngeronaCleanup -RepositoryRoot $Repository -Apply
        }finally{Set-Item function:script:Get-CleanupProcessState -Value $savedState}
    } $repo
    Assert-Cleanup (-not $deferred.completed -and $deferred.mode -eq 'apply-deferred' -and [IO.File]::Exists($blockFile)) 'Production orchestration never considers scopes while a runtime is active.'
    $hostPath=Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe'
    $installPath=Join-Path $fixture 'install-only-preview'
    $preview=& $hostPath -NoLogo -NoProfile -NonInteractive -ExecutionPolicy RemoteSigned -File (Join-Path $PSScriptRoot 'Install-AngeronaCleanup.ps1') -RepositoryRoot $repo -InstallDirectory $installPath -ValidateOnly
    Assert-Cleanup ($LASTEXITCODE -eq 0) 'Installer validates without registering a task.'
    $description=($preview -join "`n")|ConvertFrom-Json;$xml=[xml]$description.task_xml
    $ns=New-Object Xml.XmlNamespaceManager($xml.NameTable);$ns.AddNamespace('t','http://schemas.microsoft.com/windows/2004/02/mit/task')
    Assert-Cleanup (-not (Test-Path -LiteralPath $installPath)) 'ValidateOnly creates no installation.'
    Assert-Cleanup ($xml.SelectSingleNode('//t:Principal/t:RunLevel',$ns).InnerText -eq 'LeastPrivilege') 'Watcher never requests elevation.'
    Assert-Cleanup ($xml.SelectSingleNode('//t:Principal/t:LogonType',$ns).InnerText -eq 'InteractiveToken') 'Watcher is confined to signed-in user.'
    Assert-Cleanup ($xml.SelectNodes('//t:LogonTrigger',$ns).Count -eq 1 -and $xml.SelectNodes('//t:TimeTrigger',$ns).Count -eq 1) 'Task covers sign-in and current session.'
    $testSid=[Security.Principal.WindowsIdentity]::GetCurrent().User.Value
    $taskConfig=[pscustomobject]@{owner_sid=$testSid;installation_id=('a'*32);task_name=('AngeronaDisposableCleanup-'+$testSid);install_directory='D:\Agent47\AngeronaCleanup';task_start='2026-10-20T12:00:00'}
    $action=[pscustomobject]@{Type=0;Path=(Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe');Arguments=('-NoLogo -NoProfile -NonInteractive -WindowStyle Hidden -ExecutionPolicy RemoteSigned -File "'+(Join-Path $taskConfig.install_directory 'Run-AngeronaCleanup.ps1')+'" -Mode Watch');WorkingDirectory=$taskConfig.install_directory}
    $actions=[pscustomobject]@{Count=1;Entries=@($action)};$actions|Add-Member ScriptMethod Item {param($number) return $this.Entries[$number-1]}
    $logon=[pscustomobject]@{Type=9;UserId=$testSid;Delay='PT1M'};$timer=[pscustomobject]@{Type=1;StartBoundary=$taskConfig.task_start}
    $triggers=[pscustomobject]@{Count=2;Entries=@($logon,$timer)};$triggers|Add-Member ScriptMethod Item {param($number) return $this.Entries[$number-1]}
    $definition=[pscustomobject]@{RegistrationInfo=[pscustomobject]@{Source='AngeronaCleanup/1';Description=('Angerona disposable cleanup v1 '+$taskConfig.installation_id)};Actions=$actions;Triggers=$triggers;Principal=[pscustomobject]@{UserId=$testSid;LogonType=3;RunLevel=0};Settings=[pscustomobject]@{MultipleInstances=2}}
    $task=[pscustomobject]@{Path=('\'+$taskConfig.task_name);Definition=$definition}
    Assert-Cleanup (Test-CleanupTaskOwnership -Task $task -Configuration $taskConfig) 'Exact private task ownership fixture accepted.'
    $currentAccount=[Security.Principal.WindowsIdentity]::GetCurrent().Name
    $shortAccount=($currentAccount -split '\\')[-1]
    $definition.Principal.UserId=$shortAccount;$logon.UserId=$currentAccount
    Assert-Cleanup (Test-CleanupTaskOwnership -Task $task -Configuration $taskConfig) 'Windows normalized short principal and qualified logon resolve to the current SID.'
    $definition.Principal.UserId='SYSTEM'
    Assert-Cleanup (-not (Test-CleanupTaskOwnership -Task $task -Configuration $taskConfig)) 'An actual different Windows SID is rejected.'
    $definition.Principal.UserId='AngeronaCleanup-NoSuchAccount-'+[Guid]::NewGuid().ToString('N')
    Assert-Cleanup (-not (Test-CleanupTaskOwnership -Task $task -Configuration $taskConfig)) 'Unresolvable account names fail closed.'
    $definition.Principal.UserId=$testSid;$logon.UserId=$testSid
    $action.Arguments+=' -Unexpected'
    Assert-Cleanup (-not (Test-CleanupTaskOwnership -Task $task -Configuration $taskConfig)) 'Task action tampering blocks removal/rollback.'
    $action.Arguments=$action.Arguments.Replace(' -Unexpected','');$definition.Principal.RunLevel=1
    Assert-Cleanup (-not (Test-CleanupTaskOwnership -Task $task -Configuration $taskConfig)) 'Changed elevated principal blocks ownership.'
    $definition.Principal.RunLevel=0;$definition.RegistrationInfo.Description='Foreign task'
    Assert-Cleanup (-not (Test-CleanupTaskOwnership -Task $task -Configuration $taskConfig)) 'Foreign description blocks task ownership.'
    $definition.RegistrationInfo.Description='Angerona disposable cleanup v1 '+$taskConfig.installation_id;$timer.StartBoundary='2099-01-01T00:00:00'
    Assert-Cleanup (-not (Test-CleanupTaskOwnership -Task $task -Configuration $taskConfig)) 'Changed task schedule blocks ownership.'
    $absent=Get-RegisteredCleanupTask -Name ('AngeronaCleanup-FIXTURE-ABSENT-'+[Guid]::NewGuid().ToString('N'))
    Assert-Cleanup ($null -eq $absent) 'Native missing-task read is absence, not an error or mutation.'
    $watchSource=[IO.File]::ReadAllText((Join-Path $PSScriptRoot 'Run-AngeronaCleanup.ps1'))
    $signalSource=[regex]::Match($watchSource,"(?s)Add-Type -TypeDefinition @'\r?\n(.*?)\r?\n'@").Groups[1].Value
    Add-Type -TypeDefinition $signalSource
    [AngeronaCleanup.SessionSignal]::Start()
    Assert-Cleanup ([AngeronaCleanup.SessionSignal]::Listening -is [bool]) 'SessionEnding signal compiles/subscribes without a cleanup callback.'
    $actualIdentity=Get-CleanupIdentity
    Assert-Cleanup ($actualIdentity.Alive -and $actualIdentity.Pid -eq $PID -and $actualIdentity.Sid -eq $testSid) 'Native limited-rights query proves current process token identity.'
    foreach($case in @(
        @('Audit',$false,$false,'manual-no-request'),@('Apply',$false,$false,'manual-no-request'),
        @('Watch',$true,$false,'already-administrator'),@('Watch',$true,$true,'already-administrator'),
        @('Watch',$false,$true,'standard-loop-guard'),@('Watch',$false,$false,'request-once')
    )){Assert-Cleanup ((Get-CleanupElevationDecision $case[0] $case[1] $case[2]) -eq $case[3]) ('Elevation routing: '+$case[3])}
    Assert-Cleanup ([bool](Get-Command Test-CleanupLeaseFree -ErrorAction SilentlyContinue)) 'Fallback lease helper is exported for script entry.'
    $permissionRoot=Join-Path $fixture 'permission-install';[IO.Directory]::CreateDirectory($permissionRoot)|Out-Null
    $permissionConfig=[pscustomobject]@{schema=1;owner_sid=$testSid;installation_id=('b'*32);install_directory=$permissionRoot;repository_root=$repo;request_admin_on_startup=$true}
    Write-CleanupJson -Path (Join-Path $permissionRoot 'settings.json') -Value $permissionConfig
    Write-CleanupJson -Path (Join-Path $permissionRoot 'scope-authority.json') -Value @{schema=1;owner_sid=$testSid;installation_id=$permissionConfig.installation_id;install_directory=$permissionRoot;repository_root=$repo}
    Assert-Cleanup ((Read-CleanupConfiguration $permissionRoot).repository_root -eq $repo) 'Ordinary same-account scope authority loads.'
    $changedConfig=$permissionConfig|Select-Object *;$changedConfig.repository_root='D:\Other\AngeronaSuite'
    Write-CleanupJson -Path (Join-Path $permissionRoot 'settings.json') -Value $changedConfig
    $scopeRejected=$false;try{Read-CleanupConfiguration $permissionRoot|Out-Null}catch{$scopeRejected=$true}
    Assert-Cleanup $scopeRejected 'A config-only repository redirection fails after elevation too.'
    Write-CleanupJson -Path (Join-Path $permissionRoot 'settings.json') -Value $permissionConfig
    $count=@{launches=0}
    $decliner={param($HostPath,$Arguments,$TicketPath) $count.launches++;throw 'Fixture Windows consent declined'}.GetNewClosure()
    $denied=Request-CleanupStartupElevation -Configuration $permissionConfig -Launcher $decliner
    Assert-Cleanup ($count.launches -eq 1 -and $denied.action -eq 'fallback' -and -not $denied.administrator) 'Decline causes exactly one launch attempt and standard fallback.'
    Assert-Cleanup (@(Get-ChildItem -LiteralPath (Join-Path $permissionRoot 'Handoffs') -Filter '*.cancel.json').Count -eq 1) 'Declined launch cancels its private handoff ticket.'
    $readyFixture=[pscustomobject]@{schema=1;nonce=('c'*32);installation_id=$permissionConfig.installation_id;owner_sid=$testSid;state='elevated-ready';child_pid=9999;child_created='2026-10-20T12:00:00.0000000Z'}
    $ticketFixture=[pscustomobject]@{nonce=$readyFixture.nonce;installation_id=$permissionConfig.installation_id;owner_sid=$testSid;parent_session=$actualIdentity.Session}
    $identityFixture=[pscustomobject]@{Pid=9999;Alive=$true;Administrator=$true;Sid=$testSid;Session=$actualIdentity.Session;Image=$hostPath;Created=$readyFixture.child_created}
    Assert-Cleanup (Test-CleanupReady $readyFixture $ticketFixture $identityFixture 9999 $hostPath) 'Readiness requires matching actual token/lifetime/session/image.'
    foreach($field in @('Administrator','Sid','Session','Image','Created','Pid','Alive')){
        $tampered=$identityFixture|Select-Object *
        switch($field){Administrator{$tampered.Administrator=$false};Sid{$tampered.Sid='S-1-5-18'};Session{$tampered.Session=-1};Image{$tampered.Image='C:\Windows\notepad.exe'};Created{$tampered.Created='stale'};Pid{$tampered.Pid=9998};Alive{$tampered.Alive=$false}}
        Assert-Cleanup (-not (Test-CleanupReady $readyFixture $ticketFixture $tampered 9999 $hostPath)) ('Ready rejects token/process mismatch: '+$field)
    }
    Assert-Cleanup (Request-CleanupWatcherStop $permissionConfig 1) 'Same-install stop request succeeds when watcher lease is free.'
    Assert-Cleanup (Test-CleanupStopRequested $permissionConfig) 'Fresh stop request is recognized.'
    $otherConfig=$permissionConfig|Select-Object *;$otherConfig.installation_id=('d'*32)
    Assert-Cleanup (-not (Test-CleanupStopRequested $otherConfig)) 'A different installation cannot reuse a stale stop request.'
    [IO.File]::Delete((Join-Path $permissionRoot 'stop-request.json'))
    $handoffState=@{launches=0;lease=$null;child=$null;ack=$null}
    $launchReady={param($HostExecutable,$Arguments,$TicketPath)
        $handoffState.launches++;$handoffTicket=Read-CleanupPrivateJson $TicketPath
        $handoffState.child=[pscustomobject]@{Pid=99991;Alive=$true;Administrator=$true;Sid=$handoffTicket.owner_sid;Session=$handoffTicket.parent_session;Image=$HostExecutable;Created='2026-10-20T12:00:00.0000000Z'}
        $handoffState.lease=[IO.File]::Open((Join-Path $permissionRoot 'watcher.lock'),[IO.FileMode]::OpenOrCreate,[IO.FileAccess]::ReadWrite,[IO.FileShare]::None)
        $readyPath=$TicketPath.Replace('.ticket.json','.ready.json');$handoffState.ack=$TicketPath.Replace('.ticket.json','.ack.json')
        Write-CleanupJson -Path $readyPath -Value @{schema=1;nonce=$handoffTicket.nonce;installation_id=$handoffTicket.installation_id;owner_sid=$handoffTicket.owner_sid;state='elevated-ready';child_pid=99991;child_created=$handoffState.child.Created}
        return [pscustomobject]@{Id=99991}
    }.GetNewClosure()
    $probeReady={param($ProcessNumber) if($ProcessNumber -eq 99991){return $handoffState.child};return $actualIdentity}.GetNewClosure()
    try{$accepted=Request-CleanupStartupElevation -Configuration $permissionConfig -Launcher $launchReady -ProcessProbe $probeReady}
    finally{if($handoffState.lease){$handoffState.lease.Dispose()}}
    Assert-Cleanup ($accepted.action -eq 'handed-off' -and $accepted.administrator -and $handoffState.launches -eq 1) 'Parent accepts exactly one ready child only while its watcher lease is held.'
    Assert-Cleanup ((Read-CleanupPrivateJson $handoffState.ack).accepted -eq $true) 'Accepted handoff writes the nonce-bound child acknowledgement.'
    $childNonce=[Guid]::NewGuid().ToString('N');$childPrefix=Join-Path (Join-Path $permissionRoot 'Handoffs') $childNonce
    $childTicket=@{schema=1;nonce=$childNonce;installation_id=$permissionConfig.installation_id;owner_sid=$testSid;install_directory=$permissionRoot;parent_pid=777;parent_created='2026-10-20T12:00:00.0000000Z';parent_session=$actualIdentity.Session;expires_utc=[DateTime]::UtcNow.AddMinutes(5).ToString('o');config_sha256=(Get-FileHash -LiteralPath (Join-Path $permissionRoot 'settings.json')).Hash.ToLowerInvariant();authority_sha256=(Get-FileHash -LiteralPath (Join-Path $permissionRoot 'scope-authority.json')).Hash.ToLowerInvariant()}
    Write-CleanupJson -Path ($childPrefix+'.ticket.json') -Value $childTicket
    $childState=@{parent=[pscustomobject]@{Pid=777;Alive=$true;Administrator=$false;Sid=$testSid;Session=$actualIdentity.Session;Image=$hostPath;Created=$childTicket.parent_created};child=[pscustomobject]@{Pid=$PID;Alive=$true;Administrator=$true;Sid=$testSid;Session=$actualIdentity.Session;Image=$hostPath;Created=$actualIdentity.Created}}
    function Invoke-ChildTicketFixture($Configuration,$TicketPath,$Metadata,[bool]$Confirm=$false){
        return (& (Get-Module StartupPermission) {
            param($Cfg,$Path,$Meta,$DoConfirm)
            $original=(Get-Item function:Get-CleanupIdentity).ScriptBlock
            try{
                Set-Item function:script:Get-CleanupIdentity -Value ({param($ProcessNumber=$PID) if($ProcessNumber -eq 777){return $Meta.parent};return $Meta.child}.GetNewClosure())
                $context=Read-CleanupStartupTicket $Path $Cfg
                if($DoConfirm){return (Confirm-CleanupChildReady $context $Cfg)}
                return $context
            }finally{Set-Item function:script:Get-CleanupIdentity -Value $original}
        } $Configuration $TicketPath $Metadata $Confirm)
    }
    $validChild=Invoke-ChildTicketFixture $permissionConfig ($childPrefix+'.ticket.json') $childState
    Assert-Cleanup ($validChild.ticket.nonce -eq $childNonce) 'Child revalidates the approved private scope/config and parent lifetime.'
    foreach($failure in @('nonadmin','other-sid','expired','cancelled','changed-config','parent-created')){
        $savedAdmin=$childState.child.Administrator;$savedSid=$childState.child.Sid;$savedParent=$childState.parent.Created;$savedExpiry=$childTicket.expires_utc
        switch($failure){nonadmin{$childState.child.Administrator=$false};other-sid{$childState.child.Sid='S-1-5-18'};expired{$childTicket.expires_utc=[DateTime]::UtcNow.AddMinutes(-1).ToString('o');Write-CleanupJson -Path ($childPrefix+'.ticket.json') -Value $childTicket};cancelled{Write-CleanupJson -Path ($childPrefix+'.cancel.json') -Value @{cancelled=$true}};changed-config{Write-CleanupJson -Path (Join-Path $permissionRoot 'settings.json') -Value @{changed=$true}};parent-created{$childState.parent.Created='stale'}}
        $rejected=$false;try{Invoke-ChildTicketFixture $permissionConfig ($childPrefix+'.ticket.json') $childState|Out-Null}catch{$rejected=$true}
        Assert-Cleanup $rejected ('Child refuses unsafe handoff: '+$failure)
        $childState.child.Administrator=$savedAdmin;$childState.child.Sid=$savedSid;$childState.parent.Created=$savedParent;$childTicket.expires_utc=$savedExpiry
        Write-CleanupJson -Path ($childPrefix+'.ticket.json') -Value $childTicket
        Write-CleanupJson -Path (Join-Path $permissionRoot 'settings.json') -Value $permissionConfig
        if([IO.File]::Exists($childPrefix+'.cancel.json')){[IO.File]::Delete($childPrefix+'.cancel.json')}
    }
    Write-CleanupJson -Path ($childPrefix+'.ack.json') -Value @{schema=1;nonce=$childNonce;installation_id=$permissionConfig.installation_id;child_pid=$PID;accepted=$true}
    Assert-Cleanup (Invoke-ChildTicketFixture $permissionConfig ($childPrefix+'.ticket.json') $childState $true) 'Child readiness waits for the matching parent acknowledgement before work.'
    $linkedState=Join-Path $permissionRoot 'linked-state.json';New-Item -ItemType HardLink -Path $linkedState -Target (Join-Path $permissionRoot 'settings.json')|Out-Null
    $linkedRejected=$false;try{Read-CleanupPrivateJson $linkedState|Out-Null}catch{$linkedRejected=$true}
    Assert-Cleanup $linkedRejected 'Private handoff/config JSON rejects hardlinks.'
    [IO.File]::Delete($linkedState)
    # Run the actual entry script with isolated dependency doubles. The launcher
    # double records an attempt; no real UAC, WMI target cleanup, or task runs.
    $entryRoot=Join-Path $fixture 'entry-fixture';[IO.Directory]::CreateDirectory($entryRoot)|Out-Null
    [IO.File]::WriteAllBytes((Join-Path $entryRoot 'Run-AngeronaCleanup.ps1'),[IO.File]::ReadAllBytes((Join-Path $PSScriptRoot 'Run-AngeronaCleanup.ps1')))
    [IO.File]::WriteAllBytes((Join-Path $entryRoot 'SafeCleanup.psm1'),[IO.File]::ReadAllBytes((Join-Path $PSScriptRoot 'SafeCleanup.psm1')))
    $entryConfig=$permissionConfig|Select-Object *;$entryConfig.install_directory=$entryRoot
    Write-CleanupJson -Path (Join-Path $entryRoot 'settings.json') -Value $entryConfig
    $permissionDouble=@'
$script:stopCount=0
function Read-CleanupConfiguration($InstallationRoot){Get-Content -LiteralPath (Join-Path $InstallationRoot 'settings.json') -Raw -Encoding UTF8|ConvertFrom-Json}
function Get-CleanupIdentity{[pscustomobject]@{Administrator=$false;Sid=[Security.Principal.WindowsIdentity]::GetCurrent().User.Value;Session=1;Created='fixture';Alive=$true;Pid=$PID}}
function Get-CleanupElevationDecision($Mode,$Administrator,$AdminRelaunch){if($Mode -ne 'Watch'){'manual-no-request'}elseif($AdminRelaunch){'standard-loop-guard'}else{'request-once'}}
function Test-CleanupLeaseFree($Path){return $true}
function Request-CleanupStartupElevation($Configuration){[IO.File]::AppendAllText((Join-Path $PSScriptRoot 'mock-attempts.txt'),'once');return @{action='fallback';state='declined-fixture';administrator=$false;reason='Fixture decline; continue standard'}}
function Test-CleanupStopRequested($Configuration){$script:stopCount++;return (-not [IO.File]::Exists((Join-Path $PSScriptRoot 'allow-fixture-apply')))}
Export-ModuleMember -Function Read-CleanupConfiguration,Get-CleanupIdentity,Get-CleanupElevationDecision,Test-CleanupLeaseFree,Request-CleanupStartupElevation,Test-CleanupStopRequested
'@
    [IO.File]::WriteAllText((Join-Path $entryRoot 'StartupPermission.psm1'),$permissionDouble)
    $engineDouble=@'
Import-Module (Join-Path $PSScriptRoot 'SafeCleanup.psm1')
function Invoke-AngeronaCleanup($RepositoryRoot,[switch]$Apply,$ContinueCheck){if($Apply){$result=Invoke-SafeCleanup -Root (Join-Path $PSScriptRoot 'owned-runtime') -Apply -ContinueCheck $ContinueCheck;return [pscustomobject]@{mode='apply';completed=$true;deleted_bytes=$result.deleted_bytes}};return [pscustomobject]@{mode='audit';completed=$true;deleted_bytes=0}}
function Get-CleanupProcessState($RepositoryRoot){throw 'Fixture should stop before any live inventory'}
Export-ModuleMember -Function Invoke-AngeronaCleanup,Get-CleanupProcessState
'@
    [IO.File]::WriteAllText((Join-Path $entryRoot 'AngeronaCleanup.psm1'),$engineDouble)
    $auditEntry=& $hostPath -NoLogo -NoProfile -NonInteractive -ExecutionPolicy RemoteSigned -File (Join-Path $entryRoot 'Run-AngeronaCleanup.ps1') -Mode Audit
    Assert-Cleanup ($LASTEXITCODE -eq 0 -and -not [IO.File]::Exists((Join-Path $entryRoot 'mock-attempts.txt'))) 'Actual Audit entry performs no administrator launch.'
    $watchEntry=& $hostPath -NoLogo -NoProfile -NonInteractive -ExecutionPolicy RemoteSigned -File (Join-Path $entryRoot 'Run-AngeronaCleanup.ps1') -Mode Watch
    Assert-Cleanup ($LASTEXITCODE -eq 0 -and [IO.File]::ReadAllText((Join-Path $entryRoot 'mock-attempts.txt')) -eq 'once') 'Actual Watch entry survives one mocked denial without retrying.'
    $entryStatus=Read-CleanupPrivateJson (Join-Path $entryRoot 'status.json')
    Assert-Cleanup ($entryStatus.permission.state -eq 'declined-fixture' -and -not $entryStatus.permission.administrator) 'Actual denial fallback reports its standard access truthfully.'
    [IO.File]::WriteAllText((Join-Path $entryRoot 'allow-fixture-apply'),'fixture only')
    $entryTemp=New-OwnedFile (Join-Path $entryRoot 'owned-runtime\delete-me.tmp')
    $applyEntry=& $hostPath -NoLogo -NoProfile -NonInteractive -ExecutionPolicy RemoteSigned -File (Join-Path $entryRoot 'Run-AngeronaCleanup.ps1') -Mode Apply
    Assert-Cleanup ($LASTEXITCODE -eq 0 -and -not [IO.File]::Exists($entryTemp)) 'Actual Apply entry invokes the captured stop-check callback and deletes only fixture temp.'
    Assert-Cleanup ([IO.File]::ReadAllText((Join-Path $entryRoot 'mock-attempts.txt')) -eq 'once') 'Manual Apply never adds an administrator request.'
    foreach($file in Get-ChildItem -LiteralPath $PSScriptRoot -File|Where-Object{$_.Extension -in @('.ps1','.psm1')}){
        $tokens=$null;$parseErrors=$null;[Management.Automation.Language.Parser]::ParseFile($file.FullName,[ref]$tokens,[ref]$parseErrors)|Out-Null
        Assert-Cleanup (@($parseErrors).Count -eq 0) ('Parses on native PowerShell: '+$file.Name)
    }
    $proof=@{status='passed';checks=$script:checks;ps_version=$PSVersionTable.PSVersion.ToString();fixture_only=$true;runtime_cleanup_performed=$false;task_registered=$false;environment_changed=$false;utc=[DateTime]::UtcNow.ToString('o')}
    Write-CleanupJson -Path (Join-Path $PSScriptRoot 'test-results.json') -Value $proof
    $proof|ConvertTo-Json
}finally{
    if($held){$held.Dispose()}
    if(Test-Path -LiteralPath (Join-Path $temps 'junction')){[IO.Directory]::Delete((Join-Path $temps 'junction'))}
    if([IO.File]::Exists((Join-Path $temps 'readonly.tmp'))){[IO.File]::SetAttributes((Join-Path $temps 'readonly.tmp'),[IO.FileAttributes]::Normal)}
    if([IO.Path]::GetFullPath($fixture).StartsWith([IO.Path]::GetFullPath($PSScriptRoot).TrimEnd('\')+'\fixture-',[StringComparison]::OrdinalIgnoreCase)){Remove-Item -LiteralPath $fixture -Recurse -Force}
}
