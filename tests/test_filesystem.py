"""Atomic app-state writes across directory aliases; disposable fixtures only."""
from pathlib import Path
from unittest.mock import patch
import pytest
from optimus.filesystem import replace_with_retry, canonical_file


def test_parent_aliases_are_canonicalized_for_both_operands(tmp_path):
    physical = tmp_path / 'physical'
    physical.mkdir()
    alias = tmp_path / 'alias'
    temporary = physical / 'report.json.tmp'
    temporary.write_text('new')
    (physical / 'report.json').write_text('old')
    original = Path.resolve
    def resolve(path, *args, **kwargs):
        return physical if path == alias else original(path, *args, **kwargs)
    with patch.object(Path, 'resolve', resolve):
        replace_with_retry(alias / temporary.name, alias / 'report.json')
    assert (physical / 'report.json').read_text() == 'new'
    assert not temporary.exists()


def test_final_filename_is_not_resolved(tmp_path):
    destination = tmp_path / 'report.json'
    original = Path.resolve
    calls = []
    def resolve(path, *args, **kwargs):
        calls.append(path)
        return original(path, *args, **kwargs)
    with patch.object(Path, 'resolve', resolve):
        assert canonical_file(destination) == destination
    assert calls == [tmp_path]


def test_cross_volume_error_has_no_nonatomic_copy_fallback(tmp_path):
    temporary, destination = tmp_path / 'journal.tmp', tmp_path / 'journal.json'
    temporary.write_text('new')
    destination.write_text('original')
    with patch.object(Path, 'replace', side_effect=OSError(17, 'fixture cross-volume')):
        with pytest.raises(OSError):
            replace_with_retry(temporary, destination)
    assert destination.read_text() == 'original'
    assert not temporary.exists()


def test_permission_retry_is_still_atomic(tmp_path):
    temporary, destination = tmp_path / 'journal.tmp', tmp_path / 'journal.json'
    temporary.write_text('new')
    original = Path.replace
    calls = []
    def replace(path, target):
        calls.append((path, target))
        if len(calls) == 1:
            raise PermissionError('fixture transient lock')
        return original(path, target)
    with patch.object(Path, 'replace', replace), patch('optimus.filesystem.time.sleep'):
        replace_with_retry(temporary, destination)
    assert destination.read_text() == 'new' and len(calls) == 2


def test_backend_uses_canonical_data_directory(tmp_path):
    from optimus.backend import Backend
    supplied = tmp_path / 'data'
    physical = tmp_path / 'physical'
    physical.mkdir()
    original = Path.resolve
    def resolve(path, *args, **kwargs):
        return physical if path == supplied else original(path, *args, **kwargs)
    with patch.object(Path, 'resolve', resolve):
        backend = Backend(supplied)
    try:
        assert backend.data_dir == physical
        assert backend.bridge.data_dir == physical / 'engine'
        assert backend.preference_store.path.parent == physical
        assert backend.cloud.root.parent == physical
    finally:
        backend.close()
