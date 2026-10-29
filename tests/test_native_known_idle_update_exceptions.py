"""Retired exception paths use fake installers and disposable exact journals."""
import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from optimus.automated_software import run_software
from optimus.automated_system import run_drivers, driver_descriptor
from optimus.continue_updates import run_saved_updates
from optimus.navigation import can_fail_forward, update_exception_receipt
from optimus.parallel_updates import ParallelAppUpdates, ProgressMux, WINDOWS_SOFTWARE, pending_updates_barrier, installer_barrier
from optimus.update_recovery import finished_package_attempt
from test_automated_software import backend as apps, configuration
from test_automated_system import backend as drivers, driver, driver_config
from test_continue_updates import service, assert_saved


def failure(**changes):
    error = RuntimeError('Fixture exact installer failed after verified native retirement')
    values = dict(installer_started=True, outcomeKnown=True, running=False,
        failure_scope='package', exitCode=1, restartNeeded=False)
    values.update(changes)
    for key, value in values.items():
        setattr(error, key, value)
    return error


@pytest.fixture(autouse=True)
def fixture_identity(monkeypatch):
    monkeypatch.setattr('optimus.speed_enhancements._current_owner', lambda: 'uid:1234')
    monkeypatch.setattr('optimus.runtime_priority.lower_worker_priority', lambda: lambda: None)


@pytest.mark.parametrize('started', [False, True])
def test_package_exception_preserves_known_lifecycle_and_native_fields(started):
    error = failure(installer_started=started, hresult=-2145116156, aggregateHresult=-2145116156,
        resultCode=4, aggregateResultCode=4)
    receipt = update_exception_receipt(error)
    assert receipt['installer_started'] is started and receipt['outcomeKnown'] is True
    assert receipt['running'] is False and receipt['requiresReview'] is True
    assert receipt['hresult'] == -2145116156 and receipt['resultCode'] == 4
    assert can_fail_forward(receipt)


@pytest.mark.parametrize('change', [
    {'running': True}, {'running': None}, {'running': 0}, {'outcomeKnown': False},
    {'installer_started': None}, {'installer_started': 1}, {'uncertain': True},
    {'restartNeeded': True}, {'restartNeeded': 1}, {'reboot_required': True}, {'rebootRequired': True},
    {'exitCode': 3010}, {'exitCode': '1'}, {'hresult': '0'}, {'resultCode': True},
    {'hresult': 0x80070005}, {'failure_scope': 'shared'}, {'failure_scope': 'unknown'},
])
def test_active_unknown_restart_shared_and_malformed_exception_evidence_never_fail_forward(change):
    receipt = update_exception_receipt(failure(**change))
    assert not can_fail_forward(receipt)
    if 'hresult' in change:
        assert receipt['hresult'] == change['hresult']
    if 'exitCode' in change:
        assert receipt['exitCode'] == change['exitCode']
    if change.get('reboot_required') is True or change.get('rebootRequired') is True:
        assert receipt['restartNeeded'] is True and installer_barrier(receipt)


@pytest.mark.parametrize('kind', ['software', 'driver'])
def test_retired_item_exception_continues_to_next_approved_item(kind):
    if kind == 'software':
        backend = apps()
        backend.update_app_unattended.side_effect = [failure(), {'exitCode': 0, 'verified': True,
            'installer_started': True, 'outcomeKnown': True, 'running': False}]
        result = run_software(backend, configuration())
        assert backend.update_app_unattended.call_count == 2 and result['deferred_ids'] == []
    else:
        backend = drivers()
        second = driver(id='00000000-0000-0000-0000-000000000002', title='Second fixture')
        backend.driver_updates_scan.return_value = {'resultCode': 2, 'updates': [driver(), second]}
        success = dict(backend.driver_update_apply_unattended.return_value)
        backend.driver_update_apply_unattended.side_effect = [failure(resultCode=4, aggregateResultCode=4,
            hresult=-2145116156, aggregateHresult=-2145116156), success]
        result = run_drivers(backend, driver_config(driver_approved_updates=[driver_descriptor(driver()), driver_descriptor(second)]))
        assert backend.driver_update_apply_unattended.call_count == 2 and result['deferred'] == []
    assert result['items'][0]['status'] == 'failed'
    assert result['items'][0]['result']['installer_started'] is True
    assert result['items'][0]['result']['outcomeKnown'] is True
    assert result['outcomeKnown'] is True


@pytest.mark.parametrize('change', [{'installer_started': False, 'running': True},
    {'outcomeKnown': False}, {'restartNeeded': True}, {'exitCode': '1'}])
@pytest.mark.parametrize('kind', ['software', 'driver'])
def test_unsafe_item_exceptions_keep_remaining_items_deferred(kind, change):
    error = failure(**change)
    if kind == 'software':
        backend = apps()
        backend.update_app_unattended.side_effect = error
        result = run_software(backend, configuration())
        assert backend.update_app_unattended.call_count == 1 and result['deferred_ids'] == ['Vendor.Two']
    else:
        backend = drivers()
        second = driver(id='00000000-0000-0000-0000-000000000002')
        backend.driver_updates_scan.return_value = {'resultCode': 2, 'updates': [driver(), second]}
        backend.driver_update_apply_unattended.side_effect = error
        result = run_drivers(backend, driver_config(driver_approved_updates=[driver_descriptor(driver()), driver_descriptor(second)]))
        assert backend.driver_update_apply_unattended.call_count == 1 and result['deferred'][0]['id'] == second['id']


@pytest.mark.parametrize('native', [False, True])
def test_continuation_saves_retired_failure_and_finishes_next_without_restart_or_orphan(tmp_path, native):
    backend = service(tmp_path, native=native)
    original = backend.update_app_unattended
    def update(identity, **options):
        result = original(identity, **options)
        if identity == 'Fixture.One':
            raise failure()
        return result
    backend.update_app_unattended = update
    report = run_saved_updates(backend)
    assert 'update:Fixture.One' in backend.calls and 'update:Fixture.Two' in backend.calls
    assert report['status'] == 'completed_with_skips'
    assert report['queue_drained'] and pending_updates_barrier(backend) is None
    assert_saved(report)
    records = [json.loads(path.read_bytes()) for path in (tmp_path / 'parallel-updates').glob('*.json')]
    assert all(finished_package_attempt(record) for record in records)


@pytest.mark.parametrize('alias', ['reboot_required', 'rebootRequired'])
@pytest.mark.parametrize('native', [False, True])
def test_continuation_restart_alias_keeps_shared_installer_gate_closed(tmp_path, alias, native):
    backend = service(tmp_path, native=native)
    backend.outcome = failure(**{alias: True})
    report = run_saved_updates(backend)
    assert 'update:Fixture.One' in backend.calls and 'update:Fixture.Two' not in backend.calls
    assert report['status'] == 'blocked' and not report['queue_drained']
    assert report['installation_block_reason'] and pending_updates_barrier(backend)


@pytest.mark.parametrize('code', [0x8A150109, 0x8A15010A, 0x8A15010B,
    0x8A150109 - 0x100000000, 0x8A15010A - 0x100000000, 0x8A15010B - 0x100000000])
def test_native_winget_restart_codes_override_a_benign_flag_and_stop_continuation(tmp_path, code):
    error = failure(exitCode=code, restartNeeded=False)
    receipt = update_exception_receipt(error)
    assert receipt['exitCode'] == code and receipt['restartNeeded'] is True
    assert not can_fail_forward(receipt) and installer_barrier(receipt)
    backend = service(tmp_path)
    backend.outcome = error
    report = run_saved_updates(backend)
    assert report['status'] == 'blocked' and 'update:Fixture.Two' not in backend.calls
    assert pending_updates_barrier(backend)


@pytest.mark.parametrize('mode', ['active', 'unknown', 'retired_without_identity', 'untyped_discovery'])
def test_outer_unbound_native_metadata_cannot_disappear_at_next_startup(tmp_path, mode):
    backend = SimpleNamespace(data_dir=tmp_path, system='win32', bridge=None)
    error = (RuntimeError('Fixture source discovery failed') if mode == 'untyped_discovery' else
        failure(outcomeKnown=False, running=True) if mode == 'active' else
        failure(outcomeKnown=False) if mode == 'unknown' else failure())
    def operation(worker, progress):
        raise error  # No item boundary/identity was granted.
    job = ParallelAppUpdates(backend, 'a' * 32, WINDOWS_SOFTWARE, operation, ProgressMux(lambda event: None))
    job.start()
    job.join()
    record = json.loads(job.path.read_bytes())
    assert (pending_updates_barrier(backend) is None) is (mode == 'untyped_discovery')
    if mode != 'untyped_discovery':
        assert record['result']['outcomeKnown'] is False and record['installer_prepared'] is True
        assert 'package_attempt' not in record and not finished_package_attempt(record)


@pytest.mark.parametrize('binding', ['exact', 'missing', 'wrong_key', 'journal_failure'])
def test_outer_retired_exception_needs_exact_durable_item_binding(tmp_path, binding, monkeypatch):
    backend = SimpleNamespace(data_dir=tmp_path, system='win32', bridge=None)
    def operation(worker, progress):
        progress({'id': 'Vendor.App', 'current': 0, 'total': 1,
            'item_progress': {'key': 'software:update:Vendor.App', 'label': 'Vendor.App', 'phase': 'install', 'percent': None}})
        error = failure()
        if binding != 'missing':
            error.item_id = 'Vendor.App'
            error.item_key = 'software:update:Other.App' if binding == 'wrong_key' else 'software:update:Vendor.App'
        raise error
    job = ParallelAppUpdates(backend, 'a' * 32, WINDOWS_SOFTWARE, operation, ProgressMux(lambda event: None))
    if binding == 'journal_failure':
        monkeypatch.setattr(job, 'item_receipt', Mock(side_effect=OSError('Fixture terminal receipt write failed')))
    job.start()
    report = job.join()
    record = json.loads(job.path.read_bytes())
    assert report['status'] == 'failed'
    if binding == 'exact':
        assert record['result']['items'][0]['result']['outcomeKnown'] is True
        assert finished_package_attempt(record) and pending_updates_barrier(backend) is None
    else:
        assert record['result']['outcomeKnown'] is False
        assert not finished_package_attempt(record) and pending_updates_barrier(backend)
