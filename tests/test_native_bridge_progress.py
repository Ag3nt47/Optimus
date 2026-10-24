"""Request-bound progress transport with in-memory processes only."""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import re
import threading
import time
from types import SimpleNamespace
from unittest.mock import Mock

import pytest


SPEC = importlib.util.spec_from_file_location('optimus_bridge_progress_fakes',
    Path(__file__).with_name('test_bridge.py'))
fakes = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(fakes)
bridge_module = fakes.bridge_module


def wait_for(condition):
    deadline = time.monotonic() + 3
    while not condition():
        if time.monotonic() >= deadline:
            raise AssertionError('The in-memory bridge did not reach the expected boundary.')
        time.sleep(0.002)


def pending(bridge):
    with bridge._pending_lock:
        return dict(bridge._pending)


def frame(process, value):
    process.stdout.lines.put(json.dumps(value) + '\n')


def tick(process, request_id, phase='downloading', percent=25):
    frame(process, {'kind': 'progress', 'id': request_id,
                    'progress': {'phase': phase, 'percent': percent}})


class HeldProcess(fakes.MockProcess):
    """EOF does not imply this fake native owner exited."""
    def __init__(self):
        super().__init__()
        self.exited = threading.Event()
        self.wait_started = threading.Event()

    def wait(self, timeout=None):
        self.wait_calls += 1
        self.wait_started.set()
        if not self.exited.wait(timeout):
            raise bridge_module.subprocess.TimeoutExpired('fixture-engine', timeout)
        return self.returncode

    def finish(self):
        self.returncode = 0
        self.exited.set()


@pytest.fixture
def transport(monkeypatch, tmp_path):
    state = SimpleNamespace(process=HeldProcess(), tasks=[], releases=[])
    state.popen = Mock(return_value=state.process)
    monkeypatch.setattr(bridge_module.subprocess, 'Popen', state.popen)
    state.bridge = bridge_module.PowerShellBridge(tmp_path / 'fake-data')
    yield state
    for release in state.releases:
        release.set()
    state.process.finish()
    state.process.stdout.lines.put(None)
    for task in state.tasks:
        task.thread.join(3)
        assert not task.thread.is_alive()
    state.bridge.close()


def request_in_thread(state, callback=None):
    task = SimpleNamespace(result=[], errors=[])
    before = len(state.process.stdin.writes)

    def run():
        try:
            task.result.append(state.bridge.request('fixture_native_operation', timeout=5,
                progress_callback=callback))
        except BaseException as error:
            task.errors.append(error)

    task.thread = threading.Thread(target=run, daemon=True)
    state.tasks.append(task)
    task.thread.start()
    wait_for(lambda: len(state.process.stdin.writes) > before)
    task.request_id = json.loads(state.process.stdin.writes[-1])['id']
    assert re.fullmatch('[a-f0-9]{32}', task.request_id)
    return task


def complete(state, task, result=None):
    state.process.reply(result={'fixture_done': True} if result is None else result,
                        request_id=task.request_id)
    task.thread.join(3)
    assert not task.thread.is_alive() and not task.errors


def test_progress_phase_frames_never_complete_or_release_the_pending_request(transport):
    state, seen, pending_during_tick = transport, [], []

    def observe(value):
        seen.append(value)
        pending_during_tick.append(pending(state.bridge))

    task = request_in_thread(state, observe)
    tick(state.process, task.request_id)
    wait_for(lambda: len(seen) == 1 and len(pending_during_tick) == 1)
    assert seen == [{'phase': 'downloading', 'percent': 25.}]
    assert task.request_id in pending_during_tick[0] and task.request_id in pending(state.bridge)
    assert not task.result and state.bridge._replies.empty()
    tick(state.process, task.request_id, 'installing', None)
    wait_for(lambda: len(seen) == 2)
    assert seen[-1] == {'phase': 'installing', 'percent': None}
    complete(state, task, {'native_result': 'fixture terminal receipt'})
    assert task.result == [{'native_result': 'fixture terminal receipt'}]
    assert not pending(state.bridge) and len(state.process.stdin.writes) == 1
    assert not state.process.terminated


@pytest.mark.parametrize('identity', [[], {}, None, 17, 'not-a-request', 'A' * 32])
@pytest.mark.parametrize('kind', ['progress', 'result'])
def test_malformed_frame_identities_are_ignored_without_killing_the_reader(transport, identity, kind):
    seen = []
    task = request_in_thread(transport, seen.append)
    frame(transport.process, {'kind': kind, 'id': identity, 'ok': True,
        'result': {'forged': True}, 'progress': {'phase': 'unrelated', 'percent': 99}})
    tick(transport.process, task.request_id, 'installing', 41)
    wait_for(lambda: seen)
    assert seen == [{'phase': 'installing', 'percent': 41.}]
    assert task.request_id in pending(transport.bridge)
    complete(transport, task)
    assert task.result == [{'fixture_done': True}]


def test_unrelated_valid_nonce_and_malformed_progress_do_not_change_current_item(transport):
    seen = []
    task = request_in_thread(transport, seen.append)
    tick(transport.process, '0' * 32, 'unrelated', 99)
    for value in (None, [], {'phase': []}, {'phase': 'X' * 100, 'percent': 10}):
        frame(transport.process, {'kind': 'progress', 'id': task.request_id, 'progress': value})
    tick(transport.process, task.request_id, 'downloading', 12)
    wait_for(lambda: seen)
    assert seen == [{'phase': 'downloading', 'percent': 12.}]
    complete(transport, task)


def test_exact_native_removal_item_identity_survives_progress_transport(transport):
    seen = []
    task = request_in_thread(transport, seen.append)
    value = {'key': 'debloat:consumer-apps:appx-fixture', 'label': 'Fixture app',
             'phase': 'uninstall', 'percent': 41}
    frame(transport.process, {'kind': 'progress', 'id': task.request_id, 'progress': value})
    wait_for(lambda: seen)
    assert seen == [dict(value, percent=41.0)]
    assert task.request_id in pending(transport.bridge)
    complete(transport, task)


@pytest.mark.parametrize('fields', [{'key': 'A'}, {'label': 'A'}, {'key': '', 'label': 'A'},
                                  {'key': 'A', 'label': None}])
def test_partial_or_invalid_native_item_identity_is_not_replaced_by_generic_identity(transport, fields):
    seen = []
    task = request_in_thread(transport, seen.append)
    frame(transport.process, {'kind': 'progress', 'id': task.request_id,
        'progress': dict(fields, phase='uninstall', percent=77)})
    tick(transport.process, task.request_id, 'checking', 12)
    wait_for(lambda: seen)
    assert seen == [{'phase': 'checking', 'percent': 12.0}]
    complete(transport, task)


def test_callback_exception_cannot_abort_or_replay_the_native_operation(transport):
    observer = Mock(side_effect=RuntimeError('Disposable observer failure.'))
    task = request_in_thread(transport, observer)
    tick(transport.process, task.request_id)
    wait_for(lambda: observer.called)
    assert task.request_id in pending(transport.bridge)
    complete(transport, task, {'exitCode': 1603, 'outcomeKnown': True})
    assert task.result == [{'exitCode': 1603, 'outcomeKnown': True}]
    assert len(transport.process.stdin.writes) == 1 and not transport.process.terminated


def test_slow_callback_keeps_one_latest_tick_and_does_not_block_pipe_reading(transport):
    entered, release, seen = threading.Event(), threading.Event(), []
    transport.releases.append(release)

    def observer(value):
        seen.append(value)
        if len(seen) == 1:
            entered.set()
            release.wait(3)

    task = request_in_thread(transport, observer)
    tick(transport.process, task.request_id, percent=0)
    wait_for(entered.is_set)
    for number in range(1000):
        tick(transport.process, task.request_id, 'installing', number % 100)
    wait_for(lambda: transport.bridge._progress.get(task.request_id, {}).get('percent') == 99)
    assert len(transport.bridge._progress) == 1 and transport.bridge._replies.empty()
    transport.process.reply(result={'native_done': True}, request_id=task.request_id)
    wait_for(lambda: not pending(transport.bridge))
    assert task.thread.is_alive() and not transport.process.terminated
    release.set()
    task.thread.join(3)
    assert not task.thread.is_alive() and not task.errors
    assert task.result == [{'native_done': True}]
    assert seen == [{'phase': 'downloading', 'percent': 0.}, {'phase': 'installing', 'percent': 99.}]
    assert not transport.bridge._progress


def test_timeout_keeps_native_pending_and_deactivates_old_callbacks(transport):
    old = Mock()
    with pytest.raises(TimeoutError):
        transport.bridge.request('fixture_native_operation', timeout=0.02, progress_callback=old)
    old_id = json.loads(transport.process.stdin.writes[-1])['id']
    assert old_id in pending(transport.bridge) and not transport.bridge._progress_active
    transport.bridge.close()
    assert not transport.process.stdin.closed and not transport.process.terminated
    assert transport.process.wait_calls == 0
    fresh = []
    task = request_in_thread(transport, fresh.append)
    assert task.request_id != old_id
    tick(transport.process, old_id, 'installing', 99)
    transport.process.reply(result={'old_terminal': True}, request_id=old_id)
    tick(transport.process, task.request_id, 'checking', 45)
    wait_for(lambda: fresh)
    assert fresh == [{'phase': 'checking', 'percent': 45.}]
    old.assert_not_called()
    complete(transport, task)
    assert not pending(transport.bridge) and len(transport.process.stdin.writes) == 2


def test_unsolicited_progress_without_an_observer_is_not_a_terminal_result(transport):
    task = request_in_thread(transport)
    tick(transport.process, task.request_id, 'installing', 75)
    wait_for(lambda: transport.process.stdout.lines.empty())
    assert not transport.bridge._progress and transport.bridge._replies.empty()
    assert task.request_id in pending(transport.bridge)
    complete(transport, task)


@pytest.mark.parametrize('callback', [42, True, 'callback', {}, []])
def test_invalid_observer_is_rejected_before_starting_any_engine(transport, callback):
    with pytest.raises(ValueError, match='callable'):
        transport.bridge.request('fixture_native_operation', progress_callback=callback)
    transport.popen.assert_not_called()
    assert not pending(transport.bridge)


def test_stdout_eof_keeps_pending_until_the_exact_native_owner_really_exits(transport):
    task = request_in_thread(transport, Mock())
    transport.process.stdout.lines.put(None)
    wait_for(transport.process.wait_started.is_set)
    task.thread.join(3)
    assert not task.thread.is_alive() and len(task.errors) == 1
    assert 'unverified' in str(task.errors[0])
    assert task.request_id in pending(transport.bridge)
    transport.bridge.close()
    assert not transport.process.stdin.closed and not transport.process.terminated
    assert transport.process.wait_calls == 1
    other_id = 'b' * 32
    with transport.bridge._pending_lock:
        transport.bridge._pending[other_id] = ('different_native_owner', transport.process.pid + 1)
    transport.process.finish()
    wait_for(lambda: task.request_id not in pending(transport.bridge))
    assert other_id in pending(transport.bridge)
    with transport.bridge._pending_lock:
        transport.bridge._pending.pop(other_id)
    transport.bridge.close()
    assert not transport.process.terminated


def test_startup_eof_before_pending_registration_still_tracks_the_native_owner(transport, monkeypatch):
    original = transport.bridge._start

    def start_then_lose_stdout():
        original()
        transport.process.stdout.lines.put(None)
        wait_for(transport.process.wait_started.is_set)
        assert not pending(transport.bridge)

    monkeypatch.setattr(transport.bridge, '_start', start_then_lose_stdout)
    with pytest.raises(RuntimeError, match='unverified'):
        transport.bridge.request('fixture_native_operation', timeout=1)
    request_id = json.loads(transport.process.stdin.writes[-1])['id']
    assert request_id in pending(transport.bridge)
    transport.bridge.close()
    assert not transport.process.stdin.closed and not transport.process.terminated
    transport.process.finish()
    wait_for(lambda: not pending(transport.bridge))
