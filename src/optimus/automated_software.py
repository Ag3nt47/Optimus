"""Fresh, scoped WinGet updates; no installer starts without saved permission."""
from __future__ import annotations

import copy
import re
import time
from typing import Any

from .item_progress import BoundItemProgress, accepts_progress_callback, normalize_item_progress
from .update_scan_evidence import app_scan_evidence, attach_app_scan_error, with_app_scan
from .update_holds import load_update_holds, matching_update_hold, held_update_result, is_known_update_hold
from .navigation import WorkItem, cursor_for, navigation_skip, merge_presence, local_skip, can_fail_forward, shared_update_refusal, update_exception_receipt

_PACKAGE_ID = re.compile(r'^[A-Za-z0-9][A-Za-z0-9_.+\-]{1,127}$')
_VERSION = re.compile(r'[vV]?[0-9][0-9A-Za-z._+~\-]{0,127}')
_ORDERED_VERSION = re.compile(r'[vV]?([0-9]+(?:[._][0-9]+)*)(?:[-_.]?(dev|alpha|a|beta|b|rc|pre|preview)(?:[-_.]?([0-9]+))?)?(?:\+[0-9A-Za-z.\-]+)?', re.I)
_NUMERIC_RELEASE = re.compile(r'[0-9]+(?:\.[0-9]+){1,7}')
_UPPER_VERSION = re.compile(r'< ?([0-9]+(?:\.[0-9]+){1,7})')
_UNITY_PACKAGE = re.compile(r'unity\.unity\.([1-9][0-9]{3})', re.I)
_UNITY_RELEASE = re.compile(r'([1-9][0-9]{3})\.(0|[1-9][0-9]{0,3})\.(0|[1-9][0-9]{0,3})f([1-9][0-9]?)')


def valid_update_version(value, *, installed=False):
    """Recognize bounded scan text, without assigning arbitrary labels an order."""
    return (isinstance(value, str) and len(value) <= 128
            and not value.endswith('.') and '..' not in value
            and (_VERSION.fullmatch(value) is not None
                 or installed and _UPPER_VERSION.fullmatch(value) is not None))


def _ordered_version(value):
    if (not isinstance(value, str) or len(value) > 128 or not _VERSION.fullmatch(value)
            or value.endswith('.') or '..' in value):
        return None
    match = _ORDERED_VERSION.fullmatch(value)
    if not match:
        return None
    core = tuple(int(part) for part in re.split('[._]', match[1]))
    while len(core) > 1 and core[-1] == 0:
        core = core[:-1]
    stage = (match[2] or '').casefold()
    stage = {'alpha': 'a', 'beta': 'b', 'pre': 'preview'}.get(stage, stage)
    return core, stage, int(match[3] or 0)


def same_release_version(left, right):
    """Recognized release equivalence; opaque vendor labels are never guessed."""
    before, after = _ordered_version(left), _ordered_version(right)
    return before is not None and after is not None and before == after


def is_newer_version(installed, available, *, package_id=None, source=None):
    """Require provable forward ordering, rather than different version strings.

    Numeric release components, optional v, common prerelease labels and build
    metadata are understood. Vendor-specific incomparable labels stay review-only.
    Build metadata and trailing zero components do not establish a newer release.
    Exact WinGet identities additionally support stable Unity Editor releases and
    numeric installed upper bounds. Neither is generalized to opaque versions.
    """
    if source == 'winget' and isinstance(package_id, str) and _PACKAGE_ID.fullmatch(package_id):
        # Unity's documented MAJOR.MINOR.RRzNN ordering, restricted to normal
        # public (f) builds within the exact package's major release family.
        # https://docs.unity.com/en-us/engine/6000.0/manual/programming-environment/script-compilation/assembly-definition-files/includes
        unity = _UNITY_PACKAGE.fullmatch(package_id)
        if unity:
            before = _UNITY_RELEASE.fullmatch(installed) if isinstance(installed, str) else None
            after = _UNITY_RELEASE.fullmatch(available) if isinstance(available, str) else None
            return bool(before and after and before[1] == after[1] == unity[1]
                        and tuple(map(int, after.groups())) > tuple(map(int, before.groups())))
        # WinGet's ARP mapping can report a strict installed upper bound, '< '.
        # Require an exact final numeric offer strictly ABOVE that bound (even
        # equality remains review-only). Never strip >, <=, unknown or labels.
        # https://github.com/microsoft/winget-cli/blob/master/src/AppInstallerRepositoryCore/CompositeSource.cpp
        upper = _UPPER_VERSION.fullmatch(installed) if isinstance(installed, str) and len(installed) <= 128 else None
        if upper:
            if not isinstance(available, str) or len(available) > 128 or not _NUMERIC_RELEASE.fullmatch(available):
                return False
            return is_newer_version(upper[1], available)
    before, after = _ordered_version(installed), _ordered_version(available)
    if before is None or after is None:
        return False
    left, right = before[0], after[0]
    length = max(len(left), len(right))
    left, right = left + (0,) * (length - len(left)), right + (0,) * (length - len(right))
    if left != right:
        return right > left
    if before[1] == after[1]:
        return bool(before[1]) and after[2] > before[2]
    if not after[1]:
        return True  # A final release follows its recognized prerelease.
    if not before[1]:
        return False
    ranks = {'dev': 0, 'a': 1, 'b': 2, 'rc': 3}
    return before[1] in ranks and after[1] in ranks and ranks[after[1]] > ranks[before[1]]


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
        if not valid_update_version(version, installed=True) or not valid_update_version(available):
            continue
        if not is_newer_version(version, available, package_id=identity, source='winget') or item.get('pinned') is True:
            continue
        candidates[identity] = dict(item)
    return {identity: item for identity, item in candidates.items() if identity.casefold() not in repeated}


def run_software(backend: Any, configuration: dict[str, Any], *, cancel_event=None, item_callback=None,
                 prepared_callback=None, before_install=None, updates_only=False) -> dict[str, Any]:
    """Discover now and optionally block before the first native updater call.

    The owner retains its job-wide exclusive installer gate until this returns.
    Preparing and waiting do not establish that any installer has started.
    """
    notified = False
    def prepared(**values):
        nonlocal notified
        if notified:
            return
        notified = True
        state = {'operation': 'software', 'scan_complete': False, 'candidate_count': 0, 'review_only': True}
        state.update(values)
        if prepared_callback is not None:
            prepared_callback(state)
    context = {}
    try:
        try:
            from .software_app_rules import apply_update_rules
            from .app_presence import run_app_presence
            from .parallel_updates import installer_barrier
            source_configuration = configuration
            configuration = apply_update_rules(configuration)
            navigation = cursor_for(backend, 'software_updates', 'App updates')
            presence = None
            if (not updates_only and configuration.get('software_updates_enabled') is True
                    and configuration.get('software_app_rules')):
                presence = run_app_presence(backend, configuration, cancel_event=cancel_event,
                    item_callback=item_callback, before_install=before_install, navigation=navigation)
                context['app_presence'] = presence
                if installer_barrier(presence):
                    return with_app_scan(merge_presence({'items': [], 'failed_items': 0,
                        'review_only': True, 'skipped': True, 'installer_started': False,
                        'outcomeKnown': True, 'running': False,
                        'stop_reason': 'Application presence needs review before updates can continue.'}, presence),
                        not_started='Application presence stopped this software worker before update discovery.')
            outcome = _run_software(backend, configuration, cancel_event=cancel_event, item_callback=item_callback,
                                 prepared=prepared, before_install=before_install, scan_context=context,
                                 phased=prepared_callback is not None or before_install is not None,
                                 navigation=navigation, scope_reader=lambda: apply_update_rules(source_configuration))
            outcome = merge_presence(outcome, presence)
            context['outcome'] = outcome
            return outcome
        finally:
            prepared()
    except Exception as error:
        if isinstance(context.get('app_presence'), dict):
            error.app_presence = context['app_presence']
        attach_app_scan_error(error, context)
        raise


def _run_software(backend: Any, configuration: dict[str, Any], *, cancel_event, item_callback,
                  prepared, before_install, phased, scan_context, navigation=None, scope_reader=None) -> dict[str, Any]:
    """Apply only a current scan's exact IDs under the saved selected/all scope.

    The native updater checks pins again before each installation. No generic
    upgrade-all, include-unknown, pin override or reboot flag is used. The
    separate saved compatibility choice may add the non-security force flag.
    """
    cancelled = lambda: cancel_event is not None and cancel_event.is_set()
    started = time.time()
    if configuration.get('software_updates_enabled') is not True:
        return with_app_scan({'review_only': True, 'skipped': True, 'items': [], 'failed_items': 0,
                'note': 'Software Updater is off; no scan or installer was started.'},
                not_started='Software Updater is off; no update search was started.')
    if cancelled():
        return with_app_scan({'review_only': True, 'cancelled': True, 'items': [], 'failed_items': 0},
                             not_started='Cancelled before the app update search started.')
    discovery = WorkItem(navigation, 'software:discovery', 'Search for available app updates', external=cancel_event)
    try:
        cancellable_scan = getattr(backend, 'updates_scan_cancellable', None)
        scan = cancellable_scan(discovery.cancel_event) if callable(cancellable_scan) else backend.updates_scan()
    except Exception as error:
        discovery.finish('failed', message=str(error))
        error.app_scan = app_scan_evidence(started=started, error=error)
        raise
    skipped_discovery = discovery.requested()
    discovery.finish('cancelled' if skipped_discovery else 'completed', mutation_started=False)
    if cancelled():
        return with_app_scan({'items': [], 'failed_items': 0, 'review_only': True, 'cancelled': True,
            'installer_started': False, 'outcomeKnown': True, 'running': False},
            not_started='Cancelled during the app update search; no installer was started.')
    if skipped_discovery:
        return with_app_scan({'items': [], 'failed_items': 0, 'review_only': True,
            'skipped': True, 'navigation_cancelled': True, 'navigation_cancelled_items': 1,
            'installer_started': False, 'outcomeKnown': True, 'running': False,
            'note': 'The app discovery item was skipped; no app installer was started.'},
            not_started='The completed app search was discarded at your request; no installer ran.')
    if not isinstance(scan, dict):
        error = RuntimeError('The software update scan returned invalid metadata.')
        error.app_scan = app_scan_evidence(started=started, error=error)
        raise error
    scan_context.update(scan=scan, started=started)
    code_ok = type(scan.get('exitCode')) is int and scan['exitCode'] == 0
    scan_items = scan.get('updates', scan.get('packages'))
    untrusted = (not code_ok or scan.get('structured') is not True or scan.get('source', 'winget') != 'winget'
                 or not isinstance(scan_items, list) or len(scan_items) > 1000)
    partial_scan = untrusted or bool(scan.get('excluded'))
    policy = configuration['software_update_policy']
    if policy == 'review':
        prepared(scan_complete=not untrusted)
        return with_app_scan({'review_only': True, 'scan': scan, 'failed_items': int(partial_scan),
                'installer_started': False, 'outcomeKnown': True, 'running': False,
                'note': 'Software update review only; no installer was started. Failed, incomplete or unrecognized scans require review.'},
                started=started, selected=[])
    if configuration.get('software_update_agreements_accepted') is not True:
        raise ValueError('Unattended software updates require saved agreement acceptance.')
    if policy == 'all_supported' and configuration.get('software_update_all_approved') is not True:
        raise ValueError('All-supported software updates require separate saved scope approval.')
    if policy not in ('selected', 'all_supported'):
        raise ValueError('Unknown software-update policy.')
    candidates = fresh_candidates(scan)
    canonical_ids = {identity.casefold(): identity for identity in candidates}
    exclusions = {identity.casefold() for identity in configuration.get('software_update_exclude_ids', [])}
    selected = ([identity for identity in candidates if identity.casefold() not in exclusions]
                if policy == 'all_supported' else [canonical_ids[identity.casefold()] for identity in configuration['software_update_ids']
                if identity.casefold() in canonical_ids and identity.casefold() not in exclusions])
    scan_context['selected'] = list(selected)
    approved_scope = copy.deepcopy({key: configuration.get(key) for key in ('software_updates_enabled',
        'software_update_policy', 'software_update_ids', 'software_update_all_approved',
        'software_update_exclude_ids', 'software_update_agreements_accepted', 'update_compatibility_mode_enabled', 'software_app_rules')})
    approved_ids = list(selected)
    def current_scope():
        current = scope_reader() if scope_reader is not None else configuration
        return {key: current.get(key) for key in approved_scope}
    preflight_items = []
    # Exact bad offers remain visible in item results but do not acquire the
    # installer lane or keep unrelated approved applications from proceeding.
    # Maintenance re-reads the file immediately before every actual launch.
    data_dir = getattr(backend, 'data_dir', None)
    holds = load_update_holds(data_dir) if data_dir is not None else []
    rechecks = {}
    catalog_blocked = ''
    recheck = getattr(backend, 'recheck_held_updates', None)
    if callable(recheck) and not cancelled():
        held_packages = [candidates[identity] for identity in selected
                         if matching_update_hold(candidates[identity], holds)]
        try:
            rechecks = recheck(held_packages, cancel_event=cancel_event, item_callback=item_callback)
            if not isinstance(rechecks, dict):
                raise ValueError('Unrecognized catalog recheck evidence.')
        except Exception:
            # A crashed refresh can leave shared source state uncertain. These
            # read-only checks never authorize an installer or remove a hold.
            rechecks = {'needs_fresh_scan': True, 'source_verified_after_refresh': False,
                        'error': 'Automatic publisher metadata checks did not establish catalog readiness.'}
        if rechecks.get('needs_fresh_scan') is True:
            if rechecks.get('source_verified_after_refresh') is not True:
                catalog_blocked = 'The application source needs a fresh identity/trust verification after its metadata refresh; no application installer was started.'
            elif not cancelled():
                try:
                    refreshed = cancellable_scan(cancel_event) if callable(cancellable_scan) else backend.updates_scan()
                    if (not isinstance(refreshed, dict) or refreshed.get('structured') is not True
                            or type(refreshed.get('exitCode')) is not int or refreshed['exitCode'] != 0
                            or refreshed.get('source', 'winget') != 'winget'
                            or not isinstance(refreshed.get('updates', refreshed.get('packages')), list)
                            or len(refreshed.get('updates', refreshed.get('packages'))) > 1000):
                        raise ValueError('The fresh catalog scan is not trusted.')
                    # Preparation still applies the saved selected/all scope.
                    # A new valid offer can supersede an old exact-version hold;
                    # the hold itself is never removed, widened or bypassed.
                    scan = refreshed
                    candidates = fresh_candidates(scan)
                    canonical_ids = {identity.casefold(): identity for identity in candidates}
                    selected = ([identity for identity in candidates if identity.casefold() not in exclusions]
                                if policy == 'all_supported' else [canonical_ids[identity.casefold()]
                                for identity in configuration['software_update_ids']
                                if identity.casefold() in canonical_ids and identity.casefold() not in exclusions])
                    approved_ids = list(selected)
                    holds = load_update_holds(data_dir) if data_dir is not None else []
                    scan_context.update(scan=scan, selected=list(selected))
                    partial_scan = bool(scan.get('excluded'))
                except Exception:
                    catalog_blocked = 'The fresh application source scan failed after metadata refresh; no application installer was started.'
    recheck_items = rechecks.get('items') if isinstance(rechecks.get('items'), list) else []
    allowed = []
    for identity in selected:
        current = candidates[identity]
        hold = matching_update_hold(current, holds)
        if hold:
            evidence = next((row for row in recheck_items if isinstance(row, dict)
                             and row.get('id') == identity and row.get('available_version') == current['available']), None)
            preflight_items.append({'id': identity, 'name': current.get('name', identity),
                'from_version': current['version'], 'to_version': current['available'], 'status': 'skipped',
                'result': held_update_result(current, hold, recheck=evidence)})
        else:
            allowed.append(identity)
    selected = allowed
    prepared(scan_complete=not untrusted, candidate_count=len(selected), review_only=False)
    def hold_details(items):
        rows = [{'id': item['id'], 'available_version': item.get('to_version'),
                 'reason': item['result']['note']}
                for item in items if is_known_update_hold(item.get('result'))]
        return {'held_ids': [row['id'] for row in rows], 'held_count': len(rows),
                'automatic_update_holds': rows,
                **({'automatic_update_rechecks': rechecks} if rechecks else {})}
    def defer(reason, *, failed=0, refreshed_scan=None, error=None):
        return with_app_scan({'review_only': False, 'scan': scan if refreshed_scan is None else refreshed_scan,
                'policy': policy, 'items': preflight_items,
                'failed_items': failed + len(preflight_items) + int(partial_scan), 'skipped': True,
                'deferred_ids': list(selected), 'cancelled': cancelled(), 'stop_reason': reason,
                'installer_started': False, 'outcomeKnown': True, **hold_details(preflight_items)},
                started=started, selected=approved_ids, error=error)
    if catalog_blocked:
        return dict(defer(catalog_blocked), installation_blocked=True,
                    installation_block_reason=catalog_blocked, running=False)
    if selected and not cancelled() and before_install is not None:
        checkpoint = WorkItem(navigation, 'software:checkpoint', 'Wait for the application installer checkpoint', external=cancel_event, kind='check')
        permitted = before_install({'operation': 'software', 'scan_complete': not untrusted,
                           'candidate_count': len(selected), 'review_only': False}) is True
        skipped_checkpoint = checkpoint.requested()
        checkpoint.finish('cancelled' if skipped_checkpoint else 'completed', mutation_started=False)
        if skipped_checkpoint:
            return dict(defer('Application installation was skipped at your request.'),
                        navigation_cancelled=True, navigation_cancelled_items=1, cancelled=cancelled())
        if not permitted:
            return defer('The maintenance or exclusive installer gate did not permit application updates.')
    if selected and phased:
        if cancelled():
            return defer('Cancelled before the native application updater was started.')
        if current_scope() != approved_scope:
            return defer('Saved software approvals changed while waiting; review the current scope before installing.')
        # The native updater rejects stale discovery caches. Refresh only after
        # the gate, and retain the exact initially prepared version pairs rather
        # than enrolling a new application or release partway through the batch.
        revalidation = WorkItem(navigation, 'software:revalidation', 'Recheck approved app versions before installation', external=cancel_event)
        try:
            refreshed_scan = cancellable_scan(revalidation.cancel_event) if callable(cancellable_scan) else backend.updates_scan()
        except Exception as error:
            revalidation.finish('failed', message=str(error))
            return defer('Software revalidation failed before any installer started (' + type(error).__name__ + ').', failed=1, error=error)
        skipped_revalidation = revalidation.requested()
        revalidation.finish('cancelled' if skipped_revalidation else 'completed', mutation_started=False)
        if skipped_revalidation:
            return dict(defer('Application revalidation was skipped at your request.'),
                        navigation_cancelled=True, navigation_cancelled_items=1, cancelled=cancelled())
        if cancelled():
            return defer('Cancelled while revalidating application updates.', refreshed_scan=refreshed_scan)
        refreshed = fresh_candidates(refreshed_scan)
        if (not isinstance(refreshed_scan, dict) or refreshed_scan.get('structured') is not True
                or type(refreshed_scan.get('exitCode')) is not int or refreshed_scan['exitCode'] != 0
                or refreshed_scan.get('source', 'winget') != 'winget'
                or not isinstance(refreshed_scan.get('updates', refreshed_scan.get('packages')), list)
                or len(refreshed_scan.get('updates', refreshed_scan.get('packages'))) > 1000):
            return defer('The fresh software source or candidate inventory could not be verified before installation.', failed=1, refreshed_scan=refreshed_scan)
        unchanged = []
        for identity in selected:
            expected, current = candidates[identity], refreshed.get(identity)
            if current is None or any(current.get(field) != expected.get(field)
                                      for field in ('id', 'version', 'available', 'source')):
                preflight_items.append({'id': identity, 'name': expected.get('name', identity),
                    'from_version': expected['version'], 'to_version': expected['available'], 'status': 'skipped',
                    'result': {'skipped': True, 'failureKind': 'version_changed', 'installer_started': False,
                               'outcomeKnown': True, 'note': 'The prepared package identity or version pair changed while waiting; no installer was started.'}})
            else:
                unchanged.append(identity)
        selected = unchanged
        candidates = {identity: refreshed[identity] for identity in selected}
        scan = refreshed_scan
        scan_context['scan'] = scan
        partial_scan = partial_scan or bool(scan.get('excluded'))
    results, deferred = preflight_items, []
    scan_context['items'] = results
    restart_needed = False
    interrupted = False
    stop_reason = ''
    for index, identity in enumerate(selected):
        if cancelled():
            deferred = selected[index:]
            break
        if phased and current_scope() != approved_scope:
            deferred = selected[index:]
            stop_reason = 'Saved software approvals changed; remaining applications require a fresh review.'
            break
        current = candidates[identity]
        work = WorkItem(navigation, 'software:update:' + identity, 'Update ' + identity,
                        external=cancel_event, kind='mutation')
        progress_key = 'software:update:' + identity
        progress_state = {'current': index, 'total': len(selected), 'item': index + 1,
            'items': len(selected), 'id': identity,
            'description': f'Updating approved package {index + 1}/{len(selected)}: {identity} ({current["version"]} to {current["available"]})',
            'log': 'Updating approved exact ID: ' + identity}
        if item_callback:
            item_callback(dict(progress_state, item_progress=normalize_item_progress({
                'key': progress_key, 'label': identity, 'phase': 'checking', 'percent': None})))
        # Progress handlers can deliver cancellation before a mutation begins.
        if cancelled():
            work.finish('cancelled', mutation_started=False)
            deferred = selected[index:]
            break
        if phased and current_scope() != approved_scope:
            work.finish('skipped', mutation_started=False)
            deferred = selected[index:]
            stop_reason = 'Saved software approvals changed; remaining applications require a fresh review.'
            break
        reporter = BoundItemProgress(item_callback, progress_key, identity, progress_state)
        try:
            options = {'cancel_event': work.cancel_event} if work.cancel_event is not None else {}
            if callable(item_callback) and accepts_progress_callback(backend.update_app_unattended):
                options['progress_callback'] = reporter
            if configuration.get('update_compatibility_mode_enabled') is True:
                options['compatibility_mode'] = True
            if work.requested():
                outcome = navigation_skip()
            else:
                work.installing()
                outcome = backend.update_app_unattended(identity, **options)
                if (work.requested() and isinstance(outcome, dict) and outcome.get('cancelled') is True
                        and outcome.get('installer_started') is False and outcome.get('outcomeKnown') is True
                        and outcome.get('running') is False):
                    outcome = dict(outcome, navigation_cancelled=True)
            known_exit = isinstance(outcome, dict) and type(outcome.get('exitCode')) is int
            if shared_update_refusal(outcome):
                outcome = dict(outcome, shared_barrier=True)
            skipped = isinstance(outcome, dict) and outcome.get('skipped') is True
            successful = (known_exit and outcome['exitCode'] in (0, 3010, 1641)
                          and outcome.get('success', True) is not False and outcome.get('verified') is not False)
            results.append({'id': identity, 'name': current.get('name', identity),
                            'from_version': outcome.get('from_version', current['version']) if isinstance(outcome, dict) else current['version'],
                            'to_version': outcome.get('to_version', current['available']) if isinstance(outcome, dict) else current['available'],
                            'status': 'cancelled' if isinstance(outcome, dict) and outcome.get('cancelled') is True else 'skipped' if skipped else 'needs_review' if known_exit and outcome['exitCode'] == 0 and outcome.get('verified') is False else 'completed' if successful else 'failed', 'result': outcome})
            if results[-1]['status'] == 'failed' and isinstance(outcome, dict):
                diagnostic = outcome.get('stderr') or outcome.get('text') or outcome.get('note') or ''
                results[-1]['error'] = ('App updater exit code ' + str(outcome.get('exitCode')) + '. '
                    + ' '.join(str(diagnostic).split())[-1400:])[:1800]
            work.finish(results[-1]['status'], result=results[-1], mutation_started=outcome.get('installer_started', successful) if isinstance(outcome, dict) else None,
                        outcome_known=isinstance(outcome, dict) and outcome.get('outcomeKnown') is not False and outcome.get('running') is not True,
                        message=outcome.get('note') if isinstance(outcome, dict) else None)
            if local_skip(outcome):
                continue
            if isinstance(outcome, dict) and outcome.get('cancelled') is True:
                interrupted = True
                stop_reason = 'Cancelled before this installer started; remaining applications were deferred.'
                deferred = selected[index + 1:]
                break
            if is_known_update_hold(outcome):
                # This refusal proves that no installer started. A hold for a
                # single exact offer is not a global unknown-installer barrier.
                continue
            if isinstance(outcome, dict):
                restart_needed = (outcome.get('restartNeeded') is True or
                                  known_exit and outcome['exitCode'] in (3010, 1641))
                uncertain = (outcome.get('running') is True or outcome.get('outcomeKnown') is False
                             or any(marker in str(outcome.get('status', '')).casefold()
                                    for marker in ('timed out', 'timeout', 'still running')))
            else:
                uncertain = True
            if isinstance(outcome, dict) and outcome.get('requiresReview') is True:
                if can_fail_forward(outcome):
                    continue
                stop_reason = 'The update safety checks need review before remaining applications can be updated.'
                if (outcome.get('installer_started') is False and outcome.get('outcomeKnown') is True
                        and outcome.get('running') is False and isinstance(outcome.get('note'), str)
                        and outcome['note'].strip()):
                    stop_reason = 'Update deferred: ' + ' '.join(outcome['note'].split())[:800]
                deferred = selected[index + 1:]
                break
            if shared_update_refusal(outcome):
                stop_reason = shared_update_refusal(outcome)
                deferred = selected[index + 1:]
                break
            if restart_needed or uncertain or (not known_exit and not skipped):
                stop_reason = ('A restart is required before any remaining update.' if restart_needed else
                               'The installer outcome is uncertain; verify its state before continuing.')
                deferred = selected[index + 1:]
                break
        except Exception as error:
            evidence = update_exception_receipt(error, default_scope='app_batch')
            known_idle = evidence['outcomeKnown'] is True and evidence['running'] is False
            preflight = known_idle and evidence['installer_started'] is False
            results.append({'id': identity, 'status': 'failed', 'error': str(error), 'result': evidence})
            if preflight and shared_update_refusal(dict(evidence, requiresReview=True)):
                evidence['shared_barrier'] = True
            work.finish('failed', result=results[-1], mutation_started=evidence['installer_started'], outcome_known=known_idle, message=str(error))
            if can_fail_forward(evidence):
                continue
            # Known pre-install refusals must not create an orphan-installer
            # record. Unclassified exceptions still cannot prove child exit.
            deferred = selected[index + 1:]
            stop_reason = ('Update preflight failed; no installer was started for this application. Remaining packages need a fresh review.' if preflight else
                           'The updater raised an error; verify installer state before continuing.')
            break
        finally:
            reporter.close()
    needs_review = sum(item.get('result', {}).get('requiresReview') is True for item in results if isinstance(item.get('result'), dict))
    lifecycle = {}
    evidence = [item.get('result') for item in results]
    if not evidence:
        lifecycle = {'installer_started': False, 'outcomeKnown': True}
    elif all(isinstance(value, dict) and type(value.get('outcomeKnown')) is bool for value in evidence):
        lifecycle['outcomeKnown'] = all(value['outcomeKnown'] for value in evidence)
        if all(type(value.get('installer_started')) is bool for value in evidence):
            lifecycle['installer_started'] = any(value['installer_started'] for value in evidence)
    return with_app_scan({'review_only': not bool(selected), 'scan': scan, 'policy': policy, 'items': results,
            'failed_items': sum(item['status'] == 'failed' or isinstance(item.get('result'), dict)
                                and item['result'].get('requiresReview') is True for item in results) + int(partial_scan),
            'skipped_items': sum(item['status'] == 'skipped' for item in results), 'requires_review_items': needs_review,
            'navigation_cancelled_items': sum(isinstance(item.get('result'), dict) and item['result'].get('navigation_cancelled') is True for item in results),
            'cancelled': cancelled() or interrupted, 'deferred_ids': deferred, 'restartNeeded': restart_needed,
            'stop_reason': stop_reason,
            'excluded_ids': [identity for identity in candidates if identity.casefold() in exclusions],
            'not_available_ids': [identity for identity in configuration.get('software_update_ids', []) if identity.casefold() not in canonical_ids],
            'compatibility_mode': configuration.get('update_compatibility_mode_enabled') is True,
            **hold_details(results),
            **lifecycle,
            'note': ('Only complete identities with a provably newer version from a successful fresh winget-source scan may be updated. Equal, older or incomparable versions are skipped. Saved exclusions and native pin checks remain in effect. '
                     + ('The saved compatibility choice adds WinGet --force for non-security issues; it cannot bypass an installer hard block. ' if configuration.get('update_compatibility_mode_enabled') is True else 'No force option is requested. ')
                     + 'Hash/signature checks remain enabled; no pin override, unknown-version update, blind retry or automatic restart is requested. Unsupported applications require their publisher updater.'
                     + (' Held offers: ' + '; '.join(row['reason'] for row in hold_details(results)['automatic_update_holds'])
                        if hold_details(results)['held_count'] else ''))},
                     started=started, selected=approved_ids)
