"""Workers discover concurrently; every host mutation in this suite is mocked."""
import copy
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier, Event
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from optimus.automation import CURRENT_SETUP_VERSION
from optimus.automated_software import run_software
from optimus.automated_system import driver_descriptor, run_drivers


def package(identity='Fixture.One', **changes):
    value = dict(id=identity, package_id=identity, name=identity, source='winget', version='1.0', available='2.0')
    value.update(changes)
    return value


def software_scan(*records):
    return dict(exitCode=0, structured=True, source='winget', updates=list(records))


def driver(**changes):
    value = dict(id='11111111-1111-4111-8111-111111111111', revision=1, title='Fixture driver',
        hardwareId='PCI\\VEN_FFFF&DEV_1234', provider='Fixture', driverClass='Display',
        driverDate='2026-10-01', canRequestUserInput=False, rebootBehavior=2)
    value.update(changes)
    return value


def fixture(kind):
    if kind == 'software':
        backend = SimpleNamespace(updates_scan=Mock(return_value=software_scan(package())),
            update_app_unattended=Mock(return_value=dict(exitCode=0, installer_started=True, outcomeKnown=True)))
        config = dict(software_updates_enabled=True, software_update_policy='all_supported',
            software_update_all_approved=True, software_update_agreements_accepted=True,
            software_update_ids=[], software_update_exclude_ids=[], update_compatibility_mode_enabled=False)
        return run_software, backend, config, backend.update_app_unattended
    backend = SimpleNamespace(driver_updates_scan=Mock(return_value=dict(resultCode=2, updates=[driver()])),
        driver_backup_automatic=Mock(return_value=dict(exit_code=0, destination='fixture-backup')),
        driver_update_apply_unattended=Mock(return_value=dict(resultCode=2, hresult=0, aggregateResultCode=2, aggregateHresult=0,
            installer_started=True, outcomeKnown=True, running=False, restartNeeded=False)),
        is_admin=Mock(return_value=True), update_disk_readiness=Mock(return_value=dict(ready=True)))
    config = dict(setup_version=CURRENT_SETUP_VERSION, driver_updates_enabled=True,
        driver_policy='install_approved', driver_changes_approved=True, driver_update_agreements_accepted=True,
        driver_approved_updates=[driver_descriptor(driver())], driver_approved_devices=[])
    return run_drivers, backend, config, backend.driver_update_apply_unattended


@pytest.mark.parametrize('kind', ['software', 'driver'])
def test_discovery_completes_before_foreground_gate_and_no_mutation_while_waiting(kind):
    run, backend, config, install = fixture(kind)
    ready, release = Event(), Event()
    notifications = []
    def prepared(state):
        notifications.append(state)
        ready.set()
    def gate(state):
        assert state['candidate_count'] == 1 and state['review_only'] is False
        assert release.wait(5)
        return True
    with ThreadPoolExecutor(max_workers=1) as pool:
        task = pool.submit(run, backend, config, prepared_callback=prepared, before_install=gate)
        try:
            assert ready.wait(5)
            install.assert_not_called()
            if kind == 'driver':
                backend.driver_updates_scan.assert_called_once()
                backend.driver_backup_automatic.assert_not_called()
                backend.is_admin.assert_not_called()
                backend.update_disk_readiness.assert_not_called()
            else:
                backend.updates_scan.assert_called_once()
        finally:
            release.set()
        result = task.result(timeout=5)
    assert len(notifications) == 1
    assert notifications[0] == dict(operation=kind, scan_complete=True, candidate_count=1, review_only=False)
    assert result['failed_items'] == 0
    install.assert_called_once()


def test_software_and_driver_discovery_can_enter_before_either_is_released():
    both_scanning = Barrier(2)
    both_prepared, release = [Event(), Event()], Event()
    pending = []
    with ThreadPoolExecutor(max_workers=2) as pool:
        for index, kind in enumerate(('software', 'driver')):
            run, backend, config, install = fixture(kind)
            scan = backend.updates_scan if kind == 'software' else backend.driver_updates_scan
            initial = copy.deepcopy(scan.return_value)
            count = [0]
            def scan_now(initial=initial, count=count):
                count[0] += 1
                if count[0] == 1:
                    both_scanning.wait(timeout=5)
                return copy.deepcopy(initial)
            scan.side_effect = scan_now
            def gate(state):
                assert release.wait(5)
                return True
            task = pool.submit(run, backend, config, prepared_callback=lambda state, index=index: both_prepared[index].set(), before_install=gate)
            pending.append((task, backend, install))
        try:
            assert all(event.wait(5) for event in both_prepared)
            for _, backend, install in pending:
                install.assert_not_called()
                if hasattr(backend, 'driver_backup_automatic'):
                    backend.driver_backup_automatic.assert_not_called()
        finally:
            release.set()
        assert all(task.result(timeout=5)['failed_items'] == 0 for task, _, _ in pending)


@pytest.mark.parametrize('kind', ['software', 'driver'])
@pytest.mark.parametrize('allowed', [False, None])
def test_gate_refusal_never_starts_backup_or_installer(kind, allowed):
    run, backend, config, install = fixture(kind)
    prepared, gate = Mock(), Mock(return_value=allowed)
    result = run(backend, config, prepared_callback=prepared, before_install=gate)
    prepared.assert_called_once()
    gate.assert_called_once()
    install.assert_not_called()
    assert result['installer_started'] is False and result['outcomeKnown'] is True
    if kind == 'driver':
        backend.driver_backup_automatic.assert_not_called()


@pytest.mark.parametrize('kind', ['software', 'driver'])
@pytest.mark.parametrize('phase', ['prepared', 'gate'])
def test_cancellation_at_either_preparation_boundary_prevents_all_mutations(kind, phase):
    run, backend, config, install = fixture(kind)
    cancel = Event()
    def prepared(state):
        if phase == 'prepared':
            cancel.set()
    def gate(state):
        cancel.set()
        return True
    result = run(backend, config, cancel_event=cancel, prepared_callback=prepared, before_install=gate)
    assert result['cancelled']
    install.assert_not_called()
    if kind == 'driver':
        backend.driver_backup_automatic.assert_not_called()


@pytest.mark.parametrize('kind', ['software', 'driver'])
def test_approval_revoked_during_wait_is_not_applied(kind):
    run, backend, config, install = fixture(kind)
    def gate(state):
        config['software_update_agreements_accepted' if kind == 'software' else 'driver_changes_approved'] = False
        return True
    result = run(backend, config, before_install=gate)
    install.assert_not_called()
    assert result['installer_started'] is False and result['outcomeKnown'] is True
    assert 'approvals changed' in result.get('stop_reason', '')
    if kind == 'driver':
        backend.driver_backup_automatic.assert_not_called()


@pytest.mark.parametrize('kind', ['software', 'driver'])
@pytest.mark.parametrize('mode', ['off', 'review', 'empty', 'failed_scan', 'scan_exception', 'invalid_approval'])
def test_preparation_signals_once_on_every_non_installing_path(kind, mode):
    run, backend, config, install = fixture(kind)
    scan = backend.updates_scan if kind == 'software' else backend.driver_updates_scan
    if mode == 'off':
        config['software_updates_enabled' if kind == 'software' else 'driver_updates_enabled'] = False
    elif mode == 'review':
        config['software_update_policy' if kind == 'software' else 'driver_policy'] = 'review'
    elif mode == 'empty':
        scan.return_value = software_scan() if kind == 'software' else dict(resultCode=2, updates=[])
    elif mode == 'failed_scan':
        scan.return_value = dict(exitCode=4, structured=False, updates=[]) if kind == 'software' else dict(resultCode=4, updates=[])
    elif mode == 'scan_exception':
        scan.side_effect = RuntimeError('fixture scan error')
    else:
        config['software_update_agreements_accepted' if kind == 'software' else 'driver_changes_approved'] = False
    prepared, gate = Mock(), Mock(return_value=True)
    if mode in ('scan_exception', 'invalid_approval'):
        with pytest.raises((ValueError, RuntimeError)):
            run(backend, config, prepared_callback=prepared, before_install=gate)
    else:
        run(backend, config, prepared_callback=prepared, before_install=gate)
    prepared.assert_called_once()
    gate.assert_not_called()
    install.assert_not_called()
    if kind == 'driver':
        backend.driver_backup_automatic.assert_not_called()


@pytest.mark.parametrize('kind', ['software', 'driver'])
def test_prepared_callback_exception_does_not_repeat_notification_or_mutate(kind):
    run, backend, config, install = fixture(kind)
    prepared, gate = Mock(side_effect=RuntimeError('fixture observer failed')), Mock(return_value=True)
    with pytest.raises(RuntimeError, match='observer failed'):
        run(backend, config, prepared_callback=prepared, before_install=gate)
    prepared.assert_called_once()
    gate.assert_not_called()
    install.assert_not_called()


def test_driver_install_readiness_is_fresh_after_foreground_gate():
    run, backend, config, install = fixture('driver')
    def gate(state):
        backend.update_disk_readiness.return_value = dict(ready=False, reason='fixture low space')
        return True
    result = run(backend, config, before_install=gate)
    backend.driver_updates_scan.assert_called_once()
    backend.update_disk_readiness.assert_called_once()
    backend.driver_backup_automatic.assert_not_called()
    install.assert_not_called()
    assert result['outcomeKnown'] is True


def test_driver_without_elevation_still_prepares_present_candidates_without_installing():
    run, backend, config, install = fixture('driver')
    backend.is_admin.return_value = False
    prepared = Mock()
    result = run(backend, config, prepared_callback=prepared, before_install=lambda state: True)
    backend.driver_updates_scan.assert_called_once()
    assert prepared.call_args.args[0]['candidate_count'] == 1
    install.assert_not_called()
    backend.driver_backup_automatic.assert_not_called()
    assert result['outcomeKnown'] is True


@pytest.mark.parametrize('failure', ['scan', 'backup'])
def test_driver_discovery_or_backup_failure_is_not_a_live_installer(failure):
    run, backend, config, install = fixture('driver')
    if failure == 'scan':
        backend.driver_updates_scan.return_value = dict(resultCode=4, updates=[])
    else:
        backend.driver_backup_automatic.return_value = dict(exit_code=1, destination='fixture-backup')
    result = run(backend, config, before_install=lambda state: True)
    install.assert_not_called()
    assert result['failed_items'] == 1
    assert result['installer_started'] is False and result['outcomeKnown'] is True
    assert result['items'][-1]['installer_started'] is False and result['items'][-1]['outcomeKnown'] is True


@pytest.mark.parametrize('changes', [dict(version='1.1'), dict(available='2.1'), dict(source='msstore'), dict(id='fixture.one', package_id='fixture.one')])
def test_post_gate_refresh_cannot_widen_the_prepared_exact_software_version_pair(changes):
    run, backend, config, install = fixture('software')
    backend.updates_scan.side_effect = [software_scan(package()), software_scan(package(**changes), package('Fixture.New'))]
    result = run(backend, config, before_install=lambda state: True)
    install.assert_not_called()
    assert result['items'][0]['id'] == 'Fixture.One'
    assert result['items'][0]['result']['failureKind'] == 'version_changed'
    assert result['items'][0]['result']['installer_started'] is False


def test_new_software_appearing_during_gate_is_not_silently_enrolled():
    run, backend, config, install = fixture('software')
    backend.updates_scan.side_effect = [software_scan(package()), software_scan(package(), package('Fixture.New'))]
    result = run(backend, config, before_install=lambda state: True)
    install.assert_called_once_with('Fixture.One')
    assert [item['id'] for item in result['items']] == ['Fixture.One']


@pytest.mark.parametrize('kind', ['software', 'driver'])
def test_approval_revoked_from_item_progress_prevents_install(kind):
    run, backend, config, install = fixture(kind)
    def item(state):
        config['software_updates_enabled' if kind == 'software' else 'driver_updates_enabled'] = False
    result = run(backend, config, item_callback=item, before_install=lambda state: True)
    install.assert_not_called()
    assert result['installer_started'] is False and result['outcomeKnown'] is True
