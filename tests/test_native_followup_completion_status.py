"""Observed follow-up checks are separate from installations and prerequisites."""
import copy

import pytest

from optimus.parallel_updates import update_completion_state, update_completion_status


def unapproved_driver(*, rows=True):
    return dict(policy='install_matching', review_only=False, failed_items=0,
        cancelled=False, installer_started=False, outcomeKnown=True,
        items=[dict(binding={'id': 'fixture-phone-driver', 'revision': 1}, status='skipped',
            reason='No exact saved hardware/provider/class rule approves this offered driver.')] if rows else [],
        driver_scan=dict(complete=True, status='completed', offers_count=1,
            eligible_count=0, devices_checked=229, inventory_complete=True))


@pytest.mark.parametrize('rows', [True, False])
def test_checked_unapproved_driver_is_not_an_installer_wait(rows):
    result = unapproved_driver(rows=rows)
    original = copy.deepcopy(result)
    state = update_completion_state(result)
    assert update_completion_status(result) == 'completed'
    assert state['approval_required'] is True
    assert state['presentation_status'] == 'finished_with_skips'
    assert state['worker_finished'] is True and state['continuation_blocked'] is False
    assert state['driver_counts']['not_approved'] == 1
    assert state['driver_counts']['installed'] == state['driver_counts']['skipped'] == 0
    assert state['driver_counts']['not_processed'] == 0
    assert state['scan_label'] == '0 approved matches / 1 found'
    assert 'no driver installer started' in state['description']
    assert result == original


@pytest.mark.parametrize('change', [
    lambda r: r.update(outcomeKnown=False),
    lambda r: r.update(installer_started=True),
    lambda r: r.update(running=True),
    lambda r: r.update(failed_items=1),
    lambda r: r.update(cancelled=True),
    lambda r: r.update(uncertain=True),
    lambda r: r.update(skipped=True),
    lambda r: r.update(navigation_cancelled_items=1),
    lambda r: r.update(deferred_ids=['another-driver']),
    lambda r: r.update(stop_reason='A prerequisite needs review.'),
    lambda r: r.update(installation_blocked=True),
    lambda r: r.update(blocked_by_prior_installer=True),
    lambda r: r.update(status='deferred'),
    lambda r: r.update(status='failed'),
    lambda r: r.update(deferred=[{'id': 'another-driver'}]),
    lambda r: r['driver_scan'].update(eligible_count=False),
    lambda r: r['driver_scan'].update(eligible_count=1),
    lambda r: r['driver_scan'].update(complete=False, status='incomplete'),
    lambda r: r['driver_scan'].update(status='failed'),
    lambda r: r['driver_scan'].update(status='incomplete'),
    lambda r: r['items'][0].update(status='cancelled'),
    lambda r: r['items'][0].update(uncertain=True),
    lambda r: r['items'][0].update(result={'running': True}),
    lambda r: r['items'][0].update(result={'installer_started': True}),
    lambda r: r['items'][0].update(result={'outcomeKnown': False}),
    lambda r: r['items'][0].update(result=['invalid receipt']),
])
def test_unknown_blocked_or_eligible_driver_does_not_get_no_approval_completion(change):
    result = unapproved_driver()
    change(result)
    state = update_completion_state(result)
    assert state.get('approval_required') is not True
    assert update_completion_status(result) != 'completed'
    assert state['driver_counts']['installed'] == 0 if 'driver_counts' in state else True


def apps(row):
    return dict(items=[row], failed_items=1, installer_started=True,
        outcomeKnown=True, running=False, app_scan=dict(complete=True, status='completed',
            offers_count=1, recognized_offers_count=1, source_reported_offers_count=1))


def failed_app(**changes):
    receipt = dict(status='failed', exitCode=0x8A150006, installer_started=True,
        outcomeKnown=True, running=False, verified=False)
    receipt.update(changes)
    return dict(id='Fixture.Python', status='failed', result=receipt)


def test_known_native_installer_failure_is_failed_not_unknown_installation():
    result = apps(failed_app())
    original = copy.deepcopy(result)
    counts = update_completion_state(result)['app_counts']
    assert counts['failed'] == 1 and counts['unverified'] == counts['updated'] == 0
    assert update_completion_status(result) == 'needs_review'
    assert result == original


@pytest.mark.parametrize('changes', [dict(outcomeKnown=False), dict(running=True),
    dict(uncertain=True), dict(installer_started=None), dict(exitCode=0),
    dict(exitCode=True), dict(restartNeeded=True), dict(failure_scope='shared')])
def test_unproven_native_failure_remains_unverified(changes):
    counts = update_completion_state(apps(failed_app(**changes)))['app_counts']
    assert counts['failed'] == counts['updated'] == 0
    assert counts['unverified'] == 1


def test_explicit_no_launch_skip_is_not_an_unknown_installation():
    row = dict(id='Fixture.Held', status='skipped', result=dict(status='skipped',
        verified=False, installer_started=False, outcomeKnown=True, running=False,
        failureKind='failed_in_parent_run', requiresReview=True, failure_scope='package'))
    counts = update_completion_state(apps(row))['app_counts']
    assert counts['skipped'] == 1 and counts['unverified'] == counts['updated'] == counts['failed'] == 0


def test_zero_exit_without_changed_version_is_still_unverified():
    row = failed_app(exitCode=0, installation_reported=True)
    row['status'] = row['result']['status'] = 'needs_review'
    counts = update_completion_state(apps(row))['app_counts']
    assert counts['updated'] == counts['failed'] == 0 and counts['unverified'] == 1


@pytest.mark.parametrize('outer', [dict(installer_started=True), dict(outcomeKnown=False),
    dict(running=True), dict(uncertain=True), dict(uncertain='true'), dict(running=0),
    dict(installer_started=None)])
def test_outer_lifecycle_cannot_contradict_a_no_launch_skip(outer):
    row = dict(id='Fixture.Held', status='skipped', result=dict(status='skipped',
        verified=False, installer_started=False, outcomeKnown=True, running=False,
        failure_scope='package', failureKind='failed_in_parent_run', requiresReview=True), **outer)
    state = update_completion_state(apps(row))
    assert state['app_counts']['skipped'] == state['app_counts']['updated'] == 0
    assert state['app_counts']['unverified'] == 1
    assert state['worker_finished'] is False
