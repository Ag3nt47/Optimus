"""Driver result honesty and native post-checks using fixture inventories only."""
import copy
import json
import os
from pathlib import Path
import subprocess

import pytest

from optimus.automated_system import run_drivers, driver_device_rule
from optimus.parallel_updates import _journal_result
from test_automated_system import backend, driver, driver_config


@pytest.mark.parametrize('field', ['resultCode', 'hresult', 'aggregateResultCode', 'aggregateHresult',
                                  'installer_started', 'outcomeKnown', 'running'])
def test_missing_native_success_fields_are_never_labelled_applied(field):
    service = backend()
    del service.driver_update_apply_unattended.return_value[field]
    result = run_drivers(service, driver_config())
    assert result['items'][0]['status'] != 'applied'


@pytest.mark.parametrize(('field', 'value'), [('hresult', False), ('aggregateHresult', False),
    ('resultCode', True), ('aggregateResultCode', '2'), ('installer_started', 1), ('running', 0)])
def test_native_success_requires_typed_codes_and_boolean_lifecycle(field, value):
    service = backend()
    service.driver_update_apply_unattended.return_value[field] = value
    assert run_drivers(service, driver_config())['items'][0]['status'] != 'applied'


def test_unapproved_matching_offer_has_explicit_per_offer_reason():
    service = backend()
    offered = driver(hardwareId=r'USB\VID_05AC&PID_12A8', provider='Apple, Inc.', driverClass='OtherHardware')
    service.driver_updates_scan.return_value['updates'] = [offered]
    result = run_drivers(service, driver_config(driver_policy='install_matching',
        driver_approved_devices=[driver_device_rule(driver())]))
    assert result['items'][0]['binding']['id'] == offered['id']
    assert result['items'][0]['status'] == 'skipped'
    assert 'No exact saved hardware/provider/class rule' in result['items'][0]['reason']
    assert result['driver_scan']['offers_count'] == 1 and result['driver_scan']['eligible_count'] == 0
    service.driver_update_apply_unattended.assert_not_called()


def test_unconfirmed_post_check_is_not_applied_and_does_not_stop_next_driver():
    service = backend()
    second = driver(id='00000000-0000-0000-0000-000000000002')
    service.driver_updates_scan.return_value['updates'].append(second)
    good = copy.deepcopy(service.driver_update_apply_unattended.return_value)
    uncertain = dict(good, requiresReview=True, verified=False,
        post_install_verification={'schema': 1, 'status': 'still_offered', 'verified': False})
    service.driver_update_apply_unattended.side_effect = [uncertain, good]
    result = run_drivers(service, driver_config(driver_approved_updates=[driver(), second]))
    assert [row['status'] for row in result['items']] == ['needs_review', 'applied']
    assert service.driver_update_apply_unattended.call_count == 2


def test_known_current_driver_is_skipped_and_queue_continues():
    service = backend()
    second = driver(id='00000000-0000-0000-0000-000000000002')
    service.driver_updates_scan.return_value['updates'].append(second)
    good = copy.deepcopy(service.driver_update_apply_unattended.return_value)
    service.driver_update_apply_unattended.side_effect = [dict(good, installer_started=False,
        skipped=True, alreadyCurrent=True, verified=True), good]
    result = run_drivers(service, driver_config(driver_approved_updates=[driver(), second]))
    assert [row['status'] for row in result['items']] == ['skipped', 'applied']
    assert result['failed_items'] == 0


def test_compact_receipt_retains_bounded_fresh_verification_and_offer_identity():
    proof = {'schema': 1, 'status': 'confirmed', 'verified': True, 'searchComplete': True,
        'searchResultCode': 2, 'exactOfferStillApplicable': False, 'id': driver()['id'], 'revision': 1,
        'device': {'complete': True, 'versionMatches': True, 'dateMatches': True, 'providerMatches': True,
            'alreadyCurrent': True, 'offeredVersion': '2.0.0.0', 'versionSource': 'offer_title_suffix',
            'drivers': [{'deviceId': 'fixture-device', 'version': '2.0.0.0', 'date': '2026-10-02',
                         'provider': 'Fixture provider'}] * 30}}
    saved = _journal_result({'items': [{'binding': driver(), 'status': 'applied',
        'result': dict(backend().driver_update_apply_unattended.return_value, post_install_verification=proof)}]})
    row = saved['items'][0]
    assert row['binding']['revision'] == 1 and row['binding']['title'] == driver()['title']
    assert row['result']['post_install_verification']['exactOfferStillApplicable'] is False
    assert len(row['result']['post_install_verification']['device']['drivers']) == 20
    assert row['result']['post_install_verification']['device']['driver_count'] == 30
    assert row['result']['post_install_verification']['device']['drivers_truncated'] is True
    assert len(proof['device']['drivers']) == 30


@pytest.mark.skipif(os.name != 'nt', reason='Windows fake-WUA engine fixture')
def test_native_post_install_offer_and_device_version_verification():
    executable = Path(os.environ['SystemRoot']) / 'System32/WindowsPowerShell/v1.0/powershell.exe'
    script = Path(__file__).with_name('Test-DriverPostVerification.ps1')
    result = subprocess.run([str(executable), '-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass',
        '-File', str(script)], capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr
    proof = json.loads(result.stdout)
    assert proof['Result'] == 'PASS' and proof['Assertions'] >= 13
    assert proof['RealScans'] == proof['RealDownloads'] == proof['RealInstalls'] == 0
