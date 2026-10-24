"""Toolkit scopes and readiness integrate without replay or active-process control."""
import copy
import json
from pathlib import Path
import threading
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from optimus import adaptive_maintenance as adaptive
from optimus.automation import RunLease
from optimus.continue_updates import run_saved_updates
from optimus.maintenance_toolkit import MaintenanceToolkit
from optimus.parallel_updates import ParallelInstallerQueue
from optimus.toolkit_configuration import validate_configuration
from optimus.workflow import DEFAULTS, Preferences


def toolkit(tmp_path, choices=None):
    settings = {'toolkit_settings': validate_configuration(choices or {})}
    backend = SimpleNamespace(data_dir=tmp_path, system='win32',
        preferences=Mock(side_effect=lambda: copy.deepcopy(settings)),
        bridge=SimpleNamespace(request=Mock(side_effect=AssertionError('Unexpected native probe'))))
    return MaintenanceToolkit(backend), backend, settings


def observations():
    return {'cpu_percent': 30, 'memory_percent': 40, 'idle_seconds': 120,
            'fullscreen': False, 'ac_power': True, 'metered': False}


def test_disabled_facade_has_no_host_probe_or_readiness_cache(tmp_path, monkeypatch):
    facade, backend, _ = toolkit(tmp_path)
    collect = Mock(side_effect=AssertionError('Disabled readiness cannot probe the host'))
    monkeypatch.setattr(adaptive, 'collect_state', collect)
    assert facade.readiness(network=True)['allowed'] is True
    collect.assert_not_called()
    backend.bridge.request.assert_not_called()
    assert not facade._readiness_cache


def test_readiness_cache_is_invalidated_by_current_saved_adaptive_scope(tmp_path, monkeypatch):
    facade, _, saved = toolkit(tmp_path, {'adaptive': {'enabled': True}})
    collect = Mock(return_value=observations())
    monkeypatch.setattr(adaptive, 'collect_state', collect)
    assert facade.readiness()['allowed'] is True
    assert facade.readiness()['allowed'] is True
    assert collect.call_count == 1
    saved['toolkit_settings']['adaptive']['cpu_limit'] = 20
    assert facade.readiness()['allowed'] is False
    assert collect.call_count == 2


def test_cached_observations_cannot_be_mutated_by_ui_caller(tmp_path, monkeypatch):
    facade, _, _ = toolkit(tmp_path, {'adaptive': {'enabled': True}})
    collect = Mock(return_value=observations())
    monkeypatch.setattr(adaptive, 'collect_state', collect)
    first = facade.readiness()
    first['observations']['cpu_percent'] = 99
    first['allowed'] = False
    assert facade.readiness()['allowed'] is True
    assert facade.readiness()['observations']['cpu_percent'] == 30
    assert collect.call_count == 1


def test_network_probe_only_runs_for_metered_sensitive_network_work(tmp_path, monkeypatch):
    facade, backend, saved = toolkit(tmp_path, {'adaptive': {'enabled': True}})
    backend.bridge.request = Mock(return_value={'metered': False})
    def collect(*, network_reader=None):
        data = observations()
        data['metered'] = None if network_reader is None else network_reader()['metered']
        return data
    monkeypatch.setattr(adaptive, 'collect_state', collect)
    assert facade.readiness(network=False)['allowed'] is True
    backend.bridge.request.assert_not_called()
    assert facade.readiness(network=True)['allowed'] is True
    backend.bridge.request.assert_called_once_with('adaptive_network', timeout=10)
    saved['toolkit_settings']['adaptive']['avoid_metered'] = False
    assert facade.readiness(network=True)['allowed'] is True
    assert backend.bridge.request.call_count == 1


def test_captured_toolkit_stage_cannot_apply_newly_changed_scope(tmp_path, monkeypatch):
    facade, _, saved = toolkit(tmp_path, {'application_caches': {'enabled': True, 'apps': ['discord']}})
    service = Mock()
    facade.service = Mock(return_value=service)
    barrier = Mock(return_value=None)
    monkeypatch.setattr('optimus.parallel_updates.pending_updates_barrier', barrier)
    with RunLease(tmp_path) as lease:
        before, _ = facade.stages(saved, lease=lease)
        operation = next(row[2] for row in before if row[0] == 'Clean selected application caches')
        saved['toolkit_settings']['application_caches']['apps'] = ['spotify']
        result = operation()
        assert lease.handle is not None
    assert result['skipped'] is True and 'scope changed' in result['note']
    facade.service.assert_not_called()
    service.run.assert_not_called()
    barrier.assert_not_called()


def test_captured_stage_uses_owned_lease_and_defers_before_service_when_pressured(tmp_path, monkeypatch):
    facade, _, saved = toolkit(tmp_path, {'application_caches': {'enabled': True, 'apps': ['discord']},
                                        'adaptive': {'enabled': True}})
    monkeypatch.setattr('optimus.parallel_updates.pending_updates_barrier', lambda _: None)
    facade.readiness = Mock(return_value={'allowed': False, 'reasons': ['CPU pressure exceeds saved limit.']})
    facade.service = Mock(side_effect=AssertionError('No cache service while deferred'))
    with RunLease(tmp_path) as lease:
        before, _ = facade.stages(saved, lease=lease)
        result = next(row[2] for row in before if row[0] == 'Clean selected application caches')()
        assert lease.handle is not None
    assert result['deferred'] is True and result['installer_started'] is False
    facade.service.assert_not_called()


def test_update_only_continuation_accepts_and_retains_supplied_owned_lease(tmp_path):
    config = Preferences.validate(dict(DEFAULTS, setup_version=2, software_updates_enabled=False,
                                       driver_updates_enabled=False))
    backend = SimpleNamespace(data_dir=tmp_path, system='win32', preferences=lambda: config,
        preference_store=SimpleNamespace(validate=Preferences.validate))
    with RunLease(tmp_path) as lease:
        report = run_saved_updates(backend, _lease=lease)
        assert report['status'] == 'no_enabled_updates'
        assert lease.handle is not None
        with pytest.raises(RuntimeError):
            RunLease(tmp_path).acquire()
    assert report['preferences_saved'] is False


def test_supplied_lease_from_another_data_root_cannot_start_continuation(tmp_path):
    data, other = tmp_path / 'Data', tmp_path / 'Other'
    data.mkdir()
    backend = SimpleNamespace(data_dir=data, preferences=Mock(side_effect=AssertionError('Invalid lease before preferences')))
    with RunLease(other) as lease:
        with pytest.raises(ValueError, match='lease'):
            run_saved_updates(backend, _lease=lease)
    assert not (data / 'update-continuations').exists()
    backend.preferences.assert_not_called()


def queue_fixture():
    queue = ParallelInstallerQueue.__new__(ParallelInstallerQueue)
    probe = Mock(return_value={'allowed': True, 'reasons': []})
    queue.backend = SimpleNamespace(preferences=lambda: {'toolkit_settings': {'adaptive': {'enabled': True}}},
                                    maintenance_readiness=probe)
    queue.condition = threading.Condition(threading.RLock())
    queue.barrier, queue.active_job, queue.maintenance_ready = None, None, False
    job = SimpleNamespace(prepared=threading.Event(), cancel_event=threading.Event(), navigation=None,
        will_install=False, result=None, mux=Mock(), gate_refusal=None)
    queue.jobs = [job]
    return queue, job, probe


def test_installer_slot_rechecks_readiness_after_waiting_for_maintenance():
    queue, job, probe = queue_fixture()
    checked = threading.Event()
    def initial(**_options):
        checked.set()
        return {'allowed': True, 'reasons': []}
    probe.side_effect = initial
    results = []
    thread = threading.Thread(target=lambda: results.append(queue._before_install(job,
        {'candidate_count': 1, 'review_only': False})))
    thread.start()
    try:
        assert checked.wait(3)
        with queue.condition:
            probe.side_effect = None
            probe.return_value = {'allowed': False, 'reasons': ['The network became metered while waiting.']}
            queue.maintenance_ready = True
            queue.condition.notify_all()
        thread.join(3)
        assert not thread.is_alive()
        assert results == [False]
        assert queue.active_job is None and 'metered' in job.gate_refusal
        assert not job.cancel_event.is_set()
    finally:
        job.cancel_event.set()
        with queue.condition:
            queue.condition.notify_all()
        thread.join(3)


def test_current_native_call_is_not_cancelled_or_polled_by_adaptive_policy():
    queue, job, probe = queue_fixture()
    queue.maintenance_ready = True
    assert queue._before_install(job, {'candidate_count': 1, 'review_only': False}) is True
    calls = probe.call_count
    probe.return_value = {'allowed': False, 'reasons': ['CPU pressure increased during vendor work.']}
    # The live vendor call owns the lane. Changing policy has no control API
    # that suspends/kills it; the next boundary alone samples readiness again.
    assert queue.active_job is job and probe.call_count == calls
    assert not job.cancel_event.is_set()
    assert queue._before_install(job, {'candidate_count': 1, 'review_only': False}) is False
    assert not job.cancel_event.is_set()
