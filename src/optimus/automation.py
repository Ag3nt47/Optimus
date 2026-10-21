"""App-open maintenance cadence, setup transactions, and OS-owned run leases."""
from __future__ import annotations
import copy
import errno
import json
import math
import os
from pathlib import Path
import stat
import threading
import time
import uuid
from .filesystem import replace_with_retry

CURRENT_SETUP_VERSION = 2
SESSION_ID = uuid.uuid4().hex
MODES = frozenset(('manual', 'startup', 'daily', 'weekly'))
_STATE_LOCK = threading.RLock()


def _timestamp(value=None):
    value = time.time() if value is None else value
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
        raise ValueError('A finite nonnegative epoch timestamp is required.')
    return float(value)


def _run_identity(value):
    return isinstance(value, str) and len(value) == 32 and all(c in '0123456789abcdef' for c in value)


def _valid_record(value):
    if not isinstance(value, dict) or type(value.get('schema')) is not int or value.get('schema') != 1 or value.get('mode') not in MODES:
        raise ValueError('Unsupported automation state.')
    status = value.get('status')
    if status not in ('configured', 'started', 'completed', 'failed', 'cancelled') or not _run_identity(value.get('session_id')):
        raise ValueError('Invalid automation status or session identity.')
    if value.get('attempted_at') is None:
        raise ValueError('Missing automation attempt timestamp.')
    attempted = _timestamp(value['attempted_at'])
    if status == 'configured':
        if value.get('run_id') is not None or 'finished_at' in value:
            raise ValueError('A cadence baseline cannot represent an executed operation.')
    elif not _run_identity(value.get('run_id')):
        raise ValueError('Missing maintenance run identity.')
    if status in ('completed', 'failed', 'cancelled'):
        if value.get('finished_at') is None or _timestamp(value['finished_at']) < attempted:
            raise ValueError('Invalid maintenance completion timestamp.')
    elif status == 'started' and 'finished_at' in value:
        raise ValueError('An in-progress attempt cannot contain a completion timestamp.')
    return value


def load_attempt(data_dir):
    """Return saved state, None if absent, or an invalid fail-closed sentinel."""
    with _STATE_LOCK:
        try:
            return _valid_record(json.loads((Path(data_dir) / 'automation-state.json').read_text(encoding='utf-8')))
        except FileNotFoundError:
            return None
        except (OSError, ValueError, TypeError):
            return {'schema': 1, 'status': 'invalid', 'error': 'Automation state could not be verified; automatic maintenance is paused.'}


def due(settings, last_attempt, now=None):
    """A failed/manual attempt counts toward cadence; nothing runs while closed."""
    from .workflow import Preferences
    try:
        config = Preferences.validate(settings)
        current = _timestamp(now)
        if config['setup_version'] < CURRENT_SETUP_VERSION or config['automation_mode'] == 'manual':
            return False
        if last_attempt is None:
            return True
        saved = _valid_record(last_attempt)
        if config['automation_mode'] == 'startup':
            return saved['session_id'] != SESSION_ID
        interval = 86400 if config['automation_mode'] == 'daily' else 7 * 86400
        return current - float(saved['attempted_at']) >= interval
    except (ValueError, TypeError, KeyError):
        return False


def _persist_attempt(data_dir, config, status, current, error, run_id):
    from .workflow import atomic_json
    with _STATE_LOCK:
        if status in ('started', 'configured'):
            if status == 'configured' and run_id is not None:
                raise ValueError('A cadence baseline cannot have an executed run identity.')
            value = {'schema': 1, 'attempted_at': current, 'session_id': SESSION_ID,
                     'mode': config['automation_mode'], 'status': status,
                     'run_id': run_id or uuid.uuid4().hex if status == 'started' else None}
            if status == 'configured':
                value['note'] = 'Cadence configured only; no maintenance operation was run.'
        else:
            value = load_attempt(data_dir)
            _valid_record(value)
            if run_id is None or value.get('status') == 'configured' or value.get('run_id') != run_id:
                raise ValueError('The saved maintenance attempt belongs to another run.')
            value = dict(value)
            value.update({'status': status, 'finished_at': current})
        if error is not None:
            value['error'] = str(error)[:4000]
        _valid_record(value)
        atomic_json(Path(data_dir) / 'automation-state.json', value)
        return dict(value)


def record_attempt(data_dir, settings, status='started', now=None, error=None, run_id=None):
    """Record a configured baseline or update a Workflow-owned attempt.

    Workflow holds its RunLease for started/terminal updates. Configured writes
    acquire their own lease so settings cannot clobber another process's run.
    """
    from .workflow import Preferences
    config = Preferences.validate(settings)
    current = _timestamp(now)
    if status not in ('configured', 'started', 'completed', 'failed', 'cancelled'):
        raise ValueError('Unsupported automation attempt status.')
    if run_id is not None and not _run_identity(run_id):
        raise ValueError('Invalid maintenance run identity.')
    if status == 'configured':
        with RunLease(data_dir):
            return _persist_attempt(data_dir, config, status, current, error, run_id)
    return _persist_attempt(data_dir, config, status, current, error, run_id)


def _restore_state_bytes(path, original):
    if original is None:
        path.unlink(missing_ok=True)
        return
    temporary = path.with_name(path.name + '.' + uuid.uuid4().hex + '.rollback')
    with temporary.open('wb') as stream:
        stream.write(original)
        stream.flush()
        os.fsync(stream.fileno())
    replace_with_retry(temporary, path)


def save_setup(data_dir, settings, save_preferences, *, run_now=False):
    """Lease baseline + preference save, restoring exact prior state on failure.

    Return saved validated settings. run_now is the UI's explicit intent only;
    this helper never executes maintenance or enables the resource sampler.
    """
    from .workflow import Preferences
    config = Preferences.validate(settings)
    if config['setup_version'] != CURRENT_SETUP_VERSION or type(run_now) is not bool or not callable(save_preferences):
        raise ValueError('Complete the current setup and provide a valid save action.')
    with RunLease(data_dir):
        state_path = Path(data_dir) / 'automation-state.json'
        original = state_path.read_bytes() if state_path.exists() else None
        try:
            _persist_attempt(data_dir, config, 'configured', _timestamp(), None, None)
            saved = save_preferences(copy.deepcopy(config))
            return Preferences.validate(saved) if isinstance(saved, dict) else copy.deepcopy(config)
        except Exception:
            _restore_state_bytes(state_path, original)
            raise


def cadence_note(mode):
    if mode not in MODES:
        raise ValueError('Unknown automation mode.')
    return {'manual': 'Maintenance runs only when you start it.',
            'startup': 'Maintenance runs once per Optimus app session while Optimus is open.',
            'daily': 'Maintenance runs when one day has elapsed since the last attempt, only while Optimus is open.',
            'weekly': 'Maintenance runs when seven days have elapsed since the last attempt, only while Optimus is open.'}[mode]

class RunLease:
    """A nonblocking OS file lock; process exit releases it without stale PID files."""
    def __init__(self, data_dir):
        self.path = Path(data_dir) / 'maintenance-run.lock'
        self.handle = None

    def acquire(self):
        if self.handle is not None:
            raise RuntimeError('This maintenance lease is already held.')
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if self.path.exists():
            info = self.path.lstat()
            if stat.S_ISLNK(info.st_mode) or getattr(info, 'st_file_attributes', 0) & 0x400 or info.st_nlink > 1:
                raise OSError('The maintenance lock file is not a regular private state file.')
        handle = self.path.open('a+b')
        try:
            info = os.fstat(handle.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_nlink > 1:
                raise OSError('Invalid maintenance lock file.')
            handle.seek(0)
            if os.name == 'nt':
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as error:
            handle.close()
            if error.errno in (errno.EACCES, errno.EAGAIN, errno.EDEADLK):
                raise RuntimeError('Another Optimus process is already running maintenance.') from error
            raise
        self.handle = handle
        return self

    def release(self):
        handle, self.handle = self.handle, None
        if handle is not None:
            try:
                handle.seek(0)
                if os.name == 'nt':
                    import msvcrt
                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            finally:
                handle.close()

    def __enter__(self):
        return self.acquire()

    def __exit__(self, *_):
        self.release()
