"""Pinned worker geometry uses disposable Qt fixtures and never starts host work."""
import os
from pathlib import Path
import threading

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

import pytest
from PySide6.QtCore import QCoreApplication, QEvent, QPoint, QRect
from PySide6.QtGui import QFontDatabase
from PySide6.QtWidgets import QAbstractScrollArea, QApplication, QWidget

from optimus.ui import OptimizationProgressWindow
from optimus.widgets.theme import build_qss


CORE = ('maintenance', 'software_updates', 'driver_updates', 'security_scan')
WIDTHS = (480, 650, 850, 1024, 1280)
HEIGHTS = (450, 550, 650, 768, 900)


@pytest.fixture(scope='module')
def app():
    value = QApplication.instance() or QApplication([])
    registered = []
    if os.name == 'nt' and not QFontDatabase.families():
        for name in ('segoeui.ttf', 'seguisb.ttf'):
            path = Path(os.environ.get('SystemRoot', r'C:\Windows')) / 'Fonts' / name
            if path.is_file():
                identity = QFontDatabase.addApplicationFont(str(path))
                if identity >= 0:
                    registered.append(identity)
    yield value
    for identity in registered:
        QFontDatabase.removeApplicationFont(identity)


def settle(app):
    for _ in range(5):
        app.processEvents()


def set_font(owner, pixels):
    owner.setStyleSheet(build_qss() + f'\nQFrame#WorkflowWorkerCard QLabel, '
        f'QFrame#WorkflowWorkerCard QProgressBar {{font-size:{pixels}px;}}')


def state():
    return {'stage': 'Clean temporary files and verify recorded outcomes', 'percentage': 44,
        'description': 'A long maintenance detail that must stay available without enlarging the pinned worker region. ' * 8,
        'resource_totals': {'removed_known': True, 'removed_bytes': 2147483648, 'removed_files': 12799},
        'item_progress': {'key': 'cleanup:fixture', 'label': 'Checking selected files', 'phase': 'cleanup', 'percent': 62},
        'jobs': [
            {'id': 'software_updates', 'status': 'installing', 'current': 1, 'total': 11,
             'description': 'Downloading approved Example.Application.With.A.Deliberately.Long.Package.Name 27.18.1234.5678. ' * 8,
             'resource_totals': {'installed_known': True, 'installed_items': 1,
                 'downloaded_known': True, 'downloaded_bytes': 345610649,
                 'downloaded_estimated': True, 'downloaded_partial': True},
             'item_progress': {'key': 'app:fixture', 'label': 'Example application', 'phase': 'download', 'percent': 81}},
            {'id': 'driver_updates', 'status': 'waiting_for_installer', 'current': 0, 'total': 2,
             'description': 'Waiting for the approved application installer to return before the next network driver. ' * 8,
             'resource_totals': {'installed_known': True, 'installed_items': 0, 'downloaded_known': False},
             'item_progress': {'key': 'driver:fixture', 'label': 'Example network driver', 'phase': 'waiting', 'percent': None}}]}


def make_window(app, pixels=13):
    owner = QWidget()
    owner.smoke_test = True
    set_font(owner, pixels)
    window = OptimizationProgressWindow(owner, threading.Event())
    window._optimus_no_reveal = True
    window.configure_workers({'software_updates_enabled': True,
        'driver_updates_enabled': True, 'spyware_scan_enabled': True})
    window.show()
    window.update_progress(state())
    window.update_progress({'stage': 'Scan for spyware and malware', 'percentage': 74,
        'scan_status': 'scanning', 'item_progress': {'key': 'security:defender:quick_scan',
            'label': 'Fixture scan', 'phase': 'scan', 'percent': None}})
    for identity in CORE:
        window.worker_cards[identity].set_navigation({'session_id': 'a' * 32, 'revision': 1,
            'can_back': True, 'can_next': True, 'can_cancel': True,
            'message': 'Navigation detail stays available and cannot stretch a pinned card. ' * 12})
    settle(app)
    return owner, window


def dispose(owner, window, app):
    window.completed = True
    window.close()
    owner.close()
    window.deleteLater()
    owner.deleteLater()
    QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)
    app.processEvents()


def in_window(widget, window):
    return widget.rect().translated(widget.mapTo(window, QPoint()))


def assert_pinned_geometry(window, *, pixels, requested):
    context = f'font={pixels}px requested={requested} actual={window.width(), window.height()}'
    assert window.width() >= window.minimumWidth(), context
    assert window.height() >= window.minimumHeight(), context
    assert window.worker_grid._columns == 2, context
    assert window.rect().contains(in_window(window.worker_grid, window)), context
    cards = [window.worker_cards[identity] for identity in CORE]
    assert all(card.isVisibleTo(window) for card in cards), context
    for index, card in enumerate(cards):
        assert window.worker_grid.rect().contains(card.geometry()), (context, index, card.geometry())
        assert window.rect().contains(in_window(card, window)), context
        assert card.completion.fontMetrics().height() >= pixels, context
        parent = card.parentWidget()
        while parent is not None and parent is not window:
            assert not isinstance(parent, QAbstractScrollArea), (context, 'Core worker is inside a scroll area')
            parent = parent.parentWidget()
        essentials = [card.heading, card.status, card.progress, card.totals, card.activity_ring,
            *card.navigation_buttons.values()]
        for field in essentials:
            assert field.isVisibleTo(window), (context, field.objectName())
            assert field.width() > 0 and field.height() > 0, context
            mapped = field.rect().translated(field.mapTo(card, QPoint()))
            assert card.contentsRect().contains(mapped), (context, field.objectName(), mapped, card.rect())
            assert window.rect().contains(in_window(field, window)), context
        assert not card.heading.geometry().intersects(card.activity_ring.geometry()), context
        assert card.status.geometry().bottom() < card.progress.geometry().top(), context
        assert card.progress.geometry().bottom() < card.totals.geometry().top(), context
        assert card.totals.geometry().bottom() < card.navigation_buttons['next'].geometry().top(), context
        assert card.progress.height() >= card.progress.fontMetrics().height() + 6, context
        assert card.progress.fontMetrics().horizontalAdvance(card.progress.visible_text) <= card.progress.width() - 10, context
        for label in (card.heading, card.status, card.totals):
            assert label.toolTip() and label.accessibleDescription(), context
            assert all(label.fontMetrics().horizontalAdvance(line) <= label.contentsRect().width()
                for line in label.visible_text.splitlines()), context
        buttons = list(card.navigation_buttons.values())
        for left_index, left in enumerate(buttons):
            for right in buttons[left_index + 1:]:
                assert not left.geometry().intersects(right.geometry()), context
            # A direct hit test catches a footer or details pane overlay even if
            # all individual rectangles happen to lie inside the window.
            point = left.mapTo(window, left.rect().center())
            hit = window.childAt(point)
            assert hit is left or hit is not None and left.isAncestorOf(hit), (context, left.objectName(), hit)
        for other in cards[index + 1:]:
            assert not in_window(card, window).intersects(in_window(other, window)), context
    assert window.cancel_button.isVisibleTo(window), context
    assert window.rect().contains(in_window(window.cancel_button, window)), context
    assert in_window(window.worker_grid, window).bottom() < in_window(window.cancel_button, window).top(), context
    assert window.content_scroll.horizontalScrollBar().maximum() == 0, context
    assert not window.cancel.is_set(), context


@pytest.mark.parametrize('pixels', (13, 20, 26))
def test_all_four_workers_fit_every_requested_resize_without_grid_scrolling(app, pixels):
    owner, window = make_window(app, pixels)
    try:
        # Reuse the same live dialog through every combination to exercise both
        # expansion and contraction rather than only initial size hints.
        for width in WIDTHS:
            for height in reversed(HEIGHTS):
                window.resize(width, height)
                settle(app)
                assert_pinned_geometry(window, pixels=pixels, requested=(width, height))
                if pixels == 13 and (width, height) == (850, 650):
                    assert (window.width(), window.height()) == (850, 650)
    finally:
        dispose(owner, window, app)


def test_large_font_changes_and_navigation_notes_preserve_four_visible_cards(app):
    owner, window = make_window(app)
    try:
        for pixels in (26, 20, 13, 26):
            set_font(owner, pixels)
            window.resize(850, 650)
            settle(app)
            assert_pinned_geometry(window, pixels=pixels, requested=(850, 650))
            card = window.worker_cards['software_updates']
            assert 'Navigation detail' in card.navigation_note.text()
            assert card.navigation_note.toolTip() == card.navigation_note.text()
            assert 'Deliberately.Long.Package.Name' in card.details.toolTip()
    finally:
        dispose(owner, window, app)


def test_current_package_is_visible_in_the_compact_status_with_full_hover_text(app):
    owner, window = make_window(app)
    try:
        window.resize(850, 650)
        settle(app)
        card = window.worker_cards['software_updates']
        assert card.status.text() == 'Downloading'
        assert card.status.visible_text.startswith('Downloading · Example application')
        assert card.status.toolTip() == 'Downloading · Example application'
        assert card.status.accessibleDescription() == card.status.toolTip()
        card.update_state({'status': 'installing', 'current': 2, 'total': 5,
            'current_package': 'Vendor.Exact.App.With.A.Long.Name'})
        window.worker_navigation.refresh()
        assert 'Vendor.Exact.App' in card.status.visible_text
        assert card.status.toolTip().endswith('Vendor.Exact.App.With.A.Long.Name')
        assert_pinned_geometry(window, pixels=13, requested=(850, 650))
    finally:
        dispose(owner, window, app)


def test_optional_workers_cannot_expand_or_scroll_the_core_grid(app):
    owner, window = make_window(app)
    try:
        window.resize(850, 650)
        settle(app)
        before = {identity: in_window(window.worker_cards[identity], window) for identity in CORE}
        window.update_progress({'stage': 'Inspect cached optional worker states', 'percentage': 78,
            'jobs': [dict(id=identity, title='Optional ' + identity, status='running',
                description='Cached fixture only; no provider was started.', current=None, total=None)
                for identity in ('driver_backup', 'system_restore', 'file_verify', 'smart_cleaning')]})
        settle(app)
        assert set(CORE) <= set(window.worker_cards)
        for identity in ('driver_backup', 'system_restore', 'file_verify', 'smart_cleaning'):
            assert identity in window.worker_cards
            assert not window.worker_grid.isAncestorOf(window.worker_cards[identity])
        assert {identity: in_window(window.worker_cards[identity], window) for identity in CORE} == before
        assert_pinned_geometry(window, pixels=13, requested=(850, 650))
    finally:
        dispose(owner, window, app)


def test_disabled_scan_stays_visible_and_unknown_progress_stays_indeterminate(app):
    owner, window = make_window(app)
    try:
        window.configure_workers({'software_updates_enabled': True,
            'driver_updates_enabled': True, 'spyware_scan_enabled': False})
        window.resize(850, 650)
        settle(app)
        scan = window.worker_cards['security_scan']
        assert scan._raw_status == 'not_selected'
        assert scan.isVisibleTo(window)
        assert 'disabled' in scan.details.text().lower()
        assert scan.activity_ring.value_percent is None
        software = window.worker_cards['software_updates']
        software.update_state({'status': 'uninstalling', 'current': 3, 'total': 4,
            'description': 'Uninstall approved fixture application.',
            'item_progress': {'key': 'app:fixture', 'label': 'Fixture app', 'phase': 'uninstall', 'percent': None}})
        settle(app)
        assert software.activity_ring.value_percent is None
        assert software.activity_ring.active
        assert software.activity_ring.phase_caption == 'Uninstall'
        assert software.progress.value() == 3 and software.progress.maximum() == 4
        assert '3 / 4' in software.progress.text()
        assert 'not reported' in software.activity_ring.toolTip().lower()
        assert_pinned_geometry(window, pixels=13, requested=(850, 650))
    finally:
        dispose(owner, window, app)


@pytest.mark.parametrize('pixels', (13, 20, 26))
def test_open_details_drive_warning_and_result_controls_keep_the_core_visible(app, pixels):
    owner, window = make_window(app, pixels)
    try:
        window._update_space_guard({'enabled': True, 'complete': False, 'target_bytes': 2147483648,
            'volumes': [{'name': 'C:', 'free_bytes': 10, 'shortfall_bytes': 2147483638, 'below_target': True}],
            'summary': 'Uncertain secondary volume. ' * 80})
        window.auto_continue_button.show()
        window.details_toggle.setChecked(True)
        window.resize(850, 650)
        settle(app)
        assert_pinned_geometry(window, pixels=pixels, requested=(850, 650))
        assert window.guard_summary.isVisibleTo(window)
        assert window.guard_summary.visible_text.startswith('WARNING')
        assert 'shortfall' in window.guard_summary.toolTip()
        assert window.content_scroll.isVisibleTo(window)
        assert window.content_scroll.viewport().height() > 0
        assert window.rect().contains(in_window(window.details_toggle, window))
        assert window.rect().contains(in_window(window.auto_continue_button, window))
        assert in_window(window.worker_grid, window).bottom() < in_window(window.details_toggle, window).top()
        assert in_window(window.content_scroll, window).bottom() < in_window(window.auto_continue_button, window).top()
        assert 'Deliberately.Long.Package.Name' in window.worker_details.toPlainText()
    finally:
        dispose(owner, window, app)


def test_screen_fitting_cannot_lower_the_live_control_minimum(app):
    from optimus.widgets.window_geometry import fit_window_to_screen
    owner, window = make_window(app, 26)
    try:
        minimum = window.minimumSize()
        fit_window_to_screen(window, available=QRect(0, 0, 480, 450))
        settle(app)
        assert window.minimumSize() == minimum
        assert_pinned_geometry(window, pixels=26, requested=(480, 450))
    finally:
        dispose(owner, window, app)


def test_layout_settles_without_an_idle_zero_interval_reflow_loop(app):
    owner, window = make_window(app)
    try:
        window._worker_elapsed_timer.stop()
        window.spinner.stop()
        window.loading_animation.stop()
        for card in window.worker_cards.values():
            card.activity_ring.timer.stop()
        for _ in range(12):
            app.processEvents()
        assert not window.worker_grid._reflow_timer.isActive()
        assert not window._responsive_timer.isActive()
    finally:
        dispose(owner, window, app)
