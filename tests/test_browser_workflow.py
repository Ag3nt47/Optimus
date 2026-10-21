"""Browser-maintenance preferences and dispatch use mocks, never real profiles."""
from __future__ import annotations

import copy
import json
import sys
import threading
import types
from unittest.mock import Mock, patch

import pytest

from optimus.backend import Backend
from optimus.workflow import DEFAULTS, Preferences, Workflow
from test_workflow import MockBackend


PRIVACY_FLAGS = ('browser_history_enabled', 'browser_cookies_enabled', 'browser_download_history_enabled')
BROWSER_FLAGS = ('browser_cache_enabled', *PRIVACY_FLAGS)
BROWSERS = ('edge', 'chrome', 'brave', 'vivaldi', 'firefox')


def configuration(**changes):
    result = copy.deepcopy(DEFAULTS)
    result.update(cleanup_enabled=False, **{key: False for key in BROWSER_FLAGS})
    result.update(changes)
    return result


def mocked_orchestrator(operation):
    module = types.ModuleType('optimus.browser_cleanup')
    module.run_browser_cleanup = operation
    return patch.dict(sys.modules, {'optimus.browser_cleanup': module})


def test_upgrade_preserves_old_profile_and_leaves_new_privacy_operations_off():
    original = {'intensity': 'High', 'cleanup_age_days': 1, 'browser_cache_enabled': True,
                'cleanup_enabled': False, 'background_analysis_enabled': True}
    before = copy.deepcopy(original)
    selected = Preferences.validate(original)
    assert original == before
    assert all(selected[key] == value for key, value in original.items())
    assert not any(selected[key] for key in PRIVACY_FLAGS)
    assert selected['browser_cleanup_browsers'] == list(BROWSERS)
    assert selected['browser_keep_domains'] == []


def test_safe_defaults_disable_every_browser_mutation_and_are_independent():
    first = Preferences.safe_defaults()
    assert not any(first[key] for key in BROWSER_FLAGS)
    first['browser_cleanup_browsers'].clear()
    first['browser_keep_domains'].append('private.fixture')
    second = Preferences.safe_defaults()
    assert second['browser_cleanup_browsers'] == list(BROWSERS)
    assert second['browser_keep_domains'] == []
    assert DEFAULTS['browser_keep_domains'] == []


@pytest.mark.parametrize('key', PRIVACY_FLAGS)
@pytest.mark.parametrize('invalid', (None, 0, 1, 'yes'))
def test_privacy_switches_require_real_booleans(key, invalid):
    with pytest.raises(ValueError):
        Preferences.validate({key: invalid})


@pytest.mark.parametrize('invalid', ('chrome', ('chrome',), None, [True], ['opera'],
                                      ['Chrome'], ['chrome.exe'], ['C:/Chrome']))
def test_browser_scope_requires_exact_canonical_browser_ids(invalid):
    with pytest.raises(ValueError):
        Preferences.validate({'browser_cleanup_browsers': invalid})


@pytest.mark.parametrize('invalid', ('example.com', None, [None], ['https://example.com'],
    ['example.com/path'], ['example.com:443'], ['*.example.com'], ['user@example.com'],
    ['bad..example.com'], ['-bad.example.com'], [''], ['a' * 64 + '.example.com']))
def test_cookie_keep_scope_rejects_non_host_input(invalid):
    with pytest.raises(ValueError):
        Preferences.validate({'browser_keep_domains': invalid})


def test_cookie_keep_hosts_are_normalized_and_deduplicated():
    selected = Preferences.validate({'browser_keep_domains': ['.EXAMPLE.com.', 'example.com', 'bücher.de']})
    assert selected['browser_keep_domains'] == ['example.com', 'xn--bcher-kva.de']


def test_cookie_keep_scope_is_bounded():
    with pytest.raises(ValueError):
        Preferences.validate({'browser_keep_domains': [f'host{index}.example.com' for index in range(101)]})


def test_browser_preferences_round_trip_without_promoting_other_categories(tmp_path):
    preferences = Preferences(tmp_path)
    saved = preferences.save(configuration(browser_cookies_enabled=True,
        browser_cleanup_browsers=['chrome', 'firefox'], browser_keep_domains=['EXAMPLE.com']))
    assert preferences.load() == saved
    assert saved['browser_cookies_enabled'] is True
    assert saved['browser_history_enabled'] is False
    assert saved['browser_download_history_enabled'] is False
    assert saved['browser_cache_enabled'] is False
    assert saved['browser_keep_domains'] == ['example.com']


def test_corrupt_browser_preferences_fail_closed_without_overwriting_file(tmp_path):
    preferences = Preferences(tmp_path)
    preferences.path.write_text('{invalid-private-preferences', encoding='utf-8')
    selected = preferences.load()
    assert not any(selected[key] for key in BROWSER_FLAGS)
    assert preferences.path.read_text(encoding='utf-8') == '{invalid-private-preferences'


@pytest.mark.parametrize('enabled', BROWSER_FLAGS)
def test_each_browser_category_dispatches_one_orchestrator_stage(tmp_path, enabled):
    backend = MockBackend(tmp_path)
    operation = Mock(return_value={'fixture_browser_cleanup': True, 'failed_items': 0})
    with mocked_orchestrator(operation):
        report = Workflow(backend).run(configuration(**{enabled: True}), lambda _: None)
    operation.assert_called_once()
    browser_stages = [stage for stage in report['stages'] if stage.get('result', {}).get('fixture_browser_cleanup')]
    assert len(browser_stages) == 1
    assert len(report['stages']) == 4  # Two reports, browser maintenance, then final outcome verification.
    assert not any(call[0] in ('cleanup_scan', 'cleanup_apply') for call in backend.calls)


def test_combined_browser_scopes_forward_the_ui_callback_cancel_and_progress_once(tmp_path):
    backend = MockBackend(tmp_path)
    prompt = Mock(return_value={'action': 'skip'})
    cancelled = threading.Event()
    progress = []
    observed = {}
    def dispatch(actual_backend, settings, **kwargs):
        observed.update(kwargs)
        assert actual_backend is backend
        assert all(settings[key] for key in BROWSER_FLAGS)
        assert settings['browser_cleanup_browsers'] == ['chrome']
        assert settings['browser_keep_domains'] == ['example.com']
        kwargs['item_callback']({'current': 0, 'total': 1, 'description': 'Fixture browser scope'})
        return {'fixture_browser_cleanup': True, 'failed_items': 0}
    operation = Mock(side_effect=dispatch)
    with mocked_orchestrator(operation):
        report = Workflow(backend).run(configuration(**{key: True for key in BROWSER_FLAGS},
            browser_cleanup_browsers=['chrome'], browser_keep_domains=['example.com']),
            progress.append, cancelled, browser_prompt=prompt)
    operation.assert_called_once()
    assert observed['browser_prompt'] is prompt
    assert observed['cancel_event'] is cancelled
    assert callable(observed['clean'])
    assert callable(observed['item_callback'])
    assert any(event['description'] == 'Fixture browser scope' for event in progress)
    assert report['status'] == 'completed'
    prompt.assert_not_called()  # Dispatch decides whether a prompt is required.


def test_disabled_browser_scopes_do_not_dispatch_or_clean(tmp_path):
    backend = MockBackend(tmp_path)
    operation = Mock(side_effect=AssertionError('Disabled browser cleanup must not dispatch.'))
    with mocked_orchestrator(operation):
        report = Workflow(backend).run(configuration(), lambda _: None)
    operation.assert_not_called()
    assert [call[0] for call in backend.calls] == ['audit', 'audit']
    assert report['status'] == 'completed'


def test_orchestrator_failure_is_journaled_as_failed_not_clean_success(tmp_path):
    backend = MockBackend(tmp_path)
    operation = Mock(return_value={'fixture_browser_cleanup': True, 'failed_items': 1,
                                  'errors': ['Fixture profile could not be inspected.']})
    with mocked_orchestrator(operation):
        report = Workflow(backend).run(configuration(browser_history_enabled=True), lambda _: None)
    stage = next(stage for stage in report['stages'] if stage.get('result', {}).get('fixture_browser_cleanup'))
    assert stage['status'] == 'failed'
    assert report['status'] == 'completed_with_errors'
    stored = json.loads((tmp_path / 'runs' / (report['id'] + '.json')).read_text())
    assert stored['status'] == 'completed_with_errors'
    assert any(stage.get('result', {}).get('errors') for stage in stored['stages'])


def test_backend_optimize_forwards_browser_prompt_to_workflow():
    backend = Backend.__new__(Backend)
    backend.workflow = Mock()
    settings = configuration(browser_history_enabled=True)
    progress, cancelled, prompt = Mock(), threading.Event(), Mock()
    backend.optimize(settings, progress, cancelled, browser_prompt=prompt)
    args, kwargs = backend.workflow.run.call_args
    assert args[0] is settings
    assert args[1] is progress
    assert args[2] is cancelled
    assert kwargs['browser_prompt'] is prompt
