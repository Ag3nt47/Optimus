"""Plain, bounded action descriptions for security recommendation review.

These labels describe an existing backend route; they grant no approval and
never replace fresh permission, scope, lifecycle or installation verification.
"""
from __future__ import annotations


_ACTIONS = {
    'enable_defender_realtime': (
        'Apply', 'Enable Microsoft Defender real-time protection',
        'Request the supported Windows protection setting and verify its readback.'),
    'enable_firewall': (
        'Apply', 'Enable disabled Windows Firewall profiles',
        'Enable the disabled profiles and verify their state, preserving existing firewall rules.'),
    'update_defender_signatures': (
        'Apply', 'Update Microsoft Defender security intelligence',
        'Request the supported security-intelligence update and verify its current state.'),
    'remediate_defender_threats': (
        'Review', 'Request Defender remediation of reviewed active threats',
        'Review the exact current active-threat scope before requesting remediation under Defender policy. '
        'Selecting this row does not approve removal.'),
    'review_security': (
        'Open', 'Open Windows Security',
        'Open the Windows Security application for review. This action does not remove detections, '
        'purge quarantine or replace the current protection provider.'),
    'review_windows_update': (
        'Open', 'Open Windows Update',
        'Open Windows Update settings for review. Automatic installation requires saved '
        'Security/Critical update approval and accepted update agreements.'),
    'restart_required': (
        'Open', 'Open Windows restart controls',
        'Open Windows controls so you can choose a restart time. Optimus does not restart the PC.'),
    'review_legacy_patches': (
        'View', 'View vendor and legacy patch coverage',
        'Display the local provider and hardware-support report. This report does not download '
        'or install a patch, and does not establish that a compatible patch is available.'),
    'native-system-updates': (
        'Review', 'Review native system update guidance',
        'Review the native OS or vendor update route. This recommendation does not install '
        'OS, firmware or driver updates.'),
}


def _text(value, maximum=600):
    if not isinstance(value, str):
        return ''
    return ' '.join(''.join(c if c.isprintable() else ' ' for c in value[:4096]).split())[:maximum]


def recommendation_presentation(recommendation, *, windows_install_approved=False):
    """Describe a known route without trusting caller-supplied action text.

    The approval flag is display context only. The backend re-reads saved
    settings and validates exact fresh offers when this action is requested.
    Unknown identifiers remain visibly unsupported rather than promising a fix.
    """
    identifier = recommendation.get('id') if isinstance(recommendation, dict) else None
    action = _ACTIONS.get(identifier) if isinstance(identifier, str) else None
    if action is None:
        return {'selection_label': 'Review', 'action_label': 'Unsupported recommendation',
            'confirmation_title': 'Review recommendation',
            'confirmation_question': 'Review this recommendation?',
            'description': 'Optimus has no supported automatic action for this recommendation.',
            'supported': False}
    selection, label, description = action
    if identifier == 'review_windows_update' and windows_install_approved is True:
        selection, label = 'Install', 'Install saved-approved Windows security updates'
        description = ('Search fresh Security/Critical update offers and install eligible exact revisions '
            'within your saved approval and accepted agreements. Reboot, device, installation and '
            'Windows policy checks still apply. An offer is not proof of a completed installation.')
    question = (label + ' now?' if selection != 'Review' else 'Review and approve this action now?')
    return {'selection_label': selection, 'action_label': label,
        'confirmation_title': label, 'confirmation_question': question,
        'description': description, 'supported': True}


def _count(value):
    return value if type(value) is int and 0 <= value <= 1000000 else None


def recommendation_continuation_reason(result):
    """Return a stop reason, or an empty string for a terminal recommendation.

    This is a queue gate, never permission to repeat an operation. Inspect only
    current operation receipts: root, ``result``, and ``items``. Security state
    readbacks, process inventories, old journals and verification reports are
    observations, not live operation lifecycle records.
    """
    malformed = 'The recommendation returned incomplete or invalid operation evidence. Remaining actions were deferred.'
    unresolved = 'An operation may still be running or its outcome is uncertain. Remaining actions were deferred.'
    restart = 'A restart is required before continuing. Remaining actions were deferred.'
    cancelled = 'The recommendation was cancelled. Remaining actions were deferred.'
    shared = 'Shared update or security prerequisites need review. Remaining actions were deferred.'
    terminal = frozenset(('completed', 'applied', 'skipped', 'already_configured',
                          'not_applicable', 'manual_action_required', 'review'))
    failures = frozenset(('failed', 'blocked', 'needs_review'))
    stopping = frozenset(('uncertain', 'running', 'prepared', 'installing', 'pending',
                          'pending_activation', 'pending_verification', 'deferred', 'timed_out', 'timeout'))
    boolean_fields = ('outcomeKnown', 'running', 'may_continue', 'uncertain', 'timed_out',
        'cancelled', 'canceled', 'restartNeeded', 'reboot_required', 'rebootRequired',
        'shared_barrier', 'installation_blocked', 'blocked_by_prior_installer',
        'operation_started', 'installer_started', 'review_only', 'read_only', 'requiresReview', 'ok', 'skipped')
    restart_codes = frozenset((3010, 1641, 0x8A150109, 0x8A15010A, 0x8A15010B))
    visited, nodes = set(), [0]

    def inspect(value, depth):
        nodes[0] += 1
        if (type(value) is not dict or not value or depth > 12 or nodes[0] > 2048
                or len(value) > 256 or id(value) in visited):
            return malformed, False
        visited.add(id(value))
        try:
            for key in boolean_fields:
                if key in value and type(value[key]) is not bool:
                    return malformed, False
            if 'failed_items' in value and _count(value['failed_items']) is None:
                return malformed, False
            if (value.get('running') is True or value.get('may_continue') is True
                    or value.get('uncertain') is True or value.get('timed_out') is True
                    or value.get('outcomeKnown') is False):
                return unresolved, False
            if value.get('cancelled') is True or value.get('canceled') is True:
                return cancelled, False
            if any(value.get(key) is True for key in ('restartNeeded', 'reboot_required', 'rebootRequired')):
                return restart, False
            for key in ('exitCode', 'hresult', 'aggregateHresult', 'aggregate_hresult'):
                if key not in value:
                    continue
                code = value[key]
                if code is None and key == 'exitCode':
                    continue
                if type(code) is not int or not -0x80000000 <= code <= 0xffffffff:
                    return malformed, False
                if code & 0xffffffff in restart_codes:
                    return restart, False
            for key in ('resultCode', 'aggregateResultCode', 'itemResultCode'):
                if key in value and (type(value[key]) is not int or value[key] not in (2, 3, 4, 5)):
                    return malformed, False
            if any(value.get(key) is True for key in ('shared_barrier', 'installation_blocked', 'blocked_by_prior_installer')):
                return shared, False
            for key in ('failure_scope', 'failureKind'):
                if key in value and (not isinstance(value[key], str) or len(value[key]) > 128):
                    return malformed, False
            if value.get('failure_scope') in ('shared', 'system', 'batch', 'app_batch'):
                return shared, False
            from .navigation import shared_update_refusal
            # Review of one known local refusal does not become a shared
            # barrier merely because its UI asks for attention. Keep explicit
            # shared scopes, native codes and failure identities binding.
            explicit = dict(value)
            explicit.pop('requiresReview', None)
            if 'aggregate_hresult' in value:
                if ('aggregateHresult' in value
                        and (value['aggregateHresult'] & 0xffffffff) != (value['aggregate_hresult'] & 0xffffffff)):
                    return malformed, False
                explicit['aggregateHresult'] = value['aggregate_hresult']
            if shared_update_refusal(explicit):
                return shared, False
            status = value.get('status')
            if 'status' in value and (not isinstance(status, str) or not status or len(status) > 80):
                return malformed, False
            if status in ('cancelled', 'canceled'):
                return cancelled, False
            if status in stopping:
                return unresolved, False
            if status is not None and status not in terminal and status not in failures and status != 'partial':
                return malformed, False
            children = []
            if 'result' in value:
                children.append(value['result'])
            if 'items' in value:
                rows = value['items']
                if type(rows) is not list or len(rows) > 1000:
                    return malformed, False
                children.extend(rows)
            children_known = []
            for child in children:
                reason, known = inspect(child, depth + 1)
                if reason:
                    return reason, False
                children_known.append(known)
            known_idle = (value.get('outcomeKnown') is True and value.get('running') is False
                          and value.get('may_continue') is False)
            native_terminal = (type(value.get('ok')) is bool and value.get('may_continue') is False
                               or value.get('ok') is True and value.get('skipped') is True)
            # WUA's successful synchronous receipt need not repeat generic
            # lifecycle flags. Recognize the same complete typed terminal
            # shape as SecurityUpdates; ``ok`` by itself proves nothing.
            wua_terminal = (value.get('ok') is True and value.get('reboot_required') is False
                and all(type(value.get(key)) is int and value[key] == 2
                        for key in ('resultCode', 'itemResultCode'))
                and all(type(value.get(key)) is int and value[key] >= 0
                        for key in ('hresult', 'aggregate_hresult')))
            manual = value.get('read_only') is True or value.get('review_only') is True
            known = (known_idle or native_terminal or wua_terminal or status in terminal or manual
                     or bool(children_known) and all(children_known))
            return ('', True) if known else (malformed, False)
        finally:
            visited.remove(id(value))

    return inspect(result, 0)[0]


def recommendation_result_text(result):
    """Expose the action's reported outcome, not merely its generic status.

    Counts come from bounded terminal receipts, never from discovery offers or
    progress. Explicit verification remains distinct from installer completion.
    No result shape is treated as authority for another action.
    """
    if not isinstance(result, dict):
        return 'Unverified: the action returned no usable result.'
    status = _text(result.get('status'), 80) or 'Returned result'
    parts = [status.replace('_', ' ')]
    manual = status == 'manual_action_required' or result.get('review_only') is True
    if result.get('opened'):
        parts.append('Windows controls opened; complete any remaining action there')
    if manual:
        parts.append('No automatic installation or removal was applied')

    # These counters are explicitly reported installation results. Never
    # substitute scan.updates/eligible_count/remaining_count for them.
    for key, title in (('installed_count', 'reported installed'), ('updated_count', 'reported updated')):
        count = _count(result.get(key))
        if count is not None and not manual:
            parts.append(f'{count} {title}')
    verified_installs = _count(result.get('verified_installs'))
    if verified_installs is not None and not manual:
        parts.append(f'{verified_installs} verified installation' + ('' if verified_installs == 1 else 's'))
    items = result.get('items')
    if (items is None and result.get('kind') == 'security-recommendation'
            and result.get('recommendation_id') == 'review_windows_update'
            and isinstance(result.get('result'), dict)):
        items = result['result'].get('items')
    if isinstance(items, list) and len(items) <= 1000 and not manual:
        rows = [item for item in items if isinstance(item, dict)]
        counts = {state: sum(item.get('status') == state for item in rows)
            for state in ('completed', 'failed', 'skipped', 'deferred', 'uncertain', 'cancelled')}
        verified = sum(isinstance(item.get('result'), dict)
            and item['result'].get('verified') is True and item.get('status') == 'completed' for item in rows)
        if counts['completed']:
            parts.append(f"{counts['completed']} completed update" + ('' if counts['completed'] == 1 else 's'))
            if verified_installs is None:
                parts.append(f'{verified} explicitly verified')
        for state in ('failed', 'skipped', 'deferred', 'uncertain', 'cancelled'):
            if counts[state]:
                parts.append(f'{counts[state]} {state}')
    if result.get('verified') is True or result.get('completion_verified') is True:
        parts.append('Outcome verified')
    elif result.get('verified') is False or result.get('completion_verified') is False:
        parts.append('Outcome not verified')
    if result.get('reboot_required') is True or result.get('rebootRequired') is True:
        parts.append('Restart required; no automatic restart requested')
    deferred = result.get('deferred_ids')
    if isinstance(deferred, list) and 0 < len(deferred) <= 1000:
        parts.append(f'{len(deferred)} remaining update' + ('' if len(deferred) == 1 else 's') + ' deferred')
    # Prefer the specific refusal/error to a generic service summary.
    for key in ('reason', 'error', 'note', 'summary'):
        detail = _text(result.get(key))
        if detail:
            parts.append(detail)
            break
    return ' · '.join(parts)[:1200]
