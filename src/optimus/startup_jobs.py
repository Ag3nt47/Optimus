"""Background jobs that re-run on every Optimus start and restart.

The full Auto Clean run still waits for its launch review. These jobs are the
ones the user wants to keep going regardless: the driver updater, the app
updater, the Defender virus/spyware scan, the Zero Breach update and scan, and
hidden recorder/tracker blocking. Each runs only when its own saved switch is
on and uses its own saved approvals (driver/app installs still follow the saved
policy; review-only policies stay review-only).

The pass holds the normal maintenance lease, so it never overlaps an Auto Clean
run. It first reconciles installer journals left by a killed process, so an
interrupted run no longer blocks later updates. If the lease is busy, or a job
fails, it is retried after ``RETRY_SECONDS``. A per-job result is written to
``<data>/startup-jobs/last.json``.
"""
from __future__ import annotations

import copy
from datetime import datetime, timezone
import inspect
from pathlib import Path
import threading
import time
from typing import Any, Callable

RETRY_SECONDS = 15 * 60
FOLDER = 'startup-jobs'
JOBS = (
    ('drivers', 'Driver updater', 'driver_updates_enabled'),
    ('apps', 'App updater', 'software_updates_enabled'),
    ('defender', 'Virus & spyware scan', 'spyware_scan_enabled'),
    ('zero_breach', 'Zero Breach update and scan', 'zero_breach_enabled'),
    ('trackers', 'Hidden recorder and tracker blocking', 'tracker_blocking_enabled'),
)


def _utc() -> str:
    return datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


def _coverage_needs_review(result: dict) -> bool:
    """Recognize the one successful-scan coverage advisory, not a scan error."""
    from .automated_software import UNKNOWN_VERSION_COVERAGE_WARNING
    from .update_scan_evidence import app_scan_evidence

    scan, evidence = result.get('scan'), result.get('app_scan')
    if not isinstance(scan, dict) or not isinstance(evidence, dict):
        return False
    rows = scan.get('updates', scan.get('packages'))
    if (scan.get('source') != 'winget' or scan.get('structured') is not True
            or type(scan.get('exitCode')) is not int or scan['exitCode'] != 0
            or scan.get('complete', True) is not True or scan.get('error') or scan.get('errors')
            or scan.get('excluded') != [UNKNOWN_VERSION_COVERAGE_WARNING]
            or not isinstance(rows, list) or len(rows) > 1000
            or any(not isinstance(row, dict) for row in rows)):
        return False
    expected = [{'source': 'winget', 'error': UNKNOWN_VERSION_COVERAGE_WARNING}]
    derived = app_scan_evidence(scan)
    return (derived['status'] == evidence.get('status') == 'incomplete'
            and evidence.get('complete') is False
            and derived['errors'] == evidence.get('errors') == expected)


def _review_failure_count(result: dict) -> int:
    """Account for review bookkeeping only when the actual rows explain it."""
    items = result.get('items')
    if (not isinstance(items, list) or len(items) > 1000
            or not any(key in result for key in ('app_scan', 'scan_failed_items', 'requires_review_items'))
            or result.get('error') or result.get('errors')):
        return 0
    review = 0
    for item in items:
        if (not isinstance(item, dict) or item.get('error')
                or item.get('status') not in ('completed', 'skipped', 'needs_review', 'deferred')):
            return 0
        outcome = item.get('result')
        if (item.get('status') in ('skipped', 'needs_review', 'deferred')
                and isinstance(outcome, dict) and outcome.get('requiresReview') is True
                and not outcome.get('error') and not outcome.get('errors')
                and _status(outcome) == 'needs_review'):
            review += 1
    scan_failed = result.get('scan_failed_items', 0)
    if scan_failed:
        if type(scan_failed) is not int or scan_failed != 1 or not _coverage_needs_review(result):
            return 0
        review += 1
    return review


def _failed_count(result: dict) -> bool:
    failed = result.get('failed_items')
    return (isinstance(failed, (int, float)) and failed > 0
            and (type(failed) is not int or failed != _review_failure_count(result)))


def _status(result: Any) -> str:
    if not isinstance(result, dict):
        return 'completed'
    if (result.get('uncertain') is True or result.get('outcomeKnown') is False
            or result.get('running') is True or result.get('status') == 'uncertain'):
        return 'uncertain'
    # Zero Breach and tracker blocking return their component outcomes together.
    # Updaters also retain each actual package receipt beneath its item row.
    components = [result[key] for key in ('update', 'scan', 'blocked', 'result')
                  if isinstance(result.get(key), dict)]
    if (isinstance(result.get('items'), list)
            and any(key in result for key in ('app_scan', 'scan_failed_items', 'requires_review_items'))):
        components.extend(item for item in result['items'] if isinstance(item, dict))
    if isinstance(result.get('app_scan'), dict):
        components.append(result['app_scan'])
    statuses = {_status(component) for component in components}
    if 'uncertain' in statuses:
        return 'uncertain'
    if result.get('cancelled') or result.get('status') in ('cancelled', 'canceled') or 'cancelled' in statuses:
        return 'cancelled'
    if (_failed_count(result) or 'failed' in statuses
            or result.get('status') in ('failed', 'error', 'check_failed', 'update_failed',
                'blocked', 'partial', 'requires_admin', 'not_installed', 'python_missing')):
        return 'failed'
    if (result.get('requiresReview') is True or result.get('status') in ('needs_review', 'deferred')
            or 'needs_review' in statuses or _review_failure_count(result) > 0
            or isinstance(result.get('deferred_ids'), list) and result['deferred_ids']):
        return 'needs_review'
    if result.get('skipped') or result.get('status') in ('skipped', 'unsupported', 'not_applicable'):
        return 'skipped'
    return 'completed'


def _notify(progress: Callable | None, event: dict) -> None:
    """Progress observes maintenance; an observer cannot fail or alter the run."""
    if callable(progress):
        try:
            progress(copy.deepcopy(event))
        except Exception:
            pass


def _accepts_keyword(function: Callable, name: str) -> bool:
    try:
        parameters = inspect.signature(function).parameters.values()
    except (TypeError, ValueError):
        return False
    return any(parameter.kind == inspect.Parameter.VAR_KEYWORD
               or parameter.name == name and parameter.kind in (
                   inspect.Parameter.POSITIONAL_OR_KEYWORD, inspect.Parameter.KEYWORD_ONLY)
               for parameter in parameters)


class _JobCancellation:
    """Combine Stop with one job's Skip without letting Skip stop the pass."""

    def __init__(self, master, local):
        self.master, self.local = master, local

    def is_set(self):
        return self.local.is_set() or self.master is not None and self.master.is_set()

    def set(self):
        self.local.set()

    def wait(self, timeout=None):
        deadline = None if timeout is None else time.monotonic() + max(0, timeout)
        while not self.is_set():
            remaining = None if deadline is None else deadline - time.monotonic()
            if remaining is not None and remaining <= 0:
                return self.is_set()
            self.local.wait(.025 if remaining is None else min(.025, remaining))
        return True


def _skip_status(result, status):
    """A skip request cannot erase a failed or still-uncertain native outcome."""
    pending = [result]
    failure = False
    while pending:
        value = pending.pop()
        if isinstance(value, dict):
            if (value.get('uncertain') is True or value.get('outcomeKnown') is False
                    or value.get('running') is True or value.get('may_continue') is True
                    or value.get('status') == 'uncertain'):
                return 'uncertain'
            failure = failure or (_failed_count(value)
                or value.get('status') in ('failed', 'error', 'check_failed', 'update_failed', 'partial'))
            pending.extend(value.values())
        elif isinstance(value, list):
            pending.extend(value)
    if failure:
        return 'failed'
    return 'skipped' if status == 'cancelled' else status


def _not_started_cancelled():
    return {'status': 'cancelled', 'cancelled': True, 'skipped': True,
            'installer_started': False, 'outcomeKnown': True, 'running': False,
            'note': 'Cancelled before this startup operation started.'}


def _run_job(key: str, backend: Any, configuration: dict, cancel_event: Any, progress: Callable) -> Any:
    if cancel_event is not None and cancel_event.is_set():
        return _not_started_cancelled()
    if key in ('drivers', 'apps'):
        from .parallel_updates import pending_updates_barrier
        barrier = pending_updates_barrier(backend)
        if barrier:
            return {'status': 'skipped', 'skipped': True, 'requiresReview': True,
                    'installation_blocked': True, 'note': str(barrier),
                    'installer_started': False, 'outcomeKnown': True, 'running': False}
        if cancel_event is not None and cancel_event.is_set():
            return _not_started_cancelled()
    if key == 'drivers':
        from .automated_system import run_drivers
        return run_drivers(backend, configuration, cancel_event=cancel_event, item_callback=progress)
    if key == 'apps':
        from .automated_software import run_software
        from .post_run_fixes import _prior_installer_barrier
        barrier = _prior_installer_barrier(backend, {'stages': []})
        if barrier:
            return {'skipped': True, 'requiresReview': True, 'note': barrier}
        return run_software(backend, configuration, cancel_event=cancel_event, item_callback=progress)
    if key == 'defender':
        automatic = getattr(backend, 'scan_spyware_automatic', None)
        function = automatic if callable(automatic) else backend.scan_spyware
        arguments = {'cancel_event': cancel_event}
        if callable(automatic):
            arguments['settings'] = configuration
        if _accepts_keyword(function, 'progress_callback'):
            arguments['progress_callback'] = progress
        return function(**arguments)
    if key == 'zero_breach':
        function = backend.zero_breach_update_and_scan
        arguments = {'cancel_event': cancel_event}
        if _accepts_keyword(function, 'progress_callback'):
            arguments['progress_callback'] = progress
        return function(**arguments)
    if key == 'trackers':
        return backend.audio_trackers_auto_block(cancel_event=cancel_event)
    raise ValueError('Unknown startup job.')


def run_startup_jobs(backend: Any, settings: dict, *, cancel_event: Any = None,
                     progress: Callable[[dict], None] | None = None,
                     lease_factory: Callable[[Path], Any] | None = None,
                     job_runner: Callable[..., Any] = _run_job,
                     job_control: Any = None) -> dict[str, Any]:
    from .workflow import Preferences, atomic_json
    configuration = Preferences.validate(settings)
    report = {'started_at': _utc(), 'jobs': [], 'status': 'completed'}
    path = Path(backend.data_dir) / FOLDER / 'last.json'
    enabled = [job for job in JOBS if configuration.get(job[2])]
    total = len(enabled)
    if not configuration.get('startup_jobs_enabled'):
        report.update(status='disabled', finished_at=_utc())
        _notify(progress, dict(report, current=0, total=0, description='Startup jobs disabled'))
        return report
    _notify(progress, {'status': 'running', 'current': 0, 'total': total,
                       'description': 'Preparing startup updates and scans'})
    if lease_factory is None:
        from .automation import RunLease
        lease_factory = lambda root: RunLease(root).acquire()  # noqa: E731
    try:
        lease = lease_factory(Path(backend.data_dir))
    except Exception as error:  # another run holds the maintenance lease
        report.update(status='busy', detail=str(error)[:300], finished_at=_utc())
        atomic_json(path, report)
        _notify(progress, dict(report, current=0, total=total, description=report['detail']))
        return report
    try:
        reconcile = getattr(backend, 'reconcile_completed_updates', None)
        if callable(reconcile):
            try:
                report['reconciliation'] = reconcile(lease=lease)
            except Exception as error:
                report['reconciliation'] = {'status': 'unavailable', 'error': str(error)[:300]}
        unresolved = None
        for index, (key, title, _setting) in enumerate(enabled):
            if cancel_event is not None and cancel_event.is_set():
                entry = {'job': key, 'title': title, 'status': 'cancelled'}
                if job_control is not None:
                    job_control._finish_job(key, 'cancelled')
                report['jobs'].append(entry)
                _notify(progress, dict(entry, current=index + 1, total=total, description=title + ': cancelled'))
                continue
            if unresolved is not None:
                entry = {'job': key, 'title': title, 'status': 'skipped',
                         'result': {'status': 'skipped', 'skipped': True, 'requiresReview': True,
                                    'installer_started': False, 'outcomeKnown': True, 'running': False,
                                    'note': unresolved}}
                if job_control is not None and job_control._finish_job(key, 'skipped'):
                    entry['skip_requested'] = True
                report['jobs'].append(entry)
                _notify(progress, dict(entry, current=index + 1, total=total, description=unresolved))
                continue
            if job_control is not None and not job_control._begin_job(key, index, total):
                entry = {'job': key, 'title': title, 'status': 'skipped', 'skip_requested': True,
                         'result': {'status': 'skipped', 'skipped': True, 'user_skipped': True,
                                    'installer_started': False, 'outcomeKnown': True, 'running': False,
                                    'note': 'Skipped at your request before this startup job started.'}}
                job_control._finish_job(key, 'skipped')
                report['jobs'].append(entry)
                _notify(progress, dict(entry, current=index + 1, total=total,
                                       description=title + ': skipped at your request'))
                continue
            _notify(progress, {'job': key, 'title': title, 'status': 'running',
                               'current': index, 'total': total, 'description': title})
            active = threading.Event()
            active.set()

            def notify(event, *, job=key, label=title, current=index, active_job=active):
                if not active_job.is_set() or not isinstance(event, dict):
                    return
                _notify(progress, {'job': job, 'title': label, 'status': 'running',
                                   'current': current, 'total': total,
                                   'description': event.get('description') or event.get('stage') or label,
                                   **({'skip_requested': True} if job_control is not None
                                      and job_control._skip_requested(job) else {}),
                                   'detail': event})

            started = time.monotonic()
            try:
                local_cancel = (job_control._cancellation(key, cancel_event)
                                if job_control is not None else cancel_event)
                result = job_runner(key, backend, configuration, local_cancel, notify)
                status = _status(result)
                if (job_control is not None and job_control._skip_requested(key)
                        and not (cancel_event is not None and cancel_event.is_set())):
                    status = _skip_status(result, status)
                entry = {'job': key, 'title': title, 'status': status, 'result': result}
            except Exception as error:  # one job never stops the others
                entry = {'job': key, 'title': title, 'status': 'failed', 'error': str(error)[:500]}
            finally:
                active.clear()
            if job_control is not None and job_control._finish_job(key, entry['status']):
                entry['skip_requested'] = True
            if entry['status'] == 'uncertain':
                unresolved = (title + ' has an unresolved native outcome. Remaining startup jobs were not started; '
                              'review the saved outcome before retrying.')
                report['detail'] = unresolved
            entry['seconds'] = round(time.monotonic() - started, 1)
            report['jobs'].append(entry)
            _notify(progress, dict(entry, current=index + 1, total=total,
                                   description=title + ': ' + entry['status']))
    finally:
        release = getattr(lease, 'release', None)
        if callable(release):
            release()
    statuses = {entry['status'] for entry in report['jobs']}
    report['status'] = ('uncertain' if 'uncertain' in statuses else
                        'cancelled' if 'cancelled' in statuses else 'failed' if 'failed' in statuses else
                        'needs_review' if 'needs_review' in statuses else 'completed')
    report['finished_at'] = _utc()
    try:
        atomic_json(path, report)
    except (OSError, TypeError, ValueError):
        report['save_error'] = True
    _notify(progress, dict(report, current=total, total=total, description='Startup jobs ' + report['status']))
    return report


class StartupJobs:
    """Runs the pass on a background thread; retries while busy or after failures."""

    def __init__(self, backend: Any, settings_reader: Callable[[], dict], *,
                 runner: Callable[..., dict] = run_startup_jobs,
                 timer_factory: Callable[[float, Callable[[], None]], Any] = threading.Timer,
                 on_finished: Callable[[dict], None] | None = None,
                 on_progress: Callable[[dict], None] | None = None):
        self.backend, self.settings_reader, self.runner = backend, settings_reader, runner
        self.timer_factory, self.on_finished = timer_factory, on_finished
        self.on_progress = on_progress
        self.cancel = threading.Event()
        self.lock = threading.RLock()
        self.thread: threading.Thread | None = None
        self.timer = None
        self.last: dict | None = None
        self._generation = 0
        self._skips: set[str] = set()
        self._job_states: dict[str, str] = {}
        self._job_events: dict[str, threading.Event] = {}
        self._job_current = 0
        self._job_total = 0

    @property
    def running(self) -> bool:
        return self.thread is not None and self.thread.is_alive()

    def start(self) -> bool:
        with self.lock:
            if self.running:
                return False
            self.cancel.clear()
            self._generation += 1
            generation = self._generation
            self.last = None
            self._job_states = {}
            self._job_events = {}
            if self.timer is not None:
                self.timer.cancel()
                self.timer = None
            self.thread = threading.Thread(target=self._work, args=(generation,),
                                           name='optimus-startup-jobs', daemon=True)
            self.thread.start()
            return True

    def _work(self, generation: int) -> None:
        def progress(event):
            if generation == self._generation and not self.cancel.is_set():
                if isinstance(event, dict) and event.get('job') in self._job_states:
                    with self.lock:
                        self._job_states[event['job']] = str(event.get('status') or 'running')
                        self._job_current = event.get('current', self._job_current)
                _notify(self.on_progress, event)
        try:
            configuration = self.settings_reader()
            from .workflow import Preferences
            selected = Preferences.validate(configuration)
            keys = [key for key, _title, setting in JOBS
                    if selected.get('startup_jobs_enabled') and selected.get(setting)]
            with self.lock:
                self._job_states = {key: 'pending' for key in keys}
                self._job_events = {key: threading.Event() for key in keys}
                self._job_current, self._job_total = 0, len(keys)
                for key in self._skips.intersection(keys):
                    self._job_events[key].set()
            arguments = {'cancel_event': self.cancel}
            if self.on_progress is not None and _accepts_keyword(self.runner, 'progress'):
                arguments['progress'] = progress
            if _accepts_keyword(self.runner, 'job_control'):
                arguments['job_control'] = self
            report = self.runner(self.backend, configuration, **arguments)
        except Exception as error:
            report = {'status': 'failed', 'error': str(error)[:500], 'jobs': []}
        with self.lock:
            for key, status in self._job_states.items():
                if status in ('pending', 'running'):
                    self._job_states[key] = 'not_run'
            self.last = report
        if self.on_finished is not None:
            try:
                self.on_finished(report)
            except Exception:
                pass
        if report.get('status') in ('busy', 'failed') and not self.cancel.is_set():
            with self.lock:
                if self.cancel.is_set():
                    return
                self.timer = self.timer_factory(RETRY_SECONDS, self.start)
                self.timer.daemon = True
                self.timer.start()

    def _begin_job(self, key, current, total):
        with self.lock:
            self._job_current, self._job_total = current, total
            if key in self._skips:
                self._job_states[key] = 'skipped'
                return False
            self._job_states[key] = 'running'
            return True

    def _cancellation(self, key, master):
        with self.lock:
            return _JobCancellation(master, self._job_events[key])

    def _skip_requested(self, key):
        with self.lock:
            return key in self._skips

    def _finish_job(self, key, status):
        with self.lock:
            self._job_states[key] = status
            return key in self._skips

    def skip(self, key: str) -> bool:
        """Skip one pending/active job for this app session, without Stop."""
        with self.lock:
            if (not isinstance(key, str) or not self.running or self.cancel.is_set()
                    or self._job_states.get(key) not in ('pending', 'running') or key in self._skips):
                return False
            active = self._job_states[key] == 'running'
            self._skips.add(key)
            if not active:
                self._job_states[key] = 'skipped'
            title = next(title for name, title, _setting in JOBS if name == key)
            event = {'job': key, 'title': title, 'status': 'running' if active else 'skipped',
                     'skip_requested': True, 'current': self._job_current, 'total': self._job_total,
                     'description': ('Skipping safely; waiting for the active operation to return.' if active
                                     else 'Skipped at your request before this startup job started.')}
            # Publish the request before waking the native cancellation token.
            # The worker finishes jobs under this same lock, so a late request
            # can never arrive after its actual terminal progress notification.
            _notify(self.on_progress, event)
            self._job_events[key].set()
            return True

    def stop(self) -> None:
        self.cancel.set()
        with self.lock:
            if self.timer is not None:
                self.timer.cancel()
                self.timer = None
