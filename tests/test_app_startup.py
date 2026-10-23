"""GUI startup resumes saved analysis; audit-only execution never starts it."""
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock,patch
import pytest
import optimus.app as app


@pytest.fixture(autouse=True)
def gui_lease(monkeypatch):
    lease = Mock()
    lease.try_claim.return_value = True
    monkeypatch.setattr('optimus.gui_instance.GuiInstance', lambda data_dir: lease)
    priority = Mock()
    priority.apply.return_value = {'status': 'applied'}
    monkeypatch.setattr('optimus.runtime_priority.InterfacePriority', lambda: priority)
    return lease


def test_default_does_not_start_background_analysis():
    backend = SimpleNamespace(preferences=lambda: {}, analysis_start=Mock())
    assert app._resume_saved_analysis(backend) is None
    backend.analysis_start.assert_not_called()


def test_saved_opt_in_resumes_exact_interval():
    backend = SimpleNamespace(preferences=lambda: {'background_analysis_enabled': True, 'analysis_interval': 11}, analysis_start=Mock(return_value={'enabled':True}))
    assert app._resume_saved_analysis(backend) == {'enabled':True}
    backend.analysis_start.assert_called_once_with(11)


def test_explicit_off_does_not_start_background_analysis():
    backend = SimpleNamespace(preferences=lambda: {'background_analysis_enabled': False, 'analysis_interval': 2}, analysis_start=Mock())
    app._resume_saved_analysis(backend)
    backend.analysis_start.assert_not_called()


def test_cli_audit_never_resumes_enabled_analysis(monkeypatch, tmp_path):
    import optimus.backend
    backend = SimpleNamespace(data_dir=tmp_path, preferences=lambda: {'background_analysis_enabled':True,'analysis_interval':5}, analysis_start=Mock(), export_report=Mock(return_value={'path':'fixture-only'}), close=Mock())
    monkeypatch.setattr(optimus.backend, 'Backend', lambda: backend)
    monkeypatch.setattr(app.sys, 'platform', 'win32')
    assert app.main(['--audit','--output',str(tmp_path / 'report')]) == 0
    backend.analysis_start.assert_not_called()
    backend.export_report.assert_called_once_with(tmp_path / 'report')
    backend.close.assert_called_once()


def test_successful_startup_admin_launch_exits_parent_before_any_gui(monkeypatch, tmp_path):
    import optimus.backend
    backend=SimpleNamespace(data_dir=tmp_path, close=Mock())
    monkeypatch.setattr(optimus.backend,'Backend',lambda:backend)
    monkeypatch.setattr(app.sys,'platform','win32')
    with patch('optimus.elevation.startup_administrator',return_value={'started':True}) as request:
        assert app.main(['--no-orb'])==0
    backend.close.assert_called_once_with()
    assert request.call_args.args[2]==['--no-orb']


def test_denied_startup_elevation_still_shows_gui_and_explains_access(monkeypatch, tmp_path):
    import optimus.backend
    backend=SimpleNamespace(data_dir=tmp_path, preferences=lambda:{},close=Mock())
    monkeypatch.setattr(optimus.backend,'Backend',lambda:backend)
    monkeypatch.setattr(app.sys,'platform','win32')
    monkeypatch.setattr(app,'ctypes',SimpleNamespace(windll=SimpleNamespace(shell32=Mock())))
    gui,window,controller=Mock(),Mock(),Mock()
    gui.exec.return_value=0
    note='Administrator permission was declined. Continuing with standard access.'
    with patch('optimus.elevation.startup_administrator',return_value={'started':False,'note':note}),\
         patch('PySide6.QtWidgets.QApplication',return_value=gui),\
         patch('PySide6.QtCore.QTimer'),patch('PySide6.QtGui.QIcon'),\
         patch('optimus.ui.OptimusWindow',return_value=window),\
         patch('optimus.gui_startup.SetupAutomationController',return_value=controller):
        assert app.main(['--no-orb'])==0
    window.show.assert_called_once_with()
    window.status.setText.assert_called_with(note)
    gui.exec.assert_called_once_with()


def test_restart_readiness_is_a_real_session_but_waits_for_new_launch_review(monkeypatch, tmp_path):
    import optimus.backend
    backend = SimpleNamespace(data_dir=tmp_path,
        preferences=Mock(return_value={'background_analysis_enabled':True,'analysis_interval':9,'request_admin_on_startup':True}),
        analysis_start=Mock(), is_admin=Mock(), close=Mock())
    monkeypatch.setattr(optimus.backend, 'Backend', lambda: backend)
    monkeypatch.setattr(app.sys, 'platform', 'win32')
    monkeypatch.setattr(app, 'ctypes', SimpleNamespace(windll=SimpleNamespace(shell32=Mock())))
    gui, window, controller = Mock(), Mock(), Mock()
    gui.exec.return_value = 0
    marker = tmp_path / 'Sandbox' / 'restart' / 'fixture.json'
    with patch('PySide6.QtWidgets.QApplication', return_value=gui), \
         patch('PySide6.QtCore.QTimer') as timer, patch('PySide6.QtGui.QIcon'), \
         patch('optimus.ui.OptimusWindow', return_value=window) as window_factory, \
         patch('optimus.gui_startup.SetupAutomationController', return_value=controller) as controller_factory, \
         patch('optimus.elevation.request_administrator') as request:
        assert app.main(['--restart-ready-file', str(marker)]) == 0
    request.assert_not_called()
    backend.is_admin.assert_not_called()
    backend.analysis_start.assert_not_called()
    window_factory.assert_called_once_with(backend, smoke_test=False, require_launch_review=True)
    controller_factory.assert_called_once()
    assert [call.args[0] for call in timer.singleShot.call_args_list] == [750,0]
    controller.stop.assert_called_once_with()
    timer.return_value.start.assert_not_called()
    assert not marker.exists()


def test_explicit_gui_probe_never_resumes_analysis_setup_cadence_or_uac(monkeypatch, tmp_path, capsys):
    import optimus.backend
    backend = SimpleNamespace(data_dir=tmp_path,
        preferences=Mock(side_effect=AssertionError('Probe must not read opt-ins to start work.')),
        analysis_start=Mock(), is_admin=Mock(), optimize=Mock(), close=Mock())
    monkeypatch.setattr(optimus.backend, 'Backend', lambda: backend)
    monkeypatch.setattr(app.sys, 'platform', 'win32')
    monkeypatch.setattr(app, 'ctypes', SimpleNamespace(windll=SimpleNamespace(shell32=Mock())))
    gui, window = Mock(), Mock()
    gui.exec.return_value = 0
    window.windowTitle.return_value = 'Optimus: Computer optimization tool'
    window.width.return_value, window.height.return_value = 1180,780
    with patch('PySide6.QtWidgets.QApplication', return_value=gui), \
         patch('PySide6.QtCore.QTimer') as timer, patch('PySide6.QtGui.QIcon'), \
         patch('optimus.ui.OptimusWindow', return_value=window) as window_factory, \
         patch('optimus.gui_startup.SetupAutomationController') as controller, \
         patch('optimus.elevation.request_administrator') as request:
        assert app.main(['--startup-probe']) == 0
        assert timer.singleShot.call_count == 1
        delay, inspect = timer.singleShot.call_args.args
        assert delay == 750
        inspect()
    request.assert_not_called()
    backend.is_admin.assert_not_called()
    backend.preferences.assert_not_called()
    backend.analysis_start.assert_not_called()
    backend.optimize.assert_not_called()
    controller.assert_not_called()
    window_factory.assert_called_once_with(backend, smoke_test=True)
    window.setEnabled.assert_called_once_with(False)
    window.open_initial_setup.assert_not_called()
    gui.quit.assert_called_once_with()
    import json
    report = json.loads(capsys.readouterr().out)
    assert report['status'] == 'passed' and report['read_only'] is True and report['mutations'] == 0


@pytest.mark.parametrize('stop_fails', [False, True])
def test_normal_gui_starts_saved_runtime_only_after_approval_and_rechecks_each_tick(monkeypatch, tmp_path, stop_fails):
    import optimus.backend
    backend = SimpleNamespace(data_dir=tmp_path,
        preferences=Mock(return_value={'background_analysis_enabled':True,'analysis_interval':9}),
        analysis_start=Mock(), analysis_stop=Mock(), activate_smart_maintenance=Mock(),
        smart_maintenance_stop=Mock(), close=Mock())
    if stop_fails:
        backend.smart_maintenance_stop.side_effect = RuntimeError('fixture monitor stop failed')
    monkeypatch.setattr(optimus.backend, 'Backend', lambda:backend)
    monkeypatch.setattr(app.sys, 'platform', 'win32')
    monkeypatch.setattr(app, 'ctypes', SimpleNamespace(windll=SimpleNamespace(shell32=Mock())))
    gui, window, controller = Mock(), Mock(), Mock()
    window.session_approved.return_value = True
    window._quitting=window._close_when_idle=window.busy=False
    window._setup_wizard=window._settings_window=None
    with patch('PySide6.QtWidgets.QApplication', return_value=gui), \
         patch('PySide6.QtCore.QTimer') as timer, patch('PySide6.QtGui.QIcon'), \
         patch('optimus.ui.OptimusWindow', return_value=window), \
         patch('optimus.gui_startup.SetupAutomationController', return_value=controller):
        def event_loop():
            backend.analysis_start.assert_not_called(); backend.activate_smart_maintenance.assert_not_called()
            timer.return_value.start.assert_not_called()
            assert window._startup_update_pending is True
            window._session_approved_callback()
            backend.analysis_start.assert_not_called()
            backend.activate_smart_maintenance.assert_not_called()
            window._startup_update_pending = False  # The asynchronous startup update gate has finished.
            window._session_approved_callback()
            backend.analysis_start.assert_called_once_with(9)
            busy_reader = backend.activate_smart_maintenance.call_args.args[0]
            assert busy_reader() is False
            automatic_tick = timer.return_value.timeout.connect.call_args.args[0]
            window.session_approved.return_value=False
            assert busy_reader() is True
            automatic_tick()
            controller.tick.assert_not_called()
            window.invalidate_session_review.assert_called_once()
            note = window._session_invalidated_callback()
            backend.smart_maintenance_stop.assert_called_once_with(wait=False)
            backend.analysis_stop.assert_called_once_with()
            assert ('fixture monitor stop failed' in note) is stop_fails
            return 0
        gui.exec.side_effect = event_loop
        assert app.main([])==0


def test_smoke_test_remains_read_only_without_saved_background_actions(monkeypatch):
    import optimus.backend
    backend = SimpleNamespace(preferences=Mock(),analysis_start=Mock(),is_admin=Mock(),close=Mock())
    monkeypatch.setattr(optimus.backend, 'Backend', lambda: backend)
    monkeypatch.setattr(app.sys, 'platform', 'win32')
    monkeypatch.setattr(app, 'ctypes', SimpleNamespace(windll=SimpleNamespace(shell32=Mock())))
    gui, window = Mock(), Mock()
    gui.exec.return_value = 0
    with patch('PySide6.QtWidgets.QApplication', return_value=gui), \
         patch('PySide6.QtCore.QTimer') as timer, patch('PySide6.QtGui.QIcon'), \
         patch('optimus.ui.OptimusWindow', return_value=window) as window_factory, \
         patch('optimus.gui_startup.SetupAutomationController') as controller, \
         patch('optimus.elevation.request_administrator') as request:
        assert app.main(['--smoke-test']) == 0
    request.assert_not_called()
    backend.is_admin.assert_not_called()
    backend.preferences.assert_not_called()
    backend.analysis_start.assert_not_called()
    controller.assert_not_called()
    window_factory.assert_called_once_with(backend, smoke_test=True)
    assert timer.singleShot.call_args.args[0] == 11000
