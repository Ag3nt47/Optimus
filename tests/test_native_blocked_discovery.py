"""Read-only search evidence remains visible when an old installer blocks changes."""
import copy

import pytest

from test_native_worker_cards import app, fonts, progress
from optimus.parallel_updates import ProgressMux, update_completion_state, update_completion_status


JOURNAL = r'D:\FixtureUser\Optimus\Data\parallel-updates\aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa.json'
REASON = ('A previous update installer has an unresolved outcome; no automatic retry was started. '
          'Review installer state and the saved record before continuing: ' + JOURNAL)


def blocked_job(identity, offers, **changes):
    noun = 'driver' if identity == 'driver_updates' else 'app'
    state = {'id': identity, 'status': 'needs_review', 'description': 'Read-only catalog search succeeded.',
        'installation_blocked': True, 'blocked_by_prior_installer': True,
        'installation_block_reason': REASON, 'wait_reason': 'Search completed; installation remains blocked.',
        'scan_complete': True, 'scan_status': 'completed', 'offers_count': offers,
        'scan_label': f'{offers} {noun} updates found', 'current': 0, 'total': 0, 'elapsed_seconds': 46}
    state.update(changes)
    return state


@pytest.mark.parametrize('identity', ['software_updates', 'driver_updates'])
def test_completed_search_keeps_real_offer_count_and_prior_install_barrier(progress, identity):
    state = {'stage': 'Continue independent maintenance', 'percentage': 50,
             'jobs': [blocked_job(identity, 5)]}
    original = copy.deepcopy(state)
    progress.update_progress(state)
    card = progress.worker_cards[identity]
    assert card.status.text() == 'Updates found; install blocked'
    assert card.progress.format() == ('5 driver updates found' if identity == 'driver_updates' else '5 app updates found')
    assert card.elapsed.text() == 'Elapsed: 46s' and not card._elapsed_active
    assert card.progress.value() == 0 and card.progress.maximum() == 1
    assert 'Previous installer needs review.' in card.details.visible_text
    assert JOURNAL in card.details.toolTip() and JOURNAL in card.details.accessibleDescription()
    assert 'Search completed' in card.details.text()
    assert progress.log.toPlainText().count(JOURNAL) == 1
    assert progress.background_status.toolTip().count(JOURNAL) == 1
    assert state == original


def test_zero_offers_is_a_completed_search_with_installation_still_blocked(progress):
    progress.update_progress({'stage': 'Continue maintenance', 'percentage': 50,
        'jobs': [blocked_job('driver_updates', 0)]})
    card = progress.worker_cards['driver_updates']
    assert card.status.text() == 'Search complete; install blocked'
    assert card.progress.format() == '0 driver updates found'
    assert card.elapsed.text() == 'Elapsed: 46s'
    assert 'Updates found;' not in card.status.text()


@pytest.mark.parametrize('scan_status', ['failed', 'incomplete'])
def test_failed_or_partial_search_never_becomes_a_zero_offer_success(progress, scan_status):
    progress.update_progress({'stage': 'Continue maintenance', 'percentage': 50,
        'jobs': [blocked_job('software_updates', None, scan_complete=False, scan_status=scan_status,
            description='Catalog search could not confirm availability.', scan_label='Availability not confirmed')]})
    card = progress.worker_cards['software_updates']
    assert card.status.text() == 'Search needs review; install blocked'
    assert card.progress.format() == 'Availability not confirmed'
    assert '0 app updates' not in card.progress.format()
    assert JOURNAL in card.details.toolTip() and card.elapsed.text() == 'Elapsed: 46s'


def test_explicit_not_started_search_has_no_fake_zero_second_duration(progress):
    progress.update_progress({'stage': 'Continue maintenance', 'percentage': 50,
        'jobs': [blocked_job('software_updates', None, scan_complete=False, scan_status='not_started',
            scan_label='Scan not started', elapsed_seconds=0)]})
    card = progress.worker_cards['software_updates']
    assert card.status.text() == 'Search not started; install blocked'
    assert card.progress.format() == 'Search not started' and card.elapsed.text() == 'Elapsed: —'
    assert not card._elapsed_active and JOURNAL in card.details.toolTip()


def test_searching_worker_does_not_claim_discovery_has_finished(progress):
    progress.update_progress({'stage': 'Continue maintenance', 'percentage': 50,
        'jobs': [blocked_job('driver_updates', None, status='scanning', scan_complete=False,
            scan_status='running', scan_label='', description='Querying the driver catalog.', elapsed_seconds=3)]})
    card = progress.worker_cards['driver_updates']
    assert card.status.text() == 'Scanning' and card._elapsed_active
    assert card.progress.format() == 'Scanning; total unknown'
    assert card.elapsed.text() == 'Elapsed: 3s'


def test_blocked_offer_counts_are_distinct_from_installer_counts(progress):
    progress.update_progress({'stage': 'Continue maintenance', 'percentage': 50,
        'jobs': [blocked_job('software_updates', 8, current=1, total=6)]})
    card = progress.worker_cards['software_updates']
    assert card.progress.format() == '8 app updates found'
    assert 'processed' not in card.progress.text() and card.progress.maximum() == 1


def test_long_blocked_status_reflows_without_overlapping_the_count(progress, app):
    progress.resize(640, 480)
    progress.update_progress({'stage': 'Continue maintenance', 'percentage': 50,
        'jobs': [blocked_job('software_updates', None, scan_complete=False, scan_status='incomplete',
            scan_label='3 recognized; incomplete'), blocked_job('driver_updates', 5)]})
    app.processEvents()
    for identity in ('software_updates', 'driver_updates'):
        card = progress.worker_cards[identity]
        assert card.status.wordWrap() and card.status.geometry().bottom() < card.details.geometry().top()
        assert card.details.geometry().bottom() < card.progress.geometry().top()
        assert card.progress.geometry().bottom() < card.elapsed.geometry().top()
    assert len({card.width() for card in progress.worker_cards.values()}) <= 2
    assert progress.rect().contains(progress.cancel_button.geometry())


@pytest.mark.parametrize(('identity', 'evidence_key'), [('software_updates', 'app_scan'), ('driver_updates', 'driver_scan')])
def test_real_completion_and_mux_keep_scanned_count_and_block_reason_in_card(progress, monkeypatch, identity, evidence_key):
    now = [100.0]
    monkeypatch.setattr('optimus.parallel_updates.time.monotonic', lambda: now[0])
    mux = ProgressMux(progress.update_progress)
    mux({'stage': 'Continue independent maintenance', 'percentage': 50})
    mux.background('scanning', {'description': 'Querying fixture update catalog.'}, job_id=identity)
    now[0] = 146.0
    outcome = {'installation_blocked': True, 'blocked_by_prior_installer': True,
        'installation_block_reason': REASON, 'stop_reason': REASON, 'items': [],
        evidence_key: {'complete': True, 'status': 'completed', 'offers_count': 5,
            'summary': 'Fixture catalog search completed; five updates were offered.'}}
    original = copy.deepcopy(outcome)
    mux.background(update_completion_status(outcome), update_completion_state(outcome), job_id=identity)
    card = progress.worker_cards[identity]
    assert card.status.text() == 'Updates found; install blocked'
    assert card.progress.format() == ('5 app updates found' if identity == 'software_updates' else '5 driver updates found')
    assert card.elapsed.text() == 'Elapsed: 46s' and JOURNAL in card.details.toolTip()
    now[0] = 200.0
    mux.heartbeat()
    assert card.elapsed.text() == 'Elapsed: 46s' and not card._elapsed_active
    progress.finish({'status': 'completed_with_errors', 'stages': []})
    assert card.status.text() == 'Updates found; install blocked'
    assert outcome == original


def test_one_held_offer_keeps_actual_reason_visible_after_other_app_completes(progress):
    reason = 'Automatic update held: source offered 2.0 but the verified installer payload is 1.9.'
    outcome = {'held_count': 1, 'held_ids': ['Fixture.Held'], 'failed_items': 1,
        'automatic_update_holds': [{'id': 'Fixture.Held', 'available_version': '2.0', 'reason': reason}],
        'app_scan': {'complete': True, 'status': 'completed', 'offers_count': 2,
            'summary': 'Fixture app catalog: 2 offered updates found; 1 installer success and 1 held offer.'},
        'items': [{'id': 'Fixture.Held', 'status': 'skipped',
            'result': {'failureKind': 'automatic_update_hold', 'note': reason,
                'installer_started': False, 'outcomeKnown': True, 'requiresReview': True}},
            {'id': 'Fixture.Other', 'status': 'completed', 'result': {'verified': True, 'outcomeKnown': True}}]}
    original = copy.deepcopy(outcome)
    mux = ProgressMux(progress.update_progress)
    mux({'stage': 'Continue maintenance', 'percentage': 50})
    mux.background('scanning', {'description': 'Querying fixture app catalog.', 'current': 0, 'total': 2})
    mux.background(update_completion_status(outcome), update_completion_state(outcome))
    card = progress.worker_cards['software_updates']
    assert card.status.text() == 'Needs review'
    assert reason in card.details.toolTip() and reason in card.details.accessibleDescription()
    progress.finish({'status': 'completed_with_errors',
        'stages': [{'title': 'Review or update configured software', 'status': 'completed', 'result': outcome}]})
    assert reason in progress.log.toPlainText()
    assert card.status.text() == 'Needs review' and 'install blocked' not in card.status.text()
    assert outcome == original
