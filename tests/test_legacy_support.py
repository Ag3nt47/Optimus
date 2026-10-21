from optimus.legacy_support import review_legacy_support


def test_sources_are_review_only_and_do_not_assert_patch_availability():
    report = review_legacy_support()
    assert report['review_only'] and report['status'] == 'partial'
    assert report['failed_items'] > 0
    assert not report['device_inventory_complete']
    assert report['possible_product_matches'] == []
    assert all(provider['url'].startswith('https://') for provider in report['providers'])
    assert 'paid' in next(provider for provider in report['providers'] if provider['id'] == '0patch')['cost']
    assert 'universal' in report['note']


def test_windows_build_does_not_declare_esu_ltsc_system_unsupported():
    report = review_legacy_support({'build': 19045, 'edition': 'EnterpriseS'})
    match = report['possible_product_matches'][0]
    assert match['status'] == 'edition_and_esu_not_verified'
    assert 'LTSC/ESU' in match['note']
    assert not any(row.get('status') == 'outdated' for row in report['possible_product_matches'])


def test_office_family_match_requires_publisher_and_never_grants_installation():
    apps = [{'Name': 'Microsoft Office 2016', 'Publisher': 'Microsoft Corporation', 'Version': '16.0'},
            {'Name': 'Microsoft Office 2019', 'Publisher': 'Unknown'},
            {'Name': 'Office helper 2013', 'Publisher': 'Unrelated'},
            {'Name': 'Microsoft Office 2024', 'Publisher': 'Microsoft Corporation'}]
    report = review_legacy_support(applications=apps)
    assert len(report['possible_product_matches']) == 1
    assert report['possible_product_matches'][0]['status'] == 'compatibility_not_verified'
    assert 'installed' not in report


def test_device_routes_ignore_untrusted_download_urls():
    report = review_legacy_support(devices={'complete': True, 'devices': [
        {'name': 'Fixture GPU', 'manufacturer': 'NVIDIA', 'hardwareIds': [r'PCI\VEN_10DE&DEV_0000'],
         'url': 'https://untrusted.invalid/driver.exe'}]})
    assert report['device_inventory_complete']
    assert report['official_hardware_routes'] == [{'label': 'NVIDIA driver downloads', 'url': 'https://www.nvidia.com/en-us/drivers/'}]
    assert 'untrusted.invalid' not in str(report)
    assert report['device_review'][0]['status'] == 'vendor_catalog_not_queried'


def test_native_device_names_and_partial_cim_failures_are_preserved_honestly():
    report = review_legacy_support(devices={'complete': False, 'devices': [
        {'displayName': 'Actual GPU name', 'manufacturer': 'NVIDIA', 'hardwareIds': []}],
        'errors': [{'source': 'Win32_PnPSignedDriver', 'error': 'Fixture denied private path'}]})
    assert report['device_review'][0]['device'] == 'Actual GPU name'
    assert report['status'] == 'partial' and report['failed_items'] > 0
    assert not report['device_inventory_complete']
    assert any('Win32_PnPSignedDriver' in item for item in report['errors'])
    assert 'private path' not in str(report)


def test_other_cim_errors_do_not_claim_complete_support_inventory():
    report = review_legacy_support(devices={'complete': True, 'devices': [],
        'errors': [{'source': 'Win32_BaseBoard', 'error': 'Fixture denied'}]})
    assert report['status'] == 'partial' and report['failed_items'] == 1
    assert not report['device_inventory_complete']
