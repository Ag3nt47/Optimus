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


def _status(result: Any) -> str:
    if not isinstance(result, dict):
        return 'completed'
    if result.get('cancelled') or result.get('status') in ('cancelled', 'canceled'):
        return 'cancelled'
    failed = result.get('failed_items')
    if (isinstance(failed, (int, float)) and failed > 0
            or result.get('status') in ('failed', 'error', 'check_failed', 'update_failed',
                'blocked', 'partial', 'uncertain', 'requires_admin', 'not_installed', 'python_missing')):
        return 'failed'
    # Zero Breach and tracker blocking return their component outcomes together.
    components = [result[key] for key in ('update', 'scan', 'blocked') if isinstance(result.get(key), dict)]
    statuses = {_status(component) for component in components}
    if 'cancelled' in statuses:
        return 'cancelled'
    if 'failed' in statuses:
        return 'failed'
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


def _run_job(key: str, backend: Any, configuration: dict, cancel_event: Any, progress: Callable) -> Any:
    if key == 'drivers':
        from .automated_system import run_drivers
        return run_drivers(backend, configuration, cancel_event=cancel_event, item_callback=progress)
    if key == 'apps':
        from .automated_software import run_software
        from .post_run_fixes import _prior_installer_barrier
        barrier = _prior_installer_barrier(backend, {})
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
                     job_runner: Callable[..., Any] = _run_job) -> dict[str, Any]:
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
        for index, (key, title, _setting) in enumerate(enabled):
            if cancel_event is not None and cancel_event.is_set():
                entry = {'job': key, 'title': title, 'status': 'cancelled'}
                report['jobs'].append(entry)
                _notify(progress, dict(entry, current=index + 1, total=total, description=title + ': cancelled'))
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
                                   'detail': event})

            started = time.monotonic()
            try:
                result = job_runner(key, backend, configuration, cancel_event, notify)
                entry = {'job': key, 'title': title, 'status': _status(result), 'result': result}
            except Exception as error:  # one job never stops the others
                entry = {'job': key, 'title': title, 'status': 'failed', 'error': str(error)[:500]}
            finally:
                active.clear()
            entry['seconds'] = round(time.monotonic() - started, 1)
            report['jobs'].append(entry)
            _notify(progress, dict(entry, current=index + 1, total=total,
                                   description=title + ': ' + entry['status']))
    finally:
        release = getattr(lease, 'release', None)
        if callable(release):
            release()
    statuses = {entry['status'] for entry in report['jobs']}
    report['status'] = ('cancelled' if 'cancelled' in statuses else 'failed' if 'failed' in statuses else 'completed')
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
        self.lock = threading.Lock()
        self.thread: threading.Thread | None = None
        self.timer = None
        self.last: dict | None = None
        self._generation = 0

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
                _notify(self.on_progress, event)
        try:
            arguments = {'cancel_event': self.cancel}
            if self.on_progress is not None and _accepts_keyword(self.runner, 'progress'):
                arguments['progress'] = progress
            report = self.runner(self.backend, self.settings_reader(), **arguments)
        except Exception as error:
            report = {'status': 'failed', 'error': str(error)[:500], 'jobs': []}
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

    def stop(self) -> None:
        self.cancel.set()
        with self.lock:
            if self.timer is not None:
                self.timer.cancel()
                self.timer = None
