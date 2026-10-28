"""Known preinstaller refusals remain distinct from uncertain installations."""
import copy
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from optimus.parallel_updates import (
    _journal_result, installer_barrier, ParallelAppUpdates, ProgressMux, WINDOWS_SOFTWARE,
    update_completion_state, update_completion_status,
)
from optimus.update_holds import held_update_result


def low_space_queue():
    package = {'id': 'Fixture.Held', 'version': '1.0', 'available': '2.0'}
    hold = held_update_result(package, {'available_version': '2.0', 'reason': 'Publisher artifact needs review.'})
    reason = 'C: updates deferred: 0.22 GiB free; free 1,911,652,352 more bytes (~1.78 GiB) to reach the 2 GiB installation floor.'
    disk = {'ready': False, 'free_bytes': 235831296, 'minimum_free_bytes': 2147483648,
            'shortfall_bytes': 1911652352, 'system_drive': 'C:\\', 'reason': reason}
    return {'failed_items': 3, 'installer_started': False, 'outcomeKnown': True,
        'items': [{'id': package['id'], 'status': 'skipped', 'result': hold},
            {'id': 'Fixture.First', 'status': 'skipped', 'result': {
                'status': 'deferred', 'skipped': True, 'requiresReview': True, 'exitCode': 0,
                'installer_started': False, 'outcomeKnown': True, 'running': False,
                'shared_barrier': True, 'note': reason, 'disk_readiness': disk}}],
        'deferred_ids': ['Fixture.Later'], 'stop_reason': 'Update deferred: ' + reason,
        'app_scan': {'complete': False, 'status': 'incomplete', 'recognized_offers_count': 3,
            'source_reported_offers_count': 3, 'summary': 'Source coverage remains incomplete.'}}


def test_low_space_refusal_is_visible_and_not_an_unknown_installer_after_journaling():
    result = low_space_queue()
    original = copy.deepcopy(result)
    saved = _journal_result(result)
    for outcome in (result, saved):
        state = update_completion_state(outcome)
        assert state['app_counts']['deferred'] == 2
        assert state['app_counts']['skipped'] == state['app_counts']['held'] == 1
        assert state['app_counts']['failed'] == state['app_counts']['unverified'] == 0
        assert state['description'].startswith(result['stop_reason'])
        assert 'No application installer started.' in state['description']
        assert state['current'] == 2 and state['total'] == 3
        assert state['worker_finished'] is False
        assert state.get('presentation_status') != 'finished_with_skips'
        assert update_completion_status(outcome) == 'needs_review'
        # This is presentation only: the low-space shared gate must survive.
        assert installer_barrier(outcome) == result['items'][1]['result']['note']
        assert outcome['items'][1]['result']['shared_barrier'] is True
        assert outcome['failed_items'] == 3
    assert saved['items'][1]['result']['disk_readiness'] == result['items'][1]['result']['disk_readiness']
    assert result == original
    events = []
    mux = ProgressMux(events.append)
    mux.background(update_completion_status(saved), update_completion_state(saved))
    assert events[-1]['jobs'][0]['app_counts']['deferred'] == 2


def test_low_space_stage_preserves_failed_audit_with_a_concrete_preinstaller_explanation(tmp_path, monkeypatch):
    monkeypatch.setattr('optimus.speed_enhancements._current_owner', lambda: 'fixture-owner')
    monkeypatch.setattr('optimus.runtime_priority.lower_worker_priority', lambda: lambda: None)
    backend = SimpleNamespace(data_dir=tmp_path, system='win32', bridge=SimpleNamespace(_process=None))
    job = ParallelAppUpdates(backend, 'e' * 32, WINDOWS_SOFTWARE,
                             Mock(return_value=low_space_queue()), ProgressMux(lambda event: None))
    job.start(inline=True)
    job._run()
    stage = job.join()
    assert stage['status'] == 'failed' and stage['result']['failed_items'] == 3
    assert 'no application installer started' in stage['error']
    assert '1,911,652,352 more bytes' in stage['error']


@pytest.mark.parametrize('changes', [
    {'outcomeKnown': False}, {'running': True}, {'installer_started': True}, {'uncertain': True},
])
def test_deferred_label_cannot_hide_unknown_or_started_installer(changes):
    result = low_space_queue()
    result['items'][1]['result'].update(changes)
    state = update_completion_state(result)
    assert state['app_counts']['unverified'] == 1
    assert state['app_counts'].get('deferred') == 1  # Only the unattempted remaining app.
    assert state['worker_finished'] is False
    assert 'No application installer started.' not in state['description']


@pytest.mark.parametrize('changes', [
    {'installer_started': True}, {'outcomeKnown': False}, {'running': True},
])
def test_no_installer_caption_requires_consistent_application_presence_receipts(changes):
    result = low_space_queue()
    result['app_presence'] = dict(changes, items=[])
    assert 'No application installer started.' not in update_completion_state(result)['description']


def test_driver_offers_without_eligible_saved_approvals_are_not_unprocessed_installs():
    result = {'policy': 'install_matching', 'review_only': False, 'failed_items': 0,
        'items': [], 'installer_started': False, 'outcomeKnown': True,
        'driver_scan': {'complete': True, 'status': 'completed', 'offers_count': 1,
            'eligible_count': 0, 'summary': 'One applicable driver offer; zero eligible under saved approvals.'}}
    original = copy.deepcopy(result)
    for outcome in (result, _journal_result(result)):
        state = update_completion_state(outcome)
        assert state['scan_label'] == '0 approved matches / 1 found'
        assert state['driver_counts']['not_approved'] == 1
        assert state['driver_counts']['not_processed'] == state['driver_counts']['installed'] == 0
        assert state['description'].startswith('No driver offers match the saved approvals; no driver installer started.')
        assert update_completion_status(outcome) == 'deferred'
    assert result == original
    result['driver_scan'].pop('eligible_count')
    assert update_completion_state(result)['driver_counts']['not_processed'] == 1
    assert 'not_approved' not in update_completion_state(result)['driver_counts']
