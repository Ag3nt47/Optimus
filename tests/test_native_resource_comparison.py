"""Comparison is reporting only: replayed ticks, unknown and real zero differ."""
import os
import threading

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

import pytest
from PySide6.QtWidgets import QApplication, QWidget

from optimus.ui import OptimizationProgressWindow
from optimus.widgets.resource_comparison import ResourceComparison, comparison_snapshot
from optimus.worker_totals import WorkerResourceTotals


@pytest.fixture(scope='module')
def app():
    return QApplication.instance() or QApplication([])


def totals(removed=300, downloaded=100, *, estimated=False, partial=False):
    maintenance = WorkerResourceTotals()
    maintenance.removal_receipt('cleanup:fixture', {'deleted_bytes': removed, 'deleted_count': 5})
    updates = WorkerResourceTotals('software_updates')
    updates.observe({'item_progress': {'key': 'software:update:Fixture.One', 'phase': 'download',
        'downloaded_bytes': downloaded, 'download_total_bytes': downloaded if not partial else None,
        'bytes_source': 'winget_native', 'bytes_estimated': estimated}})
    return {'maintenance': maintenance.snapshot(), 'software_updates': updates.snapshot()}


def test_share_is_reported_byte_activity_not_task_completion_or_install_footprint():
    workers = totals()
    value = comparison_snapshot(workers)
    assert value['removed_percent'] == 75 and value['downloaded_percent'] == 25
    assert value['removed_files'] == 5
    assert comparison_snapshot(workers) == value
    workers['maintenance'] = totals(removed=600)['maintenance']
    assert comparison_snapshot(workers)['removed_bytes'] == 600


@pytest.mark.parametrize('case', ({}, {'software_updates': WorkerResourceTotals('software_updates').snapshot()},
    {'maintenance': totals()['maintenance']}, {'invalid': {'removed_bytes': True}}))
def test_unknown_side_never_becomes_a_measured_zero_or_one_hundred_percent(case):
    value = comparison_snapshot(case)
    assert value['removed_percent'] is value['downloaded_percent'] is None


def test_known_zero_handles_both_empty_and_one_sided_activity():
    assert comparison_snapshot(totals(0, 0))['removed_percent'] is None
    assert comparison_snapshot(totals(0, 100))['removed_percent'] == 0
    assert comparison_snapshot(totals(100, 0))['downloaded_percent'] == 0


def test_estimated_and_incomplete_downloads_are_labelled(app):
    widget = ResourceComparison()
    try:
        widget.update_totals(totals(estimated=True, partial=True))
        assert '≈' in widget.downloaded.text()
        assert 'totals incomplete' in widget.note.text()
        assert 'not a measured free-space gain' in widget.note.text()
    finally:
        widget.deleteLater()


@pytest.mark.parametrize('width', (480, 850, 1280))
def test_comparison_fits_below_global_progress_and_refreshes_without_accumulation(app, width):
    owner = QWidget()
    dialog = OptimizationProgressWindow(owner, threading.Event())
    try:
        dialog.resize(width, 900)
        dialog.show()
        workers = totals()
        event = {'stage': 'Clean temporary files', 'percentage': 44,
            'resource_totals': workers['maintenance'],
            'jobs': [{'id': 'software_updates', 'status': 'running',
                      'resource_totals': workers['software_updates']}]}
        for _ in range(3):
            dialog.update_progress(event)
            app.processEvents()
        comparison = dialog.resource_comparison
        assert comparison.snapshot['removed_bytes'] == 300
        assert comparison.snapshot['downloaded_bytes'] == 100
        assert comparison.bar.value() == 7500
        assert '75.0%' in comparison.removed.text() and '25.0%' in comparison.downloaded.text()
        assert dialog.content_scroll.horizontalScrollBar().maximum() == 0
        assert dialog.progress.geometry().bottom() < comparison.geometry().top()
        assert comparison.geometry().bottom() < dialog.stage.geometry().top()
        assert comparison.removed.geometry().right() < comparison.downloaded.geometry().left()
        dialog.configure_workers({})
        assert comparison.snapshot['removed_known'] is False
        assert comparison.snapshot['downloaded_known'] is False
    finally:
        dialog.completed = True
        dialog.close()
        owner.close()
        dialog.deleteLater()
        owner.deleteLater()
        app.processEvents()
