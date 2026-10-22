"""Legacy metadata recovery uses only fixture files, never host update actions."""
import copy
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
from unittest.mock import Mock

import pytest

from optimus.automation import RunLease
from optimus import update_recovery as recovery


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def save(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(json.dumps(value, indent=2).encode('utf-8'))


@pytest.fixture
def case(tmp_path, monkeypatch):
    data = tmp_path / 'Data'
    journal_id, parent_id = 'a' * 32, 'b' * 32
    executable = tmp_path / 'Releases/0.8.0/Optimus.exe'
    executable.parent.mkdir(parents=True)
    executable.write_bytes(b'fixture executable; never execute')
    source = executable.parent / 'source/src/optimus/maintenance.py'
    source.parent.mkdir(parents=True)
    source.write_bytes(b'fixture reviewed legacy pre-launch guard source\n')
    monkeypatch.setattr(recovery, 'LEGACY_SOURCE_SHA256', digest(source.read_bytes()))
    monkeypatch.setattr(recovery, '_current_owner', lambda: 'sid:fixture-owner')
    monkeypatch.setattr(recovery.time, 'time', lambda: 20000.0)
    packages = ['Fixture.First', 'Fixture.Second', 'Fixture.Third', 'Fixture.Later']
    offers = [dict(id=identity, package_id=identity, name=identity, source='winget', version='1.0', available='2.0') for identity in packages]
    items, compact = [], []
    for identity in packages[:2]:
        result = dict(id=identity, package_id=identity, source='winget', exitCode=0, status='completed',
            unattended=True, running=False, restartNeeded=None, from_version='1.0', to_version='2.0', text='Successfully installed')
        items.append(dict(id=identity, name=identity, status='completed', from_version='1.0', to_version='2.0', result=result))
        compact.append(dict(id=identity, status='completed', result=dict(exitCode=0, running=False, restartNeeded=None)))
    items.append(dict(id=packages[2], status='failed', error=recovery.LEGACY_CACHE_GUARD))
    compact.append(dict(id=packages[2], status='failed', result={}))
    journal = dict(schema=1, id=journal_id, parent_run_id=parent_id, owner='sid:fixture-owner', status='uncertain',
        started_at=1010.0, finished_at=2500.0, title=recovery._TITLE, installer_prepared=True,
        note='Original unresolved installer history; retain this note.',
        current_item=dict(id=packages[2], current=2, total=4, item=3,
            description='Updating approved package 3/4: Fixture.Third (1.0 to 2.0)'),
        result=dict(failed_items=1, cancelled=False, restartNeeded=False, stop_reason=recovery._STOP, items=compact))
    worker = data / 'parallel-updates' / (journal_id + '.json')
    parent_path = data / 'runs' / (parent_id + '.json')
    def audit(title, captured, started, finished):
        return dict(title=title, status='completed', started_at=started, finished_at=finished,
            result=dict(ApplicationVersion='0.8.0', ToolVersion='0.8.0',
                CapturedUtc=datetime.fromtimestamp(captured, timezone.utc).isoformat(),
                Processes=[dict(Name='Optimus.exe', ProcessId=123, StartTimeUtc='1970-01-01T00:08:20Z', Path=str(executable))]))
    result = dict(review_only=False, policy='all_supported', items=items, failed_items=1, skipped_items=0,
        requires_review_items=0, cancelled=False, deferred_ids=packages[3:], restartNeeded=False,
        stop_reason=recovery._STOP, scan=dict(exitCode=0, structured=True, source='winget', updates=offers))
    stage = dict(title=recovery._TITLE, name=recovery._TITLE, status='failed', background=True,
        started_at=1010.0, finished_at=2500.5, worker_log_path=str(worker), result=result,
        error='Keep the original failed stage and error.')
    parent = dict(id=parent_id, started_at=1000.0, finished_at=3000.0, status='completed_with_errors',
        cancelled=False, failed_stages=1, log_path=str(parent_path),
        settings=dict(software_updates_enabled=True, software_update_policy='all_supported',
            software_update_all_approved=True, software_update_agreements_accepted=True,
            software_update_exclude_ids=[], software_update_ids=[]),
        stages=[audit('Inspect current resource usage', 1005, 1000, 1009), stage,
                audit('Capture final resource report', 2600, 2590, 2610)])
    save(worker, journal)
    save(parent_path, parent)
    supporting = []
    for index, role in enumerate(('native_log', 'native_log', 'native_log_inventory', 'quiescence')):
        path = tmp_path / 'review-evidence' / (str(index) + '.json')
        save(path, dict(role=role, fixture=True))
        supporting.append(dict(path=str(path), sha256=digest(path.read_bytes()), kind=role))
    evidence = dict(schema=1, kind=recovery.REVIEW_KIND, owner='sid:fixture-owner', journal_id=journal_id,
        parent_run_id=parent_id, journal_sha256=digest(worker.read_bytes()), parent_sha256=digest(parent_path.read_bytes()),
        source_sha256=digest(source.read_bytes()), reviewed_at=19999.0, historical_runtime=recovery.REVIEW_RUNTIME,
        conclusion=recovery.REVIEW_CONCLUSION, failed_id=packages[2], prior_success_ids=packages[:2],
        native_log_scan=dict(window_started_at=1000.0, window_finished_at=2600.0, failed_package_command_absent=True),
        quiescence=dict(checked_at=19998.0, installer_processes=[]), supporting_files=supporting,
        note='Operator reviewed original source, the successful native receipts, command-log absence and current quiescence; historical arguments were unavailable.')
    return dict(data=data, journal_id=journal_id, parent_id=parent_id, worker=worker, parent_path=parent_path,
        journal=journal, parent=parent, evidence=evidence, executable=executable, source=source)


def inspect(case, **options):
    return recovery.inspect_legacy_cache_guard(case['data'], case['journal_id'], **options)


def rewrite(case):
    save(case['worker'], case['journal'])
    save(case['parent_path'], case['parent'])
    case['evidence'].update(journal_sha256=digest(case['worker'].read_bytes()), parent_sha256=digest(case['parent_path'].read_bytes()))


def test_missing_historical_arguments_stay_blocked_without_operator_review(case):
    before = set(case['data'].rglob('*'))
    result = inspect(case)
    assert result['status'] == 'blocked' and result['requires_operator_review']
    assert result['journal_sha256'] == case['evidence']['journal_sha256']
    assert set(case['data'].rglob('*')) == before


def test_complete_attestation_makes_only_the_exact_legacy_case_eligible(case):
    result = inspect(case, reviewed_evidence=case['evidence'])
    assert result['status'] == 'eligible' and result['reviewed']
    assert result['prior_success_ids'] == ['Fixture.First', 'Fixture.Second']
    assert result['failed_id'] == 'Fixture.Third' and not result['changed']
    assert not (case['data'] / 'update-recovery').exists()


def test_explicit_frozen_arguments_can_prove_runtime_without_review(case):
    for stage in (case['parent']['stages'][0], case['parent']['stages'][2]):
        stage['result']['Processes'][0]['CommandLine'] = [str(case['executable']), '--admin-relaunch']
    rewrite(case)
    assert inspect(case)['status'] == 'eligible'


def test_held_matching_lease_is_mandatory(case):
    for lease in (None, RunLease(case['data'])):
        result = recovery.reconcile_legacy_cache_guard(case['data'], case['journal_id'], lease=lease, reviewed_evidence=case['evidence'])
        assert result['status'] == 'blocked' and not result['changed']
    with RunLease(case['data'].parent / 'other-data') as wrong:
        result = recovery.reconcile_legacy_cache_guard(case['data'], case['journal_id'], lease=wrong, reviewed_evidence=case['evidence'])
        assert result['status'] == 'blocked'


def test_repair_preserves_history_and_distinguishes_batch_from_failed_attempt(case):
    journal_raw, parent_raw = case['worker'].read_bytes(), case['parent_path'].read_bytes()
    view = inspect(case, reviewed_evidence=case['evidence'])
    with RunLease(case['data']) as lease:
        result = recovery.reconcile_legacy_cache_guard(case['data'], case['journal_id'], lease=lease,
            reviewed_evidence=case['evidence'], inspection=view)
    assert result['status'] == 'reconciled' and result['changed'] and result['installer_actions'] == 0
    updated = json.loads(case['worker'].read_bytes())
    assert updated['status'] == 'needs_review' and updated['installer_prepared'] is True
    assert updated['note'] == case['journal']['note'] and updated['current_item'] == case['journal']['current_item']
    assert updated['result']['items'][:-1] == case['journal']['result']['items'][:-1]
    assert updated['result']['failed_items'] == 1 and updated['result']['stop_reason'] == recovery._STOP
    assert updated['result']['installer_started'] is True and updated['result']['outcomeKnown'] is True
    assert updated['result']['running'] is False and updated['result']['uncertain'] is False
    failed = updated['result']['items'][-1]
    assert failed['status'] == 'failed' and failed['error'] == recovery.LEGACY_CACHE_GUARD
    assert failed['result']['installer_started'] is False and failed['result']['outcomeKnown'] is True
    assert failed['result']['failureKind'] == 'preflight_failed'
    directory = Path(result['backup_directory'])
    assert (directory / 'journal.original.json').read_bytes() == journal_raw
    assert (directory / 'parent.original.json').read_bytes() == parent_raw == case['parent_path'].read_bytes()
    receipt = json.loads(Path(result['receipt']).read_bytes())
    assert receipt['status'] == 'completed' and receipt['repaired_sha256'] == digest(case['worker'].read_bytes())
    assert len(receipt['support']) == 4 and not (case['data'] / 'preferences.json').exists()


def test_reconciliation_is_idempotent_without_overwriting_any_original_backup(case):
    with RunLease(case['data']) as lease:
        first = recovery.reconcile_legacy_cache_guard(case['data'], case['journal_id'], lease=lease, reviewed_evidence=case['evidence'])
        assert first['status'] == 'reconciled'
        files = {path: path.read_bytes() for path in case['data'].rglob('*') if path.is_file() and path.name != 'maintenance-run.lock'}
        second = recovery.reconcile_legacy_cache_guard(case['data'], case['journal_id'], lease=lease)
        assert second['status'] == 'already_reconciled' and not second['changed']
        assert all(path.read_bytes() == raw for path, raw in files.items())
    assert inspect(case)['status'] == 'already_reconciled'


@pytest.mark.parametrize('change', [
    lambda c: c['journal'].update(owner='sid:other'),
    lambda c: c['journal'].update(id='c' * 32),
    lambda c: c['journal'].update(parent_run_id='c' * 32),
    lambda c: c['parent'].update(id='c' * 32),
    lambda c: c['parent'].update(status='running'),
    lambda c: c['journal'].update(finished_at=4000),
    lambda c: c['journal']['current_item'].update(id='Fixture.Other'),
    lambda c: c['journal']['current_item'].update(current=3),
    lambda c: c['journal']['current_item'].update(total=5),
    lambda c: c['journal']['result'].update(restartNeeded=True),
    lambda c: c['journal']['result'].update(running=True),
    lambda c: c['journal']['result'].update(outcomeKnown=False),
    lambda c: c['journal']['result']['items'][-1].update(installer_started=True),
    lambda c: c['journal']['result']['items'][-1].update(uncertain=True),
    lambda c: c['journal']['result']['items'][0]['result'].update(exitCode=5),
    lambda c: c['parent']['stages'][1]['result']['items'][0]['result'].update(running=True),
    lambda c: c['parent']['stages'][1]['result']['items'][0]['result'].update(outcomeKnown=False),
    lambda c: c['parent']['stages'][1]['result']['items'][0]['result'].update(restartNeeded=True),
    lambda c: c['parent']['stages'][1]['result']['items'][-1].update(error='Other installer error'),
    lambda c: c['parent']['stages'][1]['result']['items'][-1].update(result=dict(installer_started=True)),
    lambda c: c['parent']['stages'][1].update(worker_log_path='somewhere-else.json'),
    lambda c: c['parent']['stages'][1]['result'].update(deferred_ids=[]),
    lambda c: c['parent']['stages'][0]['result'].update(ApplicationVersion='0.9.0'),
    lambda c: c['parent']['stages'][0]['result']['Processes'][0].update(CommandLine=['Optimus.exe', '--source-workspace', 'custom']),
    lambda c: c['parent']['stages'][0]['result'].update(Runtime=dict(source_workspace='custom')),
])
def test_attestation_cannot_override_structural_or_lifecycle_contradictions(case, change):
    change(case)
    rewrite(case)  # Keep the review hashes valid: the contradiction itself must fail.
    assert inspect(case, reviewed_evidence=case['evidence'])['status'] == 'blocked'


@pytest.mark.parametrize('change', [
    lambda e: e.update(owner='sid:other'), lambda e: e.update(journal_sha256='0' * 64),
    lambda e: e.update(parent_sha256='0' * 64), lambda e: e.update(source_sha256='0' * 64),
    lambda e: e.update(reviewed_at=19000), lambda e: e['quiescence'].update(checked_at=19000),
    lambda e: e['quiescence'].update(installer_processes=[dict(name='msiexec.exe')]),
    lambda e: e.update(historical_runtime='frozen'), lambda e: e.update(conclusion='force retry'),
    lambda e: e['native_log_scan'].update(failed_package_command_absent=False),
    lambda e: e['native_log_scan'].update(window_finished_at=2000),
    lambda e: e.update(supporting_files=e['supporting_files'][:2]),
    lambda e: e['supporting_files'][0].update(sha256='0' * 64),
])
def test_incomplete_changed_or_expired_operator_evidence_stays_blocked(case, change):
    evidence = copy.deepcopy(case['evidence'])
    change(evidence)
    assert inspect(case, reviewed_evidence=evidence)['status'] == 'blocked'


def test_source_absent_or_changed_remains_blocked(case):
    case['source'].write_bytes(b'edited source')
    assert inspect(case, reviewed_evidence=case['evidence'])['status'] == 'blocked'
    case['source'].unlink()
    assert inspect(case, reviewed_evidence=case['evidence'])['status'] == 'blocked'


def test_duplicate_json_keys_are_not_a_valid_legacy_receipt(case):
    raw = case['worker'].read_bytes()
    case['worker'].write_bytes(raw.replace(b'"schema": 1', b'"schema": 1, "schema": 1'))
    case['evidence']['journal_sha256'] = digest(case['worker'].read_bytes())
    assert inspect(case, reviewed_evidence=case['evidence'])['status'] == 'blocked'


def test_hardlinked_evidence_is_rejected(case):
    original = case['worker']
    twin = original.with_suffix('.twin')
    try:
        os.link(original, twin)
    except OSError:
        pytest.skip('Hardlinks unavailable on this fixture filesystem')
    assert inspect(case, reviewed_evidence=case['evidence'])['status'] == 'blocked'


def test_reinspection_prevents_repair_after_inspected_bytes_change(case):
    view = inspect(case, reviewed_evidence=case['evidence'])
    case['journal']['note'] = 'changed after inspection'
    rewrite(case)
    with RunLease(case['data']) as lease:
        result = recovery.reconcile_legacy_cache_guard(case['data'], case['journal_id'], lease=lease,
            reviewed_evidence=case['evidence'], inspection=view)
    assert result['status'] == 'blocked' and not result['changed']
    assert not (case['data'] / 'update-recovery').exists()


def test_failed_final_audit_is_honestly_pending_then_completes_idempotently(case, monkeypatch):
    original_writer = recovery._private_json
    def write(path, value):
        if Path(path).name == 'receipt.json' and value.get('status') == 'completed':
            raise OSError('fixture final audit write failure')
        return original_writer(path, value)
    monkeypatch.setattr(recovery, '_private_json', write)
    with RunLease(case['data']) as lease:
        result = recovery.reconcile_legacy_cache_guard(case['data'], case['journal_id'], lease=lease, reviewed_evidence=case['evidence'])
        assert result['status'] == 'audit_pending' and result['changed']
        assert json.loads(case['worker'].read_bytes())['status'] == 'needs_review'
        monkeypatch.setattr(recovery, '_private_json', original_writer)
        again = recovery.reconcile_legacy_cache_guard(case['data'], case['journal_id'], lease=lease)
    assert again['status'] == 'already_reconciled' and not again['changed']


def test_backup_or_intent_failure_prevents_the_status_change(case, monkeypatch):
    before = case['worker'].read_bytes()
    monkeypatch.setattr(recovery, '_private_json', Mock(side_effect=OSError('fixture audit unavailable')))
    with RunLease(case['data']) as lease:
        result = recovery.reconcile_legacy_cache_guard(case['data'], case['journal_id'], lease=lease, reviewed_evidence=case['evidence'])
    assert result['status'] == 'blocked' and not result['changed']
    assert case['worker'].read_bytes() == before


@pytest.mark.parametrize('tamper', ['empty_review', 'review_owner', 'review_conclusion', 'receipt_parent', 'support_role', 'support_bytes', 'repaired_journal'])
def test_changed_archived_review_or_repair_is_not_reported_as_completed(case, tamper):
    with RunLease(case['data']) as lease:
        first = recovery.reconcile_legacy_cache_guard(case['data'], case['journal_id'], lease=lease, reviewed_evidence=case['evidence'])
        directory = Path(first['backup_directory'])
        receipt_path = Path(first['receipt'])
        receipt = json.loads(receipt_path.read_bytes())
        evidence_directory = directory / receipt['evidence_directory']
        if tamper in ('empty_review', 'review_owner', 'review_conclusion'):
            review_path = evidence_directory / 'review.original.json'
            value = json.loads(review_path.read_bytes())
            if tamper == 'empty_review':
                value = {}
            elif tamper == 'review_owner':
                value['owner'] = 'sid:other'
            else:
                value['conclusion'] = 'force an unknown outcome'
            save(review_path, value)
            receipt['review_sha256'] = digest(review_path.read_bytes())
        elif tamper == 'receipt_parent':
            receipt['parent_run_id'] = 'c' * 32
        elif tamper == 'support_role':
            receipt['support'][0]['kind'] = 'quiescence'
        elif tamper == 'support_bytes':
            (evidence_directory / receipt['support'][0]['name']).write_bytes(b'changed support')
        else:
            updated = json.loads(case['worker'].read_bytes())
            updated['result']['items'][-1]['status'] = 'completed'
            save(case['worker'], updated)
        save(receipt_path, receipt)
        result = recovery.reconcile_legacy_cache_guard(case['data'], case['journal_id'], lease=lease)
    assert result['status'] == 'blocked' and not result['changed']
    assert inspect(case)['status'] == 'blocked'


def test_archived_review_does_not_expire_after_successful_reconciliation(case, monkeypatch):
    with RunLease(case['data']) as lease:
        result = recovery.reconcile_legacy_cache_guard(case['data'], case['journal_id'], lease=lease, reviewed_evidence=case['evidence'])
        assert result['status'] == 'reconciled'
        monkeypatch.setattr(recovery.time, 'time', lambda: 20000.0 + 86400)
        # Original mutable log files may be removed after their exact bytes were
        # durably archived; validation must use the matching private backups.
        for entry in case['evidence']['supporting_files']:
            Path(entry['path']).unlink()
        assert recovery.reconcile_legacy_cache_guard(case['data'], case['journal_id'], lease=lease)['status'] == 'already_reconciled'


@pytest.mark.parametrize('failure', ['initial_intent', 'before_journal'])
def test_expired_review_can_be_replaced_append_only_after_prewrite_failure(case, monkeypatch, failure):
    writer = recovery._private_json
    def fail(path, value):
        if (failure == 'initial_intent' and Path(path).name == 'receipt.json'
                or failure == 'before_journal' and Path(path) == case['worker']):
            raise OSError('fixture prewrite failure')
        return writer(path, value)
    original_journal = case['worker'].read_bytes()
    monkeypatch.setattr(recovery, '_private_json', fail)
    with RunLease(case['data']) as lease:
        first = recovery.reconcile_legacy_cache_guard(case['data'], case['journal_id'], lease=lease, reviewed_evidence=case['evidence'])
        assert first['status'] == 'blocked' and not first['changed']
        directory = case['data'] / 'update-recovery' / case['journal_id']
        archived_before = {path: path.read_bytes() for path in directory.rglob('*') if path.is_file()}
        assert archived_before and case['worker'].read_bytes() == original_journal
        monkeypatch.setattr(recovery, '_private_json', writer)
        monkeypatch.setattr(recovery.time, 'time', lambda: 21000.0)
        fresh = copy.deepcopy(case['evidence'])
        fresh.update(reviewed_at=21000.0)
        fresh['quiescence']['checked_at'] = 20999.0
        for entry in fresh['supporting_files']:
            if entry['kind'] == 'quiescence':
                save(Path(entry['path']), dict(role='quiescence', checked_at=20999.0, installer_processes=[]))
                entry['sha256'] = digest(Path(entry['path']).read_bytes())
        result = recovery.reconcile_legacy_cache_guard(case['data'], case['journal_id'], lease=lease, reviewed_evidence=fresh)
        assert result['status'] == 'reconciled' and result['changed']
        assert len(list(directory.glob('review-*'))) == 2
        for path, raw in archived_before.items():
            if path.name != 'receipt.json':
                assert path.read_bytes() == raw
        if failure == 'before_journal':
            previous = list(directory.glob('receipt.previous-*.json'))
            assert len(previous) == 1 and previous[0].read_bytes() == archived_before[directory / 'receipt.json']
        assert recovery.reconcile_legacy_cache_guard(case['data'], case['journal_id'], lease=lease)['status'] == 'already_reconciled'
