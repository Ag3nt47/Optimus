"""New toolkit postconditions are independent, bounded metadata reads only."""
import copy
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from optimus.verification import OutcomeVerification, toolkit_cleanup_checks, _TOOLKIT_FILE_STAGES, _TOOLKIT_READ_ONLY_STAGES
from optimus.native_workflow import verify_native_outcomes


def receipt(path):
    info = path.stat()
    return {'path': str(path), 'status': 'deleted', 'identity': {'device': info.st_dev, 'inode': info.st_ino,
        'size': info.st_size, 'modified_ns': info.st_mtime_ns, 'created_ns': info.st_ctime_ns}}


def result(rows, **changes):
    return dict({'schema': 1, 'complete': True, 'deleted_count': len(rows),
                 'deleted_bytes': sum(row['identity']['size'] for row in rows), 'files': rows}, **changes)


def verify(kind, data, value, title='Clean selected application caches'):
    source = {'id': 'a' * 32, 'stages': [{'title': title, 'status': 'completed', 'result': value}]}
    if kind == 'windows':
        def forbidden(*args, **kwargs):
            raise AssertionError('Toolkit metadata verification must not launch native queries or an actuator.')
        return OutcomeVerification(data, native_reader=forbidden).verify(source)
    return verify_native_outcomes(SimpleNamespace(data_dir=data), source)


@pytest.mark.parametrize('kind', ['windows', 'native'])
@pytest.mark.parametrize('title', sorted(_TOOLKIT_FILE_STAGES))
def test_missing_receipted_files_confirmed_without_replaying_cleanup(tmp_path, kind, title):
    path = tmp_path / 'disposable-fixture'
    path.write_bytes(b'fixture')
    row = receipt(path)
    path.unlink()
    checked = verify(kind, tmp_path / 'data', result([row]), title)
    assert checked['counts']['confirmed'] == 1 and checked['counts']['needs_review'] == 0
    assert checked['counts']['unverified'] == 0 and not path.exists()


@pytest.mark.parametrize('kind', ['windows', 'native'])
def test_unchanged_original_still_present_needs_review(tmp_path, kind):
    path = tmp_path / 'original-fixture'
    path.write_bytes(b'preserve')
    checked = verify(kind, tmp_path / 'data', result([receipt(path)]))
    assert checked['counts']['needs_review'] == 1 and path.read_bytes() == b'preserve'


@pytest.mark.parametrize('kind', ['windows', 'native'])
def test_current_different_inode_is_recreated_not_failed_deletion(tmp_path, kind):
    path = tmp_path / 'recreated-fixture'
    path.write_bytes(b'new contents')
    row = receipt(path)
    row['identity']['inode'] += 1000000  # Distinct original lifetime, without relying on OS inode reuse.
    checked = verify(kind, tmp_path / 'data', result([row]))
    assert checked['counts']['confirmed'] == 1 and checked['counts']['needs_review'] == 0
    assert path.read_bytes() == b'new contents'


@pytest.mark.parametrize('kind', ['windows', 'native'])
def test_partial_cleanup_preserves_individual_confirmation_and_unknown_coverage(tmp_path, kind):
    path = tmp_path / 'removed-fixture'
    path.write_bytes(b'fixture')
    row = receipt(path)
    path.unlink()
    checked = verify(kind, tmp_path / 'data', result([row], complete=False))
    assert checked['counts']['confirmed'] == 1 and checked['counts']['unverified'] == 1


@pytest.mark.parametrize('kind', ['windows', 'native'])
def test_missing_original_receipts_do_not_confirm_byte_count_alone(tmp_path, kind):
    checked = verify(kind, tmp_path / 'data', {'complete': True, 'deleted_count': 1, 'deleted_bytes': 128, 'items': []})
    assert checked['counts']['unverified'] and not checked['counts']['confirmed']


@pytest.mark.parametrize('kind', ['windows', 'native'])
def test_wrong_byte_totals_unverified_even_when_paths_absent(tmp_path, kind):
    path = tmp_path / 'fixture'
    path.write_bytes(b'fixture')
    row = receipt(path)
    path.unlink()
    checked = verify(kind, tmp_path / 'data', result([row], deleted_bytes=999))
    assert checked['counts']['confirmed'] == 1 and checked['counts']['unverified'] == 1


@pytest.mark.parametrize('kind', ['windows', 'native'])
@pytest.mark.parametrize('title', sorted(_TOOLKIT_READ_ONLY_STAGES))
def test_metadata_tools_explicitly_claim_no_maintenance_postcondition(tmp_path, kind, title):
    checked = verify(kind, tmp_path / 'data', {'schema': 1, 'read_only': True, 'complete': True}, title)
    assert checked['counts']['not_applicable'] == 1 and checked['counts']['unverified'] == 0
    assert checked['counts']['confirmed'] == 0 and not (tmp_path / 'data').exists()


def test_cancel_or_file_cap_reports_unknown_remaining_receipts(tmp_path):
    paths = [tmp_path / str(index) for index in range(2)]
    rows = []
    for path in paths:
        path.write_bytes(b'fixture')
        rows.append(receipt(path))
        path.unlink()
    checked = toolkit_cleanup_checks(result(rows), maximum=1)
    assert {row['status'] for row in checked} == {'confirmed', 'unverified'}
    assert toolkit_cleanup_checks(result(rows), stopped=lambda: True)[0]['status'] == 'unverified'


def test_posix_changed_ctime_same_inode_is_not_proof_of_recreation(tmp_path, monkeypatch):
    import optimus.verification as verification
    path = tmp_path / 'fixture'
    path.write_bytes(b'changed original')
    row = receipt(path)
    row['identity']['created_ns'] -= 100000
    monkeypatch.setattr(verification.os, 'name', 'posix')
    checked = toolkit_cleanup_checks(result([row]))
    assert [row['status'] for row in checked] == ['unverified']
    assert path.read_bytes() == b'changed original'


def test_duplicate_or_relative_receipts_never_confirm_unknown_targets(tmp_path):
    path = tmp_path / 'fixture'
    path.write_bytes(b'fixture')
    row = receipt(path)
    path.unlink()
    duplicate = toolkit_cleanup_checks(result([row, copy.deepcopy(row)]))
    assert any(item['status'] == 'unverified' for item in duplicate)
    row['path'] = '../private-file'
    assert toolkit_cleanup_checks(result([row]))[0]['status'] == 'unverified'


@pytest.mark.parametrize('kind', ['windows', 'native'])
def test_explicit_installer_barrier_skip_claims_no_deletion(tmp_path, kind):
    checked = verify(kind, tmp_path / 'data', {'skipped': True, 'deferred': True, 'installer_started': False})
    assert checked['counts']['not_applicable'] == 1 and not checked['counts']['confirmed']
