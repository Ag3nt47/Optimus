"""Exact-offer app activation evidence; never closes an app or authorizes work.

WinGet can exit successfully while an MSIX registration waits for an application
to close. These records prevent identical downloads until that prerequisite
changes. Every result still needs a fresh installed-version query.
"""
from __future__ import annotations

import ctypes
from datetime import datetime
import hashlib
import json
import math
import os
from pathlib import Path
import re
import time
import uuid

import psutil

from .filesystem import _ordinary_data_path
from .speed_enhancements import _current_owner, _private_json
from .update_resume import boot_identity

_ID = re.compile(r'[0-9a-f]{32}\Z')
_PACKAGE = re.compile(r'([A-Za-z0-9][A-Za-z0-9.\-]{2,100})_([0-9]+(?:\.[0-9]+){3})_(?:x64|x86|arm64|neutral)_([^_\s]*)_([a-z0-9]{13})\Z')
_MAX_LOG = 4 * 1024**2
_CLI_NAME = re.compile(r'WinGet-(\d{4}-\d{2}-\d{2}-\d{2}-\d{2}-\d{2}\.\d{3})\.log\Z')


def _cli_identity(result, arguments, *, package, owner, prepared_at, now):
    """Only a completed, authenticated ConPTY target can select a native log."""
    from .winget_console import _digest
    identity = getattr(result, 'native_diagnostics', None)
    proof, code = getattr(result, 'console_receipt', None), getattr(result, 'returncode', None)
    if (not isinstance(arguments, list) or len(arguments) < 9
            or arguments[1:9] != ['upgrade', '--id', package['id'], '--exact',
                '--source', 'winget', '--version', package['available']]
            or getattr(result, 'args', None) != arguments
            or getattr(result, 'capture_complete', False) is not True
            or not isinstance(proof, dict) or type(proof.get('schema')) is not int or proof['schema'] != 1
            or proof.get('verified') is not True or proof.get('quiescent') is not True
            or proof.get('job_verified') is not True or proof.get('output_eof') is not True
            or any(type(proof.get(key)) is not int or proof[key] != expected for key, expected in (
                ('job_active_processes', 0), ('console_client_count', 1), ('supervisor_exit_code', 0)))
            or type(code) is not int or not -0x80000000 <= code <= 0xffffffff
            or type(proof.get('target_exit_code')) is not int or proof['target_exit_code'] != (code & 0xffffffff)
            or not isinstance(identity, dict) or type(identity.get('schema')) is not int
            or identity['schema'] != 1 or identity.get('verified') is not True
            or identity.get('owner') != owner or _current_owner() != owner
            or identity.get('argv_sha256') != _digest(arguments)
            or type(identity.get('target_pid')) is not int or not 0 < identity['target_pid'] <= 0xffffffff):
        raise ValueError('The completed native command identity is unavailable.')
    started, finished = identity.get('started_at'), identity.get('finished_at')
    if (any(type(value) not in (int, float) or not math.isfinite(value)
            for value in (started, finished, prepared_at, now))
            or not 0 < prepared_at <= started <= finished <= now + 1
            or started - prepared_at > 180 or finished - started > 86400 or now - finished > 300):
        raise ValueError('The native diagnostic command is outside its exact attempt.')
    return dict(identity)


def _parse_cli_diagnostic(raw, identity, arguments, *, filename_stamp):
    """Read error text only after exact native process/command/time binding.

    WinGet --log is for supported vendor installers, not its own FileLogger;
    MSIX errors can therefore exist only in the provider's default CLI log.
    No native success or installed-version claim is inferred from this text.
    """
    from .automatic_update_recovery import _STAMP, _epoch
    text = raw.decode('utf-8-sig', errors='strict').replace('\r\n', '\n')
    starts = re.findall(r'^(' + _STAMP + r') <I> \[CORE\] Process: winget\.exe\[([1-9]\d*)\],', text, re.M)
    commands = re.findall(r'^' + _STAMP + r' <I> \[CLI \] WinGet invoked with arguments: (.+)$', text, re.M)
    if (len(starts) != 1 or int(starts[0][1]) != identity['target_pid']
            or len(commands) != 1 or commands[0] != ' '.join("'" + value + "'" for value in arguments[1:])):
        raise ValueError('The native log does not identify this exact process and command.')
    started = _epoch(starts[0][0])
    stamps = [_epoch(value) for value in re.findall(r'^(' + _STAMP + r') ', text, re.M)]
    if (not identity['started_at'] - 1 <= started <= min(identity['started_at'] + 180, identity['finished_at'] + 1)
            or abs(filename_stamp - started) > 2
            or any(not started - 1 <= stamp <= identity['finished_at'] + 1 for stamp in stamps)):
        raise ValueError('The native log timestamps do not match this attempt.')
    return text


def target_installed(expected, actual, identity):
    from .automated_software import same_release_version, is_newer_version
    return (isinstance(actual, str) and (actual == expected or same_release_version(expected, actual)
        or is_newer_version(expected, actual, package_id=identity, source='winget')))


def _read(path, maximum):
    before = _ordinary_data_path(path, file=True)
    if before is None or before.st_size > maximum:
        raise ValueError('Bounded ordinary update evidence required.')
    with path.open('rb') as stream:
        opened = os.fstat(stream.fileno())
        raw = stream.read(maximum + 1)
    after = _ordinary_data_path(path, file=True)
    fields = lambda row: (row.st_dev, row.st_ino, row.st_size, row.st_mtime_ns)
    if (after is None or opened.st_nlink != 1 or len(raw) > maximum
            or fields(before) != fields(opened) or fields(before) != fields(after)):
        raise ValueError('Update evidence changed during inspection.')
    return raw


def diagnostic_blockers(text):
    """Return package identities, never arbitrary instructions, from our log."""
    result = set()
    for line in text.splitlines():
        if '0x80073d02' not in line.casefold():
            continue
        for value in line.split():
            match = _PACKAGE.fullmatch(value.rstrip(',.'))
            if match:
                result.add(match[1] + '_' + match[4])
    return sorted(result)[:20]


def _process_family(pid, *, strict=False):
    """Read a process package identity with QUERY_LIMITED_INFORMATION only.

    https://learn.microsoft.com/windows/win32/api/appmodel/nf-appmodel-getpackagefamilyname
    """
    def unavailable():
        if strict:
            raise OSError('A process package identity could not be queried.')
        return None
    if os.name != 'nt':
        return unavailable()
    from ctypes import wintypes
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.GetPackageFamilyName.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.UINT), wintypes.LPWSTR]
    kernel.GetPackageFamilyName.restype = wintypes.LONG
    handle = kernel.OpenProcess(0x1000, False, pid)
    if not handle:
        return unavailable()
    try:
        length = wintypes.UINT(0)
        status = kernel.GetPackageFamilyName(handle, ctypes.byref(length), None)
        if status == 15700:  # APPMODEL_ERROR_NO_PACKAGE is a known nonmatch.
            return None
        if status != 122 or not 1 < length.value <= 256:
            return unavailable()
        buffer = ctypes.create_unicode_buffer(length.value)
        status = kernel.GetPackageFamilyName(handle, ctypes.byref(length), buffer)
        if status == 15700:
            return None
        return buffer.value if status == 0 and buffer.value else unavailable()
    finally:
        kernel.CloseHandle(handle)


def blocker_inventory(families):
    """Distinguish a proven empty family census from inaccessible processes."""
    wanted, result, complete, count = set(families), [], True, 0
    unknown = []
    if not wanted:
        return {'complete': True, 'processes': result, 'process_count': 0, 'unknown_count': 0}
    try:
        for count, process in enumerate(psutil.process_iter(['pid', 'name', 'create_time'], ad_value=None), 1):
            if count > 10000:
                complete = False
                break
            name = str(process.info.get('name') or '')
            # The two reserved Windows kernel processes cannot host MSIX code.
            if (process.pid, name.casefold()) in ((0, 'system idle process'), (4, 'system')):
                continue
            try:
                identity = _process_family(process.pid, strict=True)
                created = process.info.get('create_time')
                if (type(created) not in (int, float) or not math.isfinite(created) or created <= 0
                        or psutil.Process(process.pid).create_time() != created):
                    complete = False
                    unknown.append({'pid': process.pid, 'name': name[:120]})
                    continue
                if identity in wanted:
                    if len(result) >= 256:
                        complete = False
                        break
                    result.append({'pid': process.pid, 'created_at': created, 'package_family': identity,
                                   'name': name[:120]})
            except psutil.NoSuchProcess:
                pass
            except (OSError, psutil.Error):
                try:
                    psutil.Process(process.pid).create_time()
                except psutil.NoSuchProcess:
                    continue
                except (OSError, psutil.Error):
                    pass
                complete = False
                unknown.append({'pid': process.pid, 'name': name[:120]})
    except (OSError, psutil.Error):
        complete = False
    return {'complete': complete, 'processes': result, 'process_count': count,
            'unknown_count': len(unknown), 'unknown_processes': unknown[:64]}


def blocker_processes(families):
    """Compatibility view: only positively identified exact family processes."""
    return blocker_inventory(families)['processes']


class AppUpdateVerification:
    def __init__(self, data_dir, package, *, owner=None, boot=None, now=None, directory_policy=None):
        self.data_dir, self.package = Path(data_dir), dict(package)
        self.base = self.data_dir / 'app-update-verification'
        self.scope = {'id': self.package['id'].casefold(), 'source': 'winget',
                      'from_version': self.package['version'], 'to_version': self.package['available']}
        scope = json.dumps(self.scope, sort_keys=True, separators=(',', ':')).encode('utf-8')
        self.directory = self.base / hashlib.sha256(scope).hexdigest()
        self.owner = owner or _current_owner()
        self.boot = boot_identity if boot is None else boot
        self.now = time.time if now is None else now
        if directory_policy is None:
            from .self_update_permissions import create_private_directory
            directory_policy = create_private_directory
        self.directory_policy = directory_policy
        self.folder = None
        self.prepared_at = None

    def _owner_anchor(self, directory=None):
        directory = self.directory if directory is None else directory
        anchor = json.loads(_read(directory / 'owner.json', 4096).decode('utf-8'))
        expected = {'schema': 1, 'kind': 'app-update-verification', 'owner': self.owner}
        if directory == self.directory:
            expected['offer'] = self.scope
        if anchor != expected:
            raise ValueError('The app activation evidence belongs to another owner.')

    def pending(self):
        """A record can only restrict retry; it never establishes installation."""
        _ordinary_data_path(self.base)
        if not self.base.exists():
            return None
        self._owner_anchor(self.base)
        _ordinary_data_path(self.directory)
        if not self.directory.exists():
            return None
        self._owner_anchor()
        entries = list(self.directory.iterdir())
        if len(entries) > 4096:
            raise ValueError('App verification history exceeds its bound.')
        candidates = []
        for folder in entries:
            if not _ID.fullmatch(folder.name):
                continue
            _ordinary_data_path(folder)
            path = folder / 'verification.json'
            if not path.exists():
                continue
            row = json.loads(_read(path, 65536).decode('utf-8'))
            if (isinstance(row, dict) and row.get('schema') == 1 and row.get('owner') == self.owner
                    and row.get('id') == self.package.get('id') and row.get('from_version') == self.package.get('version')
                    and row.get('to_version') == self.package.get('available') and row.get('status') in ('pending_activation', 'pending_app_close', 'pending_verification')
                    and type(row.get('recorded_at')) in (int, float) and math.isfinite(row['recorded_at'])):
                candidates.append(row)
        if not candidates:
            return None
        previous = max(candidates, key=lambda row: row['recorded_at'])
        boot = previous.get('boot_identity')
        if type(boot) is int and self.boot() > boot + 10:
            return None  # A changed OS boot permits one freshly validated attempt.
        processes = previous.get('blocking_processes')
        active = False
        recorded_exited = False
        if isinstance(processes, list) and processes:
            for row in processes[:256]:
                if not isinstance(row, dict) or type(row.get('pid')) is not int or type(row.get('created_at')) not in (int, float):
                    active = True
                    break
                try:
                    process = psutil.Process(row['pid'])
                    active = active or process.create_time() == row['created_at']
                except psutil.NoSuchProcess:
                    pass
                except (OSError, psutil.Error):
                    active = True
            recorded_exited = not active
        families = previous.get('blocking_package_families')
        if (not active and isinstance(families, list) and 0 < len(families) <= 20
                and all(isinstance(value, str) and value for value in families)):
            # A blocker can close before finish() captures its PID; another
            # process of that same family can also replace a recorded PID.
            # A proven recorded prerequisite change permits one revalidated
            # retry even if unrelated protected processes cannot be queried.
            # An initially empty snapshot needs a complete fresh empty census.
            current = blocker_inventory(families)
            if current.get('processes') == [] and (current.get('complete') is True or recorded_exited):
                return None
        return previous

    def prepare(self):
        for directory in (self.base, self.directory):
            _ordinary_data_path(directory)
            try:
                self.directory_policy(directory, self.owner)
            except FileExistsError:
                _ordinary_data_path(directory)
                self._owner_anchor(directory)
            else:
                anchor = {'schema': 1, 'kind': 'app-update-verification', 'owner': self.owner}
                if directory == self.directory:
                    anchor['offer'] = self.scope
                _private_json(directory / 'owner.json', anchor)
        self.folder = self.directory / uuid.uuid4().hex
        self.directory_policy(self.folder, self.owner)
        self.prepared_at = self.now()
        return self.folder / 'winget.log'

    def capture_cli_diagnostic(self, result, arguments):
        """Copy only the one fresh matching CLI log from the known provider.

        Never choose the newest log, search arbitrary directories, follow a
        linked path, or interpret an unmatched file as application evidence.
        """
        from .automatic_update_recovery import _log_folder
        identity = _cli_identity(result, arguments, package=self.package, owner=self.owner,
            prepared_at=self.prepared_at, now=self.now())
        folder = _log_folder()
        _ordinary_data_path(folder)
        proven, candidates = [], 0
        for count, path in enumerate(folder.iterdir(), 1):
            if count > 4096:
                raise ValueError('The provider diagnostic inventory exceeds its bound.')
            match = _CLI_NAME.fullmatch(path.name)
            if match is None:
                continue
            stamp = datetime.strptime(match[1], '%Y-%m-%d-%H-%M-%S.%f').astimezone().timestamp()
            if not identity['started_at'] - 1 <= stamp <= min(identity['started_at'] + 180, identity['finished_at'] + 1):
                continue
            candidates += 1
            if candidates > 16:
                raise ValueError('The fresh provider diagnostic inventory is ambiguous.')
            try:
                raw = _read(path, _MAX_LOG)
                text = _parse_cli_diagnostic(raw, identity, arguments, filename_stamp=stamp)
            except (OSError, ValueError, UnicodeError):
                continue
            proven.append((path.name, raw, text))
        if len(proven) != 1:
            raise ValueError('No unique exact native diagnostic log was found.')
        name, raw, text = proven[0]
        destination = self.folder / 'winget-cli.log'
        _ordinary_data_path(destination, file=True)
        with destination.open('xb') as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        digest = hashlib.sha256(raw).hexdigest()
        if hashlib.sha256(_read(destination, _MAX_LOG)).hexdigest() != digest:
            raise ValueError('The private native diagnostic copy could not be verified.')
        return text, dict(identity, status='captured', filename=name, sha256=digest,
                          private_copy=destination.name)

    def finish(self, receipt, *, native_result=None, arguments=None):
        if self.folder is None:
            return
        text = ''
        try:
            text = _read(self.folder / 'winget.log', _MAX_LOG).decode('utf-8-sig')
        except (OSError, ValueError, UnicodeError):
            pass
        diagnostic = None
        if (native_result is not None and self.package['id'] != 'Ollama.Ollama'
                and receipt.get('verified') is False and receipt.get('outcomeKnown') is True
                and receipt.get('running') is False):
            try:
                cli_text, diagnostic = self.capture_cli_diagnostic(native_result, arguments)
                text += '\n' + cli_text
            except (OSError, ValueError, UnicodeError):
                diagnostic = {'status': 'unavailable'}
        families = diagnostic_blockers(text)
        pending = (receipt.get('verified') is False and receipt.get('outcomeKnown') is True
            and receipt.get('running') is False and (receipt.get('installation_reported') is True or bool(families)))
        restart_app = receipt.get('application_restart_required') is True or bool(families)
        pending_status = (('pending_activation' if receipt.get('installation_reported') is True else 'pending_app_close')
                          if restart_app else 'pending_verification')
        inventory = blocker_inventory(families)
        row = {'schema': 1, 'owner': self.owner, 'id': self.package['id'],
            'from_version': self.package['version'], 'to_version': self.package['available'],
            'installed_version': receipt.get('installed_version'), 'recorded_at': self.now(),
            'status': pending_status if pending else 'verified' if receipt.get('verified') is True else 'not_verified',
            'application_restart_required': restart_app,
            'boot_identity': self.boot(), 'blocking_package_families': families,
            'blocking_processes': inventory['processes'],
            'blocking_process_inventory_complete': inventory.get('complete') is True}
        if diagnostic is not None:
            row['cli_diagnostic'] = diagnostic
        _private_json(self.folder / 'verification.json', row)
        receipt['verification_attempt'] = self.folder.name
        if pending:
            receipt.update(verification_status=pending_status,
                blocking_package_families=families, blocking_processes=row['blocking_processes'])
            if families:
                receipt['application_restart_required'] = True
            if pending_status == 'pending_app_close':
                receipt['failure_scope'] = 'package'
                receipt['requiresReview'] = True
            detail = (' Close ' + ', '.join(families) + ' after saving work.' if families else
                      ' Recheck after the application finishes its own updater or Windows restarts.')
            receipt['note'] += (' The failed deployment needs application closure before a freshly validated retry.'
                if pending_status == 'pending_app_close' else ' The installed version is pending verification.')
            receipt['note'] += detail + ' Optimus will recheck automatically; this package is not counted as installed.'


def deferred_receipt(package, pending, installed_version):
    families = pending.get('blocking_package_families', [])
    detail = ', '.join(value for value in families if isinstance(value, str))[:500] or package['id']
    restart_app = pending.get('application_restart_required') is True
    return {'id': package['id'], 'package_id': package['id'], 'source': 'winget',
        'status': 'deferred', 'skipped': True, 'exitCode': None,
        'installer_started': False, 'outcomeKnown': True, 'running': False,
        'requiresReview': True, 'failure_scope': 'package', 'failureKind': 'app_activation_pending',
        'verified': False, 'installed_version': installed_version,
        'from_version': package['version'], 'to_version': package['available'],
        'application_restart_required': restart_app, 'verification_status': pending.get('status', 'pending_verification'),
        'blocking_package_families': families,
        'note': ('The earlier update requires application closure before activation or a retry: ' if restart_app else
                 'The earlier installer returned without a confirmed version change: ') + detail +
            '. The installed version is still ' + str(installed_version or 'unverified') +
            '. This unchanged offer was not downloaded again. Other approved applications continue; retry is revalidated after its exact blocking process exits or Windows restarts.'}
