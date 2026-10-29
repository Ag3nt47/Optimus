"""Continue saved app/driver update choices without running general maintenance.

An exact bad package is skipped by the normal updater; the next approved package
can proceed. Native liveness/restart barriers remain binding. This command does
not erase journals, retry uncertain installers, save preferences or run presence
rules that install missing apps or uninstall unwanted apps.
"""
from __future__ import annotations

import copy
from pathlib import Path
import threading
import time
import uuid

from .automation import CURRENT_SETUP_VERSION, RunLease
from .filesystem import _ordinary_data_path, resolve_data_root
from .navigation import NavigationHub
from .parallel_updates import (
    NATIVE_SOFTWARE, WINDOWS_DRIVERS, WINDOWS_DRIVER_REVIEW, WINDOWS_SOFTWARE,
    ParallelInstallerQueue, ProgressMux, installer_barrier, pending_updates_barrier,
    update_completion_state,
)
from .workflow import Preferences, atomic_json


def continuation_exit_code(report):
    """Zero means the eligible queue drained, including individually skipped offers."""
    return 0 if report.get('status') in (
        'completed', 'completed_with_skips', 'no_enabled_updates') else 2


def _pending_native_request(backend):
    # A timed-out PowerShell call can still be executing. A new command must
    # neither close its bridge nor start more work while that request survives.
    from .bridge import PowerShellBridge
    bridge = getattr(backend, 'bridge', None)
    if isinstance(bridge, PowerShellBridge):
        with bridge._pending_lock:
            return bool(bridge._pending)
    return False


def _summary(stages, *, barrier=None, cancelled=False):
    workers, has_skips, scan_incomplete = {}, False, False
    for stage in stages:
        result = stage.get('result') if isinstance(stage.get('result'), dict) else {}
        state = update_completion_state(result)
        key = 'driver_updates' if stage['title'] in (WINDOWS_DRIVERS, WINDOWS_DRIVER_REVIEW) else 'software_updates'
        workers[key] = state
        barrier = barrier or installer_barrier(result)
        cancelled = cancelled or result.get('cancelled') is True
        evidence = next((result[key] for key in ('app_scan', 'driver_scan')
                         if isinstance(result.get(key), dict)), {})
        scan_incomplete = scan_incomplete or evidence.get('complete') is not True
        has_skips = has_skips or bool(result.get('failed_items') or result.get('held_count')
            or result.get('deferred_ids') or result.get('skipped'))
        for row in result.get('items', []) if isinstance(result.get('items'), list) else []:
            has_skips = has_skips or (isinstance(row, dict)
                and row.get('status') in ('failed', 'needs_review', 'skipped', 'cancelled'))
    status = ('cancelled' if cancelled else 'blocked' if barrier else 'needs_review' if scan_incomplete
              else 'completed_with_skips' if has_skips else 'completed')
    return {'status': status, 'workers': workers, 'installation_block_reason': barrier,
            'queue_drained': not bool(barrier or cancelled)}


def run_saved_updates(backend, *, output=None, progress_callback=None, cancel_event=None, _lease=None):
    """Run only currently enabled, saved-approved update scopes under one lease.

    Both catalogs are searched together. Their actual installers share the
    existing exclusive lane and its durable per-package receipts. Known failures
    and exact-offer holds do not stop unrelated packages. A still-running or
    unknown installer is never treated as a disposable stale error.

    The returned JSON contains a persistent report and live progress-file path;
    this also makes results available when launched from a windowed executable.
    """
    from .emergency_stop import controller_for
    controller = controller_for(backend)
    controller.check()
    cancel_event = controller.bind_cancel(cancel_event)
    from .maintenance_toolkit import _lease_context
    with _lease_context(backend.data_dir, _lease) as lease:
        if _pending_native_request(backend):
            raise RuntimeError('A native Optimus request is still running; wait for its saved outcome before continuing updates.')
        validator = getattr(getattr(backend, 'preference_store', None), 'validate', Preferences.validate)
        configuration = validator(backend.preferences())
        if configuration.get('setup_version') != CURRENT_SETUP_VERSION:
            raise ValueError('Complete and save the current setup before continuing approved updates.')
        native = getattr(backend, 'system', 'win32') != 'win32'
        identifier = uuid.uuid4().hex
        operations = []

        if configuration.get('software_updates_enabled') is True:
            def software(worker, item, prepared, before_install):
                if native:
                    from .native_workflow import run_native_software as operation
                else:
                    from .automated_software import run_software as operation
                return operation(worker, configuration, updates_only=True,
                    cancel_event=getattr(worker, '_optimus_lane_cancel_event', cancel_event),
                    item_callback=item, prepared_callback=prepared, before_install=before_install)
            operations.append((NATIVE_SOFTWARE if native else WINDOWS_SOFTWARE, software))
        if not native and configuration.get('driver_updates_enabled') is True:
            def drivers(worker, item, prepared, before_install):
                from .automated_system import run_drivers
                return run_drivers(worker, configuration,
                    cancel_event=getattr(worker, '_optimus_lane_cancel_event', cancel_event),
                    item_callback=item, prepared_callback=prepared, before_install=before_install)
            title = WINDOWS_DRIVER_REVIEW if configuration.get('driver_policy') == 'review' else WINDOWS_DRIVERS
            operations.append((title, drivers))

        navigation = getattr(getattr(backend, 'workflow', None), 'navigation', None)
        if isinstance(navigation, NavigationHub) and navigation.active:
            raise RuntimeError('An Optimus workflow still owns worker navigation; wait for that run to finish.')
        recovery = None
        reconcile = getattr(backend, 'reconcile_completed_updates', None)
        if operations and callable(reconcile):
            recovery = reconcile(lease=lease)
        prior_barrier = pending_updates_barrier(backend) if operations else None
        folder = resolve_data_root(Path(output) if output is not None else Path(backend.data_dir) / 'update-continuations')
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / ('continue-updates-' + identifier + '.json')
        progress_path = folder / ('continue-updates-' + identifier + '.progress.json')
        _ordinary_data_path(path, file=True)
        _ordinary_data_path(progress_path, file=True)
        report = {'schema': 1, 'id': identifier, 'command': 'continue-updates',
            'started_at': time.time(), 'status': 'running', 'updates_only': True,
            'preferences_saved': False, 'cleanup_actions_executed': 0,
            'presence_actions_executed': 0, 'journals_deleted': 0,
            'recovery': recovery, 'stages': [], 'report': str(path), 'progress_report': str(progress_path),
            'note': 'Saved update approvals only. Known failed or held packages are skipped; the remaining eligible queue continues. Active, unknown or restart-required native operations retain their safety gate.'}
        atomic_json(path, report)  # The command receipt precedes every updater.
        observed, last_saved, progress_errors = None, 0.0, []
        progress_lock = threading.RLock()

        def emit(event, *, final=False):
            nonlocal observed, last_saved
            with progress_lock:
                observed = copy.deepcopy(event)
                now = time.monotonic()
                if final or now - last_saved >= .5:
                    try:
                        atomic_json(progress_path, {'schema': 1, 'run_id': identifier,
                            'updated_at': time.time(), 'final': final, 'event': observed})
                        last_saved = now
                    except (OSError, ValueError) as error:
                        if len(progress_errors) < 10:
                            progress_errors.append(type(error).__name__ + ': ' + str(error)[:500])
                if callable(progress_callback):
                    try:
                        progress_callback(copy.deepcopy(event))
                    except Exception:
                        pass  # A display failure cannot abandon a vendor installer.

        mux = ProgressMux(emit)
        queue, navigation_started = None, False
        try:
            if not operations:
                report.update(status='no_enabled_updates', workers={}, queue_drained=True,
                    all_offers_installed=False, installation_block_reason=None,
                    note='App and driver update stages are disabled in the saved settings; no update search or installer was started.')
            else:
                if isinstance(navigation, NavigationHub):
                    navigation.begin(identifier, cancel_event=cancel_event)
                    navigation_started = True
                mux({'percentage': 0, 'stage': 'Continue saved updates', 'stage_status': 'running',
                     'description': 'Searching saved app and driver scopes; individual held offers will be skipped.', 'processes': []})
                queue = ParallelInstallerQueue(backend, identifier, operations, mux,
                    cancel_event=cancel_event, prior_barrier=prior_barrier)
                report['stages'] = queue.start()
                atomic_json(path, report)
                preparation_barrier = queue.wait_prepared()
                queue.release_maintenance(preparation_barrier)
                report['stages'] = queue.join()
                report.update(_summary(report['stages'], barrier=queue.barrier,
                    cancelled=cancel_event is not None and cancel_event.is_set()))
            report['finished_at'] = time.time()
            report['resource_totals'] = mux.worker_resource_snapshots()
            if configuration.get('toolkit_settings', {}).get('resume', {}).get('enabled') is True:
                from .update_resume import UpdateResumeService
                try:
                    report['update_resume'] = UpdateResumeService(backend).capture(configuration, report, lease=lease)
                except (OSError, ValueError, RuntimeError) as error:
                    report['update_resume'] = {'status': 'unavailable', 'reason': str(error)[:1000]}
            if progress_errors:
                report['progress_write_errors'] = progress_errors
            atomic_json(path, report)
            terminal = dict(observed or {}, stage='Saved update continuation finished',
                stage_status=report['status'],
                percentage=100 if report.get('queue_drained') else (observed or {}).get('percentage', 0),
                description=report.get('installation_block_reason') or report['note'])
            emit(terminal, final=True)
            return report
        except BaseException as error:
            if queue is not None:
                queue.cancel()
                report['stages'] = queue.join()  # Retain the lease until every native call returns.
            report.update(status='error', finished_at=time.time(), error=type(error).__name__ + ': ' + str(error)[:2000])
            atomic_json(path, report)
            raise
        finally:
            if navigation_started:
                navigation.end(report['status'])
