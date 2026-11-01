"""Repair one explicitly approved, relocated per-user Python installation.

No installer, elevation request, ACL change, file move, or security-policy change
is performed here. Callers must own the normal maintenance lease, bind failure
evidence to the exact WinGet offer, persist the user's exact mapping approval,
and verify installed versions after their own single update attempt.

Microsoft documents that junction creator/modifier privilege determines trust:
https://www.microsoft.com/en-us/msrc/blog/2025/06/redirectionguard-mitigating-unsafe-junction-traversal-in-windows
https://learn.microsoft.com/windows/win32/api/winioctl/ni-winioctl-fsctl_set_reparse_point
Reapplying identical reparse bytes as an administrator is an inference from that
documented mechanism. It counts as repaired ONLY after a fresh child process
enforcing RedirectionGuard can read the same Python DLL's security information.
"""
from __future__ import annotations

from contextlib import ExitStack, contextmanager
import ctypes
from dataclasses import dataclass
import hashlib
import json
import ntpath
import os
from pathlib import Path, PureWindowsPath
import re
import stat
import struct
import subprocess
import sys


_PACKAGE = re.compile(r'Python\.Python\.3\.([1-9][0-9]?)\Z', re.I)
_SHA256 = re.compile(r'[0-9a-f]{64}\Z')
_LOG_NAME = re.compile(r'winget_[0-9]{3}_core_JustForMe\.log\Z', re.I)
_TRUSTED_INSTALLER = 'S-1-5-80-956008885-3418522649-1831038044-1853292631-2271478464'
_TRUSTED_SIDS = {'S-1-5-18', 'S-1-5-32-544', _TRUSTED_INSTALLER}
_WRITE_MASK = 0x10000000 | 0x40000000 | 0x000D0000 | 0x00000156
# On an ordinary nonempty ancestor, adding a new sibling alone cannot replace
# the selected child. Deletion/renaming, DACL/owner edits and reparse metadata
# writes can invalidate the namespace after our temporary handles close.
_ANCESTOR_WRITE_MASK = _WRITE_MASK & ~0x6
_SID = re.compile(r'S-1-(?:[0-9]+-)*[0-9]+\Z')
_CREATOR_PLACEHOLDERS = {'S-1-3-0', 'S-1-3-1', 'S-1-3-2', 'S-1-3-3'}
_OWNER_RIGHTS = 'S-1-3-4'
_VALID_FILE_MASK = 0xF11F01FF
_MAX_LOG = 4 * 1024**2
_REPARSE = 0x400
_DIRECTORY = 0x10
_MOUNT_POINT = 0xA0000003


def _windows_path(value):
    if (not isinstance(value, str) or not 4 <= len(value) <= 2048
            or not re.match(r'^[A-Za-z]:\\', value) or '/' in value
            or any(ord(c) < 32 for c in value) or ':' in value[2:]
            or any(c in value for c in '*?"<>|') or ntpath.normpath(value) != value):
        raise ValueError('An ordinary canonical local Windows path is required.')
    path = PureWindowsPath(value)
    for part in path.parts[1:]:
        if (part.endswith((' ', '.')) or part in ('.', '..')
                or re.fullmatch(r'(?:CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\..*)?', part, re.I)):
            raise ValueError('Device names and ambiguous path components are not allowed.')
    return str(path)


@dataclass(frozen=True)
class ApprovedPythonRelocation:
    package_id: str
    source: str
    target: str
    reparse_sha256: str

    def __post_init__(self):
        match = _PACKAGE.fullmatch(self.package_id) if isinstance(self.package_id, str) else None
        if not match:
            raise ValueError('Only an exact Python.Python.3.x package can be approved.')
        source, target = _windows_path(self.source), _windows_path(self.target)
        if (not source.casefold().endswith('\\appdata\\local\\programs\\python')
                or len(PureWindowsPath(source).parts) < 7
                or PureWindowsPath(target).name.casefold() != 'python'
                or len(PureWindowsPath(target).parts) < 3
                or PureWindowsPath(source).drive.casefold() == PureWindowsPath(target).drive.casefold()
                or not isinstance(self.reparse_sha256, str) or not _SHA256.fullmatch(self.reparse_sha256)):
            raise ValueError('Approve the exact per-user Python junction, other-drive target, and byte hash.')
        object.__setattr__(self, 'package_id', 'Python.Python.3.' + match[1])
        object.__setattr__(self, 'source', source)
        object.__setattr__(self, 'target', target)

    def as_dict(self):
        return dict(package_id=self.package_id, source=self.source, target=self.target,
                    reparse_sha256=self.reparse_sha256)

    @classmethod
    def from_dict(cls, value):
        if not isinstance(value, dict) or set(value) != {'package_id', 'source', 'target', 'reparse_sha256'}:
            raise ValueError('An exact Python relocation approval is required.')
        return cls(**value)

    @property
    def dll(self):
        minor = _PACKAGE.fullmatch(self.package_id)[1]
        return str(PureWindowsPath(self.source) / ('Python3' + minor) / 'python3.dll')

    @property
    def target_dll(self):
        return str(PureWindowsPath(self.target) / PureWindowsPath(self.dll).relative_to(self.source))


def validate_python_relocation_approval(package_id, source, target, reparse_sha256):
    return ApprovedPythonRelocation(package_id, source, target, reparse_sha256)


def diagnose_python_relocation_failure(package_id, log_path, *, owned_log_root):
    """Parse bounded owned-attempt evidence, never an instruction in log text.

    The caller supplies its authenticated exact-offer attempt root. Structural
    containment alone does not establish which installer generated a log.
    """
    if not isinstance(package_id, str) or not _PACKAGE.fullmatch(package_id):
        return None
    try:
        root, path = Path(owned_log_root).absolute(), Path(log_path).absolute()
        relative = path.relative_to(root)
        if not relative.parts or not _LOG_NAME.fullmatch(path.name):
            return None
        for current in [root, *[root.joinpath(*relative.parts[:i]) for i in range(1, len(relative.parts))]]:
            info = current.lstat()
            if not stat.S_ISDIR(info.st_mode) or getattr(info, 'st_file_attributes', 0) & _REPARSE:
                return None
        before = path.lstat()
        if (not stat.S_ISREG(before.st_mode) or before.st_nlink != 1 or before.st_size > _MAX_LOG
                or getattr(before, 'st_file_attributes', 0) & _REPARSE):
            return None
        with path.open('rb') as stream:
            opened, raw = os.fstat(stream.fileno()), stream.read(_MAX_LOG + 1)
        after = path.lstat()
        identity = lambda row: (row.st_dev, row.st_ino, row.st_nlink, row.st_size, row.st_mtime_ns)
        if len(raw) > _MAX_LOG or not identity(before) == identity(opened) == identity(after):
            return None
        text = raw.decode('utf-16' if raw.startswith((b'\xff\xfe', b'\xfe\xff')) else 'utf-8-sig')
        found = set()
        minor = _PACKAGE.fullmatch(package_id)[1]
        for line in text.splitlines():
            match = re.search(r'\bError 1326\. Error getting file security: (.+?) GetLastError: 448(?:\s|$)', line)
            if match:
                dll = _windows_path(match[1])
                if tuple(p.casefold() for p in PureWindowsPath(dll).parts[-2:]) != (('Python3' + minor).casefold(), 'python3.dll'):
                    return None
                found.add(dll)
        if len(found) != 1:
            return None
        dll = found.pop()
        return dict(package_id='Python.Python.3.' + minor, error=1326, win32_error=448,
                    dll=dll, source=str(PureWindowsPath(dll).parent.parent),
                    log_sha256=hashlib.sha256(raw).hexdigest())
    except (OSError, ValueError, UnicodeError):
        return None


def _parse_mount_point(raw):
    if not isinstance(raw, bytes) or not 16 <= len(raw) <= 16384:
        raise ValueError('An ordinary mount-point buffer is required.')
    tag, length, reserved, offset, size, print_offset, print_size = struct.unpack_from('<IHHHHHH', raw)
    if (tag != _MOUNT_POINT or reserved or length + 8 != len(raw) or length < 8
            or any(value % 2 for value in (offset, size, print_offset, print_size))
            or size == 0 or max(offset + size, print_offset + print_size) > len(raw) - 16):
        raise ValueError('The existing junction buffer is malformed or unsupported.')
    destination = raw[16 + offset:16 + offset + size].decode('utf-16-le')
    if not destination.startswith('\\??\\'):
        raise ValueError('Only an ordinary drive-letter mount point is supported.')
    return _windows_path(destination[4:])


def _path_chain(path):
    parts = PureWindowsPath(path).parts
    return [str(PureWindowsPath(*parts[:i])) for i in range(1, len(parts) + 1)]


class _UnsafeNamespaceError(ValueError):
    def __init__(self, reason, path, role):
        super().__init__(reason + (': ' + path if path else '.'))
        self.path, self.role = path, role


def _check_security(info, owner, *, role='leaf', path=None):
    """Reject persistent namespace authority outside the approved account.

    This is deliberately conservative, not a general Windows AccessCheck
    implementation: deny ACEs never cancel an unsafe allow ACE for this gate.
    Harmless ancestor sibling creation is permitted, while every selected
    directory's deletion and every parent's DELETE_CHILD remain forbidden.

    https://learn.microsoft.com/windows/win32/api/winbase/nf-winbase-deletefile
    https://learn.microsoft.com/windows/win32/secauthz/ace-inheritance-rules
    """
    def unsafe(reason):
        raise _UnsafeNamespaceError(reason, path, role)

    if not isinstance(owner, str) or not _SID.fullmatch(owner):
        unsafe('The approved account identity is unavailable')
    allowed = _TRUSTED_SIDS | {owner}
    if (not isinstance(info, dict) or not isinstance(info.get('owner'), str)
            or info['owner'] not in allowed or type(info.get('aces')) is not list or not info['aces']):
        unsafe('The Python namespace needs a known owner and explicit DACL')
    forbidden = _ANCESTOR_WRITE_MASK if role == 'ancestor' else _WRITE_MASK
    for ace in info['aces']:
        # Validate before interpreting inherit-only, including boolean values
        # that Python otherwise considers integers. Unrecognized object,
        # callback or audit ACEs must not silently widen automatic repair.
        if (not isinstance(ace, dict) or type(ace.get('type')) is not int
                or ace['type'] not in (0, 1) or type(ace.get('flags')) is not int
                or ace['flags'] < 0 or ace['flags'] & ~0x1F
                or type(ace.get('mask')) is not int or not 0 <= ace['mask'] <= 0xFFFFFFFF
                or ace['mask'] & ~_VALID_FILE_MASK
                or not isinstance(ace.get('sid'), str) or not _SID.fullmatch(ace['sid'])):
            unsafe('An unsupported or incomplete access-control entry needs review')
        # Inherit-only grants do not authorize access to an existing component.
        # CREATOR_OWNER/GROUP are placeholders replaced on effective inherited
        # ACEs. Never treat an unresolved effective placeholder as the caller.
        if ace['flags'] & 8:
            continue
        if ace['sid'] in _CREATOR_PLACEHOLDERS:
            unsafe('An unmapped effective creator access-control entry needs review')
        sid = info['owner'] if ace['sid'] == _OWNER_RIGHTS else ace['sid']
        if ace['type'] == 0 and ace['mask'] & forbidden and sid not in allowed:
            unsafe('Another account can modify or replace the Python namespace')


@contextmanager
def _guarded_mapping(approval, api, *, write):
    """Hold non-following handles against writes/deletion for the entire check.

    Every component must remain protected after these handles close. Ancestor
    ACLs may permit creation of unrelated siblings (e.g. C:\\Users), but must not
    grant another account deletion, child replacement or security/reparse edits.
    The junction, target, interpreter and DLL retain the stricter write gate.
    """
    if not isinstance(approval, ApprovedPythonRelocation):
        raise ValueError('A validated explicit approval is required.')
    owner = api.current_user_sid()
    for drive in {PureWindowsPath(approval.source).anchor, PureWindowsPath(approval.target).anchor}:
        if api.filesystem(drive) != (3, 'NTFS'):
            raise ValueError('Only fixed local NTFS source and target volumes are supported.')
    with ExitStack() as stack:
        handles = {}
        paths = _path_chain(approval.source) + _path_chain(approval.target)
        paths += [str(PureWindowsPath(approval.target_dll).parent), approval.target_dll]
        roles = {approval.source.casefold(): 'junction', approval.target.casefold(): 'target',
                 str(PureWindowsPath(approval.target_dll).parent).casefold(): 'interpreter',
                 approval.target_dll.casefold(): 'dll'}
        for path in paths:
            key = path.casefold()
            if key in handles:
                continue
            handle = stack.enter_context(api.guard_path(path, write=write and key == approval.source.casefold()))
            info = api.info(handle)
            directory = key != approval.target_dll.casefold()
            if bool(info['attributes'] & _DIRECTORY) != directory:
                raise ValueError('An approved path has an unexpected object type.')
            if key != approval.source.casefold() and info['attributes'] & _REPARSE:
                raise ValueError('Additional reparse points are not permitted in the approved mapping.')
            if key == approval.source.casefold() and not info['attributes'] & _REPARSE:
                raise ValueError('The approved source is no longer a junction.')
            role = roles.get(key, 'ancestor')
            try:
                security = api.security(handle)
            except (OSError, ValueError, TypeError) as error:
                raise _UnsafeNamespaceError('The namespace access-control information is unavailable', path, role) from error
            _check_security(security, owner, role=role, path=path)
            handles[key] = handle
        source = handles[approval.source.casefold()]
        raw = api.get_reparse(source)
        if (_parse_mount_point(raw).casefold() != approval.target.casefold()
                or hashlib.sha256(raw).hexdigest() != approval.reparse_sha256):
            raise ValueError('The approved junction target or reparse bytes changed.')
        identity = api.info(source)['identity']
        yield source, raw
        if api.info(source)['identity'] != identity or api.get_reparse(source) != raw:
            raise ValueError('The approved junction identity or bytes changed during repair.')


def _result(status, code, note, *, mutated=False, probe_error=None):
    return dict(status=status, code=code, note=note, mutated=mutated, probe_error=probe_error)


def preflight_python_relocation(approval, *, api=None):
    """Read-only mapping validation and isolated enforced security-read probe."""
    try:
        api = _WindowsAPI() if api is None else api
        with _guarded_mapping(approval, api, write=False):
            probe = api.probe(approval.dll)
            if probe.get('supported') is not True or type(probe.get('error')) is not int:
                return _result('review_required', 'probe_unavailable', 'The enforced native security probe is unavailable.')
            if probe['error'] == 0:
                return _result('already_trusted', 'already_trusted', 'The approved Python path already passes RedirectionGuard.', probe_error=0)
            if probe['error'] != 448:
                return _result('review_required', 'different_native_error', 'The Python path failed a different native check.', probe_error=probe['error'])
            return _result('ready', 'untrusted_approved_junction', 'The exact approved junction reproduces native error 448.', probe_error=448)
    except NotImplementedError:
        return _result('unsupported', 'windows_only', 'Python relocation repair is available on Windows only.')
    except _UnsafeNamespaceError as error:
        return dict(_result('review_required', 'unsafe_namespace', str(error)[:240]),
                    unsafe_path=error.path, path_role=error.role)
    except (OSError, ValueError, TypeError, KeyError) as error:
        return _result('review_required', 'mapping_validation_failed', str(error)[:240])


def repair_python_relocation(approval, *, api=None):
    """Reapply identical bytes on one approved handle, never retry an installer."""
    mutated = False
    try:
        api = _WindowsAPI() if api is None else api
        if not api.is_elevated():
            return _result('review_required', 'administrator_required', 'Administrator approval is required for this exact junction repair.')
        with _guarded_mapping(approval, api, write=True) as (handle, raw):
            before = api.probe(approval.dll)
            if before.get('supported') is not True or type(before.get('error')) is not int:
                return _result('review_required', 'probe_unavailable', 'The enforced native security probe is unavailable.')
            if before['error'] == 0:
                return _result('already_trusted', 'already_trusted', 'The approved Python path already passes RedirectionGuard.', probe_error=0)
            if before['error'] != 448:
                return _result('review_required', 'different_native_error', 'A different native failure must be reviewed.', probe_error=before['error'])
            # No unlink/recreate interval, target change, ACL edit or trust-stream
            # manipulation. The kernel applies normal administrator semantics.
            api.set_reparse(handle, raw)
            mutated = True
            if api.get_reparse(handle) != raw:
                raise ValueError('The junction buffer changed unexpectedly after the native operation.')
            after = api.probe(approval.dll)
            if after.get('supported') is not True or type(after.get('error')) is not int or after.get('error') != 0:
                return _result('failed', 'repair_not_verified', 'The approved junction operation did not pass the enforced read probe.',
                               mutated=True, probe_error=after.get('error'))
        return _result('repaired', 'verified_trusted_junction', 'The unchanged Python junction now passes an enforced RedirectionGuard security read.', mutated=True, probe_error=0)
    except NotImplementedError:
        return _result('unsupported', 'windows_only', 'Python relocation repair is available on Windows only.')
    except _UnsafeNamespaceError as error:
        return dict(_result('failed' if mutated else 'review_required', 'unsafe_namespace', str(error)[:240], mutated=mutated),
                    unsafe_path=error.path, path_role=error.role)
    except (OSError, ValueError, TypeError, KeyError) as error:
        return _result('failed' if mutated else 'review_required', 'native_repair_failed', str(error)[:240], mutated=mutated)


_PROBE_PYTHON = r'''
import ctypes,json,sys
from ctypes import wintypes as w
k=ctypes.WinDLL('kernel32',use_last_error=True); a=ctypes.WinDLL('advapi32',use_last_error=True)
k.SetProcessMitigationPolicy.argtypes=[ctypes.c_int,ctypes.c_void_p,ctypes.c_size_t]
k.GetCurrentProcess.restype=w.HANDLE
k.GetProcessMitigationPolicy.argtypes=[w.HANDLE,ctypes.c_int,ctypes.c_void_p,ctypes.c_size_t]
a.GetNamedSecurityInfoW.argtypes=[w.LPWSTR,w.DWORD,w.DWORD,ctypes.c_void_p,ctypes.c_void_p,ctypes.c_void_p,ctypes.c_void_p,ctypes.POINTER(ctypes.c_void_p)]
a.GetNamedSecurityInfoW.restype=w.DWORD; k.LocalFree.argtypes=[ctypes.c_void_p]
f=w.DWORD(1); actual=w.DWORD(); sd=ctypes.c_void_p()
ok=bool(k.SetProcessMitigationPolicy(16,ctypes.byref(f),ctypes.sizeof(f)))
ok=ok and bool(k.GetProcessMitigationPolicy(k.GetCurrentProcess(),16,ctypes.byref(actual),ctypes.sizeof(actual))) and bool(actual.value & 1)
error=int(a.GetNamedSecurityInfoW(json.load(sys.stdin)['path'],1,7,None,None,None,None,ctypes.byref(sd))) if ok else None
if sd.value:k.LocalFree(sd)
print(json.dumps({'supported':ok,'error':error}))
'''


def probe_redirection_trust(path, *, python_executable=None):
    """A bounded fresh child only ENABLES its own policy, then reads security."""
    if os.name != 'nt':
        return dict(supported=False, error=None)
    try:
        path = _windows_path(path)
        executable = python_executable or (None if getattr(sys, 'frozen', False) else sys.executable)
        options = dict(capture_output=True, text=True, timeout=25, creationflags=0x08000000)
        if executable:
            completed = subprocess.run([str(executable), '-I', '-S', '-c', _PROBE_PYTHON],
                                       input=json.dumps({'path': path}), **options)
        else:
            # Frozen releases do not assume a separately installed Python. The
            # stock, no-profile Windows PowerShell child contains fixed C# only.
            import base64
            encoded = base64.b64encode(path.encode('utf-16-le')).decode('ascii')
            script = r'''$ErrorActionPreference='Stop'; Add-Type -TypeDefinition @'
using System; using System.Runtime.InteropServices;
public static class OptimusTrustRead {
 [DllImport("kernel32.dll",SetLastError=true)] static extern bool SetProcessMitigationPolicy(int p,ref uint b,UIntPtr n);
 [DllImport("kernel32.dll")] static extern IntPtr GetCurrentProcess();
 [DllImport("kernel32.dll",SetLastError=true)] static extern bool GetProcessMitigationPolicy(IntPtr h,int p,out uint b,UIntPtr n);
 [DllImport("advapi32.dll",CharSet=CharSet.Unicode)] static extern uint GetNamedSecurityInfo(string n,uint t,uint i,IntPtr o,IntPtr g,IntPtr d,IntPtr s,out IntPtr sd);
 [DllImport("kernel32.dll")] static extern IntPtr LocalFree(IntPtr p);
 public static string Probe(string path) { uint f=1,actual; if(!SetProcessMitigationPolicy(16,ref f,(UIntPtr)4) || !GetProcessMitigationPolicy(GetCurrentProcess(),16,out actual,(UIntPtr)4) || (actual & 1)==0) return "{\"supported\":false,\"error\":null}";
 IntPtr sd; uint e=GetNamedSecurityInfo(path,1,7,IntPtr.Zero,IntPtr.Zero,IntPtr.Zero,IntPtr.Zero,out sd); if(sd!=IntPtr.Zero)LocalFree(sd);
 return "{\"supported\":true,\"error\":"+e.ToString()+"}"; }
}
'@
[OptimusTrustRead]::Probe([Text.Encoding]::Unicode.GetString([Convert]::FromBase64String(''' + "'" + encoded + "'" + ')))'
            command = base64.b64encode(script.encode('utf-16-le')).decode('ascii')
            # Resolve the system binary using a native API, not a mutable
            # environment variable inherited by an elevated parent.
            kernel = ctypes.WinDLL('kernel32', use_last_error=True)
            kernel.GetSystemDirectoryW.argtypes = [ctypes.c_wchar_p, ctypes.c_uint]
            directory = ctypes.create_unicode_buffer(32768)
            length = kernel.GetSystemDirectoryW(directory, len(directory))
            if not 0 < length < len(directory):
                return dict(supported=False, error=None)
            shell = Path(directory.value) / 'WindowsPowerShell/v1.0/powershell.exe'
            completed = subprocess.run([str(shell), '-NoProfile', '-NonInteractive', '-EncodedCommand', command], **options)
        value = json.loads(completed.stdout.strip()) if completed.returncode == 0 else {}
        if value.get('supported') is True and type(value.get('error')) is int and 0 <= value['error'] <= 0xffffffff:
            return dict(supported=True, error=value['error'])
    except (OSError, ValueError, TypeError, subprocess.SubprocessError):
        pass
    return dict(supported=False, error=None)


class _WindowsAPI:
    """Minimal native surface; no registry, task, installer or elevation APIs."""
    def __init__(self):
        if os.name != 'nt':
            raise NotImplementedError
        from ctypes import wintypes as w
        self.w = w
        self.k = ctypes.WinDLL('kernel32', use_last_error=True)
        self.a = ctypes.WinDLL('advapi32', use_last_error=True)
        k, a = self.k, self.a
        k.GetCurrentProcess.restype = w.HANDLE
        k.CloseHandle.argtypes = [w.HANDLE]
        k.LocalFree.argtypes = [ctypes.c_void_p]
        k.CreateFileW.argtypes = [w.LPCWSTR, w.DWORD, w.DWORD, ctypes.c_void_p, w.DWORD, w.DWORD, w.HANDLE]
        k.CreateFileW.restype = w.HANDLE
        k.DeviceIoControl.argtypes = [w.HANDLE, w.DWORD, ctypes.c_void_p, w.DWORD, ctypes.c_void_p, w.DWORD, ctypes.POINTER(w.DWORD), ctypes.c_void_p]
        k.GetFileInformationByHandle.argtypes = [w.HANDLE, ctypes.c_void_p]
        k.GetDriveTypeW.argtypes = [w.LPCWSTR]
        k.GetVolumeInformationW.argtypes = [w.LPCWSTR, w.LPWSTR, w.DWORD, ctypes.POINTER(w.DWORD), ctypes.POINTER(w.DWORD), ctypes.POINTER(w.DWORD), w.LPWSTR, w.DWORD]
        a.OpenProcessToken.argtypes = [w.HANDLE, w.DWORD, ctypes.POINTER(w.HANDLE)]
        a.GetTokenInformation.argtypes = [w.HANDLE, ctypes.c_int, ctypes.c_void_p, w.DWORD, ctypes.POINTER(w.DWORD)]
        a.ConvertSidToStringSidW.argtypes = [ctypes.c_void_p, ctypes.POINTER(w.LPWSTR)]
        a.ConvertStringSidToSidW.argtypes = [w.LPCWSTR, ctypes.POINTER(ctypes.c_void_p)]
        a.CheckTokenMembership.argtypes = [w.HANDLE, ctypes.c_void_p, ctypes.POINTER(w.BOOL)]
        a.GetSecurityInfo.argtypes = [w.HANDLE, w.DWORD, w.DWORD, ctypes.POINTER(ctypes.c_void_p), ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p), ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p)]
        a.GetSecurityInfo.restype = w.DWORD
        a.GetAce.argtypes = [ctypes.c_void_p, w.DWORD, ctypes.POINTER(ctypes.c_void_p)]

    def _error(self):
        return ctypes.WinError(ctypes.get_last_error())

    @contextmanager
    def _token(self):
        token = self.w.HANDLE()
        if not self.a.OpenProcessToken(self.k.GetCurrentProcess(), 8, ctypes.byref(token)):
            raise self._error()
        try:
            yield token
        finally:
            self.k.CloseHandle(token)

    def is_elevated(self):
        value, length = self.w.DWORD(), self.w.DWORD()
        with self._token() as token:
            if not self.a.GetTokenInformation(token, 20, ctypes.byref(value), ctypes.sizeof(value), ctypes.byref(length)):
                raise self._error()
        if value.value != 1:
            return False
        administrators, member = ctypes.c_void_p(), self.w.BOOL()
        if not self.a.ConvertStringSidToSidW('S-1-5-32-544', ctypes.byref(administrators)):
            raise self._error()
        try:
            if not self.a.CheckTokenMembership(None, administrators, ctypes.byref(member)):
                raise self._error()
            return bool(member.value)
        finally:
            self.k.LocalFree(administrators)

    def _sid(self, pointer):
        text = self.w.LPWSTR()
        if not pointer or not self.a.ConvertSidToStringSidW(pointer, ctypes.byref(text)):
            raise self._error()
        try:
            return text.value
        finally:
            self.k.LocalFree(ctypes.cast(text, ctypes.c_void_p))

    def current_user_sid(self):
        size = self.w.DWORD()
        with self._token() as token:
            self.a.GetTokenInformation(token, 1, None, 0, ctypes.byref(size))
            if not 0 < size.value < 65536:
                raise ValueError('The current account identity is unavailable.')
            buffer = ctypes.create_string_buffer(size.value)
            if not self.a.GetTokenInformation(token, 1, buffer, size, ctypes.byref(size)):
                raise self._error()
            return self._sid(ctypes.cast(buffer, ctypes.POINTER(ctypes.c_void_p)).contents.value)

    def filesystem(self, drive):
        name = ctypes.create_unicode_buffer(32)
        if not self.k.GetVolumeInformationW(drive, None, 0, None, None, None, name, len(name)):
            raise self._error()
        return self.k.GetDriveTypeW(drive), name.value

    @contextmanager
    def guard_path(self, path, *, write):
        # All objects opened without following the final component. Share READ
        # only prevents replacement/reparse changes while this handle is held.
        access = 0x20080 | (0x40000000 if write else 0)
        handle = self.k.CreateFileW(path, access, 1, None, 3, 0x02200000, None)
        if handle == ctypes.c_void_p(-1).value:
            raise self._error()
        try:
            yield handle
        finally:
            self.k.CloseHandle(handle)

    def info(self, handle):
        class Info(ctypes.Structure):
            _fields_ = [('attributes', self.w.DWORD), ('created', self.w.FILETIME),
                        ('accessed', self.w.FILETIME), ('modified', self.w.FILETIME),
                        ('volume', self.w.DWORD), ('size_high', self.w.DWORD),
                        ('size_low', self.w.DWORD), ('links', self.w.DWORD),
                        ('index_high', self.w.DWORD), ('index_low', self.w.DWORD)]
        value = Info()
        if not self.k.GetFileInformationByHandle(handle, ctypes.byref(value)):
            raise self._error()
        return dict(attributes=value.attributes, identity=(value.volume, value.index_high, value.index_low))

    def security(self, handle):
        owner, dacl, sd = ctypes.c_void_p(), ctypes.c_void_p(), ctypes.c_void_p()
        status = self.a.GetSecurityInfo(handle, 1, 5, ctypes.byref(owner), None, ctypes.byref(dacl), None, ctypes.byref(sd))
        if status:
            raise ctypes.WinError(status)
        try:
            if not dacl.value:
                raise ValueError('A null DACL cannot be approved.')
            header = ctypes.string_at(dacl, 8)
            count = struct.unpack_from('<H', header, 4)[0]
            if count > 256:
                raise ValueError('An unusually complex DACL needs review.')
            aces = []
            for index in range(count):
                pointer = ctypes.c_void_p()
                if not self.a.GetAce(dacl, index, ctypes.byref(pointer)):
                    raise self._error()
                head = ctypes.string_at(pointer, 4)
                kind, flags, size = struct.unpack('<BBH', head)
                if size < 8:
                    raise ValueError('A malformed DACL needs review.')
                if kind not in (0, 1):
                    aces.append(dict(type=kind, flags=flags))
                else:
                    mask = struct.unpack('<I', ctypes.string_at(pointer.value + 4, 4))[0]
                    aces.append(dict(type=kind, flags=flags, mask=mask, sid=self._sid(pointer.value + 8)))
            return dict(owner=self._sid(owner), aces=aces)
        finally:
            if sd.value:
                self.k.LocalFree(sd)

    def get_reparse(self, handle):
        buffer, size = ctypes.create_string_buffer(16384), self.w.DWORD()
        if not self.k.DeviceIoControl(handle, 0x900A8, None, 0, buffer, len(buffer), ctypes.byref(size), None):
            raise self._error()
        return buffer.raw[:size.value]

    def set_reparse(self, handle, raw):
        if not self.is_elevated():
            raise ValueError('A true elevated token is required.')
        buffer, size = ctypes.create_string_buffer(raw), self.w.DWORD()
        if not self.k.DeviceIoControl(handle, 0x900A4, buffer, len(raw), None, 0, ctypes.byref(size), None):
            raise self._error()

    def probe(self, path):
        return probe_redirection_trust(path)
