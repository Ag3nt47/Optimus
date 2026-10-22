[CmdletBinding()]
param([Parameter(Mandatory=$true)][string]$RepositoryRoot,[string]$InstallDirectory='D:\Agent47\AngeronaCleanup',[switch]$ValidateOnly)
$ErrorActionPreference='Stop';Set-StrictMode -Version 2
Import-Module (Join-Path $PSScriptRoot 'TaskRegistration.psm1') -Force
$repo=[IO.Path]::GetFullPath($RepositoryRoot).TrimEnd('\')
$install=[IO.Path]::GetFullPath($InstallDirectory).TrimEnd('\')
if($install -notmatch '^D:\\.+[^\\]$' -or $install -match '["\r\n]' -or $repo -notmatch '^[A-Za-z]:\\' -or -not [IO.File]::Exists((Join-Path $repo 'src\angerona\app.py'))){throw 'Use an ordinary D: installation directory and the verified Angerona source repository.'}
foreach($path in @($install,$repo)){
    $part=$path
    while($part){if((Test-Path -LiteralPath $part) -and ((Get-Item -LiteralPath $part -Force).Attributes -band [IO.FileAttributes]::ReparsePoint)){throw 'Indirect installation/repository paths are unsupported.'};$part=Split-Path -Path $part -Parent}
}
if($install.StartsWith($repo+'\',[StringComparison]::OrdinalIgnoreCase) -or $repo.StartsWith($install+'\',[StringComparison]::OrdinalIgnoreCase) -or $install -ieq $repo){throw 'Keep the cleanup installation separate from the Angerona repository.'}
$sid=[Security.Principal.WindowsIdentity]::GetCurrent().User.Value
$token=[Guid]::NewGuid().ToString('N');$taskName='AngeronaDisposableCleanup-'+$sid
$powershell=Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe'
$arguments='-NoLogo -NoProfile -NonInteractive -WindowStyle Hidden -ExecutionPolicy RemoteSigned -File "'+(Join-Path $install 'Run-AngeronaCleanup.ps1')+'" -Mode Watch'
$description='Angerona disposable cleanup v1 '+$token
$start=[DateTime]::Now.AddMinutes(1).ToString('yyyy-MM-ddTHH:mm:ss')
$escape={param($v)[Security.SecurityElement]::Escape([string]$v)}
$xml=@"
<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
 <RegistrationInfo><Description>$description</Description><Source>AngeronaCleanup/1</Source></RegistrationInfo>
 <Triggers><LogonTrigger><Enabled>true</Enabled><UserId>$sid</UserId><Delay>PT1M</Delay></LogonTrigger><TimeTrigger><StartBoundary>$start</StartBoundary><Enabled>true</Enabled></TimeTrigger></Triggers>
 <Principals><Principal id="User"><UserId>$sid</UserId><LogonType>InteractiveToken</LogonType><RunLevel>LeastPrivilege</RunLevel></Principal></Principals>
 <Settings><MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy><DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries><StopIfGoingOnBatteries>false</StopIfGoingOnBatteries><StartWhenAvailable>true</StartWhenAvailable><ExecutionTimeLimit>PT0S</ExecutionTimeLimit><Enabled>true</Enabled><Hidden>true</Hidden><RestartOnFailure><Interval>PT1M</Interval><Count>3</Count></RestartOnFailure></Settings>
 <Actions Context="User"><Exec><Command>$(& $escape $powershell)</Command><Arguments>$(& $escape $arguments)</Arguments><WorkingDirectory>$(& $escape $install)</WorkingDirectory></Exec></Actions>
</Task>
"@
if($ValidateOnly){@{validated=$true;task_xml=$xml;repository_root=$repo;install_directory=$install}|ConvertTo-Json -Depth 5;exit 0}
if(Get-RegisteredCleanupTask -Name $taskName){throw 'This task name already exists. It will not be overwritten; inspect/uninstall the owned installation first.'}
if((Test-Path -LiteralPath $install) -and @(Get-ChildItem -LiteralPath $install -Force).Count -gt 0){throw 'The installation directory must be empty.'}
[IO.Directory]::CreateDirectory($install)|Out-Null
$acl=New-Object Security.AccessControl.DirectorySecurity;$acl.SetAccessRuleProtection($true,$false)
foreach($principal in @($sid,'S-1-5-18','S-1-5-32-544')){$acl.AddAccessRule((New-Object Security.AccessControl.FileSystemAccessRule((New-Object Security.Principal.SecurityIdentifier($principal)),'FullControl','ContainerInherit,ObjectInherit','None','Allow')))}
Set-Acl -LiteralPath $install -AclObject $acl
$registered=$false
try{
    foreach($file in @('SafeCleanup.psm1','AngeronaCleanup.psm1','TaskRegistration.psm1','StartupPermission.psm1','Run-AngeronaCleanup.ps1','Uninstall-AngeronaCleanup.ps1','README.txt','LICENSE')){[IO.File]::WriteAllBytes((Join-Path $install $file),[IO.File]::ReadAllBytes((Join-Path $PSScriptRoot $file)))}
    Import-Module (Join-Path $install 'SafeCleanup.psm1') -Force
    $configuration=[pscustomobject]@{schema=1;owner_sid=$sid;installation_id=$token;install_directory=$install;repository_root=$repo;task_name=$taskName;task_description=$description;task_command=$powershell;task_arguments=$arguments;task_start=$start;request_admin_on_startup=$true}
    Write-CleanupJson -Path (Join-Path $install 'settings.json') -Value $configuration
    Write-CleanupJson -Path (Join-Path $install 'scope-authority.json') -Value @{schema=1;owner_sid=$sid;installation_id=$token;install_directory=$install;repository_root=$repo}
    Register-ScheduledTask -TaskName $taskName -Xml $xml|Out-Null;$registered=$true
    $actual=Get-RegisteredCleanupTask -Name $taskName
    if(-not $actual -or -not (Test-CleanupTaskOwnership -Task $actual -Configuration $configuration)){throw 'Installed task could not be verified.'}
    & $powershell -NoLogo -NoProfile -NonInteractive -ExecutionPolicy RemoteSigned -File (Join-Path $install 'Run-AngeronaCleanup.ps1') -Mode Audit
    if($LASTEXITCODE -ne 0){throw 'Initial read-only audit failed.'}
    Write-Output 'Installed user-level watcher. It starts in approximately one minute and at sign-in. No source, security settings, or Angerona custody permissions were modified.'
}catch{
    if($registered){try{Remove-RegisteredCleanupTask -Configuration $configuration}catch{Write-Warning ('Installation rollback did not remove a changed/unavailable task: '+$_.Exception.Message)}}
    throw
}
