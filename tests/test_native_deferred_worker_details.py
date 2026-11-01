"""Long saved-installer warnings stay readable without hiding their evidence."""
import copy
from types import SimpleNamespace

import pytest
from PySide6.QtCore import QPoint, QRect, Qt
from PySide6.QtGui import QFont

from test_native_worker_cards import app, fonts, progress
from test_native_auto_continue_ui import wait_until
from optimus.ui import WorkflowSummaryLabel


@pytest.fixture(autouse=True)
def reduced_motion(monkeypatch):
    monkeypatch.setenv('OPTIMUS_REDUCE_MOTION', '1')


JOURNAL = r'D:\FixtureUser\Optimus\Data\parallel-updates\aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa.json'
REASON = ('A previous update installer has an unresolved outcome; no automatic retry was started. '
          'Review installer state and the saved record before continuing: ' + JOURNAL)
SHORT_WARNING = 'Previous installer needs review.\nNo automatic retry started.'


def deferred_snapshot():
    return {'stage': 'Apply saved privacy controls', 'percentage': 40,
        'description': 'Applying the saved privacy controls.',
        'jobs': [{'id': identity, 'status': 'deferred', 'description': REASON,
                  'current': 0, 'total': 0, 'elapsed_seconds': 0}
                 for identity in ('software_updates', 'driver_updates')]}


def assert_fitting_summary(label):
    lines = label.visible_text.splitlines()
    assert 1 <= len(lines) <= 2
    assert all(label.fontMetrics().horizontalAdvance(line) <= label.contentsRect().width() for line in lines)
    assert label.height() >= len(lines) * label.fontMetrics().lineSpacing()


def assert_compact_fields_fit(card, window):
    controls = (card.heading, card.status, card.activity_ring, card.progress, card.totals,
                *card.navigation_buttons.values())
    for field in controls:
        assert field.isVisible()
        assert card.rect().contains(QRect(field.mapTo(card, QPoint()), field.size()))
        assert window.rect().contains(QRect(field.mapTo(window, QPoint()), field.size()))
    status = QRect(card.status.mapTo(card, QPoint()), card.status.size())
    count = QRect(card.progress.mapTo(card, QPoint()), card.progress.size())
    assert status.bottom() < count.top()
    assert all(label.isHidden() for label in (card.elapsed, card.activity_state, card.navigation_note))
    assert_optional_reason_fit(card, window)


def assert_optional_reason_fit(card, window):
    if not card.details.isHidden():
        assert_fitting_summary(card.details)
        reason = QRect(card.details.mapTo(card, QPoint()), card.details.size())
        assert card.rect().contains(reason)
        assert window.rect().contains(QRect(card.details.mapTo(window, QPoint()), card.details.size()))
        controls = (card.heading, card.status, card.activity_ring, card.progress, card.totals,
                    *card.navigation_buttons.values())
        assert all(not reason.intersects(QRect(field.mapTo(card, QPoint()), field.size())) for field in controls)


def assert_short_installer_warning(card, expected=SHORT_WARNING):
    label = card.details
    # The concise message remains the source of the card caption. A real
    # one-line slot may elide it, while complete evidence stays available.
    assert label._summary == expected
    assert label.toolTip() == label.text() == label.accessibleDescription() == label.whatsThis()
    if label.isHidden():
        return
    assert_fitting_summary(label)
    metrics, width = label.fontMetrics(), label.contentsRect().width()
    if label._max_lines == 1:
        painted = metrics.elidedText(' '.join(expected.split()), Qt.ElideRight, width)
        assert label.visible_text == painted
        if metrics.horizontalAdvance(' '.join(expected.split())) > width:
            assert label.visible_text.endswith('…')
    elif all(metrics.horizontalAdvance(line) <= width for line in expected.splitlines()):
        # Do not relax the full-warning assertion when two complete lines fit.
        assert label.visible_text == expected
    else:
        painted, complete = ' '.join(label.visible_text.split()), ' '.join(expected.split())
        assert (painted.endswith('…') and complete.startswith(painted[:-1])
                or painted == complete)


def assert_body_reachable(widget, window, app):
    window.body_scroll.ensureWidgetVisible(widget, 0, 2)
    def ready():
        if not widget.isVisibleTo(window):
            return False
        ancestor = widget.parentWidget()
        while ancestor is not None:
            if not ancestor.contentsRect().contains(QRect(widget.mapTo(ancestor, QPoint()), widget.size())):
                return False
            if ancestor is window:
                hit = window.childAt(widget.mapTo(window, widget.rect().center()))
                return hit is widget or hit is not None and widget.isAncestorOf(hit)
            ancestor = ancestor.parentWidget()
        return False
    wait_until(app, ready, diagnostics=lambda: repr({
        'widget': widget.objectName(), 'geometry': widget.geometry().getRect(),
        'body_viewport': window.body_scroll.viewport().geometry().getRect(),
        'scroll': window.body_scroll.verticalScrollBar().value()}))


@pytest.mark.parametrize('width', [850, 1024])
def test_deferred_cards_keep_primary_fields_visible_and_full_warning_in_details(progress, app, width):
    progress.resize(width, 650)
    progress.update_progress(deferred_snapshot())
    app.processEvents()
    for identity in ('software_updates', 'driver_updates'):
        card = progress.worker_cards[identity]
        assert card.status.text() == 'Deferred' and not card._elapsed_active
        assert_short_installer_warning(card)
        assert_compact_fields_fit(card, progress)
        assert card.details.text() == REASON == card.details.toolTip() == card.details.accessibleDescription()
        assert REASON in progress.worker_details.toPlainText()
        assert card.progress.value() == 0 and card.progress.maximum() > 0
        assert card.progress.format() == 'Search not started'
        assert card.elapsed.text() == 'Elapsed: —'
    assert progress.worker_cards['maintenance'].status.text() == 'Running'
    assert progress.progress.value() == 40 and progress.cancel_button.isEnabled()
    assert not progress.details_toggle.isChecked() and progress.background_status.isVisibleTo(progress)
    assert progress.analysis_panel.isAncestorOf(progress.background_status)
    assert not progress.content_scroll.isAncestorOf(progress.background_status)
    assert_body_reachable(progress.background_status, progress, app)
    assert not progress.log.isVisible() and not progress.processes.isVisible()
    assert JOURNAL in progress.background_status.text()
    progress.details_toggle.click()
    app.processEvents()
    assert progress.log.isVisibleTo(progress) and progress.processes.isVisibleTo(progress)
    assert REASON in progress.log.toPlainText()
    assert_body_reachable(progress.processes.viewport(), progress, app)
    assert_body_reachable(progress.log.viewport(), progress, app)
    assert_body_reachable(progress.background_status, progress, app)
    assert progress.background_status.isVisible() and not progress.background_status.visibleRegion().isEmpty()
    assert_fitting_summary(progress.background_status)
    assert progress.rect().contains(QRect(progress.cancel_button.mapTo(progress, QPoint()), progress.cancel_button.size()))


def test_shared_warning_preserves_complete_evidence_once_across_heartbeats(progress):
    state = deferred_snapshot()
    original = copy.deepcopy(state)
    for _ in range(5):
        progress.update_progress(state)
    for identity in ('software_updates', 'driver_updates'):
        label = progress.worker_cards[identity].details
        assert label.text() == REASON and label.toolTip() == REASON
        assert label.accessibleDescription() == REASON and label.whatsThis() == REASON
    assert progress.background_status.text().count(JOURNAL) == 1
    assert progress.background_status.toolTip().count(JOURNAL) == 1
    assert progress.background_status.accessibleDescription().count(JOURNAL) == 1
    assert JOURNAL not in progress.background_status.visible_text
    assert 'App updates: deferred' in progress.background_status.visible_text
    assert 'Driver updates: deferred' in progress.background_status.visible_text
    assert progress.log.toPlainText().count(REASON) == 1
    assert state == original


def test_distinct_saved_records_are_not_collapsed_into_one_warning(progress):
    state = deferred_snapshot()
    other_journal = JOURNAL.replace('a' * 32, 'b' * 32)
    state['jobs'][1]['description'] = REASON.replace(JOURNAL, other_journal)
    progress.update_progress(state)
    assert progress.log.toPlainText().count(JOURNAL) == 1
    assert progress.log.toPlainText().count(other_journal) == 1
    assert other_journal in progress.worker_cards['driver_updates'].details.toolTip()
    assert progress.background_status.text().count(JOURNAL) == 1
    assert progress.background_status.text().count(other_journal) == 1


def test_other_long_waits_are_elided_without_fabricating_an_installer_warning(progress, app):
    reason = 'Temporary cleanup is still using an installer folder: ' + 'D:\\' + 'folder-' * 80
    state = deferred_snapshot()
    state['jobs'] = [{'id': 'software_updates', 'status': 'waiting_for_maintenance',
        'description': reason, 'current': 3, 'total': 7, 'elapsed_seconds': 12}]
    progress.update_progress(state)
    app.processEvents()
    card = progress.worker_cards['software_updates']
    assert card.status.text() == 'Waiting for maintenance' and card._elapsed_active
    assert '…' in card.details.visible_text and 'Previous installer' not in card.details.visible_text
    assert card.details.toolTip() == reason and card.details.accessibleDescription() == reason
    assert card.details.text() == reason and reason in progress.worker_details.toPlainText()
    assert card.progress.value() == 3 and card.progress.maximum() == 7
    assert_compact_fields_fit(card, progress)


def test_expanded_secondary_summary_reflows_on_resize_and_for_larger_fonts(progress, app):
    progress.update_progress(deferred_snapshot())
    progress.details_toggle.click()
    label = progress.background_status
    for width in (850, 1024, 850):
        progress.resize(width, 650)
        app.processEvents()
        assert_body_reachable(label, progress, app)
        assert label.isVisible() and not label.visibleRegion().isEmpty()
        assert_fitting_summary(label)
    enlarged = QFont(label.font())
    enlarged.setPixelSize(18)
    label.setFont(enlarged)
    app.processEvents()
    assert_body_reachable(label, progress, app)
    assert_fitting_summary(label)
    label.clear()
    assert label.visible_text == '' and label.text() == '' and label.toolTip() == ''


@pytest.mark.parametrize('lines', [1, 2])
def test_compact_warning_requires_fitted_text_and_preserves_the_complete_evidence(app, lines):
    label = WorkflowSummaryLabel(max_lines=lines)
    metrics = label.fontMetrics()
    width = max(metrics.horizontalAdvance(line) for line in SHORT_WARNING.splitlines()) + 8
    label.setFixedWidth(width)
    label.set_summary(SHORT_WARNING, REASON)
    label.show()
    app.processEvents()
    card = SimpleNamespace(details=label)
    try:
        assert_short_installer_warning(card)
        assert label.toolTip() == label.accessibleDescription() == label.whatsThis() == REASON
        if lines == 1:
            assert label.visible_text.endswith('…') and 'No automatic retry started.' not in label.visible_text
        else:
            assert label.visible_text == SHORT_WARNING
        # Correct hover evidence cannot excuse an unfitted or false card caption.
        label.visible_text = label.visible_text.replace('review', 'retry allowed') + ' extra misleading text'
        with pytest.raises(AssertionError):
            assert_short_installer_warning(card)
    finally:
        label.close()
        label.deleteLater()
        app.processEvents()
