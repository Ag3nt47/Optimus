"""Idle-retirement owns only bridge hosts; unresolved operations stay alive."""
import threading
import time
from unittest.mock import Mock

import pytest
from optimus import bridge as bridge_module
from optimus.bridge import PowerShellBridge
from optimus.emergency_stop import EmergencyStop
from test_bridge import MockProcess


def test_retirement_defers_outstanding_native_then_retries_at_late_idle_reply(tmp_path):
    bridge = PowerShellBridge(tmp_path)
    bridge.close = Mock()
    bridge._pending['fixture'] = ('hardware_health', 123)
    bridge.close_when_idle()
    bridge.close.assert_not_called()
    assert bridge._retire_when_idle
    bridge._pending.pop('fixture')
    assert bridge._retire_idle_host()
    bridge.close.assert_called_once_with()


def test_reader_reply_thread_never_waits_on_a_busy_request_lock(tmp_path):
    bridge = PowerShellBridge(tmp_path)
    bridge.close = Mock()
    entered, release = threading.Event(), threading.Event()
    def hold():
        with bridge._lock:
            entered.set(); release.wait(2)
    thread = threading.Thread(target=hold); thread.start(); assert entered.wait(1)
    try:
        bridge.close_when_idle()
        bridge.close.assert_not_called()
    finally:
        release.set(); thread.join(2)
    assert bridge._retire_idle_host()
    bridge.close.assert_called_once_with()


def test_busy_or_unresolved_installer_ticket_prevents_idle_close(tmp_path):
    bridge = PowerShellBridge(tmp_path)
    bridge.close = Mock()
    bridge._busy = True
    bridge.close_when_idle()
    bridge.close.assert_not_called()
    bridge._busy = False
    bridge._stop_operations['installer'] = (object(), 'driver_update_apply')
    assert not bridge._retire_idle_host()
    bridge.close.assert_not_called()
    bridge._stop_operations.clear()
    assert bridge._retire_idle_host()
    bridge.close.assert_called_once_with()


def test_cleanup_failure_is_retryable_without_leaving_a_lock_held(tmp_path):
    bridge = PowerShellBridge(tmp_path)
    bridge.close = Mock(side_effect=[OSError('Exit race.'), None])
    bridge.close_when_idle()
    assert bridge._lock.acquire(blocking=False)
    bridge._lock.release()
    assert bridge._retire_idle_host()
    assert bridge.close.call_count == 2


def test_actual_reader_loop_retires_after_a_late_terminal_reply(tmp_path, monkeypatch):
    process = MockProcess()
    monkeypatch.setattr(bridge_module.subprocess, 'Popen', Mock(return_value=process))
    bridge = PowerShellBridge(tmp_path)
    controller = EmergencyStop()
    bridge._bind_emergency_stop(controller)
    try:
        with pytest.raises(TimeoutError):
            bridge.request('hardware_health', timeout=.02)
        bridge.close_when_idle()
        assert not process.stdin.closed and bridge._pending
        process.reply(result={'read_only': True, 'status': 'completed'})
        deadline = time.monotonic() + 2
        while bridge._process is not None and time.monotonic() < deadline:
            time.sleep(.005)
        assert bridge._process is None and process.stdin.closed
        assert not process.terminated and not bridge._pending and not bridge._stop_operations
        assert not controller._operations
    finally:
        process.stdout.lines.put(None)
