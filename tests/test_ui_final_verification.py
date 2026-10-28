"""The final outcome report must be readable before any recommendation dialog."""
import copy
import threading
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from PySide6.QtCore import QPoint
from PySide6.QtWidgets import QApplication

from optimus.ui import OptimizationProgressWindow, OptimusWindow
from optimus.workflow import DEFAULTS


@pytest.fixture(scope='module')
def qapp():
    return QApplication.instance() or QApplication([])


def result(**changes):
    value = {'status': 'completed', 'stages': [], 'cancelled': False,
        'final_verification': {'schema': 1, 'read_only': True, 'status': 'needs_review', 'failed_items': 0,
            'coverage_complete': True, 'counts': {'confirmed': 1, 'needs_review': 1, 'unverified': 1, 'not_applicable': 2},
            'stages': [{'title': 'Fixture stage', 'status': 'needs_review', 'checks': [
                {'status': 'unverified', 'reason': 'A local fixture identity could not be verified.'}]}]},
        'security_review': {'recommendations': [{'id': 'fixture', 'title': 'Fixture recommendation'}]},
        'process_review': {'complete': True, 'actions': []}}
    value.update(changes)
    return value


def window(qapp, tmp_path):
    backend = SimpleNamespace(data_dir=tmp_path, preferences=lambda: copy.deepcopy(DEFAULTS))
    value = OptimusWindow(backend, smoke_test=True)
    value.smoke_test = False
    value.show()
    value.busy = True
    value._worker = SimpleNamespace(settings={'post_process_review_enabled': True}, _browser_prompts=None)
    progress = OptimizationProgressWindow(value, threading.Event())
    progress.resultsAcknowledged.connect(value._continue_post_optimization_reviews)
    progress.resultsDismissed.connect(value._dismiss_post_optimization_reviews)
    value._progress_window = progress
    progress.show()
    return value, progress


def dispose(value, qapp):
    value.smoke_test = True
    value._pending_post_review = None
    value._post_review_active = False
    if value._progress_window is not None:
        value._progress_window.close()
    value.shutdown()
    value.close()
    value.deleteLater()
    qapp.processEvents()


def assert_pinned_report_visible(progress):
    for widget in (progress.verification_summary, progress.verification_details):
        assert widget.isVisibleTo(progress)
        assert not progress.content_scroll.isAncestorOf(widget)
        assert progress.rect().contains(widget.rect().translated(widget.mapTo(progress, QPoint())))
        ancestor = widget.parentWidget()
        while ancestor is not None:
            assert ancestor.contentsRect().contains(widget.rect().translated(widget.mapTo(ancestor, QPoint())))
            if ancestor is progress:
                break
            ancestor = ancestor.parentWidget()
        assert ancestor is progress
    assert progress.verification_details.viewport().height() >= 45
    assert progress.worker_grid.mapTo(progress, progress.worker_grid.rect().bottomLeft()).y() < progress.completion_review.mapTo(progress, QPoint()).y()


def test_completed_report_is_visible_before_user_continues_to_prompts(qapp, tmp_path):
    main, progress = window(qapp, tmp_path)
    observed = []
    def offer(actual, settings):
        assert_pinned_report_visible(progress)
        assert progress.verification_summary.isVisible()
        assert 'Needs review' in progress.verification_summary.text()
        assert 'could not be verified' in progress.verification_details.toPlainText()
        observed.append('recommendations')
        main._post_review_active = False
    main._offer_post_optimization_reviews = Mock(side_effect=offer)
    try:
        actual = result()
        main._workflow_result(actual)
        main._workflow_finished()
        qapp.processEvents()
        main._offer_post_optimization_reviews.assert_not_called()
        assert progress.cancel_button.text() == 'Continue to recommendations'
        assert main._post_review_active and not main.busy
        assert len(main.main_buttons) == 4
        assert '1 unverified' in progress.verification_summary.text()
        assert '"needs_review"' in progress.verification_details.toPlainText()
        assert_pinned_report_visible(progress)
        observed.append('report-visible')
        progress.cancel_button.click()
        qapp.processEvents()
        qapp.processEvents()
        assert observed == ['report-visible', 'recommendations']
        main._offer_post_optimization_reviews.assert_called_once_with(actual, {'post_process_review_enabled': True})
        assert main._pending_post_review is None
    finally:
        dispose(main, qapp)


@pytest.mark.parametrize('dismiss', ['close', 'reject'])
def test_dismissing_results_skips_prompts_and_unblocks_the_window(qapp, tmp_path, dismiss):
    main, progress = window(qapp, tmp_path)
    main._offer_post_optimization_reviews = Mock()
    try:
        main._workflow_result(result())
        main._workflow_finished()
        assert main._post_review_active
        getattr(progress, dismiss)()
        qapp.processEvents()
        assert not main._post_review_active and main._pending_post_review is None
        main._offer_post_optimization_reviews.assert_not_called()
        assert not progress.cancel.is_set()
    finally:
        dispose(main, qapp)


@pytest.mark.parametrize('status,label', [('confirmed', 'Selected checks confirmed'),
    ('unverified', 'Not fully verified'), ('not_applicable', 'Review or no-change checks only')])
def test_status_summary_does_not_turn_unknown_or_review_into_all_clear(qapp, tmp_path, status, label):
    main, progress = window(qapp, tmp_path)
    try:
        actual = result(security_review=None, process_review=None)
        actual['final_verification']['status'] = status
        actual['final_verification']['counts'] = {'confirmed': int(status == 'confirmed'),
            'needs_review': 0, 'unverified': int(status == 'unverified'), 'not_applicable': int(status == 'not_applicable')}
        main._workflow_result(actual)
        main._workflow_finished()
        assert label in progress.verification_summary.text()
        assert 'all clear' not in progress.verification_summary.text().lower()
        assert progress.cancel_button.text() == 'Close results'
        assert not main._post_review_active
    finally:
        dispose(main, qapp)


def test_cancelled_run_without_verification_has_no_post_run_prompts(qapp, tmp_path):
    main, progress = window(qapp, tmp_path)
    main._offer_post_optimization_reviews = Mock()
    try:
        actual = result(cancelled=True, status='cancelled')
        del actual['final_verification']
        main._workflow_result(actual)
        main._workflow_finished()
        qapp.processEvents()
        assert 'not run' in progress.verification_summary.text()
        assert 'cancelled' in progress.verification_summary.text()
        assert not main._post_review_active and main._pending_post_review is None
        main._offer_post_optimization_reviews.assert_not_called()
    finally:
        dispose(main, qapp)


def test_read_only_observed_changes_are_prominent_with_expanded_evidence(qapp, tmp_path):
    main, progress = window(qapp, tmp_path)
    try:
        insights = {'schema':1,'read_only':True,
            'summary':'Available RAM 2.0 to 2.5 GB; one new startup entry was observed. Measurements do not prove a speed gain.',
            'resource_changes':{'memory_available_bytes':{'before':2000000000,'after':2500000000}},
            'startup_changes':{'new_count':1}}
        main._workflow_result(result(maintenance_insights=insights))
        main._workflow_finished()
        assert progress.insights_summary.isVisible()
        assert insights['summary'] in progress.insights_summary.text()
        assert 'READ-ONLY MAINTENANCE OBSERVATIONS' in progress.verification_details.toPlainText()
        assert '"new_count": 1' in progress.verification_details.toPlainText()
        assert progress.verification_details.isReadOnly()
        assert progress.verification_summary.isVisible()
        assert_pinned_report_visible(progress)
        assert progress.rect().contains(progress.insights_summary.rect().translated(progress.insights_summary.mapTo(progress, QPoint())))
        assert insights['summary'] in progress.insights_summary.toolTip()
        assert len(main.main_buttons) == 4
    finally:
        dispose(main, qapp)


def test_missing_or_unattested_observations_do_not_invent_resource_results(qapp, tmp_path):
    main, progress = window(qapp, tmp_path)
    try:
        main._workflow_result(result(maintenance_insights={'summary':'Unattested fixture speed claim'}))
        main._workflow_finished()
        assert progress.insights_summary.isHidden()
        assert 'Unattested fixture speed claim' not in progress.verification_details.toPlainText()
    finally:
        dispose(main, qapp)
