"""Explicitly configured maintenance stages, durable reports, and safe preferences."""
from __future__ import annotations
import copy
import json
import ntpath
import os
import re
import threading
import time
import uuid
from pathlib import Path
from .filesystem import replace_with_retry
from .automation import CURRENT_SETUP_VERSION, RunLease, record_attempt

DEFAULTS = {
    'intensity': 'Standard', 'cleanup_enabled': True, 'cleanup_age_days': 7,
    'browser_cache_enabled': True, 'consumer_debloat_enabled': False,
    'm365_debloat_enabled': False, 'outlook_debloat_enabled': False,
    'edge_debloat_enabled': False, 'codex_priority_enabled': False,
    'codex_priority': 'High', 'background_analysis_enabled': False,
    'analysis_interval': 5, 'reduce_motion': False,
    'setup_version': 0, 'automation_mode': 'manual',
    'applications_review_enabled': False, 'performance_review_enabled': False,
    'startup_review_enabled': False, 'duplicates_enabled': False, 'duplicate_roots': [],
    'cloud_review_enabled': False, 'software_updates_enabled': False,
    'software_update_policy': 'review', 'software_update_ids': [],
    'software_update_agreements_accepted': False, 'driver_updates_enabled': False,
    'cleanup_category_ids': [],
}
_PACKAGE_ID = re.compile(r'^[A-Za-z0-9][A-Za-z0-9_.+\-]{1,127}$')
_CATEGORY_ID = re.compile(r'^[a-z0-9][a-z0-9_-]{0,79}$')


def atomic_json(path: Path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path = path.parent.resolve(strict=True) / path.name
    temporary = path.with_name(path.name + '.' + uuid.uuid4().hex + '.tmp')
    with temporary.open('w', encoding='utf-8') as stream:
        json.dump(value, stream, indent=2)
        stream.flush()
        os.fsync(stream.fileno())
    replace_with_retry(temporary, path)


class Preferences:
    def __init__(self, data_dir):
        self.path = Path(data_dir) / 'preferences.json'
        self.lock = threading.RLock()

    @staticmethod
    def safe_defaults():
        value = copy.deepcopy(DEFAULTS)
        value.update({'cleanup_enabled': False, 'browser_cache_enabled': False})
        return value

    def load(self):
        with self.lock:
            try:
                return self.validate(json.loads(self.path.read_text(encoding='utf-8')))
            except FileNotFoundError:
                return copy.deepcopy(DEFAULTS)
            except (OSError, ValueError, TypeError, json.JSONDecodeError):
                # Invalid stored settings never turn on cleanup or automatic work.
                return self.safe_defaults()

    @staticmethod
    def validate(value):
        if not isinstance(value, dict):
            raise ValueError('Settings must be an object.')
        result = copy.deepcopy(DEFAULTS)
        for key in DEFAULTS:
            if key in value:
                if isinstance(DEFAULTS[key], bool) and type(value[key]) is not bool:
                    raise ValueError(f'{key} must be on or off.')
                result[key] = copy.deepcopy(value[key])
        if result['intensity'] not in ('Low', 'Standard', 'High'):
            raise ValueError('Unknown optimization intensity.')
        if result['codex_priority'] not in ('Normal', 'AboveNormal', 'High'):
            raise ValueError('Unsupported Codex priority.')
        if type(result['cleanup_age_days']) is not int or not 1 <= result['cleanup_age_days'] <= 3650:
            raise ValueError('Cleanup age must be 1 to 3650 days.')
        if type(result['analysis_interval']) is not int or not 2 <= result['analysis_interval'] <= 60:
            raise ValueError('Analysis interval must be 2 to 60 seconds.')
        if type(result['setup_version']) is not int or not 0 <= result['setup_version'] <= CURRENT_SETUP_VERSION:
            raise ValueError('Unsupported setup version.')
        if result['automation_mode'] not in ('manual', 'startup', 'daily', 'weekly'):
            raise ValueError('Choose manual, startup, daily, or weekly maintenance.')
        if result['software_update_policy'] not in ('review', 'selected'):
            raise ValueError('Software updates require review or selected-ID policy.')
        for key, limit, pattern in (('software_update_ids', 100, _PACKAGE_ID), ('cleanup_category_ids', 50, _CATEGORY_ID)):
            items = result[key]
            if type(items) is not list or len(items) > limit:
                raise ValueError(f'{key} must be a list with at most {limit} entries.')
            if any(not isinstance(item, str) or not pattern.fullmatch(item) for item in items):
                raise ValueError(f'{key} contains an invalid exact identifier.')
            if len({item.casefold() for item in items}) != len(items):
                raise ValueError(f'{key} contains duplicate identifiers.')
        roots = result['duplicate_roots']
        if type(roots) is not list or len(roots) > 20:
            raise ValueError('Choose at most 20 duplicate-review folders.')
        normalized = []
        for root in roots:
            if not isinstance(root, str) or not 3 < len(root) <= 1024 or re.search(r'[\x00-\x1f*?<>|"%]', root):
                raise ValueError('Duplicate review requires absolute local folder paths.')
            drive, tail = ntpath.splitdrive(root)
            if not re.fullmatch(r'[A-Za-z]:', drive) or not tail.startswith(('\\', '/')) or ':' in tail:
                raise ValueError('Duplicate review requires an absolute local drive folder.')
            selected = ntpath.normpath(root)
            if selected.rstrip('\\/') == drive or any(part.endswith((' ', '.')) for part in selected[len(drive):].split('\\') if part):
                raise ValueError('Select a folder below a drive root for duplicate review.')
            normalized.append(selected)
        if len({root.casefold() for root in normalized}) != len(normalized):
            raise ValueError('Duplicate-review folders must be unique.')
        result['duplicate_roots'] = normalized
        if result['software_updates_enabled'] and result['software_update_policy'] == 'selected' and not result['software_update_agreements_accepted']:
            raise ValueError('Selected unattended updates require explicit agreement acceptance.')
        if result['software_updates_enabled'] and result['software_update_policy'] == 'selected' and not result['software_update_ids']:
            raise ValueError('Choose at least one exact software ID for unattended updates.')
        return result

    def save(self, value):
        result = self.validate(value)
        with self.lock:
            atomic_json(self.path, result)
        return result


def _fresh_software_candidates(scan):
    """Only a successful recognized WinGet table can authorize selected updates."""
    if not isinstance(scan, dict) or scan.get('structured') is not True or type(scan.get('exitCode')) is not int or scan['exitCode'] != 0:
        return {}
    items = scan.get('updates', scan.get('packages'))
    if not isinstance(items, list) or len(items) > 1000:
        return {}
    candidates, repeated, seen = {}, set(), set()
    for item in items:
        if not isinstance(item, dict):
            continue
        identity = item.get('id', item.get('package_id'))
        if not isinstance(identity, str) or not _PACKAGE_ID.fullmatch(identity):
            continue
        if identity.casefold() in seen:
            repeated.add(identity.casefold())
        seen.add(identity.casefold())
        if item.get('package_id', identity) != identity or item.get('source') != 'winget':
            continue
        version, available = item.get('version'), item.get('available')
        if any(not isinstance(part, str) or not part or part.casefold() in ('unknown', 'n/a', 'not available') or '<' in part or '>' in part or '...' in part or '\u2026' in part for part in (version, available)) or version == available:
            continue
        candidates[identity] = item
    for identity in list(candidates):
        if identity.casefold() in repeated:
            candidates.pop(identity, None)
    return candidates


class Workflow:
    def __init__(self, backend):
        self.backend = backend
        self.lock = threading.Lock()
        self.active = False
        self.last_report = None

    def run(self, settings, progress_callback, cancel_event=None):
        configuration = Preferences.validate(settings)
        if not self.lock.acquire(blocking=False):
            raise RuntimeError('An optimization run is already active.')
        self.active = True
        report, log_path, lease = None, None, None
        attempt_started = False
        try:
            lease = RunLease(self.backend.data_dir).acquire()
            run_id = uuid.uuid4().hex
            log_path = self.backend.data_dir / 'runs' / (run_id + '.json')
            report = {'id': run_id, 'started_at': time.time(), 'settings': configuration, 'stages': [], 'cancelled': False,
                      'status': 'running', 'note': 'Percent completion measures completed stages and selected items, not elapsed time. Daily/weekly automation runs only while Optimus is open. Review stages do not remove apps/files, change startup/process settings, or install drivers.'}
            self.last_report = report
            atomic_json(log_path, report)
            if configuration['setup_version'] >= CURRENT_SETUP_VERSION:
                record_attempt(self.backend.data_dir, configuration, run_id=run_id)
                attempt_started = True

            def cancelled():
                return cancel_event is not None and cancel_event.is_set()

            def review(operation, note):
                return {'review_only': True, 'result': operation(), 'note': note}

            stages = [('Inspect current resource usage', 'Reading running programs, memory, storage, startup entries and services.', self.backend.audit)]
            if configuration['applications_review_enabled']:
                stages.append(('Review installed applications', 'Collecting installed-program metadata; no uninstaller is launched.', lambda: review(self.backend.installed_apps, 'Review only; no applications were uninstalled.')))
            if configuration['performance_review_enabled']:
                stages.append(('Review application resource use', 'Measuring readable processes; no processes are suspended or reprioritized.', lambda: review(self.backend.processes, 'Review only; memory use alone does not prove an application is unnecessary.')))
            if configuration['startup_review_enabled']:
                stages.append(('Review startup registrations', 'Inspecting sign-in entries; no registration is changed.', lambda: review(self.backend.startup_list, 'Review only; no startup entry was disabled or deleted.')))
            if configuration['duplicates_enabled']:
                for root in configuration['duplicate_roots']:
                    stages.append(('Review duplicates: ' + ntpath.basename(root), 'Hashing eligible files in the selected folder with bounded scanning.', lambda root=root: review(lambda: self.backend.duplicate_scan(root), 'Review only; identical-content candidates require separate selected-file review. No file was recycled.')))
                if not configuration['duplicate_roots']:
                    stages.append(('Review duplicate configuration', 'No local folder has been selected.', lambda: {'review_only': True, 'skipped': True, 'note': 'Choose a local user folder to scan. No files were read or recycled.'}))
            if configuration['cloud_review_enabled']:
                def cloud_review():
                    status = self.backend.cloud_status()
                    if not isinstance(status, dict) or status.get('connected') is not True:
                        return {'review_only': True, 'skipped': True, 'status': status, 'note': 'Connect the supported personal OneDrive account explicitly before cloud review. No sign-in or cloud change was started.'}
                    if cancelled():
                        return {'cancelled': True, 'review_only': True}
                    return {'review_only': True, 'status': status, 'result': self.backend.cloud_scan(), 'note': 'Metadata review only; no cloud file was recycled or downloaded by this workflow.'}
                stages.append(('Review connected cloud drive', 'Inspecting existing connection and metadata-only duplicate candidates.', cloud_review))
            if configuration['driver_updates_enabled']:
                def driver_review():
                    result = self.backend.driver_updates_scan()
                    successful = isinstance(result, dict) and type(result.get('resultCode')) is int and result['resultCode'] == 2
                    return {'review_only': True, 'result': result, 'failed_items': 0 if successful else 1,
                            'note': 'Review only; no driver was downloaded or installed by this workflow. Windows Update must report a successful completed scan; partial/failed/unknown scan results require review.'}
                stages.append(('Review available driver updates', 'Checking Windows Update metadata; driver installation requires separate review.', driver_review))

            age = max(30, configuration['cleanup_age_days']) if configuration['intensity'] == 'Low' else configuration['cleanup_age_days']
            def clean(category_ids):
                plan = self.backend.cleanup_scan(category_ids, age)
                if cancelled():
                    return {'cancelled': True, 'reason': 'Cancelled after preview; no files were deleted.'}
                outcome = self.backend.cleanup_apply(plan) if plan.get('files') else {'deleted_count': 0, 'deleted_bytes': 0, 'skipped_count': 0}
                return {'preview_summary': {'files': len(plan.get('files', [])), 'bytes': plan.get('total_bytes', 0), 'complete': plan.get('complete'), 'skipped': plan.get('skipped')}, 'result': outcome}
            if configuration['cleanup_enabled']:
                stages.append(('Clean old temporary files', f'Removing approved unlocked temporary files at least {age} days old.', lambda: clean(['user-temp'])))
            if configuration['cleanup_category_ids']:
                def additional_cleanup():
                    known = {item['id'] for item in self.backend.cleanup_categories() if item.get('available') and isinstance(item.get('id'), str)}
                    selected = [item for item in configuration['cleanup_category_ids'] if item in known and item != 'user-temp']
                    if not selected:
                        return {'skipped': True, 'note': 'None of the separately selected additional categories is currently available.'}
                    return clean(selected)
                stages.append(('Clean selected additional categories', 'Applying only the configured categories that are currently available.', additional_cleanup))
            if configuration['browser_cache_enabled']:
                def browser_cleanup():
                    categories = [item['id'] for item in self.backend.cleanup_categories() if item.get('process_names') and item.get('available')]
                    return clean(categories) if categories else {'skipped': True, 'note': 'No eligible browser-cache category is currently available.'}
                stages.append(('Clean closed-browser caches', f'Cache files at least {age} days old; passwords, cookies, history and bookmarks are preserved.', browser_cleanup))
            for key, action, label in (
                ('consumer_debloat_enabled', 'consumer-apps', 'Remove selected consumer app group'),
                ('m365_debloat_enabled', 'm365', 'Remove Microsoft 365 consumer packages'),
                ('outlook_debloat_enabled', 'outlook', 'Remove new Outlook package'),
                ('edge_debloat_enabled', 'edge', 'Uninstall Microsoft Edge using the supported installer'),
            ):
                if configuration[key]:
                    def remove_group(action=action):
                        plan = self.backend.debloat_preview(action)
                        if cancelled():
                            return {'cancelled': True, 'reason': 'Cancelled after preview; no packages were removed.'}
                        ids = [item['id'] for item in plan.get('targets', []) if item.get('id')]
                        if not ids:
                            return {'skipped': True, 'reason': 'No eligible installed targets found.'}
                        outcome = self.backend.debloat_apply({'planId': plan['planId'], 'selectedIds': ids, 'confirmed': True})
                        if isinstance(outcome, dict) and isinstance(outcome.get('results'), list):
                            refused = sum(not isinstance(item, dict) or bool(item.get('error')) or item.get('removed') is False for item in outcome['results'])
                            if refused:
                                outcome = dict(outcome, failed_items=refused)
                        return outcome
                    stages.append((label, 'Applying only the curated package group explicitly enabled in Settings. Bing Wallpaper, Store, security, frameworks and WebView2 are preserved.', remove_group))
            if configuration['software_updates_enabled']:
                def software_updates():
                    scan = self.backend.updates_scan()
                    if not isinstance(scan, dict):
                        raise RuntimeError('The software update scan returned invalid metadata.')
                    if configuration['software_update_policy'] == 'review':
                        return {'review_only': True, 'scan': scan, 'failed_items': 0 if type(scan.get('exitCode')) is int and scan['exitCode'] == 0 else 1, 'note': 'Software update review only; no installer was started. Nonzero or unknown scan exit codes require review.'}
                    candidates = _fresh_software_candidates(scan)
                    selected = [identity for identity in configuration['software_update_ids'] if identity in candidates]
                    results = []
                    deferred = []
                    for index, identity in enumerate(selected):
                        if cancelled():
                            break
                        progress_callback({'percentage': min(99, int((completed + index / max(1, len(selected))) * 100 / len(stages))),
                                           'stage': 'Update configured software', 'description': f'Updating selected package {index + 1}/{len(selected)}: {identity}',
                                           'processes': ['Optimus workflow worker', 'Windows Package Manager; exact configured package'],
                                           'log': 'Updating configured ID: ' + identity, 'completed': completed, 'total': len(stages), 'item': index + 1, 'items': len(selected)})
                        try:
                            outcome = self.backend.update_app_unattended(identity)
                            successful = isinstance(outcome, dict) and type(outcome.get('exitCode')) is int and outcome['exitCode'] in (0, 3010, 1641) and outcome.get('success', True) is not False
                            results.append({'id': identity, 'status': 'completed' if successful else 'failed', 'result': outcome})
                            if isinstance(outcome, dict) and (outcome.get('running') is True or any(marker in str(outcome.get('status', '')).casefold() for marker in ('timed out', 'timeout', 'still running'))):
                                deferred = selected[index + 1:]
                                break
                        except Exception as error:
                            results.append({'id': identity, 'status': 'failed', 'error': str(error)})
                            if isinstance(error, TimeoutError) or any(marker in str(error).casefold() for marker in ('timed out', 'timeout', 'still running')):
                                deferred = selected[index + 1:]
                                break
                    return {'review_only': not bool(selected), 'scan': scan, 'items': results,
                            'failed_items': sum(item['status'] == 'failed' for item in results) + (0 if type(scan.get('exitCode')) is int and scan['exitCode'] == 0 else 1), 'cancelled': cancelled(), 'deferred_ids': deferred,
                            'not_available_ids': [identity for identity in configuration['software_update_ids'] if identity not in candidates],
                            'note': 'Only configured exact IDs from a successful fresh recognized winget-source scan may be updated. Unparsed, ambiguous, unknown-version and unselected packages remain unchanged. Driver installation is separate.'}
                stages.append(('Review or update configured software', 'Scanning first, then applying only configured exact IDs when selected policy and agreement authorization are enabled.', software_updates))
            if configuration['codex_priority_enabled']:
                stages.append(('Apply Codex performance profile', 'Applying your selected process priority. Existing external priority watchers remain unchanged.', lambda: self.backend.codex_profile(configuration['codex_priority'])))
            if configuration['background_analysis_enabled']:
                stages.append(('Enable resource analysis', 'Starting the optional read-only CPU, memory, disk and network sampler.', lambda: self.backend.analysis_start(configuration['analysis_interval'])))
            stages.append(('Capture final resource report', 'Saving final measurements and the outcome of each operation.', self.backend.audit))
            completed = 0
            for title, description, operation in stages:
                if cancelled():
                    report['cancelled'] = True
                    break
                process_lines = ['Optimus workflow worker']
                process = getattr(self.backend.bridge, '_process', None)
                if process is not None and process.poll() is None:
                    process_lines.append(f'Windows PowerShell engine (PID {process.pid})')
                progress_callback({'percentage': int(completed * 100 / len(stages)), 'stage': title,
                                   'description': description, 'processes': process_lines, 'log': f'Starting: {title}', 'completed': completed, 'total': len(stages)})
                started = time.time()
                try:
                    result = operation()
                    interrupted = cancelled() or (isinstance(result, dict) and result.get('cancelled') is True)
                    status = 'cancelled' if interrupted else 'failed' if isinstance(result, dict) and result.get('failed_items', 0) else 'completed'
                    stage = {'title': title, 'status': status, 'started_at': started, 'finished_at': time.time(), 'result': result}
                    if status == 'failed':
                        stage['error'] = 'One or more selected operations or scan checks failed; review the saved item results.'
                    if interrupted:
                        report['cancelled'] = True
                except Exception as error:
                    stage = {'title': title, 'status': 'failed', 'started_at': started, 'finished_at': time.time(), 'error': str(error)}
                report['stages'].append(stage)
                completed += 1
                atomic_json(log_path, report)
                percentage = int(completed * 100 / len(stages))
                if report['cancelled']:
                    percentage = min(99, percentage)
                progress_callback({'percentage': percentage, 'stage': title, 'description': stage.get('error', f"{title}: {stage['status']}"),
                                   'processes': process_lines, 'log': f"{stage['status'].upper()}: {title}" + (': ' + stage['error'] if stage.get('error') else ''), 'completed': completed, 'total': len(stages)})
            report['finished_at'] = time.time()
            report['failed_stages'] = sum(item['status'] == 'failed' for item in report['stages'])
            report['log_path'] = str(log_path)
            report['status'] = 'cancelled' if report['cancelled'] else 'completed_with_errors' if report['failed_stages'] else 'completed'
            atomic_json(log_path, report)
            if attempt_started:
                record_attempt(self.backend.data_dir, configuration, status='cancelled' if report['cancelled'] else 'failed' if report['failed_stages'] else 'completed', run_id=run_id,
                               error=f"{report['failed_stages']} stages failed" if report['failed_stages'] else None)
            return report
        except Exception as error:
            if report is None:
                report = {'settings': configuration, 'stages': [], 'cancelled': False}
            report.update({'status': 'failed', 'error': str(error), 'finished_at': time.time()})
            if log_path is not None:
                report['log_path'] = str(log_path)
                try:
                    atomic_json(log_path, report)
                except Exception as journal_error:
                    report['journal_error'] = str(journal_error)
            if attempt_started:
                try:
                    record_attempt(self.backend.data_dir, configuration, status='failed', run_id=run_id, error=error)
                except Exception as state_error:
                    report['automation_state_error'] = str(state_error)
            self.last_report = report
            raise
        finally:
            try:
                if lease is not None:
                    lease.release()
            finally:
                self.active = False
                self.lock.release()
