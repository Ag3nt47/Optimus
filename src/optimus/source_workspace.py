"""Editable source workspace with bounded version history; candidates never auto-execute."""
from __future__ import annotations
import ast
import hashlib
import json
import os
import shutil
import time
import uuid
from pathlib import Path
from .filesystem import replace_with_retry, resolve_data_root, default_data_root

TEXT_SUFFIXES = {'.py', '.ps1', '.toml', '.yml', '.yaml', '.md', '.txt', '.bat', '.json', '.svg', '.qss'}
ASSET_SUFFIXES = {'.ico', '.png'}
MAX_ASSET_BYTES = 8 * 1024 * 1024
SKIP_DIRS = {'.git', '.venv', 'venv', '__pycache__', '.pytest_cache', 'build', 'dist', 'test-artifacts', 'reports', 'private', '.history'}

class SourceWorkspace:
    def __init__(self, source_root: Path, data_root: Path | None = None):
        self.source_root = Path(source_root).resolve()
        data_root = default_data_root() if data_root is None else resolve_data_root(data_root)
        self.root = data_root / 'Sandbox' / 'workspace'
        self.history = data_root / 'Sandbox' / 'history'
        for folder in (self.root, self.history):
            for part in (folder, *folder.parents):
                if part.exists() and (part.is_symlink() or getattr(part.lstat(), 'st_file_attributes', 0) & 0x400):
                    raise ValueError('Indirect source sandbox folders are not supported.')
        self.root.mkdir(parents=True, exist_ok=True)
        self.history.mkdir(parents=True, exist_ok=True)
        if not self.source_root.is_dir():
            raise FileNotFoundError('The editable source package is missing. Extract the complete Optimus download, including its source folder.')
        self._seed()

    def _seed(self):
        for directory, subdirs, names in os.walk(self.source_root, followlinks=False):
            subdirs[:] = [name for name in subdirs if name not in SKIP_DIRS and not (Path(directory) / name).is_symlink() and not (getattr((Path(directory) / name).lstat(), 'st_file_attributes', 0) & 0x400)]
            for name in names:
                source = Path(directory) / name
                if source.is_symlink() or getattr(source.lstat(), 'st_file_attributes', 0) & 0x400 or (source.suffix.lower() not in TEXT_SUFFIXES | ASSET_SUFFIXES and name not in {'LICENSE', 'NOTICE', '.gitignore', '.gitattributes'}):
                    continue
                if source.suffix.lower() in ASSET_SUFFIXES and source.stat().st_size > MAX_ASSET_BYTES:
                    continue
                relative = source.relative_to(self.source_root)
                destination = self._path(str(relative))
                if not destination.exists():
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(source, destination)

    def _path(self, relative: str) -> Path:
        path = self.root / relative
        full = path.resolve()
        if not full.is_relative_to(self.root.resolve()) or full == self.root.resolve():
            raise ValueError('Path is outside the source sandbox.')
        for item in (path, *path.parents):
            if item == self.root.parent:
                break
            if item.is_symlink() or (item.exists() and getattr(item.lstat(), 'st_file_attributes', 0) & 0x400):
                raise ValueError('Indirect sandbox paths are not supported.')
        return full

    def files(self):
        result = []
        for directory, subdirs, names in os.walk(self.root, followlinks=False):
            subdirs[:] = [name for name in subdirs if name not in SKIP_DIRS and not (Path(directory) / name).is_symlink() and not (getattr((Path(directory) / name).lstat(), 'st_file_attributes', 0) & 0x400)]
            for name in names:
                path = Path(directory) / name
                if path.is_symlink() or getattr(path.lstat(), 'st_file_attributes', 0) & 0x400:
                    continue
                if path.suffix.lower() in TEXT_SUFFIXES or path.name in {'LICENSE', 'NOTICE', '.gitignore', '.gitattributes'}:
                    result.append(str(path.relative_to(self.root)).replace('\\', '/'))
        return sorted(result)

    def read(self, relative):
        return self._path(relative).read_text(encoding='utf-8-sig')

    def _history_dir(self, relative):
        folder = self.history / hashlib.sha256(str(relative).encode()).hexdigest()
        for part in (folder, *folder.parents):
            if part == self.history.parent:
                break
            try:
                attributes = part.lstat()
            except FileNotFoundError:
                continue
            if part.is_symlink() or getattr(attributes, 'st_file_attributes', 0) & 0x400:
                raise ValueError('Indirect source history folders are not supported.')
        return folder

    def save(self, relative: str, text: str):
        target = self._path(relative)
        if target.suffix.lower() == '.py':
            ast.parse(text, filename=relative)
        previous = target.read_text(encoding='utf-8-sig')
        version_dir = self._history_dir(relative)
        version_dir.mkdir(parents=True, exist_ok=True)
        revision = f'{time.time_ns()}-{uuid.uuid4().hex[:8]}.txt'
        (version_dir / revision).write_text(previous, encoding='utf-8')
        temporary = target.with_name(target.name + '.saving-' + uuid.uuid4().hex)
        temporary.write_text(text, encoding='utf-8')
        replace_with_retry(temporary, target)
        revisions = sorted(version_dir.glob('*.txt'), reverse=True)
        for extra in revisions[50:]:
            extra.unlink()
        return {'path': str(target), 'revision': revision, 'saved': True}

    def versions(self, relative):
        folder = self._history_dir(relative)
        return sorted((path.name for path in folder.glob('*.txt')), reverse=True) if folder.is_dir() else []

    def revert(self, relative, revision=None):
        versions = self.versions(relative)
        if not versions:
            raise ValueError('No saved revision exists for this file.')
        revision = revision or versions[0]
        if revision not in versions or Path(revision).name != revision:
            raise ValueError('Unknown saved revision.')
        text = (self._history_dir(relative) / revision).read_text(encoding='utf-8')
        self.save(relative, text)
        return text

    def restore_original(self, relative):
        original = (self.source_root / relative).resolve()
        if not original.is_relative_to(self.source_root) or not original.is_file():
            raise ValueError('Original source is unavailable.')
        text = original.read_text(encoding='utf-8-sig')
        self.save(relative, text)
        return text

    def validate_all(self):
        errors = []
        for name in self.files():
            if name.endswith('.py'):
                try:
                    ast.parse(self.read(name), filename=name)
                except (SyntaxError, OSError) as error:
                    errors.append(f'{name}: {error}')
        return errors

    def _assets(self):
        """Binary build assets are copied and exported, never offered to the text editor."""
        result = []
        for directory, subdirs, names in os.walk(self.root, followlinks=False):
            subdirs[:] = [name for name in subdirs if name not in SKIP_DIRS and not (Path(directory) / name).is_symlink() and not (getattr((Path(directory) / name).lstat(), 'st_file_attributes', 0) & 0x400)]
            for name in names:
                path = Path(directory) / name
                if path.suffix.lower() not in ASSET_SUFFIXES or path.is_symlink() or getattr(path.lstat(), 'st_file_attributes', 0) & 0x400:
                    continue
                if path.stat().st_size <= MAX_ASSET_BYTES:
                    result.append(str(path.relative_to(self.root)).replace('\\', '/'))
        return sorted(result)

    def export(self, destination):
        destination = Path(destination)
        if destination.exists() and any(destination.iterdir()):
            raise ValueError('Choose an empty destination folder.')
        destination.mkdir(parents=True, exist_ok=True)
        names = self.files() + self._assets()
        for name in names:
            source = self._path(name)
            if source.suffix.lower() in ASSET_SUFFIXES and source.stat().st_size > MAX_ASSET_BYTES:
                raise ValueError('A source build asset exceeds the export limit.')
            target = destination / name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
        return {'destination': str(destination), 'files': len(names)}





