"""Exact owned Python failure evidence; no native mutation or installation."""
import copy
import hashlib
import json
import os
from types import SimpleNamespace
import threading
import time

import pytest

from optimus import python_relocation_automation as automation
from optimus.app_update_verification import AppUpdateVerification


PACKAGE = {'id': 'Python.Python.3.14', 'source': 'winget', 'version': '3.14.0', 'available': '3.14.1'}
APPROVAL = {'package_id': PACKAGE['id'], 'source': r'C:\Users\Fixture\AppData\Local\Programs\Python',
            'target': r'D:\Applications\Python', 'reparse_sha256': 'a' * 64}
DLL = APPROVAL['source'] + r'\Python314\python3.dll'


def native(status, *, code='fixture', mutated=False):
    return dict(status=status, code=code, note='Fixture native check.', mutated=mutated,
        probe_error=0 if status in ('repaired', 'already_trusted') else 448)


def seed_attempt(backend, *, package=None, changes=None, error=1326, win32=448, log_name='winget_000_core_JustForMe.log'):
    package = package or PACKAGE
    service = AppUpdateVerification(backend.data_dir, package,
        directory_policy=lambda path, owner: path.mkdir(mode=0o700))
    folder = service.prepare().parent
    row = dict(schema=1, owner=service.owner, id=package['id'], from_version=package['version'],
        to_version=package['available'], status='not_verified', recorded_at=time.time())
    row.update(changes or {})
    (folder / 'verification.json').write_text(json.dumps(row), encoding='utf-8')
    log = folder / log_name
    log.write_text(f'MSI (s) [12:00:00]: Error {error}. Error getting file security: {DLL} GetLastError: {win32}\n',
        encoding='utf-16')
    return service, folder, log


@pytest.fixture
def fixture(tmp_path, monkeypatch):
    monkeypatch.setattr(automation, '_windows', lambda: True)
    monkeypatch.setattr(automation.RunLease, 'owned_by_current_process',
        classmethod(lambda cls, data_dir: True), raising=False)
    settings = {'python_relocation_repair_enabled': True, 'python_relocation_approvals': [dict(APPROVAL)]}
    backend = SimpleNamespace(data_dir=tmp_path, preferences=lambda: copy.deepcopy(settings),
        maintenance=SimpleNamespace(_last_update_packages={PACKAGE['id']: dict(PACKAGE)},
            _last_update_ids={PACKAGE['id']}, _last_update_scan=time.time()))
    calls = []
    def preflight(approval):
        calls.append(('preflight', approval.as_dict()))
        return native('ready')
    def repair(approval):
        calls.append(('repair', approval.as_dict()))
        return native('repaired', mutated=True)
    monkeypatch.setattr(automation, 'preflight_python_relocation', preflight)
    monkeypatch.setattr(automation, 'repair_python_relocation', repair)
    return backend, settings, calls


def assert_skipped(result, calls):
    assert result['proceed'] is False
    assert result['installer_started'] is False
    assert result['outcomeKnown'] is True and result['running'] is False
    assert result['failure_scope'] == 'package'
    assert not any(call[0] == 'repair' for call in calls)


def test_exact_owned_failure_repairs_once_and_preserves_preferences(fixture):
    backend, settings, calls = fixture
    service, folder, log = seed_attempt(backend)
    original = copy.deepcopy(settings)
    result = automation.prepare_python_update(backend, PACKAGE['id'])
    assert result['status'] == 'repaired' and result['proceed'] is True
    assert result['installer_started'] is False and result['running'] is False
    assert [row[0] for row in calls] == ['preflight', 'repair']
    assert calls[-1][1] == APPROVAL
    assert result['offer'] == PACKAGE and result['approval'] == APPROVAL
    assert result['evidence'][0]['attempt'] == folder.name
    assert result['evidence'][0]['log_sha256'] == hashlib.sha256(log.read_bytes()).hexdigest()
    assert settings == original
    assert not (backend.data_dir / 'preferences.json').exists()
    json.dumps(result, allow_nan=False)


@pytest.mark.parametrize('package_id', ['Vendor.App', 'Python.Python.2.7', 'Python.Python.3.14.extra', None])
def test_non_python_never_reads_settings_or_evidence(fixture, package_id):
    backend, settings, calls = fixture
    backend.preferences = lambda: pytest.fail('Non-Python must not inspect settings.')
    assert automation.prepare_python_update(backend, package_id) is None
    assert calls == []


def test_other_platform_is_a_noop(fixture, monkeypatch):
    backend, settings, calls = fixture
    monkeypatch.setattr(automation, '_windows', lambda: False)
    backend.preferences = lambda: pytest.fail('Native platforms must not inspect Windows settings.')
    assert automation.prepare_python_update(backend, PACKAGE['id']) is None
    assert calls == []


def test_disabled_automation_preserves_normal_updater(fixture):
    backend, settings, calls = fixture
    settings['python_relocation_repair_enabled'] = False
    assert automation.prepare_python_update(backend, PACKAGE['id']) is None
    assert calls == []
    assert list(backend.data_dir.iterdir()) == []


def test_no_failure_history_is_a_noop(fixture):
    backend, settings, calls = fixture
    assert automation.prepare_python_update(backend, PACKAGE['id']) is None
    assert calls == []


@pytest.mark.parametrize('error,win32', [(1326, 5), (1603, 448)])
def test_other_native_failures_never_authorize_repair(fixture, error, win32):
    backend, settings, calls = fixture
    seed_attempt(backend, error=error, win32=win32)
    assert automation.prepare_python_update(backend, PACKAGE['id']) is None
    assert calls == []


def test_unanchored_outside_log_cannot_authorize_repair(fixture):
    backend, settings, calls = fixture
    outside = backend.data_dir / 'winget_000_core_JustForMe.log'
    outside.write_text(f'Error 1326. Error getting file security: {DLL} GetLastError: 448\n', encoding='utf-16')
    assert automation.prepare_python_update(backend, PACKAGE['id']) is None
    assert calls == []


def test_different_offer_history_is_not_selected(fixture):
    backend, settings, calls = fixture
    seed_attempt(backend, package=dict(PACKAGE, available='3.14.2'))
    assert automation.prepare_python_update(backend, PACKAGE['id']) is None
    assert calls == []


@pytest.mark.parametrize('changes', [dict(owner='another-owner'), dict(id='Vendor.App'),
    dict(from_version='3.13.0'), dict(to_version='3.14.2'), dict(schema=True)])
def test_attempt_identity_mismatch_skips_only_this_package(fixture, changes):
    backend, settings, calls = fixture
    seed_attempt(backend, changes=changes)
    assert_skipped(automation.prepare_python_update(backend, PACKAGE['id']), calls)
    assert calls == []


def test_scope_owner_anchor_must_match_current_owner(fixture):
    backend, settings, calls = fixture
    service, folder, log = seed_attempt(backend)
    anchor = service.directory / 'owner.json'
    row = json.loads(anchor.read_text(encoding='utf-8')); row['owner'] = 'another-owner'
    anchor.write_text(json.dumps(row), encoding='utf-8')
    assert_skipped(automation.prepare_python_update(backend, PACKAGE['id']), calls)


@pytest.mark.parametrize('status', ['verified', 'pending_activation', 'pending_app_close', 'pending_verification'])
def test_success_or_activation_history_defers_to_existing_updater_checks(fixture, status):
    backend, settings, calls = fixture
    seed_attempt(backend, changes=dict(status=status))
    assert automation.prepare_python_update(backend, PACKAGE['id']) is None
    assert calls == []


def test_missing_explicit_mapping_cannot_repair(fixture):
    backend, settings, calls = fixture
    seed_attempt(backend)
    settings['python_relocation_approvals'] = []
    result = automation.prepare_python_update(backend, PACKAGE['id'])
    assert_skipped(result, calls)
    assert 'explicit saved mapping' in result['reason']


@pytest.mark.parametrize('change', [dict(package_id='Python.Python.3.13'),
    dict(source=r'C:\Users\SomeoneElse\AppData\Local\Programs\Python')])
def test_approval_for_other_package_or_path_is_not_reused(fixture, change):
    backend, settings, calls = fixture
    seed_attempt(backend)
    settings['python_relocation_approvals'] = [dict(APPROVAL, **change)]
    assert_skipped(automation.prepare_python_update(backend, PACKAGE['id']), calls)


def test_duplicate_mapping_is_ambiguous(fixture):
    backend, settings, calls = fixture
    seed_attempt(backend)
    settings['python_relocation_approvals'].append(dict(APPROVAL))
    assert_skipped(automation.prepare_python_update(backend, PACKAGE['id']), calls)


def test_nonordinary_hardlinked_native_log_cannot_authorize_repair(fixture):
    backend, settings, calls = fixture
    service, folder, log = seed_attempt(backend)
    os.link(log, backend.data_dir / 'unowned-copy.log')
    assert_skipped(automation.prepare_python_update(backend, PACKAGE['id']), calls)
    assert calls == []


def test_nonordinary_hardlinked_receipt_cannot_authorize_repair(fixture):
    backend, settings, calls = fixture
    service, folder, log = seed_attempt(backend)
    os.link(folder / 'verification.json', backend.data_dir / 'unowned-receipt.json')
    assert_skipped(automation.prepare_python_update(backend, PACKAGE['id']), calls)


def test_oversized_log_is_bounded(fixture):
    backend, settings, calls = fixture
    service, folder, log = seed_attempt(backend)
    log.write_bytes(b'x' * (automation._MAX_LOG + 1))
    assert_skipped(automation.prepare_python_update(backend, PACKAGE['id']), calls)


def test_noncanonical_log_name_is_ignored(fixture):
    backend, settings, calls = fixture
    seed_attempt(backend, log_name='newest.log')
    assert automation.prepare_python_update(backend, PACKAGE['id']) is None
    assert calls == []


def test_cancel_before_read_or_native_preparation(fixture):
    backend, settings, calls = fixture
    seed_attempt(backend)
    event = threading.Event(); event.set()
    result = automation.prepare_python_update(backend, PACKAGE['id'], cancel_event=event)
    assert result['status'] == 'cancelled' and result['cancelled'] is True
    assert_skipped(result, calls)


def test_cancellation_at_final_native_boundary_never_mutates(fixture, monkeypatch):
    backend, settings, calls = fixture
    seed_attempt(backend)
    event = threading.Event()
    def preflight(approval):
        event.set()
        return native('ready')
    monkeypatch.setattr(automation, 'preflight_python_relocation', preflight)
    result = automation.prepare_python_update(backend, PACKAGE['id'], cancel_event=event)
    assert result['status'] == 'cancelled'
    assert_skipped(result, calls)


def test_already_trusted_mapping_does_not_repeat_repair(fixture, monkeypatch):
    backend, settings, calls = fixture
    seed_attempt(backend)
    monkeypatch.setattr(automation, 'preflight_python_relocation', lambda approval: native('already_trusted'))
    result = automation.prepare_python_update(backend, PACKAGE['id'])
    assert result['status'] == 'already_trusted' and result['proceed'] is True
    assert calls == []


@pytest.mark.parametrize('status,code', [('failed', 'native_repair_failed'),
    ('review_required', 'administrator_required'), ('unsupported', 'platform_unavailable')])
def test_failed_native_repair_is_a_terminal_package_only_skip(fixture, monkeypatch, status, code):
    backend, settings, calls = fixture
    seed_attempt(backend)
    monkeypatch.setattr(automation, 'repair_python_relocation', lambda approval: native(status, code=code))
    result = automation.prepare_python_update(backend, PACKAGE['id'])
    assert result['status'] == status and result['receipt']['code'] == code
    assert_skipped(result, calls)


def test_preflight_exception_does_not_become_unknown_installer_state(fixture, monkeypatch):
    backend, settings, calls = fixture
    seed_attempt(backend)
    def fail(approval):
        raise OSError('Fixture native failure')
    monkeypatch.setattr(automation, 'preflight_python_relocation', fail)
    assert_skipped(automation.prepare_python_update(backend, PACKAGE['id']), calls)


@pytest.mark.parametrize('change', ['offer', 'approval'])
def test_offer_and_consent_are_rechecked_after_native_preflight(fixture, monkeypatch, change):
    backend, settings, calls = fixture
    seed_attempt(backend)
    def preflight(approval):
        if change == 'offer':
            backend.maintenance._last_update_packages[PACKAGE['id']]['available'] = '3.14.2'
        else:
            settings['python_relocation_repair_enabled'] = False
        return native('ready')
    monkeypatch.setattr(automation, 'preflight_python_relocation', preflight)
    assert_skipped(automation.prepare_python_update(backend, PACKAGE['id']), calls)


def test_native_log_race_is_detected_before_repair(fixture, monkeypatch):
    backend, settings, calls = fixture
    seed_attempt(backend)
    original = automation.diagnose_python_relocation_failure
    def changed(package_id, log, **kwargs):
        result = original(package_id, log, **kwargs)
        log.write_text('Changed after diagnosis', encoding='utf-8')
        return result
    monkeypatch.setattr(automation, 'diagnose_python_relocation_failure', changed)
    assert_skipped(automation.prepare_python_update(backend, PACKAGE['id']), calls)


def test_display_failure_cannot_abort_scoped_preparation(fixture):
    backend, settings, calls = fixture
    seed_attempt(backend)
    def fail(event):
        raise RuntimeError('Display unavailable')
    assert automation.prepare_python_update(backend, PACKAGE['id'], progress_callback=fail)['proceed'] is True


@pytest.mark.parametrize('key,value', [('source', 'other'), ('id', 'Vendor.App'), ('available', '3.13.0')])
def test_current_scan_binding_is_required(fixture, key, value):
    backend, settings, calls = fixture
    seed_attempt(backend)
    backend.maintenance._last_update_packages[PACKAGE['id']][key] = value
    assert_skipped(automation.prepare_python_update(backend, PACKAGE['id']), calls)


@pytest.mark.parametrize('change', [dict(probe_error=448), dict(probe_error=True),
    dict(mutated=False), dict(code=None)])
def test_malformed_native_success_is_not_accepted(fixture, monkeypatch, change):
    backend, settings, calls = fixture
    seed_attempt(backend)
    result = dict(native('repaired', mutated=True), **change)
    monkeypatch.setattr(automation, 'repair_python_relocation', lambda approval: result)
    assert_skipped(automation.prepare_python_update(backend, PACKAGE['id']), calls)


def test_attempt_inventory_is_bounded_before_reading_logs(fixture, monkeypatch):
    backend, settings, calls = fixture
    seed_attempt(backend)
    seed_attempt(backend)
    monkeypatch.setattr(automation, '_MAX_ATTEMPTS', 1)
    assert_skipped(automation.prepare_python_update(backend, PACKAGE['id']), calls)
    assert calls == []


def test_native_log_inventory_is_bounded(fixture, monkeypatch):
    backend, settings, calls = fixture
    service, folder, log = seed_attempt(backend)
    log.with_name('winget_001_core_JustForMe.log').write_bytes(log.read_bytes())
    monkeypatch.setattr(automation, '_MAX_LOGS', 1)
    assert_skipped(automation.prepare_python_update(backend, PACKAGE['id']), calls)
    assert calls == []


def test_real_maintenance_lease_is_required_before_native_preparation(fixture, monkeypatch):
    backend, settings, calls = fixture
    seed_attempt(backend)
    monkeypatch.setattr(automation.RunLease, 'owned_by_current_process', classmethod(lambda cls, path: False))
    result = automation.prepare_python_update(backend, PACKAGE['id'])
    assert_skipped(result, calls)
    assert 'maintenance lease' in result['reason']
    assert calls == []


def test_lease_lost_during_preflight_cannot_authorize_mutation(fixture, monkeypatch):
    backend, settings, calls = fixture
    seed_attempt(backend)
    def preflight(approval):
        monkeypatch.setattr(automation.RunLease, 'owned_by_current_process', classmethod(lambda cls, path: False))
        return native('ready')
    monkeypatch.setattr(automation, 'preflight_python_relocation', preflight)
    assert_skipped(automation.prepare_python_update(backend, PACKAGE['id']), calls)


def test_expired_twenty_minute_catalog_does_not_prepare_relocation(fixture):
    backend, settings, calls = fixture
    seed_attempt(backend)
    backend.maintenance._last_update_scan = time.time() - 1201
    assert_skipped(automation.prepare_python_update(backend, PACKAGE['id']), calls)
    assert calls == []


def test_catalog_expiring_during_preflight_cannot_authorize_mutation(fixture, monkeypatch):
    backend, settings, calls = fixture
    seed_attempt(backend)
    def preflight(approval):
        backend.maintenance._last_update_scan = time.time() - 1201
        return native('ready')
    monkeypatch.setattr(automation, 'preflight_python_relocation', preflight)
    assert_skipped(automation.prepare_python_update(backend, PACKAGE['id']), calls)
