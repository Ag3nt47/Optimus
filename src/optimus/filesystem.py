"""Small Windows-friendly atomic file operations for app-owned state."""
from __future__ import annotations
import os
import json
import ntpath
import re
import stat
import sys
import time
from pathlib import Path

LOCATION_FILE = 'Optimus.location.json'


def _configuration_exists(path: Path) -> bool:
    try:
        path.lstat()
        return True
    except FileNotFoundError:
        return False


def _local_data_path(value) -> Path:
    if sys.platform != 'win32':
        if (not isinstance(value, str) or not value or len(value) > 1024
                or re.search(r'[\x00-\x1f]', value) or not value.startswith('/')
                or '..' in Path(value).parts or Path(value) == Path('/')):
            raise ValueError('Choose an absolute ordinary local data folder below the filesystem root.')
        return Path(value)
    if not isinstance(value, str) or not value or len(value) > 1024 or re.search(r'[\x00-\x1f*?<>|"%]', value):
        raise ValueError('The configured data directory must be an absolute ordinary local-drive folder.')
    drive, tail = ntpath.splitdrive(value)
    if not re.fullmatch('[A-Za-z]:', drive) or not tail.startswith(('\\','/')) or ':' in tail:
        raise ValueError('The configured data directory must use an absolute local drive, not a network or relative path.')
    parts = [part for part in re.split(r'[\\/]', tail) if part]
    if not parts or any(part in ('.','..') or part.endswith((' ','.')) for part in parts):
        raise ValueError('Choose a concrete ordinary data folder below the local drive root.')
    return Path(ntpath.normpath(value))


def default_data_root(installation_root: Path | None = None) -> Path:
    """Read a bounded installation-owned location file, or use the legacy default.

    A present but invalid configuration never silently selects another directory.
    Source restarts consult the original installation, never an editable Sandbox
    configuration. The public download carries no machine-specific location file.
    """
    if installation_root is not None:
        installation = Path(os.path.abspath(installation_root))
    elif getattr(sys, 'frozen', False):
        installation = Path(sys.executable).parent
        if sys.platform == 'darwin' and installation.name == 'MacOS':
            installation = installation.parent / 'Resources'
    else:
        installation = Path(os.environ.get('OPTIMUS_ORIGINAL_SOURCE') or Path(__file__).parents[2])
        # The portable bundle keeps its original source directly under App/source.
        if installation.name.casefold() == 'source' and _configuration_exists(installation.parent / LOCATION_FILE):
            installation = installation.parent
    _ordinary_data_path(installation)
    config = installation / LOCATION_FILE
    info = _ordinary_data_path(config, file=True)
    if info is None:
        if sys.platform == 'darwin':
            root = Path.home() / 'Library' / 'Application Support' / 'Optimus'
        elif sys.platform == 'linux':
            configured = os.environ.get('XDG_STATE_HOME', '')
            base = Path(configured) if configured and Path(configured).is_absolute() else Path.home() / '.local/state'
            root = base / 'optimus'
        else:
            root = Path(os.environ.get('LOCALAPPDATA', Path.home())) / 'Optimus'
        return resolve_data_root(root)
    physical = config.resolve(strict=True)
    physical_info = _ordinary_data_path(physical, file=True)
    if physical_info is None or (info.st_dev, info.st_ino) != (physical_info.st_dev, physical_info.st_ino):
        raise ValueError('The installation location file changed during verification.')
    if physical_info.st_size > 4096:
        raise ValueError('The installation location file exceeds its 4096-byte limit.')
    with physical.open('rb') as stream:
        raw = stream.read(4097)
    if len(raw) > 4096:
        raise ValueError('The installation location file exceeds its 4096-byte limit.')
    try:
        location = json.loads(raw.decode('utf-8-sig'))
    except (ValueError, UnicodeError) as error:
        raise ValueError('The installation location file is not valid UTF-8 JSON.') from error
    if (not isinstance(location, dict) or set(location) != {'schema','data_dir'}
            or type(location.get('schema')) is not int or location['schema'] != 1):
        raise ValueError('Unsupported installation location schema.')
    return resolve_data_root(_local_data_path(location.get('data_dir')))


def _ordinary_data_path(path: Path, *, file=False):
    """Inspect logical paths before resolving them; never follow a reparse point."""
    leaf_info = None
    for index, part in enumerate((path, *path.parents)):
        try:
            info = part.lstat()
        except FileNotFoundError:
            if index == 0:
                continue
            continue
        native_system_alias = (sys.platform == 'darwin' and part in (Path('/var'), Path('/tmp'), Path('/etc'))
                               and part.is_symlink() and str(part.resolve()) == '/private' + str(part)
                               and info.st_uid == 0)
        if (stat.S_ISLNK(info.st_mode) and not native_system_alias) or getattr(info, 'st_file_attributes', 0) & 0x400:
            raise ValueError('Indirect Optimus data paths are not supported.')
        if native_system_alias:
            continue
        if index == 0 and file:
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise ValueError('The preferences anchor must be an ordinary file without hardlinks.')
            leaf_info = info
        elif not stat.S_ISDIR(info.st_mode):
            raise ValueError('Optimus data ancestors must be ordinary directories.')
    return leaf_info


def resolve_data_root(path: Path) -> Path:
    """Use an existing ordinary preference file as the data-directory authority.

    MSIX merged directories can report the logical directory itself while an
    existing ordinary file resolves to its physical backing directory. Resolve
    that anchor before constructing any writer or Sandbox path. Directory and
    file links are rejected on both sides of the mapping; no data is moved.
    """
    logical = Path(os.path.abspath(path))
    _ordinary_data_path(logical)
    anchor = logical / 'preferences.json'
    logical_info = _ordinary_data_path(anchor, file=True)
    if logical_info is not None:
        physical_anchor = anchor.resolve(strict=True)
        physical_info = _ordinary_data_path(physical_anchor, file=True)
        if physical_info is None:
            raise ValueError('The resolved preferences anchor is unavailable.')
        logical_identity = (logical_info.st_dev, logical_info.st_ino)
        physical_identity = (physical_info.st_dev, physical_info.st_ino)
        if logical_identity != physical_identity:
            raise ValueError('The preferences anchor changed while resolving the data directory.')
        authority = physical_anchor.parent
    else:
        authority = logical.resolve()
    _ordinary_data_path(authority)
    return authority


def canonical_file(path: Path) -> Path:
    """Resolve the containing directory, preserving the exact final file name.

    MSIX/Windows app-data aliases can resolve a new temporary file and an
    existing destination through different volumes. Use one physical parent
    representation for both operands; journals still require atomic replace.
    """
    path = Path(path)
    return path.parent.resolve() / path.name


def replace_with_retry(temporary: Path, destination: Path, attempts: int = 12):
    """Replace atomically after brief Windows sharing or access conflicts.

    A reader or scanner can temporarily prevent replacing an otherwise writable
    file. The default retry budget sleeps for at most 3.25 seconds; persistent
    failures still preserve the previous destination and raise to the caller.
    """
    if type(attempts) is not int or attempts < 1:
        raise ValueError('Atomic replacement requires at least one attempt.')
    temporary = canonical_file(temporary)
    destination = canonical_file(destination)
    try:
        for attempt in range(attempts):
            try:
                temporary.replace(destination)
                return
            except OSError as error:
                if getattr(error, 'winerror', None) not in (5, 32, 33) or attempt == attempts - 1:
                    raise
                time.sleep(min(0.05 * (attempt + 1), 0.5))
    finally:
        if temporary.exists():
            try:
                temporary.unlink()
            except OSError:
                pass
