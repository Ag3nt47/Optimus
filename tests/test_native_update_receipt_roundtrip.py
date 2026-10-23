"""Saved update outcomes retain offer counts and prerequisite/hold findings."""
import copy

from optimus.parallel_updates import (
    _journal_result, installer_barrier, ProgressMux,
    update_completion_state, update_completion_status,
)
from optimus.update_holds import held_update_result


def blocked_inbox_pair():
    rows = [{'binding': {'id': identity}, 'status': 'skipped',
        'reason': 'No verified rollback backup is available.',
        'result': {'installer_started': False, 'outcomeKnown': True, 'running': False}}
        for identity in ('fixture.monitor', 'fixture.watchdog')]
    rows.append({'binding': {'stage': 'driver-backup'}, 'status': 'failed',
        'installer_started': False, 'outcomeKnown': True,
        'error': 'The backup prerequisite did not establish success.',
        'result': {'installer_started': False, 'outcomeKnown': True, 'running': False}})
    return {'policy': 'install_matching', 'review_only': False, 'failed_items': 1,
        'installer_started': False, 'outcomeKnown': True, 'items': rows,
        'driver_scan': {'complete': True, 'status': 'completed', 'offers_count': 2,
                        'summary': 'Two applicable offers were found.'}}


def test_two_skipped_offers_do_not_gain_a_third_failed_driver():
    result = blocked_inbox_pair()
    original = copy.deepcopy(result)
    state = update_completion_state(result)
    assert state['driver_counts'] == dict(installed=0, failed=0, unverified=0,
        skipped=2, deferred=0, not_processed=0)
    assert len(state['prerequisite_findings']) == 1
    assert state['prerequisite_findings'][0]['known_no_launch'] is True
    assert 'Prerequisite findings: 1; separate from driver results.' in state['description']
    assert state['scan_label'] == '0 installed / 2 found'
    assert update_completion_status(result) == 'needs_review'
    assert installer_barrier(result) is None
    assert result == original


def test_compact_driver_receipt_preserves_prerequisite_domain_and_lifecycle():
    result = blocked_inbox_pair()
    before = update_completion_state(result)
    saved = _journal_result(result)
    after = update_completion_state(saved)
    assert after['driver_counts'] == before['driver_counts']
    assert after['prerequisite_findings'] == before['prerequisite_findings']
    assert saved['items'][-1]['binding'] == {'stage': 'driver-backup'}
    assert saved['items'][-1]['installer_started'] is False
    assert saved['items'][-1]['outcomeKnown'] is True
    assert update_completion_status(saved) == update_completion_status(result)


def test_unknown_prerequisite_cannot_become_a_known_skip():
    result = blocked_inbox_pair()
    result.pop('outcomeKnown')
    result.pop('installer_started')
    row = result['items'][-1]
    row.pop('outcomeKnown')
    row.pop('installer_started')
    row.update(uncertain=True, result={'installer_started': True, 'outcomeKnown': False,
                                    'running': True})
    assert update_completion_state(result)['driver_counts']['unverified'] == 1
    assert update_completion_state(result)['prerequisite_findings'][0]['known_no_launch'] is False
    assert installer_barrier(result)
    assert update_completion_status(result) == 'needs_review'
    assert installer_barrier(_journal_result(result))


def test_compact_hold_receipt_retains_exact_known_hold_classification():
    package = {'id': 'Fixture.Editor', 'version': '1.0', 'available': '2.0'}
    hold = {'id': package['id'], 'available_version': '2.0',
            'reason': 'The offered artifact identifies as the previous release.'}
    result = {'failed_items': 1, 'installer_started': False, 'outcomeKnown': True,
        'items': [{'id': package['id'], 'status': 'skipped',
                   'result': held_update_result(package, hold)}],
        'held_count': 1, 'held_ids': [package['id']], 'automatic_update_holds': [hold],
        'app_scan': {'complete': False, 'status': 'incomplete',
            'recognized_offers_count': 1, 'source_reported_offers_count': 2}}
    original = copy.deepcopy(result)
    full = update_completion_state(result)
    compact = update_completion_state(_journal_result(result))
    assert full['app_counts']['held'] == 1
    assert compact['app_counts'] == full['app_counts']
    assert compact['presentation_status'] == full['presentation_status']
    assert compact['worker_finished'] is True
    assert compact['app_counts']['source_unverified'] == 1
    assert result == original


def test_mux_keeps_driver_counts_and_prerequisite_findings_separate():
    events = []
    mux = ProgressMux(events.append)
    state = update_completion_state(blocked_inbox_pair())
    mux.background('needs_review', state, job_id='driver_updates')
    job = events[-1]['jobs'][0]
    assert job['driver_counts'] == state['driver_counts']
    assert job['prerequisite_findings'] == state['prerequisite_findings']
    assert '0 failed' in job['description']


def test_driver_stage_failure_uses_driver_wording_without_running_a_backend():
    from optimus.parallel_updates import ParallelAppUpdates, WINDOWS_DRIVERS
    job = object.__new__(ParallelAppUpdates)
    job.joined = False
    job.thread = None
    job.title = WINDOWS_DRIVERS
    job.result = blocked_inbox_pair()
    job.record = {'started_at': 1, 'finished_at': 2}
    job.stage = {}
    job.mux = ProgressMux(lambda _: None)
    stage = job.join()
    assert stage['status'] == 'failed'
    assert 'driver update checks or prerequisites' in stage['error']
    assert 'application' not in stage['error']


def test_read_only_metadata_progress_does_not_record_an_installer_intent():
    from optimus.parallel_updates import ParallelAppUpdates, WINDOWS_SOFTWARE
    from types import SimpleNamespace
    events = []
    job = object.__new__(ParallelAppUpdates)
    job.title = WINDOWS_SOFTWARE
    job.concurrent_preparation = True
    job.record = {'installer_prepared': False}
    job._write = lambda: None
    job.mux = SimpleNamespace(background=lambda status, state: events.append((status, state)))
    for phase in ('source_refresh', 'metadata_recheck'):
        job.item_progress({'phase': phase, 'id': 'Fixture.Editor', 'current': 1, 'total': 5})
        assert job.record['installer_prepared'] is False
        assert events[-1][0] == 'scanning'
        assert events[-1][1]['total'] == 0
    job.item_progress({'id': 'Fixture.Editor', 'current': 1, 'total': 1})
    assert job.record['installer_prepared'] is True
    job.item_progress({'phase': 'metadata_recheck'})
    assert job.record['installer_prepared'] is True  # Never erase an earlier intent.


def test_automatic_publisher_wait_evidence_survives_compaction_without_authorizing():
    package = {'id': 'Fixture.Editor', 'version': '1.0', 'available': '2.0'}
    hold = {'id': package['id'], 'available_version': '2.0', 'reason': 'Artifact version mismatch.'}
    audit = {'id': package['id'], 'available_version': '2.0', 'status': 'awaiting_publisher',
        'automatic_recheck': True, 'authorization_granted': False, 'artifact_verified': False,
        'installer_started': False, 'outcomeKnown': True, 'running': False,
        'recheck_status': 'metadata_checked', 'recheck_schedule': 'next_approved_run_after_cooldown',
        'checked_at': 123, 'next_check_at': 21723, 'metadata_sha256': 'a' * 64,
        'note': 'Publisher replacement awaited; automatic checks continue.', 'unbounded_raw_text': 'x' * 5000}
    outcome = dict(held_update_result(package, hold), automatic_recheck=audit)
    result = {'failed_items': 0, 'installer_started': False, 'outcomeKnown': True,
        'items': [{'id': package['id'], 'status': 'skipped', 'result': outcome}],
        'held_count': 1, 'automatic_update_holds': [hold],
        'automatic_update_rechecks': {'items': [audit], 'authorization_granted': False,
            'needs_fresh_scan': True, 'source_verified_after_refresh': True},
        'app_scan': {'complete': True, 'status': 'completed', 'offers_count': 1}}
    saved = _journal_result(result)
    assert 'unbounded_raw_text' not in saved['items'][0]['result']['automatic_recheck']
    assert saved['automatic_update_rechecks']['authorization_granted'] is False
    assert saved['automatic_update_rechecks']['needs_fresh_scan'] is True
    for value in (result, saved):
        state = update_completion_state(value)
        assert state['automatic_recheck_count'] == 1
        assert state['app_counts']['held'] == 1
        assert 'automatic publisher rechecks enabled on later approved runs' in state['description']
        assert installer_barrier(value) is None
    saved['items'][0]['result']['automatic_recheck']['authorization_granted'] = True
    assert 'automatic_recheck_count' not in update_completion_state(saved)


def test_equivalent_offer_spelling_keeps_saved_hold_recheck_caption():
    package = {'id': 'Fixture.Editor', 'version': '1.0', 'available': 'v2.0.0'}
    hold = {'id': package['id'], 'available_version': '2.0', 'reason': 'Artifact version mismatch.'}
    audit = {'id': package['id'], 'available_version': 'v2.0.0', 'held_available_version': '2.0',
        'status': 'awaiting_publisher', 'automatic_recheck': True, 'authorization_granted': False,
        'artifact_verified': False, 'installer_started': False, 'outcomeKnown': True, 'running': False}
    result = {'failed_items': 0, 'installer_started': False, 'outcomeKnown': True,
        'items': [{'id': package['id'], 'status': 'skipped',
            'result': dict(held_update_result(package, hold), automatic_recheck=audit)}],
        'held_count': 1, 'automatic_update_holds': [hold],
        'app_scan': {'complete': True, 'status': 'completed', 'offers_count': 1}}
    for value in (result, _journal_result(result)):
        state = update_completion_state(value)
        assert state['automatic_recheck_count'] == 1
        assert 'automatic publisher rechecks enabled on later approved runs' in state['description']
        assert installer_barrier(value) is None
