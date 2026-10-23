"""GUI ownership uses fixture processes/data, never the installed Optimus app."""
import json
import os
from pathlib import Path
import subprocess
import sys
import time
from types import SimpleNamespace
from unittest.mock import Mock, patch

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
import pytest
from PySide6.QtCore import QCoreApplication

from optimus import app
from optimus.gui_instance import GuiInstance, instance_key


def test_identity_is_user_and_canonical_data_root_scoped(tmp_path):
    first, second = tmp_path / 'first', tmp_path / 'second'
    first.mkdir()
    second.mkdir()
    assert instance_key(first, user='one') == instance_key(first / '.', user='one')
    assert instance_key(first, user='one') != instance_key(first, user='two')
    assert instance_key(first, user='one') != instance_key(second, user='one')


def test_endpoint_name_fits_typical_macos_temp_directory(tmp_path):
    lease = GuiInstance(tmp_path)
    assert len(lease.key) == 64
    assert len('/var/folders/xx/' + 'a'*32 + '/T/' + lease.name) < 104
    assert len(lease.name) <= 40


def test_lease_identity_does_not_depend_on_home_environment(tmp_path, monkeypatch):
    original = instance_key(tmp_path)
    monkeypatch.setenv('HOME', str(tmp_path / 'other-home'))
    monkeypatch.setenv('USERPROFILE', str(tmp_path / 'other-profile'))
    assert instance_key(tmp_path) == original


def test_crashed_fixture_owner_can_be_recovered_without_expiring_a_live_lease(tmp_path):
    result = subprocess.run([sys.executable, '-c', '''
import os, sys
from optimus.gui_instance import GuiInstance
lease = GuiInstance(sys.argv[1])
assert lease.try_claim()
os._exit(0)
''', str(tmp_path)], capture_output=True, text=True, timeout=10,
        creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    assert result.returncode == 0, result.stdout + result.stderr
    lease = GuiInstance(tmp_path)
    try:
        assert lease.try_claim()
    finally:
        lease.close()


def test_real_second_process_activates_live_primary_without_reclaiming_lease(tmp_path):
    fixture = tmp_path / 'primary.py'
    fixture.write_text('''
import json, pathlib, sys
from PySide6.QtCore import QCoreApplication, QTimer
from optimus.gui_instance import GuiInstance
root = pathlib.Path(sys.argv[1])
app = QCoreApplication([])
lease = GuiInstance(root)
assert lease.try_claim()
count = 0
def activate():
    global count
    count += 1
    (root / 'activated.json').write_text(json.dumps({'count':count}))
    return True
lease.listen(activate)
(root / 'ready').write_text('ready')
timer = QTimer()
timer.timeout.connect(lambda: app.quit() if (root / 'stop').exists() else None)
timer.start(20)
QTimer.singleShot(15000, app.quit)
app.exec()
lease.close()
''', encoding='utf-8')
    primary = subprocess.Popen([sys.executable, str(fixture), str(tmp_path)],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    duplicate = None
    try:
        deadline = time.monotonic() + 10
        while not (tmp_path / 'ready').exists() and primary.poll() is None:
            assert time.monotonic() < deadline, 'Fixture primary did not become ready.'
            time.sleep(.02)
        assert primary.poll() is None
        duplicate = GuiInstance(tmp_path)
        # A very old timestamp never overrides a living owner.
        os.utime(duplicate.lock.fileName(), (1, 1))
        assert not duplicate.try_claim()
        assert duplicate.activate_existing()
        assert json.loads((tmp_path / 'activated.json').read_text()) == {'count':1}
        # Exercise the actual GUI gate in a fresh process with no QApplication
        # or QCoreApplication. A duplicate must never reach UAC/window creation.
        gate = '''
from pathlib import Path
from types import SimpleNamespace
import sys
from optimus import app, elevation
def forbidden(*args, **kwargs):
    raise AssertionError('A duplicate reached GUI or UAC startup.')
app._show_gui = forbidden
elevation.startup_administrator = forbidden
backend = SimpleNamespace(data_dir=Path(sys.argv[1]), close=lambda: None)
raise SystemExit(app._run_gui(backend, app.parser().parse_args([]), []))
'''
        result = subprocess.run([sys.executable, '-c', gate, str(tmp_path)], capture_output=True,
                                text=True, timeout=10, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        assert result.returncode == 0, result.stdout + result.stderr
        assert json.loads((tmp_path / 'activated.json').read_text()) == {'count':2}
        assert not duplicate.try_claim()
    finally:
        if duplicate is not None:
            duplicate.close()
        (tmp_path / 'stop').write_text('stop')
        stdout, stderr = primary.communicate(timeout=20)
    assert primary.returncode == 0, stdout + stderr
    reclaimed = GuiInstance(tmp_path)
    try:
        assert reclaimed.try_claim()
    finally:
        reclaimed.close()


def test_live_lock_without_activation_endpoint_fails_closed(tmp_path):
    first, duplicate = GuiInstance(tmp_path), GuiInstance(tmp_path)
    try:
        assert first.try_claim()
        original = Path(first.lock.fileName()).read_bytes()
        assert not duplicate.try_claim()
        assert not duplicate.activate_existing(timeout_ms=100)
        assert Path(first.lock.fileName()).read_bytes() == original
        assert not duplicate.try_claim()
    finally:
        duplicate.close()
        first.close()


@pytest.mark.parametrize('platform', ['win32', 'darwin', 'linux'])
@pytest.mark.parametrize('activated', [True, False])
def test_duplicate_exits_before_uac_window_or_automatic_work(tmp_path, monkeypatch, platform, activated):
    backend = SimpleNamespace(data_dir=tmp_path, close=Mock())
    lease = Mock()
    lease.try_claim.return_value = False
    lease.activate_existing.return_value = activated
    monkeypatch.setattr(app.sys, 'platform', platform)
    with patch('optimus.gui_instance.GuiInstance', return_value=lease), \
         patch('optimus.elevation.startup_administrator') as elevation, \
         patch.object(app, '_show_gui') as gui:
        assert app._run_gui(backend, app.parser().parse_args([]), []) == (0 if activated else 2)
    elevation.assert_not_called()
    gui.assert_not_called()
    backend.close.assert_called_once_with()
    lease.close.assert_called_once_with()


def test_approved_uac_child_waits_for_lease_before_building_gui(tmp_path, monkeypatch):
    backend = SimpleNamespace(data_dir=tmp_path, close=Mock())
    lease = Mock()
    lease.try_claim.return_value = False
    lease.wait_for_handoff.return_value = True
    monkeypatch.setattr(app.sys, 'platform', 'win32')
    with patch('optimus.gui_instance.GuiInstance', return_value=lease), \
         patch('optimus.elevation.startup_administrator', return_value={'started':False}), \
         patch.object(app, '_show_gui', return_value=0) as gui:
        assert app._run_gui(backend, app.parser().parse_args(['--admin-relaunch']), ['--admin-relaunch']) == 0
    lease.wait_for_handoff.assert_called_once_with()
    lease.activate_existing.assert_not_called()
    assert gui.call_args.args[4] is False


def test_source_restart_stays_hidden_and_does_no_automatic_work_until_lease_handoff(tmp_path, monkeypatch):
    backend = SimpleNamespace(data_dir=tmp_path, close=Mock(), preferences=Mock(return_value={}), analysis_start=Mock())
    lease, gui, window = Mock(), Mock(), Mock()
    gui.exec.return_value = 0
    lease.try_claim.side_effect = [False, True]
    marker = tmp_path / 'Sandbox' / 'restart' / 'fixture.json'
    args = app.parser().parse_args(['--source-workspace', str(tmp_path / 'workspace'), '--restart-ready-file', str(marker)])
    monkeypatch.setattr(app.sys, 'platform', 'win32')
    monkeypatch.setattr(app, 'ctypes', SimpleNamespace(windll=SimpleNamespace(shell32=Mock())))
    with patch('PySide6.QtWidgets.QApplication', return_value=gui), \
         patch('PySide6.QtCore.QTimer') as timer, patch('PySide6.QtGui.QIcon'), \
         patch('optimus.ui.OptimusWindow', return_value=window), \
         patch('optimus.gui_startup.SetupAutomationController') as controller, \
         patch.object(app, '_resume_saved_analysis') as resume:
        assert app._show_gui(backend, args, {}, lease, True, Mock()) == 0
        window.show.assert_not_called()
        window.tray.hide.assert_called_once_with()
        resume.assert_not_called()
        controller.assert_not_called()
        take_over = window._handoff_timer.timeout.connect.call_args.args[0]
        take_over()
        window.show.assert_not_called()
        take_over()
        window.show.assert_called_once_with()
        resume.assert_not_called()
        window.session_approved.return_value = True
        window._quitting = window._close_when_idle = False
        assert window._startup_update_pending is True
        window._session_approved_callback()
        resume.assert_not_called()
        window._startup_update_pending = False  # The new startup update gate has completed.
        window._session_approved_callback()
        resume.assert_called_once_with(backend)
        controller.assert_called_once()
        lease.listen.assert_called_once_with(window.activate_existing_window, safe_stop=window.request_safe_stop)


@pytest.mark.parametrize('option', ['--smoke-test', '--startup-probe'])
def test_read_only_gui_does_not_join_or_activate_interactive_session(tmp_path, option):
    backend = SimpleNamespace(data_dir=tmp_path, close=Mock())
    args = app.parser().parse_args([option])
    with patch('optimus.gui_instance.GuiInstance') as lease, \
         patch('optimus.elevation.startup_administrator', return_value={}), \
         patch.object(app, '_show_gui', return_value=0):
        assert app._run_gui(backend, args, [option]) == 0
    lease.assert_not_called()
