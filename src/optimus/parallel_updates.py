"""One isolated application-updater worker, joined before other installers.

The workflow owns the global maintenance lease for this worker's entire life.
Only independent foreground stages overlap; no installer is killed on cancel.
"""
from __future__ import annotations

import copy
import json
from pathlib import Path
import re
import threading
import time
import uuid

from .filesystem import _ordinary_data_path

WINDOWS_SOFTWARE = 'Review or update configured software'
NATIVE_SOFTWARE = 'Review or install approved native package updates'
WAITING_FOR_UPDATES = 'Waiting for background application updates'
BEFORE_UPDATES = frozenset({
    'Clean temporary files', 'Clean selected additional categories', 'Clean selected browser data',
    'Clean approved native temporary files and caches', 'Clean selected browser data after close or skip choice',
})
INDEPENDENT = frozenset({
    'Review installed applications', 'Identify programs contributing to slowdowns',
    'Apply supported speed enhancements', 'Prepare approved Windows CPU upgrade workaround',
    'Apply Codex performance profile', 'Enable resource analysis', 'Apply saved privacy controls',
    'Review installed native applications', 'Measure applications contributing to pressure',
    'Review native kernel and hardware-support inventory', 'Review native security observations',
    'Review vendor support and patch limitations', 'Start requested read-only background analysis',
    'Review startup registrations', 'Review duplicate configuration', 'Review connected cloud drive',
    'Review available driver updates', 'Review native kernel and hardware-support coverage',
    'Review native startup registrations', 'Review native duplicate copies',
})
INSTALLER_STAGES = frozenset({
    'Install approved driver updates', 'Check or install Windows security updates',
    'Optimize eligible internal drives', 'Remove selected consumer app group',
    'Remove Microsoft 365 consumer packages', 'Remove new Outlook package',
    'Uninstall Microsoft Edge using the supported installer',
})
BLOCKED_STAGES = INSTALLER_STAGES | frozenset({
    WINDOWS_SOFTWARE, NATIVE_SOFTWARE, 'Clean temporary files', 'Clean selected additional categories',
    'Clean selected browser data', 'Recycle approved local duplicates', 'Recycle approved cloud duplicates',
    'Disable approved startup entries', 'Scan for spyware and malware',
    'Review or disable approved user startup entries', 'Find or recycle approved duplicate copies',
    'Clean approved native temporary files and caches', 'Clean selected browser data after close or skip choice',
})


def independent_stage(title):
    return title in INDEPENDENT or title.startswith('Review duplicates: ')


def isolated_backend(backend):
    """Copy known mutable updater caches without discovering host adapters."""
    result = copy.copy(backend)
    from .maintenance import Maintenance
    from .native_operations import NativePackages
    service = getattr(backend, 'maintenance', None)
    if type(service) is Maintenance:
        result.maintenance = copy.copy(service)
        result.maintenance._last_update_ids = set()
        result.maintenance._last_update_packages = {}
        result.maintenance._last_update_scan = 0.0
    service = getattr(backend, 'native_packages', None)
    if type(service) is NativePackages:
        result.native_packages = copy.copy(service)
        result.native_packages._last_scan = None
        # Preserve the manager's serialization lock, while isolating its cache.
        # Independent inventory never starts another package installer.
    return result


class ProgressMux:
    """Keep foreground stage/progress stable while background item text changes."""
    def __init__(self, callback):
        self.callback = callback
        self.lock = threading.RLock()
        self.foreground = {}
        self.job = None
        self.item_started = None

    def _event(self):
        event = dict(self.foreground)
        if self.job is not None:
            event['jobs'] = [dict(self.job)]
            if self.item_started is not None and self.job['status'] in ('pending', 'running', 'waiting'):
                event['jobs'][0]['elapsed_seconds'] = max(0, time.monotonic() - self.item_started)
            processes = list(event.get('processes', []))
            if self.job['status'] in ('pending', 'running', 'waiting'):
                processes.append(self.job['description'])
                event['percentage'] = min(99, event.get('percentage', 0))
            event['processes'] = list(dict.fromkeys(processes))
        return event

    def __call__(self, event):
        with self.lock:
            self.foreground = dict(event)
            self.callback(self._event())

    def background(self, status, state=None):
        state = state if isinstance(state, dict) else {}
        with self.lock:
            previous = self.job or {}
            package = state.get('id', previous.get('current_package'))
            if self.item_started is None or package != previous.get('current_package'):
                self.item_started = time.monotonic()
            self.job = {'id': 'software_updates', 'status': status,
                        'description': str(state.get('description') or previous.get('description')
                                           or 'Application update worker: ' + status)[:2000],
                        'current': state.get('current', previous.get('current', 0)),
                        'total': state.get('total', previous.get('total', 0)),
                        'current_package': package}
            # Serialize emission too, so an older background event cannot
            # replace a newer foreground stage in the queued UI callbacks.
            try:
                output = self._event()
                output.pop('log', None)  # The foreground log line was already emitted.
                self.callback(output)
            except Exception:
                # A display failure must never abandon a vendor installer.
                pass

    def wait_for_updates(self, next_stage=None):
        """Replace stale foreground text, preserving actual package progress."""
        with self.lock:
            description = (('Independent maintenance has finished. ' if next_stage else '')
                           + 'Application updates are still running. '
                           + ('Waiting before: ' + str(next_stage) + '. ' if next_stage else '')
                           + 'The current installer is not restarted; Cancel stops further package attempts.')
            self.foreground = dict(self.foreground, stage=WAITING_FOR_UPDATES,
                waiting_for='software_updates', description=description,
                processes=['Optimus foreground worker: waiting for application updates'],
                log='WAITING: Background application updates before ' + str(next_stage or 'workflow completion'))
            try:
                self.callback(self._event())
            except Exception:
                pass
            self.foreground.pop('log', None)

    def heartbeat(self):
        with self.lock:
            try:
                self.callback(self._event())
            except Exception:
                pass


def installer_barrier(result):
    if not isinstance(result, dict):
        return 'The application worker outcome is unknown; review installer state before another installer.'
    if result.get('cancelled'):
        return 'Application updating was cancelled; later installers were deferred.'
    if result.get('restartNeeded') or result.get('reboot_required'):
        return 'An application update requires a restart; later installers were deferred.'
    if (result.get('failed_items') or result.get('uncertain') or result.get('running')
            or result.get('outcomeKnown') is False or result.get('stop_reason')):
        return str(result.get('stop_reason') or 'An application update failed or remains uncertain; review installer state before another installer.')[:4000]
    for row in result.get('items', []):
        if not isinstance(row, dict):
            return 'An application update item has an unknown outcome; later installers were deferred.'
        outcome = row.get('result', {})
        if row.get('status') in ('failed', 'uncertain'):
            return 'An application update failed or remains uncertain; later installers were deferred.'
        if isinstance(outcome, dict) and (outcome.get('running') or outcome.get('outcomeKnown') is False
                                        or outcome.get('restartNeeded') or outcome.get('exitCode') in (3010, 1641)):
            return 'An application installer needs state or restart review; later installers were deferred.'
    return None


def _uncertain(result, *, native=False):
    """Persist liveness ambiguity, rather than every ordinary deferral/failure."""
    pending = [result]
    while pending:
        value = pending.pop()
        if isinstance(value, dict):
            if (value.get('uncertain') or value.get('running') or value.get('outcomeKnown') is False
                    or any(marker in (str(value.get('status', '')) + ' ' + str(value.get('stop_reason', ''))).casefold()
                           for marker in ('timeout', 'timed out', 'still running', 'uncertain', 'verify installer state'))):
                return True
            if (value.get('error') and value.get('status') == 'failed'
                    or native and value.get('status') == 'failed' and not value.get('skipped')):
                return True
            pending.extend(value.values())
        elif isinstance(value, list):
            pending.extend(value)
    return False


def pending_updates_barrier(backend):
    """Read owned unresolved records for both serial and parallel workflows."""
    probe = ParallelAppUpdates(backend, '0' * 32, WINDOWS_SOFTWARE, None, None)
    return probe._pending()


class ParallelAppUpdates:
    def __init__(self, backend, parent_run_id, title, operation, mux, *, cancel_event=None):
        if not re.fullmatch('[a-f0-9]{32}', str(parent_run_id)):
            raise ValueError('A current workflow identifier is required.')
        self.backend, self.title, self.operation, self.mux = backend, title, operation, mux
        self.directory = Path(backend.data_dir) / 'parallel-updates'
        self.path = self.directory / (uuid.uuid4().hex + '.json')
        from .speed_enhancements import _current_owner
        self.owner = _current_owner()
        self.record = {'schema': 1, 'id': self.path.stem, 'parent_run_id': parent_run_id,
                       'owner': self.owner, 'status': 'prepared', 'started_at': time.time(),
                       'title': title, 'installer_prepared': False,
                       'note': 'No automatic retry is authorized by this record. An unresolved installer record requires manual review; preserve this evidence until installer state is established.'}
        self.result = None
        self.thread = None
        self.stage = None
        self.joined = False
        self.stop_event = threading.Event()
        class Cancellation:
            def is_set(inner):
                return self.stop_event.is_set() or cancel_event is not None and cancel_event.is_set()
        self.cancel_event = Cancellation()

    def cancel(self):
        self.stop_event.set()

    def _write(self):
        from .speed_enhancements import _private_json
        _ordinary_data_path(self.directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        _ordinary_data_path(self.path, file=True)
        _private_json(self.path, self.record)

    def _pending(self):
        _ordinary_data_path(self.directory)
        if not self.directory.exists():
            return None
        paths = list(self.directory.glob('*.json'))
        if len(paths) > 2000:
            return 'Application update history requires review before another background installer.'
        for path in paths:
            info = _ordinary_data_path(path, file=True)
            if info is None or info.st_size > 8 * 1024 * 1024:
                return 'An application update journal is unreadable; verify installer state first.'
            try:
                value = json.loads(path.read_text(encoding='utf-8'))
            except (OSError, ValueError):
                return 'An application update journal is unreadable; verify installer state first.'
            if (not isinstance(value, dict) or value.get('schema') != 1 or value.get('owner') != self.owner
                    or value.get('id') != path.stem):
                return 'Application update history ownership is unknown; verify installer state first.'
            if (value.get('status') in ('prepared', 'running', 'uncertain')
                    and value.get('installer_prepared', True) is not False):
                return ('A previous application installer has an unresolved outcome; no automatic retry was started. '
                        'Review installer state and the saved record before continuing: ' + str(path))
            if value.get('status') == 'needs_review' and isinstance(value.get('result'), dict) and value['result'].get('restartNeeded'):
                try:
                    security = self.backend.security_status()
                except Exception:
                    security = None
                if not isinstance(security, dict) or security.get('reboot_required') is not False:
                    return 'A previous application update requested a restart; its cleared restart state has not been verified.'
        return None

    def start(self):
        reason = self._pending()
        self.stage = {'title': self.title, 'name': self.title, 'status': 'running',
                      'started_at': self.record['started_at'], 'background': True,
                      'worker_log_path': str(self.path)}
        self._write()  # Prepared evidence is durable before the thread can mutate.
        if reason:
            self.result = {'items': [], 'failed_items': 0, 'review_only': True, 'skipped': True,
                           'requiresReview': True, 'uncertain': True, 'stop_reason': reason, 'note': reason}
            self.record.update(status='deferred', result=self.result, finished_at=time.time())
            self._write()
            self.mux.background('deferred', {'description': reason})
            return self.stage
        self.mux.background('pending')
        self.thread = threading.Thread(target=self._run, name='Optimus application updater', daemon=False)
        try:
            self.thread.start()
        except BaseException:
            self.thread = None
            self.record.update(status='deferred', finished_at=time.time())
            self._write()
            raise
        return self.stage

    def item_progress(self, state):
        state = state if isinstance(state, dict) else {'description': str(state)}
        self.record['current_item'] = {key: state[key] for key in ('id', 'description', 'current', 'total', 'item') if key in state}
        self.record['installer_prepared'] = True
        self._write()  # Existing sequential helpers emit this before each installer.
        self.mux.background('running', state)

    def _run(self):
        try:
            self.record['status'] = 'running'
            self._write()
            self.mux.background('running')
            self.result = self.operation(isolated_backend(self.backend), self.item_progress)
            if not isinstance(self.result, dict):
                raise ValueError('The application updater returned an unknown result.')
        except BaseException as error:
            self.result = {'items': [], 'failed_items': 1,
                           'uncertain': self.record['installer_prepared'],
                           'stop_reason': ('The application worker raised an error; verify installer state before continuing.'
                                           if self.record['installer_prepared'] else
                                           'The application scan failed before any installer was prepared; review the saved error.'),
                           'error': str(error)[:4000]}
        native = getattr(self.backend, 'system', 'win32') != 'win32'
        try:
            journal_result = {key: self.result[key] for key in ('failed_items', 'cancelled', 'restartNeeded',
                              'uncertain', 'stop_reason') if key in self.result}
            journal_result['items'] = [{'id': str(row.get('id', ''))[:280], 'status': row.get('status'),
                                       'result': {key: row['result'][key] for key in ('exitCode', 'running', 'outcomeKnown',
                                                  'restartNeeded', 'requiresReview') if key in row['result']}
                                       if isinstance(row.get('result'), dict) else {}}
                                      for row in self.result.get('items', []) if isinstance(row, dict)][:1000]
            self.record.update(status='uncertain' if _uncertain(self.result, native=native) else
                               'needs_review' if installer_barrier(self.result) else 'completed',
                               finished_at=time.time(), result=journal_result)
            self._write()
        except BaseException as error:
            self.result = dict(self.result, failed_items=max(1, self.result.get('failed_items', 0)), uncertain=True,
                               stop_reason='The application outcome journal could not be saved; verify installer state before continuing.',
                               journal_error=str(error)[:2000])
        self.mux.background('needs_review' if installer_barrier(self.result) else 'completed',
                            {'description': str(self.result.get('stop_reason') or 'Application update checks completed; item results are ready.'),
                             'current': len(self.result.get('items', []))})

    def join(self, *, next_stage=None):
        if self.joined:
            return self.stage
        if self.thread is not None:
            if self.thread.is_alive():
                self.mux.wait_for_updates(next_stage)
            while self.thread.is_alive():
                self.thread.join(timeout=1)
                if self.thread.is_alive():
                    self.mux.heartbeat()
            self.thread.join()  # Never release the workflow lease before this returns.
        result = self.result or {'items': [], 'failed_items': 1, 'uncertain': True,
                                 'stop_reason': 'The application worker did not establish an outcome.'}
        status = 'cancelled' if result.get('cancelled') else 'failed' if result.get('failed_items') else 'completed'
        self.stage.update(status=status, finished_at=time.time(), result=result)
        self.joined = True
        if status == 'failed':
            self.stage['error'] = 'One or more application update checks failed; review the saved item results.'
        self.mux.background('needs_review' if installer_barrier(result) else 'completed',
                            {'description': str(result.get('stop_reason') or 'Application update checks completed; item results are ready.'),
                             'current': len(result.get('items', []))})
        return self.stage


def parallel_stages(stages, software_title):
    """Clean before installers; drain safe work before joining the updater."""
    software = next((row for row in stages if row[0] == software_title), None)
    if software is None:
        return stages, None, None
    remaining = [row for row in stages[1:] if row is not software]
    cleanup = [row for row in remaining if row[0] in BEFORE_UPDATES]
    independent = [row for row in remaining if independent_stage(row[0])]
    queued = [row for row in remaining if row[0] not in BEFORE_UPDATES and not independent_stage(row[0])]
    launch_after = (cleanup[-1] if cleanup else stages[0])[0]
    return [stages[0], *cleanup, *independent, *queued], software, launch_after
