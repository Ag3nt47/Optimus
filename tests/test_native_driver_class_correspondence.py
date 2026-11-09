"""Exact device approval survives WUA's generic class without host mutations."""
from copy import deepcopy
import json
import os
from pathlib import Path
import subprocess
from unittest.mock import Mock

import pytest

from optimus.automated_system import driver_device_rule, run_drivers
from optimus.driver_coverage import build_driver_coverage
from test_automated_system import backend, driver, driver_config


HARDWARE = r'SWC\VEN_MSFT&AUDIO_EFFECTPACK_VOICECLARITY'


def voice_offer(**changes):
    value = driver(title='Microsoft AudioProcessingObject Driver Update (1.0.4.55794)',
        hardwareId=HARDWARE.lower(), provider='Microsoft Corporation', driverClass='OtherHardware',
        driverDate='2026-10-16T06:00:00Z', canRequestUserInput=True)
    value.update(changes)
    return value


def voice_device(**changes):
    value = dict(deviceId=r'SWD\FixtureVoiceClarity', displayName='Voice Clarity',
        present=True, deviceClass='AudioProcessingObject', problemCode=0,
        hardwareIds=[HARDWARE], compatibleIds=[r'SWC\Generic'],
        installedDrivers=[dict(version='1.0.4.7057', date='2026-06-16T00:00:00Z',
            inf='oem1.inf', signed=True, provider='Microsoft Corporation',
            deviceClass='AUDIOPROCESSINGOBJECT')])
    value.update(changes)
    return value


def voice_config(**changes):
    value = driver_config(driver_policy='install_matching', driver_approved_updates=[],
        driver_approved_devices=[dict(hardwareId=HARDWARE, provider='Microsoft Corporation',
            **{'class': 'AUDIOPROCESSINGOBJECT', 'notOlderThanDriverDate': '2026-06-16'})])
    value.update(changes)
    return value


def scan(devices, offers=None):
    value = dict(resultCode=2, updates=[voice_offer()] if offers is None else offers,
                 online=True, errors=[], warnings=[])
    value['coverage'] = build_driver_coverage(dict(complete=True, devices=devices, errors=[]), value)
    return value


@pytest.mark.parametrize('multiple_devices', [False, True])
def test_otherhardware_uses_same_scan_signed_devices_and_preserves_exact_offer(multiple_devices):
    service = backend()
    devices = [voice_device()]
    if multiple_devices:
        devices.append(voice_device(deviceId=r'SWD\SecondFixtureVoiceClarity'))
    service.driver_updates_scan.return_value = scan(devices)
    settings = voice_config()
    original = deepcopy(settings)

    result = run_drivers(service, settings)

    service.driver_backup_automatic.assert_called_once_with()
    applied = service.driver_update_apply_unattended.call_args.args[0]
    assert applied == dict(voice_offer(), approvalMode='matching_installed_class',
        notOlderThanDriverDate='2026-06-16', installedClassApproval={
            key: settings['driver_approved_devices'][0][key] for key in ('hardwareId', 'provider', 'class')})
    assert applied['driverClass'] == 'OtherHardware'
    assert service.driver_update_apply_unattended.call_args.kwargs == {'accept_eula': True}
    assert result['driver_scan']['eligible_count'] == 1
    assert settings == original


@pytest.mark.parametrize('missing_proof', [
    'coverage', 'inventory_complete', 'scan_complete', 'inventory_error', 'no_device',
    'disconnected', 'unknown_presence', 'unknown_class', 'wrong_pnp_class',
    'missing_drivers', 'unsigned', 'unknown_signature', 'wrong_provider',
    'wrong_installed_class', 'missing_date', 'missing_identity', 'duplicate_identity',
    'conflicting_classes', 'wrong_hardware', 'malformed_hardware', 'missing_provider',
])
def test_missing_or_conflicting_device_proof_skips_only_that_offer(missing_proof):
    service = backend()
    devices = [voice_device()]
    device = devices[0]
    installed = device['installedDrivers'][0]
    if missing_proof == 'no_device':
        devices.clear()
    elif missing_proof == 'disconnected':
        device['present'] = False
    elif missing_proof == 'unknown_presence':
        device['present'] = None
    elif missing_proof == 'unknown_class':
        device['deviceClass'] = 'Unknown'
    elif missing_proof == 'wrong_pnp_class':
        device['deviceClass'] = 'Display'
    elif missing_proof == 'missing_drivers':
        device['installedDrivers'] = []
    elif missing_proof == 'unsigned':
        installed['signed'] = False
    elif missing_proof == 'unknown_signature':
        installed['signed'] = None
    elif missing_proof == 'wrong_provider':
        installed['provider'] = 'Different provider'
    elif missing_proof == 'wrong_installed_class':
        installed['deviceClass'] = 'Display'
    elif missing_proof == 'missing_date':
        installed['date'] = ''
    elif missing_proof == 'missing_identity':
        device['deviceId'] = ''
    elif missing_proof == 'duplicate_identity':
        devices.append(deepcopy(device))
    elif missing_proof == 'conflicting_classes':
        other = voice_device(deviceId=r'SWD\OtherFixture', deviceClass='Display')
        other['installedDrivers'][0]['deviceClass'] = 'Display'
        devices.append(other)
    elif missing_proof == 'wrong_hardware':
        device['hardwareIds'] = [r'USB\Unrelated']
    elif missing_proof == 'missing_provider':
        installed['provider'] = ''
    next_offer = driver(id='00000000-0000-0000-0000-000000000002')
    current_scan = scan(devices, [voice_offer(), next_offer])
    if missing_proof == 'coverage':
        del current_scan['coverage']
    elif missing_proof == 'inventory_complete':
        current_scan['coverage']['inventoryComplete'] = False
    elif missing_proof == 'scan_complete':
        current_scan['coverage']['updateScanComplete'] = False
    elif missing_proof == 'inventory_error':
        current_scan['coverage']['inventoryErrors'] = [{'error': 'Fixture incomplete inventory'}]
    elif missing_proof == 'malformed_hardware':
        current_scan['coverage']['devices'][0]['hardwareIds'] = [HARDWARE, None]
    service.driver_updates_scan.return_value = current_scan
    settings = voice_config()
    settings['driver_approved_devices'].append(driver_device_rule(next_offer))

    result = run_drivers(service, settings)

    assert result['items'][0]['binding']['id'] == voice_offer()['id']
    assert result['items'][0]['status'] == 'skipped'
    service.driver_update_apply_unattended.assert_called_once()
    assert service.driver_update_apply_unattended.call_args.args[0]['id'] == next_offer['id']
    assert result['driver_scan']['eligible_count'] == 1


@pytest.mark.parametrize('changed', [dict(hardwareId=r'USB\VID_05AC&PID_12A8'),
    dict(provider='Different publisher'), dict(driverClass='Display'),
    dict(driverDate='2026-06-15T00:00:00Z')])
def test_correspondence_cannot_expand_hardware_provider_class_or_date_approval(changed):
    service = backend()
    service.driver_updates_scan.return_value = scan([voice_device()], [voice_offer(**changed)])

    result = run_drivers(service, voice_config())

    assert result['items'][0]['status'] == 'skipped'
    service.driver_backup_automatic.assert_not_called()
    service.driver_update_apply_unattended.assert_not_called()


def test_exact_compatible_id_needs_the_same_saved_provider_and_actual_class():
    service = backend()
    device = voice_device(hardwareIds=[r'SWC\FixtureSpecific'], compatibleIds=[HARDWARE])
    service.driver_updates_scan.return_value = scan([device])

    result = run_drivers(service, voice_config())

    assert result['driver_scan']['eligible_count'] == 1
    service.driver_update_apply_unattended.assert_called_once()


def fallback_record():
    return dict(voice_offer(), approvalMode='matching_installed_class',
        notOlderThanDriverDate='2026-06-16', installedClassApproval={
            key: voice_config()['driver_approved_devices'][0][key] for key in ('hardwareId', 'provider', 'class')})


@pytest.mark.parametrize('policy', ['install_approved', 'install_matching', 'installed_class'])
def test_current_exact_saved_scope_reaches_bridge_without_altering_wua_metadata(policy):
    from optimus.backend import Backend
    service = Backend.__new__(Backend)
    service.bridge = Mock()
    if policy == 'installed_class':
        record, settings = fallback_record(), voice_config()
    elif policy == 'install_matching':
        record = dict(driver(), approvalMode='matching', notOlderThanDriverDate='2026-10-01')
        settings = driver_config(driver_policy='install_matching',
            driver_approved_devices=[driver_device_rule(driver())])
    else:
        record, settings = driver(), driver_config()
    service.preferences = Mock(return_value=settings)

    service.driver_update_apply_unattended(record, accept_eula=True)

    service.bridge.request.assert_called_once()
    assert service.bridge.request.call_args.kwargs['record'] is record
    assert service.bridge.request.call_args.kwargs['record']['driverClass'] == record['driverClass']


@pytest.mark.parametrize('revoked', ['enabled', 'setup', 'agreement', 'changes', 'policy',
    'rule', 'provider', 'hardware', 'class', 'floor', 'missing_binding', 'malformed_binding', 'binding_hardware'])
def test_current_saved_approval_is_required_before_native_bridge(revoked):
    from optimus.backend import Backend
    from optimus.maintenance import UpdatePreflightError
    service = Backend.__new__(Backend)
    service.bridge = Mock()
    settings = voice_config()
    record = fallback_record()
    changes = {'enabled': ('driver_updates_enabled', False), 'setup': ('setup_version', 0),
        'agreement': ('driver_update_agreements_accepted', False), 'changes': ('driver_changes_approved', False),
        'policy': ('driver_policy', 'review'), 'rule': ('driver_approved_devices', [])}
    if revoked in changes:
        key, value = changes[revoked]
        settings[key] = value
    elif revoked in ('provider', 'hardware', 'class', 'floor'):
        key = {'hardware': 'hardwareId', 'floor': 'notOlderThanDriverDate'}.get(revoked, revoked)
        settings['driver_approved_devices'][0][key] = '2026-06-17' if revoked == 'floor' else 'Changed approval'
    elif revoked == 'missing_binding':
        del record['installedClassApproval']
    elif revoked == 'malformed_binding':
        record['installedClassApproval']['unexpected'] = 'Changed binding'
    else:
        record['installedClassApproval']['hardwareId'] = r'USB\OtherDevice'
    service.preferences = Mock(return_value=settings)

    with pytest.raises(UpdatePreflightError) as caught:
        service.driver_update_apply_unattended(record, accept_eula=True)

    assert caught.value.installer_started is False
    assert caught.value.outcomeKnown is True and caught.value.running is False
    assert caught.value.failure_scope == 'package'
    service.bridge.request.assert_not_called()


def test_unreadable_current_preferences_is_a_known_no_launch_driver_refusal():
    from optimus.backend import Backend
    from optimus.maintenance import UpdatePreflightError
    service = Backend.__new__(Backend)
    service.bridge = Mock()
    service.preferences = Mock(side_effect=OSError('Fixture settings unavailable'))

    with pytest.raises(UpdatePreflightError) as caught:
        service.driver_update_apply_unattended(fallback_record(), accept_eula=True)

    assert caught.value.failure_scope == 'package' and caught.value.outcomeKnown is True
    assert caught.value.installer_started is False and caught.value.running is False
    service.bridge.request.assert_not_called()


def test_revoked_one_device_rule_continues_other_currently_approved_driver():
    from optimus.backend import Backend
    service = backend()
    next_offer = driver(id='00000000-0000-0000-0000-000000000002')
    snapshot = voice_config()
    snapshot['driver_approved_devices'].append(driver_device_rule(next_offer))
    current = deepcopy(snapshot)
    current['driver_approved_devices'].pop(0)
    service.preferences = Mock(return_value=current)
    service._driver_payload_cleanup_enabled = lambda: True
    service.bridge = Mock()
    service.bridge.request.return_value = deepcopy(service.driver_update_apply_unattended.return_value)
    service.driver_update_apply_unattended = Backend.driver_update_apply_unattended.__get__(service)
    service.driver_updates_scan.return_value = scan([voice_device()], [voice_offer(), next_offer])

    result = run_drivers(service, snapshot)

    assert [row['status'] for row in result['items']] == ['failed', 'applied']
    assert result['items'][0]['result']['failureKind'] == 'driver_approval_changed'
    assert result['outcomeKnown'] is True and result['running'] is False
    service.bridge.request.assert_called_once()
    assert service.bridge.request.call_args.kwargs['id'] == next_offer['id']


@pytest.mark.skipif(os.name != 'nt', reason='Windows PowerShell fake-device mutation checkpoint.')
def test_native_class_approval_is_rechecked_after_download_with_fake_devices_only():
    executable = Path(os.environ['SystemRoot']) / 'System32/WindowsPowerShell/v1.0/powershell.exe'
    script = Path(__file__).with_name('Test-DriverClassCorrespondence.ps1')
    completed = subprocess.run([str(executable), '-NoLogo', '-NoProfile', '-NonInteractive',
        '-ExecutionPolicy', 'Bypass', '-File', str(script)], capture_output=True, text=True, timeout=45)
    assert completed.returncode == 0, completed.stdout + completed.stderr
    result = json.loads(completed.stdout)
    assert result['Result'] == 'PASS' and result['Assertions'] >= 24
    assert result['RealScans'] == result['RealDownloads'] == result['RealInstalls'] == 0
