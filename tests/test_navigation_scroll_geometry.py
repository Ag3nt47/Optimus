"""Native sidebar metrics fit the complete action without hidden scrolling."""
import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

import pytest
from PySide6.QtCore import QPoint, Qt
from PySide6.QtWidgets import QApplication, QHBoxLayout, QPushButton, QVBoxLayout, QWidget

from optimus.widgets.navigation_scroll import NavigationScrollArea


@pytest.fixture(scope='module')
def app():
    return QApplication.instance() or QApplication([])


@pytest.mark.parametrize('required', [170, 180, 205])
def test_sidebar_fits_real_action_minimum_after_native_scrollbar_subtracts_space(app, required):
    window, sidebar, content = QWidget(), QWidget(), QWidget()
    window.resize(850, 650)
    shell = QHBoxLayout(window)
    shell.setContentsMargins(0, 0, 0, 0)
    sidebar.setFixedWidth(205)
    layout = QVBoxLayout(sidebar)
    layout.setContentsMargins(13, 22, 13, 15)
    scroll = NavigationScrollArea(sidebar)
    scroll.setFrameShape(NavigationScrollArea.NoFrame)
    scroll.setWidgetResizable(True)
    body = QVBoxLayout(content)
    body.setContentsMargins(0, 0, 0, 0)
    actions = []
    for index in range(25):
        action = QPushButton('Fixture navigation ' + str(index))
        action.setMinimumWidth(required)
        action.setMinimumHeight(38)
        body.addWidget(action)
        actions.append(action)
    scroll.setWidget(content)
    layout.addWidget(scroll)
    shell.addWidget(sidebar)
    shell.addWidget(QWidget(), 1)
    window.show()
    try:
        for _ in range(8):
            app.processEvents()
        scroll.ensureWidgetVisible(actions[-1], 8, 8)
        app.processEvents()
        top = actions[-1].mapTo(scroll.viewport(), QPoint(0, 0))
        assert top.x() == 0
        assert actions[-1].width() <= scroll.viewport().width()
        assert scroll.horizontalScrollBar().value() == 0
        assert not scroll._fit_timer.isActive()
        # Fitting must settle, instead of continuously changing the sidebar.
        width = sidebar.width()
        for _ in range(5):
            app.processEvents()
        assert sidebar.width() == width and not scroll._fit_timer.isActive()
        assert window.width() == 850
    finally:
        window.close()
        window.deleteLater()
        app.processEvents()
