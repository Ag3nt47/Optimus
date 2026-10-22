"""Owned, per-user XDG autostart / launchd registration; never elevates the GUI."""
from __future__ import annotations

from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import plistlib
import stat
import sys
import threading
import uuid

from .filesystem import _ordinary_data_path, replace_with_retry
from .workflow import atomic_json


def launch_specification():
    executable = Path(sys.executable).resolve(strict=True)
    if not executable.is_file():
        raise ValueError('The installed native launcher is unavailable.')
    if getattr(sys, 'frozen', False):
        if executable.name != 'Optimus':
            raise ValueError('The native Optimus executable could not be identified.')
        return [str(executable)]
    source = Path(os.environ.get('OPTIMUS_ORIGINAL_SOURCE') or Path(__file__).parents[2]).resolve(strict=True)
    launcher = source / 'launch.py'
    if _ordinary_data_path(launcher, file=True) is None:
        raise ValueError('The original native source launcher is unavailable.')
    return [str(executable), '-B', str(launcher)]


class NativeStartupRegistration:
    def __init__(self, data_dir, *, platform=None, home=None, launch_provider=None):
        self.data_dir = Path(data_dir)
        self.platform = platform or sys.platform
        self.home = Path(home) if home is not None else Path.home()
        self.launch_provider = launch_provider or launch_specification
        self.lock = threading.RLock()
        if self.platform == 'darwin':
            self.target = self.home / 'Library/LaunchAgents/io.github.ag3nt47.optimus.plist'
        elif self.platform == 'linux':
            configured = os.environ.get('XDG_CONFIG_HOME', '') if home is None else ''
            config = Path(configured) if configured and Path(configured).is_absolute() else self.home / '.config'
            self.target = config / 'autostart/io.github.ag3nt47.optimus.desktop'
        else:
            raise ValueError('Native sign-in launch requires macOS or Linux.')
        self.manifest = self.data_dir / 'native-startup-launch.json'

    def _bytes(self, path, maximum=65536):
        info = _ordinary_data_path(path, file=True)
        if info is None:
            return None
        if info.st_size > maximum or hasattr(os, 'getuid') and info.st_uid != os.getuid():
            raise ValueError('The sign-in file is oversized or belongs to another account.')
        with path.open('rb') as stream:
            opened = os.fstat(stream.fileno())
            raw = stream.read(maximum + 1)
        after = path.lstat()
        if (len(raw) > maximum or (opened.st_dev, opened.st_ino) != (info.st_dev, info.st_ino)
                or (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
                != (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns)):
            raise ValueError('The sign-in file changed during inspection.')
        return raw

    def _owned(self):
        raw = self._bytes(self.manifest)
        if raw is None:
            return None
        result = json.loads(raw.decode('utf-8'))
        if (not isinstance(result, dict) or result.get('schema') != 1
                or result.get('target') != str(self.target)
                or not isinstance(result.get('sha256'), str) or len(result['sha256']) != 64):
            raise ValueError('The native sign-in ownership record is invalid.')
        return result

    def status(self):
        with self.lock:
            try:
                current = self._bytes(self.target)
                owned = self._owned()
                if current is None:
                    state, enabled = 'absent', False
                elif owned is None or hashlib.sha256(current).hexdigest() != owned['sha256']:
                    state, enabled = 'foreign', None
                else:
                    state, enabled = 'enabled', True
                return {'available': True, 'state': state, 'enabled': enabled,
                        'owned': owned is not None and state != 'foreign', 'target': str(self.target),
                        'reason': 'Applies at the next desktop sign-in. Desktop/session policy can override automatic launch.'}
            except (OSError, ValueError, TypeError) as error:
                return {'available': False, 'state': 'unavailable', 'enabled': None,
                        'owned': False, 'target': str(self.target), 'reason': str(error)}

    @contextmanager
    def _mutation(self):
        with self.lock:
            self.data_dir.mkdir(parents=True, exist_ok=True)
            path = self.data_dir / 'native-signin.lock'
            _ordinary_data_path(path, file=True)
            with path.open('a+b') as stream:
                info = os.fstat(stream.fileno())
                if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                    raise ValueError('The native sign-in lock is not an ordinary file.')
                if os.name == 'posix':
                    import fcntl
                    fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
                try:
                    yield
                finally:
                    if os.name == 'posix':
                        fcntl.flock(stream, fcntl.LOCK_UN)

    def _content(self):
        command = self.launch_provider()
        if (not isinstance(command, list) or not command or len(command) > 3
                or any(not isinstance(item, str) or not item or any(char in item for char in '\x00\r\n') for item in command)):
            raise ValueError('The native launch specification is invalid.')
        if self.platform == 'darwin':
            return plistlib.dumps({'Label': 'io.github.ag3nt47.optimus', 'ProgramArguments': command,
                                   'RunAtLoad': True, 'KeepAlive': False, 'ProcessType': 'Interactive'})
        # Desktop Entry Exec is not a shell command. Quote each argument and
        # escape percent field codes so filenames cannot become substitutions.
        def quote(value):
            value = value.replace('\\', '\\\\').replace('"', '\\"').replace('`', '\\`').replace('$', '\\$').replace('%', '%%')
            return '"' + value + '"'
        return ('[Desktop Entry]\nType=Application\nName=Optimus\nComment=Computer optimization tool\n'
                + 'Exec=' + ' '.join(quote(item) for item in command)
                + '\nTerminal=false\nX-GNOME-Autostart-enabled=true\nX-GNOME-Autostart-Delay=60\n').encode('utf-8')

    def _write(self, path, content):
        _ordinary_data_path(path, file=True)
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(path.name + '.' + uuid.uuid4().hex + '.tmp')
        try:
            with temporary.open('xb') as stream:
                if os.name == 'posix':
                    os.fchmod(stream.fileno(), 0o600)
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
            replace_with_retry(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)

    def set_enabled(self, enabled):
        if type(enabled) is not bool:
            raise ValueError('Sign-in launch must be on or off.')
        with self._mutation():
            state = self.status()
            if not state['available'] or state['state'] == 'foreign':
                raise ValueError('The sign-in entry is unrecognized or unavailable; it was preserved.')
            before, manifest_before = self._bytes(self.target), self._bytes(self.manifest)
            after = self._content() if enabled else None
            try:
                if enabled:
                    manifest = {'schema': 1, 'target': str(self.target), 'sha256': hashlib.sha256(after).hexdigest()}
                    # Ownership is persisted before registration. Recovery can
                    # recognize the intended bytes after an interrupted write.
                    atomic_json(self.manifest, manifest)
                    self._write(self.target, after)
                elif before is not None:
                    self.target.unlink()
            except Exception:
                if before is not None:
                    self._write(self.target, before)
                if manifest_before is not None:
                    self._write(self.manifest, manifest_before)
                else:
                    self.manifest.unlink(missing_ok=True)
                raise
            return {'before': before, 'manifest_before': manifest_before, 'after': after,
                    'manifest_after': self._bytes(self.manifest), 'status': self.status()}

    def rollback(self, receipt):
        with self._mutation():
            if self._bytes(self.target) != receipt['after'] or self._bytes(self.manifest) != receipt['manifest_after']:
                raise ValueError('The native sign-in entry changed; rollback preserved its current state.')
            if receipt['before'] is None:
                self.target.unlink(missing_ok=True)
            else:
                self._write(self.target, receipt['before'])
            if receipt['manifest_before'] is None:
                self.manifest.unlink(missing_ok=True)
            else:
                self._write(self.manifest, receipt['manifest_before'])
