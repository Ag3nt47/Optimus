"""Read-only free-space buffers for mounted local filesystems.

A buffer is an available-space target, never an allocated reservation. These
observations do not authorize deletion, relocation, partition or pagefile work.
"""
from __future__ import annotations

import ctypes
import math
import ntpath
import os
import re
import sys
from datetime import datetime, timezone

import psutil

GIB = 1024 ** 3
_LOCAL_FILESYSTEMS = frozenset(('ext2', 'ext3', 'ext4', 'xfs', 'btrfs', 'zfs', 'apfs', 'hfs',
    'hfsplus', 'ufs', 'ntfs', 'ntfs3', 'vfat', 'msdos', 'exfat', 'f2fs', 'reiserfs', 'jfs',
    'nilfs2', 'fuseblk'))
_EXCLUDED_FILESYSTEMS = frozenset(('tmpfs', 'ramfs', 'devtmpfs', 'devfs', 'proc', 'procfs',
    'sysfs', 'securityfs', 'cgroup', 'cgroup2', 'pstore', 'configfs', 'debugfs', 'tracefs',
    'mqueue', 'hugetlbfs', 'squashfs', 'iso9660', 'udf', 'overlay', 'aufs', 'autofs',
    'nfs', 'nfs4', 'cifs', 'smb', 'smb3', 'smbfs', 'sshfs', 'fuse.sshfs', '9p', 'davfs',
    'davfs2', 'afpfs', 'ceph', 'glusterfs', 'fuse.rclone', 'fuse.s3fs'))


def _text(value, maximum=32767):
    return (isinstance(value, str) and 0 < len(value) <= maximum
            and not any(ord(char) < 32 or ord(char) == 127 for char in value))


def _windows_drive_type(mountpoint):
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.GetDriveTypeW.argtypes = [ctypes.c_wchar_p]
    kernel.GetDriveTypeW.restype = ctypes.c_uint
    return int(kernel.GetDriveTypeW(mountpoint.rstrip('\\/') + '\\'))


def _classification(partition, mountpoint, *, windows):
    if windows:
        if not ntpath.isabs(mountpoint) or mountpoint.startswith(('\\\\', '//')):
            return 'excluded'
        kind = _windows_drive_type(mountpoint)
        return 'fixed' if kind == 3 else 'excluded' if kind in (2, 4, 5, 6) else 'unknown'
    if not os.path.isabs(mountpoint):
        return 'unknown'
    filesystem = str(getattr(partition, 'fstype', '')).casefold()
    device = str(getattr(partition, 'device', ''))
    options = set(str(getattr(partition, 'opts', '')).casefold().split(','))
    if (filesystem in _EXCLUDED_FILESYSTEMS or filesystem.startswith(('nfs', 'smb', 'fuse.ssh', 'fuse.rclone'))
            or device.startswith(('//', '\\\\')) or re.match(r'^[^/]+:', device)
            or options.intersection(('_netdev', 'remote', 'removable', 'cdrom'))):
        return 'excluded'
    return 'fixed' if filesystem in _LOCAL_FILESYSTEMS else 'unknown'


def check_drive_buffers(settings, *, partition_reader=None, usage_reader=None):
    """Inspect every eligible mounted local filesystem without changing it.

    Injectable readers make tests deterministic and perform no host discovery.
    Missing or inconsistent data stays explicitly unknown, never zero or healthy.
    """
    if not isinstance(settings, dict):
        raise ValueError('Drive-buffer settings must be a saved settings object.')
    enabled, target = settings.get('drive_space_guard_enabled', True), settings.get('drive_space_guard_target_gib', 2.0)
    if type(enabled) is not bool or type(target) not in (int, float) or not math.isfinite(target) or not 1 <= target <= 2:
        raise ValueError('Choose a drive-space target from 1 to 2 GiB and an explicit enable toggle.')
    target_bytes = math.ceil(float(target) * GIB)
    report = {'schema': 1, 'enabled': enabled, 'review_only': True, 'target_gib': float(target),
        'target_bytes': target_bytes, 'complete': True, 'volumes': [], 'warnings': [], 'errors': [],
        'excluded_count': 0, 'status': 'disabled' if not enabled else 'completed',
        'checked_at_utc': datetime.now(timezone.utc).isoformat(),
        'summary': 'Drive-space buffer checks are off.' if not enabled else '',
        'note': 'Available-space targets only. No storage is reserved and no files, partitions or pagefile settings are changed.'}
    if not enabled:
        return report
    windows = sys.platform == 'win32'
    partition_reader = partition_reader or psutil.disk_partitions
    usage_reader = usage_reader or psutil.disk_usage
    try:
        partitions = partition_reader(all=False)
        if not isinstance(partitions, (list, tuple)) or len(partitions) > 4096:
            raise ValueError('Mounted-filesystem inventory is unavailable or exceeds its bound.')
    except Exception as error:
        report['errors'].append({'operation': 'mounted-filesystem inventory', 'error_type': type(error).__name__})
        report.update(complete=False, status='needs_review', summary='Drive-space buffer check unavailable; mounted volumes are unknown.')
        report['warnings'].append(report['summary'])
        return report
    seen = set()
    for partition in partitions:
        mountpoint = getattr(partition, 'mountpoint', None)
        if not _text(mountpoint):
            report['errors'].append({'operation': 'mount metadata', 'error_type': 'InvalidMountpoint'})
            continue
        identity = ntpath.normcase(ntpath.normpath(mountpoint)) if windows else os.path.normpath(mountpoint)
        if identity in seen:
            continue
        seen.add(identity)
        name = mountpoint.rstrip('\\/') or '/'
        row = {'mountpoint': mountpoint, 'name': name, 'device': str(getattr(partition, 'device', ''))[:32767],
            'filesystem': str(getattr(partition, 'fstype', ''))[:128], 'free_bytes': None, 'total_bytes': None,
            'target_bytes': target_bytes, 'shortfall_bytes': None, 'below_target': None,
            'status': 'unknown', 'warning': None}
        try:
            classification = _classification(partition, mountpoint, windows=windows)
            if classification == 'excluded':
                report['excluded_count'] += 1
                continue
            if classification != 'fixed':
                raise ValueError('The mounted filesystem could not be verified as local storage.')
            usage = usage_reader(mountpoint)
            free, total = getattr(usage, 'free', None), getattr(usage, 'total', None)
            if (type(free) is not int or type(total) is not int or not 0 <= free <= total <= 2 ** 63 - 1 or total <= 0):
                raise ValueError('Free-space measurements are missing or inconsistent.')
            shortfall = max(0, target_bytes - free)
            row.update(free_bytes=free, total_bytes=total, shortfall_bytes=shortfall,
                       below_target=shortfall > 0, status='below_target' if shortfall else 'ok')
            if shortfall:
                row['warning'] = (f'{name}: {free / GIB:.2f} GiB free; target {target_bytes / GIB:.2f} GiB '
                                  f'(shortfall {shortfall / GIB:.2f} GiB).')
        except Exception as error:
            row['warning'] = name + ': free space is unknown; the buffer target could not be verified.'
            report['errors'].append({'mountpoint': mountpoint, 'operation': 'local free-space check',
                                     'error_type': type(error).__name__})
        if row['warning']:
            report['warnings'].append(row['warning'])
        report['volumes'].append(row)
    report['complete'] = not report['errors']
    below = sum(row['below_target'] is True for row in report['volumes'])
    unknown = sum(row['below_target'] is None for row in report['volumes'])
    if below or not report['complete']:
        report['status'] = 'needs_review'
    checked = len(report['volumes'])
    if not checked and report['complete']:
        report.update(complete=False, status='needs_review')
        report['warnings'].append('No eligible mounted local volumes were reported; free-space buffers are unverified.')
    report['summary'] = (f'Drive-space buffers: {checked} local volumes checked; {below} below '
                         f'{target_bytes / GIB:.2f} GiB; {unknown} unknown.')
    if not report['complete'] and not unknown:
        report['summary'] += ' Inventory needs review.'
    return report
