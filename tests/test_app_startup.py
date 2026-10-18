"""GUI startup resumes saved analysis; audit-only execution never starts it."""
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock
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
