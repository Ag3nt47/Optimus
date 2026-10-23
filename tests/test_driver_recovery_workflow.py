"""Exact driver recovery integration; all native exports/installs are fixtures."""
from pathlib import Path
from unittest.mock import Mock

import pytest

from optimus.automated_system import run_drivers
from optimus.driver_update_backup import DriverUpdateBackup
from optimus.parallel_updates import installer_barrier
from test_automated_system import backend, driver, driver_config
from test_driver_update_backup import FakeAdapter, attach_inbox_fixture


def blocked(offered):
    return {'exit_code': 1, 'destination': 'private-fixture', 'status': 'blocked',
        'backed_updates': [], 'blocked_updates': [
            {'id': row['id'], 'revision': row['revision'], 'reason': 'A complete matching recovery backup was unavailable.'}
            for row in offered]}


def test_all_exact_offers_known_blocked_do_not_invent_a_failed_driver():
    value = backend()
    first, second = driver(), driver(id='00000000-0000-0000-0000-000000000002')
    value.driver_updates_scan.return_value['updates'] = [first, second]
    value.driver_backup_for_updates = Mock(return_value=blocked([first, second]))
    result = run_drivers(value, driver_config(driver_approved_updates=[first, second]))
    assert result['failed_items'] == 0 and result['skipped_items'] == 2 and result['backup_unavailable'] is True
    assert len(result['items']) == 2 and all(row['binding'].get('id') for row in result['items'])
    assert result['installer_started'] is False and result['outcomeKnown'] is True and result['running'] is False
    assert installer_barrier(result) is None
    value.driver_update_apply_unattended.assert_not_called()
    value.driver_backup_automatic.assert_not_called()


@pytest.mark.parametrize('change', [
    lambda receipt: receipt.update(status='failed'),
    lambda receipt: receipt.update(exit_code=True),
    lambda receipt: receipt.update(destination=None),
    lambda receipt: receipt.update(blocked_updates=[]),
    lambda receipt: receipt['blocked_updates'].append(dict(receipt['blocked_updates'][0])),
    lambda receipt: receipt['blocked_updates'][0].update(revision=2),
    lambda receipt: receipt['blocked_updates'][0].update(reason=''),
    lambda receipt: receipt.update(error='An unverified prerequisite failure remains.'),
    lambda receipt: receipt.update(backed_updates=[{'id':driver()['id'],'revision':1}]),
])
def test_malformed_or_contradictory_all_blocked_receipt_keeps_failure(change):
    value = backend()
    receipt = blocked([driver()]); change(receipt)
    value.driver_backup_for_updates = Mock(return_value=receipt)
    result = run_drivers(value, driver_config())
    assert result['failed_items'] == 1 and not result.get('backup_unavailable')
    value.driver_update_apply_unattended.assert_not_called()


def inbox_service(tmp_path):
    value = backend(); value.data_dir = tmp_path / 'state'
    adapter = FakeAdapter()
    _, _, description = attach_inbox_fixture(adapter, tmp_path)
    service = DriverUpdateBackup(value, adapter=adapter, owner='fixture-owner', system='win32')
    value.driver_backup_for_updates = Mock(side_effect=service.backup)
    value.driver_update_apply_unattended.return_value.update(installer_started=True, outcomeKnown=True, running=False,
        aggregateResultCode=2, aggregateHresult=0)
    return value, adapter, description


def test_production_targeted_backup_allows_exact_wua_offer_only_after_complete_inbox_copy(tmp_path):
    value, adapter, _ = inbox_service(tmp_path)
    def install(offered, **kwargs):
        receipts = list((value.data_dir / 'recovery' / 'driver-update-backups').glob('*/receipt.json'))
        import json
        receipt = json.loads(receipts[0].read_text())
        assert receipt['status'] == 'completed'
        assert receipt['backed_updates'] == [{'id': offered['id'], 'revision': offered['revision']}]
        assert receipt['packages'][0]['final_verified'] is True
        assert receipt['packages'][0]['kind'] == 'inbox-driver-store-snapshot'
        return {'installer_started':True,'outcomeKnown':True,'running':False,'resultCode':2,
            'hresult':0,'aggregateResultCode':2,'aggregateHresult':0,'restartNeeded':False}
    value.driver_update_apply_unattended.side_effect = install
    result = run_drivers(value, driver_config())
    assert result['failed_items'] == 0 and result['items'][0]['status'] == 'applied'
    value.driver_update_apply_unattended.assert_called_once()
    value.driver_backup_automatic.assert_not_called()
    assert not any(call[0] == 'export' for call in adapter.calls)


def test_inbox_catalog_refusal_is_known_skipped_without_retry_or_install(tmp_path):
    value, adapter, description = inbox_service(tmp_path)
    description['catalog_trust_verified'] = False
    result = run_drivers(value, driver_config())
    assert result['failed_items'] == 0 and result['skipped_items'] == 1
    assert result['backup_unavailable'] is True and installer_barrier(result) is None
    assert 'incomplete' in result['items'][0]['reason']
    value.driver_update_apply_unattended.assert_not_called()
    adapter.snapshot_inbox.assert_called_once()


def test_device_switch_during_final_backup_readback_never_starts_installer(tmp_path):
    value, adapter, _ = inbox_service(tmp_path)
    original = adapter.verify_inbox.side_effect
    def switch_device(*args, **kwargs):
        result = original(*args, **kwargs)
        adapter.devices[0]['installedDrivers'][0]['inf'] = 'machine.inf'
        return result
    adapter.verify_inbox.side_effect = switch_device
    result = run_drivers(value, driver_config())
    assert result['skipped_items'] == 1 and result['backup_unavailable'] is True
    assert 'identity changed' in result['items'][0]['reason']
    value.driver_update_apply_unattended.assert_not_called()
