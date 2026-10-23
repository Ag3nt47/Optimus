import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

from PySide6.QtWidgets import QApplication
from optimus.ui import WorkflowWorkerCard
from optimus.widgets.worker_grid import WorkerGrid


def test_additional_actual_workers_reflow_and_keep_explicit_unknown_counts():
    app = QApplication.instance() or QApplication([])
    grid = WorkerGrid(WorkflowWorkerCard)
    try:
        for identity in ('maintenance', 'software_updates', 'driver_updates'):
            grid.ensure(identity, identity)
        grid.resize(1000, 400)
        grid.show()
        app.processEvents()
        grid.update_jobs([{'id': 'driver_backup', 'title': 'Driver backup', 'status': 'running',
                           'description': 'Verifying exported files', 'current': None, 'total': None}])
        assert len(grid.cards) == 4 and grid._columns == 3
        assert grid.grid.getItemPosition(3)[:2] == (1, 0)
        assert 'No item count' in grid.cards['driver_backup'].progress.format()
        grid.resize(440, 800)
        app.processEvents()
        assert grid._columns == 1
        assert grid.grid.getItemPosition(3)[:2] == (3, 0)
        assert grid.ensure('../bad', 'Invalid') is None
        assert grid.ensure('driver_backup', 'Same worker') is grid.cards['driver_backup']
    finally:
        grid.close()
