"""Small Windows-friendly atomic file operations for app-owned state."""
from __future__ import annotations
import os
import stat
import time
from pathlib import Path


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
        if stat.S_ISLNK(info.st_mode) or getattr(info, 'st_file_attributes', 0) & 0x400:
            raise ValueError('Indirect Optimus data paths are not supported.')
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


def replace_with_retry(temporary: Path, destination: Path, attempts: int = 6):
    if type(attempts) is not int or attempts < 1:
        raise ValueError('Atomic replacement requires at least one attempt.')
    temporary = canonical_file(temporary)
    destination = canonical_file(destination)
    try:
        for attempt in range(attempts):
            try:
                temporary.replace(destination)
                return
            except PermissionError:
                if attempt == attempts - 1:
                    raise
                time.sleep(0.03 * (attempt + 1))
    finally:
        if temporary.exists():
            try:
                temporary.unlink()
            except OSError:
                pass
