"""Native window lifecycle with fixture data; no host maintenance or app exit."""
import json
import os
from types import SimpleNamespace
from unittest.mock import Mock

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

import pytest
from PySide6.QtWidgets import QApplication, QCheckBox, QMessageBox, QPushButton

from optimus.app import parser
from optimus.native_ui import NativeOptimusWindow
from optimus.sandbox import SandboxWindow
from optimus.ui import DetailedSettingsWindow, OptimusWindow


@pytest.fixture(scope='module')
def qapp():
    return QApplication.instance() or QApplication([])


@pytest.fixture(params=[OptimusWindow, NativeOptimusWindow], ids=['windows', 'native'])
def window(request, qapp, tmp_path, monkeypatch):
    # A saved legacy opt-in must never restore the removed behavior.
    (tmp_path / 'ui-settings.json').write_text(json.dumps({'orb': True, 'motion': False}))
    source = tmp_path / 'source'
    (source / 'src' / 'optimus').mkdir(parents=True)
    (source / 'src' / 'optimus' / 'ui.py').write_text('VALUE = 1\n')
    backend = SimpleNamespace(data_dir=tmp_path, preferences=lambda: {}, source_root=lambda: source)
    value = request.param(backend, smoke_test=True)
    value.smoke_test = False
    quit_app = Mock()
    monkeypatch.setattr(qapp, 'quit', quit_app)
    value._test_quit = quit_app
    yield value
    if value._sandbox_window is not None:
        value._sandbox_window.editor.document().setModified(False)
        value._sandbox_window.close()
        value._sandbox_window.deleteLater()
    value._settings_window = None
    value._setup_wizard = None
    value.busy = False
    value._quitting = True
    value.shutdown()
    value.close()
    value.deleteLater()
    qapp.processEvents()


def test_minimize_keeps_native_window_and_tray_restore_preserves_stars(window, qapp):
    window.canvas.motion = True
    window.show()
    qapp.processEvents()
    visible = {widget for widget in qapp.topLevelWidgets() if widget.isVisible()}
    assert window.canvas.timer.isActive()
    assert not hasattr(window, '_holographic_orb')
    window.showMinimized()
    qapp.processEvents()
    qapp.processEvents()
    assert window.isMinimized() and window.isVisible()
    assert not window.canvas.timer.isActive()
    assert {widget for widget in qapp.topLevelWidgets() if widget.isVisible()} <= visible
    window.tray.contextMenu().actions()[0].trigger()
    qapp.processEvents()
    qapp.processEvents()
    assert window.isVisible() and not window.isMinimized()
    assert window.canvas.timer.isActive()
    window._test_quit.assert_not_called()


def test_main_close_exits_without_floating_surface(window, qapp):
    window.show()
    qapp.processEvents()
    assert window.close()
    assert window._quitting and not window.isVisible()
    assert not window.canvas.timer.isActive()
    window._test_quit.assert_called_once_with()


@pytest.mark.parametrize('guard', ['workflow', 'settings', 'setup', 'post_review'])
def test_close_respects_existing_operation_and_review_guards(window, qapp, guard):
    window.show()
    qapp.processEvents()
    if guard == 'workflow':
        window.busy = True
    elif guard == 'settings':
        window._settings_window = SimpleNamespace(busy=True)
    elif guard == 'setup':
        window._setup_wizard = object()
    else:
        window._post_review_active = True
    assert not window.close()
    assert window.isVisible() and not window.isMinimized()
    assert not window._quitting
    window._test_quit.assert_not_called()


@pytest.mark.parametrize('answer', [QMessageBox.Save, QMessageBox.Discard, QMessageBox.Cancel])
def test_main_close_runs_sandbox_unsaved_guard(window, qapp, monkeypatch, answer):
    editor = SandboxWindow(window.backend)
    window._sandbox_window = editor
    editor.editor.setPlainText('VALUE = 2\n')
    editor.editor.document().setModified(True)
    window.show()
    editor.show()
    qapp.processEvents()
    window.config.ui_motion_enabled = True
    editor._optimus_reverse_reveal_close = True
    prompt = Mock(return_value=answer)
    monkeypatch.setattr(QMessageBox, 'question', prompt)
    closed = window.close()
    prompt.assert_called_once()
    assert closed == (answer != QMessageBox.Cancel)
    if answer == QMessageBox.Cancel:
        assert window.isVisible() and editor.isVisible()
        assert editor.editor.document().isModified()
        window._test_quit.assert_not_called()
    else:
        assert not editor.isVisible()
        window._test_quit.assert_called_once_with()
        if answer == QMessageBox.Save:
            assert editor.workspace.read(editor.current) == 'VALUE = 2\n'
            assert not editor.editor.document().isModified()


def test_unsaved_sandbox_save_failure_prevents_exit(window, qapp, monkeypatch):
    editor = SandboxWindow(window.backend)
    window._sandbox_window = editor
    editor.editor.setPlainText('VALUE =\n')
    editor.editor.document().setModified(True)
    window.show()
    editor.show()
    qapp.processEvents()
    monkeypatch.setattr(QMessageBox, 'question', Mock(return_value=QMessageBox.Save))
    warning = Mock()
    monkeypatch.setattr(QMessageBox, 'warning', warning)
    assert not window.close()
    assert window.isVisible() and editor.isVisible()
    assert editor.editor.document().isModified()
    warning.assert_called_once()
    window._test_quit.assert_not_called()


def test_native_appearance_has_no_orb_control_and_drops_legacy_key(window, qapp):
    # Both settings UIs save only supported appearance choices.
    if isinstance(window, NativeOptimusWindow):
        window.smoke_test = True
        window.open_settings()
        detail = window._settings_window
        detail.smoke_test = False
        detail._save_appearance()
    else:
        detail = DetailedSettingsWindow(window.backend, smoke_test=True)
        detail.save_preferences()
    try:
        assert all('orb' not in checkbox.text().lower() for checkbox in detail.findChildren(QCheckBox))
        saved = json.loads(window.preferences_path.read_text())
        assert set(saved) == {'theme', 'motion'}
    finally:
        detail.shutdown()
        detail.close()
        detail.deleteLater()
        window._settings_window = None
        qapp.processEvents()


def test_settings_exit_uses_main_exit_guards(qapp, tmp_path):
    detail = DetailedSettingsWindow(SimpleNamespace(data_dir=tmp_path), smoke_test=True)
    quit_main = Mock(return_value=False)
    detail._main_window = SimpleNamespace(quit_application=quit_main)
    try:
        button = next(button for button in detail.findChildren(QPushButton) if button.text() == 'Exit Optimus')
        button.click()
        quit_main.assert_called_once_with()
    finally:
        detail.shutdown()
        detail.close()
        detail.deleteLater()
        qapp.processEvents()


def test_legacy_no_orb_argument_remains_compatible_and_hidden():
    assert parser().parse_args(['--no-orb']).no_orb is True
    assert '--no-orb' not in parser().format_help()
