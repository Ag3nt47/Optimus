"""Explicitly configured maintenance stages, durable reports, and safe preferences."""
from __future__ import annotations
import copy
import json
import math
import ntpath
import os
import re
import threading
import time
import uuid
from pathlib import Path
from .filesystem import replace_with_retry
from .automation import CURRENT_SETUP_VERSION, RunLease, record_attempt
from .approval_policy import APPROVAL_DEFAULTS, validate_approvals
from .smart_configuration import SMART_DEFAULTS, validate_smart_configuration
from .unattended_configuration import UNATTENDED_DEFAULTS, validate_unattended_configuration
from .software_app_rules import APP_RULE_DEFAULTS, validate_app_rules

CONSUMER_PACKAGE_NAMES = (
    'Microsoft.BingNews', 'Microsoft.BingWeather', 'Microsoft.Getstarted',
    'Microsoft.MicrosoftSolitaireCollection', 'Microsoft.Microsoft3DViewer', 'Microsoft.Print3D',
    'Microsoft.MixedReality.Portal', 'Microsoft.People', 'Microsoft.WindowsMaps',
    'Microsoft.WindowsFeedbackHub', 'Microsoft.549981C3F5F10', 'Microsoft.SkypeApp',
    'Microsoft.Windows.DevHome', 'Microsoft.MicrosoftPCManager', 'MicrosoftCorporationII.Windows365',
    'MicrosoftCorporationII.MicrosoftFamily', 'Clipchamp.Clipchamp', 'MicrosoftTeams', 'MSTeams', 'Microsoft.Copilot',
)

DEFAULTS = {
    'optimization_mode': 'custom',
    'request_admin_on_startup': False,
    'launch_at_sign_in': False,
    'intensity': 'Standard', 'cleanup_enabled': True, 'cleanup_age_days': 7,
    'browser_cache_enabled': True, 'consumer_debloat_enabled': False,
    'consumer_debloat_package_names': list(CONSUMER_PACKAGE_NAMES),
    'browser_history_enabled': False, 'browser_cookies_enabled': False,
    'browser_download_history_enabled': False,
    'browser_cleanup_browsers': ['edge', 'chrome', 'brave', 'vivaldi', 'firefox'],
    'browser_keep_domains': [],
    'm365_debloat_enabled': False, 'outlook_debloat_enabled': False,
    'edge_debloat_enabled': False, 'codex_priority_enabled': False,
    'codex_priority': 'High', 'background_analysis_enabled': False,
    'analysis_interval': 5, 'reduce_motion': False,
    'setup_version': 0, 'automation_mode': 'manual',
    'applications_review_enabled': False, 'performance_review_enabled': False,
    'speed_enhancements_enabled': False,
    'drive_optimization_enabled': False, 'auto_continue_fixes_enabled': False,
    'parallel_app_updates_enabled': False,
    'interface_priority_boost_enabled': True,
    'update_compatibility_mode_enabled': False, 'windows_cpu_compatibility_enabled': False,
    'startup_review_enabled': False, 'duplicates_enabled': False, 'duplicate_roots': [],
    'cloud_review_enabled': False, 'software_updates_enabled': False,
    'software_update_policy': 'review', 'software_update_ids': [],
    'software_update_all_approved': False, 'software_update_exclude_ids': [],
    'software_update_agreements_accepted': False, 'driver_updates_enabled': False,
    'cleanup_category_ids': [],
    'codex_archives_cleanup_enabled': False, 'codex_archives_retention_days': 7,
    'angerona_logs_cleanup_enabled': False, 'angerona_logs_retention_days': 7,
    'failed_downloads_cleanup_enabled': False, 'failed_downloads_retention_days': 7,
    'drive_space_guard_enabled': True, 'drive_space_guard_target_gib': 2.0,
    'post_process_review_enabled': False,
    'privacy_advertising_enabled': False, 'privacy_diagnostics_enabled': False,
    'privacy_block_silent_consumer_apps_enabled': False,
    'privacy_disable_cloud_promotions_enabled': False,
    'privacy_disable_clipboard_sync_enabled': False,
    'privacy_disable_recall_snapshots_enabled': False,
    'privacy_disable_advertising_id': True, 'privacy_disable_tailored_experiences': True,
    'privacy_disable_optional_diagnostics': True, 'privacy_disable_app_diagnostics': True,
    'privacy_disable_suggestions': True,
    'spyware_scan_enabled': False, 'security_review_enabled': False,
    'windows_security_updates_enabled': False,
    'windows_security_updates_install_approved': False,
    'windows_security_update_agreements_accepted': False,
    'legacy_patch_review_enabled': False,
}
DEFAULTS.update(copy.deepcopy(APPROVAL_DEFAULTS))
DEFAULTS.update(copy.deepcopy(SMART_DEFAULTS))
DEFAULTS.update(copy.deepcopy(UNATTENDED_DEFAULTS))
DEFAULTS.update(copy.deepcopy(APP_RULE_DEFAULTS))
_PACKAGE_ID = re.compile(r'^[A-Za-z0-9][A-Za-z0-9_.+\-]{1,127}$')
_CATEGORY_ID = re.compile(r'^[a-z0-9][a-z0-9_-]{0,79}$')
FINAL_VERIFICATION_STAGE = 'Verify optimization outcomes'


def _unavailable_verification(reason):
    return {'schema': 1, 'read_only': True, 'status': 'unverified',
            'counts': {'confirmed': 0, 'needs_review': 0, 'unverified': 1, 'not_applicable': 0},
            'stages': [], 'coverage_complete': False, 'deadline_reached': False, 'failed_items': 1,
            'summary': 'The final outcome check could not be completed; outcomes remain unverified.',
            'errors': [reason]}


def full_optimization_settings(settings=None):
    """Select Full once; never reapply a preset over saved changes on opening.

    Preserve exact scopes, exclusions, agreements and cookie exceptions. Full
    enables update discovery; saved approvals determine whether updates apply.
    Process removal and security recommendations always require review.
    """
    value = copy.deepcopy(DEFAULTS)
    if settings is not None:
        value.update(copy.deepcopy(settings))
    value.update({
        'optimization_mode': 'full', 'automation_mode': 'startup',
        'request_admin_on_startup': True,
        'intensity': 'High', 'cleanup_enabled': True, 'cleanup_age_days': 0,
        'browser_cache_enabled': True, 'browser_history_enabled': True,
        'browser_cookies_enabled': True, 'browser_download_history_enabled': True,
        'applications_review_enabled': True, 'performance_review_enabled': True,
        'speed_enhancements_enabled': True,
        'drive_optimization_enabled': True, 'auto_continue_fixes_enabled': True,
        'parallel_app_updates_enabled': True,
        'startup_review_enabled': True, 'software_updates_enabled': True,
        'driver_updates_enabled': True, 'post_process_review_enabled': True,
        'privacy_advertising_enabled': True, 'privacy_diagnostics_enabled': True,
        'spyware_scan_enabled': True, 'security_review_enabled': True,
        'windows_security_updates_enabled': True,
        'legacy_patch_review_enabled': True,
        'consumer_debloat_enabled': True,
    })
    if not value.get('browser_cleanup_browsers'):
        value['browser_cleanup_browsers'] = copy.deepcopy(DEFAULTS['browser_cleanup_browsers'])
    value['cleanup_category_ids'] = list(dict.fromkeys([*value.get('cleanup_category_ids', []), 'windows-temp', 'canonical-user-temp']))
    # Selecting the preset is not agreement acceptance or an invented device,
    # account, file, startup or update approval.
    if not value.get('software_update_agreements_accepted'):
        value['software_update_policy'] = 'review'
    elif value.get('software_update_all_approved'):
        value['software_update_policy'] = 'all_supported'
    elif not value.get('software_update_ids'):
        value['software_update_policy'] = 'review'
    if not (value.get('driver_changes_approved') and value.get('driver_update_agreements_accepted')
            and (value.get('driver_approved_updates') or value.get('driver_approved_devices'))):
        value['driver_policy'] = 'review'
    # Only the concrete consumer list is selected by this preset. Other removal
    # groups retain their explicitly saved individual choices.
    return value


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
        value.update({'cleanup_enabled': False, 'browser_cache_enabled': False,
                      'automation_mode': 'manual', 'optimization_mode': 'custom'})
        return value

    def load(self):
        with self.lock:
            try:
                return self.validate(json.loads(self.path.read_text(encoding='utf-8')))
            except FileNotFoundError:
                return full_optimization_settings()
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
        if result['optimization_mode'] not in ('full', 'custom'):
            raise ValueError('Choose Full Optimization or Custom mode.')
        if result['codex_priority'] not in ('Normal', 'AboveNormal', 'High'):
            raise ValueError('Unsupported Codex priority.')
        if type(result['cleanup_age_days']) is not int or not 0 <= result['cleanup_age_days'] <= 3650:
            raise ValueError('Cleanup age must be 0 to 3650 days (0 includes all eligible temporary files).')
        if type(result['analysis_interval']) is not int or not 2 <= result['analysis_interval'] <= 60:
            raise ValueError('Analysis interval must be 2 to 60 seconds.')
        from .retention_configuration import validate_retention_scope
        validate_retention_scope(result, require_all=True)
        if (type(result['drive_space_guard_target_gib']) not in (int, float)
                or not math.isfinite(result['drive_space_guard_target_gib'])
                or not 1 <= result['drive_space_guard_target_gib'] <= 2):
            raise ValueError('Drive space buffer target must be a finite number from 1 to 2 GiB.')
        if type(result['setup_version']) is not int or not 0 <= result['setup_version'] <= CURRENT_SETUP_VERSION:
            raise ValueError('Unsupported setup version.')
        if result['automation_mode'] not in ('manual', 'startup', 'daily', 'weekly', 'monthly'):
            raise ValueError('Choose manual, startup, daily, weekly, or monthly maintenance.')
        result.update(validate_smart_configuration(result))
        result.update(validate_unattended_configuration(result))
        result['software_app_rules'] = validate_app_rules(result['software_app_rules'])
        if result['software_update_policy'] not in ('review', 'selected', 'all_supported'):
            raise ValueError('Software updates require review, selected IDs, or all-supported policy.')
        packages = result['consumer_debloat_package_names']
        if (type(packages) is not list or len(packages) > len(CONSUMER_PACKAGE_NAMES)
                or any(not isinstance(item, str) or item not in CONSUMER_PACKAGE_NAMES for item in packages)
                or len(set(packages)) != len(packages)):
            raise ValueError('Choose unique exact names from the supported consumer-app removal list.')
        browsers = result['browser_cleanup_browsers']
        if type(browsers) is not list or len(browsers) > 7 or any(not isinstance(item, str) or item not in ('edge', 'chrome', 'chromium', 'brave', 'vivaldi', 'firefox', 'opera') for item in browsers) or len(set(browsers)) != len(browsers):
            raise ValueError('Choose unique supported browser identifiers for browser cleanup.')
        if type(result['browser_keep_domains']) is not list:
            raise ValueError('Cookie keep domains must be a list of plain host names.')
        from .browser_privacy import _domains
        result['browser_keep_domains'] = _domains(result['browser_keep_domains'])
        if any(result[key] for key in ('browser_cache_enabled', 'browser_history_enabled', 'browser_cookies_enabled', 'browser_download_history_enabled')) and not browsers:
            raise ValueError('Choose at least one browser for the enabled browser cleanup categories.')
        for key, limit, pattern in (('software_update_ids', 100, _PACKAGE_ID), ('software_update_exclude_ids', 100, _PACKAGE_ID), ('cleanup_category_ids', 50, _CATEGORY_ID)):
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
        active_rules = result['software_updates_enabled'] and any(
            row['update_policy'] == 'always_update' or row['presence'] != 'keep_current'
            for row in result['software_app_rules'])
        if active_rules and (result['setup_version'] != CURRENT_SETUP_VERSION or not result['software_update_agreements_accepted']):
            raise ValueError('Complete setup and accept package agreements before automatic per-app update/install/uninstall actions.')
        if result['software_updates_enabled'] and result['software_update_policy'] != 'review' and not result['software_update_agreements_accepted']:
            raise ValueError('Unattended software updates require explicit agreement acceptance.')
        if (result['software_updates_enabled'] and result['software_update_policy'] == 'selected'
                and not result['software_update_ids'] and not any(row['update_policy'] == 'always_update' for row in result['software_app_rules'])):
            raise ValueError('Choose at least one exact software ID for unattended updates.')
        if result['software_updates_enabled'] and result['software_update_policy'] == 'all_supported' and not result['software_update_all_approved']:
            raise ValueError('Updating all supported installed applications requires separate explicit scope approval.')
        if (result['windows_security_updates_enabled'] and result['windows_security_updates_install_approved']
                and not result['windows_security_update_agreements_accepted']):
            raise ValueError('Automatic Windows Security/Critical updates require explicit Microsoft agreement acceptance.')
        return validate_approvals(result)

    def save(self, value):
        result = self.validate(value)
        with self.lock:
            atomic_json(self.path, result)
        return result


def _fresh_software_candidates(scan):
    """Compatibility helper for fresh scoped software candidates."""
    from .automated_software import fresh_candidates
    return fresh_candidates(scan)


def _space_guard_result(backend, settings):
    """Read-only guard failures remain visible without starting extra cleanup."""
    operation = getattr(backend, 'space_guard_check', None)
    if not callable(operation):
        return {'schema': 1, 'review_only': True, 'enabled': settings.get('drive_space_guard_enabled') is True,
                'complete': False, 'status': 'unsupported', 'volumes': [],
                'warnings': ['Drive-space buffer verification is unavailable for this backend.'],
                'errors': [], 'summary': 'Drive-space buffer verification is unavailable for this backend.'}
    try:
        result = operation(settings)
        if not isinstance(result, dict) or result.get('review_only') is not True or not isinstance(result.get('volumes'), list):
            raise ValueError('The drive-space check returned incomplete read-only evidence.')
        return result
    except Exception as error:
        return {'schema': 1, 'review_only': True, 'enabled': True, 'complete': False, 'status': 'needs_review',
                'volumes': [], 'warnings': ['Drive-space buffers could not be verified; review available free space.'],
                'errors': [{'error': str(error)[:500]}], 'failed_items': 1,
                'summary': 'Drive-space buffers could not be verified; review available free space.'}

class Workflow:
    def __init__(self, backend):
        self.backend = backend
        from .navigation import NavigationHub
        self.navigation = NavigationHub()
        self.lock = threading.Lock()
        self.active = False
        self.last_report = None

    def run(self, settings, progress_callback, cancel_event=None, *, browser_prompt=None):
        global_cancel_event = cancel_event
        configuration = Preferences.validate(settings)
        if not self.lock.acquire(blocking=False):
            raise RuntimeError('An optimization run is already active.')
        self.active = True
        report, log_path, lease, parallel_job = None, None, None, None
        attempt_started = False
        try:
            from .parallel_updates import (ProgressMux, ParallelAppUpdates, ParallelInstallerQueue, WINDOWS_SOFTWARE, WINDOWS_DRIVERS,
                                           WINDOWS_DRIVER_REVIEW, DRIVER_TITLES,
                                           independent_stage, ready_maintenance_stages, BEFORE_UPDATES, BLOCKED_STAGES, installer_barrier,
                                           pending_updates_barrier, parallel_installer_stages, record_installer_outcome)
            progress_callback = ProgressMux(progress_callback)
            lease = RunLease(self.backend.data_dir).acquire()
            unresolved_apps = pending_updates_barrier(self.backend)
            run_id = uuid.uuid4().hex
            self.navigation.begin(run_id, cancel_event=global_cancel_event)
            maintenance_navigation = self.navigation.cursor('maintenance', 'Maintenance')
            cancel_event = maintenance_navigation.cancellation(global_cancel_event)
            log_path = self.backend.data_dir / 'runs' / (run_id + '.json')
            report = {'id': run_id, 'started_at': time.time(), 'settings': configuration, 'stages': [], 'cancelled': False,
                      'status': 'running', 'note': 'Percent completion measures completed stages and selected items, not elapsed time. Daily/weekly automation runs only while Optimus is open. Review stages do not make changes. Apply stages require the current setup and saved scope approvals; targets are revalidated on each run.'}
            self.last_report = report
            atomic_json(log_path, report)
            if configuration['setup_version'] >= CURRENT_SETUP_VERSION:
                record_attempt(self.backend.data_dir, configuration, run_id=run_id)
                attempt_started = True

            def cancelled():
                return cancel_event is not None and cancel_event.is_set()

            def review(operation, note):
                return {'review_only': True, 'result': operation(), 'note': note}

            def space_guard(*, initial=False):
                result = _space_guard_result(self.backend, configuration)
                report['space_guard'] = result
                if initial:
                    report['initial_space_guard'] = result
                progress_callback.update_space_guard(result)
                return result

            serial_job = None
            stages = [('Inspect current resource usage', 'Reading running programs, memory, storage, startup entries and services.', self.backend.audit)]
            has_space_guard = configuration['drive_space_guard_enabled'] and callable(getattr(self.backend, 'space_guard_check', None))
            if has_space_guard:
                stages.append(('Check drive space buffers', 'Checking the saved temporary-space target before cleanup and installers; no reserve file or disk setting is changed.', lambda: space_guard(initial=True)))
            elif configuration['drive_space_guard_enabled']:
                space_guard(initial=True)
            if configuration['applications_review_enabled']:
                stages.append(('Review installed applications', 'Collecting installed-program metadata; no uninstaller is launched.', lambda: review(self.backend.installed_apps, 'Review only; no applications were uninstalled.')))
            if configuration['performance_review_enabled']:
                stages.append(('Identify programs contributing to slowdowns', 'Sampling CPU, memory and disk activity to identify sustained pressure and explain each program.', lambda: review(lambda: self.backend.slowdown_report(cancel_event=cancel_event), 'Measured review only; no process is suspended, killed or reprioritized automatically.')))
            def item_progress(title):
                def emit(state):
                    state = state if isinstance(state, dict) else {'description': str(state)}
                    if serial_job is not None:
                        serial_job.item_progress(state)
                    current = state.get('current', state.get('processed', state.get('item', 0)))
                    total = state.get('total', state.get('items', 0))
                    fraction = max(0, min(1, current / total)) if type(current) in (int, float) and type(total) in (int, float) and total > 0 else 0
                    description = str(state.get('description', state.get('item', title)))
                    from .item_progress import normalize_item_progress
                    current_progress = normalize_item_progress(state.get('item_progress'))
                    if current_progress is None:
                        current_progress = normalize_item_progress({'key': 'maintenance:' + title,
                            'label': description, 'phase': str(state.get('phase') or 'working'), 'percent': None})
                    progress_callback({'item_progress': current_progress,
                                       'deleted_bytes': state.get('deleted_bytes'),
                                       'deleted_count': state.get('deleted_count'),
                                       'percentage': min(99, int((completed + fraction) * 100 / total_stages)),
                                       'stage': title, 'stage_status': 'running', 'description': description,
                                       'processes': ['Optimus maintenance worker', description],
                                       'log': str(state.get('log', description)), 'completed': completed, 'total': total_stages,
                                       'item': state.get('item', current), 'items': state.get('items', total)})
                return emit

            if configuration['speed_enhancements_enabled']:
                stages.append(('Apply supported speed enhancements',
                               'Applying eligible native power-performance settings under the saved automatic choice, with recovery records and fresh readback.',
                               lambda: self.backend._apply_speed_enhancements_stage(configuration, cancel_event=cancel_event,
                                   progress_callback=item_progress('Apply supported speed enhancements'))))
            if configuration['windows_cpu_compatibility_enabled']:
                stages.append(('Prepare approved Windows CPU upgrade workaround',
                               'Recording and verifying the selected legacy media-Setup compatibility policy. This does not install Windows or bypass storage, bootability or installer security checks.',
                               lambda: self.backend._prepare_windows_compatibility_stage(configuration, cancel_event=cancel_event)))
            if configuration['startup_review_enabled']:
                if configuration['startup_policy'] == 'disable_selected':
                    from .automated_system import run_startup
                    stages.append(('Disable approved startup entries', 'Revalidating saved registration identities and disabling only approved matches with recovery records.',
                                   lambda: run_startup(self.backend, configuration, cancel_event=cancel_event, item_callback=item_progress('Disable approved startup entries'))))
                else:
                    stages.append(('Review startup registrations', 'Inspecting sign-in entries; no registration is changed.', lambda: review(self.backend.startup_list, 'Review only; no startup entry was disabled or deleted.')))
            if configuration['duplicates_enabled']:
                if configuration['duplicate_policy'] != 'review':
                    from .automated_files import run_local
                    stages.append(('Recycle approved local duplicates', 'Scanning only approved folders; applying the saved keeper, age and per-run limit after current identity checks.',
                                   lambda: run_local(self.backend, configuration, cancel_event=cancel_event, item_callback=item_progress('Recycle approved local duplicates'))))
                else:
                    for root in configuration['duplicate_roots']:
                        stages.append(('Review duplicates: ' + ntpath.basename(root), 'Hashing eligible files in the selected folder with bounded scanning.', lambda root=root: review(lambda: self.backend.duplicate_scan(root), 'Review only; identical-content candidates require separate selected-file review. No file was recycled.')))
                    if not configuration['duplicate_roots']:
                        stages.append(('Review duplicate configuration', 'No local folder has been selected.', lambda: {'review_only': True, 'skipped': True, 'note': 'Choose a local user folder to scan. No files were read or recycled.'}))
            if configuration['cloud_review_enabled']:
                if configuration['cloud_policy'] != 'review':
                    from .automated_files import run_cloud
                    stages.append(('Recycle approved cloud duplicates', 'Rechecking the approved personal drive/folders and comparing content before bounded recycling. No sign-in is started automatically.',
                                   lambda: run_cloud(self.backend, configuration, cancel_event=cancel_event, item_callback=item_progress('Recycle approved cloud duplicates'))))
                else:
                    def cloud_review():
                        status = self.backend.cloud_status()
                        if not isinstance(status, dict) or status.get('connected') is not True:
                            return {'review_only': True, 'skipped': True, 'status': status, 'note': 'Connect the supported personal OneDrive account explicitly before cloud review. No sign-in or cloud change was started.'}
                        if cancelled():
                            return {'cancelled': True, 'review_only': True}
                        return {'review_only': True, 'status': status, 'result': self.backend.cloud_scan(), 'note': 'Metadata review only; no cloud file was recycled or downloaded by this workflow.'}
                    stages.append(('Review connected cloud drive', 'Inspecting existing connection and metadata-only duplicate candidates.', cloud_review))
            if configuration['driver_updates_enabled']:
                if configuration['driver_policy'] != 'review':
                    from .automated_system import run_drivers
                    stages.append(('Install approved driver updates', 'Matching current Windows Update candidates to saved update/device approvals, checking elevation and backing up drivers before quiet installation.',
                                   lambda: run_drivers(self.backend, configuration, cancel_event=cancel_event, item_callback=item_progress('Install approved driver updates'))))
                else:
                    def driver_review():
                        from .automated_system import run_drivers
                        result = run_drivers(self.backend, configuration, cancel_event=cancel_event)
                        return {**result, 'result': result.get('scan'),
                                'note': 'Review only; no driver was downloaded or installed by this workflow. Windows Update must report a successful completed scan; partial/failed/unknown scan results require review.'}
                    stages.append(('Review available driver updates', 'Checking Windows Update metadata; driver installation requires separate review.', driver_review))

            def progress_operation(operation, *args, title, **kwargs):
                from .item_progress import accepts_progress_callback
                if accepts_progress_callback(operation):
                    kwargs['progress_callback'] = item_progress(title)
                return operation(*args, **kwargs)

            age = max(30, configuration['cleanup_age_days']) if configuration['intensity'] == 'Low' else configuration['cleanup_age_days']
            def clean(category_ids):
                title = ('Clean temporary files' if category_ids == ['user-temp'] else
                         'Clean selected browser data' if any(':' in value for value in category_ids) else
                         'Clean selected additional categories')
                plan = progress_operation(self.backend.cleanup_scan, category_ids, age, title=title)
                if cancelled():
                    return {'cancelled': True, 'reason': 'Cancelled after preview; no files were deleted.'}
                outcome = progress_operation(self.backend.cleanup_apply, plan, title=title) if plan.get('files') else {'deleted_count': 0, 'deleted_bytes': 0, 'skipped_count': 0}
                if isinstance(plan.get('id'), str):
                    progress_callback.removal_receipt('cleanup:' + plan['id'], outcome)
                errors = plan.get('errors', [])
                preview_partial = plan.get('complete') is not True or bool(errors)
                refusals = max(outcome.get('skipped_count', 0), len(outcome.get('errors', [])))
                partial = preview_partial or refusals > 0
                note = ('Only individually verified eligible files were considered. The scan was incomplete; review its access errors or limits before treating cleanup as complete.' if preview_partial else
                        'Some listed files could not be removed after revalidation or opening. Review the apply errors and skipped files; those files are not counted as deleted.' if refusals else
                        'The preview and selected apply operations completed; review any category-level skip notes.')
                return {'preview_summary': {'files': len(plan.get('files', [])), 'bytes': plan.get('total_bytes', 0),
                                            'complete': plan.get('complete'), 'skipped': plan.get('skipped'), 'errors': errors},
                        'result': outcome, 'partial': partial, 'failed_items': (max(1, len(errors)) if preview_partial else 0) + refusals,
                        'note': note}
            if configuration['cleanup_enabled']:
                stages.append(('Clean temporary files',
                               'Removing all eligible unlocked files in the approved temporary-file category.' if age == 0 else f'Removing approved unlocked temporary files at least {age} days old.',
                               lambda: clean(['user-temp'])))
            if configuration['cleanup_category_ids']:
                def additional_cleanup():
                    known = {item['id'] for item in self.backend.cleanup_categories() if item.get('available') and isinstance(item.get('id'), str)}
                    selected = [item for item in configuration['cleanup_category_ids'] if item in known and item != 'user-temp']
                    if not selected:
                        return {'skipped': True, 'note': 'None of the separately selected additional categories is currently available.'}
                    return clean(selected)
                stages.append(('Clean selected additional categories', 'Applying only the configured categories that are currently available.', additional_cleanup))
            if any(configuration[key] for key in ('browser_cache_enabled', 'browser_history_enabled', 'browser_cookies_enabled', 'browser_download_history_enabled')):
                from .browser_cleanup import run_browser_cleanup
                stages.append(('Clean selected browser data',
                    'Applying only selected browser categories. Each open browser needs Close and clean permission for this run; No skips that browser. Passwords, bookmarks and downloaded files are preserved.',
                    lambda: run_browser_cleanup(self.backend, configuration, clean=clean,
                        browser_prompt=browser_prompt, cancel_event=cancel_event,
                        item_callback=item_progress('Clean selected browser data'))))
            for retention_kind, retention_title in (
                ('codex_archives', 'Clean archived Codex conversations'),
                ('angerona_logs', 'Clean inactive Angerona logs'),
                ('failed_downloads', 'Clean incomplete download leftovers'),
            ):
                if configuration[retention_kind + '_cleanup_enabled']:
                    def retention_operation(kind=retention_kind, title=retention_title):
                        operation = (self.backend.codex_archive_cleanup if kind == 'codex_archives' else
                                     lambda settings, **options: self.backend.retention_cleanup(kind, settings, **options))
                        result = operation(configuration, progress_callback=item_progress(title), cancel_event=cancel_event)
                        if isinstance(result, dict) and isinstance(result.get('cleanup_id'), str):
                            progress_callback.removal_receipt('cleanup:' + result['cleanup_id'], result)
                        return result
                    stages.append((retention_title,
                        f'Applying only this explicitly enabled retention scope, older than {configuration[retention_kind + "_retention_days"]} days; active, unknown and unsupported items are preserved.',
                        retention_operation))
            if configuration['drive_optimization_enabled']:
                stages.append(('Optimize eligible internal drives',
                               'Checking eligible internal drives and running the native SSD TRIM/HDD optimization only when current power, space, health and cadence checks allow it.',
                               lambda: self.backend._apply_drive_optimization_stage(configuration, cancel_event=cancel_event,
                                   progress_callback=item_progress('Optimize eligible internal drives'))))
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
                        targets = plan.get('targets', [])
                        if action == 'consumer-apps':
                            names = set(configuration['consumer_debloat_package_names'])
                            targets = [item for item in targets if item.get('name') in names
                                       and item.get('kind') == 'appx' and item.get('scope') == 'current-user'
                                       and 'bingwallpaper' not in re.sub(r'[^a-z0-9]', '', str(item.get('name', '')).casefold())]
                        ids = [item['id'] for item in targets if item.get('id')]
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
                from .automated_software import run_software
                def software_updates():
                    from .post_run_fixes import _prior_installer_barrier
                    barrier = _prior_installer_barrier(self.backend, report)
                    if barrier:
                        return {'skipped': True, 'requiresReview': True, 'review_only': True,
                                'items': [], 'failed_items': 0, 'stop_reason': barrier, 'note': barrier}
                    return run_software(self.backend, configuration, cancel_event=cancel_event,
                                        item_callback=item_progress('Review or update configured software'))
                stages.append(('Review or update configured software', 'Scanning supported installed applications, respecting saved exact IDs/all-supported scope, exclusions and pins.',
                               software_updates))
            if configuration['codex_priority_enabled']:
                stages.append(('Apply Codex performance profile', 'Applying your selected process priority. Existing external priority watchers remain unchanged.', lambda: self.backend.codex_profile(configuration['codex_priority'])))
            if configuration['background_analysis_enabled']:
                stages.append(('Enable resource analysis', 'Starting the optional read-only CPU, memory, disk and network sampler.', lambda: self.backend.analysis_start(configuration['analysis_interval'])))
            if any(configuration[key] for key in ('privacy_advertising_enabled', 'privacy_diagnostics_enabled',
                    'privacy_block_silent_consumer_apps_enabled', 'privacy_disable_cloud_promotions_enabled',
                    'privacy_disable_clipboard_sync_enabled', 'privacy_disable_recall_snapshots_enabled')):
                stages.append(('Apply saved privacy controls',
                               'Disabling selected Windows advertising and optional diagnostic sharing settings, with recovery records; managed or administrator-only settings may need review.',
                               lambda: progress_operation(self.backend.apply_system_privacy, configuration,
                                   cancel_event=cancel_event, title='Apply saved privacy controls')))
            if configuration['windows_security_updates_enabled']:
                def security_updates():
                    result = self.backend.windows_security_updates(configuration, cancel_event=cancel_event,
                        item_callback=item_progress('Check or install Windows security updates'))
                    report['windows_security_updates'] = result
                    return result
                stages.append(('Check or install Windows security updates',
                               'Checking applicable Security/Critical patches through the configured Windows Update service; installation uses saved scope and agreement approvals, without an automatic reboot.', security_updates))
            if configuration['spyware_scan_enabled']:
                stages.append(('Scan for spyware and malware',
                               'Running a Microsoft Defender quick scan using its detection and remediation policy; no arbitrary process or application is classified as spyware.',
                               lambda: self.backend.scan_spyware(cancel_event=cancel_event)))
            if configuration['security_review_enabled']:
                def security_review():
                    result = self.backend.security_status()
                    if isinstance(result, dict):
                        result['update_coverage'] = [
                            {'channel': 'Windows Security/Critical patches', 'enabled': configuration['windows_security_updates_enabled'],
                             'result': report.get('windows_security_updates'), 'source': 'https://learn.microsoft.com/windows/win32/wua_sdk/portal-client'},
                            {'channel': 'Third-party application security and maintenance updates', 'enabled': configuration['software_updates_enabled'],
                             'note': 'WinGet checks supported installed apps for newer vendor versions. Unsupported catalogs and unknown versions require the publisher updater. This does not identify every installed vulnerability.',
                             'source': 'https://learn.microsoft.com/windows/package-manager/winget/upgrade'},
                            {'channel': 'OEM drivers, BIOS and device firmware', 'enabled': configuration['driver_updates_enabled'],
                             'note': 'Windows Update driver coverage and official manufacturer support links are available in Drivers. Vendor-only BIOS/firmware and out-of-band patches require model-specific vendor review; no offer is not proof of newest firmware.'},
                        ]
                        if configuration['windows_security_updates_enabled']:
                            patch_report = report.get('windows_security_updates', {})
                            patch_scan = patch_report.get('scan', {}) if isinstance(patch_report, dict) else {}
                            if (isinstance(patch_scan, dict) and patch_scan.get('updates')
                                    and (patch_report.get('review_only') or patch_report.get('skipped') or patch_report.get('failed_items'))):
                                recommendations = result.setdefault('recommendations', [])
                                if not any(item.get('id') == 'review_windows_update' for item in recommendations if isinstance(item, dict)):
                                    recommendations.append({'id': 'review_windows_update', 'title': 'Review available Windows security patches',
                                        'reason': 'Applicable patches were reported, but the automatic installation was not completed. Review the scope, agreements, permissions and restart state in Windows Update.',
                                        'action': 'review_windows_update', 'mitigations': [{'id': 'M1051', 'name': 'Update Software', 'url': 'https://attack.mitre.org/mitigations/M1051/'}]})
                    report['security_review'] = result
                    return result
                stages.append(('Check basic system security',
                               'Checking protection and firewall status; remaining recommendations are presented for Yes/No decisions afterward.', security_review))
            if configuration['legacy_patch_review_enabled']:
                def legacy_review():
                    security = report.setdefault('security_review', {'recommendations': [], 'review_only': True})
                    legacy = self.backend.legacy_patch_review(security.get('context') if isinstance(security, dict) else None)
                    report['legacy_patch_review'] = legacy
                    if isinstance(security, dict):
                        security['legacy_patch_review'] = legacy
                        security.setdefault('recommendations', []).extend(legacy.get('recommendations', []))
                    return legacy
                stages.append(('Review vendor-only and legacy patch sources',
                               'Comparing local inventory to known provider families and official OEM routes. No unofficial package is downloaded or installed, and availability/compatibility remains unverified.', legacy_review))
            stages.append(('Capture final resource report', 'Saving final measurements and the outcome of each operation.', self.backend.audit))
            if configuration['post_process_review_enabled']:
                def process_review():
                    result = self.backend.process_harness_scan(cancel_event=cancel_event)
                    report['process_review'] = result
                    return {'review_only': True, 'result': result,
                            'failed_items': 0 if isinstance(result, dict) and result.get('complete') is True else 1,
                            'note': 'Potential background overhead is presented after completion. Pausing, startup changes and program removal require a selected action; memory use alone does not establish that a program is unnecessary.'}
                stages.append(('Prepare background-program recommendations',
                               'Inspecting every accessible process and its launch evidence; protecting Windows, security software and Bing Wallpaper. No process is stopped or program uninstalled.', process_review))
            def maintenance_insights():
                from .maintenance_insights import build_insights
                result = dict(build_insights(report, self.backend.data_dir), review_only=True)
                report['maintenance_insights'] = result
                return result
            stages.append(('Summarize resource and startup changes',
                           'Comparing the existing before/after measurements and verified supported startup registrations. Observations do not prove a speed gain, and no startup entry is changed.', maintenance_insights))
            if has_space_guard:
                stages.append(('Recheck drive space buffers', 'Rechecking free space after the selected maintenance; any remaining shortfall is reported without broader deletion.', space_guard))
            def final_verification():
                try:
                    result = self.backend.verify_optimization(report, cancel_event=cancel_event, timeout=30)
                    statuses = {'confirmed', 'needs_review', 'unverified', 'not_applicable'}
                    if (not isinstance(result, dict) or type(result.get('schema')) is not int or result['schema'] != 1
                            or result.get('read_only') is not True or result.get('status') not in statuses
                            or not isinstance(result.get('counts'), dict)
                            or any(type(result['counts'].get(key)) is not int or result['counts'][key] < 0 for key in statuses)
                            or type(result.get('failed_items')) is not int or result['failed_items'] < 0
                            or not isinstance(result.get('stages'), list) or type(result.get('coverage_complete')) is not bool
                            or result['status'] == 'confirmed' and (result['counts']['needs_review'] or result['counts']['unverified'])):
                        raise ValueError('The verifier returned an incomplete or inconsistent outcome report.')
                except Exception as error:
                    result = _unavailable_verification('The local final check was unavailable (' + type(error).__name__ + ').')
                    if cancelled():
                        result['cancelled'] = True
                report['final_verification'] = result
                return result
            stages.append((FINAL_VERIFICATION_STAGE,
                           'Checking the selected operations against their saved evidence and fresh local state. Review-only, unknown and failed outcomes stay explicit; no new changes are applied.', final_verification))
            parallel_specs = []
            parallel_launch_after = None
            if configuration['parallel_app_updates_enabled']:
                stages, parallel_specs, parallel_launch_after = parallel_installer_stages(stages, (WINDOWS_SOFTWARE, WINDOWS_DRIVERS, WINDOWS_DRIVER_REVIEW))
            total_stages = len(stages) + len(parallel_specs)
            completed = 0
            joined = False
            parallel_barrier = unresolved_apps

            def join_updates(next_stage=None):
                nonlocal joined, completed, parallel_barrier
                if parallel_job is not None and not joined:
                    background_stages = parallel_job.join(next_stage=next_stage)
                    joined = True
                    completed += len(background_stages)
                    for stage in background_stages:
                        parallel_barrier = parallel_barrier or installer_barrier(stage['result'])
                        report['cancelled'] = report['cancelled'] or stage['result'].get('cancelled') is True
                    atomic_json(log_path, report)

            checkpoint_open = False
            cleanup_barrier = None
            if parallel_specs:
                from .automated_software import run_software
                def background_software(worker, item_callback, prepared_callback, before_install):
                    return run_software(worker, configuration, cancel_event=getattr(worker, '_optimus_lane_cancel_event', global_cancel_event),
                        item_callback=item_callback, prepared_callback=prepared_callback, before_install=before_install)
                def background_drivers(worker, item_callback, prepared_callback, before_install):
                    from .automated_system import run_drivers
                    return run_drivers(worker, configuration, cancel_event=getattr(worker, '_optimus_lane_cancel_event', global_cancel_event),
                        item_callback=item_callback, prepared_callback=prepared_callback, before_install=before_install)
                operations = [(spec[0], background_drivers if spec[0] in DRIVER_TITLES else background_software)
                              for spec in parallel_specs]
                parallel_job = ParallelInstallerQueue(self.backend, run_id, operations, progress_callback,
                                                     cancel_event=global_cancel_event, prior_barrier=unresolved_apps)
                report['stages'].extend(parallel_job.start())
                atomic_json(log_path, report)

            for title, description, operation in ready_maintenance_stages(stages, parallel_job):
                if parallel_job is not None and checkpoint_open and not independent_stage(title):
                    join_updates(title)
                if parallel_job is not None and title in BEFORE_UPDATES and not checkpoint_open:
                    cleanup_barrier = cleanup_barrier or parallel_job.wait_prepared()
                if global_cancel_event is not None and global_cancel_event.is_set():
                    report['cancelled'] = True
                    break
                navigation_item = title not in (WINDOWS_SOFTWARE, WINDOWS_DRIVERS, WINDOWS_DRIVER_REVIEW, 'Check or install Windows security updates')
                mutation_item = title.startswith(('Apply ', 'Prepare approved ', 'Disable ', 'Recycle ', 'Clean ', 'Optimize ', 'Remove ', 'Uninstall ', 'Enable '))
                navigation_available = True
                if navigation_item:
                    navigation_available = maintenance_navigation.begin_item('stage:' + str(completed) + ':' + title, title,
                        kind='mutation' if mutation_item else 'scan')
                process_lines = ['Optimus workflow worker']
                process = getattr(self.backend.bridge, '_process', None)
                if process is not None and process.poll() is None:
                    process_lines.append(f'Windows PowerShell engine (PID {process.pid})')
                progress_callback({'percentage': int(completed * 100 / total_stages), 'stage': title,
                                   'stage_status': 'running', 'maintenance_status': 'scanning' if title == stages[0][0] else 'running',
                                   'description': description, 'processes': process_lines, 'log': f'Starting: {title}', 'completed': completed, 'total': total_stages})
                started = time.time()
                serial_job = None
                try:
                    if not navigation_available:
                        result = {'skipped': True, 'navigation_blocked': True, 'requiresReview': True, 'failed_items': 0,
                                  'note': 'The preceding maintenance item has an unresolved outcome; this stage was not started.'}
                    elif navigation_item and maintenance_navigation.local_requested():
                        result = {'skipped': True, 'cancelled': True, 'navigation_cancelled': True, 'navigation_not_started': True,
                                  'failed_items': 0, 'installer_started': False, 'outcomeKnown': True,
                                  'note': 'You skipped this exact maintenance stage before it started.'}
                    elif cleanup_barrier and title in BEFORE_UPDATES:
                        result = {'skipped': True, 'review_only': True, 'requiresReview': True,
                                  'failed_items': 0, 'items': [], 'stop_reason': cleanup_barrier,
                                  'note': 'No cleanup ran while update discovery still owned temporary files.'}
                    elif parallel_barrier and title in (WINDOWS_SOFTWARE, *DRIVER_TITLES):
                        # Serial mode still searches for updates when an old
                        # installer blocks mutation. The phased operation must
                        # pass the same closed gate before backup/revalidation
                        # or installation; no background thread is created.
                        from .automated_software import run_software
                        from .automated_system import run_drivers
                        updater = run_drivers if title in DRIVER_TITLES else run_software
                        def discover_only(worker, item_callback, prepared_callback, before_install):
                            return updater(worker, configuration, cancel_event=cancel_event,
                                item_callback=item_callback, prepared_callback=prepared_callback,
                                before_install=before_install)
                        guarded = ParallelInstallerQueue(self.backend, run_id, [(title, discover_only)],
                            progress_callback, cancel_event=cancel_event, prior_barrier=parallel_barrier)
                        guarded.start(inline=True)
                        guarded.join()
                        serial_job = guarded.jobs[0]
                        result = serial_job.result
                    elif parallel_barrier and title in BLOCKED_STAGES:
                        result = {'skipped': True, 'review_only': True, 'requiresReview': True,
                                  'failed_items': 0, 'items': [], 'stop_reason': parallel_barrier,
                                  'blocked_by_prior_installer': True,
                                  'note': 'Deferred after update installation: ' + parallel_barrier}
                        if title == 'Check or install Windows security updates':
                            report['windows_security_updates'] = result
                    elif title in (WINDOWS_SOFTWARE, WINDOWS_DRIVERS, 'Check or install Windows security updates'):
                        # Inline execution uses the same durable lifecycle as
                        # the background lane, including a before-install item
                        # receipt. A crash in a serial installer must not erase
                        # its unresolved state on the next launch.
                        observer_mux = (ProgressMux(lambda _: None) if title == 'Check or install Windows security updates'
                                        else progress_callback)
                        serial_job = ParallelAppUpdates(self.backend, run_id, title,
                            lambda worker, callback, operation=operation: operation(), observer_mux, cancel_event=cancel_event)
                        serial_job.start(inline=True)
                        if serial_job.result is None:
                            serial_job._run()
                        result = serial_job.result
                    else:
                        if navigation_item:
                            maintenance_navigation.phase('applying' if mutation_item else 'scanning', mutation_started=mutation_item)
                        result = operation()
                    local_navigation_cancel = navigation_item and maintenance_navigation.local_requested()
                    if local_navigation_cancel and isinstance(result, dict) and result.get('cancelled') is True:
                        result = dict(result, navigation_cancelled=True,
                                      navigation_cancelled_items=max(1, result.get('navigation_cancelled_items', 0)))
                    interrupted = (global_cancel_event is not None and global_cancel_event.is_set()
                                   or isinstance(result, dict) and result.get('cancelled') is True)
                    navigation_cancelled = isinstance(result, dict) and (result.get('navigation_cancelled') is True or bool(result.get('navigation_cancelled_items')))
                    status = 'cancelled' if interrupted or navigation_cancelled else 'skipped' if isinstance(result, dict) and result.get('navigation_blocked') else 'failed' if isinstance(result, dict) and result.get('failed_items', 0) else 'completed'
                    stage = {'title': title, 'status': status, 'started_at': started, 'finished_at': time.time(), 'result': result}
                    if serial_job is not None:
                        stage['worker_log_path'] = str(serial_job.path)
                    if status == 'failed':
                        stage['error'] = 'One or more selected operations or scan checks failed; review the saved item results.'
                    if interrupted and not local_navigation_cancel:
                        report['cancelled'] = True
                except Exception as error:
                    stage = {'title': title, 'status': 'failed', 'started_at': started, 'finished_at': time.time(), 'error': str(error)}
                    for key in ('driver_scan', 'app_scan'):
                        evidence = getattr(error, key, None)
                        if isinstance(evidence, dict):
                            from .parallel_updates import _bounded_scan_evidence
                            stage['result'] = {'failed_items': 1, 'items': [],
                                'installer_started': False, 'outcomeKnown': True,
                                key: _bounded_scan_evidence(evidence)}
                if title in (WINDOWS_SOFTWARE, WINDOWS_DRIVERS, 'Check or install Windows security updates'):
                    parallel_barrier = parallel_barrier or installer_barrier(stage.get('result'))
                    if serial_job is None and not (isinstance(stage.get('result'), dict) and stage['result'].get('blocked_by_prior_installer')):
                        record_installer_outcome(self.backend, run_id, title, stage.get('result'))
                if title == stages[0][0]:
                    report['stages'].insert(0, stage)
                else:
                    report['stages'].append(stage)
                completed += 1
                report['resource_totals'] = progress_callback.resource_snapshot()
                report['worker_resource_totals'] = progress_callback.worker_resource_snapshots()
                atomic_json(log_path, report)
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
                        prior_barrier = 'The initial before-inventory failed; update installation was deferred.'
                    if report['cancelled']:
                        parallel_job.cancel()
                    parallel_job.release_maintenance(prior_barrier)
                percentage = int(completed * 100 / total_stages)
                if report['cancelled']:
                    percentage = min(99, percentage)
                description = stage.get('error', f"{title}: {stage['status']}")
                insight_summary = ''
                if title == 'Summarize resource and startup changes' and isinstance(stage.get('result'), dict):
                    insight_summary = str(stage['result'].get('summary', ''))[:4000]
                    if insight_summary:
                        description = insight_summary
                if title == FINAL_VERIFICATION_STAGE and isinstance(stage.get('result'), dict):
                    description = str(stage['result'].get('summary') or 'Final verification: ' + str(stage['result'].get('status', 'unverified')).replace('_', ' ') + '. Review the outcome details before recommendations.')
                if title in ('Check drive space buffers', 'Recheck drive space buffers') and isinstance(stage.get('result'), dict):
                    description = str(stage['result'].get('summary') or description)
                progress_callback({'percentage': percentage, 'stage': title, 'description': description,
                                   'stage_status': stage['status'],
                                   'processes': process_lines, 'log': f"{stage['status'].upper()}: {title}" + (': ' + stage['error'] if stage.get('error') else ': ' + insight_summary if insight_summary else ''), 'completed': completed, 'total': total_stages})
            join_updates()
            report['finished_at'] = time.time()
            report['failed_stages'] = sum(item['status'] == 'failed' for item in report['stages'])
            report['cancelled_stages'] = sum(item['status'] == 'cancelled' for item in report['stages'])
            report['log_path'] = str(log_path)
            report['status'] = 'cancelled' if report['cancelled'] else 'completed_with_errors' if report['failed_stages'] or report['cancelled_stages'] else 'completed'
            report['resource_totals'] = progress_callback.resource_snapshot()
            report['worker_resource_totals'] = progress_callback.worker_resource_snapshots()
            atomic_json(log_path, report)
            if attempt_started:
                record_attempt(self.backend.data_dir, configuration, status='cancelled' if report['cancelled'] else 'failed' if report['failed_stages'] else 'completed', run_id=run_id,
                               error=f"{report['failed_stages']} stages failed" if report['failed_stages'] else None)
            return report
        except Exception as error:
            if parallel_job is not None:
                parallel_job.cancel()
                parallel_job.join()
            if report is None:
                report = {'settings': configuration, 'stages': [], 'cancelled': False}
            report.update({'status': 'failed', 'error': str(error), 'finished_at': time.time()})
            if hasattr(progress_callback, 'resource_snapshot'):
                report['resource_totals'] = progress_callback.resource_snapshot()
                report['worker_resource_totals'] = progress_callback.worker_resource_snapshots()
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
                if parallel_job is not None:
                    if not parallel_job.joined:
                        parallel_job.cancel()
                    parallel_job.join()
                if lease is not None:
                    lease.release()
            finally:
                self.navigation.end((report or {}).get('status', 'failed'))
                self.active = False
                self.lock.release()


