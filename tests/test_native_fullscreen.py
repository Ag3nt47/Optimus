"""Full-screen fixtures never run maintenance, save source, or restart Optimus."""
import gc
import json
import os
import threading
import weakref
from types import SimpleNamespace
from unittest.mock import Mock

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

import pytest
from PySide6.QtCore import QCoreApplication, QEvent, QObject, QRect, Qt
from PySide6.QtGui import QKeyEvent
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QDialog, QLineEdit, QMainWindow, QMessageBox, QVBoxLayout, QWidget
from shiboken6 import isValid

from optimus.native_ui import NativeOptimusWindow
from optimus.sandbox import SandboxWindow
from optimus.ui import OptimizationProgressWindow, OptimusWindow
from optimus.widgets.fullscreen import FullScreenControl
from optimus.widgets.window_geometry import install_window_geometry
from test_native_worker_cards import fonts


@pytest.fixture(scope='module')
def app():
    return QApplication.instance() or QApplication([])


def settle(app):
    for _ in range(4):
        app.processEvents()


def reachable(window, button):
    assert button.isVisible() and button.isEnabled()
    center = button.mapTo(window, button.rect().center())
    assert window.rect().contains(center)
    assert window.mask().isEmpty() or window.mask().contains(center)
    hit = window.childAt(center)
    assert hit is button or button.isAncestorOf(hit)


class CountingControl(FullScreenControl):
    def __init__(self, *args, **kwargs):
        self.callback_count = 0
        super().__init__(*args, **kwargs)

    def eventFilter(self, watched, event):
        self.callback_count += 1
        return super().eventFilter(watched, event)


@pytest.mark.parametrize('count', [100, 1000])
@pytest.mark.parametrize('window_count', [1, 16, 64])
def test_unrelated_events_use_one_router_and_no_per_window_handlers(app, window_count, count):
    windows = [QWidget() for _ in range(window_count)]
    controls = [CountingControl(window) for window in windows]
    router = app._optimus_fullscreen_router
    try:
        for control in controls:
            control.callback_count = 0
        event = QEvent(QEvent.User)
        for _ in range(count):
            app.sendEvent(windows[0], event)
        assert all(control.callback_count == 0 for control in controls)
        assert router.parent() is app
        assert app.findChildren(QObject, 'OptimusFullScreenRouter') == [router]
    finally:
        for window in windows:
            window.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)
    assert app._optimus_fullscreen_router is router
    assert isValid(router)


def test_focused_hotkeys_route_only_to_their_window_and_ordinary_keys_stay_local(app):
    windows = [QWidget() for _ in range(16)]
    controls = [CountingControl(window) for window in windows]
    child = QLineEdit(windows[0])
    try:
        windows[0].setProperty('_optimus_no_reveal', True)
        windows[0].show()
        child.setFocus()
        settle(app)
        for control in controls:
            control.callback_count = 0
        QTest.keyClick(child, Qt.Key_A)
        assert child.text() == 'a'
        assert all(control.callback_count == 0 for control in controls)
        QTest.keyClick(child, Qt.Key_F11)
        settle(app)
        assert windows[0].isFullScreen() and controls[0].callback_count > 0
        assert all(control.callback_count == 0 for control in controls[1:])
        QTest.keyClick(child, Qt.Key_Escape)
        settle(app)
        assert windows[0].isVisible() and not windows[0].isFullScreen()
        assert all(control.callback_count == 0 for control in controls[1:])
    finally:
        for window in windows:
            window.hide()
            window.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)


@pytest.mark.parametrize('delete_window', [True, False], ids=['window-destroyed', 'control-destroyed'])
def test_cpp_destruction_and_gc_remove_weak_registration_without_retaining_wrappers(app, delete_window):
    window = QWidget()
    control = FullScreenControl(window)
    window_ref, control_ref = weakref.ref(window), weakref.ref(control)
    router = app._optimus_fullscreen_router
    key = id(window)
    assert key in router._controls
    if delete_window:
        window.deleteLater()
    else:
        control.deleteLater()
    QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)
    assert not isValid(control)
    assert key not in router._controls
    if not delete_window:
        QTest.keyClick(window, Qt.Key_F11)  # No invalid C++ handler is dispatched.
        assert not window.isFullScreen()
        window.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)
    assert not isValid(window)
    del control, window
    gc.collect()
    assert window_ref() is None and control_ref() is None
    assert app._optimus_fullscreen_router is router and isValid(router)
    assert app.findChildren(QObject, 'OptimusFullScreenRouter') == [router]


@pytest.fixture
def fixture_window(app):
    value = QDialog()
    value.setProperty('_optimus_no_reveal', True)
    value.resize(460, 330)
    value.move(70, 80)
    layout = QVBoxLayout(value)
    value.fullscreen = FullScreenControl(value)
    layout.addWidget(value.fullscreen.button)
    value.edit = QLineEdit('Unsaved fixture input')
    layout.addWidget(value.edit)
    value.show()
    settle(app)
    yield value
    value.hide()
    value.deleteLater()
    QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)
    settle(app)


def test_mouse_and_child_keyboard_restore_normal_geometry_and_accessibility(fixture_window, app):
    window = fixture_window
    control = window.fullscreen
    original = QRect(window.geometry())
    reachable(window, control.button)
    assert not control.button.icon().isNull()
    assert control.button.accessibleName() == 'Full screen'
    assert 'F11' in control.button.toolTip()
    QTest.mouseClick(control.button, Qt.LeftButton)
    settle(app)
    assert window.isFullScreen() and control.button.isChecked()
    assert control.button.accessibleName() == 'Exit full screen'
    assert 'Esc' in control.button.toolTip()
    assert 'previous window size' in control.button.accessibleDescription()
    reachable(window, control.button)
    window.edit.setFocus()
    QTest.keyClick(window.edit, Qt.Key_Escape)
    settle(app)
    assert window.isVisible() and not window.isFullScreen()
    assert window.geometry() == original
    assert window.edit.text() == 'Unsaved fixture input'
    assert not control.button.isChecked()
    QTest.keyClick(window.edit, Qt.Key_F11)
    settle(app)
    assert window.isFullScreen()
    QTest.keyClick(window.edit, Qt.Key_F11)
    settle(app)
    assert not window.isFullScreen() and window.geometry() == original
    # Once restored, QDialog's existing Esc rejection remains available.
    QTest.keyClick(window.edit, Qt.Key_Escape)
    assert not window.isVisible() and window.result() == QDialog.Rejected


def test_maximized_state_and_original_normal_geometry_survive_fullscreen(fixture_window, app):
    window = fixture_window
    original = QRect(window.geometry())
    window.showMaximized()
    settle(app)
    window.fullscreen.toggle()
    settle(app)
    assert window.isFullScreen()
    window.showMinimized()
    settle(app)
    window.showFullScreen()
    settle(app)
    assert window.isFullScreen()
    window.fullscreen.exit_full_screen()
    settle(app)
    assert window.isMaximized() and not window.isFullScreen()
    window.showNormal()
    settle(app)
    assert window.geometry() == original


def test_os_state_changes_update_control_and_repeated_escape_cannot_reject(fixture_window, app):
    window = fixture_window
    original = QRect(window.geometry())
    window.showFullScreen()  # Simulate a state change initiated outside the helper.
    settle(app)
    assert window.fullscreen.button.isChecked()
    assert window.fullscreen.button.accessibleName() == 'Exit full screen'
    QTest.keyPress(window.edit, Qt.Key_Escape)
    repeat_release = QKeyEvent(QEvent.KeyRelease, Qt.Key_Escape, Qt.NoModifier, '', True, 1)
    app.sendEvent(window.edit, repeat_release)
    repeated = QKeyEvent(QEvent.KeyPress, Qt.Key_Escape, Qt.NoModifier, '', True, 1)
    app.sendEvent(window.edit, repeated)
    QTest.keyRelease(window.edit, Qt.Key_Escape)
    settle(app)
    assert window.isVisible() and not window.isFullScreen()
    assert window.geometry() == original
    window.showFullScreen()
    settle(app)
    window.showNormal()
    settle(app)
    assert not window.fullscreen.button.isChecked()
    assert window.fullscreen.button.accessibleName() == 'Full screen'


def test_child_dialog_escape_does_not_exit_its_fullscreen_owner(fixture_window, app):
    window = fixture_window
    window.fullscreen.toggle()
    prompt = QDialog(window)
    prompt.setProperty('_optimus_no_reveal', True)
    prompt.show()
    settle(app)
    QTest.keyClick(prompt, Qt.Key_Escape)
    settle(app)
    assert not prompt.isVisible() and window.isFullScreen()
    prompt.deleteLater()


@pytest.fixture
def sandbox(app, tmp_path):
    source = tmp_path / 'source'
    (source / 'src/optimus').mkdir(parents=True)
    (source / 'src/optimus/ui.py').write_text('VALUE = 1\n', encoding='utf-8')
    value = SandboxWindow(SimpleNamespace(source_root=lambda: source, data_dir=tmp_path / 'data'))
    value.setProperty('_optimus_no_reveal', True)
    value.resize(640, 480)
    value.show()
    settle(app)
    yield value
    value.editor.document().setModified(False)
    value.close()
    value.deleteLater()
    QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)
    settle(app)


def test_sandbox_escape_preserves_unsaved_code_without_prompt_save_or_restart(sandbox, app, monkeypatch):
    window = sandbox
    saved = window.workspace.read(window.current)
    window.editor.setPlainText('VALUE = 2\n')
    window.editor.document().setModified(True)
    question = Mock(side_effect=AssertionError('Full screen must not open an unsaved-code prompt'))
    monkeypatch.setattr(QMessageBox, 'question', question)
    monkeypatch.setattr(window.workspace, 'save', Mock(side_effect=AssertionError('No fixture save')))
    monkeypatch.setattr(window, 'restart', Mock(side_effect=AssertionError('No fixture restart')))
    reachable(window, window.fullscreen.button)
    QTest.mouseClick(window.fullscreen.button, Qt.LeftButton)
    settle(app)
    reachable(window, window.fullscreen.button)
    window.editor.setFocus()
    QTest.keyClick(window.editor, Qt.Key_Escape)
    settle(app)
    assert window.isVisible() and not window.isFullScreen()
    assert window.editor.document().isModified()
    assert window.editor.toPlainText() == 'VALUE = 2\n'
    assert window.workspace.read(window.current) == saved
    QTest.keyClick(window.editor, Qt.Key_Escape)
    settle(app)
    assert window.isVisible() and window.editor.document().isModified()
    assert window.editor.toPlainText() == 'VALUE = 2\n'
    question.assert_not_called()


def test_progress_escape_exits_fullscreen_before_safe_cancel(app):
    owner = QWidget()
    owner.config = SimpleNamespace(ui_motion_enabled=False)
    cancel = threading.Event()
    window = OptimizationProgressWindow(owner, cancel)
    window.setProperty('_optimus_no_reveal', True)
    try:
        window.show()
        settle(app)
        reachable(window, window.fullscreen.button)
        QTest.mouseClick(window.fullscreen.button, Qt.LeftButton)
        settle(app)
        reachable(window, window.fullscreen.button)
        QTest.keyClick(window, Qt.Key_Escape)
        settle(app)
        assert window.isVisible() and not window.isFullScreen()
        assert not cancel.is_set() and window.cancel_button.isEnabled()
        QTest.keyClick(window, Qt.Key_Escape)
        settle(app)
        assert cancel.is_set() and window.isVisible()
    finally:
        window.completed = True
        window.spinner.stop()
        window.close()
        window.deleteLater()
        owner.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)
        settle(app)


@pytest.mark.parametrize('kind', [OptimusWindow, NativeOptimusWindow])
def test_main_and_native_inherit_one_reachable_control_without_saving_preferences(kind, app, tmp_path, monkeypatch):
    preferences = tmp_path / 'ui-settings.json'
    preferences.write_text(json.dumps({'motion': False, 'theme': 'cyber'}), encoding='utf-8')
    before = preferences.read_bytes()
    window = kind(SimpleNamespace(data_dir=tmp_path, preferences=lambda: {'reduce_motion': True}), smoke_test=True)
    monkeypatch.setattr(app, 'quit', Mock())
    try:
        window.resize(640, 480)
        window.show()
        settle(app)
        assert len(window.findChildren(QWidget, 'FullScreenButton')) == 1
        reachable(window, window.fullscreen.button)
        QTest.keyClick(window, Qt.Key_F11)
        settle(app)
        assert window.isFullScreen()
        reachable(window, window.fullscreen.button)
        QTest.keyClick(window, Qt.Key_Escape)
        settle(app)
        assert window.isVisible() and not window.isFullScreen()
        assert preferences.read_bytes() == before
    finally:
        window._quitting = True
        window.shutdown()
        window.close()
        window.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)
        settle(app)


@pytest.mark.parametrize('remembered', [False, True], ids=['first-open', 'remembered-open'])
def test_geometry_coordinator_never_centers_or_restores_onto_fullscreen(app, remembered):
    owner = QMainWindow()
    owner.setProperty('_optimus_no_reveal', True)
    owner.setGeometry(80, 90, 500, 400)
    coordinator = install_window_geometry(owner)
    owner.show()
    settle(app)
    previous = None
    window = QDialog(owner)
    window.setProperty('_optimus_no_reveal', True)
    window.setWindowTitle('Full screen fixture with session geometry')
    window.setGeometry(95, 105, 400, 300)
    original = QRect(window.geometry())
    if remembered:
        previous = QDialog(owner)
        previous.setProperty('_optimus_no_reveal', True)
        previous.setWindowTitle(window.windowTitle())
        previous.resize(520, 350)
        previous.show()
        settle(app)
        previous.hide()
    control = FullScreenControl(window)
    try:
        control.toggle()  # First Show occurs while the window is full screen.
        settle(app)
        screen = window.screen().geometry()
        assert window.isFullScreen() and window.geometry() == screen
        window.hide()
        window.show()
        settle(app)
        assert window.isFullScreen() and window.geometry() == screen
        control.exit_full_screen()
        settle(app)
        assert window.geometry() == original
    finally:
        window.hide()
        window.deleteLater()
        if previous is not None:
            previous.deleteLater()
        owner.hide()
        owner.deleteLater()
        coordinator.clear_session_geometry()
        QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)
        settle(app)
