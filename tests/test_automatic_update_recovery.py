"""Crash-gap recovery uses native terminal evidence; no fixture launches tools."""
from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest

from optimus import automatic_update_recovery as recovery
from optimus.automation import RunLease
from optimus.parallel_updates import pending_updates_barrier
from optimus.update_recovery import finished_package_attempt


@pytest.fixture
def completed(tmp_path):
    owner = recovery._current_owner()
    data, logs = tmp_path / 'Data', tmp_path / 'DiagOutputDir'
    (data / 'parallel-updates').mkdir(parents=True)
    (data / 'runs').mkdir()
    logs.mkdir()
    prepared = recovery._epoch('2026-01-02 03:04:00.000')
    now = recovery._epoch('2026-01-02 03:07:00.000')
    record = {'schema': 1, 'id': 'a' * 32, 'owner': owner, 'parent_run_id': 'b' * 32,
        'title': 'Review or update configured software', 'status': 'running', 'installer_prepared': True,
        'started_at': prepared - 20, 'current_item': {'id': 'Fixture.App',
            'description': 'Updating approved package 1/2: Fixture.App (1.0 to 2.0)'},
        'package_attempt': {'kind': 'terminal-package-attempt-v1', 'id': 'c' * 32,
            'item_id': 'Fixture.App', 'state': 'prepared', 'prepared_at': prepared}}
    path = data / 'parallel-updates' / (record['id'] + '.json')
    path.write_bytes(json.dumps(record, indent=2).encode())
    parent = {'id': 'b' * 32, 'started_at': prepared - 20, 'stages': [
        {'title': 'Inspect current resource usage', 'status': 'completed', 'result': {'Processes': [
            {'Name': 'Optimus.exe', 'ProcessId': 44, 'Path': r'D:\Optimus\0.11.10\Optimus.exe',
                'StartTimeUtc': '2026-01-02T02:00:00Z'}]}}]}
    parent_path = data / 'runs' / ('b' * 32 + '.json')
    parent_path.write_text(json.dumps(parent))
    inno = logs / 'Fixture.App.2.0-26-01-02-03-05-00.log'
    inno.write_text('2026-01-02 03:05:58.100   Installation process succeeded.\n'
        '2026-01-02 03:05:58.200   Need to restart Windows? No\n'
        '2026-01-02 03:05:58.300   Log closed.\n')
    native = logs / 'WinGet-2026-01-02-03-04-10.013.log'
    text = f'''2026-01-02 03:04:10.016 <I> [CORE] Process: winget.exe[1234], Level[1], Offset: 0000
2026-01-02 03:04:10.025 <I> [CLI ] WinGet invoked with arguments: 'upgrade' '--id' 'Fixture.App' '--exact' '--source' 'winget' '--version' '2.0' '--force' '--silent' '--disable-interactivity' '--accept-source-agreements' '--accept-package-agreements'
2026-01-02 03:04:11.118 <I> [CLI ] Found one app. App id: Fixture.App App name: Fixture
2026-01-02 03:04:12.416 <I> [CLI ] Manifest fields: Name [Fixture], Version [2.0]
2026-01-02 03:05:00.946 <I> [CLI ] Installer hash verified
2026-01-02 03:05:00.140 <I> [CLI ] Starting: 'D:\\Temp\\FixtureSetup.exe' with arguments '/VERYSILENT /NORESTART /LOG="{inno}"'
2026-01-02 03:05:59.043 <I> [CLI ] The entry determined to be associated with the package is 'Fixture version 2.0', with publisher 'Fixture Corp'
2026-01-02 03:06:00.102 <I> [CLI ] Leaf command succeeded: root:upgrade
'''
    native.write_text(text)
    probe = {'schema': 1, 'complete': True, 'review_only': True, 'owner': owner, 'captured_at': now,
        'wua_busy': False, 'reboot_required': False,
        'processes': [{'pid': 98, 'name': 'fixture.exe', 'path': r'D:\Fixture\fixture.exe'},
            {'pid': 0, 'name': 'System Idle Process', 'path': ''},
            {'pid': 4, 'name': 'System', 'path': ''},
            {'pid': 100, 'name': 'services.exe', 'path': r'C:\Windows\system32\services.exe'},
            {'pid': 102, 'name': 'csrss.exe', 'path': r'C:\Windows\system32\csrss.exe'}],
        'applications': [{'name': 'Fixture version 2.0', 'publisher': 'Fixture Corp', 'version': '2.0'}]}
    backend = SimpleNamespace(data_dir=data, system='win32', automatic_update_recovery_probe=Mock(return_value=probe))
    return SimpleNamespace(data=data, folder=logs, record=record, path=path, parent_path=parent_path,
        native=native, inno=inno, probe=probe, backend=backend, now=now, native_text=text)


def run(f):
    return recovery.reconcile_completed_updates(f.backend, folder=f.folder, now=f.now)


def test_completed_crash_gap_clears_both_lanes_preserving_parent_preferences_holds(completed):
    f = completed
    originals = {f.path: f.path.read_bytes(), f.parent_path: f.parent_path.read_bytes()}
    for name in ('preferences.json', 'software-update-holds.json'):
        p = f.data / name
        p.write_bytes(b'{"never":"rewrite these user choices"}\n')
        originals[p] = p.read_bytes()
    assert pending_updates_barrier(f.backend)
    outcome = run(f)
    assert outcome['status'] == 'reconciled'
    assert outcome['reconciled'] == [{'id': 'a' * 32, 'package_id': 'Fixture.App', 'version': '2.0'}]
    assert not pending_updates_barrier(f.backend)
    updated = json.loads(f.path.read_text())
    assert updated['status'] == 'running'  # Never promote an incomplete batch.
    assert finished_package_attempt(updated)
    audits = list((f.data / 'update-reconciliation').iterdir())
    assert len(audits) == 1
    audit = audits[0]
    assert (audit / 'original.json').read_bytes() == originals[f.path]
    proof = json.loads((audit / 'evidence.json').read_text())
    assert proof['original_sha256'] == hashlib.sha256(originals[f.path]).hexdigest()
    assert proof['evidence']['winget_log_sha256'] == hashlib.sha256(f.native.read_bytes()).hexdigest()
    for p, raw in originals.items():
        if p != f.path:
            assert p.read_bytes() == raw
    assert run(f)['reconciled'] == []  # Idempotent, no duplicate terminal item.
    assert len(json.loads(f.path.read_text())['terminal_items']) == 1


def test_authenticated_current_console_owner_does_not_block_its_own_recovery(completed):
    f = completed
    created = recovery._utc_epoch('2026-01-02T03:06:30Z')
    identity = {'pid': 560, 'path': r'D:\Optimus\0.11.13\OptimusConsole.exe',
        'created_at': created, 'owner': recovery._current_owner()}
    f.probe['processes'].append({'pid': 560, 'name': 'OptimusConsole.exe',
        'path': identity['path'], 'started_utc': '2026-01-02T03:06:30Z'})
    with patch.object(recovery, '_current_console_identity', return_value=identity):
        assert run(f)['status'] == 'reconciled'


@pytest.mark.parametrize('field,value', [('pid', 561), ('path', r'D:\Other\OptimusConsole.exe'),
    ('created_at', 1.0), ('owner', 'sid:other')])
def test_console_name_cannot_exempt_unrelated_helper(completed, field, value):
    f = completed
    identity = {'pid': 560, 'path': r'D:\Optimus\0.11.13\OptimusConsole.exe',
        'created_at': recovery._utc_epoch('2026-01-02T03:06:30Z'), 'owner': recovery._current_owner()}
    f.probe['processes'].append({'pid': 560, 'name': 'OptimusConsole.exe',
        'path': identity['path'], 'started_utc': '2026-01-02T03:06:30Z'})
    identity[field] = value
    original = f.path.read_bytes()
    with patch.object(recovery, '_current_console_identity', return_value=identity):
        assert not run(f)['reconciled']
    assert f.path.read_bytes() == original


def test_current_console_exemption_never_exempts_second_monitor(completed):
    f = completed
    identity = {'pid': 560, 'path': r'D:\Optimus\0.11.13\OptimusConsole.exe',
        'created_at': recovery._utc_epoch('2026-01-02T03:06:30Z'), 'owner': recovery._current_owner()}
    f.probe['processes'].extend({'pid': pid, 'name': 'OptimusConsole.exe',
        'path': identity['path'], 'started_utc': '2026-01-02T03:06:30Z'} for pid in (560, 561))
    with patch.object(recovery, '_current_console_identity', return_value=identity):
        assert not run(f)['reconciled']


@pytest.mark.parametrize('change', [
    lambda s: s.replace("'Fixture.App'", "'Other.App'"),
    lambda s: s.replace("'--version' '2.0'", "'--version' '3.0'"),
    lambda s: s.replace("'--source' 'winget'", "'--source' 'msstore'"),
    lambda s: s.replace("'--exact' ", ''),
    lambda s: s.replace("'--silent'", "'--interactive'"),
    lambda s: s.replace('Installer hash verified', 'Installer hash mismatch'),
    lambda s: s.replace('Leaf command succeeded: root:upgrade', 'Leaf command failed: root:upgrade'),
    lambda s: s.replace('2026-01-02 03:04:10.016', '2026-01-01 03:04:10.016'),
    lambda s: s.replace('2026-01-02 03:06:00.102', '2026-01-03 03:06:00.102'),
    lambda s: s.replace('with publisher', 'with unknown publisher'),
])
def test_near_matching_or_incomplete_native_commands_do_not_clear(completed, change):
    f = completed
    raw = f.path.read_bytes()
    f.native.write_text(change(f.native_text))
    outcome = run(f)
    assert not outcome['reconciled'] and outcome['unresolved']
    assert f.path.read_bytes() == raw
    assert pending_updates_barrier(f.backend)


@pytest.mark.parametrize('mutation', [
    lambda p: p.update(complete=False),
    lambda p: p.update(schema=True),
    lambda p: p.update(captured_at=p['captured_at'] - 31),
    lambda p: p.update(wua_busy=True),
    lambda p: p.update(reboot_required=True),
    lambda p: p.update(owner='different-owner'),
    lambda p: p['processes'].append({'pid': 44, 'name': 'Optimus.exe', 'path': 'old'}),
    lambda p: p['processes'].append({'pid': 1234, 'name': 'winget.exe', 'path': 'old'}),
    lambda p: p['processes'].append({'pid': 999, 'name': 'OtherSetup.exe', 'path': 'other'}),
    lambda p: p['processes'].append({'pid': 999, 'name': '', 'path': ''}),
    lambda p: p['applications'][0].update(version='1.0'),
    lambda p: p.update(applications=[]),
    lambda p: p['applications'].append(copy.deepcopy(p['applications'][0])),
])
def test_active_unknown_restart_or_wrong_current_version_is_preserved(completed, mutation):
    f = completed
    raw = f.path.read_bytes()
    mutation(f.probe)
    assert not run(f)['reconciled']
    assert f.path.read_bytes() == raw
    assert not (f.data / 'update-reconciliation').exists()


@pytest.mark.parametrize('marker', ['Installation process succeeded.', 'Need to restart Windows? No', 'Log closed.'])
def test_incomplete_or_unclosed_installer_is_never_recovered(completed, marker):
    f = completed
    raw = f.path.read_bytes()
    f.inno.write_text(f.inno.read_text().replace(marker, 'Missing'))
    assert not run(f)['reconciled']
    assert f.path.read_bytes() == raw


@pytest.mark.parametrize('field', ['id', 'item_id', 'state', 'prepared_at'])
def test_invalid_exact_attempt_binding_preserves_original(completed, field):
    f = completed
    f.record['package_attempt'][field] = 'invalid'
    f.path.write_text(json.dumps(f.record))
    raw = f.path.read_bytes()
    assert not run(f)['reconciled']
    assert f.path.read_bytes() == raw


def test_partial_audit_failure_does_not_clear_and_next_run_recovers(completed):
    f = completed
    raw = f.path.read_bytes()
    write = recovery._exclusive
    def fail_evidence(path, value):
        if path.name == 'evidence.json':
            raise OSError('fixture disk full')
        write(path, value)
    with patch.object(recovery, '_exclusive', side_effect=fail_evidence):
        assert not run(f)['reconciled']
    assert f.path.read_bytes() == raw
    assert run(f)['reconciled']


def test_partial_original_backup_never_becomes_a_permanent_recovery_block(completed):
    f = completed
    raw = f.path.read_bytes()
    write = recovery._exclusive
    def partial(path, value):
        if path.name == 'original.json':
            write(path, value[:11])
            raise OSError('fixture disk filled during backup')
        write(path, value)
    with patch.object(recovery, '_exclusive', side_effect=partial):
        assert not run(f)['reconciled']
    assert f.path.read_bytes() == raw
    assert run(f)['reconciled']
    originals = [p.read_bytes() for p in (f.data / 'update-reconciliation').glob('*/original.json')]
    assert len(originals) == 2 and raw in originals and raw[:11] in originals


def test_later_package_completion_preserves_earlier_known_item_receipt(completed):
    f = completed
    prior = {'id': 'Earlier.App', 'status': 'failed', 'result': {'exitCode': 1,
        'outcomeKnown': True, 'running': False, 'installer_started': True, 'restartNeeded': False}}
    f.record['terminal_items'] = [prior]
    f.path.write_text(json.dumps(f.record))
    assert run(f)['reconciled']
    updated = json.loads(f.path.read_text())
    assert updated['terminal_items'][0] == prior and len(updated['terminal_items']) == 2
    assert finished_package_attempt(updated) and not pending_updates_barrier(f.backend)


def test_earlier_unknown_item_cannot_be_washed_out_by_later_success(completed):
    f = completed
    f.record['terminal_items'] = [{'id': 'Earlier.App', 'status': 'needs_review',
        'result': {'outcomeKnown': False, 'running': True, 'installer_started': True}}]
    f.path.write_text(json.dumps(f.record))
    original = f.path.read_bytes()
    assert not run(f)['reconciled']
    assert f.path.read_bytes() == original and pending_updates_barrier(f.backend)
    assert not (f.data / 'update-reconciliation').exists()


def test_current_maintenance_lease_is_reused_and_busy_startup_noops(completed):
    f = completed
    with RunLease(f.data) as lease:
        assert run(f)['status'] == 'busy'
        assert f.backend.automatic_update_recovery_probe.call_count == 0
        assert recovery.reconcile_completed_updates(f.backend, lease=lease, folder=f.folder, now=f.now)['reconciled']
        assert lease.handle is not None


def test_failed_package_hold_and_unrelated_terminal_journal_do_not_block_recovery(completed):
    f = completed
    known = {'schema': 1, 'id': 'd' * 32, 'owner': recovery._current_owner(), 'status': 'completed',
        'installer_prepared': True, 'result': {'items': [{'id': 'Other.App', 'status': 'failed',
            'result': {'outcomeKnown': True, 'running': False, 'installer_started': False}}]}}
    p = f.data / 'parallel-updates' / ('d' * 32 + '.json')
    p.write_text(json.dumps(known))
    raw = p.read_bytes()
    assert run(f)['reconciled']
    assert not pending_updates_barrier(f.backend)
    assert p.read_bytes() == raw


def test_unproven_second_installer_still_blocks_without_preventing_proven_repair(completed):
    f = completed
    second = copy.deepcopy(f.record)
    second['id'] = 'e' * 32
    second['current_item']['id'] = 'Other.App'
    p = f.data / 'parallel-updates' / ('e' * 32 + '.json')
    p.write_text(json.dumps(second))
    raw = p.read_bytes()
    result = run(f)
    assert result['reconciled'] and result['unresolved']
    assert pending_updates_barrier(f.backend)
    assert p.read_bytes() == raw


@pytest.mark.parametrize('name', ['TrustedInstaller.exe', 'TiWorker.exe'])
def test_later_unrelated_windows_servicing_does_not_resurrect_old_block(completed, name):
    f = completed
    f.probe['windows_directory'] = r'C:\Windows'
    born = datetime_from_epoch(f.now - 1)
    f.probe['processes'].append({'pid': 777, 'name': name, 'path': r'C:\Windows\servicing' + '\\' + name,
        'started_utc': born})
    assert run(f)['reconciled']


def datetime_from_epoch(value):
    from datetime import datetime, timezone
    return datetime.fromtimestamp(value, timezone.utc).isoformat()


def add_console_runtime_owner(f):
    prepared = f.record['package_attempt']['prepared_at']
    born = prepared - 100
    f.record['runtime_owner'] = {'schema': 1, 'kind': recovery.RUNTIME_OWNER_KIND,
        'owner': f.record['owner'], 'journal_id': f.record['id'], 'parent_run_id': f.record['parent_run_id'],
        'pid': 44, 'created_at': born, 'created_utc': datetime_from_epoch(born),
        'captured_at': f.record['started_at'] + .01, 'executable': r'D:\Python\python.exe',
        'name': 'python.exe', 'frozen': False}
    f.path.write_text(json.dumps(f.record))


def test_console_only_crash_gap_recovers_without_gui_audit_or_runs_parent(completed):
    f = completed
    add_console_runtime_owner(f)
    f.parent_path.unlink()
    continuation = f.data / 'update-continuations' / ('continue-updates-' + 'b' * 32 + '.json')
    continuation.parent.mkdir()
    continuation.write_bytes(b'{"command":"continue-updates","stages":[]}\n')
    original = f.path.read_bytes()
    parent_original = continuation.read_bytes()
    assert run(f)['reconciled']
    updated = json.loads(f.path.read_text())
    assert finished_package_attempt(updated) and updated['runtime_owner'] == f.record['runtime_owner']
    assert not f.parent_path.exists() and continuation.read_bytes() == parent_original
    audit = next((f.data / 'update-reconciliation').iterdir())
    assert (audit / 'original.json').read_bytes() == original
    proof = json.loads((audit / 'evidence.json').read_text())
    assert proof['parent_sha256'] is None and proof['runtime_owner_source'] == recovery.RUNTIME_OWNER_KIND


def test_console_identity_prefers_journal_and_preserves_existing_non_gui_parent(completed):
    f = completed
    add_console_runtime_owner(f)
    # A console run's report is valid but contains no invented process audit.
    f.parent_path.write_text(json.dumps({'id': 'b' * 32, 'command': 'continue-updates', 'stages': []}))
    original = f.parent_path.read_bytes()
    assert run(f)['reconciled']
    audit = next((f.data / 'update-reconciliation').iterdir())
    proof = json.loads((audit / 'evidence.json').read_text())
    assert proof['parent_sha256'] == hashlib.sha256(original).hexdigest()
    assert f.parent_path.read_bytes() == original


@pytest.mark.parametrize('mutation', [
    lambda r: r['runtime_owner'].update(schema=True),
    lambda r: r['runtime_owner'].update(owner='another-owner'),
    lambda r: r['runtime_owner'].update(journal_id='d' * 32),
    lambda r: r['runtime_owner'].update(parent_run_id='d' * 32),
    lambda r: r['runtime_owner'].update(pid=True),
    lambda r: r['runtime_owner'].update(executable='python.exe'),
    lambda r: r['runtime_owner'].update(executable=r'\Python\python.exe'),
    lambda r: r['runtime_owner'].update(name='Optimus.exe'),
    lambda r: r['runtime_owner'].update(frozen='source'),
    lambda r: r['runtime_owner'].update(created_utc='2020-01-01T00:00:00Z'),
    lambda r: r['runtime_owner'].update(created_utc='2026-01-02T00:00:00'),
    lambda r: r['runtime_owner'].update(created_at=float('nan')),
    lambda r: r['runtime_owner'].update(captured_at=r['package_attempt']['prepared_at'] + 1),
    lambda r: r['runtime_owner'].update(created_at=r['started_at'] + 1),
])
def test_invalid_console_identity_never_downgrades_to_existing_legacy_audit(completed, mutation):
    f = completed
    add_console_runtime_owner(f)
    mutation(f.record)
    f.path.write_text(json.dumps(f.record))
    original = f.path.read_bytes()
    assert not run(f)['reconciled']
    assert f.path.read_bytes() == original and pending_updates_barrier(f.backend)


def test_live_console_owner_cannot_be_reconciled_without_native_owner_exit(completed):
    f = completed
    add_console_runtime_owner(f)
    f.parent_path.unlink()
    identity = f.record['runtime_owner']
    f.probe['processes'].append({'pid': identity['pid'], 'name': identity['name'],
        'path': identity['executable'], 'started_utc': identity['created_utc']})
    original = f.path.read_bytes()
    assert not run(f)['reconciled']
    assert f.path.read_bytes() == original


@pytest.mark.parametrize('birth', [None, '2020-01-01T00:00:00Z', 'invalid'])
def test_missing_or_older_pid_birth_is_not_proof_console_owner_exited(completed, birth):
    f = completed
    add_console_runtime_owner(f)
    f.probe['processes'].append({'pid': 44, 'name': 'python.exe', 'path': r'D:\Python\python.exe',
        'started_utc': birth})
    original = f.path.read_bytes()
    assert not run(f)['reconciled'] and f.path.read_bytes() == original


def test_journal_captures_actual_native_self_identity_before_any_updater_launch(tmp_path):
    import os
    import psutil
    from optimus.parallel_updates import ParallelAppUpdates
    backend = SimpleNamespace(data_dir=tmp_path)
    worker = ParallelAppUpdates(backend, 'b' * 32, 'Review or update configured software', None, None)
    identity = worker.record['runtime_owner']
    own = psutil.Process(os.getpid())
    assert identity['pid'] == own.pid and identity['executable'] == own.exe() and identity['name'] == own.name()
    assert abs(identity['created_at'] - own.create_time()) <= .001
    assert identity['owner'] == worker.record['owner']
    assert identity['journal_id'] == worker.record['id'] and identity['parent_run_id'] == 'b' * 32
    assert not worker.path.exists() and worker.record['installer_prepared'] is False


def test_unavailable_native_writer_identity_does_not_break_normal_update_job(tmp_path):
    import psutil
    from optimus.parallel_updates import ParallelAppUpdates, ProgressMux
    calls = []
    backend = SimpleNamespace(data_dir=tmp_path, bridge=SimpleNamespace(_process=None))
    def operation(worker, emit):
        calls.append('fixture-only updater')
        emit({'id': 'Fixture.App', 'description': 'Updating approved package 1/1: Fixture.App (1.0 to 2.0)'})
        return {'items': [{'id': 'Fixture.App', 'status': 'completed', 'result': {
            'outcomeKnown': True, 'running': False, 'installer_started': True, 'exitCode': 0,
            'restartNeeded': False}}], 'failed_items': 0, 'outcomeKnown': True, 'running': False}
    with patch.object(recovery, 'capture_runtime_owner', side_effect=psutil.AccessDenied(pid=99)), \
         patch('optimus.runtime_priority.lower_worker_priority', return_value=lambda: None):
        job = ParallelAppUpdates(backend, 'b' * 32, 'Review or update configured software', operation,
            ProgressMux(lambda event: None))
        assert 'runtime_owner' not in job.record
        assert 'AccessDenied' in job.record['runtime_owner_unavailable']['reason']
        job.start(inline=True)
        job._run()
        stage = job.join()
    assert stage['status'] == 'completed' and calls == ['fixture-only updater']
    saved = json.loads(job.path.read_text())
    assert saved['status'] == 'completed' and 'runtime_owner' not in saved
    assert saved['runtime_owner_unavailable'] == job.record['runtime_owner_unavailable']


@pytest.mark.parametrize('mutation', [
    lambda p: p['processes'].append(copy.deepcopy(p['processes'][0])),
    lambda p: p.update(processes=p['processes'][:4]),
    lambda p: p['processes'].append({'pid': 777, 'name': 'TrustedInstaller.exe',
        'path': r'C:\Windows\servicing\TrustedInstaller.exe', 'started_utc': '2020-01-01T00:00:00Z'}),
])
def test_incomplete_duplicate_or_overlapping_servicing_census_is_preserved(completed, mutation):
    f = completed
    mutation(f.probe)
    original = f.path.read_bytes()
    assert not run(f)['reconciled'] and f.path.read_bytes() == original


def test_verified_pid_reuse_is_not_a_permanent_block(completed):
    f = completed
    f.probe['processes'].append({'pid': 44, 'name': 'new.exe', 'path': r'D:\Other\new.exe',
        'started_utc': datetime_from_epoch(f.now - 1)})
    f.probe['processes'].append({'pid': 1234, 'name': 'other.exe', 'path': r'D:\Other\other.exe',
        'started_utc': datetime_from_epoch(f.now - 1)})
    assert run(f)['reconciled']


def test_maintenance_recovers_before_first_pending_snapshot_and_audit(completed):
    from test_workflow import MockBackend
    from optimus.workflow import DEFAULTS, Workflow
    f = completed
    backend = MockBackend(f.data)
    backend.reconcile_completed_updates = lambda *, lease: recovery.reconcile_completed_updates(
        f.backend, lease=lease, folder=f.folder, now=f.now)
    calls = []
    original_barrier = pending_updates_barrier
    def snapshot(candidate):
        assert finished_package_attempt(json.loads(f.path.read_text()))
        with pytest.raises(RuntimeError, match='already running maintenance'):
            RunLease(f.data).acquire()
        calls.append('snapshot')
        return original_barrier(candidate)
    def audit():
        assert calls and finished_package_attempt(json.loads(f.path.read_text()))
        calls.append('audit')
    backend.audit_callback = audit
    config = dict(DEFAULTS, cleanup_enabled=False, browser_cache_enabled=False,
        driver_updates_enabled=False, software_updates_enabled=False)
    with patch('optimus.parallel_updates.pending_updates_barrier', side_effect=snapshot):
        report = Workflow(backend).run(config, lambda event: None)
    assert calls[0] == 'snapshot' and 'audit' in calls
    assert report['automatic_update_reconciliation']['status'] == 'reconciled'
    assert not original_barrier(f.backend)
