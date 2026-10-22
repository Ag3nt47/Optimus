"""Headless verification/configuration must never enter GUI or maintenance."""
import json
import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from optimus import app

RUN = '1234567890abcdef1234567890abcdef'


def completed(data, **changes):
    runs = data / 'runs'
    runs.mkdir(exist_ok=True)
    report = dict(id=RUN, status='completed_with_errors', finished_at=1234, stages=[])
    report.update(changes)
    (runs / (RUN + '.json')).write_text(json.dumps(report), encoding='utf-8')
    return report


def test_saved_verification_exports_findings_without_replaying_work(tmp_path):
    original = completed(tmp_path)
    findings = dict(status='needs_review', counts={'needs_review': 1}, coverage_complete=True,
                    read_only=True, stages=[])
    service = SimpleNamespace(data_dir=tmp_path, verify_optimization=Mock(return_value=findings),
                              optimize=Mock(), cleanup_apply=Mock())
    result = app._verify_saved_run(service, RUN, tmp_path / 'export')
    saved = json.loads(__import__('pathlib').Path(result['report']).read_text())
    assert saved == {'schema': 1, 'source_run_id': RUN, 'verification': findings}
    assert result['status'] == 'needs_review' and result['read_only'] is True
    service.verify_optimization.assert_called_once_with(original)
    service.optimize.assert_not_called()
    service.cleanup_apply.assert_not_called()
    assert json.loads((tmp_path / 'runs' / (RUN + '.json')).read_text()) == original


@pytest.mark.parametrize('changes', ({'status': 'running'}, {'finished_at': None}, {'id': 'a' * 32}))
def test_unfinished_or_substituted_journal_is_not_verified(tmp_path, changes):
    completed(tmp_path, **changes)
    service = SimpleNamespace(data_dir=tmp_path, verify_optimization=Mock())
    with pytest.raises(ValueError):
        app._verify_saved_run(service, RUN, tmp_path / 'export')
    service.verify_optimization.assert_not_called()
    assert not (tmp_path / 'export').exists()


@pytest.mark.parametrize('value', ('../preferences', 'A' * 32, 'a' * 33, ''))
def test_run_ids_cannot_be_paths_or_ambiguous_identifiers(value):
    with pytest.raises(SystemExit):
        app.parser().parse_args(['--verify-run', value])


def test_cli_commands_are_mutually_exclusive():
    with pytest.raises(SystemExit):
        app.parser().parse_args(['--verify-run', RUN, '--startup-mode', 'on'])


def test_linked_saved_journal_is_not_accepted(tmp_path):
    completed(tmp_path)
    os.link(tmp_path/'runs'/(RUN+'.json'),tmp_path/'linked.json')
    service=SimpleNamespace(data_dir=tmp_path,verify_optimization=Mock())
    with pytest.raises(ValueError,match='hardlinks'):
        app._verify_saved_run(service,RUN)
    service.verify_optimization.assert_not_called()


def test_indirect_run_directory_is_not_followed(tmp_path,monkeypatch):
    completed(tmp_path)
    original=Path.lstat
    def lstat(path,*args,**kwargs):
        value=original(path,*args,**kwargs)
        return SimpleNamespace(st_mode=value.st_mode,st_file_attributes=0x400) if path==tmp_path/'runs' else value
    monkeypatch.setattr(Path,'lstat',lstat)
    service=SimpleNamespace(data_dir=tmp_path,verify_optimization=Mock())
    with pytest.raises(ValueError,match='Indirect'):
        app._verify_saved_run(service,RUN)
    service.verify_optimization.assert_not_called()


@pytest.mark.parametrize('mode,expected', (('on', True), ('off', False), ('status', None)))
def test_startup_cli_uses_backend_transaction_and_always_closes(tmp_path, monkeypatch, mode, expected):
    from optimus import backend
    service = SimpleNamespace(data_dir=tmp_path, preferences=Mock(return_value={'existing': 123}),
                              save_preferences=Mock(), set_startup_launch=Mock(), startup_launch_status=Mock(return_value={'enabled': expected}),
                              close=Mock(), optimize=Mock())
    monkeypatch.setattr(app.sys, 'platform', 'win32')
    monkeypatch.setattr(backend, 'Backend', lambda: service)
    assert app.main(['--startup-mode', mode]) == 0
    if expected is None:
        service.set_startup_launch.assert_not_called()
    else:
        service.set_startup_launch.assert_called_once_with(expected)
    service.save_preferences.assert_not_called()
    service.preferences.assert_not_called()
    service.startup_launch_status.assert_called_once_with()
    service.optimize.assert_not_called()
    service.close.assert_called_once_with()


def test_verification_cli_returns_before_uac_gui_and_maintenance(tmp_path, monkeypatch):
    from optimus import backend, elevation
    completed(tmp_path)
    service = SimpleNamespace(data_dir=tmp_path,
        verify_optimization=Mock(return_value={'status': 'unverified', 'counts': {'unverified': 1}}),
        close=Mock(), optimize=Mock())
    request = Mock(side_effect=AssertionError('CLI must not request UAC'))
    monkeypatch.setattr(elevation, 'startup_administrator', request)
    monkeypatch.setattr(app.sys, 'platform', 'win32')
    monkeypatch.setattr(backend, 'Backend', lambda: service)
    assert app.main(['--verify-run', RUN]) == 0
    assert len(list((tmp_path / 'VerificationReports').glob('verification-*.json'))) == 1
    request.assert_not_called()
    service.optimize.assert_not_called()
    service.close.assert_called_once_with()


@pytest.mark.parametrize('diagnostic', ['--startup-probe','--smoke-test'])
@pytest.mark.parametrize('command', (['--audit'], ['--process-scan'], ['--startup-mode','on'], ['--verify-run',RUN]))
def test_read_only_gui_diagnostics_cannot_combine_with_cli_commands(diagnostic, command):
    with pytest.raises(SystemExit):
        app.parser().parse_args([diagnostic,*command])
