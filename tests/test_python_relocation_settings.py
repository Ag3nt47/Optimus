"""New repair authority stays narrow and existing unattended choices survive."""
import copy
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest

from optimus.backend import Backend
from optimus.launch_review import launch_warning
from optimus.native_settings import NativePreferences
from optimus.unattended_configuration import approve_unattended_settings, matches_unattended
from optimus.workflow import DEFAULTS, Preferences


RULE = {'package_id': 'Python.Python.3.14',
        'source': r'C:\Users\Fixture\AppData\Local\Programs\Python',
        'target': r'D:\ApprovedApps\Python', 'reparse_sha256': 'a' * 64}


def test_old_settings_add_no_repair_authority_or_file_writes(tmp_path):
    import json
    old = {key: copy.deepcopy(value) for key, value in DEFAULTS.items()
           if not key.startswith('python_relocation_')}
    path = tmp_path / 'preferences.json'
    path.write_text(json.dumps(old), encoding='utf-8')
    raw = path.read_bytes()
    loaded = Preferences(tmp_path).load()
    assert loaded['python_relocation_repair_enabled'] is False
    assert loaded['python_relocation_approvals'] == []
    assert path.read_bytes() == raw


def test_explicit_scope_is_required_validated_and_revocable():
    with pytest.raises(ValueError):
        Preferences.validate(dict(DEFAULTS, python_relocation_repair_enabled=True))
    settings = Preferences.validate(dict(DEFAULTS, python_relocation_approvals=[RULE],
        python_relocation_repair_enabled=True))
    assert settings['python_relocation_approvals'][0]['package_id'] == RULE['package_id']
    assert 'identical junction metadata' in launch_warning(settings)
    settings['python_relocation_repair_enabled'] = False
    assert Preferences.validate(settings)['python_relocation_approvals']


@pytest.mark.parametrize('rules', [None, [dict(RULE, unexpected=True)], [RULE, RULE],
    [dict(RULE, package_id='Unrelated.Package')], [dict(RULE, reparse_sha256='bad')]])
def test_invalid_scopes_fail_closed(rules):
    with pytest.raises(ValueError):
        Preferences.validate(dict(DEFAULTS, python_relocation_approvals=rules))


def test_disabled_new_defaults_preserve_preexisting_unattended_scope():
    old = {key: copy.deepcopy(value) for key, value in DEFAULTS.items()
           if not key.startswith('python_relocation_')}
    old.update(setup_version=2, unattended_auto_clean_enabled=True)
    approved = approve_unattended_settings(old)
    assert matches_unattended(Preferences.validate(approved))
    changed = Preferences.validate(approved)
    changed.update(python_relocation_approvals=[RULE], python_relocation_repair_enabled=True)
    assert not matches_unattended(changed)
    assert NativePreferences.validate(changed)['python_relocation_repair_enabled'] is False


@pytest.mark.parametrize('status', ['admin_required', 'failed', 'needs_review'])
def test_native_repair_refusal_skips_only_one_package_before_installer(status):
    backend = object.__new__(Backend)
    backend.update_disk_readiness = Mock(return_value={'ready': True})
    backend.maintenance = SimpleNamespace(updates_apply=Mock())
    refusal = {'proceed': False, 'status': status, 'reason': 'Exact repair needs attention.'}
    with patch('optimus.python_relocation_automation.prepare_python_update', return_value=refusal):
        receipt = backend.update_app_unattended(RULE['package_id'])
    from optimus.navigation import can_fail_forward
    assert receipt['installer_started'] is False and receipt['outcomeKnown'] is True
    assert receipt['failure_scope'] == 'package' and receipt['running'] is False
    assert can_fail_forward(receipt)
    backend.maintenance.updates_apply.assert_not_called()


def test_repair_success_allows_exact_normal_update_once_with_verification_unchanged():
    backend = object.__new__(Backend)
    backend.update_disk_readiness = Mock(return_value={'ready': True})
    native = {'exitCode': 0, 'verified': True, 'installed_version': '3.14.10'}
    backend.maintenance = SimpleNamespace(updates_apply=Mock(return_value=native))
    repair = {'proceed': True, 'status': 'repaired'}
    with patch('optimus.python_relocation_automation.prepare_python_update', return_value=repair):
        receipt = backend.update_app_unattended(RULE['package_id'])
    backend.maintenance.updates_apply.assert_called_once_with(RULE['package_id'],
        confirmed=True, unattended=True)
    assert receipt['verified'] is True and receipt['relocation_repair'] == repair
    assert 'relocation_repair' not in native


def test_owned_lease_requires_the_current_process_live_os_lock(tmp_path):
    import threading
    from optimus.automation import RunLease
    assert RunLease.owned_by_current_process(tmp_path) is False
    with RunLease(tmp_path):
        assert RunLease.owned_by_current_process(tmp_path) is True
        observed = []
        thread = threading.Thread(target=lambda: observed.append(RunLease.owned_by_current_process(tmp_path)))
        thread.start()
        thread.join()
        assert observed == [True]
    assert RunLease.owned_by_current_process(tmp_path) is False
    # A stale file alone is never an owned lease.
    assert (tmp_path / 'maintenance-run.lock').exists()
    assert RunLease.owned_by_current_process(tmp_path) is False


def test_cancelled_repair_retains_worker_navigation_and_observers():
    backend = object.__new__(Backend)
    backend.update_disk_readiness = Mock(return_value={'ready': True})
    backend.maintenance = SimpleNamespace(updates_apply=Mock())
    event, observer = object(), Mock()
    refusal = {'proceed': False, 'status': 'cancelled', 'cancelled': True,
               'reason': 'Skipped at your request.'}
    with patch('optimus.python_relocation_automation.prepare_python_update', return_value=refusal) as prepare:
        receipt = backend.update_app_unattended(RULE['package_id'],
            cancel_event=event, progress_callback=observer)
    prepare.assert_called_once_with(backend, RULE['package_id'],
        cancel_event=event, progress_callback=observer)
    assert receipt['cancelled'] is True and receipt['status'] == 'cancelled'
    assert receipt['requiresReview'] is False and receipt['installer_started'] is False
    backend.maintenance.updates_apply.assert_not_called()
