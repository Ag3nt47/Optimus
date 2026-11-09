"""Update-only continuation uses real queue/receipt logic with fake installers."""
import copy
import json
from pathlib import Path
import threading
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from optimus.automation import RunLease
from optimus.bridge import PowerShellBridge
from optimus.continue_updates import continuation_exit_code, run_saved_updates
from optimus.navigation import NavigationHub
from optimus.parallel_updates import WINDOWS_DRIVERS, WINDOWS_SOFTWARE, pending_updates_barrier
from test_native_parallel_updates import Backend, driver_settings, enable_fixture_driver, settings


@pytest.fixture(autouse=True)
def fixture_services(monkeypatch):
    monkeypatch.setattr('optimus.speed_enhancements._current_owner', lambda: 'uid:1234')
    monkeypatch.setattr('optimus.runtime_priority.lower_worker_priority', lambda: lambda: None)


def service(tmp_path, config=None, *, native=False):
    backend = Backend(tmp_path, native=native)
    backend.release.set()
    backend.outcome = {'exitCode': 0, 'verified': True, 'installer_started': True,
                       'outcomeKnown': True, 'running': False, 'restartNeeded': False}
    config = config or settings()
    backend.preferences = Mock(side_effect=lambda: copy.deepcopy(config))
    backend.save_preferences = Mock(side_effect=AssertionError('Update continuation cannot save preferences'))
    backend.cleanup_apply = Mock(side_effect=AssertionError('No cleanup in an update-only command'))
    backend.debloat_apply = Mock(side_effect=AssertionError('No debloat in an update-only command'))
    backend.workflow = SimpleNamespace(navigation=NavigationHub())
    return backend


def assert_saved(report):
    assert json.loads(Path(report['report']).read_text(encoding='utf-8')) == report
    progress = json.loads(Path(report['progress_report']).read_text(encoding='utf-8'))
    assert progress['final'] is True and progress['run_id'] == report['id']
    assert report['updates_only'] is True and report['preferences_saved'] is False
    assert report['cleanup_actions_executed'] == 0 and report['presence_actions_executed'] == 0
    assert report['journals_deleted'] == 0


def test_exact_first_offer_hold_skips_only_it_and_installs_next_without_rewriting_settings(tmp_path):
    backend = service(tmp_path)
    preferences = tmp_path / 'preferences.json'
    original = b'{"fixture settings preserved": true}\n'
    preferences.write_bytes(original)
    hold_path = tmp_path / 'update-holds.json'
    hold = {'schema': 1, 'owner': 'uid:1234', 'holds': [
        {'id': 'Fixture.One', 'available_version': '2.0', 'reason': 'Publisher installer identifies an older release.'}]}
    hold_path.write_text(json.dumps(hold), encoding='utf-8')
    hold_path.chmod(0o600)
    original_hold = hold_path.read_bytes()
    report = run_saved_updates(backend)
    assert report['status'] == 'completed_with_skips' and continuation_exit_code(report) == 0
    assert 'update:Fixture.One' not in backend.calls
    assert 'update:Fixture.Two' in backend.calls
    rows = report['stages'][0]['result']['items']
    assert next(row for row in rows if row['id'] == 'Fixture.One')['status'] == 'skipped'
    assert next(row for row in rows if row['id'] == 'Fixture.Two')['status'] == 'completed'
    assert preferences.read_bytes() == original and hold_path.read_bytes() == original_hold
    backend.save_preferences.assert_not_called()
    backend.cleanup_apply.assert_not_called()
    backend.debloat_apply.assert_not_called()
    assert_saved(report)
    assert pending_updates_barrier(backend) is None


def test_known_first_native_failure_continues_next_and_does_not_claim_all_installed(tmp_path):
    backend = service(tmp_path)
    original = backend.update_app_unattended
    def update(identity, **kwargs):
        value = original(identity, **kwargs)
        return dict(value, exitCode=1) if identity == 'Fixture.One' else value
    backend.update_app_unattended = update
    report = run_saved_updates(backend)
    assert backend.calls.index('update:Fixture.Two') > backend.calls.index('update:Fixture.One')
    assert report['status'] == 'completed_with_skips'
    assert report['workers']['software_updates']['app_counts']['failed'] == 1
    assert report['workers']['software_updates']['app_counts']['updated'] == 1
    assert 'all_offers_installed' not in report
    assert pending_updates_barrier(backend) is None
    assert_saved(report)


@pytest.mark.parametrize('mode', ['unknown_result', 'unknown_exception'])
def test_unresolved_native_outcome_never_retries_or_starts_second_package(tmp_path, mode):
    backend = service(tmp_path)
    if mode == 'unknown_result':
        backend.outcome = {'exitCode': None, 'installer_started': True, 'outcomeKnown': False, 'running': True}
    else:
        backend.outcome = RuntimeError('Vendor reply is unknown')
    report = run_saved_updates(backend)
    assert backend.calls.count('update:Fixture.One') == 1
    assert 'update:Fixture.Two' not in backend.calls
    assert report['status'] == 'blocked' and continuation_exit_code(report) == 2
    assert report['queue_drained'] is False
    assert report['installation_block_reason']
    assert pending_updates_barrier(backend)
    assert_saved(report)
    # Another invocation may search, but cannot erase the original journal or
    # turn an unknown outcome into permission to launch another installer.
    journals = {path: path.read_bytes() for path in (tmp_path / 'parallel-updates').glob('*.json')}
    again = run_saved_updates(backend)
    assert again['status'] == 'blocked'
    assert again['queue_drained'] is False
    assert backend.calls.count('update:Fixture.One') == 1
    assert all(path.read_bytes() == value for path, value in journals.items())


def test_busy_global_lease_refuses_before_config_recovery_reports_or_installer_writes(tmp_path):
    backend = service(tmp_path)
    backend.reconcile_completed_updates = Mock(side_effect=AssertionError('Cannot recover while busy'))
    with RunLease(tmp_path):
        before = {path: path.read_bytes() for path in tmp_path.rglob('*')
                  if path.is_file() and path.name != 'maintenance-run.lock'}
        with pytest.raises(RuntimeError, match='already running maintenance'):
            run_saved_updates(backend, output=tmp_path / 'reports')
        after = {path: path.read_bytes() for path in tmp_path.rglob('*')
                 if path.is_file() and path.name != 'maintenance-run.lock'}
    assert before == after
    backend.preferences.assert_not_called()
    backend.reconcile_completed_updates.assert_not_called()
    assert not backend.calls


def test_pending_native_request_refuses_without_recovery_or_report_writes(tmp_path):
    backend = service(tmp_path)
    backend.bridge = PowerShellBridge(tmp_path / 'fake-engine')
    backend.bridge._pending['fixture-request'] = ('DriverUpdateApply', 999999)
    backend.reconcile_completed_updates = Mock(side_effect=AssertionError('Native request is still active'))
    with pytest.raises(RuntimeError, match='native Optimus request is still running'):
        run_saved_updates(backend, output=tmp_path / 'reports')
    assert not (tmp_path / 'reports').exists() and not (tmp_path / 'parallel-updates').exists()
    backend.preferences.assert_not_called()
    backend.reconcile_completed_updates.assert_not_called()
    assert not backend.calls


def test_app_and_driver_searches_overlap_while_real_installer_lane_is_exclusive(tmp_path):
    backend = service(tmp_path, driver_settings())
    app_search, driver_search = threading.Event(), threading.Event()
    original_app_scan = backend.updates_scan
    def app_scan():
        app_search.set()
        assert driver_search.wait(5), 'Driver discovery did not run alongside app discovery'
        return original_app_scan()
    enable_fixture_driver(backend, Mock(return_value={'resultCode': 2, 'hresult': 0,
        'aggregateResultCode': 2, 'aggregateHresult': 0, 'installer_started': True,
        'outcomeKnown': True, 'running': False, 'restartNeeded': False}))
    original_driver_scan = backend.driver_updates_scan
    def driver_scan():
        driver_search.set()
        assert app_search.wait(5), 'App discovery did not run alongside driver discovery'
        return original_driver_scan()
    backend.updates_scan, backend.driver_updates_scan = app_scan, driver_scan
    active, lock = [], threading.Lock()
    original_app_update = backend.update_app_unattended
    original_driver_update = backend.driver_update_apply_unattended
    def wrap(operation, label):
        def run(*args, **kwargs):
            with lock:
                assert not active, 'Two native installers overlap'
                active.append(label)
            try:
                with pytest.raises(RuntimeError):
                    RunLease(tmp_path).acquire()
                return operation(*args, **kwargs)
            finally:
                with lock:
                    assert active.pop() == label
        return run
    backend.update_app_unattended = wrap(original_app_update, 'app')
    backend.driver_update_apply_unattended = wrap(original_driver_update, 'driver')
    report = run_saved_updates(backend)
    assert report['status'] == 'completed'
    assert {stage['title'] for stage in report['stages']} == {WINDOWS_SOFTWARE, WINDOWS_DRIVERS}
    assert all(stage['result'].get('app_scan', stage['result'].get('driver_scan'))['complete'] for stage in report['stages'])
    assert backend.driver_backup_automatic.call_count == 1
    assert_saved(report)


def test_recovery_gets_existing_lease_before_new_pending_barrier_and_preserves_settings(tmp_path, monkeypatch):
    backend = service(tmp_path)
    def recover(*, lease):
        assert lease.handle is not None
        with pytest.raises(RuntimeError):
            RunLease(tmp_path).acquire()
        backend.calls.append('recover')
        return {'schema': 1, 'status': 'unchanged', 'reconciled': [], 'unresolved': [],
                'installers_started': 0, 'retries_started': 0, 'records_deleted': 0}
    backend.reconcile_completed_updates = recover
    from optimus.continue_updates import pending_updates_barrier as original
    def barrier(value):
        assert 'recover' in backend.calls
        return original(value)
    monkeypatch.setattr('optimus.continue_updates.pending_updates_barrier', barrier)
    report = run_saved_updates(backend)
    assert report['recovery']['status'] == 'unchanged'
    assert backend.calls.index('recover') < backend.calls.index('updates_scan')
    backend.save_preferences.assert_not_called()


def test_disabled_stages_never_get_enabled_and_do_not_search_or_recover(tmp_path):
    backend = service(tmp_path, settings(software_updates_enabled=False, driver_updates_enabled=False))
    backend.reconcile_completed_updates = Mock(side_effect=AssertionError('No enabled update stage'))
    report = run_saved_updates(backend)
    assert report['status'] == 'no_enabled_updates' and continuation_exit_code(report) == 0
    assert report['stages'] == [] and backend.calls == []
    backend.reconcile_completed_updates.assert_not_called()
    assert_saved(report)


def test_update_only_preserves_always_update_ignore_rules_but_never_presence_uninstalls(tmp_path, monkeypatch):
    # Use validated real descriptors, including a destructive presence rule.
    rules = [{'id': 'Fixture.One', 'source': 'winget', 'update_policy': 'always_ignore', 'presence': 'keep_uninstalled'},
             {'id': 'Fixture.Two', 'source': 'winget', 'update_policy': 'always_update', 'presence': 'keep_current'}]
    backend = service(tmp_path, settings(software_update_ids=[], software_update_policy='review', software_app_rules=rules))
    presence = Mock(side_effect=AssertionError('Update-only command cannot run app presence changes'))
    monkeypatch.setattr('optimus.app_presence.run_app_presence', presence)
    report = run_saved_updates(backend)
    assert 'update:Fixture.One' not in backend.calls and 'update:Fixture.Two' in backend.calls
    presence.assert_not_called()
    assert report['presence_actions_executed'] == 0


def test_broken_catalog_does_not_block_successful_other_channel_or_claim_empty_scan(tmp_path):
    backend = service(tmp_path, driver_settings())
    backend.updates_scan = Mock(side_effect=RuntimeError('Fixture catalog unavailable'))
    install = Mock(return_value={'resultCode': 2, 'hresult': 0, 'aggregateResultCode': 2, 'aggregateHresult': 0, 'installer_started': True,
                                'outcomeKnown': True, 'running': False, 'restartNeeded': False})
    enable_fixture_driver(backend, install)
    report = run_saved_updates(backend)
    stages = {stage['title']: stage for stage in report['stages']}
    assert stages[WINDOWS_SOFTWARE]['result']['app_scan']['complete'] is False
    assert stages[WINDOWS_DRIVERS]['result']['driver_scan']['complete'] is True
    install.assert_called_once()
    assert report['status'] == 'needs_review' and continuation_exit_code(report) == 2
    assert pending_updates_barrier(backend) is None
    assert_saved(report)


def test_progress_callback_failure_does_not_abandon_update_queue(tmp_path):
    backend = service(tmp_path)
    report = run_saved_updates(backend, progress_callback=Mock(side_effect=RuntimeError('Broken display')))
    assert report['status'] == 'completed'
    assert 'update:Fixture.Two' in backend.calls
    assert_saved(report)


@pytest.mark.parametrize('manager', ['brew', 'apt'])
def test_native_update_only_uses_saved_native_rules_and_never_windows_drivers_or_presence(tmp_path, monkeypatch, manager):
    from optimus.native_settings import NativePreferences
    identity = manager + ':fixture-two'
    rules = [{'id': identity, 'source': manager, 'update_policy': 'always_update', 'presence': 'keep_installed'}]
    config = NativePreferences.validate(dict(settings(), software_update_ids=[],
        software_update_policy='review', software_app_rules=rules))
    backend = service(tmp_path, config, native=True)
    backend.preference_store = SimpleNamespace(validate=NativePreferences.validate)
    backend.updates_scan = Mock(return_value={'structured': True, 'complete': True, 'exitCode': 0,
        'manager': manager, 'updates': [{'id': identity, 'version': '1.0', 'available': '2.0', 'source': manager}]})
    backend.driver_updates_scan = Mock(side_effect=AssertionError('No Windows driver channel on native OS'))
    presence = Mock(side_effect=AssertionError('Native update-only cannot install missing apps'))
    monkeypatch.setattr('optimus.app_presence.run_app_presence', presence)
    report = run_saved_updates(backend)
    assert report['status'] == 'completed'
    assert 'update:' + identity in backend.calls
    assert report['stages'][0]['result']['app_scan']['complete'] is True
    presence.assert_not_called()
    backend.driver_updates_scan.assert_not_called()
    assert_saved(report)


def test_review_policy_stays_read_only_even_with_available_updates(tmp_path):
    backend = service(tmp_path, settings(software_update_policy='review'))
    report = run_saved_updates(backend)
    assert report['status'] == 'completed'
    assert report['stages'][0]['result']['review_only'] is True
    assert report['workers']['software_updates']['offers_count'] == 2
    assert not any(value.startswith('update:') for value in backend.calls)
    assert 'all_offers_installed' not in report


def test_invalid_setup_does_not_implicitly_grant_update_authority(tmp_path):
    backend = service(tmp_path, settings(setup_version=0, software_update_policy='review'))
    with pytest.raises(ValueError, match='Complete and save the current setup'):
        run_saved_updates(backend)
    assert not backend.calls
    assert not (tmp_path / 'parallel-updates').exists()
    assert not (tmp_path / 'update-continuations').exists()


def test_global_cancel_does_not_start_an_installer_and_remains_visible(tmp_path):
    backend = service(tmp_path)
    cancelled = threading.Event()
    cancelled.set()
    report = run_saved_updates(backend, cancel_event=cancelled)
    assert report['status'] == 'cancelled' and continuation_exit_code(report) == 2
    assert not any(value.startswith('update:') for value in backend.calls)
    assert_saved(report)


@pytest.mark.parametrize('status,code', [('completed', 0), ('completed_with_skips', 0),
    ('no_enabled_updates', 0), ('blocked', 2), ('needs_review', 2), ('cancelled', 2), ('error', 2)])
def test_exit_status_distinguishes_drained_queue_from_blocked_run(status, code):
    assert continuation_exit_code({'status': status}) == code


def test_initial_boot_proof_is_durable_and_bound_before_any_update_worker(tmp_path, monkeypatch):
    import time
    from datetime import datetime, timezone
    from optimus import automatic_update_recovery as recovery
    backend = service(tmp_path)
    owner, boot = recovery._current_owner(), round(time.time() - 3600)
    monkeypatch.setattr('optimus.speed_enhancements._current_owner', lambda: owner)
    utc = datetime.fromtimestamp(boot, timezone.utc).isoformat()
    monkeypatch.setattr(recovery, '_current_owner', lambda: owner)
    monkeypatch.setattr('optimus.update_resume.boot_identity', lambda: boot)
    def runtime(owner, journal_id, parent_id):
        return {'schema': 1, 'kind': recovery.RUNTIME_OWNER_KIND, 'owner': owner,
            'journal_id': journal_id, 'parent_run_id': parent_id, 'pid': 44,
            'created_at': boot + 10, 'created_utc': datetime.fromtimestamp(boot + 10, timezone.utc).isoformat(),
            'executable': r'D:\Optimus\OptimusConsole.exe', 'name': 'OptimusConsole.exe',
            'captured_at': time.time(), 'frozen': True}
    monkeypatch.setattr(recovery, 'capture_runtime_owner', runtime)
    backend.automatic_update_recovery_probe = Mock(side_effect=lambda: {
        'schema': 1, 'owner': owner, 'complete': True, 'review_only': True,
        'captured_at': time.time(), 'boot_utc': utc, 'boot_event_utc': utc})
    original_update = backend.update_app_unattended
    checked = []
    def update(identity, **options):
        journals = list((tmp_path / 'parallel-updates').glob('*.json'))
        assert journals
        for path in journals:
            record = json.loads(path.read_text())
            reference = record['continuation_boot_provenance']
            proof_path = tmp_path / 'update-continuation-provenance' / (record['parent_run_id'] + '.json')
            import hashlib
            assert hashlib.sha256(proof_path.read_bytes()).hexdigest() == reference['sha256']
            assert json.loads(proof_path.read_text())['captured_at'] <= record['started_at']
        with pytest.raises(RuntimeError):
            RunLease(tmp_path).acquire()
        checked.append(identity)
        return original_update(identity, **options)
    backend.update_app_unattended = update
    report = run_saved_updates(backend, output=tmp_path / 'operator-output')
    assert report['status'] == 'completed' and checked
    assert report['boot_provenance']['status'] == 'captured'
    backend.automatic_update_recovery_probe.assert_called_once()
    assert_saved(report)


def test_unavailable_optional_initial_boot_proof_does_not_revoke_saved_update_approval(tmp_path):
    backend = service(tmp_path)
    backend.automatic_update_recovery_probe = Mock(side_effect=OSError('Fixture native boot unavailable'))
    report = run_saved_updates(backend)
    assert report['status'] == 'completed'
    assert report['boot_provenance']['status'] == 'unavailable'
    assert 'update:Fixture.One' in backend.calls and 'update:Fixture.Two' in backend.calls
    assert all('continuation_boot_provenance' not in json.loads(path.read_text())
               for path in (tmp_path / 'parallel-updates').glob('*.json'))
    assert_saved(report)
