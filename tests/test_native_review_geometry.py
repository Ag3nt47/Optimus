"""Independent clipping checks, including a fixture that ordinary bounds miss."""
import os
import threading

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

import pytest
from PySide6.QtCore import QPoint, QRect
from PySide6.QtWidgets import QApplication, QPushButton, QWidget

from optimus.ui import OptimizationProgressWindow
from optimus.widgets.theme import build_qss


@pytest.fixture(scope='module')
def app():
    return QApplication.instance() or QApplication([])


def unoccluded_by_ancestors(control, window):
    """Window bounds alone do not prove that an intermediate parent shows it."""
    assert control.isVisibleTo(window), 'Control or an ancestor is hidden'
    ancestor = control.parentWidget()
    while ancestor is not None:
        mapped = control.rect().translated(control.mapTo(ancestor, QPoint()))
        assert ancestor.contentsRect().contains(mapped), ('Ancestor clips a control',
            type(control).__name__, type(ancestor).__name__, mapped, ancestor.contentsRect())
        if ancestor is window:
            return
        ancestor = ancestor.parentWidget()
    raise AssertionError('Control is outside the reviewed window hierarchy')


def test_negative_fixture_detects_parent_clipping_that_window_bounds_miss(app):
    window = QWidget()
    window.resize(640, 480)
    parent = QWidget(window)
    parent.setGeometry(20, 20, 200, 40)
    button = QPushButton('Partially clipped fixture', parent)
    button.setGeometry(0, 30, 180, 30)
    window.show()
    app.processEvents()
    try:
        assert window.rect().contains(QRect(button.mapTo(window, QPoint()), button.size()))
        assert button.isVisibleTo(window)
        with pytest.raises(AssertionError, match='Ancestor clips a control'):
            unoccluded_by_ancestors(button, window)
    finally:
        window.close()
        window.deleteLater()
        app.processEvents()


@pytest.mark.parametrize('pixels', (13, 26))
def test_core_controls_remain_visible_after_details_warning_and_font_changes(app, pixels):
    owner = QWidget()
    owner.smoke_test = True
    owner.setStyleSheet(build_qss())
    window = OptimizationProgressWindow(owner, threading.Event())
    window.setProperty('_optimus_no_reveal', True)
    window.configure_workers({'spyware_scan_enabled': True})
    window.resize(850, 650)
    window.show()
    try:
        for current_font in (pixels, 20, pixels):
            owner.setStyleSheet(build_qss() + '\nQFrame#WorkflowWorkerCard QLabel, '
                f'QFrame#WorkflowWorkerCard QProgressBar {{font-size:{current_font}px;}}')
            for details in (False, True, False):
                window.details_toggle.setChecked(details)
                window.update_progress({'stage': 'Fixture maintenance', 'percentage': 37,
                    'space_guard': {'enabled': True, 'complete': True,
                        'volumes': [{'name': f'Fixture-{index}:', 'free_bytes': 0,
                            'target_bytes': 2147483648, 'shortfall_bytes': 2147483648,
                            'below_target': True} for index in range(32)],
                        'warnings': ['Fixture warning only; no host volume was queried.']}})
                for _ in range(8):
                    app.processEvents()
                for identity in ('maintenance', 'software_updates', 'driver_updates', 'security_scan'):
                    card = window.worker_cards[identity]
                    fields = [card.heading, card.status, card.progress, card.totals,
                              card.activity_ring, *card.navigation_buttons.values()]
                    for field in fields:
                        # The body deliberately scrolls on enlarged text or
                        # expanded logs. Check actual ancestor containment at
                        # the real reached position, not just logical visibility.
                        window.body_scroll.ensureWidgetVisible(field, 0, 2)
                        for _ in range(3):
                            app.processEvents()
                        unoccluded_by_ancestors(field, window)
                    for first, second in ((card.heading, card.status), (card.status, card.progress),
                                          (card.progress, card.totals)):
                        first_rect = first.rect().translated(first.mapTo(window, QPoint()))
                        second_rect = second.rect().translated(second.mapTo(window, QPoint()))
                        assert not first_rect.intersects(second_rect), 'Essential text rows overlap'
                    for button in card.navigation_buttons.values():
                        window.body_scroll.ensureWidgetVisible(button, 0, 2)
                        app.processEvents()
                        hit = window.childAt(button.mapTo(window, button.rect().center()))
                        assert hit is button or hit is not None and button.isAncestorOf(hit)
    finally:
        window.completed = True
        window.close()
        owner.close()
        window.deleteLater()
        owner.deleteLater()
        app.processEvents()
