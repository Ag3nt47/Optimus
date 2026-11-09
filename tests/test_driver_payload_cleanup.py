"""Driver cleanup preference transport; all native operations are mocked."""
from unittest.mock import Mock

import pytest

from optimus.backend import Backend
from optimus.automated_system import driver_descriptor
from optimus.automation import CURRENT_SETUP_VERSION


DRIVER = driver_descriptor({'id': '00000000-0000-0000-0000-000000000001',
    'revision': 3, 'title': 'Offline payload-cleanup fixture driver',
    'hardwareId': r'PCI\VEN_1234&DEV_5678', 'provider': 'Fixture provider',
    'driverClass': 'Display', 'driverDate': '2026-10-01T00:00:00Z',
    'canRequestUserInput': False, 'rebootBehavior': 2, 'eulaAccepted': False})


def backend(settings):
    value = Backend.__new__(Backend)
    current = {'setup_version': CURRENT_SETUP_VERSION, 'driver_updates_enabled': True,
        'driver_policy': 'install_approved', 'driver_changes_approved': True,
        'driver_update_agreements_accepted': True, 'driver_approved_updates': [dict(DRIVER)]}
    value.preferences = Mock(return_value=dict(current, **settings))
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
    record = dict(DRIVER)
    kwargs = {'accept_eula': True} if method.endswith('unattended') else {}
    assert getattr(value, method)(record, **kwargs) is outcome
    args = value.bridge.request.call_args
    assert args.args == ('driver_update_apply',)
    assert args.kwargs['cleanupDownloads'] is expected
    assert args.kwargs['id'] == record['id'] and args.kwargs['revision'] == 3
    # Automatic calls first recheck approval, then read the cleanup choice.
    assert value.preferences.call_count == (2 if method.endswith('unattended') else 1)


@pytest.mark.parametrize('method', ['driver_update_apply', 'driver_update_apply_unattended'])
@pytest.mark.parametrize('invalid', [None, 0, 1, 'false', [], {}])
def test_corrupt_cleanup_choice_cannot_launch_driver_work(method, invalid):
    value = backend({'update_payload_cleanup_enabled': invalid})
    kwargs = {'accept_eula': True} if method.endswith('unattended') else {}
    with pytest.raises(ValueError, match='on or off'):
        getattr(value, method)(dict(DRIVER), **kwargs)
    value.bridge.request.assert_not_called()


def test_saved_cleanup_choice_is_read_again_for_each_manual_driver_update():
    value = backend({})
    value.preferences.side_effect = [
        dict(value.preferences.return_value, update_payload_cleanup_enabled=True),
        dict(value.preferences.return_value, update_payload_cleanup_enabled=False)]
    record = dict(DRIVER)
    value.driver_update_apply(record)
    value.driver_update_apply(record)
    assert [call.kwargs['cleanupDownloads'] for call in value.bridge.request.call_args_list] == [True, False]
