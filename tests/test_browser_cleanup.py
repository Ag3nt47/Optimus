"""Browser closure/cleanup contracts use fixtures; no live window or DB actions."""
import copy
import json
import threading
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest

from optimus.browser_cleanup import BROWSERS, BrowserSessionController, run_browser_cleanup


def closed():
    return {'complete': True, 'running': False, 'can_close': True, 'processes': [], 'errors': []}


def opened(browser_id='chrome'):
    return dict(closed(), running=True, processes=[{'pid': 123, 'name': BROWSERS[browser_id]['process']}])


def profile(browser_id='chrome', identifier='a' * 32):
    return {'id': identifier, 'browser': BROWSERS[browser_id]['name'], 'name': 'Default',
            'family': 'firefox' if browser_id == 'firefox' else 'chromium',
            'supportedCategories': ['history', 'cookies'] if browser_id == 'firefox' else ['history', 'cookies', 'downloads']}


def cache(browser_id='chrome'):
    return {'id': browser_id + ':Default:Cache', 'available': True,
            'process_names': [BROWSERS[browser_id]['process']]}


def configuration(**changes):
    return dict({'browser_cache_enabled': True, 'browser_history_enabled': True,
                 'browser_cookies_enabled': True, 'browser_download_history_enabled': True,
                 'browser_cleanup_browsers': ['chrome'], 'browser_keep_domains': []}, **changes)


def service(profiles=None, caches=None):
    def scan(identity, *, categories, keep_domains):
        return {'planId': 'b' * 32, 'canApply': True, 'categories': categories,
                'counts': {category: 3 for category in categories}}
    return SimpleNamespace(
        privacy_profiles=Mock(return_value=profiles if profiles is not None else [profile()]),
        cleanup_categories=Mock(return_value=caches if caches is not None else [cache()]),
        browser_cleanup_status=Mock(return_value=closed()),
        browser_cleanup_close=Mock(return_value={'closed': True, 'complete': True}),
        privacy_scan=Mock(side_effect=scan),
        privacy_apply=Mock(return_value={'state': 'applied', 'backupId': 'c' * 32,
                                        'counts': {'history': 3, 'cookies': 3, 'downloads': 3}}))


def cleaner():
    return Mock(return_value={'preview_summary': {'complete': True},
                              'result': {'deleted_count': 2, 'deleted_bytes': 100, 'skipped_count': 0, 'errors': []}})


@pytest.mark.parametrize('answer', [None, False, 1, 'yes'])
def test_open_browser_requires_exact_yes_and_decline_skips_all_profiles_and_cache(answer):
    backend = service([profile(), profile(identifier='d' * 32)])
    backend.browser_cleanup_status.return_value = opened()
    prompt, clean = Mock(return_value=answer), cleaner()
    result = run_browser_cleanup(backend, configuration(), clean=clean, browser_prompt=prompt)
    assert result['failed_items'] == 0 and result['items'][0]['status'] == 'skipped'
    prompt.assert_called_once()
    backend.browser_cleanup_close.assert_not_called()
    backend.privacy_scan.assert_not_called()
    backend.privacy_apply.assert_not_called()
    clean.assert_not_called()


def test_no_prompt_callback_never_closes_or_cleans_open_browser():
    backend, clean = service(), cleaner()
    backend.browser_cleanup_status.return_value = opened()
    run_browser_cleanup(backend, configuration(), clean=clean)
    backend.browser_cleanup_close.assert_not_called()
    backend.privacy_scan.assert_not_called()
    clean.assert_not_called()


def test_yes_closes_once_then_cleans_all_fresh_profiles_and_selected_cache():
    backend, clean, prompt = service([profile(), profile(identifier='d' * 32)]), cleaner(), Mock(return_value=True)
    backend.browser_cleanup_status.side_effect = [opened()] + [closed()] * 8
    result = run_browser_cleanup(backend, configuration(browser_keep_domains=['Example.COM']), clean=clean, browser_prompt=prompt)
    backend.browser_cleanup_close.assert_called_once_with('chrome', cancel_event=None)
    assert backend.privacy_scan.call_count == backend.privacy_apply.call_count == 2
    assert all(call.kwargs['keep_domains'] == ['example.com'] for call in backend.privacy_scan.call_args_list)
    assert all(call.args[0]['confirmed'] is True for call in backend.privacy_apply.call_args_list)
    clean.assert_called_once_with(['chrome:Default:Cache'])
    assert result['failed_items'] == 0 and result['items'][0]['status'] == 'completed'
    request = prompt.call_args.args[0]
    assert request['processes'] == [{'pid': 123, 'name': 'chrome.exe'}]
    assert request['profile_count'] == 2 and set(request['categories']) == {'cache', 'history', 'cookies', 'downloads'}


def test_closed_browser_needs_no_close_prompt():
    backend, clean, prompt = service(), cleaner(), Mock(side_effect=AssertionError('Unexpected prompt'))
    result = run_browser_cleanup(backend, configuration(), clean=clean, browser_prompt=prompt)
    assert result['failed_items'] == 0
    backend.browser_cleanup_close.assert_not_called()
    assert backend.privacy_apply.call_count == clean.call_count == 1


def test_browser_choices_and_cache_prefixes_are_scoped_independently():
    backend = service([profile(), profile('firefox', 'd' * 32), profile('edge', 'e' * 32)],
                      [cache(), cache('firefox'), cache('edge'), {'id': 'user-temp', 'available': True}])
    clean = cleaner()
    result = run_browser_cleanup(backend, configuration(browser_cleanup_browsers=['edge']), clean=clean)
    assert [item['browser_id'] for item in result['items']] == ['edge']
    backend.privacy_scan.assert_called_once_with('e' * 32, categories=['history', 'cookies', 'downloads'], keep_domains=[])
    clean.assert_called_once_with(['edge:Default:Cache'])
    assert {call.args[0] for call in backend.browser_cleanup_status.call_args_list} == {'edge'}


def test_declining_one_browser_does_not_skip_a_different_closed_browser():
    backend = service([profile(), profile('firefox', 'd' * 32)], [cache(), cache('firefox')])
    backend.browser_cleanup_status.side_effect = lambda identity: opened() if identity == 'chrome' else closed()
    clean, prompt = cleaner(), Mock(return_value=False)
    result = run_browser_cleanup(backend, configuration(browser_cleanup_browsers=['chrome', 'firefox']), clean=clean, browser_prompt=prompt)
    assert result['items'][0]['status'] == 'skipped' and result['items'][1]['status'] == 'completed'
    backend.privacy_scan.assert_called_once_with('d' * 32, categories=['history', 'cookies'], keep_domains=[])
    clean.assert_called_once_with(['firefox:Default:Cache'])


@pytest.mark.parametrize('flags,categories', [
    ((True, False, False), ['history']), ((False, True, False), ['cookies']),
    ((False, False, True), ['downloads']), ((True, True, True), ['history', 'cookies', 'downloads']),
])
def test_privacy_categories_cannot_expand_from_saved_settings(flags, categories):
    backend, clean = service(), cleaner()
    result = run_browser_cleanup(backend, configuration(browser_cache_enabled=False,
        browser_history_enabled=flags[0], browser_cookies_enabled=flags[1], browser_download_history_enabled=flags[2]), clean=clean)
    backend.privacy_scan.assert_called_once_with('a' * 32, categories=categories, keep_domains=[])
    assert backend.privacy_apply.call_args.args[0]['selectedCategories'] == categories
    clean.assert_not_called()
    assert result['failed_items'] == 0


def test_cache_only_does_not_discover_or_open_privacy_databases():
    backend, clean = service(), cleaner()
    run_browser_cleanup(backend, configuration(browser_history_enabled=False, browser_cookies_enabled=False,
        browser_download_history_enabled=False), clean=clean)
    backend.privacy_profiles.assert_not_called()
    backend.privacy_scan.assert_not_called()
    backend.privacy_apply.assert_not_called()
    clean.assert_called_once_with(['chrome:Default:Cache'])


def test_firefox_downloads_only_never_opens_history_database_or_closes_browser():
    backend, clean, prompt = service([profile('firefox')], []), cleaner(), Mock(return_value=True)
    result = run_browser_cleanup(backend, configuration(browser_cleanup_browsers=['firefox'], browser_cache_enabled=False,
        browser_history_enabled=False, browser_cookies_enabled=False), clean=clean, browser_prompt=prompt)
    assert result['items'][0]['unsupported_categories'] == ['downloads']
    backend.browser_cleanup_status.assert_not_called()
    backend.privacy_scan.assert_not_called()
    prompt.assert_not_called()


@pytest.mark.parametrize('status', [None, [], {}, {'running': False, 'complete': False, 'processes': []},
    dict(closed(), errors=['Failure']), dict(closed(), running=None), dict(opened(), processes=[]),
    dict(opened(), can_close=False), dict(opened(), processes=[{'pid': 4, 'name': 'chrome.exe'}]),
    dict(opened(), processes=[{'pid': 123, 'name': 'other.exe'}]),
    dict(opened(), processes=[{'pid': 123, 'name': 'chrome.exe'}] * 2)])
def test_unknown_or_inconsistent_process_state_fails_closed(status):
    backend, clean, prompt = service(), cleaner(), Mock(return_value=True)
    backend.browser_cleanup_status.return_value = status
    result = run_browser_cleanup(backend, configuration(), clean=clean, browser_prompt=prompt)
    assert result['failed_items'] == 1
    backend.privacy_scan.assert_not_called()
    backend.browser_cleanup_close.assert_not_called()
    clean.assert_not_called()
    prompt.assert_not_called()


@pytest.mark.parametrize('outcome', [None, {}, {'closed': False, 'complete': True}, {'closed': True, 'complete': False}])
def test_failed_or_background_only_closure_skips_whole_browser(outcome):
    backend, clean = service(), cleaner()
    backend.browser_cleanup_status.return_value = opened()
    backend.browser_cleanup_close.return_value = outcome
    result = run_browser_cleanup(backend, configuration(), clean=clean, browser_prompt=lambda request: True)
    assert result['failed_items'] == 1 and result['items'][0]['status'] == 'skipped'
    backend.privacy_scan.assert_not_called()
    clean.assert_not_called()


@pytest.mark.parametrize('initial', [opened(), closed()])
def test_browser_reopened_after_yes_or_initial_closed_check_is_skipped(initial):
    backend, clean = service(), cleaner()
    backend.browser_cleanup_status.side_effect = [initial, opened()]
    result = run_browser_cleanup(backend, configuration(), clean=clean, browser_prompt=lambda request: True)
    assert result['failed_items'] == 1
    backend.privacy_scan.assert_not_called()
    clean.assert_not_called()


def test_browser_reopens_after_privacy_preview_prevents_apply_and_cache():
    backend, clean = service(), cleaner()
    backend.browser_cleanup_status.side_effect = [closed(), closed(), closed(), opened()]
    result = run_browser_cleanup(backend, configuration(), clean=clean)
    assert backend.privacy_scan.call_count == 1 and result['failed_items'] == 1
    backend.privacy_apply.assert_not_called()
    clean.assert_not_called()


@pytest.mark.parametrize('boundary', ['before', 'prompt', 'preview'])
def test_cancellation_at_prompt_or_preview_never_starts_a_later_mutation(boundary):
    backend, clean, stop = service(), cleaner(), threading.Event()
    if boundary == 'before':
        stop.set()
    if boundary == 'prompt':
        backend.browser_cleanup_status.return_value = opened()
    if boundary == 'preview':
        scan = backend.privacy_scan.side_effect
        def cancelled_scan(*args, **kwargs):
            stop.set()
            return scan(*args, **kwargs)
        backend.privacy_scan.side_effect = cancelled_scan
    def prompt(request):
        stop.set()
        return True
    result = run_browser_cleanup(backend, configuration(), clean=clean, browser_prompt=prompt, cancel_event=stop)
    assert result['cancelled']
    backend.browser_cleanup_close.assert_not_called()
    backend.privacy_apply.assert_not_called()
    clean.assert_not_called()


def test_failed_privacy_profile_is_reported_while_other_profile_and_cache_can_finish():
    backend, clean = service([profile(), profile(identifier='d' * 32)]), cleaner()
    backend.privacy_apply.side_effect = [RuntimeError('PRIVATE cookie-value https://visited.example/path'),
                                       {'state': 'applied', 'backupId': 'c' * 32, 'counts': {'history': 2}}]
    result = run_browser_cleanup(backend, configuration(), clean=clean)
    assert result['failed_items'] == 1 and result['items'][0]['status'] == 'partial'
    assert [item['status'] for item in result['items'][0]['profiles']] == ['failed', 'applied']
    assert 'PRIVATE' not in json.dumps(result) and 'visited.example' not in json.dumps(result)
    clean.assert_called_once()


def test_cache_partial_or_error_outcomes_remain_visible_without_raw_file_paths():
    backend, clean = service(), cleaner()
    clean.return_value = {'preview_summary': {'complete': False}, 'result': {
        'deleted_count': 1, 'deleted_bytes': 99, 'skipped_count': 1,
        'errors': ['PRIVATE https://secret.example file error'], 'files': [{'path': 'PRIVATE'}]}}
    result = run_browser_cleanup(backend, configuration(), clean=clean)
    assert result['failed_items'] == 1 and result['items'][0]['cache']['partial'] is True
    assert result['items'][0]['cache']['deleted_bytes'] == 99
    assert 'PRIVATE' not in json.dumps(result)


@pytest.mark.parametrize('part', ['profiles', 'caches', 'status', 'close'])
def test_inventory_or_close_exceptions_are_visible_and_never_trigger_cleanup(part):
    backend, clean = service(), cleaner()
    target = {'profiles': backend.privacy_profiles, 'caches': backend.cleanup_categories,
              'status': backend.browser_cleanup_status, 'close': backend.browser_cleanup_close}[part]
    target.side_effect = RuntimeError('PRIVATE failure')
    if part == 'close':
        backend.browser_cleanup_status.return_value = opened()
    result = run_browser_cleanup(backend, configuration(), clean=clean, browser_prompt=lambda request: True)
    assert result['failed_items'] == 1 and 'PRIVATE' not in json.dumps(result)
    backend.privacy_scan.assert_not_called()
    clean.assert_not_called()


def test_empty_or_disabled_selection_does_not_inspect_host():
    backend, clean = service(), cleaner()
    run_browser_cleanup(backend, configuration(browser_cleanup_browsers=[]), clean=clean)
    backend.privacy_profiles.assert_not_called()
    backend.cleanup_categories.assert_not_called()
    backend.browser_cleanup_status.assert_not_called()
    clean.assert_not_called()


@pytest.mark.parametrize('changes', [
    {'browser_history_enabled': 'yes'}, {'browser_cache_enabled': 1},
    {'browser_cleanup_browsers': 'chrome'}, {'browser_cleanup_browsers': ['unknown']},
    {'browser_cleanup_browsers': ['chrome', 'chrome']}, {'browser_keep_domains': ['https://example.com']},
])
def test_invalid_saved_scope_is_rejected_before_inventory(changes):
    backend, clean = service(), cleaner()
    with pytest.raises(ValueError):
        run_browser_cleanup(backend, configuration(**changes), clean=clean)
    backend.privacy_profiles.assert_not_called()
    backend.cleanup_categories.assert_not_called()
    clean.assert_not_called()


def test_privacy_plan_cannot_expand_categories_or_invent_a_plan_identity():
    backend, clean = service(), cleaner()
    backend.privacy_scan.side_effect = None
    backend.privacy_scan.return_value = {'canApply': True, 'planId': 'b' * 32, 'categories': ['cookies']}
    result = run_browser_cleanup(backend, configuration(browser_cache_enabled=False,
        browser_history_enabled=True, browser_cookies_enabled=False, browser_download_history_enabled=False), clean=clean)
    assert result['failed_items'] == 1
    backend.privacy_apply.assert_not_called()


def identity():
    return {'pid': 123, 'name': 'chrome.exe', 'exe': r'c:\apps\chrome.exe', 'create_time': 10.0,
            'username': 'user', 'session_id': 1, 'verified': True}


def native_open():
    return dict(opened(), processes=[identity()])


@pytest.fixture
def sessions():
    controller = BrowserSessionController(timeout_seconds=0.1)
    controller._inventory = Mock(return_value=closed())
    controller._windows = Mock(return_value=[(1000, 123)])
    controller._same_process = Mock(return_value=True)
    controller._post_close = Mock(return_value=True)
    return controller


def test_session_closure_posts_only_verified_initial_windows_and_checks_full_exit(sessions):
    sessions._inventory.side_effect = [native_open(), closed()]
    result = sessions.close_browser('chrome')
    assert result['closed'] and result['complete'] and result['requested_windows'] == 1
    sessions._windows.assert_called_once_with({123})
    sessions._same_process.assert_called_once_with('chrome', identity())
    sessions._post_close.assert_called_once_with(1000, 123)


@pytest.mark.parametrize('state', [dict(native_open(), complete=False), dict(native_open(), can_close=False)])
def test_unverified_or_foreign_browser_session_cannot_receive_close_request(sessions, state):
    sessions._inventory.return_value = state
    assert not sessions.close_browser('chrome')['closed']
    sessions._windows.assert_not_called()
    sessions._post_close.assert_not_called()


def test_reused_process_identity_never_receives_wm_close(sessions):
    sessions._inventory.return_value = native_open()
    sessions._same_process.return_value = False
    assert not sessions.close_browser('chrome')['closed']
    sessions._post_close.assert_not_called()


def test_background_processes_or_nonresponsive_browser_are_never_force_killed(sessions):
    sessions._inventory.return_value = native_open()
    sessions._windows.return_value = []
    with patch('optimus.browser_cleanup.psutil.Process') as process:
        result = sessions.close_browser('chrome')
    assert result['closed'] is False and result['remaining_processes']
    sessions._post_close.assert_not_called()
    process.assert_not_called()


def test_cancelled_native_closure_sends_no_messages(sessions):
    stop = threading.Event()
    stop.set()
    assert sessions.close_browser('chrome', stop)['cancelled']
    sessions._inventory.assert_not_called()
    sessions._post_close.assert_not_called()


def test_browser_session_identity_requires_current_user_session_and_lifetime():
    process = SimpleNamespace(pid=123, name=lambda: 'chrome.exe', exe=lambda: r'C:\Apps\chrome.exe',
                              create_time=lambda: 10.0, username=lambda: 'User')
    with patch('optimus.browser_cleanup._session_id', return_value=1):
        assert BrowserSessionController._identity(process, 'chrome', ('user', 1))['verified']
        with pytest.raises(ValueError):
            BrowserSessionController._identity(process, 'chrome', ('other-user', 1))
        with pytest.raises(ValueError):
            BrowserSessionController._identity(process, 'chrome', ('user', 2))
        process.create_time = lambda: float('nan')
        with pytest.raises(ValueError):
            BrowserSessionController._identity(process, 'chrome', ('user', 1))


@pytest.mark.parametrize('timeout', [True, 0, -1, 31, float('nan')])
def test_native_close_deadline_cannot_be_unbounded(timeout):
    with pytest.raises(ValueError):
        BrowserSessionController(timeout_seconds=timeout)


def inventory_controller():
    controller = BrowserSessionController()
    controller._context = Mock(return_value=('user', 1))
    controller._native_process_names = Mock(return_value={321: 'ProtectedService.exe'})
    controller._identity = Mock(return_value=identity())
    return controller


def unknown_process(pid=321):
    return SimpleNamespace(pid=pid, info={'pid': pid, 'name': None})


def test_fresh_positive_native_nonbrowser_name_does_not_block_browser_inventory():
    controller = inventory_controller()
    with patch('optimus.browser_cleanup.psutil.process_iter', return_value=[unknown_process()]):
        status = controller.browser_status('chrome')
    assert status['complete'] and not status['running'] and status['can_close']
    controller._native_process_names.assert_called_once()
    controller._identity.assert_not_called()


@pytest.mark.parametrize('native_pid', [321, 777])
def test_unknown_or_new_browser_in_native_snapshot_still_requires_full_identity(native_pid):
    controller = inventory_controller()
    controller._native_process_names.return_value = {321: 'ProtectedService.exe', native_pid: 'chrome.exe'}
    fresh = SimpleNamespace(pid=native_pid)
    record = dict(identity(), pid=native_pid)
    controller._identity.return_value = record
    with patch('optimus.browser_cleanup.psutil.process_iter', return_value=[unknown_process()]), \
            patch('optimus.browser_cleanup.psutil.Process', return_value=fresh) as process:
        status = controller.browser_status('chrome')
    assert status['complete'] and status['running'] and status['processes'] == [record]
    process.assert_called_once_with(native_pid)
    controller._identity.assert_called_once_with(fresh, 'chrome', ('user', 1))


def test_native_browser_name_alone_never_authorizes_closing_inaccessible_browser():
    controller = inventory_controller()
    controller._native_process_names.return_value = {321: 'chrome.exe'}
    controller._identity.side_effect = PermissionError('Inaccessible full identity')
    with patch('optimus.browser_cleanup.psutil.process_iter', return_value=[unknown_process()]), \
            patch('optimus.browser_cleanup.psutil.Process', return_value=SimpleNamespace(pid=321)):
        status = controller.browser_status('chrome')
    assert status['running'] and not status['complete'] and not status['can_close']
    assert status['processes'][0]['verified'] is False


def test_failed_native_fallback_leaves_inventory_untrusted():
    controller = inventory_controller()
    controller._native_process_names.side_effect = OSError('Fixture native snapshot failure')
    with patch('optimus.browser_cleanup.psutil.process_iter', return_value=[unknown_process()]):
        status = controller.browser_status('chrome')
    assert not status['complete'] and not status['can_close']


@pytest.mark.parametrize('alive', [True, False])
def test_unknown_pid_absent_from_native_snapshot_is_ignored_only_when_gone(alive):
    controller = inventory_controller()
    controller._native_process_names.return_value = {}
    with patch('optimus.browser_cleanup.psutil.process_iter', return_value=[unknown_process()]), \
            patch('optimus.browser_cleanup.psutil.pid_exists', return_value=alive):
        status = controller.browser_status('chrome')
    assert status['complete'] is (not alive)
    assert status['can_close'] is (not alive)


def test_native_snapshot_does_not_duplicate_browser_already_verified_by_psutil():
    controller = inventory_controller()
    controller._native_process_names.return_value = {321: 'ProtectedService.exe', 123: 'chrome.exe'}
    browser = SimpleNamespace(pid=123, info={'pid': 123, 'name': 'chrome.exe'})
    with patch('optimus.browser_cleanup.psutil.process_iter', return_value=[browser, unknown_process()]):
        status = controller.browser_status('chrome')
    assert status['complete'] and len(status['processes']) == 1
    controller._identity.assert_called_once_with(browser, 'chrome', ('user', 1))
