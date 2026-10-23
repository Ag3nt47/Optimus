"""Application services consumed by the Qt interface."""
from __future__ import annotations
import ctypes
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from .bridge import PowerShellBridge
from .cleaner import Cleaner
from .sampler import ResourceSampler
from .workflow import Preferences, Workflow
from . import performance
from .filesystem import resolve_data_root, default_data_root
from .extended_backend import ExtendedMaintenanceBackend

class Backend(ExtendedMaintenanceBackend):
    def __init__(self, data_dir: Path | None = None):
        self.data_dir = resolve_data_root(Path(data_dir)) if data_dir is not None else default_data_root()
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.bridge = PowerShellBridge(self.data_dir / 'engine')
        self.cleaner = Cleaner(self.data_dir)
        self.sampler = ResourceSampler()
        self.preference_store = Preferences(self.data_dir)
        self.performance_controller = performance.PerformanceController(self.data_dir / 'suspended-processes.json')
        self.workflow = Workflow(self)
        from .browser_privacy import BrowserPrivacy
        self.browser_privacy_engine = BrowserPrivacy(self.data_dir)
        from .browser_cleanup import BrowserSessionController
        self.browser_sessions = BrowserSessionController()
        from .cloud_drive import CloudDrive
        self.cloud = CloudDrive(self.data_dir)
        from .maintenance import Maintenance
        self.maintenance = Maintenance(self.bridge, self.data_dir)
        self.last_audit = None
        from .process_harness import ProcessHarness
        self.process_harness = ProcessHarness(self)

    @staticmethod
    def _list(value):
        return [] if value is None else value if isinstance(value, list) else [value]

    def elevate(self):
        from .elevation import request_administrator
        result = request_administrator()
        if result.get('started') is not True:
            raise RuntimeError('Administrator relaunch was not completed.')
        return dict(result, note='The administrator window is a separate process. Your current window remains open.')

    def source_root(self):
        if os.environ.get('OPTIMUS_ORIGINAL_SOURCE'):
            return Path(os.environ['OPTIMUS_ORIGINAL_SOURCE'])
        return Path(sys.executable).parent / 'source' if getattr(sys, 'frozen', False) else Path(__file__).parents[2]

    def preferences(self):
        return self.preference_store.load()

    def retention_cleanup(self, kind, settings, *, progress_callback=None, cancel_event=None):
        from .retention_cleanup import RetentionCleanup
        return RetentionCleanup(self.data_dir, scope_reader=self.preferences).run(kind, settings,
            progress_callback=progress_callback, cancel_event=cancel_event)

    def codex_archive_cleanup(self, settings, *, progress_callback=None, cancel_event=None):
        from .codex_archives import run_archive_cleanup
        return run_archive_cleanup(self, settings, progress_callback=progress_callback, cancel_event=cancel_event)

    @staticmethod
    def space_guard_check(settings):
        from .volume_guard import check_drive_buffers
        return check_drive_buffers(settings)

    def save_preferences(self, settings, *, _force_startup_launch=False):
        candidate = self.preference_store.validate(settings)
        with self.preference_store.lock:
            previous = self.preferences()
            receipt = None
            if _force_startup_launch or candidate.get('launch_at_sign_in', False) != previous.get('launch_at_sign_in', False):
                receipt = self._startup_launch_service().set_enabled(candidate['launch_at_sign_in'])
            try:
                return self.preference_store.save(candidate)
            except Exception as error:
                if receipt is not None:
                    try:
                        self._startup_launch_service().rollback(receipt)
                    except Exception as rollback_error:
                        raise RuntimeError('Settings were not saved and the sign-in task rollback could not be verified. Check sign-in launch status before retrying.') from rollback_error
                raise error

    def _startup_launch_service(self):
        from .startup_registration import StartupRegistration
        if not hasattr(self, '_startup_registration'):
            self._startup_registration = StartupRegistration(self.data_dir)
        return self._startup_registration

    def startup_launch_status(self):
        return self._startup_launch_service().status()

    def set_startup_launch(self, enabled):
        if type(enabled) is not bool:
            raise ValueError('Sign-in launch must be on or off.')
        settings = self.preferences()
        settings['launch_at_sign_in'] = enabled
        self.save_preferences(settings, _force_startup_launch=True)
        return self.startup_launch_status()

    def optimize(self, settings=None, progress_callback=None, cancel_event=None, *, browser_prompt=None):
        return self.workflow.run(settings or self.preferences(), progress_callback or (lambda event: None), cancel_event,
                                 browser_prompt=browser_prompt)

    def workflow_navigation_status(self):
        navigation = getattr(self.workflow, 'navigation', None)
        return navigation.snapshot() if navigation is not None else {'active': False, 'session_id': None, 'jobs': []}

    def request_workflow_navigation(self, session_id, job_id, revision, action):
        navigation = getattr(self.workflow, 'navigation', None)
        if navigation is None:
            return {'accepted': False, 'message': 'This workflow does not expose active worker navigation.'}
        return navigation.request(session_id, job_id, revision, action)

    def available_fixes(self, report, settings):
        from .post_run_fixes import available_fixes
        return available_fixes(self, report, settings)

    def apply_available_fixes(self, report, settings, *, progress_callback=None, cancel_event=None):
        from .post_run_fixes import apply_available_fixes
        return apply_available_fixes(self, report, settings, progress_callback=progress_callback, cancel_event=cancel_event)

    def _speed_enhancements_service(self):
        if not hasattr(self, '_speed_service'):
            from .speed_enhancements import SpeedEnhancements
            self._speed_service = SpeedEnhancements(self)
        return self._speed_service

    def speed_enhancements_scan(self):
        return self._speed_enhancements_service().scan()

    def speed_enhancements_history(self):
        return self._speed_enhancements_service().history()

    def _apply_speed_enhancements_stage(self, settings, *, progress_callback=None, cancel_event=None):
        # Workflow already owns the global maintenance lease. Standalone callers
        # enter through apply_speed_enhancements so they cannot overlap a run.
        return self._speed_enhancements_service().apply(settings, progress_callback=progress_callback,
                                                       cancel_event=cancel_event)

    def _speed_maintenance(self, operation):
        from .automation import RunLease
        if not self.workflow.lock.acquire(blocking=False):
            raise RuntimeError('Wait for the current optimization or speed operation to finish.')
        lease = None
        try:
            lease = RunLease(self.data_dir).acquire()
            self.workflow.active = True
            return operation()
        finally:
            try:
                if lease is not None:
                    lease.release()
            finally:
                self.workflow.active = False
                self.workflow.lock.release()

    def apply_speed_enhancements(self, settings=None, *, progress_callback=None, cancel_event=None):
        configuration = self.preference_store.validate(self.preferences() if settings is None else settings)
        return self._speed_maintenance(lambda: self._apply_speed_enhancements_stage(configuration,
            progress_callback=progress_callback, cancel_event=cancel_event))

    def restore_speed_enhancements(self, run_id, *, progress_callback=None, cancel_event=None):
        return self._speed_maintenance(lambda: self._speed_enhancements_service().restore(run_id,
            progress_callback=progress_callback, cancel_event=cancel_event))

    def verify_speed_enhancements(self, report, *, cancel_event=None, timeout=30):
        return self._speed_enhancements_service().verify(report, cancel_event=cancel_event, timeout=timeout)

    def _windows_compatibility_service(self):
        if not hasattr(self, '_windows_compatibility'):
            from .windows_compatibility import WindowsCompatibility
            self._windows_compatibility = WindowsCompatibility(self)
        return self._windows_compatibility

    def _drive_optimization_service(self):
        if not hasattr(self, '_drive_optimizer'):
            from .drive_optimization import DriveOptimization
            self._drive_optimizer = DriveOptimization(self)
        return self._drive_optimizer

    def drive_optimization_scan(self):
        return self._drive_optimization_service().scan()

    def _apply_drive_optimization_stage(self, settings, *, cancel_event=None, progress_callback=None):
        return self._drive_optimization_service().apply(settings, cancel_event=cancel_event,
                                                        progress_callback=progress_callback)

    def optimize_drives(self, settings=None, *, cancel_event=None, progress_callback=None):
        configuration = self.preference_store.validate(self.preferences() if settings is None else settings)
        return self._speed_maintenance(lambda: self._apply_drive_optimization_stage(configuration,
            cancel_event=cancel_event, progress_callback=progress_callback))

    def verify_drive_optimization(self, report, *, cancel_event=None, timeout=30):
        return self._drive_optimization_service().verify(report, cancel_event=cancel_event, timeout=timeout)

    def windows_compatibility_status(self):
        return self._windows_compatibility_service().status()

    def _prepare_windows_compatibility_stage(self, settings, *, cancel_event=None):
        return self._windows_compatibility_service().apply(settings, cancel_event=cancel_event)

    def prepare_windows_compatibility(self):
        return self._speed_maintenance(lambda: self._prepare_windows_compatibility_stage(self.preferences()))

    def verify_windows_compatibility(self, report, *, cancel_event=None, timeout=30):
        return self._windows_compatibility_service().verify(report, cancel_event=cancel_event, timeout=timeout)

    def verify_optimization(self, report, cancel_event=None, timeout=30):
        from .verification import OutcomeVerification
        active = getattr(getattr(self, 'workflow', None), 'last_report', None)
        same_session = active is report or isinstance(active, dict) and active.get('id') == report.get('id')
        return OutcomeVerification(self.data_dir, sampler_status=self.sampler.status if same_session else None,
                                   speed_reader=getattr(self, 'verify_speed_enhancements', None),
                                   compatibility_reader=getattr(self, 'verify_windows_compatibility', None),
                                   drive_reader=getattr(self, 'verify_drive_optimization', None)).verify(
            report, cancel_event=cancel_event, timeout=timeout)

    def _privacy_service(self):
        from .system_privacy import SystemPrivacy
        if not hasattr(self, '_system_privacy_service'):
            self._system_privacy_service = SystemPrivacy(self.data_dir)
        return self._system_privacy_service

    def apply_system_privacy(self, settings, cancel_event=None, *, progress_callback=None):
        return self._privacy_service().apply_privacy(settings, cancel_event=cancel_event, progress_callback=progress_callback)

    def restore_system_privacy(self, change_id, cancel_event=None):
        return self._privacy_service().restore_privacy(change_id, cancel_event=cancel_event)

    def system_privacy_history(self):
        return self._privacy_service().history()

    def scan_spyware(self, cancel_event=None):
        return self._privacy_service().scan_spyware(cancel_event=cancel_event)

    def security_status(self):
        return self._privacy_service().security_status()

    def windows_security_updates(self, settings, cancel_event=None, item_callback=None):
        from .security_updates import SecurityUpdates
        return SecurityUpdates(self.data_dir).run(settings, cancel_event=cancel_event, item_callback=item_callback)

    def legacy_patch_review(self, context=None):
        from .legacy_support import review_legacy_support
        errors = []
        try:
            applications = self.installed_apps()
        except Exception as error:
            applications = []
            errors.append('Installed applications could not be read: ' + type(error).__name__)
        try:
            devices = self.bridge.request('driver_device_inventory', timeout=120)
        except Exception as error:
            devices = {}
            errors.append('Device inventory could not be read: ' + type(error).__name__)
        report = review_legacy_support(context, applications, devices)
        report['errors'] = list(report.get('errors', [])) + errors
        report['failed_items'] = int(report.get('failed_items', 0)) + len(errors)
        self._last_legacy_patch_review = report
        return report

    def apply_security_recommendation(self, recommendation_id, cancel_event=None):
        if recommendation_id == 'review_legacy_patches':
            if cancel_event is not None and cancel_event.is_set():
                return {'cancelled': True, 'review_only': True}
            return getattr(self, '_last_legacy_patch_review', None) or self.legacy_patch_review()
        return self._privacy_service().apply_recommendation(recommendation_id, cancel_event=cancel_event)

    def browser_cleanup_status(self, browser_id):
        return self.browser_sessions.browser_status(browser_id)

    def browser_cleanup_close(self, browser_id, cancel_event=None):
        return self.browser_sessions.close_browser(browser_id, cancel_event=cancel_event)

    def audit(self):
        from . import __version__
        snapshot = self.bridge.request('audit')
        if not isinstance(snapshot, dict):
            raise RuntimeError('The Windows audit returned no usable report.')
        self.last_audit = dict(snapshot, EngineVersion=snapshot.get('ToolVersion'),
                               ToolVersion=__version__, ApplicationVersion=__version__)
        return self.last_audit

    def processes(self):
        return performance.list_performance_candidates()

    def process_harness_scan(self, cancel_event=None):
        return self.process_harness.scan(cancel_event=cancel_event)

    def process_harness_apply(self, request, cancel_event=None):
        return self.process_harness.apply(request, cancel_event=cancel_event)

    def process_harness_export(self, plan_id, destination):
        return self.process_harness.export(plan_id, destination)

    def slowdown_report(self, cancel_event=None):
        from .slowdown import analyze_slowdowns
        return analyze_slowdowns(cancel_event=cancel_event)

    def set_priority(self, record, priority='High'):
        return self.performance_controller.set_process_priority(record, priority)

    def sleep_process(self, record):
        return self.performance_controller.sleep_process(record)

    def wake_process(self, record):
        return self.performance_controller.wake_process(record)

    def wake_all(self):
        return self.performance_controller.wake_all()

    def suspended(self):
        return self.performance_controller.list_suspended()

    def codex_profile(self, priority='High'):
        return self.performance_controller.performance_profile(priority=priority)

    def analysis_start(self, interval=5):
        return self.sampler.start(interval)

    def analysis_stop(self):
        return self.sampler.stop()

    def analysis_status(self):
        return self.sampler.status()

    def set_affinity(self, record, cores):
        return self.performance_controller.set_affinity(record, cores)

    def process_details(self, record):
        local = performance.get_process_details(record)
        local['launch_evidence'] = self.bridge.request('process_details', record=record)
        return local

    def cleanup_categories(self):
        return self.cleaner.categories()

    def cleanup_scan(self, category_ids=None, min_age_days=7, *, progress_callback=None):
        if category_ids is None:
            category_ids = ['user-temp']
        return self.cleaner.scan(list(category_ids), int(min_age_days), progress_callback=progress_callback)

    def cleanup_apply(self, plan, *, progress_callback=None):
        return self.cleaner.apply(plan, progress_callback=progress_callback)

    def storage_scan(self, root):
        return self.bridge.request('storage_scan', root=str(root), timeout=120)

    def duplicate_scan(self, root):
        return self.bridge.request('duplicate_scan', root=str(root), timeout=120)

    def duplicate_recycle(self, record):
        return self.bridge.request('duplicate_recycle', record=record)

    def duplicate_recycle_approved(self, record):
        approved_record = dict(record, AutomaticApproved=True)
        return self.bridge.request('duplicate_recycle', record=approved_record)

    def startup_list(self):
        return self._list(self.bridge.request('startup_list'))

    def startup_set(self, record, enabled):
        return self.bridge.request('startup_set', record=record, enabled=bool(enabled))

    def startup_remove(self, record):
        return self.bridge.request('startup_remove', record=record)

    def services(self):
        return self._list(self.bridge.request('services'))

    def service_set(self, record, action):
        return self.bridge.request('service_set', record=record, action=action)

    def installed_apps(self):
        return self._list(self.bridge.request('installed_apps'))

    def uninstall(self, record):
        current = self.installed_apps()
        if not any(item.get('Identity') == record.get('Identity') and item.get('Name') == record.get('Name') for item in current):
            raise ValueError('The installed-program selection changed. Refresh first.')
        os.startfile('ms-settings:appsfeatures')
        return {'opened': 'Windows Installed apps', 'search': record.get('Name'), 'note': 'Use the registered Windows uninstall action for this application.'}

    def updates_scan(self):
        return self.maintenance.updates_scan()

    def updates_scan_cancellable(self, cancel_event=None):
        return self.maintenance.updates_scan(cancel_event=cancel_event)

    def recheck_held_updates(self, packages, *, cancel_event=None, item_callback=None):
        return self.maintenance.recheck_held_updates(packages, cancel_event=cancel_event, item_callback=item_callback)

    def update_app(self, package_id):
        return self.maintenance.updates_apply(package_id, confirmed=True)

    def update_app_unattended(self, package_id, *, cancel_event=None, compatibility_mode=False, progress_callback=None):
        """Saved scope/agreements authorize updates; cancellation still gates installation."""
        from .maintenance import UpdatePreflightError
        before_install = {'installer_started': False, 'outcomeKnown': True, 'running': False}
        try:
            readiness = self.update_disk_readiness()
            if not isinstance(readiness, dict) or type(readiness.get('ready')) is not bool:
                raise ValueError('Invalid installation-space metadata.')
        except Exception as error:
            raise UpdatePreflightError('Installation-space checks failed before any installer started (' + type(error).__name__ + ').') from error
        if readiness['ready'] is not True:
            return {'skipped': True, 'requiresReview': True, 'exitCode': 0,
                    'status': 'deferred', 'note': readiness.get('reason', 'Installation space could not be established.'), 'disk_readiness': readiness, **before_install}
        options = {'cancel_event': cancel_event} if cancel_event is not None else {}
        if progress_callback is not None:
            options['progress_callback'] = progress_callback
        if type(compatibility_mode) is not bool:
            raise UpdatePreflightError('Choose an explicit compatibility mode.')
        if compatibility_mode:
            from .automation import CURRENT_SETUP_VERSION
            try:
                saved = self.preferences()
                if not isinstance(saved, dict):
                    raise ValueError('Invalid saved compatibility metadata.')
            except Exception as error:
                raise UpdatePreflightError('Saved compatibility choices could not be verified before any installer started.') from error
            if saved.get('update_compatibility_mode_enabled') is not True or saved.get('setup_version') != CURRENT_SETUP_VERSION:
                return {'skipped': True, 'requiresReview': True, 'exitCode': None,
                        'status': 'deferred', 'note': 'The saved non-security compatibility choice is off or setup is incomplete.', **before_install}
            try:
                current_security = self.security_status()
            except Exception:
                current_security = {}
            if not isinstance(current_security, dict) or current_security.get('reboot_required') is not False:
                return {'skipped': True, 'requiresReview': True, 'exitCode': None, 'status': 'deferred',
                        'note': 'A Windows restart is pending or its state could not be verified. Compatibility mode did not start an installer.', **before_install}
            options['compatibility_mode'] = True
        return self.maintenance.updates_apply(package_id, confirmed=True, unattended=True, **options)

    def app_presence_inspect(self, package_id, source='winget'):
        """Read-only exact catalog and installed identity; grants no apply authority."""
        from .app_presence import WindowsAppPresence
        return WindowsAppPresence(self).inspect(package_id, source)

    def app_presence_capabilities(self):
        from .app_presence import SUPPORTED_INSTALLER_TYPES
        return {'source': 'winget', 'supported': True,
                'installer_types': list(SUPPORTED_INSTALLER_TYPES),
                'note': 'Exact verifiable catalog releases only; quiet install/uninstall runs inside the software worker. Unsupported or ambiguous metadata needs review. This does not block other applications from reinstalling software.'}

    def update_disk_readiness(self):
        """Windows installers need system-drive space even when Optimus is on D:."""
        import shutil
        minimum = 2 * 1024 ** 3
        try:
            drive = os.environ.get('SystemDrive', 'C:') + os.sep
            free = shutil.disk_usage(drive).free
            return {'ready': free >= minimum, 'free_bytes': free, 'minimum_free_bytes': minimum,
                    'reason': 'System-drive free space is below the 2 GiB installation floor. Free space before retrying updates.' if free < minimum else 'System-drive installation-space floor passed; individual installers can require more.'}
        except (OSError, ValueError):
            return {'ready': False, 'reason': 'System-drive free space could not be verified; update installation was deferred.'}

    def drivers_list(self):
        return self._list(self.bridge.request('drivers_list'))

    @staticmethod
    def _driver_discovery_progress(progress_callback, phase, percent=None):
        if progress_callback is not None and not callable(progress_callback):
            raise ValueError('An optional driver discovery callback must be callable.')
        if progress_callback is not None:
            try:
                progress_callback({'phase': phase, 'percent': percent})
            except Exception:
                pass  # A display observer cannot abandon or replay discovery.

    def _driver_updates_scan_raw(self, *, progress_callback=None):
        self._driver_discovery_progress(progress_callback, 'scan')
        options = ({'progress_callback': progress_callback, 'reportProgress': True}
                   if progress_callback is not None else {})
        try:
            scan = self.bridge.request('driver_updates_scan', timeout=300, **options)
        except Exception as error:
            return {'updates': [], 'resultCode': 4, 'source': 'Windows Update', 'error': str(error)}
        if not isinstance(scan, dict):
            return {'updates': [], 'resultCode': 4, 'source': 'Windows Update',
                    'error': 'Windows Update returned no usable scan report.'}
        return scan

    def driver_updates_scan(self, *, progress_callback=None):
        self._driver_discovery_progress(progress_callback, 'scan')
        options = {'progress_callback': progress_callback} if progress_callback is not None else {}
        scan = dict(self._driver_updates_scan_raw(**options))
        scan['coverage'] = self.driver_coverage(scan, **options)
        from .driver_coverage import driver_scan_evidence
        evidence = driver_scan_evidence(scan)
        complete = (evidence['complete'] and scan['coverage'].get('inventoryComplete') is True
                    and not scan['coverage'].get('inventoryErrors'))
        self._driver_discovery_progress(progress_callback, 'verify', 100 if complete else None)
        return scan

    def driver_devices(self):
        """Setup inventory only; no online Windows Update scan or changes."""
        return self.driver_coverage({'updates': [], 'resultCode': None,
                                     'source': 'Not scanned (device inventory only)'})


    def driver_coverage(self, scan=None, *, progress_callback=None):
        """Present devices, device problems and exact WUA matches; no changes."""
        from .driver_coverage import build_driver_coverage
        self._driver_discovery_progress(progress_callback, 'device_query')
        options = ({'progress_callback': progress_callback, 'reportProgress': True}
                   if progress_callback is not None else {})
        if scan is None:
            scan = self._driver_updates_scan_raw(**({'progress_callback': progress_callback}
                                                   if progress_callback is not None else {}))
        try:
            inventory = self.bridge.request('driver_device_inventory', timeout=120, **options)
        except Exception as error:
            inventory = {'complete': False, 'devices': [],
                         'errors': [{'source': 'Device inventory', 'error': str(error)}]}
        self._driver_discovery_progress(progress_callback, 'verify')
        return build_driver_coverage(inventory, scan)


    def is_admin(self):
        return bool(ctypes.windll.shell32.IsUserAnAdmin())

    def driver_backup_automatic(self):
        import uuid
        return self.backup_drivers(self.data_dir / 'driver-backups' / uuid.uuid4().hex)

    def driver_backup_for_updates(self, offers, cancel_event=None, item_callback=None):
        """Export and verify only current OEM packages replaced by exact offers."""
        from .driver_update_backup import DriverUpdateBackup
        return DriverUpdateBackup(self).backup(offers, cancel_event=cancel_event, item_callback=item_callback)

    def driver_update_apply_unattended(self, record, accept_eula=False, *, progress_callback=None):
        if accept_eula is not True:
            raise ValueError('Saved driver agreement approval is required.')
        options = {}
        if progress_callback is not None:
            if not callable(progress_callback):
                raise ValueError('An optional driver progress callback must be callable.')
            options.update(progress_callback=progress_callback, reportProgress=True)
        result = self.bridge.request('driver_update_apply', id=record['id'], revision=record['revision'],
                                     unattended=True, acceptEula=True, record=record, timeout=900, **options)
        if isinstance(result, dict) and isinstance(result.get('download_progress'), dict):
            from .item_progress import normalize_item_progress
            observed = normalize_item_progress(dict(result['download_progress'],
                key='driver:update:' + str(record['id']) + ':' + str(record['revision']),
                label=str(record.get('title') or record['id'])))
            result = dict(result)
            if observed is not None and 'downloaded_bytes' in observed:
                result['download_progress'] = observed
            else:
                result.pop('download_progress', None)
        return result

    def driver_update_apply(self, record):
        return self.bridge.request('driver_update_apply', id=record['id'], revision=record['revision'], timeout=900)

    def backup_drivers(self, destination):
        destination = Path(destination)
        destination.mkdir(parents=True, exist_ok=True)
        result = subprocess.run([str(Path(os.environ['SystemRoot']) / 'System32/pnputil.exe'), '/export-driver', '*', str(destination)],
                                capture_output=True, text=True, errors='replace', creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0), timeout=300)
        return {'exit_code': result.returncode, 'output': result.stdout + result.stderr, 'destination': str(destination)}

    def debloat_actions(self):
        return self.maintenance.debloat_actions()

    def debloat_preview(self, action):
        return self.maintenance.debloat_preview(action)

    def debloat_apply(self, plan):
        return self.maintenance.debloat_apply(plan)

    def changes(self):
        return (self._list(self.bridge.request('changes')) + self.performance_controller.allocation_history()
                + self._windows_compatibility_service().history())

    def restore(self, change):
        if change.get('source') == 'python-compatibility':
            return self._speed_maintenance(lambda: self._windows_compatibility_service().restore(change.get('Id') or change.get('id')))
        if change.get('source') == 'python-allocation':
            return self.performance_controller.restore_allocation(change.get('Id') or change.get('id'))
        return self.bridge.request('restore', change=change)

    def registry_scan(self):
        return self.bridge.request('registry_scan')

    def registry_remove(self, record):
        return self.bridge.request('registry_remove', record=record)

    def registry_restore(self, change):
        return self.bridge.request('registry_restore', change=change)

    def power_plans(self):
        return self._list(self.bridge.request('power_plans'))

    def set_power_plan(self, guid):
        return self.bridge.request('set_power_plan', guid=guid)

    def schedule_list(self):
        return self.maintenance.schedules_list()

    def create_schedule(self, name='Daily audit', time='18:00', mode='audit'):
        if mode != 'audit':
            raise ValueError('Scheduled jobs currently run read-only audits.')
        hour, minute = map(int, time.split(':'))
        return self.maintenance.schedule_create(hour=hour, minute=minute, confirmed=True)

    def remove_schedule(self, name):
        return self.maintenance.schedule_delete(name, confirmed=True)

    def cloud_status(self):
        return self.cloud.status()

    def cloud_configure(self, client_id, tenant='consumers'):
        return self.cloud.configure(client_id, tenant)

    def cloud_connect(self):
        return self.cloud.begin_connect()

    def cloud_poll(self, session_id):
        return self.cloud.poll_connect(session_id)

    def cloud_logout(self):
        return self.cloud.logout()

    def cloud_scan(self, folder_id='root'):
        return self.cloud.scan(folder_id)

    def cloud_approval_scope(self, folder_id='root'):
        return self.cloud.approval_scope(folder_id)

    def cloud_recycle_approved(self, plan, policy, cancel_event=None, item_callback=None):
        return self.cloud.recycle_approved(plan, policy, cancel_event=cancel_event, item_callback=item_callback)

    def cloud_recycle(self, plan):
        return self.cloud.recycle(plan)

    def privacy_profiles(self):
        return self.browser_privacy_engine.profiles()

    def privacy_scan(self, profile_id, categories=None, keep_domains=None, *, browser_prompt=None, cancel_event=None):
        if browser_prompt is not None:
            profile = self.browser_privacy_engine._profile(profile_id)
            identifiers = {'Microsoft Edge': 'edge', 'Google Chrome': 'chrome', 'Brave': 'brave',
                           'Vivaldi': 'vivaldi', 'Mozilla Firefox': 'firefox', 'Opera': 'opera'}
            browser_id = identifiers.get(profile['browser'])
            if not browser_id:
                raise ValueError('This browser is not supported by the close-and-clean prompt.')
            state = self.browser_cleanup_status(browser_id)
            def skip(note):
                return {'skipped': True, 'canApply': False,
                        'profile': {'browser': profile['browser'], 'name': profile['name']},
                        'counts': {}, 'note': note}
            if cancel_event is not None and cancel_event.is_set():
                return skip('Cancelled; this browser was not cleaned.')
            if state.get('complete') is not True or type(state.get('running')) is not bool:
                return skip('Browser process status could not be verified; this browser was skipped.')
            if state['running']:
                question = {'browser_id': browser_id, 'browser': profile['browser'],
                            'processes': [{'pid': item.get('pid'), 'name': item.get('name')} for item in state.get('processes', [])],
                            'categories': list(categories or []), 'profile_count': 1}
                if browser_prompt(question) is not True:
                    return skip('You chose to skip this open browser. It was left running and no data was cleaned.')
                if cancel_event is not None and cancel_event.is_set():
                    return skip('Cancelled before closing the browser; no data was cleaned.')
                result = self.browser_cleanup_close(browser_id, cancel_event=cancel_event)
                if result.get('closed') is not True or result.get('complete') is not True:
                    return skip('The browser did not close completely; it was skipped without forcing it to exit.')
        return self.browser_privacy_engine.scan(profile_id, categories=categories or [], keep_domains=keep_domains or [])

    def privacy_apply(self, plan):
        return self.browser_privacy_engine.apply(plan)

    def privacy_backups(self):
        return self.browser_privacy_engine.list_backups()

    def privacy_restore(self, backup_id, confirmed=False):
        return self.browser_privacy_engine.restore_backup(backup_id, confirmed=confirmed)

    def browser_privacy(self, browser_id='edge'):
        # Browser-native clearing preserves choices about passwords, logins and history.
        browsers = {
            'edge': ([Path(os.environ.get('ProgramFiles(x86)', r'C:\Program Files (x86)')) / 'Microsoft/Edge/Application/msedge.exe', Path(os.environ.get('ProgramFiles', r'C:\Program Files')) / 'Microsoft/Edge/Application/msedge.exe'], 'edge://settings/clearBrowserData'),
            'chrome': ([Path(os.environ.get('ProgramFiles', r'C:\Program Files')) / 'Google/Chrome/Application/chrome.exe', Path(os.environ['LOCALAPPDATA']) / 'Google/Chrome/Application/chrome.exe'], 'chrome://settings/clearBrowserData'),
            'brave': ([Path(os.environ.get('ProgramFiles', r'C:\Program Files')) / 'BraveSoftware/Brave-Browser/Application/brave.exe'], 'brave://settings/clearBrowserData'),
            'firefox': ([Path(os.environ.get('ProgramFiles', r'C:\Program Files')) / 'Mozilla Firefox/firefox.exe'], 'about:preferences#privacy'),
        }
        if browser_id not in browsers:
            raise ValueError('Unknown browser.')
        candidates, url = browsers[browser_id]
        executable = next((path for path in candidates if path.is_file()), None)
        if not executable:
            raise RuntimeError('This browser was not found in its standard installation locations.')
        subprocess.Popen([str(executable), url])
        return {'opened': str(executable), 'page': url}

    def system_tool(self, tool_id):
        aliases = {'installed-apps': 'apps', 'windows-update': 'updates', 'device-manager': 'devices', 'task-scheduler': 'tasks', 'defrag': 'drives'}
        tool_id = aliases.get(tool_id, tool_id)
        settings = {'storage': 'ms-settings:storagesense', 'updates': 'ms-settings:windowsupdate', 'startup': 'ms-settings:startupapps',
                    'apps': 'ms-settings:appsfeatures', 'recovery': 'ms-settings:recovery', 'privacy': 'ms-settings:privacy-general', 'optional-updates': 'ms-settings:windowsupdate-optionalupdates', 'store-updates': 'ms-windows-store://downloadsandupdates'}
        programs = {'resource-monitor': 'System32/resmon.exe', 'drives': 'System32/dfrgui.exe', 'services': 'System32/services.msc',
                    'tasks': 'System32/taskschd.msc', 'devices': 'System32/devmgmt.msc', 'system-restore': 'System32/rstrui.exe'}
        if tool_id in settings:
            os.startfile(settings[tool_id])
        elif tool_id in programs:
            os.startfile(str(Path(os.environ['SystemRoot']) / programs[tool_id]))
        else:
            raise ValueError('Unknown Windows tool.')
        return {'opened': tool_id}

    def export_report(self, destination):
        snapshot = self.audit()
        destination = Path(destination)
        destination.mkdir(parents=True, exist_ok=True)
        (destination / 'audit.json').write_text(json.dumps(snapshot, indent=2), encoding='utf-8')
        return {'path': str(destination / 'audit.json')}

    def close(self):
        self._close_optional_tools()
        self.sampler.stop()
        self.bridge.close()





