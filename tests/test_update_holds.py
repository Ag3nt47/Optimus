"""Exact-offer holds exercise fake metadata/installers and private fixtures."""
import copy
import json
import os
import subprocess
from unittest.mock import Mock, patch

import pytest

from optimus.automated_software import run_software
from optimus.maintenance import Maintenance
from optimus.parallel_updates import installer_barrier, _uncertain, WINDOWS_SOFTWARE, WINDOWS_DRIVERS
from optimus.update_holds import UpdateHoldError, load_update_holds, held_update_result
from optimus.workflow import Workflow
from test_updates import table, scan
from test_automated_software import package, configuration, backend
from test_native_parallel_updates import Backend, driver_settings, enable_fixture_driver

OWNER = 'sid:S-1-5-21-1001'
REASON = 'Catalog version 2.0 resolves to verified older payload 1.0; failed attempt reviewed.'


@pytest.fixture(autouse=True)
def owner():
    with patch('optimus.speed_enhancements._current_owner', return_value=OWNER):
        yield


def hold_file(directory, *, identity='Vendor.One', version='2.0', value=None):
    path = directory / 'update-holds.json'
    data = value if value is not None else {'schema': 1, 'owner': OWNER, 'holds': [
        {'id': identity, 'available_version': version, 'reason': REASON}]}
    with path.open('w', encoding='utf-8') as stream:
        json.dump(data, stream)
    path.chmod(0o600)
    return path


def updater(directory, *, available='2.0'):
    service = Maintenance(None, directory)
    service._winget = Mock(return_value='winget-fixture.exe')
    scan(service, table(('Fixture App', 'Vendor.One', '1.0', available, 'winget')))
    return service


def test_missing_hold_file_has_no_effect_and_does_not_create_it(tmp_path):
    assert load_update_holds(tmp_path) == []
    assert not (tmp_path / 'update-holds.json').exists()


def test_exact_case_insensitive_offer_is_held_before_network_pin_or_installer(tmp_path):
    hold_file(tmp_path, identity='vendor.one')
    service = updater(tmp_path)
    service._run.reset_mock()
    result = service.updates_apply('Vendor.One', confirmed=True, unattended=True)
    assert result['failureKind'] == 'automatic_update_hold' and result['requiresReview'] is True
    assert result['installer_started'] is False and result['outcomeKnown'] is True and result['running'] is False
    assert REASON in result['note'] and result['to_version'] == '2.0'
    service._run.assert_not_called()


@pytest.mark.parametrize('manual,available', [(False, '3.0'), (True, '2.0')])
def test_future_offer_and_explicit_manual_route_are_not_held(tmp_path, manual, available):
    hold_file(tmp_path)
    service = updater(tmp_path, available=available)
    service._run = Mock(side_effect=[
        subprocess.CompletedProcess([], 0, table(('Fixture App', 'Vendor.One', '1.0', available, 'winget')), ''),
        subprocess.CompletedProcess([], 0, 'There are no pins configured.', ''),
        subprocess.CompletedProcess([], 0, 'Fixture installed', '')])
    result = service.updates_apply('Vendor.One', confirmed=True, unattended=not manual)
    assert result['installer_started'] is True and result['status'] == 'completed'
    assert service._run.call_count == 3
    assert '--version' in service._run.call_args.args[0]
    assert service._run.call_args.args[0][service._run.call_args.args[0].index('--version')+1] == available


def test_hold_added_during_pin_check_is_enforced_at_final_launch_boundary(tmp_path):
    service = updater(tmp_path)
    def native(arguments, **kwargs):
        if 'pin' in arguments:
            hold_file(tmp_path)
            return subprocess.CompletedProcess([], 0, 'There are no pins configured.', '')
        assert '--id' not in arguments, 'A late hold must prevent installer launch.'
        return subprocess.CompletedProcess([], 0, table(('Fixture App', 'Vendor.One', '1.0', '2.0', 'winget')), '')
    service._run = Mock(side_effect=native)
    result = service.updates_apply('Vendor.One', confirmed=True, unattended=True)
    assert result['failureKind'] == 'automatic_update_hold' and service._run.call_count == 2


@pytest.mark.parametrize('change', [
    {'schema': True}, {'schema': 2}, {'owner': 'sid:S-1-5-21-9999'}, {'unknown': True},
    {'holds': 'Vendor.One'}, {'holds': [{'id': '--all', 'available_version': '2.0', 'reason': REASON}]},
    {'holds': [{'id': 'Vendor.One', 'available_version': '2.0;calc', 'reason': REASON}]},
    {'holds': [{'id': 'Vendor.One', 'available_version': '2.0', 'reason': '\nunsafe'}]},
    {'holds': [{'id': 'Vendor.One', 'available_version': '2.0', 'reason': REASON}]*2},
    {'holds': [{'id': 'Vendor.One', 'available_version': '2.0', 'reason': REASON}]*101},
])
def test_malformed_or_foreign_file_fails_closed_before_any_updater_call(tmp_path, change):
    value = {'schema': 1, 'owner': OWNER, 'holds': []}
    value.update(copy.deepcopy(change))
    hold_file(tmp_path, value=value)
    service = updater(tmp_path)
    service._run.reset_mock()
    with pytest.raises(UpdateHoldError) as raised:
        service.updates_apply('Vendor.One', confirmed=True, unattended=True)
    assert raised.value.installer_started is False and raised.value.outcomeKnown is True
    service._run.assert_not_called()


@pytest.mark.parametrize('raw', ['{"schema":1,"schema":1,"owner":"'+OWNER+'","holds":[]}',
                                '{"schema":1,"owner":"'+OWNER+'","holds":NaN}', '['*5000, ' '*65537],
                         ids=['duplicate-key', 'nonfinite', 'deep-json', 'oversized'])
def test_duplicate_nonfinite_deep_and_oversized_files_are_rejected(tmp_path, raw):
    path = tmp_path / 'update-holds.json'
    path.write_text(raw, encoding='utf-8')
    path.chmod(0o600)
    with pytest.raises(UpdateHoldError):
        load_update_holds(tmp_path)


def test_hardlinked_hold_file_is_rejected(tmp_path):
    path = hold_file(tmp_path)
    try:
        os.link(path, tmp_path / 'other.json')
    except OSError:
        pytest.skip('This filesystem cannot create the hardlink fixture.')
    with pytest.raises(UpdateHoldError):
        load_update_holds(tmp_path)


def test_prefiltered_hold_leaves_other_approved_apps_and_correct_gate_counts(tmp_path):
    hold_file(tmp_path)
    service = backend(package(), package('Vendor.Two'))
    service.data_dir = tmp_path
    gate, progress, prepared = Mock(return_value=True), [], []
    result = run_software(service, configuration(), before_install=gate,
                          prepared_callback=prepared.append, item_callback=progress.append)
    service.update_app_unattended.assert_called_once_with('Vendor.Two')
    assert gate.call_args.args[0]['candidate_count'] == prepared[0]['candidate_count'] == 1
    assert all(row['id'] == 'Vendor.Two' and row['total'] == 1 for row in progress)
    assert result['held_ids'] == ['Vendor.One'] and result['held_count'] == 1
    assert REASON in result['automatic_update_holds'][0]['reason'] and REASON in result['note']
    assert [row['status'] for row in result['items']] == ['skipped', 'completed']


def test_only_held_offers_never_request_installer_gate(tmp_path):
    hold_file(tmp_path)
    service = backend(package())
    service.data_dir = tmp_path
    gate = Mock(side_effect=AssertionError('No installer gate for a wholly held batch'))
    result = run_software(service, configuration(), before_install=gate)
    assert result['held_count'] == 1 and result['installer_started'] is False and result['outcomeKnown'] is True
    service.update_app_unattended.assert_not_called()
    assert installer_barrier(result) is None and _uncertain(result) is False


def test_final_boundary_hold_alone_continues_but_uncertain_lookalike_stops(tmp_path):
    service = backend(package(), package('Vendor.Two'))
    held = held_update_result(package(), {'reason': REASON})
    service.update_app_unattended.side_effect = [held, {'exitCode': 0}]
    result = run_software(service, configuration())
    assert service.update_app_unattended.call_count == 2 and result['held_count'] == 1
    service = backend(package(), package('Vendor.Two'))
    service.update_app_unattended.return_value = dict(held, running=True, outcomeKnown=False)
    result = run_software(service, configuration())
    assert service.update_app_unattended.call_count == 1 and result['deferred_ids'] == ['Vendor.Two']
    assert result['outcomeKnown'] is False


def test_held_app_does_not_block_other_app_or_parallel_driver_lane(tmp_path):
    hold_file(tmp_path, identity='Fixture.One')
    service = Backend(tmp_path)
    service.release.set()
    service.outcome = {'exitCode': 0, 'installer_started': True, 'outcomeKnown': True, 'running': False}
    install = Mock(return_value={'resultCode': 2, 'hresult': 0, 'restartNeeded': False})
    enable_fixture_driver(service, install)
    report = Workflow(service).run(driver_settings(), lambda _: None)
    stages = {row['title']: row for row in report['stages']}
    assert 'update:Fixture.One' not in service.calls and 'update:Fixture.Two' in service.calls
    assert stages[WINDOWS_SOFTWARE]['result']['held_ids'] == ['Fixture.One']
    assert stages[WINDOWS_DRIVERS]['status'] == 'completed'
    install.assert_called_once()
