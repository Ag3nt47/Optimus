"""Reject or remove a build-environment ICU collision in a Windows Qt bundle.

Run after PyInstaller. Windows supplies the unversioned ICU API used by Qt.
No system files are changed. pefile is a PyInstaller build dependency.
"""
from __future__ import annotations
import argparse
import json
import os
from pathlib import Path
import re


def inspect_pe(path):
    import pefile  # supplied by the Windows PyInstaller build dependency
    pe = pefile.PE(str(path), fast_load=True)
    try:
        pe.parse_data_directories(directories=[0, 1, 13])
        exports = {entry.name.decode('ascii') for entry in getattr(getattr(pe, 'DIRECTORY_ENTRY_EXPORT', None), 'symbols', []) if entry.name}
        imports = {}
        for table in ('DIRECTORY_ENTRY_IMPORT', 'DIRECTORY_ENTRY_DELAY_IMPORT'):
            for entry in getattr(pe, table, []):
                imports.setdefault(entry.dll.decode('ascii').lower(), set()).update(item.name.decode('ascii') for item in entry.imports if item.name)
        return {'exports': exports, 'imports': imports}
    finally:
        pe.close()


def direct(path):
    for item in (path, *path.parents):
        if item.is_symlink() or (item.exists() and getattr(item.lstat(), 'st_file_attributes', 0) & 0x400):
            raise ValueError('Bundle normalization refuses indirect paths.')


def plan_normalization(bundle, system_directory=None, reader=inspect_pe):
    bundle = Path(os.path.abspath(bundle))
    direct(bundle)
    internal = bundle / '_internal'
    qt = internal / 'PySide6' / 'Qt6Core.dll'
    if not qt.is_file():
        raise ValueError('Expected a Windows PyInstaller onedir bundle with PySide6/Qt6Core.dll.')
    direct(qt)
    required = reader(qt)['imports'].get('icuuc.dll', set())
    if not required or 'ucnv_open' not in required or any(re.search(r'_\d+$', name) for name in required):
        raise ValueError('Qt does not use the expected unversioned Windows ICU API; review this build manually.')
    conflict = internal / 'icuuc.dll'
    if not conflict.exists():
        return []
    direct(conflict)
    packaged = reader(conflict)
    if required.issubset(packaged['exports']):
        return []
    if not any(re.fullmatch(r'ucnv_open_\d+', name) for name in packaged['exports']):
        raise ValueError('Unknown incompatible icuuc.dll; no files were removed.')
    if system_directory is None:
        system_directory = Path(os.environ['WINDIR']) / 'System32'
    system_icu = Path(system_directory) / 'icuuc.dll'
    if not system_icu.is_file() or not required.issubset(reader(system_icu)['exports']):
        raise ValueError('Windows ICU does not provide all required Qt symbols; no files were removed.')
    removals = {conflict}
    for dependency in packaged['imports']:
        if dependency.startswith('icu'):
            if not re.fullmatch(r'icudt\d+\.dll', dependency):
                raise ValueError('Unexpected ICU dependency; review this build manually.')
            target = internal / dependency
            if not target.is_file():
                raise ValueError('The conflicting ICU data dependency is missing.')
            direct(target)
            removals.add(target)
    # Do not remove a dependency needed by another packaged binary.
    for binary in internal.rglob('*'):
        if binary.suffix.lower() not in {'.dll', '.pyd', '.exe'} or binary in removals:
            continue
        direct(binary)
        dependencies = reader(binary)['imports']
        for target in removals:
            names = dependencies.get(target.name.lower())
            if names is None:
                continue
            if target.name.lower() != 'icuuc.dll' or not names.issubset(reader(system_icu)['exports']):
                raise ValueError('Another packaged binary requires the conflicting ICU library; no files were removed.')
    return sorted(removals)


def normalize_bundle(bundle, system_directory=None, reader=inspect_pe):
    planned = plan_normalization(bundle, system_directory, reader)
    for target in planned:
        target.unlink()
    return {'removed': [str(path.relative_to(Path(os.path.abspath(bundle)))) for path in planned], 'reason': 'Use the Windows ICU interface required by Qt; exclude a renamed ICU DLL collected from the build PATH.'}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--bundle', type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(normalize_bundle(args.bundle), indent=2))

