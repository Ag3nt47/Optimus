"""Empty the signed-in user's Recycle Bin (Windows) or Trash (macOS/Linux).

This is permanent deletion. It runs only when ``recycle_bin_empty_enabled`` is
saved, and the maintenance workflow schedules it before Optimus recycles any
duplicates, so items recycled in the same run stay restorable until the next.
Results report measured item and byte counts; nothing is invented when a
platform does not expose them.
"""
from __future__ import annotations

import ctypes
import os
from pathlib import Path
import shutil
import stat
import sys

# SHERB_NOCONFIRMATION | SHERB_NOPROGRESSUI | SHERB_NOSOUND
_SHERB_SILENT = 0x1 | 0x2 | 0x4
_E_UNEXPECTED = -2147418113  # 0x8000FFFF: returned when every bin is already empty.
_MAX_ENTRIES = 200_000


class _QueryInfo(ctypes.Structure):
    _fields_ = [('cbSize', ctypes.c_uint32), ('i64Size', ctypes.c_int64), ('i64NumItems', ctypes.c_int64)]


def _windows_query(shell):
    info = _QueryInfo()
    info.cbSize = ctypes.sizeof(_QueryInfo)
    result = shell.SHQueryRecycleBinW(None, ctypes.byref(info))
    if result != 0:
        raise OSError(f'The Recycle Bin size query failed (HRESULT 0x{result & 0xFFFFFFFF:08X}).')
    return int(info.i64NumItems), int(info.i64Size)


def _empty_windows(shell=None):
    shell = shell if shell is not None else ctypes.windll.shell32
    items, size = _windows_query(shell)
    if items == 0:
        return {'emptied': False, 'skipped': True, 'items': 0, 'bytes': 0, 'location': 'Recycle Bin',
                'note': 'Every Recycle Bin on this account was already empty.'}
    result = shell.SHEmptyRecycleBinW(None, None, _SHERB_SILENT)
    remaining, remaining_bytes = _windows_query(shell)
    if result not in (0, _E_UNEXPECTED) or remaining:
        return {'emptied': False, 'failed_items': max(1, remaining), 'items': items - remaining,
                'bytes': max(0, size - remaining_bytes), 'remaining_items': remaining, 'location': 'Recycle Bin',
                'error': (f'Windows reported HRESULT 0x{result & 0xFFFFFFFF:08X}; ' if result not in (0, _E_UNEXPECTED) else '')
                         + f'{remaining} item(s) remain in the Recycle Bin.'}
    return {'emptied': True, 'items': items, 'bytes': size, 'location': 'Recycle Bin',
            'note': 'Permanently deleted every item in this account\'s Recycle Bins; they cannot be restored.'}


def trash_directory(platform=None, home=None, environ=None):
    platform = sys.platform if platform is None else platform
    home = Path.home() if home is None else Path(home)
    environ = os.environ if environ is None else environ
    if platform == 'darwin':
        return home / '.Trash'
    data = environ.get('XDG_DATA_HOME')
    base = Path(data) if data and Path(data).is_absolute() else home / '.local/share'
    return base / 'Trash'


def _measure(path):
    """Count entries and logical bytes without following symbolic links."""
    count = size = 0
    stack = [path]
    while stack:
        with os.scandir(stack.pop()) as entries:
            for entry in entries:
                count += 1
                if count > _MAX_ENTRIES:
                    raise OSError('The Trash holds more entries than one run measures; it was left unchanged.')
                info = entry.stat(follow_symlinks=False)
                if stat.S_ISDIR(info.st_mode):
                    stack.append(entry.path)
                else:
                    size += info.st_size
    return count, size


def _remove(entry):
    info = os.lstat(entry)
    if stat.S_ISDIR(info.st_mode):
        shutil.rmtree(entry)  # rmtree never follows links found inside the tree.
    else:
        os.unlink(entry)


def _empty_posix(root, cancel_event=None, *, linux_layout=True):
    location = str(root)
    try:
        info = os.lstat(root)
    except FileNotFoundError:
        return {'emptied': False, 'skipped': True, 'items': 0, 'bytes': 0, 'location': location,
                'note': 'This account has no Trash folder yet.'}
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid():
        return {'emptied': False, 'failed_items': 1, 'location': location,
                'error': 'The Trash is not a directory owned by this account; it was left unchanged.'}
    # Linux keeps files/, info/ and expunged/ inside Trash; macOS stores items directly.
    containers = ([root / name for name in ('files', 'info', 'expunged') if (root / name).is_dir()]
                  if linux_layout else [])
    targets = containers or [root]
    try:
        entries = [Path(entry.path) for target in targets for entry in os.scandir(target)]
        sizes = {entry: (_measure(entry)[1] if entry.is_dir() and not entry.is_symlink() else os.lstat(entry).st_size)
                 for entry in entries}
    except PermissionError:
        return {'emptied': False, 'failed_items': 1, 'location': location,
                'error': 'The operating system denied access to the Trash. On macOS, grant Optimus Full Disk Access to empty it.'}
    if not entries:
        return {'emptied': False, 'skipped': True, 'items': 0, 'bytes': 0, 'location': location,
                'note': 'The Trash was already empty.'}
    failures, cancelled = [], False
    for entry in entries:
        if cancel_event is not None and cancel_event.is_set():
            cancelled = True
            break
        try:
            _remove(entry)
        except FileNotFoundError:
            continue
        except OSError as error:
            failures.append(f'{entry.name}: {error.strerror or type(error).__name__}')
    gone = [entry for entry in entries if not os.path.lexists(entry)]
    # On Linux, info/ holds metadata for each item in files/; count only real items.
    items = sum(1 for entry in gone if not containers or entry.parent.name == 'files')
    result = {'emptied': not failures and not cancelled, 'items': items,
              'bytes': sum(sizes[entry] for entry in gone), 'location': location,
              'note': "Permanently deleted the items in this account's Trash; they cannot be restored."}
    if cancelled:
        result.update(cancelled=True, note='Cancelled between Trash items; remaining items were kept.')
    if failures:
        result.update(failed_items=len(failures), error='Some Trash items could not be removed: ' + '; '.join(failures[:10]))
    return result


def empty_trash(*, platform=None, cancel_event=None, shell=None, trash=None):
    platform = sys.platform if platform is None else platform
    if cancel_event is not None and cancel_event.is_set():
        return {'cancelled': True, 'emptied': False, 'note': 'Cancelled before the Recycle Bin/Trash was emptied.'}
    if platform == 'win32':
        return _empty_windows(shell)
    return _empty_posix(Path(trash) if trash is not None else trash_directory(platform), cancel_event,
                        linux_layout=platform != 'darwin')
