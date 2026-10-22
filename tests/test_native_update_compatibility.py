"""Non-security force mode preserves exact packages, pins, cancellation and readback."""
import copy
import json
import subprocess
import time
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from optimus.automated_software import run_software
from optimus.backend import Backend
from optimus.maintenance import Maintenance
from optimus.native_settings import NativePreferences
from optimus.workflow import DEFAULTS


def tool(tmp_path):
    value = Maintenance(Mock(), tmp_path)
    row = {'id': 'Fixture.App', 'name': 'Fixture App', 'version': '1.0',
           'available': '2.0', 'source': 'winget'}
    value._last_update_scan = time.time()
    value._last_update_packages = {'Fixture.App': row}
    value._last_update_ids = {'Fixture.App'}
    value.updates_scan = Mock(return_value={'structured': True, 'exitCode': 0, 'updates': [row]})
    value._winget = Mock(return_value='fixture-winget.exe')
    value._pin_status = Mock(return_value=(True, '', False))
    value._run = Mock(return_value=subprocess.CompletedProcess([], 0, 'Fixture updated', ''))
    return value


@pytest.mark.parametrize('enabled', [False, True])
def test_only_explicit_mode_adds_one_non_security_force_flag(tmp_path, enabled):
    service = tool(tmp_path)
    result = service.updates_apply('Fixture.App', confirmed=True, unattended=True, compatibility_mode=enabled)
    argv = service._run.call_args.args[0]
    assert argv.count('--force') == int(enabled)
    assert argv[argv.index('--id') + 1] == 'Fixture.App'
    assert argv[argv.index('--version') + 1] == '2.0'
    assert argv[argv.index('--source') + 1] == 'winget'
    assert '--exact' in argv and '--silent' in argv
    assert not {'--ignore-security-hash', '--ignore-local-archive-malware-scan', '--include-pinned',
                '--allow-reboot', '--include-unknown', '--override'} & set(argv)
    assert result['compatibility_mode'] is enabled
    assert service.updates_scan.call_count == service._pin_status.call_count == service._run.call_count == 1


def test_force_mode_still_refuses_pin_and_changed_version(tmp_path):
    service = tool(tmp_path)
    service._pin_status.return_value = (False, 'Fixture pin is preserved.', False)
    result = service.updates_apply('Fixture.App', confirmed=True, compatibility_mode=True)
    assert result['failureKind'] == 'pinned' and result['skipped']
    service._run.assert_not_called()
    service = tool(tmp_path)
    def changed_offer():
        service._last_update_packages['Fixture.App'].update(available='3.0')
        return {'structured': True, 'exitCode': 0, 'updates': list(service._last_update_packages.values())}
    service.updates_scan.side_effect = changed_offer
    result = service.updates_apply('Fixture.App', confirmed=True, compatibility_mode=True)
    assert result['failureKind'] == 'version_changed'
    service._run.assert_not_called()


def test_timeout_never_replays_the_installer_with_force(tmp_path):
    service = tool(tmp_path)
    service._run.side_effect = subprocess.TimeoutExpired('fixture', 1800)
    with pytest.raises(RuntimeError, match='may still be running'):
        service.updates_apply('Fixture.App', confirmed=True, compatibility_mode=True)
    service._run.assert_called_once()


def test_backend_revoked_compatibility_setting_defers_before_installer(tmp_path):
    service = Backend.__new__(Backend)
    service.update_disk_readiness = Mock(return_value={'ready': True})
    service.preferences = Mock(return_value={'setup_version': 2, 'update_compatibility_mode_enabled': False})
    service.maintenance = Mock()
    result = service.update_app_unattended('Fixture.App', compatibility_mode=True)
    assert result['status'] == 'deferred' and result['requiresReview']
    service.maintenance.updates_apply.assert_not_called()


@pytest.mark.parametrize('restart', [True, None])
def test_forced_installer_defers_when_fresh_restart_state_is_pending_or_unknown(restart):
    service = Backend.__new__(Backend)
    service.update_disk_readiness = Mock(return_value={'ready': True})
    service.preferences = Mock(return_value={'setup_version': 2, 'update_compatibility_mode_enabled': True})
    service.security_status = Mock(return_value={'reboot_required': restart})
    service.maintenance = Mock()
    result = service.update_app_unattended('Fixture.App', compatibility_mode=True)
    assert result['status'] == 'deferred' and result['requiresReview']
    service.maintenance.updates_apply.assert_not_called()


def test_main_workflow_defers_software_after_prior_driver_requests_restart(tmp_path, monkeypatch):
    from test_workflow import MockBackend
    from optimus.workflow import Workflow
    backend = MockBackend(tmp_path)
    backend.driver_updates_scan = Mock(return_value={'resultCode': 2, 'updates': [], 'restartNeeded': True})
    updater = Mock(side_effect=AssertionError('No app installer may follow the prior driver restart.'))
    monkeypatch.setattr('optimus.automated_software.run_software', updater)
    configuration = dict(copy.deepcopy(DEFAULTS), setup_version=2, cleanup_enabled=False,
                         browser_cache_enabled=False, driver_updates_enabled=True, software_updates_enabled=True)
    report = Workflow(backend).run(configuration, lambda event: None)
    updater.assert_not_called()
    software = next(item for item in report['stages'] if item['title'] == 'Review or update configured software')
    assert software['result']['requiresReview'] and software['result']['items'] == []


def test_saved_mode_flows_only_to_windows_scoped_update_service():
    settings = dict(copy.deepcopy(DEFAULTS), setup_version=2, software_updates_enabled=True,
        software_update_policy='selected', software_update_ids=['Fixture.App'],
        software_update_agreements_accepted=True, update_compatibility_mode_enabled=True)
    backend = SimpleNamespace(updates_scan=Mock(return_value={'structured': True, 'exitCode': 0,
        'source': 'winget', 'updates': [{'id': 'Fixture.App', 'package_id': 'Fixture.App',
            'name': 'Fixture App', 'source': 'winget', 'version': '1.0', 'available': '2.0'}]}),
        update_app_unattended=Mock(return_value={'exitCode': 0}))
    result = run_software(backend, settings)
    backend.update_app_unattended.assert_called_once_with('Fixture.App', compatibility_mode=True)
    assert result['compatibility_mode'] is True
    assert NativePreferences.validate(dict(DEFAULTS, update_compatibility_mode_enabled=True,
        windows_cpu_compatibility_enabled=True))['update_compatibility_mode_enabled'] is False


@pytest.mark.parametrize('mode,method', [('status', 'windows_compatibility_status'),
                                       ('prepare', 'prepare_windows_compatibility')])
def test_compatibility_cli_never_starts_an_os_upgrade_or_gui(monkeypatch, capsys, mode, method):
    from optimus import app, backend
    service = SimpleNamespace(windows_compatibility_status=Mock(return_value={'read_only': True}),
        prepare_windows_compatibility=Mock(return_value={'status': 'deferred'}),
        close=Mock(), optimize=Mock(), save_preferences=Mock())
    monkeypatch.setattr(backend, 'Backend', lambda: service)
    monkeypatch.setattr(app.sys, 'platform', 'win32')
    gui = Mock(side_effect=AssertionError('No GUI or automatic work in this command.'))
    monkeypatch.setattr(app, '_run_gui', gui)
    assert app.main(['--windows-compatibility', mode]) == 0
    assert json.loads(capsys.readouterr().out) == getattr(service, method).return_value
    getattr(service, method).assert_called_once_with()
    service.close.assert_called_once_with()
    service.optimize.assert_not_called()
    service.save_preferences.assert_not_called()
    gui.assert_not_called()


@pytest.mark.parametrize('platform', ['darwin', 'linux'])
def test_windows_compatibility_cli_is_unavailable_on_native_hosts(monkeypatch, capsys, platform):
    from optimus import app, native_backend
    service = SimpleNamespace(close=Mock(), prepare_windows_compatibility=Mock())
    monkeypatch.setattr(native_backend, 'NativeBackend', lambda: service)
    monkeypatch.setattr(app.sys, 'platform', platform)
    assert app.main(['--windows-compatibility', 'prepare']) == 2
    assert json.loads(capsys.readouterr().err)['status'] == 'error'
    service.prepare_windows_compatibility.assert_not_called()
    service.close.assert_called_once_with()
