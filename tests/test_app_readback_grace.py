"""Read-back grace uses fake, already-retired installers and exact queries only."""
import subprocess
import threading
from unittest.mock import Mock

import pytest

from optimus import maintenance
from optimus.emergency_stop import EmergencyStop
from optimus.maintenance import Maintenance
from test_app_presence import installed_table
from test_update_payload_providers import provider


def installed(version='2.0'):
    return subprocess.CompletedProcess([], 0, installed_table('Vendor.App', version), '')


@pytest.mark.parametrize('versions', [('2.0',), ('1.0', '2.0')])
def test_late_registry_version_is_verified_without_repeating_installer(provider, versions):
    provider.fixture_after = '1.0'
    provider._read_only_winget.side_effect = [installed(version) for version in versions]
    result = provider.updates_apply('Vendor.App', confirmed=True, unattended=True)
    assert result['verified'] is True and result['installed_version'] == '2.0'
    assert result['verification_attempts'] == len(versions) + 1
    assert result['verified_after_readback'] is True
    assert result['verification_status'] == 'verified'
    assert len(provider.fixture_paths) == 1
    for call in provider._read_only_winget.call_args_list:
        assert call.args[0] == ['fixture-winget.exe', 'list', '--id', 'Vendor.App', '--exact',
            '--source', 'winget', '--accept-source-agreements', '--disable-interactivity']
        assert 0 < call.kwargs['timeout'] <= 4


def test_normal_verified_install_does_not_add_queries(provider):
    result = provider.updates_apply('Vendor.App', confirmed=True, unattended=True)
    assert result['verified'] is True and result['verification_attempts'] == 1
    assert result['verified_after_readback'] is False
    provider._read_only_winget.assert_not_called()


def test_still_unchanged_version_has_at_most_two_read_only_rechecks(provider):
    provider.fixture_after = '1.0'
    result = provider.updates_apply('Vendor.App', confirmed=True, unattended=True)
    assert result['verified'] is False and result['requiresReview'] is True
    assert result['verification_attempts'] == 3
    assert provider._read_only_winget.call_count == 2 and len(provider.fixture_paths) == 1
    assert result['verification_status'] == 'pending_verification'


@pytest.mark.parametrize('condition', ['child_live', 'app_restart', 'windows_restart', 'failed_installer'])
def test_grace_never_polls_a_known_prerequisite_or_failed_installer(provider, condition):
    provider.fixture_after = '1.0'
    if condition == 'child_live':
        provider.fixture_quiescent = False
    elif condition == 'failed_installer':
        provider.fixture_code = 2
    else:
        original = provider._run.side_effect
        def run(arguments, **options):
            result = original(arguments, **options)
            if arguments[1] == 'upgrade' and '--id' in arguments:
                result.stdout = ('Restart the application to complete the upgrade.' if condition == 'app_restart'
                    else 'Restart your PC to finish installation.')
            return result
        provider._run.side_effect = run
    result = provider.updates_apply('Vendor.App', confirmed=True, unattended=True)
    assert result['verified'] is False
    provider._read_only_winget.assert_not_called()
    assert len(provider.fixture_paths) == 1


@pytest.mark.parametrize('query_result', [
    subprocess.CompletedProcess([], 0, 'unsupported table', ''),
    subprocess.CompletedProcess([], 1, '', 'source unavailable'),
    subprocess.CompletedProcess([], 0x8A150014, 'No installed package found.', ''),
])
def test_unhealthy_initial_inventory_never_triggers_more_queries(provider, query_result):
    original = provider._run.side_effect
    def run(arguments, **options):
        return query_result if arguments[1] == 'list' else original(arguments, **options)
    provider._run.side_effect = run
    result = provider.updates_apply('Vendor.App', confirmed=True, unattended=True)
    assert result['verified'] is False and result['requiresReview'] is True
    provider._read_only_winget.assert_not_called()


@pytest.mark.parametrize('mode', ['cancel', 'stop'])
def test_cancel_or_emergency_stop_prevents_extra_readback(provider, mode):
    event = threading.Event()
    controller = EmergencyStop() if mode == 'stop' else None
    if controller:
        provider.stop_controller = controller
    original = provider._run.side_effect
    def run(arguments, **options):
        result = original(arguments, **options)
        if arguments[1] == 'list':
            event.set() if mode == 'cancel' else controller.event.set()
        return result
    provider.fixture_after = '1.0'
    provider._run.side_effect = run
    result = provider.updates_apply('Vendor.App', confirmed=True, unattended=True, cancel_event=event)
    assert result['verified'] is False
    assert result['verification_attempts'] == 1
    assert all(call.args[0][1] != 'list' for call in provider._read_only_winget.call_args_list)
    assert len(provider.fixture_paths) == 1


def test_exhausted_grace_budget_does_not_launch_third_query(provider, monkeypatch):
    provider.fixture_after = '1.0'
    clock = [100.0]
    monkeypatch.setattr(maintenance.time, 'monotonic', lambda: clock[0])
    def slow_query(*args, **kwargs):
        clock[0] += 4.1
        return installed('1.0')
    provider._read_only_winget.side_effect = slow_query
    result = provider.updates_apply('Vendor.App', confirmed=True, unattended=True)
    assert result['verified'] is False and result['verification_attempts'] == 2
    provider._read_only_winget.assert_called_once()


def test_unhealthy_retry_query_stops_the_grace_without_claiming_install(provider):
    provider.fixture_after = '1.0'
    provider._read_only_winget.return_value = subprocess.CompletedProcess([], 1, '', 'unavailable')
    provider._read_only_winget.side_effect = None
    result = provider.updates_apply('Vendor.App', confirmed=True, unattended=True)
    assert result['verified'] is False and result['verification_attempts'] == 2
    assert result['requiresReview'] is True
    provider._read_only_winget.assert_called_once()


def test_exact_version_query_is_disposable_and_cancellable_without_native_ticket(monkeypatch):
    arguments = ['fixture', 'list', '--id', 'Vendor.App', '--exact', '--source', 'winget',
        '--accept-source-agreements', '--disable-interactivity']
    child = Mock(returncode=0)
    child.communicate.return_value = ('fixture output', '')
    controller = Mock()
    event = threading.Event()
    controller.bind_cancel.return_value = event
    controller.spawn_disposable.return_value = child
    result = Maintenance._read_only_winget(arguments, event, stop_controller=controller, timeout=1)
    assert result.returncode == 0
    controller.spawn_disposable.assert_called_once()
    controller.begin_native.assert_not_called()


@pytest.mark.parametrize('query', ['list', 'show'])
def test_installed_version_and_catalog_timeouts_retire_only_the_exact_query(tmp_path, monkeypatch, query):
    arguments = ['fixture', query, '--id', 'Vendor.App', '--exact', '--source', 'winget']
    if query == 'show':
        arguments += ['--version', '2.0']
    arguments += ['--accept-source-agreements', '--disable-interactivity']
    clock = [100.0]
    monkeypatch.setattr(maintenance.time, 'monotonic', lambda: clock[0])
    child = Mock(returncode=-15)
    def communicate(**options):
        if not child.terminate.called:
            clock[0] += 2
            raise subprocess.TimeoutExpired(arguments, options['timeout'])
        return ('partial version metadata', '')
    child.communicate.side_effect = communicate
    controller = Mock()
    controller.bind_cancel.side_effect = lambda event: event
    controller.spawn_disposable.return_value = child
    service = Maintenance(None, tmp_path)
    service.stop_controller = controller
    result = service._run(arguments, timeout=1)
    assert result.returncode == 130
    child.terminate.assert_called_once()
    controller.spawn_disposable.assert_called_once()
    assert controller.spawn_disposable.call_args.args[0] == arguments
    controller.begin_native.assert_not_called()
    controller.observe_native_child.assert_not_called()


@pytest.mark.parametrize('arguments', [
    ['fixture', 'list', '--id', 'Vendor.App', '--exact', '--source', 'other', '--accept-source-agreements', '--disable-interactivity'],
    ['fixture', 'list', '--id', 'Vendor.App;calc', '--exact', '--source', 'winget', '--accept-source-agreements', '--disable-interactivity'],
    ['fixture', 'upgrade', '--id', 'Vendor.App'],
    ['fixture', 'list', '--id', 'Vendor.App', '--exact', '--source', 'winget', '--accept-source-agreements', '--disable-interactivity', '--force'],
    ['fixture', 'show', '--id', 'Vendor.App', '--exact', '--source', 'winget', '--version', '--force', '--accept-source-agreements', '--disable-interactivity'],
])
def test_disposable_query_grammar_does_not_admit_installers_or_changed_sources(arguments):
    with pytest.raises(ValueError, match='exact read-only'):
        Maintenance._read_only_winget(arguments, threading.Event(), timeout=1)


def test_read_only_timeout_retires_only_its_query_and_does_not_report_success(monkeypatch):
    arguments = ['fixture', 'list', '--id', 'Vendor.App', '--exact', '--source', 'winget',
        '--accept-source-agreements', '--disable-interactivity']
    clock = [100.0]
    monkeypatch.setattr(maintenance.time, 'monotonic', lambda: clock[0])
    child = Mock(returncode=-15)
    def communicate(**options):
        if not child.terminate.called:
            clock[0] += 2
            raise subprocess.TimeoutExpired(arguments, options['timeout'])
        return ('partial query output', '')
    child.communicate.side_effect = communicate
    spawn = Mock(return_value=child)
    monkeypatch.setattr(maintenance.subprocess, 'Popen', spawn)
    result = Maintenance._read_only_winget(arguments, threading.Event(), timeout=1)
    assert result.returncode == 130
    child.terminate.assert_called_once()
    child.kill.assert_not_called()
    assert spawn.call_args.args[0] == arguments


@pytest.mark.parametrize('timeout', [0, -1, True, '4', float('nan'), float('inf'), 121])
def test_read_only_timeout_must_be_a_finite_bounded_duration(timeout, monkeypatch):
    spawn = Mock()
    monkeypatch.setattr(maintenance.subprocess, 'Popen', spawn)
    with pytest.raises(ValueError, match='bounded positive'):
        Maintenance._read_only_winget(['fixture', 'pin', 'list', '--disable-interactivity'],
            threading.Event(), timeout=timeout)
    spawn.assert_not_called()
