"""Review button on update workers, and the Proceed / Deny review window."""
from __future__ import annotations

import json
import os
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest

pytest.importorskip('PySide6')
from PySide6.QtCore import QEvent  # noqa: E402
from PySide6.QtTest import QTest  # noqa: E402
from PySide6.QtWidgets import QApplication, QDialog  # noqa: E402
from shiboken6 import isValid  # noqa: E402

from optimus.parallel_updates import NATIVE_SOFTWARE, WINDOWS_DRIVERS  # noqa: E402
from optimus.ui import WorkflowWorkerCard  # noqa: E402
from optimus.widgets import update_review_dialog as review  # noqa: E402


@pytest.fixture(scope='module')
def app():
    os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
    return QApplication.instance() or QApplication([])


def _journal(data_dir, name, title, status, mtime, **result):
    folder = data_dir / 'parallel-updates'
    folder.mkdir(parents=True, exist_ok=True)
    record = {'schema': 1, 'id': name, 'title': title, 'status': status, 'started_at': 1.0,
              'finished_at': 2.0, 'note': 'No automatic retry is authorized by this record.',
              'result': dict({'installer_started': False, 'outcomeKnown': True, 'items': []}, **result)}
    path = folder / (name + '.json')
    path.write_text(json.dumps(record), encoding='utf-8')
    os.utime(path, (mtime, mtime))
    return record


SCAN = {'source': 'macOS native update providers', 'complete': False, 'offers_count': None,
        'summary': 'update search incomplete; the number of available updates is unknown.',
        'errors': [{'source': 'macOS native update providers',
                    'error': 'Checked Apple Software Update and found no supported third-party package manager.'}]}


def test_latest_journal_picks_newest_attention_record_for_the_lane(tmp_path):
    _journal(tmp_path, 'old', NATIVE_SOFTWARE, 'needs_review', 100)
    _journal(tmp_path, 'done', NATIVE_SOFTWARE, 'completed', 300)
    _journal(tmp_path, 'new', NATIVE_SOFTWARE, 'needs_review', 200, app_scan=SCAN)
    _journal(tmp_path, 'driver', WINDOWS_DRIVERS, 'blocked', 400)
    record, path = review.latest_journal(tmp_path, 'software_updates')
    assert record['id'] == 'new' and path.name == 'new.json'
    assert review.latest_journal(tmp_path, 'driver_updates')[0]['id'] == 'driver'
    assert review.latest_journal(tmp_path / 'missing', 'software_updates') == (None, None)


def test_report_explains_the_stop_and_each_offer(tmp_path):
    record = _journal(tmp_path, 'j1', NATIVE_SOFTWARE, 'needs_review', 1, app_scan=SCAN, items=[
        {'name': 'Example App', 'version': '1.0', 'available': '1.1', 'status': 'failed',
         'result': {'error': 'Installer exited with code 1'}}])
    text = review.describe('software_updates', {'status': 'needs_review',
                                                'description': 'Search needs review'}, record, tmp_path / 'j1.json')
    for expected in ('Needs review'.lower(), 'found no supported third-party package manager',
                     'Example App (1.0 → 1.1): failed — Installer exited with code 1',
                     'Complete: no', 'Journal file:'):
        assert expected.lower() in text.lower()


def test_deny_records_decision_without_touching_journal(app, tmp_path):
    _journal(tmp_path, 'j1', NATIVE_SOFTWARE, 'needs_review', 1, app_scan=SCAN)
    before = (tmp_path / 'parallel-updates/j1.json').read_bytes()
    dialog = review.UpdateReviewDialog(object(), 'software_updates', {'status': 'needs_review'},
                                       data_dir=tmp_path)
    seen = []
    dialog.denied.connect(lambda lane, entry: seen.append((lane, entry)))
    dialog.deny()
    saved = json.loads((tmp_path / review.DECISIONS_FILE).read_text())
    assert saved['decisions'][-1]['decision'] == 'denied'
    assert saved['decisions'][-1]['journal_id'] == 'j1'
    assert seen and seen[0][0] == 'software_updates'
    assert (tmp_path / 'parallel-updates/j1.json').read_bytes() == before


def test_proceed_runs_only_the_saved_continuation_and_reports_it(app, tmp_path, monkeypatch):
    _journal(tmp_path, 'j1', NATIVE_SOFTWARE, 'needs_review', 1, app_scan=SCAN)
    dialog = review.UpdateReviewDialog(object(), 'software_updates', {'status': 'needs_review'},
                                       data_dir=tmp_path)
    report = {'status': 'completed_with_skips',
              'workers': {'software_updates': {'status': 'completed', 'description': 'done'}}}
    calls = []
    monkeypatch.setattr(dialog, '_run_continuation', lambda: calls.append(1) or report)
    seen = []
    dialog.proceeded.connect(lambda lane, value: seen.append((lane, value)))
    dialog._proceed_finished(dialog._run_continuation())
    assert calls == [1] and seen == [('software_updates', report)]
    assert 'some individual offers were skipped' in dialog.status.text()
    assert json.loads((tmp_path / review.DECISIONS_FILE).read_text())['decisions'][-1]['decision'] \
        == 'proceeded:completed_with_skips'


def test_running_worker_cannot_be_decided(app, tmp_path):
    dialog = review.UpdateReviewDialog(object(), 'software_updates', {'status': 'installing'},
                                       data_dir=tmp_path)
    assert not dialog.proceed_button.isEnabled() and not dialog.deny_button.isEnabled()


def test_proceed_waits_for_the_other_lanes_and_never_starts_a_second_worker(app, tmp_path):
    parent = QDialog()
    owner = SimpleNamespace(busy=True)
    parent.owner = owner
    dialog = review.UpdateReviewDialog(object(), 'software_updates', {'status': 'needs_review'},
                                       parent, data_dir=tmp_path)
    try:
        assert not dialog.proceed_button.isEnabled()
        assert dialog.deny_button.isEnabled()
        assert 'Maintenance is still running' in dialog.status.text()
        with patch('optimus.ui.BackendWorker') as worker:
            dialog.proceed()
        worker.assert_not_called()
        assert not (tmp_path / review.DECISIONS_FILE).exists()
    finally:
        dialog.deleteLater()
        parent.deleteLater()
        app.processEvents()


def test_finishing_maintenance_enables_proceed_without_automatically_installing(app, tmp_path):
    parent = QDialog()
    owner = SimpleNamespace(busy=True)
    parent.owner = owner
    dialog = review.UpdateReviewDialog(object(), 'software_updates', {'status': 'needs_review'},
                                       parent, data_dir=tmp_path)
    dialog._run_continuation = Mock()
    try:
        dialog.show()
        app.processEvents()
        assert not dialog.proceed_button.isEnabled()
        owner.busy = False
        QTest.qWait(650)
        app.processEvents()
        assert dialog.proceed_button.isEnabled()
        assert 'Maintenance has finished' in dialog.status.text()
        dialog._run_continuation.assert_not_called()
        assert not (tmp_path / review.DECISIONS_FILE).exists()
        dialog.hide()
        assert not dialog._maintenance_timer.isActive()
    finally:
        dialog.deleteLater()
        parent.deleteLater()
        app.processEvents()


def test_backend_active_workflow_also_blocks_proceed(app, tmp_path):
    backend = SimpleNamespace(workflow=SimpleNamespace(active=True))
    dialog = review.UpdateReviewDialog(backend, 'driver_updates', {'status': 'needs_review'},
                                       data_dir=tmp_path)
    try:
        with patch('optimus.ui.BackendWorker') as worker:
            dialog.proceed()
        worker.assert_not_called()
        assert not dialog.proceed_button.isEnabled()
        backend.workflow.active = False
        dialog._refresh_maintenance_state()
        assert dialog.proceed_button.isEnabled()
    finally:
        dialog.deleteLater()
        app.processEvents()


def test_review_dialogs_share_one_in_flight_continuation(app, tmp_path, monkeypatch):
    backend = SimpleNamespace(workflow=SimpleNamespace(active=False))
    first = review.UpdateReviewDialog(backend, 'software_updates', {'status': 'needs_review'},
                                      data_dir=tmp_path)
    second = review.UpdateReviewDialog(backend, 'driver_updates', {'status': 'needs_review'},
                                       data_dir=tmp_path)
    queued = []
    pool = SimpleNamespace(start=lambda worker: queued.append(worker))
    monkeypatch.setattr(review.QThreadPool, 'globalInstance', lambda: pool)
    try:
        first.proceed()
        second.proceed()
        assert len(queued) == 1
        assert not second.proceed_button.isEnabled()
        assert 'Maintenance is still running' in second.status.text()
        queued[0].signals.result.emit({'status': 'completed_with_skips'})
        queued[0].signals.finished.emit()
        app.processEvents()
        second._refresh_maintenance_state()
        assert second.proceed_button.isEnabled()
        assert not review._CONTINUATIONS
    finally:
        for worker in queued:
            worker.signals.finished.emit()
        first.deleteLater()
        second.deleteLater()
        app.processEvents()


def test_continuation_reservation_releases_even_after_dialog_deletion(app, tmp_path, monkeypatch):
    backend = SimpleNamespace(workflow=SimpleNamespace(active=False))
    first = review.UpdateReviewDialog(backend, 'software_updates', {'status': 'needs_review'},
                                      data_dir=tmp_path)
    queued = []
    monkeypatch.setattr(review.QThreadPool, 'globalInstance',
                        lambda: SimpleNamespace(start=lambda worker: queued.append(worker)))
    first.proceed()
    first.deleteLater()
    app.sendPostedEvents(None, QEvent.DeferredDelete)
    app.processEvents()
    assert not isValid(first)
    second = review.UpdateReviewDialog(backend, 'driver_updates', {'status': 'needs_review'},
                                       data_dir=tmp_path)
    try:
        assert not second.proceed_button.isEnabled()
        queued[0].signals.finished.emit()
        app.processEvents()
        second._refresh_maintenance_state()
        assert second.proceed_button.isEnabled()
        assert not review._CONTINUATIONS
    finally:
        queued[0].signals.finished.emit()
        second.deleteLater()
        app.processEvents()


def test_worker_start_failure_does_not_leave_continuation_reserved(app, tmp_path):
    backend = SimpleNamespace(workflow=SimpleNamespace(active=False))
    dialog = review.UpdateReviewDialog(backend, 'software_updates', {'status': 'needs_review'},
                                       data_dir=tmp_path)
    try:
        with patch('optimus.ui.BackendWorker', side_effect=RuntimeError('Fixture construction failure')):
            dialog.proceed()
        assert not dialog._continuation_active and not review._CONTINUATIONS
        assert 'Fixture construction failure' in dialog.status.text()
        assert dialog.proceed_button.isEnabled()
    finally:
        dialog.deleteLater()
        app.processEvents()


@pytest.mark.parametrize('state,visible', [
    ({'status': 'needs_review'}, True),
    ({'status': 'failed'}, True),
    ({'status': 'deferred', 'installation_blocked': True}, True),
    ({'status': 'completed', 'approval_required': True, 'worker_finished': True}, True),
    ({'status': 'completed'}, False),
    ({'status': 'running'}, False),
    ({'status': 'not_selected'}, False),
])
def test_review_button_appears_only_when_an_update_worker_needs_attention(app, state, visible):
    card = WorkflowWorkerCard('App updates')
    card.enable_review()
    card.show()
    card.update_state(state)
    assert card.review_button.isVisible() is visible
    clicks = []
    card.reviewRequested.connect(lambda: clicks.append(1))
    card.review_button.click()
    assert clicks == [1]


def test_review_button_is_never_shown_on_other_workers(app):
    card = WorkflowWorkerCard('Maintenance')
    card.show()
    card.update_state({'status': 'needs_review'})
    assert not card.review_button.isVisible()


def test_review_button_survives_compact_layout(app):
    card = WorkflowWorkerCard('App updates')
    card.enable_review()
    card.set_compact()
    card.show()
    card.update_state({'status': 'needs_review'})
    assert card.review_button.isVisible()
