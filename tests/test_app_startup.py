"""GUI startup resumes saved analysis; audit-only execution never starts it."""
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock,patch
import optimus.app as app


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


def test_successful_startup_admin_launch_exits_parent_before_any_gui(monkeypatch):
    import optimus.backend
    backend=SimpleNamespace(close=Mock())
    monkeypatch.setattr(optimus.backend,'Backend',lambda:backend)
    monkeypatch.setattr(app.sys,'platform','win32')
    with patch('optimus.elevation.startup_administrator',return_value={'started':True}) as request:
        assert app.main(['--no-orb'])==0
    backend.close.assert_called_once_with()
    assert request.call_args.args[2]==['--no-orb']


def test_denied_startup_elevation_still_shows_gui_and_explains_access(monkeypatch):
    import optimus.backend
    backend=SimpleNamespace(preferences=lambda:{},close=Mock())
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
