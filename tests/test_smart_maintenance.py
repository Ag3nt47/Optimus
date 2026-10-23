"""Only fixture inventories and fake cleanup calls; never inspect the host."""
import copy
import json
import threading
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from optimus.automation import RunLease
from optimus.smart_maintenance import SMART_DEFAULTS, SmartMaintenance, validate_smart_configuration


@pytest.fixture
def fixture(tmp_path, monkeypatch):
    monkeypatch.setattr('optimus.smart_maintenance._current_owner', lambda: 'fixture-owner')
    settings = dict(copy.deepcopy(SMART_DEFAULTS), cleanup_enabled=True, cleanup_age_days=0,
        cleanup_category_ids=[], intensity='Standard', browser_cache_enabled=True,
        browser_history_enabled=True, browser_cookies_enabled=True, browser_download_history_enabled=False,
        browser_cleanup_browsers=['chrome'], browser_keep_domains=['example.com'])
    plan = dict(id='a' * 32, total_bytes=500_000_000, files=[{'size': 500_000_000}], complete=True, errors=[], skipped=[])
    cleaner = SimpleNamespace(categories=Mock(return_value=[dict(id='user-temp', available=True, process_names=[])]),
        scan=Mock(side_effect=lambda *a, **kw: copy.deepcopy(plan)),
        apply=Mock(return_value=dict(deleted_count=1, deleted_bytes=500_000_000, skipped_count=0, errors=[])))
    cleaner.discard_plan = Mock()
    backend = SimpleNamespace(data_dir=tmp_path, cleaner=cleaner, preferences=lambda: copy.deepcopy(settings),
        browser_cleanup_status=Mock(return_value=dict(running=False, complete=True, processes=[], errors=[])))
    now = [100000.0]
    browser = Mock(return_value=dict(items=[dict(browser_id='chrome', status='completed')], failed_items=0))
    service = SmartMaintenance(backend, clock=lambda: now[0], monotonic=lambda: now[0],
        barrier_reader=lambda _: None, browser_runner=browser)
    return SimpleNamespace(service=service, backend=backend, cleaner=cleaner, settings=settings,
        plan=plan, now=now, browser=browser, root=tmp_path)


def active(pid=100, created=10):
    return dict(running=True, complete=True, errors=[], processes=[dict(pid=pid, create_time=created, verified=True)])


def closed():
    return dict(running=False, complete=True, errors=[], processes=[])


def test_defaults_construction_configuration_and_status_do_not_inspect_or_clean(fixture):
    f = fixture
    assert SMART_DEFAULTS['smart_cleaning_threshold_bytes'] == 500_000_000
    assert not SMART_DEFAULTS['smart_cleaning_enabled'] and not SMART_DEFAULTS['browser_exit_cleaning_enabled']
    f.service.configure(f.settings)
    f.service.status()
    f.service.start()
    f.cleaner.scan.assert_not_called()
    f.backend.browser_cleanup_status.assert_not_called()
    assert not list(f.root.iterdir())


@pytest.mark.parametrize(('key', 'value'), [('smart_cleaning_enabled', 1),
    ('browser_exit_cleaning_enabled', 'yes'), ('smart_cleaning_threshold_bytes', True),
    ('smart_cleaning_cooldown_seconds', 0), ('browser_exit_grace_seconds', 301),
    ('smart_maintenance_poll_seconds', 1)])
def test_invalid_settings_rejected(key, value):
    with pytest.raises(ValueError):
        validate_smart_configuration({key: value})


def test_threshold_uses_decimal_bytes_real_preview_and_durable_cooldown(fixture):
    f = fixture
    f.settings['smart_cleaning_enabled'] = True
    result = f.service.tick()
    assert result['status'] == 'completed'
    f.cleaner.scan.assert_called_once_with(['user-temp'], 0, max_files=20000, max_seconds=5)
    f.cleaner.apply.assert_called_once()
    f.cleaner.discard_plan.assert_called_with('a' * 32)
    assert json.loads((f.root / 'smart-maintenance/state.json').read_text())['pending'] is None
    other = SmartMaintenance(f.backend, clock=lambda: f.now[0], monotonic=lambda: f.now[0], barrier_reader=lambda _: None)
    assert other.tick()['actions'] == []
    assert f.cleaner.scan.call_count == 1
    f.now[0] += 3600
    other.tick()
    assert f.cleaner.apply.call_count == 2


@pytest.mark.parametrize('kind', ['below', 'partial', 'errors', 'skipped'])
def test_incomplete_or_below_threshold_never_cleans(fixture, kind):
    f = fixture
    f.settings['smart_cleaning_enabled'] = True
    if kind == 'below':
        f.plan['total_bytes'] -= 1
    elif kind == 'partial':
        f.plan['complete'] = False
    else:
        f.plan[kind] = ['fixture inaccessible/omitted scope']
    result = f.service.tick()
    f.cleaner.apply.assert_not_called()
    assert not (f.root / 'smart-maintenance').exists()
    if kind != 'below':
        assert result['status'] == 'partial'


def test_preview_never_writes_state_or_applies_even_above_threshold(fixture):
    f = fixture
    f.settings['smart_cleaning_enabled'] = True
    result = f.service.preview()
    assert result['preview'] and result['checks'][0]['bytes'] == 500000000
    f.cleaner.apply.assert_not_called()
    assert not (f.root / 'smart-maintenance').exists()


def test_revoked_scope_after_scan_and_cancel_prevent_apply(fixture):
    f = fixture
    f.settings['smart_cleaning_enabled'] = True
    def revoke(*a, **kw):
        f.settings['cleanup_enabled'] = False
        return copy.deepcopy(f.plan)
    f.cleaner.scan.side_effect = revoke
    assert f.service.tick()['status'] == 'needs_review'
    f.cleaner.apply.assert_not_called()
    f.settings['cleanup_enabled'] = True
    cancel = threading.Event()
    f.cleaner.scan.side_effect = lambda *a, **kw: cancel.set() or copy.deepcopy(f.plan)
    assert f.service.tick(cancel_event=cancel)['status'] == 'cancelled'
    f.cleaner.apply.assert_not_called()


def test_run_lease_and_prior_installer_block_all_inventory_and_cleanup(fixture):
    f = fixture
    f.settings['smart_cleaning_enabled'] = True
    with RunLease(f.root):
        assert f.service.tick()['status'] == 'deferred'
    f.service.barrier_reader = lambda _: 'Fixture unresolved installer.'
    assert f.service.tick()['note'] == 'Fixture unresolved installer.'
    f.cleaner.scan.assert_not_called()
    f.backend.browser_cleanup_status.assert_not_called()


def test_exception_keeps_uncertain_receipt_and_prevents_automatic_replay(fixture):
    f = fixture
    f.settings['smart_cleaning_enabled'] = True
    f.cleaner.apply.side_effect = OSError('fixture apply outcome unknown')
    assert f.service.tick()['status'] == 'needs_review'
    state = json.loads((f.root / 'smart-maintenance/state.json').read_text())
    assert state['pending']
    f.now[0] += 4000
    assert f.service.tick()['status'] == 'needs_review'
    assert f.cleaner.apply.call_count == 1


def test_browser_exit_requires_observed_running_and_stable_closed_grace(fixture):
    f = fixture
    f.settings['browser_exit_cleaning_enabled'] = True
    f.service.tick()
    f.now[0] += 20
    f.service.tick()
    f.browser.assert_not_called()  # Initially closed is not an observed exit.
    f.backend.browser_cleanup_status.return_value = active()
    f.service.tick()
    f.backend.browser_cleanup_status.return_value = closed()
    f.service.tick()
    f.now[0] += 14
    f.service.tick()
    f.browser.assert_not_called()
    f.now[0] += 1
    assert f.service.tick()['status'] == 'completed'
    assert f.browser.call_count == 1
    settings = f.browser.call_args.args[1]
    assert settings['browser_keep_domains'] == ['example.com']
    assert settings['browser_cleanup_browsers'] == ['chrome']
    assert f.browser.call_args.kwargs['browser_prompt']({}) is False
    f.now[0] += 4000
    f.service.tick()
    assert f.browser.call_count == 1


@pytest.mark.parametrize('uncertain', [dict(complete=False, running=False, processes=[], errors=['unknown']),
    dict(complete=True, running=False, processes=[dict(pid=9)], errors=[]),
    dict(complete=True, running=True, processes=[dict(pid=9, verified=False)], errors=[])])
def test_unknown_browser_state_discards_exit_arm_instead_of_guessing(fixture, uncertain):
    f = fixture
    f.settings['browser_exit_cleaning_enabled'] = True
    f.backend.browser_cleanup_status.return_value = active()
    f.service.tick()
    f.backend.browser_cleanup_status.return_value = uncertain
    result = f.service.tick()
    assert result['status'] == 'partial' and result['checks'][-1]['complete'] is False
    f.backend.browser_cleanup_status.return_value = closed()
    f.service.tick()
    f.now[0] += 50
    f.service.tick()
    f.browser.assert_not_called()


def test_reopened_or_reused_pid_restarts_browser_exit_grace(fixture):
    f = fixture
    f.settings['browser_exit_cleaning_enabled'] = True
    for status in (active(), closed()):
        f.backend.browser_cleanup_status.return_value = status
        f.service.tick()
    f.now[0] += 20
    f.backend.browser_cleanup_status.return_value = active(created=20)
    f.service.tick()
    f.backend.browser_cleanup_status.return_value = closed()
    f.service.tick()
    f.browser.assert_not_called()
    f.now[0] += 15
    f.service.tick()
    assert f.browser.call_count == 1


def test_stop_is_cooperative_keeps_lease_until_current_apply_returns(fixture):
    f = fixture
    f.settings['smart_cleaning_enabled'] = True
    entered, release = threading.Event(), threading.Event()
    def apply(plan):
        entered.set()
        assert release.wait(5)
        return dict(deleted_count=1, deleted_bytes=500000000, skipped_count=0, errors=[])
    f.cleaner.apply.side_effect = apply
    worker = threading.Thread(target=f.service.tick)
    worker.start()
    try:
        assert entered.wait(3)
        f.service.stop(wait=False)
        with pytest.raises(RuntimeError):
            RunLease(f.root).acquire()
    finally:
        release.set()
        worker.join(5)
    assert not worker.is_alive() and f.service.status()['status'] == 'cancelled'
    assert json.loads((f.root / 'smart-maintenance/state.json').read_text())['pending'] is None


def test_low_intensity_age_and_additional_saved_scopes_remain_bounded(fixture):
    f = fixture
    f.settings.update(smart_cleaning_enabled=True, intensity='Low', cleanup_category_ids=['crash-dumps', 'chrome:Default:Cache'])
    f.cleaner.categories.return_value += [dict(id='crash-dumps', available=True, process_names=[]),
        dict(id='chrome:Default:Cache', available=True, process_names=['chrome.exe'])]
    f.service.tick()
    assert f.cleaner.scan.call_args.args == (['user-temp', 'crash-dumps'], 30)


def test_external_busy_flag_prevents_apply_checks_and_preview_remains_readonly(fixture):
    f = fixture
    f.settings['smart_cleaning_enabled'] = True
    f.service.busy_reader = lambda: True
    assert f.service.tick()['status'] == 'deferred'
    f.cleaner.scan.assert_not_called()
    f.service.preview()
    f.cleaner.scan.assert_called_once()
    f.cleaner.apply.assert_not_called()


def test_foreign_or_corrupt_owned_state_never_cleans(fixture):
    f = fixture
    f.settings['smart_cleaning_enabled'] = True
    directory = f.root / 'smart-maintenance'
    directory.mkdir()
    for text in ('not json', json.dumps(dict(schema=1, owner='another-user', attempts={}, pending=None))):
        (directory / 'state.json').write_text(text)
        assert f.service.tick()['status'] == 'needs_review'
    f.cleaner.scan.assert_not_called()
    f.cleaner.apply.assert_not_called()


def test_preview_release_error_does_not_leave_service_tick_lock_held(fixture):
    f = fixture
    f.settings['smart_cleaning_enabled'] = True
    f.cleaner.discard_plan.side_effect = OSError('fixture release failure')
    f.service.preview()
    assert not f.service._tick_lock.locked()
    f.service.preview()
    assert f.cleaner.scan.call_count == 2


def test_periodic_preview_releases_own_real_cleaner_plans_and_keeps_manual_preview(fixture, monkeypatch):
    from optimus.cleaner import Cleaner, Category
    f = fixture
    f.settings.update(smart_cleaning_enabled=True, cleanup_enabled=False, cleanup_category_ids=['fixture-cache'])
    cache = f.root / 'fixture-cache'
    cache.mkdir()
    (cache / 'item').write_bytes(b'rebuildable fixture')
    actual = Cleaner(f.root / 'data', [Category('fixture-cache', 'Fixture', cache, 'Disposable test cache')])
    monkeypatch.setattr(actual, '_running', lambda _: [])
    monkeypatch.setattr(actual, '_open_files_snapshot', lambda: (set(), True))
    f.backend.cleaner = actual
    manual = actual.scan(['fixture-cache'], 0)
    for _ in range(3):
        assert f.service.preview()['checks'][0]['complete']
        assert list(actual._plans) == [manual['id']]
    with pytest.raises(ValueError):
        actual.discard_plan('../forged')
    assert actual.discard_plan(manual['id']) and not actual._plans
    assert (cache / 'item').read_bytes() == b'rebuildable fixture'


def test_failed_terminal_write_keeps_pending_receipt_and_prevents_repeat(fixture):
    f = fixture
    f.settings['smart_cleaning_enabled'] = True
    original = f.service._write
    def fail_terminal(path, value):
        if value.get('status') == 'completed':
            raise OSError('fixture final receipt write failure')
        return original(path, value)
    f.service._write = fail_terminal
    assert f.service.tick()['status'] == 'needs_review'
    f.service._write = original
    f.now[0] += 5000
    assert f.service.tick()['status'] == 'needs_review'
    assert f.cleaner.apply.call_count == 1


def test_actual_browser_orchestrator_checks_fresh_scopes_before_each_privacy_apply(fixture):
    from optimus.browser_cleanup import run_browser_cleanup
    from test_browser_cleanup import service, profile
    f = fixture
    f.settings['browser_exit_cleaning_enabled'] = True
    f.settings['browser_cache_enabled'] = False
    native = service(profiles=[profile()], caches=[])
    for name in ('privacy_profiles', 'cleanup_categories', 'privacy_scan', 'privacy_apply', 'browser_cleanup_close'):
        setattr(f.backend, name, getattr(native, name))
    f.service.browser_runner = run_browser_cleanup
    f.backend.browser_cleanup_status.return_value = active()
    f.service.tick()
    f.backend.browser_cleanup_status.return_value = closed()
    f.service.tick()
    f.now[0] += 15
    def revoke(*args, **kwargs):
        f.settings['browser_cookies_enabled'] = False
        return dict(planId='b' * 32, canApply=True, categories=['history', 'cookies'], counts={})
    f.backend.privacy_scan.side_effect = revoke
    f.backend.browser_privacy_engine = SimpleNamespace(discard_plan=Mock())
    result = f.service.tick()
    assert result['status'] == 'completed_with_errors'
    f.backend.privacy_apply.assert_not_called()
    f.backend.browser_cleanup_close.assert_not_called()
    f.backend.browser_privacy_engine.discard_plan.assert_called_once_with('b' * 32)


def test_actual_browser_orchestrator_never_closes_browser_reopened_before_apply(fixture):
    from optimus.browser_cleanup import run_browser_cleanup
    from test_browser_cleanup import service, profile, opened
    f = fixture
    f.settings['browser_exit_cleaning_enabled'] = True
    f.settings['browser_cache_enabled'] = False
    native = service(profiles=[profile()], caches=[])
    for name in ('privacy_profiles', 'cleanup_categories', 'privacy_scan', 'privacy_apply', 'browser_cleanup_close'):
        setattr(f.backend, name, getattr(native, name))
    f.service.browser_runner = run_browser_cleanup
    f.backend.browser_cleanup_status.return_value = active()
    f.service.tick()
    f.backend.browser_cleanup_status.return_value = closed()
    f.service.tick()
    f.now[0] += 15
    f.backend.browser_cleanup_status.side_effect = [closed(), opened()]
    result = f.service.tick()
    assert result['status'] == 'completed_with_errors'
    f.backend.privacy_apply.assert_not_called()
    f.backend.browser_cleanup_close.assert_not_called()
