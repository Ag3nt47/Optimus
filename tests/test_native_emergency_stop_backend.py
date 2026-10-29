"""STOP uses owned fake children/native jobs; no host process is terminated."""
from __future__ import annotations

import json
from pathlib import Path
import subprocess
import threading
import time
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from optimus.emergency_stop import EmergencyStop, StopRequested, controller_for
from optimus.bridge import PowerShellBridge
from optimus.maintenance import Maintenance
from optimus.native_operations import NativeRunner
from optimus.parallel_updates import isolated_backend
from optimus import winget_console
from test_bridge import MockProcess
from test_native_winget_console import FakeConsole


def wait_until(predicate):
    deadline = time.monotonic() + 4
    while not predicate():
        assert time.monotonic() < deadline, 'The fake owned observer did not reach its checkpoint.'
        time.sleep(.005)


class Child:
    def __init__(self, *, timeout_once=False):
        self.pid, self.returncode = 987654321, None
        self.ended, self.reached = threading.Event(), threading.Event()
        self.terminate_calls, self.kill_calls, self.calls = 0, 0, 0
        self.timeout_once = timeout_once

    def poll(self):
        return self.returncode

    def wait(self, timeout=None):
        assert self.ended.wait(4)
        return self.returncode

    def terminate(self):
        self.terminate_calls += 1
        self.returncode = -15
        self.ended.set()

    def kill(self):
        self.kill_calls += 1
        raise AssertionError('No child kill escalation is permitted here.')

    def communicate(self, timeout=None):
        self.calls += 1
        self.reached.set()
        if self.timeout_once and self.calls == 1:
            raise subprocess.TimeoutExpired(['fixture'], timeout)
        assert self.ended.wait(4)
        return 'actual fixture result', ''


def test_lifetime_latch_sets_all_tokens_and_clear_cannot_restart():
    controller = EmergencyStop()
    first, second = threading.Event(), threading.Event()
    tokens = [controller.bind_cancel(first), controller.bind_cancel(second)]
    assert not any(value.is_set() for value in tokens)
    assert controller.request_stop()['stopped']
    assert first.is_set() and second.is_set()
    first.clear(); second.clear()
    assert all(value.is_set() for value in tokens)
    assert controller.bind_cancel(threading.Event()).is_set()
    with pytest.raises(StopRequested) as raised:
        with controller.launch_gate():
            pytest.fail('A future launch entered the stopped gate.')
    assert raised.value.installer_started is False and raised.value.outcomeKnown is True
    assert raised.value.running is False


def test_stop_and_snapshot_return_without_waiting_for_busy_launch_gate():
    controller = EmergencyStop()
    entered, release = threading.Event(), threading.Event()
    def launching():
        with controller.launch_gate():
            ticket = controller.begin_native('Already entering an owned launch')
            entered.set()
            assert release.wait(4)
            ticket.finish()  # This fixture deliberately launched no process.
    thread = threading.Thread(target=launching)
    thread.start()
    try:
        assert entered.wait(2)
        before = time.monotonic()
        state = controller.request_stop()
        assert time.monotonic() - before < .2
        assert state['stopped'] and not state['drained']
        assert state['ownership_registration_pending']
        assert controller.bind_cancel(threading.Event()).is_set()
        assert not controller.snapshot()['drained']
    finally:
        release.set(); thread.join(4)
    assert not thread.is_alive()
    wait_until(lambda: controller.snapshot()['drained'])


def test_cloned_bridge_and_caches_share_one_lifetime_controller(tmp_path):
    backend = SimpleNamespace(bridge=PowerShellBridge(tmp_path),
        maintenance=Maintenance(None, tmp_path))
    controller = controller_for(backend)
    clone = isolated_backend(backend, isolate_bridge=True)
    assert clone.bridge is not backend.bridge and clone.maintenance is not backend.maintenance
    assert controller_for(clone) is controller
    assert clone.bridge.stop_controller is clone.maintenance.stop_controller is controller
    controller.request_stop()
    with pytest.raises(StopRequested):
        clone.bridge.request('audit')
    assert clone.bridge._process is None


def test_stop_terminates_only_original_disposable_popen_not_other_objects(monkeypatch):
    controller, child, unrelated = EmergencyStop(), Child(), Child()
    monkeypatch.setattr('optimus.emergency_stop.subprocess.Popen', Mock(return_value=child))
    monkeypatch.setattr('optimus.emergency_stop.sys.platform', 'win32')
    assert controller.spawn_disposable(['fixture-read-only']) is child
    controller.request_stop()
    wait_until(lambda: controller.snapshot()['drained'])
    assert child.terminate_calls == 1 and child.kill_calls == 0
    assert unrelated.terminate_calls == unrelated.kill_calls == 0
    with pytest.raises(StopRequested):
        controller.spawn_disposable(['fixture-must-not-start'])


def test_stop_waits_for_active_native_and_does_not_adopt_arbitrary_pids():
    controller, abort = EmergencyStop(), Mock()
    ticket = controller.begin_native('Exact Windows operation', cancel=abort)
    ticket.started = True
    controller.request_stop()
    wait_until(lambda: abort.called)
    state = controller.snapshot()
    assert state['native_pending'] == 1 and not state['drained']
    assert state['operations'][0]['status'] == 'waiting_for_native_completion'
    assert state['operations'][0]['outcomeKnown'] is False
    ticket.finish(known=False, error='Native receipt missing')
    assert controller.snapshot()['native_pending'] == 1
    ticket.finish()  # Only the actual native observer may establish this.
    assert controller.snapshot()['drained']
    assert not hasattr(controller, 'terminate_pid') and not hasattr(controller, 'reset')


@pytest.mark.parametrize('kind', ['maintenance', 'native'])
def test_timed_out_native_retains_original_observer_then_drains_without_kill(tmp_path, monkeypatch, kind):
    controller, child = EmergencyStop(), Child(timeout_once=True)
    monkeypatch.setattr('optimus.maintenance.subprocess.Popen', Mock(return_value=child))
    if kind == 'maintenance':
        service = Maintenance(None, tmp_path)
        service.stop_controller = controller
        action = lambda: service._run(['fixture', 'download'], timeout=.001)
        expected = subprocess.TimeoutExpired
    else:
        service = NativeRunner('linux')
        service.stop_controller = controller
        action = lambda: service.run(['/fixture/package-manager', 'update'], timeout=.001)
        expected = RuntimeError
    try:
        with pytest.raises(expected):
            action()
        assert controller.request_stop()['native_pending'] == 1
        assert child.terminate_calls == child.kill_calls == 0
        child.returncode = 1
        child.ended.set()
        wait_until(lambda: controller.snapshot()['drained'])
        assert child.calls == 2
        assert child.terminate_calls == child.kill_calls == 0
    finally:
        child.ended.set()


def bridge_fixture(tmp_path, monkeypatch):
    process = MockProcess()
    monkeypatch.setattr('optimus.bridge.subprocess.Popen', Mock(return_value=process))
    backend = SimpleNamespace(bridge=PowerShellBridge(tmp_path))
    controller = controller_for(backend)
    return backend.bridge, process, controller


def test_stop_requests_driver_abort_marker_keeps_host_and_waits_late_terminal(tmp_path, monkeypatch):
    bridge, process, controller = bridge_fixture(tmp_path, monkeypatch)
    try:
        with pytest.raises(TimeoutError):
            bridge.request('driver_update_apply', timeout=.02)
        request = json.loads(process.stdin.writes[0])
        marker = tmp_path / 'request-cancellation' / request['id'] / 'cancel.request'
        assert not marker.exists() and controller.snapshot()['native_pending'] == 1
        controller.request_stop()
        wait_until(marker.exists)
        assert not process.terminated and bridge._pending
        process.reply(result={'cancelled': True, 'installer_started': True, 'outcomeKnown': True, 'running': False})
        wait_until(lambda: controller.snapshot()['drained'])
        assert not bridge._pending and not marker.exists()
        assert not process.terminated
    finally:
        process.stdout.lines.put(None)


@pytest.mark.parametrize('operation,known', [('audit', True), ('registry_remove', True),
                                          ('maintenance_apply', False), ('driver_update_apply', False)])
def test_terminal_bridge_error_does_not_invent_native_installation_state(tmp_path, monkeypatch, operation, known):
    bridge, process, controller = bridge_fixture(tmp_path, monkeypatch)
    process.stdin.on_flush = lambda: process.reply(ok=False, error='fixture error')
    try:
        with pytest.raises(RuntimeError, match='fixture error'):
            bridge.request(operation, timeout=1)
        assert controller.snapshot()['drained'] is known
        assert not process.terminated
        if not known:
            assert controller.snapshot()['operations'][0]['outcomeKnown'] is False
    finally:
        process.stdout.lines.put(None)


@pytest.mark.parametrize('result', [
    {'outcomeKnown': False, 'running': True, 'installer_started': True},
    {'outcomeKnown': 'true', 'running': False, 'installer_started': True},
    {'outcomeKnown': True, 'running': 0, 'installer_started': True},
    {'outcomeKnown': True, 'running': False},
])
def test_incomplete_or_untyped_native_terminal_fields_stay_unknown(tmp_path, monkeypatch, result):
    bridge, process, controller = bridge_fixture(tmp_path, monkeypatch)
    process.stdin.on_flush = lambda: process.reply(result=result)
    try:
        assert bridge.request('driver_update_apply', timeout=1) == result
        assert controller.snapshot()['native_pending'] == 1
    finally:
        process.stdout.lines.put(None)


def test_stop_refuses_all_real_launchers_before_any_child(tmp_path, monkeypatch):
    launch = Mock()
    monkeypatch.setattr('optimus.emergency_stop.subprocess.Popen', launch)
    controller = EmergencyStop()
    maintenance, native = Maintenance(None, tmp_path), NativeRunner()
    maintenance.stop_controller = native.stop_controller = controller
    controller.request_stop()
    for operation in (lambda: maintenance._run(['fixture', 'install']),
                      lambda: native.run(['fixture']), lambda: native.privileged(['fixture'])):
        with pytest.raises(StopRequested):
            operation()
    launch.assert_not_called()


def test_console_timeout_then_actual_native_quiescence_releases_stop_entry(monkeypatch):
    FakeConsole.instances = []
    FakeConsole.automatic, FakeConsole.terminal_nonce = False, None
    FakeConsole.launch_error = FakeConsole.post_launch_error = None
    FakeConsole.exit_code, FakeConsole.flood = 0, False
    monkeypatch.setattr(winget_console, '_ConsoleProcess', FakeConsole)
    monkeypatch.setattr(winget_console, '_source_console_executable', lambda: 'fixture')
    controller = EmergencyStop()
    try:
        with pytest.raises(subprocess.TimeoutExpired):
            winget_console.run_winget_console(['C:/fixture/app.exe'], timeout=.03,
                progress_callback=lambda value: None, stop_controller=controller)
        native = FakeConsole.instances[-1]
        controller.request_stop()
        assert controller.snapshot()['native_pending'] == 1
        assert not native.closed and not native.close_calls and not native.dispose_calls
        native.complete()
        wait_until(lambda: controller.snapshot()['drained'])
        assert native.events == ['launched', 'target_exited', 'console_closed', 'finished']
    finally:
        for native in FakeConsole.instances:
            if native.exit is None:
                native.complete()


def test_windows_security_install_cancel_retains_host_until_actual_exit(monkeypatch):
    from optimus import security_updates
    controller, child, stop = EmergencyStop(), Child(), threading.Event()
    original_communicate = child.communicate
    def communicate(timeout=None):
        if child.calls == 0:
            child.calls += 1
            stop.set()
            raise subprocess.TimeoutExpired(['fixture-security-host'], timeout)
        return original_communicate(timeout)
    child.communicate = communicate
    monkeypatch.setattr(security_updates, 'os', SimpleNamespace(name='nt', environ={}))
    monkeypatch.setattr(security_updates.subprocess, 'Popen', Mock(return_value=child))
    try:
        result = security_updates.native_request('install', {}, cancel_event=stop, stop_controller=controller)
        assert result['cancelled'] is True and result['may_continue'] is True
        assert result['ok'] is False
        assert controller.snapshot()['native_pending'] == 1
        controller.request_stop()
        assert child.terminate_calls == child.kill_calls == 0
        child.returncode = 1; child.ended.set()
        wait_until(lambda: controller.snapshot()['drained'])
        assert child.terminate_calls == child.kill_calls == 0
        assert result['ok'] is False  # Observer cannot rewrite a failed receipt.
    finally:
        child.ended.set()


def test_windows_security_install_stop_before_launch_has_no_native_child(monkeypatch):
    from optimus import security_updates
    controller = EmergencyStop()
    launch = Mock()
    monkeypatch.setattr(security_updates, 'os', SimpleNamespace(name='nt', environ={}))
    monkeypatch.setattr(security_updates.subprocess, 'Popen', launch)
    controller.request_stop()
    with pytest.raises(StopRequested):
        security_updates.native_request('install', {}, stop_controller=controller)
    launch.assert_not_called()


def test_additional_default_installer_and_reader_share_backend_controller(tmp_path, monkeypatch):
    from optimus.additional_packages import AdditionalPackages, _read_command
    backend = SimpleNamespace(data_dir=tmp_path)
    installer = Mock(return_value='owned native result')
    monkeypatch.setattr(winget_console, 'run_winget_console', installer)
    service = AdditionalPackages(backend, discover=Mock(return_value=None))
    controller = controller_for(backend)
    assert service.stop_controller is controller
    assert service.reader.func is _read_command
    assert service.reader.keywords['stop_controller'] is controller
    assert service.installer(['fixture'], timeout=1, progress_callback=lambda value: None) == 'owned native result'
    assert installer.call_args.kwargs['stop_controller'] is controller
    controller.request_stop()
    with pytest.raises(StopRequested):
        service.scan()
    service.discover.assert_not_called()


def test_additional_stop_during_prelaunch_progress_closes_exact_known_idle_intent(tmp_path):
    from test_additional_packages import Harness
    from optimus.parallel_updates import pending_updates_barrier
    harness = Harness(tmp_path)
    controller = controller_for(harness.backend)
    result = harness.run(progress_callback=lambda value: controller.request_stop())
    assert result['cancelled'] is True and result['installer_started'] is False
    assert not result.get('uncertain') and result['outcomeKnown'] is True
    assert harness.calls == []
    assert result['items'][0]['status'] == 'cancelled'
    assert pending_updates_barrier(harness.backend) is None


def test_stop_latched_between_ticket_preparation_and_launch_rejects_spawn(monkeypatch):
    controller, spawn = EmergencyStop(), Mock()
    prepared = controller.begin_native('Prepared only')
    controller.request_stop()
    try:
        with pytest.raises(StopRequested):
            with controller.launch_gate():
                spawn()
        spawn.assert_not_called()
    finally:
        prepared.finish()  # No child was launched.


def test_workflow_entrypoints_refuse_new_work_after_latch_without_running_lease(tmp_path):
    from optimus.workflow import Workflow
    from optimus.native_workflow import NativeWorkflow
    from optimus.continue_updates import run_saved_updates
    from optimus.post_run_fixes import PostRunFixes
    backend = SimpleNamespace(data_dir=tmp_path)
    controller_for(backend).request_stop()
    for operation in (lambda: Workflow(backend).run({}, lambda value: None),
                      lambda: NativeWorkflow(backend).run({}, lambda value: None),
                      lambda: run_saved_updates(backend),
                      lambda: PostRunFixes(backend).apply({}, {})):
        with pytest.raises(StopRequested):
            operation()
    assert list(tmp_path.iterdir()) == []
