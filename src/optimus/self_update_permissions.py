"""Current-user ACLs for private Windows updater directories.

Python's Windows 0700 directory policy can grant OWNER RIGHTS to an elevated
Administrators owner. Use the actual TokenUser SID instead. This module never
decrypts keys, edits evidence, recursively resets ACLs, or changes release files.
"""
from __future__ import annotations

import ctypes
import os
from pathlib import Path
import re
import stat

from .filesystem import _ordinary_data_path

_SID = re.compile(r'S-1-[0-9]+(?:-[0-9]+){1,14}\Z')
_ADMIN = 'S-1-5-32-544'
_WELL_KNOWN = {'S-1-5-18': 'SY', _ADMIN: 'BA', 'S-1-3-4': 'OW'}


def _sid(owner):
    value = owner[4:] if isinstance(owner, str) and owner.startswith('sid:') else owner
    if not isinstance(value, str) or len(value) > 184 or not _SID.fullmatch(value):
        raise ValueError('Use the authenticated current Windows TokenUser identity.')
    return value


def _identity(path):
    _ordinary_data_path(path)
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode):
        raise ValueError('The private updater path must be an ordinary directory.')
    return info.st_dev, info.st_ino


def _principals(security):
    """Accept only the small, protected, full-control policy we can repair."""
    if isinstance(security, dict) and 'aces' in security:
        # Compare binary trustees, not diagnostic SDDL abbreviations. Windows
        # renders the local Administrator RID500 as LA, while a domain account
        # with that RID is a different SID and must never be conflated with it.
        entries = security['aces']
        if security.get('dacl_protected') is not True or not isinstance(entries, list) or len(entries) != 3:
            return None
        principals = []
        for entry in entries:
            if not isinstance(entry, dict) or set(entry) != {'type', 'flags', 'mask', 'sid'}:
                return None
            if (any(type(entry[field]) is not int for field in ('type', 'flags', 'mask'))
                    or (entry['type'], entry['flags'], entry['mask']) != (0, 3, 0x1f01ff)
                    or not isinstance(entry['sid'], str) or not _SID.fullmatch(entry['sid'])):
                return None
            principals.append(_WELL_KNOWN.get(entry['sid'], entry['sid']))
        return frozenset(principals) if len(set(principals)) == 3 else None
    sddl = security.get('sddl', '') if isinstance(security, dict) else ''
    if not isinstance(sddl, str) or 'D:' not in sddl:
        return None
    dacl = sddl.split('D:', 1)[1]
    prefix, separator, tail = dacl.partition('(')
    if not separator or prefix not in ('P', 'PAI'):
        return None
    entries = re.findall(r'\(([^()]*)\)', '(' + tail)
    if ''.join('(' + entry + ')' for entry in entries) != '(' + tail or len(entries) != 3:
        return None
    principals = []
    for entry in entries:
        fields = entry.split(';')
        if len(fields) != 6 or fields[:5] != ['A', 'OICI', 'FA', '', '']:
            return None
        principals.append(fields[5])
    return frozenset(principals) if len(set(principals)) == 3 else None


def _policy(sid):
    return 'D:P(A;OICI;FA;;;' + sid + ')(A;OICI;FA;;;SY)(A;OICI;FA;;;BA)'


class WindowsDirectoryACL:
    """Native directory creation and narrowly scoped DACL replacement."""
    def __init__(self):
        if os.name != 'nt':
            raise OSError('Windows directory ACLs are unavailable on this host.')
        from ctypes import wintypes
        self.w = wintypes
        self.api = ctypes.WinDLL('advapi32', use_last_error=True)
        self.kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        pointer = ctypes.c_void_p
        self.api.ConvertStringSecurityDescriptorToSecurityDescriptorW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, ctypes.POINTER(pointer), ctypes.POINTER(wintypes.DWORD)]
        self.api.ConvertStringSecurityDescriptorToSecurityDescriptorW.restype = wintypes.BOOL
        self.api.ConvertSecurityDescriptorToStringSecurityDescriptorW.argtypes = [pointer, wintypes.DWORD, wintypes.DWORD, ctypes.POINTER(wintypes.LPWSTR), ctypes.POINTER(wintypes.DWORD)]
        self.api.ConvertSecurityDescriptorToStringSecurityDescriptorW.restype = wintypes.BOOL
        self.api.ConvertSidToStringSidW.argtypes = [pointer, ctypes.POINTER(wintypes.LPWSTR)]
        self.api.ConvertSidToStringSidW.restype = wintypes.BOOL
        self.api.GetNamedSecurityInfoW.argtypes = [wintypes.LPWSTR, ctypes.c_int, wintypes.DWORD, ctypes.POINTER(pointer), ctypes.POINTER(pointer), ctypes.POINTER(pointer), ctypes.POINTER(pointer), ctypes.POINTER(pointer)]
        self.api.GetNamedSecurityInfoW.restype = wintypes.DWORD
        self.api.GetSecurityDescriptorDacl.argtypes = [pointer, ctypes.POINTER(wintypes.BOOL), ctypes.POINTER(pointer), ctypes.POINTER(wintypes.BOOL)]
        self.api.GetSecurityDescriptorDacl.restype = wintypes.BOOL
        self.api.GetSecurityDescriptorControl.argtypes = [pointer, ctypes.POINTER(wintypes.WORD), ctypes.POINTER(wintypes.DWORD)]
        self.api.GetSecurityDescriptorControl.restype = wintypes.BOOL
        self.api.GetAclInformation.argtypes = [pointer, pointer, wintypes.DWORD, ctypes.c_int]
        self.api.GetAclInformation.restype = wintypes.BOOL
        self.api.GetAce.argtypes = [pointer, wintypes.DWORD, ctypes.POINTER(pointer)]
        self.api.GetAce.restype = wintypes.BOOL
        self.api.IsValidSid.argtypes = [pointer]
        self.api.IsValidSid.restype = wintypes.BOOL
        self.api.GetLengthSid.argtypes = [pointer]
        self.api.GetLengthSid.restype = wintypes.DWORD
        self.api.SetSecurityInfo.argtypes = [wintypes.HANDLE, ctypes.c_int, wintypes.DWORD, pointer, pointer, pointer, pointer]
        self.api.SetSecurityInfo.restype = wintypes.DWORD
        self.kernel.LocalFree.argtypes = [pointer]
        self.kernel.LocalFree.restype = pointer
        self.kernel.CreateDirectoryW.argtypes = [wintypes.LPCWSTR, pointer]
        self.kernel.CreateDirectoryW.restype = wintypes.BOOL
        self.kernel.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, pointer, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
        self.kernel.CreateFileW.restype = wintypes.HANDLE
        self.kernel.GetFileInformationByHandleEx.argtypes = [wintypes.HANDLE, ctypes.c_int, pointer, wintypes.DWORD]
        self.kernel.GetFileInformationByHandleEx.restype = wintypes.BOOL
        self.kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        self.kernel.CloseHandle.restype = wintypes.BOOL

    def owner_sid(self):
        from .speed_enhancements import _current_owner
        return _sid(_current_owner())

    def _descriptor(self, sddl):
        descriptor = ctypes.c_void_p()
        if not self.api.ConvertStringSecurityDescriptorToSecurityDescriptorW(sddl, 1, ctypes.byref(descriptor), None):
            raise ctypes.WinError(ctypes.get_last_error())
        return descriptor

    def create(self, path, sddl):
        class Attributes(ctypes.Structure):
            _fields_ = [('nLength', self.w.DWORD), ('lpSecurityDescriptor', ctypes.c_void_p), ('bInheritHandle', self.w.BOOL)]
        descriptor = self._descriptor(sddl)
        try:
            attributes = Attributes(ctypes.sizeof(Attributes), descriptor, False)
            if not self.kernel.CreateDirectoryW(str(path), ctypes.byref(attributes)):
                raise ctypes.WinError(ctypes.get_last_error())
        finally:
            self.kernel.LocalFree(descriptor)

    def read(self, path):
        descriptor, owner, dacl = ctypes.c_void_p(), ctypes.c_void_p(), ctypes.c_void_p()
        result = self.api.GetNamedSecurityInfoW(str(path), 1, 5, ctypes.byref(owner), None, ctypes.byref(dacl), None, ctypes.byref(descriptor))
        if result:
            raise ctypes.WinError(result)
        sid, text = self.w.LPWSTR(), self.w.LPWSTR()
        try:
            if not owner or not dacl or not self.api.ConvertSidToStringSidW(owner, ctypes.byref(sid)):
                raise OSError('The private update directory has no verifiable owner or DACL.')
            if not self.api.ConvertSecurityDescriptorToStringSecurityDescriptorW(descriptor, 1, 5, ctypes.byref(text), None):
                raise ctypes.WinError(ctypes.get_last_error())
            return dict(self._binary_dacl(descriptor, dacl), owner=sid.value, sddl=text.value)
        finally:
            for allocation in (text, sid, descriptor):
                if allocation:
                    self.kernel.LocalFree(allocation)

    def _binary_dacl(self, descriptor, dacl):
        """Read the exact allow-ACE policy using numeric trustees from the ACL."""
        control, revision = self.w.WORD(), self.w.DWORD()
        if not self.api.GetSecurityDescriptorControl(descriptor, ctypes.byref(control), ctypes.byref(revision)):
            raise ctypes.WinError(ctypes.get_last_error())
        result = {'dacl_protected': bool(control.value & 0x1000), 'aces': []}
        class ACLSize(ctypes.Structure):
            _fields_ = [('count', self.w.DWORD), ('used', self.w.DWORD), ('free', self.w.DWORD)]
        size = ACLSize()
        if not self.api.GetAclInformation(dacl, ctypes.byref(size), ctypes.sizeof(size), 2):
            raise ctypes.WinError(ctypes.get_last_error())
        if size.count != 3:
            return result
        class ACEHeader(ctypes.Structure):
            _fields_ = [('kind', self.w.BYTE), ('flags', self.w.BYTE), ('size', self.w.WORD)]
        for index in range(size.count):
            ace = ctypes.c_void_p()
            if not self.api.GetAce(dacl, index, ctypes.byref(ace)):
                raise ctypes.WinError(ctypes.get_last_error())
            header = ACEHeader.from_address(ace.value)
            if header.kind != 0 or header.size < 12:
                return dict(result, aces=[])
            trustee = ctypes.c_void_p(ace.value + 8)
            if (not self.api.IsValidSid(trustee)
                    or self.api.GetLengthSid(trustee) > header.size - 8):
                return dict(result, aces=[])
            text = self.w.LPWSTR()
            try:
                if not self.api.ConvertSidToStringSidW(trustee, ctypes.byref(text)):
                    raise ctypes.WinError(ctypes.get_last_error())
                result['aces'].append({'type': header.kind, 'flags': header.flags,
                    'mask': self.w.DWORD.from_address(ace.value + 4).value, 'sid': text.value})
            finally:
                if text:
                    self.kernel.LocalFree(text)
        return result

    def replace(self, path, sddl, *, expected_identity):
        # Pin the leaf without following a junction and deny deletion/rename
        # while setting its ACL. SetSecurityInfo writes this handle, never a
        # re-resolved path controlled by another process.
        descriptor = self._descriptor(sddl)
        handle = self.kernel.CreateFileW(str(path), 0x60080, 3, None, 3, 0x2200000, None)
        if handle == ctypes.c_void_p(-1).value:
            self.kernel.LocalFree(descriptor)
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            class Tag(ctypes.Structure):
                _fields_ = [('attributes', self.w.DWORD), ('tag', self.w.DWORD)]
            info = Tag()
            if not self.kernel.GetFileInformationByHandleEx(handle, 9, ctypes.byref(info), ctypes.sizeof(info)):
                raise ctypes.WinError(ctypes.get_last_error())
            if info.attributes & 0x400 or not info.attributes & 0x10 or _identity(path) != expected_identity:
                raise ValueError('The private update directory changed or became indirect before repair.')
            present, defaulted, dacl = self.w.BOOL(), self.w.BOOL(), ctypes.c_void_p()
            if not self.api.GetSecurityDescriptorDacl(descriptor, ctypes.byref(present), ctypes.byref(dacl), ctypes.byref(defaulted)) or not present or not dacl:
                raise OSError('A protected non-null private DACL is required.')
            result = self.api.SetSecurityInfo(handle, 1, 0x80000004, None, None, dacl, None)
            if result:
                raise ctypes.WinError(result)
        finally:
            self.kernel.LocalFree(descriptor)
            self.kernel.CloseHandle(handle)


def create_private_directory(path, owner, *, native=None):
    """Create one exclusive directory; never adopt or reset an existing path."""
    path = Path(path)
    _ordinary_data_path(path.parent)
    if os.name != 'nt' and native is None:
        path.mkdir(mode=0o700)
        return {'status': 'created', 'changed': True}
    native = native or WindowsDirectoryACL()
    sid = _sid(owner)
    if sid != _sid(native.owner_sid()):
        raise ValueError('The directory owner must be this process TokenUser.')
    native.create(path, _policy(sid))
    _identity(path)
    if _principals(native.read(path)) != frozenset((sid, 'SY', 'BA')):
        raise OSError('The new private update directory ACL failed readback.')
    return {'status': 'created', 'changed': True}


def repair_legacy_private_directory(path, owner, *, authenticated_owner, native=None):
    """Repair only the exact legacy OW/SYSTEM/Administrators directory policy.

    The caller MUST first decrypt the existing owner.key through current-user
    DPAPI and validate the expected key length. Passing an owner name alone does
    not authenticate a workspace. No other ACL or protected evidence is adopted.
    """
    path = Path(path)
    sid = _sid(owner)
    if _sid(authenticated_owner) != sid:
        raise ValueError('Legacy repair requires the authenticated current-user key.')
    native = native or WindowsDirectoryACL()
    if sid != _sid(native.owner_sid()):
        raise ValueError('The authenticated key owner must match this process TokenUser.')
    before = _identity(path)
    security = native.read(path)
    principals = _principals(security)
    if principals == frozenset((sid, 'SY', 'BA')):
        return {'status': 'already_configured', 'changed': False}
    if (security.get('owner') not in (sid, _ADMIN)
            or principals != frozenset(('OW', 'SY', 'BA'))):
        raise PermissionError('Unrecognized update directory permissions were preserved.')
    if _identity(path) != before:
        raise ValueError('The private update directory changed before repair.')
    native.replace(path, _policy(sid), expected_identity=before)
    if _identity(path) != before or _principals(native.read(path)) != frozenset((sid, 'SY', 'BA')):
        raise OSError('The private update directory permission repair failed readback.')
    return {'status': 'repaired', 'changed': True, 'recursive_reset': False}
