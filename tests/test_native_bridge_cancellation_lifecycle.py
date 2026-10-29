"""Force cancellation/terminal races using fake hosts and disposable markers."""
import json
import threading
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from optimus.bridge import PowerShellBridge
from optimus.emergency_stop import controller_for
from test_bridge import MockProcess
from test_native_emergency_stop_backend import wait_until


def fixture(tmp_path, monkeypatch):
    process = MockProcess()
    monkeypatch.setattr('optimus.bridge.subprocess.Popen', Mock(return_value=process))
    backend = SimpleNamespace(bridge=PowerShellBridge(tmp_path))
    controller = controller_for(backend)
    return backend.bridge, process, controller


@pytest.mark.parametrize('active_request', [False, True])
@pytest.mark.parametrize('iteration', range(8))
def test_terminal_waits_for_marker_identity_publication_and_no_late_writer(
        tmp_path, monkeypatch, active_request, iteration):
    bridge, process, controller = fixture(tmp_path, monkeypatch)
    opened, release, clearing, cleared = (threading.Event() for _ in range(4))
    worker, errors, results = None, [], []
    original_open = Path.open
    original_clear = bridge._clear_cancellation_marker
    creates = []

    class PausedMarker:
        def __init__(self, stream):
            self.stream = stream

        def __enter__(self):
            result = self.stream.__enter__()
            opened.set()  # File exists, but no identity has been published yet.
            if not release.wait(10):
                self.stream.close()
                raise AssertionError('Fixture marker publication was not released.')
            return result

        def __exit__(self, *arguments):
            return self.stream.__exit__(*arguments)

    def open_file(path, *args, **kwargs):
        stream = original_open(path, *args, **kwargs)
        if path.name == 'cancel.request' and args[:1] == ('x',):
            creates.append(path)
            return PausedMarker(stream)
        return stream

    def clear(identity):
        clearing.set()
        try:
            return original_clear(identity)
        finally:
            cleared.set()

    monkeypatch.setattr(Path, 'open', open_file)
    monkeypatch.setattr(bridge, '_clear_cancellation_marker', clear)
    try:
        if active_request:
            def request():
                try:
                    results.append(bridge.request('driver_update_apply', timeout=8))
                except Exception as error:
                    errors.append(error)
            worker = threading.Thread(target=request)
            worker.start()
            wait_until(lambda: bool(process.stdin.writes))
        else:
            with pytest.raises(TimeoutError):
                bridge.request('driver_update_apply', timeout=.02)
        identity = json.loads(process.stdin.writes[0])['id']
        marker = tmp_path / 'request-cancellation' / identity / 'cancel.request'
        with controller.lock:
            late_callback = next(iter(controller._operations.values())).cancel
        controller.request_stop()
        assert opened.wait(5) and marker.exists()
        assert not process.terminated and identity in bridge._pending
        process.reply(result={'cancelled': True, 'installer_started': True,
                              'outcomeKnown': True, 'running': False})
        assert clearing.wait(5)
        assert not cleared.wait(.05)  # Retirement must wait for the same lock.
        assert not controller.snapshot()['drained']
        assert not process.terminated  # The native receipt is still retiring.
        release.set()
        wait_until(lambda: controller.snapshot()['drained'])
        if worker is not None:
            worker.join(5)
            assert not worker.is_alive() and not errors
            assert results[0]['cancelled'] is True
        assert cleared.is_set() and not bridge._pending
        assert not marker.exists() and not marker.parent.exists()
        assert identity not in bridge._cancellation_paths
        late_callback()  # Captured before terminal; may run after retirement.
        assert len(creates) == 1 and not marker.parent.exists()
    finally:
        release.set()
        if worker is not None:
            worker.join(10)
        if controller._stop_thread is not None:
            controller._stop_thread.join(10)
        process.stdout.lines.put(None)


def test_failed_ownership_cleanup_still_closes_cancellation_lifecycle(tmp_path, monkeypatch):
    bridge, process, controller = fixture(tmp_path, monkeypatch)
    try:
        with pytest.raises(TimeoutError):
            bridge.request('driver_update_apply', timeout=.02)
        identity = json.loads(process.stdin.writes[0])['id']
        marker = tmp_path / 'request-cancellation' / identity / 'cancel.request'
        marker.write_text('preserve substituted marker', encoding='ascii')
        with controller.lock:
            late_callback = next(iter(controller._operations.values())).cancel
        process.reply(result={'cancelled': True, 'installer_started': False,
                              'outcomeKnown': True, 'running': False})
        wait_until(lambda: controller.snapshot()['drained'])
        assert marker.read_text(encoding='ascii') == 'preserve substituted marker'
        assert bridge._cancellation_paths[identity]['closed'] is True
        marker.unlink()  # Fixture removes its own substitution after preservation.
        late_callback()
        assert not marker.exists()  # A retired request cannot recreate it.
        assert not bridge._pending
    finally:
        process.stdout.lines.put(None)
