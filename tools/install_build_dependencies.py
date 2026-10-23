"""Install pinned build dependencies without an accidental dynamic OpenSSL build.

PyCA stopped publishing Intel macOS wheels in cryptography 49. The current
cryptography pin is kept: Intel macOS builds it from source with a static
Homebrew OpenSSL, instead of bundling a second incompatible libssl. Every other
target requires binary cryptography/cffi wheels. A Darwin linkage check fails
before packaging if the Rust binding still needs an external SSL/crypto dylib.

Primary references:
https://cryptography.io/en/50.0.0/changelog/#v49-0-0
https://cryptography.io/en/50.0.0/installation/#building-cryptography-on-macos
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
from pathlib import Path
import platform
import re
import shutil
import subprocess
import sys


def intel_macos(system=None, machine=None):
    return ((sys.platform if system is None else system) == 'darwin'
            and (platform.machine() if machine is None else machine).lower() in ('x86_64', 'amd64'))


def install_command(requirements, *, system=None, machine=None, executable=None):
    command = [executable or sys.executable, '-m', 'pip', 'install']
    if intel_macos(system, machine):
        # Cached locally built wheels may contain the original dynamic link.
        command.extend(['--no-cache-dir', '--force-reinstall', '--no-binary=cryptography', '--only-binary=cffi'])
    else:
        command.append('--only-binary=cryptography,cffi')
    return command + ['-r', str(requirements)]


def install_environment(*, system=None, machine=None, environment=None, runner=None, brew=None):
    result = dict(os.environ if environment is None else environment)
    if not intel_macos(system, machine):
        return result
    runner = runner or subprocess.run
    brew = brew or shutil.which('brew')
    if not brew:
        raise RuntimeError('The Intel macOS builder requires Homebrew OpenSSL static libraries and Rust.')
    probe = runner([brew, '--prefix', 'openssl@3'], check=True, capture_output=True,
                   text=True, timeout=30)
    raw = probe.stdout.strip()
    if not raw or any(ord(character) < 32 for character in raw) or not Path(raw).is_absolute():
        raise RuntimeError('Homebrew returned an invalid OpenSSL prefix.')
    prefix = Path(raw).resolve(strict=True)
    for relative in ('lib/libssl.a', 'lib/libcrypto.a', 'include/openssl/ssl.h'):
        path = prefix / relative
        if not path.is_file() or path.stat().st_size == 0:
            raise RuntimeError('Intel macOS requires the installed OpenSSL static archives and development headers.')
    result.update(OPENSSL_STATIC='1', OPENSSL_DIR=str(prefix), OPENSSL_LIB_DIR=str(prefix / 'lib'),
                  OPENSSL_INCLUDE_DIR=str(prefix / 'include'))
    return result


def verify_static_openssl(*, system=None, module_path=None, runner=None):
    if (sys.platform if system is None else system) != 'darwin':
        return {'status': 'not_applicable', 'reason': 'Mach-O linkage inspection is macOS-specific.'}
    if module_path is None:
        spec = importlib.util.find_spec('cryptography.hazmat.bindings._rust')
        if spec is None or not spec.origin:
            raise RuntimeError('The installed cryptography Rust binding is unavailable.')
        module_path = spec.origin
    path = Path(module_path).resolve(strict=True)
    if not path.is_file() or path.suffix not in ('.so', '.dylib'):
        raise RuntimeError('The cryptography Rust binding is not a native module.')
    result = (runner or subprocess.run)(['/usr/bin/otool', '-L', str(path)], check=True,
        capture_output=True, text=True, timeout=30)
    text = result.stdout
    if not isinstance(text, str) or len(text) > 65536:
        raise RuntimeError('The Mach-O linkage inspection result is invalid or oversized.')
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if len(lines) < 2 or not lines[0].endswith(':') or not any('compatibility version' in line for line in lines[1:]):
        raise RuntimeError('The Mach-O linkage inspection did not return recognizable dependencies.')
    if any(re.search(r'(?:^|[/@])lib(?:ssl|crypto)(?:[.\-_]|$)', line, re.I) for line in lines[1:]):
        raise RuntimeError('The cryptography Rust binding still uses external OpenSSL libraries. '
                           'Rebuild it with OPENSSL_STATIC=1 and no cached source-built wheel before packaging.')
    return {'status': 'passed', 'crypto_openssl_linkage': 'static', 'binding': path.name}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--requirements', type=Path,
                        default=Path(__file__).resolve().parents[1] / 'requirements-build.txt')
    args = parser.parse_args(argv)
    requirements = args.requirements.resolve(strict=True)
    if not requirements.is_file():
        parser.error('Build requirements must be a file.')
    environment = install_environment()
    subprocess.run(install_command(requirements), check=True, env=environment)
    proof = verify_static_openssl()
    print(json.dumps({'dependency_install': 'completed',
                      'crypto_policy': 'static_source_build' if intel_macos() else 'binary_wheels_required',
                      'linkage': proof}))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
