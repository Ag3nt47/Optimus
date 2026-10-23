"""A new session retries terminal failures without erasing recovery evidence."""
from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from optimus.parallel_updates import ParallelAppUpdates, ProgressMux, WINDOWS_SOFTWARE


def backend(directory):
    return SimpleNamespace(data_dir=directory, system='win32', bridge=None)


@pytest.mark.parametrize('failure', [
    {'exitCode': 1, 'outcomeKnown': True, 'installer_started': True, 'running': False,
     'failure_scope': 'package', 'note': 'Terminal fixture installer failure.'},
    {'exitCode': 1, 'outcomeKnown': True, 'installer_started': False, 'running': False,
     'failure_scope': 'package', 'note': 'Terminal fixture pre-install refusal.'},
])
def test_new_run_uses_fresh_work_after_a_known_failed_attempt_and_keeps_history(tmp_path, failure):
    service = backend(tmp_path)
    calls = []
    def operation(worker, progress):
        calls.append('fresh_scan')
        progress({'id': 'Fixture.App', 'current': 1, 'total': 1})
        return {'failed_items': 1, 'items': [{'id': 'Fixture.App', 'status': 'failed', 'result': failure}]}
    with patch('optimus.speed_enhancements._current_owner', return_value='fixture-owner'):
        old = ParallelAppUpdates(service, 'a' * 32, WINDOWS_SOFTWARE, operation, ProgressMux(lambda _: None))
        old.start()
        old.join()
        original = old.path.read_bytes()
        assert json.loads(original)['status'] == 'needs_review'
        assert calls == ['fresh_scan']
        def retry(worker, progress):
            calls.append('fresh_scan_again')
            return {'failed_items': 0, 'items': [], 'installer_started': False,
                    'outcomeKnown': True, 'running': False, 'review_only': True}
        fresh = ParallelAppUpdates(service, 'b' * 32, WINDOWS_SOFTWARE, retry, ProgressMux(lambda _: None))
        assert fresh.path != old.path
        assert fresh.record['installer_prepared'] is False
        assert not fresh.stop_event.is_set() and fresh.result is None
        assert fresh._pending() is None
        fresh.start()
        assert fresh.join()['status'] == 'completed'
        assert calls == ['fresh_scan', 'fresh_scan_again']
        assert old.path.read_bytes() == original


def test_new_session_does_not_forget_an_unresolved_or_running_installer(tmp_path):
    service = backend(tmp_path)
    directory = tmp_path / 'parallel-updates'
    directory.mkdir()
    path = directory / ('a' * 32 + '.json')
    original = json.dumps({'schema': 1, 'id': path.stem, 'owner': 'fixture-owner',
        'status': 'uncertain', 'installer_prepared': True,
        'result': {'running': True, 'outcomeKnown': False}}).encode()
    path.write_bytes(original)
    called = []
    with patch('optimus.speed_enhancements._current_owner', return_value='fixture-owner'):
        fresh = ParallelAppUpdates(service, 'b' * 32, WINDOWS_SOFTWARE,
            lambda *_: called.append(True), ProgressMux(lambda _: None))
        fresh.start()
        assert fresh.join()['result']['blocked_by_prior_installer'] is True
    assert not called and path.read_bytes() == original
