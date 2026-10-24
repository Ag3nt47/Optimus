import os
from pathlib import Path
from types import SimpleNamespace
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

import pytest
from PySide6.QtGui import QFontDatabase
from PySide6.QtWidgets import QApplication, QWidget
from optimus.ui import WorkflowWorkerCard
from optimus.widgets.worker_grid import WorkerBoard, WorkerGrid


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


def test_additional_actual_workers_reflow_and_keep_explicit_unknown_counts(app):
    grid = WorkerGrid(WorkflowWorkerCard)
    try:
        for identity in ('maintenance', 'software_updates', 'driver_updates'):
            grid.ensure(identity, identity)
        grid.show()
        app.processEvents()
        grid.update_jobs([{'id': 'driver_backup', 'title': 'Driver backup', 'status': 'running',
                           'description': 'Verifying exported files', 'current': None, 'total': None}])
        app.processEvents()
        # Navigation labels set the readable card width using the active font.
        # A fixed 1000px window can correctly fit only two Windows cards.
        readable = max(card.minimum_readable_width() for card in grid.cards.values())
        spacing = grid.grid.horizontalSpacing()
        grid.resize(3 * readable + 2 * spacing + 60, 800)
        for _ in range(3):
            app.processEvents()
        assert len(grid.cards) == 4 and grid._columns == 3
        assert grid.grid.getItemPosition(3)[:2] == (1, 0)
        assert all(card.width() >= readable for card in grid.cards.values())
        assert 'No item count' in grid.cards['driver_backup'].progress.format()
        grid.resize(2 * readable + spacing + 20, 800)
        for _ in range(3):
            app.processEvents()
        assert grid._columns == 2
        assert grid.grid.getItemPosition(3)[:2] == (1, 1)
        assert all(card.width() >= readable for card in grid.cards.values())
        grid.resize(readable + 20, 800)
        for _ in range(3):
            app.processEvents()
        assert grid._columns == 1
        assert grid.grid.getItemPosition(3)[:2] == (3, 0)
        assert grid.ensure('../bad', 'Invalid') is None
        assert grid.ensure('driver_backup', 'Same worker') is grid.cards['driver_backup']
    finally:
        grid.close()


def test_smart_worker_uses_only_last_check_action_receipts_without_starting_cleanup(app):
    owner = QWidget()
    owner.backend = SimpleNamespace(worker_status=lambda: [], smart_maintenance_status=lambda: {
        'running': True, 'status': 'checked', 'preview': False,
        'actions': [{'status': 'completed', 'receipt': 'fixture-cleanup.json',
                     'result': {'deleted_bytes': 1048576, 'deleted_count': 3}}]})
    board = WorkerBoard(owner)
    try:
        board.refresh()
        card = board.grid.cards['smart_cleaning']
        assert 'Removed: 1.0 MiB · 3 files' in card.totals.text()
        assert 'Latest smart check' in card.totals.text()
        assert 'separate from this one-click run' in card.totals.accessibleDescription()
        assert 'Completed installs' not in card.totals.text()
        assert 'cleanup' not in vars(owner.backend)
        board.refresh()
        assert 'Removed: 1.0 MiB · 3 files' in card.totals.text()
    finally:
        board.shutdown()
        owner.close()
        board.deleteLater()
        owner.deleteLater()
        app.processEvents()
