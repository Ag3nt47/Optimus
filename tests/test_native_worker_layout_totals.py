"""Worker layout and measured totals use in-memory snapshots, never host cleanup."""
import os
from pathlib import Path
import threading

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

import pytest
from PySide6.QtCore import QCoreApplication, QEvent
from PySide6.QtGui import QFontDatabase
from PySide6.QtWidgets import QApplication, QWidget

from optimus.ui import OptimizationProgressWindow, WorkflowProgressBar, WorkflowWorkerCard
from optimus.widgets.theme import build_qss


@pytest.fixture(scope='module')
def app():
    value = QApplication.instance() or QApplication([])
    loaded = []
    if os.name == 'nt' and not QFontDatabase.families():
        for name in ('segoeui.ttf', 'seguisb.ttf'):
            path = Path(os.environ.get('SystemRoot', r'C:\Windows')) / 'Fonts' / name
            if path.is_file():
                identity = QFontDatabase.addApplicationFont(str(path))
                if identity >= 0:
                    loaded.append(identity)
    yield value
    for identity in loaded:
        QFontDatabase.removeApplicationFont(identity)


def fixture_state():
    return {'stage': 'Clean temporary files and verify recorded outcomes', 'percentage': 44,
        'description': 'Checking approved temporary files, retaining locked files and recording each successful removal.',
        'resource_totals': {'removed_known': True, 'removed_bytes': 2147483648, 'removed_files': 12799},
        'item_progress': {'key': 'cleanup:fixture', 'label': 'Checking selected files', 'phase': 'cleanup', 'percent': 62},
        'jobs': [
            {'id': 'software_updates', 'status': 'installing', 'current': 1, 'total': 11,
             'description': 'Downloading approved Example.Application.With.A.Deliberately.Long.Package.Name 27.18.1234.5678',
             'scan_label': '11 recognized applications; additional software sources have not reported complete results',
             'resource_totals': {'installed_known': True, 'installed_items': 1,
                 'downloaded_known': True, 'downloaded_bytes': 345610649, 'downloaded_estimated': True,
                 'downloaded_partial': True, 'installed_reported': True},
             'item_progress': {'key': 'app:fixture', 'label': 'Example application', 'phase': 'download', 'percent': 81}},
            {'id': 'driver_updates', 'status': 'waiting_for_installer', 'current': 0, 'total': 2,
             'description': 'Waiting for the approved application installer to return before installing the next network driver.',
             'resource_totals': {'installed_known': True, 'installed_items': 0, 'downloaded_known': False},
             'item_progress': {'key': 'driver:fixture', 'label': 'Example network driver', 'phase': 'waiting', 'percent': None}}]}


def make_window(app, width, font_pixels):
    owner = QWidget()
    owner.setStyleSheet(build_qss() + f'\nQFrame#WorkflowWorkerCard QLabel, QFrame#WorkflowWorkerCard QProgressBar {{font-size:{font_pixels}px;}}')
    progress = OptimizationProgressWindow(owner, threading.Event())
    progress._optimus_no_reveal = True
    progress.resize(width, 850)
    progress.show()
    progress.update_progress(fixture_state())
    for _ in range(3):
        app.processEvents()
    # The global window coordinator can fit a first presentation to the small
    # offscreen display. Then exercise an explicit user resize to this viewport.
    progress.resize(width, 850)
    for _ in range(3):
        app.processEvents()
    return owner, progress


def dispose(owner, progress, app):
    progress.completed = True
    progress.close()
    owner.close()
    progress.deleteLater()
    owner.deleteLater()
    QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)
    app.processEvents()


@pytest.mark.parametrize('width', (850, 1280))
@pytest.mark.parametrize('font_pixels', (13, 20, 26))
def test_card_rows_fit_and_scale_without_clipped_counters(app, width, font_pixels):
    owner, progress = make_window(app, width, font_pixels)
    try:
        cards = list(progress.worker_cards.values())
        assert progress.width() == width
        assert progress.content_scroll.horizontalScrollBar().maximum() == 0
        assert cards[0].completion.fontMetrics().height() >= font_pixels
        if width == 850 and font_pixels == 26:
            assert progress.worker_grid._columns < 3
        for card in cards:
            assert card.width() >= 200
            assert card.heading.geometry().right() < card.activity_ring.geometry().left()
            fields = [card.status, card.details, card.progress, card.totals, card.elapsed, card.activity_state]
            for earlier, later in zip(fields, fields[1:]):
                assert earlier.geometry().bottom() < later.geometry().top()
            for label in (card.heading, card.completion, card.status, card.details, card.totals, card.elapsed, card.activity_state):
                assert all(label.fontMetrics().horizontalAdvance(line) <= label.contentsRect().width()
                    for line in label.visible_text.splitlines())
                assert label.accessibleDescription()
            assert card.progress.height() >= card.progress.fontMetrics().height() + 6
            assert card.progress.fontMetrics().horizontalAdvance(card.progress.visible_text) <= card.progress.width() - 10
            assert card.progress.toolTip() == card.progress.text()
            assert card.totals.geometry().bottom() < card.navigation_buttons['next'].geometry().top()
        for column in range(min(progress.worker_grid._columns, len(cards)) - 1):
            left, right = cards[column:column + 2]
            assert abs(left.width() - right.width()) <= 1
            assert left.height() == right.height()
            assert left.totals.geometry().top() == right.totals.geometry().top()
        assert progress.worker_cards['software_updates'].status.text() == 'Downloading'
        assert progress.worker_cards['driver_updates'].activity_ring.value_percent is None
    finally:
        dispose(owner, progress, app)


def test_totals_are_confirmed_separate_and_survive_unrelated_snapshots(app):
    owner, progress = make_window(app, 850, 13)
    try:
        maintenance = progress.worker_cards['maintenance']
        software = progress.worker_cards['software_updates']
        driver = progress.worker_cards['driver_updates']
        assert 'Removed: 2.0 GiB · 12,799 files' in maintenance.totals.text()
        assert 'not a measurement of free disk space gained' in maintenance.totals.accessibleDescription()
        assert 'Completed installs: 1 app' in software.totals.text()
        assert 'Downloaded: ~329.6 MiB observed' in software.totals.text()
        assert 'not installed disk size' in software.totals.accessibleDescription()
        assert 'without an independent installed-version readback' in software.totals.accessibleDescription()
        assert 'Completed installs: 0 updates' in driver.totals.text()
        assert 'Downloaded: size not reported' in driver.totals.text()
        progress.update_progress({'stage': 'Verify outcomes', 'percentage': 90,
            'jobs': [{'id': 'software_updates', 'status': 'completed', 'description': 'Fixture outcome recorded.'}]})
        assert '2.0 GiB' in maintenance.totals.text() and '1 app' in software.totals.text()
        progress.configure_workers({'software_updates_enabled': True})
        assert '2.0 GiB' not in maintenance.totals.text()
        assert 'Installs: not reported' in software.totals.text()
    finally:
        dispose(owner, progress, app)


@pytest.mark.parametrize('values', [
    {'installed_known': True, 'installed_items': True, 'downloaded_known': True, 'downloaded_bytes': -1},
    {'installed_known': 'yes', 'installed_items': 99, 'downloaded_known': 1, 'downloaded_bytes': 999},
    {'installed_known': True, 'installed_items': 1.0, 'downloaded_known': True, 'downloaded_bytes': 2 ** 100},
])
def test_invalid_or_unconfirmed_totals_stay_unknown(app, values):
    card = WorkflowWorkerCard('App updates')
    try:
        card.update_state({'status': 'running', 'resource_totals': values})
        assert 'Installs: not reported' in card.totals.text()
        assert 'Downloaded: size not reported' in card.totals.text()
    finally:
        card.close()
        card.deleteLater()
        app.processEvents()


def test_zero_removals_are_distinct_from_unknown_and_final_receipts_replace_totals(app):
    owner, progress = make_window(app, 850, 13)
    try:
        progress.finish({'status': 'completed', 'stages': [],
            'resource_totals': {'removed_known': True, 'removed_bytes': 0, 'removed_files': 0},
            'worker_resource_totals': {'software_updates': {'installed_known': True, 'installed_items': 2,
                'downloaded_known': True, 'downloaded_bytes': 104857600}}})
        assert 'Removed: 0 B · 0 files' in progress.worker_cards['maintenance'].totals.text()
        assert 'Completed installs: 2 apps' in progress.worker_cards['software_updates'].totals.text()
        assert 'Downloaded: 100.0 MiB' in progress.worker_cards['software_updates'].totals.text()
    finally:
        dispose(owner, progress, app)


def test_progress_caption_retains_full_scan_result_without_overflow(app):
    bar = WorkflowProgressBar()
    try:
        bar.resize(210, 30)
        bar.setRange(0, 1)
        bar.setValue(0)
        full = 'Availability unknown: source one returned partial results and source two is temporarily unreachable.'
        bar.setFormat(full)
        bar.show()
        app.processEvents()
        assert bar.format() == bar.text() == full
        assert bar.visible_text.endswith('…')
        assert bar.accessibleDescription() == full and bar.toolTip() == full
        assert bar.fontMetrics().horizontalAdvance(bar.visible_text) <= bar.width() - 12
    finally:
        bar.close()
        bar.deleteLater()
        app.processEvents()


def test_navigation_note_and_live_font_change_keep_shared_rows_aligned(app):
    owner, progress = make_window(app, 1280, 13)
    try:
        maintenance, software, driver = progress.worker_cards.values()
        maintenance.set_navigation_message('Previous result: a deliberately long fixture explanation retained for review; no completed changes are replayed.')
        for _ in range(3):
            app.processEvents()
        for field in ('status', 'details', 'progress', 'totals', 'elapsed', 'activity_state'):
            assert len({getattr(card, field).geometry().top() for card in (maintenance, software, driver)}) == 1
        owner.setStyleSheet(build_qss() + '\nQFrame#WorkflowWorkerCard QLabel, QFrame#WorkflowWorkerCard QProgressBar {font-size:26px;}')
        progress.resize(850, 850)
        for _ in range(3):
            app.processEvents()
        assert progress.worker_grid._columns == 2
        assert maintenance.totals.geometry().top() == software.totals.geometry().top()
        assert maintenance.progress.height() >= maintenance.progress.fontMetrics().height() + 6
        assert progress.content_scroll.horizontalScrollBar().maximum() == 0
    finally:
        dispose(owner, progress, app)
