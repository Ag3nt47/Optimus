"""Only fake volume observations and disposable cache files; no host maintenance."""
import copy
from dataclasses import replace
import json
import os
import threading
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from optimus.automation import RunLease
from optimus.cleaner import Category, Cleaner
from optimus.low_space_cleanup import LowSpacePlanner
from optimus.smart_configuration import SMART_DEFAULTS
from optimus.smart_maintenance import SmartMaintenance
from optimus.toolkit_configuration import launch_descriptions, validate_configuration


@pytest.fixture
def fixture(tmp_path, monkeypatch):
    monkeypatch.setattr('optimus.smart_maintenance._current_owner', lambda: 'fixture-owner')
    low, healthy = tmp_path / 'low-volume', tmp_path / 'healthy-volume'
    low.mkdir(); healthy.mkdir()
    categories = []
    for identity, base in (('cache-a', low), ('cache-b', low), ('healthy-cache', healthy)):
        root = base / identity
        root.mkdir()
        (root / 'rebuildable.bin').write_bytes(b'cache fixture')
        categories.append(Category(identity, identity, root, 'Disposable test cache.'))
    cleaner = Cleaner(tmp_path / 'data', categories)
    monkeypatch.setattr(cleaner, '_running', lambda _: [])
    monkeypatch.setattr(cleaner, '_open_files_snapshot', lambda: (set(), True))
    settings = dict(copy.deepcopy(SMART_DEFAULTS), cleanup_enabled=False, cleanup_age_days=0,
        cleanup_category_ids=['cache-a', 'cache-b', 'healthy-cache'], intensity='Standard',
        browser_cleanup_browsers=[], drive_space_guard_enabled=True, drive_space_guard_target_gib=2.0,
        toolkit_settings={'low_space_cleaning': {'enabled': True}})
    def row(path, free):
        return dict(mountpoint=str(path), device='fixture-' + path.name, filesystem='ntfs',
            free_bytes=free, target_bytes=2 * 1024**3, shortfall_bytes=max(0, 2 * 1024**3 - free),
            below_target=free < 2 * 1024**3, status='below_target' if free < 2 * 1024**3 else 'ok')
    observation = dict(schema=1, enabled=True, review_only=True, target_bytes=2 * 1024**3,
        complete=True, volumes=[row(low, 1024**3), row(healthy, 3 * 1024**3)])
    volumes = Mock(side_effect=lambda _: copy.deepcopy(observation))
    backend = SimpleNamespace(data_dir=tmp_path / 'data', cleaner=cleaner,
        preferences=lambda: copy.deepcopy(settings))
    now = [100000.0]
    service = SmartMaintenance(backend, volume_reader=volumes, clock=lambda: now[0],
        monotonic=lambda: now[0], barrier_reader=lambda _: None)
    return SimpleNamespace(root=tmp_path, low=low, healthy=healthy, cleaner=cleaner,
        settings=settings, observation=observation, volumes=volumes, backend=backend,
        service=service, now=now, categories=categories)


def test_nested_default_off_has_no_extra_top_level_setting_or_launch_action():
    assert validate_configuration({})['low_space_cleaning'] == {'enabled': False}
    from optimus.workflow import DEFAULTS
    assert len(DEFAULTS) == 100
    assert 'low_space_cleaning' not in DEFAULTS
    assert launch_descriptions({}) == ([], [])


def test_enabled_launch_description_names_existing_scopes_target_and_limits(fixture):
    actions, checks = launch_descriptions(fixture.settings)
    assert len(actions) == 1 and not checks
    assert all(text in actions[0] for text in ('already selected', '2.0 GiB', 'cooldown', 'excluded', 'not guaranteed'))


def test_low_space_only_mode_cleans_existing_approved_categories_not_healthy_volume(fixture):
    f = fixture
    result = f.service.tick()
    assert result['status'] == 'completed' and len(result['actions']) == 2
    assert all(action['trigger'] == 'low-space' for action in result['actions'])
    assert sum(action['result']['deleted_bytes'] for action in result['actions']) == 2 * len(b'cache fixture')
    assert not list(f.low.rglob('*.bin'))
    assert (f.healthy / 'healthy-cache/rebuildable.bin').read_bytes() == b'cache fixture'
    assert all(action['result']['target_met'] is False for action in result['actions'])
    assert not f.cleaner._plans


@pytest.mark.parametrize('change', ['toggle', 'guard', 'cleanup_scope'])
def test_disabled_scope_or_guard_never_reads_volumes_or_deletes(fixture, change):
    f = fixture
    if change == 'toggle':
        f.settings['toolkit_settings']['low_space_cleaning']['enabled'] = False
    elif change == 'guard':
        f.settings['drive_space_guard_enabled'] = False
    else:
        f.settings['cleanup_category_ids'] = []
    result = f.service.tick()
    assert not result['actions']
    if change != 'cleanup_scope':
        f.volumes.assert_not_called()
    assert len(list(f.root.rglob('*.bin'))) == 3
    assert not (f.root / 'data/smart-maintenance').exists()


def test_preview_checks_real_files_without_writes_or_plan_leaks(fixture):
    f = fixture
    manual = f.cleaner.scan(['healthy-cache'], 0)
    before = {str(path.relative_to(f.root)): path.read_bytes() for path in f.root.rglob('*') if path.is_file()}
    report = f.service.preview()
    assert report['preview'] and not report['actions']
    assert sum(row.get('bytes', 0) for row in report['checks']) == 2 * len(b'cache fixture')
    assert {str(path.relative_to(f.root)): path.read_bytes() for path in f.root.rglob('*.bin')} == before
    assert not (f.backend.data_dir / 'smart-maintenance').exists()
    assert list(f.cleaner._plans) == [manual['id']]


@pytest.mark.parametrize('case', ['unknown', 'healthy', 'wrong_free', 'missing', 'ambiguous'])
def test_unknown_healthy_or_inconsistent_low_volume_is_preserved(fixture, case):
    f = fixture
    row = f.observation['volumes'][0]
    if case == 'unknown':
        row.update(status='unknown', free_bytes=None, below_target=None)
    elif case == 'healthy':
        row.update(status='ok', free_bytes=3 * 1024**3, shortfall_bytes=0, below_target=False)
    elif case == 'wrong_free':
        row['free_bytes'] = -1
    elif case == 'missing':
        f.observation['volumes'].pop(0)
    else:
        f.observation['volumes'].append(copy.deepcopy(row))
    assert not f.service.tick()['actions']
    assert len(list(f.root.rglob('*.bin'))) == 3


def test_nearer_unknown_mount_is_not_treated_as_parent_low_volume(fixture):
    f = fixture
    f.observation['volumes'].append(dict(f.observation['volumes'][0], mountpoint=str(f.low / 'cache-a'),
        status='unknown', free_bytes=None, below_target=None))
    result = f.service.tick()
    assert len(result['actions']) == 1
    assert (f.low / 'cache-a/rebuildable.bin').exists()
    assert not (f.low / 'cache-b/rebuildable.bin').exists()


def test_space_recovery_before_apply_preserves_preview(fixture, monkeypatch):
    f = fixture
    original = f.cleaner.scan
    def recover(*args, **kwargs):
        plan = original(*args, **kwargs)
        f.observation['volumes'][0].update(status='ok', free_bytes=3 * 1024**3, shortfall_bytes=0, below_target=False)
        return plan
    monkeypatch.setattr(f.cleaner, 'scan', recover)
    assert not f.service.tick()['actions']
    assert len(list(f.root.rglob('*.bin'))) == 3
    assert not f.cleaner._plans


def test_space_recovery_after_first_category_stops_further_category_deletion(fixture, monkeypatch):
    f = fixture
    original = f.cleaner.apply
    def recover(plan):
        result = original(plan)
        f.observation['volumes'][0].update(status='ok', free_bytes=3 * 1024**3, shortfall_bytes=0, below_target=False)
        return result
    monkeypatch.setattr(f.cleaner, 'apply', recover)
    result = f.service.tick()
    assert len(result['actions']) == 1 and result['actions'][0]['result']['target_met'] is True
    assert not (f.low / 'cache-a/rebuildable.bin').exists()
    assert (f.low / 'cache-b/rebuildable.bin').exists()


@pytest.mark.parametrize('change', ['target', 'guard', 'toggle', 'categories', 'adaptive'])
def test_fresh_saved_scope_revocation_after_scan_never_applies(fixture, monkeypatch, change):
    f = fixture
    original = f.cleaner.scan
    def revoke(*args, **kwargs):
        plan = original(*args, **kwargs)
        if change == 'target': f.settings['drive_space_guard_target_gib'] = 1.0
        elif change == 'guard': f.settings['drive_space_guard_enabled'] = False
        elif change == 'toggle': f.settings['toolkit_settings']['low_space_cleaning']['enabled'] = False
        elif change == 'categories': f.settings['cleanup_category_ids'] = []
        else: f.settings['toolkit_settings']['adaptive'] = {'enabled': True}
        return plan
    monkeypatch.setattr(f.cleaner, 'scan', revoke)
    result = f.service.tick()
    assert all(action['status'] == 'cancelled' for action in result['actions'])
    assert len(list(f.root.rglob('*.bin'))) == 3


def test_new_installer_barrier_or_busy_session_after_scan_preserves_files(fixture, monkeypatch):
    f = fixture
    original = f.cleaner.scan
    def block(*args, **kwargs):
        plan = original(*args, **kwargs)
        f.service.barrier_reader = lambda _: 'A native installer now owns shared state.'
        return plan
    monkeypatch.setattr(f.cleaner, 'scan', block)
    result = f.service.tick()
    assert result['actions'] and all(action['status'] == 'cancelled' for action in result['actions'])
    assert len(list(f.root.rglob('*.bin'))) == 3


def test_initial_busy_lease_and_installer_barrier_prevent_volume_inventory(fixture):
    f = fixture
    with RunLease(f.backend.data_dir):
        assert f.service.tick()['status'] == 'deferred'
    f.service.busy_reader = lambda: True
    assert f.service.tick()['status'] == 'deferred'
    f.service.busy_reader = lambda: False
    f.service.barrier_reader = lambda _: 'Fixture installer remains unresolved.'
    assert f.service.tick()['status'] == 'deferred'
    f.volumes.assert_not_called()


@pytest.mark.parametrize('change', ['file', 'device', 'category', 'path', 'total', 'partial'])
def test_changed_or_cross_scope_preview_is_never_applied(fixture, monkeypatch, change):
    f = fixture
    f.settings['cleanup_category_ids'] = ['cache-a']
    original = f.cleaner.scan
    def alter(*args, **kwargs):
        plan = original(*args, **kwargs)
        row = plan['files'][0]
        if change == 'file': Path(row['path']).write_bytes(b'new unrelated bytes')
        elif change == 'device': row['device'] += 1
        elif change == 'category': row['category_id'] = 'healthy-cache'
        elif change == 'path': row['path'] = str(f.healthy / 'healthy-cache/rebuildable.bin')
        elif change == 'total': plan['total_bytes'] += 1
        else: plan['complete'] = False
        return plan
    monkeypatch.setattr(f.cleaner, 'scan', alter)
    assert not f.service.tick()['actions']
    assert len(list(f.root.rglob('*.bin'))) == 3
    assert not f.cleaner._plans


def test_cooldown_is_shared_with_junk_and_survives_new_service(fixture):
    f = fixture
    f.settings['smart_cleaning_enabled'] = True
    f.settings['smart_cleaning_threshold_bytes'] = 1_000_000
    assert len(f.service.tick()['actions']) == 2
    fresh = SmartMaintenance(f.backend, volume_reader=f.volumes, clock=lambda: f.now[0],
        monotonic=lambda: f.now[0], barrier_reader=lambda _: None)
    f.volumes.reset_mock()
    assert not fresh.tick()['actions']
    f.volumes.assert_not_called()
    state = json.loads((f.backend.data_dir / 'smart-maintenance/state.json').read_text())
    assert state['attempts']['low-space'] == f.now[0] and state['pending'] is None


def test_uncertain_cleanup_stays_blocked_and_is_not_replayed(fixture, monkeypatch):
    f = fixture
    apply = Mock(side_effect=OSError('Fixture native result is unknown.'))
    monkeypatch.setattr(f.cleaner, 'apply', apply)
    assert f.service.tick()['status'] == 'needs_review'
    f.now[0] += 4000
    assert f.service.tick()['status'] == 'needs_review'
    assert apply.call_count == 1
    state = json.loads((f.backend.data_dir / 'smart-maintenance/state.json').read_text())
    assert state['pending']


def test_category_with_running_application_guard_is_not_added(fixture):
    f = fixture
    f.categories[0] = replace(f.categories[0], process_names=('fixture-app.exe',))
    result = f.service.tick()
    assert len(result['actions']) == 1
    assert (f.low / 'cache-a/rebuildable.bin').exists()


@pytest.mark.parametrize('report', [None, {}, {'schema': 1, 'enabled': False},
    {'schema': 1, 'enabled': True, 'review_only': False, 'target_bytes': 1, 'volumes': []}])
def test_invalid_volume_authority_is_never_cleanup_permission(report):
    with pytest.raises(ValueError):
        LowSpacePlanner({}, volume_reader=lambda _: report).observe()


def test_different_nested_filesystem_device_is_not_cleaned(fixture, monkeypatch):
    import optimus.low_space_cleanup as module
    f = fixture
    original = module._directory
    def changed(path):
        value = original(path)
        return SimpleNamespace(st_dev=value.st_dev + 1, st_ino=value.st_ino) if path == f.low / 'cache-a' else value
    monkeypatch.setattr(module, '_directory', changed)
    result = f.service.tick()
    assert len(result['actions']) == 1
    assert (f.low / 'cache-a/rebuildable.bin').exists()


def test_low_space_only_start_waits_for_poll_and_can_stop_without_actions(fixture):
    f = fixture
    result = f.service.start()
    assert result['running'] is True
    f.volumes.assert_not_called()
    assert f.service.stop()['running'] is False
    assert len(list(f.root.rglob('*.bin'))) == 3


def test_cancellation_after_preview_preserves_all_files(fixture, monkeypatch):
    f = fixture
    cancel = threading.Event()
    original = f.cleaner.scan
    def cancel_after_scan(*args, **kwargs):
        plan = original(*args, **kwargs)
        cancel.set()
        return plan
    monkeypatch.setattr(f.cleaner, 'scan', cancel_after_scan)
    result = f.service.tick(cancel_event=cancel)
    assert result['status'] == 'cancelled' and not result['actions']
    assert len(list(f.root.rglob('*.bin'))) == 3 and not f.cleaner._plans


@pytest.mark.parametrize('change', ['scope', 'session_busy', 'installer'])
def test_last_volume_verification_cannot_outlive_approval_or_native_gate(fixture, change):
    f = fixture
    f.settings['cleanup_category_ids'] = ['cache-a']
    calls = [0]
    def observe(_):
        calls[0] += 1
        if calls[0] == 4:
            if change == 'scope': f.settings['toolkit_settings']['low_space_cleaning']['enabled'] = False
            elif change == 'session_busy': f.service.busy_reader = lambda: True
            else: f.service.barrier_reader = lambda _: 'Installer appeared during final path verification.'
        return copy.deepcopy(f.observation)
    f.service.volume_reader = observe
    result = f.service.tick()
    assert result['actions'][0]['status'] == 'cancelled'
    assert len(list(f.root.rglob('*.bin'))) == 3


def test_adaptive_pressure_rechecked_before_entering_category_apply(fixture):
    f = fixture
    f.settings['cleanup_category_ids'] = ['cache-a']
    f.settings['toolkit_settings']['adaptive'] = {'enabled': True}
    f.backend.maintenance_readiness = Mock(side_effect=[{'allowed': True},
        {'allowed': False, 'reasons': ['Fixture memory pressure rose.']}])
    result = f.service.tick()
    assert result['actions'][0]['status'] == 'cancelled'
    assert len(list(f.root.rglob('*.bin'))) == 3


def test_adaptive_pressure_change_during_final_file_verification_prevents_apply(fixture):
    f = fixture
    f.settings['cleanup_category_ids'] = ['cache-a']
    f.settings['toolkit_settings']['adaptive'] = {'enabled': True}
    readiness = {'allowed': True, 'reasons': []}
    f.backend.maintenance_readiness = Mock(side_effect=lambda **_: copy.deepcopy(readiness))
    observations = [0]
    def observe(_):
        observations[0] += 1
        if observations[0] == 4:
            readiness.update(allowed=False, reasons=['Resource pressure rose during final verification.'])
        return copy.deepcopy(f.observation)
    f.service.volume_reader = observe
    result = f.service.tick()
    assert f.backend.maintenance_readiness.call_count == 3
    assert len(result['actions']) == 1 and result['actions'][0]['status'] == 'cancelled'
    assert 'after file verification' in result['actions'][0]['result']['note']
    assert len(list(f.root.rglob('*.bin'))) == 3


def test_final_adaptive_callback_cannot_revoke_scope_then_authorize_cleanup(fixture):
    f = fixture
    f.settings['cleanup_category_ids'] = ['cache-a']
    f.settings['toolkit_settings']['adaptive'] = {'enabled': True}
    calls = [0]
    def readiness(**_):
        calls[0] += 1
        if calls[0] == 3:
            f.settings['toolkit_settings']['low_space_cleaning']['enabled'] = False
        return {'allowed': True, 'reasons': []}
    f.backend.maintenance_readiness = Mock(side_effect=readiness)
    result = f.service.tick()
    assert result['actions'][0]['status'] == 'cancelled'
    assert len(list(f.root.rglob('*.bin'))) == 3


def test_unselected_existing_cache_is_not_implicitly_approved(fixture):
    f = fixture
    f.settings['cleanup_category_ids'] = ['cache-a']
    assert len(f.service.tick()['actions']) == 1
    assert (f.low / 'cache-b/rebuildable.bin').exists()


def test_false_healthy_label_after_apply_does_not_claim_target_recovered(fixture, monkeypatch):
    f = fixture
    original = f.cleaner.apply
    def false_healthy(plan):
        result = original(plan)
        f.observation['volumes'][0].update(status='ok', below_target=False)
        return result
    monkeypatch.setattr(f.cleaner, 'apply', false_healthy)
    result = f.service.tick()
    assert len(result['actions']) == 1
    assert result['actions'][0]['result']['target_met'] is None


def test_category_batch_limit_is_candid_and_preserves_remaining_files(fixture, monkeypatch):
    f = fixture
    monkeypatch.setattr('optimus.low_space_cleanup.MAX_CATEGORIES', 1)
    result = f.service.tick()
    assert len(result['actions']) == 1 and result['status'] == 'completed_with_errors'
    assert any(row['kind'] == 'low_space_limit' for row in result['checks'])
    assert (f.low / 'cache-b/rebuildable.bin').exists()


def test_changed_volume_target_is_not_an_authority_for_cleanup(fixture):
    f = fixture
    f.observation['target_bytes'] = 1024**3
    result = f.service.tick()
    assert result['status'] == 'needs_review' and not result['actions']
    assert len(list(f.root.rglob('*.bin'))) == 3
