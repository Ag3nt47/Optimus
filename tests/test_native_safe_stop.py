"""Safe-stop requests use fixture windows/processes, never installed maintenance."""
import copy
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import time
from types import SimpleNamespace
from unittest.mock import Mock, patch

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

import pytest
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from optimus import app
from optimus.automation import CURRENT_SETUP_VERSION
from optimus.native_ui import NativeOptimusWindow
from optimus.ui import OptimusWindow
from optimus.workflow import DEFAULTS


def wait_until(qapp, predicate):
    deadline = time.monotonic() + 5
    while not predicate() and time.monotonic() < deadline:
        qapp.processEvents()
        QTest.qWait(5)
    qapp.processEvents()
    assert predicate()


@pytest.fixture(params=[OptimusWindow, NativeOptimusWindow], ids=['windows', 'native'])
def window(request, tmp_path, monkeypatch):
    qapp = QApplication.instance() or QApplication([])
    settings = dict(copy.deepcopy(DEFAULTS), setup_version=CURRENT_SETUP_VERSION,
        browser_cache_enabled=False, browser_history_enabled=False,
        browser_cookies_enabled=False, browser_download_history_enabled=False,
        auto_continue_fixes_enabled=True)
    backend = SimpleNamespace(data_dir=tmp_path, preferences=lambda: copy.deepcopy(settings),
        optimize=Mock(), available_fixes=Mock(return_value={'actions': [{'id': 'fixture'}]}),
        apply_available_fixes=Mock())
    value = request.param(backend, smoke_test=True)
    value.smoke_test = False
    value._test_quit = Mock()
    monkeypatch.setattr(qapp, 'quit', value._test_quit)
    value.show()
    yield value, qapp
    value._cancel.set()
    if hasattr(value, '_test_release'):
        value._test_release.set()
    value.pool.waitForDone(5000)
    qapp.processEvents()
    value._quitting = True
    value._settings_window = value._setup_wizard = value._sandbox_window = None
    if value._progress_window is not None:
        value._progress_window.finish({'status': 'cancelled', 'cancelled': True, 'stages': []})
        value._progress_window.close()
    value.shutdown()
    value.close()
    value.deleteLater()
    qapp.processEvents()


@pytest.mark.parametrize('mode', ['close', 'tray', 'ipc_callback'])
def test_stop_drains_active_work_and_saves_before_exit_without_followups(window, mode):
    owner, qapp = window
    started, stopped, release = (threading.Event() for _ in range(3))
    owner._test_release = release
    receipt = owner.backend.data_dir / 'fixture-final-receipt.json'
    owner._automation_controller = SimpleNamespace(stop=Mock())
    owner._automation_timer = SimpleNamespace(stop=Mock())

    def operation(settings, *, progress_callback, cancel_event):
        started.set()
        while not release.wait(.01):
            if cancel_event.is_set():
                stopped.set()
        # This models the workflow's terminal write, not an installer kill.
        receipt.write_text(json.dumps({'status': 'cancelled', 'outcomeKnown': True}))
        return {'status': 'cancelled', 'cancelled': True, 'stages': [],
                'security_review': {'recommendations': [{'id': 'must-not-open'}]}}

    owner.backend.optimize.side_effect = operation
    owner.start_optimization()
    wait_until(qapp, started.is_set)
    prompts = SimpleNamespace(shutdown=Mock(), deleteLater=Mock())
    owner._worker._browser_prompts = prompts
    if mode == 'close':
        assert not owner.close()
    elif mode == 'tray':
        assert owner.quit_application() is False
    else:
        assert owner.request_safe_stop()
    wait_until(qapp, stopped.is_set)
    assert owner.busy and owner.isVisible()
    assert owner._cancel.is_set() and owner._close_when_idle
    assert not receipt.exists()
    owner._test_quit.assert_not_called()
    prompts.shutdown.assert_called()
    owner._automation_controller.stop.assert_called()
    owner._automation_timer.stop.assert_called()
    owner.start_optimization()
    assert owner.backend.optimize.call_count == 1
    release.set()
    wait_until(qapp, lambda: owner._test_quit.called)
    assert json.loads(receipt.read_text()) == {'status': 'cancelled', 'outcomeKnown': True}
    assert not owner.busy and owner._quitting
    assert owner._pending_auto_fixes is None and owner._pending_post_review is None
    owner.backend.available_fixes.assert_not_called()
    owner.backend.apply_available_fixes.assert_not_called()


def test_cancel_button_stops_new_work_without_requesting_exit(window):
    owner, qapp = window
    started, release = threading.Event(), threading.Event()
    owner._test_release = release

    def operation(settings, *, progress_callback, cancel_event):
        started.set()
        assert release.wait(10)
        assert cancel_event.is_set()
        return {'status': 'cancelled', 'cancelled': True, 'stages': []}

    owner.backend.optimize.side_effect = operation
    owner.start_optimization()
    wait_until(qapp, started.is_set)
    owner._progress_window.cancel_button.click()
    assert owner._cancel.is_set() and not owner._close_when_idle
    release.set()
    wait_until(qapp, lambda: not owner.busy)
    assert owner._progress_window.completed
    owner._test_quit.assert_not_called()
    owner.backend.apply_available_fixes.assert_not_called()


def test_shutdown_releases_browser_wait_then_drains_own_worker(window):
    owner, _ = window
    owner.busy = True
    order = []
    owner._worker = SimpleNamespace(_browser_prompts=SimpleNamespace(shutdown=lambda: order.append('prompt_released')))
    with patch.object(owner.pool, 'waitForDone', side_effect=lambda: order.append('worker_drained')):
        owner.shutdown()
    assert owner._cancel.is_set()
    assert order == ['prompt_released', 'worker_drained']


def test_idle_safe_stop_preserves_cancelled_sandbox_edits(window):
    owner, qapp = window
    owner._sandbox_window = SimpleNamespace(close=Mock(return_value=False))
    assert owner.request_safe_stop()
    qapp.processEvents()
    assert not owner._quitting and not owner._close_when_idle
    owner._test_quit.assert_not_called()


@pytest.mark.parametrize('platform', ['win32', 'linux', 'darwin'])
@pytest.mark.parametrize('accepted', [True, False])
def test_cli_stop_never_opens_gui_elevates_or_runs_maintenance(tmp_path, monkeypatch, platform, accepted):
    backend = SimpleNamespace(data_dir=tmp_path, close=Mock())
    monkeypatch.setattr(app.sys, 'platform', platform)
    with patch('optimus.backend.Backend', return_value=backend), \
         patch('optimus.native_backend.NativeBackend', return_value=backend), \
         patch('optimus.gui_instance.GuiInstance') as lease, \
         patch.object(app, '_run_gui') as gui, \
         patch('optimus.elevation.startup_administrator') as elevation:
        lease.return_value.stop_existing.return_value = accepted
        assert app.main(['--safe-stop']) == (0 if accepted else 2)
    lease.return_value.try_claim.assert_not_called()
    lease.return_value.stop_existing.assert_called_once_with()
    lease.return_value.close.assert_called_once_with()
    gui.assert_not_called()
    elevation.assert_not_called()
    backend.close.assert_called_once_with()


def test_real_safe_stop_cli_has_no_qapplication_and_only_requests_existing_fixture(tmp_path):
    fixture = tmp_path / 'primary.py'
    fixture.write_text('''
import json, pathlib, sys
from PySide6.QtCore import QCoreApplication, QTimer
from optimus.gui_instance import GuiInstance
root = pathlib.Path(sys.argv[1])
app = QCoreApplication([])
lease = GuiInstance(root)
assert lease.try_claim()
def stop():
    (root / 'requested').write_text('request only')
    return True
lease.listen(lambda: False, safe_stop=stop)
(root / 'ready').write_text('ready')
timer = QTimer()
timer.timeout.connect(lambda: app.quit() if (root / 'finish').exists() else None)
timer.start(20)
QTimer.singleShot(15000, app.quit)
app.exec()
lease.close()
''', encoding='utf-8')
    primary = subprocess.Popen([sys.executable, str(fixture), str(tmp_path)],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    client = '''
import json, pathlib, sys
from types import SimpleNamespace
from optimus.app import _safe_stop_command
from PySide6.QtCore import QCoreApplication
assert QCoreApplication.instance() is None
print(json.dumps(_safe_stop_command(SimpleNamespace(data_dir=pathlib.Path(sys.argv[1])))))
assert QCoreApplication.instance() is None
'''
    try:
        deadline = time.monotonic() + 10
        while not (tmp_path / 'ready').exists() and primary.poll() is None:
            assert time.monotonic() < deadline
            time.sleep(.02)
        assert primary.poll() is None
        before = list(tmp_path.glob('.gui-*.lock'))[0].read_bytes()
        result = subprocess.run([sys.executable, '-c', client, str(tmp_path)],
            capture_output=True, text=True, timeout=10,
            creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        assert result.returncode == 0, result.stdout + result.stderr
        assert json.loads(result.stdout)['status'] == 'stop_requested'
        assert (tmp_path / 'requested').read_text() == 'request only'
        assert primary.poll() is None  # Ack is not a fabricated termination claim.
        assert list(tmp_path.glob('.gui-*.lock'))[0].read_bytes() == before
    finally:
        (tmp_path / 'finish').write_text('finish')
        stdout, stderr = primary.communicate(timeout=20)
    assert primary.returncode == 0, stdout + stderr
    empty = tmp_path / 'not-running'
    empty.mkdir()
    result = subprocess.run([sys.executable, '-c', client, str(empty)],
        capture_output=True, text=True, timeout=10,
        creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    assert result.returncode == 0, result.stdout + result.stderr
    assert json.loads(result.stdout)['status'] == 'unavailable'
    assert list(empty.iterdir()) == []


@pytest.mark.skipif(sys.platform != 'win32', reason='Windows PowerShell wrapper contract')
@pytest.mark.parametrize('acknowledgement', [0, 2])
def test_powershell_wrapper_awaits_gui_command_and_preserves_rejection(tmp_path, acknowledgement):
    executable = tmp_path / 'fixture application.exe'
    executable.write_bytes(b'not executed: Start-Process is injected')
    wrapper = Path(__file__).resolve().parents[1] / 'scripts' / 'Safe-Stop-Optimus.ps1'
    quote = lambda value: "'" + str(value).replace("'", "''") + "'"
    script = '''
function Start-Process {
    param([string]$FilePath, [string[]]$ArgumentList, [switch]$Wait,
          [switch]$PassThru, [string]$WindowStyle)
    if (-not $Wait -or -not $PassThru -or $WindowStyle -cne 'Hidden') {
        throw 'GUI command was not awaited hidden with an exit-code handle.'
    }
    if ($ArgumentList.Count -ne 1 -or $ArgumentList[0] -cne '--safe-stop') {
        throw 'Unexpected command arguments.'
    }
    [pscustomobject]@{ExitCode=ACKNOWLEDGEMENT}
}
& WRAPPER -Executable EXECUTABLE
exit $LASTEXITCODE
'''.replace('ACKNOWLEDGEMENT', str(acknowledgement)).replace('WRAPPER', quote(wrapper)).replace('EXECUTABLE', quote(executable))
    powershell = Path(os.environ['SystemRoot']) / 'System32/WindowsPowerShell/v1.0/powershell.exe'
    result = subprocess.run([str(powershell), '-NoLogo', '-NoProfile', '-NonInteractive',
        '-ExecutionPolicy', 'Bypass', '-Command', script], capture_output=True, text=True, timeout=15,
        creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    assert result.returncode == acknowledgement, result.stdout + result.stderr
