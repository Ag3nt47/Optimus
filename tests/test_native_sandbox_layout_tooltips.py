"""The editor uses remaining space; hover descriptions perform no action."""
import os
from types import SimpleNamespace

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

import pytest
from PySide6.QtCore import QEvent
from PySide6.QtWidgets import QApplication, QPushButton, QWidget

from optimus.sandbox import SandboxWindow
from optimus.widgets.button_tooltips import install_button_descriptions


@pytest.fixture(scope='module')
def app():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def editor(tmp_path, app):
    source = tmp_path / 'source'
    (source / 'src/optimus').mkdir(parents=True)
    (source / 'src/optimus/ui.py').write_text('VALUE = 1\n', encoding='utf-8')
    (source / 'launch.py').write_text('# Never executed fixture\n', encoding='utf-8')
    window = SandboxWindow(SimpleNamespace(source_root=lambda: source, data_dir=tmp_path / 'data'))
    window.setProperty('_optimus_no_reveal', True)
    yield window
    window.editor.document().setModified(False)
    window.close()
    window.deleteLater()
    app.processEvents()


@pytest.mark.parametrize('width,height', [(640, 480), (850, 650), (1280, 850)])
def test_editor_fills_remaining_window_and_toolbars_stay_compact(editor, app, width, height):
    editor.resize(width, height)
    editor.show()
    for _ in range(3):
        app.processEvents()
    assert editor.centralWidget().rect().contains(editor.splitter.geometry())
    assert editor.splitter.height() > editor.centralWidget().height() * 0.55
    assert editor.editor.height() == editor.splitter.height()
    assert editor.tree.height() == editor.splitter.height()
    assert editor.description.height() < 90
    assert editor.status.geometry().top() > editor.splitter.geometry().bottom()
    assert all(button.toolTip() for button in editor.findChildren(QPushButton))
    before = editor.editor.height()
    editor.resize(editor.width(), editor.height() + 150)
    app.processEvents()
    assert editor.editor.height() >= before + 140


def test_main_actions_explain_scope_without_running_any_callback(app):
    window = QWidget()
    buttons = [QPushButton(text, window) for text in ('1 click Optimize and debloat', 'Settings', 'Sandbox', 'Revert')]
    helper = install_button_descriptions(window)
    assert 'saved cleanup' in buttons[0].toolTip()
    assert 'automatically' in buttons[1].toolTip()
    assert 'full Optimus source code' in buttons[2].toolTip()
    assert 'undo' in buttons[3].toolTip()
    dynamic = QPushButton('→', window)
    dynamic.setProperty('_optimus_navigation_action', 'next')
    app.sendEvent(dynamic, QEvent(QEvent.Enter))
    assert 'safe skip' in dynamic.toolTip() and 'installer' in dynamic.toolTip()
    dynamic.setToolTip('Exact native safety reason supplied by this tool.')
    helper.describe(dynamic)
    assert dynamic.toolTip() == 'Exact native safety reason supplied by this tool.'
    window.close()
    window.deleteLater()
    app.processEvents()
