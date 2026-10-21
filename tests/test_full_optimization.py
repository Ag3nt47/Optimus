"""Full profile, saved overrides and review routing; all services are fixtures."""
import json
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest

from optimus.automation import CURRENT_SETUP_VERSION, due, record_attempt
from optimus.workflow import CONSUMER_PACKAGE_NAMES, DEFAULTS, Preferences, Workflow, full_optimization_settings
from test_workflow import mock_verification


def test_new_install_selects_full_but_setup_must_be_saved(tmp_path):
    settings = Preferences(tmp_path).load()
    assert settings['optimization_mode'] == 'full'
    assert settings['automation_mode'] == 'startup'
    assert settings['cleanup_age_days'] == 0
    assert settings['consumer_debloat_enabled']
    assert settings['consumer_debloat_package_names'] == list(CONSUMER_PACKAGE_NAMES)
    assert settings['software_updates_enabled'] and settings['driver_updates_enabled']
    assert settings['software_update_policy'] == settings['driver_policy'] == 'review'
    assert not settings['software_update_agreements_accepted']
    assert not settings['driver_changes_approved']
    assert not due(settings, None)
    assert Preferences.validate(settings) == settings


def test_saved_changes_are_not_reset_when_full_profile_opens(tmp_path):
    prefs = Preferences(tmp_path)
    saved = full_optimization_settings()
    saved.update(automation_mode='manual', cleanup_age_days=9, browser_cookies_enabled=False,
                 consumer_debloat_package_names=['Microsoft.BingNews'], privacy_advertising_enabled=False)
    prefs.save(saved)
    assert prefs.load() == saved
    assert not due(prefs.load(), None)


def test_corrupt_or_legacy_configuration_never_invents_full_consent(tmp_path):
    prefs = Preferences(tmp_path)
    prefs.path.write_text('{broken', encoding='utf-8')
    fallback = prefs.load()
    assert fallback['optimization_mode'] == 'custom'
    assert fallback['automation_mode'] == 'manual'
    assert not fallback['consumer_debloat_enabled']
    assert not fallback['privacy_advertising_enabled']
    legacy = Preferences.validate({'setup_version': CURRENT_SETUP_VERSION, 'automation_mode': 'manual'})
    assert legacy['optimization_mode'] == 'custom'
    assert not legacy['post_process_review_enabled']


def test_preset_preserves_scopes_exclusions_and_cannot_accept_agreements():
    initial = Preferences.validate({'browser_keep_domains': ['example.com'],
                                    'consumer_debloat_package_names': ['Microsoft.Getstarted'],
                                    'software_update_exclude_ids': ['Fixture.Excluded']})
    settings = full_optimization_settings(initial)
    assert settings['consumer_debloat_package_names'] == ['Microsoft.Getstarted']
    assert settings['software_update_exclude_ids'] == ['Fixture.Excluded']
    assert settings['browser_keep_domains'] == ['example.com']
    assert not settings['software_update_all_approved']
    assert not settings['software_update_agreements_accepted']
    assert settings['software_update_policy'] == 'review'
    assert initial['optimization_mode'] == 'custom'


def test_saved_all_supported_approval_is_used_by_full_preset():
    settings = full_optimization_settings(dict(DEFAULTS, software_update_all_approved=True,
                                               software_update_agreements_accepted=True))
    assert settings['software_update_policy'] == 'all_supported'
    Preferences.validate(settings)


@pytest.mark.parametrize('names', [['Microsoft.WindowsStore'], ['Microsoft.BingWallpaper'],
                                  ['*'], ['Microsoft.BingNews', 'Microsoft.BingNews'], 'Microsoft.BingNews'])
def test_unknown_and_protected_packages_cannot_enter_automatic_list(names):
    with pytest.raises(ValueError):
        Preferences.validate({'consumer_debloat_package_names': names})


def test_full_profile_only_once_per_open_and_manual_persists(tmp_path):
    settings = full_optimization_settings()
    settings['setup_version'] = CURRENT_SETUP_VERSION
    assert due(settings, None)
    attempt = record_attempt(tmp_path, settings)
    assert not due(settings, attempt)
    with patch('optimus.automation.SESSION_ID', 'e' * 32):
        assert due(settings, attempt)
        assert not due(dict(settings, automation_mode='manual'), attempt)


def fixture_backend(tmp_path):
    review = {'id': 'fixture-plan', 'processes': [], 'actions': [], 'complete': True, 'errors': []}
    security = {'status': 'completed', 'recommendations': [{'id': 'enable_firewall', 'title': 'Enable firewall',
                                                          'reason': 'Fixture disabled', 'action': 'enable'}]}
    service = SimpleNamespace(data_dir=tmp_path, bridge=SimpleNamespace(_process=None),
        audit=Mock(return_value={}), installed_apps=Mock(return_value=[]),
        slowdown_report=Mock(return_value={}), startup_list=Mock(return_value=[]),
        driver_updates_scan=Mock(return_value={'resultCode': 2, 'updates': []}),
        updates_scan=Mock(return_value={'exitCode': 0, 'structured': True, 'updates': []}),
        cleanup_categories=Mock(return_value=[{'id': 'windows-temp', 'available': True}]),
        cleanup_scan=Mock(return_value={'files': [], 'complete': True, 'errors': []}),
        cleanup_apply=Mock(side_effect=AssertionError('No fixture files need deletion')),
        browser_cleanup_status=Mock(return_value={'running': False, 'complete': True, 'processes': []}),
        privacy_profiles=Mock(return_value=[]),
        debloat_preview=Mock(return_value={'planId': 'fixture', 'targets': []}),
        debloat_apply=Mock(), apply_system_privacy=Mock(return_value={'status': 'completed', 'failed_items': 0}),
        windows_security_updates=Mock(return_value={'review_only': True, 'scan': {'updates': []}, 'failed_items': 0}),
        legacy_patch_review=Mock(return_value={'review_only': True, 'recommendations': []}),
        scan_spyware=Mock(return_value={'status': 'completed', 'failed_items': 0}),
        security_status=Mock(return_value=security), process_harness_scan=Mock(return_value=review),
        apply_security_recommendation=Mock(), process_harness_apply=Mock(), update_app_unattended=Mock(),
        verify_optimization=Mock(side_effect=mock_verification))
    return service, review, security


def test_full_workflow_uses_zero_age_and_returns_reviews_without_taking_actions(tmp_path):
    service, processes, security = fixture_backend(tmp_path)
    settings = full_optimization_settings()
    # Browser privacy has separate integration fixtures; keep this test scoped.
    settings.update(browser_cache_enabled=False, browser_history_enabled=False,
                    browser_cookies_enabled=False, browser_download_history_enabled=False)
    result = Workflow(service).run(settings, lambda _: None)
    assert result['status'] == 'completed'
    assert service.cleanup_scan.call_args_list[0].args == (['user-temp'], 0)
    assert result['process_review'] == processes
    assert result['security_review']['recommendations'] == security['recommendations']
    assert len(result['security_review']['update_coverage']) == 3
    assert result['stages'][-2]['result']['review_only'] is True
    assert result['stages'][-1]['result'] == result['final_verification']
    service.apply_system_privacy.assert_called_once()
    service.scan_spyware.assert_called_once()
    service.process_harness_apply.assert_not_called()
    service.apply_security_recommendation.assert_not_called()
    service.update_app_unattended.assert_not_called()
    assert json.loads((tmp_path / 'runs' / (result['id'] + '.json')).read_text())['process_review'] == processes


def test_automatic_consumer_removal_respects_exact_list_and_scope(tmp_path):
    service, _, _ = fixture_backend(tmp_path)
    service.debloat_preview.return_value = {'planId': 'fixture', 'targets': [
        {'id': 'yes', 'name': 'Microsoft.BingNews', 'kind': 'appx', 'scope': 'current-user'},
        {'id': 'not-selected', 'name': 'Microsoft.BingWeather', 'kind': 'appx', 'scope': 'current-user'},
        {'id': 'protected', 'name': 'Microsoft.BingWallpaper', 'kind': 'appx', 'scope': 'current-user'},
        {'id': 'future-user', 'name': 'Microsoft.BingNews', 'kind': 'provisioned', 'scope': 'future-users'},
        {'id': 'wrong-kind', 'name': 'Microsoft.BingNews', 'kind': 'office-dialog', 'scope': 'current-user'},
    ]}
    service.debloat_apply.return_value = {'results': [{'id': 'yes', 'removed': True}]}
    settings = dict(DEFAULTS, cleanup_enabled=False, browser_cache_enabled=False,
                    consumer_debloat_enabled=True, consumer_debloat_package_names=['Microsoft.BingNews'])
    result = Workflow(service).run(settings, lambda _: None)
    assert result['status'] == 'completed'
    service.debloat_apply.assert_called_once_with({'planId': 'fixture', 'selectedIds': ['yes'], 'confirmed': True})
