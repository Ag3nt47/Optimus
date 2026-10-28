"""The visible analysis panel uses synthetic counters, never host operations."""
import threading

import pytest
from PySide6.QtCore import QPoint, Qt
from PySide6.QtGui import QMovie
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QWidget

from optimus.ui import OptimizationProgressWindow
from optimus.widgets.theme import build_qss
from test_native_worker_cards import app, fonts
from test_native_auto_continue_ui import wait_until


CORE = ('maintenance', 'software_updates', 'driver_updates', 'security_scan')


@pytest.fixture
def progress(app, monkeypatch):
    monkeypatch.setenv('OPTIMUS_REDUCE_MOTION', '1')
    owner = QWidget()
    owner.smoke_test = True
    owner.setStyleSheet(build_qss())
    value = OptimizationProgressWindow(owner, threading.Event())
    value.configure_workers({'software_updates_enabled': True,
        'driver_updates_enabled': True, 'spyware_scan_enabled': False})
    value.show()
    wait_until(app, lambda: value.body_scroll.viewport().height() > 100)
    yield value
    value.completed = True
    value.close()
    owner.close()
    value.deleteLater()
    owner.deleteLater()
    app.processEvents()


def mapped(widget, ancestor):
    return widget.rect().translated(widget.mapTo(ancestor, QPoint()))


def assert_hit(window, widget):
    point = widget.mapTo(window, widget.rect().center())
    assert window.rect().contains(mapped(widget, window))
    hit = window.childAt(point)
    assert hit is widget or hit is not None and widget.isAncestorOf(hit), (widget.objectName(), hit)


def reach(window, widget, app):
    if widget.height() > window.body_scroll.viewport().height():
        # A complete wrapping drive report can be taller than a small viewport.
        # Its first and last painted rows must both scroll to real hit targets;
        # unlike an elided summary, all intervening text remains in the body.
        for y in (widget.fontMetrics().height() // 2,
                widget.height() - widget.fontMetrics().height() // 2):
            local = QPoint(widget.width() // 2, y)
            point = widget.mapTo(window.body_scroll.widget(), local)
            window.body_scroll.ensureVisible(point.x(), point.y(), 0, 3)
            wait_until(app, lambda: window.body_scroll.viewport().rect().contains(
                widget.mapTo(window.body_scroll.viewport(), local)))
            hit = window.childAt(widget.mapTo(window, local))
            assert hit is widget or hit is not None and widget.isAncestorOf(hit)
        return
    window.body_scroll.ensureWidgetVisible(widget, 0, 2)
    wait_until(app, lambda: window.body_scroll.viewport().rect().contains(mapped(widget, window.body_scroll.viewport())))
    assert_hit(window, widget)


def drive_check():
    return {'enabled': True, 'complete': True, 'target_bytes': 2 * 2 ** 30,
        'volumes': [{'name': 'C:', 'free_bytes': 1 * 2 ** 30, 'target_bytes': 2 * 2 ** 30,
            'shortfall_bytes': 1 * 2 ** 30, 'below_target': True},
            {'name': 'D:', 'free_bytes': 60 * 2 ** 30, 'target_bytes': 2 * 2 ** 30,
                'shortfall_bytes': 0, 'below_target': False}]}


def test_normal_dashboard_and_bottom_analysis_fit_without_opening_logs(progress, app):
    value = progress
    value._update_space_guard(drive_check())
    value.live_system_status.update_metrics({'computer': {'cpu_percent': 27.4, 'memory_percent': 63.2,
        'cpu_frequency_mhz': 3200}, 'network': {'kind': 'wifi', 'adapter': 'Fixture Wi-Fi',
        'is_up': True, 'receive_mbps': 12.5, 'send_mbps': 1.2, 'link_mbps': 300,
        'internet_state': 'unverified'}})
    value.resize(850, 650)
    wait_until(app, lambda: value.body_scroll.verticalScrollBar().maximum() == 0
        and value.body_scroll.viewport().rect().contains(mapped(value.analysis_panel, value.body_scroll.viewport())))
    assert value.size().toTuple() == (850, 650)
    assert value.worker_grid._columns == 2
    assert not value.details_toggle.isChecked() and not value.log.isVisible() and not value.processes.isVisible()
    for widget in (value.analysis_panel, value.loading_animation, value.live_system_status.computer,
            value.live_system_status.network, value.space_guard_status, *value.worker_cards.values()):
        assert widget.isVisibleTo(value)
        assert value.body_scroll.viewport().rect().contains(mapped(widget, value.body_scroll.viewport()))
        assert not value.content_scroll.isAncestorOf(widget)
    assert '27.4%' in value.live_system_status.computer.text()
    assert '63.2%' in value.live_system_status.computer.text()
    assert '12.50 Mbps' in value.live_system_status.network.text()
    assert 'WARNING' in value.space_guard_status.text()
    assert 'C: free 1.00 GiB' in value.space_guard_status.text()
    assert 'target 2.00 GiB' in value.space_guard_status.text()
    assert 'shortfall 1.00 GiB' in value.space_guard_status.text()
    assert 'D: free 60.00 GiB' in value.space_guard_status.text()
    assert value.loading_animation.ready and isinstance(value.loading_animation._movie, QMovie)
    assert value.loading_animation._movie.frameCount() > 1
    assert value.resource_comparison.mapTo(value, QPoint()).y() > value.progress.mapTo(value, value.progress.rect().bottomLeft()).y()
    assert_hit(value, value.fullscreen.button)
    assert_hit(value, value.cancel_button)
    assert not value.cancel.is_set()


@pytest.mark.parametrize('pixels', [13, 20, 26])
@pytest.mark.parametrize('size', [(480, 450), (650, 550), (850, 650)])
def test_small_and_enlarged_font_views_scroll_instead_of_forcing_windows(progress, app, pixels, size):
    value = progress
    value.owner.setStyleSheet(build_qss() + f'\nQWidget {{font-size:{pixels}px;}}')
    value._update_space_guard(drive_check())
    value.resize(*size)
    wait_until(app, lambda: value.size().toTuple() == size)
    assert value.body_scroll.horizontalScrollBar().maximum() == 0
    assert mapped(value.body_scroll, value).bottom() < mapped(value._completed_footer, value).top()
    assert_hit(value, value.fullscreen.button)
    assert_hit(value, value.details_toggle)
    assert_hit(value, value.cancel_button)
    for identity in CORE:
        card = value.worker_cards[identity]
        assert value.body_scroll.widget().rect().contains(mapped(card, value.body_scroll.widget()))
        for button in card.navigation_buttons.values():
            reach(value, button, app)
    for reading in (value.loading_animation, value.live_system_status.computer,
            value.live_system_status.network, value.space_guard_status):
        reach(value, reading, app)
    assert value.body_scroll.horizontalScrollBar().maximum() == 0
    assert not value.cancel.is_set()


def test_only_processes_and_log_toggle_and_completion_scope_keeps_one_click(progress, app):
    value = progress
    value.update_progress({'stage': 'Fixture read-only inspection', 'percentage': 37,
        'description': 'Exact fixture operation', 'processes': ['Fixture.exe'], 'log': 'Read-only fixture log'})
    value.details_toggle.click()
    wait_until(app, lambda: value.log.isVisible() and value.processes.isVisible())
    assert value.content_scroll.widget().layout().count() == 4
    assert not value.content_scroll.isAncestorOf(value.analysis_panel)
    value.details_toggle.click()
    wait_until(app, lambda: value.loading_animation.isVisibleTo(value) and not value.log.isVisible())
    value.finish({'status': 'completed', 'stages': [], 'final_verification': {
        'status': 'needs_review', 'counts': {'unverified': 1}, 'summary': 'Fixture outcome needs review.'}})
    actions = [{'title': f'Exact approved fixture action {index}'} for index in range(7)]
    value.set_auto_continue_plan({'actions': actions, 'manual_items': []}, ready=True)
    wait_until(app, lambda: value.body_scroll.viewport().rect().contains(mapped(value.completion_review, value.body_scroll.viewport())))
    for pane in (value.verification_details, value.auto_continue_details):
        assert pane.isReadOnly() and pane.viewport().height() >= 45
        assert_hit(value, pane.viewport())
    requested = []
    value.autoContinueRequested.connect(lambda: requested.append(True))
    QTest.mouseClick(value.auto_continue_button, Qt.LeftButton)
    assert requested == [True]
    assert not value.cancel.is_set()
    assert value.loading_animation.isVisible() and value.loading_animation._movie.state() == QMovie.NotRunning
    value.begin_auto_continue(threading.Event())
    wait_until(app, lambda: value.body_scroll.verticalScrollBar().value() == 0)
    assert not value.completion_review.isVisible() and value._completed_footer.isVisible()
    assert value.loading_animation.isVisible()
    assert_hit(value, value.cancel_button)
    QTest.mouseClick(value.cancel_button, Qt.LeftButton)
    assert value.cancel.is_set() and value.isVisible()


def test_unknown_and_disabled_drive_checks_never_imply_passing_metrics(progress):
    progress.live_system_status.update_metrics({})
    assert 'CPU —' in progress.live_system_status.computer.text()
    assert 'RAM —' in progress.live_system_status.computer.text()
    assert 'Connection unverified' in progress.live_system_status.network.text()
    assert 'unknown' in progress.space_guard_status.text()
    progress._update_space_guard({'enabled': False})
    assert progress.space_guard_status.isVisibleTo(progress)
    assert 'disabled' in progress.space_guard_status.text()
    assert 'unverified' in progress.space_guard_status.text()
    progress._update_space_guard({'enabled': True, 'complete': False, 'target_bytes': 2 * 2 ** 30})
    assert 'no verified volume measurement' in progress.space_guard_status.text()
    assert 'needs review' in progress.space_guard_status.text()


def test_all_reported_drive_rows_wrap_and_can_be_scrolled_into_view(progress, app):
    payload = drive_check()
    payload['volumes'].extend({'name': name, 'free_bytes': 3 * 2 ** 30,
        'target_bytes': 2 * 2 ** 30, 'shortfall_bytes': 0, 'below_target': False}
        for name in ('F:', 'G:'))
    progress.resize(480, 450)
    progress._update_space_guard(payload)
    label = progress.space_guard_status
    wait_until(app, lambda: label.height() >= label.heightForWidth(label.width()))
    assert label.wordWrap() and label.hasHeightForWidth()
    assert all(name + ' free' in label.text() for name in ('C:', 'D:', 'F:', 'G:'))
    assert label.text().count(' · target ') == 4 and label.text().count(' · shortfall ') == 4
    assert progress.body_scroll.verticalScrollBar().maximum() > 0
    reach(progress, label, app)
    assert progress.body_scroll.horizontalScrollBar().maximum() == 0
    assert not progress.details_toggle.isChecked()
