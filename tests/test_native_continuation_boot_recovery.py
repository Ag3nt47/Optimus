"""Typed update-only boot provenance in disposable state, without native actions."""
import copy
import hashlib
import json

import pytest

from optimus import automatic_update_recovery as recovery
from optimus.automation import RunLease
from optimus.parallel_updates import pending_updates_barrier
from test_automatic_update_recovery import completed, console_lost, datetime_from_epoch, run


@pytest.fixture
def continuation(console_lost, monkeypatch):
    f = console_lost
    parent_raw = f.parent_path.read_bytes()
    previous_boot = json.loads(parent_raw)['stages'][0]['result']['System']['LastBootUtc']
    captured = f.record['started_at'] - .5
    runtime = dict(f.record['runtime_owner'], journal_id=f.record['parent_run_id'],
                   captured_at=captured - .5)
    initial_probe = {'schema': 1, 'owner': f.record['owner'], 'complete': True, 'review_only': True,
        'captured_at': captured - .25, 'boot_utc': previous_boot, 'boot_event_utc': previous_boot}
    later = f.boot.return_value
    f.boot.return_value = round(recovery._utc_epoch(previous_boot))
    with monkeypatch.context() as scoped:
        scoped.setattr(recovery, 'capture_runtime_owner', lambda *args: copy.deepcopy(runtime))
        scoped.setattr(recovery.time, 'time', lambda: captured)
        f.backend.automatic_update_recovery_probe.return_value = initial_probe
        with RunLease(f.data) as lease:
            proof = recovery.capture_continuation_boot(f.backend, f.record['parent_run_id'], lease=lease)
    assert proof['status'] == 'captured', proof
    f.boot.return_value = later
    f.backend.automatic_update_recovery_probe.return_value = f.probe
    f.record[recovery._CONTINUATION_BOOT_FIELD] = proof['reference']
    f.path.write_text(json.dumps(f.record))
    f.parent_path.unlink()  # Disposable CLI fixture has no GUI workflow audit.
    f.parent_raw = parent_raw
    f.provenance_path = f.data / recovery._CONTINUATION_BOOT_DIR / (f.record['parent_run_id'] + '.json')
    return f


def test_continuation_without_gui_audit_recovers_only_after_verified_later_boot(continuation):
    f = continuation
    before = f.path.read_bytes()
    proof = f.provenance_path.read_bytes()
    assert pending_updates_barrier(f.backend)
    outcome = run(f)
    assert outcome['reconciled'] == []
    assert outcome['reconciled_after_reboot'] == [
        {'id': f.record['id'], 'package_id': 'Fixture.App', 'version': '2.0'}]
    assert not pending_updates_barrier(f.backend)
    assert f.path.read_bytes() == before and f.provenance_path.read_bytes() == proof
    assert not f.parent_path.exists()
    receipt = json.loads(recovery._restart_marker(f.data, f.record['id']).read_text())
    assert receipt['prior_boot'] == {'kind': recovery._CONTINUATION_BOOT_KIND,
        'parent_sha256': hashlib.sha256(proof).hexdigest(),
        'previous_boot_utc': json.loads(proof)['boot']['boot_utc']}
    assert receipt['historical_outcome_known'] is False


@pytest.mark.parametrize('failure', ['missing_file', 'changed_bytes', 'bad_reference', 'different_owner',
    'different_process', 'different_parent', 'late_capture', 'different_boot_event', 'missing_reference',
    'same_boot', 'existing_gui_audit'])
def test_unverified_continuation_provenance_preserves_installer_barrier(continuation, failure):
    f = continuation
    value = json.loads(f.provenance_path.read_text())
    if failure == 'missing_file':
        f.provenance_path.unlink()
    elif failure == 'changed_bytes':
        f.provenance_path.write_bytes(f.provenance_path.read_bytes() + b'\n')
    elif failure == 'bad_reference':
        f.record[recovery._CONTINUATION_BOOT_FIELD]['sha256'] = 'f' * 64
    elif failure == 'missing_reference':
        del f.record[recovery._CONTINUATION_BOOT_FIELD]
    elif failure == 'same_boot':
        old = value['boot']
        f.probe.update(boot_utc=old['boot_utc'], boot_event_utc=old['boot_event_utc'])
        f.boot.return_value = old['boot_identity']
    elif failure == 'existing_gui_audit':
        # A declared alternate origin cannot replace even a valid GUI audit.
        f.parent_path.write_bytes(f.parent_raw)
    else:
        if failure == 'different_owner':
            value['owner'] = 'other-owner'
        elif failure == 'different_process':
            value['runtime_owner']['pid'] += 1
        elif failure == 'different_parent':
            value['runtime_owner']['parent_run_id'] = 'f' * 32
        elif failure == 'late_capture':
            value['captured_at'] = f.record['started_at'] + 1
        else:
            value['boot']['boot_event_utc'] = datetime_from_epoch(
                recovery._utc_epoch(value['boot']['boot_event_utc']) - 500)
        # Model a structurally wrong producer, with a matching external hash.
        raw = json.dumps(value).encode()
        f.provenance_path.write_bytes(raw)
        f.record[recovery._CONTINUATION_BOOT_FIELD]['sha256'] = hashlib.sha256(raw).hexdigest()
    f.path.write_text(json.dumps(f.record))
    original = f.path.read_bytes()
    assert not run(f)['reconciled_after_reboot']
    assert f.path.read_bytes() == original and pending_updates_barrier(f.backend)
    assert not (f.data / 'update-holds.json').exists()


def test_bad_declared_reference_never_downgrades_to_gui_audit(console_lost):
    f = console_lost
    f.record[recovery._CONTINUATION_BOOT_FIELD] = {}
    f.path.write_text(json.dumps(f.record))
    assert f.parent_path.exists()
    original = f.path.read_bytes()
    assert not run(f)['reconciled_after_reboot']
    assert f.path.read_bytes() == original and pending_updates_barrier(f.backend)
