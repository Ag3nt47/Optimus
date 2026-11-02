"""Snapshot/history UI uses only prepared data and a mocked local-file reader."""
import os
import threading
import time
from types import SimpleNamespace
from unittest.mock import Mock

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

import pytest
from PySide6.QtCore import QCoreApplication, QEvent, QPoint, QRect, QThreadPool, Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QDialogButtonBox, QWidget
from shiboken6 import isValid

from optimus.widgets import hardware_health_dialog as dialogs
from optimus.widgets import window_geometry as geometry


def report():
    return {'schema': 1, 'read_only': True, 'status': 'completed', 'captured_at': 1793633738.0,
        'batteries': [{'name': 'Fixture battery', 'condition': 'Normal', 'capacity_retained_percent': 82,
            'full_charge_capacity': 41000, 'design_capacity': 50000, 'capacity_unit': 'mWh',
            'cycle_count': 123, 'temp_c': 31, 'source': 'Fixture provider'}],
        'drives': [{'name': 'Fixture SSD', 'device_id': 'fixture-disk', 'health_status': 'Healthy',
            'wear_percent_used': 12, 'temperature_c': 40, 'source': 'Fixture provider'}], 'errors': []}


def history():
    return {'schema': 1, 'status': 'available', 'scan_count': 0, 'series': [], 'note': 'Fixture local history.'}


@pytest.fixture(scope='module')
def app():
    value = QApplication.instance() or QApplication([])
    value.setQuitOnLastWindowClosed(False)
    return value


@pytest.fixture(autouse=True)
def no_native_operations(monkeypatch):
    import subprocess
    import optimus.runtime_priority
    forbidden = Mock(side_effect=AssertionError('The snapshot dialog must not call native providers.'))
    monkeypatch.setattr(subprocess, 'Popen', forbidden)
    monkeypatch.setattr(subprocess, 'run', forbidden)
    monkeypatch.setattr(optimus.runtime_priority, 'lower_worker_priority', lambda: lambda: None)
    yield forbidden
    forbidden.assert_not_called()


def backend(operation=None):
    return SimpleNamespace(hardware_health_history=operation or Mock(return_value=history()),
        hardware_health_scan=Mock(side_effect=AssertionError('No fresh native hardware scan.')),
        hardware_health_details=Mock(side_effect=AssertionError('Refresh requires an explicit fixture click.')))


def wait_until(app, predicate, seconds=3):
    deadline = time.monotonic() + seconds
    while not predicate() and time.monotonic() < deadline:
        app.processEvents()
        QTest.qWait(5)
    assert predicate()


def dispose(app, dialog):
    if isValid(dialog):
        dialog.close()
        dialog.deleteLater()
    QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)
    app.processEvents()


def test_prepared_history_is_passive_and_explicit_refresh_is_available(app):
    service = backend()
    dialog = dialogs.SnapshotHealthDialog(service, report(), history=history())
    try:
        dialog.show()
        app.processEvents()
        assert dialog.panel.report['batteries'][0]['capacity_retained_percent'] == 82
        assert dialog.panel.refresh_button.isEnabled()
        service.hardware_health_scan.assert_not_called()
        service.hardware_health_history.assert_not_called()
        service.hardware_health_details.assert_not_called()
        assert 'Saved scan snapshot' in dialog.snapshot_note.text()
        assert 'Local history loaded' in dialog.history_status.text()
        assert 'saved snapshot' in dialog.panel.status.text()
        assert dialog.panel.scroll.horizontalScrollBarPolicy() == Qt.ScrollBarAlwaysOff
        close = dialog.buttons.button(QDialogButtonBox.Close)
        assert close.isEnabled()
        # The preferred 850x650 size is capped by the shared fitter on small
        # work areas, including Linux's offscreen CI display. Test reachability
        # rather than requiring the desktop preference to exceed the screen.
        for bounds in (geometry.available_screen_geometry(dialog),
                       QRect(0, 0, 800, 600), QRect(0, 0, 640, 480)):
            geometry.fit_window_to_screen(dialog, available=bounds)
            app.processEvents()
            assert bounds.contains(dialog.frameGeometry())
            assert dialog.rect().contains(close.rect().translated(close.mapTo(dialog, QPoint())))
            assert dialog.childAt(close.mapTo(dialog, close.rect().center())) is close
            viewport = dialog.panel.scroll.viewport()
            assert viewport.isVisibleTo(dialog) and viewport.width() > 0 and viewport.height() > 0
            name = dialog.panel.cards[-1].name
            dialog.panel.scroll.ensureWidgetVisible(name, 0, 0)
            app.processEvents()
            assert viewport.rect().contains(name.rect().translated(name.mapTo(viewport, QPoint())))
    finally:
        dispose(app, dialog)


def test_constructor_queues_only_the_local_history_read_on_a_worker(app):
    called_on = []
    main_thread = threading.get_ident()
    def read_local():
        called_on.append(threading.get_ident())
        return history()
    operation = Mock(side_effect=read_local)
    service = backend(operation)
    dialog = dialogs.SnapshotHealthDialog(service, report())
    try:
        operation.assert_not_called()  # No synchronous query in construction.
        dialog.show()
        wait_until(app, lambda: operation.call_count == 1 and dialog._worker is None)
        assert called_on and called_on[0] != main_thread
        assert 'Local history loaded' in dialog.history_status.text()
        assert dialog.panel.refresh_button.isEnabled()
        assert not dialog.findChildren(QThreadPool)
        service.hardware_health_scan.assert_not_called()
    finally:
        dispose(app, dialog)


@pytest.mark.parametrize('payload', [None, [], {}, {'status': 'unavailable', 'series': []}])
def test_invalid_or_unavailable_prepared_history_shows_visible_failure(app, payload):
    service = backend()
    dialog = dialogs.SnapshotHealthDialog(service, report(), history=payload)
    try:
        assert 'History unavailable' in dialog.history_status.text()
        assert len(dialog.panel.cards) == 2
        assert dialog.panel.refresh_button.isEnabled()
        service.hardware_health_history.assert_not_called()
    finally:
        dispose(app, dialog)


def test_failed_local_history_reader_keeps_snapshot_and_does_not_show_private_exception(app):
    service = backend(Mock(side_effect=OSError('PRIVATE-FILE-CONTENT')))
    dialog = dialogs.SnapshotHealthDialog(service, report())
    try:
        dialog.show()
        wait_until(app, lambda: dialog._worker is None and service.hardware_health_history.called)
        assert 'History unavailable' in dialog.history_status.text()
        assert 'PRIVATE-FILE-CONTENT' not in dialog.history_status.text()
        assert len(dialog.panel.cards) == 2 and dialog.panel.refresh_button.isEnabled()
    finally:
        dispose(app, dialog)


def test_reject_before_queued_read_prevents_work_and_remains_nonblocking(app):
    service = backend()
    dialog = dialogs.SnapshotHealthDialog(service, report())
    try:
        dialog.reject()
        app.processEvents()
        assert dialog._closed is True
        service.hardware_health_history.assert_not_called()
    finally:
        dispose(app, dialog)


def test_deleting_closed_dialog_while_file_read_runs_keeps_global_worker_alive(app):
    entered, release = threading.Event(), threading.Event()
    def read_local():
        entered.set()
        release.wait(3)
        return history()
    service = backend(Mock(side_effect=read_local))
    owner = QWidget()
    dialog = dialogs.SnapshotHealthDialog(service, report(), owner)
    try:
        dialog.show()
        wait_until(app, entered.is_set)
        token = id(dialog._worker)
        assert token in dialogs._HISTORY_WORKERS
        started = time.monotonic()
        dialog.close()
        assert time.monotonic() - started < 1
        assert token in dialogs._HISTORY_WORKERS  # Closing never destroys the runnable.
        dialog.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)
        assert not isValid(dialog)
        release.set()
        wait_until(app, lambda: token not in dialogs._HISTORY_WORKERS)
        service.hardware_health_scan.assert_not_called()
    finally:
        release.set()
        if isValid(dialog):
            dispose(app, dialog)
        owner.deleteLater()
        app.processEvents()


def test_later_snapshot_update_keeps_refresh_available_without_querying(app):
    service = backend()
    dialog = dialogs.SnapshotHealthDialog(service, report(), history=history())
    try:
        new = report()
        new['status'] = 'partial'
        new['batteries'][0]['capacity_retained_percent'] = 75
        dialog.set_report(new)
        assert dialog.panel.report['batteries'][0]['capacity_retained_percent'] == 75
        assert 'partial' in dialog.panel.status.text() and 'snapshot' in dialog.panel.status.text()
        assert dialog.panel.refresh_button.isEnabled()
        service.hardware_health_history.assert_not_called()
        service.hardware_health_details.assert_not_called()
    finally:
        dispose(app, dialog)


def test_explicit_refresh_is_async_forced_and_updates_report_history_while_owner_busy(app):
    service = backend()
    fresh = report()
    fresh['captured_at'] += 60
    fresh['batteries'][0]['capacity_retained_percent'] = 81
    called_on = []
    def details(**kwargs):
        called_on.append(threading.get_ident())
        return {'report': fresh, 'history': history()}
    service.hardware_health_details.side_effect = details
    owner = QWidget()
    owner.busy = True
    dialog = dialogs.SnapshotHealthDialog(service, report(), owner, history=history())
    changed = Mock()
    dialog.reportRefreshed.connect(changed)
    try:
        service.hardware_health_details.assert_not_called()
        dialog.panel.refresh_button.click()
        wait_until(app, lambda: changed.called and dialog._worker is None)
        assert len(called_on) == 1 and called_on[0] != threading.get_ident()
        kwargs = service.hardware_health_details.call_args.kwargs
        assert kwargs == {'refresh': True, 'force_refresh': True, 'cancel_event': dialog._refresh_cancel}
        assert not kwargs['cancel_event'].is_set()
        assert dialog.panel.report == fresh and changed.call_args.args[0] == fresh
        assert 'fresh read-only observation' in dialog.panel.status.text()
        assert 'Fresh hardware readings and local history loaded' in dialog.history_status.text()
        assert dialog.panel.refresh_button.isEnabled() and owner.busy is True
        service.hardware_health_scan.assert_not_called()
        service.hardware_health_history.assert_not_called()
    finally:
        dispose(app, dialog)
        owner.deleteLater()
        app.processEvents()


def test_refresh_before_initial_history_timer_runs_replaces_that_read(app):
    service = backend()
    service.hardware_health_details.side_effect = None
    service.hardware_health_details.return_value = {'report': report(), 'history': history()}
    dialog = dialogs.SnapshotHealthDialog(service, report())
    try:
        dialog.panel.refresh_button.click()
        wait_until(app, lambda: service.hardware_health_details.called and dialog._worker is None)
        service.hardware_health_details.assert_called_once()
        service.hardware_health_history.assert_not_called()
        assert dialog.panel.refresh_button.isEnabled()
    finally:
        dispose(app, dialog)


def test_refresh_queues_once_behind_inflight_local_history_without_blocking(app):
    entered, release = threading.Event(), threading.Event()
    def read_local():
        entered.set()
        release.wait(3)
        return history()
    service = backend(Mock(side_effect=read_local))
    service.hardware_health_details.side_effect = None
    service.hardware_health_details.return_value = {'report': report(), 'history': history()}
    dialog = dialogs.SnapshotHealthDialog(service, report())
    try:
        wait_until(app, entered.is_set)
        dialog.panel.refresh_button.click()
        dialog.panel.refresh_button.click()
        assert dialog._refresh_pending is True and not dialog.panel.refresh_button.isEnabled()
        service.hardware_health_details.assert_not_called()
        release.set()
        wait_until(app, lambda: service.hardware_health_details.called and dialog._worker is None)
        service.hardware_health_details.assert_called_once()
        assert dialog.panel.refresh_button.isEnabled()
    finally:
        release.set()
        dispose(app, dialog)


def test_close_cancels_refresh_without_destroying_worker_or_emitting_stale_result(app):
    entered, release = threading.Event(), threading.Event()
    def details(**kwargs):
        entered.set()
        release.wait(3)
        return {'report': report(), 'history': history()}
    service = backend()
    service.hardware_health_details.side_effect = details
    dialog = dialogs.SnapshotHealthDialog(service, report(), history=history())
    changed = Mock()
    dialog.reportRefreshed.connect(changed)
    try:
        dialog.panel.refresh_button.click()
        wait_until(app, entered.is_set)
        token = id(dialog._worker)
        assert token in dialogs._HISTORY_WORKERS
        dialog._refresh_hardware()  # A repeated request cannot queue another scan.
        assert not dialog._refresh_pending
        started = time.monotonic()
        dialog.close()
        assert time.monotonic() - started < 1
        assert service.hardware_health_details.call_args.kwargs['cancel_event'].is_set()
        assert token in dialogs._HISTORY_WORKERS
        dialog.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)
        assert not isValid(dialog)
        release.set()
        wait_until(app, lambda: token not in dialogs._HISTORY_WORKERS)
        service.hardware_health_details.assert_called_once()
        changed.assert_not_called()
    finally:
        release.set()
        if isValid(dialog):
            dispose(app, dialog)


def test_close_discards_pending_refresh_after_local_history(app):
    entered, release = threading.Event(), threading.Event()
    def read_local():
        entered.set()
        release.wait(3)
        return history()
    service = backend(Mock(side_effect=read_local))
    dialog = dialogs.SnapshotHealthDialog(service, report())
    try:
        wait_until(app, entered.is_set)
        token = id(dialog._worker)
        dialog.panel.refresh_button.click()
        assert dialog._refresh_pending
        dialog.close()
        release.set()
        wait_until(app, lambda: token not in dialogs._HISTORY_WORKERS)
        service.hardware_health_details.assert_not_called()
    finally:
        release.set()
        dispose(app, dialog)


@pytest.mark.parametrize('invalid', [None, {}, {'report': {}}, {'report': {'read_only': False}}])
def test_malformed_refresh_keeps_previous_readings_and_allows_retry(app, invalid):
    service = backend()
    service.hardware_health_details.side_effect = None
    service.hardware_health_details.return_value = invalid
    dialog = dialogs.SnapshotHealthDialog(service, report(), history=history())
    try:
        before = dialog.panel.report
        dialog.panel.refresh_button.click()
        wait_until(app, lambda: service.hardware_health_details.called and dialog._worker is None)
        assert dialog.panel.report == before
        assert 'refresh unavailable' in dialog.panel.status.text()
        assert 'saved readings remain visible' in dialog.history_status.text()
        assert dialog.panel.refresh_button.isEnabled()
    finally:
        dispose(app, dialog)


def test_failed_refresh_hides_private_exception_and_preserves_copyable_report(app):
    service = backend()
    service.hardware_health_details.side_effect = OSError('PRIVATE-DEVICE-PATH')
    dialog = dialogs.SnapshotHealthDialog(service, report(), history=history())
    try:
        dialog.panel.refresh_button.click()
        wait_until(app, lambda: service.hardware_health_details.called and dialog._worker is None)
        assert 'PRIVATE-DEVICE-PATH' not in dialog.panel.report_text() + dialog.history_status.text()
        assert '82.0%' in dialog.panel.report_text()
        assert dialog.panel.refresh_button.isEnabled()
    finally:
        dispose(app, dialog)


def test_fresh_report_with_unavailable_history_keeps_prior_graph(app):
    service = backend()
    service.hardware_health_details.side_effect = None
    fresh = report()
    fresh['batteries'][0]['capacity_retained_percent'] = 79
    service.hardware_health_details.return_value = {'report': fresh, 'history': {'status': 'unavailable'}}
    dialog = dialogs.SnapshotHealthDialog(service, report(), history=history())
    dialog.panel.history_plot.note.setText('Fixture prior history remains available.')
    try:
        dialog.panel.refresh_button.click()
        wait_until(app, lambda: service.hardware_health_details.called and dialog._worker is None)
        assert dialog.panel.report == fresh
        assert dialog.panel.history_plot.note.text() == 'Fixture prior history remains available.'
        assert 'prior graph data remains visible' in dialog.history_status.text()
    finally:
        dispose(app, dialog)
