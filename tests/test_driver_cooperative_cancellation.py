"""Only private events, fake bridge clients and offline PowerShell WUA jobs."""
import json
import os
from pathlib import Path
import subprocess
import threading
import time
from unittest.mock import Mock

import pytest

from optimus.backend import Backend
from optimus.bridge import PowerShellBridge
from test_bridge import MockProcess


def wait_until(predicate):
    deadline = time.monotonic() + 3
    while not predicate():
        assert time.monotonic() < deadline, 'Mock request did not reach its checkpoint.'
        time.sleep(.005)


def client(tmp_path, monkeypatch):
    process = MockProcess()
    monkeypatch.setattr('optimus.bridge.subprocess.Popen', Mock(return_value=process))
    return PowerShellBridge(tmp_path), process


def directory_alias(target, alias):
    if os.name == 'nt':
        import _winapi
        _winapi.CreateJunction(str(target), str(alias))
    else:
        alias.symlink_to(target, target_is_directory=True)


def test_native_skip_marker_is_exact_owned_and_removed_only_after_terminal(tmp_path, monkeypatch):
    bridge, process = client(tmp_path, monkeypatch)
    stop = threading.Event()
    results = []
    worker = threading.Thread(target=lambda: results.append(bridge.request(
        'driver_update_apply', timeout=3, cancel_event=stop)))
    worker.start()
    try:
        wait_until(lambda: process.stdin.writes)
        request = json.loads(process.stdin.writes[0])
        assert request['arguments'] == {'cooperativeCancellation': True}
        marker = tmp_path / 'request-cancellation' / request['id'] / 'cancel.request'
        assert not marker.exists()
        stop.set()
        wait_until(marker.exists)
        assert worker.is_alive() and not process.terminated
        assert len(bridge._pending) == 1
        process.reply(result={'cancelled': True, 'outcomeKnown': True, 'running': False})
        worker.join(3)
        assert not worker.is_alive() and results[0]['cancelled']
        assert not marker.parent.exists() and not bridge._pending
        assert not process.terminated and process.wait_calls == 0
    finally:
        process.stdout.lines.put(None)
        worker.join(3)


def test_timeout_retains_marker_and_barrier_until_actual_late_reply(tmp_path, monkeypatch):
    bridge, process = client(tmp_path, monkeypatch)
    stop = threading.Event(); stop.set()
    try:
        with pytest.raises(TimeoutError):
            bridge.request('driver_update_apply', timeout=.02, cancel_event=stop)
        request = json.loads(process.stdin.writes[0])
        marker = tmp_path / 'request-cancellation' / request['id'] / 'cancel.request'
        assert marker.exists() and bridge._pending
        bridge.close()
        assert not process.terminated and not process.stdin.closed
        process.reply(result={'cancelled': True})
        wait_until(lambda: not bridge._pending)
        assert not marker.parent.exists()
    finally:
        process.stdout.lines.put(None)


def test_unsupported_native_cancellation_fails_before_process_start(tmp_path, monkeypatch):
    bridge, process = client(tmp_path, monkeypatch)
    with pytest.raises(ValueError, match='no supported'):
        bridge.request('maintenance_apply', cancel_event=threading.Event())
    assert bridge._process is None and not process.stdin.writes


def test_cancellation_root_alias_is_rejected_before_native_request(tmp_path, monkeypatch):
    data, external = tmp_path / 'data', tmp_path / 'external'
    data.mkdir(); external.mkdir()
    directory_alias(external, data / 'request-cancellation')
    bridge, process = client(data, monkeypatch)
    try:
        with pytest.raises(ValueError, match='Indirect'):
            bridge.request('driver_update_apply', cancel_event=threading.Event())
        assert not process.stdin.writes and list(external.iterdir()) == []
    finally:
        process.stdout.lines.put(None)


def test_replaced_cancel_directory_never_deletes_external_marker(tmp_path, monkeypatch):
    data, external = tmp_path / 'data', tmp_path / 'external'
    data.mkdir(); external.mkdir()
    protected = external / 'cancel.request'; protected.write_text('preserve', encoding='ascii')
    bridge, process = client(data, monkeypatch)
    stop = threading.Event(); stop.set()
    try:
        with pytest.raises(TimeoutError):
            bridge.request('driver_update_apply', timeout=.02, cancel_event=stop)
        request = json.loads(process.stdin.writes[0])
        directory = data / 'request-cancellation' / request['id']
        (directory / 'cancel.request').unlink(); directory.rmdir()
        directory_alias(external, directory)
        process.reply(result={'cancelled': True})
        wait_until(lambda: not bridge._pending)
        assert protected.read_text(encoding='ascii') == 'preserve'
        assert directory.exists()
    finally:
        process.stdout.lines.put(None)


@pytest.mark.parametrize('method', ['driver_update_apply', 'driver_update_apply_unattended'])
def test_driver_backend_forwards_local_token_only_in_memory(method):
    backend = Backend.__new__(Backend)
    backend.preferences = Mock(return_value={})
    backend.bridge = Mock()
    token = threading.Event()
    options = {'accept_eula': True} if method.endswith('unattended') else {}
    getattr(backend, method)({'id': 'fixture', 'revision': 1}, cancel_event=token, **options)
    assert backend.bridge.request.call_args.kwargs['cancel_event'] is token


def test_cancelled_search_timeout_keeps_unknown_running_barrier():
    backend = Backend.__new__(Backend)
    backend.bridge = Mock()
    backend.bridge.request.side_effect = TimeoutError('native request still pending')
    backend.driver_coverage = Mock()
    token = threading.Event(); token.set()
    result = backend.driver_updates_scan(cancel_event=token)
    assert result['cancelled'] and result['outcomeKnown'] is False and result['running'] is True
    backend.driver_coverage.assert_not_called()


@pytest.mark.skipif(os.name != 'nt', reason='Windows PowerShell 5.1 fake-WUA fixtures.')
def test_offline_driver_abort_waits_for_terminal_and_never_aborts_install(tmp_path):
    script = Path(__file__).with_name('Test-DriverCancellation.ps1')
    executable = Path(os.environ['SystemRoot']) / 'System32/WindowsPowerShell/v1.0/powershell.exe'
    completed = subprocess.run([str(executable), '-NoLogo', '-NoProfile', '-NonInteractive',
        '-ExecutionPolicy', 'Bypass', '-File', str(script), '-FixtureRoot', str(tmp_path)],
        capture_output=True, text=True, timeout=45)
    assert completed.returncode == 0, completed.stdout + completed.stderr
    result = json.loads(completed.stdout)
    assert result['Result'] == 'PASS' and result['Assertions'] >= 12
    assert result['RealScans'] == result['RealDownloads'] == result['RealInstalls'] == 0
