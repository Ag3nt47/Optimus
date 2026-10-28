"""Driver cleanup preference transport; all native operations are mocked."""
from unittest.mock import Mock

import pytest

from optimus.backend import Backend


def backend(settings):
    value = Backend.__new__(Backend)
    value.preferences = Mock(return_value=settings)
    value.bridge = Mock()
    return value


@pytest.mark.parametrize('method', ['driver_update_apply', 'driver_update_apply_unattended'])
@pytest.mark.parametrize('settings,expected', [({}, True),
    ({'update_payload_cleanup_enabled': True}, True),
    ({'update_payload_cleanup_enabled': False}, False)])
def test_driver_request_uses_current_saved_cleanup_choice(method, settings, expected):
    value = backend(settings)
    outcome = {'resultCode': 2, 'payload_cleanup': {
        'ownership': 'operating_system', 'status': 'os_managed',
        'removed_files': 0, 'removed_bytes': 0}}
    value.bridge.request.return_value = outcome
    record = {'id': '00000000-0000-0000-0000-000000000001', 'revision': 3}
    kwargs = {'accept_eula': True} if method.endswith('unattended') else {}
    assert getattr(value, method)(record, **kwargs) is outcome
    args = value.bridge.request.call_args
    assert args.args == ('driver_update_apply',)
    assert args.kwargs['cleanupDownloads'] is expected
    assert args.kwargs['id'] == record['id'] and args.kwargs['revision'] == 3
    assert value.preferences.call_count == 1


@pytest.mark.parametrize('method', ['driver_update_apply', 'driver_update_apply_unattended'])
@pytest.mark.parametrize('invalid', [None, 0, 1, 'false', [], {}])
def test_corrupt_cleanup_choice_cannot_launch_driver_work(method, invalid):
    value = backend({'update_payload_cleanup_enabled': invalid})
    kwargs = {'accept_eula': True} if method.endswith('unattended') else {}
    with pytest.raises(ValueError, match='on or off'):
        getattr(value, method)({'id': 'fixture', 'revision': 1}, **kwargs)
    value.bridge.request.assert_not_called()


def test_saved_cleanup_choice_is_read_again_for_each_manual_driver_update():
    value = backend({})
    value.preferences.side_effect = [
        {'update_payload_cleanup_enabled': True},
        {'update_payload_cleanup_enabled': False}]
    record = {'id': 'fixture', 'revision': 1}
    value.driver_update_apply(record)
    value.driver_update_apply(record)
    assert [call.kwargs['cleanupDownloads'] for call in value.bridge.request.call_args_list] == [True, False]
