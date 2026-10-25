param([switch]$ValidateOnly)
$ErrorActionPreference='Stop'
Set-StrictMode -Version 2
[Console]::OutputEncoding=New-Object Text.UTF8Encoding($false)
[Console]::InputEncoding=New-Object Text.UTF8Encoding($false)

function Assert-OrdinaryPath([string]$Path,[bool]$File) {
    if(-not [IO.Path]::IsPathRooted($Path) -or $Path -notmatch '^[A-Za-z]:\\' -or $Path -match '[\x00-\x1f%]' -or $Path.Length -gt 4096){throw 'Invalid local launcher path.'}
    $current=[IO.Path]::GetFullPath($Path)
    while($current){
        $item=Get-Item -LiteralPath $current -Force
        if($item.Attributes -band [IO.FileAttributes]::ReparsePoint){throw 'Indirect launch paths are unsupported.'}
        $current=Split-Path -Path $current -Parent
    }
    if($File -and -not [IO.File]::Exists($Path)){throw 'The installed launcher is unavailable.'}
}
function Quote-WindowsArgument([string]$Text) {
    if($Text -notmatch '[\s"]' -and $Text.Length -gt 0){return $Text}
    $out='"';$slashes=0
    foreach($c in $Text.ToCharArray()){
        if($c -eq '\'){$slashes++;continue}
        if($c -eq '"'){$out+=(''.PadLeft(2*$slashes+1,'\'))+'"'}else{$out+=(''.PadLeft($slashes,'\'))+$c}
        $slashes=0
    }
    return $out+(''.PadLeft(2*$slashes,'\'))+'"'
}
function Assert-Manifest($Manifest,[string]$Sid,[switch]$RequireFiles) {
    if($null -eq $Manifest){return}
    if($Manifest.schema -isnot [int] -or $Manifest.schema -ne 1 -or $Manifest.owner_token -notmatch '^[0-9a-f]{32}$' -or $Manifest.owner_sid -ne $Sid){throw 'Invalid sign-in registration ownership.'}
    foreach($launch in @($Manifest.launch,$Manifest.previous_launch)){
        if($null -eq $launch){continue}
        if($launch.executable -isnot [string] -or $launch.working_directory -isnot [string] -or $launch.argument_line -isnot [string] -or $launch.arguments -isnot [Array]){throw 'Invalid launch specification.'}
        if($launch.arguments.Count -gt 2 -or $launch.argument_line.Length -gt 8192){throw 'Invalid launch arguments.'}
        $parts=@();foreach($arg in $launch.arguments){if($arg -isnot [string] -or $arg.Length -gt 2048 -or $arg -match '[\x00-\x1f]'){throw 'Invalid launch argument.'};$parts+=Quote-WindowsArgument $arg}
        if(($parts -join ' ') -cne $launch.argument_line){throw 'The sign-in argument quoting does not match.'}
        $leaf=[IO.Path]::GetFileName($launch.executable)
        if($launch.arguments.Count -eq 0){if($leaf -ine 'Optimus.exe'){throw 'Only the installed Optimus executable is supported.'}}
        elseif($launch.arguments.Count -ne 2 -or $leaf -notmatch '^python(w|\d+(\.\d+)*)?\.exe$' -or $launch.arguments[0] -cne '-B' -or [IO.Path]::GetFileName($launch.arguments[1]) -cne 'launch.py' -or [IO.Path]::GetDirectoryName($launch.arguments[1]) -ine $launch.working_directory){throw 'Only the original source launcher is supported.'}
        if($RequireFiles -and $launch -eq $Manifest.launch){Assert-OrdinaryPath $launch.executable $true;Assert-OrdinaryPath $launch.working_directory $false;if($launch.arguments.Count -eq 2){Assert-OrdinaryPath $launch.arguments[1] $true}}
    }
}
function Test-TaskUser([string]$UserId,[string]$Sid) {
    if([string]::IsNullOrWhiteSpace($UserId) -or $UserId.Length -gt 512){return $false}
    if($UserId -ceq $Sid){return $true}
    # Task Scheduler may normalize a supplied SID to an account or short local
    # name. Resolve that name; matching username text alone proves no ownership.
    try{
        $resolved=([Security.Principal.NTAccount]::new($UserId)).Translate([Security.Principal.SecurityIdentifier])
        return $resolved.Value -ceq $Sid
    }catch{return $false}
}
function Test-Owned($Definition,$Manifest,[string]$Sid) {
    if($null -eq $Manifest){return $null}
    if($Definition.RegistrationInfo.Description -cne ('Optimus sign-in launcher v1 '+$Manifest.owner_token) -or $Definition.RegistrationInfo.Source -cne 'Optimus/SignIn/1' -or $Definition.Actions.Count -ne 1 -or -not (Test-TaskUser $Definition.Principal.UserId $Sid) -or $Definition.Principal.RunLevel -ne 0 -or $Definition.Principal.LogonType -ne 3 -or $Definition.Triggers.Count -ne 1){return $null}
    $trigger=$Definition.Triggers.Item(1)
    if($trigger.Type -ne 9 -or -not (Test-TaskUser $trigger.UserId $Sid) -or $trigger.Delay -ne 'PT1M'){return $null}
    $action=$Definition.Actions.Item(1)
    if($action.Type -ne 0){return $null}
    foreach($launch in @($Manifest.launch,$Manifest.previous_launch)){
        # Registered task COM objects return null for an empty argument line.
        # Normalize that empty representation; nonempty argument text is exact.
        if($launch -and $action.Path -ieq $launch.executable -and [string]$action.Arguments -ceq $launch.argument_line -and $action.WorkingDirectory -ieq $launch.working_directory){return $launch}
    }
    return $null
}
function Read-Task($Folder,[string]$Name) {
    try{return $Folder.GetTask($Name)}catch{
        # COM invocation can be wrapped in MethodInvocationException or mapped
        # to FileNotFoundException. Inspect the bounded chain, while treating
        # only HRESULT_FROM_WIN32(ERROR_FILE_NOT_FOUND) as an absent task.
        $exception=$_.Exception
        for($depth=0;$exception -and $depth -lt 16;$depth++){
            if($exception.HResult -eq -2147024894){return $null}
            if($exception -is [ComponentModel.Win32Exception] -and $exception.NativeErrorCode -eq 2){return $null}
            $exception=$exception.InnerException
        }
        throw
    }
}
function Task-Status($Task,$Manifest,[string]$Sid,[string]$Name) {
    if($null -eq $Task){return @{enabled=$false;owned=$false;available=$true;state='absent';reason='Optimus is not registered to open at sign-in.';task_name=$Name;target=$null}}
    $match=Test-Owned $Task.Definition $Manifest $Sid
    if($null -eq $match){return @{enabled=$null;owned=$false;available=$true;state='foreign';reason='The task name is occupied by an unrecognized or manually changed task; it was not changed.';task_name=$Name;target=$null}}
    $enabled=[bool]($Task.Enabled -and $Task.Definition.Triggers.Item(1).Enabled)
    return @{enabled=$enabled;owned=$true;available=$true;state=$(if($enabled){'enabled'}else{'disabled'});reason='Owned user-level sign-in task; Windows administrator consent remains separate.';task_name=$Name;target=$match.executable}
}

$registrationStarted=$false
try {
    if(-not (Get-Command Assert-OptimusStartupHeadroom -CommandType Function -ErrorAction SilentlyContinue)){
        . (Join-Path $PSScriptRoot 'StartupSafety.ps1')
    }
    $raw=[Console]::In.ReadToEnd()
    if($raw.Length -gt 131072){throw 'Sign-in request exceeds its size limit.'}
    $request=$raw|ConvertFrom-Json
    if($request.operation -notin @('status','set','restore')){throw 'Unsupported sign-in task operation.'}
    $sid=[Security.Principal.WindowsIdentity]::GetCurrent().User.Value
    Assert-Manifest $request.manifest $sid
    $name='Optimus-SignIn-'+$sid
    if($ValidateOnly){Assert-Manifest $request.manifest $sid -RequireFiles;@{ok=$true;owner_sid=$sid;validated=$true;task_name=$name}|ConvertTo-Json -Compress;exit 0}
    $scheduler=New-Object -ComObject 'Schedule.Service';$scheduler.Connect();$folder=$scheduler.GetFolder('\')
    $task=Read-Task $folder $name
    $beforeXml=if($task){$task.Definition.XmlText}else{$null}
    $status=Task-Status $task $request.manifest $sid $name
    $persisted=if($task -and $status.owned){Read-OptimusPersistedTask $name ([string]$task.Definition.XmlText)}else{$null}
    if($request.operation -eq 'set'){
        if($request.enabled -isnot [bool]){throw 'Sign-in launch must be on or off.'}
        if($status.state -eq 'foreign'){throw $status.reason}
        if($request.enabled){
            Assert-Manifest $request.manifest $sid -RequireFiles
            if($null -eq $request.manifest){throw 'The launch ownership record is missing.'}
            $definition=$scheduler.NewTask(0)
            $definition.RegistrationInfo.Description='Optimus sign-in launcher v1 '+$request.manifest.owner_token
            $definition.RegistrationInfo.Source='Optimus/SignIn/1'
            $definition.Principal.UserId=$sid;$definition.Principal.LogonType=3;$definition.Principal.RunLevel=0
            $definition.Settings.Enabled=$true;$definition.Settings.MultipleInstances=2
            $definition.Settings.DisallowStartIfOnBatteries=$false;$definition.Settings.StopIfGoingOnBatteries=$false
            $definition.Settings.ExecutionTimeLimit='PT0S'
            $trigger=$definition.Triggers.Create(9);$trigger.UserId=$sid;$trigger.Delay='PT1M';$trigger.Enabled=$true
            $action=$definition.Actions.Create(0);$action.Path=$request.manifest.launch.executable;$action.Arguments=$request.manifest.launch.argument_line;$action.WorkingDirectory=$request.manifest.launch.working_directory
            $flags=if($task){4}else{2}
            $headroom=Assert-OptimusStartupHeadroom
            $registrationStarted=$true
            $folder.RegisterTaskDefinition($name,$definition,$flags,$sid,$null,3,$null)|Out-Null
        }elseif($task){$folder.DeleteTask($name,0)}
    }elseif($request.operation -eq 'restore'){
        if($status.state -eq 'foreign'){throw 'The sign-in task changed before rollback; it was preserved.'}
        if($request.before_xml){
            if($request.before_xml -isnot [string] -or $request.before_xml.Length -gt 65536){throw 'Invalid rollback task record.'}
            Assert-Manifest $request.before_manifest $sid
            $previous=$scheduler.NewTask(0);$previous.XmlText=$request.before_xml
            if($null -eq (Test-Owned $previous $request.before_manifest $sid)){throw 'The rollback task is not owned.'}
            $flags=if($task){4}else{2}
            $headroom=Assert-OptimusStartupHeadroom
            $registrationStarted=$true
            $folder.RegisterTaskDefinition($name,$previous,$flags,$sid,$null,3,$null)|Out-Null
        }elseif($task){$folder.DeleteTask($name,0)}
    }
    $task=Read-Task $folder $name
    $status=Task-Status $task $request.manifest $sid $name
    $matched=if($task){Test-Owned $task.Definition $request.manifest $sid}else{$null}
    $persisted=if($task -and $matched){Read-OptimusPersistedTask $name ([string]$task.Definition.XmlText)}else{$null}
    if($request.operation -eq 'set' -and ($status.state -eq 'foreign' -or $status.enabled -ne $request.enabled)){throw 'Windows sign-in task verification failed; inspect the actual status before retrying.'}
    @{ok=$true;status=$status;owner_sid=$sid;matched_launch=$matched;before_xml=$beforeXml;persisted_task=$persisted}|ConvertTo-Json -Depth 12 -Compress
} catch {
    if(Get-Command Get-OptimusStartupFailure -CommandType Function -ErrorAction SilentlyContinue){Get-OptimusStartupFailure $_.Exception $registrationStarted|ConvertTo-Json -Depth 6 -Compress}
    else{@{ok=$false;error='The native startup verification helper is unavailable.'}|ConvertTo-Json -Compress}
    exit 1
}
