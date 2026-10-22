"""UAC launch decisions and quoting are mocked; tests never request elevation."""
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch
import pytest

from optimus import elevation


def options(**changes):
    return SimpleNamespace(**dict(dict(audit=False, process_scan=False, smoke_test=False,
        restart_ready_file=None, admin_relaunch=False), **changes))


def backend(**preferences):
    return SimpleNamespace(is_admin=Mock(return_value=False),
                           preferences=Mock(return_value=preferences))


@pytest.mark.parametrize('mode', ('audit', 'process_scan', 'verify_run', 'startup_mode', 'startup_probe', 'smoke_test', 'restart_ready_file'))
def test_noninteractive_or_editor_readiness_start_never_requests_uac(mode):
    service=backend(request_admin_on_startup=True)
    with patch.object(elevation,'request_administrator') as request:
        result=elevation.startup_administrator(service,options(**{mode:True}),[])
    assert result['status']=='skipped'
    service.is_admin.assert_not_called()
    service.preferences.assert_not_called()
    request.assert_not_called()


def test_already_elevated_session_does_not_request_again():
    service=backend(request_admin_on_startup=True);service.is_admin.return_value=True
    with patch.object(elevation,'request_administrator') as request:
        assert elevation.startup_administrator(service,options(),[])['status']=='already_administrator'
    request.assert_not_called()


@pytest.mark.parametrize('value',(False,None,1,'yes'))
def test_only_explicit_true_preference_requests_startup_elevation(value):
    service=backend(request_admin_on_startup=value)
    with patch.object(elevation,'request_administrator') as request:
        result=elevation.startup_administrator(service,options(),[])
    assert result['started'] is False
    request.assert_not_called()


def test_relaunch_flag_prevents_loops_but_does_not_claim_admin_access():
    service=backend(request_admin_on_startup=True)
    with patch.object(elevation,'request_administrator') as request:
        result=elevation.startup_administrator(service,options(admin_relaunch=True),[])
    service.is_admin.assert_called_once_with()
    request.assert_not_called()
    assert result['started'] is False and result['status']=='not_elevated'
    assert 'standard access' in result['note']


def test_normal_gui_requests_uac_once_with_original_arguments():
    service=backend(request_admin_on_startup=True)
    arguments=['--no-orb','--source-workspace',r'C:\Fixture path\workspace']
    with patch.object(elevation,'request_administrator',return_value={'started':True}) as request:
        assert elevation.startup_administrator(service,options(),arguments)['started'] is True
    request.assert_called_once_with(arguments)


def test_frozen_launch_preserves_source_workspace_and_quotes_spaces(monkeypatch):
    monkeypatch.setattr(elevation.sys,'frozen',True,raising=False)
    monkeypatch.setattr(elevation.sys,'executable',r'C:\Optimus folder\Optimus.exe')
    runner=Mock(return_value=42)
    result=elevation.request_administrator(['--source-workspace',r'C:\Fixture path\workspace','--no-orb'],shell_execute=runner)
    assert result['started'] is True
    args=runner.call_args.args
    assert args[1:3]==('runas',r'C:\Optimus folder\Optimus.exe')
    assert args[3]=='--source-workspace "C:\\Fixture path\\workspace" --no-orb --admin-relaunch'
    assert args[4] is None


def test_source_launch_uses_original_launcher_and_drops_previous_readiness_marker(tmp_path,monkeypatch):
    original=tmp_path/'original source';original.mkdir();(original/'launch.py').write_text('# owned fixture')
    monkeypatch.setattr(elevation.sys,'frozen',False,raising=False)
    monkeypatch.setenv('OPTIMUS_ORIGINAL_SOURCE',str(original))
    executable,args=elevation.relaunch_command(['--source-workspace',str(tmp_path/'workspace'),
        '--restart-ready-file',str(tmp_path/'old-ready.json'),'--no-orb','--admin-relaunch'])
    assert executable==elevation.sys.executable
    assert args[:2]==['-B',str(original/'launch.py')]
    assert args[2:4]==['--source-workspace',str(tmp_path/'workspace')]
    assert '--restart-ready-file' not in args and str(tmp_path/'old-ready.json') not in args
    assert args.count('--admin-relaunch')==1
    assert '--no-orb' in args


def test_missing_original_launcher_fails_without_calling_windows(tmp_path,monkeypatch):
    monkeypatch.setattr(elevation.sys,'frozen',False,raising=False)
    monkeypatch.setenv('OPTIMUS_ORIGINAL_SOURCE',str(tmp_path/'missing-original'))
    runner=Mock()
    result=elevation.request_administrator([],shell_execute=runner)
    assert result['started'] is False
    runner.assert_not_called()


@pytest.mark.parametrize('code',(0,2,5,31,32,None))
def test_denied_or_failed_launch_continues_with_standard_access(code,monkeypatch):
    monkeypatch.setattr(elevation.sys,'frozen',True,raising=False)
    result=elevation.request_administrator([],shell_execute=Mock(return_value=code))
    assert result['started'] is False
    assert 'standard access' in result['note']


def test_shell_failure_and_unknown_admin_status_do_not_terminate_startup(monkeypatch):
    monkeypatch.setattr(elevation.sys,'frozen',True,raising=False)
    result=elevation.request_administrator([],shell_execute=Mock(side_effect=OSError('Fixture UAC failure')))
    assert result['started'] is False
    service=backend(request_admin_on_startup=True);service.is_admin.side_effect=OSError('Fixture check failure')
    with patch.object(elevation,'request_administrator') as request:
        result=elevation.startup_administrator(service,options(),[])
    assert result['started'] is False
    request.assert_not_called()


def test_manual_backend_elevation_uses_same_argument_preserving_helper():
    from optimus.backend import Backend
    service=Backend.__new__(Backend)
    with patch.object(elevation,'request_administrator',return_value={'started':True,'code':42}) as request:
        result=service.elevate()
    request.assert_called_once_with()
    assert result['started'] is True


def test_manual_backend_denial_is_reported_without_claiming_launch_success():
    from optimus.backend import Backend
    service=Backend.__new__(Backend)
    with patch.object(elevation,'request_administrator',return_value={'started':False}):
        with pytest.raises(RuntimeError,match='not completed'):
            service.elevate()
