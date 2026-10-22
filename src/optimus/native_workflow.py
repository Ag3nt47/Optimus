"""Consent-scoped native one-click workflow and read-only outcome verification."""
from __future__ import annotations

import copy
import json
from pathlib import Path
import threading
import time
import uuid

from .automation import CURRENT_SETUP_VERSION, RunLease, record_attempt
from .workflow import atomic_json
from .native_operations import sha256_file


def _cancelled(event):
    return event is not None and event.is_set()


def run_native_software(backend, settings, *, cancel_event=None, item_callback=None):
    scan = backend.updates_scan()
    policy = settings.get('software_update_policy', 'review')
    result = {'review_only': policy == 'review', 'scan': scan, 'items': [], 'failed_items': 0,
              'cancelled': False, 'note': 'Only currently offered native-manager packages within saved approvals can install. Native authorization may still ask for each privileged action.'}
    if scan.get('complete') is not True or scan.get('structured') is not True or scan.get('exitCode') != 0:
        result.update(review_only=True, failed_items=1, note='Package metadata is incomplete; no installer ran.')
        return result
    if policy == 'review':
        return result
    if settings.get('setup_version') != CURRENT_SETUP_VERSION or settings.get('software_update_agreements_accepted') is not True:
        raise ValueError('Complete native setup and approve package-update agreements first.')
    if policy == 'all_supported' and settings.get('software_update_all_approved') is not True:
        raise ValueError('Updating all supported packages requires separate saved scope approval.')
    if policy not in ('selected', 'all_supported'):
        raise ValueError('Unknown software update policy.')
    wanted = set(settings.get('software_update_ids', []))
    excluded = set(settings.get('software_update_exclude_ids', []))
    selected = [row for row in scan['updates'] if row['id'] not in excluded and (policy == 'all_supported' or row['id'] in wanted)]
    for index, row in enumerate(selected):
        if _cancelled(cancel_event):
            result['cancelled'] = True
            break
        if item_callback:
            item_callback({'description': 'Updating approved native package ' + row['id'],
                           'id': row['id'], 'current': index, 'total': len(selected)})
        if _cancelled(cancel_event):
            result['cancelled'] = True
            break
        try:
            outcome = backend.update_app_unattended(row['id'], cancel_event=cancel_event)
            success = outcome.get('exitCode') == 0 and outcome.get('verified') is True
            status = 'cancelled' if outcome.get('cancelled') else 'skipped' if outcome.get('skipped') else 'completed' if success else 'failed'
            result['items'].append({'id': row['id'], 'status': status, 'from_version': row['version'],
                                    'result': outcome})
            if status == 'failed' or outcome.get('requiresReview'):
                result['failed_items'] += 1
                break  # Unknown installer state never authorizes another one.
            if status == 'cancelled':
                result['cancelled'] = True
                break
        except Exception as error:
            result['items'].append({'id': row['id'], 'status': 'failed', 'error': str(error)})
            result['failed_items'] += 1
            break
    return result


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
        self.lock = threading.Lock()
        self.active = False
        self.last_report = None

    def run(self, settings, progress_callback, cancel_event=None, *, browser_prompt=None):
        configuration = self.backend.preference_store.validate(settings)
        if not self.lock.acquire(blocking=False):
            raise RuntimeError('An optimization run is already active.')
        self.active = True
        report, lease, path, attempted, parallel_job = None, None, None, False, None
        try:
            from .parallel_updates import (ProgressMux, ParallelAppUpdates, NATIVE_SOFTWARE,
                                           independent_stage, BLOCKED_STAGES, installer_barrier,
                                           pending_updates_barrier, parallel_stages)
            progress_callback = ProgressMux(progress_callback)
            lease = RunLease(self.backend.data_dir).acquire()
            unresolved_apps = pending_updates_barrier(self.backend)
            identifier = uuid.uuid4().hex
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

            def clean(categories):
                if _cancelled(cancel_event):
                    return {'cancelled': True, 'failed_items': 0}
                plan = self.backend.cleanup_scan(categories, configuration.get('cleanup_age_days', 7))
                if plan.get('complete') is not True:
                    return {'preview_summary': {'complete': False, 'total_bytes': plan.get('total_bytes', 0)},
                            'failed_items': 1, 'note': 'Incomplete cleanup preview; no files were deleted.'}
                if _cancelled(cancel_event):
                    return {'cancelled': True, 'failed_items': 0}
                applied = self.backend.cleanup_apply(plan)
                return {'result': applied, 'failed_items': len(applied.get('errors', []))}

            stages = [('Inspect native resources and process identities', self.backend.audit)]
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
            if configuration.get('software_updates_enabled'):
                stages.append(('Review or install approved native package updates', lambda: run_native_software(self.backend, configuration, cancel_event=cancel_event)))
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
            stages.append(('Capture final native resource report', self.backend.audit))
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
                    stage = parallel_job.join(next_stage=next_stage)
                    joined = True
                    completed += 1
                    parallel_barrier = installer_barrier(stage['result'])
                    report['cancelled'] = report['cancelled'] or stage['result'].get('cancelled') is True
                    atomic_json(path, report)

            for title, operation in stages:
                if parallel_job is not None and not independent_stage(title):
                    join_updates(title)
                if _cancelled(cancel_event):
                    report['cancelled'] = True
                    break
                progress_callback({'percentage': int(completed * 100 / total_stages), 'stage': title,
                                   'description': 'Starting: ' + title, 'processes': ['Optimus native maintenance worker'],
                                   'log': 'Starting: ' + title, 'completed': completed, 'total': total_stages})
                stage = {'name': title, 'title': title, 'started_at': time.time(), 'status': 'completed'}
                try:
                    if parallel_barrier and title in BLOCKED_STAGES:
                        result = {'skipped': True, 'review_only': True, 'requiresReview': True,
                                  'failed_items': 0, 'items': [], 'stop_reason': parallel_barrier,
                                  'note': 'Deferred after application updating: ' + parallel_barrier}
                    else:
                        result = operation()
                    stage['result'] = result
                    if isinstance(result, dict):
                        if result.get('failed_items', 0):
                            stage['status'] = 'failed'
                            stage['error'] = 'One or more selected operations or verification checks need review.'
                        if result.get('cancelled'):
                            report['cancelled'] = True
                    if title == 'Prepare optional background-program recommendations':
                        report['process_review'] = result
                    if title == 'Review native security observations':
                        report['security_review'] = result
                    if title == 'Verify optimization outcomes':
                        report['final_verification'] = result
                except Exception as error:
                    stage.update(status='failed', error=str(error))
                if title == NATIVE_SOFTWARE and parallel_job is None:
                    parallel_barrier = installer_barrier(stage.get('result'))
                stage['finished_at'] = time.time()
                report['stages'].append(stage)
                completed += 1
                atomic_json(path, report)
                if parallel_spec is not None and parallel_job is None and title == parallel_launch_after and not report['cancelled']:
                    initial_failed = report['stages'][0]['status'] != 'completed'
                    def background_software(worker, item_callback):
                        if unresolved_apps or initial_failed:
                            return {'items': [], 'failed_items': 0, 'review_only': True, 'skipped': True,
                                    'stop_reason': unresolved_apps or 'The initial before-inventory failed; application updating was deferred.'}
                        return run_native_software(worker, configuration, cancel_event=parallel_job.cancel_event, item_callback=item_callback)
                    parallel_job = ParallelAppUpdates(self.backend, identifier, NATIVE_SOFTWARE,
                                                      background_software, progress_callback, cancel_event=cancel_event)
                    report['stages'].append(parallel_job.start())
                    atomic_json(path, report)
                progress_callback({'percentage': int(completed * 100 / total_stages), 'stage': title,
                                   'description': stage.get('error') or title + ': ' + stage['status'],
                                   'processes': ['Optimus native maintenance worker'],
                                   'log': stage['status'].upper() + ': ' + title,
                                   'completed': completed, 'total': total_stages})
                if report['cancelled']:
                    break
            join_updates()
            report['finished_at'] = time.time()
            report['failed_stages'] = sum(row['status'] == 'failed' for row in report['stages'])
            report['status'] = 'cancelled' if report['cancelled'] else 'completed_with_errors' if report['failed_stages'] else 'completed'
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
                if lease is not None:
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
        if isinstance(cleanup, dict) and 'deleted_count' in cleanup:
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
