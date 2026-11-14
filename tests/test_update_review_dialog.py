"""Review button on update workers, and the Proceed / Deny review window."""
from __future__ import annotations

import json
import os

import pytest

pytest.importorskip('PySide6')
from PySide6.QtWidgets import QApplication  # noqa: E402

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
