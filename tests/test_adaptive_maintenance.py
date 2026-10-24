"""Readiness decisions are bounded, truthful and apply only before new work."""
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from optimus import adaptive_maintenance as adaptive


def state(**changes):
    return dict(cpu_percent=10, memory_percent=40, idle_seconds=120,
                ac_power=True, fullscreen=False, metered=False, **changes)


def assess(configuration=None, observation=None, **options):
    config = {'enabled': True, **(configuration or {})}
    return adaptive.assess(config, state() if observation is None else observation,
                           now=options.pop('now', datetime(2026, 1, 2, 12)), **options)


@pytest.mark.parametrize('field,bad', [('enabled', 1), ('cpu_limit', True), ('cpu_limit', 9),
    ('cpu_limit', 101), ('memory_limit', 9), ('idle_seconds', -1), ('idle_seconds', 3601),
    ('start_hour', 24), ('end_hour', -1), ('require_ac', 'yes'), ('avoid_metered', None),
    ('unknown_scope', True)])
def test_configuration_refuses_ambiguous_or_unbounded_choices(field, bad):
    with pytest.raises(ValueError):
        adaptive.validate_configuration({field: bad})


def test_disabled_assessment_needs_no_observations_and_does_not_claim_measurements():
    result = adaptive.assess({}, {}, network=True)
    assert result['allowed'] is True and result['enabled'] is False
    assert result['observations'] == {} and result['read_only'] is True


@pytest.mark.parametrize('field,limit', [('cpu_percent', 80), ('memory_percent', 90)])
def test_pressure_defers_at_saved_boundary_and_allows_just_below(field, limit):
    data = state()
    data[field] = limit
    assert assess(observation=data)['status'] == 'deferred'
    data[field] = limit - .1
    assert assess(observation=data)['status'] == 'ready'


@pytest.mark.parametrize('field', ['cpu_percent', 'memory_percent'])
@pytest.mark.parametrize('bad', [None, True, float('nan'), float('inf'), -1, 101, '10'])
def test_unknown_or_coerced_pressure_never_becomes_healthy(field, bad):
    data = state()
    data[field] = bad
    result = assess(observation=data)
    assert result['allowed'] is False and 'unavailable' in ' '.join(result['reasons'])


@pytest.mark.parametrize('start,end,hour,allowed', [
    (0, 0, 15, True), (8, 17, 8, True), (8, 17, 16, True), (8, 17, 17, False),
    (8, 17, 7, False), (22, 6, 22, True), (22, 6, 2, True), (22, 6, 5, True),
    (22, 6, 6, False), (22, 6, 15, False),
])
def test_daily_and_overnight_maintenance_hours(start, end, hour, allowed):
    result = assess({'start_hour': start, 'end_hour': end}, now=datetime(2026, 1, 2, hour))
    assert result['allowed'] is allowed


@pytest.mark.parametrize('idle,allowed', [(None, False), (59.9, False), (60, True), (120, True), (True, False)])
def test_idle_requirement_uses_measured_seconds(idle, allowed):
    data = state()
    data['idle_seconds'] = idle
    assert assess({'idle_seconds': 60}, data)['allowed'] is allowed


@pytest.mark.parametrize('power', [False, None, 'true', 1])
def test_required_ac_must_be_established(power):
    data = state()
    data['ac_power'] = power
    assert assess({'require_ac': True}, data)['allowed'] is False


def test_fullscreen_guard_can_be_explicitly_disabled():
    data = state()
    data['fullscreen'] = True
    assert assess(observation=data)['allowed'] is False
    assert assess({'avoid_fullscreen': False}, data)['allowed'] is True


@pytest.mark.parametrize('metered', [True, None, 'false', 0])
def test_network_work_requires_known_unmetered_when_guard_selected(metered):
    data = state()
    data['metered'] = metered
    assert assess(observation=data, network=True)['allowed'] is False
    assert assess(observation=data, network=False)['allowed'] is True
    assert assess({'avoid_metered': False}, data, network=True)['allowed'] is True


def test_checkpoint_disabled_never_calls_host_probe_or_readiness_service():
    backend = SimpleNamespace(preferences=Mock(return_value={'toolkit_settings': {}}),
                              maintenance_readiness=Mock(side_effect=AssertionError('Disabled means no host probes')))
    assert adaptive.checkpoint(backend, title='Clean selected application caches') is None
    backend.maintenance_readiness.assert_not_called()


@pytest.mark.parametrize('title', ['Inspect current resource usage', 'Review hardware, boot and memory health',
    'Capture final resource report', 'Review available driver updates'])
def test_readonly_boundary_does_not_probe_adaptive_policy(title):
    backend = SimpleNamespace(preferences=Mock(side_effect=AssertionError('Read-only boundary should not load readiness')))
    assert adaptive.checkpoint(backend, title=title) is None
    backend.preferences.assert_not_called()


@pytest.mark.parametrize('title,network', [('Clean selected application caches', False),
    ('Apply saved privacy controls', False), ('Install approved driver updates', True),
    ('Review or update configured software', True), ('Update approved Chocolatey packages', True)])
def test_mutation_boundary_passes_correct_network_requirement(title, network):
    backend = SimpleNamespace(preferences=lambda: {'toolkit_settings': {'adaptive': {'enabled': True}}},
        maintenance_readiness=Mock(return_value={'allowed': True, 'status': 'ready'}))
    assert adaptive.checkpoint(backend, title=title)['allowed'] is True
    backend.maintenance_readiness.assert_called_once_with(network=network)


@pytest.mark.parametrize('response', [None, {}, {'allowed': 1}, {'allowed': 'yes'}])
def test_enabled_readiness_failure_defers_before_mutation(response):
    backend = SimpleNamespace(preferences=lambda: {'toolkit_settings': {'adaptive': {'enabled': True}}},
        maintenance_readiness=Mock(return_value=response))
    result = adaptive.checkpoint(backend, network=True)
    assert result['allowed'] is False and result['status'] == 'deferred'


def test_deferral_is_known_no_launch_and_does_not_cancel_an_active_installer():
    decision = assess(observation={'cpu_percent': 99, 'memory_percent': 99})
    result = adaptive.deferred_result(decision)
    assert result['skipped'] is True and result['deferred'] is True
    assert result['installer_started'] is False and result['outcomeKnown'] is True
    assert result['failed_items'] == 0 and 'terminate' in decision['note']


def test_state_collection_probes_only_readonly_bounded_measurements(monkeypatch):
    import psutil
    cpu = Mock(return_value=30)
    monkeypatch.setattr(psutil, 'cpu_percent', cpu)
    monkeypatch.setattr(psutil, 'virtual_memory', lambda: SimpleNamespace(percent=50))
    monkeypatch.setattr(adaptive, '_windows_interaction', lambda: {
        'idle_seconds': 90, 'fullscreen': False, 'ac_power': True})
    network = Mock(return_value={'metered': False})
    observation = adaptive.collect_state(network_reader=network)
    assert observation['cpu_percent'] == 30 and observation['memory_percent'] == 50
    assert observation['metered'] is False and observation['errors'] == []
    cpu.assert_called_once_with(interval=.15)
    network.assert_called_once_with()


def test_unavailable_native_observations_stay_unknown(monkeypatch):
    import psutil
    monkeypatch.setattr(psutil, 'cpu_percent', Mock(side_effect=psutil.AccessDenied(999)))
    monkeypatch.setattr(psutil, 'virtual_memory', lambda: SimpleNamespace(percent=float('nan')))
    monkeypatch.setattr(adaptive, '_windows_interaction', Mock(side_effect=OSError('fixture access denied')))
    observation = adaptive.collect_state(network_reader=Mock(side_effect=RuntimeError('fixture no network cost')))
    assert observation['cpu_percent'] is None and observation['memory_percent'] is None
    assert observation['metered'] is None and observation['ac_power'] is None
    assert len(observation['errors']) == 3
    assert assess(observation=observation, network=True)['allowed'] is False
