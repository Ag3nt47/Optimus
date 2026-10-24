"""Application presence fixtures never run a host package manager."""
from __future__ import annotations

import copy
import json
from pathlib import Path
import subprocess
import threading
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from optimus import app_presence as presence
from optimus.app_presence import (PresencePreflightError, WindowsAppPresence,
    parse_catalog, parse_installed, protected_application, run_app_presence)
from optimus.automation import CURRENT_SETUP_VERSION
from optimus.navigation import NavigationHub


IDENTITY = 'Example.Reader'
SOURCE = {'Name': 'winget', 'Arg': 'https://cdn.winget.microsoft.com/cache',
          'Type': 'Microsoft.PreIndexed.Package', 'Identifier': 'Microsoft.Winget.Source_8wekyb3d8bbwe',
          'Data': 'Microsoft.Winget.Source_8wekyb3d8bbwe'}


def result(text='', code=0):
    return subprocess.CompletedProcess([], code, text, '')


def installed_table(identity=IDENTITY, version='2.0', name='Example Reader'):
    header = f"{'Name':35}{'Id':45}{'Version':20}Source"
    return header + '\n' + '-' * len(header) + '\n' + f'{name:35}{identity:45}{version:20}winget\n'


def catalog_text(identity=IDENTITY, version='2.0', kind='msi', name='Example Reader'):
    return (f'Found {name} [{identity}]\nVersion: {version}\nPublisher: Example Software\nInstaller:\n'
            f'  Installer Type: {kind}\n  Installer Url: https://example.test/reader.msi\n'
            '  Installer SHA256: ' + 'a' * 64 + '\n')


def settings(*, identity=IDENTITY, desired='keep_installed'):
    return {'setup_version': CURRENT_SETUP_VERSION, 'software_update_agreements_accepted': True,
            'software_updates_enabled': True,
            'software_app_rules': [{'id': identity, 'source': 'winget',
                'update_policy': 'always_ignore' if desired == 'keep_uninstalled' else 'follow_global',
                'presence': desired}]}


class FixtureMaintenance:
    def __init__(self):
        self.installed = {}
        self.actions = []
        self.commands = []
        self.source = dict(SOURCE)
        self.kind = 'msi'
        self.native_code = 0
        self.error = None
        self.mutate = True
        self.pin = (True, '', False)
        self.on_command = None

    def _winget(self):
        return 'fixture-winget.exe'

    def _pin_status(self, package):
        return self.pin

    def _run(self, arguments, *, timeout, progress_callback=None):
        self.commands.append(list(arguments))
        operation = arguments[1]
        identity = arguments[arguments.index('--id') + 1] if '--id' in arguments else IDENTITY
        if self.on_command:
            self.on_command(arguments)
        if operation == 'source':
            return result(json.dumps(self.source))
        if operation == 'list':
            return result(installed_table(identity, self.installed[identity])) if identity in self.installed else result('', 0x8A150014)
        if operation == 'show':
            version = arguments[arguments.index('--version') + 1] if '--version' in arguments else '2.0'
            return result(catalog_text(identity, version, self.kind))
        assert operation in ('install', 'uninstall'), arguments
        self.actions.append(list(arguments))
        if progress_callback is not None:
            progress_callback({'phase': operation, 'percent': 12.0})
        if self.error:
            raise self.error
        if self.mutate:
            if operation == 'install':
                self.installed[identity] = arguments[arguments.index('--version') + 1]
            else:
                self.installed.pop(identity, None)
        return result('Fixture native operation returned.', self.native_code)


@pytest.fixture
def backend(tmp_path, monkeypatch):
    monkeypatch.setattr(presence, '_current_owner', lambda: 'sid:S-1-5-21-1-2-3-1000')
    service = SimpleNamespace(data_dir=tmp_path, system='win32', workflow=SimpleNamespace(active=True),
        maintenance=FixtureMaintenance(), is_admin=Mock(return_value=True),
        security_status=Mock(return_value={'reboot_required': False}),
        update_disk_readiness=Mock(return_value={'ready': True}))
    service.saved = settings()
    service.preferences = lambda: copy.deepcopy(service.saved)
    return service


def apply(backend, config=None, **kwargs):
    config = config or settings()
    backend.saved = copy.deepcopy(config)
    return run_app_presence(backend, config, **kwargs)


def journals(backend):
    return [json.loads(path.read_text()) for path in (backend.data_dir / 'parallel-updates').glob('*.json')]


def test_no_presence_rules_does_not_probe_host_or_create_files(tmp_path):
    backend = SimpleNamespace(data_dir=tmp_path, system='win32')
    report = run_app_presence(backend, {'software_updates_enabled': True, 'software_app_rules': []})
    assert report['installer_started'] is False and report['outcomeKnown'] is True
    assert not list(tmp_path.iterdir())


def test_software_updater_off_preserves_rules_without_any_host_probe(tmp_path):
    service = SimpleNamespace(data_dir=tmp_path, system='win32')
    config = settings()
    config['software_updates_enabled'] = False
    report = run_app_presence(service, config)
    assert report['skipped'] is True and report['installer_started'] is False
    assert report['outcomeKnown'] is True and not list(tmp_path.iterdir())


def test_missing_app_is_installed_with_exact_quiet_release_and_fresh_readback(backend):
    gate = Mock(return_value=True)
    progress = Mock()
    report = apply(backend, before_install=gate, item_callback=progress)
    assert report['items'][0]['status'] == 'completed'
    assert report['items'][0]['result']['verified'] is True
    assert report['installer_started'] is True and report['outcomeKnown'] is True
    command, = backend.maintenance.actions
    for flag in ('--id', '--exact', '--source', '--version', '--silent', '--disable-interactivity',
                 '--accept-package-agreements', '--accept-source-agreements', '--no-upgrade', '--installer-type'):
        assert flag in command
    for flag in ('--force', '--ignore-security-hash', '--allow-reboot', '--purge', '--override', '--uninstall-previous'):
        assert flag not in command
    assert command[command.index('--version') + 1] == '2.0'
    assert gate.call_count == 1 and progress.call_count >= 3
    receipt, = journals(backend)
    assert receipt['status'] == 'completed' and receipt['installer_prepared'] is True
    assert receipt['result']['items'][0]['result']['verified'] is True


@pytest.mark.parametrize('kind', ['msi', 'wix', 'msix', 'appx', 'burn', 'inno'])
def test_supported_native_quiet_handlers_can_install_exact_release(backend, kind):
    backend.maintenance.kind = kind
    report = apply(backend)
    assert report['items'][0]['status'] == 'completed'
    command, = backend.maintenance.actions
    assert command[command.index('--installer-type') + 1] == kind


@pytest.mark.parametrize('kind', ['exe', 'zip', 'portable', 'nullsoft', 'unknown'])
def test_handlers_without_verified_quiet_and_restart_behavior_are_review_only(backend, kind):
    backend.maintenance.kind = kind
    report = apply(backend)
    assert report['items'][0]['status'] == 'needs_review'
    assert not backend.maintenance.actions


def test_installed_app_is_removed_at_exact_installed_version(backend):
    backend.maintenance.installed[IDENTITY] = '1.0'
    report = apply(backend, settings(desired='keep_uninstalled'))
    command, = backend.maintenance.actions
    assert command[1] == 'uninstall'
    assert command[command.index('--version') + 1] == '1.0'
    assert '--all' not in command and '--purge' not in command
    assert report['items'][0]['result']['after'] is None
    assert report['items'][0]['status'] == 'completed'


@pytest.mark.parametrize('action,desired', [('install', 'keep_installed'), ('uninstall', 'keep_uninstalled')])
def test_presence_reports_native_item_progress_and_only_finishes_after_readback(backend, action, desired):
    if action == 'uninstall':
        backend.maintenance.installed[IDENTITY] = '1.0'
    events = []
    report = apply(backend, settings(desired=desired), item_callback=events.append)
    values = [event['item_progress'] for event in events]
    assert values[0]['phase'] == action and values[0]['percent'] is None
    assert any(value['phase'] == action and value['percent'] == 12.0 for value in values)
    assert values[-2]['phase'] == 'verify' and values[-2]['percent'] is None
    assert values[-1]['phase'] == action and values[-1]['percent'] == 100.0
    assert all(value['key'] == 'presence:winget:' + IDENTITY for value in values)
    assert report['items'][0]['result']['verified'] is True


@pytest.mark.parametrize('problem', ['postcondition', 'native_failure', 'restart', 'incomplete'])
def test_presence_never_reports_terminal_100_for_unverified_or_restart_outcome(backend, problem):
    backend.maintenance.installed[IDENTITY] = '1.0'
    if problem == 'postcondition':
        backend.maintenance.mutate = False
    elif problem == 'native_failure':
        backend.maintenance.native_code = 1603
    elif problem == 'restart':
        backend.maintenance.native_code = 3010
    else:
        original = backend.maintenance._run
        def incomplete(arguments, **options):
            native = original(arguments, **options)
            if arguments[1] == 'uninstall':
                native.capture_complete = False
            return native
        backend.maintenance._run = incomplete
    events = []
    report = apply(backend, settings(desired='keep_uninstalled'), item_callback=events.append)
    assert not any(event['item_progress']['percent'] == 100 for event in events)
    assert len(backend.maintenance.actions) == 1
    if problem == 'incomplete':
        assert report['outcomeKnown'] is False
        assert journals(backend)[0]['status'] == 'uncertain'


def test_presence_progress_resets_for_next_identity_and_cannot_receive_late_ticks(backend):
    config = settings(desired='keep_uninstalled')
    config['software_app_rules'].append(dict(config['software_app_rules'][0], id='Example.Second'))
    backend.maintenance.installed = {IDENTITY: '1.0', 'Example.Second': '1.0'}
    callbacks, events = [], []
    original = backend.maintenance._run
    def run(arguments, *, timeout, progress_callback=None):
        if progress_callback is not None:
            callbacks.append(progress_callback)
        return original(arguments, timeout=timeout, progress_callback=progress_callback)
    backend.maintenance._run = run
    report = apply(backend, config, item_callback=events.append)
    assert len(report['items']) == 2 and all(row['status'] == 'completed' for row in report['items'])
    starts = [event for event in events if event.get('progress_only') is not True]
    assert [event['item_progress']['percent'] for event in starts] == [None, None]
    assert starts[0]['item_progress']['key'] != starts[1]['item_progress']['key']
    count = len(events)
    for callback in callbacks:
        callback({'phase': 'uninstall', 'percent': 50.0})
    assert len(events) == count


@pytest.mark.parametrize('desired,installed', [('keep_installed', True), ('keep_uninstalled', False)])
def test_satisfied_rule_never_launches_or_creates_intent(backend, desired, installed):
    if installed:
        backend.maintenance.installed[IDENTITY] = '1.0'
    report = apply(backend, settings(desired=desired))
    assert report['items'][0]['status'] == 'already_satisfied'
    assert backend.maintenance.actions == [] and journals(backend) == []


@pytest.mark.parametrize('desired,installed', [('keep_installed', True), ('keep_uninstalled', False)])
@pytest.mark.parametrize('catalog_problem', ['unsupported_exe', 'missing_old_release'])
def test_satisfied_rule_needs_only_trusted_installed_query_not_catalog_or_readiness(backend, desired, installed, catalog_problem):
    if installed:
        backend.maintenance.installed[IDENTITY] = '1.0'
    backend.maintenance.kind = 'exe'
    backend.is_admin.return_value = False
    backend.security_status.side_effect = AssertionError('Satisfied rules do not check restart readiness.')
    backend.update_disk_readiness.side_effect = AssertionError('Satisfied rules do not check disk readiness.')
    backend.maintenance._pin_status = Mock(side_effect=AssertionError('Satisfied rules do not inspect installer pins.'))
    def command(arguments):
        if arguments[1] == 'show':
            raise AssertionError('A satisfied ' + catalog_problem + ' rule must not require its catalog.')
    backend.maintenance.on_command = command
    gate = Mock(side_effect=AssertionError('No installer lane is required for an already satisfied rule.'))
    report = apply(backend, settings(desired=desired), before_install=gate)
    assert report['items'][0]['status'] == 'already_satisfied'
    assert report['outcomeKnown'] is True and report['installer_started'] is False
    assert report['failed_items'] == 0 and not backend.maintenance.actions and not journals(backend)
    assert [row[1] for row in backend.maintenance.commands] == ['source', 'list']
    backend.is_admin.assert_not_called()
    gate.assert_not_called()


@pytest.mark.parametrize('desired', ['keep_installed', 'keep_uninstalled'])
def test_unknown_or_localized_installed_query_cannot_claim_satisfied_state(backend, desired):
    original = backend.maintenance._run
    def command(arguments, *, timeout):
        if arguments[1] == 'list':
            return result('Nom       Identifiant       Version\n------------------------------------\n', 0)
        return original(arguments, timeout=timeout)
    backend.maintenance._run = command
    report = apply(backend, settings(desired=desired))
    assert report['items'][0]['status'] == 'needs_review'
    assert report['installer_started'] is False and not backend.maintenance.actions


def test_manual_inspection_still_requires_and_returns_full_catalog(backend):
    backend.maintenance.installed[IDENTITY] = '1.0'
    snapshot = WindowsAppPresence(backend).inspect(IDENTITY)
    assert snapshot['catalog']['version'] == '1.0' and snapshot['fingerprint']
    backend.maintenance.kind = 'exe'
    with pytest.raises(PresencePreflightError, match='installer type'):
        WindowsAppPresence(backend).inspect(IDENTITY)


@pytest.mark.parametrize('identity', ['Microsoft.BingWallpaper', 'OpenAI.Codex', 'Agent47.Optimus',
    'Microsoft.WindowsTerminal', 'Microsoft.VCRedist.2015+.x64', 'Microsoft.DotNet.Runtime.8',
    'Malwarebytes.Malwarebytes', 'CrowdStrike.Falcon', 'Microsoft.EdgeWebView2Runtime'])
def test_protected_apps_are_preserved(backend, identity):
    backend.maintenance.installed[identity] = '1.0'
    report = apply(backend, settings(identity=identity, desired='keep_uninstalled'))
    assert report['items'][0]['result']['installer_started'] is False
    assert report['items'][0]['status'] == 'needs_review'
    assert not backend.maintenance.actions and not journals(backend)


@pytest.mark.parametrize('guard', ['admin', 'restart', 'space', 'pin', 'source', 'type', 'agreements', 'setup', 'lease'])
def test_readiness_and_source_guards_prevent_installer(backend, guard):
    config = settings()
    if guard == 'admin': backend.is_admin.return_value = False
    elif guard == 'restart': backend.security_status.return_value = {'reboot_required': True}
    elif guard == 'space': backend.update_disk_readiness.return_value = {'ready': False}
    elif guard == 'pin': backend.maintenance.pin = (False, 'Pinned application.', False)
    elif guard == 'source': backend.maintenance.source['Arg'] = 'https://untrusted.test/cache'
    elif guard == 'type': backend.maintenance.kind = 'exe'
    elif guard == 'agreements': config['software_update_agreements_accepted'] = False
    elif guard == 'setup': config['setup_version'] = 0
    elif guard == 'lease': backend.workflow.active = False
    if guard in ('agreements', 'setup', 'lease'):
        with pytest.raises(PresencePreflightError): apply(backend, config)
    else:
        report = apply(backend, config)
        assert report['items'][0]['status'] == 'needs_review'
    assert not backend.maintenance.actions


def test_saved_exact_offer_hold_is_preserved(backend, monkeypatch):
    monkeypatch.setattr(presence, 'load_update_holds', lambda _: [{'id': IDENTITY,
        'available_version': '2.0', 'reason': 'Fixture incompatible release'}])
    report = apply(backend)
    assert 'hold' in report['items'][0]['result']['note']
    assert not backend.maintenance.actions


def test_native_presence_reports_actual_unsupported_capability_without_host_calls(tmp_path):
    backend = SimpleNamespace(data_dir=tmp_path, system='linux')
    config = settings(identity='Example.Reader')
    report = run_app_presence(backend, config)
    assert report['items'][0]['status'] == 'needs_review'
    assert report['installer_started'] is False and report['outcomeKnown'] is True
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize('boundary', ['source', 'list', 'show', 'callback', 'gate'])
def test_cancellation_before_launch_never_mutates(backend, boundary):
    event = threading.Event()
    def command(arguments):
        if arguments[1] == boundary: event.set()
    backend.maintenance.on_command = command
    callback = (lambda _: event.set()) if boundary == 'callback' else None
    def gate(_):
        if boundary == 'gate': event.set()
        return True
    report = apply(backend, cancel_event=event, item_callback=callback, before_install=gate)
    assert report['cancelled'] is True and backend.maintenance.actions == []
    assert all(row['installer_prepared'] is False for row in journals(backend))


def test_changed_saved_scope_during_progress_prevents_launch(backend):
    def callback(_): backend.saved['software_app_rules'] = []
    report = apply(backend, item_callback=callback)
    assert not backend.maintenance.actions
    assert report['items'][0]['result']['outcomeKnown'] is True


def test_disabling_master_during_final_pin_check_prevents_launch(backend):
    calls = [0]
    def pin(_):
        calls[0] += 1
        if calls[0] == 2: backend.saved['software_updates_enabled'] = False
        return True, '', False
    backend.maintenance._pin_status = pin
    report = apply(backend)
    assert not backend.maintenance.actions and report['installer_started'] is False
    assert report['outcomeKnown'] is True


def test_read_only_revalidation_error_is_known_no_launch(backend):
    calls = [0]
    def command(arguments):
        if arguments[1] == 'source':
            calls[0] += 1
            if calls[0] == 2: raise subprocess.TimeoutExpired(['fixture source probe'], 90)
    backend.maintenance.on_command = command
    report = apply(backend)
    assert report['installer_started'] is False and report['outcomeKnown'] is True
    assert not backend.maintenance.actions


def test_changed_catalog_between_preview_and_launch_prevents_mutation(backend):
    count = [0]
    def command(arguments):
        if arguments[1] == 'source':
            count[0] += 1
            if count[0] == 2: backend.maintenance.kind = 'exe'
    backend.maintenance.on_command = command
    report = apply(backend)
    assert not backend.maintenance.actions and report['outcomeKnown'] is True
    assert report['items'][0]['status'] == 'needs_review'


def test_intent_is_durable_before_native_mutation_and_unknown_result_stays_barrier(backend):
    def command(arguments):
        if arguments[1] == 'install':
            receipt, = journals(backend)
            assert receipt['status'] == 'running' and receipt['installer_prepared'] is True
    backend.maintenance.on_command = command
    backend.maintenance.error = subprocess.TimeoutExpired(['fixture-winget'], 1800)
    report = apply(backend)
    assert report['outcomeKnown'] is False and report['items'][0]['status'] == 'needs_review'
    receipt, = journals(backend)
    assert receipt['status'] == 'uncertain'


def test_unknown_presence_receipt_blocks_existing_installer_barrier_on_next_run(backend, monkeypatch):
    from optimus import speed_enhancements
    from optimus.parallel_updates import pending_updates_barrier
    monkeypatch.setattr(speed_enhancements, '_current_owner', presence._current_owner)
    backend.maintenance.error = subprocess.TimeoutExpired(['fixture-winget'], 1800)
    apply(backend)
    assert 'unresolved' in pending_updates_barrier(backend)


def test_readback_failure_is_unknown_and_cannot_be_claimed_installed(backend):
    original = backend.maintenance._run
    def command(arguments, *, timeout):
        if arguments[1] == 'list' and backend.maintenance.actions:
            return result('Fixture installed query failed.', 1)
        return original(arguments, timeout=timeout)
    backend.maintenance._run = command
    report = apply(backend)
    assert report['outcomeKnown'] is False and report['items'][0]['result']['verified'] is False
    assert journals(backend)[0]['status'] == 'uncertain'


def test_invalid_completion_metadata_after_launch_remains_unknown(backend):
    original = backend.maintenance._run
    def command(arguments, *, timeout):
        if arguments[1] == 'install':
            backend.maintenance.actions.append(arguments)
            return SimpleNamespace(returncode=True, stdout='Fixture malformed completion')
        return original(arguments, timeout=timeout)
    backend.maintenance._run = command
    report = apply(backend)
    assert report['installer_started'] is True and report['outcomeKnown'] is False
    assert journals(backend)[0]['status'] == 'uncertain'


def test_failed_final_receipt_retains_unknown_and_durable_running_intent(backend, monkeypatch):
    original = presence._private_json
    def write(path, value):
        if value['status'] != 'prepared' and 'result' in value:
            raise OSError('Fixture final receipt write denied')
        original(path, value)
    monkeypatch.setattr(presence, '_private_json', write)
    report = apply(backend)
    assert report['outcomeKnown'] is False
    assert len(report['items']) == 1
    receipt, = journals(backend)
    assert receipt['status'] == 'running' and receipt['installer_prepared'] is True


def test_failed_intent_receipt_prevents_native_mutation(backend, monkeypatch):
    def write(path, value): raise OSError('Fixture no evidence volume')
    monkeypatch.setattr(presence, '_private_json', write)
    report = apply(backend)
    assert report['installer_started'] is False and not backend.maintenance.actions


@pytest.mark.parametrize('code', [3010, 1641, 1618, 0x8A150102])
def test_restart_or_running_installer_stops_remaining_rules(backend, code):
    backend.maintenance.native_code = code
    config = settings()
    config['software_app_rules'].append({**config['software_app_rules'][0], 'id': 'Example.Second'})
    report = apply(backend, config)
    assert len(backend.maintenance.actions) == 1
    assert report['deferred_ids'] == ['Example.Second']
    assert report['restartNeeded'] is True if code in (3010, 1641) else report['outcomeKnown'] is False


def test_success_exit_without_postcondition_is_not_claimed_applied(backend):
    backend.maintenance.mutate = False
    report = apply(backend)
    assert report['items'][0]['status'] == 'needs_review'
    assert report['items'][0]['result']['verified'] is False


def two_rules(desired='keep_installed'):
    config = settings(desired=desired)
    config['software_app_rules'].append({**config['software_app_rules'][0], 'id': 'Example.Second'})
    return config


@pytest.mark.parametrize('code', [0, 1603])
@pytest.mark.parametrize('desired', ['keep_installed', 'keep_uninstalled'])
def test_known_terminal_presence_failure_continues_to_next_distinct_rule(backend, code, desired):
    if desired == 'keep_uninstalled':
        backend.maintenance.installed = {IDENTITY: '1.0', 'Example.Second': '1.0'}
    def command(arguments):
        if arguments[1] in ('install', 'uninstall'):
            first = arguments[arguments.index('--id') + 1] == IDENTITY
            backend.maintenance.mutate = not first
            backend.maintenance.native_code = code if first else 0
    backend.maintenance.on_command = command
    report = apply(backend, two_rules(desired))
    assert [row['status'] for row in report['items']] == ['needs_review', 'completed']
    assert report['items'][0]['result']['verified'] is False
    assert report['items'][1]['result']['verified'] is True
    assert report['failed_items'] == 1 and report['outcomeKnown'] is True and report['deferred_ids'] == []
    assert [command[command.index('--id') + 1] for command in backend.maintenance.actions] == [IDENTITY, 'Example.Second']
    receipt, = journals(backend)
    assert receipt['result']['failed_items'] == 1 and len(receipt['result']['items']) == 2


def test_unsupported_first_rule_is_recorded_then_next_supported_rule_is_tried(backend):
    def command(arguments):
        if arguments[1] == 'show':
            first = arguments[arguments.index('--id') + 1] == IDENTITY
            backend.maintenance.kind = 'exe' if first else 'msi'
    backend.maintenance.on_command = command
    report = apply(backend, two_rules())
    assert [row['status'] for row in report['items']] == ['needs_review', 'completed']
    assert len(backend.maintenance.actions) == 1
    assert backend.maintenance.actions[0][backend.maintenance.actions[0].index('--id') + 1] == 'Example.Second'
    assert report['items'][0]['result']['failure_scope'] == 'package'
    from optimus.parallel_updates import installer_barrier
    assert installer_barrier(report) is None


@pytest.mark.parametrize('guard', ['admin', 'source', 'pin_inventory'])
def test_shared_presence_safety_failure_defers_remaining_rules_with_typed_reason(backend, guard):
    if guard == 'admin': backend.is_admin.return_value = False
    elif guard == 'source': backend.maintenance.source['Arg'] = 'https://untrusted.test/source'
    else: backend.maintenance.pin = (False, 'The pin inventory is untrusted.', True)
    report = apply(backend, two_rules())
    assert len(report['items']) == 1 and not backend.maintenance.actions
    assert report['shared_barrier'] is True and report['stop_reason']
    assert report['items'][0]['result']['failure_scope'] == 'shared'
    assert report['deferred_ids'] == ['Example.Second']
    from optimus.parallel_updates import installer_barrier
    assert installer_barrier(report)


def test_one_pinned_app_is_package_scoped_and_does_not_block_next_app(backend):
    backend.maintenance._pin_status = lambda package: ((False, 'This exact application is pinned.', False)
        if package['id'] == IDENTITY else (True, '', False))
    report = apply(backend, two_rules())
    assert [row['status'] for row in report['items']] == ['needs_review', 'completed']
    assert report['items'][0]['result']['failureKind'] == 'pinned'
    assert report['items'][0]['result']['failure_scope'] == 'package'


@pytest.mark.parametrize('code', [0x80070005, 0x80070070])
def test_shared_native_security_or_disk_failure_cannot_fail_forward(backend, code):
    backend.maintenance.native_code = code
    backend.maintenance.mutate = False
    report = apply(backend, two_rules())
    assert len(backend.maintenance.actions) == 1
    assert report['shared_barrier'] is True and report['deferred_ids'] == ['Example.Second']


@pytest.mark.parametrize('failure', ['timeout', 'readback', 'receipt'])
def test_unknown_presence_outcome_still_defers_remaining_rule(backend, monkeypatch, failure):
    if failure == 'timeout':
        backend.maintenance.error = subprocess.TimeoutExpired(['fixture-winget'], 1800)
    elif failure == 'readback':
        original = backend.maintenance._run
        def command(arguments, *, timeout):
            if arguments[1] == 'list' and backend.maintenance.actions:
                return result('Unverified installed state.', 1)
            return original(arguments, timeout=timeout)
        backend.maintenance._run = command
    else:
        original = presence._private_json
        def write(path, value):
            if 'result' in value:
                raise OSError('Fixture terminal receipt unavailable')
            original(path, value)
        monkeypatch.setattr(presence, '_private_json', write)
    report = apply(backend, two_rules())
    assert len(backend.maintenance.actions) == 1
    assert report['outcomeKnown'] is False and report['deferred_ids'] == ['Example.Second']
    assert journals(backend)[0]['status'] in ('running', 'uncertain')


def test_continuing_after_known_failure_rechecks_restart_guard_for_next_rule(backend):
    def command(arguments):
        if arguments[1] == 'install':
            backend.maintenance.mutate = False
            backend.maintenance.native_code = 1603
            backend.security_status.return_value = {'reboot_required': True}
    backend.maintenance.on_command = command
    report = apply(backend, two_rules())
    assert len(backend.maintenance.actions) == 1
    assert [row['status'] for row in report['items']] == ['needs_review', 'needs_review']
    assert 'restart state' in report['items'][1]['result']['note']


def test_navigation_next_skips_one_presence_item_without_cancelling_batch(backend):
    hub = NavigationHub()
    hub.begin('a' * 32)
    cursor = hub.cursor('software_updates')
    config = settings()
    config['software_app_rules'].append({**config['software_app_rules'][0], 'id': 'Example.Second'})
    def progress(_):
        if cursor.current['key'].endswith(IDENTITY):
            response = hub.request(hub.session_id, 'software_updates', cursor.revision, 'next')
            assert response['accepted'] is True
    report = apply(backend, config, item_callback=progress, navigation=cursor)
    assert report['cancelled'] is False and report['outcomeKnown'] is True
    assert [row['status'] for row in report['items']] == ['skipped', 'completed']
    command, = backend.maintenance.actions
    assert command[command.index('--id') + 1] == 'Example.Second'
    assert cursor.current is None and cursor.history[0]['mutation_started'] is False


def test_navigation_next_during_install_retains_actual_completion(backend):
    hub = NavigationHub()
    hub.begin('a' * 32)
    cursor = hub.cursor('software_updates')
    def command(arguments):
        if arguments[1] == 'install':
            assert hub.request(hub.session_id, 'software_updates', cursor.revision, 'next')['accepted'] is True
    backend.maintenance.on_command = command
    report = apply(backend, navigation=cursor)
    assert report['cancelled'] is False and report['items'][0]['status'] == 'completed'
    assert cursor.history[0]['mutation_started'] is True
    assert 'finished before' in cursor.message


@pytest.mark.parametrize('output,code', [('', 0), (installed_table(version='Unknown'), 0),
    (installed_table(identity='Example.Other'), 0), (installed_table() + installed_table().splitlines()[-1], 0),
    ('No installed package found matching input criteria.', 1), ('Nom Id Version\n----------\n', 0)])
def test_unknown_installed_metadata_never_becomes_absent(output, code):
    with pytest.raises(PresencePreflightError): parse_installed(result(output, code), IDENTITY)


@pytest.mark.parametrize('change', ['duplicate', 'wrong_id', 'hash', 'http', 'exe', 'version'])
def test_incomplete_or_untrusted_catalog_metadata_fails_closed(change):
    text = catalog_text()
    if change == 'duplicate': text += 'Version: 2.0\n'
    elif change == 'wrong_id': text = text.replace(IDENTITY, 'Example.Other')
    elif change == 'hash': text = text.replace('a' * 64, 'no hash')
    elif change == 'http': text = text.replace('https://', 'http://')
    elif change == 'exe': text = text.replace('Type: msi', 'Type: exe')
    elif change == 'version': text = text.replace('Version: 2.0', 'Version: Unknown')
    with pytest.raises(PresencePreflightError): parse_catalog(result(text), IDENTITY)
