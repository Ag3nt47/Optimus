Set-StrictMode -Version 2
function Test-CleanupTaskIdentity {
    param([string]$Identity,[string]$ExpectedSid)
    if(-not $Identity){return $false}
    try{
        if($Identity -match '^S-\d-'){$resolved=(New-Object Security.Principal.SecurityIdentifier($Identity)).Value}
        else{$resolved=(New-Object Security.Principal.NTAccount($Identity)).Translate([Security.Principal.SecurityIdentifier]).Value}
        return ($resolved -eq $ExpectedSid)
    }catch{return $false}
}
function Get-RegisteredCleanupTask {
    param([Parameter(Mandatory=$true)][string]$Name)
    $service=New-Object -ComObject 'Schedule.Service';$service.Connect();$folder=$service.GetFolder('\')
    try{return $folder.GetTask($Name)}catch{
        $detail=$_.Exception
        while($detail){if($detail.HResult -eq -2147024894 -or ($detail -is [ComponentModel.Win32Exception] -and $detail.NativeErrorCode -eq 2)){return $null};$detail=$detail.InnerException}
        throw
    }
}
function Test-CleanupTaskOwnership {
    param([Parameter(Mandatory=$true)]$Task,[Parameter(Mandatory=$true)]$Configuration)
    try{
        $sid=[Security.Principal.WindowsIdentity]::GetCurrent().User.Value
        if($Configuration.owner_sid -ne $sid -or $Configuration.installation_id -notmatch '^[a-f0-9]{32}$' -or $Configuration.task_name -ne ('AngeronaDisposableCleanup-'+$sid)){return $false}
        $definition=$Task.Definition;$action=$definition.Actions.Item(1)
        $command=Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe'
        $arguments='-NoLogo -NoProfile -NonInteractive -WindowStyle Hidden -ExecutionPolicy RemoteSigned -File "'+(Join-Path $Configuration.install_directory 'Run-AngeronaCleanup.ps1')+'" -Mode Watch'
        if($Task.Path -ine ('\'+$Configuration.task_name) -or $definition.RegistrationInfo.Source -cne 'AngeronaCleanup/1' -or $definition.RegistrationInfo.Description -cne ('Angerona disposable cleanup v1 '+$Configuration.installation_id) -or $definition.Actions.Count -ne 1 -or $action.Type -ne 0 -or $action.Path -ine $command -or $action.Arguments -cne $arguments -or $action.WorkingDirectory -ine $Configuration.install_directory){return $false}
        if(-not (Test-CleanupTaskIdentity -Identity $definition.Principal.UserId -ExpectedSid $sid) -or $definition.Principal.LogonType -ne 3 -or $definition.Principal.RunLevel -ne 0 -or $definition.Settings.MultipleInstances -ne 2 -or $definition.Triggers.Count -ne 2){return $false}
        $logon=0;$timer=0
        for($index=1;$index -le $definition.Triggers.Count;$index++){
            $trigger=$definition.Triggers.Item($index)
            if($trigger.Type -eq 9 -and (Test-CleanupTaskIdentity -Identity $trigger.UserId -ExpectedSid $sid) -and $trigger.Delay -eq 'PT1M'){$logon++}
            elseif($trigger.Type -eq 1 -and $trigger.StartBoundary -eq $Configuration.task_start){$timer++}
            else{return $false}
        }
        return ($logon -eq 1 -and $timer -eq 1)
    }catch{return $false}
}
function Remove-RegisteredCleanupTask {
    param([Parameter(Mandatory=$true)]$Configuration)
    $task=Get-RegisteredCleanupTask -Name $Configuration.task_name
    if(-not $task){return}
    if(-not (Test-CleanupTaskOwnership -Task $task -Configuration $Configuration)){throw 'The cleanup task changed; it was preserved.'}
    try{$task.Stop(0)}catch{
        $detail=$_.Exception;$notRunning=$false
        while($detail){if($detail.HResult -eq -2147216629){$notRunning=$true;break};$detail=$detail.InnerException}
        if(-not $notRunning){throw}
    }
    $task=Get-RegisteredCleanupTask -Name $Configuration.task_name
    if(-not $task){return}
    if(-not (Test-CleanupTaskOwnership -Task $task -Configuration $Configuration)){throw 'The cleanup task changed after stop; it was preserved.'}
    $service=New-Object -ComObject 'Schedule.Service';$service.Connect();$service.GetFolder('\').DeleteTask($Configuration.task_name,0)
    if(Get-RegisteredCleanupTask -Name $Configuration.task_name){throw 'Windows did not confirm removal of the cleanup task.'}
}
Export-ModuleMember -Function Get-RegisteredCleanupTask,Test-CleanupTaskOwnership,Remove-RegisteredCleanupTask
