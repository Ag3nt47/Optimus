Set-StrictMode -Version 2

function Initialize-CleanupNative {
    if ('AngeronaDisposableCleanup.SafeTempFiles' -as [type]) { return }
    Add-Type -TypeDefinition @'
using System;
using System.IO;
using System.Collections.Generic;
using System.Runtime.InteropServices;
using Microsoft.Win32.SafeHandles;
namespace AngeronaDisposableCleanup {
 public static class SafeTempFiles {
  [StructLayout(LayoutKind.Sequential)] struct Info {
   public uint Attributes; public System.Runtime.InteropServices.ComTypes.FILETIME Creation, Access, Write;
   public uint Volume, SizeHigh, SizeLow, Links, IndexHigh, IndexLow;
  }
  [StructLayout(LayoutKind.Sequential)] struct Disposition { [MarshalAs(UnmanagedType.Bool)] public bool Delete; }
  [DllImport("kernel32.dll", CharSet=CharSet.Unicode, SetLastError=true)] static extern SafeFileHandle CreateFileW(string p,uint a,uint s,IntPtr sec,uint mode,uint flags,IntPtr t);
  [DllImport("kernel32.dll", SetLastError=true)] static extern bool GetFileInformationByHandle(SafeFileHandle h,out Info i);
  [DllImport("kernel32.dll", SetLastError=true)] static extern bool SetFileInformationByHandle(SafeFileHandle h,int c,ref Disposition i,uint n);
  [DllImport("user32.dll", CharSet=CharSet.Unicode, SetLastError=true)] static extern IntPtr SendMessageTimeout(IntPtr h,uint m,IntPtr w,string l,uint f,uint t,out IntPtr r);
  public static void NotifyEnvironment() { IntPtr result; SendMessageTimeout(new IntPtr(0xffff),0x1a,IntPtr.Zero,"Environment",2,3000,out result); }
  static long Stamp(System.Runtime.InteropServices.ComTypes.FILETIME f) { return ((long)(uint)f.dwHighDateTime<<32)|(uint)f.dwLowDateTime; }
  public static bool Act(string root,string path,long cutoff,long budget,bool delete,out long bytes,out string reason) {
   bytes=0; reason="unknown"; var parents=new List<SafeFileHandle>(); SafeFileHandle file=null;
   try {
    root=Path.GetFullPath(root).TrimEnd('\\'); path=Path.GetFullPath(path);
    if(!path.StartsWith(root+"\\",StringComparison.OrdinalIgnoreCase)) { reason="outside-root"; return false; }
    string directory=Path.GetDirectoryName(path), current=Path.GetPathRoot(directory);
    var names=directory.Substring(current.Length).Split(new char[]{'\\'},StringSplitOptions.RemoveEmptyEntries);
    var paths=new List<string>(); paths.Add(current); foreach(string name in names) { current=Path.Combine(current,name); paths.Add(current); }
    // Keep every ancestor open without FILE_SHARE_DELETE. A directory cannot be
    // exchanged for a junction between the path checks and deletion.
    foreach(string parent in paths) {
     var h=CreateFileW(parent,0x80,3,IntPtr.Zero,3,0x02200000,IntPtr.Zero); parents.Add(h); Info i;
     if(h.IsInvalid || !GetFileInformationByHandle(h,out i)) { reason="parent-unavailable"; return false; }
     if((i.Attributes&0x400)!=0 || (i.Attributes&0x10)==0) { reason="reparse-parent"; return false; }
    }
    file=CreateFileW(path,0x10080,0,IntPtr.Zero,3,0x00200000,IntPtr.Zero); Info info;
    if(file.IsInvalid || !GetFileInformationByHandle(file,out info)) { reason="locked-or-inaccessible"; return false; }
    if((info.Attributes&(0x400|0x10|0x1|0x4|0x1000))!=0) { reason="protected-attributes"; return false; }
    if(info.Links!=1) { reason="multiple-hard-links"; return false; }
    if(Stamp(info.Write)>cutoff || Stamp(info.Creation)>cutoff) { reason="too-recent"; return false; }
    bytes=((long)info.SizeHigh<<32)|info.SizeLow;
    if(bytes>budget) { bytes=0; reason="per-run-byte-limit"; return false; }
    if(delete) { var disposition=new Disposition { Delete=true };
     if(!SetFileInformationByHandle(file,4,ref disposition,(uint)Marshal.SizeOf(typeof(Disposition)))) { bytes=0; reason="delete-denied"; return false; }
    }
    reason=delete?"deleted":"eligible"; return true;
   } catch { bytes=0; reason="inspection-failed"; return false; }
   finally { if(file!=null) file.Dispose(); for(int n=parents.Count-1;n>=0;n--) parents[n].Dispose(); }
  }
 }
}
'@
}

function Invoke-SafeCleanup {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory=$true)][string]$Root,
        [ValidateRange(0,8760)][int]$MinimumAgeHours=0,
        [ValidateRange(1,100000)][int]$MaximumFiles=5000,
        [ValidateRange(1,107374182400)][long]$MaximumBytes=4294967296,
        [ValidateRange(1,300)][int]$MaximumSeconds=90,
        [switch]$Apply,
        [long]$StopAtFreeBytes=0,
        [string]$FreeSpaceDrive='C:\',
        [string[]]$FileNames=@(),
        [scriptblock]$ContinueCheck
    )
    Initialize-CleanupNative
    $rootFull=[IO.Path]::GetFullPath($Root).TrimEnd('\')
    if ($rootFull -eq [IO.Path]::GetPathRoot($Root).TrimEnd('\')) { throw 'A drive root is never a cleanup root.' }
    $counts=@{}; $seen=0; $eligible=0L; $deleted=0L; $filesDeleted=0; $limited=$false
    $watch=[Diagnostics.Stopwatch]::StartNew()
    $cutoff=[DateTime]::UtcNow.AddHours(-$MinimumAgeHours).ToFileTimeUtc()
    $stack=New-Object 'System.Collections.Generic.Stack[string]'; $stack.Push($rootFull)
    while ($stack.Count -gt 0) {
        if ($seen -ge $MaximumFiles -or $watch.Elapsed.TotalSeconds -ge $MaximumSeconds) { $limited=$true; break }
        $directory=$stack.Pop()
        try {
            $info=New-Object IO.DirectoryInfo($directory)
            if (($info.Attributes -band [IO.FileAttributes]::ReparsePoint) -or -not $info.Exists) { continue }
            foreach ($entry in $info.EnumerateFileSystemInfos()) {
                if ($seen -ge $MaximumFiles -or $watch.Elapsed.TotalSeconds -ge $MaximumSeconds) { $limited=$true; break }
                if ($entry.Attributes -band [IO.FileAttributes]::ReparsePoint) { $counts['reparse-entry']=1+[int]$counts['reparse-entry']; continue }
                if ($entry.Attributes -band [IO.FileAttributes]::Directory) { if($FileNames.Count -eq 0){$stack.Push($entry.FullName)}; continue }
                if($FileNames.Count -gt 0 -and $entry.Name -notin $FileNames){$counts['outside-log-allowlist']=1+[int]$counts['outside-log-allowlist'];continue}
                if($ContinueCheck -and -not (& $ContinueCheck)){$counts['runtime-started']=1;$limited=$true;$stack.Clear();break}
                $seen++; $bytes=0L; $reason=''
                $remaining=$MaximumBytes-$deleted
                if (-not $Apply) { $remaining=$MaximumBytes-$eligible }
                if ($remaining -le 0) { $limited=$true; break }
                $ok=[AngeronaDisposableCleanup.SafeTempFiles]::Act($rootFull,$entry.FullName,$cutoff,$remaining,[bool]$Apply,[ref]$bytes,[ref]$reason)
                $counts[$reason]=1+[int]$counts[$reason]
                if ($ok) { $eligible+=$bytes; if ($Apply) { $deleted+=$bytes; $filesDeleted++ } }
                if ($Apply -and $StopAtFreeBytes -gt 0 -and (New-Object IO.DriveInfo($FreeSpaceDrive)).AvailableFreeSpace -ge $StopAtFreeBytes) { $stack.Clear(); break }
            }
        } catch { $counts['directory-inaccessible']=1+[int]$counts['directory-inaccessible'] }
    }
    [pscustomobject]@{root=$rootFull; mode=$(if($Apply){'cleanup'}else{'audit'}); minimum_age_hours=$MinimumAgeHours; files_inspected=$seen; eligible_bytes=$eligible; deleted_bytes=$deleted; deleted_files=$filesDeleted; bounded=$limited; reasons=$counts; seconds=[Math]::Round($watch.Elapsed.TotalSeconds,2)}
}

function Write-CleanupJson {
    param([Parameter(Mandatory=$true)][string]$Path,[Parameter(Mandatory=$true)]$Value)
    $temp=$Path+'.'+[Guid]::NewGuid().ToString('N')+'.tmp'
    try {
        [IO.File]::WriteAllText($temp,($Value|ConvertTo-Json -Depth 12),(New-Object Text.UTF8Encoding($false)))
        for($attempt=0;$attempt -lt 8;$attempt++) {
            try {
                if(Test-Path -LiteralPath $Path){[IO.File]::Replace($temp,$Path,[System.Management.Automation.Language.NullString]::Value)}else{[IO.File]::Move($temp,$Path)}
                break
            } catch [IO.IOException] {
                $nativeCode=$_.Exception.HResult -band 65535
                if($nativeCode -notin @(32,33) -or $attempt -eq 7){throw}
                Start-Sleep -Milliseconds ([Math]::Min(200,25*[Math]::Pow(2,$attempt)))
            }
        }
    }
    finally { if(Test-Path -LiteralPath $temp){[IO.File]::Delete($temp)} }
}
Export-ModuleMember -Function Invoke-SafeCleanup,Write-CleanupJson

