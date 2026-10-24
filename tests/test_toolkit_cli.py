"""The diagnostic harness cannot enter the interactive or automatic flow."""
import json
import threading
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from optimus import app


@pytest.mark.parametrize('mode,method', [('status', 'toolkit_status'), ('check', 'toolkit_check'),
    ('resume', 'toolkit_resume'), ('integrity-check', 'toolkit_integrity')])
def test_cli_dispatches_only_requested_tool_and_exports_exclusive_report(tmp_path, monkeypatch, capsys, mode, method):
    from optimus import backend
    service = SimpleNamespace(close=Mock(), optimize=Mock(), save_preferences=Mock(),
        toolkit_status=Mock(return_value={'read_only': True}), toolkit_check=Mock(return_value={'read_only': True}),
        toolkit_resume=Mock(return_value={'status': 'no_pending_plan'}), toolkit_integrity=Mock(return_value={'read_only': True}))
    monkeypatch.setattr(app.sys, 'platform', 'win32')
    monkeypatch.setattr(backend, 'Backend', lambda: service)
    monkeypatch.setattr(app, '_run_gui', Mock(side_effect=AssertionError('No GUI or UAC')))
    destination = tmp_path / 'reports'
    assert app.main(['--toolkit', mode, '--output', str(destination)]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report['command'] == 'toolkit-' + mode
    exported = list(destination.glob('toolkit-' + mode + '-*.json'))
    assert len(exported) == 1
    assert json.loads(exported[0].read_text('utf-8'))['result'] == report['result']
    getattr(service, method).assert_called_once_with()
    for other in ('toolkit_status', 'toolkit_check', 'toolkit_resume', 'toolkit_integrity'):
        if other != method:
            getattr(service, other).assert_not_called()
    service.optimize.assert_not_called()
    service.save_preferences.assert_not_called()
    service.close.assert_called_once_with()


def test_cli_error_always_closes_without_retry_or_repair(monkeypatch, capsys):
    from optimus import backend
    service = SimpleNamespace(toolkit_check=Mock(side_effect=RuntimeError('Native inventory unavailable')), close=Mock())
    monkeypatch.setattr(app.sys, 'platform', 'win32')
    monkeypatch.setattr(backend, 'Backend', lambda: service)
    monkeypatch.setattr(app, '_run_gui', Mock(side_effect=AssertionError('No implicit UI retry')))
    assert app.main(['--toolkit', 'check']) == 2
    assert 'inventory unavailable' in capsys.readouterr().err
    service.close.assert_called_once_with()


@pytest.mark.parametrize('status', ['blocked', 'needs_review', 'approval_changed', 'unavailable'])
def test_resume_refusal_is_an_unsuccessful_action_not_a_success_exit(monkeypatch, capsys, status):
    from optimus import backend
    service = SimpleNamespace(toolkit_resume=Mock(return_value={'status': status}), close=Mock())
    monkeypatch.setattr(app.sys, 'platform', 'win32')
    monkeypatch.setattr(backend, 'Backend', lambda: service)
    assert app.main(['--toolkit', 'resume']) == 2
    assert json.loads(capsys.readouterr().out)['result']['status'] == status
    service.close.assert_called_once_with()


@pytest.mark.parametrize('arguments', [ ['--toolkit', 'check', '--continue-updates'],
    ['--toolkit', 'check', '--audit'], ['--toolkit', 'repair'], ['--toolkit', 'check', '--smoke-test'] ])
def test_toolkit_cannot_be_combined_with_another_action_or_headless_repair(arguments):
    with pytest.raises(SystemExit):
        app.parser().parse_args(arguments)


def test_real_worker_cancellation_supports_timed_resource_sampling(tmp_path):
    from optimus.extended_backend import ExtendedMaintenanceBackend, _Cancellation
    from optimus.performance_history import collect_resource_window
    service = ExtendedMaintenanceBackend()
    service.data_dir = tmp_path
    stopped = threading.Event()
    stopped.set()
    result = service._tool_operation('sample', 'Resource check', collect_resource_window,
        1, cancel_event=stopped, _supports_progress=False)
    assert result['complete'] is False
    first, second = threading.Event(), threading.Event()
    combined = _Cancellation(first, second)
    assert combined.wait(0) is False
    second.set()
    assert combined.wait(1) is True
