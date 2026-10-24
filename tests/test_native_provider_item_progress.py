"""Provider phase fixtures never launch a real package manager or broker."""
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from optimus.native_backend import NativeBackend
from optimus.native_operations import NativePackages
from optimus.native_workflow import run_native_software


@pytest.fixture
def packages(tmp_path, monkeypatch):
    value = NativePackages(tmp_path, system='linux', runner=Mock())
    monkeypatch.setattr('optimus.native_operations.shutil.disk_usage', lambda _: SimpleNamespace(free=10 * 1024**3))
    value.runner.run.return_value = value.runner.privileged.return_value = {'exitCode': 0}
    return value


@pytest.mark.parametrize('manager,name,extra', [('apt', 'editor', {}), ('dnf', 'editor.x86_64', {}),
    ('brew', 'editor', {'kind': 'formula'}), ('brew', 'editor', {'kind': 'cask'})])
def test_native_provider_has_indeterminate_install_then_verified100_with_original_command(packages, manager, name, extra):
    packages.manager, packages.executable = manager, '/fixture/' + manager
    row = packages._record(manager, name, '1.0', '2.0', **extra)
    packages.scan = Mock(return_value={'updates': [row]})
    packages.installed = Mock(return_value=[packages._record(manager, name, row['available'], **extra)])
    events = []
    result = packages.apply(row['id'], confirmed=True, progress_callback=events.append)
    assert result['verified'] is True
    assert [(event['phase'], event['percent']) for event in events] == [('install', None), ('verify', None), ('install', 100.0)]
    assert all(event['key'] == 'software:update:' + row['id'] and event['label'] == row['id'] for event in events)
    assert events[-1]['item_terminal'] is True
    runner = packages.runner.run if manager == 'brew' else packages.runner.privileged
    command = runner.call_args.args[0]
    expected = (['/usr/bin/apt-get', '--assume-yes', '--no-remove', '--only-upgrade', 'install', 'editor=2.0'] if manager == 'apt' else
        ['/fixture/dnf', '--assumeyes', 'upgrade', 'editor-0:2.0.x86_64'] if manager == 'dnf' else
        ['/fixture/brew', 'upgrade', '--' + extra['kind'], '--', 'editor'])
    assert command == expected
    assert runner.call_args.kwargs == {'timeout': 900, 'cancel_event': None}


@pytest.mark.parametrize('problem', ['failure', 'unchanged', 'readback_error', 'restart', 'cancelled'])
def test_native_provider_never_claims100_without_actual_verified_postcondition(packages, problem):
    row = packages._record('apt', 'editor', '1', '2')
    packages.manager = 'apt'
    packages.scan = Mock(return_value={'updates': [row]})
    packages.installed = Mock(return_value=[packages._record('apt', 'editor', '1' if problem == 'unchanged' else '2')])
    packages.runner.privileged.return_value = {'exitCode': 1603 if problem == 'failure' else 0,
        'restartNeeded': problem == 'restart', 'cancelled': problem == 'cancelled'}
    if problem == 'readback_error':
        packages.installed.side_effect = OSError('Fixture inventory unavailable')
    events = []
    if problem == 'readback_error':
        with pytest.raises(OSError):
            packages.apply(row['id'], confirmed=True, progress_callback=events.append)
    else:
        packages.apply(row['id'], confirmed=True, progress_callback=events.append)
    assert events[0]['phase'] == 'install' and events[0]['percent'] is None
    assert not any(event['percent'] == 100 for event in events)
    assert packages.runner.privileged.call_count == 1


def test_native_observer_error_cannot_abort_or_replay_and_invalid_observer_is_preflight(packages):
    packages.manager = 'apt'
    row = packages._record('apt', 'editor', '1', '2')
    packages.scan = Mock(return_value={'updates': [row]})
    packages.installed = Mock(return_value=[packages._record('apt', 'editor', '2')])
    result = packages.apply(row['id'], confirmed=True, progress_callback=Mock(side_effect=RuntimeError('Display fault')))
    assert result['verified'] is True and packages.runner.privileged.call_count == 1
    with pytest.raises(ValueError):
        packages.apply(row['id'], confirmed=True, progress_callback=42)
    assert packages.runner.privileged.call_count == 1


def test_native_backend_forwards_exact_observer_without_changing_other_options():
    callback = Mock()
    fixture = SimpleNamespace(native_packages=SimpleNamespace(apply=Mock(return_value={'fixture': True})))
    result = NativeBackend.update_app_unattended(fixture, 'apt:editor', progress_callback=callback)
    assert result == {'fixture': True}
    fixture.native_packages.apply.assert_called_once_with('apt:editor', confirmed=True, cancel_event=None, progress_callback=callback)


def test_native_workflow_resets_item_progress_and_closes_late_callbacks():
    callbacks, events = [], []
    backend = SimpleNamespace(updates_scan=lambda: {'complete': True, 'structured': True, 'exitCode': 0,
        'updates': [{'id': 'apt:alpha', 'version': '1'}, {'id': 'apt:beta', 'version': '1'}]})
    def install(identity, *, cancel_event=None, progress_callback=None):
        callbacks.append(progress_callback)
        progress_callback({'key': 'software:update:' + identity, 'phase': 'install', 'percent': None})
        progress_callback({'phase': 'verify', 'percent': None})
        progress_callback({'phase': 'install', 'percent': 100, 'item_terminal': True})
        return {'exitCode': 0, 'verified': True, 'installed_version': '2', 'installer_started': True,
                'outcomeKnown': True, 'running': False}
    backend.update_app_unattended = install
    settings = {'setup_version': 2, 'software_update_policy': 'selected', 'software_update_agreements_accepted': True,
                'software_update_ids': ['apt:alpha', 'apt:beta'], 'software_update_exclude_ids': []}
    result = run_native_software(backend, settings, item_callback=events.append)
    assert [row['status'] for row in result['items']] == ['completed', 'completed']
    boundaries = [event for event in events if event.get('progress_only') is not True]
    assert [event['current'] for event in boundaries] == [0, 1]
    assert [event['item_progress']['percent'] for event in boundaries] == [None, None]
    assert boundaries[0]['item_progress']['key'] != boundaries[1]['item_progress']['key']
    assert all(event['total'] == 2 for event in events)
    count = len(events)
    for callback in callbacks:
        callback({'phase': 'install', 'percent': 12})
    assert len(events) == count


def test_native_workflow_does_not_retry_mutation_typeerror_without_observer():
    calls = []
    backend = SimpleNamespace(updates_scan=lambda: {'complete': True, 'structured': True, 'exitCode': 0,
        'updates': [{'id': 'apt:alpha', 'version': '1'}, {'id': 'apt:beta', 'version': '1'}]})
    def install(identity, *, cancel_event=None, progress_callback=None):
        calls.append(identity)
        progress_callback({'phase': 'install', 'percent': None})
        raise TypeError('Failure after native mutation entry')
    backend.update_app_unattended = install
    result = run_native_software(backend, {'setup_version': 2, 'software_update_policy': 'selected',
        'software_update_agreements_accepted': True, 'software_update_ids': ['apt:alpha', 'apt:beta'],
        'software_update_exclude_ids': []}, item_callback=lambda _: None)
    assert calls == ['apt:alpha']
    assert result['items'][0]['result']['outcomeKnown'] is False
    assert result['deferred_ids'] == ['apt:beta']
