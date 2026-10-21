# Silent one-file recycling for already validated automatic duplicate records.
# No invocation at load and no permanent-delete or legacy shell fallback.
# API contracts: IFileOperation.SetOperationFlags and
# IFileOperationProgressSink.PreDeleteItem/PostDeleteItem on Microsoft Learn.
function Initialize-SCMSafeRecycle {
    if ('OptimusSafeRecycle' -as [type]) { return }
    Add-Type -TypeDefinition @'
using System;
using System.IO;
using System.Runtime.InteropServices;
using System.Threading;
[ComImport,Guid("43826d1e-e718-42ee-bc55-a1e261c37bfe"),InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
public interface IOptimusShellItem {
 [PreserveSig] int BindToHandler(IntPtr c,ref Guid h,ref Guid iid,out IntPtr r);
 [PreserveSig] int GetParent(out IOptimusShellItem p);
 [PreserveSig] int GetDisplayName(uint kind,out IntPtr name);
 [PreserveSig] int GetAttributes(uint mask,out uint attributes);
 [PreserveSig] int Compare(IOptimusShellItem other,uint hint,out int order);
}
[Guid("04b0f1a7-9490-44bc-96e1-4296a31252e2"),InterfaceType(ComInterfaceType.InterfaceIsIUnknown),ComVisible(true)]
public interface IOptimusFileProgress {
 [PreserveSig] int StartOperations();
 [PreserveSig] int FinishOperations(int result);
 [PreserveSig] int PreRenameItem(uint f,IOptimusShellItem i,[MarshalAs(UnmanagedType.LPWStr)] string n);
 [PreserveSig] int PostRenameItem(uint f,IOptimusShellItem i,[MarshalAs(UnmanagedType.LPWStr)] string n,int r,IOptimusShellItem c);
 [PreserveSig] int PreMoveItem(uint f,IOptimusShellItem i,IOptimusShellItem d,[MarshalAs(UnmanagedType.LPWStr)] string n);
 [PreserveSig] int PostMoveItem(uint f,IOptimusShellItem i,IOptimusShellItem d,[MarshalAs(UnmanagedType.LPWStr)] string n,int r,IOptimusShellItem c);
 [PreserveSig] int PreCopyItem(uint f,IOptimusShellItem i,IOptimusShellItem d,[MarshalAs(UnmanagedType.LPWStr)] string n);
 [PreserveSig] int PostCopyItem(uint f,IOptimusShellItem i,IOptimusShellItem d,[MarshalAs(UnmanagedType.LPWStr)] string n,int r,IOptimusShellItem c);
 [PreserveSig] int PreDeleteItem(uint flags,IOptimusShellItem item);
 [PreserveSig] int PostDeleteItem(uint flags,IOptimusShellItem item,int result,IOptimusShellItem created);
 [PreserveSig] int PreNewItem(uint f,IOptimusShellItem d,[MarshalAs(UnmanagedType.LPWStr)] string n);
 [PreserveSig] int PostNewItem(uint f,IOptimusShellItem d,[MarshalAs(UnmanagedType.LPWStr)] string n,[MarshalAs(UnmanagedType.LPWStr)] string t,uint a,int r,IOptimusShellItem c);
 [PreserveSig] int UpdateProgress(uint total,uint done);
 [PreserveSig] int ResetTimer();
 [PreserveSig] int PauseTimer();
 [PreserveSig] int ResumeTimer();
}
[ComImport,Guid("947aab5f-0a5c-4c13-b4d6-4bf7836fc9f8"),InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
public interface IOptimusFileOperation {
 [PreserveSig] int Advise(IOptimusFileProgress sink,out uint cookie);
 [PreserveSig] int Unadvise(uint cookie);
 [PreserveSig] int SetOperationFlags(uint flags);
 [PreserveSig] int SetProgressMessage([MarshalAs(UnmanagedType.LPWStr)] string message);
 [PreserveSig] int SetProgressDialog(IntPtr dialog);
 [PreserveSig] int SetProperties(IntPtr properties);
 [PreserveSig] int SetOwnerWindow(IntPtr owner);
 [PreserveSig] int ApplyPropertiesToItem(IOptimusShellItem item);
 [PreserveSig] int ApplyPropertiesToItems([MarshalAs(UnmanagedType.IUnknown)] object items);
 [PreserveSig] int RenameItem(IOptimusShellItem i,[MarshalAs(UnmanagedType.LPWStr)] string n,IOptimusFileProgress s);
 [PreserveSig] int RenameItems([MarshalAs(UnmanagedType.IUnknown)] object i,[MarshalAs(UnmanagedType.LPWStr)] string n);
 [PreserveSig] int MoveItem(IOptimusShellItem i,IOptimusShellItem d,[MarshalAs(UnmanagedType.LPWStr)] string n,IOptimusFileProgress s);
 [PreserveSig] int MoveItems([MarshalAs(UnmanagedType.IUnknown)] object i,IOptimusShellItem d);
 [PreserveSig] int CopyItem(IOptimusShellItem i,IOptimusShellItem d,[MarshalAs(UnmanagedType.LPWStr)] string n,IOptimusFileProgress s);
 [PreserveSig] int CopyItems([MarshalAs(UnmanagedType.IUnknown)] object i,IOptimusShellItem d);
 [PreserveSig] int DeleteItem(IOptimusShellItem item,IOptimusFileProgress sink);
 [PreserveSig] int DeleteItems([MarshalAs(UnmanagedType.IUnknown)] object items);
 [PreserveSig] int NewItem(IOptimusShellItem d,uint a,[MarshalAs(UnmanagedType.LPWStr)] string n,[MarshalAs(UnmanagedType.LPWStr)] string t,IOptimusFileProgress s);
 [PreserveSig] int PerformOperations();
 [PreserveSig] int GetAnyOperationsAborted([MarshalAs(UnmanagedType.Bool)] out bool aborted);
}
[ComVisible(true),ClassInterface(ClassInterfaceType.None)]
public sealed class OptimusRecycleSink:IOptimusFileProgress {
 public const int Abort=unchecked((int)0x80004004);
 public readonly string ExpectedPath;
 public uint PreFlags,PostFlags;
 public bool PreAllowed,PostObserved,RecycleDestinationConfirmed;
 public int DeleteResult=unchecked((int)0x80004005);
 public OptimusRecycleSink(string expected){ExpectedPath=Path.GetFullPath(expected);}
 public static bool IsRecycle(uint flags){return (flags&0x80)!=0;}
 public int StartOperations(){return 0;}
 public int FinishOperations(int result){return 0;}
 public int PreDeleteItem(uint flags,IOptimusShellItem item){
  PreFlags=flags;if(!IsRecycle(flags)||PreAllowed||item==null)return Abort;
  IntPtr name=IntPtr.Zero;
  try{
   if(item.GetDisplayName(0x80058000,out name)<0||name==IntPtr.Zero)return Abort;
   string actual=Path.GetFullPath(Marshal.PtrToStringUni(name));
   if(!actual.Equals(ExpectedPath,StringComparison.OrdinalIgnoreCase))return Abort;
   var a=File.GetAttributes(actual);
   if((a&(FileAttributes.Directory|FileAttributes.ReparsePoint|FileAttributes.System|FileAttributes.ReadOnly))!=0)return Abort;
   PreAllowed=true;return 0;
  }catch{return Abort;}finally{if(name!=IntPtr.Zero)Marshal.FreeCoTaskMem(name);}
 }
 public int PostDeleteItem(uint flags,IOptimusShellItem item,int result,IOptimusShellItem created){
  PostFlags=flags;DeleteResult=result;PostObserved=true;
  RecycleDestinationConfirmed=PreAllowed&&IsRecycle(flags)&&result>=0&&created!=null;
  return RecycleDestinationConfirmed?0:Abort;
 }
 public int PreRenameItem(uint f,IOptimusShellItem i,string n){return Abort;}
 public int PostRenameItem(uint f,IOptimusShellItem i,string n,int r,IOptimusShellItem c){return Abort;}
 public int PreMoveItem(uint f,IOptimusShellItem i,IOptimusShellItem d,string n){return Abort;}
 public int PostMoveItem(uint f,IOptimusShellItem i,IOptimusShellItem d,string n,int r,IOptimusShellItem c){return Abort;}
 public int PreCopyItem(uint f,IOptimusShellItem i,IOptimusShellItem d,string n){return Abort;}
 public int PostCopyItem(uint f,IOptimusShellItem i,IOptimusShellItem d,string n,int r,IOptimusShellItem c){return Abort;}
 public int PreNewItem(uint f,IOptimusShellItem d,string n){return Abort;}
 public int PostNewItem(uint f,IOptimusShellItem d,string n,string t,uint a,int r,IOptimusShellItem c){return Abort;}
 public int UpdateProgress(uint t,uint d){return 0;}
 public int ResetTimer(){return 0;}
 public int PauseTimer(){return 0;}
 public int ResumeTimer(){return 0;}
}
public sealed class OptimusRecycleResult{
 public bool Recycled,RecycleDestinationConfirmed,Silent;
 public uint PreDeleteFlags,PostDeleteFlags;
}
public static class OptimusSafeRecycle{
 // RECYCLEONDELETE, EARLYFAILURE, NOCOPYHOOKS, NOERRORUI, NOCONFIRMATION,
 // SILENT, ALLOWUNDO, NORECURSION, NO_CONNECTED_ELEMENTS. Never SHOWELEVATION.
 public const uint OperationFlags=0x00080000|0x00100000|0x00800000|0x0400|0x0010|0x0004|0x0040|0x1000|0x2000;
 [DllImport("shell32.dll",CharSet=CharSet.Unicode,PreserveSig=true)]
 static extern int SHCreateItemFromParsingName(string p,IntPtr c,ref Guid iid,[MarshalAs(UnmanagedType.Interface)]out IOptimusShellItem i);
 static void Check(int result){if(result<0)Marshal.ThrowExceptionForHR(result);}
 public static OptimusRecycleResult Recycle(string path){
  if(Thread.CurrentThread.GetApartmentState()!=ApartmentState.STA)throw new InvalidOperationException("Silent recycling requires the Windows PowerShell STA engine.");
  if(Environment.OSVersion.Version.Major<6||(Environment.OSVersion.Version.Major==6&&Environment.OSVersion.Version.Minor<2))throw new PlatformNotSupportedException("Recycle-only operations require Windows 8 or later.");
  string full=Path.GetFullPath(path);
  if(full.Length<4||full[1]!=':'||full[2]!='\\'||full.IndexOf(':',2)>=0)throw new ArgumentException("A single local file path is required.");
  if(new DriveInfo(Path.GetPathRoot(full)).DriveType!=DriveType.Fixed)throw new InvalidOperationException("A fixed local drive with Recycle Bin is required.");
  var info=new FileInfo(full);
  if(!info.Exists||(info.Attributes&(FileAttributes.Directory|FileAttributes.ReparsePoint|FileAttributes.System|FileAttributes.ReadOnly))!=0)throw new InvalidOperationException("Only an ordinary existing writable file can be recycled.");
  for(var directory=info.Directory;directory!=null;directory=directory.Parent)if((directory.Attributes&FileAttributes.ReparsePoint)!=0)throw new InvalidOperationException("Indirect file ancestors cannot be recycled.");
  IOptimusShellItem item=null;IOptimusFileOperation operation=null;
  try{
   Guid iid=new Guid("43826d1e-e718-42ee-bc55-a1e261c37bfe");Check(SHCreateItemFromParsingName(full,IntPtr.Zero,ref iid,out item));
   operation=(IOptimusFileOperation)Activator.CreateInstance(Type.GetTypeFromCLSID(new Guid("3ad05575-8857-4850-9277-11b85bdb8e09")));
   var sink=new OptimusRecycleSink(full);
   Check(operation.SetOperationFlags(OperationFlags));Check(operation.SetOwnerWindow(IntPtr.Zero));Check(operation.DeleteItem(item,sink));
   int performed=operation.PerformOperations();bool aborted;Check(operation.GetAnyOperationsAborted(out aborted));Check(performed);
   if(aborted||!sink.PreAllowed||!sink.PostObserved||!sink.RecycleDestinationConfirmed||File.Exists(full))throw new InvalidOperationException("Windows did not confirm a recycle-only operation; no fallback was attempted.");
   return new OptimusRecycleResult{Recycled=true,RecycleDestinationConfirmed=true,PreDeleteFlags=sink.PreFlags,PostDeleteFlags=sink.PostFlags,Silent=true};
  }finally{
   if(operation!=null&&Marshal.IsComObject(operation))Marshal.FinalReleaseComObject(operation);
   if(item!=null&&Marshal.IsComObject(item))Marshal.FinalReleaseComObject(item);
  }
 }
}
'@
}
function Invoke-SCMSafeRecycleFile {
    [CmdletBinding()]
    param([Parameter(Mandatory=$true)][string]$Path)
    Initialize-SCMSafeRecycle
    [OptimusSafeRecycle]::Recycle($Path)
}
