"""Explicit, user-owned Windows sign-in launch; never grants administrator access."""
from __future__ import annotations

import json
from contextlib import contextmanager
import os
from pathlib import Path
import re
import subprocess
import sys
import threading
import uuid

from .filesystem import _ordinary_data_path
from .workflow import atomic_json


class StartupRegistrationError(RuntimeError):
    pass


def launch_specification():
    """Register only the original installed launcher, never editable session args."""
    executable = Path(os.path.abspath(sys.executable))
    if getattr(sys, 'frozen', False):
        if executable.name.casefold() != 'optimus.exe':
            raise ValueError('The installed Optimus executable could not be identified.')
        arguments = []
        directory = executable.parent
    else:
        if not re.fullmatch(r'python(?:w|\d+(?:\.\d+)*)?\.exe', executable.name, re.I):
            raise ValueError('Sign-in launch requires an ordinary installed Python executable.')
        directory = Path(os.path.abspath(os.environ.get('OPTIMUS_ORIGINAL_SOURCE') or Path(__file__).parents[2]))
        launcher = directory / 'launch.py'
        if _ordinary_data_path(launcher, file=True) is None:
            raise ValueError('The original Optimus source launcher is unavailable.')
        arguments = ['-B', str(launcher)]
    if _ordinary_data_path(executable, file=True) is None:
        raise ValueError('The original Optimus executable is unavailable.')
    for path in (executable, directory):
        if any(part.casefold() in ('windowsapps', 'packages') for part in path.parts):
            raise ValueError('Use an ordinary installed launcher outside packaged application storage.')
    return {'executable': str(executable), 'arguments': arguments,
            'argument_line': subprocess.list2cmdline(arguments), 'working_directory': str(directory)}


class StartupRegistration:
    def __init__(self, data_dir, *, runner=None, launch_provider=None):
        self.data_dir = Path(data_dir)
        self.path = self.data_dir / 'startup-launch.json'
        self.runner = runner or self._native
        self.launch_provider = launch_provider or launch_specification
        self.lock = threading.RLock()

    def _native(self, request):
        executable = Path(os.environ.get('SystemRoot', r'C:\Windows')) / 'System32/WindowsPowerShell/v1.0/powershell.exe'
        helper = Path(__file__).with_name('engine') / 'OptimusStartup.ps1'
        environment = {k: v for k, v in os.environ.items() if k.casefold() != 'psmodulepath'}
        environment['PSModulePath'] = str(executable.parent / 'Modules')
        try:
            result = subprocess.run([str(executable), '-NoLogo', '-NoProfile', '-NonInteractive',
                '-ExecutionPolicy', 'RemoteSigned', '-File', str(helper)],
                input=json.dumps(request, separators=(',', ':')), capture_output=True,
                text=True, encoding='utf-8', errors='replace', timeout=10, env=environment,
                creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0), check=False)
        except subprocess.TimeoutExpired as error:
            raise StartupRegistrationError('The Windows sign-in task request timed out. Its outcome is unknown; check sign-in launch status before retrying.') from error
        if len(result.stdout) > 131072:
            raise StartupRegistrationError('Windows sign-in registration could not be checked or changed.')
        try:
            value = json.loads(result.stdout.lstrip('\ufeff'))
        except ValueError as error:
            raise StartupRegistrationError('Windows returned an unreadable sign-in task result.') from error
        if not isinstance(value, dict) or value.get('ok') is not True:
            raise StartupRegistrationError(str(value.get('error', 'Windows sign-in task request failed.')) if isinstance(value, dict) else 'Invalid sign-in task result.')
        if result.returncode:
            raise StartupRegistrationError('Windows sign-in task request did not complete successfully.')
        return value

    def _manifest(self):
        info = _ordinary_data_path(self.path, file=True)
        if info is None:
            return None
        if info.st_size > 16384:
            raise ValueError('The sign-in ownership record exceeds its size limit.')
        with self.path.open('rb') as stream:
            raw = stream.read(16385)
        if len(raw) > 16384:
            raise ValueError('The sign-in ownership record exceeds its size limit.')
        value = json.loads(raw.decode('utf-8'))
        if (not isinstance(value, dict) or set(value) != {'schema', 'owner_token', 'owner_sid', 'launch', 'previous_launch'}
                or type(value['schema']) is not int or value['schema'] != 1
                or not isinstance(value['owner_token'], str) or not re.fullmatch('[0-9a-f]{32}', value['owner_token'])
                or not isinstance(value['owner_sid'], str) or not re.fullmatch(r'S-1-\d+(?:-\d+){1,15}', value['owner_sid'])):
            raise ValueError('The sign-in ownership record is invalid.')
        for launch in (value['launch'], value['previous_launch']):
            if launch is None:
                continue
            if (not isinstance(launch, dict) or set(launch) != {'executable', 'arguments', 'argument_line', 'working_directory'}
                    or not isinstance(launch['arguments'], list) or len(launch['arguments']) > 2
                    or any(not isinstance(v, str) or len(v) > 2048 or '\x00' in v for v in launch['arguments'])
                    or any(not isinstance(launch[k], str) or len(launch[k]) > 4096 or '\x00' in launch[k]
                           for k in ('executable', 'argument_line', 'working_directory'))
                    or subprocess.list2cmdline(launch['arguments']) != launch['argument_line']):
                raise ValueError('The sign-in launcher ownership record is invalid.')
        if value['launch'] is None:
            raise ValueError('The sign-in launcher ownership record is missing.')
        return value

    @contextmanager
    def _mutation(self):
        with self.lock:
            self.data_dir.mkdir(parents=True, exist_ok=True)
            path = self.data_dir / 'startup-launch.lock'
            _ordinary_data_path(path, file=True)
            with path.open('a+b') as stream:
                stream.seek(0, 2)
                if stream.tell() == 0:
                    stream.write(b'0')
                    stream.flush()
                stream.seek(0)
                if os.name == 'nt':
                    import msvcrt
                    try:
                        msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
                    except OSError as error:
                        raise StartupRegistrationError('Another Optimus window is changing sign-in launch. Retry after it finishes.') from error
                try:
                    yield
                finally:
                    if os.name == 'nt':
                        stream.seek(0)
                        msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)

    def status(self):
        with self.lock:
            try:
                value = self.runner({'operation': 'status', 'manifest': self._manifest()})
                status = value.get('status')
                if (not isinstance(status, dict) or type(status.get('owned')) is not bool
                        or type(status.get('available')) is not bool
                        or (status.get('enabled') is not None and type(status.get('enabled')) is not bool)
                        or not isinstance(status.get('state'), str)):
                    raise ValueError('Windows returned invalid sign-in task status.')
                return status
            except (OSError, ValueError, StartupRegistrationError) as error:
                return {'enabled': None, 'owned': False, 'available': False, 'state': 'unavailable',
                        'reason': str(error), 'task_name': None, 'target': None}

    def set_enabled(self, enabled):
        if type(enabled) is not bool:
            raise ValueError('Sign-in launch must be on or off.')
        with self._mutation():
            before = self._manifest()
            inspected = self.runner({'operation': 'status', 'manifest': before})
            status = inspected['status']
            if status.get('available') is not True or status.get('state') == 'foreign':
                raise StartupRegistrationError(status.get('reason') or 'An unrelated task uses the Optimus sign-in name; it was not changed.')
            manifest = before
            if enabled:
                manifest = {'schema': 1, 'owner_token': before['owner_token'] if before else uuid.uuid4().hex,
                            'owner_sid': inspected['owner_sid'], 'launch': self.launch_provider(),
                            'previous_launch': inspected.get('matched_launch')}
                self.data_dir.mkdir(parents=True, exist_ok=True)
                # Persist both old/new ownership before touching Task Scheduler.
                # A timeout/crash never loses the ability to identify either one.
                atomic_json(self.path, manifest)
            result = self.runner({'operation': 'set', 'enabled': enabled, 'manifest': manifest})
            result['_before_manifest'] = before
            result['_after_manifest'] = manifest
            result['_after_status'] = dict(result['status'])
            return result

    def rollback(self, receipt):
        with self._mutation():
            if self._manifest() != receipt['_after_manifest']:
                raise StartupRegistrationError('Sign-in ownership changed before rollback; the new state was preserved.')
            current = self.runner({'operation': 'status', 'manifest': receipt['_after_manifest']})['status']
            if any(current.get(key) != receipt['_after_status'].get(key) for key in ('enabled', 'owned', 'state', 'target')):
                raise StartupRegistrationError('The sign-in task changed before rollback; the new state was preserved.')
            self.runner({'operation': 'restore', 'manifest': receipt['_after_manifest'],
                         'before_manifest': receipt['_before_manifest'], 'before_xml': receipt.get('before_xml')})
            if receipt['_before_manifest'] is None:
                if self.path.exists():
                    _ordinary_data_path(self.path, file=True)
                    self.path.unlink()
            else:
                atomic_json(self.path, receipt['_before_manifest'])
