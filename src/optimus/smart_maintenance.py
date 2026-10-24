"""Opt-in, app-open junk thresholds and observed browser-exit cleanup.

Constructing this service, reading status, and validating configuration perform
no inventory or cleanup. The owner explicitly starts/stops its one low-priority
thread. No service, task, process killer, updater or browser closer is installed.
"""
from __future__ import annotations

import copy
from datetime import datetime, timezone
import json
import math
from pathlib import Path
import re
import threading
import time
import uuid

from .automation import RunLease
from .browser_cleanup import run_browser_cleanup
from .browser_paths import browser_definitions
from .filesystem import _ordinary_data_path
from .parallel_updates import pending_updates_barrier
from .speed_enhancements import _current_owner, _private_json
from .smart_configuration import SMART_DEFAULTS, validate_smart_configuration


_SCOPES = ('cleanup_enabled', 'cleanup_age_days', 'cleanup_category_ids', 'intensity',
           'browser_cache_enabled', 'browser_history_enabled', 'browser_cookies_enabled',
           'browser_download_history_enabled', 'browser_cleanup_browsers', 'browser_keep_domains',
           'toolkit_settings', 'drive_space_guard_enabled', 'drive_space_guard_target_gib')


def _number(value):
    return type(value) in (int, float) and math.isfinite(value) and value >= 0


def _scope(settings):
    return {key: copy.deepcopy(settings.get(key)) for key in (*SMART_DEFAULTS, *_SCOPES)}


class SmartMaintenance:
    """One explicitly started worker; every scan/action owns the maintenance lease.

    Backend requirements: preferences(), cleaner.scan/categories/apply, browser
    cleanup/status/privacy methods already used by run_browser_cleanup. Configure
    validates already-saved preferences; persistence belongs to the backend's
    normal preference transaction. Preview never writes a state file or cleans.
    """
    def __init__(self, backend, *, settings_provider=None, clock=None, monotonic=None,
                 lease_factory=RunLease, barrier_reader=pending_updates_barrier,
                 browser_runner=run_browser_cleanup, busy_reader=None, volume_reader=None):
        self.backend = backend
        self.data_dir = Path(backend.data_dir)
        self.directory = self.data_dir / 'smart-maintenance'
        self.settings_provider = settings_provider or backend.preferences
        self.clock, self.monotonic = clock or time.time, monotonic or time.monotonic
        self.lease_factory, self.barrier_reader, self.browser_runner = lease_factory, barrier_reader, browser_runner
        self.volume_reader = volume_reader
        self.busy_reader = busy_reader or (lambda: bool(getattr(getattr(backend, 'workflow', None), 'active', False))
            or bool(getattr(getattr(backend, 'bridge', None), '_busy', False))
            or bool(getattr(getattr(backend, 'bridge', None), '_pending', {})))
        self._stop = threading.Event()
        self._tick_lock, self._lock = threading.Lock(), threading.RLock()
        self._thread = None
        self._observed = {}
        self._browser_scope = None
        self._configuration = copy.deepcopy(SMART_DEFAULTS)
        self._unused_plans = []
        self._unused_privacy_plans = []
        self._status = {'status': 'stopped', 'running': False, 'last_check': None,
                        'note': 'Smart Maintenance is off until saved choices enable it and the app starts monitoring.'}

    def configure(self, saved_settings):
        configuration = validate_smart_configuration(saved_settings)
        from .toolkit_configuration import validate_configuration
        configuration['low_space_cleaning'] = validate_configuration(saved_settings.get('toolkit_settings', {}))['low_space_cleaning']
        configuration['drive_space_guard_enabled'] = saved_settings.get('drive_space_guard_enabled', True)
        with self._lock:
            self._configuration = configuration
            if not configuration['browser_exit_cleaning_enabled']:
                self._observed.clear()
        return self.status()

    def status(self):
        with self._lock:
            return copy.deepcopy(dict(self._status, running=self._thread is not None and self._thread.is_alive(),
                                      configuration=self._configuration))

    def _publish(self, value):
        with self._lock:
            self._status = copy.deepcopy(value)
        return self.status()

    def start(self):
        self.configure(self.settings_provider())
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return self.status()
            if not self._enabled(self._configuration):
                return self._publish(dict(self._status, status='disabled'))
            self._stop.clear()
            self._thread = threading.Thread(target=self._loop, name='Optimus Smart Maintenance', daemon=False)
            self._thread.start()
        return self.status()

    def _loop(self):
        from .runtime_priority import lower_worker_priority
        restore = lower_worker_priority()
        try:
            while not self._stop.wait(self._configuration['smart_maintenance_poll_seconds']):
                self.tick()
        finally:
            restore()

    def stop(self, *, wait=True, timeout=None):
        """Stop dispatch; a currently entered cleanup finishes its safe boundary."""
        self._stop.set()
        with self._lock:
            worker = self._thread
        if wait and worker is not None and worker is not threading.current_thread():
            worker.join(timeout=timeout)
        return self._publish(dict(self._status, status='stopping' if worker is not None and worker.is_alive() else 'stopped',
                                  note='Cancellation stops new work. An entered cleanup is allowed to save its outcome; no process is killed.'))

    def preview(self, *, cancel_event=None):
        return self.tick(apply=False, cancel_event=cancel_event)

    def _settings(self):
        settings = copy.deepcopy(self.settings_provider())
        settings.update(validate_smart_configuration(settings))
        age = settings.get('cleanup_age_days', 7)
        if type(age) is not int or not 0 <= age <= 3650:
            raise ValueError('The saved cleanup age is invalid.')
        from .toolkit_configuration import validate_configuration
        settings['toolkit_settings'] = validate_configuration(settings.get('toolkit_settings', {}))
        enabled, target = settings.get('drive_space_guard_enabled', True), settings.get('drive_space_guard_target_gib', 2.0)
        if type(enabled) is not bool or type(target) not in (int, float) or not math.isfinite(target) or not 1 <= target <= 2:
            raise ValueError('Choose an explicit drive-space guard and a target from 1 to 2 GiB.')
        settings.update(drive_space_guard_enabled=enabled, drive_space_guard_target_gib=target)
        return settings

    @staticmethod
    def _enabled(settings):
        low = settings.get('low_space_cleaning', settings.get('toolkit_settings', {}).get('low_space_cleaning', {}))
        return (any(settings[key] for key in ('smart_cleaning_enabled', 'browser_exit_cleaning_enabled'))
                or low.get('enabled') is True and settings.get('drive_space_guard_enabled', True) is True)

    def _write(self, path, value):
        _ordinary_data_path(self.directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        _ordinary_data_path(path, file=True)
        _private_json(path, value)

    def _read_json(self, path):
        info = _ordinary_data_path(path, file=True)
        if info is None:
            return None
        if info.st_size > 65536:
            raise ValueError('Smart Maintenance history exceeds its input limit.')
        return json.loads(path.read_text(encoding='utf-8'))

    def _state(self):
        owner = _current_owner()
        value = self._read_json(self.directory / 'state.json')
        if value is None:
            return {'schema': 1, 'owner': owner, 'attempts': {}, 'pending': None}
        if (not isinstance(value, dict) or set(value) != {'schema', 'owner', 'attempts', 'pending'}
                or type(value['schema']) is not int or value['schema'] != 1 or value['owner'] != owner
                or not isinstance(value['attempts'], dict) or len(value['attempts']) > 100):
            raise ValueError('Smart Maintenance history ownership or schema requires review.')
        for key, stamp in value['attempts'].items():
            if not isinstance(key, str) or (key not in ('junk', 'low-space') and not re.fullmatch(r'browser:[a-z][a-z0-9_-]{0,39}', key)) or not _number(stamp):
                raise ValueError('Smart Maintenance attempt history is malformed.')
        pending = value['pending']
        if pending is not None:
            if not isinstance(pending, str) or not re.fullmatch('[a-f0-9]{32}', pending):
                raise ValueError('Smart Maintenance pending receipt is malformed.')
            receipt = self._read_json(self.directory / (pending + '.json'))
            if (not isinstance(receipt, dict) or receipt.get('id') != pending or receipt.get('owner') != owner
                    or receipt.get('status') not in ('completed', 'completed_with_errors', 'cancelled')):
                raise ValueError('A previous Smart Maintenance cleanup has an unresolved outcome; review its private receipt.')
            value['pending'] = None
        return value

    def _cool(self, state, key, now, settings):
        previous = state['attempts'].get(key)
        return previous is None or now - previous >= settings['smart_cleaning_cooldown_seconds']

    def _junk_cool(self, state, now, settings):
        return all(self._cool(state, key, now, settings) for key in ('junk', 'low-space'))

    def _categories(self, settings):
        selected = list(settings.get('cleanup_category_ids') or [])
        if settings.get('cleanup_enabled') is True:
            selected.insert(0, 'user-temp')
        available = self.backend.cleaner.categories()
        if not isinstance(available, list) or len(available) > 2000:
            raise ValueError('Cleanup categories could not be verified.')
        known = {row['id']: row for row in available if isinstance(row, dict) and isinstance(row.get('id'), str)}
        return list(dict.fromkeys(identity for identity in selected if isinstance(identity, str)
            and ':' not in identity and identity in known and known[identity].get('available') is True
            and not known[identity].get('process_names')))

    def _scan(self, categories, settings, *, max_files=20_000, max_seconds=5):
        age = settings['cleanup_age_days']
        age = max(30, age) if settings.get('intensity') == 'Low' else age
        plan = self.backend.cleaner.scan(categories, age, max_files=max_files, max_seconds=max_seconds)
        if isinstance(plan, dict) and isinstance(plan.get('id'), str) and re.fullmatch('[a-f0-9]{32}', plan['id']):
            self._unused_plans.append(plan['id'])
        if not isinstance(plan, dict) or not isinstance(plan.get('files'), list) or len(plan['files']) > 20_000:
            raise ValueError('The bounded cleanup preview is invalid.')
        if type(plan.get('total_bytes')) is not int or plan['total_bytes'] < 0:
            raise ValueError('The cleanup preview size is unknown.')
        return plan

    @staticmethod
    def _complete(plan):
        return plan.get('complete') is True and not plan.get('errors') and not plan.get('skipped')

    @staticmethod
    def _summary(outcome):
        if not isinstance(outcome, dict):
            raise ValueError('The cleanup did not return a result.')
        summary = {key: copy.deepcopy(outcome[key]) for key in
            ('deleted_count', 'deleted_bytes', 'skipped_count', 'failed_items', 'cancelled', 'partial', 'log_path', 'note',
             'trigger_reason', 'volume_checks', 'target_met', 'volume_check_error') if key in outcome}
        summary['errors'] = [str(error)[:500] for error in outcome.get('errors', [])[:20]]
        if isinstance(outcome.get('items'), list):
            summary['items'] = []
            for row in outcome['items'][:20]:
                if not isinstance(row, dict):
                    continue
                item = {key: copy.deepcopy(row[key]) for key in ('browser_id', 'status', 'failed_items', 'reason') if key in row}
                profiles = row.get('profiles') if isinstance(row.get('profiles'), list) else []
                item['profiles'] = [{key: copy.deepcopy(profile[key]) for key in
                    ('profile_id', 'categories', 'status', 'counts', 'backup_id') if key in profile}
                    for profile in profiles[:20] if isinstance(profile, dict)]
                item['profiles_truncated'] = len(profiles) > 20
                if isinstance(row.get('cache'), dict):
                    item['cache'] = {key: copy.deepcopy(row['cache'][key]) for key in
                        ('deleted_count', 'deleted_bytes', 'skipped_count', 'failed_items', 'partial') if key in row['cache']}
                summary['items'].append(item)
        return summary

    def _attempt(self, state, key, now, operation, cancelled):
        identity = uuid.uuid4().hex
        receipt = {'schema': 1, 'id': identity, 'owner': state['owner'], 'trigger': key,
                   'started_at': now, 'status': 'prepared', 'note': 'Fresh saved scopes only; no installer or browser closure.'}
        self._write(self.directory / (identity + '.json'), receipt)
        state['pending'] = identity
        state['attempts'][key] = now
        self._write(self.directory / 'state.json', state)
        try:
            if cancelled():
                outcome = {'cancelled': True, 'note': 'Cancelled before cleanup.'}
            else:
                outcome = operation()
            if not isinstance(outcome, dict):
                raise ValueError('The cleanup returned no usable outcome.')
            if outcome.get('cancelled') is not True:
                if key in ('junk', 'low-space') and (any(type(outcome.get(field)) is not int or outcome[field] < 0 for field in
                    ('deleted_count', 'deleted_bytes', 'skipped_count')) or not isinstance(outcome.get('errors'), list)):
                    raise ValueError('The junk cleanup result is incomplete; its outcome requires review.')
                if key.startswith('browser:') and (type(outcome.get('failed_items')) is not int
                    or outcome['failed_items'] < 0 or not isinstance(outcome.get('items'), list)):
                    raise ValueError('The browser cleanup result is incomplete; its outcome requires review.')
            receipt['result'] = self._summary(outcome)
            receipt['status'] = ('cancelled' if cancelled() or outcome.get('cancelled') else
                'completed_with_errors' if outcome.get('failed_items') or outcome.get('partial') or outcome.get('errors') or outcome.get('skipped_count') or outcome.get('skips') else 'completed')
        except Exception as error:
            # An apply exception cannot establish which files/records changed.
            receipt.update(status='uncertain', error=type(error).__name__ + ': ' + str(error)[:1000])
            self._write(self.directory / (identity + '.json'), receipt)
            raise
        receipt['finished_at'] = self.clock()
        self._write(self.directory / (identity + '.json'), receipt)
        state['pending'] = None
        self._write(self.directory / 'state.json', state)
        return {'trigger': key, 'status': receipt['status'], 'result': receipt['result'], 'receipt': str(self.directory / (identity + '.json'))}

    def _low_space(self, settings, state, now, report, *, apply, cancelled):
        from .low_space_cleanup import LowSpacePlanner, MAX_CATEGORIES, MAX_FILES, MAX_SCAN_SECONDS
        if (settings['toolkit_settings']['low_space_cleaning']['enabled'] is not True
                or settings['drive_space_guard_enabled'] is not True or not self._junk_cool(state, now, settings)):
            return
        planner = LowSpacePlanner(settings, volume_reader=self.volume_reader)
        observation = planner.observe()
        report['checks'].append({'kind': 'low_space', 'volumes': planner.summary(observation),
            'note': 'Existing selected categories only; unknown or healthy volumes are preserved.'})
        selected = self._categories(settings)
        available = self.backend.cleaner.categories()
        if not isinstance(available, list) or len(available) > 2000:
            raise ValueError('Cleanup category roots are unavailable.')
        known = {row['id']: row for row in available if isinstance(row, dict) and isinstance(row.get('id'), str)}
        deadline = time.monotonic() + MAX_SCAN_SECONDS
        scanned, inspected = 0, 0
        for category_id in selected:
            if cancelled():
                break
            if inspected >= MAX_CATEGORIES or scanned >= MAX_FILES or time.monotonic() >= deadline:
                report['checks'].append({'kind': 'low_space_limit', 'note': 'The bounded low-space check ended; remaining categories await a later check.'})
                report['status'] = 'partial'
                break
            category = known.get(category_id)
            try:
                observation = planner.observe()
                target = planner.target(category, observation)
                if target is None:
                    continue
                inspected += 1
                plan = self._scan([category_id], settings, max_files=MAX_FILES - scanned,
                    max_seconds=max(1, math.ceil(deadline - time.monotonic())))
                scanned += len(plan['files'])
                check = {'kind': 'low_space_category', 'category_id': category_id,
                    'bytes': plan['total_bytes'], 'complete': self._complete(plan), 'mountpoint': target['mountpoint']}
                report['checks'].append(check)
                if not check['complete']:
                    report['status'] = 'partial'
                    continue
                current, before = planner.revalidate(category, target, plan)
                check['still_below_target'] = current is not None
                if current is None or not plan['files'] or not apply or cancelled():
                    continue
            except (OSError, ValueError) as error:
                report['checks'].append({'kind': 'low_space_category', 'category_id': category_id,
                    'complete': False, 'error': type(error).__name__ + ': ' + str(error)[:1000]})
                report['status'] = 'partial'
                continue

            def clean_category():
                if cancelled() or self.busy_reader() or _scope(self._settings()) != _scope(settings):
                    return {'cancelled': True, 'note': 'Cancelled, busy or saved choices changed before low-space cleanup.'}
                barrier = self.barrier_reader(self.backend)
                if barrier:
                    return {'cancelled': True, 'note': 'Low-space cleanup deferred: ' + str(barrier)[:1000]}
                from .adaptive_maintenance import checkpoint
                readiness = checkpoint(self.backend)
                if readiness is not None and not readiness['allowed']:
                    return {'cancelled': True, 'note': 'Low-space cleanup deferred: ' + ' '.join(readiness['reasons'])}
                try:
                    current, before_apply = planner.revalidate(category, target, plan)
                except (OSError, ValueError) as error:
                    return {'cancelled': True, 'note': 'Low-space candidate verification deferred: ' + str(error)[:1000]}
                if current is None:
                    return {'cancelled': True, 'note': 'The volume recovered or changed; no low-space cleanup was started.'}
                # Volume and file identity checks can take time. Resource/idle
                # readiness must still permit entry after those checks finish.
                readiness = checkpoint(self.backend)
                if readiness is not None and not readiness['allowed']:
                    return {'cancelled': True, 'note': 'Low-space cleanup deferred after file verification: ' + ' '.join(readiness['reasons'])}
                if cancelled() or self.busy_reader() or _scope(self._settings()) != _scope(settings):
                    return {'cancelled': True, 'note': 'Cancellation, activity or saved choices changed during the volume/file checks.'}
                barrier = self.barrier_reader(self.backend)
                if barrier:
                    return {'cancelled': True, 'note': 'Low-space cleanup deferred: ' + str(barrier)[:1000]}
                # Consume the original Cleaner preview unchanged. This helper
                # cannot forge or widen its privately retained approval records.
                outcome = self.backend.cleaner.apply(plan)
                if not isinstance(outcome, dict):
                    raise ValueError('The low-space cleanup returned no usable result.')
                outcome.update(trigger_reason='low_space', volume_checks={'before': planner.summary(before_apply)},
                               target_met=None)
                try:
                    after = planner.observe()
                    outcome['volume_checks']['after'] = planner.summary(after)
                    outcome['target_met'] = planner.recovered(category, target, after)
                except (OSError, ValueError) as error:
                    outcome['volume_check_error'] = type(error).__name__ + ': ' + str(error)[:1000]
                return outcome
            report['actions'].append(self._attempt(state, 'low-space', now, clean_category, cancelled))

    def _browser_transition(self, browser_id, status, current):
        rows = status.get('processes') if isinstance(status, dict) else None
        valid = (isinstance(status, dict) and status.get('complete') is True and not status.get('errors')
            and type(status.get('running')) is bool and isinstance(rows, list) and len(rows) <= 1000
            and status['running'] == bool(rows))
        if valid and rows:
            valid = all(isinstance(row, dict) and row.get('verified') is True and type(row.get('pid')) is int
                and row['pid'] > 4 and _number(row.get('create_time')) and row['create_time'] > 0 for row in rows)
            valid = valid and len({row['pid'] for row in rows}) == len(rows)
        if not valid:
            self._observed.pop(browser_id, None)
            return None, False
        previous = self._observed.get(browser_id)
        if rows:
            self._observed[browser_id] = {'armed': True, 'closed_since': None,
                'identities': [(row['pid'], row['create_time']) for row in rows]}
            return None, True
        if previous is None or not previous['armed']:
            return None, True
        if previous['closed_since'] is None:
            previous['closed_since'] = current
        return max(0, current - previous['closed_since']), True

    def tick(self, *, apply=True, cancel_event=None):
        """One bounded check. ``apply=False`` is an explicit, non-writing preview."""
        if type(apply) is not bool:
            raise ValueError('Choose an explicit preview or apply mode.')
        def cancelled():
            return (apply and self._stop.is_set()) or cancel_event is not None and cancel_event.is_set()
        if not self._tick_lock.acquire(blocking=False):
            return dict(self.status(), status='busy', note='A Smart Maintenance check is already active.')
        report = {'status': 'checked', 'last_check': datetime.now(timezone.utc).isoformat(),
                  'preview': not apply, 'actions': [], 'checks': [], 'note': 'Only observed app-open events and bounded eligible-file scans are covered.'}
        try:
            settings = self._settings()
            self.configure(settings)
            if cancelled():
                report['status'] = 'cancelled'
                return self._publish(report)
            if not self._enabled(settings):
                report['status'] = 'disabled'
                return self._publish(report)
            if apply and self.busy_reader():
                report.update(status='deferred', note='An application operation is active; Smart Maintenance waits for an idle check.')
                return self._publish(report)
            if apply:
                from .adaptive_maintenance import checkpoint
                readiness = checkpoint(self.backend)
                if readiness is not None and not readiness['allowed']:
                    report.update(status='deferred', note=' '.join(readiness['reasons']), adaptive_readiness=readiness)
                    return self._publish(report)
            with self.lease_factory(self.data_dir):
                barrier = self.barrier_reader(self.backend)
                if barrier:
                    report.update(status='deferred', note=str(barrier)[:2000])
                    return self._publish(report)
                state = self._state()
                now, current = self.clock(), self.monotonic()
                if not _number(now) or not _number(current):
                    raise ValueError('The monitoring clock is invalid.')
                self._low_space(settings, state, now, report, apply=apply, cancelled=cancelled)
                if settings['smart_cleaning_enabled'] and self._junk_cool(state, now, settings):
                    categories = self._categories(settings)
                    if categories and not cancelled():
                        plan = self._scan(categories, settings)
                        complete = self._complete(plan)
                        report['checks'].append({'kind': 'junk', 'bytes': plan['total_bytes'], 'complete': complete,
                            'threshold_bytes': settings['smart_cleaning_threshold_bytes'], 'categories': categories})
                        if not complete:
                            report['status'] = 'partial'
                        elif plan['total_bytes'] >= settings['smart_cleaning_threshold_bytes'] and plan['files'] and apply and not cancelled():
                            if self.busy_reader() or _scope(self._settings()) != _scope(settings):
                                raise ValueError('Saved cleanup choices changed after preview; no smart cleanup was started.')
                            def apply_junk():
                                if cancelled() or self.busy_reader() or _scope(self._settings()) != _scope(settings):
                                    return {'cancelled': True, 'note': 'Cancelled or saved choices changed before junk cleanup.'}
                                return self.backend.cleaner.apply(plan)
                            report['actions'].append(self._attempt(state, 'junk', now, apply_junk, cancelled))
                selected = settings.get('browser_cleanup_browsers') or []
                if not isinstance(selected, list) or len(selected) > 20 or any(item not in browser_definitions() for item in selected):
                    raise ValueError('The saved browser selection is invalid.')
                browser_scope = {key: settings.get(key) for key in _SCOPES if key.startswith('browser_')}
                if browser_scope != self._browser_scope:
                    self._observed.clear()
                    self._browser_scope = copy.deepcopy(browser_scope)
                if settings['browser_exit_cleaning_enabled']:
                    for browser_id in dict.fromkeys(selected):
                        if cancelled():
                            break
                        status = self.backend.browser_cleanup_status(browser_id)
                        elapsed, verified = self._browser_transition(browser_id, status, current)
                        report['checks'].append({'kind': 'browser', 'browser_id': browser_id,
                            'complete': verified,
                            'closed_for_seconds': elapsed})
                        if not verified:
                            report['status'] = 'partial'
                        key = 'browser:' + browser_id
                        if (elapsed is None or elapsed < settings['browser_exit_grace_seconds']
                                or not self._cool(state, key, now, settings) or not apply or cancelled()):
                            continue
                        if self.busy_reader() or _scope(self._settings()) != _scope(settings):
                            raise ValueError('Saved browser choices changed; no browser-exit cleanup was started.')
                        closed_settings = dict(settings, browser_cleanup_browsers=[browser_id])
                        def clean_cache(category_ids):
                            plan = self._scan(category_ids, settings)
                            if not self._complete(plan):
                                return {'partial': True, 'failed_items': 1, 'note': 'The cache scan was incomplete; nothing was deleted.'}
                            if cancelled() or self.busy_reader() or _scope(self._settings()) != _scope(settings):
                                return {'cancelled': True, 'note': 'Cancelled or saved choices changed before cache cleanup.'}
                            return self.backend.cleaner.apply(plan) if plan['files'] else {'deleted_count': 0, 'deleted_bytes': 0}
                        # The existing orchestrator rechecks closure before every
                        # privacy/cache apply. No prompt authorizes window closure.
                        service = self
                        class GuardedBrowserBackend:
                            def __getattr__(inner, name):
                                return getattr(service.backend, name)
                            def privacy_scan(inner, *args, **kwargs):
                                plan = service.backend.privacy_scan(*args, **kwargs)
                                if isinstance(plan, dict) and isinstance(plan.get('planId'), str) and re.fullmatch('[a-f0-9]{32}', plan['planId']):
                                    service._unused_privacy_plans.append(plan['planId'])
                                return plan
                            def privacy_apply(inner, *args, **kwargs):
                                if cancelled() or service.busy_reader() or _scope(service._settings()) != _scope(settings):
                                    raise InterruptedError('Saved browser choices changed or monitoring was cancelled before privacy cleanup.')
                                return service.backend.privacy_apply(*args, **kwargs)
                        outcome = self._attempt(state, key, now, lambda: self.browser_runner(GuardedBrowserBackend(),
                            closed_settings, clean=clean_cache, browser_prompt=lambda _: False,
                            cancel_event=type('Cancellation', (), {'is_set': staticmethod(cancelled)})()), cancelled)
                        report['actions'].append(outcome)
                        self._observed.pop(browser_id, None)
                if cancelled():
                    report['status'] = 'cancelled'
                elif report['actions']:
                    report['status'] = 'completed_with_errors' if report['status'] == 'partial' or any(action['status'] != 'completed' for action in report['actions']) else 'completed'
            return self._publish(report)
        except RuntimeError as error:
            report.update(status='deferred' if str(error) == 'Another Optimus process is already running maintenance.' else 'needs_review', note=str(error)[:2000])
            return self._publish(report)
        except Exception as error:
            report.update(status='needs_review', note=type(error).__name__ + ': ' + str(error)[:2000])
            return self._publish(report)
        finally:
            try:
                pairs = ((getattr(self.backend.cleaner, 'discard_plan', None), self._unused_plans),
                    (getattr(getattr(self.backend, 'browser_privacy_engine', None), 'discard_plan', None), self._unused_privacy_plans))
                for discard, identities in pairs:
                    if not callable(discard):
                        continue
                    for identity in identities:
                        try:
                            discard(identity)
                        except Exception:
                            self._publish(dict(self._status, status='needs_review', note='An unused cleanup preview could not be released.'))
            finally:
                self._unused_plans.clear()
                self._unused_privacy_plans.clear()
                self._tick_lock.release()
