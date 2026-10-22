"""Skipped work is not a failed postcondition; real receipts still get checked."""
import copy
from unittest.mock import Mock

import pytest

from optimus.verification import OutcomeVerification


def recorded(title, value, status='completed'):
    return {'title': title, 'result': value, 'status': status, 'started_at': 100, 'finished_at': 110}


def verify(tmp_path, stages, **options):
    native = Mock(return_value={})
    tool = OutcomeVerification(tmp_path, native_reader=native, **options)
    report = {'id': 'a' * 32, 'stages': stages, 'settings': {}}
    before = copy.deepcopy(report)
    result = tool.verify(report)
    assert report == before
    return result, native


@pytest.mark.parametrize('title', ['Clean temporary files', 'Clean selected additional categories',
    'Clean selected browser data', 'Scan for spyware and malware', 'Optimize eligible internal drives'])
@pytest.mark.parametrize('flag', ['skipped', 'review_only'])
def test_channel_specific_readers_do_not_run_for_unapplied_work(tmp_path, title, flag):
    drive = Mock(side_effect=AssertionError('A skipped drive operation has no receipt to read'))
    result, native = verify(tmp_path, [recorded(title, {flag: True, 'items': [], 'stop_reason': 'Fixture preflight deferral'})],
                            drive_reader=drive)
    assert result['status'] == 'not_applicable'
    assert result['counts'] == {'confirmed': 0, 'needs_review': 0, 'unverified': 0, 'not_applicable': 1}
    assert result['failed_items'] == 0 and result['coverage_complete'] is True
    native.assert_not_called()
    drive.assert_not_called()


@pytest.mark.parametrize('status', ['failed', 'cancelled'])
def test_original_failure_survives_a_skipped_outcome_label(tmp_path, status):
    result, _ = verify(tmp_path, [recorded('Scan for spyware and malware', {'skipped': True}, status)])
    assert result['status'] == 'needs_review'
    assert result['counts']['needs_review'] == result['counts']['not_applicable'] == 1
    assert result['failed_items'] == 0  # Do not count the original failure twice.


def test_skipped_label_cannot_hide_a_claimed_file_deletion(tmp_path):
    target = tmp_path / 'retained.fixture'
    target.write_text('Must remain untouched')
    info = target.stat()
    value = {'skipped': True, 'result': {'deleted_count': 1, 'files': [{'path': str(target),
        'status': 'deleted', 'identity': {'device': info.st_dev, 'inode': info.st_ino,
        'size': info.st_size, 'modified_ns': info.st_mtime_ns, 'created_ns': info.st_ctime_ns}}]}}
    result, _ = verify(tmp_path, [recorded('Clean temporary files', value)])
    assert result['status'] == 'needs_review' and result['failed_items'] == 1
    assert target.read_text() == 'Must remain untouched'


def test_review_label_cannot_hide_reported_defender_failure(tmp_path):
    result, _ = verify(tmp_path, [recorded('Scan for spyware and malware',
        {'review_only': True, 'failed_items': 1, 'status': 'blocked'})])
    assert result['status'] == 'needs_review'
    assert result['counts']['not_applicable'] == 0


def test_review_label_cannot_hide_a_claimed_drive_change(tmp_path):
    drive = Mock(return_value={'schema': 1, 'read_only': True, 'status': 'needs_review',
        'checks': [{'target': 'Fixture drive', 'status': 'needs_review', 'reason': 'Fixture mismatch'}]})
    result, _ = verify(tmp_path, [recorded('Optimize eligible internal drives',
        {'review_only': True, 'items': [{'status': 'completed'}]})], drive_reader=drive)
    drive.assert_called_once()
    assert result['status'] != 'not_applicable'
