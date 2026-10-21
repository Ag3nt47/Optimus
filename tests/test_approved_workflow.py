"""Persisted permissions and automatic workflow routing use fixtures only."""
import copy
import threading
from types import SimpleNamespace
from unittest.mock import Mock, patch
import pytest
from optimus.automation import CURRENT_SETUP_VERSION
from optimus.workflow import DEFAULTS, Preferences, Workflow


def configuration(**values):
    result = copy.deepcopy(DEFAULTS)
    result.update({'setup_version': CURRENT_SETUP_VERSION, 'cleanup_enabled': False, 'browser_cache_enabled': False})
    result.update(values)
    return result


def local_choices(**values):
    return configuration(duplicates_enabled=True, duplicate_policy='keep_oldest', duplicate_changes_approved=True,
                         duplicate_roots=[r'C:\Fixture\Duplicates'], **values)


def cloud_choices(**values):
    return configuration(cloud_review_enabled=True, cloud_policy='keep_newest', cloud_changes_approved=True,
                         cloud_drive_id='drive!fixture', cloud_folder_ids=['folder!fixture'], **values)


def backend(tmp_path):
    return SimpleNamespace(data_dir=tmp_path, bridge=SimpleNamespace(_process=None), audit=Mock(return_value={'fixture': True}))


def test_legacy_setup_preserves_review_defaults_without_new_permissions():
    saved = Preferences.validate({'setup_version': 1, 'automation_mode': 'daily', 'duplicates_enabled': True})
    assert saved['duplicate_policy'] == 'review'
    assert not saved['duplicate_changes_approved']
    assert not saved['cloud_changes_approved']
    assert not saved['startup_changes_approved']
    assert not saved['driver_changes_approved']


@pytest.mark.parametrize('changes', [
    {'duplicate_changes_approved': False}, {'setup_version': 1}, {'duplicate_roots': []},
    {'duplicate_max_recycle': True}, {'duplicate_max_recycle': 501}, {'duplicate_min_age_days': 0},
])
def test_duplicate_rules_cannot_apply_without_valid_current_scope(changes):
    value = local_choices()
    value.update(changes)
    with pytest.raises(ValueError):
        Preferences.validate(value)


@pytest.mark.parametrize('changes', [
    {'cloud_changes_approved': False}, {'setup_version': 1}, {'cloud_drive_id': ''},
    {'cloud_folder_ids': []}, {'cloud_folder_ids': ['root']}, {'cloud_folder_ids': ['../other']},
    {'cloud_folder_ids': ['folder!fixture', 'folder!fixture']}, {'cloud_max_recycle': False},
])
def test_cloud_permissions_need_bound_concrete_scopes(changes):
    value = cloud_choices()
    value.update(changes)
    with pytest.raises(ValueError):
        Preferences.validate(value)


def test_disabled_action_can_retain_its_choices_without_applying_or_extending_consent():
    value = Preferences.validate(configuration(duplicate_policy='keep_oldest', duplicate_changes_approved=False))
    assert value['duplicates_enabled'] is False
    assert value['duplicate_changes_approved'] is False


def test_scopes_are_copied_and_do_not_mutate_global_defaults():
    value = Preferences.validate(cloud_choices())
    value['cloud_folder_ids'].append('folder!other')
    assert DEFAULTS['cloud_folder_ids'] == []


def test_approved_duplicate_workflow_uses_saved_rules_without_confirmation(tmp_path):
    service = backend(tmp_path)
    choices = local_choices()
    with patch('optimus.automated_files.run_local', return_value={'items': [{'status':'recycled'}], 'failed_items':0}) as apply:
        result = Workflow(service).run(choices, lambda _: None)
    apply.assert_called_once()
    assert apply.call_args.args[0] is service
    assert apply.call_args.args[1]['duplicate_roots'] == choices['duplicate_roots']
    assert result['status'] == 'completed'
    assert next(row for row in result['stages'] if 'local duplicates' in row['title'])['result']['items'][0]['status'] == 'recycled'


def test_approved_cloud_failure_remains_failed_without_blocking_final_report(tmp_path):
    service = backend(tmp_path)
    with patch('optimus.automated_files.run_cloud', return_value={'items':[], 'failed_items':1, 'skips':['changed account']}) as apply:
        result = Workflow(service).run(cloud_choices(), lambda _: None)
    assert apply.call_count == 1
    assert result['status'] == 'completed_with_errors'
    assert result['failed_stages'] == 1
    assert service.audit.call_count == 2


def test_cancellation_from_automatic_module_stops_further_stages(tmp_path):
    service = backend(tmp_path)
    cancelled = threading.Event()
    def stop(*args, **kwargs):
        cancelled.set()
        return {'cancelled': True, 'items':[], 'failed_items':0}
    with patch('optimus.automated_files.run_local', side_effect=stop):
        result = Workflow(service).run(local_choices(), lambda _: None, cancelled)
    assert result['status'] == 'cancelled'
    assert service.audit.call_count == 1
