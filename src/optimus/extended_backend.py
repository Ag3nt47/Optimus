"""Lazy optional tools shared by native backends; no monitor starts implicitly."""
from __future__ import annotations

import threading
import time

_CREATION = threading.RLock()


class _Cancellation:
    def __init__(self, *events):
        self.events = events

    def is_set(self):
        return any(event is not None and event.is_set() for event in self.events)

    def wait(self, timeout=None):
        deadline = None if timeout is None else time.monotonic() + max(0, timeout)
        sleeper = threading.Event()
        while not self.is_set():
            remaining = None if deadline is None else deadline - time.monotonic()
            if remaining is not None and remaining <= 0:
                return False
            sleeper.wait(.05 if remaining is None else min(.05, remaining))
        return True


class ExtendedMaintenanceBackend:
    def _toolkit(self):
        from .maintenance_toolkit import MaintenanceToolkit
        return self._optional_service('_maintenance_toolkit', lambda _: MaintenanceToolkit(self))

    def configure_toolkit(self, choices):
        from .toolkit_configuration import validate_configuration
        settings = self.preferences()
        settings['toolkit_settings'] = validate_configuration(choices)
        self.save_preferences(settings)
        return self._toolkit().status()

    def toolkit_status(self):
        return self._toolkit().status()

    def maintenance_readiness(self, *, network=False):
        return self._toolkit().readiness(network=network)

    def toolkit_check(self, *, cancel_event=None):
        return self._tool_operation('performance_check', 'Sustained performance and system health', self._toolkit().check,
                                    cancel_event=cancel_event, _supports_progress=False)

    def toolkit_observe(self, *, cancel_event=None):
        return self._tool_operation('performance_history', 'Performance observation history', self._toolkit().observe,
                                    cancel_event=cancel_event, _supports_progress=False)

    def toolkit_archive_report(self, *, cancel_event=None):
        if cancel_event is not None and cancel_event.is_set():
            return {'status': 'cancelled', 'note': 'No report copy was archived.'}
        return self._toolkit().archive_last_report()

    def toolkit_preview(self, key, *, cancel_event=None):
        return self._tool_operation('toolkit_' + key, 'Review ' + key.replace('_', ' '), self._toolkit().preview,
                                    key, cancel_event=cancel_event, _supports_progress=False)

    def toolkit_apply(self, key, plan=None, *, cancel_event=None):
        return self._tool_operation('toolkit_' + key, 'Apply ' + key.replace('_', ' '), self._toolkit().apply,
                                    key, plan, cancel_event=cancel_event)

    def toolkit_integrity(self, *, repair=False, approved=False, cancel_event=None):
        return self._tool_operation('windows_integrity', 'Windows integrity ' + ('repair' if repair else 'check'),
                                    self._toolkit().integrity, repair=repair, approved=approved,
                                    cancel_event=cancel_event, _supports_progress=False)

    def pending_updates_barrier(self):
        from .parallel_updates import pending_updates_barrier
        return pending_updates_barrier(self)

    def toolkit_resume(self, *, cancel_event=None):
        from .update_resume import UpdateResumeService
        from .continue_updates import run_saved_updates
        from .automation import RunLease
        with RunLease(self.data_dir) as lease:
            return UpdateResumeService(self).resume(self.preferences(),
                lambda configuration: run_saved_updates(self, cancel_event=cancel_event, _lease=lease), lease=lease)

    def _optional_service(self, name, factory):
        with _CREATION:
            if name not in self.__dict__:
                self.__dict__[name] = factory(self)
            return self.__dict__[name]

    def _worker_registry(self):
        from .worker_status import WorkerStatus
        return self._optional_service('_extra_workers', lambda _: WorkerStatus())

    def worker_status(self):
        registry = self.__dict__.get('_extra_workers')
        return registry.snapshot() if registry is not None else []

    def request_optional_stop(self):
        registry = self.__dict__.get('_extra_workers')
        requested = registry.request_stop() if registry is not None else []
        self.smart_maintenance_stop(wait=False)
        return requested

    def _tool_operation(self, identity, title, operation, *args, **kwargs):
        registry = self._worker_registry()
        supports_progress = kwargs.pop('_supports_progress', True)
        caller_progress = kwargs.pop('progress_callback', None)
        caller_cancel = kwargs.pop('cancel_event', None)
        with registry.operation(identity, title) as (token, stop):
            def progress(event):
                if isinstance(event, dict):
                    details = event.get('description') or event.get('stage') or event.get('message')
                    counts = {}
                    current, total = event.get('current'), event.get('total')
                    if type(current) is int and type(total) is int and 0 <= current <= total:
                        counts = dict(current=current, total=total)
                    registry.update(identity, token, description=str(details or title), **counts)
                if caller_progress is not None:
                    caller_progress(event)
            operation_options = dict(kwargs, cancel_event=_Cancellation(caller_cancel, stop))
            if supports_progress:
                operation_options['progress_callback'] = progress
            result = operation(*args, **operation_options)
            status = result.get('status') if isinstance(result, dict) else None
            terminal = ('completed' if status in ('completed', 'verified') else
                        'cancelled' if status == 'cancelled' else
                        'failed' if status == 'failed' else 'needs_review')
            registry.update(identity, token, status=terminal,
                description=(str(result.get('reason') or result.get('note') or title) if isinstance(result, dict)
                             else 'Review the saved operation result.'))
            return result

    def _driver_recovery_service(self):
        from .driver_backups import DriverBackups
        return self._optional_service('_driver_recovery', DriverBackups)

    def driver_recovery_scan(self):
        return self._driver_recovery_service().scan()

    def driver_backup_export(self, **kwargs):
        return self._tool_operation('driver_backup', 'Driver backup', self._driver_recovery_service().export, **kwargs)

    def driver_backup_verify(self, backup_id, **kwargs):
        return self._tool_operation('driver_backup', 'Verify driver backup', self._driver_recovery_service().verify, backup_id, **kwargs)

    def driver_rollback(self, plan_id, selection_id, **kwargs):
        return self._tool_operation('driver_recovery', 'Driver rollback', self._driver_recovery_service().rollback, plan_id, selection_id, **kwargs)

    def driver_recovery_pending(self):
        return self._driver_recovery_service().pending_barrier()

    def _change_history_service(self):
        from .change_history import ChangeHistory
        return self._optional_service('_change_history', ChangeHistory)

    def change_history_scan(self):
        return self._change_history_service().scan()

    def change_history_revert(self, plan_id, selection_ids, **kwargs):
        return self._tool_operation('revert', 'Revert selected changes',
            self._change_history_service().revert, plan_id, selection_ids, **kwargs)

    def _system_restore_service(self):
        from .system_restore import SystemRestore
        return self._optional_service('_system_restore', SystemRestore)

    def system_restore_scan(self):
        return self._system_restore_service().scan()

    def system_restore_create(self, description, **kwargs):
        return self._tool_operation('system_restore', 'Create restore point', self._system_restore_service().create, description, _supports_progress=False, **kwargs)

    def system_restore_delete(self, plan_id, selection_id, **kwargs):
        return self._tool_operation('system_restore', 'Delete restore point', self._system_restore_service().delete, plan_id, selection_id, _supports_progress=False, **kwargs)

    def system_restore_open(self, plan_id, selection_id, **kwargs):
        # This opens the native review wizard; it does not complete restoration.
        return self._system_restore_service().open_restore(plan_id, selection_id, **kwargs)

    def recovery_mutation_barrier(self):
        """Read recovery receipts without invoking the updater guard recursively."""
        from pathlib import Path
        from .filesystem import _ordinary_data_path
        from .system_diagnostics import integrity_mutation_barrier
        integrity_reason = integrity_mutation_barrier(self.data_dir, reboot_reader=getattr(self, 'security_status', None))
        if integrity_reason:
            return integrity_reason
        try:
            for name in ('driver-backups', 'system-restore'):
                _ordinary_data_path(Path(self.data_dir) / 'recovery' / name)
            _ordinary_data_path(Path(self.data_dir) / 'change-history')
        except (OSError, ValueError) as error:
            return 'Recovery records require review: ' + str(error)[:1000]
        if (Path(self.data_dir) / 'recovery' / 'driver-backups').exists():
            try:
                reason = self.driver_recovery_pending()
                if reason:
                    return reason
            except Exception as error:
                return 'Driver recovery records require review: ' + str(error)[:1000]
        if (Path(self.data_dir) / 'recovery' / 'system-restore').exists():
            try:
                self._system_restore_service()._pending()
            except Exception as error:
                return 'System Restore records require review: ' + str(error)[:1000]
        if (Path(self.data_dir) / 'change-history').exists():
            try:
                reason = self._change_history_service().pending_barrier()
                if reason:
                    return reason
            except Exception as error:
                return 'Revert records require review: ' + str(error)[:1000]
        return None

    def _file_utilities_service(self):
        from .file_utilities import FileUtilities
        return self._optional_service('_file_utilities', FileUtilities)

    def file_utilities_capabilities(self):
        return self._file_utilities_service().capabilities()

    def file_utilities_encrypt(self, *args, **kwargs):
        return self._tool_operation('file_tools', 'Encrypt selected file', self._file_utilities_service().encrypt, *args, **kwargs)

    def file_utilities_decrypt(self, *args, **kwargs):
        return self._tool_operation('file_tools', 'Decrypt selected file', self._file_utilities_service().decrypt, *args, **kwargs)

    def file_utilities_split(self, *args, **kwargs):
        return self._tool_operation('file_tools', 'Split selected file', self._file_utilities_service().split, *args, **kwargs)

    def file_utilities_join(self, *args, **kwargs):
        return self._tool_operation('file_tools', 'Join verified file parts', self._file_utilities_service().join, *args, **kwargs)

    def file_utilities_shred_plan(self, *args, **kwargs):
        return self._tool_operation('file_tools', 'Review selected overwrite targets', self._file_utilities_service().shred_plan, *args, **kwargs)

    def file_utilities_shred_apply(self, *args, **kwargs):
        return self._tool_operation('file_tools', 'Overwrite selected files', self._file_utilities_service().shred_apply, *args, **kwargs)

    def _smart_service(self):
        from .smart_maintenance import SmartMaintenance
        return self._optional_service('_smart_maintenance', SmartMaintenance)

    def smart_maintenance_status(self):
        service = self.__dict__.get('_smart_maintenance')
        return service.status() if service is not None else {
            'status': 'stopped', 'running': False, 'note': 'Monitoring awaits launch approval and saved opt-in settings.'}

    def smart_maintenance_preview(self, *, cancel_event=None):
        return self._smart_service().preview(cancel_event=cancel_event)

    def configure_smart_maintenance(self, patch):
        from .smart_maintenance import SMART_DEFAULTS, validate_smart_configuration
        if not isinstance(patch, dict) or set(patch) - set(SMART_DEFAULTS):
            raise ValueError('Only Smart Maintenance settings may be changed here.')
        merged = self.preferences()
        merged.update(patch)
        merged.update(validate_smart_configuration(merged))
        self.save_preferences(merged)
        # Saving settings does not grant session consent or start a monitor.
        return self._smart_service().configure(self.preferences())

    def activate_smart_maintenance(self, busy_reader=None):
        service = self._smart_service()
        if busy_reader is not None:
            service.busy_reader = busy_reader
        return service.start()

    def smart_maintenance_stop(self, *, wait=True, timeout=None):
        service = self.__dict__.get('_smart_maintenance')
        return service.stop(wait=wait, timeout=timeout) if service is not None else self.smart_maintenance_status()

    def _close_optional_tools(self):
        registry = self.__dict__.get('_extra_workers')
        if registry is not None:
            registry.request_stop()
        self.smart_maintenance_stop(wait=True)
