"""Explicit Windows free-space wiping with conservative physical-HDD gating.

Nothing is wiped on import or inspection. SSD, unknown, virtual and ambiguous
media stay unavailable. This is not a forensic guarantee for remapped sectors,
snapshots, backups or other copies.
"""
from __future__ import annotations
import base64
import ctypes
import json
import os
from pathlib import Path
import re
import subprocess
import threading
import uuid


class FreeSpaceWipeUnavailable(RuntimeError):
    pass


_MEDIA_QUERY = r'''
$ErrorActionPreference='Stop'
$physical=@(Get-PhysicalDisk -ErrorAction Stop)
$rows=@(Get-Partition -ErrorAction Stop | Where-Object DriveLetter | ForEach-Object {
 $disk=Get-Disk -Number $_.DiskNumber -ErrorAction Stop
 $matches=@($physical | Where-Object { $_.UniqueId -and $disk.UniqueId -and ([string]$_.UniqueId).Trim() -eq ([string]$disk.UniqueId).Trim() })
 [pscustomobject]@{DriveLetter=[string]$_.DriveLetter;DiskUniqueId=[string]$disk.UniqueId;BusType=[string]$disk.BusType;Matches=@($matches | ForEach-Object {[pscustomobject]@{UniqueId=[string]$_.UniqueId;MediaType=[string]$_.MediaType}})}
})
ConvertTo-Json -InputObject $rows -Depth 5 -Compress
'''
_JOBS: dict[str, dict] = {}
_LOCK = threading.RLock()


def _system_executable(name: str) -> Path:
    if os.name != 'nt':
        raise FreeSpaceWipeUnavailable('This tool supports Windows only.')
    buffer = ctypes.create_unicode_buffer(32768)
    length = ctypes.windll.kernel32.GetSystemDirectoryW(buffer, len(buffer))
    if not length or length >= len(buffer):
        raise FreeSpaceWipeUnavailable('The Windows system directory could not be verified.')
    path = Path(buffer.value) / name
    if not path.is_file():
        raise FreeSpaceWipeUnavailable('The required built-in Windows tool is unavailable.')
    return path


def _validated_directory(directory) -> tuple[Path, str]:
    if os.name != 'nt':
        raise FreeSpaceWipeUnavailable('This tool supports Windows only.')
    path = Path(os.path.abspath(directory))
    if not re.fullmatch(r'[A-Za-z]:\\', path.anchor):
        raise FreeSpaceWipeUnavailable('Choose an existing folder on a local fixed disk.')
    if not path.is_dir():
        raise FreeSpaceWipeUnavailable('Choose an existing folder.')
    for part in (path, *path.parents):
        flags = getattr(part.lstat(), 'st_file_attributes', 0)
        if part.is_symlink() or flags & (0x400 | 0x1000 | 0x40000 | 0x400000):
            raise FreeSpaceWipeUnavailable('Indirect and cloud-placeholder paths are excluded.')
    for key in ('OneDrive', 'OneDriveCommercial', 'OneDriveConsumer'):
        cloud = os.environ.get(key)
        if cloud and (path == Path(cloud) or Path(cloud) in path.parents):
            raise FreeSpaceWipeUnavailable('Choose a folder outside cloud synchronization.')
    if ctypes.windll.kernel32.GetDriveTypeW(str(path.anchor)) != 3:
        raise FreeSpaceWipeUnavailable('Only fixed local disks are supported.')
    return path, path.anchor[0].upper()


def _physical_hdd(letter: str) -> tuple[bool, str]:
    encoded = base64.b64encode(_MEDIA_QUERY.encode('utf-16-le')).decode('ascii')
    executable = _system_executable('WindowsPowerShell/v1.0/powershell.exe')
    result = subprocess.run([str(executable), '-NoProfile', '-NonInteractive', '-EncodedCommand', encoded],
                            capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=25,
                            creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    if result.returncode:
        return False, 'Windows could not positively identify the physical media; wiping is unavailable.'
    rows = json.loads(result.stdout.lstrip('\ufeff').strip() or '[]')
    if not isinstance(rows, list):
        rows = [rows]
    selected = [row for row in rows if str(row.get('DriveLetter', '')).upper() == letter]
    if len(selected) != 1:
        return False, 'The drive-to-physical-disk mapping is ambiguous.'
    disk = selected[0]
    matches = disk.get('Matches') or []
    if not isinstance(matches, list):
        matches = [matches]
    if disk.get('BusType') not in {'SATA', 'ATA', 'SAS', 'USB', 'SCSI'} or len(matches) != 1:
        return False, 'Virtual, pooled, RAID, SSD and ambiguous storage are not eligible.'
    physical = matches[0]
    identity = str(disk.get('DiskUniqueId', '')).strip().casefold()
    if not identity or identity != str(physical.get('UniqueId', '')).strip().casefold() or physical.get('MediaType') != 'HDD':
        return False, 'The media is SSD, unknown, or not a positively matched physical HDD.'
    return True, 'A single physical HDD was matched by its Windows storage unique ID.'


def free_space_wipe_status(directory) -> dict:
    try:
        path, letter = _validated_directory(directory)
        eligible, reason = _physical_hdd(letter)
        if eligible:
            _system_executable('cipher.exe')
        return {'available': eligible, 'directory': str(path), 'drive': letter + ':', 'media_type': 'HDD' if eligible else 'Unverified',
                'reason': reason, 'note': 'Cipher overwrites free space on the entire selected volume, not just the selected folder. It can run for hours and temporarily consume nearly all free space. Existing files are not selected for deletion. No guarantee is made for remapped sectors, snapshots, backups or other copies.'}
    except (OSError, ValueError, subprocess.SubprocessError, FreeSpaceWipeUnavailable) as exc:
        return {'available': False, 'reason': str(exc), 'media_type': 'Unverified'}


def launch_free_space_wipe(directory, *, confirmed: bool = False) -> dict:
    if confirmed is not True:
        raise FreeSpaceWipeUnavailable('Explicit confirmation of whole-volume free-space wiping is required.')
    status = free_space_wipe_status(directory)
    if not status.get('available'):
        raise FreeSpaceWipeUnavailable(status.get('reason', 'Physical HDD identity was not verified.'))
    # Revalidate directory attributes immediately before launching the built-in tool.
    path, letter = _validated_directory(directory)
    if letter + ':' != status['drive']:
        raise FreeSpaceWipeUnavailable('The selected volume changed.')
    executable = _system_executable('cipher.exe')
    local_data = os.environ.get('LOCALAPPDATA')
    if not local_data:
        raise FreeSpaceWipeUnavailable('The current-user application log directory is unavailable.')
    job_id = uuid.uuid4().hex
    log = Path(local_data) / 'Optimus' / 'wipe-logs' / (job_id + '.log')
    log.parent.mkdir(parents=True, exist_ok=True)
    with log.open('xb') as output:
        process = subprocess.Popen([str(executable), '/w:' + str(path)], stdin=subprocess.DEVNULL, stdout=output, stderr=subprocess.STDOUT,
                                   creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    with _LOCK:
        _JOBS[job_id] = {'process': process, 'log_path': str(log), 'drive': status['drive']}
    return {'job_id': job_id, 'pid': process.pid, 'drive': status['drive'], 'started': True, 'log_path': str(log), 'note': status['note']}


def free_space_wipe_job(job_id: str) -> dict:
    with _LOCK:
        job = _JOBS.get(job_id)
        if not job:
            raise FreeSpaceWipeUnavailable('This wipe job is not owned by the current Optimus session.')
        code = job['process'].poll()
        return {'job_id': job_id, 'running': code is None, 'exit_code': code, 'drive': job['drive'], 'log_path': job['log_path']}
