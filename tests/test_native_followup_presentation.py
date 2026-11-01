"""Follow-up presentation retains measured facts and optional technical details."""
import copy
import threading

from test_native_progress_analysis import app, progress
from test_native_worker_cards import fonts
from optimus.ui import _combined_followup_totals
from optimus.worker_totals import MAX_BYTES, WorkerResourceTotals


def totals(**changes):
    value = WorkerResourceTotals().snapshot()
    value.update(changes)
    return value


def main_result():
    return {'id': 'main-fixture', 'status': 'completed_with_errors', 'cancelled': False,
        'resource_totals': totals(removed_bytes=4000, removed_files=8, removed_known=True),
        'worker_resource_totals': {'software_updates': totals(downloaded_bytes=1000,
            downloaded_known=True, installed_items=0, installed_known=True)},
        'stages': [{'title': 'Scan for spyware and malware', 'status': 'cancelled',
            'result': {'status': 'cancelled', 'completion_verified': False,
                'reason': 'You cancelled this scan.', 'elapsed_seconds': 12}}],
        'final_verification': {'status': 'needs_review', 'counts': {'needs_review': 1},
            'stages': [{'checks': [{'status': 'needs_review', 'reason': 'A fixture app remains unchanged.'}]}]}}


def followup_result():
    return {'id': 'followup-fixture', 'status': 'completed_with_errors', 'cancelled': False,
        'resource_totals': totals(),
        'worker_resource_totals': {'software_updates': totals(downloaded_bytes=500,
            downloaded_known=True, installed_items=1, installed_known=True,
            removed_bytes=500, removed_files=1, removed_known=True)},
        'stages': [], 'counts': {'completed': 1, 'failed': 1},
        'summary': 'One fixture update remains unresolved.',
        'final_verification': {'status': 'needs_review', 'counts': {'needs_review': 1},
            'stages': [{'checks': [{'status': 'needs_review', 'reason': 'The fixture installer failed.'}]}]}}


def test_followup_preserves_cancelled_scan_and_main_cleanup(progress):
    progress.configure_workers({'software_updates_enabled': True,
        'driver_updates_enabled': True, 'spyware_scan_enabled': True})
    progress.finish(main_result())
    scan = progress.worker_cards['security_scan']
    assert scan._raw_status == 'cancelled'
    before_scan = (scan.status.text(), scan.details.text(), scan.totals.text(), scan.elapsed.text())
    driver_status = progress.worker_cards['driver_updates']._raw_status
    progress.begin_auto_continue(threading.Event())
    assert scan._raw_status == 'cancelled'
    assert (scan.status.text(), scan.details.text(), scan.totals.text(), scan.elapsed.text()) == before_scan
    assert progress.worker_cards['driver_updates']._raw_status == driver_status
    assert progress.worker_cards['maintenance']._resource_totals['removed_bytes'] == 4000
    assert progress.resource_comparison.snapshot['removed_bytes'] == 4000
    assert progress.resource_comparison.snapshot['downloaded_bytes'] == 1000


def test_current_run_plus_followup_totals_are_absolute_and_finish_is_idempotent(progress):
    progress.finish(main_result())
    progress.begin_auto_continue(threading.Event())
    fresh = followup_result()
    original = copy.deepcopy(fresh)
    event = {'stage': 'Review or update configured software', 'percentage': 40,
        'resource_totals': fresh['resource_totals'],
        'worker_resource_totals': fresh['worker_resource_totals']}
    progress.update_progress(event)
    progress.update_progress(event)
    progress.finish_auto_continue(fresh)
    progress.finish_auto_continue(fresh)
    assert fresh == original
    card = progress.worker_cards['software_updates']
    assert card._resource_totals['downloaded_bytes'] == 1500
    assert card._resource_totals['installed_items'] == 1
    assert card._resource_totals['removed_bytes'] == 500
    assert progress.resource_comparison.snapshot['removed_bytes'] == 4500
    assert progress.resource_comparison.snapshot['removed_files'] == 9
    assert progress.resource_comparison.snapshot['downloaded_bytes'] == 1500
    assert 'Main run:' in card.totals.toolTip() and 'This follow-up:' in card.totals.toolTip()
    assert 'Main run + this follow-up.' in progress.resource_comparison.note.text()
    assert 'review findings' in progress.progress.format()
    assert 'does not mean every update' in progress.progress.toolTip()


def test_missing_or_invalid_followup_measurements_do_not_erase_main_facts(progress):
    progress.finish(main_result())
    progress.begin_auto_continue(threading.Event())
    progress.finish_auto_continue({'status': 'completed', 'stages': [],
        'worker_resource_totals': {'software_updates': {'downloaded_bytes': 'unknown'}}})
    assert progress.resource_comparison.snapshot['downloaded_bytes'] == 1000
    assert progress.resource_comparison.snapshot['removed_bytes'] == 4000


def test_plain_findings_visible_while_raw_verification_and_logs_stay_optional(progress, app):
    progress.finish(main_result())
    app.processEvents()
    assert progress.verification_summary.isVisibleTo(progress)
    assert progress.verification_findings.isVisibleTo(progress)
    assert 'A fixture app remains unchanged.' in progress.verification_findings.text()
    assert not progress.verification_details.isVisibleTo(progress)
    assert not progress.log.isVisibleTo(progress)
    assert progress.content_scroll.isAncestorOf(progress.verification_details)
    progress.details_toggle.setChecked(True)
    app.processEvents()
    assert progress.verification_details.isVisibleTo(progress)
    assert progress.log.isVisibleTo(progress)
    assert '"needs_review"' in progress.verification_details.toPlainText()
    progress.details_toggle.setChecked(False)
    app.processEvents()
    assert progress.verification_findings.isVisibleTo(progress)
    assert not progress.verification_details.isVisibleTo(progress)


def test_approval_caption_does_not_imply_a_driver_install_or_block(progress):
    card = progress.worker_cards['driver_updates']
    card.update_state({'status': 'completed', 'worker_finished': True, 'approval_required': True,
        'presentation_status': 'finished_with_skips', 'continuation_blocked': False,
        'scan_complete': True, 'scan_label': '0 approved matches / 1 found',
        'description': 'No driver offers match the saved approvals.'})
    assert card.status.text() == 'Checked; needs approval'
    assert card.progress.format() == '0 approved matches / 1 found'
    assert 'No driver offers match' in card.details.text()


def test_compact_card_uses_spare_height_for_the_reason_and_keeps_navigation_clear(progress, app):
    progress.resize(850, 810)
    progress.finish(main_result())
    card = progress.worker_cards['software_updates']
    card.update_state({'status': 'needs_review',
        'description': 'Example installer failed. Other updates continued.'})
    for _ in range(30):
        app.processEvents()
    assert card.details.isVisibleTo(progress)
    assert 'Other updates continued.' in card.details.text()
    assert card.details.height() >= card.details.fontMetrics().lineSpacing()
    for button in card.navigation_buttons.values():
        assert button.y() > card.details.y() + card.details.height()
        assert card.rect().contains(button.geometry())


def test_combined_totals_do_not_turn_overflow_into_a_measured_number():
    value = _combined_followup_totals(totals(downloaded_bytes=MAX_BYTES, downloaded_known=True),
        totals(downloaded_bytes=1, downloaded_known=True))
    assert value['downloaded_known'] is False and value['downloaded_partial'] is True
    assert value['downloaded_bytes'] == 0
