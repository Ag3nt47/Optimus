"""Responsive control geometry and decorative motion use fixture windows only."""
import threading
import os
from pathlib import Path

import pytest
from PySide6.QtWidgets import QScrollArea
from PySide6.QtGui import QFontDatabase
from PySide6.QtCore import Qt, QPoint
from PySide6.QtTest import QTest

from optimus.ui import OptimizationProgressWindow
from optimus.widgets.header_controls import motion_allowed
from optimus.widgets.theme import build_qss
from optimus.widgets.wrapping_checkbox import WrappingCheckBox
from test_native_speed_enhancements_ui import app, window


@pytest.fixture(autouse=True)
def installed_fixture_fonts(app):
    # Qt's Windows offscreen plugin has no system font database. Register
    # installed fonts for geometry checks only, then restore the test process.
    added = []
    if os.name == 'nt' and not QFontDatabase.families():
        for name in ('segoeui.ttf', 'seguisb.ttf'):
            path = Path(os.environ.get('SystemRoot', r'C:\Windows')) / 'Fonts' / name
            if path.is_file():
                identifier = QFontDatabase.addApplicationFont(str(path))
                if identifier >= 0:
                    added.append(identifier)
    yield
    for identifier in added:
        QFontDatabase.removeApplicationFont(identifier)


def ensure_reachable(widget, app):
    parent = widget.parentWidget()
    viewports = []
    while parent is not None:
        if isinstance(parent, QScrollArea):
            parent.ensureWidgetVisible(widget, 8, 8)
            viewports.append(parent.viewport())
        parent = parent.parentWidget()
    app.processEvents()
    assert not widget.visibleRegion().isEmpty()
    for viewport in viewports:
        top = viewport.mapFromGlobal(widget.mapToGlobal(widget.rect().topLeft()))
        bottom = viewport.mapFromGlobal(widget.mapToGlobal(widget.rect().bottomRight()))
        assert viewport.rect().contains(top) and viewport.rect().contains(bottom)


@pytest.mark.parametrize('scale', [1.0, 1.25])
def test_settings_and_speed_actions_fit_small_viewport_and_are_scroll_reachable(window, app, scale):
    detail = window._settings_window
    detail.setStyleSheet(detail.styleSheet() + build_qss(scale=scale))
    detail.resize(850, 650)
    app.processEvents()
    assert detail.width() == 850 and detail.height() == 650
    panel = detail.speed_enhancements_panel
    for button in (panel.save_button, panel.review_button, panel.apply_button,
                   panel.history_button, panel.restore_button):
        ensure_reachable(button, app)
        assert button.height() >= 36
        assert button.width() >= button.fontMetrics().horizontalAdvance(button.text()) + 20
    assert panel.history.width() <= panel.width()
    if hasattr(detail, 'navigation'):
        ensure_reachable(detail.navigation['Settings'], app)


def test_main_keeps_three_centered_actions_and_pauses_stars_for_tools(window, app):
    window.resize(1024, 768)
    window.show()
    app.processEvents()
    assert len(window.main_buttons) == 3
    for button in window.main_buttons:
        center = button.mapTo(window.canvas, button.rect().center())
        assert abs(center.x() - window.canvas.rect().center().x()) <= 1
    window.canvas.motion = True
    window.canvas.sync_motion()
    assert not window.canvas.timer.isActive()  # Settings is visible.
    window._settings_window.hide()
    window.canvas.sync_motion()
    assert window.canvas.timer.isActive()
    window.busy = True
    window.canvas._tick()
    assert not window.canvas.timer.isActive()
    window.busy = False


def test_reduced_motion_keeps_decorative_background_static(window, app, monkeypatch):
    monkeypatch.setenv('OPTIMUS_REDUCE_MOTION', '1')
    window._settings_window.hide()
    window.show()
    window.canvas.motion = motion_allowed(window.config)
    window.canvas.sync_motion()
    before = window.canvas.phase
    window.canvas._tick()
    assert window.canvas.phase == before and not window.canvas.timer.isActive()


def test_foreground_and_background_progress_remain_distinct_and_readable(window, app, monkeypatch):
    monkeypatch.setenv('OPTIMUS_REDUCE_MOTION', '1')
    progress = OptimizationProgressWindow(window, threading.Event())
    try:
        progress.resize(1024, 768)
        progress.show()
        progress.update_progress({'stage': 'Inspect current resources', 'percentage': 35,
            'description': 'Foreground inspection is running.',
            'jobs': [{'id': 'software_updates', 'status': 'running', 'description': 'Approved app updates', 'current': 2, 'total': 4}]})
        app.processEvents()
        assert progress.progress.height() >= 24
        assert progress.stage.text() == 'Inspect current resources'
        assert 'Background: Approved app updates — running (2/4)' == progress.background_status.text()
        assert progress.background_status.isVisible()
        assert progress.cancel_button.geometry().bottom() < progress.height()
        progress.update_progress({'stage': 'Next foreground stage', 'jobs': []})
        assert progress.background_status.isHidden()
    finally:
        progress.finish({'status': 'completed', 'stages': []})
        progress.close()
        progress.deleteLater()
        app.processEvents()


def test_wrapped_choice_keeps_label_click_and_keyboard_toggle(window, app):
    control = WrappingCheckBox('A long saved choice remains readable and clickable when settings are narrow.')
    try:
        control.resize(230, control.heightForWidth(230))
        control.show()
        app.processEvents()
        assert control.height() > 32
        QTest.mouseClick(control, Qt.LeftButton, pos=QPoint(100, control.height() - 8))
        assert control.isChecked()
        control.setFocus()
        QTest.keyClick(control, Qt.Key_Space)
        assert not control.isChecked()
    finally:
        control.close()
        control.deleteLater()
