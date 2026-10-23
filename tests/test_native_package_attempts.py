"""Crash-gap receipts use only disposable journals and injected native results."""
import copy
import json
from types import SimpleNamespace

import pytest

from optimus.parallel_updates import ParallelAppUpdates, ProgressMux, WINDOWS_SOFTWARE, pending_updates_barrier
from optimus.update_recovery import finished_package_attempt


@pytest.fixture
def job(tmp_path, monkeypatch):
    monkeypatch.setattr('optimus.speed_enhancements._current_owner', lambda: 'fixture-owner')
    service = SimpleNamespace(data_dir=tmp_path, system='win32')
    job = ParallelAppUpdates(service, 'a'*32, WINDOWS_SOFTWARE, lambda *_: None, ProgressMux(lambda _: None))
    job.record['status'] = 'running'
    return job


def begin(job, identity='Vendor.One'):
    job.item_progress({'id': identity, 'description': 'Update '+identity,
        'item_progress': {'key': 'software:update:'+identity, 'label': identity,
                          'phase': 'checking', 'percent': None}})


def receipt(identity='Vendor.One', *, status='failed', **changes):
    outcome = dict(installer_started=True, outcomeKnown=True, running=False,
                   exitCode=1, restartNeeded=False)
    outcome.update(changes)
    return {'id': identity, 'status': status, 'result': outcome}


@pytest.mark.parametrize('status,code', [('completed', 0), ('failed', 1), ('skipped', None)])
def test_exact_durable_terminal_receipt_closes_stale_running_header_without_replay(job, status, code):
    begin(job)
    assert pending_updates_barrier(job.backend)
    token = job.record['package_attempt']['id']
    job.item_receipt(receipt(status=status, exitCode=code, installer_started=code is not None))
    saved = job.path.read_bytes()
    record = json.loads(saved)
    assert record['status'] == 'running' and record['installer_prepared'] is True
    assert record['package_attempt']['id'] == token
    assert finished_package_attempt(record)
    assert pending_updates_barrier(job.backend) is None
    assert job.path.read_bytes() == saved  # The reader changes no history.
    assert record['terminal_items'][0]['status'] == status


@pytest.mark.parametrize('change', [dict(outcomeKnown=False), dict(running=True),
    dict(installer_started=None), dict(restartNeeded=True), dict(exitCode=3010),
    dict(uncertain=True), dict(outcomeKnown=1), dict(running=0)])
def test_unknown_or_restart_receipt_cannot_close_live_package_intent(job, change):
    begin(job)
    job.item_receipt(receipt(**change))
    assert not finished_package_attempt(job.record)
    assert pending_updates_barrier(job.backend)


@pytest.mark.parametrize('restart', ['true', 1, 'false', 0])
def test_malformed_restart_values_do_not_close_attempt(job, restart):
    begin(job); job.item_receipt(receipt(restartNeeded=restart))
    assert pending_updates_barrier(job.backend)


def test_real_captured_winget_no_restart_none_is_supported(job):
    begin(job); job.item_receipt(receipt(restartNeeded=None))
    assert pending_updates_barrier(job.backend) is None


@pytest.mark.parametrize('code', [3010, 1641, 0x8A150109, -1978334967, 1618, 0x8A150102, -1978334974])
def test_null_restart_marker_cannot_hide_native_restart_exit(job, code):
    begin(job); job.item_receipt(receipt(restartNeeded=None, exitCode=code))
    assert pending_updates_barrier(job.backend)


@pytest.mark.parametrize('next_id', ['Vendor.Two', 'Vendor.One'])
def test_next_boundary_invalidates_previous_receipt_before_native_call(job, next_id):
    begin(job)
    job.item_receipt(receipt())
    prior = job.record['package_attempt']['id']
    assert pending_updates_barrier(job.backend) is None
    begin(job, next_id)
    assert job.record['package_attempt']['id'] != prior
    assert job.record['package_attempt']['state'] == 'prepared'
    assert pending_updates_barrier(job.backend)


def test_different_receipt_and_unbound_boundary_never_reuse_previous_closure(job):
    begin(job)
    job.item_receipt(receipt('Vendor.Other'))
    assert pending_updates_barrier(job.backend)
    begin(job)
    job.item_receipt(receipt())
    # An unbound future native operation is still a live intent.
    job.item_progress({'description': 'Unbound mutation'})
    assert 'package_attempt' not in job.record
    assert pending_updates_barrier(job.backend)


@pytest.mark.parametrize('mutate', [
    lambda r: r['package_attempt'].update(terminal_sha256='0'*64),
    lambda r: r['package_attempt'].update(terminal_index=False),
    lambda r: r['package_attempt'].update(item_id='Other'),
    lambda r: r['package_attempt'].update(id='not-an-intent'),
    lambda r: r['package_attempt'].update(finished_at=0),
    lambda r: r['package_attempt'].update(finished_at=10**1000),
    lambda r: r['package_attempt'].update(prepared_at=10**1000),
    lambda r: r['terminal_items'][0]['result'].update(running=True),
    lambda r: r.update(result={'outcomeKnown':False,'running':True}),
    lambda r: r.update(status='uncertain'),
    lambda r: r.update(schema=True),
    lambda r: r.pop('package_attempt'),
])
def test_invalid_or_contradictory_evidence_keeps_pending_guard(job, mutate):
    begin(job); job.item_receipt(receipt())
    altered = copy.deepcopy(job.record); mutate(altered)
    job.path.write_text(json.dumps(altered), encoding='utf-8')
    assert pending_updates_barrier(job.backend)


def test_earlier_unknown_item_cannot_be_washed_out_by_later_known_result(job):
    begin(job); job.item_receipt(receipt(outcomeKnown=False))
    begin(job, 'Vendor.Two'); job.item_receipt(receipt('Vendor.Two'))
    assert not finished_package_attempt(job.record)
    assert pending_updates_barrier(job.backend)


def test_terminal_save_failure_does_not_close_durable_intent(job, monkeypatch):
    begin(job)
    original = job.path.read_bytes()
    monkeypatch.setattr(job, '_write', lambda: (_ for _ in ()).throw(OSError('fixture fsync failure')))
    with pytest.raises(OSError):
        job.item_receipt(receipt())
    assert job.path.read_bytes() == original
    assert pending_updates_barrier(job.backend)


def test_legacy_running_record_without_token_remains_unresolved(job):
    begin(job)
    job.record.pop('package_attempt')
    job.record['terminal_items'] = [receipt()]
    job._write()
    assert pending_updates_barrier(job.backend)
