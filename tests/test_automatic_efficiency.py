"""Automatic efficiency choices preserve saved scopes and use explicit run hooks."""
import copy

import pytest

from optimus.workflow import DEFAULTS, WINDOWS_REMOVAL_KEYS, Preferences, Workflow
from optimus.unattended_configuration import approve_unattended_settings, matches_unattended, _scope_hash
from test_workflow import MockBackend


def configuration(**changes):
    result = copy.deepcopy(DEFAULTS)
    result.update(dict.fromkeys(WINDOWS_REMOVAL_KEYS, False))
    result.update({key: False for key in result if key.startswith('privacy_')})
    return Preferences.validate(dict(result, cleanup_enabled=False,
        browser_cache_enabled=False, drive_space_guard_enabled=False, **changes))


def test_new_defaults_reduce_duplicate_work_without_granting_mutation_scopes():
    settings = configuration()
    assert settings['review_inventory_reuse_enabled'] is True
    assert settings['defender_scan_reuse_enabled'] is True
    assert settings['defender_scan_reuse_hours'] == 6
    assert settings['driver_changes_approved'] is False
    assert settings['software_update_all_approved'] is False
    assert settings['unattended_auto_clean_enabled'] is False


@pytest.mark.parametrize('value', [0, 25, -1, 1.5, True, '6', None])
def test_invalid_scan_window_is_rejected(value):
    with pytest.raises(ValueError):
        configuration(defender_scan_reuse_hours=value)


@pytest.mark.parametrize('key', ['review_inventory_reuse_enabled', 'defender_scan_reuse_enabled'])
def test_efficiency_toggles_require_real_booleans(key):
    with pytest.raises(ValueError):
        configuration(**{key: 1})


def test_explicit_saved_choices_are_not_overwritten(tmp_path):
    choices = configuration(review_inventory_reuse_enabled=False,
        defender_scan_reuse_enabled=False, defender_scan_reuse_hours=2)
    preferences = Preferences(tmp_path)
    preferences.save(choices)
    before = preferences.path.read_bytes()
    assert preferences.load() == choices
    assert preferences.path.read_bytes() == before


def test_old_unattended_scope_is_preserved_only_for_reducing_work_defaults():
    settings = configuration(setup_version=2, unattended_auto_clean_enabled=True, spyware_scan_enabled=True)
    previous = copy.deepcopy(settings)
    for name in ('review_inventory_reuse_enabled', 'defender_scan_reuse_enabled', 'defender_scan_reuse_hours'):
        previous.pop(name)
    approved = approve_unattended_settings(previous)
    migrated = Preferences.validate(approved)
    assert matches_unattended(migrated)
    assert _scope_hash(migrated) == _scope_hash(approved)
    migrated['review_inventory_reuse_enabled'] = False  # read-only query optimization
    assert matches_unattended(migrated)
    migrated['defender_scan_reuse_hours'] = 3
    assert not matches_unattended(migrated)


def test_disabled_scan_does_not_change_old_unattended_scope():
    settings = configuration(setup_version=2, unattended_auto_clean_enabled=True)
    previous = copy.deepcopy(settings)
    for name in ('review_inventory_reuse_enabled', 'defender_scan_reuse_enabled', 'defender_scan_reuse_hours'):
        previous.pop(name)
    approved = approve_unattended_settings(previous)
    migrated = Preferences.validate(approved)
    migrated['defender_scan_reuse_enabled'] = False  # Native settings disable Windows-only controls.
    assert matches_unattended(migrated)


class EfficientBackend(MockBackend):
    def begin_review_inventory(self, *, enabled):
        self.calls.append(('begin_inventory', enabled))

    def end_review_inventory(self):
        self.calls.append(('end_inventory',))

    def invalidate_review_inventory(self):
        self.calls.append(('invalidate_inventory',))

    def installed_apps_review(self):
        self.calls.append(('apps_review',))
        return []

    def startup_list_review(self):
        self.calls.append(('startup_review',))
        return []

    def scan_spyware_automatic(self, settings, *, cancel_event=None, progress_callback=None):
        self.calls.append(('automatic_scan', settings['defender_scan_reuse_hours']))
        return {'status': 'skipped_recent_scan', 'skipped': True, 'failed_items': 0,
                'completion_verified': False}


def test_workflow_shares_review_metadata_only_inside_run_and_uses_automatic_scan(tmp_path):
    backend = EfficientBackend(tmp_path)
    settings = configuration(applications_review_enabled=True, startup_review_enabled=True,
        spyware_scan_enabled=True)
    report = Workflow(backend).run(settings, lambda event: None)
    assert report['status'] == 'completed'
    assert backend.calls[0] == ('begin_inventory', True)
    assert backend.calls[-1] == ('end_inventory',)
    assert ('apps_review',) in backend.calls and ('installed_apps',) not in backend.calls
    assert ('startup_review',) in backend.calls and ('startup_list',) not in backend.calls
    assert ('automatic_scan', 6) in backend.calls


def test_workflow_discards_review_scope_after_failure(tmp_path):
    backend = EfficientBackend(tmp_path)
    backend.audit_callback = lambda: (_ for _ in ()).throw(RuntimeError('fixture audit failure'))
    workflow = Workflow(backend)
    workflow.run(configuration(), lambda event: None)
    assert backend.calls[-1] == ('end_inventory',)
    assert not workflow.active and not workflow.lock.locked()
