"""Measured action flow fits native scroll viewports without horizontal drift."""
import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

from types import SimpleNamespace
import pytest
from PySide6.QtCore import QPoint, Qt
from PySide6.QtWidgets import QApplication, QScrollArea, QVBoxLayout, QWidget

from optimus.widgets.speed_enhancements_panel import SpeedEnhancementsPanel
from optimus.widgets.theme import build_qss


@pytest.fixture(scope='module')
def app():
    return QApplication.instance() or QApplication([])


@pytest.mark.parametrize('width', [520, 563, 650])
@pytest.mark.parametrize('scale', [1.0, 1.25])
def test_speed_actions_wrap_measured_minimums_without_widening_scroll_content(app, width, scale):
    window = QWidget()
    window.backend = SimpleNamespace(system='win32')
    window.setStyleSheet(build_qss(scale=scale))
    scroll = QScrollArea(window)
    scroll.setWidgetResizable(True)
    scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
    scroll.setGeometry(0, 0, width, 450)
    window.resize(width, 450)
    body = QWidget()
    layout = QVBoxLayout(body)
    panel = SpeedEnhancementsPanel(window)
    # Reproduce fonts whose two restore controls cannot share a 563px row.
    panel.history_button.setMinimumWidth(285)
    panel.restore_button.setMinimumWidth(310)
    layout.addWidget(panel)
    scroll.setWidget(body)
    window.show()
    try:
        for _ in range(8):
            app.processEvents()
        for button in (panel.save_button, panel.review_button, panel.apply_button,
                       panel.history_button, panel.restore_button):
            scroll.ensureWidgetVisible(button, 8, 8)
            app.processEvents()
            bounds = button.rect().translated(button.mapTo(scroll.viewport(), QPoint(0, 0)))
            assert scroll.viewport().rect().contains(bounds), (button.text(), bounds, scroll.viewport().rect())
            assert button.width() >= button.minimumSizeHint().width()
        assert body.width() == scroll.viewport().width()
        assert scroll.horizontalScrollBar().value() == 0
        assert window.width() == width
        if width < 650:
            assert panel.history_button.y() < panel.restore_button.y()
    finally:
        panel.shutdown()
        window.close()
        window.deleteLater()
        app.processEvents()
