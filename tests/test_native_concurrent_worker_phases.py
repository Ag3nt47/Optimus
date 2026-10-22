"""Concurrent discovery stays distinct from permission to mutate this computer."""
from PySide6.QtTest import QTest
import pytest

from test_native_worker_cards import app, fonts, progress


def snapshot(maintenance='scanning', apps='scanning', drivers='scanning', **changes):
    state = {'stage': 'Inspect current resource usage', 'maintenance_status': maintenance,
        'description': 'Maintenance and update discovery are working independently.', 'percentage': 8,
        'jobs': [{'id': 'software_updates', 'status': apps, 'description': 'Read available application updates',
                  'current': 0, 'total': 0, 'elapsed_seconds': 3},
                 {'id': 'driver_updates', 'status': drivers, 'description': 'Read available driver updates',
                  'current': 0, 'total': 0, 'elapsed_seconds': 4}]}
    state.update(changes)
    return state


def test_three_discovery_workers_are_visible_and_active_together_without_installer_claim(progress):
    progress.update_progress(snapshot())
    assert [card.status.text() for card in progress.worker_cards.values()] == ['Scanning', 'Scanning', 'Scanning']
    assert all(card.isVisible() for card in progress.worker_cards.values())
    assert all(card.progress.maximum() > 0 for card in progress.worker_cards.values())
    assert all('Installing' not in card.status.text() for card in progress.worker_cards.values())
    assert progress.progress.value() == 8 and not progress.completed
    assert progress.worker_cards['software_updates'].elapsed.text() == 'Elapsed: 3s'
    assert progress.worker_cards['driver_updates'].elapsed.text() == 'Elapsed: 4s'
    assert progress.title.text() == 'OPTIMIZATION IN PROGRESS'


@pytest.mark.parametrize(('phase', 'label'), [('waiting_for_maintenance', 'Waiting for maintenance'),
                                           ('waiting_for_installer', 'Waiting for installer')])
def test_blocked_update_worker_explains_its_wait_and_does_not_advance_count(progress, phase, label):
    state = snapshot(maintenance='preparing', apps=phase)
    reason = 'Temporary cleanup owns the maintenance gate.' if phase == 'waiting_for_maintenance' else 'Another approved installer owns the installer lane.'
    state['jobs'][0].update(description='Application update preparation is complete.', wait_reason=reason,
                            current=1, total=7)
    progress.update_progress(state)
    card = progress.worker_cards['software_updates']
    assert card.status.text() == label
    assert 'Running' not in card.status.text() and 'Installing' not in card.status.text()
    assert card.details.text().startswith(reason) and reason in card.details.toolTip()
    assert card.progress.value() == 1 and card.progress.maximum() == 7
    assert progress.worker_cards['maintenance'].status.text() == 'Preparing'
    assert progress.worker_cards['driver_updates'].status.text() == 'Scanning'
    progress._refresh_worker_elapsed()
    assert card.progress.value() == 1 and progress.progress.value() == 8


def test_maintenance_waits_for_preparation_with_its_actual_reason(progress):
    reason = 'Waiting for application and driver discovery before cleaning installer temporary folders.'
    progress.update_progress(snapshot(maintenance='waiting_for_preparation', apps='preparing',
        maintenance_wait_reason=reason))
    maintenance = progress.worker_cards['maintenance']
    assert maintenance.status.text() == 'Waiting for update preparation'
    assert maintenance.details.text() == reason and maintenance.details.toolTip() == reason
    assert progress.worker_cards['software_updates'].status.text() == 'Preparing'
    assert progress.worker_cards['driver_updates'].status.text() == 'Scanning'


def test_installer_handoff_preserves_independent_maintenance_and_clear_waits(progress):
    state = snapshot(maintenance='running', apps='installing', drivers='waiting_for_installer',
        stage='Review installed applications')
    state['jobs'][0].update(description='Installing approved Fixture.App', current_package='Fixture.App', current=0, total=2)
    state['jobs'][1].update(description='Driver preparation is complete.', wait_reason='The application installer is still active.', current=0, total=1)
    progress.update_progress(state)
    assert progress.worker_cards['maintenance'].status.text() == 'Running'
    assert progress.worker_cards['software_updates'].status.text() == 'Installing'
    assert progress.worker_cards['driver_updates'].status.text() == 'Waiting for installer'
    assert 'application installer is still active' in progress.worker_cards['driver_updates'].details.text()
    state['jobs'][0].update(status='completed', current=2)
    state['jobs'][1].update(status='installing', description='Installing approved Fixture GPU driver',
                           wait_reason='', current_package='Fixture GPU driver', elapsed_seconds=0)
    progress.update_progress(state)
    assert progress.worker_cards['software_updates'].status.text() == 'Completed'
    assert progress.worker_cards['driver_updates'].status.text() == 'Installing'
    assert progress.title.text() == 'OPTIMIZATION IN PROGRESS'
    assert progress.cancel_button.isEnabled()


def test_new_live_phases_keep_elapsed_live_and_stop_after_reported_completion(progress):
    state = snapshot(apps='waiting_for_maintenance')
    state['jobs'][0]['elapsed_seconds'] = 0
    progress.update_progress(state)
    card = progress.worker_cards['software_updates']
    QTest.qWait(1150)
    progress._refresh_worker_elapsed()
    assert card.elapsed.text() not in ('Elapsed: 0s', 'Elapsed: —')
    assert card.status.text() == 'Waiting for maintenance' and card.progress.value() == 0
    state['jobs'][0].update(status='completed', elapsed_seconds=2)
    progress.update_progress(state)
    QTest.qWait(1050)
    progress._refresh_worker_elapsed()
    assert card.elapsed.text() == 'Elapsed: 2s' and card.status.text() == 'Completed'


@pytest.mark.parametrize('phase', ['scanning', 'preparing', 'waiting_for_maintenance', 'waiting_for_installer', 'installing'])
def test_stopped_workflow_keeps_unreported_phase_outcomes_explicitly_unverified(progress, phase):
    progress.update_progress(snapshot(apps=phase))
    progress.finish(None, error='Fixture worker did not establish a final outcome.')
    assert progress.worker_cards['software_updates'].status.text() == 'Needs review'
    assert 'before a final updater status' in progress.worker_cards['software_updates'].details.text()
    assert '100%' not in progress.worker_cards['software_updates'].progress.text()

