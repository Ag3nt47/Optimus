"""Explicit hardware reads are isolated from mutating workers; no host is started."""
import threading
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from optimus import hardware_health as module
from optimus.bridge import PowerShellBridge
from optimus.emergency_stop import EmergencyStop
from optimus.extended_backend import ExtendedMaintenanceBackend


def observation():
    return module.normalize_hardware_health({'schema': 1, 'read_only': True,
        'status': 'completed', 'storage': [], 'batteries': []})


def test_forced_read_uses_own_host_controller_and_closes_it_after_completion(tmp_path, monkeypatch):
    original = PowerShellBridge(tmp_path)
    original.request = Mock(side_effect=AssertionError('Must not queue on maintenance host.'))
    controller = EmergencyStop()
    backend = SimpleNamespace(data_dir=tmp_path, bridge=original, _emergency_stop=controller)
    service = module.HardwareHealth(backend)
    captured = []
    def collect(reader, **kwargs):
        assert reader.bridge is not original and reader.bridge._process is None
        assert reader.bridge.stop_controller is controller
        assert kwargs['force_refresh'] is True
        assert not kwargs['cancel_event'].is_set()
        reader.bridge.close = Mock()
        captured.append(reader)
        return observation()
    monkeypatch.setattr(module, 'collect_hardware_health', collect)
    result = service.scan(force_refresh=True, record_history=False)
    assert result['status'] == 'completed'
    assert service.cached()['scan_id'] == result['scan_id']
    captured[0].bridge.close.assert_called_once_with()
    original.request.assert_not_called()
    assert original._process is None


def test_regular_read_reuses_existing_workflow_host_without_resident_extra_host(tmp_path, monkeypatch):
    backend = SimpleNamespace(data_dir=tmp_path, bridge=PowerShellBridge(tmp_path))
    service = module.HardwareHealth(backend)
    def collect(reader, **kwargs):
        assert reader is backend and 'force_refresh' not in kwargs
        return observation()
    monkeypatch.setattr(module, 'collect_hardware_health', collect)
    service.scan(record_history=False)
    assert service._reader_bridge is None and backend.bridge._process is None


def test_reader_cleanup_error_preserves_observation_and_releases_scan_lock(tmp_path, monkeypatch):
    backend = SimpleNamespace(data_dir=tmp_path, bridge=PowerShellBridge(tmp_path))
    service = module.HardwareHealth(backend)
    def collect(reader, **kwargs):
        reader.bridge.close_when_idle = Mock(side_effect=OSError('Closed pipe fixture.'))
        return observation()
    monkeypatch.setattr(module, 'collect_hardware_health', collect)
    result = service.scan(force_refresh=True, record_history=False)
    assert result['status'] == 'completed' and service.cached()['scan_id'] == result['scan_id']
    assert service._lock.acquire(blocking=False)
    service._lock.release()


def test_dialog_cancel_token_remains_live_through_serialized_read(tmp_path, monkeypatch):
    token = threading.Event()
    service = module.HardwareHealth(SimpleNamespace(data_dir=tmp_path))
    def collect(reader, *, cancel_event):
        assert not cancel_event.is_set()
        token.set()
        assert cancel_event.is_set()
        return module.normalize_hardware_health({'schema': 1, 'read_only': True,
            'status': 'cancelled', 'storage': [], 'batteries': []})
    monkeypatch.setattr(module, 'collect_hardware_health', collect)
    assert service.scan(cancel_event=token)['status'] == 'cancelled'
    assert not (tmp_path / 'hardware-health').exists()


@pytest.mark.parametrize('refresh,force', [(True, True), (True, False), (False, False)])
def test_backend_explicit_flag_and_history_are_forwarded_only_when_requested(refresh, force):
    token = threading.Event()
    backend = SimpleNamespace(hardware_health=Mock(return_value=observation()),
        hardware_health_cached=Mock(return_value=observation()),
        hardware_health_history=Mock(return_value={'schema': 1, 'series': []}))
    result = ExtendedMaintenanceBackend.hardware_health_details(backend,
        refresh=refresh, force_refresh=force, cancel_event=token)
    if refresh:
        backend.hardware_health.assert_called_once_with(cancel_event=token, force_refresh=force)
        backend.hardware_health_cached.assert_not_called()
    else:
        backend.hardware_health.assert_not_called()
        backend.hardware_health_cached.assert_called_once_with()
    assert result['history']['series'] == []


@pytest.mark.parametrize('refresh,force', [(False, True), (True, 1), ('yes', False)])
def test_invalid_force_options_do_not_start_any_read(refresh, force):
    backend = SimpleNamespace(hardware_health=Mock(), hardware_health_cached=Mock())
    with pytest.raises(ValueError):
        ExtendedMaintenanceBackend.hardware_health_details(backend, refresh=refresh, force_refresh=force)
    backend.hardware_health.assert_not_called()
    backend.hardware_health_cached.assert_not_called()
