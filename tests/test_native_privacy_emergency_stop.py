"""STOP owns privacy/security clients without aborting native changes."""
import json
import subprocess
import threading
import time
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from optimus import system_privacy as privacy
from optimus.backend import Backend
from optimus.emergency_stop import EmergencyStop, StopRequested, controller_for


class Child:
    def __init__(self):
        self.pid = 987654321  # No real child identity is adopted by this fixture.
        self.returncode = None
        self.ended = threading.Event()
        self.terminate_calls = self.kill_calls = self.calls = 0

    def poll(self):
        return self.returncode

    def communicate(self, timeout=None):
        self.calls += 1
        if not self.ended.wait(timeout):
            raise subprocess.TimeoutExpired('fixture-privacy-host', timeout)
        return json.dumps({'ok': True}), ''

    def wait(self, timeout=None):
        if not self.ended.wait(timeout):
            raise subprocess.TimeoutExpired('fixture-privacy-host', timeout)
        return self.returncode

    def terminate(self):
        self.terminate_calls += 1
        self.returncode = -1
        self.ended.set()

    def kill(self):
        self.kill_calls += 1
        self.terminate()


def drained(controller):
    deadline = time.monotonic() + 2
    while not controller.snapshot()['drained'] and time.monotonic() < deadline:
        time.sleep(.005)
    assert controller.snapshot()['drained']


@pytest.mark.parametrize('operation', ['registry_write', 'update_defender_signatures',
                                      'remediate_defender_threats', 'enable_firewall'])
@pytest.mark.parametrize('reason', ['stop', 'timeout'])
def test_cancelled_native_change_keeps_exact_host_observed(monkeypatch, operation, reason):
    controller, child = EmergencyStop(), Child()
    original = child.communicate
    if reason == 'stop':
        def communicate(timeout=None):
            if child.calls == 0:
                child.calls += 1
                controller.request_stop()
                raise subprocess.TimeoutExpired('fixture-privacy-host', timeout)
            return original(timeout)
        child.communicate = communicate
    monkeypatch.setattr(privacy, 'os', SimpleNamespace(name='nt', environ={}))
    monkeypatch.setattr(privacy.subprocess, 'Popen', Mock(return_value=child))
    try:
        result = privacy._native_request(operation, {}, timeout=0 if reason == 'timeout' else 10,
                                         stop_controller=controller)
        assert result['ok'] is False and result['may_continue'] is True
        assert result['outcomeKnown'] is False and result['running'] is True
        assert result['cancelled'] is (reason == 'stop')
        assert result['timed_out'] is (reason == 'timeout')
        assert controller.snapshot()['native_pending'] == 1
        controller.request_stop()
        assert child.terminate_calls == child.kill_calls == 0
        child.returncode = 0
        child.ended.set()
        drained(controller)
        assert result['ok'] is False and result['outcomeKnown'] is False
        assert child.terminate_calls == child.kill_calls == 0
    finally:
        child.ended.set()


@pytest.mark.parametrize('operation', ['quick_scan', 'security_status', 'registry_write',
                                      'remediate_defender_threats'])
def test_stop_refuses_privacy_scan_and_mutation_before_any_child(monkeypatch, operation):
    controller, launch = EmergencyStop(), Mock()
    monkeypatch.setattr(privacy, 'os', SimpleNamespace(name='nt', environ={}))
    monkeypatch.setattr(privacy.subprocess, 'Popen', launch)
    controller.request_stop()
    with pytest.raises(StopRequested):
        privacy._native_request(operation, {}, stop_controller=controller)
    launch.assert_not_called()


def test_quick_scan_client_is_registered_as_disposable_before_read(monkeypatch):
    from optimus import defender_scan
    controller, child = EmergencyStop(), Child()
    monkeypatch.setattr(privacy, 'os', SimpleNamespace(name='nt', environ={}))
    monkeypatch.setattr(privacy.subprocess, 'Popen', Mock(return_value=child))
    def read(actual, **options):
        assert actual is child
        assert controller.snapshot()['disposable_running'] == 1
        assert controller.snapshot()['native_pending'] == 0
        child.returncode = 0
        child.ended.set()
        return {'ok': True, 'completion_verified': True}
    monkeypatch.setattr(defender_scan, 'read_scan_process', read)
    try:
        assert privacy._native_request('quick_scan', {}, stop_controller=controller)['ok'] is True
        drained(controller)
    finally:
        child.ended.set()


def test_unreadable_live_native_result_stays_observed_and_never_killed(monkeypatch):
    controller, child = EmergencyStop(), Child()
    original = child.communicate
    def communicate(timeout=None):
        if child.calls == 0:
            child.calls += 1
            return 'not json', ''
        return original(timeout)
    child.communicate = communicate
    monkeypatch.setattr(privacy, 'os', SimpleNamespace(name='nt', environ={}))
    monkeypatch.setattr(privacy.subprocess, 'Popen', Mock(return_value=child))
    try:
        with pytest.raises(ValueError):
            privacy._native_request('registry_write', {}, stop_controller=controller)
        assert controller.snapshot()['native_pending'] == 1
        controller.request_stop()
        assert child.terminate_calls == child.kill_calls == 0
        child.returncode = 1
        child.ended.set()
        drained(controller)
    finally:
        child.ended.set()


@pytest.mark.parametrize('output,code,raises', [('{"ok":true}', 0, False),
                                              ('{"ok":false}', 1, False),
                                              ('not json', 1, True)])
def test_real_native_exit_drains_observation_without_inventing_success(monkeypatch, output, code, raises):
    controller, child = EmergencyStop(), Child()
    child.returncode = code
    child.ended.set()
    child.communicate = lambda timeout=None: (output, '')
    monkeypatch.setattr(privacy, 'os', SimpleNamespace(name='nt', environ={}))
    monkeypatch.setattr(privacy.subprocess, 'Popen', Mock(return_value=child))
    if raises:
        with pytest.raises(ValueError):
            privacy._native_request('registry_write', {}, stop_controller=controller)
    else:
        result = privacy._native_request('registry_write', {}, stop_controller=controller)
        assert result['ok'] is (code == 0)
    assert controller.snapshot()['drained'] is True
    assert child.terminate_calls == child.kill_calls == 0


def test_backend_privacy_service_uses_the_lifetime_controller(tmp_path):
    backend = SimpleNamespace(data_dir=tmp_path)
    service = Backend._privacy_service(backend)
    assert Backend._privacy_service(backend) is service
    assert service.stop_controller is controller_for(backend)
    service.stop_controller.request_stop()
    with pytest.raises(StopRequested):
        service._call('registry_write', {})
    assert not service.directory.exists()


def test_injected_runner_still_observes_the_shared_stop_latch(tmp_path):
    controller, seen = EmergencyStop(), []
    def runner(operation, payload, **options):
        seen.append(options['cancel_event'])
        assert not options['cancel_event'].is_set()
        return {'ok': True}
    tool = privacy.SystemPrivacy(tmp_path, runner=runner, stop_controller=controller)
    assert tool._call('context')['ok'] is True
    controller.request_stop()
    assert seen[0].is_set()
    with pytest.raises(StopRequested):
        tool._call('registry_write', {})
    assert len(seen) == 1
