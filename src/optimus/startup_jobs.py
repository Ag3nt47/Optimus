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

from datetime import datetime, timezone
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
    if result.get('cancelled'):
        return 'cancelled'
    if result.get('skipped'):
        return 'skipped'
    if result.get('status') in ('failed', 'error', 'check_failed', 'update_failed'):
        return 'failed'
    return 'completed'


def _run_job(key: str, backend: Any, configuration: dict, cancel_event: Any, progress: Callable) -> Any:
    if key == 'drivers':
        from .automated_system import run_drivers
        return run_drivers(backend, configuration, cancel_event=cancel_event)
    if key == 'apps':
        from .automated_software import run_software
        from .post_run_fixes import _prior_installer_barrier
        barrier = _prior_installer_barrier(backend, {})
        if barrier:
            return {'skipped': True, 'requiresReview': True, 'note': barrier}
        return run_software(backend, configuration, cancel_event=cancel_event)
    if key == 'defender':
        automatic = getattr(backend, 'scan_spyware_automatic', None)
        if callable(automatic):
            return automatic(settings=configuration, cancel_event=cancel_event)
        return backend.scan_spyware(cancel_event=cancel_event)
    if key == 'zero_breach':
        return backend.zero_breach_update_and_scan(cancel_event=cancel_event)
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
    if not configuration.get('startup_jobs_enabled'):
        report.update(status='disabled', finished_at=_utc())
        return report
    if lease_factory is None:
        from .automation import RunLease
        lease_factory = lambda root: RunLease(root).acquire()  # noqa: E731
    try:
        lease = lease_factory(Path(backend.data_dir))
    except Exception as error:  # another run holds the maintenance lease
        report.update(status='busy', detail=str(error)[:300], finished_at=_utc())
        atomic_json(path, report)
        return report
    notify = progress or (lambda event: None)
    try:
        reconcile = getattr(backend, 'reconcile_completed_updates', None)
        if callable(reconcile):
            try:
                report['reconciliation'] = reconcile(lease=lease)
            except Exception as error:
                report['reconciliation'] = {'status': 'unavailable', 'error': str(error)[:300]}
        enabled = [job for job in JOBS if configuration.get(job[2])]
        for index, (key, title, _setting) in enumerate(enabled):
            if cancel_event is not None and cancel_event.is_set():
                report['jobs'].append({'job': key, 'title': title, 'status': 'cancelled'})
                continue
            notify({'current': index, 'total': len(enabled), 'description': title})
            started = time.monotonic()
            try:
                result = job_runner(key, backend, configuration, cancel_event, notify)
                entry = {'job': key, 'title': title, 'status': _status(result), 'result': result}
            except Exception as error:  # one job never stops the others
                entry = {'job': key, 'title': title, 'status': 'failed', 'error': str(error)[:500]}
            entry['seconds'] = round(time.monotonic() - started, 1)
            report['jobs'].append(entry)
        notify({'current': len(enabled), 'total': len(enabled), 'description': 'Startup jobs finished'})
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
    return report


class StartupJobs:
    """Runs the pass on a background thread; retries while busy or after failures."""

    def __init__(self, backend: Any, settings_reader: Callable[[], dict], *,
                 runner: Callable[..., dict] = run_startup_jobs,
                 timer_factory: Callable[[float, Callable[[], None]], Any] = threading.Timer,
                 on_finished: Callable[[dict], None] | None = None):
        self.backend, self.settings_reader, self.runner = backend, settings_reader, runner
        self.timer_factory, self.on_finished = timer_factory, on_finished
        self.cancel = threading.Event()
        self.lock = threading.Lock()
        self.thread: threading.Thread | None = None
        self.timer = None
        self.last: dict | None = None

    @property
    def running(self) -> bool:
        return self.thread is not None and self.thread.is_alive()

    def start(self) -> bool:
        with self.lock:
            if self.running:
                return False
            self.cancel.clear()
            if self.timer is not None:
                self.timer.cancel()
                self.timer = None
            self.thread = threading.Thread(target=self._work, name='optimus-startup-jobs', daemon=True)
            self.thread.start()
            return True

    def _work(self) -> None:
        try:
            report = self.runner(self.backend, self.settings_reader(), cancel_event=self.cancel)
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
                self.timer = self.timer_factory(RETRY_SECONDS, self.start)
                self.timer.daemon = True
                self.timer.start()

    def stop(self) -> None:
        self.cancel.set()
        with self.lock:
            if self.timer is not None:
                self.timer.cancel()
                self.timer = None
