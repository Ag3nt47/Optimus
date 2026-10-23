"""Owner-scoped catalog fixtures; no real source refresh, download or install."""
from __future__ import annotations

import copy
import json
import os
import subprocess
import threading
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest

from optimus.automated_software import run_software
from optimus.update_hold_rechecks import HeldUpdateRechecks, _SOURCE, _native_command
from optimus.update_holds import matching_update_hold, load_update_holds
from test_automated_software import backend, configuration, package
from test_update_holds import OWNER, hold_file


@pytest.fixture(autouse=True)
def owner():
    with patch('optimus.speed_enhancements._current_owner', return_value=OWNER), \
         patch('optimus.update_hold_rechecks._current_owner', return_value=OWNER):
        yield


def source(**changes):
    value = dict(_SOURCE, TrustLevel=['Trusted', 'StoreOrigin'], Explicit=False)
    value.update(changes)
    return json.dumps(value)


def details(identity='Vendor.One', version='2.0', digest='a' * 64, url='https://vendor.example/installer.exe'):
    return f'Found Fixture [{identity}]\nVersion: {version}\nInstaller:\n  Installer Url: {url}\n  Installer SHA256: {digest}\n'


def completed(text='', code=0, errors=''):
    return subprocess.CompletedProcess([], code, text, errors)


def checker(tmp_path, runner=None, clock=None, **kwargs):
    maintenance = SimpleNamespace(data_dir=tmp_path, _winget=Mock(return_value='fixture-winget.exe'))
    native = runner or Mock(side_effect=[completed(source()), completed(), completed(source()), completed(details())])
    return HeldUpdateRechecks(maintenance, owner=OWNER, runner=native,
                             clock=clock or Mock(return_value=100000), **kwargs), native


def test_constructor_and_no_held_packages_have_no_native_or_file_actions(tmp_path):
    check, native = checker(tmp_path)
    assert list(tmp_path.iterdir()) == []
    result = check.run([package()])
    native.assert_not_called()
    assert result['authorization_granted'] is False and result['items'] == []
    assert list(tmp_path.iterdir()) == []


def test_exact_held_offer_gets_verified_existing_source_and_exact_metadata_without_authorization(tmp_path):
    anchor = hold_file(tmp_path)
    original = anchor.read_bytes()
    check, native = checker(tmp_path)
    events = []
    result = check.run([package()], item_callback=events.append)
    assert anchor.read_bytes() == original
    row = result['items'][0]
    assert result['needs_fresh_scan'] and result['source_verified_after_refresh'] and result['source_refreshed']
    assert row['recheck_status'] == 'metadata_checked' and row['artifact_verified'] is False
    assert row['installer_sha256'] == 'a' * 64 and len(row['metadata_sha256']) == 64
    assert row['metadata_changed'] is None and row['authorization_granted'] is False
    assert row['note'] == 'Publisher replacement awaited; automatic checks continue.'
    args = [call.args[1] for call in native.call_args_list]
    assert args == [['source', 'export', '--name', 'winget', '--disable-interactivity'],
        ['source', 'update', '--name', 'winget', '--disable-interactivity'],
        ['source', 'export', '--name', 'winget', '--disable-interactivity'],
        ['show', '--id', 'Vendor.One', '--exact', '--source', 'winget', '--version', '2.0', '--disable-interactivity']]
    assert all(event['phase'] == 'metadata_recheck' and 'current' not in event and 'total' not in event for event in events)
    assert matching_update_hold(package(), load_update_holds(tmp_path, owner=OWNER))


def test_cooldown_is_durable_and_changed_catalog_hash_never_clears_hold(tmp_path):
    anchor = hold_file(tmp_path)
    original = anchor.read_bytes()
    check, native = checker(tmp_path)
    check.run([package()])
    second, other = checker(tmp_path, clock=Mock(return_value=100001))
    assert second.run([package()])['items'][0]['recheck_status'] == 'cooldown'
    other.assert_not_called()
    runner = Mock(side_effect=[completed(source()), completed(), completed(source()), completed(details(digest='b' * 64))])
    third, _ = checker(tmp_path, runner=runner, clock=Mock(return_value=121601))
    result = third.run([package()])
    assert result['items'][0]['metadata_changed'] is True
    assert result['items'][0]['artifact_verified'] is False and result['authorization_granted'] is False
    assert anchor.read_bytes() == original


def test_equivalent_release_audit_binds_both_current_and_original_held_spelling(tmp_path):
    anchor = hold_file(tmp_path)
    original = anchor.read_bytes()
    native = Mock(side_effect=[completed(source()), completed(), completed(source()), completed(details(version='v2.0.0+build2'))])
    check, _ = checker(tmp_path, runner=native)
    result = check.run([package(available='v2.0.0+build2')])
    row = result['items'][0]
    assert row['available_version'] == 'v2.0.0+build2' and row['held_available_version'] == '2.0'
    assert row['authorization_granted'] is False and row['artifact_verified'] is False
    assert anchor.read_bytes() == original


@pytest.mark.parametrize('change', [
    {'Arg': 'https://untrusted.example/cache'}, {'Name': 'other'}, {'Type': 'Microsoft.Rest'},
    {'Identifier': 'Other.Source'}, {'Data': 'Other.Source'},
    {'TrustLevel': []}, {'TrustLevel': 'Trusted'}, {'TrustLevel': ['Trusted', 'Trusted']},
])
def test_unverified_existing_source_is_never_refreshed(tmp_path, change):
    hold_file(tmp_path)
    runner = Mock(return_value=completed(source(**change)))
    check, _ = checker(tmp_path, runner=runner)
    result = check.run([package()])
    assert runner.call_count == 1 and result['refresh_attempted'] is False
    assert result['needs_fresh_scan'] and not result['source_verified_after_refresh']
    assert json.loads(check.path.read_text())['catalog_uncertain'] is True
    assert result['items'][0]['recheck_status'] == 'source_unavailable'


def test_unknown_source_after_refresh_persists_barrier_even_within_cooldown_and_no_current_hold(tmp_path):
    hold_file(tmp_path)
    native = Mock(side_effect=[completed(source()), completed(code=5), completed(code=5)])
    check, _ = checker(tmp_path, runner=native)
    first = check.run([package()])
    assert first['needs_fresh_scan'] and not first['source_verified_after_refresh']
    saved = json.loads(check.path.read_text())
    assert saved['catalog_uncertain'] is True
    retry_native = Mock(return_value=completed(source()))
    second, _ = checker(tmp_path, runner=retry_native, clock=Mock(return_value=100001))
    result = second.run([])
    assert result['needs_fresh_scan'] and result['source_verified_after_refresh']
    assert retry_native.call_count == 1 and retry_native.call_args.args[1][0:2] == ['source', 'export']
    assert json.loads(second.path.read_text())['catalog_uncertain'] is False


def test_refresh_failure_requires_verified_source_and_fresh_scan_even_when_export_recovers(tmp_path):
    hold_file(tmp_path)
    native = Mock(side_effect=[completed(source()), completed(code=5), completed(source())])
    check, _ = checker(tmp_path, runner=native)
    result = check.run([package()])
    assert result['needs_fresh_scan'] and result['source_verified_after_refresh']
    assert not result['source_refreshed'] and not result['authorization_granted']
    assert result['items'][0]['recheck_status'] == 'source_unavailable'


@pytest.mark.parametrize('text', [details(identity='Other.Package'), details(version='3.0'),
    details(digest='bad'), details(url='http://vendor.example/a.exe'),
    details(url='https://user:password@vendor.example/a.exe'),
    details() + 'Installer SHA256: ' + 'a' * 64,
    'Unrecognized localized output'])
def test_bad_or_ambiguous_exact_metadata_does_not_authorize_or_retry_payload(tmp_path, text):
    hold_file(tmp_path)
    native = Mock(side_effect=[completed(source()), completed(), completed(source()), completed(text)])
    check, _ = checker(tmp_path, runner=native)
    row = check.run([package()])['items'][0]
    assert row['recheck_status'] == 'metadata_unavailable' and row['installer_started'] is False
    assert row['authorization_granted'] is False


def test_cancel_during_refresh_does_not_run_show_and_retains_shared_uncertainty(tmp_path):
    hold_file(tmp_path)
    cancel = threading.Event()
    def native(executable, arguments, **kwargs):
        if arguments[0:2] == ['source', 'update']:
            cancel.set()
            return completed(code=130)
        assert arguments[0:2] == ['source', 'export']
        return completed(source())
    check, _ = checker(tmp_path, runner=Mock(side_effect=native))
    result = check.run([package()], cancel_event=cancel)
    assert result['needs_fresh_scan'] and not result['source_verified_after_refresh']
    assert json.loads(check.path.read_text())['catalog_uncertain'] is True


def test_total_time_budget_prevents_starting_more_catalog_commands(tmp_path):
    hold_file(tmp_path)
    ticks = iter([0, 0, 46, 47])
    native = Mock(return_value=completed(source()))
    check, _ = checker(tmp_path, runner=native, monotonic=lambda: next(ticks))
    result = check.run([package()])
    assert native.call_count == 1 and native.call_args.args[1][0:2] == ['source', 'export']
    assert result['needs_fresh_scan'] and not result['source_verified_after_refresh']
    assert result['items'][0]['installer_started'] is False


def test_native_cancellation_terminates_and_drains_only_owned_catalog_child():
    cancel = threading.Event()
    child = Mock(returncode=0)
    def communicate(**kwargs):
        if not child.terminate.called:
            cancel.set()
            raise subprocess.TimeoutExpired('fixture-metadata', 0.1)
        return 'Fixture metadata', ''
    child.communicate.side_effect = communicate
    with patch('optimus.update_hold_rechecks.subprocess.Popen', return_value=child) as launch:
        result = _native_command('fixture-winget.exe', ['source', 'export', '--name', 'winget', '--disable-interactivity'],
                                 timeout=10, cancel_event=cancel)
    assert launch.call_args.args[0] == ['fixture-winget.exe', 'source', 'export', '--name', 'winget', '--disable-interactivity']
    assert launch.call_args.kwargs['shell'] is False and result.returncode == 130
    child.terminate.assert_called_once()
    child.kill.assert_not_called()
    assert child.communicate.call_count == 2


def test_attempt_is_durable_before_native_refresh_and_five_offer_limit_is_fair(tmp_path):
    rows = [{'id': f'Vendor.App{n}', 'available_version': '2.0', 'reason': 'Fixture hold'} for n in range(8)]
    hold_file(tmp_path, value={'schema': 1, 'owner': OWNER, 'holds': rows})
    packages = [package(row['id']) for row in rows]
    check = None
    def native(executable, arguments, **kwargs):
        saved = json.loads(check.path.read_text())
        assert len(saved['offers']) >= 3
        if arguments[0] == 'show':
            return completed(details(identity=arguments[arguments.index('--id') + 1]))
        if arguments[0:2] == ['source', 'update']:
            assert saved['catalog_uncertain'] is True
            return completed()
        return completed(source())
    runner = Mock(side_effect=native)
    check, _ = checker(tmp_path, runner=runner)
    first = check.run(packages)
    assert sum(row['recheck_status'] == 'metadata_checked' for row in first['items']) == 5
    assert sum(row['recheck_status'] == 'per_run_limit' for row in first['items']) == 3
    runner.reset_mock()
    check.clock = Mock(return_value=100001)
    second = check.run(packages)
    assert sum(row['recheck_status'] == 'metadata_checked' for row in second['items']) == 3
    assert sum(call.args[1][0] == 'show' for call in runner.call_args_list) == 3


@pytest.mark.parametrize('raw', [b'{}', b' '*131073,
    b'{"schema":1,"schema":1,"owner":"bad","offers":{},"catalog_uncertain":false}'],
    ids=['empty-object', 'oversized', 'duplicate-key'])
def test_invalid_private_receipt_fails_closed_without_network(tmp_path, raw):
    hold_file(tmp_path)
    path = tmp_path / 'update-hold-rechecks.json'
    path.write_bytes(raw)
    path.chmod(0o600)
    check, native = checker(tmp_path)
    result = check.run([package()])
    assert result['needs_fresh_scan'] and not result['source_verified_after_refresh']
    native.assert_not_called()


def test_hardlinked_private_receipt_fails_closed(tmp_path):
    hold_file(tmp_path)
    check, native = checker(tmp_path)
    check.run([package()])
    try:
        os.link(check.path, tmp_path / 'linked.json')
    except OSError:
        pytest.skip('Hardlinks unavailable on this fixture filesystem.')
    native.reset_mock()
    result = check.run([package()])
    assert result['needs_fresh_scan'] and not result['source_verified_after_refresh']
    native.assert_not_called()


def scan_result(*rows):
    return {'structured': True, 'exitCode': 0, 'source': 'winget', 'updates': list(rows)}


@pytest.mark.parametrize('metadata_status', ['metadata_checked', 'metadata_unavailable'])
def test_metadata_only_never_releases_same_offer_and_other_app_continues(tmp_path, metadata_status):
    anchor = hold_file(tmp_path)
    original = anchor.read_bytes()
    service = backend(package(), package('Vendor.Two'))
    service.data_dir = tmp_path
    service.recheck_held_updates = Mock(return_value={'needs_fresh_scan': True,
        'source_verified_after_refresh': True, 'source_refreshed': True, 'items': [
        {'id': 'Vendor.One', 'available_version': '2.0', 'recheck_status': metadata_status,
         'metadata_changed': True, 'artifact_verified': False, 'authorization_granted': False}]})
    result = run_software(service, configuration())
    assert service.updates_scan.call_count == 2
    service.update_app_unattended.assert_called_once_with('Vendor.Two')
    assert result['items'][0]['result']['automatic_recheck']['metadata_changed'] is True
    assert 'Publisher replacement awaited' in result['items'][0]['result']['note']
    assert result['held_ids'] == ['Vendor.One'] and anchor.read_bytes() == original


@pytest.mark.parametrize('fault', ['identity', 'scan_failed', 'scan_exception'])
def test_shared_catalog_uncertainty_defers_all_app_installers_without_taking_gate(tmp_path, fault):
    hold_file(tmp_path)
    service = backend(package(), package('Vendor.Two'))
    service.data_dir = tmp_path
    service.recheck_held_updates = Mock(return_value={'needs_fresh_scan': True,
        'source_verified_after_refresh': fault != 'identity'})
    if fault != 'identity':
        service.updates_scan.side_effect = [scan_result(package(), package('Vendor.Two')),
            ValueError('Fixture source failure') if fault == 'scan_exception' else {'structured': False, 'exitCode': 5}]
    gate = Mock()
    result = run_software(service, configuration(), before_install=gate)
    assert result['installation_blocked'] and result['installer_started'] is False and result['outcomeKnown'] is True
    assert result['deferred_ids'] == ['Vendor.Two'] and result['running'] is False
    gate.assert_not_called()
    service.update_app_unattended.assert_not_called()


def test_observed_bad_source_blocks_unheld_app_through_cooldown_until_fresh_trust_and_scan(tmp_path):
    hold_file(tmp_path)
    native = Mock(return_value=completed(source(Arg='https://untrusted.example/cache')))
    check, _ = checker(tmp_path, runner=native)
    service = backend(package(), package('Vendor.Two'))
    service.data_dir = tmp_path
    service.recheck_held_updates = check.run
    gate = Mock()
    first = run_software(service, configuration(), before_install=gate)
    assert first['installation_blocked'] and first['installer_started'] is False
    gate.assert_not_called()
    service.update_app_unattended.assert_not_called()
    assert native.call_count == 1
    # The next approved run is still within the hold's cooldown. It performs
    # only trust recovery, then a new scan; it cannot use the failed old check.
    native.reset_mock()
    native.return_value = completed(source())
    check.clock = Mock(return_value=100001)
    second = run_software(service, configuration())
    assert native.call_count == 1 and native.call_args.args[1][0:2] == ['source', 'export']
    assert service.updates_scan.call_count == 3  # First scan, then new run + post-trust fresh scan.
    service.update_app_unattended.assert_called_once_with('Vendor.Two')
    assert second['held_ids'] == ['Vendor.One']


def test_new_available_version_after_verified_refresh_can_proceed_under_original_saved_scope(tmp_path):
    anchor = hold_file(tmp_path)
    original = anchor.read_bytes()
    service = backend(package())
    service.data_dir = tmp_path
    service.recheck_held_updates = Mock(return_value={'needs_fresh_scan': True, 'source_verified_after_refresh': True})
    service.updates_scan.side_effect = [scan_result(package()), scan_result(package(available='3.0'), package('Vendor.Unselected'))]
    result = run_software(service, configuration(software_update_policy='selected', software_update_ids=['Vendor.One'],
                                             software_update_all_approved=False))
    service.update_app_unattended.assert_called_once_with('Vendor.One')
    assert result['items'][0]['to_version'] == '3.0' and result['held_count'] == 0
    assert anchor.read_bytes() == original


@pytest.mark.parametrize('settings', [configuration(software_updates_enabled=False),
    configuration(software_update_policy='review'), configuration(software_update_agreements_accepted=False)])
def test_metadata_recheck_cannot_run_before_master_policy_and_agreements(tmp_path, settings):
    hold_file(tmp_path)
    service = backend(package())
    service.data_dir = tmp_path
    service.recheck_held_updates = Mock()
    try:
        run_software(service, settings)
    except ValueError:
        pass
    service.recheck_held_updates.assert_not_called()
    service.update_app_unattended.assert_not_called()
