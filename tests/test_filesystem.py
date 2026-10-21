"""Atomic app-state writes across directory aliases; disposable fixtures only."""
from pathlib import Path
from unittest.mock import patch
import ctypes
import os
import threading
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
            error = PermissionError('fixture transient lock')
            error.winerror = 5
            raise error
        return original(path, target)
    with patch.object(Path, 'replace', replace), patch('optimus.filesystem.time.sleep'):
        replace_with_retry(temporary, destination)
    assert destination.read_text() == 'new' and len(calls) == 2


@pytest.mark.parametrize('winerror', [5, 32, 33])
def test_windows_sharing_retry_has_a_bounded_budget(tmp_path, winerror):
    temporary, destination = tmp_path / 'journal.tmp', tmp_path / 'journal.json'
    temporary.write_text('new'); destination.write_text('original')
    error = OSError('fixture persistent Windows conflict'); error.winerror = winerror
    with patch.object(Path, 'replace', side_effect=error) as replace, patch('optimus.filesystem.time.sleep') as sleep:
        with pytest.raises(OSError):
            replace_with_retry(temporary, destination)
    assert replace.call_count == 12 and sleep.call_count == 11
    assert sum(call.args[0] for call in sleep.call_args_list) == pytest.approx(3.25)
    assert destination.read_text() == 'original' and not temporary.exists()


@pytest.mark.parametrize('winerror', [2, 17, 87, 112])
def test_other_windows_errors_are_not_retried(tmp_path, winerror):
    temporary, destination = tmp_path / 'journal.tmp', tmp_path / 'journal.json'
    temporary.write_text('new'); destination.write_text('original')
    error = OSError('fixture nonretryable failure'); error.winerror = winerror
    with patch.object(Path, 'replace', side_effect=error) as replace, patch('optimus.filesystem.time.sleep') as sleep:
        with pytest.raises(OSError):
            replace_with_retry(temporary, destination)
    assert replace.call_count == 1 and not sleep.called
    assert destination.read_text() == 'original' and not temporary.exists()


def _native_reader(path, share):
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.CreateFileW.argtypes = [ctypes.c_wchar_p, ctypes.c_uint32, ctypes.c_uint32,
                                   ctypes.c_void_p, ctypes.c_uint32, ctypes.c_uint32, ctypes.c_void_p]
    kernel.CreateFileW.restype = ctypes.c_void_p
    kernel.CloseHandle.argtypes = [ctypes.c_void_p]
    kernel.CloseHandle.restype = ctypes.c_int
    handle = kernel.CreateFileW(str(path), 0x80000000, share, None, 3, 0x80, None)
    if handle == ctypes.c_void_p(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())
    return kernel, handle


@pytest.mark.skipif(os.name != 'nt', reason='Native Windows file-sharing behavior')
@pytest.mark.parametrize('share', [1, 7], ids=['read-share', 'read-write-delete-share'])
def test_native_reader_released_after_old_retry_budget_allows_atomic_save(tmp_path, share):
    temporary, destination = tmp_path / 'journal.tmp', tmp_path / 'journal.json'
    temporary.write_text('new'); destination.write_text('original')
    kernel, handle = _native_reader(destination, share)
    release = threading.Event()
    def close_reader():
        release.wait(0.8)
        kernel.CloseHandle(handle)
    thread = threading.Thread(target=close_reader)
    thread.start()
    try:
        replace_with_retry(temporary, destination)
    finally:
        release.set(); thread.join(timeout=5)
    assert not thread.is_alive()
    assert destination.read_text() == 'new' and not temporary.exists()


@pytest.mark.skipif(os.name != 'nt', reason='Native Windows file-sharing behavior')
def test_permanently_open_native_reader_preserves_the_old_journal(tmp_path):
    temporary, destination = tmp_path / 'journal.tmp', tmp_path / 'journal.json'
    temporary.write_text('new'); destination.write_text('original')
    kernel, handle = _native_reader(destination, 1)
    try:
        with pytest.raises(PermissionError) as outcome:
            replace_with_retry(temporary, destination)
        assert outcome.value.winerror in (5, 32, 33)
        assert destination.read_text() == 'original' and not temporary.exists()
    finally:
        kernel.CloseHandle(handle)


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
