"""One shared facade for opt-in workflow stages, UI tools and read-only checks."""
from __future__ import annotations

import copy
import os
from pathlib import Path
import tempfile
import threading
import time
import uuid

from .automation import RunLease
from .toolkit_configuration import validate_configuration, launch_descriptions

CACHE_STAGE = 'Clean selected application caches'
RETENTION_STAGE = 'Expire selected recovery and report data'
PROVIDER_STAGE = 'Update approved Chocolatey packages'
BEFORE_STAGE = 'Observe sustained resources before maintenance'
AFTER_STAGE = 'Observe sustained resources after maintenance'
READ_ONLY_STAGES = frozenset((BEFORE_STAGE, AFTER_STAGE, 'Review hardware, boot and memory health',
                              'Check installer volume headroom', 'Compare selected folder growth'))


class MaintenanceToolkit:
    def __init__(self, backend):
        self.backend = backend
        self._services = {}
        self._state_lock = threading.RLock()
        self._readiness_cache = {}
        self._archivable_report = None

    def configuration(self):
        return validate_configuration(self.backend.preferences().get('toolkit_settings', {}))

    def service(self, key):
        with self._state_lock:
            if key not in self._services:
                from .application_caches import ApplicationCacheCleaner
                from .storage_growth import StorageGrowthTracker
                from .recovery_retention import RecoveryRetention
                from .performance_history import PerformanceHistory
                from .additional_packages import AdditionalPackages
                factories = {
                    'application_caches': lambda: ApplicationCacheCleaner(self.backend.data_dir, scope_reader=lambda: self.configuration()['application_caches']),
                    'storage_growth': lambda: StorageGrowthTracker(self.backend.data_dir),
                    'recovery_retention': lambda: RecoveryRetention(self.backend.data_dir, scope_reader=lambda: self.configuration()['recovery_retention']),
                    'history': lambda: PerformanceHistory(self.backend.data_dir),
                    'providers': lambda: AdditionalPackages(self.backend),
                }
                self._services[key] = factories[key]()
            return self._services[key]

    def readiness(self, *, network=False, force=False):
        from .adaptive_maintenance import collect_state, assess
        config = self.configuration()['adaptive']
        if not config['enabled']:
            return assess(config, {}, network=network)
        with self._state_lock:
            cache_key = (network, tuple(sorted(config.items())))
            cached = self._readiness_cache.get(cache_key)
            if not force and cached is not None and time.monotonic() - cached[0] < 3:
                return copy.deepcopy(cached[1])
        reader = None
        if network and config['avoid_metered'] and getattr(self.backend, 'system', 'win32') == 'win32':
            reader = lambda: self.backend.bridge.request('adaptive_network', timeout=10)
        result = assess(config, collect_state(network_reader=reader), network=network)
        with self._state_lock:
            self._readiness_cache = {cache_key: (time.monotonic(), copy.deepcopy(result))}
        return result

    def observe(self, *, phase='manual', configuration=None, cancel_event=None):
        from .performance_history import collect_resource_window
        config = self.configuration() if configuration is None else configuration
        window = collect_resource_window(config['history']['sample_seconds'], cancel_event=cancel_event)
        value = {'schema': 1, 'read_only': True, 'window': window, 'history_saved': False,
                 'status': 'completed' if window.get('complete') is True else 'cancelled' if window.get('cancelled') is True else 'needs_review'}
        if (config['history']['enabled'] and window.get('complete') is True
                and self.configuration()['history'] == config['history']):
            from .adaptive_maintenance import collect_state
            state = collect_state()
            idle = state.get('idle_seconds')
            context = {'power_source': 'ac' if state['ac_power'] is True else 'battery' if state['ac_power'] is False else 'unknown',
                       'activity_mode': 'idle' if type(idle) in (int, float) and idle >= 60 else 'active' if type(idle) in (int, float) else 'unknown'}
            value.update(history=self.service('history').record(window, phase=phase, context=context), history_saved=True)
        return value

    def diagnostics(self, *, cancel_event=None):
        from .system_diagnostics import collect_system_diagnostics
        return collect_system_diagnostics(self.backend, cancel_event=cancel_event)

    def headroom(self, configuration=None):
        from .system_diagnostics import check_installer_headroom
        config = (self.configuration() if configuration is None else configuration)['installer_space']
        temporary = Path(tempfile.gettempdir())
        paths = {'temporary': temporary, 'Optimus data': Path(self.backend.data_dir)}
        if os.name == 'nt':
            import ctypes
            buffer = ctypes.create_unicode_buffer(32768)
            count = ctypes.windll.kernel32.GetWindowsDirectoryW(buffer, len(buffer))
            if 0 < count < len(buffer):
                paths['system'] = Path(buffer.value)
        else:
            paths['system'] = Path('/')
        if config['destination']:
            paths['destination'] = Path(config['destination'])
        # A single entered estimate applies to its destination, rather than
        # pretending the same bytes are written to every staging volume.
        sizes = {key: None for key in paths}
        if 'destination' in paths:
            sizes['destination'] = config['required_bytes']
        return check_installer_headroom(paths, required_bytes=sizes)

    def status(self):
        config = self.configuration()
        actions, checks = launch_descriptions({'toolkit_settings': config})
        return {'schema': 1, 'read_only': True, 'configuration': config, 'actions': actions, 'checks': checks,
                'note': 'New scopes start off. Settings are applied only after an explicit save; Windows repair has a separate confirmation.'}

    def check(self, *, cancel_event=None):
        from .performance_history import collect_resource_window
        config = self.configuration()
        result = {'schema': 1, 'id': uuid.uuid4().hex, 'read_only': True, 'resource_window': collect_resource_window(config['history']['sample_seconds'], cancel_event=cancel_event),
                'diagnostics': self.diagnostics(cancel_event=cancel_event), 'installer_headroom': self.headroom(),
                'adaptive_readiness': self.readiness(force=True), 'maintenance_actions_executed': 0,
                'note': 'Observed performance and native health only. No cleanup, installer, pagefile change or repair was started.'}
        result['status'] = ('cancelled' if result['resource_window'].get('cancelled') is True else
                            'completed' if result['resource_window'].get('complete') is True
                            and result['diagnostics'].get('status') == 'completed'
                            and not result['installer_headroom'].get('errors')
                            and result['installer_headroom'].get('status') != 'insufficient_known_space' else 'needs_review')
        self._remember_report(result, 'health_check')
        return result

    def _remember_report(self, result, kind):
        # Archive accepts only a report produced here, never arbitrary UI input.
        with self._state_lock:
            self._archivable_report = (kind, copy.deepcopy(result))

    def archive_last_report(self):
        with self._state_lock:
            report = copy.deepcopy(self._archivable_report)
        if report is None:
            raise ValueError('Run a complete read-only health check or folder-growth scan first.')
        kind, value = report
        return self.service('recovery_retention').archive_completed_report(value, kind=kind)

    def preview(self, key, *, cancel_event=None):
        config = self.configuration()
        if key == 'application_caches':
            return self.service(key).scan(config[key], cancel_event=cancel_event)
        if key == 'recovery_retention':
            return self.service(key).preview(config[key], cancel_event=cancel_event)
        if key == 'storage_growth':
            result = self.service(key).scan(config[key], cancel_event=cancel_event)
            self._remember_report(result, 'storage_growth')
            return result
        if key == 'providers':
            return self.service(key).scan()
        if key == 'history':
            return self.service(key).report()
        raise ValueError('Choose a supported toolkit preview.')

    def _mutate(self, key, *, lease=None, plan=None, cancel_event=None, progress_callback=None, configuration=None):
        from .parallel_updates import pending_updates_barrier
        config = self.configuration() if configuration is None else validate_configuration(configuration)
        with _lease_context(self.backend.data_dir, lease) as owned:
            if self.configuration() != config:
                return {'skipped': True, 'deferred': True, 'installer_started': False, 'failed_items': 0,
                        'note': 'The saved toolkit scope changed after this run was planned. Review the new choices on a fresh run.'}
            barrier = pending_updates_barrier(self.backend)
            if barrier:
                return {'skipped': True, 'deferred': True, 'installer_started': False, 'failed_items': 0, 'note': barrier}
            assessment = self.readiness(network=key == 'providers')
            if not assessment['allowed']:
                from .adaptive_maintenance import deferred_result
                return deferred_result(assessment)
            options = {'cancel_event': cancel_event, 'progress_callback': progress_callback}
            if key == 'providers':
                return self.service(key).run(config[key], lease=owned, **options)
            if plan is None:
                return self.service(key).run(config[key], **options)
            if key == 'recovery_retention':
                options['confirmed'] = True
            return self.service(key).apply(plan, config[key], **options)

    def apply(self, key, plan=None, **options):
        if key not in ('application_caches', 'recovery_retention', 'providers'):
            raise ValueError('This toolkit action is read-only.')
        return self._mutate(key, plan=plan, **options)

    def integrity(self, *, repair=False, approved=False, cancel_event=None):
        from .parallel_updates import pending_updates_barrier
        from .system_diagnostics import integrity_check_plan, run_windows_integrity
        config = self.configuration()
        plan = integrity_check_plan(config['diagnostics']['integrity_symptoms'], requested=True)
        with RunLease(self.backend.data_dir):
            reason = pending_updates_barrier(self.backend)
            if reason:
                return {'skipped': True, 'status': 'deferred', 'note': reason, 'items': []}
            return run_windows_integrity(self.backend, plan, repair=repair, approved=approved, cancel_event=cancel_event)

    def stages(self, configuration, *, lease, progress_callback=None, cancel_event=None):
        config = validate_configuration(configuration.get('toolkit_settings', {}))
        before, after = [], []
        if config['history']['enabled']:
            before.append((BEFORE_STAGE, 'Collecting comparable sustained observations, with local history.', lambda: self.observe(phase='before', configuration=config, cancel_event=cancel_event)))
            after.append((AFTER_STAGE, 'Collecting final sustained observations; changes are measurements, not a claimed speed gain.', lambda: self.observe(phase='after', configuration=config, cancel_event=cancel_event)))
        if config['diagnostics']['enabled']:
            before.append(('Review hardware, boot and memory health', 'Reading available hardware, boot-delay and pagefile facts.', lambda: self.diagnostics(cancel_event=cancel_event)))
        if config['installer_space']['enabled']:
            before.append(('Check installer volume headroom', 'Comparing real volume space with known requirements; unknown sizes remain unknown.', lambda: self.headroom(config)))
        if config['storage_growth']['enabled']:
            before.append(('Compare selected folder growth', 'Bounded metadata-only measurements of selected local folders.', lambda: self.service('storage_growth').scan(config['storage_growth'], cancel_event=cancel_event)))
        for key, title in (('application_caches', CACHE_STAGE), ('recovery_retention', RETENTION_STAGE), ('providers', PROVIDER_STAGE)):
            if config[key]['enabled']:
                operation = lambda key=key: self._mutate(key, lease=lease, cancel_event=cancel_event,
                    progress_callback=progress_callback, configuration=config)
                before.append((title, 'Applying the exact saved toolkit scope with fresh identity and activity checks.', operation))
        return before, after


class _lease_context:
    def __init__(self, data_dir, supplied):
        self.data_dir, self.supplied = Path(data_dir), supplied
    def __enter__(self):
        if self.supplied is None:
            self.supplied = RunLease(self.data_dir).acquire()
            self.owned = True
        else:
            if not isinstance(self.supplied, RunLease) or self.supplied.handle is None or self.supplied.path != self.data_dir / 'maintenance-run.lock':
                raise ValueError('A current owned maintenance lease is required.')
            self.owned = False
        return self.supplied
    def __exit__(self, *args):
        if self.owned:
            self.supplied.release()
