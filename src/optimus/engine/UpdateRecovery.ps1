# Read-only, bounded local evidence. No search, installation, restart or retry.
function Get-OptimusUpdateRecoveryProbe {
    $session=$null
    $installer=$null
    $system=$null
    try {
        $session=New-Object -ComObject Microsoft.Update.Session
        $installer=$session.CreateUpdateInstaller()
        $system=New-Object -ComObject Microsoft.Update.SystemInfo
        $busy=$installer.IsBusy
        $reboot=$system.RebootRequired
        if($busy -isnot [bool] -or $reboot -isnot [bool]) {
            throw 'Windows Update busy/restart state is unavailable.'
        }
        foreach($key in @('HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Component Based Servicing\RebootPending',
                           'HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\WindowsUpdate\Auto Update\RebootRequired')) {
            if(Test-Path -LiteralPath $key -ErrorAction Stop){$reboot=$true}
        }
        $processes=@(Get-CimInstance -ClassName Win32_Process -OperationTimeoutSec 10 -ErrorAction Stop)
        if($processes.Count -lt 1 -or $processes.Count -gt 10000){throw 'The process inventory exceeds its bound.'}
        $rows=@(foreach($process in $processes) {
            [pscustomobject]@{pid=[int]$process.ProcessId;name=[string]$process.Name;
                path=[string]$process.ExecutablePath;started_utc=(Get-SCMUtcTimeString $process.CreationDate)}
        })
        $apps=New-Object 'System.Collections.Generic.List[object]'
        foreach($root in @('HKCU:\Software\Microsoft\Windows\CurrentVersion\Uninstall',
            'HKLM:\Software\Microsoft\Windows\CurrentVersion\Uninstall',
            'HKLM:\Software\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall')) {
            if(-not(Test-Path -LiteralPath $root -ErrorAction Stop)){continue}
            foreach($key in @(Get-ChildItem -LiteralPath $root -ErrorAction Stop)) {
                if($apps.Count -ge 10000){throw 'The installed application inventory exceeds its bound.'}
                $app=Get-ItemProperty -LiteralPath $key.PSPath -ErrorAction Stop
                if([string]$app.DisplayName) {
                    $apps.Add([pscustomobject]@{name=[string]$app.DisplayName;version=[string]$app.DisplayVersion;
                        publisher=[string]$app.Publisher})
                }
            }
        }
        [pscustomobject]@{schema=1;complete=$true;review_only=$true;
            owner=('sid:'+ [Security.Principal.WindowsIdentity]::GetCurrent().User.Value);
            windows_directory=[Environment]::GetFolderPath('Windows');
            captured_at=([DateTimeOffset]::UtcNow.ToUnixTimeMilliseconds()/1000.0);
            wua_busy=$busy;reboot_required=$reboot;processes=$rows;applications=@($apps.ToArray())}
    } finally {
        foreach($native in @($installer,$system,$session)) {
            if($null -ne $native -and [Runtime.InteropServices.Marshal]::IsComObject($native)) {
                [void][Runtime.InteropServices.Marshal]::ReleaseComObject($native)
            }
        }
    }
}
