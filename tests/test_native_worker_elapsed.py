"""Worker durations include discovery and waiting, then retain a final value."""
from types import SimpleNamespace

import pytest

from optimus.parallel_updates import ProgressMux


@pytest.fixture
def clock(monkeypatch):
    value = SimpleNamespace(now=100.0)
    monkeypatch.setattr('optimus.parallel_updates.time.monotonic', lambda: value.now)
    return value


def job(event, job_id='software_updates'):
    return next(row for row in event['jobs'] if row['id'] == job_id)


def test_elapsed_includes_discovery_both_gates_and_package_changes(clock):
    events = []
    mux = ProgressMux(events.append)
    mux.background('pending')
    clock.now = 103.0
    mux.background('scanning', {'description': 'Read current updates.'})
    assert job(events[-1])['elapsed_seconds'] == 3
    assert job(events[-1])['total'] == 0

    clock.now = 113.0
    mux.background('waiting_for_maintenance', {'total': 6})
    clock.now = 125.0
    mux.background('waiting_for_installer')
    assert job(events[-1])['elapsed_seconds'] == 25
    assert job(events[-1])['current'] == 0

    clock.now = 130.0
    mux.background('preparing')
    clock.now = 132.0
    mux.background('installing', {'id': 'Fixture.First', 'current': 0, 'total': 6})
    clock.now = 140.0
    mux.background('installing', {'id': 'Fixture.Second', 'current': 1, 'total': 6})
    assert job(events[-1])['elapsed_seconds'] == 40
    assert job(events[-1])['current_package'] == 'Fixture.Second'
    assert job(events[-1])['current'] == 1

    clock.now = 143.0
    mux.background('needs_review', {'description': 'A preflight check stopped further attempts.', 'current': 1})
    assert job(events[-1])['elapsed_seconds'] == 43
    assert job(events[-1])['status'] == 'needs_review'
    assert (job(events[-1])['current'], job(events[-1])['total']) == (1, 6)


@pytest.mark.parametrize('terminal', ['completed', 'needs_review', 'cancelled', 'deferred', 'failed'])
def test_terminal_duration_freezes_across_heartbeats_foreground_and_join_emission(clock, terminal):
    events = []
    mux = ProgressMux(events.append)
    mux.background('scanning')
    clock.now = 142.75
    mux.background(terminal, {'current': 1, 'total': 6})
    original = dict(job(events[-1]))
    assert original['elapsed_seconds'] == 42.75

    clock.now = 500.0
    mux.heartbeat()
    assert job(events[-1]) == original
    mux({'stage': 'Final verification', 'percentage': 95})
    assert job(events[-1]) == original
    # join() reports the same terminal outcome again, potentially much later.
    mux.background(terminal, {'current': 1, 'total': 6})
    assert job(events[-1]) == original


def test_each_worker_has_an_independent_clock_and_terminal_boundary(clock):
    events = []
    mux = ProgressMux(events.append)
    apps, drivers = mux.for_job('software_updates'), mux.for_job('driver_updates')
    apps.background('scanning')
    clock.now = 110.0
    drivers.background('scanning')
    clock.now = 133.0
    drivers.background('completed', {'current': 0, 'total': 0})
    clock.now = 143.0
    apps.background('needs_review', {'current': 1, 'total': 6})
    assert job(events[-1])['elapsed_seconds'] == 43
    assert job(events[-1], 'driver_updates')['elapsed_seconds'] == 23
    clock.now = 166.0
    mux({'stage': 'Continuing maintenance', 'percentage': 70})
    assert job(events[-1])['elapsed_seconds'] == 43
    assert job(events[-1], 'driver_updates')['elapsed_seconds'] == 23


def test_first_observed_terminal_event_does_not_invent_an_earlier_start(clock):
    events = []
    mux = ProgressMux(events.append)
    mux.background('deferred', {'description': 'Blocked before discovery; no installer started.'})
    assert job(events[-1])['elapsed_seconds'] == 0
    assert job(events[-1])['current'] == 0
    assert job(events[-1])['total'] == 0
    clock.now = 160.0
    mux.heartbeat()
    assert job(events[-1])['elapsed_seconds'] == 0


def test_heartbeat_updates_elapsed_without_changing_observed_progress(clock):
    events = []
    mux = ProgressMux(events.append)
    mux.background('waiting_for_installer', {'id': 'Fixture.App', 'current': 1, 'total': 6})
    first = dict(job(events[-1]))
    clock.now = 125.5
    mux.heartbeat()
    later = dict(job(events[-1]))
    assert later.pop('elapsed_seconds') == 25.5
    first.pop('elapsed_seconds')
    assert later == first
