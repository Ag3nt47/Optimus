"""Long saved-installer warnings stay readable without hiding their evidence."""
import copy

import pytest
from PySide6.QtGui import QFont

from test_native_worker_cards import app, fonts, progress


JOURNAL = r'D:\FixtureUser\Optimus\Data\parallel-updates\aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa.json'
REASON = ('A previous update installer has an unresolved outcome; no automatic retry was started. '
          'Review installer state and the saved record before continuing: ' + JOURNAL)


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


@pytest.mark.parametrize('width', [850, 1024])
def test_deferred_cards_show_two_fitting_lines_without_overlapping_counts(progress, app, width):
    progress.resize(width, 650)
    progress.update_progress(deferred_snapshot())
    app.processEvents()
    for identity in ('software_updates', 'driver_updates'):
        card = progress.worker_cards[identity]
        assert card.status.text() == 'Deferred' and not card._elapsed_active
        assert 'Previous installer needs review.' in card.details.visible_text
        assert 'No automatic retry started.' in card.details.visible_text
        assert card.details.geometry().bottom() < card.progress.geometry().top()
        assert card.progress.geometry().bottom() < card.elapsed.geometry().top()
        assert card.progress.value() == 0 and card.progress.maximum() > 0
        assert_fitting_summary(card.details)
    assert progress.worker_cards['maintenance'].status.text() == 'Running'
    assert progress.progress.value() == 40 and progress.cancel_button.isEnabled()
    assert_fitting_summary(progress.background_status)
    assert progress.cancel_button.geometry().bottom() <= progress.height()


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
    assert card.progress.value() == 3 and card.progress.maximum() == 7
    assert card.details.geometry().bottom() < card.progress.geometry().top()
    assert_fitting_summary(card.details)


def test_summary_reflows_on_resize_and_updates_its_height_for_larger_fonts(progress, app):
    progress.update_progress(deferred_snapshot())
    label = progress.worker_cards['software_updates'].details
    for width in (850, 1024, 850):
        progress.resize(width, 650)
        app.processEvents()
        assert_fitting_summary(label)
    enlarged = QFont(label.font())
    enlarged.setPixelSize(18)
    label.setFont(enlarged)
    app.processEvents()
    assert_fitting_summary(label)
    label.clear()
    assert label.visible_text == '' and label.text() == '' and label.toolTip() == ''
