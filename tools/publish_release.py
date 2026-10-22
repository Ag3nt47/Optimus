"""Publish complete native artifacts, refusing to replace different existing bytes."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import tempfile


ASSETS = {'Optimus-Windows-x64.zip', 'Optimus-macOS-arm64.zip', 'Optimus-macOS-x64.zip',
          'Optimus-Linux-x86_64.tar.gz', 'Optimus-Source.zip', 'SHA256SUMS.txt'}


def validated_assets(directory):
    directory = Path(directory)
    actual = {path.name for path in directory.iterdir() if path.is_file()}
    if actual != ASSETS:
        raise ValueError(f'The release must contain exactly these files: {sorted(ASSETS)}; got {sorted(actual)}.')
    if any((directory / name).is_symlink() for name in ASSETS):
        raise ValueError('Release assets must be ordinary files.')
    checksums = {}
    for line in (directory / 'SHA256SUMS.txt').read_text(encoding='ascii').splitlines():
        digest, name = line.split('  ', 1)
        if name in checksums:
            raise ValueError('Duplicate release checksum.')
        checksums[name] = digest
    if set(checksums) != ASSETS - {'SHA256SUMS.txt'}:
        raise ValueError('Release checksum names do not match the artifacts.')
    for name, expected in checksums.items():
        path = directory / name
        if path.is_symlink():
            raise ValueError('Release assets must be ordinary files.')
        with path.open('rb') as stream:
            if hashlib.file_digest(stream, 'sha256').hexdigest() != expected:
                raise ValueError('Release checksum does not match: ' + name)
    return [directory / name for name in sorted(ASSETS)]


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--tag', required=True)
    parser.add_argument('--assets', type=Path, required=True)
    parser.add_argument('--repo')
    arguments = parser.parse_args(argv)
    files = validated_assets(arguments.assets)
    repository = ['--repo', arguments.repo] if arguments.repo else []
    view = ['gh', 'release', 'view', arguments.tag, *repository, '--json', 'assets,isDraft']
    existing = subprocess.run(view, text=True, capture_output=True)
    if existing.returncode:
        # Keep incomplete downloads private. This also works when the
        # repository makes published releases immutable.
        subprocess.run(['gh', 'release', 'create', arguments.tag, *repository,
                        '--verify-tag', '--generate-notes', '--draft'], check=True)
        existing_names = set()
        is_draft = True
    else:
        metadata = json.loads(existing.stdout)
        existing_names = {item['name'] for item in metadata['assets']}
        is_draft = metadata['isDraft']
    # Validate every existing byte before uploading missing assets. A retry
    # cannot partially extend a release before discovering a conflict.
    for file in files:
        if file.name in existing_names:
            with tempfile.TemporaryDirectory(prefix='optimus-release-verify-') as temporary:
                subprocess.run(['gh', 'release', 'download', arguments.tag, *repository,
                                '--pattern', file.name, '--dir', temporary], check=True)
                with file.open('rb') as local, (Path(temporary) / file.name).open('rb') as remote:
                    if hashlib.file_digest(local, 'sha256').digest() != hashlib.file_digest(remote, 'sha256').digest():
                        raise RuntimeError('An existing release asset differs; refusing to overwrite: ' + file.name)
    for file in files:
        if file.name not in existing_names:
            subprocess.run(['gh', 'release', 'upload', arguments.tag, *repository, str(file)], check=True)
    if is_draft:
        subprocess.run(['gh', 'release', 'edit', arguments.tag, *repository, '--draft=false', '--latest'], check=True)
    print(f'Published {len(files)} individually downloadable assets for {arguments.tag}.')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
