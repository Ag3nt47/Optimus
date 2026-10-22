"""One reviewed post-run attempt using existing scoped maintenance services.

The completed report is evidence for a preview, not write authority. Applying
requires the current Backend's completed run, current saved approvals, fresh
service checks, a run lease and a durable linked journal. Optional process
actions and application removal never enter this batch.
"""
from __future__ import annotations

import copy
import math
from pathlib import Path
import re
import time
import uuid

from .automation import RunLease
from .filesystem import _ordinary_data_path
from .workflow import FINAL_VERIFICATION_STAGE, Preferences, _unavailable_verification, atomic_json


SECURITY_FIXES = {
    'enable_defender_realtime': 'Enable Microsoft Defender real-time protection',
    'enable_firewall': 'Enable disabled Windows Firewall profiles',
    'update_defender_signatures': 'Update Microsoft Defender security intelligence',
    'remediate_defender_threats': 'Remediate Defender-detected active threats',
}
MANUAL_SECURITY = {
    'review_security': ('Review protection in Windows Security', 'Review the active security provider and Windows security policy.'),
    'review_windows_update': ('Review Windows Update', 'Windows Update settings or saved update scope may require review.'),
    'restart_required': ('Choose when to restart Windows', 'A restart must be completed through Windows controls; Optimus does not reboot automatically.'),
    'review_legacy_patches': ('Review vendor-only and legacy patch coverage', 'OEM firmware, vendor-only and community patches require product-specific compatibility and rollback review.'),
    'native-system-updates': ('Review native OS and security updates', 'Review OS, firmware and security settings through the native OS or vendor updater.'),
}
CHANNELS = {
    'software_updates': ('Recheck remaining approved application updates', 'Review or update configured software'),
    'windows_security_updates': ('Recheck remaining approved Windows Security/Critical updates', 'Check or install Windows security updates'),
    'driver_updates': ('Recheck remaining approved Windows Update drivers', 'Install approved driver updates'),
}
_IDENTITY = re.compile(r'[0-9a-f]{32}\Z')
_STATES = ('confirmed', 'needs_review', 'unverified', 'not_applicable')
_ACTION_STATES = ('completed', 'skipped', 'blocked', 'failed', 'uncertain', 'cancelled', 'deferred')


def _bounded(value):
    """Reject non-JSON, cyclic, oversized or excessively nested report inputs."""
    pending, nodes, text_size = [(value, 0)], 0, 0
    while pending:
        item, depth = pending.pop()
        nodes += 1
        if nodes > 100000 or depth > 24:
            raise ValueError('The report exceeds the post-run review bounds.')
        if type(item) is dict:
            if len(item) > 10000 or any(type(key) is not str or len(key) > 512 for key in item):
                raise ValueError('The report contains invalid object fields.')
            pending.extend((part, depth + 1) for part in item.values())
            text_size += sum(len(key) for key in item)
        elif type(item) is list:
            if len(item) > 10000:
                raise ValueError('The report contains too many items.')
            pending.extend((part, depth + 1) for part in item)
        elif type(item) is str:
            text_size += len(item)
        elif type(item) is float:
            if not math.isfinite(item):
                raise ValueError('The report contains an invalid number.')
        elif item is not None and type(item) not in (int, bool):
            raise ValueError('The report must contain ordinary JSON values.')
        if text_size > 8 * 1024 * 1024:
            raise ValueError('The report exceeds the post-run review size limit.')
    return value


def _validate_report(report):
    _bounded(report)
    if (not isinstance(report, dict) or not isinstance(report.get('id'), str)
            or not _IDENTITY.fullmatch(report['id'])
            or report.get('status') not in ('completed', 'completed_with_errors')
            or report.get('cancelled') or report.get('canceled')
            or not isinstance(report.get('stages'), list) or len(report['stages']) > 256):
        raise ValueError('Choose a completed, non-cancelled optimization result.')
    return report


def _settings(backend, value):
    _bounded(value)
    validator = getattr(getattr(backend, 'preference_store', None), 'validate', None)
    return (validator or Preferences.validate)(value)


def _native(backend):
    return getattr(backend, 'system', None) in ('linux', 'darwin')


def _recommendation_ids(value):
    rows = value.get('recommendations', []) if isinstance(value, dict) else []
    if not isinstance(rows, list) or len(rows) > 1000:
        return set()
    return {row['id'] for row in rows if isinstance(row, dict) and isinstance(row.get('id'), str)}


def _channel_results(report, channel):
    titles = {
        'software_updates': {'Review or update configured software', 'Review or install approved native package updates'},
        'windows_security_updates': {'Check or install Windows security updates'},
        'driver_updates': {'Install approved driver updates', 'Review available driver updates'},
    }[channel]
    found = []
    for stage in report['stages']:
        if isinstance(stage, dict) and stage.get('title') in titles:
            value = stage.get('result')
            found.append((value if isinstance(value, dict) else {}, stage.get('status') == 'failed'))
    top = report.get(channel)
    if isinstance(top, dict) and not any(top is item[0] or top == item[0] for item in found):
        found.append((top, False))
    return found


def _remaining(value, failed):
    if failed or value.get('failed_items', 0) or value.get('deferred_ids') or value.get('deferred'):
        return True
    if value.get('requiresReview') is True or value.get('requires_review_items', 0):
        return True
    scan = value.get('scan', value)
    if not isinstance(scan, dict):
        return False
    offers = scan.get('updates', scan.get('packages', []))
    if not isinstance(offers, list):
        return False
    completed = set()
    for item in value.get('items', []) if isinstance(value.get('items'), list) else []:
        if isinstance(item, dict) and item.get('status') in ('completed', 'applied'):
            binding = item.get('binding') if isinstance(item.get('binding'), dict) else item
            identity = binding.get('id')
            if isinstance(identity, str):
                completed.add(identity.casefold())
    return any(isinstance(row, dict) and isinstance(row.get('id', row.get('package_id')), str)
               and row.get('id', row.get('package_id')).casefold() not in completed for row in offers)


def _authorized(settings, channel):
    if channel == 'software_updates':
        return (settings.get('software_updates_enabled') is True
                and settings.get('software_update_agreements_accepted') is True
                and (settings.get('software_update_policy') == 'selected' and bool(settings.get('software_update_ids'))
                     or settings.get('software_update_policy') == 'all_supported' and settings.get('software_update_all_approved') is True))
    if channel == 'windows_security_updates':
        return all(settings.get(key) is True for key in ('windows_security_updates_enabled',
                   'windows_security_updates_install_approved', 'windows_security_update_agreements_accepted'))
    return (settings.get('driver_updates_enabled') is True and settings.get('driver_changes_approved') is True
            and settings.get('driver_update_agreements_accepted') is True
            and (settings.get('driver_policy') == 'install_approved' and bool(settings.get('driver_approved_updates'))
                 or settings.get('driver_policy') == 'install_matching' and bool(settings.get('driver_approved_devices'))))


def _preview(backend, report, settings):
    actions, manual = [], []
    identifiers = _recommendation_ids(report.get('security_review'))
    for identity, title in SECURITY_FIXES.items():
        if identity in identifiers and not _native(backend):
            actions.append({'id': identity, 'title': title, 'kind': 'security'})
    for identity, (title, reason) in MANUAL_SECURITY.items():
        if identity in identifiers:
            manual.append({'id': identity, 'title': title, 'reason': reason})
    for channel, (title, _) in CHANNELS.items():
        if not any(_remaining(value, failed) for value, failed in _channel_results(report, channel)):
            continue
        if _native(backend) and channel != 'software_updates':
            manual.append({'id': channel, 'title': title, 'reason': 'This Windows update channel is unavailable on this platform; use the native OS or vendor updater.'})
        elif _authorized(settings, channel):
            actions.append({'id': channel, 'title': title, 'kind': channel})
        else:
            manual.append({'id': channel, 'title': title, 'reason': 'Automatic installation needs an enabled channel, saved target scope and accepted update agreements. Review setup before retrying.'})
    process_review = report.get('process_review')
    if isinstance(process_review, dict) and (
            isinstance(process_review.get('actions'), list) and bool(process_review['actions'])
            or process_review.get('complete') is False or bool(process_review.get('errors'))):
        manual.append({'id': 'process_review', 'title': 'Optional background-program review',
                       'reason': 'Process pauses and startup changes require individual selection in the process harness. Memory use does not authorize application removal.'})
    return {'actions': actions, 'manual_items': manual,
            'summary': f'{len(actions)} available automatic action(s); {len(manual)} item(s) need separate review. Update actions recheck current offers within saved approvals.'}


def available_fixes(backend, report, settings):
    """Pure preview: validate supplied snapshots without scanning or writing."""
    return _preview(backend, _validate_report(report), _settings(backend, settings))


def _narrow_settings(backend, approved, current):
    """A later preference change can narrow this click's authority, never widen it."""
    effective = copy.deepcopy(current)
    for key in ('software_updates_enabled', 'software_update_agreements_accepted', 'software_update_all_approved',
                'windows_security_updates_enabled', 'windows_security_updates_install_approved',
                'windows_security_update_agreements_accepted', 'driver_updates_enabled',
                'driver_changes_approved', 'driver_update_agreements_accepted', 'update_compatibility_mode_enabled'):
        effective[key] = approved.get(key) is True and current.get(key) is True
    fold = (lambda value: value) if _native(backend) else str.casefold
    old_policy, new_policy = approved.get('software_update_policy'), current.get('software_update_policy')
    if 'review' in (old_policy, new_policy):
        effective['software_update_policy'] = 'review'
    elif old_policy == 'all_supported' and new_policy == 'all_supported':
        effective['software_update_policy'] = 'all_supported'
    else:
        choices = current['software_update_ids'] if old_policy == 'all_supported' else approved['software_update_ids']
        current_ids = {fold(item) for item in current['software_update_ids']}
        effective['software_update_ids'] = [item for item in choices if new_policy == 'all_supported' or fold(item) in current_ids]
        effective['software_update_policy'] = 'selected' if effective['software_update_ids'] else 'review'
    excluded = {fold(item): item for item in approved.get('software_update_exclude_ids', [])}
    excluded.update({fold(item): item for item in current.get('software_update_exclude_ids', [])})
    if len(excluded) <= 100:
        effective['software_update_exclude_ids'] = list(excluded.values())
    else:
        effective['software_update_policy'] = 'review'
    policy = current.get('driver_policy')
    if policy != approved.get('driver_policy') or policy == 'review':
        effective['driver_policy'] = 'review'
    else:
        key = 'driver_approved_updates' if policy == 'install_approved' else 'driver_approved_devices'
        effective[key] = [item for item in current[key] if item in approved[key]]
        if not effective[key]:
            effective['driver_policy'] = 'review'
    return _settings(backend, effective)


def _stop_reason(value, *, prior=False):
    """Normalize updater-specific stop flags across the entire batch."""
    pending = [value]
    while pending:
        item = pending.pop()
        if isinstance(item, dict):
            if item.get('restartNeeded') is True or item.get('reboot_required') is True:
                return 'A restart is required. Remaining installers were deferred.'
            if (item.get('may_continue') is True or item.get('running') is True or item.get('outcomeKnown') is False
                    or item.get('status') == 'uncertain' or item.get('uncertain') is True):
                return 'An operation may still be running or its outcome is uncertain. Verify its state before another installer.'
            if not prior and item.get('requiresReview') is True:
                return 'An update safety check requires review before another installer.'
            if item.get('cancelled') is True:
                return 'Cancellation requested; remaining actions were deferred.'
            if not prior and (item.get('status') == 'failed' or item.get('failed_items', 0)):
                return 'An operation failed; remaining installers were deferred for review.'
            pending.extend(item.values())
        elif isinstance(item, list):
            pending.extend(item)
    if isinstance(value, dict) and value.get('stop_reason'):
        return 'The updater deferred remaining work; review its saved outcome before another installer.'
    return ''


def _prior_installer_barrier(backend, report):
    security = report.get('security_review')
    if isinstance(security, dict) and security.get('reboot_required') is True:
        return 'The completed run reported a pending restart. Review Windows Update before another installer.'
    for channel in CHANNELS:
        for value, failed in _channel_results(report, channel):
            if failed and not value:
                return 'A previous updater stage failed without a verified operation result. Review installer state before another installer.'
            if _native(backend) and channel == 'software_updates' and (
                    failed or value.get('failed_items', 0)
                    or any(isinstance(item, dict) and item.get('status') == 'failed'
                           for item in (value['items'] if isinstance(value.get('items'), list) else []))):
                return 'The previous native package operation failed and its installer termination is unverified. Review the native package manager before retrying.'
            reason = _stop_reason(value, prior=True)
            if reason:
                return 'The completed run has an unresolved installer or restart state. Review its saved outcome before another installer.'
    return ''


def _action_status(value):
    if not isinstance(value, dict):
        return 'uncertain'
    if value.get('cancelled') is True or value.get('status') == 'cancelled':
        return 'cancelled'
    pending, failed = [value], False
    while pending:
        item = pending.pop()
        if isinstance(item, dict):
            if (item.get('may_continue') is True or item.get('running') is True or item.get('outcomeKnown') is False
                    or item.get('uncertain') is True or item.get('status') == 'uncertain'):
                return 'uncertain'
            failed = failed or item.get('status') == 'failed' or bool(item.get('failed_items', 0))
            pending.extend(item.values())
        elif isinstance(item, list):
            pending.extend(item)
    if value.get('status') in ('blocked', 'failed', 'completed', 'manual_action_required', 'uncertain'):
        return value['status']
    if failed:
        return 'failed'
    items = value.get('items', [])
    if isinstance(items, list) and any(isinstance(row, dict) and row.get('status') in ('completed', 'applied') for row in items):
        return 'completed'
    if value.get('requiresReview') is True or value.get('skipped') is True and value.get('reason'):
        return 'blocked'
    return 'skipped'


def _valid_verification(value):
    return (isinstance(value, dict) and type(value.get('schema')) is int and value.get('schema') == 1 and value.get('read_only') is True
            and value.get('status') in _STATES and isinstance(value.get('stages'), list)
            and isinstance(value.get('counts'), dict)
            and all(type(value['counts'].get(key)) is int and value['counts'][key] >= 0 for key in _STATES)
            and type(value.get('failed_items')) is int and value['failed_items'] >= 0
            and type(value.get('coverage_complete')) is bool
            and not (value['status'] == 'confirmed' and (value['counts']['needs_review'] or value['counts']['unverified'])))


def _verification(backend, report, cancel_event):
    # Existing verifiers understand these canonical updater/audit stage titles.
    # Security fixes use their service's verified result plus the final fresh
    # local security readback below, rather than an unsupported generic check.
    evidence = dict(report, stages=[row for row in report['stages'] if row.get('kind') != 'security'])
    before = report.get('software_before_snapshot')
    if isinstance(before, dict):
        evidence['stages'] = [{'title': 'Inspect current resource usage', 'status': 'completed',
                               'result': before}, *evidence['stages']]
    try:
        value = backend.verify_optimization(evidence, cancel_event=cancel_event, timeout=30)
        _bounded(value)
        if not _valid_verification(value):
            raise ValueError('Invalid verifier response.')
        value = copy.deepcopy(value)
    except Exception as error:
        value = _unavailable_verification('Post-run verification could not be completed (' + type(error).__name__ + ').')
    fresh = report.get('security_review', {})
    remaining = _recommendation_ids(fresh)
    checks = []
    for stage in report['stages']:
        if stage.get('kind') != 'security':
            continue
        action_status = stage.get('action_status')
        state = ('confirmed' if action_status == 'completed' and fresh.get('status') == 'completed' and stage['action_id'] not in remaining
                 else 'not_applicable' if action_status == 'skipped'
                 else 'unverified' if action_status in ('uncertain', 'cancelled', 'deferred') or action_status == 'completed' and fresh.get('status') != 'completed'
                 else 'needs_review')
        checks.append({'kind': 'security_readback', 'target': stage['title'], 'status': state,
                       'reason': 'The supported action result and fresh security state are recorded; confirmation applies only to this control.'})
    for item in report.get('manual_items', []):
        checks.append({'kind': 'manual_review', 'target': item['title'],
                       'status': 'not_applicable' if item['id'] == 'process_review' else 'needs_review', 'reason': item['reason']})
    if fresh.get('status') == 'partial':
        checks.append({'kind': 'security_readback', 'target': 'Final security state', 'status': 'unverified',
                       'reason': 'Fresh security evidence was incomplete; unknown protection state is not a clean result.'})
        value['coverage_complete'] = False
        value['failed_items'] += 1
    if report.get('stop_reason'):
        checks.append({'kind': 'deferred_work', 'target': 'Remaining automatic actions', 'status': 'needs_review', 'reason': report['stop_reason']})
    if checks:
        value['stages'].append({'title': 'Post-run security and remaining review', 'checks': checks})
        for check in checks:
            value['counts'][check['status']] += 1
    counts = value['counts']
    value['status'] = ('needs_review' if counts['needs_review'] else 'unverified' if counts['unverified']
                       else 'confirmed' if counts['confirmed'] else 'not_applicable')
    value['summary'] = (f"Post-run verification: {counts['confirmed']} confirmed, {counts['needs_review']} need review, "
                        f"{counts['unverified']} unverified; optional/manual work remains explicit.")
    return value


class PostRunFixes:
    def __init__(self, backend):
        self.backend = backend
        self.attempted = set()

    def apply(self, report, settings, *, progress_callback=None, cancel_event=None):
        requested = _validate_report(report)
        approved = _settings(self.backend, settings)
        reviewed_ids = {row['id'] for row in _preview(self.backend, requested, approved)['actions']}
        workflow = self.backend.workflow
        if not workflow.lock.acquire(blocking=False):
            raise RuntimeError('An optimization or follow-up run is already active.')
        lease = None
        try:
            active = getattr(workflow, 'last_report', None)
            if not isinstance(active, dict) or active.get('id') != requested['id']:
                raise ValueError('This completed result does not belong to the current Optimus session.')
            source = copy.deepcopy(_validate_report(active))
            # The completed run's settings cap the caller's preview snapshot.
            # Neither a forged export nor newly broadened preferences can add
            # update authority to the batch the user already reviewed.
            approved = _narrow_settings(self.backend, approved, _settings(self.backend, source.get('settings')))
            if source['id'] in self.attempted:
                raise ValueError('The automatic follow-up was already attempted for this result. Review its saved outcomes before running a new optimization.')
            workflow.active = True
            lease = RunLease(self.backend.data_dir).acquire()
            directory = Path(self.backend.data_dir) / 'post-run-fixes'
            _ordinary_data_path(directory)
            identifier = uuid.uuid4().hex
            path = directory / (identifier + '.json')
            result = {'schema': 1, 'id': identifier, 'parent_run_id': source['id'], 'started_at': time.time(),
                      'settings': approved, 'status': 'running', 'stages': [], 'cancelled': False,
                      'manual_items': [], 'counts': {key: 0 for key in _ACTION_STATES},
                      'log_path': str(path), 'note': 'The Auto continue click or saved automatic-follow-up choice authorizes these supported security fixes for one attempt. Update installation remains within saved scopes and agreements. Processes, startup entries, application removal and cleanup are not included.'}
            original_plan = _preview(self.backend, source, approved)
            original_plan['actions'] = [row for row in original_plan['actions'] if row['id'] in reviewed_ids]
            result['reviewed_plan'] = original_plan
            result['manual_items'] = copy.deepcopy(original_plan['manual_items'])
            if isinstance(source.get('process_review'), dict):
                result['process_review'] = copy.deepcopy(source['process_review'])
            def journal():
                _ordinary_data_path(directory)
                _ordinary_data_path(path, file=True)
                _bounded(result)
                atomic_json(path, result)
            journal()
            self.attempted.add(source['id'])
            # Bound retained one-shot identities to the current and recent runs.
            if len(self.attempted) > 100:
                self.attempted = {source['id']}
            stopped = lambda: cancel_event is not None and cancel_event.is_set()
            progress = progress_callback or (lambda event: None)
            actions = original_plan['actions']
            halted = False
            installer_barrier = _prior_installer_barrier(self.backend, source)
            installer_state_checked = False
            for index, action in enumerate(actions):
                stage = {'title': action['title'], 'action_id': action['id'], 'kind': action['kind'],
                         'started_at': time.time(), 'status': 'prepared', 'action_status': 'prepared'}
                result['stages'].append(stage)
                if stopped() or halted:
                    stage.update(status='skipped', action_status='deferred', result={'skipped': True, 'reason': result.get('stop_reason', 'Cancellation requested.')})
                    result['counts']['deferred'] += 1
                    result['cancelled'] = result['cancelled'] or stopped()
                    journal()
                    continue
                progress({'stage': action['title'], 'description': action['title'], 'percentage': int(index * 85 / max(1, len(actions))),
                          'processes': [action['title']], 'log': action['title']})
                journal()  # Durable prepared record before any service may mutate.
                try:
                    current = _settings(self.backend, self.backend.preferences())
                    effective = _narrow_settings(self.backend, approved, current)
                    permitted = {row['id'] for row in _preview(self.backend, source, effective)['actions']}
                    if action['id'] not in permitted:
                        outcome = {'status': 'blocked', 'reason': 'Saved update approval was revoked or narrowed after this result; no installer was started.'}
                    elif stopped():
                        outcome = {'status': 'cancelled', 'cancelled': True}
                    else:
                        if action['id'] in CHANNELS and not installer_state_checked:
                            installer_state_checked = True
                            if not _native(self.backend):
                                readiness = self.backend.security_status()
                                _bounded(readiness)
                                result['installer_readiness'] = readiness
                                if not isinstance(readiness, dict) or readiness.get('reboot_required') is not False:
                                    installer_barrier = 'A fresh Windows restart state is pending or unverifiable. Remaining installers were deferred.'
                        if action['id'] in CHANNELS and installer_barrier:
                            stage.update(status='skipped', action_status='deferred',
                                         result={'skipped': True, 'reason': installer_barrier})
                            result['counts']['deferred'] += 1
                            result['stop_reason'] = installer_barrier
                            if not any(item['id'] == 'installer_state' for item in result['manual_items']):
                                result['manual_items'].append({'id': 'installer_state', 'title': 'Review installer and restart state',
                                                               'reason': installer_barrier})
                            stage['finished_at'] = time.time()
                            journal()
                            continue
                        result['effective_settings'] = effective
                        stage['authorization'] = {key: copy.deepcopy(value) for key, value in effective.items()
                                                  if key.startswith(('software_update', 'windows_security_update', 'driver_')) or key == 'setup_version'}
                        journal()
                        if action['id'] == 'software_updates' and not _native(self.backend):
                            progress({'stage': action['title'], 'description': 'Reading local application identities before updates.',
                                      'percentage': int(index * 85 / max(1, len(actions))),
                                      'processes': ['Optimus follow-up worker: read-only application inventory']})
                            try:
                                before = self.backend.audit()
                                _bounded(before)
                                if not isinstance(before, dict):
                                    raise ValueError('Invalid before-update resource report.')
                                result['software_before_snapshot'] = copy.deepcopy(before)
                            except Exception as error:
                                result['software_before_snapshot'] = {'InstalledApps': [],
                                    'InventoryErrors': ['Before-update application identities are unavailable (' + type(error).__name__ + ').']}
                            result['software_before_at'] = time.time()
                            journal()
                        def item_progress(event):
                            _bounded(event)
                            stage['current_item'] = copy.deepcopy(event)
                            journal()  # Existing helpers call this before each installer.
                            update = dict(event, stage=action['title'], percentage=int(index * 85 / max(1, len(actions))))
                            update.setdefault('processes', [event.get('description') or action['title']])
                            progress(update)
                        outcome = self._execute(action, effective, cancel_event, item_progress)
                    _bounded(outcome)
                    status = _action_status(outcome)
                    stage.update(result=outcome, action_status=status,
                                 status='failed' if status in ('failed', 'blocked', 'uncertain') else 'cancelled' if status == 'cancelled' else 'completed')
                    result['counts'][status if status in result['counts'] else 'blocked'] += 1
                    reason = _stop_reason(outcome)
                    if reason:
                        halted = True
                        result['stop_reason'] = reason
                except Exception as error:
                    stage.update(status='failed', action_status='uncertain',
                                 result={'status': 'uncertain', 'reason': 'The operation outcome could not be verified (' + type(error).__name__ + '). Verify its state before retrying.'})
                    result['counts']['uncertain'] += 1
                    result['stop_reason'] = 'An operation raised an error; remaining installers were deferred until its state is reviewed.'
                    halted = True
                stage['finished_at'] = time.time()
                result['cancelled'] = result['cancelled'] or stopped() or stage['action_status'] == 'cancelled'
                journal()
            self._final_readbacks(result, cancel_event, progress, journal)
            result['finished_at'] = time.time()
            result['failed_stages'] = sum(stage['status'] == 'failed' for stage in result['stages'])
            result['status'] = ('cancelled' if result['cancelled'] else 'completed_with_errors' if result['failed_stages'] else 'completed')
            counts = result['counts']
            result['summary'] = (f"Automatic follow-up actions/checks: {counts['completed']} completed, {counts['skipped']} skipped, "
                                 f"{counts['blocked']} blocked, {counts['failed']} failed, {counts['uncertain']} uncertain, "
                                 f"{counts['deferred']} deferred; {len(result['manual_items'])} separate review item(s).")
            journal()
            progress({'stage': 'Automatic follow-up returned results', 'description': result['summary'],
                      'percentage': 99 if result['cancelled'] else 100, 'processes': [], 'log': 'Saved: ' + str(path)})
            return result
        finally:
            try:
                if lease is not None:
                    lease.release()
            finally:
                workflow.active = False
                workflow.lock.release()

    def _execute(self, action, settings, cancel_event, progress):
        identity = action['id']
        if identity in SECURITY_FIXES:
            return self.backend.apply_security_recommendation(identity, cancel_event=cancel_event)
        if identity == 'software_updates':
            if _native(self.backend):
                from .native_workflow import run_native_software
                return run_native_software(self.backend, settings, cancel_event=cancel_event, item_callback=progress)
            from .automated_software import run_software
            return run_software(self.backend, settings, cancel_event=cancel_event, item_callback=progress)
        if identity == 'windows_security_updates':
            return self.backend.windows_security_updates(settings, cancel_event=cancel_event, item_callback=progress)
        if identity == 'driver_updates':
            from .automated_system import run_drivers
            return run_drivers(self.backend, settings, cancel_event=cancel_event, item_callback=progress)
        raise ValueError('Unsupported automatic follow-up action.')

    def _final_readbacks(self, result, cancel_event, progress, journal):
        progress({'stage': 'Check final security and outcomes', 'description': 'Reading current protection and verifying recorded follow-up outcomes.',
                  'percentage': 90, 'processes': ['Optimus follow-up worker: read-only verification']})
        try:
            security = self.backend.security_status()
            _bounded(security)
            if not isinstance(security, dict):
                raise ValueError('Invalid final security state.')
            result['security_review'] = copy.deepcopy(security)
        except Exception as error:
            result['security_review'] = {'status': 'partial', 'failed_items': 1, 'recommendations': [],
                                         'errors': ['Fresh security state is unavailable (' + type(error).__name__ + ').']}
        # These manual routes are supplied by workflow coverage, not the local
        # security-status reader. Keep them available to the existing Continue
        # dialog without treating an old control observation as fresh evidence.
        retained = {item['id'] for item in result['manual_items']} & {'review_windows_update', 'review_legacy_patches'}
        present = _recommendation_ids(result['security_review'])
        for identity in sorted(retained - present):
            title, reason = MANUAL_SECURITY[identity]
            result['security_review'].setdefault('recommendations', []).append(
                {'id': identity, 'title': title, 'reason': reason, 'action': identity,
                 'requires_admin': False, 'review_only': True, 'source': 'completed_report'})
        existing = {item['id'] for item in result['manual_items']}
        for identity in _recommendation_ids(result['security_review']):
            if identity in MANUAL_SECURITY and identity not in existing:
                title, reason = MANUAL_SECURITY[identity]
                result['manual_items'].append({'id': identity, 'title': title, 'reason': reason})
        for action in result['reviewed_plan']['actions']:
            for stage in result['stages']:
                if stage.get('action_id') == action['id']:
                    stage['title'] = (('Review or install approved native package updates' if _native(self.backend) else CHANNELS[action['id']][1])
                                      if action['id'] == 'software_updates' else CHANNELS[action['id']][1] if action['id'] in CHANNELS else action['title'])
        try:
            audit = self.backend.audit()
            _bounded(audit)
            if not isinstance(audit, dict):
                raise ValueError('Invalid final resource report.')
            result['stages'].append({'title': 'Capture final resource report', 'status': 'completed', 'result': audit})
        except Exception as error:
            result['stages'].append({'title': 'Capture final resource report', 'status': 'failed',
                                     'result': {'error': 'Fresh resource report is unavailable (' + type(error).__name__ + ').'}})
        result['cancelled'] = result['cancelled'] or cancel_event is not None and cancel_event.is_set()
        result['final_verification'] = _verification(self.backend, result, cancel_event)
        result['stages'].append({'title': FINAL_VERIFICATION_STAGE,
                                 'status': 'failed' if result['final_verification']['failed_items'] else 'completed',
                                 'result': result['final_verification']})
        journal()


def apply_available_fixes(backend, report, settings, *, progress_callback=None, cancel_event=None):
    service = getattr(backend, '_post_run_fixes_service', None)
    if service is None:
        service = backend._post_run_fixes_service = PostRunFixes(backend)
    return service.apply(report, settings, progress_callback=progress_callback, cancel_event=cancel_event)
