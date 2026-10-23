"""Real WinGet table/parser/apply boundaries; every process is a fixture."""
from __future__ import annotations

import subprocess
from unittest.mock import Mock

import pytest

from optimus.automated_software import fresh_candidates, is_newer_version, run_software
from optimus.maintenance import Maintenance
from test_automated_software import configuration
from test_updates import table


@pytest.mark.parametrize('identity,installed,available', [
    ('Unity.Unity.6000', '6000.5.4f1', '6000.6.7f1'),
    ('Unity.Unity.6000', '6000.6.7f1', '6000.6.7f2'),
    ('unity.unity.2022', '2022.3.9f1', '2022.3.10f1'),
    ('Unity.UnityHub', '< 3.21.0.65535', '3.22.4.65535'),
    ('Unity.UnityHub', '<3.21.0.65535', '3.22.4.65535'),
    ('Vendor.Other', '< 1.9.99', '1.10.0'),
])
def test_official_vendor_order_requires_exact_source_and_preserves_literal_pair(identity, installed, available):
    assert is_newer_version(installed, available, package_id=identity, source='winget')
    assert not is_newer_version(installed, available)
    assert not is_newer_version(installed, available, package_id=identity, source='other')
    rows, structured, excluded = Maintenance._parse_update_table(
        table(('Fixture', identity, installed, available, 'winget')))
    assert structured and excluded == []
    assert rows[0]['version'] == installed and rows[0]['available'] == available
    assert fresh_candidates({'structured': True, 'exitCode': 0, 'source': 'winget', 'updates': rows}) == {identity: rows[0]}


@pytest.mark.parametrize('identity,installed,available', [
    ('Vendor.UnityLookalike', '6000.5.4f1', '6000.6.7f1'),
    ('Unity.Unity.6000', '6000.5.4f1', '7000.0.0f1'),
    ('Unity.Unity.2022', '6000.5.4f1', '6000.6.7f1'),
    ('Unity.Unity.6000', '6000.6.7f1', '6000.5.4f1'),
    ('Unity.Unity.6000', '6000.6.7f1', '6000.6.7f1'),
    ('Unity.Unity.6000', '6000.5.4f1', '6000.6.7b1'),
    ('Unity.Unity.6000', '6000.5.4b1', '6000.6.7f1'),
    ('Unity.Unity.6000', '6000.5.4f1', '6000.6.7p1'),
    ('Unity.Unity.6000', '6000.5.4f1', '6000.6.7x1'),
    ('Unity.Unity.6000', '6000.5.4f1', '6000.6.7f0'),
    ('Unity.Unity.6000', '6000.5.4f1', '6000.6.7f01'),
    ('Unity.Unity.6000', '6000.5.4f1', '6000.06.7f1'),
    ('Unity.Unity.6000', '6000.5.4', '6000.6.7'),
    ('Vendor.App', '< 3.21.0.65535', '3.21.0.65535'),
    ('Vendor.App', '< 3.21.0.65535', '3.21.0.65535.0'),
    ('Vendor.App', '< 3.21.0.65535', '3.20.9.65535'),
    ('Vendor.App', '< 3.21.0.65535', '3.22.0-beta1'),
    ('Vendor.App', '< 3.21.0.65535', '3.22.0+build1'),
    ('Vendor.App', '<=3.21.0.65535', '3.22.0'),
    ('Vendor.App', '> 3.21.0.65535', '3.22.0'),
    ('Vendor.App', '< Unknown', '3.22.0'),
    ('Vendor.App', '<  3.21.0', '3.22.0'),
    ('Vendor.App', '< 3.21.0...', '3.22.0'),
    ('Vendor.App', '< 3.21.0', '< 3.22.0'),
    ('Vendor.App', '< 3.21.0', '3.22.0;calc'),
])
def test_unsupported_vendor_or_range_versions_still_never_become_candidates(identity, installed, available):
    assert not is_newer_version(installed, available, package_id=identity, source='winget')
    rows, structured, excluded = Maintenance._parse_update_table(table(('Fixture', identity, installed, available, 'winget')))
    assert structured and not rows and excluded


@pytest.mark.parametrize('identity,installed,available', [
    ('Unity.Unity.6000', '6000.5.4f1', '6000.6.7f1'),
    ('Unity.UnityHub', '< 3.21.0.65535', '3.22.4.65535'),
])
def test_supported_vendor_update_revalidates_then_invokes_only_exact_silent_version(tmp_path, identity, installed, available):
    native = Maintenance(None, tmp_path)
    native._winget = Mock(return_value='fixture-winget.exe')
    listing = table(('Fixture', identity, installed, available, 'winget'))
    native._run = Mock(return_value=subprocess.CompletedProcess([], 0, listing, ''))
    native.updates_scan()
    native._run = Mock(side_effect=[subprocess.CompletedProcess([], 0, listing, ''),
        subprocess.CompletedProcess([], 0, 'There are no pins configured.', ''),
        subprocess.CompletedProcess([], 0, 'Fixture installed', '')])
    result = native.updates_apply(identity, confirmed=True, unattended=True)
    assert result['installer_started'] is True and result['outcomeKnown'] is True
    assert result['from_version'] == installed and result['to_version'] == available
    args = native._run.call_args.args[0]
    assert args == ['fixture-winget.exe', 'upgrade', '--id', identity, '--exact', '--source', 'winget',
        '--version', available, '--silent', '--disable-interactivity', '--accept-source-agreements', '--accept-package-agreements']


def test_expanded_version_support_does_not_override_exclusions_or_selected_scope():
    from test_automated_software import backend, package
    service = backend(package('Unity.Unity.6000', version='6000.5.4f1', available='6000.6.7f1'),
        package('Unity.UnityHub', version='< 3.21.0.65535', available='3.22.4.65535'))
    result = run_software(service, configuration(software_update_exclude_ids=['Unity.UnityHub']))
    service.update_app_unattended.assert_called_once_with('Unity.Unity.6000')
    assert result['excluded_ids'] == ['Unity.UnityHub']
    service.update_app_unattended.reset_mock()
    run_software(service, configuration(software_update_policy='selected', software_update_all_approved=False,
                                      software_update_ids=['Vendor.Other']))
    service.update_app_unattended.assert_not_called()
