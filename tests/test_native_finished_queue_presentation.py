"""Finished queue presentation is distinct from catalog coverage and barriers."""
import copy
import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from optimus.parallel_updates import (ParallelAppUpdates, ProgressMux, WINDOWS_SOFTWARE,
    installer_barrier, update_completion_state, update_completion_status)
from optimus.update_holds import held_update_result
from test_native_worker_cards import app, fonts, progress


def held_queue(*, incomplete=True):
    package = {'id': 'Microsoft.VisualStudioCode', 'version': '1.0', 'available': '2.0'}
    hold = {'reason': 'Fixture exact-version catalog failure; publisher review required.', 'available_version': '2.0'}
    return {'items': [{'id': package['id'], 'status': 'skipped', 'result': held_update_result(package, hold)}],
        'held_count': 1, 'held_ids': [package['id']],
        'automatic_update_holds': [dict(hold, id=package['id'])], 'failed_items': 2 if incomplete else 1,
        'deferred_ids': [], 'stop_reason': '', 'installer_started': False, 'outcomeKnown': True, 'running': False,
        'app_scan': {'schema': 1, 'complete': not incomplete, 'status': 'incomplete' if incomplete else 'completed',
            'source_reported_offers_count': 3 if incomplete else 1, 'recognized_offers_count': 1,
            'offers_count': None if incomplete else 1, 'summary': 'Fixture source search incomplete; availability remains unknown.'
                if incomplete else 'Fixture source offered one update.',
            'errors': [{'source': 'Fixture catalog', 'error': 'Two additional source offers were unrecognized.'}] if incomplete else []}}


def test_actual_compact_incomplete_held_shape_finishes_only_the_known_queue_without_resolving_hold():
    result = held_queue(); before = copy.deepcopy(result)
    state = update_completion_state(result)
    assert update_completion_status(result) == 'needs_review' and installer_barrier(result) is None
    assert state['presentation_status'] == 'finished_with_skips'
    assert state['worker_finished'] is True and state['continuation_blocked'] is False
    assert state['scan_complete'] is False and state['scan_status'] == 'incomplete'
    assert state['scan_label'] == '1 recognized; incomplete'
    assert state['app_counts'] == dict(updated=0, skipped=1, failed=0, unverified=0, held=1, source_unverified=2)
    assert '2 additional source offers unverified' in state['description']
    assert 'availability remains unknown' in state['description']
    assert 'Microsoft.VisualStudioCode' in state['description']
    assert state['current'] == state['total'] == 1
    assert result == before


@pytest.mark.parametrize('changes', [
    {'cancelled': True}, {'navigation_cancelled_items': 1}, {'restartNeeded': True},
    {'deferred_ids': ['Fixture.Next']}, {'deferred': [{'id': 'Fixture.Next'}]},
    {'stop_reason': 'Shared service needs review.'}, {'installation_blocked': True},
    {'running': True}, {'outcomeKnown': False}, {'shared_barrier': True},
])
def test_pending_shared_cancelled_or_unknown_result_never_uses_finished_presentation(changes):
    result = dict(held_queue(), **changes)
    assert update_completion_state(result).get('presentation_status') != 'finished_with_skips'


def test_unknown_item_or_presence_receipt_never_claims_finished():
    result = held_queue()
    result['items'][0]['result'].pop('running')
    assert not update_completion_state(result)['worker_finished']
    result = held_queue()
    result['app_presence'] = {'items': [{'id': 'Fixture.Other', 'status': 'completed', 'result': {}}]}
    assert not update_completion_state(result)['worker_finished']


def test_preflight_rows_are_in_terminal_total_and_counts_are_not_install_successes():
    result = held_queue(incomplete=False)
    result['items'].append({'id': 'Fixture.Updated', 'status': 'completed', 'result': {
        'installer_started': True, 'outcomeKnown': True, 'running': False, 'exitCode': 0}})
    state = update_completion_state(result)
    assert state['current'] == state['total'] == 2
    assert state['app_counts']['updated'] == state['app_counts']['skipped'] == 1
    assert '1 updated; 1 skipped; 0 failed' in state['description']
    events = []; mux = ProgressMux(events.append)
    mux.background('installing', {'current': 0, 'total': 1, 'description': 'One non-held installer selected.'})
    mux.background(update_completion_status(result), state)
    terminal = events[-1]['jobs'][0]
    assert terminal['current'] == terminal['total'] == 2
    assert terminal['presentation_status'] == 'finished_with_skips'


def test_waiter_names_active_driver_and_held_app_is_amber_with_incomplete_scan_visible(progress):
    events = []; mux = ProgressMux(events.append)
    result = held_queue()
    mux.background(update_completion_status(result), update_completion_state(result))
    mux.background('installing', {'description': 'Installing Rivet fixture driver', 'current': 1, 'total': 5}, job_id='driver_updates')
    mux.wait_for_updates('Optimize eligible internal drives')
    event = events[-1]
    assert event['active_job_id'] == event['waiting_for'] == 'driver_updates'
    assert 'Waiting for driver updates: Installing Rivet' in event['description']
    assert 'Waiting for driver updates: Installing Rivet' in event['maintenance_wait_reason']
    progress.update_progress(event)
    card = progress.worker_cards['software_updates']
    assert card.status.text() == 'Finished with skips; scan incomplete'
    card.set_live_metrics({})
    assert card.activity_ring.health == 'waiting' and not card.activity_ring.active
    assert '1 recognized; incomplete' in card.progress.format()
    assert '0 updated; 1 skipped; 0 failed' in card.details.text()
    assert progress.title.text() == 'WAITING FOR DRIVER UPDATES'
    assert progress.worker_cards['maintenance'].status.text() == 'Waiting for driver updates'
    assert 'Rivet' in progress.description.text() and 'Rivet' in progress.worker_cards['maintenance'].details.text()
    assert 'App updates: finished with skips; scan incomplete' in progress.background_status.text()
    assert 'App updates: needs review' not in progress.background_status.text()
    progress.request_close()
    assert 'current driver update must return' in progress.description.text()


def test_failed_search_or_missing_terminal_receipt_stays_red_review(progress):
    result = held_queue()
    result['items'][0]['result']['outcomeKnown'] = False
    state = update_completion_state(result)
    progress.update_progress({'stage': 'Maintenance continues', 'jobs': [dict(state, id='software_updates', status='needs_review')]})
    card = progress.worker_cards['software_updates']; card.set_live_metrics({})
    assert card.status.text() == 'Needs review' and card.activity_ring.health == 'error'


def test_presentation_does_not_change_durable_audit_status_or_held_evidence(tmp_path, monkeypatch):
    monkeypatch.setattr('optimus.speed_enhancements._current_owner', lambda: 'fixture-owner')
    monkeypatch.setattr('optimus.runtime_priority.lower_worker_priority', lambda: lambda: None)
    result = held_queue()
    backend = SimpleNamespace(data_dir=tmp_path, system='win32', bridge=SimpleNamespace(_process=None))
    events = []; job = ParallelAppUpdates(backend, 'd' * 32, WINDOWS_SOFTWARE, Mock(return_value=result), ProgressMux(events.append))
    job.start(inline=True); job._run(); job.join()
    saved = json.loads(job.path.read_text())
    assert saved['status'] == 'needs_review'
    assert saved['result']['held_count'] == 1 and saved['result']['app_scan']['complete'] is False
    assert 'presentation_status' not in saved['result']
    assert events[-1]['jobs'][0]['presentation_status'] == 'finished_with_skips'
