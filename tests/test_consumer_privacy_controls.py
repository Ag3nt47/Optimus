"""Supported privacy controls use only mock registry/OS evidence."""
import copy
import json
import os
from unittest.mock import Mock

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

import pytest

from optimus.native_settings import NativePreferences, full_native_settings
from optimus.onboarding import InitialSetupWizard
from optimus.system_privacy import SystemPrivacy, _CONTROLS, _script
from optimus.workflow import DEFAULTS, Preferences, Workflow, full_optimization_settings
from test_system_privacy import NativeFixture, dword, mutation_calls, settings
from test_workflow import MockBackend


NEW_FLAGS = ('privacy_block_silent_consumer_apps_enabled', 'privacy_disable_cloud_promotions_enabled',
             'privacy_disable_clipboard_sync_enabled', 'privacy_disable_recall_snapshots_enabled')
SILENT = 'suggestions_SilentInstalledAppsEnabled'
POLICIES = {'consumer_features', 'cloud_optimized_content', 'consumer_account_content',
            'cross_device_clipboard', 'recall_snapshots'}


@pytest.fixture
def fixture(tmp_path):
    runner = NativeFixture()
    return SystemPrivacy(tmp_path, runner=runner), runner


def only(**changes):
    return dict.fromkeys(NEW_FLAGS, False) | changes


def test_new_preferences_and_full_presets_preserve_explicit_opt_in():
    for config in (DEFAULTS, Preferences.validate({}), Preferences.safe_defaults(),
                   full_optimization_settings(), full_native_settings()):
        assert all(config[key] is False for key in NEW_FLAGS)
    for key in NEW_FLAGS:
        assert Preferences.validate({key: True})[key] is True
        assert full_optimization_settings({key: True})[key] is True
        assert NativePreferences.validate({key: True})[key] is False


@pytest.mark.parametrize('key', NEW_FLAGS)
@pytest.mark.parametrize('invalid', [None, 0, 1, 'yes'])
def test_new_preferences_and_direct_apply_reject_coercion(fixture, key, invalid):
    tool, runner = fixture
    with pytest.raises(ValueError):
        Preferences.validate({key: invalid})
    with pytest.raises(ValueError):
        tool.apply_privacy({key: invalid})
    assert not runner.calls


def test_legacy_approvals_never_enable_new_policy_scopes(fixture):
    tool, runner = fixture
    result = tool.apply_privacy(settings())
    assert not POLICIES.intersection(item['id'] for item in result['items'])
    assert not any(payload['control'] in POLICIES for _, payload, _ in mutation_calls(runner))


def test_separate_silent_install_approval_works_on_home_only_for_existing_user_preference(fixture):
    tool, runner = fixture
    runner.context.update(edition='Core', is_admin=False)
    result = tool.apply_privacy(only(privacy_block_silent_consumer_apps_enabled=True))
    items = {row['id']: row for row in result['items']}
    assert set(items) == {SILENT, 'consumer_features'}
    assert items[SILENT]['status'] == 'applied'
    assert items['consumer_features']['status'] == 'unsupported'
    assert result['changed_count'] == 1
    assert [payload['control'] for _, payload, _ in mutation_calls(runner)] == [SILENT]
    assert 'do not block every installer' in result['note']


def test_missing_silent_preference_is_not_created_on_home(fixture):
    tool, runner = fixture
    runner.context['edition'] = 'Core'
    runner.values = {}
    result = tool.apply_privacy(only(privacy_block_silent_consumer_apps_enabled=True))
    assert all(row['status'] == 'unsupported' for row in result['items'])
    assert not mutation_calls(runner)


@pytest.mark.parametrize('edition', ['Core', 'CoreSingleLanguage', 'Professional', 'ProfessionalEducation', 'unknown'])
def test_enterprise_content_policies_are_never_written_on_unsupported_editions(fixture, edition):
    tool, runner = fixture
    runner.context['edition'] = edition
    result = tool.apply_privacy(only(privacy_disable_cloud_promotions_enabled=True))
    assert {row['id'] for row in result['items']} == {'cloud_optimized_content', 'consumer_account_content'}
    assert all(row['status'] == 'unsupported' for row in result['items'])
    assert not mutation_calls(runner)


@pytest.mark.parametrize('edition', ['Enterprise', 'EnterpriseS', 'Education', 'IoTEnterpriseS'])
def test_supported_enterprise_controls_save_originals_and_read_back_then_undo(fixture, edition):
    tool, runner = fixture
    runner.context.update(edition=edition, ubr=3915)
    original = copy.deepcopy(runner.values)
    def inspect(payload):
        journal = next(tool.directory.glob('*.json'))
        item = next(row for row in json.loads(journal.read_text())['items'] if row['id'] == payload['control'])
        assert item['before'] == payload['expected'] and item['status'] == 'prepared'
    runner.before_write = inspect
    result = tool.apply_privacy(dict.fromkeys(NEW_FLAGS, True))
    assert result['status'] == 'completed'
    assert result['changed_count'] == 6
    assert all(row['status'] == 'applied' for row in result['items'])
    assert POLICIES | {SILENT} == {row['id'] for row in result['items']}
    runner.before_write = None
    restored = tool.restore_privacy(result['id'])
    assert restored['status'] == 'completed'
    assert all(row['status'] == 'restored' for row in restored['items'])
    assert all(runner.values[key] == before for key, before in original.items())
    assert all(runner.values[key] == {'exists': False} for key in POLICIES)
    recall = next(row for row in restored['items'] if row['id'] == 'recall_snapshots')
    assert 'cannot restore deleted snapshots' in recall['note']


def test_devicewide_controls_need_admin_even_on_supported_edition(fixture):
    tool, runner = fixture
    runner.context.update(edition='Enterprise', ubr=3915, is_admin=False)
    result = tool.apply_privacy(dict.fromkeys(NEW_FLAGS, True))
    assert result['changed_count'] == 1  # Existing user preference remains eligible.
    assert sum(row['status'] == 'requires_admin' for row in result['items']) == 5
    assert all(payload['control'] == SILENT for _, payload, _ in mutation_calls(runner))


@pytest.mark.parametrize('ubr', [None, True, 3914, '3915'])
def test_recall_exact_cumulative_revision_must_be_verified(fixture, ubr):
    tool, runner = fixture
    runner.context['ubr'] = ubr
    result = tool.apply_privacy(only(privacy_disable_recall_snapshots_enabled=True))
    assert result['items'][0]['status'] == 'unsupported'
    assert not mutation_calls(runner)


@pytest.mark.parametrize('build,ubr', [(26100, 3915), (26100, 5000), (26200, None)])
def test_supported_recall_policy_is_explicit_about_irreversible_windows_deletion(fixture, build, ubr):
    tool, runner = fixture
    runner.context.update(build=build, ubr=ubr)
    result = tool.apply_privacy(only(privacy_disable_recall_snapshots_enabled=True))
    item = result['items'][0]
    assert item['status'] == 'applied'
    assert 'deletes previously saved snapshots' in item['note']
    assert 'does not verify snapshot deletion' in item['note']
    assert runner.values['recall_snapshots'] == dword(1)


def test_clipboard_disables_only_cross_device_sync_not_local_history(fixture):
    tool, runner = fixture
    runner.context['build'] = 17763
    result = tool.apply_privacy(only(privacy_disable_clipboard_sync_enabled=True))
    assert result['status'] == 'completed'
    assert [payload['control'] for _, payload, _ in mutation_calls(runner)] == ['cross_device_clipboard']
    item = result['items'][0]
    assert item['desired'] == 0 and 'Local clipboard history is unchanged' in item['note']
    assert all(control['name'] != 'AllowClipboardHistory' for control in _CONTROLS.values())


@pytest.mark.parametrize('key,build', [('cross_device_clipboard', 17762), ('recall_snapshots', 26099),
                                   ('cloud_optimized_content', 21999), ('consumer_features', 14392)])
def test_documented_build_floors_are_enforced(fixture, key, build):
    tool, runner = fixture
    runner.context.update(build=build, edition='Enterprise', ubr=9999)
    result = tool.apply_privacy(only(**{_CONTROLS[key]['master']: True}))
    assert next(row for row in result['items'] if row['id'] == key)['status'] == 'unsupported'
    assert not any(payload['control'] == key for _, payload, _ in mutation_calls(runner))


def test_generated_native_context_reads_update_revision_and_all_writes_stay_allowlisted():
    script = _script('context', {})
    assert 'ubr=[int]$v.UBR' in script
    assert 'Set-MpPreference -DisableRealtimeMonitoring $false' in script
    assert 'Set-MpPreference -DisableRealtimeMonitoring $true' not in script
    assert all(control['hive'] in ('CurrentUser', 'LocalMachine') for control in _CONTROLS.values())


@pytest.mark.parametrize('key', NEW_FLAGS)
def test_independent_new_toggle_dispatches_existing_privacy_stage(tmp_path, key):
    backend = MockBackend(tmp_path)
    backend.apply_system_privacy = Mock(return_value={'status': 'completed', 'failed_items': 0,
                                                     'changed_count': 0, 'items': []})
    config = dict(DEFAULTS, cleanup_enabled=False, browser_cache_enabled=False, **{key: True})
    report = Workflow(backend).run(config, lambda _: None)
    backend.apply_system_privacy.assert_called_once()
    assert any(row['title'] == 'Apply saved privacy controls' for row in report['stages'])
    assert report['failed_stages'] == 0


def test_windows_setup_discloses_new_optional_policies_without_saving(tmp_path):
    from PySide6.QtWidgets import QApplication
    app = QApplication.instance() or QApplication([])
    backend = Mock(data_dir=tmp_path, preferences=Mock(return_value=copy.deepcopy(DEFAULTS)), cleanup_categories=Mock(return_value=[]))
    wizard = InitialSetupWizard(backend)
    try:
        assert all(not wizard.controls[key].isChecked() for key in NEW_FLAGS)
        assert 'deleted snapshots cannot be restored' in wizard.controls['privacy_disable_recall_snapshots_enabled'].text()
        wizard.controls['privacy_disable_recall_snapshots_enabled'].setChecked(True)
        wizard.refresh_summary()
        assert wizard.collect_choices()['privacy_disable_recall_snapshots_enabled'] is True
        wizard.reject()
        backend.save_preferences.assert_not_called()
    finally:
        wizard.deleteLater()
        app.processEvents()
