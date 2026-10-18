"""Copy only public source files into a portable distribution's editable source folder."""
from __future__ import annotations
import argparse
import os
import shutil
from pathlib import Path

SKIP = {'.git','.venv','venv','__pycache__','.pytest_cache','test-artifacts','dist','build','reports','private'}
ALLOW_DIRS = {'src','tests','tools','docs','.github'}
ROOT_FILES = {'pyproject.toml','LICENSE','NOTICE','README.md','.gitignore','.gitattributes','requirements-build.txt','Install-Source.bat','Build-Windows.bat','Start-Optimus.bat','launch.py','SECURITY.md','CONTRIBUTING.md'}

def indirect(path):
    """Reject symlinks and Windows junctions/placeholders without following them."""
    try:
        info = path.lstat()
    except FileNotFoundError:
        return False
    return path.is_symlink() or bool(getattr(info, 'st_file_attributes', 0) & 0x400)

def direct_ancestry(path):
    for part in (path, *path.parents):
        if indirect(part):
            raise ValueError('Indirect source or destination paths are not packaged.')

def package(source, destination):
    source = Path(os.path.abspath(source))
    destination = Path(os.path.abspath(destination))
    direct_ancestry(source)
    direct_ancestry(destination)
    source, destination = source.resolve(), destination.resolve()
    if not source.is_dir():
        raise ValueError('The source directory is missing.')
    if destination == source or destination.is_relative_to(source):
        # dist/Optimus/source is expected, but it is outside allowed source directories.
        if not destination.is_relative_to(source / 'dist'):
            raise ValueError('Source destination overlaps the source tree.')
    count=0
    for directory, subdirs, names in os.walk(source,followlinks=False):
        folder=Path(directory)
        relative=folder.relative_to(source)
        subdirs[:]=[item for item in subdirs if item not in SKIP and not indirect(folder/item) and (relative.parts or item in ALLOW_DIRS)]
        for name in names:
            file=folder/name
            if indirect(file) or (not relative.parts and name not in ROOT_FILES) or name.endswith(('.pyc','.pyo','.log','.lic','.key','-tests.json')):
                continue
            if name.startswith('.env'):
                continue
            target=destination/file.relative_to(source)
            direct_ancestry(target)
            target.parent.mkdir(parents=True,exist_ok=True)
            shutil.copy2(file,target)
            count+=1
    return count

if __name__=='__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('--source',type=Path,default=Path('.'))
    parser.add_argument('--destination',type=Path,required=True)
    args=parser.parse_args()
    print(package(args.source,args.destination))

