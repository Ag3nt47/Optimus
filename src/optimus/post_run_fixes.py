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
}
MANUAL_SECURITY = {
    'remediate_defender_threats': ('Review Defender detections and removal', 'Review all currently active Defender threats and confirm their exact displayed scope before asking Defender to remediate. Saved automatic continuation does not approve threat removal. Quarantined or historical detections can be reviewed in Windows Security.'),
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
# Full optimization reports retain initial/final inventories and worker receipts.
# A normal large installation can exceed 100,000 JSON values even while its
# text is well below the separate 8 MiB limit. The same budget must cover the
# linked follow-up journal, which also retains fresh before/after inventories.
_MAX_JSON_NODES = 500000


def _bounded(value):
    """Reject non-JSON, cyclic, oversized or excessively nested report inputs."""
    pending, nodes, text_size = [(value, 0)], 0, 0
    while pending:
        item, depth = pending.pop()
        nodes += 1
        if nodes > _MAX_JSON_NODES or depth > 24:
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


def _navigation_skipped(value):
    """Keep an explicit per-item skip out of automatic follow-up retries."""
    if not isinstance(value, dict):
        return False
    count = value.get('navigation_cancelled_items')
    if value.get('navigation_cancelled') is True or type(count) is int and count > 0:
        return True
    for item in value.get('items', []) if isinstance(value.get('items'), list) else []:
        if isinstance(item, dict) and (item.get('navigation_skipped') is True or _navigation_skipped(item.get('result'))):
            return True
    return any(_navigation_skipped(value.get(key)) for key in ('app_presence', 'presence'))


def _failed_parent_app_offers(report):
    """Return bounded exact failures only from the current owned parent run."""
    from .automated_software import known_failed_offer
    offers = {}
    for result, _ in _channel_results(report, 'software_updates'):
        rows = result.get('items') if isinstance(result, dict) else None
        for row in rows[:1000] if isinstance(rows, list) else []:
            key = known_failed_offer(row)
            if key is None:
                continue
            if len(offers) >= 1000 and key not in offers:
                continue
            receipt = row['result']
            fields = ('id', 'package_id', 'source', 'from_version', 'to_version', 'status',
                      'installer_started', 'outcomeKnown', 'running', 'exitCode')
            text = row.get('error') or receipt.get('note') or ''
            text = (' '.join(''.join(c if c.isprintable() else ' ' for c in text).split())[-1000:]
                    if isinstance(text, str) else '')
            offers[key] = {'id': row['id'], 'from_version': row['from_version'],
                'to_version': row['to_version'], 'status': 'failed', 'error': text,
                'result': {field: receipt[field] for field in fields}}
    return tuple(offers.values())


def _authorized(settings, channel):
    if channel == 'software_updates':
        return (settings.get('software_updates_enabled') is True
                and settings.get('software_update_agreements_accepted') is True
                and (settings.get('software_update_policy') == 'selected' and bool(settings.get('software_update_ids'))
                     or settings.get('software_update_policy') == 'all_supported' and settings.get('software_update_all_approved') is True
                     or any(rule.get('update_policy') == 'always_update' and rule.get('presence') != 'keep_uninstalled'
                            for rule in settings.get('software_app_rules', []) if isinstance(rule, dict))))
    if channel == 'windows_security_updates':
        return all(settings.get(key) is True for key in ('windows_security_updates_enabled',
                   'windows_security_updates_install_approved', 'windows_security_update_agreements_accepted'))
    return (settings.get('driver_updates_enabled') is True and settings.get('driver_changes_approved') is True
            and settings.get('driver_update_agreements_accepted') is True
            and (settings.get('driver_policy') == 'install_approved' and bool(settings.get('driver_approved_updates'))
                 or settings.get('driver_policy') == 'install_matching' and bool(settings.get('driver_approved_devices'))))


def _preview(backend, report, settings):
    actions, manual = [], []
    prior_barrier = _prior_installer_barrier(backend, report)
    identifiers = _recommendation_ids(report.get('security_review'))
    for identity, title in SECURITY_FIXES.items():
        if identity in identifiers and not _native(backend):
            actions.append({'id': identity, 'title': title, 'kind': 'security'})
    for identity, (title, reason) in MANUAL_SECURITY.items():
        if identity in identifiers:
            if (identity == 'review_windows_update' and not _native(backend)
                    and _authorized(settings, 'windows_security_updates')):
                # This finding has a native install path when that exact saved
                # update scope is already approved. Do not send it to Settings.
                continue
            manual.append({'id': identity, 'title': title, 'reason': reason})
    for channel, (title, _) in CHANNELS.items():
        outcomes = _channel_results(report, channel)
        requested_security = (channel == 'windows_security_updates' and 'review_windows_update' in identifiers
                              and _authorized(settings, channel) and not _native(backend))
        if not requested_security and not any(_remaining(value, failed) for value, failed in outcomes):
            continue
        if any(_navigation_skipped(value) for value, failed in outcomes):
            manual.append({'id': channel, 'title': title,
                'reason': 'You skipped or cancelled an item in this worker. Automatic follow-up will not retry that channel in this run; review it before a new run.'})
            continue
        if _native(backend) and channel != 'software_updates':
            manual.append({'id': channel, 'title': title, 'reason': 'This Windows update channel is unavailable on this platform; use the native OS or vendor updater.'})
        elif prior_barrier:
            manual.append({'id': channel, 'title': title, 'reason': prior_barrier})
        elif channel == 'software_updates' and any(_app_only_preflight_stop(value) for value, _ in outcomes):
            manual.append({'id': channel, 'title': title,
                'reason': 'An application preflight stopped this batch. Review it before a new run; other approved update channels remain available.'})
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
    # A follow-up never enrolls a new app rule or repeats install/removal
    # presence policy. Only the exact originally approved update choices can
    # survive; a changed rule set requires a fresh maintenance run.
    old_rules = [dict(rule, presence='keep_current') for rule in approved.get('software_app_rules', [])]
    current_rules = [dict(rule, presence='keep_current') for rule in current.get('software_app_rules', [])]
    rules_unchanged = old_rules == current_rules
    effective['software_app_rules'] = old_rules if rules_unchanged else []
    for key in ('software_updates_enabled', 'software_update_agreements_accepted', 'software_update_all_approved',
                'windows_security_updates_enabled', 'windows_security_updates_install_approved',
                'windows_security_update_agreements_accepted', 'driver_updates_enabled',
                'driver_changes_approved', 'driver_update_agreements_accepted', 'update_compatibility_mode_enabled'):
        effective[key] = approved.get(key) is True and current.get(key) is True
    if not rules_unchanged:
        effective['software_updates_enabled'] = False
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
        # Exact Always update rules can authorize installs under global review.
        # If every old/new exclusion cannot be retained, disable the channel.
        effective['software_updates_enabled'] = False
    policy = current.get('driver_policy')
    if policy != approved.get('driver_policy') or policy == 'review':
        effective['driver_policy'] = 'review'
    else:
        key = 'driver_approved_updates' if policy == 'install_approved' else 'driver_approved_devices'
        effective[key] = [item for item in current[key] if item in approved[key]]
        if not effective[key]:
            effective['driver_policy'] = 'review'
    return _settings(backend, effective)


def _terminal_software_coverage(value):
    """Account for one known coverage advisory without inventing installer exit.

    The counter is not authority. Require the native WinGet response, matching
    derived scan evidence and explicit terminal receipts for every processed
    item. Other scan errors and absent lifecycle evidence remain barriers.
    """
    from .automated_software import UNKNOWN_VERSION_COVERAGE_WARNING, fresh_candidates
    from .navigation import can_fail_forward, shared_update_refusal
    from .update_scan_evidence import app_scan_evidence

    def terminal(receipt):
        if (not isinstance(receipt, dict) or not can_fail_forward(receipt)
                or receipt.get('status') == 'uncertain' or receipt.get('installation_blocked') is True):
            return False
        for key in ('cancelled', 'uncertain', 'may_continue', 'reboot_required', 'rebootRequired', 'shared_barrier'):
            if key in receipt and (type(receipt[key]) is not bool or receipt[key]):
                return False
        if 'restartNeeded' in receipt and receipt['restartNeeded'] is not None and type(receipt['restartNeeded']) is not bool:
            return False
        if 'requiresReview' in receipt and type(receipt['requiresReview']) is not bool:
            return False
        if 'exitCode' in receipt and receipt['exitCode'] is not None and type(receipt['exitCode']) is not int:
            return False
        if 'failed_items' in receipt and (type(receipt['failed_items']) is not int or receipt['failed_items'] < 0):
            return False
        return not shared_update_refusal(receipt)

    if not terminal(value) or value.get('stop_reason') or value.get('deferred_ids'):
        return False
    if value.get('navigation_cancelled') is True or value.get('navigation_cancelled_items', 0) != 0:
        return False
    scan, evidence, items = value.get('scan'), value.get('app_scan'), value.get('items')
    if (not isinstance(scan, dict) or not isinstance(evidence, dict)
            or not isinstance(items, list) or len(items) > 1000
            or scan.get('source') != 'winget' or scan.get('structured') is not True
            or type(scan.get('exitCode')) is not int or scan['exitCode'] != 0
            or scan.get('complete', True) is not True
            or scan.get('excluded') != [UNKNOWN_VERSION_COVERAGE_WARNING]
            or scan.get('error') or scan.get('errors')):
        return False
    diagnostics = scan.get('stderr')
    if diagnostics is not None and (not isinstance(diagnostics, str) or diagnostics.strip()):
        return False
    rows = scan.get('updates', scan.get('packages'))
    if not isinstance(rows, list) or len(rows) > 1000 or any(not isinstance(row, dict) for row in rows):
        return False
    if len(fresh_candidates(scan)) != len(rows):
        return False
    reported = scan.get('source_reported_offers_count')
    if reported is not None and (type(reported) is not int or reported != len(rows)):
        return False
    failures, started = 0, False
    for item in items:
        if (not isinstance(item, dict) or item.get('status') not in ('completed', 'failed', 'skipped', 'blocked', 'needs_review')
                or not terminal(item.get('result'))):
            return False
        receipt = item['result']
        failures += int(item['status'] == 'failed' or receipt.get('requiresReview') is True)
        started |= receipt['installer_started']
    if value['installer_started'] is not started or value.get('failed_items') != failures + 1:
        return False
    expected = app_scan_evidence(scan, result=value)
    for key in ('schema', 'complete', 'status', 'source', 'criteria', 'offers_count',
                'recognized_offers_count', 'source_reported_offers_count', 'completed_count',
                'verified_count', 'failed_count', 'skipped_count', 'deferred_count',
                'remaining_count', 'no_more_offered', 'errors'):
        if type(evidence.get(key)) is not type(expected[key]) or evidence[key] != expected[key]:
            return False
    return True


def _known_terminal_failure(value):
    """A group needs actual per-item terminal receipts, not inferred flags."""
    from .navigation import can_fail_forward
    if not isinstance(value, dict):
        return False
    items = value.get('items')
    if isinstance(items, list):
        scan_failures = value.get('scan_failed_items', 0)
        if type(scan_failures) is not int or scan_failures not in (0, 1):
            return False
        if scan_failures == 1:
            count = value.get('failed_items')
            return type(count) is int and count >= 1 and _terminal_software_coverage(value)
        failed = [row for row in items if isinstance(row, dict) and (
            row.get('status') in ('failed', 'blocked', 'needs_review') or isinstance(row.get('result'), dict)
            and (row['result'].get('requiresReview') is True or row['result'].get('failed_items', 0)))]
        count = value.get('failed_items', 0)
        if type(count) is not int or count < 0 or count > len(failed) or not failed:
            return False
        return all(_known_terminal_failure(row) for row in failed)
    receipt = value.get('result')
    if isinstance(receipt, dict) and isinstance(receipt.get('items'), list):
        return _known_terminal_failure(receipt)
    return can_fail_forward(receipt if isinstance(receipt, dict) else value)


def _app_only_preflight_stop(value):
    """A stopped app batch does not imply an unresolved shared installer."""
    from .navigation import can_fail_forward, shared_update_refusal
    if (not isinstance(value, dict) or not isinstance(value.get('stop_reason'), str)
            or not value['stop_reason'].strip() or type(value.get('failed_items')) is not int
            or value['failed_items'] < 1 or type(value.get('installer_started')) is not bool
            or value.get('outcomeKnown') is not True or value.get('running') is not False):
        return False
    items = value.get('items')
    if not isinstance(items, list) or len(items) > 1000:
        return False

    def app_refusal(receipt):
        return (isinstance(receipt, dict) and receipt.get('failure_scope') == 'app_batch'
                and receipt.get('failureKind') in ('source_revalidation', 'cache_unavailable',
                                                   'manager_unavailable', 'preflight_failed')
                and receipt.get('installer_started') is False and receipt.get('outcomeKnown') is True
                and receipt.get('running') is False and receipt.get('exitCode') is None)

    if items:
        if any(not isinstance(row, dict) or not isinstance(row.get('result'), dict) for row in items):
            return False
        receipts = [row['result'] for row in items]
        if (items[-1].get('status') != 'failed' or not app_refusal(receipts[-1])
                or not all(type(receipt.get('installer_started')) is bool for receipt in receipts[:-1])
                or value['installer_started'] != any(receipt['installer_started'] for receipt in receipts)):
            return False
        for row, receipt in zip(items, receipts):
            if any(key in row and row[key] is not receipt.get(key)
                   for key in ('installer_started', 'outcomeKnown', 'running')):
                return False
    elif not app_refusal(value):
        return False
    else:
        receipts = []
    pending, count = [value], 0
    while pending:
        item = pending.pop()
        count += 1
        if count > 20000:
            return False
        if isinstance(item, dict):
            if (item.get('status') in ('uncertain', 'running')
                    or 'outcomeKnown' in item and item['outcomeKnown'] is not True
                    or 'installer_started' in item and type(item['installer_started']) is not bool
                    or any(key in item and item[key] is not False for key in (
                        'running', 'uncertain', 'cancelled', 'timed_out', 'may_continue', 'reboot_required', 'rebootRequired',
                        'shared_barrier', 'blocked_by_prior_installer', 'installation_blocked'))
                    or item.get('restartNeeded') is not None and item['restartNeeded'] is not False
                    or any(key in item and item[key] is not None and not isinstance(item[key], str)
                           for key in ('failure_scope', 'failureKind'))
                    or shared_update_refusal(item)):
                return False
            code = item.get('exitCode')
            if code is not None and (type(code) is not int or code & 0xffffffff in (
                    3010, 1641, 0x8A150109, 0x8A15010A, 0x8A15010B)):
                return False
            pending.extend(part for part in item.values() if isinstance(part, (list, dict)))
        elif isinstance(item, list):
            pending.extend(part for part in item if isinstance(part, (list, dict)))
    return all(can_fail_forward(receipt) for receipt in receipts[:-1])


def _stop_reason(value, *, prior=False):
    """Normalize updater-specific stop flags across the entire batch."""
    pending = [value]
    while pending:
        item = pending.pop()
        if isinstance(item, dict):
            from .navigation import shared_update_refusal
            terminal_failure = _known_terminal_failure(item)
            shared = dict(item)
            if terminal_failure:
                # Aggregated review flags do not override proven package-local
                # receipts. Explicit shared scope/codes remain authoritative.
                shared.pop('requiresReview', None)
            if shared_update_refusal(shared):
                return 'Shared update safety checks require review before another installer.'
            if item.get('restartNeeded') is True or item.get('reboot_required') is True:
                return 'A restart is required. Remaining installers were deferred.'
            if (item.get('may_continue') is True or item.get('running') is True or item.get('outcomeKnown') is False
                    or item.get('status') == 'uncertain' or item.get('uncertain') is True):
                return 'An operation may still be running or its outcome is uncertain. Verify its state before another installer.'
            if not prior and item.get('requiresReview') is True and not terminal_failure:
                return 'An update safety check requires review before another installer.'
            if item.get('cancelled') is True:
                return 'Cancellation requested; remaining actions were deferred.'
            if not prior and (item.get('status') == 'failed' or item.get('failed_items', 0)) and not terminal_failure:
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
            if channel == 'software_updates' and not _native(backend) and _app_only_preflight_stop(value):
                continue
            if _native(backend) and channel == 'software_updates' and (
                    failed or value.get('failed_items', 0)
                    or any(isinstance(item, dict) and item.get('status') == 'failed'
                           for item in (value['items'] if isinstance(value.get('items'), list) else []))) and not _known_terminal_failure(value):
                return 'The previous native package operation failed and its installer termination is unverified. Review the native package manager before retrying.'
            reason = _stop_reason(value, prior=True)
            if reason:
                return 'The completed run has an unresolved installer or restart state. Review its saved outcome before another installer.'
    return ''


def _blocked_installer(channel, reason, *, prior=False):
    """Record only this follow-up's verified no-launch refusal, never an old scan."""
    reason = str(reason)[:4000]
    outcome = {'status': 'blocked', 'skipped': True, 'review_only': True, 'requiresReview': True,
        'reason': reason, 'stop_reason': reason, 'installation_block_reason': reason,
        'installation_blocked': True, 'blocked_by_prior_installer': prior is True,
        'installer_started': False, 'outcomeKnown': True, 'running': False, 'items': []}
    scan_key = {'software_updates': 'app_scan', 'driver_updates': 'driver_scan'}.get(channel)
    if scan_key:
        outcome[scan_key] = {'schema': 1, 'complete': False, 'status': 'not_started',
            'offers_count': None, 'summary': 'This follow-up search was not started because installation is blocked. ' + reason}
    return outcome


def _local_security_stop_reason(action, outcome):
    """A proven terminal control failure need not poison unrelated work.

    A native error, timeout, missing lifecycle record, restart or shared refusal
    remains a barrier. Only the three fixed non-removal controls can be local.
    """
    if action.get('id') == 'software_updates' and _app_only_preflight_stop(outcome):
        return ''
    known_local = (action.get('id') in SECURITY_FIXES and isinstance(outcome, dict)
        and outcome.get('id') == action['id'] and outcome.get('status') in ('failed', 'blocked')
        and outcome.get('failure_scope') == 'security_control'
        and outcome.get('outcomeKnown') is True and outcome.get('running') is False
        and outcome.get('may_continue') is False and type(outcome.get('operation_started')) is bool
        and all(key not in outcome or outcome[key] is False for key in (
            'timed_out', 'cancelled', 'uncertain', 'restartNeeded', 'reboot_required',
            'rebootRequired', 'shared_barrier')))
    if not known_local:
        return _stop_reason(outcome)
    observed = dict(outcome)
    for key in ('status', 'failed_items', 'requiresReview'):
        observed.pop(key, None)
    # Keep every nested outcome and restart/shared indicator binding.
    return _stop_reason(observed)


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
    for stage in report['stages']:
        if stage.get('action_id') != 'software_updates':
            continue
        outcome = stage.get('result')
        rows = outcome.get('items') if isinstance(outcome, dict) else None
        for row in rows[:1000] if isinstance(rows, list) else []:
            receipt = row.get('result') if isinstance(row, dict) else None
            if (isinstance(receipt, dict) and receipt.get('failureKind') == 'failed_in_parent_run'
                    and receipt.get('requiresReview') is True and receipt.get('installer_started') is False
                    and receipt.get('outcomeKnown') is True and receipt.get('running') is False):
                checks.append({'kind': 'previous_app_update_failure', 'target': row.get('id', 'Application'),
                    'status': 'needs_review', 'previous_exit_code': receipt.get('previous_exit_code'),
                    'reason': receipt.get('note') or 'The earlier installer failed; skipping its immediate retry did not repair this application.'})
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
        from .emergency_stop import controller_for
        controller = controller_for(self.backend)
        controller.check()
        cancel_event = controller.bind_cancel(cancel_event)
        requested = _validate_report(report)
        approved = _settings(self.backend, settings)
        reviewed_ids = {row['id'] for row in _preview(self.backend, requested, approved)['actions']}
        workflow = self.backend.workflow
        if not workflow.lock.acquire(blocking=False):
            raise RuntimeError('An optimization or follow-up run is already active.')
        lease = None
        navigation_started = False
        navigation = getattr(workflow, 'navigation', None)
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
            from .navigation import NavigationHub
            if isinstance(navigation, NavigationHub):
                navigation.begin(identifier, cancel_event=cancel_event)
                navigation_started = True
            path = directory / (identifier + '.json')
            result = {'schema': 1, 'id': identifier, 'parent_run_id': source['id'], 'started_at': time.time(),
                      'settings': approved, 'status': 'running', 'stages': [], 'cancelled': False,
                      'manual_items': [], 'counts': {key: 0 for key in _ACTION_STATES},
                      'log_path': str(path), 'note': 'The Auto continue click or saved automatic-follow-up choice authorizes these supported security fixes for one attempt. Update installation remains within saved scopes and agreements. Processes, startup entries, application removal and cleanup are not included.'}
            from .worker_totals import WorkerResourceTotals
            maintenance_totals = WorkerResourceTotals()
            worker_totals = {}
            failed_parent_offers = _failed_parent_app_offers(source) if not _native(self.backend) else ()
            original_plan = _preview(self.backend, source, approved)
            original_plan['actions'] = [row for row in original_plan['actions'] if row['id'] in reviewed_ids]
            result['reviewed_plan'] = original_plan
            result['manual_items'] = copy.deepcopy(original_plan['manual_items'])
            if isinstance(source.get('process_review'), dict):
                result['process_review'] = copy.deepcopy(source['process_review'])
            def journal():
                result['resource_totals'] = maintenance_totals.snapshot()
                result['worker_resource_totals'] = {key: ledger.snapshot() for key, ledger in worker_totals.items()}
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
            initial_installer_barrier = _prior_installer_barrier(self.backend, source)
            installer_barrier = initial_installer_barrier
            installer_state_checked = False
            for index, action in enumerate(actions):
                ledger = (worker_totals.setdefault(action['id'], WorkerResourceTotals(action['id']))
                          if action['id'] in CHANNELS else None)
                def terminal_totals(stage):
                    outcome = stage.get('result')
                    if ledger is not None and isinstance(outcome, dict):
                        ledger.result(outcome)
                        stage['result'] = dict(outcome, resource_totals=ledger.snapshot())
                stage = {'title': action['title'], 'action_id': action['id'], 'kind': action['kind'],
                         'started_at': time.time(), 'status': 'prepared', 'action_status': 'prepared'}
                result['stages'].append(stage)
                if stopped() or halted:
                    reason = result.get('stop_reason', 'Cancellation requested.')
                    outcome = (_blocked_installer(action['id'], reason)
                               if halted and not stopped() and action['id'] in CHANNELS else
                               {'skipped': True, 'reason': reason})
                    stage.update(status='skipped', action_status='deferred', result=outcome)
                    result['counts']['deferred'] += 1
                    result['cancelled'] = result['cancelled'] or stopped()
                    terminal_totals(stage)
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
                                         result=_blocked_installer(action['id'], installer_barrier,
                                             prior=bool(initial_installer_barrier)))
                            result['counts']['deferred'] += 1
                            result['stop_reason'] = installer_barrier
                            if not any(item['id'] == 'installer_state' for item in result['manual_items']):
                                result['manual_items'].append({'id': 'installer_state', 'title': 'Review installer and restart state',
                                                               'reason': installer_barrier})
                            stage['finished_at'] = time.time()
                            terminal_totals(stage)
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
                            if ledger is not None:
                                ledger.observe(event)
                            stage['current_item'] = copy.deepcopy(event)
                            journal()  # Existing helpers call this before each installer.
                            update = dict(event, stage=action['title'], percentage=int(index * 85 / max(1, len(actions))))
                            update.setdefault('processes', [event.get('description') or action['title']])
                            update['worker_resource_totals'] = {key: value.snapshot() for key, value in worker_totals.items()}
                            progress(update)
                        execution_options = ({'prior_failed_offers': failed_parent_offers}
                            if action['id'] == 'software_updates' and failed_parent_offers else {})
                        outcome = self._execute(action, effective, cancel_event, item_progress, **execution_options)
                    _bounded(outcome)
                    status = _action_status(outcome)
                    stage.update(result=outcome, action_status=status,
                                 status='failed' if status in ('failed', 'blocked', 'uncertain') else 'cancelled' if status == 'cancelled' else 'completed')
                    result['counts'][status if status in result['counts'] else 'blocked'] += 1
                    reason = _local_security_stop_reason(action, outcome)
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
                terminal_totals(stage)
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
                try:
                    if navigation_started:
                        navigation.end()
                finally:
                    if lease is not None:
                        lease.release()
            finally:
                workflow.active = False
                workflow.lock.release()

    def _execute(self, action, settings, cancel_event, progress, *, prior_failed_offers=()):
        identity = action['id']
        if identity in SECURITY_FIXES:
            return self.backend.apply_security_recommendation(identity, cancel_event=cancel_event)
        if identity == 'software_updates':
            if _native(self.backend):
                from .native_workflow import run_native_software
                return run_native_software(self.backend, settings, cancel_event=cancel_event, item_callback=progress)
            from .automated_software import run_software
            options = {'prior_failed_offers': prior_failed_offers} if prior_failed_offers else {}
            return run_software(self.backend, settings, cancel_event=cancel_event, item_callback=progress, **options)
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


def apply_saved_windows_security_recommendation(backend, approved, *, cancel_event=None):
    """Use an already-approved native update scope from the recommendation UI.

    Backend owns the global maintenance lease before entering. The native
    service repeats its catalog, agreement, reboot and exact-revision checks.
    Merely opening Settings is never reported as an installed update.
    """
    identifier = uuid.uuid4().hex
    directory = Path(backend.data_dir) / 'security-recommendations'
    path = directory / (identifier + '.json')
    result = {'schema': 1, 'id': identifier, 'kind': 'security-recommendation',
              'recommendation_id': 'review_windows_update', 'status': 'prepared',
              'started_at': time.time(), 'stages': [], 'installed_count': 0,
              'verified_installs': 0, 'verified': False,
              'operation_started': False, 'preferences_saved': False, 'log_path': str(path)}

    def journal():
        _ordinary_data_path(directory)
        _ordinary_data_path(path, file=True)
        _bounded(result)
        atomic_json(path, result)

    journal()  # A durable owned identity precedes the native service call.
    service_requested = False
    try:
        current = _settings(backend, backend.preferences())
        settings = _narrow_settings(backend, approved, current)
        from .parallel_updates import pending_updates_barrier
        prior_barrier = pending_updates_barrier(backend)
        result['settings'] = {key: settings[key] for key in (
            'windows_security_updates_enabled', 'windows_security_updates_install_approved',
            'windows_security_update_agreements_accepted')}
        if cancel_event is not None and cancel_event.is_set():
            result.update(status='cancelled', cancelled=True, reason='Cancellation requested before updating.')
        elif not _authorized(settings, 'windows_security_updates'):
            result.update(status='blocked', reason='Saved Windows security-update approval was revoked. No installation was requested.')
        elif prior_barrier:
            result.update(status='blocked', reason=str(prior_barrier), installation_blocked=True,
                          outcomeKnown=True, running=False, may_continue=False)
        else:
            stage = {'title': CHANNELS['windows_security_updates'][1],
                     'started_at': time.time(), 'status': 'prepared'}
            result['stages'].append(stage)
            result['status'] = 'running'
            journal()
            service_requested = True
            result['operation_started'] = True
            outcome = backend.windows_security_updates(settings, cancel_event=cancel_event)
            _bounded(outcome)
            if not isinstance(outcome, dict):
                raise ValueError('The Windows security update service returned invalid metadata.')
            action_status = _action_status(outcome)
            stage.update(result=copy.deepcopy(outcome), finished_at=time.time(),
                         status='completed' if action_status == 'completed' else
                                'cancelled' if action_status == 'cancelled' else
                                'skipped' if action_status in ('skipped', 'deferred') else 'failed')
            result.update(status=action_status, result=copy.deepcopy(outcome))
            for key in ('cancelled', 'may_continue', 'outcomeKnown', 'running', 'reboot_required'):
                if key in outcome:
                    result[key] = copy.deepcopy(outcome[key])
            journal()
            verification_report = dict(result, settings=settings)
            try:
                verified = backend.verify_optimization(verification_report, cancel_event=cancel_event, timeout=30)
                _bounded(verified)
                if not _valid_verification(verified) or verified.get('run_id') != identifier:
                    raise ValueError('The security update verifier returned invalid metadata.')
                result['final_verification'] = copy.deepcopy(verified)
            except Exception as error:
                result['final_verification'] = _unavailable_verification(
                    'The native installation history could not be verified (' + type(error).__name__ + ').')
            completed = [row for row in outcome.get('items', []) if isinstance(row, dict)
                         and row.get('status') in ('completed', 'applied')]
            result['installed_count'] = len(completed)  # Native installer claims; separate readback below.
            checks = [check for entry in result['final_verification'].get('stages', [])
                      if isinstance(entry, dict) and entry.get('title') == stage['title']
                      for check in entry.get('checks', [])
                      if isinstance(check, dict) and check.get('kind') == 'local_update_history']
            offers = outcome.get('scan', {}).get('updates', []) if isinstance(outcome.get('scan'), dict) else []
            expected = set()
            for row in completed:
                identity = row.get('id')
                matching = [offer for offer in offers if isinstance(offer, dict)
                            and isinstance(offer.get('id'), str) and isinstance(identity, str)
                            and offer['id'].casefold() == identity.casefold()]
                if len(matching) == 1 and type(matching[0].get('revision')) is int and matching[0]['revision'] > 0:
                    expected.add((identity.casefold(), matching[0]['revision']))
            confirmed = set()
            for check in checks:
                evidence = check.get('evidence')
                if (check.get('status') == 'confirmed' and isinstance(check.get('target'), str)
                        and isinstance(evidence, dict) and type(evidence.get('revision')) is int):
                    confirmed.add((check['target'].casefold(), evidence['revision']))
            result['verified_installs'] = len(expected & confirmed)
            verified = result['final_verification']
            result['verified'] = (bool(completed) and len(expected) == len(completed)
                and len(checks) == len(confirmed) == result['verified_installs'] == len(completed)
                and verified.get('status') == 'confirmed' and verified.get('coverage_complete') is True
                and verified.get('cancelled') is False and verified.get('deadline_reached') is False
                and verified.get('failed_items') == 0 and verified.get('errors') == []
                and verified['counts']['confirmed'] == len(completed) and verified['counts']['not_applicable'] == 0
                and verified['counts']['needs_review'] == 0 and verified['counts']['unverified'] == 0)
            if completed and action_status == 'completed':
                if not result['verified']:
                    result.update(status='needs_review', reason='Windows returned installation results, but fresh exact-revision history did not verify every update.')
                else:
                    result['reason'] = 'Fresh Windows installation history verified the installed exact update revisions.'
            elif action_status == 'skipped':
                result['reason'] = outcome.get('note') or 'No applicable approved update was installed.'
    except Exception as error:
        if service_requested:
            result.update(status='uncertain', outcomeKnown=False, running=None, may_continue=True,
                          reason='The recommendation outcome could not be verified (' + type(error).__name__ + '). No automatic retry was started.')
        else:
            result.update(status='blocked', outcomeKnown=True, running=False, may_continue=False,
                          reason='The recommendation preflight could not be verified (' + type(error).__name__ + '). No update service or installer was started.')
    result['finished_at'] = time.time()
    journal()
    return result
