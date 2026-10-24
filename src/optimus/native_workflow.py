"""Consent-scoped native one-click workflow and read-only outcome verification."""
from __future__ import annotations

import copy
import json
from pathlib import Path
import threading
import time
import uuid

from .automation import CURRENT_SETUP_VERSION, RunLease, record_attempt
from .workflow import atomic_json, _space_guard_result
from .native_operations import sha256_file
from .update_scan_evidence import app_scan_evidence, attach_app_scan_error, with_app_scan
from .navigation import WorkItem, cursor_for, navigation_skip, merge_presence, local_skip, can_fail_forward, shared_update_refusal


def _cancelled(event):
    return event is not None and event.is_set()


def run_native_software(backend, settings, *, cancel_event=None, item_callback=None,
                        prepared_callback=None, before_install=None, updates_only=False):
    notified = False
    def prepared(state=None):
        nonlocal notified
        if not notified:
            notified = True
            if prepared_callback:
                prepared_callback(state or {'operation': 'software', 'scan_complete': False,
                                            'candidate_count': 0, 'review_only': True})
    context = {}
    try:
        try:
            from .software_app_rules import apply_update_rules
            from .app_presence import run_app_presence
            from .parallel_updates import installer_barrier
            source_settings = settings
            settings = apply_update_rules(settings)
            navigation = cursor_for(backend, 'software_updates', 'App updates')
            presence = None
            if (not updates_only and settings.get('software_updates_enabled') is True
                    and settings.get('software_app_rules')):
                presence = run_app_presence(backend, settings, cancel_event=cancel_event,
                    item_callback=item_callback, before_install=before_install, navigation=navigation)
                context['app_presence'] = presence
                if installer_barrier(presence):
                    return with_app_scan(merge_presence({'items': [], 'failed_items': 0,
                        'review_only': True, 'skipped': True, 'installer_started': False,
                        'outcomeKnown': True, 'running': False,
                        'stop_reason': 'Application presence needs review before native updates can continue.'}, presence),
                        native=True, not_started='Application presence stopped this software worker before update discovery.')
            outcome = _run_native_software(backend, settings, cancel_event=cancel_event,
                item_callback=item_callback, prepared_callback=prepared, before_install=before_install,
                scan_context=context, navigation=navigation,
                scope_reader=lambda: apply_update_rules(source_settings))
            outcome = merge_presence(outcome, presence)
            context['outcome'] = outcome
            return outcome
        finally:
            prepared()
    except Exception as error:
        if isinstance(context.get('app_presence'), dict):
            error.app_presence = context['app_presence']
        attach_app_scan_error(error, context, native=True)
        raise


def _run_native_software(backend, settings, *, cancel_event=None, item_callback=None,
                         prepared_callback=None, before_install=None, scan_context, navigation=None, scope_reader=None):
    started = time.time()
    if settings.get('software_updates_enabled', True) is not True:
        return with_app_scan({'items': [], 'failed_items': 0, 'skipped': True, 'review_only': True,
            'installer_started': False, 'outcomeKnown': True, 'running': False}, native=True,
            not_started='Software Updater is off; no app scan or installer was started.')
    if _cancelled(cancel_event):
        return with_app_scan({'items': [], 'failed_items': 0, 'cancelled': True,
                'installer_started': False, 'outcomeKnown': True}, native=True,
                not_started='Cancelled before the app update search started.')
    discovery = WorkItem(navigation, 'software:discovery', 'Search for available native app updates', external=cancel_event)
    try:
        scan = backend.updates_scan()
    except Exception as error:
        discovery.finish('failed', message=str(error))
        error.app_scan = app_scan_evidence(native=True, started=started, error=error)
        raise
    skipped_discovery = discovery.requested()
    discovery.finish('cancelled' if skipped_discovery else 'completed', mutation_started=False)
    if skipped_discovery:
        return with_app_scan({'items': [], 'failed_items': 0, 'review_only': True,
            'skipped': True, 'navigation_cancelled': True, 'navigation_cancelled_items': 1,
            'installer_started': False, 'outcomeKnown': True, 'running': False}, native=True,
            not_started='The native app search was discarded at your request; no installer ran.')
    if not isinstance(scan, dict):
        error = ValueError('Invalid native application update metadata.')
        error.app_scan = app_scan_evidence(native=True, started=started, error=error)
        raise error
    scan_context.update(scan=scan, started=started)
    policy = settings.get('software_update_policy', 'review')
    result = {'review_only': policy == 'review', 'scan': scan, 'items': [], 'failed_items': 0,
              'cancelled': False, 'installer_started': False, 'outcomeKnown': True, 'running': False,
              'note': 'Only currently offered native-manager packages within saved approvals can install. Native authorization may still ask for each privileged action.'}
    scan_context['items'] = result['items']
    if scan.get('complete') is not True or scan.get('structured') is not True or scan.get('exitCode') != 0:
        result.update(review_only=True, failed_items=1, note='Package metadata is incomplete; no installer ran.')
        return with_app_scan(result, native=True, started=started)
    if policy == 'review':
        prepared_callback({'operation': 'software', 'scan_complete': True, 'candidate_count': 0, 'review_only': True})
        return with_app_scan(result, native=True, started=started, selected=[])
    if settings.get('setup_version') != CURRENT_SETUP_VERSION or settings.get('software_update_agreements_accepted') is not True:
        raise ValueError('Complete native setup and approve package-update agreements first.')
    if policy == 'all_supported' and settings.get('software_update_all_approved') is not True:
        raise ValueError('Updating all supported packages requires separate saved scope approval.')
    if policy not in ('selected', 'all_supported'):
        raise ValueError('Unknown software update policy.')
    wanted = {value.casefold() for value in settings.get('software_update_ids', [])}
    excluded = {value.casefold() for value in settings.get('software_update_exclude_ids', [])}
    selected = [row for row in scan['updates'] if row['id'].casefold() not in excluded and (policy == 'all_supported' or row['id'].casefold() in wanted)]
    approved_scope = copy.deepcopy({key: settings.get(key) for key in ('software_updates_enabled',
        'setup_version', 'software_update_policy', 'software_update_ids', 'software_update_all_approved',
        'software_update_exclude_ids', 'software_update_agreements_accepted', 'software_app_rules')})
    def scope_changed():
        current = scope_reader() if scope_reader is not None else settings
        return {key: current.get(key) for key in approved_scope} != approved_scope
    scan_context['selected'] = list(selected)
    state = {'operation': 'software', 'scan_complete': True, 'candidate_count': len(selected), 'review_only': False}
    prepared_callback(state)
    checkpoint = WorkItem(navigation, 'software:checkpoint', 'Wait for the native app installer checkpoint', external=cancel_event, kind='check') if selected and before_install else None
    permitted = not (selected and before_install) or before_install(state) is True
    skipped_checkpoint = checkpoint is not None and checkpoint.requested()
    if checkpoint:
        checkpoint.finish('cancelled' if skipped_checkpoint else 'completed', mutation_started=False)
    if not permitted or skipped_checkpoint:
        result.update(skipped=True, review_only=True, cancelled=_cancelled(cancel_event),
                      installer_started=False, outcomeKnown=True,
                      deferred_ids=[row['id'] for row in selected],
                      note='Approved native updates were deferred at the maintenance/installer checkpoint; no installer ran.')
        if skipped_checkpoint:
            result.update(cancelled=_cancelled(cancel_event), navigation_cancelled=True, navigation_cancelled_items=1)
        return with_app_scan(result, native=True, started=started, selected=selected)
    for index, row in enumerate(selected):
        if _cancelled(cancel_event):
            result['cancelled'] = True
            break
        if scope_changed():
            result.update(stop_reason='Saved application rules or update approvals changed; remaining packages require fresh review.')
            break
        work = WorkItem(navigation, 'software:update:' + row['id'], 'Update ' + row['id'], external=cancel_event, kind='mutation')
        from .item_progress import BoundItemProgress, accepts_progress_callback, normalize_item_progress
        progress_key = 'software:update:' + row['id']
        progress_state = {'description': 'Updating approved native package ' + row['id'],
                          'id': row['id'], 'current': index, 'total': len(selected)}
        if item_callback:
            item_callback(dict(progress_state, item_progress=normalize_item_progress({
                'key': progress_key, 'label': row['id'], 'phase': 'checking', 'percent': None})))
        if _cancelled(cancel_event):
            work.finish('cancelled', mutation_started=False)
            result['cancelled'] = True
            break
        reporter = BoundItemProgress(item_callback, progress_key, row['id'], progress_state)
        try:
            if work.requested():
                outcome = navigation_skip()
            elif scope_changed():
                outcome = dict(navigation_skip('Saved app approvals changed before launch.'), navigation_cancelled=False)
            else:
                work.installing()
                options = {'progress_callback': reporter} if callable(item_callback) and accepts_progress_callback(backend.update_app_unattended) else {}
                outcome = backend.update_app_unattended(row['id'], cancel_event=work.cancel_event, **options)
                if (work.requested() and outcome.get('cancelled') is True and outcome.get('installer_started') is False
                        and outcome.get('outcomeKnown') is True and outcome.get('running') is False):
                    outcome = dict(outcome, navigation_cancelled=True)
            if shared_update_refusal(outcome):
                outcome = dict(outcome, shared_barrier=True)
            success = outcome.get('exitCode') == 0 and outcome.get('verified') is True
            status = 'cancelled' if outcome.get('cancelled') else 'skipped' if outcome.get('skipped') else 'completed' if success else 'failed'
            result['items'].append({'id': row['id'], 'status': status, 'from_version': row['version'],
                                    'result': outcome})
            work.finish(status, result=result['items'][-1], mutation_started=outcome.get('installer_started', success),
                        outcome_known=outcome.get('outcomeKnown') is not False and outcome.get('running') is not True,
                        message=outcome.get('note'))
            if local_skip(outcome):
                continue
            if outcome.get('outcomeKnown') is False or outcome.get('running') is True or outcome.get('restartNeeded') is True:
                result.update(stop_reason='The native installer requires review or restart before any remaining update.')
                break
            if status == 'failed' or outcome.get('requiresReview'):
                result['failed_items'] += 1
                if can_fail_forward(outcome):
                    continue
                break  # Unknown installer state never authorizes another one.
            if status == 'cancelled':
                result['cancelled'] = True
                break
        except Exception as error:
            known = getattr(error, 'installer_started', None) is False and getattr(error, 'outcomeKnown', None) is True
            result['items'].append({'id': row['id'], 'status': 'failed', 'error': str(error),
                'result': {'installer_started': not known, 'outcomeKnown': known, 'running': False}})
            result['failed_items'] += 1
            work.finish('failed', mutation_started=not known, outcome_known=known, message=str(error))
            break
        finally:
            reporter.close()
    processed = {item['id'] for item in result['items']}
    result['deferred_ids'] = [row['id'] for row in selected if row['id'] not in processed]
    result['navigation_cancelled_items'] = sum(isinstance(item.get('result'), dict) and item['result'].get('navigation_cancelled') is True for item in result['items'])
    outcomes = [item.get('result') for item in result['items']]
    if outcomes:
        # Never retain the initial no-installer receipt after entering a native
        # manager. Only explicit native lifecycle evidence establishes a batch.
        for key in ('installer_started', 'outcomeKnown', 'running'): result.pop(key, None)
        if all(isinstance(value, dict) and type(value.get('installer_started')) is bool
               and type(value.get('outcomeKnown')) is bool and type(value.get('running')) is bool for value in outcomes):
            result.update(installer_started=any(value['installer_started'] for value in outcomes),
                          outcomeKnown=all(value['outcomeKnown'] for value in outcomes),
                          running=any(value['running'] for value in outcomes))
    return with_app_scan(result, native=True, started=started, selected=selected)


def run_native_duplicates(backend, settings, *, cancel_event=None):
    automatic = settings.get('duplicate_policy') != 'review'
    if automatic and (settings.get('setup_version') != CURRENT_SETUP_VERSION or settings.get('duplicate_changes_approved') is not True):
        raise ValueError('Automatic duplicate recycling requires current saved scope approval.')
    result = {'review_only': not automatic, 'scans': [], 'items': [], 'failed_items': 0, 'cancelled': False,
              'note': 'Only selected user folders are scanned. Keeper ordering and minimum age use file modification time; Unix ctime is not treated as a birth time. One identical verified keeper always remains.'}
    remaining = settings.get('duplicate_max_recycle', 25)
    age = settings.get('duplicate_min_age_days', 7) * 86400
    for root in settings.get('duplicate_roots', []):
        if _cancelled(cancel_event):
            result['cancelled'] = True
            break
        scan = backend.native_files.duplicates(root, cancel_event=cancel_event)
        result['scans'].append(scan)
        if scan.get('complete') is not True:
            result['failed_items'] += 1
            continue
        if not automatic:
            continue
        for group in scan['groups']:
            newest = settings['duplicate_policy'] == 'keep_newest'
            ordered = sorted(group['files'], key=lambda row: (row['modified_at'], row['Path']), reverse=newest)
            for row in ordered[1:]:
                if remaining <= 0 or _cancelled(cancel_event):
                    break
                if time.time() - row['modified_at'] < age:
                    continue
                try:
                    outcome = backend.native_files.recycle(row, cancel_event=cancel_event)
                    result['items'].append({'status': 'completed', 'result': outcome})
                    remaining -= 1
                except Exception as error:
                    result['items'].append({'status': 'failed', 'error': str(error)})
                    result['failed_items'] += 1
            if remaining <= 0 or _cancelled(cancel_event):
                break
    result['cancelled'] = _cancelled(cancel_event)
    return result


def run_native_startup(backend, settings, *, cancel_event=None):
    automatic = settings.get('startup_policy') == 'disable_selected'
    current = backend.startup_list()
    result = {'review_only': not automatic, 'entries': current, 'items': [], 'failed_items': 0, 'cancelled': False}
    if not automatic:
        return result
    if settings.get('setup_version') != CURRENT_SETUP_VERSION or settings.get('startup_changes_approved') is not True:
        raise ValueError('Automatic startup disabling requires current saved selection approval.')
    for approved in settings.get('startup_approved_entries', []):
        if _cancelled(cancel_event):
            result['cancelled'] = True
            break
        matches = [row for row in current if backend.native_startup.descriptor(row) == backend.native_startup.descriptor(approved)]
        if len(matches) != 1:
            result['items'].append({'name': approved.get('Name'), 'status': 'skipped',
                                    'note': 'The exact saved registration is no longer enabled or has changed; no broader match was substituted.'})
            continue
        try:
            outcome = backend.startup_set(matches[0], False)
            result['items'].append({'name': approved['Name'], 'status': 'completed', 'result': outcome})
        except Exception as error:
            result['items'].append({'name': approved.get('Name'), 'status': 'failed', 'error': str(error)})
            result['failed_items'] += 1
    return result


class NativeWorkflow:
    def __init__(self, backend):
        self.backend = backend
        from .navigation import NavigationHub
        self.navigation = NavigationHub()
        self.lock = threading.Lock()
        self.active = False
        self.last_report = None

    def run(self, settings, progress_callback, cancel_event=None, *, browser_prompt=None, _lease=None):
        global_cancel_event = cancel_event
        configuration = self.backend.preference_store.validate(settings)
        if not self.lock.acquire(blocking=False):
            raise RuntimeError('An optimization run is already active.')
        self.active = True
        report, lease, path, attempted, parallel_job = None, None, None, False, None
        try:
            from .parallel_updates import (ProgressMux, ParallelAppUpdates, ParallelInstallerQueue, NATIVE_SOFTWARE,
                                           independent_stage, ready_maintenance_stages, BEFORE_UPDATES, BLOCKED_STAGES, installer_barrier,
                                           pending_updates_barrier, parallel_stages, record_installer_outcome)
            progress_callback = ProgressMux(progress_callback)
            if _lease is None:
                lease = RunLease(self.backend.data_dir).acquire()
            else:
                if (not isinstance(_lease, RunLease) or _lease.handle is None
                        or _lease.path != self.backend.data_dir / 'maintenance-run.lock'):
                    raise ValueError('A current supplied maintenance lease is required.')
                lease = _lease
            unresolved_apps = pending_updates_barrier(self.backend)
            identifier = uuid.uuid4().hex
            self.navigation.begin(identifier, cancel_event=global_cancel_event)
            maintenance_navigation = self.navigation.cursor('maintenance', 'Maintenance')
            cancel_event = maintenance_navigation.cancellation(global_cancel_event)
            path = self.backend.data_dir / 'runs' / (identifier + '.json')
            report = {'id': identifier, 'platform': self.backend.system, 'started_at': time.time(),
                      'settings': copy.deepcopy(configuration), 'status': 'running', 'stages': [],
                      'cancelled': False, 'log_path': str(path),
                      'note': 'Percentage is completed stages, not time. Native operations apply only saved choices and exact current scopes. Recommendations do not remove programs automatically.'}
            self.last_report = report
            atomic_json(path, report)
            record_attempt(self.backend.data_dir, configuration, status='started', run_id=identifier,
                           validator=self.backend.preference_store.validate)
            attempted = True

            def space_guard(*, initial=False):
                result = _space_guard_result(self.backend, configuration)
                report['space_guard'] = result
                if initial:
                    report['initial_space_guard'] = result
                progress_callback.update_space_guard(result)
                return result

            def clean(categories):
                if _cancelled(cancel_event):
                    return {'cancelled': True, 'failed_items': 0}
                from .item_progress import accepts_progress_callback
                scan_options = ({'progress_callback': serial_item_progress}
                                if accepts_progress_callback(self.backend.cleanup_scan) else {})
                plan = self.backend.cleanup_scan(categories, configuration.get('cleanup_age_days', 7), **scan_options)
                if plan.get('complete') is not True:
                    return {'preview_summary': {'complete': False, 'total_bytes': plan.get('total_bytes', 0)},
                            'failed_items': 1, 'note': 'Incomplete cleanup preview; no files were deleted.'}
                if _cancelled(cancel_event):
                    return {'cancelled': True, 'failed_items': 0}
                apply_options = ({'progress_callback': serial_item_progress}
                                 if accepts_progress_callback(self.backend.cleanup_apply) else {})
                applied = self.backend.cleanup_apply(plan, **apply_options)
                if isinstance(plan.get('id'), str):
                    progress_callback.removal_receipt('cleanup:' + plan['id'], applied)
                return {'result': applied, 'failed_items': len(applied.get('errors', []))}

            serial_job = None
            def serial_item_progress(state):
                if serial_job is not None:
                    serial_job.item_progress(state)
                if isinstance(state, dict):
                    from .item_progress import normalize_item_progress
                    value = normalize_item_progress(state.get('item_progress'))
                    if value is not None:
                        progress_callback(dict(progress_callback.foreground, item_progress=value,
                            deleted_bytes=state.get('deleted_bytes'), deleted_count=state.get('deleted_count'),
                            description=str(state.get('description') or value['label'])))

            stages = [('Inspect native resources and process identities', self.backend.audit)]
            has_space_guard = configuration.get('drive_space_guard_enabled') is True and callable(getattr(self.backend, 'space_guard_check', None))
            if has_space_guard:
                stages.append(('Check drive space buffers', lambda: space_guard(initial=True)))
            elif configuration.get('drive_space_guard_enabled') is True:
                space_guard(initial=True)
            if configuration.get('applications_review_enabled'):
                stages.append(('Review installed native applications', lambda: {'review_only': True, 'items': self.backend.installed_apps()}))
            if configuration.get('performance_review_enabled'):
                stages.append(('Measure applications contributing to pressure', lambda: self.backend.slowdown_report(cancel_event=cancel_event)))
            if configuration.get('speed_enhancements_enabled'):
                stages.append(('Apply supported speed enhancements',
                               lambda: self.backend._apply_speed_enhancements_stage(configuration, cancel_event=cancel_event)))
            if configuration.get('startup_review_enabled'):
                startup_title = ('Review or disable approved user startup entries' if configuration.get('startup_policy') == 'disable_selected'
                                 else 'Review native startup registrations')
                stages.append((startup_title, lambda: run_native_startup(self.backend, configuration, cancel_event=cancel_event)))
            if configuration.get('duplicates_enabled'):
                duplicate_title = ('Find or recycle approved duplicate copies' if configuration.get('duplicate_policy') != 'review'
                                   else 'Review native duplicate copies')
                stages.append((duplicate_title, lambda: run_native_duplicates(self.backend, configuration, cancel_event=cancel_event)))
            if configuration.get('cleanup_enabled'):
                # Browser caches have their own per-run close choice. Never let
                # a general category approval bypass that separate interaction.
                available = {row['id'] for row in self.backend.cleanup_categories() if row.get('available') and ':' not in row['id']}
                selected = [identity for identity in configuration.get('cleanup_category_ids', []) if identity in available]
                stages.append(('Clean approved native temporary files and caches', lambda: clean(selected)))
            if any(configuration.get(key) for key in ('browser_cache_enabled', 'browser_history_enabled', 'browser_cookies_enabled', 'browser_download_history_enabled')):
                from .browser_cleanup import run_browser_cleanup
                stages.append(('Clean selected browser data after close or skip choice',
                               lambda: run_browser_cleanup(self.backend, configuration, clean=clean,
                                                            browser_prompt=browser_prompt, cancel_event=cancel_event)))
            for retention_kind, retention_title in (
                ('codex_archives', 'Clean archived Codex conversations'),
                ('angerona_logs', 'Clean inactive Angerona logs'),
                ('failed_downloads', 'Clean incomplete download leftovers'),
            ):
                if configuration.get(retention_kind + '_cleanup_enabled') is True:
                    def retention_operation(kind=retention_kind):
                        operation = (self.backend.codex_archive_cleanup if kind == 'codex_archives' else
                                     lambda settings, **options: self.backend.retention_cleanup(kind, settings, **options))
                        result = operation(configuration, progress_callback=serial_item_progress, cancel_event=cancel_event)
                        if isinstance(result, dict) and isinstance(result.get('cleanup_id'), str):
                            progress_callback.removal_receipt('cleanup:' + result['cleanup_id'], result)
                        return result
                    stages.append((retention_title, retention_operation))
            if configuration.get('software_updates_enabled'):
                stages.append(('Review or install approved native package updates', lambda: run_native_software(self.backend, configuration, cancel_event=cancel_event, item_callback=serial_item_progress)))
            if configuration.get('driver_updates_enabled'):
                stages.append(('Review native kernel and hardware-support coverage', self.backend.driver_updates_scan))
            if configuration.get('security_review_enabled'):
                stages.append(('Review native kernel and hardware-support inventory', self.backend.driver_devices))
                stages.append(('Review native security observations', self.backend.security_status))
            if configuration.get('legacy_patch_review_enabled'):
                stages.append(('Review vendor support and patch limitations', self.backend.legacy_patch_review))
            if configuration.get('background_analysis_enabled'):
                stages.append(('Start requested read-only background analysis', lambda: self.backend.analysis_start(configuration.get('analysis_interval', 5))))
            if configuration.get('post_process_review_enabled'):
                stages.append(('Prepare optional background-program recommendations', lambda: self.backend.process_harness_scan(cancel_event=cancel_event)))
            if any(section.get('enabled') is True for section in configuration.get('toolkit_settings', {}).values()):
                factory = getattr(self.backend, '_toolkit', None)
                if callable(factory):
                    early, late = factory().stages(configuration, lease=lease, cancel_event=cancel_event,
                                                 progress_callback=serial_item_progress)
                    stages[1:1] = [(title, operation) for title, _, operation in early]
                    stages.extend((title, operation) for title, _, operation in late)
            stages.append(('Capture final native resource report', self.backend.audit))
            if has_space_guard:
                stages.append(('Recheck drive space buffers', space_guard))
            stages.append(('Verify optimization outcomes', lambda: self.backend.verify_optimization(report, cancel_event=cancel_event)))
            parallel_spec = None
            parallel_launch_after = None
            if configuration.get('parallel_app_updates_enabled'):
                stages, parallel_spec, parallel_launch_after = parallel_stages(stages, NATIVE_SOFTWARE)
            total_stages = len(stages) + int(parallel_spec is not None)
            completed, joined = 0, False
            parallel_barrier = unresolved_apps

            def join_updates(next_stage=None):
                nonlocal joined, completed, parallel_barrier
                if parallel_job is not None and not joined:
                    stage = parallel_job.join(next_stage=next_stage)[0]
                    joined = True
                    completed += 1
                    parallel_barrier = parallel_barrier or installer_barrier(stage['result'])
                    report['cancelled'] = report['cancelled'] or stage['result'].get('cancelled') is True
                    atomic_json(path, report)

            checkpoint_open = False
            cleanup_barrier = None
            if parallel_spec is not None:
                def background_software(worker, item_callback, prepared_callback, before_install):
                    return run_native_software(worker, configuration, cancel_event=getattr(worker, '_optimus_lane_cancel_event', global_cancel_event),
                        item_callback=item_callback, prepared_callback=prepared_callback, before_install=before_install)
                parallel_job = ParallelInstallerQueue(self.backend, identifier,
                    [(NATIVE_SOFTWARE, background_software)], progress_callback,
                    cancel_event=global_cancel_event, prior_barrier=unresolved_apps)
                report['stages'].extend(parallel_job.start())
                atomic_json(path, report)

            for title, operation in ready_maintenance_stages(stages, parallel_job):
                if parallel_job is not None and checkpoint_open and not independent_stage(title):
                    join_updates(title)
                if parallel_job is not None and title in BEFORE_UPDATES and not checkpoint_open:
                    cleanup_barrier = cleanup_barrier or parallel_job.wait_prepared()
                if _cancelled(global_cancel_event):
                    report['cancelled'] = True
                    break
                navigation_item = title != NATIVE_SOFTWARE
                mutation_item = title.startswith(('Apply ', 'Review or disable ', 'Find or recycle ', 'Clean ', 'Start requested '))
                navigation_available = True
                if navigation_item:
                    navigation_available = maintenance_navigation.begin_item('stage:' + str(completed) + ':' + title, title,
                        kind='mutation' if mutation_item else 'scan')
                progress_callback({'percentage': int(completed * 100 / total_stages), 'stage': title,
                                   'stage_status': 'running', 'maintenance_status': 'scanning' if title == stages[0][0] else 'running',
                                   'description': 'Starting: ' + title, 'processes': ['Optimus native maintenance worker'],
                                   'log': 'Starting: ' + title, 'completed': completed, 'total': total_stages})
                stage = {'name': title, 'title': title, 'started_at': time.time(), 'status': 'completed'}
                serial_job = None
                try:
                    from .adaptive_maintenance import checkpoint, deferred_result
                    assessment = checkpoint(self.backend, title=title)
                    if not navigation_available:
                        result = {'skipped': True, 'navigation_blocked': True, 'requiresReview': True, 'failed_items': 0,
                                  'note': 'The preceding maintenance item has an unresolved outcome; this stage was not started.'}
                    elif navigation_item and maintenance_navigation.local_requested():
                        result = {'skipped': True, 'cancelled': True, 'navigation_cancelled': True, 'navigation_not_started': True,
                                  'failed_items': 0, 'installer_started': False, 'outcomeKnown': True,
                                  'note': 'You skipped this exact native maintenance stage before it started.'}
                    elif assessment is not None and not assessment['allowed']:
                        result = deferred_result(assessment)
                    elif cleanup_barrier and title in BEFORE_UPDATES:
                        result = {'skipped': True, 'review_only': True, 'requiresReview': True,
                                  'failed_items': 0, 'items': [], 'stop_reason': cleanup_barrier,
                                  'note': 'No cleanup ran while update discovery still owned temporary files.'}
                    elif parallel_barrier and title == NATIVE_SOFTWARE:
                        # Search in serial mode too, while retaining the closed
                        # installer gate and the unresolved installer's receipt.
                        def discover_only(worker, item_callback, prepared_callback, before_install):
                            return run_native_software(worker, configuration, cancel_event=cancel_event,
                                item_callback=item_callback, prepared_callback=prepared_callback,
                                before_install=before_install)
                        guarded = ParallelInstallerQueue(self.backend, identifier, [(title, discover_only)],
                            progress_callback, cancel_event=cancel_event, prior_barrier=parallel_barrier)
                        guarded.start(inline=True)
                        guarded.join()
                        serial_job = guarded.jobs[0]
                        result = serial_job.result
                        stage['worker_log_path'] = str(serial_job.path)
                    elif parallel_barrier and title in BLOCKED_STAGES:
                        result = {'skipped': True, 'review_only': True, 'requiresReview': True,
                                  'failed_items': 0, 'items': [], 'stop_reason': parallel_barrier,
                                  'blocked_by_prior_installer': True,
                                  'note': 'Deferred after update installation: ' + parallel_barrier}
                    elif title == NATIVE_SOFTWARE:
                        serial_job = ParallelAppUpdates(self.backend, identifier, title,
                            lambda worker, callback, operation=operation: operation(), progress_callback, cancel_event=cancel_event)
                        serial_job.start(inline=True)
                        if serial_job.result is None:
                            serial_job._run()
                        result = serial_job.result
                        stage['worker_log_path'] = str(serial_job.path)
                    else:
                        if navigation_item:
                            maintenance_navigation.phase('applying' if mutation_item else 'scanning', mutation_started=mutation_item)
                        result = operation()
                    if navigation_item and maintenance_navigation.local_requested() and isinstance(result, dict) and result.get('cancelled') is True:
                        result = dict(result, navigation_cancelled=True,
                                      navigation_cancelled_items=max(1, result.get('navigation_cancelled_items', 0)))
                    stage['result'] = result
                    if isinstance(result, dict):
                        if result.get('navigation_blocked') is True or result.get('deferred') is True:
                            stage['status'] = 'skipped'
                        if result.get('failed_items', 0):
                            stage['status'] = 'failed'
                            stage['error'] = 'One or more selected operations or verification checks need review.'
                        if result.get('cancelled'):
                            stage['status'] = 'cancelled'
                            if not (navigation_item and maintenance_navigation.local_requested()):
                                report['cancelled'] = True
                        if result.get('navigation_cancelled') is True or result.get('navigation_cancelled_items'):
                            stage['status'] = 'cancelled'
                    if title == 'Prepare optional background-program recommendations':
                        report['process_review'] = result
                    if title == 'Review native security observations':
                        report['security_review'] = result
                    if title == 'Verify optimization outcomes':
                        report['final_verification'] = result
                except Exception as error:
                    stage.update(status='failed', error=str(error))
                if title == NATIVE_SOFTWARE and parallel_job is None:
                    parallel_barrier = parallel_barrier or installer_barrier(stage.get('result'))
                    if serial_job is None and not (isinstance(stage.get('result'), dict) and stage['result'].get('blocked_by_prior_installer')):
                        record_installer_outcome(self.backend, identifier, title, stage.get('result'))
                stage['finished_at'] = time.time()
                if title == stages[0][0]:
                    report['stages'].insert(0, stage)
                else:
                    report['stages'].append(stage)
                completed += 1
                report['resource_totals'] = progress_callback.resource_snapshot()
                report['worker_resource_totals'] = progress_callback.worker_resource_snapshots()
                atomic_json(path, report)
                if navigation_item:
                    maintenance_navigation.finish(stage['status'],
                        message=stage.get('error') or (stage.get('result') or {}).get('note') if isinstance(stage.get('result'), dict) else stage.get('error'),
                        mutation_started=False if isinstance(stage.get('result'), dict) and stage['result'].get('navigation_not_started') is True else mutation_item,
                        outcome_known=not (mutation_item and stage.get('error') and 'result' not in stage
                            or isinstance(stage.get('result'), dict) and stage['result'].get('outcomeKnown') is False))
                if parallel_job is not None and title == parallel_launch_after:
                    checkpoint_open = True
                    prior_barrier = unresolved_apps or cleanup_barrier
                    if report['stages'][0]['status'] != 'completed':
                        prior_barrier = 'The initial before-inventory failed; native update installation was deferred.'
                    if report['cancelled']:
                        parallel_job.cancel()
                    parallel_job.release_maintenance(prior_barrier)
                progress_callback({'percentage': int(completed * 100 / total_stages), 'stage': title,
                                   'stage_status': stage['status'],
                                   'description': (str((stage.get('result') or {}).get('summary'))
                                       if title in ('Check drive space buffers', 'Recheck drive space buffers')
                                       and isinstance(stage.get('result'), dict) and stage['result'].get('summary')
                                       else stage.get('error') or title + ': ' + stage['status']),
                                   'processes': ['Optimus native maintenance worker'],
                                   'log': stage['status'].upper() + ': ' + title,
                                   'completed': completed, 'total': total_stages})
                if report['cancelled']:
                    break
            join_updates()
            report['finished_at'] = time.time()
            report['failed_stages'] = sum(row['status'] == 'failed' for row in report['stages'])
            report['cancelled_stages'] = sum(row['status'] == 'cancelled' for row in report['stages'])
            report['status'] = 'cancelled' if report['cancelled'] else 'completed_with_errors' if report['failed_stages'] or report['cancelled_stages'] else 'completed'
            report['resource_totals'] = progress_callback.resource_snapshot()
            report['worker_resource_totals'] = progress_callback.worker_resource_snapshots()
            if configuration.get('toolkit_settings', {}).get('resume', {}).get('enabled') is True:
                from .update_resume import UpdateResumeService
                try:
                    report['update_resume'] = UpdateResumeService(self.backend).capture(configuration, report, lease=lease)
                except (OSError, ValueError, RuntimeError) as error:
                    report['update_resume'] = {'status': 'unavailable', 'reason': str(error)[:1000]}
            atomic_json(path, report)
            record_attempt(self.backend.data_dir, configuration, status='cancelled' if report['cancelled'] else 'failed' if report['failed_stages'] else 'completed',
                           run_id=identifier, validator=self.backend.preference_store.validate)
            return report
        except Exception as error:
            if parallel_job is not None:
                parallel_job.cancel()
                parallel_job.join()
            if report is not None:
                report.update(status='failed', error=str(error), finished_at=time.time())
                if hasattr(progress_callback, 'resource_snapshot'):
                    report['resource_totals'] = progress_callback.resource_snapshot()
                    report['worker_resource_totals'] = progress_callback.worker_resource_snapshots()
                if path is not None:
                    atomic_json(path, report)
                if attempted:
                    record_attempt(self.backend.data_dir, configuration, status='failed', run_id=report['id'],
                                   error=str(error), validator=self.backend.preference_store.validate)
            raise
        finally:
            try:
                if parallel_job is not None:
                    if not parallel_job.joined:
                        parallel_job.cancel()
                    parallel_job.join()
            finally:
                self.navigation.end((report or {}).get('status', 'failed'))
                if lease is not None and _lease is None:
                    lease.release()
                self.active = False
                self.lock.release()


def verify_native_outcomes(backend, report, *, cancel_event=None, timeout=30):
    """Inspect current postconditions without repeating a cleanup or installer."""
    deadline = time.monotonic() + max(1, min(float(timeout), 120))
    result = {'schema': 1, 'read_only': True, 'status': 'confirmed', 'stages': [],
              'counts': {'confirmed': 0, 'needs_review': 0, 'unverified': 0, 'not_applicable': 0},
              'coverage_complete': True, 'deadline_reached': False, 'failed_items': 0,
              'note': 'Checks describe current postconditions. They do not prove a speed increase or that every background application is necessary.'}

    def add(label, status, detail):
        result['stages'].append({'name': label, 'status': status, 'detail': detail})
        result['counts'][status] += 1

    def expired():
        if _cancelled(cancel_event) or time.monotonic() >= deadline:
            result['coverage_complete'] = False
            result['deadline_reached'] = time.monotonic() >= deadline
            return True
        return False

    installed = None
    if expired():
        add('Native outcome verification', 'unverified', 'Verification was cancelled or its deadline elapsed before postconditions were inspected.')
    for stage in report.get('stages', []):
        label = stage.get('title') or stage.get('name', 'Native stage')
        if expired():
            add(label, 'unverified', 'The bounded verification was cancelled or reached its deadline.')
            continue
        if stage.get('status') == 'failed':
            add(label, 'needs_review', 'This stage reported a failure; inspect its saved item results.')
            continue
        outcome = stage.get('result')
        if not isinstance(outcome, dict):
            add(label, 'not_applicable', 'Read-only inventory; no mutation postcondition to check.')
            continue
        cleanup = outcome.get('result', outcome)
        from .verification import _TOOLKIT_FILE_STAGES, _TOOLKIT_READ_ONLY_STAGES, toolkit_cleanup_checks
        if label in _TOOLKIT_FILE_STAGES:
            for check in toolkit_cleanup_checks(cleanup, stopped=expired):
                add(label, check['status'], check['reason'] + ' ' + json.dumps(check['evidence'], sort_keys=True))
        elif label in _TOOLKIT_READ_ONLY_STAGES and outcome.get('read_only') is True:
            add(label, 'not_applicable', 'Bounded read-only observations or metadata history; no cleanup, repair or performance gain is claimed.')
        elif isinstance(cleanup, dict) and 'deleted_count' in cleanup:
            checked, remaining = 0, 0
            for row in cleanup.get('files', []):
                if expired():
                    break
                if row.get('status') == 'deleted':
                    checked += 1
                    # A recreated filename is not proof the deletion failed.
                    path = Path(row['path'])
                    try:
                        info = path.lstat()
                        identity = row.get('identity', {})
                        if info.st_ino == identity.get('inode') and info.st_dev == identity.get('device'):
                            remaining += 1
                    except FileNotFoundError:
                        pass
                    except OSError:
                        remaining += 1
            valid = checked == cleanup.get('deleted_count') and not remaining and not expired()
            add(label, 'confirmed' if valid else 'needs_review', f'{checked} reported deletions checked; {remaining} original identities remain or are inaccessible.')
        elif 'Review or install approved native package updates' == label:
            for item in outcome.get('items', []):
                if expired():
                    add(item.get('id', label), 'unverified', 'The bounded verification deadline or cancellation prevented this package check.')
                    continue
                if item.get('status') != 'completed':
                    add(item.get('id', label), 'not_applicable' if item.get('status') == 'skipped' else 'needs_review', 'No completed package installation to confirm.')
                    continue
                if installed is None:
                    try:
                        installed = {row['id']: row for row in backend.native_packages.installed(timeout=max(0.1, deadline - time.monotonic()), cancel_event=cancel_event)}
                    except Exception as error:
                        add(item['id'], 'unverified', 'Current native package inventory is unavailable (' + type(error).__name__ + ').')
                        continue
                if expired():
                    add(item['id'], 'unverified', 'The package inventory did not finish within the verification budget.')
                    continue
                current = installed.get(item['id'])
                recorded = item.get('result', {}).get('installed_version')
                valid = current is not None and recorded and current['version'] == recorded and recorded != item.get('from_version')
                add(item['id'], 'confirmed' if valid else 'needs_review', 'Current native package version matches the recorded installation result.' if valid else 'The recorded installed-version change could not be confirmed.')
            if not outcome.get('items'):
                add(label, 'not_applicable', 'Review only or no offered approved updates; no installer ran.')
        elif label in ('Review or disable approved user startup entries', 'Review native startup registrations'):
            for item in outcome.get('items', []):
                receipt = item.get('result', {}).get('entry')
                if item.get('status') == 'completed' and receipt:
                    try:
                        current = backend.native_startup._entry(receipt['Path'])
                        valid = current['SHA256'] == receipt['SHA256'] and current['Enabled'] is False
                        add(item.get('name', label), 'confirmed' if valid else 'needs_review', 'Current native startup identity and disabled state checked.')
                    except Exception:
                        add(item.get('name', label), 'unverified', 'The current startup file is unavailable.')
            if not outcome.get('items'):
                add(label, 'not_applicable', 'Startup review only; no change was made.')
        elif label in ('Find or recycle approved duplicate copies', 'Review native duplicate copies'):
            for item in outcome.get('items', []):
                change = item.get('result', {})
                if item.get('status') == 'completed':
                    valid = not Path(change['path']).exists() and Path(change['keeper']).is_file()
                    try:
                        valid = valid and bool(change.get('sha256')) and sha256_file(change['keeper'], cancel_event=cancel_event, deadline=deadline) == change['sha256']
                    except (OSError, ValueError, TimeoutError, InterruptedError):
                        valid = False
                    add(label, 'confirmed' if valid else 'needs_review', 'Recycled source is absent and its keeper is present; restore through native Trash.')
            if not outcome.get('items'):
                add(label, 'not_applicable', 'Duplicate review only or no eligible old extras.')
        elif label == 'Clean selected browser data after close or skip choice':
            for browser in outcome.get('items', []):
                for profile in browser.get('profiles', []):
                    if profile.get('status') != 'applied':
                        continue
                    try:
                        state = backend.browser_cleanup_status(browser['browser_id'])
                        if state.get('complete') is not True or state.get('running') is not False:
                            raise ValueError('Browser reopened or its closed state is unknown.')
                        preview = backend.privacy_scan(profile['profile_id'], categories=profile['categories'], keep_domains=report['settings'].get('browser_keep_domains', []))
                        if expired():
                            add(label, 'unverified', 'The browser profile check exceeded the remaining verification budget.')
                        else:
                            valid = all(preview.get('counts', {}).get(category) == 0 for category in profile['categories'])
                            add(label, 'confirmed' if valid else 'needs_review', 'A fresh read-only profile count found no remaining eligible selected rows.' if valid else 'Selected browser data remains; new activity or unsupported schemas need review.')
                    except Exception as error:
                        add(label, 'unverified', 'Current browser postcondition unavailable: ' + str(error)[:250])
                cache = browser.get('cache', {})
                if cache.get('deleted_count', 0):
                    # Cache journals are local evidence, but browsers may create
                    # fresh files immediately. Do not claim cache success from
                    # a reported byte count alone.
                    log_id = cache.get('cleanup_log_id')
                    if isinstance(log_id, str) and len(log_id) == 32 and all(c in '0123456789abcdef' for c in log_id):
                        try:
                            journal = json.loads((backend.data_dir / 'cleanup' / (log_id + '.json')).read_text(encoding='utf-8'))
                            nested = {'stages': [{'title': label + ' cache', 'status': 'completed', 'result': journal['result']}]}
                            checked = verify_native_outcomes(backend, nested, cancel_event=cancel_event, timeout=max(1, deadline - time.monotonic()))
                            for row in checked['stages']:
                                add(row['name'], row['status'], row['detail'])
                        except Exception:
                            add(label, 'unverified', 'The browser cache deletion journal could not be verified.')
                    else:
                        add(label, 'unverified', 'No valid cache deletion journal was recorded.')
            if not any(row.get('profiles') or row.get('cache') for row in outcome.get('items', [])):
                add(label, 'not_applicable', 'No browser data was changed; unavailable or declined open browsers were skipped.')
        elif label == 'Apply supported speed enhancements':
            try:
                checked = backend.verify_speed_enhancements(outcome, cancel_event=cancel_event,
                                                            timeout=max(.01, deadline - time.monotonic()))
                from .speed_verification import normalized_speed_checks
                for check in normalized_speed_checks(checked):
                    add(check['target'], check['status'], check['reason'])
            except Exception:
                add(label, 'unverified', 'The fresh speed-configuration readback was unavailable.')
        elif label == 'Start requested read-only background analysis':
            state = backend.analysis_status()
            sample_deadline = min(deadline, time.monotonic() + 2)
            while state.get('enabled') is True and state.get('latest') is None and not _cancelled(cancel_event) and time.monotonic() < sample_deadline:
                time.sleep(0.05)
                state = backend.analysis_status()
            enabled = state.get('enabled') is True
            latest = state.get('latest')
            good = enabled and isinstance(latest, dict) and not latest.get('errors')
            add(label, 'confirmed' if good else 'unverified', 'A live read-only sampler and a successful current sample were verified.' if good else 'The sampler is not yet producing a verified successful sample; check background-analysis status.')
        else:
            add(label, 'not_applicable', 'Read-only discovery, recommendation or monitoring stage; no mutation was repeated.')
    if expired() and not result['counts']['unverified']:
        add('Native outcome verification', 'unverified', 'The verification budget expired or cancellation interrupted postcondition checks.')
    result['failed_items'] = result['counts']['needs_review'] + result['counts']['unverified']
    result['coverage_complete'] = result['coverage_complete'] and not result['counts']['unverified']
    result['status'] = 'needs_review' if result['counts']['needs_review'] else 'unverified' if result['counts']['unverified'] else 'confirmed'
    result['summary'] = (f"Native verification: {result['counts']['confirmed']} confirmed, "
                         f"{result['counts']['needs_review']} need review, {result['counts']['unverified']} unverified.")
    return result
