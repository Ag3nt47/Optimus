"""Driver cards distinguish discovered offers from explicit install receipts."""
import copy

import pytest

from optimus.parallel_updates import (_journal_result, ProgressMux,
                                     update_completion_state, update_completion_status)
from test_native_worker_cards import app, fonts, progress


def installed(identity='fixture-driver', **changes):
    receipt = dict(resultCode=2, hresult=0, aggregateResultCode=2, aggregateHresult=0,
                   installer_started=True, outcomeKnown=True, running=False)
    receipt.update(changes)
    return {'binding': {'id': identity, 'revision': 1}, 'status': 'applied', 'result': receipt}


def automatic(*, items=None, offers=5, **changes):
    value = {'policy': 'install_matching', 'review_only': False, 'failed_items': 0,
        'items': items or [], 'driver_scan': {'complete': True, 'status': 'completed',
            'offers_count': offers, 'summary': f'Windows Update search completed: {offers} applicable driver offers.'}}
    value.update(changes)
    return value


def test_counts_proven_installs_separately_from_discovery_and_deferral(progress):
    outcome = automatic(items=[installed(str(index)) for index in range(3)],
                        deferred=[{'id': 'four'}, {'id': 'five'}])
    original = copy.deepcopy(outcome)
    state = update_completion_state(outcome)
    assert state['scan_label'] == '3 installed / 5 found'
    assert '3 installed; 0 failed; 0 unverified; 0 skipped; 2 deferred; 0 not processed' in state['description']
    assert state['offers_count'] == 5 and state['scan_complete']
    assert state['total'] == 0  # Final receipt label replaces stale per-installer progress.
    mux = ProgressMux(progress.update_progress)
    mux.background('installing', {'current': 1, 'total': 5}, job_id='driver_updates')
    mux.background(update_completion_status(outcome), state, job_id='driver_updates')
    card = progress.worker_cards['driver_updates']
    assert card.status.text() == 'Deferred'
    assert card.progress.format() == '3 installed / 5 found'
    assert '2 deferred' in card.details.toolTip()
    assert outcome == original


def test_all_explicit_successes_complete_but_reboot_remains_visible():
    outcome = automatic(items=[installed()], offers=1)
    assert update_completion_status(outcome) == 'completed'
    assert update_completion_state(outcome)['scan_label'] == '1 installed / 1 found'
    outcome['items'][0]['result']['restartNeeded'] = True
    assert update_completion_status(outcome) == 'needs_review'
    assert 'requires a restart' in update_completion_state(outcome)['description']


@pytest.mark.parametrize(('field', 'value'), [
    ('resultCode', None), ('resultCode', True), ('resultCode', '2'), ('resultCode', 3),
    ('hresult', None), ('hresult', False), ('hresult', 1),
    ('aggregateResultCode', None), ('aggregateResultCode', True), ('aggregateResultCode', 3),
    ('aggregateHresult', None), ('aggregateHresult', False), ('aggregateHresult', 1),
    ('outcomeKnown', None), ('outcomeKnown', False), ('outcomeKnown', 1),
    ('installer_started', None), ('installer_started', False), ('installer_started', 1),
    ('running', None), ('running', True), ('running', 0), ('requiresReview', True),
])
def test_success_label_needs_strict_successful_native_and_lifecycle_receipt(field, value):
    row = installed(**{field: value})
    outcome = automatic(items=[row], offers=1)
    state = update_completion_state(outcome)
    assert state['scan_label'] == '0 installed / 1 found'
    assert state['driver_counts']['installed'] == 0
    assert state['driver_counts']['unverified'] == 1
    assert update_completion_status(outcome) == 'needs_review'


@pytest.mark.parametrize('missing', ['resultCode', 'hresult', 'aggregateResultCode', 'aggregateHresult',
                                    'installer_started', 'outcomeKnown', 'running'])
def test_missing_success_fields_never_default_to_installed(missing):
    row = installed()
    del row['result'][missing]
    state = update_completion_state(automatic(items=[row], offers=1))
    assert state['scan_label'] == '0 installed / 1 found'
    assert state['driver_counts']['unverified'] == 1


def test_failed_quiet_mode_surfaces_native_reason_and_never_counts_install(progress):
    note = 'Windows Update could not complete the approved driver in quiet mode. Review this update in Windows Update.'
    row = installed(resultCode=4, hresult=-2145116156, aggregateResultCode=4,
                    aggregateHresult=-2145116156, requiresReview=True,
                    failureKind='quiet_mode_unavailable', note=note)
    row['status'] = 'failed'
    outcome = automatic(items=[row], failed_items=1, deferred=[{'id': str(index)} for index in range(4)])
    state = update_completion_state(outcome)
    assert state['scan_label'] == '0 installed / 5 found'
    assert '1 failed' in state['description'] and '4 deferred' in state['description']
    assert note in state['description']
    progress.update_progress({'stage': 'Continue maintenance', 'percentage': 75,
        'jobs': [dict(state, id='driver_updates', status=update_completion_status(outcome), elapsed_seconds=46)]})
    card = progress.worker_cards['driver_updates']
    assert card.status.text() == 'Needs review'
    assert card.progress.format() == '0 installed / 5 found'
    assert note in card.details.toolTip() and card.elapsed.text() == 'Elapsed: 46s'


@pytest.mark.parametrize('reason', [
    'Administrator access is required; no elevation or driver action was started.',
    'System-drive installation space is insufficient or unverified; driver updates were deferred.',
    'Driver backup could not be verified; no driver installer was started.',
])
def test_reason_only_deferrals_do_not_hide_behind_successful_scan(reason):
    outcome = automatic(skipped=True, reason=reason, installer_started=False,
                        outcomeKnown=True, deferred=[{'id': str(index)} for index in range(5)])
    state = update_completion_state(outcome)
    assert state['description'].startswith(reason)
    assert '0 installed' in state['description'] and '5 deferred' in state['description']
    assert update_completion_status(outcome) == 'deferred'


def test_backup_failure_is_a_failed_prerequisite_not_a_processed_driver():
    outcome = automatic(failed_items=1, installer_started=False, outcomeKnown=True, items=[{
        'binding': {'stage': 'driver-backup'}, 'status': 'failed', 'result': {'exit_code': 1},
        'installer_started': False, 'outcomeKnown': True,
        'error': 'A successful third-party driver backup is required before installation.'}])
    state = update_completion_state(outcome)
    assert state['driver_counts']['failed'] == 0
    assert len(state['prerequisite_findings']) == 1
    assert state['driver_counts']['not_processed'] == 5
    assert 'successful third-party driver backup' in state['description']
    assert update_completion_status(outcome) == 'needs_review'


def test_unknown_outcome_preserves_uncertainty_and_concrete_failure():
    outcome = automatic(failed_items=1, items=[{'binding': {'id': 'one'}, 'status': 'failed',
        'uncertain': True, 'error': 'Native fixture timed out and may still be running.'}],
        deferred=[{'id': str(index)} for index in range(4)])
    state = update_completion_state(outcome)
    assert state['driver_counts']['unverified'] == 1 and state['driver_counts']['failed'] == 0
    assert 'may still be running' in state['description']
    assert state['scan_label'] == '0 installed / 5 found'
    assert update_completion_status(outcome) == 'needs_review'


def test_review_and_prior_barrier_keep_scan_labels():
    review = automatic(policy='review', review_only=True)
    assert update_completion_state(review)['scan_label'] == '5 driver updates found'
    blocked = automatic(installation_blocked=True, blocked_by_prior_installer=True,
        installation_block_reason='Previous installer needs review.')
    state = update_completion_state(blocked)
    assert state['scan_label'] == '5 driver updates found' and state['installation_blocked']


def test_unprocessed_or_skipped_offers_do_not_claim_all_updated():
    outcome = automatic(items=[installed()], offers=5)
    assert update_completion_state(outcome)['driver_counts']['not_processed'] == 4
    assert update_completion_status(outcome) == 'deferred'
    outcome = automatic(items=[{'binding': {'id': 'one'}, 'status': 'skipped',
        'reason': 'The offered driver predates the saved date floor.'}], offers=1)
    assert 'predates the saved date floor' in update_completion_state(outcome)['description']
    assert update_completion_status(outcome) == 'deferred'


def test_compact_journal_preserves_native_com_result_and_reason():
    outcome = automatic(items=[installed()], reason='A concrete driver-stage reason.')
    saved = _journal_result(outcome)
    assert saved['reason'] == outcome['reason']
    for key in ('resultCode', 'hresult', 'aggregateResultCode', 'aggregateHresult',
                'installer_started', 'outcomeKnown', 'running'):
        assert saved['items'][0]['result'][key] == outcome['items'][0]['result'][key]
