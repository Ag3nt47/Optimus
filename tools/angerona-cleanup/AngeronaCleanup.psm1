Set-StrictMode -Version 2
Import-Module (Join-Path $PSScriptRoot 'SafeCleanup.psm1')
$script:liveTrackedProcesses=@{}

function Get-CleanupProcessState {
    param([Parameter(Mandatory=$true)][string]$RepositoryRoot,[object[]]$Records,[hashtable]$TrackedProcesses)
    $activeProcesses=New-Object 'System.Collections.Generic.List[object]'
    $uncertain=New-Object 'System.Collections.Generic.List[int]'
    if(-not $PSBoundParameters.ContainsKey('Records')){
        try{$Records=@(Get-CimInstance -ClassName Win32_Process -Property Name,ProcessId,ParentProcessId,CreationDate,ExecutablePath,CommandLine -ErrorAction Stop)}
        catch{return [pscustomobject]@{safe=$false;active=$false;uncertain=$true;reason='Windows process inventory is unavailable.';matches=@();unreadable_processes=@()}}
    }
    if($null -eq $Records -or $Records.Count -eq 0){return [pscustomobject]@{safe=$false;active=$false;uncertain=$true;reason='An empty process inventory is not proof that Angerona exited.';matches=@();unreadable_processes=@()}}
    $repo=[IO.Path]::GetFullPath($RepositoryRoot).TrimEnd('\')
    if($null -eq $TrackedProcesses){$TrackedProcesses=if($PSBoundParameters.ContainsKey('Records')){@{}}else{$script:liveTrackedProcesses}}
    $activeIds=@{};$now=[DateTime]::UtcNow
    foreach($process in $Records){
        $name=[string]$process.Name;$command=[string]$process.CommandLine
        $matched=$false;$why=''
        if($name -in @('Angerona.exe','AngeronaStartup.exe')){$matched=$true;$why='Angerona executable'}
        elseif($name -match '^(python(w|\d+(\.\d+)*)?|pyw?)\.exe$'){
            if(-not $command){
                $uncertain.Add([int]$process.ProcessId)
                $birth=$process.PSObject.Properties['CreationDate']
                if($birth -and $birth.Value){$TrackedProcesses[[string]$process.ProcessId]=@{created=([DateTime]$birth.Value).ToUniversalTime().Ticks;seen=$now}}
                continue
            }
            # Strip the executable token before matching. Optimus uses Angerona's
            # venv Python; that shared interpreter path alone is not Angerona.
            $arguments=$command -replace '^\s*("[^"]+"|\S+)\s*',''
            $first=($command -replace '^\s*"([^\"]+)".*$','$1')
            if($first -eq $command){$first=($command -split '\s+',2)[0]}
            $sameVenv=([string]$process.ExecutablePath).StartsWith($repo+'\venv\',[StringComparison]::OrdinalIgnoreCase) -or $first.StartsWith($repo+'\venv\',[StringComparison]::OrdinalIgnoreCase)
            $knownOptimus=$arguments -match '(?i)(^|\s)-m\s+optimus(\.[a-z0-9_.]+)?(\s|$)' -or $arguments -match '(?i)([\\/]optimus-src[\\/]launch\.py|[\\/]Optimus[\\/](App|Release[0-9]+)[\\/]source[\\/]launch\.py)"?(\s|$)'
            if($arguments -match '(?i)(^|\s)-m\s+angerona(\.[a-z0-9_.]+)?(\s|$)'){$matched=$true;$why='Angerona module or worker'}
            elseif($arguments -match '(?i)(import\s+angerona\b|from\s+angerona\b)'){$matched=$true;$why='Angerona imported by Python worker/test'}
            elseif($arguments.IndexOf($repo+'\',[StringComparison]::OrdinalIgnoreCase) -ge 0){$matched=$true;$why='Angerona repository script or test'}
            # A pytest using the same interpreter with relative paths has an
            # unobservable working directory. Conservatively defer cleanup.
            elseif($sameVenv -and -not $knownOptimus){$uncertain.Add([int]$process.ProcessId)}
            elseif($arguments -match '(?i)(multiprocessing\.(spawn|forkserver)|spawn_main|resource_tracker|(^|\s)-m\s+tools\.ci_pytest\b|(^|["\s])\.tmp[\\/])'){$uncertain.Add([int]$process.ProcessId)}
        }elseif($name -in @('powershell.exe','pwsh.exe','cmd.exe')){
            if($command -and $command.IndexOf($repo+'\',[StringComparison]::OrdinalIgnoreCase) -ge 0 -and $command -match '(?i)(Invoke-Angerona|start-angerona|repair_source|startup_repair)'){$matched=$true;$why='Angerona launch/repair supervisor'}
        }
        $key=[string]$process.ProcessId
        $creation=$process.PSObject.Properties['CreationDate']
        if($matched){$activeProcesses.Add([pscustomobject]@{pid=[int]$process.ProcessId;name=$name;reason=$why});$activeIds[$key]=$true}
        if($creation -and $creation.Value){
            $stamp=([DateTime]$creation.Value).ToUniversalTime().Ticks
            if($matched -or [int]$process.ProcessId -in $uncertain.ToArray()){$TrackedProcesses[$key]=@{created=$stamp;seen=$now}}
            elseif($TrackedProcesses.ContainsKey($key) -and $TrackedProcesses[$key].created -eq $stamp){$activeProcesses.Add([pscustomobject]@{pid=[int]$process.ProcessId;name=$name;reason='Previously observed Angerona process lifetime'});$activeIds[$key]=$true;$TrackedProcesses[$key].seen=$now}
            elseif($TrackedProcesses.ContainsKey($key)){$TrackedProcesses.Remove($key)}
        }
    }
    # Retain exact child lifetimes even if a helper or dashboard parent exits.
    # Creation times reject PID reuse; no executable-wide stop/kill is used.
    for($depth=0;$depth -lt 32;$depth++){
        $added=$false
        foreach($process in $Records){
            $key=[string]$process.ProcessId
            if($activeIds.ContainsKey($key)){continue}
            $parentProperty=$process.PSObject.Properties['ParentProcessId'];$creation=$process.PSObject.Properties['CreationDate']
            if(-not $parentProperty -or -not $creation -or -not $creation.Value){continue}
            $parent=[string]$parentProperty.Value;$stamp=([DateTime]$creation.Value).ToUniversalTime().Ticks
            if($TrackedProcesses.ContainsKey($parent) -and $TrackedProcesses[$parent].created -le $stamp){
                $TrackedProcesses[$key]=@{created=$stamp;seen=$now};$activeIds[$key]=$true;$added=$true
                $activeProcesses.Add([pscustomobject]@{pid=[int]$process.ProcessId;name=[string]$process.Name;reason='Observed Angerona descendant process lifetime'})
            }
        }
        if(-not $added){break}
    }
    foreach($key in @($TrackedProcesses.Keys)){if(($now-$TrackedProcesses[$key].seen).TotalMinutes -gt 5){$TrackedProcesses.Remove($key)}}
    return [pscustomobject]@{safe=($activeProcesses.Count -eq 0 -and $uncertain.Count -eq 0);active=($activeProcesses.Count -gt 0);uncertain=($uncertain.Count -gt 0);reason=$(if($activeProcesses.Count){'Angerona runtime, helper, worker, or repository test is still active.'}elseif($uncertain.Count){'Some Python command lines or test working directories cannot be verified.'}else{'No Angerona runtime was found in a complete process inventory.'});matches=@($activeProcesses.ToArray());unreadable_processes=@($uncertain.ToArray())}
}

function Get-CleanupScopes {
    param([Parameter(Mandatory=$true)][string]$RepositoryRoot)
    # The Shell authority, not inherited LOCALAPPDATA, selects native C: paths.
    $local=[Environment]::GetFolderPath('LocalApplicationData')
    $repo=[IO.Path]::GetFullPath($RepositoryRoot).TrimEnd('\')
    $profiles=@((Join-Path $local 'Angerona\SourceData'),(Join-Path $local 'Angerona\Startup'),(Join-Path (Split-Path $repo -Parent) 'AngeronaData'),'D:\AngeronaData')
    $scopes=New-Object 'System.Collections.Generic.List[object]'
    foreach($profile in @($profiles|Select-Object -Unique)){
        if($profile.StartsWith($repo+'\',[StringComparison]::OrdinalIgnoreCase)){continue}
        $scopes.Add([pscustomobject]@{root=(Join-Path $profile 'tmp');kind='runtime-temp';names=@()})
        $scopes.Add([pscustomobject]@{root=(Join-Path $profile 'logs');kind='diagnostic-logs';names=@('startup-preflight.log','startup-preflight.previous.log','startup-dashboard.log','startup-dashboard.previous.log','blackbox.log','crash.log','uiwatchdog.log')})
        $scopes.Add([pscustomobject]@{root=(Join-Path $profile 'diagnostics');kind='crash-diagnostic';names=@('crash.log')})
    }
    return @($scopes.ToArray())
}

function Invoke-AngeronaCleanup {
    param([Parameter(Mandatory=$true)][string]$RepositoryRoot,[switch]$Apply,[scriptblock]$ContinueCheck)
    $before=Get-CleanupProcessState -RepositoryRoot $RepositoryRoot
    if(-not $before.safe){return [pscustomobject]@{utc=[DateTime]::UtcNow.ToString('o');mode=$(if($Apply){'apply-deferred'}else{'audit-deferred'});completed=$false;deleted_bytes=0;process_state=$before;scopes=@();reason=$before.reason}}
    $results=New-Object 'System.Collections.Generic.List[object]';$deleted=0L;$allComplete=$true
    $checkState=@{last=[DateTime]::MinValue;safe=$true}
    $processReader=(Get-Command Get-CleanupProcessState -CommandType Function).ScriptBlock
    $externalCheck=$ContinueCheck
    $check={
        if($externalCheck -and -not (& $externalCheck)){return $false}
        if(([DateTime]::UtcNow-$checkState.last).TotalSeconds -ge 1){$checkState.last=[DateTime]::UtcNow;$checkState.safe=(& $processReader -RepositoryRoot $RepositoryRoot).safe}
        return $checkState.safe
    }.GetNewClosure()
    foreach($scope in Get-CleanupScopes -RepositoryRoot $RepositoryRoot){
        try{
            # Existence checks cannot turn denied enumeration into absence.
            try{$item=Get-Item -LiteralPath $scope.root -Force -ErrorAction Stop}
            catch [System.Management.Automation.ItemNotFoundException]{$results.Add([pscustomobject]@{root=$scope.root;kind=$scope.kind;state='absent'});continue}
            if($item.Attributes -band [IO.FileAttributes]::ReparsePoint){throw 'A scope root is a reparse point.'}
            if(-not $item.PSIsContainer){throw 'A scope root is not a directory.'}
            # Recheck at each root and during deletion, never use a stale exit.
            if(-not (& $check)){$allComplete=$false;$results.Add([pscustomobject]@{root=$scope.root;kind=$scope.kind;state='runtime-started'});break}
            $scan=Invoke-SafeCleanup -Root $scope.root -MinimumAgeHours 0 -MaximumFiles 5000 -MaximumBytes 1073741824 -MaximumSeconds 10 -Apply:$Apply -FileNames $scope.names -ContinueCheck $check
            $deleted+=$scan.deleted_bytes
            if($scan.bounded -or $scan.reasons.ContainsKey('directory-inaccessible')){$allComplete=$false}
            $results.Add([pscustomobject]@{root=$scope.root;kind=$scope.kind;state='inspected';result=$scan})
        }catch{$allComplete=$false;$results.Add([pscustomobject]@{root=$scope.root;kind=$scope.kind;state='skipped-unavailable';reason=$_.Exception.Message})}
    }
    return [pscustomobject]@{utc=[DateTime]::UtcNow.ToString('o');mode=$(if($Apply){'cleanup'}else{'audit'});completed=$allComplete;deleted_bytes=$deleted;process_state=$before;scopes=@($results.ToArray());reason='Only verified runtime temp and exact disposable diagnostic filenames were considered. Protected roots, live files and unknown states are preserved.'}
}
Export-ModuleMember -Function Get-CleanupProcessState,Get-CleanupScopes,Invoke-AngeronaCleanup
