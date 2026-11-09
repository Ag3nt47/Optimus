"""Production package guards with disposable metadata and no real subprocess."""
import json
import subprocess
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from optimus.automated_software import run_software
from optimus.maintenance import Maintenance, UpdatePreflightError
from optimus.navigation import can_fail_forward, shared_update_refusal
from optimus.parallel_updates import installer_barrier
from optimus.workflow import DEFAULTS


IDENTITIES = ('Vendor.First', 'Vendor.Middle', 'Vendor.Last')


def table(rows):
    header = f"{'Name':24}{'Id':28}{'Version':16}{'Available':16}Source"
    body = [f'{identity:24}{identity:28}{version:16}{available:16}winget'
            for identity, version, available in rows]
    return '\n'.join([header, '-' * 100, *body, str(len(rows)) + ' upgrades available.', ''])


def configuration(policy='all_supported'):
    return dict(DEFAULTS, setup_version=2, software_updates_enabled=True,
        software_update_policy=policy, software_update_all_approved=policy == 'all_supported',
        software_update_ids=list(IDENTITIES) if policy == 'selected' else [],
        software_update_agreements_accepted=True)


def fixture_backend(tmp_path, *, later_rows=None, pin_output='There are no pins configured.', source_failure=False):
    tool = Maintenance(None, tmp_path)
    tool._winget = Mock(return_value='fixture-winget.exe')
    launches, listing_count = [], 0
    initial = [(identity, '1.0', '2.0') for identity in IDENTITIES]
    def native(arguments, **options):
        nonlocal listing_count
        assert arguments[0] == 'fixture-winget.exe'
        if arguments[1:] == ['upgrade', '--source', 'winget', '--disable-interactivity']:
            listing_count += 1
            output = ('Unrecognized source metadata' if source_failure and listing_count > 1 else
                      table(initial if listing_count == 1 or later_rows is None else later_rows))
            return subprocess.CompletedProcess(arguments, 0, output, '')
        if arguments[1:] == ['pin', 'list', '--disable-interactivity']:
            return subprocess.CompletedProcess(arguments, 0, pin_output, '')
        if arguments[1] == 'list':
            identity = arguments[arguments.index('--id') + 1]
            assert identity in launches
            assert arguments[arguments.index('--source') + 1] == 'winget'
            assert '--exact' in arguments and '--disable-interactivity' in arguments
            return subprocess.CompletedProcess(arguments, 0, table([(identity, '2.0', '2.0')]), '')
        assert arguments[1] == 'upgrade' and '--id' in arguments and '--exact' in arguments
        assert arguments[arguments.index('--source') + 1] == 'winget'
        assert arguments[arguments.index('--version') + 1] == '2.0'
        assert '--silent' in arguments and '--disable-interactivity' in arguments
        assert not any(value in arguments for value in ('--all','--ignore-security-hash','--ignore-local-archive-malware-scan','--include-unknown','--allow-reboot'))
        launches.append(arguments[arguments.index('--id') + 1])
        return subprocess.CompletedProcess(arguments, 0, 'Fixture exact native update returned.', '')
    tool._run = Mock(side_effect=native)
    backend = SimpleNamespace(data_dir=tmp_path, maintenance=tool, updates_scan=tool.updates_scan,
        update_app_unattended=lambda identity, **kwargs: tool.updates_apply(identity, confirmed=True,
            unattended=True, **kwargs))
    return backend, tool, launches


@pytest.mark.parametrize('issue', ['missing', 'ambiguous', 'unsupported_version'])
@pytest.mark.parametrize('policy', ['all_supported', 'selected'])
def test_middle_target_lost_during_first_revalidation_does_not_stop_last_package(tmp_path, issue, policy):
    later = [(IDENTITIES[0], '1.0', '2.0'), (IDENTITIES[2], '1.0', '2.0')]
    if issue == 'ambiguous':
        later[1:1] = [(IDENTITIES[1], '1.0', '2.0')] * 2
    elif issue == 'unsupported_version':
        later.insert(1, (IDENTITIES[1], 'Unknown', '2.0'))
    backend, tool, launches = fixture_backend(tmp_path, later_rows=later)
    result = run_software(backend, configuration(policy))
    assert launches == [IDENTITIES[0], IDENTITIES[2]]
    assert [row['status'] for row in result['items']] == ['completed','failed','completed']
    for row in (result['items'][0], result['items'][2]):
        assert row['result']['verified'] is True and row['result']['installed_version'] == '2.0'
    middle = result['items'][1]['result']
    assert middle['failure_scope'] == 'package' and middle['failureKind'] == 'package_unavailable'
    assert middle['installer_started'] is False and middle['outcomeKnown'] is True
    assert result['deferred_ids'] == [] and result['failed_items'] == 1
    assert installer_barrier(result) is None


def test_middle_exact_hold_preserves_hold_and_updates_both_other_packages(tmp_path):
    from optimus.speed_enhancements import _current_owner
    owner = _current_owner()
    hold = {'schema':1,'owner':owner,'holds':[{'id':IDENTITIES[1], 'available_version':'2.0',
        'reason':'Fixture reviewed catalog payload mismatch; preserve exact offer hold.'}]}
    path = tmp_path / 'update-holds.json'
    path.write_text(json.dumps(hold), encoding='utf-8'); path.chmod(0o600)
    before = path.read_bytes()
    backend, tool, launches = fixture_backend(tmp_path)
    result = run_software(backend, configuration())
    assert launches == [IDENTITIES[0], IDENTITIES[2]]
    assert result['held_ids'] == [IDENTITIES[1]] and result['held_count'] == 1
    row = next(row for row in result['items'] if row['id'] == IDENTITIES[1])
    assert row['status'] == 'skipped' and row['result']['failureKind'] == 'automatic_update_hold'
    assert result['deferred_ids'] == [] and installer_barrier(result) is None
    assert path.read_bytes() == before


def test_whole_source_revalidation_still_stops_remaining_app_batch(tmp_path):
    backend, tool, launches = fixture_backend(tmp_path, source_failure=True)
    result = run_software(backend, configuration())
    assert launches == [] and result['deferred_ids'] == list(IDENTITIES[1:])
    evidence = result['items'][0]['result']
    assert evidence['failure_scope'] == 'app_batch' and evidence['failureKind'] == 'source_revalidation'
    assert evidence['installer_started'] is False and not can_fail_forward(evidence)


def test_unknown_pin_inventory_still_closes_shared_installer_gate(tmp_path):
    backend, tool, launches = fixture_backend(tmp_path, pin_output='Unrecognized pin inventory')
    result = run_software(backend, configuration())
    assert launches == [] and result['deferred_ids'] == list(IDENTITIES[1:])
    evidence = result['items'][0]['result']
    assert evidence['failureKind'] == 'pin_inventory_untrusted' and evidence['shared_barrier'] is True
    assert installer_barrier(result) and not can_fail_forward(evidence)


def test_pin_read_exception_remains_explicit_shared_guard(tmp_path):
    backend, tool, launches = fixture_backend(tmp_path)
    tool._pin_status = Mock(side_effect=RuntimeError('Fixture unreadable pin state'))
    result = run_software(backend, configuration())
    evidence = result['items'][0]['result']
    assert launches == [] and evidence['failure_scope'] == 'shared'
    assert evidence['failureKind'] == 'pin_inventory_untrusted' and installer_barrier(result)


def test_typed_preflight_default_stays_app_batch_and_package_scope_is_explicit():
    def evidence(error):
        return dict(vars(error), installer_started=error.installer_started,
                    outcomeKnown=error.outcomeKnown, running=error.running)
    ordinary = UpdatePreflightError('Fixture generic preflight refusal')
    assert ordinary.failure_scope == 'app_batch' and ordinary.failureKind == 'preflight_failed'
    assert not can_fail_forward(evidence(ordinary))
    package = UpdatePreflightError('Fixture exact package unavailable', failure_scope='package', failureKind='package_unavailable')
    assert package.installer_started is False and package.outcomeKnown is True and package.running is False
    assert package.failure_scope == 'package' and shared_update_refusal(vars(package)) is None
    assert can_fail_forward(evidence(package))
    with pytest.raises(ValueError): UpdatePreflightError('Bad fixture scope', failure_scope='automatic')
    with pytest.raises(ValueError): UpdatePreflightError('Bad fixture kind', failureKind='package\ncommand')


def test_changed_cached_version_is_package_local_before_any_native_call(tmp_path):
    backend, tool, launches = fixture_backend(tmp_path)
    tool.updates_scan()
    tool._last_update_packages[IDENTITIES[1]]['available'] = '1.0'
    previous = tool._run.call_count
    with pytest.raises(UpdatePreflightError) as caught:
        tool.updates_apply(IDENTITIES[1], confirmed=True, unattended=True)
    assert caught.value.failure_scope == 'package' and caught.value.failureKind == 'version_changed'
    assert tool._run.call_count == previous and launches == []


def test_missing_approval_stays_shared_and_does_not_probe_native_manager(tmp_path):
    backend, tool, launches = fixture_backend(tmp_path)
    with pytest.raises(UpdatePreflightError) as caught:
        tool.updates_apply(IDENTITIES[0], confirmed=False)
    assert caught.value.failure_scope == 'shared' and caught.value.failureKind == 'approvals_changed'
    assert launches == [] and tool._run.call_count == 0


def test_missing_winget_stops_app_batch_without_trying_other_targets(tmp_path):
    backend, tool, launches = fixture_backend(tmp_path)
    tool._winget.side_effect = ['fixture-winget.exe'] * 3 + [FileNotFoundError('Fixture package manager disappeared')]
    result = run_software(backend, configuration())
    evidence = result['items'][0]['result']
    assert launches == [] and result['deferred_ids'] == list(IDENTITIES[1:])
    assert evidence['failure_scope'] == 'app_batch' and evidence['failureKind'] == 'manager_unavailable'
    assert not can_fail_forward(evidence)


def test_process_launch_permission_refusal_stays_shared(tmp_path):
    backend, tool, launches = fixture_backend(tmp_path)
    ordinary = tool._run.side_effect
    def native(arguments, **options):
        if '--id' in arguments:
            raise PermissionError('Fixture executable permission denied before CreateProcess')
        return ordinary(arguments, **options)
    tool._run.side_effect = native
    result = run_software(backend, configuration())
    evidence = result['items'][0]['result']
    assert launches == [] and result['deferred_ids'] == list(IDENTITIES[1:])
    assert evidence['failure_scope'] == 'shared' and evidence['failureKind'] == 'authorization'
    assert installer_barrier(result)


def test_parent_console_preflight_preserves_the_proven_permission_refusal(tmp_path):
    from optimus.winget_console import ConsolePreflightError
    backend, tool, launches = fixture_backend(tmp_path)
    original = tool._run.side_effect
    def native(arguments, **options):
        if arguments[1:3] == ['upgrade', '--id']:
            try:
                raise PermissionError('Fixture access denied before supervisor start')
            except PermissionError as error:
                raise ConsolePreflightError('Fixture console launch was refused.') from error
        return original(arguments, **options)
    tool._run.side_effect = native
    result = run_software(backend, configuration())
    evidence = result['items'][0]['result']
    assert launches == [] and result['deferred_ids'] == list(IDENTITIES[1:])
    assert evidence['failure_scope'] == 'shared' and evidence['failureKind'] == 'authorization'
    assert evidence['installer_started'] is False and evidence['outcomeKnown'] is True
    assert installer_barrier(result)


def test_activation_evidence_constructor_failure_skips_only_the_bound_package(tmp_path, monkeypatch):
    from optimus import app_update_verification
    original = app_update_verification.AppUpdateVerification
    def make(data_dir, package):
        if package['id'] == IDENTITIES[1]:
            raise OSError('Fixture private diagnostic identity unavailable')
        return original(data_dir, package)
    monkeypatch.setattr(app_update_verification, 'AppUpdateVerification', make)
    backend, tool, launches = fixture_backend(tmp_path)
    result = run_software(backend, configuration())
    evidence = result['items'][1]['result']
    assert launches == [IDENTITIES[0], IDENTITIES[2]]
    assert evidence['failure_scope'] == 'package' and evidence['failureKind'] == 'activation_evidence_unavailable'
    assert evidence['installer_started'] is False and evidence['outcomeKnown'] is True and evidence['running'] is False
    assert result['failed_items'] == 1 and result['deferred_ids'] == [] and installer_barrier(result) is None


@pytest.mark.parametrize('stage,code,scope,kind', [
    ('create_target', 1237, 'package', 'console_target_not_started'),
    ('create_target', 32, 'package', 'console_target_not_started'),
    ('create_target', 33, 'package', 'console_target_not_started'),
    ('prepare', 32, 'app_batch', 'manager_unavailable'),
    ('create_target', 87, 'app_batch', 'manager_unavailable'),
    ('create_target', 2, 'app_batch', 'manager_unavailable'),
    ('create_target', 3, 'app_batch', 'manager_unavailable'),
    ('create_target', 193, 'app_batch', 'manager_unavailable'),
    ('create_target', 5, 'shared', 'authorization'),
    ('prepare', 740, 'shared', 'authorization'),
    ('create_target', 112, 'shared', 'insufficient_disk'),
    ('create_target', 1260, 'shared', 'security_policy'),
])
def test_authenticated_console_refusal_preserves_the_scope_of_its_native_reason(tmp_path, stage, code, scope, kind):
    from optimus.winget_console import ConsoleTargetNotStartedError
    backend, tool, launches = fixture_backend(tmp_path)
    original = tool._run.side_effect
    attempts = []
    def native(arguments, **options):
        if arguments[1] == 'upgrade' and '--id' in arguments:
            identity = arguments[arguments.index('--id') + 1]
            attempts.append(identity)
            if identity == IDENTITIES[1]:
                raise ConsoleTargetNotStartedError(stage, code)
        return original(arguments, **options)
    tool._run.side_effect = native
    result = run_software(backend, configuration())
    evidence = result['items'][1]['result']
    assert evidence['failure_scope'] == scope and evidence['failureKind'] == kind
    assert evidence['installer_started'] is False and evidence['outcomeKnown'] is True and evidence['running'] is False
    assert attempts == list(IDENTITIES if scope == 'package' else IDENTITIES[:2])
    assert launches == ([IDENTITIES[0], IDENTITIES[2]] if scope == 'package' else [IDENTITIES[0]])
    assert bool(installer_barrier(result)) is (scope == 'shared')


@pytest.mark.parametrize('code,kind', [(0x8A150008, 'download_failed'),
    (0x8A15002E - 2**32, 'download_size_mismatch'), (0x8A150011, 'installer_hash_mismatch')])
def test_verified_native_download_failure_keeps_reason_and_continues_exact_batch(tmp_path, code, kind):
    from test_update_payload_providers import terminal
    backend, tool, launches = fixture_backend(tmp_path)
    original = tool._run.side_effect
    def native(arguments, **options):
        if arguments[1:4] == ['upgrade', '--id', IDENTITIES[1]]:
            launches.append(IDENTITIES[1])
            assert not any(flag in arguments for flag in ('--ignore-security-hash', '--ignore-local-archive-malware-scan'))
            return terminal(code)
        return original(arguments, **options)
    tool._run.side_effect = native
    result = run_software(backend, configuration())
    evidence = result['items'][1]['result']
    assert launches == list(IDENTITIES)
    assert evidence['exitCode'] == code and evidence['failureKind'] == kind and evidence['failure_scope'] == 'package'
    assert evidence['installer_started'] is True and evidence['outcomeKnown'] is True and evidence['running'] is False
    assert evidence['verified'] is False and 'No retry or security bypass' in evidence['note']
    assert result['failed_items'] == 1 and result['deferred_ids'] == [] and installer_barrier(result) is None


@pytest.mark.parametrize('known_terminal', [True, False])
def test_native_queue_continues_terminal_package_failure_but_persists_real_timeout_barrier(tmp_path, monkeypatch, known_terminal):
    from optimus.automation import RunLease
    from optimus.parallel_updates import ParallelInstallerQueue, ProgressMux, WINDOWS_SOFTWARE, pending_updates_barrier
    from test_update_payload_providers import terminal

    monkeypatch.setattr('optimus.runtime_priority.lower_worker_priority', lambda: lambda: None)
    backend, tool, launches = fixture_backend(tmp_path)
    backend.system = 'win32'
    backend.bridge = SimpleNamespace(_process=None)
    original = tool._run.side_effect

    def native(arguments, **options):
        if arguments[1:4] == ['upgrade', '--id', IDENTITIES[1]]:
            launches.append(IDENTITIES[1])
            if known_terminal:
                return terminal(0x8A150008)
            raise subprocess.TimeoutExpired(arguments, 1800)
        return original(arguments, **options)

    tool._run.side_effect = native
    def operation(worker, callback, prepared, before):
        return run_software(worker, configuration(), item_callback=callback,
            prepared_callback=prepared, before_install=before)
    queue = ParallelInstallerQueue(backend, 'a' * 32,
        [(WINDOWS_SOFTWARE, operation)], ProgressMux(lambda _: None))
    with RunLease(tmp_path):
        queue.start()
        try:
            assert queue.wait_prepared() is None
            queue.release_maintenance()
            queue.join()
        finally:
            queue.cancel()
            queue.join()
    job = queue.jobs[0]
    saved = json.loads(job.path.read_text())
    middle = saved['result']['items'][1]['result']
    assert launches == list(IDENTITIES if known_terminal else IDENTITIES[:2])
    assert saved['status'] == ('needs_review' if known_terminal else 'uncertain')
    assert middle['failureKind'] == ('download_failed' if known_terminal else 'update_timeout')
    assert middle['outcomeKnown'] is known_terminal and middle['running'] is not known_terminal
    assert bool(queue.barrier) is not known_terminal
    assert bool(pending_updates_barrier(backend)) is not known_terminal
    if known_terminal:
        assert job.result['deferred_ids'] == []
    else:
        assert job.result['deferred_ids'] == [IDENTITIES[2]]
        assert middle['note'] == 'The update timed out. A vendor installer may still be running; verify its outcome before trying again.'
