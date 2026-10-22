"""Offline coverage fixtures; no host inventories, update scans or installs."""
from copy import deepcopy
from unittest.mock import Mock
import pytest

from optimus.driver_coverage import build_driver_coverage, driver_scan_evidence, is_trusted_support_url, support_links


def device(**changes):
    value = dict(deviceId=r'PCI\fixture-instance', displayName='Fixture GPU', deviceClass='Display',
                 manufacturer='NVIDIA', present=True, problemCode=0,
                 hardwareIds=[r'PCI\VEN_10DE&DEV_1234'], compatibleIds=[r'PCI\VEN_10DE'],
                 installedDrivers=[dict(version='1.2.3', date='2026-10-01', inf='oem1.inf', signed=True)])
    value.update(changes)
    return value


def offer(**changes):
    value = dict(id='00000000-0000-0000-0000-000000000001', revision=2,
                 title='Fixture GPU driver', hardwareId=r'PCI\VEN_10DE&DEV_1234')
    value.update(changes)
    return value


def report(devices=None, offers=None, **changes):
    inventory = dict(complete=True, devices=[device()] if devices is None else devices,
                     errors=[], system=dict(manufacturer='Dell Inc.', model='Fixture computer',
                                           baseboardManufacturer='ASUSTeK COMPUTER INC.'))
    scan = dict(resultCode=2, updates=[offer()] if offers is None else offers)
    inventory.update(changes.pop('inventory', {}))
    scan.update(changes.pop('scan', {}))
    return build_driver_coverage(inventory, scan)


def test_exact_hardware_match_and_metadata_are_preserved():
    result = report()
    assert result['summary']['offered'] == 1
    row = result['devices'][0]
    assert row['status'] == 'offered'
    assert row['installedDrivers'][0]['version'] == '1.2.3'
    assert row['updateOffers'][0]['revision'] == 2
    assert result['unmatchedUpdates'] == []


def test_compatible_case_insensitive_match_is_not_instance_identity():
    result = report(devices=[device(), device(deviceId='second physical device')],
                    offers=[offer(hardwareId=r'pci\ven_10de')])
    assert result['summary']['devices'] == 2
    assert result['summary']['offered'] == 2
    assert all(len(row['updateOffers']) == 1 for row in result['devices'])


def test_offer_only_joined_once_when_matching_multiple_ids():
    result = report(devices=[device(compatibleIds=[r'PCI\VEN_10DE&DEV_1234'])])
    assert len(result['devices'][0]['updateOffers']) == 1


def test_same_name_without_matching_ids_is_never_a_match():
    result = report(offers=[offer(title='Fixture GPU', hardwareId=r'PCI\VEN_OTHER')])
    assert result['devices'][0]['status'] == 'no_offer'
    assert result['summary']['unmatchedUpdates'] == 1


def test_no_offer_does_not_claim_current_or_missing_driver():
    result = report(offers=[], devices=[device(installedDrivers=[])])
    row = result['devices'][0]
    assert row['status'] == 'no_offer'
    assert 'newer vendor release may exist' in row['statusDescription']
    assert not row['missingDriver'] and row['driverMetadataMissing']
    assert result['summary']['missingDriver'] == 0


def test_scan_evidence_reports_successful_zero_only_with_valid_completed_search():
    scan = dict(resultCode=2, updates=[], online=True,
                criteria="IsInstalled=0 and Type='Driver' and IsHidden=0",
                started_utc='2026-10-22T22:00:00Z', completed_utc='2026-10-22T22:00:46Z',
                duration_seconds=46.125, returned_offer_count=0, errors=[], warnings=[])
    scan['coverage'] = report(offers=[])
    proof = driver_scan_evidence(scan, eligible_count=0)
    assert proof['complete'] and proof['status'] == 'completed'
    assert proof['offers_count'] == 0 and proof['eligible_count'] == 0
    assert proof['online'] and proof['duration_seconds'] == 46.125
    assert proof['started_utc'] == scan['started_utc'] and proof['completed_utc'] == scan['completed_utc']
    assert proof['inventory_complete'] and proof['devices_checked'] == 1
    assert 'No applicable drivers were offered' in proof['summary']
    assert 'OEM tools may offer other' in proof['summary']


@pytest.mark.parametrize('changes', [
    dict(resultCode=3), dict(resultCode=True), dict(resultCode=4), dict(updates=None),
    dict(updates=[None]), dict(updates=[offer(id='invalid')]), dict(updates=[offer(), offer()]),
    dict(error='Fixture denied'), dict(errors=[{'error': 'Fixture policy error'}]),
    dict(warnings=[{'message': 'Fixture partial source'}]), dict(online=False),
    dict(online='true'), dict(returned_offer_count=2), dict(criteria='IsInstalled=0'),
])
def test_scan_evidence_never_turns_malformed_partial_or_failed_into_zero_offers(changes):
    scan = dict(resultCode=2, updates=[])
    scan.update(changes)
    proof = driver_scan_evidence(scan)
    assert not proof['complete'] and proof['errors']
    assert 'No applicable drivers were offered' not in proof['summary']
    assert 'available updates could not be verified' in proof['summary']
    coverage = build_driver_coverage(dict(complete=True, devices=[device()]), scan)
    assert not coverage['updateScanComplete'] and coverage['summary']['noOffer'] == 0


def test_partial_driver_offers_retained_without_claiming_full_availability():
    result = report(scan={'resultCode': 3})
    assert result['devices'][0]['updateOffers']
    assert result['devices'][0]['status'] == 'scan_incomplete'
    assert not result['updateScanComplete']


def test_driver_scan_proof_bounds_error_and_text_payload():
    proof = driver_scan_evidence(dict(resultCode=4, updates=[], source='x' * 3000,
        errors=[{'source': 'y' * 3000, 'error': 'z' * 3000}] * 3000))
    assert len(proof['errors']) <= 20 and len(proof['source']) <= 200
    assert all(len(row['source']) <= 120 and len(row['error']) <= 500 for row in proof['errors'])


@pytest.mark.parametrize('code', [28, 22, 43, 52, 65])
def test_problem_codes_reported_independently_of_update_offers(code):
    result = report(devices=[device(problemCode=code)])
    row = result['devices'][0]
    assert row['hasProblem'] and row['status'] == 'device_problem'
    assert row['missingDriver'] == (code == 28)
    assert row['updateOffers']
    assert 'does not guarantee repair' in row['statusDescription']


def test_disconnected_filtered_but_unknown_presence_is_shown():
    result = report(devices=[device(present=False), device(present=None, deviceId='unknown presence')])
    assert result['summary']['devices'] == 1
    assert result['devices'][0]['present'] is None


@pytest.mark.parametrize('changes', [dict(inventory={'complete': False}),
                                   dict(scan={'resultCode': 3}),
                                   dict(scan={'resultCode': True}),
                                   dict(scan={'resultCode': None})])
def test_incomplete_scan_never_claims_no_update(changes):
    result = report(offers=[], **changes)
    assert result['devices'][0]['status'] == 'scan_incomplete'
    assert result['summary']['noOffer'] == 0


def test_null_problem_status_is_unknown_not_healthy():
    row = report(devices=[device(problemCode=None)])['devices'][0]
    assert row['problemCode'] is None
    assert row['problem'] == 'Device problem status is unavailable.'


def test_missing_offer_hardware_id_remains_unmatched():
    result = report(offers=[offer(hardwareId='')])
    assert result['summary']['unmatchedUpdates'] == 1
    assert not result['devices'][0]['updateOffers']


def test_malformed_provider_data_tolerated_without_execution():
    result = build_driver_coverage(dict(complete=True, devices=[None, device(hardwareIds=[None, 3, '*'])]),
                                   dict(resultCode=2, updates=[False, offer(hardwareId='PCI\\VEN_10DE')]))
    assert result['summary']['devices'] == 1
    assert result['summary']['offered'] == 1  # exact compatible ID fixture
    assert result['devices'][0]['hardwareIds'] == ['*']


def test_input_records_not_mutated():
    inventory = dict(complete=True, devices=[device()], errors=[])
    scan = dict(resultCode=2, updates=[offer()], error='Fixture unavailable')
    before = deepcopy((inventory, scan))
    build_driver_coverage(inventory, scan)
    assert (inventory, scan) == before


def test_fixed_official_routes_for_vendor_and_pc_motherboard():
    result = report()
    assert all(is_trusted_support_url(row['url']) for row in result['systemSupport'])
    assert {row['label'] for row in result['systemSupport']} == {'Dell drivers and downloads', 'ASUS download center'}
    assert report()['devices'][0]['supportLinks'] == support_links('nvidia')
    assert support_links('Generic provider', [r'PCI\VEN_8086&DEV_1234'])[0]['label'] == 'Intel driver downloads'
    assert support_links('my-nvidia-malware https://evil.invalid')[0]['url'] == 'https://www.nvidia.com/en-us/drivers/'


@pytest.mark.parametrize('url', ['http://www.nvidia.com/en-us/drivers/',
    'https://www.nvidia.com.evil.invalid/en-us/drivers/',
    'https://user:password@www.nvidia.com/en-us/drivers/',
    'https://www.nvidia.com/en-us/drivers/?next=https://evil.invalid',
    'file:///C:/Windows/System32/cmd.exe', 'ms-settings:windowsupdate', None])
def test_arbitrary_or_modified_support_urls_rejected(url):
    assert not is_trusted_support_url(url)


def test_unknown_maker_keeps_support_route_empty():
    assert support_links('Unknown hardware maker') == []
    assert support_links('NotIntel') == []


def backend_fixture(request):
    from optimus.backend import Backend
    backend = Backend.__new__(Backend)
    backend.bridge = Mock(request=Mock(side_effect=request))
    return backend


def test_backend_scan_adds_coverage_and_does_not_duplicate_wua_scan():
    calls = []
    def request(operation, **kwargs):
        calls.append(operation)
        if operation == 'driver_updates_scan':
            return dict(resultCode=2, updates=[offer()])
        if operation == 'driver_device_inventory':
            return dict(complete=True, devices=[device()])
        raise AssertionError('Unexpected mutation or operation')
    result = backend_fixture(request).driver_updates_scan()
    assert calls == ['driver_updates_scan', 'driver_device_inventory']
    assert result['coverage']['summary']['offered'] == 1
    assert len(result['updates']) == 1


def test_backend_can_report_devices_when_update_service_fails():
    def request(operation, **kwargs):
        if operation == 'driver_updates_scan':
            raise RuntimeError('Fixture Windows Update unavailable')
        return dict(complete=True, devices=[device()])
    result = backend_fixture(request).driver_updates_scan()
    assert result['resultCode'] == 4
    assert result['coverage']['summary']['devices'] == 1
    assert result['coverage']['devices'][0]['status'] == 'scan_incomplete'
    assert result['coverage']['inventoryErrors'][0]['source'] == 'Windows Update'


def test_backend_inventory_failure_does_not_discard_applicable_offers():
    def request(operation, **kwargs):
        if operation == 'driver_updates_scan':
            return dict(resultCode=2, updates=[offer()])
        raise RuntimeError('Fixture CIM denied')
    result = backend_fixture(request).driver_updates_scan()
    assert len(result['updates']) == 1
    assert not result['coverage']['inventoryComplete']
    assert result['coverage']['summary']['unmatchedUpdates'] == 1


def test_backend_direct_coverage_reuses_supplied_scan():
    calls = []
    def request(operation, **kwargs):
        calls.append(operation)
        return dict(complete=True, devices=[device()])
    result = backend_fixture(request).driver_coverage(dict(resultCode=2, updates=[offer()]))
    assert calls == ['driver_device_inventory']
    assert result['updateScanComplete']


def installed_driver(**changes):
    value = dict(provider='NVIDIA', deviceClass='DISPLAY', date='2026-10-01T00:00:00Z',
                 version='1.2.3', inf='oem1.inf', signed=True)
    value.update(changes)
    return value


def test_no_current_offer_still_has_present_hardware_future_approval_rules():
    result = report(offers=[], devices=[device(installedDrivers=[installed_driver()])])
    row = result['devices'][0]
    assert row['status'] == 'no_offer'
    assert {rule['hardwareId'] for rule in row['approvalRules']} == {
        r'PCI\VEN_10DE&DEV_1234', r'PCI\VEN_10DE'}
    assert all(rule['provider'] == 'NVIDIA' and rule['class'] == 'DISPLAY'
               and rule['notOlderThanDriverDate'] == '2026-10-01' for rule in row['approvalRules'])
    assert row['approvalUnavailableReason'] == ''


def test_installed_rule_provider_class_and_date_are_never_guessed():
    for changes in [dict(provider=''), dict(deviceClass=''), dict(date=''), dict(date='nonsense')]:
        row = report(devices=[device(installedDrivers=[installed_driver(**changes)])])['devices'][0]
        assert not row['approvalRules']
        assert 'known installed driver provider, class and ISO date' in row['approvalUnavailableReason']


def test_complete_inventory_required_for_new_hardware_rules():
    row = report(devices=[device(installedDrivers=[installed_driver()])],
                 inventory={'complete': False})['devices'][0]
    assert row['approvalRules'] == []
    assert 'complete' in row['approvalUnavailableReason']


def test_hardware_rule_duplicate_identity_keeps_newest_installed_date():
    row = report(devices=[device(installedDrivers=[
        installed_driver(), installed_driver(date='2026-10-20'), installed_driver(provider='nvidia')])])['devices'][0]
    assert len(row['approvalRules']) == 2
    assert {rule['notOlderThanDriverDate'] for rule in row['approvalRules']} == {'2026-10-20'}


def test_malformed_or_wildcard_ids_cannot_create_hardware_approvals():
    row = report(devices=[device(hardwareIds=['PCI\\VEN_*', 'exact\r\nmultiline'],
        compatibleIds=['?'], installedDrivers=[installed_driver()])])['devices'][0]
    assert row['approvalRules'] == []


def test_problem_without_installed_metadata_has_no_invented_future_rules():
    row = report(devices=[device(problemCode=28, installedDrivers=[])])['devices'][0]
    assert row['missingDriver'] and row['approvalRules'] == []


def test_backend_setup_inventory_never_starts_online_scan():
    calls = []
    def request(operation, **kwargs):
        calls.append(operation)
        if operation != 'driver_device_inventory':
            raise AssertionError('Setup device inventory must not start online scans')
        return dict(complete=True, devices=[device(installedDrivers=[installed_driver()])])
    result = backend_fixture(request).driver_devices()
    assert calls == ['driver_device_inventory']
    assert result['devices'][0]['approvalRules']
    assert not result['updateScanComplete']


def test_device_approval_capacity_covers_large_present_inventories():
    from optimus.automated_system import validate_driver_devices, validate_driver_approvals
    rules = [dict(hardwareId='PCI\\VEN_1234&DEV_'+str(index), provider='Fixture', **{
        'class':'NET', 'notOlderThanDriverDate':'2026-10-01'}) for index in range(2048)]
    assert len(validate_driver_devices(rules)) == 2048
    with pytest.raises(ValueError, match='2048'):
        validate_driver_devices(rules + [dict(rules[0], hardwareId='extra')])
    # The broader inventory rule limit must not expand exact-update approval count.
    with pytest.raises(ValueError, match='100'):
        validate_driver_approvals([offer()] * 101)


def test_hardware_approval_byte_limit_still_applies_with_large_count_allowance():
    from optimus.automated_system import validate_driver_devices
    rules = [dict(hardwareId='PCI\\'+str(index), provider='F' * 4096, **{
        'class':'NET', 'notOlderThanDriverDate':'2026-10-01'}) for index in range(300)]
    with pytest.raises(ValueError, match='saved approval limit'):
        validate_driver_devices(rules)

