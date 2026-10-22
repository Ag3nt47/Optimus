"""Power policy tests use only fake adapters, fixture commands and owned files."""
import copy
import json
import os
from pathlib import Path
import re
import threading
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from optimus import speed_enhancements as speed
from optimus.automation import CURRENT_SETUP_VERSION, RunLease
from optimus.workflow import DEFAULTS, Preferences


def configuration(**changes):
    value = dict(copy.deepcopy(DEFAULTS), setup_version=CURRENT_SETUP_VERSION, speed_enhancements_enabled=True)
    value.update(changes)
    return value


def windows(**changes):
    value = dict(mechanism='windows-ac-mode', value=speed.AC_BALANCED, active_scheme=speed.BALANCED,
                 offered=[speed.BALANCED, speed.HIGH_PERFORMANCE], ac=True, no_battery=False,
                 can_write=True, energy_saver=False, dc_mode='fixture-battery-policy')
    value.update(changes)
    return value


class Adapter:
    def __init__(self, snapshot=None):
        self.state = copy.deepcopy(snapshot or windows())
        self.calls = []
        self.on_write = None
        self.on_read = None

    def read(self, timeout=30):
        self.calls.append(('read', timeout))
        if self.on_read:
            self.on_read()
        return copy.deepcopy(self.state)

    def write(self, mechanism, desired):
        self.calls.append(('write', mechanism, desired))
        if self.on_write:
            return self.on_write(mechanism, desired)
        self.state['value'] = desired
        if mechanism == 'windows-scheme':
            self.state['active_scheme'] = desired
        if mechanism == 'macos-ac-low-power':
            self.state['settings']['AC Power']['lowpowermode'] = str(desired)
        return True


def service(tmp_path, snapshot=None, system='win32'):
    adapter = Adapter(snapshot)
    backend = SimpleNamespace(data_dir=tmp_path, preference_store=SimpleNamespace(validate=Preferences.validate))
    diagnostic = Mock(return_value={'id': 'resource-observations', 'title': 'Fixture observations',
                                   'status': 'diagnostic', 'reason': 'Read-only fixture.', 'before': {}, 'desired': None})
    return speed.SpeedEnhancements(backend, system=system, adapter=adapter, diagnostics=diagnostic, owner='fixture-owner'), adapter


def writes(adapter):
    return [entry for entry in adapter.calls if entry[0] == 'write']


def test_scan_is_read_only_and_explains_configuration_without_speed_claim(tmp_path):
    engine, adapter = service(tmp_path)
    result = engine.scan()
    assert result['platform'] == 'win32' and result['read_only'] is True
    assert result['items'][0]['status'] == 'available'
    assert 'no speed gain' in result['summary']
    assert not writes(adapter) and not list(tmp_path.iterdir())


@pytest.mark.parametrize('changes', [
    {'ac': False}, {'ac': None}, {'active_scheme': '11111111-2222-3333-4444-555555555555'},
    {'value': '961cc777-2547-4f9d-8174-7d86181b8a7a'}, {'can_write': False}, {'energy_saver': True}, {'energy_saver': None},
])
def test_windows_battery_custom_unknown_or_efficiency_choices_are_preserved(tmp_path, changes):
    engine, adapter = service(tmp_path, windows(**changes))
    result = engine.apply(configuration())
    assert result['status'] == 'deferred' and result['counts']['deferred'] == 1
    assert not writes(adapter)
    assert engine.verify(result)['status'] == 'not_applicable'


@pytest.mark.parametrize('no_battery, offered, expected', [
    (False, [speed.BALANCED, speed.HIGH_PERFORMANCE], 'deferred'),
    (None, [speed.BALANCED, speed.HIGH_PERFORMANCE], 'deferred'),
    (True, [speed.BALANCED], 'deferred'),
    (True, [speed.BALANCED, speed.HIGH_PERFORMANCE], 'completed'),
])
def test_legacy_global_plan_only_changes_an_ac_battery_free_desktop_with_existing_plan(tmp_path, no_battery, offered, expected):
    snapshot = windows(mechanism='windows-scheme', value=speed.BALANCED, no_battery=no_battery, offered=offered)
    engine, adapter = service(tmp_path, snapshot)
    result = engine.apply(configuration())
    assert result['status'] == expected
    assert bool(writes(adapter)) is (expected == 'completed')
    if expected == 'completed':
        assert engine.verify(result)['status'] == 'confirmed'
        restored = engine.restore(result['id'])
        assert restored['status'] == 'restored'
        assert adapter.state['active_scheme'] == speed.BALANCED
        assert engine.verify(restored)['status'] == 'confirmed'


def test_ac_change_is_journaled_before_mutation_and_restores_exact_original_policy(tmp_path):
    engine, adapter = service(tmp_path)
    def mutate(mechanism, desired):
        record = json.loads(next((tmp_path / 'speed-enhancements').glob('[a-f0-9]' * 32 + '.json')).read_text())
        assert record['status'] == 'prepared'
        assert record['change']['before']['value'] == speed.AC_BALANCED
        with pytest.raises(RuntimeError):
            RunLease(tmp_path / 'speed-enhancements').acquire()
        adapter.state['value'] = desired
        return True
    adapter.on_write = mutate
    result = engine.apply(configuration())
    assert result['status'] == 'completed' and result['restore_available']
    assert adapter.state['dc_mode'] == 'fixture-battery-policy'
    assert engine.verify(result)['status'] == 'confirmed'
    adapter.on_write = None
    restored = engine.restore(result['id'])
    assert restored['status'] == 'restored' and not restored['restore_available']
    assert adapter.state['value'] == speed.AC_BALANCED
    assert restored['change']['before']['value'] == speed.AC_BALANCED
    assert engine.verify(restored)['status'] == 'confirmed'


def test_noop_runs_do_not_replace_original_undo_or_create_fake_applied_history(tmp_path):
    engine, adapter = service(tmp_path)
    first = engine.apply(configuration())
    second = engine.apply(configuration())
    assert second['status'] == 'skipped' and second['counts']['applied'] == 0
    assert not second['restore_available'] and second['log_path'] is None
    assert [row['id'] for row in engine.history()] == [first['id']]
    assert engine.history()[0]['restore_available'] is True
    engine.restore(first['id'])
    assert adapter.state['value'] == speed.AC_BALANCED


def test_persistent_same_owner_recovery_works_and_export_cannot_supply_arbitrary_actions(tmp_path):
    engine, adapter = service(tmp_path)
    result = engine.apply(configuration())
    other, _ = service(tmp_path)
    other.adapter = adapter
    assert other.history()[0]['id'] == result['id']
    assert other.verify(dict(result, log_path=str(tmp_path / 'arbitrary.json')))['status'] == 'confirmed'
    with pytest.raises(ValueError):
        other.restore('../arbitrary')
    with pytest.raises(ValueError):
        other.restore('e' * 32)
    other.restore(result['id'])


def test_later_user_change_is_not_clobbered_and_failed_restore_keeps_existing_undo(tmp_path):
    engine, adapter = service(tmp_path)
    result = engine.apply(configuration())
    adapter.state['value'] = speed.AC_BALANCED
    before = len(writes(adapter))
    assert engine.restore(result['id'])['status'] == 'deferred'
    assert len(writes(adapter)) == before
    adapter.state['value'] = speed.AC_PERFORMANCE
    adapter.on_write = lambda *args: False
    failed = engine.restore(result['id'])
    assert failed['status'] == 'failed' and failed['restore_available']
    adapter.on_write = None
    assert engine.restore(result['id'])['status'] == 'restored'


def test_hmac_tampering_cross_owner_hardlinks_and_invalid_history_are_rejected(tmp_path):
    engine, adapter = service(tmp_path)
    result = engine.apply(configuration())
    path = Path(result['log_path'])
    original = path.read_bytes()
    forged = json.loads(original)
    forged['change']['before']['value'] = 'arbitrary-command'
    path.write_text(json.dumps(forged))
    with pytest.raises(ValueError, match='signature'):
        engine.restore(result['id'])
    assert engine.verify(result)['status'] == 'unverified'
    path.write_bytes(original)
    other, _ = service(tmp_path)
    other.owner = 'different-owner'
    with pytest.raises(ValueError, match='ownership'):
        other.history()
    link = tmp_path / 'hardlink.json'
    try:
        os.link(path, link)
    except OSError:
        return
    with pytest.raises(ValueError, match='hardlinks'):
        engine.restore(result['id'])
    assert len(writes(adapter)) == 1


def test_disabled_setup_cancellation_and_journal_failure_prevent_mutation(tmp_path, monkeypatch):
    engine, adapter = service(tmp_path)
    for config in (configuration(speed_enhancements_enabled=False), configuration(setup_version=0)):
        result = engine.apply(config)
        assert result['status'] == 'skipped'
    cancel = threading.Event()
    cancel.set()
    assert engine.apply(configuration(), cancel)['status'] == 'cancelled'
    monkeypatch.setattr(speed, '_private_json', Mock(side_effect=OSError('Fixture journal failure')))
    with pytest.raises(OSError):
        engine.apply(configuration())
    assert not writes(adapter)


def test_cancel_or_state_change_after_prepared_journal_never_calls_setter(tmp_path):
    engine, adapter = service(tmp_path)
    cancel = threading.Event()
    result = engine.apply(configuration(), cancel, progress_callback=lambda event: cancel.set())
    assert result['cancelled'] and not writes(adapter)
    cancel.clear()
    result = engine.apply(configuration(), progress_callback=lambda event: adapter.state.update(ac=False))
    assert result['status'] == 'deferred' and not writes(adapter)


def test_unknown_setter_outcome_is_uncertain_blocks_replay_and_stays_visible(tmp_path):
    engine, adapter = service(tmp_path)
    def timeout(*args):
        adapter.state['value'] = 'unknown-after-timeout'
        raise TimeoutError('private fixture payload')
    adapter.on_write = timeout
    result = engine.apply(configuration())
    assert result['status'] == 'uncertain' and result['failed_items'] == 1
    assert 'private fixture payload' not in json.dumps(result)
    assert engine.verify(result)['status'] == 'needs_review'
    assert engine.apply(configuration())['status'] == 'deferred'
    assert len(writes(adapter)) == 1


def test_changed_battery_policy_during_ac_change_is_not_reported_as_confirmed(tmp_path):
    engine, adapter = service(tmp_path)
    def mutate(mechanism, desired):
        adapter.state.update(value=desired, dc_mode='different-battery-policy')
        return True
    adapter.on_write = mutate
    result = engine.apply(configuration())
    assert result['status'] == 'uncertain' and not result['restore_available']


def test_verifier_uses_owned_ids_cancellation_and_remaining_budget(tmp_path):
    engine, adapter = service(tmp_path)
    assert engine.verify({'id': 'f' * 32, 'log_path': None})['status'] == 'unverified'
    result = engine.apply(configuration())
    adapter.calls.clear()
    assert engine.verify(result, timeout=2)['status'] == 'confirmed'
    assert adapter.calls == [('read', 2)]
    cancel = threading.Event()
    cancel.set()
    adapter.calls.clear()
    assert engine.verify(result, cancel_event=cancel)['status'] == 'unverified'
    assert not adapter.calls


def pmset(low=1, battery=1, high=0):
    return f'Battery Power:\n Sleep On Power Button 1\n lowpowermode {battery}\n sleep 5\nAC Power:\n Sleep On Power Button 1\n lowpowermode {low}\n highpowermode {high}\n sleep 10\n'


def test_macos_parser_and_setter_are_charger_only_and_preserve_battery_settings(tmp_path):
    settings = {'low': 1}
    trace = []
    def command(argv):
        trace.append(argv)
        if argv[1:] == ['-g', 'batt']:
            return "Now drawing from 'AC Power'\n -InternalBattery-0 50%; charging;\n"
        if argv[1:] == ['-g', 'custom']:
            return pmset(settings['low'])
        assert argv == ['/usr/bin/pmset', '-c', 'lowpowermode', '0']
        settings['low'] = 0
        return ''
    engine, _ = service(tmp_path, system='darwin')
    engine.adapter = speed._MacPower(command, root=lambda: True)
    result = engine.apply(configuration())
    assert result['status'] == 'completed'
    assert result['change']['before']['settings']['Battery Power'] == result['after']['settings']['Battery Power']
    assert all('-a' not in argv and '-b' not in argv for argv in trace)


@pytest.mark.parametrize('text', [pmset(high=1), pmset().replace('lowpowermode 1', 'lowpowermode 2'),
    pmset() + 'AC Power:\n lowpowermode 1\n', 'unrecognized output'])
def test_macos_unknown_or_high_power_modes_are_not_overwritten(text):
    with pytest.raises(ValueError):
        speed._pmset_custom(text)


def test_macos_normal_user_does_not_attempt_authentication_or_setter(tmp_path):
    command = Mock(side_effect=["Now drawing from 'AC Power'\n", pmset()])
    engine, _ = service(tmp_path, system='darwin')
    engine.adapter = speed._MacPower(command, root=lambda: False)
    assert engine.apply(configuration())['status'] == 'deferred'
    assert len(command.call_args_list) == 2


def profiles(degraded='no', inhibited='no'):
    return f'  performance:\n    CpuDriver: fixture\n    Degraded: {degraded}\n    Inhibited: {inhibited}\n* balanced:\n    CpuDriver: fixture\n  power-saver:\n    CpuDriver: fixture\n'


@pytest.mark.parametrize('no_battery, ac, holds, degraded, expected', [
    (True, True, '', 'no', 'completed'), (False, True, '', 'no', 'deferred'),
    (True, None, '', 'no', 'deferred'), (True, True, 'performance hold\n', 'no', 'deferred'),
    (True, True, '', 'lap-detected', 'deferred'),
])
def test_linux_global_profile_requires_verified_ac_desktop_and_clear_service_state(tmp_path, no_battery, ac, holds, degraded, expected):
    current, trace = {'value': 'balanced'}, []
    def command(argv):
        trace.append(argv)
        if argv[-1] == 'get': return current['value'] + '\n'
        if argv[-1] == 'list': return profiles(degraded)
        if argv[-1] == 'list-holds': return holds
        assert argv == ['/usr/bin/powerprofilesctl', 'set', 'performance']
        current['value'] = 'performance'
        return ''
    engine, _ = service(tmp_path, system='linux')
    engine.adapter = speed._LinuxPower(command, supply=lambda: {'ac': ac, 'no_battery': no_battery})
    assert engine.apply(configuration())['status'] == expected
    assert any('set' in argv for argv in trace) is (expected == 'completed')


def test_linux_supply_inventory_never_equates_no_battery_with_known_ac(tmp_path):
    assert speed._linux_supply(tmp_path) == {'ac': None, 'no_battery': True}
    mains = tmp_path / 'AC'
    mains.mkdir()
    (mains / 'type').write_text('Mains\n')
    (mains / 'online').write_text('1\n')
    assert speed._linux_supply(tmp_path) == {'ac': True, 'no_battery': True}
    battery = tmp_path / 'BAT0'
    battery.mkdir()
    (battery / 'type').write_text('Battery\n')
    assert speed._linux_supply(tmp_path) == {'ac': True, 'no_battery': False}


def test_windows_api_adapter_prefers_ac_only_and_exposes_existing_schemes_without_commands():
    api = SimpleNamespace(ac_mode=Mock(return_value=speed.AC_BALANCED), dc_mode=Mock(return_value='battery-mode'),
        source=Mock(return_value={'ac': True, 'no_battery': False}), active=Mock(return_value=speed.BALANCED),
        schemes=Mock(return_value=[speed.BALANCED]), set=Mock(return_value=True))
    adapter = speed._WindowsPower(api)
    assert adapter.read()['mechanism'] == 'windows-ac-mode'
    adapter.write('windows-ac-mode', speed.AC_PERFORMANCE)
    api.set.assert_called_once_with('windows-ac-mode', speed.AC_PERFORMANCE)
    api.ac_mode.return_value = None
    assert adapter.read()['mechanism'] == 'windows-scheme'
    assert speed._GUID.parse(speed.AC_PERFORMANCE).text() == speed.AC_PERFORMANCE


@pytest.mark.skipif(os.name != 'nt', reason='Windows token identity is a read-only native integration check.')
def test_windows_64bit_token_owner_readback_uses_real_sid_without_power_changes():
    assert re.fullmatch(r'sid:S-1-\d+(?:-\d+)+', speed._current_owner())
