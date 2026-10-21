"""Fresh, scoped WinGet updates; no installer starts without saved permission."""
from __future__ import annotations

import re
from typing import Any

_PACKAGE_ID = re.compile(r'^[A-Za-z0-9][A-Za-z0-9_.+\-]{1,127}$')
_VERSION = re.compile(r'[vV]?[0-9][0-9A-Za-z._+~\-]{0,127}')


def fresh_candidates(scan: Any) -> dict[str, dict[str, Any]]:
    """An unsupported/partial table cannot supply automatic update identities."""
    if (not isinstance(scan, dict) or scan.get('structured') is not True
            or type(scan.get('exitCode')) is not int or scan['exitCode'] != 0):
        return {}
    if scan.get('source', 'winget') != 'winget':
        return {}
    items = scan.get('updates', scan.get('packages'))
    if not isinstance(items, list) or len(items) > 1000:
        return {}
    candidates, repeated, seen = {}, set(), set()
    for item in items:
        if not isinstance(item, dict):
            continue
        identity = item.get('id', item.get('package_id'))
        if (not isinstance(identity, str) or not _PACKAGE_ID.fullmatch(identity)
                or identity.endswith('.') or '...' in identity):
            continue
        folded = identity.casefold()
        if folded in seen:
            repeated.add(folded)
        seen.add(folded)
        if item.get('package_id', identity) != identity or item.get('source') != 'winget':
            continue
        version, available = item.get('version'), item.get('available')
        if any(not isinstance(part, str) or not _VERSION.fullmatch(part)
               or part.endswith('.') or '..' in part for part in (version, available)):
            continue
        if version.casefold() == available.casefold() or item.get('pinned') is True:
            continue
        candidates[identity] = dict(item)
    return {identity: item for identity, item in candidates.items() if identity.casefold() not in repeated}


def run_software(backend: Any, configuration: dict[str, Any], *, cancel_event=None, item_callback=None) -> dict[str, Any]:
    """Apply only a current scan's exact IDs under the saved selected/all scope.

    The native updater checks pins again before each installation. No generic
    upgrade-all, force, include-unknown, pin override or reboot flag is used.
    """
    cancelled = lambda: cancel_event is not None and cancel_event.is_set()
    if cancelled():
        return {'review_only': True, 'cancelled': True, 'items': [], 'failed_items': 0}
    scan = backend.updates_scan()
    if not isinstance(scan, dict):
        raise RuntimeError('The software update scan returned invalid metadata.')
    code_ok = type(scan.get('exitCode')) is int and scan['exitCode'] == 0
    policy = configuration['software_update_policy']
    if policy == 'review':
        return {'review_only': True, 'scan': scan, 'failed_items': 0 if code_ok else 1,
                'note': 'Software update review only; no installer was started. Nonzero or unknown scan exit codes require review.'}
    if configuration.get('software_update_agreements_accepted') is not True:
        raise ValueError('Unattended software updates require saved agreement acceptance.')
    if policy == 'all_supported' and configuration.get('software_update_all_approved') is not True:
        raise ValueError('All-supported software updates require separate saved scope approval.')
    if policy not in ('selected', 'all_supported'):
        raise ValueError('Unknown software-update policy.')
    candidates = fresh_candidates(scan)
    exclusions = {identity.casefold() for identity in configuration.get('software_update_exclude_ids', [])}
    selected = ([identity for identity in candidates if identity.casefold() not in exclusions]
                if policy == 'all_supported' else [identity for identity in configuration['software_update_ids']
                if identity in candidates and identity.casefold() not in exclusions])
    results, deferred = [], []
    restart_needed = False
    interrupted = False
    stop_reason = ''
    for index, identity in enumerate(selected):
        if cancelled():
            deferred = selected[index:]
            break
        current = candidates[identity]
        if item_callback:
            item_callback({'current': index, 'total': len(selected), 'item': index + 1,
                           'items': len(selected), 'id': identity,
                           'description': f'Updating approved package {index + 1}/{len(selected)}: {identity} ({current["version"]} to {current["available"]})',
                           'log': 'Updating approved exact ID: ' + identity})
        # Progress handlers can deliver cancellation before a mutation begins.
        if cancelled():
            deferred = selected[index:]
            break
        try:
            options = {'cancel_event': cancel_event} if cancel_event is not None else {}
            outcome = backend.update_app_unattended(identity, **options)
            known_exit = isinstance(outcome, dict) and type(outcome.get('exitCode')) is int
            skipped = isinstance(outcome, dict) and outcome.get('skipped') is True
            successful = (known_exit and outcome['exitCode'] in (0, 3010, 1641)
                          and outcome.get('success', True) is not False)
            results.append({'id': identity, 'name': current.get('name', identity),
                            'from_version': outcome.get('from_version', current['version']) if isinstance(outcome, dict) else current['version'],
                            'to_version': outcome.get('to_version', current['available']) if isinstance(outcome, dict) else current['available'],
                            'status': 'cancelled' if isinstance(outcome, dict) and outcome.get('cancelled') is True else 'skipped' if skipped else 'completed' if successful else 'failed', 'result': outcome})
            if isinstance(outcome, dict) and outcome.get('cancelled') is True:
                interrupted = True
                stop_reason = 'Cancelled before this installer started; remaining applications were deferred.'
                deferred = selected[index + 1:]
                break
            if isinstance(outcome, dict):
                restart_needed = (outcome.get('restartNeeded') is True or
                                  known_exit and outcome['exitCode'] in (3010, 1641))
                uncertain = (outcome.get('running') is True or outcome.get('outcomeKnown') is False
                             or any(marker in str(outcome.get('status', '')).casefold()
                                    for marker in ('timed out', 'timeout', 'still running')))
            else:
                uncertain = True
            if isinstance(outcome, dict) and outcome.get('requiresReview') is True:
                stop_reason = 'The update safety checks need review before remaining applications can be updated.'
                deferred = selected[index + 1:]
                break
            if restart_needed or uncertain or (not known_exit and not skipped):
                stop_reason = ('A restart is required before any remaining update.' if restart_needed else
                               'The installer outcome is uncertain; verify its state before continuing.')
                deferred = selected[index + 1:]
                break
        except Exception as error:
            results.append({'id': identity, 'status': 'failed', 'error': str(error)})
            # Exceptions cannot establish that an installer child has stopped.
            deferred = selected[index + 1:]
            stop_reason = 'The updater raised an error; verify installer state before continuing.'
            break
    scan_items = scan.get('updates', scan.get('packages'))
    untrusted = (not code_ok or scan.get('structured') is not True or scan.get('source', 'winget') != 'winget'
                 or not isinstance(scan_items, list) or len(scan_items) > 1000)
    needs_review = sum(item.get('result', {}).get('requiresReview') is True for item in results if isinstance(item.get('result'), dict))
    return {'review_only': not bool(selected), 'scan': scan, 'policy': policy, 'items': results,
            'failed_items': sum(item['status'] == 'failed' for item in results) + needs_review + int(untrusted),
            'skipped_items': sum(item['status'] == 'skipped' for item in results), 'requires_review_items': needs_review,
            'cancelled': cancelled() or interrupted, 'deferred_ids': deferred, 'restartNeeded': restart_needed,
            'stop_reason': stop_reason,
            'excluded_ids': [identity for identity in candidates if identity.casefold() in exclusions],
            'not_available_ids': [identity for identity in configuration.get('software_update_ids', []) if identity not in candidates],
            'note': 'Only complete known-version identities from a successful fresh winget-source scan may be updated. Saved exclusions and native pin checks are honored; no force, pin override, unknown-version update or automatic restart is requested. Unsupported applications require their publisher updater.'}