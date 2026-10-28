"""Responsive control geometry and decorative motion use fixture windows only."""
import threading
import os
import json
from pathlib import Path

import pytest
from PySide6.QtWidgets import QLabel, QPushButton, QScrollArea, QWidget
from PySide6.QtGui import QFontDatabase
from PySide6.QtCore import Qt, QPoint, QRect, QTimer
from PySide6.QtTest import QTest

from optimus.ui import OptimizationProgressWindow
from optimus.widgets.header_controls import motion_allowed
from optimus.widgets.theme import build_qss
from optimus.widgets.wrapping_checkbox import WrappingCheckBox
from test_native_speed_enhancements_ui import app, window


@pytest.fixture(autouse=True)
def virtual_fixture_desktop(monkeypatch):
    # These fixtures measure exact 850/1024 client viewports, independently of
    # the offscreen plugin's artificial 800x800 desktop. Keep the production
    # fitter active against an explicit virtual screen; genuine small-screen
    # bounds and DPI behavior are covered by test_native_window_geometry.
    monkeypatch.setattr('optimus.widgets.window_geometry.available_screen_geometry',
                        lambda _: QRect(0, 0, 1920, 1080))


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
    scrolls = []
    while parent is not None:
        if isinstance(parent, QScrollArea):
            scrolls.append(parent)
        parent = parent.parentWidget()
    def snapshot():
        return [{'viewport': scroll.viewport().size().toTuple(),
            'content': scroll.widget().size().toTuple(),
            'content_minimum': scroll.widget().minimumSizeHint().toTuple(),
            'button': widget.size().toTuple(),
            'position': widget.mapTo(scroll.viewport(), QPoint(0, 0)).toTuple(),
            'horizontal': [scroll.horizontalScrollBar().value(), scroll.horizontalScrollBar().maximum()],
            'vertical': [scroll.verticalScrollBar().value(), scroll.verticalScrollBar().maximum()]}
            for scroll in scrolls]
    first = snapshot()
    # A nested height-for-width layout may change when the first actual scroll
    # request drains pending native style/layout events. Repeat navigation
    # against that settled geometry; the complete final hit area still must fit.
    for _ in range(2):
        app.processEvents()
        for scroll in scrolls:
            scroll.ensureWidgetVisible(widget, 8, 8)
            app.processEvents()
    final = snapshot()
    assert not widget.visibleRegion().isEmpty()
    for scroll in scrolls:
        viewport = scroll.viewport()
        top = viewport.mapFromGlobal(widget.mapToGlobal(widget.rect().topLeft()))
        bottom = viewport.mapFromGlobal(widget.mapToGlobal(widget.rect().bottomRight()))
        evidence = {'label': widget.text() if hasattr(widget, 'text') else widget.objectName(),
            'first': first, 'settled': final, 'final': {'viewport': viewport.size().toTuple(),
                'button': widget.size().toTuple(), 'top': top.toTuple(), 'bottom': bottom.toTuple()}}
        assert viewport.rect().contains(top) and viewport.rect().contains(bottom), json.dumps(evidence, indent=2)
    return {'first': first, 'final': final}


def test_reachability_repeats_real_navigation_after_a_pending_layout_moves_the_action(app):
    class PendingLayoutScroll(QScrollArea):
        adjusted = False
        def ensureWidgetVisible(self, child, *margins):
            super().ensureWidgetVisible(child, *margins)
            if not self.adjusted:
                self.adjusted = True
                QTimer.singleShot(0, lambda: child.move(child.x(), child.y() + 60))
    window = QWidget()
    scroll = PendingLayoutScroll(window)
    scroll.setGeometry(0, 0, 260, 200)
    body = QWidget()
    body.resize(230, 800)
    button = QPushButton('Pending layout action', body)
    button.setGeometry(10, 130, 180, 40)
    scroll.setWidget(body)
    window.resize(260, 200)
    window.show()
    app.processEvents()
    try:
        measured = ensure_reachable(button, app)
        assert measured['first'][0]['vertical'][0] == 0
        assert measured['final'][0]['vertical'][0] > 0
        assert scroll.viewport().rect().contains(button.rect().translated(button.mapTo(scroll.viewport(), QPoint(0, 0))))
    finally:
        window.close()
        window.deleteLater()
        app.processEvents()


def test_reachability_still_rejects_a_button_wider_than_its_viewport(app):
    window = QWidget()
    scroll = QScrollArea(window)
    scroll.setGeometry(0, 0, 220, 100)
    body = QWidget()
    body.resize(400, 100)
    button = QPushButton('Oversized fixture action', body)
    button.setGeometry(0, 10, 350, 40)
    scroll.setWidget(body)
    window.resize(220, 100)
    window.show()
    app.processEvents()
    try:
        with pytest.raises(AssertionError, match='Oversized fixture action'):
            ensure_reachable(button, app)
    finally:
        window.close()
        window.deleteLater()
        app.processEvents()


def test_long_windows_heading_wraps_without_widening_a_563px_workflow_viewport(window, app):
    detail = window._settings_window
    heading = detail.findChild(QLabel, 'WindowsUpdateCompatibilityHeading')
    if heading is None:
        # The native form has a different short section heading; the existing
        # parameterized reachability test exercises its actual layout instead.
        return
    detail.setStyleSheet(detail.styleSheet() + build_qss(scale=1.25))
    detail.resize(850, 650)
    heading.setStyleSheet('font-size:18px;')
    # Native DejaVu metrics exposed these two formerly fixed action pairs.
    # Exercise the same measured-width pressure on every platform, without
    # depending on which optional font families the test machine installed.
    for button in detail.findChildren(QPushButton):
        if button.objectName() in ('CheckSignInLaunch', 'ApplySignInLaunch') or button.text() in (
                'Select all consumer packages', 'Clear all consumer packages'):
            button.setMinimumWidth(300)
    parent = heading.parentWidget()
    while not isinstance(parent, QScrollArea):
        parent = parent.parentWidget()
    for _ in range(4):
        app.processEvents()
    parent.setFixedWidth(parent.width() + 563 - parent.viewport().width())
    for _ in range(4):
        app.processEvents()
    assert parent.viewport().width() == 563
    assert heading.fontMetrics().horizontalAdvance(heading.text()) > 563
    assert heading.wordWrap() and heading.heightForWidth(heading.width()) > heading.fontMetrics().height()
    assert parent.widget().width() == parent.viewport().width()
    for button in (detail.speed_enhancements_panel.save_button, detail.speed_enhancements_panel.review_button,
                   detail.speed_enhancements_panel.apply_button, detail.speed_enhancements_panel.history_button,
                   detail.speed_enhancements_panel.restore_button):
        ensure_reachable(button, app)


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


def test_main_keeps_four_centered_actions_and_pauses_stars_for_tools(window, app):
    window.resize(1024, 768)
    window.show()
    app.processEvents()
    assert len(window.main_buttons) == 4
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
        assert progress.background_status.toolTip() == progress.background_status.text()
        assert progress.background_status.accessibleDescription() == progress.background_status.text()
        assert not progress.details_toggle.isChecked() and progress.background_status.isVisible()
        assert progress.analysis_panel.isAncestorOf(progress.background_status)
        assert not progress.content_scroll.isAncestorOf(progress.background_status)
        card = progress.worker_cards['software_updates']
        assert card.status.text() == 'Running' and card.progress.value() == 2 and card.progress.maximum() == 4
        for field in (card.heading, card.status, card.activity_ring, card.progress, card.totals,
                      *card.navigation_buttons.values()):
            assert field.isVisible()
            assert card.rect().contains(QRect(field.mapTo(card, QPoint()), field.size()))
            assert progress.rect().contains(QRect(field.mapTo(progress, QPoint()), field.size()))
        assert 'Approved app updates' in progress.worker_details.toPlainText()
        progress.details_toggle.click()
        app.processEvents()
        progress.body_scroll.ensureWidgetVisible(progress.background_status)
        app.processEvents()
        assert progress.background_status.isVisible() and not progress.background_status.visibleRegion().isEmpty()
        shown = QRect(progress.background_status.mapTo(progress.body_scroll.viewport(), QPoint()),
                      progress.background_status.size())
        assert progress.body_scroll.viewport().rect().contains(shown)
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
