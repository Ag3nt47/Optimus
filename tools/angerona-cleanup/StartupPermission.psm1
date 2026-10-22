Set-StrictMode -Version 2
Import-Module (Join-Path $PSScriptRoot 'SafeCleanup.psm1')
function Initialize-CleanupIdentity {
    if('AngeronaCleanup.NativeIdentity' -as [type]){return}
    Add-Type -TypeDefinition @'
using System;
using System.IO;
using System.Text;
using System.Collections.Generic;
using System.Runtime.InteropServices;
using System.Security.Principal;
using Microsoft.Win32.SafeHandles;
namespace AngeronaCleanup {
 public sealed class Identity { public int Pid,Session; public bool Administrator,Alive; public string Sid,Image,Created; }
 public static class NativeIdentity {
  [StructLayout(LayoutKind.Sequential)] struct Info {public uint Attr;public System.Runtime.InteropServices.ComTypes.FILETIME Created,Access,Write;public uint Volume,High,Low,Links,IndexHigh,IndexLow;}
  [DllImport("kernel32.dll",SetLastError=true)] static extern IntPtr OpenProcess(uint a,bool i,int p);
  [DllImport("kernel32.dll")] static extern bool CloseHandle(IntPtr h);
  [DllImport("kernel32.dll",SetLastError=true)] static extern bool GetProcessTimes(IntPtr h,out long c,out long e,out long k,out long u);
  [DllImport("kernel32.dll",SetLastError=true)] static extern bool GetExitCodeProcess(IntPtr h,out uint c);
  [DllImport("kernel32.dll",CharSet=CharSet.Unicode,SetLastError=true)] static extern bool QueryFullProcessImageName(IntPtr h,uint f,StringBuilder n,ref int s);
  [DllImport("advapi32.dll",SetLastError=true)] static extern bool OpenProcessToken(IntPtr p,uint a,out IntPtr t);
  [DllImport("advapi32.dll",SetLastError=true)] static extern bool GetTokenInformation(IntPtr t,int c,IntPtr b,int n,out int r);
  [DllImport("kernel32.dll",CharSet=CharSet.Unicode,SetLastError=true)] static extern SafeFileHandle CreateFileW(string p,uint a,uint s,IntPtr sec,uint mode,uint flags,IntPtr template);
  [DllImport("kernel32.dll",SetLastError=true)] static extern bool GetFileInformationByHandle(SafeFileHandle h,out Info i);
  public static Identity Read(int pid){
   IntPtr p=OpenProcess(0x1000,false,pid),t=IntPtr.Zero,b=IntPtr.Zero;
   try{
    if(p==IntPtr.Zero||!OpenProcessToken(p,8,out t))return null;
    int size;GetTokenInformation(t,1,IntPtr.Zero,0,out size);b=Marshal.AllocHGlobal(Math.Max(size,16));
    if(!GetTokenInformation(t,1,b,size,out size))return null;string sid=new SecurityIdentifier(Marshal.ReadIntPtr(b)).Value;
    if(!GetTokenInformation(t,20,b,4,out size))return null;bool admin=Marshal.ReadInt32(b)!=0;
    if(!GetTokenInformation(t,12,b,4,out size))return null;int session=Marshal.ReadInt32(b);
    long c,e,k,u;uint exit;int length=32768;var image=new StringBuilder(length);
    if(!GetProcessTimes(p,out c,out e,out k,out u)||!GetExitCodeProcess(p,out exit)||!QueryFullProcessImageName(p,0,image,ref length))return null;
    return new Identity{Pid=pid,Session=session,Administrator=admin,Alive=exit==259,Sid=sid,Image=image.ToString(),Created=DateTime.FromFileTimeUtc(c).ToString("o")};
   }finally{if(b!=IntPtr.Zero)Marshal.FreeHGlobal(b);if(t!=IntPtr.Zero)CloseHandle(t);if(p!=IntPtr.Zero)CloseHandle(p);}
  }
  public static string ReadPrivate(string path,int maximum){
   var held=new List<SafeFileHandle>();SafeFileHandle leaf=null;
   try{
    string directory=Path.GetDirectoryName(Path.GetFullPath(path)),current=Path.GetPathRoot(directory);
    var paths=new List<string>();paths.Add(current);foreach(string name in directory.Substring(current.Length).Split(new char[]{'\\'},StringSplitOptions.RemoveEmptyEntries)){current=Path.Combine(current,name);paths.Add(current);}
    foreach(string parent in paths){var h=CreateFileW(parent,0x80,3,IntPtr.Zero,3,0x02200000,IntPtr.Zero);held.Add(h);Info i;if(h.IsInvalid||!GetFileInformationByHandle(h,out i)||(i.Attr&0x400)!=0||(i.Attr&0x10)==0)throw new IOException("Private state has an unavailable or indirect ancestor.");}
    leaf=CreateFileW(path,0x80000000,5,IntPtr.Zero,3,0x00200000,IntPtr.Zero);Info info;
    if(leaf.IsInvalid||!GetFileInformationByHandle(leaf,out info)||(info.Attr&(0x400|0x10))!=0||info.Links!=1||info.High!=0||info.Low>maximum)throw new IOException("Private state is unavailable, linked or oversized.");
    using(var stream=new FileStream(leaf,FileAccess.Read)){var bytes=new byte[maximum+1];int total=0,n;while(total<bytes.Length&&(n=stream.Read(bytes,total,bytes.Length-total))>0)total+=n;if(total>maximum)throw new IOException("Private state exceeds its limit.");return new UTF8Encoding(false,true).GetString(bytes,0,total).TrimStart('\ufeff');}
   }finally{if(leaf!=null)leaf.Dispose();for(int i=held.Count-1;i>=0;i--)held[i].Dispose();}
  }
 }
}
'@
}
function Read-CleanupPrivateJson([string]$Path,[int]$Maximum=16384){Initialize-CleanupIdentity;return ([AngeronaCleanup.NativeIdentity]::ReadPrivate($Path,$Maximum)|ConvertFrom-Json)}
function Get-CleanupIdentity([int]$ProcessNumber=$PID){Initialize-CleanupIdentity;return [AngeronaCleanup.NativeIdentity]::Read($ProcessNumber)}
function Get-CleanupElevationDecision([string]$Mode,[bool]$Administrator,[bool]$AdminRelaunch){
    if($Mode -ne 'Watch'){return 'manual-no-request'}
    if($Administrator){return 'already-administrator'}
    if($AdminRelaunch){return 'standard-loop-guard'}
    return 'request-once'
}
function Test-CleanupLeaseFree([string]$Path){
    try{$stream=[IO.File]::Open($Path,[IO.FileMode]::OpenOrCreate,[IO.FileAccess]::ReadWrite,[IO.FileShare]::None);$stream.Dispose();return $true}catch [IO.IOException]{return $false}
}
function Get-CleanupFileHash([string]$Path){return (Get-FileHash -LiteralPath $Path -Algorithm SHA256 -ErrorAction Stop).Hash.ToLowerInvariant()}
function Read-CleanupConfiguration {
    param([string]$InstallationRoot)
    $install=[IO.Path]::GetFullPath($InstallationRoot).TrimEnd('\')
    if($install -notmatch '^D:\\.+[^\\]$'){throw 'The approved cleanup installation must remain on D:.'}
    $config=Read-CleanupPrivateJson (Join-Path $install 'settings.json')
    $authority=Read-CleanupPrivateJson (Join-Path $install 'scope-authority.json')
    $sid=[Security.Principal.WindowsIdentity]::GetCurrent().User.Value
    if($config.schema -ne 1 -or $config.owner_sid -ne $sid -or $authority.schema -ne 1 -or $authority.owner_sid -ne $sid -or $config.installation_id -notmatch '^[a-f0-9]{32}$' -or $config.installation_id -cne $authority.installation_id -or [IO.Path]::GetFullPath($config.install_directory).TrimEnd('\') -ine $install -or [IO.Path]::GetFullPath($authority.install_directory).TrimEnd('\') -ine $install){throw 'Cleanup ownership or scope authority does not match this account/installation.'}
    $repo=[IO.Path]::GetFullPath($config.repository_root).TrimEnd('\')
    if($repo -ine [IO.Path]::GetFullPath($authority.repository_root).TrimEnd('\') -or $repo -notmatch '^[CD]:\\' -or [IO.Path]::GetFileName($repo) -ine 'AngeronaSuite' -or $repo.StartsWith($install+'\',[StringComparison]::OrdinalIgnoreCase) -or $install.StartsWith($repo+'\',[StringComparison]::OrdinalIgnoreCase)){throw 'The repository differs from the installation-approved scope; edit/reinstall the tool explicitly to change its source authority.'}
    # Verify the original source anchor without importing or editing it.
    Read-CleanupPrivateJson (Join-Path $install 'scope-authority.json')|Out-Null
    $part=$repo
    while($part){$item=Get-Item -LiteralPath $part -Force -ErrorAction Stop;if($item.Attributes -band [IO.FileAttributes]::ReparsePoint){throw 'The approved repository has an indirect ancestor.'};$part=Split-Path $part -Parent}
    Initialize-CleanupIdentity
    [AngeronaCleanup.NativeIdentity]::ReadPrivate((Join-Path $repo 'src\angerona\app.py'),1048576)|Out-Null
    return $config
}
function Test-CleanupStopRequested($Configuration){
    $path=Join-Path $Configuration.install_directory 'stop-request.json'
    if(-not [IO.File]::Exists($path)){return $false}
    try{$request=Read-CleanupPrivateJson $path 4096;return ($request.schema -eq 1 -and $request.installation_id -ceq $Configuration.installation_id -and $request.owner_sid -eq $Configuration.owner_sid -and [DateTime]::Parse($request.expires_utc).ToUniversalTime() -gt [DateTime]::UtcNow)}catch{return $false}
}
function Request-CleanupWatcherStop($Configuration,[int]$TimeoutSeconds=15){
    Write-CleanupJson -Path (Join-Path $Configuration.install_directory 'stop-request.json') -Value @{schema=1;installation_id=$Configuration.installation_id;owner_sid=$Configuration.owner_sid;utc=[DateTime]::UtcNow.ToString('o');expires_utc=[DateTime]::UtcNow.AddMinutes(2).ToString('o')}
    $deadline=[DateTime]::UtcNow.AddSeconds($TimeoutSeconds)
    while([DateTime]::UtcNow -lt $deadline){if(Test-CleanupLeaseFree (Join-Path $Configuration.install_directory 'watcher.lock')){return $true};Start-Sleep -Milliseconds 100}
    return $false
}
function Test-CleanupReady($Ready,$Ticket,$Identity,[int]$ExpectedProcess,[string]$HostPath){
    if(-not $Identity){return $false}
    return ($Ready.schema -eq 1 -and $Ready.nonce -ceq $Ticket.nonce -and $Ready.installation_id -ceq $Ticket.installation_id -and $Ready.owner_sid -eq $Ticket.owner_sid -and $Ready.state -ceq 'elevated-ready' -and $Ready.child_pid -eq $ExpectedProcess -and $Identity.Pid -eq $ExpectedProcess -and $Identity.Alive -and $Identity.Administrator -and $Identity.Sid -eq $Ticket.owner_sid -and $Identity.Session -eq $Ticket.parent_session -and $Identity.Image -ieq $HostPath -and $Identity.Created -ceq $Ready.child_created)
}
function Request-CleanupStartupElevation {
    param([Parameter(Mandatory=$true)]$Configuration,[scriptblock]$Launcher,[scriptblock]$ProcessProbe)
    if(-not $Launcher){$Launcher={param($HostPath,$Arguments,$TicketPath) Start-Process -FilePath $HostPath -ArgumentList $Arguments -Verb RunAs -WindowStyle Hidden -PassThru -ErrorAction Stop}}
    if(-not $ProcessProbe){$ProcessProbe={param($ProcessNumber) Get-CleanupIdentity $ProcessNumber}}
    $install=$Configuration.install_directory;$reservation=$null;$ticketPath=$null;$cancelPath=$null
    try{
        try{$reservation=[IO.File]::Open((Join-Path $install 'startup.lock'),[IO.FileMode]::OpenOrCreate,[IO.FileAccess]::ReadWrite,[IO.FileShare]::None)}catch [IO.IOException]{return @{action='already-starting';state='startup-reserved';administrator=$false;reason='Another watcher startup owns the reservation.'}}
        if(Test-CleanupStopRequested $Configuration){return @{action='stopped';state='stop-requested';administrator=$false}}
        if(-not (Test-CleanupLeaseFree (Join-Path $install 'watcher.lock'))){return @{action='already-running';state='watcher-running';administrator=$false}}
        $parent=& $ProcessProbe $PID
        if(-not $parent -or -not $parent.Alive -or $parent.Sid -ne $Configuration.owner_sid){throw 'The startup parent identity could not be verified.'}
        $handoffs=Join-Path $install 'Handoffs';[IO.Directory]::CreateDirectory($handoffs)|Out-Null
        $part=$handoffs
        while($part){$entry=Get-Item -LiteralPath $part -Force;if($entry.Attributes -band [IO.FileAttributes]::ReparsePoint){throw 'The administrator handoff directory is indirect.'};$part=Split-Path $part -Parent}
        $oldNames=@(Get-ChildItem -LiteralPath $handoffs -File|Where-Object{$_.Name -match '^[0-9a-f]{32}\.(ticket|ready|ack|cancel)\.json$' -and $_.LastWriteTimeUtc -lt [DateTime]::UtcNow.AddDays(-1)}|Select-Object -First 1000 -ExpandProperty Name)
        if($oldNames.Count){Invoke-SafeCleanup -Root $handoffs -MinimumAgeHours 24 -MaximumFiles 1000 -MaximumBytes 1048576 -MaximumSeconds 1 -Apply -FileNames $oldNames|Out-Null}
        $nonce=[Guid]::NewGuid().ToString('N');$ticketPath=Join-Path $handoffs ($nonce+'.ticket.json');$readyPath=Join-Path $handoffs ($nonce+'.ready.json');$ackPath=Join-Path $handoffs ($nonce+'.ack.json');$cancelPath=Join-Path $handoffs ($nonce+'.cancel.json')
        $ticket=@{schema=1;nonce=$nonce;installation_id=$Configuration.installation_id;owner_sid=$Configuration.owner_sid;install_directory=$install;parent_pid=$PID;parent_created=$parent.Created;parent_session=$parent.Session;expires_utc=[DateTime]::UtcNow.AddMinutes(15).ToString('o');config_sha256=(Get-CleanupFileHash (Join-Path $install 'settings.json'));authority_sha256=(Get-CleanupFileHash (Join-Path $install 'scope-authority.json'))}
        Write-CleanupJson -Path $ticketPath -Value $ticket
        $hostPath=Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe'
        $arguments='-NoLogo -NoProfile -NonInteractive -WindowStyle Hidden -ExecutionPolicy RemoteSigned -File "'+(Join-Path $install 'Run-AngeronaCleanup.ps1')+'" -Mode Watch -AdminRelaunch -StartupTicket "'+$ticketPath+'"'
        Write-CleanupJson -Path (Join-Path $install 'status.json') -Value @{utc=[DateTime]::UtcNow.ToString('o');watcher_alive=$false;startup_parent_pid=$PID;startup_parent_created=$parent.Created;permission=@{state='awaiting-windows-consent';administrator=$false;startup_prompt_attempted=$true};deferred=$true;reason='Waiting for the normal Windows administrator consent dialog.'}
        try{$child=& $Launcher $hostPath $arguments $ticketPath}catch{
            Write-CleanupJson -Path $cancelPath -Value @{nonce=$nonce;cancelled=$true}
            return @{action='fallback';state='consent-declined-or-unavailable';administrator=$false;reason='Windows administrator consent was declined or unavailable. Continuing with standard access; this watcher will not request it again.'}
        }
        if(-not $child -or -not $child.Id){throw 'Windows did not return a child process identity.'}
        $deadline=[DateTime]::UtcNow.AddSeconds(10)
        while([DateTime]::UtcNow -lt $deadline){
            if([IO.File]::Exists($readyPath)){
                try{$ready=Read-CleanupPrivateJson $readyPath 4096;$identity=& $ProcessProbe ([int]$child.Id)}catch{$ready=$null;$identity=$null}
                if($ready -and (Test-CleanupReady $ready $ticket $identity ([int]$child.Id) $hostPath) -and -not (Test-CleanupLeaseFree (Join-Path $install 'watcher.lock'))){
                    Write-CleanupJson -Path $ackPath -Value @{schema=1;nonce=$nonce;installation_id=$Configuration.installation_id;child_pid=[int]$child.Id;accepted=$true}
                    return @{action='handed-off';state='elevated-watcher-ready';administrator=$true;child_pid=[int]$child.Id;child_created=$identity.Created;reason='The same-account elevated watcher acquired its lease and confirmed startup.'}
                }
            }
            Start-Sleep -Milliseconds 100
        }
        Write-CleanupJson -Path $cancelPath -Value @{nonce=$nonce;cancelled=$true}
        $drain=[DateTime]::UtcNow.AddSeconds(5)
        while([DateTime]::UtcNow -lt $drain -and -not (Test-CleanupLeaseFree (Join-Path $install 'watcher.lock'))){Start-Sleep -Milliseconds 100}
        if(-not (Test-CleanupLeaseFree (Join-Path $install 'watcher.lock'))){return @{action='unverified';state='handoff-unverified';administrator=$false;reason='The elevated startup could not be verified; its cancellation was requested. A second watcher was not started.'}}
        return @{action='fallback';state='handoff-failed-standard';administrator=$false;reason='The elevated child did not confirm readiness. Its ticket was cancelled; continuing with standard access without another UAC request.'}
    }catch{
        if($cancelPath){try{Write-CleanupJson -Path $cancelPath -Value @{cancelled=$true}}catch{}}
        return @{action='fallback';state='startup-check-failed-standard';administrator=$false;reason=$_.Exception.Message}
    }finally{if($reservation){$reservation.Dispose()}}
}
function Read-CleanupStartupTicket {
    param([string]$Path,$Configuration)
    $expectedRoot=[IO.Path]::GetFullPath((Join-Path $Configuration.install_directory 'Handoffs')).TrimEnd('\')
    $full=[IO.Path]::GetFullPath($Path)
    if([IO.Path]::GetDirectoryName($full) -ine $expectedRoot -or [IO.Path]::GetFileName($full) -notmatch '^([0-9a-f]{32})\.ticket\.json$'){throw 'Invalid administrator handoff path.'}
    $ticket=Read-CleanupPrivateJson $full 8192
    if([IO.Path]::GetFileName($full) -cne ($ticket.nonce+'.ticket.json')){throw 'The startup handoff nonce does not match its path.'}
    $identity=Get-CleanupIdentity
    if(-not $identity -or -not $identity.Administrator -or $ticket.schema -ne 1 -or $ticket.nonce -notmatch '^[0-9a-f]{32}$' -or $ticket.owner_sid -ne $identity.Sid -or $ticket.owner_sid -ne $Configuration.owner_sid -or $ticket.installation_id -cne $Configuration.installation_id -or $ticket.install_directory -ine $Configuration.install_directory -or $ticket.parent_session -ne $identity.Session -or [DateTime]::Parse($ticket.expires_utc).ToUniversalTime() -le [DateTime]::UtcNow){throw 'The elevated watcher does not match the same-account startup ticket.'}
    $parent=Get-CleanupIdentity ([int]$ticket.parent_pid)
    $hostPath=Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe'
    if(-not $parent -or -not $parent.Alive -or $parent.Created -cne $ticket.parent_created -or $parent.Sid -ne $ticket.owner_sid -or $parent.Session -ne $ticket.parent_session -or $parent.Image -ine $hostPath){throw 'The startup parent lifetime could not be verified.'}
    if($ticket.config_sha256 -cne (Get-CleanupFileHash (Join-Path $Configuration.install_directory 'settings.json')) -or $ticket.authority_sha256 -cne (Get-CleanupFileHash (Join-Path $Configuration.install_directory 'scope-authority.json'))){throw 'Cleanup scope/configuration changed during administrator startup.'}
    $prefix=Join-Path $expectedRoot $ticket.nonce
    if([IO.File]::Exists($prefix+'.cancel.json') -or (Test-CleanupStopRequested $Configuration)){throw 'Administrator handoff was cancelled.'}
    return [pscustomobject]@{ticket=$ticket;identity=$identity;prefix=$prefix}
}
function Confirm-CleanupChildReady($Context,$Configuration){
    # Re-read the ticket after acquiring the watcher lease and initializing all
    # watcher services. A flag or a stale ready file grants no authority.
    $fresh=Read-CleanupStartupTicket ($Context.prefix+'.ticket.json') $Configuration
    Write-CleanupJson -Path ($Context.prefix+'.ready.json') -Value @{schema=1;nonce=$fresh.ticket.nonce;installation_id=$Configuration.installation_id;owner_sid=$fresh.identity.Sid;state='elevated-ready';child_pid=$PID;child_created=$fresh.identity.Created}
    $deadline=[DateTime]::UtcNow.AddSeconds(15)
    while([DateTime]::UtcNow -lt $deadline){
        if([IO.File]::Exists($Context.prefix+'.cancel.json') -or (Test-CleanupStopRequested $Configuration)){return $false}
        if([IO.File]::Exists($Context.prefix+'.ack.json')){
            $ack=Read-CleanupPrivateJson ($Context.prefix+'.ack.json') 4096
            return ($ack.schema -eq 1 -and $ack.nonce -ceq $fresh.ticket.nonce -and $ack.installation_id -ceq $Configuration.installation_id -and $ack.child_pid -eq $PID -and $ack.accepted -eq $true)
        }
        Start-Sleep -Milliseconds 100
    }
    return $false
}
Export-ModuleMember -Function Read-CleanupPrivateJson,Get-CleanupIdentity,Get-CleanupElevationDecision,Read-CleanupConfiguration,Test-CleanupStopRequested,Request-CleanupWatcherStop,Request-CleanupStartupElevation,Read-CleanupStartupTicket,Confirm-CleanupChildReady,Test-CleanupReady,Test-CleanupLeaseFree
