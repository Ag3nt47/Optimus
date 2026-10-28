"""Native maintenance primitives for macOS and Linux.

Read operations never acquire administrator privileges. Mutations accept only
fresh, narrowly validated identities; subprocesses always receive argv lists.
"""
from __future__ import annotations

import configparser
import hashlib
import json
import os
from pathlib import Path
import platform
import plistlib
import re
import shlex
import shutil
import stat
import subprocess
import sys
import threading
import time
import uuid
from xml.parsers.expat import ExpatError

from .workflow import atomic_json


class UnsupportedOperation(RuntimeError):
    """The current operating system cannot provide this operation safely."""


def protected_startup(record):
    name = str(record.get('Name', '')).casefold()
    filename = Path(str(record.get('Path', ''))).name.casefold()
    return (name.startswith(('com.ag3nt47.optimus', 'io.github.ag3nt47.optimus', 'optimus')) or
            filename.startswith(('com.ag3nt47.optimus.', 'io.github.ag3nt47.optimus.')))


def _uid():
    return os.getuid() if hasattr(os, 'getuid') else None


def _owned(path):
    identity = _uid()
    return identity is None or path.stat().st_uid == identity


def _within(path, root):
    try:
        return Path(path).is_relative_to(Path(root))
    except (OSError, ValueError):
        return False


def ordinary_path(path, *, directory=False, root=None, owner=False):
    """Reject links in the selected path, including every existing ancestor."""
    path = Path(os.path.abspath(os.path.expanduser(str(path))))
    if root is not None and (path == Path(root) or not _within(path, root)):
        raise ValueError('Select an ordinary folder or file below the approved root.')
    for component in (path, *path.parents):
        info = component.lstat()
        if stat.S_ISLNK(info.st_mode):
            # /tmp and /var are OS-owned aliases on macOS. Callers must resolve
            # such roots before granting a scope, rather than widen it here.
            raise ValueError('Symbolic links are excluded from maintenance scopes.')
    info = path.stat()
    if directory and not stat.S_ISDIR(info.st_mode):
        raise ValueError('Select an existing ordinary directory.')
    if not directory and (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1):
        raise ValueError('Only ordinary files with one link can be changed.')
    if owner and not _owned(path):
        raise ValueError('This item is not owned by the signed-in user.')
    return path


def fingerprint(path):
    info = Path(path).stat()
    return {'device': info.st_dev, 'inode': info.st_ino, 'size': info.st_size,
            'mtime_ns': info.st_mtime_ns, 'ctime_ns': info.st_ctime_ns}


def sha256_file(path, *, cancel_event=None, deadline=None):
    digest = hashlib.sha256()
    # O_NOFOLLOW prevents a last-component link swap where the OS supports it.
    descriptor = os.open(path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0))
    with os.fdopen(descriptor, 'rb') as stream:
        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
            raise ValueError('The file identity changed while reading it.')
        while True:
            if cancel_event is not None and cancel_event.is_set():
                raise InterruptedError('Cancelled before hashing completed.')
            if deadline is not None and time.monotonic() >= deadline:
                raise TimeoutError('The bounded content-hash verification reached its time limit.')
            block = stream.read(1024 * 1024)
            if not block:
                break
            digest.update(block)
    return digest.hexdigest()


class NativeRunner:
    """Bounded read-only commands and an explicit native privilege broker."""
    def __init__(self, system=None):
        self.system = system or sys.platform

    def run(self, argv, *, timeout=90, cancel_event=None):
        if cancel_event is not None and cancel_event.is_set():
            return {'exitCode': 0, 'cancelled': True, 'stdout': '', 'stderr': ''}
        environment = dict(os.environ, LC_ALL='C', LANG='C', HOMEBREW_NO_AUTO_UPDATE='1',
                           HOMEBREW_NO_INSTALL_CLEANUP='1')
        try:
            completed = subprocess.run([str(value) for value in argv], capture_output=True,
                                       text=True, errors='replace', timeout=timeout, env=environment)
        except subprocess.TimeoutExpired as error:
            # subprocess.run kills and waits for the direct child on timeout;
            # installer descendants may outlive it. Never report success.
            raise RuntimeError('The native command timed out; verify its state before retrying.') from error
        return {'exitCode': completed.returncode, 'stdout': completed.stdout,
                'stderr': completed.stderr, 'command': Path(str(argv[0])).name}

    def privileged(self, argv, *, timeout=900, cancel_event=None):
        if cancel_event is not None and cancel_event.is_set():
            return {'exitCode': 0, 'cancelled': True, 'skipped': True}
        # There is no GUI-wide root mode. The OS asks only for this one
        # already-reviewed action. Never use sudo -S, cached passwords or an
        # automatic click-through.
        if hasattr(os, 'geteuid') and os.geteuid() == 0:
            return self.run(argv, timeout=timeout, cancel_event=cancel_event)
        if self.system == 'darwin':
            broker = Path('/usr/bin/osascript')
            if not broker.is_file():
                raise UnsupportedOperation('macOS authorization services are unavailable.')
            command = shlex.join([str(value) for value in argv])
            script = 'do shell script ' + json.dumps(command) + ' with administrator privileges'
            return self.run([str(broker), '-e', script], timeout=timeout, cancel_event=cancel_event)
        broker = Path('/usr/bin/pkexec')
        if not broker.is_file():
            raise UnsupportedOperation('Installations need an active desktop polkit agent. Run the shown package command in your terminal if polkit is unavailable.')
        return self.run([str(broker), *argv], timeout=timeout, cancel_event=cancel_event)


_PACKAGE = re.compile(r'^[A-Za-z0-9][A-Za-z0-9+_.@:/-]{0,255}$')
_VERSION = re.compile(r'^[A-Za-z0-9][A-Za-z0-9+_.:~^%-]{0,255}$')


class NativePackages:
    def __init__(self, data_dir, *, system=None, runner=None):
        self.data_dir = Path(data_dir)
        self.system = system or sys.platform
        self.runner = runner or NativeRunner(self.system)
        self.manager, self.executable = self._discover()
        self._last_scan = None
        self._lock = threading.RLock()

    def _discover(self):
        choices = ([('brew', '/opt/homebrew/bin/brew'), ('brew', '/usr/local/bin/brew')]
                   if self.system == 'darwin' else
                   [('apt', '/usr/bin/apt'), ('dnf', '/usr/bin/dnf'), ('pacman', '/usr/bin/pacman')])
        for manager, executable in choices:
            if Path(executable).is_file():
                return manager, executable
        return None, None

    def status(self):
        return {'available': self.manager is not None, 'source': self.manager,
                'scope': 'Only packages registered with this native package manager are covered.',
                'automatic_install_supported': self.manager in ('brew', 'apt', 'dnf'),
                'note': ('Arch Linux requires a complete system upgrade; individual package upgrades are review-only to avoid unsupported partial upgrades.'
                         if self.manager == 'pacman' else 'Signed repository and vendor update policy remains controlled by the native package manager. No unofficial drivers or lifecycle bypasses are downloaded.')}

    def _command(self, argv, *, allowed=(0,), timeout=120, cancel_event=None, evidence=None):
        options = {'cancel_event': cancel_event} if cancel_event is not None else {}
        result = self.runner.run(argv, timeout=timeout, **options)
        if evidence is not None:
            evidence.update(exitCode=result.get('exitCode'), stderr=result.get('stderr', ''))
        if result.get('cancelled'):
            raise InterruptedError('Native package inventory was cancelled.')
        if result['exitCode'] not in allowed:
            raise RuntimeError(Path(str(argv[0])).name + ' could not read package metadata: ' + result.get('stderr', '')[:1000])
        return result['stdout']

    @staticmethod
    def _record(manager, name, installed, available=None, **extra):
        if manager == 'dnf':
            # rpm's EPOCHNUM field includes 0:, while check-update omits the
            # zero epoch. Normalize equivalent NEVRA version identities before
            # comparing the fresh offer with the post-install rpm inventory.
            if not re.match(r'^\d+:', installed):
                installed = '0:' + installed
            if available is not None and not re.match(r'^\d+:', available):
                available = '0:' + available
        if not _PACKAGE.fullmatch(name) or not _VERSION.fullmatch(installed):
            raise ValueError('The native package manager returned an invalid identity.')
        record = {'id': manager + ':' + name, 'package_id': manager + ':' + name,
                  'Identity': manager + ':' + name, 'Name': name, 'name': name,
                  'Version': installed, 'version': installed, 'source': manager,
                  'native_name': name, 'offered_by_manager': available is not None}
        if available is not None:
            if not _VERSION.fullmatch(available) or available == installed:
                raise ValueError('The native package manager did not offer a changed version.')
            record['available'] = available
            record['installedVersion'] = installed
            record['availableVersion'] = available
            record['eligible'] = True
        return dict(record, **extra)

    def installed(self, *, timeout=120, cancel_event=None):
        options = {'timeout': max(0.1, min(float(timeout), 120)), 'cancel_event': cancel_event}
        if self.manager is None:
            return []
        if self.manager == 'brew':
            result = json.loads(self._command([self.executable, 'info', '--json=v2', '--installed'], **options))
            records = []
            for item in result.get('formulae', []):
                installed = item.get('installed', [])
                if installed:
                    records.append(self._record('brew', item.get('full_name') or item['name'], str(installed[-1]['version']), kind='formula'))
            for item in result.get('casks', []):
                installed = item.get('installed')
                if installed:
                    if isinstance(installed, list):
                        installed = installed[-1]
                    try:
                        records.append(self._record('brew', item.get('full_token') or item['token'], str(installed), kind='cask'))
                    except ValueError:
                        continue  # "latest" / vendor comma versions are not safe unattended targets.
            return records
        if self.manager == 'apt':
            output = self._command(['/usr/bin/dpkg-query', '-W', '-f=${binary:Package}\t${Version}\t${db:Status-Status}\n'], **options)
            return [self._record('apt', parts[0], parts[1]) for line in output.splitlines()
                    if len(parts := line.split('\t')) == 3 and parts[2] == 'installed']
        if self.manager == 'dnf':
            output = self._command(['/usr/bin/rpm', '-qa', '--qf', '%{NAME}.%{ARCH}\t%{EPOCHNUM}:%{VERSION}-%{RELEASE}\n'], **options)
            return [self._record('dnf', parts[0], parts[1]) for line in output.splitlines()
                    if len(parts := line.split('\t')) == 2]
        output = self._command([self.executable, '-Q'], **options)
        return [self._record('pacman', parts[0], parts[1]) for line in output.splitlines()
                if len(parts := line.split()) == 2]

    def scan(self):
        started = time.time()
        if self.manager is None:
            return {'available': False, 'source': None, 'updates': [], 'structured': True,
                    'exitCode': 0, 'complete': False, 'discovery': {'started_at': started, 'completed_at': time.time()},
                    'note': 'No supported native package manager was found.'}
        installed = {item['native_name']: item for item in self.installed()}
        updates, excluded, query = [], [], {}
        if self.manager == 'brew':
            result = json.loads(self._command([self.executable, 'outdated', '--json=v2'], evidence=query))
            if not isinstance(result, dict) or not any(key in result for key in ('formulae', 'casks')):
                raise ValueError('Homebrew returned an unrecognized update inventory.')
            for kind, key in (('formula', 'formulae'), ('cask', 'casks')):
                if not isinstance(result.get(key, []), list):
                    raise ValueError('Homebrew returned an invalid update inventory.')
                for item in result.get(key, []):
                    if not isinstance(item, dict):
                        raise ValueError('Homebrew returned an invalid update row.')
                    name = item.get('full_name') or item.get('full_token') or item.get('name') or item.get('token')
                    current = installed.get(name)
                    offered = str(item.get('current_version', ''))
                    if current and not item.get('pinned') and current['version'] != offered:
                        try:
                            updates.append(self._record('brew', name, current['version'], offered, kind=kind))
                        except ValueError:
                            excluded.append('A Homebrew offer has an unsupported identity or version.')
                            continue
                    else:
                        excluded.append('A pinned, unmatched or unchanged Homebrew offer was excluded.')
        elif self.manager == 'apt':
            output = self._command([self.executable, 'list', '--upgradable'], evidence=query)
            if not output.strip():
                excluded.append('APT returned an empty unrecognized update response.')
            for line in output.splitlines():
                match = re.fullmatch(r'([^/\s]+)/\S+\s+(\S+)\s+\S+\s+\[upgradable from: ([^\]]+)\]', line.strip())
                if not match:
                    if line.strip() and not re.fullmatch(r'Listing\.\.\.(?:\s*Done)?', line.strip()):
                        excluded.append('An unrecognized APT update row was excluded.')
                    continue
                name, available, old = match.groups()
                current = installed.get(name) or next((item for key, item in installed.items() if key.split(':')[0] == name and item['version'] == old), None)
                if current is None or current['version'] != old:
                    excluded.append('An APT offer could not be matched to the installed version.')
                    continue
                # Let dpkg implement Debian's complete epoch/tilde ordering.
                newer = self.runner.run(['/usr/bin/dpkg', '--compare-versions', available, 'gt', old])
                if newer['exitCode'] == 0:
                    updates.append(self._record('apt', current['native_name'], old, available))
                else:
                    excluded.append('An APT offered version could not be confirmed newer.')
        elif self.manager == 'dnf':
            output = self._command([self.executable, '--cacheonly', 'check-update'], allowed=(0, 100), evidence=query)
            for line in output.splitlines():
                parts = line.split()
                if len(parts) != 3 or parts[0] not in installed or not _VERSION.fullmatch(parts[1]):
                    if line.strip() and not line.startswith('Last metadata expiration check:'):
                        excluded.append('An unrecognized or unmatched DNF update row was excluded.')
                    continue
                current = installed[parts[0]]
                try:
                    # check-update already applies RPM ordering, including an
                    # epoch-only upgrade. Do not discard that epoch evidence.
                    updates.append(self._record('dnf', parts[0], current['version'], parts[1]))
                except ValueError:
                    excluded.append('A DNF offer has an unsupported identity or version.')
                    continue
        else:
            output = self._command([self.executable, '-Qu'], allowed=(0, 1), evidence=query)
            for line in output.splitlines():
                parts = line.split()
                if len(parts) == 4 and parts[2] == '->' and parts[0] in installed:
                    updates.append(self._record('pacman', parts[0], parts[1], parts[3]))
                elif line.strip():
                    excluded.append('An unrecognized or unmatched pacman update row was excluded.')
        query_code = query.get('exitCode')
        if type(query_code) is not int:
            excluded.append('The native update query completion status could not be verified.')
        elif self.manager == 'dnf' and ((query_code == 100 and not updates) or (query_code == 0 and updates)):
            excluded.append('The DNF update status does not match the recognized offered updates.')
        elif self.manager == 'pacman' and query_code == 1 and updates:
            excluded.append('The pacman query status does not match the recognized offered updates.')
        raw_diagnostic = str(query.get('stderr', ''))[:4000]
        diagnostics = raw_diagnostic.strip()
        if self.manager == 'apt':
            diagnostics = '\n'.join(line for line in diagnostics.splitlines() if line.strip() and line.strip() !=
                'WARNING: apt does not have a stable CLI interface. Use with caution in scripts.')
        if diagnostics:
            excluded.append('The native manager reported diagnostic output; repository coverage requires review.')
        ids = [item['id'] for item in updates]
        if len(ids) != len(set(ids)):
            raise ValueError('Ambiguous native package identities; refresh before installing.')
        result = {'available': True, 'source': self.manager, 'structured': True, 'exitCode': 0,
                  'complete': not excluded, 'updates': updates, 'scanned_at': time.time(),
                  'excluded': excluded[:1000], 'installed_packages_checked': len(installed),
                  'manager_exit_code': query_code, 'stderr': diagnostics,
                  'manager_diagnostics': raw_diagnostic,
                  'discovery': {'started_at': started, 'completed_at': time.time()},
                  'note': 'Offered updates use existing native repository metadata. Use the native updater to refresh repositories; unsupported, held, pinned, unregistered and incomparable packages are not forced.'}
        self._last_scan = result
        return result

    def apply(self, identity, *, confirmed=False, cancel_event=None, progress_callback=None):
        if confirmed is not True:
            raise ValueError('Explicit package update approval is required.')
        if progress_callback is not None and not callable(progress_callback):
            raise ValueError('An optional native install progress observer must be callable.')
        if not isinstance(identity, str) or len(identity) > 280:
            raise ValueError('Choose one exact native package identity.')
        def observe(phase, percent=None, *, terminal=False):
            if progress_callback is not None:
                try:
                    progress_callback({'key': 'software:update:' + identity, 'label': identity,
                        'phase': phase, 'percent': percent, 'item_terminal': terminal})
                except Exception:
                    pass  # Display failures cannot abandon or replay native work.
        with self._lock:
            no_install = {'installer_started': False, 'outcomeKnown': True, 'running': False}
            if cancel_event is not None and cancel_event.is_set():
                return dict(no_install, exitCode=0, skipped=True, cancelled=True)
            fresh = self.scan()
            candidates = [item for item in fresh['updates'] if item['id'] == identity]
            if len(candidates) != 1:
                return {**no_install, 'skipped': True, 'exitCode': 0, 'status': 'no_update',
                        'note': 'This package no longer has an offered update; no installer ran.'}
            item = candidates[0]
            if self.manager == 'pacman':
                raise UnsupportedOperation('Use the native full-system updater (pacman -Syu). Optimus does not create unsupported partial Arch upgrades.')
            if cancel_event is not None and cancel_event.is_set():
                return dict(no_install, exitCode=0, skipped=True, cancelled=True)
            if shutil.disk_usage(Path.home()).free < 2 * 1024 ** 3:
                return {**no_install, 'exitCode': 0, 'skipped': True, 'requiresReview': True,
                        'note': 'At least 2 GiB free on the user filesystem is required before installation; individual packages may need more.'}
            # Native broker commands expose no authenticated intermediate
            # percentage through this adapter. Keep the install indeterminate.
            observe('install')
            if self.manager == 'brew':
                argv = [self.executable, 'upgrade', '--formula' if item['kind'] == 'formula' else '--cask', '--', item['native_name']]
                result = self.runner.run(argv, timeout=900, cancel_event=cancel_event)
            elif self.manager == 'apt':
                # No removals; upgrade the already-installed requested package
                # to its exact offer. Native dependency resolution still applies.
                argv = ['/usr/bin/apt-get', '--assume-yes', '--no-remove', '--only-upgrade', 'install', item['native_name'] + '=' + item['available']]
                result = self.runner.privileged(argv, timeout=900, cancel_event=cancel_event)
            else:
                name, _, arch = item['native_name'].rpartition('.')
                target = name + '-' + item['available'] + '.' + arch
                result = self.runner.privileged([self.executable, '--assumeyes', 'upgrade', target], timeout=900, cancel_event=cancel_event)
            result.update(id=identity, from_version=item['version'], offered_version=item['available'])
            from .update_payload_lifecycle import cleanup_enabled, retained_receipt
            payload_cleanup_enabled = cleanup_enabled(self.data_dir)
            result['payload_cleanup'] = retained_receipt(self.manager, payload_cleanup_enabled,
                'This native adapter has no Optimus-owned download workspace with verified descendant quiescence; native package caches are preserved.',
                status='provider_managed' if payload_cleanup_enabled else 'disabled')
            if result.get('cancelled'):
                return dict(result, **no_install)
            result.update(installer_started=True, outcomeKnown=True, running=False)
            observe('verify')
            after = next((row for row in self.installed() if row['id'] == identity), None)
            result['installed_version'] = after['version'] if after else None
            result['verified'] = bool(result['exitCode'] == 0 and after and after['version'] == item['available'] and after['version'] != item['version'])
            result['success'] = result['verified']
            if result['exitCode'] == 0 and not result['verified']:
                result['requiresReview'] = True
                result['note'] = 'The package manager returned success but the installed-version change could not be verified.'
            if result['verified'] is True and result.get('restartNeeded') is not True:
                observe('install', 100.0, terminal=True)
            return result


class NativeFiles:
    def __init__(self, *, home=None, data_dir=None):
        self.home = Path(home or Path.home()).resolve()
        self.data_dir = Path(data_dir) if data_dir is not None else self.home / '.local/share/Optimus'
        self._plans = {}
        self._lock = threading.RLock()

    def _root(self, root):
        # Native tools are intentionally scoped to selected user folders. No
        # root filesystem, system package or another user's content is deleted.
        return ordinary_path(root, directory=True, root=self.home, owner=True)

    def _walk(self, root, *, cancel_event=None, max_files=100000, max_seconds=60):
        stack, count = [root], 0
        deadline = time.monotonic() + max_seconds
        while stack:
            if count >= max_files or time.monotonic() >= deadline:
                raise TimeoutError('The bounded file scan is incomplete; select a smaller folder.')
            if cancel_event is not None and cancel_event.is_set():
                raise InterruptedError('File scan cancelled.')
            directory = stack.pop()
            ordinary_path(directory, directory=True, owner=True)
            with os.scandir(directory) as entries:
                for entry in entries:
                    if entry.is_symlink():
                        continue
                    # Windows fixture runs and some filesystems do not expose
                    # inode/link/device information in a DirEntry stat cache.
                    info = Path(entry.path).lstat()
                    count += 1
                    if count > max_files:
                        raise TimeoutError('The bounded file scan is incomplete; select a smaller folder.')
                    if stat.S_ISDIR(info.st_mode):
                        if (not entry.name.startswith('.optimus-recycle-') and not _within(Path(entry.path), self.data_dir)
                                and info.st_dev == root.stat().st_dev and _owned(Path(entry.path))):
                            stack.append(Path(entry.path))
                    elif stat.S_ISREG(info.st_mode) and info.st_nlink == 1 and _owned(Path(entry.path)):
                        yield Path(entry.path), info

    def storage(self, root, *, cancel_event=None):
        root = self._root(root)
        report = {'Root': str(root), 'root': str(root), 'complete': True, 'files': [], 'errors': [], 'TotalBytes': 0, 'FileCount': 0}
        try:
            for path, info in self._walk(root, cancel_event=cancel_event):
                report['TotalBytes'] += info.st_size
                report['FileCount'] += 1
                report['files'].append({'Path': str(path), 'Size': info.st_size, 'SizeMB': round(info.st_size / 1048576, 3)})
        except (OSError, ValueError, TimeoutError, InterruptedError) as error:
            report['complete'] = False
            report['errors'].append(str(error))
        report['files'] = sorted(report['files'], key=lambda row: row['Size'], reverse=True)[:500]
        return report

    def duplicates(self, root, *, cancel_event=None):
        root = self._root(root)
        plan_id = uuid.uuid4().hex
        report = {'id': plan_id, 'Root': str(root), 'root': str(root), 'complete': True,
                  'groups': [], 'duplicates': [], 'errors': [], 'scanned_files': 0,
                  'note': 'SHA256 equality is verified again immediately before moving a selected extra copy to the operating-system Trash. One verified keeper always remains.'}
        sizes, authority = {}, {}
        deadline = time.monotonic() + 60
        try:
            for path, info in self._walk(root, cancel_event=cancel_event):
                report['scanned_files'] += 1
                if info.st_size:
                    sizes.setdefault(info.st_size, []).append(path)
            for size, paths in sizes.items():
                if len(paths) < 2:
                    continue
                hashes = {}
                for path in paths:
                    ordinary_path(path, root=root, owner=True)
                    before = fingerprint(path)
                    digest = sha256_file(path, cancel_event=cancel_event, deadline=deadline)
                    if before != fingerprint(path):
                        raise ValueError('A file changed during hashing; the scan cannot authorize deletion.')
                    hashes.setdefault(digest, []).append((path, before))
                for digest, copies in hashes.items():
                    if len(copies) < 2:
                        continue
                    group_id = uuid.uuid4().hex
                    rows = []
                    for path, identity in copies:
                        row = dict(id=uuid.uuid4().hex, plan_id=plan_id, group_id=group_id,
                                   Path=str(path), path=str(path), Size=size,
                                   SHA256=digest, Hash=digest, modified_at=path.stat().st_mtime,
                                   created_at=path.stat().st_ctime, **identity)
                        rows.append(row)
                        authority[row['id']] = row.copy()
                    report['groups'].append({'id': group_id, 'Hash': digest, 'Size': size, 'files': rows, 'Count': len(rows)})
                    report['duplicates'].extend(rows)
        except (OSError, ValueError, TimeoutError, InterruptedError) as error:
            report['complete'] = False
            report['errors'].append(str(error))
        if report['complete']:
            with self._lock:
                self._plans = {key: value for key, value in self._plans.items() if value['expires_at'] >= time.time()}
                while len(self._plans) >= 10:
                    del self._plans[next(iter(self._plans))]
                self._plans[plan_id] = {'expires_at': time.time() + 900, 'root': root, 'files': authority}
        return report

    def recycle(self, selection, *, cancel_event=None):
        if not isinstance(selection, dict):
            raise ValueError('Select a current duplicate-scan row.')
        with self._lock:
            if cancel_event is not None and cancel_event.is_set():
                raise InterruptedError('Duplicate recycling cancelled before any change.')
            deadline = time.monotonic() + 120
            plan = self._plans.get(selection.get('plan_id'))
            if not plan or plan['expires_at'] < time.time():
                raise ValueError('The duplicate scan expired. Scan this folder again.')
            row = plan['files'].get(selection.get('id'))
            if row is None:
                raise ValueError('This file is not authorized by the current scan.')
            path = ordinary_path(row['Path'], root=plan['root'], owner=True)
            if fingerprint(path) != {key: row[key] for key in ('device', 'inode', 'size', 'mtime_ns', 'ctime_ns')}:
                raise ValueError('The selected duplicate changed. Scan again before recycling.')
            keepers = [copy for copy in plan['files'].values() if copy['group_id'] == row['group_id'] and copy['id'] != row['id']]
            keeper, keeper_identity = None, None
            for copy in keepers:
                try:
                    candidate = ordinary_path(copy['Path'], root=plan['root'], owner=True)
                    if fingerprint(candidate) != {key: copy[key] for key in ('device', 'inode', 'size', 'mtime_ns', 'ctime_ns')}:
                        continue
                    if sha256_file(candidate, cancel_event=cancel_event, deadline=deadline) == row['SHA256']:
                        keeper = candidate
                        keeper_identity = fingerprint(candidate)
                        break
                except (OSError, ValueError):
                    continue
            if keeper is None or sha256_file(path, cancel_event=cancel_event, deadline=deadline) != row['SHA256']:
                raise ValueError('A current identical keeper could not be verified. No file was recycled.')
            # Recheck both identities after reading their full content.
            if fingerprint(path) != {key: row[key] for key in ('device', 'inode', 'size', 'mtime_ns', 'ctime_ns')}:
                raise ValueError('The selected file changed while verifying its content.')
            if fingerprint(keeper) != keeper_identity:
                raise ValueError('The keeper changed while verifying the selected extra copy.')
            # Stage through an inode-checked, no-follow parent handle before a
            # path-based native Trash API receives the item. A replacement
            # raced into the original basename is preserved, never deleted.
            change_id = uuid.uuid4().hex
            stage_directory = path.with_name('.optimus-recycle-' + change_id)
            staged = stage_directory / path.name
            receipt = {'id': change_id, 'state': 'prepared', 'path': str(path), 'keeper': str(keeper),
                       'staged_path': str(staged), 'sha256': row['SHA256'], 'identity': row.copy(), 'created_at': time.time()}
            journal = self.data_dir / 'native-recycle' / (change_id + '.json')
            atomic_json(journal, receipt)  # A recovery map exists before any rename.
            parent_fd, stage_fd = None, None
            descriptors = []
            moved = False
            try:
                if os.name != 'nt':
                    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
                    descriptor = os.open(path.anchor, flags)
                    descriptors.append(descriptor)
                    for part in path.parent.parts[1:]:
                        descriptor = os.open(part, flags, dir_fd=descriptor)
                        descriptors.append(descriptor)
                    parent_fd = descriptor
                    os.mkdir(stage_directory.name, mode=0o700, dir_fd=parent_fd)
                    stage_fd = os.open(stage_directory.name, flags, dir_fd=parent_fd)
                    descriptors.append(stage_fd)
                    os.rename(path.name, staged.name, src_dir_fd=parent_fd, dst_dir_fd=stage_fd)
                else:
                    stage_directory.mkdir(mode=0o700)
                    path.rename(staged)
                moved = True
                # Rename changes ctime on Unix; device/inode/content metadata
                # still have to match the scan-approved original exactly.
                expected = {key: row[key] for key in ('device', 'inode', 'size', 'mtime_ns')}
                actual = fingerprint(ordinary_path(staged, root=plan['root'], owner=True))
                if any(actual[key] != value for key, value in expected.items()):
                    raise ValueError('The duplicate identity changed during staging; its bytes were preserved.')
                if fingerprint(keeper) != keeper_identity or sha256_file(staged, cancel_event=cancel_event, deadline=deadline) != row['SHA256']:
                    raise ValueError('The duplicate or keeper changed during staging; no file was recycled.')
                if fingerprint(keeper) != keeper_identity:
                    raise ValueError('The keeper changed while the staged extra copy was hashed.')
                if parent_fd is not None:
                    opened = os.fstat(parent_fd)
                    current = path.parent.stat()
                    if (opened.st_dev, opened.st_ino) != (current.st_dev, current.st_ino):
                        raise ValueError('The selected parent directory moved during staging.')
                from send2trash import send2trash
                send2trash(str(staged))
                if staged.exists():
                    raise RuntimeError('The staged file remains in place; recycling could not be verified.')
                moved = False
                receipt.update(state='recycled', verified=True, recycled_at=time.time())
                atomic_json(journal, receipt)
            finally:
                if moved:
                    # Restore only into a free original basename. If another
                    # file appeared there, retain the staged bytes for review.
                    try:
                        if parent_fd is not None:
                            try:
                                os.stat(path.name, dir_fd=parent_fd, follow_symlinks=False)
                            except FileNotFoundError:
                                # link() fails atomically if an application
                                # recreates the original name after the probe.
                                # POSIX rename() would overwrite those bytes.
                                os.link(staged.name, path.name, src_dir_fd=stage_fd, dst_dir_fd=parent_fd, follow_symlinks=False)
                                os.unlink(staged.name, dir_fd=stage_fd)
                        elif not path.exists() and staged.exists():
                            staged.rename(path)
                    except OSError:
                        receipt['recovery_required'] = True
                    receipt.update(state='preserved', verified=False, preserved_at=time.time())
                    try:
                        atomic_json(journal, receipt)
                    except OSError:
                        pass  # The complete prepared recovery map already exists.
                for descriptor in reversed(descriptors):
                    os.close(descriptor)
            del plan['files'][row['id']]
            return {'recycled': True, 'verified': True, 'path': str(path), 'keeper': str(keeper),
                    'bytes': row['Size'], 'sha256': row['SHA256'], 'trash_name': staged.name, 'journal_path': str(journal),
                    'restore_staging_folder': str(stage_directory),
                    'note': 'The inode-checked extra copy is in the operating-system Trash under its original filename. Put Back can restore it into the recorded private staging folder; then move it to the original path ' + str(path) + '. The local journal retains both locations.'}


class NativeStartup:
    """User-owned Linux autostart and macOS LaunchAgent changes with recovery."""
    def __init__(self, data_dir, *, system=None, home=None, runner=None):
        self.data_dir = Path(data_dir)
        self.system = system or sys.platform
        self.home = Path(home or Path.home()).resolve()
        self.runner = runner or NativeRunner(self.system)
        if self.system == 'darwin':
            self.root = self.home / 'Library/LaunchAgents'
        else:
            configured = Path(os.environ.get('XDG_CONFIG_HOME', str(self.home / '.config')))
            self.root = configured / 'autostart'
        self.history_path = self.data_dir / 'native-startup-changes.json'
        self._lock = threading.RLock()

    def _valid(self, path):
        if not _within(self.root, self.home):
            raise ValueError('The autostart folder must be under this user home.')
        path = ordinary_path(path, root=self.root, owner=True)
        if path.parent != self.root or path.stat().st_size > 1024 * 1024:
            raise ValueError('Only small direct user-owned startup registrations can be changed.')
        suffixes = ('.plist', '.disabled') if self.system == 'darwin' else ('.desktop',)
        if path.suffix not in suffixes:
            raise ValueError('This is not a native startup registration.')
        return path

    def _entry(self, path):
        path = self._valid(path)
        content = path.read_bytes()
        if self.system == 'darwin':
            try:
                payload = plistlib.loads(content)
            except (plistlib.InvalidFileException, ExpatError, ValueError, OverflowError) as error:
                raise ValueError('The LaunchAgent plist is invalid.') from error
            if not isinstance(payload, dict):
                raise ValueError('The LaunchAgent plist must contain a dictionary.')
            label = payload.get('Label')
            if not isinstance(label, str) or not re.fullmatch(r'[A-Za-z0-9_.-]{1,255}', label):
                raise ValueError('The LaunchAgent label is invalid.')
            name = label
            program = payload.get('Program')
            arguments = payload.get('ProgramArguments')
            if program is not None and (not isinstance(program, str) or not program or '\x00' in program):
                raise ValueError('The LaunchAgent Program field is invalid.')
            if arguments is not None and (not isinstance(arguments, list) or not arguments or
                                          any(not isinstance(item, str) or '\x00' in item for item in arguments)):
                raise ValueError('The LaunchAgent ProgramArguments field is invalid.')
            program = program or (arguments or [''])[0]
            if not program:
                raise ValueError('The LaunchAgent executable is unavailable.')
            enabled = path.suffix == '.plist' and payload.get('Disabled') is not True
        else:
            parser = configparser.ConfigParser(interpolation=None, strict=True)
            parser.optionxform = str
            parser.read_string(content.decode('utf-8'))
            section = parser['Desktop Entry']
            if section.get('Type') != 'Application':
                raise ValueError('Only application autostart entries are supported.')
            name = section.get('Name', path.stem)
            command = section.get('Exec', '')
            try:
                program = shlex.split(command)[0]
            except (ValueError, IndexError):
                program = ''
            enabled = section.get('Hidden', '').casefold() != 'true' and section.get('X-GNOME-Autostart-enabled', 'true').casefold() != 'false'
        protected = protected_startup({'Name': name, 'Path': str(path)})
        return {'Source': 'native-startup', 'Name': str(name)[:255], 'Path': str(path),
                'SHA256': hashlib.sha256(content).hexdigest(), 'Enabled': bool(enabled),
                'Command': str(program), 'Program': str(program), 'Manageable': True,
                'CanDisable': enabled and not protected, 'CanManage': not protected, 'Protected': protected, 'Location': str(path),
                'note': 'The executable is startup evidence; it does not prove how any currently running instance was launched.'}

    def list(self):
        if not self.root.exists():
            return []
        ordinary_path(self.root, directory=True, owner=True)
        results = []
        for path in sorted(self.root.iterdir()):
            if path.suffix not in (('.plist', '.disabled') if self.system == 'darwin' else ('.desktop',)):
                continue
            try:
                results.append(self._entry(path))
            except (OSError, ValueError, KeyError, configparser.Error, plistlib.InvalidFileException) as error:
                results.append({'Source': 'native-startup', 'Name': path.name, 'Path': str(path),
                                'Manageable': False, 'CanManage': False, 'CanDisable': False, 'Protected': True, 'Enabled': None,
                                'error': type(error).__name__})
        return results

    @staticmethod
    def descriptor(record):
        return {key: record.get(key) for key in ('Source', 'Name', 'Path', 'SHA256', 'Enabled')}

    def history(self):
        try:
            value = json.loads(self.history_path.read_text(encoding='utf-8'))
            if not isinstance(value, list):
                raise ValueError('The native recovery journal is invalid.')
            return value
        except FileNotFoundError:
            return []

    def set(self, record, enabled):
        if type(enabled) is not bool:
            raise ValueError('Startup state must be on or off.')
        with self._lock:
            current = self._entry(record.get('Path', ''))
            if self.descriptor(current) != self.descriptor(record):
                raise ValueError('The startup registration changed; refresh before applying.')
            if protected_startup(current):
                raise ValueError('Use the Optimus launch-at-sign-in setting for its own registration.')
            if current['Enabled'] == enabled:
                return {'changed': False, 'verified': True}
            path = Path(current['Path'])
            before = path.read_bytes()
            identifier = uuid.uuid4().hex
            backup = self.data_dir / 'native-startup-backups' / (identifier + '.backup')
            backup.parent.mkdir(parents=True, exist_ok=True)
            backup.write_bytes(before)
            os.chmod(backup, 0o600)
            destination = path
            planned = before
            if self.system == 'darwin':
                destination = path.with_suffix('.plist' if enabled else '.disabled')
                if destination != path and destination.exists():
                    raise ValueError('A conflicting startup file exists; no file was overwritten.')
                payload = plistlib.loads(before)
                if enabled and payload.get('Disabled') is True:
                    payload['Disabled'] = False
                    planned = plistlib.dumps(payload, fmt=plistlib.FMT_BINARY if before.startswith(b'bplist') else plistlib.FMT_XML)
            else:
                parser = configparser.ConfigParser(interpolation=None, strict=True)
                parser.optionxform = str
                parser.read_string(before.decode('utf-8'))
                parser['Desktop Entry']['Hidden'] = 'false' if enabled else 'true'
                parser['Desktop Entry']['X-GNOME-Autostart-enabled'] = 'true' if enabled else 'false'
                from io import StringIO
                stream = StringIO()
                parser.write(stream, space_around_delimiters=False)
                planned = stream.getvalue().encode('utf-8')
            # The complete recovery receipt must exist before changing any
            # startup file. If final journaling fails, the pending receipt still
            # contains the original bytes and the exact planned postcondition.
            change = {'Id': identifier, 'source': 'native-startup', 'Name': current['Name'],
                      'Path': str(path), 'AfterPath': str(destination), 'BeforeSHA256': current['SHA256'],
                      'AfterSHA256': hashlib.sha256(planned).hexdigest(), 'Backup': str(backup), 'Enabled': enabled,
                      'created_at': time.time(), 'restored': False, 'state': 'prepared'}
            history = self.history()
            history.append(change)
            atomic_json(self.history_path, history)
            if self._entry(path)['SHA256'] != current['SHA256']:
                raise ValueError('The startup identity changed while preparing recovery; no registration was changed.')
            if planned != before:
                temporary = path.with_name('.' + path.name + '.' + identifier + '.tmp')
                temporary.write_bytes(planned)
                os.chmod(temporary, stat.S_IMODE(path.stat().st_mode))
                os.replace(temporary, path)
            if destination != path:
                path.rename(destination)
            after = self._entry(destination)
            if after['Enabled'] != enabled:
                raise RuntimeError('The changed startup state could not be verified.')
            change['state'] = 'applied'
            atomic_json(self.history_path, history)
            return {'changed': True, 'verified': True, 'change': change, 'entry': after,
                    'note': 'Next-sign-in behavior was changed. The currently running application was not stopped or launched; use reviewed process controls separately.'}

    def restore(self, identifier):
        with self._lock:
            history = self.history()
            change = next((row for row in history if row.get('Id') == identifier and row.get('restored') is not True), None)
            if change is None:
                raise ValueError('Choose a current native startup recovery record.')
            current_path = None
            if change.get('state') == 'prepared':
                if Path(change['Path']).exists():
                    original = self._valid(change['Path'])
                    current_hash = hashlib.sha256(original.read_bytes()).hexdigest()
                else:
                    original, current_hash = None, None
                if original is not None and current_hash == change['BeforeSHA256']:
                    change.update(restored=True, restored_at=time.time(), state='unchanged')
                    atomic_json(self.history_path, history)
                    return {'restored': True, 'verified': True, 'note': 'The prepared startup change never altered the original registration.'}
                if original is not None and current_hash == change['AfterSHA256']:
                    current_path = original  # Planned bytes written; final rename did not happen.
            current_path = current_path or self._valid(change['AfterPath'])
            if hashlib.sha256(current_path.read_bytes()).hexdigest() != change['AfterSHA256']:
                raise ValueError('The startup file changed since optimization; recovery will not overwrite it.')
            current = self._entry(current_path)
            backup = ordinary_path(change['Backup'], root=self.data_dir / 'native-startup-backups', owner=True)
            before = backup.read_bytes()
            if hashlib.sha256(before).hexdigest() != change['BeforeSHA256']:
                raise ValueError('The startup backup integrity check failed.')
            target = Path(change['Path'])
            if target.parent != self.root:
                raise ValueError('The original startup destination is invalid.')
            source = current_path
            if target != source and target.exists():
                raise ValueError('The original startup path now exists; recovery will not overwrite it.')
            if source.read_bytes() != before:
                temporary = target.with_name('.' + target.name + '.restore-' + uuid.uuid4().hex)
                temporary.write_bytes(before)
                os.chmod(temporary, stat.S_IMODE(source.stat().st_mode))
                os.replace(temporary, source)
            if source != target:
                source.rename(target)
            verified = self._entry(target)['SHA256'] == change['BeforeSHA256']
            if not verified:
                raise RuntimeError('Native startup recovery could not be verified.')
            change['restored'] = True
            change['restored_at'] = time.time()
            atomic_json(self.history_path, history)
            return {'restored': True, 'verified': True, 'note': 'The original registration was restored. Its next sign-in launch behavior is restored; no application was launched automatically.'}
