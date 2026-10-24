"""Only the facade's own last read-only report can become an expirable copy."""
import copy
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from optimus.maintenance_toolkit import MaintenanceToolkit


def facade(tmp_path):
    return MaintenanceToolkit(SimpleNamespace(data_dir=tmp_path, preferences=lambda: {}))


def test_arbitrary_caller_report_cannot_be_archived(tmp_path):
    tool = facade(tmp_path)
    with pytest.raises(ValueError, match='complete read-only'):
        tool.archive_last_report()
    with pytest.raises(TypeError):
        tool.archive_last_report({'complete': True})
    assert not list(tmp_path.iterdir())


def test_archiver_receives_defensive_copy_of_owned_result_and_exact_kind(tmp_path):
    tool = facade(tmp_path)
    result = {'schema': 1, 'read_only': True, 'id': 'a' * 32, 'nested': {'preserve': True}}
    snapshot = copy.deepcopy(result)
    archive = Mock(return_value={'status': 'archived'})
    tool._services['recovery_retention'] = SimpleNamespace(archive_completed_report=archive)
    tool._remember_report(result, 'health_check')
    result['nested']['preserve'] = False
    assert tool.archive_last_report()['status'] == 'archived'
    archive.assert_called_once_with(snapshot, kind='health_check')


def test_incomplete_owned_result_is_still_validated_by_retention_service(tmp_path):
    tool = facade(tmp_path)
    tool._remember_report({'schema': 1, 'read_only': True, 'id': 'a' * 32,
        'maintenance_actions_executed': 0, 'resource_window': {'complete': False}}, 'health_check')
    with pytest.raises(ValueError):
        tool.archive_last_report()
