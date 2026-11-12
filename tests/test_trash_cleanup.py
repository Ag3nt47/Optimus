"""Recycle Bin/Trash emptying with fake shell APIs and disposable Trash folders only."""
import ctypes
import os
import threading

import pytest

from optimus import trash_cleanup
from optimus.verification import OutcomeVerification
from optimus.workflow import DEFAULTS, full_optimization_settings


class FakeShell:
    def __init__(self, before, after, empty_result=0):
        self.queries = [before, after]
        self.empty_result, self.empty_calls = empty_result, []

    def SHQueryRecycleBinW(self, root, pointer):
        items, size = self.queries.pop(0)
        info = ctypes.cast(pointer, ctypes.POINTER(trash_cleanup._QueryInfo)).contents
        assert root is None and info.cbSize == ctypes.sizeof(trash_cleanup._QueryInfo)
        info.i64NumItems, info.i64Size = items, size
        return 0

    def SHEmptyRecycleBinW(self, window, root, flags):
        self.empty_calls.append((window, root, flags))
        return self.empty_result


def test_windows_empties_every_bin_silently_and_reports_measured_totals():
    shell = FakeShell((3, 4096), (0, 0))
    result = trash_cleanup.empty_trash(platform='win32', shell=shell)
    assert result['emptied'] is True and result['items'] == 3 and result['bytes'] == 4096
    assert shell.empty_calls == [(None, None, 0x7)]


def test_windows_already_empty_bin_is_skipped_without_deletion():
    shell = FakeShell((0, 0), (0, 0))
    result = trash_cleanup.empty_trash(platform='win32', shell=shell)
    assert result['skipped'] is True and result['emptied'] is False
    assert shell.empty_calls == []


def test_windows_remaining_items_are_reported_not_claimed_as_emptied():
    shell = FakeShell((5, 500), (2, 200), empty_result=-2147024891)
    result = trash_cleanup.empty_trash(platform='win32', shell=shell)
    assert result['emptied'] is False and result['remaining_items'] == 2 and result['failed_items'] == 2
    assert result['items'] == 3 and '0x80070005' in result['error']


def test_cancel_before_start_deletes_nothing(tmp_path):
    event = threading.Event()
    event.set()
    (tmp_path / 'kept.txt').write_text('kept')
    result = trash_cleanup.empty_trash(platform='linux', trash=tmp_path, cancel_event=event)
    assert result['cancelled'] is True and (tmp_path / 'kept.txt').exists()


@pytest.fixture
def owned(monkeypatch):
    monkeypatch.setattr(trash_cleanup.os, 'getuid', lambda: os.lstat(os.getcwd()).st_uid, raising=False)


def test_linux_trash_contents_are_removed_without_following_links(tmp_path, owned):
    outside = tmp_path / 'outside.txt'
    outside.write_text('must survive')
    trash = tmp_path / 'Trash'
    (trash / 'files' / 'folder').mkdir(parents=True)
    (trash / 'info').mkdir()
    (trash / 'files' / 'one.txt').write_bytes(b'12345')
    (trash / 'files' / 'folder' / 'two.bin').write_bytes(b'123')
    (trash / 'info' / 'one.txt.trashinfo').write_text('[Trash Info]')
    try:
        os.symlink(outside, trash / 'files' / 'link.txt')
        linked = True
    except (OSError, NotImplementedError):
        linked = False
    result = trash_cleanup.empty_trash(platform='linux', trash=trash)
    assert result['emptied'] is True and result['items'] == 2 + linked
    assert result['bytes'] >= 8
    assert list((trash / 'files').iterdir()) == [] and list((trash / 'info').iterdir()) == []
    assert outside.read_text() == 'must survive'


def test_macos_layout_treats_a_folder_named_files_as_an_item(tmp_path, owned):
    trash = tmp_path / '.Trash'
    (trash / 'files').mkdir(parents=True)
    (trash / 'files' / 'inner.txt').write_text('x')
    (trash / 'photo.jpg').write_bytes(b'jpg')
    result = trash_cleanup.empty_trash(platform='darwin', trash=trash)
    assert result['emptied'] is True and result['items'] == 2 and list(trash.iterdir()) == []


def test_missing_or_empty_trash_is_skipped(tmp_path, owned):
    assert trash_cleanup.empty_trash(platform='linux', trash=tmp_path / 'absent')['skipped'] is True
    (tmp_path / 'Trash').mkdir()
    assert trash_cleanup.empty_trash(platform='linux', trash=tmp_path / 'Trash')['skipped'] is True


def test_trash_not_owned_by_this_account_is_left_unchanged(tmp_path, monkeypatch):
    monkeypatch.setattr(trash_cleanup.os, 'getuid', lambda: -1, raising=False)
    (tmp_path / 'kept.txt').write_text('kept')
    result = trash_cleanup.empty_trash(platform='linux', trash=tmp_path)
    assert result['emptied'] is False and result['failed_items'] == 1 and (tmp_path / 'kept.txt').exists()


def test_trash_locations_follow_platform_conventions(tmp_path):
    assert trash_cleanup.trash_directory('darwin', tmp_path, {}) == tmp_path / '.Trash'
    assert trash_cleanup.trash_directory('linux', tmp_path, {}) == tmp_path / '.local/share/Trash'
    data = tmp_path / 'xdg'
    assert trash_cleanup.trash_directory('linux', tmp_path, {'XDG_DATA_HOME': str(data)}) == data / 'Trash'
    assert trash_cleanup.trash_directory('linux', tmp_path, {'XDG_DATA_HOME': 'relative'}) == tmp_path / '.local/share/Trash'


def test_setting_is_off_by_default_and_on_in_full_optimization():
    assert DEFAULTS['recycle_bin_empty_enabled'] is False
    assert full_optimization_settings({})['recycle_bin_empty_enabled'] is True


def test_native_full_settings_keep_trash_emptying():
    from optimus.native_settings import full_native_settings
    assert full_native_settings()['recycle_bin_empty_enabled'] is True


def verify(tmp_path, title, value, status='completed'):
    report = {'id': 'a' * 32, 'settings': {}, 'stages': [
        {'title': title, 'result': value, 'status': status, 'started_at': 100, 'finished_at': 110}]}
    return OutcomeVerification(tmp_path, native_reader=lambda *args, **kwargs: {}).verify(report)


@pytest.mark.parametrize('title', ['Empty the Recycle Bin', 'Empty the Trash'])
def test_verification_uses_the_stage_readback(tmp_path, title):
    assert verify(tmp_path, title, {'emptied': True, 'items': 3, 'bytes': 10})['status'] == 'confirmed'
    assert verify(tmp_path, title, {'emptied': False, 'failed_items': 2, 'error': 'locked'})['status'] == 'needs_review'
    assert verify(tmp_path, title, {'skipped': True, 'emptied': False})['status'] == 'not_applicable'
