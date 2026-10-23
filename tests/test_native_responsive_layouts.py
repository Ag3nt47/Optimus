"""Window controls stay reachable when a desktop or editor is narrowed."""
from PySide6.QtCore import QPoint, QRect, Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QPushButton, QVBoxLayout, QWidget
from types import SimpleNamespace

from optimus.parallel_updates import ProgressMux
from optimus.widgets.flow_layout import FlowLayout
from optimus.ui import OptimusWindow
from optimus.workflow import DEFAULTS
from test_native_worker_cards import app, fonts, progress


def settle_scroll_layout(area, child, app):
    """Wait for the card/grid/body layout requests before asking Qt to scroll.

    A narrow resize reflows several parent layouts. Scrolling before those
    posted requests settle can target the old child position on native Linux.
    Keep the real viewport and visibility assertions after this boundary.
    """
    previous, stable = None, 0
    for _ in range(40):
        QTest.qWait(5)
        app.processEvents()
        point = child.mapTo(area.widget(), QPoint())
        state = (area.widget().size().toTuple(), area.viewport().size().toTuple(),
                 point.toTuple(), child.size().toTuple(), area.verticalScrollBar().maximum())
        stable = stable + 1 if state == previous else 0
        if stable >= 3:
            return
        previous = state
    raise AssertionError('The scroll content did not settle after a compact resize.')


def test_sandbox_toolbar_controls_wrap_without_overlap_or_lost_actions(app):
    window = QWidget()
    layout = QVBoxLayout(window)
    flow = FlowLayout()
    labels = ['Save', 'Revert', 'Original version', 'History', 'Restart Optimus', 'Export project']
    buttons = [QPushButton(label) for label in labels]
    for button in buttons:
        flow.addWidget(button)
    layout.addLayout(flow)
    window.resize(450, 220)
    window.show()
    app.processEvents()
    try:
        compact = [button.geometry() for button in buttons]
        assert len({rect.y() for rect in compact}) > 1
        assert all(button.isVisible() and window.rect().contains(button.geometry()) for button in buttons)
        assert all(not a.intersects(b) for i, a in enumerate(compact) for b in compact[i + 1:])
        assert [button.text() for button in buttons] == labels
        window.resize(1280, 220)
        app.processEvents()
        assert len({button.geometry().y() for button in buttons}) == 1
    finally:
        window.close()
        window.deleteLater()
        app.processEvents()


def test_completed_worker_displays_its_full_scan_duration_and_freezes(progress, app, monkeypatch):
    now = [100.0]
    monkeypatch.setattr('optimus.parallel_updates.time.monotonic', lambda: now[0])
    mux = ProgressMux(progress.update_progress)
    mux({'stage': 'Read-only fixture maintenance', 'percentage': 20})
    mux.background('scanning', {'description': 'Checking approved offers'})
    now[0] = 142.0
    mux.background('installing', {'id': 'Fixture.App', 'current': 0, 'total': 6})
    now[0] = 143.0
    mux.background('needs_review', {'description': 'Update deferred: system-drive space below 2 GiB.',
                                  'current': 1, 'total': 6})
    app.processEvents()
    card = progress.worker_cards['software_updates']
    assert card.status.text() == 'Needs review' and card.elapsed.text() == 'Elapsed: 43s'
    assert card.progress.value() == 1 and card.progress.maximum() == 6
    now[0] = 250.0
    mux.heartbeat()
    app.processEvents()
    assert card.elapsed.text() == 'Elapsed: 43s'


def test_small_progress_window_keeps_cancel_visible_and_body_scrollable(progress, app):
    progress.resize(640, 400)
    progress.update_progress({'stage': 'Waiting for background updates', 'percentage': 74,
        'description': 'A long fixture description explains the current operation and its remaining checks. ' * 4,
        'jobs': [{'id': 'software_updates', 'status': 'needs_review', 'current': 1, 'total': 6,
                  'description': 'Update deferred: system-drive space below 2 GiB.', 'elapsed_seconds': 43}]})
    app.processEvents()
    settle_scroll_layout(progress.content_scroll, progress.log, app)
    assert progress.width() <= 640 and progress.height() <= 400
    assert progress.rect().contains(progress.cancel_button.geometry())
    area = progress.content_scroll
    assert area.verticalScrollBar().maximum() > 0
    assert area.horizontalScrollBar().maximum() == 0
    assert len({card.width() for card in progress.worker_cards.values()}) <= 2
    area.ensureWidgetVisible(progress.log)
    app.processEvents()
    visible = QRect(progress.log.mapTo(area.viewport(), QPoint()), progress.log.size())
    assert visible.intersects(area.viewport().rect())


def test_short_landing_window_retains_all_four_controls_with_scroll_access(app, tmp_path):
    backend = SimpleNamespace(data_dir=tmp_path, preferences=lambda: dict(DEFAULTS, reduce_motion=True))
    window = OptimusWindow(backend, smoke_test=True)
    window.setMinimumSize(0, 0)
    window.resize(640, 360)
    window.show()
    app.processEvents()
    try:
        assert window.width() <= 640 and window.height() <= 360
        area = window.landing_scroll
        assert area.verticalScrollBar().maximum() > 0
        assert area.horizontalScrollBar().maximum() == 0
        assert len(window.main_buttons) == 4
        assert len({button.width() for button in window.main_buttons}) == 1
        for button in window.main_buttons:
            area.ensureWidgetVisible(button)
            app.processEvents()
            shown = QRect(button.mapTo(area.viewport(), QPoint()), button.size())
            assert area.viewport().rect().contains(shown)
    finally:
        window.shutdown()
        window.close()
        window.deleteLater()
        app.processEvents()


def test_long_settings_headings_are_fully_readable_in_a_narrow_window(app, tmp_path):
    from test_native_critical_review import bot
    from optimus.ui import DetailedSettingsWindow
    backend = bot.FixtureBackend(tmp_path / 'fixture-state', 'win32')
    window = DetailedSettingsWindow(backend, smoke_test=True)
    window.resize(640, 480)
    window.show()
    app.processEvents()
    try:
        for page in ('Browser privacy', 'Duplicate finder', 'Registry review', 'Restore/history'):
            window.select_page(page, load=False)
            app.processEvents()
            title = window.page_title
            fitted = title.fontMetrics().boundingRect(QRect(0, 0, title.contentsRect().width(), 1000),
                                                      Qt.TextWordWrap, title.text())
            assert fitted.width() <= title.contentsRect().width(), (page, fitted, title.contentsRect())
            assert fitted.height() <= title.contentsRect().height(), (page, fitted, title.contentsRect())
    finally:
        window.shutdown()
        window.close()
        window.deleteLater()
        app.processEvents()
