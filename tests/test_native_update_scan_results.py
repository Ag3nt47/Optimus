"""Discovery outcomes survive the worker/journal boundary without fake totals."""
import copy
import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from optimus.parallel_updates import (ParallelAppUpdates, ProgressMux, WINDOWS_DRIVER_REVIEW,
                                     WINDOWS_SOFTWARE, update_completion_state, update_completion_status,
                                     installer_barrier, _uncertain)


@pytest.mark.parametrize('title,key,noun', [
    (WINDOWS_DRIVER_REVIEW, 'driver_scan', 'driver'),
    (WINDOWS_SOFTWARE, 'app_scan', 'app'),
])
@pytest.mark.parametrize('complete,offers,expected_status', [
    (True, 0, 'completed'), (True, 3, 'completed'), (False, None, 'needs_review'),
])
def test_read_only_discovery_retains_evidence_and_distinguishes_zero_from_unknown(
        tmp_path, monkeypatch, title, key, noun, complete, offers, expected_status):
    monkeypatch.setattr('optimus.speed_enhancements._current_owner', lambda: 'fixture-owner')
    monkeypatch.setattr('optimus.runtime_priority.lower_worker_priority', lambda: lambda: None)
    summary = (f'Fixture source returned {offers} applicable updates.' if complete else
               'Fixture source failed; availability is unknown.')
    evidence = {'schema': 1, 'complete': complete, 'status': 'completed' if complete else 'failed',
                'source': 'Fixture catalog', 'offers_count': offers, 'eligible_count': 0,
                'summary': summary, 'errors': [] if complete else [{'source': 'Fixture catalog', 'error': 'Offline'}]}
    outcome = {'review_only': True, 'failed_items': 0 if complete else 1, key: evidence,
               'scan': {'updates': [], 'coverage': {'devices': ['private device detail']}}}
    original = copy.deepcopy(outcome)
    operation = Mock(return_value=outcome)
    backend = SimpleNamespace(data_dir=tmp_path, system='win32', bridge=SimpleNamespace(_process=None))
    events = []
    job = ParallelAppUpdates(backend, 'a' * 32, title, operation, ProgressMux(events.append))
    job.start(inline=True)
    job._run()
    stage = job.join()
    operation.assert_called_once()
    assert stage['result'] == original and outcome == original
    card = next(row for row in events[-1]['jobs'] if row['id'] == job.job_id)
    assert card['status'] == expected_status
    assert card['description'].startswith(summary)
    if not complete:
        assert 'Offline' in card['description']
    assert card['scan_label'] == (f'{offers} {noun} updates found' if complete else 'Availability not confirmed')
    assert card['current'] == card['total'] == 0  # Discovery is not an installer count.
    saved = json.loads(job.path.read_text())
    assert saved['result'][key] == evidence
    assert saved['installer_prepared'] is False
    assert 'scan' not in saved['result']
    assert 'private device detail' not in job.path.read_text()


def test_terminal_scan_summary_does_not_replace_the_concrete_install_refusal():
    state = update_completion_state({'items': [], 'stop_reason': 'A restart is required before installation.',
        'app_scan': {'complete': True, 'offers_count': 4,
                     'summary': 'WinGet found 4 offered updates; none were installed.'}})
    assert state['description'].startswith('A restart is required before installation.')
    assert '4 offered updates' in state['description']
    assert state['scan_label'] == '4 app updates found'


def test_unstarted_scan_cannot_present_zero_updates():
    state = update_completion_state({'driver_scan': {'complete': False, 'status': 'not_started',
        'offers_count': None, 'summary': 'Driver discovery was not started.'}})
    assert state['scan_label'] == 'Scan not started'
    assert '0 driver' not in state['description']


def test_successful_discovery_with_an_installation_refusal_remains_deferred():
    outcome = {'skipped': True, 'installer_started': False, 'outcomeKnown': True,
        'stop_reason': 'Administrator access is required before installing.',
        'deferred': [{'id': 'Fixture.Driver'}],
        'driver_scan': {'complete': True, 'status': 'completed', 'offers_count': 1,
                        'summary': 'Windows Update offered 1 applicable driver.'}}
    assert update_completion_status(outcome) == 'deferred'
    assert update_completion_state(outcome)['scan_label'] == '1 driver update found'


@pytest.mark.parametrize('key,title', [('driver_scan', WINDOWS_DRIVER_REVIEW), ('app_scan', WINDOWS_SOFTWARE)])
def test_discovery_exception_preserves_failed_source_proof_without_claiming_a_live_installer(
        tmp_path, monkeypatch, key, title):
    monkeypatch.setattr('optimus.speed_enhancements._current_owner', lambda: 'fixture-owner')
    monkeypatch.setattr('optimus.runtime_priority.lower_worker_priority', lambda: lambda: None)
    error = RuntimeError('Fixture discovery unavailable')
    setattr(error, key, {'schema': 1, 'complete': False, 'status': 'failed', 'offers_count': None,
                        'summary': 'The fixture source could not be searched.'})
    backend = SimpleNamespace(data_dir=tmp_path, system='win32', bridge=SimpleNamespace(_process=None))
    events = []
    job = ParallelAppUpdates(backend, 'b' * 32, title, Mock(side_effect=error), ProgressMux(events.append))
    job.start(inline=True)
    job._run()
    job.join()
    saved = json.loads(job.path.read_text())['result']
    assert saved[key]['status'] == 'failed' and saved[key]['offers_count'] is None
    assert saved['installer_started'] is False and saved['outcomeKnown'] is True
    card = events[-1]['jobs'][0]
    assert card['status'] == 'needs_review'
    assert card['scan_label'] == 'Availability not confirmed'


@pytest.mark.parametrize('key', ['app_scan', 'driver_scan'])
def test_failed_search_proof_cannot_block_an_independent_installer_after_a_known_preflight_failure(key):
    outcome = {'failed_items': 1, 'installer_started': False, 'outcomeKnown': True,
        'running': False, 'uncertain': False, 'items': [],
        key: {'schema': 1, 'complete': False, 'status': 'failed', 'offers_count': None,
              'summary': 'Fixture source timeout; availability is uncertain.',
              'errors': [{'source': 'Fixture source', 'error': 'Fixture catalog error'}]}}
    assert installer_barrier(outcome) is None
    assert _uncertain(outcome) is False
    assert update_completion_status(outcome) == 'needs_review'
    # Actual unresolved installer evidence still blocks the sibling channel.
    outcome['items'] = [{'status': 'failed', 'result': {'installer_started': True, 'outcomeKnown': False}}]
    assert installer_barrier(outcome)
    assert _uncertain(outcome) is True
