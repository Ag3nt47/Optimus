"""Build and verify Optimus on the OS/architecture that will run the result.

PyInstaller is not a cross compiler. A macOS build is produced on macOS and
a Linux build on Linux. This helper never modifies system settings or runs
maintenance; its packaged checks use the read-only application commands.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import platform
import shutil
import subprocess
import sys
import tarfile
import tempfile

import package_licenses
import package_source


def target_name(system=None, machine=None):
    system = sys.platform if system is None else system
    machine = (platform.machine() if machine is None else machine).lower()
    architecture = {'amd64': 'x64', 'x86_64': 'x64', 'arm64': 'arm64', 'aarch64': 'arm64'}.get(machine)
    names = {('win32', 'x64'): 'Windows-x64', ('darwin', 'arm64'): 'macOS-arm64',
             ('darwin', 'x64'): 'macOS-x64', ('linux', 'x64'): 'Linux-x86_64'}
    try:
        return names[(system, architecture)]
    except KeyError as error:
        raise ValueError(f'No published native build target for {system}/{machine}.') from error


def build_command(source, destination, system=None, machine=None):
    system = sys.platform if system is None else system
    target = target_name(system, machine)
    source, destination = Path(source), Path(destination)
    command = [sys.executable, '-m', 'PyInstaller', '--noconfirm', '--clean', '--onedir',
               '--name', 'Optimus', '--paths', str(source / 'src'), '--collect-all', 'optimus',
               '--collect-submodules', 'psutil', '--collect-submodules', 'send2trash',
               '--distpath', str(destination), '--workpath', str(destination.parent / 'pyinstaller-work'),
               '--specpath', str(destination.parent)]
    if system in ('win32', 'darwin'):
        command.append('--windowed')
    if system == 'win32':
        command.extend(['--icon', str(source / 'src/optimus/assets/optimus.ico')])
    if system == 'darwin':
        command.extend(['--osx-bundle-identifier', 'io.github.Ag3nt47.Optimus',
                        '--target-arch', 'arm64' if target.endswith('arm64') else 'x86_64'])
    separator = ';' if system == 'win32' else ':'
    for name in ('LICENSE', 'NOTICE'):
        command.extend(['--add-data', str(source / name) + separator + '.'])
    return command + [str(source / 'launch.py')]


def bundle_paths(destination, system=None):
    system = sys.platform if system is None else system
    destination = Path(destination)
    if system == 'darwin':
        bundle = destination / 'Optimus.app'
        return bundle, bundle / 'Contents/Resources', bundle / 'Contents/MacOS/Optimus'
    bundle = destination / 'Optimus'
    return bundle, bundle, bundle / ('Optimus.exe' if system == 'win32' else 'Optimus')


def verify_package(executable, *, output=None):
    """Exercise the real packaged UI and inventory without maintenance/UAC."""
    environment = dict(os.environ)
    if sys.platform == 'linux':
        environment['QT_QPA_PLATFORM'] = 'offscreen'
    screenshot = Path(output).with_suffix('.png').resolve() if output is not None else None
    if screenshot is not None:
        screenshot.parent.mkdir(parents=True, exist_ok=True)
    smoke = ['--smoke-test'] + (['--screenshot', str(screenshot)] if screenshot is not None else [])
    with tempfile.TemporaryDirectory(prefix='optimus-native-check-') as temporary:
        root = Path(temporary).resolve()
        # Isolation keeps first-run reports and Sandbox copies out of the
        # builder's personal settings. XDG_DATA_HOME is also used by Qt.
        environment.update(LOCALAPPDATA=str(root / 'local'), XDG_DATA_HOME=str(root / 'data'),
                           XDG_STATE_HOME=str(root / 'state'), HOME=str(root / 'home'))
        (root / 'home').mkdir()
        for command in (smoke, ['--startup-probe'], ['--process-scan', '--output', str(root / 'scan')]):
            subprocess.run([str(executable), *command], check=True, timeout=150, env=environment)
        if screenshot is not None and (not screenshot.is_file() or not screenshot.stat().st_size):
            raise RuntimeError('The packaged interface screenshot was not produced.')
        reports = sorted((root / 'scan').rglob('*.json'))
        if not reports:
            raise RuntimeError('The packaged process scan produced no JSON report.')
        payloads = [json.loads(report.read_text(encoding='utf-8-sig')) for report in reports]
        inventories = [value for value in payloads if isinstance(value, dict) and isinstance(value.get('processes'), list)]
        if not inventories or not any(value['processes'] for value in inventories):
            raise RuntimeError('The packaged process scan contained no process inventory.')
        if any(action.get('selected') is True for value in inventories
               for action in value.get('actions', []) if isinstance(action, dict)):
            raise RuntimeError('A read-only process scan preselected a mutation.')
        result = {'target': target_name(), 'executable': str(executable),
                  'checks': ['interface smoke test', 'startup probe', 'read-only process report'],
                  'json_reports': len(reports), 'process_rows': sum(len(value['processes']) for value in inventories),
                  'inventory_complete': [value.get('complete') for value in inventories], 'maintenance_applied': False}
        if screenshot is not None:
            result['interface_screenshot'] = str(screenshot)
        if output is not None:
            Path(output).parent.mkdir(parents=True, exist_ok=True)
            Path(output).write_text(json.dumps(result, indent=2) + '\n', encoding='utf-8')
        return result


def archive_bundle(bundle, destination, system=None, machine=None):
    system = sys.platform if system is None else system
    target = target_name(system, machine)
    bundle, destination = Path(bundle), Path(destination)
    destination.mkdir(parents=True, exist_ok=True)
    if system == 'linux':
        result = destination / f'Optimus-{target}.tar.gz'
        # Tar preserves the executable bit and the shared-library symlinks.
        with tarfile.open(result, 'w:gz') as archive:
            archive.add(bundle, arcname='Optimus', recursive=True)
    elif system == 'darwin':
        result = destination / f'Optimus-{target}.zip'
        # ditto preserves framework symlinks and the .app's native metadata.
        subprocess.run(['/usr/bin/ditto', '-c', '-k', '--sequesterRsrc', '--keepParent',
                        str(bundle), str(result)], check=True, timeout=300)
    else:
        result = Path(shutil.make_archive(str(destination / f'Optimus-{target}'), 'zip',
                                          root_dir=bundle.parent, base_dir=bundle.name))
    return result


def write_source_archive(source, destination):
    destination = Path(destination)
    destination.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='optimus-public-source-') as temporary:
        public = Path(temporary).resolve() / 'Optimus-Source'
        package_source.package(source, public)
        return Path(shutil.make_archive(str(destination / 'Optimus-Source'), 'zip',
                                       root_dir=public.parent, base_dir=public.name))


def write_checksums(directory):
    directory = Path(directory)
    files = sorted(path for path in directory.iterdir() if path.is_file() and path.name != 'SHA256SUMS.txt')
    lines = []
    for path in files:
        with path.open('rb') as stream:
            digest = hashlib.file_digest(stream, 'sha256').hexdigest()
        lines.append(f'{digest}  {path.name}\n')
    result = directory / 'SHA256SUMS.txt'
    result.write_text(''.join(lines), encoding='ascii')
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument('--destination', type=Path, default=Path('dist'))
    parser.add_argument('--artifacts', type=Path, default=Path('release-assets'))
    parser.add_argument('--expected-target', choices=('Windows-x64', 'macOS-arm64', 'macOS-x64', 'Linux-x86_64'))
    parser.add_argument('--verify-only', action='store_true')
    parser.add_argument('--source-only', action='store_true')
    parser.add_argument('--checksums-only', action='store_true')
    arguments = parser.parse_args(argv)
    source, destination = arguments.source.resolve(), arguments.destination.resolve()
    if arguments.checksums_only:
        print(write_checksums(arguments.artifacts))
        return 0
    if arguments.source_only:
        print(write_source_archive(source, arguments.artifacts))
        return 0
    target = target_name()
    if arguments.expected_target is not None and target != arguments.expected_target:
        parser.error(f'This interpreter builds {target}, not {arguments.expected_target}.')
    bundle, resources, executable = bundle_paths(destination)
    if not arguments.verify_only:
        subprocess.run(build_command(source, destination), cwd=source, check=True)
        if sys.platform == 'win32':
            from normalize_bundle import normalize_bundle
            print(json.dumps(normalize_bundle(bundle)))
        package_source.package(source, resources / 'source')
        package_licenses.package(source, resources)
        if sys.platform == 'darwin':
            # Adding the editable source changes the bundle seal. Sign only
            # after the final payload is present. This is ad-hoc signing,
            # not Developer ID signing/notarization.
            subprocess.run(['/usr/bin/codesign', '--force', '--deep', '--sign', '-', str(bundle)], check=True)
            subprocess.run(['/usr/bin/codesign', '--verify', '--deep', '--strict', str(bundle)], check=True)
    print(json.dumps(verify_package(executable, output=Path('test-artifacts/native-package.json'))))
    if not arguments.verify_only:
        print(archive_bundle(bundle, arguments.artifacts))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
