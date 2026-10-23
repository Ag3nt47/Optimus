"""Real driver engine refusal shapes cross the production queue using fake WUA only."""
import copy
import json
import os
from pathlib import Path
import subprocess
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from optimus.automated_system import driver_descriptor, run_drivers
from optimus.automation import RunLease
from optimus.parallel_updates import (ParallelInstallerQueue, ProgressMux, WINDOWS_DRIVERS,
    pending_updates_barrier, update_completion_state)
from test_automated_system import backend, driver, driver_config


@pytest.fixture(autouse=True)
def fixture_owner(monkeypatch):
    monkeypatch.setattr('optimus.speed_enhancements._current_owner', lambda: 'fixture-owner')
    monkeypatch.setattr('optimus.runtime_priority.lower_worker_priority', lambda: lambda: None)


@pytest.fixture(scope='module')
def native_receipts(tmp_path_factory):
    if os.name != 'nt':
        pytest.skip('Native Windows PowerShell fake-WUA engine fixture.')
    directory = tmp_path_factory.mktemp('driver-preflight-native')
    output = directory / 'receipts.json'
    script = Path(__file__).with_name('Test-DriverPreflight.ps1')
    executable = Path(os.environ['SystemRoot']) / 'System32/WindowsPowerShell/v1.0/powershell.exe'
    result = subprocess.run([str(executable), '-NoLogo', '-NoProfile', '-NonInteractive',
        '-ExecutionPolicy', 'Bypass', '-File', str(script), '-ReceiptsPath', str(output)],
        capture_output=True, text=True, timeout=45)
    assert result.returncode == 0, result.stdout + result.stderr
    summary = json.loads(result.stdout)
    assert summary['Result'] == 'PASS' and summary['Assertions'] >= 50
    assert summary['RealScans'] == summary['RealDownloads'] == summary['RealInstalls'] == 0
    return json.loads(output.read_text(encoding='utf-8-sig'))


def success():
    return dict(resultCode=2, aggregateResultCode=2, hresult=0, aggregateHresult=0,
        installer_started=True, outcomeKnown=True, running=False, restartNeeded=False)


def exercise_queue(tmp_path, middle):
    service = backend()
    service.data_dir, service.system, service.bridge = tmp_path, 'win32', SimpleNamespace(_process=None)
    offers = [driver(id='00000000-0000-0000-0000-000000000002'), driver(),
              driver(id='00000000-0000-0000-0000-000000000003')]
    service.driver_updates_scan.return_value = {'resultCode': 2, 'updates': offers}
    config = driver_config(driver_approved_updates=[driver_descriptor(value) for value in offers])
    called = []
    def apply(record, **kwargs):
        called.append(record['id'])
        if record['id'] == offers[1]['id']:
            if isinstance(middle, Exception):
                raise middle
            return copy.deepcopy(middle)
        return success()
    service.driver_update_apply_unattended.side_effect = apply
    events = []
    def operation(worker, callback, prepared, before):
        return run_drivers(worker, config, item_callback=callback,
            prepared_callback=prepared, before_install=before)
    queue = ParallelInstallerQueue(service, 'a' * 32,
        [(WINDOWS_DRIVERS, operation)], ProgressMux(events.append))
    with RunLease(tmp_path):
        queue.start()
        try:
            assert queue.wait_prepared() is None
            queue.release_maintenance()
            queue.join()
        finally:
            queue.cancel()
            queue.join()
    return service, offers, queue, called, events


@pytest.mark.parametrize('kind', ['package_unavailable', 'version_changed', 'unsupported_installer', 'download_failed'])
def test_known_package_preflight_advances_next_driver_and_preserves_failure_journal(tmp_path, kind):
    refusal = dict(resultCode=4, aggregateResultCode=4, hresult=0, aggregateHresult=0,
        installer_started=False, outcomeKnown=True, running=False, restartNeeded=False,
        requiresReview=True, failure_scope='package', failureKind=kind, note='Fixture exact package refusal.')
    service, offers, queue, called, _ = exercise_queue(tmp_path, refusal)
    assert called == [row['id'] for row in offers]
    result = queue.jobs[0].result
    assert [row['status'] for row in result['items']] == ['applied', 'failed', 'applied']
    assert result['deferred'] == [] and queue.barrier is None
    assert result['installer_started'] is True and result['outcomeKnown'] is True and result['running'] is False
    assert pending_updates_barrier(service) is None
    saved = json.loads(queue.jobs[0].path.read_text())
    assert saved['status'] == 'needs_review'
    assert saved['result']['items'][1]['result']['failureKind'] == kind
    state = update_completion_state(result)
    assert state['driver_counts']['installed'] == 2 and state['driver_counts']['failed'] == 1


@pytest.mark.parametrize('name', ['missing', 'ambiguous', 'changed-title', 'changed-hardwareId',
    'changed-provider', 'changed-driverClass', 'changed-manufacturer', 'changed-model',
    'changed-driverDate', 'interaction', 'quiet', 'download-3', 'download-4', 'download-5'])
def test_actual_native_engine_refusals_continue_the_production_queue(tmp_path, native_receipts, name):
    refusal = native_receipts[name]
    service, offers, queue, called, _ = exercise_queue(tmp_path, refusal)
    assert called == [row['id'] for row in offers]
    assert queue.barrier is None and pending_updates_barrier(service) is None
    assert queue.jobs[0].result['items'][1]['result'] == refusal
    assert queue.jobs[0].result['deferred'] == []


@pytest.mark.parametrize('code', [-2147024891, -2147024784, -2145124330, -2145124321])
def test_shared_native_hresult_overrides_package_label_and_blocks_later_driver(tmp_path, code):
    refusal = dict(resultCode=4, aggregateResultCode=4, hresult=code, aggregateHresult=code,
        installer_started=False, outcomeKnown=True, running=False, restartNeeded=False,
        requiresReview=True, failure_scope='package', failureKind='download_failed')
    _, offers, queue, called, _ = exercise_queue(tmp_path, refusal)
    assert called == [offers[0]['id'], offers[1]['id']]
    assert queue.barrier and len(queue.jobs[0].result['deferred']) == 1
    assert queue.jobs[0].result['items'][1]['result']['shared_barrier'] is True


def test_actual_wrapped_quiet_authorization_hresult_stays_shared(tmp_path, native_receipts):
    refusal = native_receipts['shared-quiet']
    assert refusal['hresult'] == -2147024891
    _, offers, queue, called, _ = exercise_queue(tmp_path, refusal)
    assert called == [offers[0]['id'], offers[1]['id']] and queue.barrier


def test_unknown_exception_after_install_call_stays_uncertain_and_stops_queue(tmp_path):
    service, offers, queue, called, _ = exercise_queue(tmp_path, RuntimeError('Fixture unknown native COM operation.'))
    assert called == [offers[0]['id'], offers[1]['id']]
    assert queue.barrier and pending_updates_barrier(service)
    assert queue.jobs[0].result['items'][1]['uncertain'] is True
    saved = json.loads(queue.jobs[0].path.read_text())
    assert saved['status'] == 'uncertain'
