"""A serial background installer lane with independent foreground maintenance.

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
WINDOWS_DRIVERS = 'Install approved driver updates'
WAITING_FOR_UPDATES = 'Waiting for background updates'
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


def isolated_backend(backend, *, isolate_bridge=False):
    """Copy known mutable updater caches without discovering host adapters."""
    result = copy.copy(backend)
    if isolate_bridge:
        from .bridge import PowerShellBridge
        if type(getattr(backend, 'bridge', None)) is PowerShellBridge:
            # A long driver install must not hold the foreground inventory
            # bridge's request lock. Construction does not start PowerShell.
            result.bridge = PowerShellBridge(backend.bridge.data_dir)
    from .maintenance import Maintenance
    from .native_operations import NativePackages
    service = getattr(backend, 'maintenance', None)
    if type(service) is Maintenance:
        result.maintenance = copy.copy(service)
        result.maintenance._last_update_ids = set()
        result.maintenance._last_update_packages = {}
        result.maintenance._last_update_scan = 0.0
        if hasattr(result, 'bridge'):
            result.maintenance.bridge = result.bridge
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
        self.jobs = {}
        self.item_started = {}

    def _event(self):
        event = dict(self.foreground)
        if self.jobs:
            event['jobs'] = []
            processes = list(event.get('processes', []))
            for job_id, job in self.jobs.items():
                current = dict(job)
                if job_id in self.item_started and job['status'] in ('pending', 'running', 'waiting'):
                    current['elapsed_seconds'] = max(0, time.monotonic() - self.item_started[job_id])
                event['jobs'].append(current)
                if job['status'] in ('pending', 'running', 'waiting'):
                    processes.append(job['description'])
                    event['percentage'] = min(99, event.get('percentage', 0))
            active = next((job['id'] for job in event['jobs'] if job['status'] == 'running'), None)
            if active:
                event['active_job_id'] = active
            event['processes'] = list(dict.fromkeys(processes))
        return event

    def __call__(self, event):
        with self.lock:
            self.foreground = dict(event)
            self.callback(self._event())

    def background(self, status, state=None, *, job_id='software_updates'):
        state = state if isinstance(state, dict) else {}
        with self.lock:
            previous = self.jobs.get(job_id, {})
            package = state.get('id', previous.get('current_package'))
            if job_id not in self.item_started or package != previous.get('current_package'):
                self.item_started[job_id] = time.monotonic()
            self.jobs[job_id] = {'id': job_id, 'status': status,
                        'description': str(state.get('description') or previous.get('description')
                                           or ('Driver' if job_id == 'driver_updates' else 'Application') + ' update worker: ' + status)[:2000],
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
                           + 'Background updates are still running. '
                           + ('Waiting before: ' + str(next_stage) + '. ' if next_stage else '')
                           + 'The current installer is not restarted; Cancel stops further package attempts.')
            self.foreground = dict(self.foreground, stage=WAITING_FOR_UPDATES,
                stage_status='waiting', waiting_for='installer_updates', next_stage=next_stage, description=description,
                processes=['Optimus foreground worker: waiting for background updates'],
                log='WAITING: Background updates before ' + str(next_stage or 'workflow completion'))
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

    def for_job(self, job_id):
        parent = self
        class JobProgress:
            def background(self, status, state=None):
                parent.background(status, state, job_id=job_id)
            def wait_for_updates(self, next_stage=None):
                parent.wait_for_updates(next_stage)
            def heartbeat(self):
                parent.heartbeat()
        return JobProgress()


def _known_preflight(value):
    """Explicit evidence distinguishes a failed check from a live installer."""
    if not isinstance(value, dict):
        return False
    if value.get('installer_started') is False and value.get('outcomeKnown') is True:
        return True
    if isinstance(value.get('result'), dict):
        return _known_preflight(value['result'])
    items = value.get('items')
    return (isinstance(items, list) and bool(items)
            and all(isinstance(row, dict) and _known_preflight(row) for row in items))


def _known_outcome(value):
    return (isinstance(value, dict) and (value.get('outcomeKnown') is True
            or isinstance(value.get('result'), dict) and _known_outcome(value['result'])))


def _restart_needed(value):
    if isinstance(value, dict):
        return (value.get('restartNeeded') is True or value.get('reboot_required') is True
                or value.get('exitCode') in (3010, 1641) or any(_restart_needed(item) for item in value.values()))
    return isinstance(value, list) and any(_restart_needed(item) for item in value)


def installer_barrier(result):
    """One barrier for application, driver and Windows installer outcomes."""
    if not isinstance(result, dict):
        return 'The update worker outcome is unknown; review installer state before another mutation.'
    pending = [result]
    while pending:
        value = pending.pop()
        if isinstance(value, dict):
            if value.get('blocked_by_prior_installer') is True:
                return str(value.get('stop_reason') or 'A previous installer remains unresolved; later conflicting operations were deferred.')[:4000]
            if value.get('cancelled') is True:
                return 'Updating was cancelled; later conflicting operations were deferred.'
            if (value.get('restartNeeded') is True or value.get('reboot_required') is True
                    or value.get('exitCode') in (3010, 1641)):
                return 'An update requires a restart; later conflicting operations were deferred.'
            known_preflight = _known_preflight(value)
            known_batch = isinstance(value.get('items'), list) and _known_outcome(value)
            if (value.get('running') is True or value.get('outcomeKnown') is False
                    or value.get('uncertain') is True and not known_preflight):
                return 'An installer may still be running or its outcome is uncertain; later conflicting operations were deferred.'
            if not known_preflight and not known_batch and (value.get('failed_items') or value.get('status') in ('failed', 'uncertain')
                                        or value.get('stop_reason')):
                return str(value.get('stop_reason') or 'An update failed or remains uncertain; review installer state before another mutation.')[:4000]
            if 'items' in value and not isinstance(value['items'], list):
                return 'The update item outcomes are malformed; later conflicting operations were deferred.'
            pending.extend(value.values())
        elif isinstance(value, list):
            if any(not isinstance(row, dict) for row in value) and value is result.get('items'):
                return 'An update item has an unknown outcome; later conflicting operations were deferred.'
            pending.extend(value)
    return None


def _uncertain(result, *, native=False):
    """Persist liveness ambiguity, preserving explicit known preflight outcomes."""
    pending = [result]
    while pending:
        value = pending.pop()
        if isinstance(value, dict):
            known_preflight = _known_preflight(value)
            if (value.get('running') is True or value.get('outcomeKnown') is False
                    or value.get('uncertain') is True and not known_preflight):
                return True
            if not known_preflight and not _known_outcome(value):
                if any(marker in (str(value.get('status', '')) + ' ' + str(value.get('stop_reason', ''))).casefold()
                       for marker in ('timeout', 'timed out', 'still running', 'uncertain', 'verify installer state')):
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


def _journal_result(result):
    saved = {key: result[key] for key in ('failed_items', 'cancelled', 'restartNeeded', 'uncertain',
             'stop_reason', 'installer_started', 'outcomeKnown', 'blocked_by_prior_installer') if key in result}
    saved['items'] = [{'id': str(row.get('id') or row.get('binding', {}).get('id', ''))[:280],
                      'status': row.get('status'), 'uncertain': row.get('uncertain', False),
                      'error': str(row.get('error', ''))[:2000],
                      'result': {key: row['result'][key] for key in ('exitCode', 'running', 'outcomeKnown',
                                 'restartNeeded', 'requiresReview', 'installer_started') if key in row['result']}
                      if isinstance(row.get('result'), dict) else {}}
                     for row in result.get('items', []) if isinstance(row, dict)][:1000]
    if _restart_needed(result):
        saved['restartNeeded'] = True
    return saved


def record_installer_outcome(backend, run_id, title, result):
    """Keep a serial driver's/security install ambiguity from disappearing next run."""
    if not isinstance(result, dict):
        result = {'failed_items': 1, 'uncertain': True, 'items': [],
                  'stop_reason': 'The installer stage failed without an established outcome.'}
    uncertain = _uncertain(result, native=getattr(backend, 'system', 'win32') != 'win32')
    if not uncertain and not _restart_needed(result):
        return
    evidence = ParallelAppUpdates(backend, run_id, title, None, None)
    evidence.record.update(status='uncertain' if uncertain else 'needs_review',
        installer_prepared=True, finished_at=time.time(), result=_journal_result(result), serial=True)
    evidence._write()


class ParallelAppUpdates:
    def __init__(self, backend, parent_run_id, title, operation, mux, *, cancel_event=None, isolate_bridge=False):
        if not re.fullmatch('[a-f0-9]{32}', str(parent_run_id)):
            raise ValueError('A current workflow identifier is required.')
        self.backend, self.title, self.operation = backend, title, operation
        self.job_id = ('driver_updates' if title == WINDOWS_DRIVERS else
                       'windows_updates' if title == 'Check or install Windows security updates' else 'software_updates')
        self.mux = mux.for_job(self.job_id) if hasattr(mux, 'for_job') else mux
        self.isolate_bridge = isolate_bridge
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
        restart_checked, restart_clear = False, False
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
                    return ('A previous update installer has an unresolved outcome; no automatic retry was started. '
                        'Review installer state and the saved record before continuing: ' + str(path))
            if value.get('status') == 'needs_review' and isinstance(value.get('result'), dict) and value['result'].get('restartNeeded'):
                if not restart_checked:
                    restart_checked = True
                    try:
                        security = self.backend.security_status()
                    except Exception:
                        security = None
                    restart_clear = isinstance(security, dict) and security.get('reboot_required') is False
                if not restart_clear:
                    return 'A previous update requested a restart; its cleared restart state has not been verified.'
        return None

    def start(self, *, inline=False, prior_barrier=None):
        reason = prior_barrier or self._pending()
        self.stage = {'title': self.title, 'name': self.title, 'status': 'running',
                      'started_at': self.record['started_at'], 'background': True,
                      'worker_log_path': str(self.path)}
        self._write()  # Prepared evidence is durable before the thread can mutate.
        if reason:
            self.defer(reason)
            return self.stage
        self.mux.background('pending', {'description': ('Driver updates queued in the background installer lane.'
                            if self.title == WINDOWS_DRIVERS else 'Application updates queued in the background installer lane.')})
        if inline:
            return self.stage
        self.thread = threading.Thread(target=self._run, name='Optimus background updater', daemon=False)
        try:
            self.thread.start()
        except BaseException:
            self.thread = None
            self.record.update(status='deferred', finished_at=time.time())
            self._write()
            raise
        return self.stage

    def defer(self, reason, *, cancelled=False):
        self.result = {'items': [], 'failed_items': 0, 'review_only': True, 'skipped': True,
                       'requiresReview': not cancelled, 'cancelled': cancelled,
                       'installer_started': False, 'outcomeKnown': True,
                       'blocked_by_prior_installer': not cancelled, 'uncertain': not cancelled,
                       'stop_reason': reason, 'note': reason}
        # A guarded deferral did not launch an installer, but retain the
        # barrier itself in the result so the workflow cannot clear it.
        self.record.update(status='deferred', result=self.result, finished_at=time.time())
        self._write()
        self.mux.background('cancelled' if cancelled else 'deferred', {'description': reason})

    def item_progress(self, state):
        state = state if isinstance(state, dict) else {'description': str(state)}
        if 'id' not in state and isinstance(state.get('binding'), dict):
            state = dict(state, id=state['binding'].get('id'))
        if self.title == WINDOWS_DRIVERS and type(state.get('current')) is int:
            state = dict(state, current=max(0, state['current'] - 1))
        self.record['current_item'] = {key: state[key] for key in ('id', 'description', 'current', 'total', 'item') if key in state}
        self.record['installer_prepared'] = True
        self._write()  # Existing sequential helpers emit this before each installer.
        self.mux.background('running', state)

    def _run(self):
        from .runtime_priority import lower_worker_priority
        restore_priority = lower_worker_priority()
        worker = None
        try:
            self.record['status'] = 'running'
            self.record['execution_started_at'] = time.time()
            self._write()
            self.mux.background('running')
            self.mux.background('running', {'description': 'Checking approved driver updates.'
                                if self.title == WINDOWS_DRIVERS else 'Checking approved application updates.'})
            worker = isolated_backend(self.backend, isolate_bridge=self.isolate_bridge)
            self.result = self.operation(worker, self.item_progress)
            if not isinstance(self.result, dict):
                raise ValueError('The application updater returned an unknown result.')
        except BaseException as error:
            self.result = {'items': [], 'failed_items': 1,
                           'uncertain': self.record['installer_prepared'],
                           'stop_reason': ('The application worker raised an error; verify installer state before continuing.'
                                           if self.record['installer_prepared'] else
                                           'The application scan failed before any installer was prepared; review the saved error.'),
                           'error': str(error)[:4000]}
            if not self.record['installer_prepared'] or (getattr(error, 'installer_started', None) is False
                                                        and getattr(error, 'outcomeKnown', None) is True):
                self.result.update(installer_started=False, outcomeKnown=True, running=False, uncertain=False)
        finally:
            if worker is not None and getattr(worker, 'bridge', None) is not getattr(self.backend, 'bridge', None):
                # Never close the shared backend (its sampler/controller belongs
                # to the foreground). A bridge with pending work stays alive.
                process = getattr(worker.bridge, '_process', None)
                if process is not None:
                    self.record['worker_pid'] = process.pid
                try:
                    worker.bridge.close()
                except Exception:
                    pass
            restore_priority()
        native = getattr(self.backend, 'system', 'win32') != 'win32'
        try:
            journal_result = _journal_result(self.result)
            self.record.update(status='uncertain' if _uncertain(self.result, native=native) else
                               'needs_review' if installer_barrier(self.result) or self.result.get('failed_items') else 'completed',
                               finished_at=time.time(), result=journal_result)
            self._write()
        except BaseException as error:
            self.result = dict(self.result, failed_items=max(1, self.result.get('failed_items', 0)), uncertain=True,
                               stop_reason='The application outcome journal could not be saved; verify installer state before continuing.',
                               journal_error=str(error)[:2000])
        self.mux.background('needs_review' if installer_barrier(self.result) or self.result.get('failed_items') else 'completed',
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
        self.stage.update(status=status,
                          started_at=self.record.get('execution_started_at', self.record['started_at']),
                          finished_at=self.record.get('finished_at', time.time()), result=result)
        self.joined = True
        if status == 'failed':
            self.stage['error'] = 'One or more application update checks failed; review the saved item results.'
        self.mux.background('cancelled' if result.get('cancelled') else 'needs_review' if installer_barrier(result) or result.get('failed_items') else 'completed',
                            {'description': str(result.get('stop_reason') or 'Application update checks completed; item results are ready.'),
                             'current': len(result.get('items', []))})
        return self.stage


class ParallelInstallerQueue:
    """One worker executes app/driver jobs serially; foreground reviews continue."""
    def __init__(self, backend, parent_run_id, operations, mux, *, cancel_event=None, prior_barrier=None):
        self.backend, self.mux = backend, mux
        self.thread, self.joined = None, False
        self.prior_barrier = prior_barrier
        self.stop_event = threading.Event()
        queue = self
        class Cancellation:
            def is_set(self):
                return queue.stop_event.is_set() or cancel_event is not None and cancel_event.is_set()
        self.cancel_event = Cancellation()
        self.jobs = [ParallelAppUpdates(backend, parent_run_id, title, operation, mux,
                     cancel_event=self.cancel_event, isolate_bridge=title == WINDOWS_DRIVERS)
                     for title, operation in operations]
        if not self.jobs:
            raise ValueError('Choose at least one configured background update job.')

    def start(self):
        stages = []
        # Every job is durably prepared before the queue thread can mutate.
        for index, job in enumerate(self.jobs):
            stages.append(job.start(inline=True, prior_barrier=self.prior_barrier))
            if job.result is None and index:
                job.mux.background('pending', {'description': 'Driver updates queued behind application updates; installers run one at a time.'
                                   if job.title == WINDOWS_DRIVERS else 'Updates queued behind the current installer job.'})
        self.thread = threading.Thread(target=self._run, name='Optimus background installer queue', daemon=False)
        try:
            self.thread.start()
        except BaseException:
            self.thread = None
            for job in self.jobs:
                job.defer('The background queue could not start; no installer was launched.')
            raise
        return stages

    def _run(self):
        from .runtime_priority import lower_worker_priority
        restore_priority = lower_worker_priority()
        try:
            barrier = self.prior_barrier
            for job in self.jobs:
                if self.cancel_event.is_set():
                    job.defer('Cancelled before this background job started; no installer was launched.', cancelled=True)
                elif barrier:
                    job.defer(barrier)
                elif job.result is None:
                    job._run()
                barrier = barrier or installer_barrier(job.result)
        finally:
            restore_priority()

    def cancel(self):
        self.stop_event.set()
        for job in self.jobs:
            job.cancel()

    def join(self, *, next_stage=None):
        if not self.joined:
            if self.thread is not None:
                if self.thread.is_alive():
                    self.mux.wait_for_updates(next_stage)
                while self.thread.is_alive():
                    self.thread.join(timeout=1)
                    if self.thread.is_alive():
                        self.mux.heartbeat()
                self.thread.join()
            self.joined = True
        return [job.join() for job in self.jobs]


def parallel_installer_stages(stages, titles):
    """Clean before installers; drain safe work before joining the updater."""
    operations = [row for title in titles for row in stages if row[0] == title]
    if not operations:
        return stages, [], None
    remaining = [row for row in stages[1:] if row[0] not in titles]
    cleanup = [row for row in remaining if row[0] in BEFORE_UPDATES]
    independent = [row for row in remaining if independent_stage(row[0])]
    queued = [row for row in remaining if row[0] not in BEFORE_UPDATES and not independent_stage(row[0])]
    launch_after = (cleanup[-1] if cleanup else stages[0])[0]
    return [stages[0], *cleanup, *independent, *queued], operations, launch_after


def parallel_stages(stages, software_title):
    planned, operations, launch_after = parallel_installer_stages(stages, (software_title,))
    return planned, operations[0] if operations else None, launch_after
