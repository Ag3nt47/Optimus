"""Ready-checked restart contracts. Popen, timers and application exit are mocked."""
from __future__ import annotations
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock
import pytest
from PySide6.QtWidgets import QApplication, QMessageBox
import optimus.sandbox as sandbox


class FakeSignal:
    def connect(self, callback):
        self.callback = callback


class FakeTimer:
    def __init__(self, parent=None):
        self.timeout = FakeSignal()
        self.stopped = False
        self.interval = None
    def start(self, interval):
        self.interval = interval
    def stop(self):
        self.stopped = True


@pytest.fixture
def editor(tmp_path, monkeypatch):
    app = QApplication.instance() or QApplication([])
    source = tmp_path / 'fixture-source'
    (source / 'src/optimus').mkdir(parents=True)
    (source / 'src/optimus/ui.py').write_text('VALUE = 1\n')
    (source / 'launch.py').write_text('# Never executed fixture launcher.\n')
    backend = SimpleNamespace(data_dir=tmp_path / 'fixture-data', source_root=lambda: source, workflow=SimpleNamespace(active=False))
    window = sandbox.SandboxWindow(backend)
    fake_process = SimpleNamespace(pid=4242, poll=Mock(return_value=None))
    popen = Mock(return_value=fake_process)
    fake_app = SimpleNamespace(quit=Mock())
    monkeypatch.setattr(sandbox.subprocess, 'Popen', popen)
    monkeypatch.setattr(sandbox, 'QTimer', FakeTimer)
    monkeypatch.setattr(sandbox, 'QApplication', SimpleNamespace(instance=lambda: fake_app))
    monkeypatch.setattr(sandbox.QMessageBox, 'question', lambda *a, **kw: QMessageBox.Yes)
    monkeypatch.setattr(sandbox.QMessageBox, 'warning', Mock())
    monkeypatch.setattr(sandbox.time, 'monotonic', lambda: 100.0)
    monkeypatch.setattr(sandbox.sys, 'frozen', False, raising=False)
    yield window, popen, fake_process, fake_app, monkeypatch
    window.editor.document().setModified(False)
    window.close()
    window.deleteLater()
    app.processEvents()


def marker_for(popen):
    command = popen.call_args.args[0]
    return Path(command[command.index('--restart-ready-file') + 1])


def test_source_restart_builds_workspace_command_without_launching(editor):
    window, popen, process, fake_app, _ = editor
    window.restart()
    command = popen.call_args.args[0]
    assert command[1:3] == ['-B', str(window.workspace.root / 'launch.py')]
    assert command[command.index('--source-workspace')+1] == str(window.workspace.root)
    assert marker_for(popen).parent == window.backend.data_dir / 'Sandbox/restart'
    assert popen.call_args.kwargs['env']['OPTIMUS_ORIGINAL_SOURCE'] == str(window.workspace.source_root)
    assert window.restarting is True
    assert window.restart_timer.interval == 250
    fake_app.quit.assert_not_called()


def test_ready_marker_requires_matching_child_pid(editor):
    window, popen, _, fake_app, _ = editor
    window.restart()
    marker = marker_for(popen)
    marker.write_text(json.dumps({'ready':True,'pid':9999}))
    window.restart_timer.timeout.callback()
    fake_app.quit.assert_not_called()
    marker.write_text(json.dumps({'ready':True,'pid':4242}))
    window.restart_timer.timeout.callback()
    fake_app.quit.assert_called_once()
    assert window.restart_timer.stopped is True


def test_timeout_keeps_current_app_open(editor):
    window, _, _, fake_app, monkeypatch = editor
    window.restart()
    monkeypatch.setattr(sandbox.time, 'monotonic', lambda: 126.0)
    window.restart_timer.timeout.callback()
    fake_app.quit.assert_not_called()
    assert window.restarting is False
    assert window.restart_timer.stopped is True
    assert 'kept open' in window.status.text()


def test_child_failure_keeps_current_app_open(editor):
    window, _, process, fake_app, _ = editor
    window.restart()
    process.poll.return_value = 2
    window.restart_timer.timeout.callback()
    fake_app.quit.assert_not_called()
    assert window.restarting is False


def test_active_workflow_blocks_restart(editor):
    window, popen, _, _, _ = editor
    window.backend.workflow.active = True
    window.restart()
    popen.assert_not_called()
    assert window.restarting is False


def test_cancelled_restart_confirmation_starts_no_process(editor):
    window, popen, _, _, monkeypatch = editor
    monkeypatch.setattr(sandbox.QMessageBox, 'question', lambda *a, **kw: QMessageBox.Cancel)
    window.restart()
    popen.assert_not_called()
    assert window.restarting is False


def test_frozen_restart_passes_workspace_to_same_executable(editor):
    window, popen, _, _, monkeypatch = editor
    monkeypatch.setattr(sandbox.sys, 'frozen', True)
    window.restart()
    command = popen.call_args.args[0]
    assert command[0] == sandbox.sys.executable
    assert command[1] == '--source-workspace'
    assert '-B' not in command
    assert not any(item.endswith('launch.py') for item in command)


def test_busy_shared_settings_window_blocks_restart_before_save(editor):
    window, popen, _, fake_app, monkeypatch = editor
    details = SimpleNamespace(backend=window.backend, busy=True)
    monkeypatch.setattr(sandbox, 'QApplication', SimpleNamespace(instance=lambda: fake_app, topLevelWidgets=lambda: [details]))
    window.restart()
    popen.assert_not_called()
    assert window.workspace.versions('src/optimus/ui.py') == []


def test_owner_main_busy_settings_blocks_restart(editor):
    window, popen, _, fake_app, monkeypatch = editor
    main = SimpleNamespace(backend=window.backend, busy=False, _settings_window=SimpleNamespace(busy=True))
    monkeypatch.setattr(sandbox, 'QApplication', SimpleNamespace(instance=lambda: fake_app, topLevelWidgets=lambda: [main]))
    window.restart()
    popen.assert_not_called()


def test_bridge_pending_after_timeout_blocks_restart(editor):
    window, popen, _, _, _ = editor
    window.backend.bridge = SimpleNamespace(_busy=False, _pending={'fixture-request': ('driver-update', 999)})
    window.restart()
    popen.assert_not_called()


def test_bridge_busy_blocks_restart(editor):
    window, popen, _, _, _ = editor
    window.backend.bridge = SimpleNamespace(_busy=True, _pending={})
    window.restart()
    popen.assert_not_called()


def test_unrelated_backend_window_does_not_block_restart(editor):
    window, popen, _, fake_app, monkeypatch = editor
    unrelated = SimpleNamespace(backend=object(), busy=True)
    monkeypatch.setattr(sandbox, 'QApplication', SimpleNamespace(instance=lambda: fake_app, topLevelWidgets=lambda: [unrelated]))
    window.restart()
    popen.assert_called_once()
