from __future__ import annotations
import copy
import os
import time
from pathlib import Path
from unittest.mock import patch
import pytest
from optimus.cleaner import Category, Cleaner

def engine(tmp_path):
    root = tmp_path / 'cache'
    root.mkdir()
    data = tmp_path / 'data'
    cleaner = Cleaner(data, [Category('test-cache', 'Test cache', root, 'Disposable fixture cache.')])
    return cleaner, root

def old_plan(cleaner):
    future = time.time() + 10 * 86400
    with patch('optimus.cleaner.time.time', return_value=future):
        return cleaner.scan(['test-cache'], 7)

def test_preview_is_read_only(tmp_path):
    cleaner, root = engine(tmp_path)
    target = root / 'old.tmp'
    target.write_bytes(b'fixture')
    plan = old_plan(cleaner)
    assert plan['total_bytes'] == 7
    assert target.read_bytes() == b'fixture'
    assert len(plan['files']) == 1

def test_fresh_files_excluded(tmp_path):
    cleaner, root = engine(tmp_path)
    (root / 'fresh.tmp').write_text('fixture')
    assert cleaner.scan(['test-cache'], 7)['files'] == []

def test_unknown_category_rejected(tmp_path):
    cleaner, _ = engine(tmp_path)
    with pytest.raises(ValueError):
        cleaner.scan(['outside'])

def test_preview_is_bounded(tmp_path):
    cleaner, root = engine(tmp_path)
    for index in range(4):
        (root / f'{index}.tmp').write_text('fixture')
    with patch('optimus.cleaner.time.time', return_value=time.time() + 10 * 86400):
        plan = cleaner.scan(['test-cache'], 7, max_files=2)
    assert not plan['complete']
    assert len(plan['files']) == 2

@pytest.mark.skipif(os.name != 'nt', reason='Windows deletion is intentional and explicit.')
def test_delete_only_approved_fixture(tmp_path):
    cleaner, root = engine(tmp_path)
    target = root / 'old.tmp'
    target.write_bytes(b'disposable-fixture')
    plan = old_plan(cleaner)
    future = plan['created_at']
    with patch('optimus.cleaner.time.time', return_value=future):
        result = cleaner.apply(plan)
    assert result['deleted_count'] == 1, result
    assert not target.exists()
    assert Path(result['log_path']).is_file()

@pytest.mark.skipif(os.name != 'nt', reason='Windows deletion is intentional and explicit.')
def test_changed_file_preserved(tmp_path):
    cleaner, root = engine(tmp_path)
    target = root / 'old.tmp'
    target.write_text('before')
    plan = old_plan(cleaner)
    target.write_text('after-modified')
    with patch('optimus.cleaner.time.time', return_value=plan['created_at']):
        result = cleaner.apply(plan)
    assert result['deleted_count'] == 0
    assert target.read_text() == 'after-modified'

def test_unapproved_plan_rejected(tmp_path):
    cleaner, _ = engine(tmp_path)
    with pytest.raises(ValueError):
        cleaner.apply({'id': 'not-a-plan'})

@pytest.mark.skipif(os.name != 'nt', reason='Windows deletion is intentional and explicit.')
def test_caller_cannot_redirect_approved_file(tmp_path):
    cleaner, root = engine(tmp_path)
    target = root / 'old.tmp'
    outside = tmp_path / 'personal-document.txt'
    target.write_text('fixture')
    outside.write_text('keep')
    plan = old_plan(cleaner)
    plan['files'][0]['path'] = str(outside)
    with patch('optimus.cleaner.time.time', return_value=plan['created_at']):
        result = cleaner.apply(plan)
    assert outside.read_text() == 'keep'
    assert result['deleted_count'] == 1
    assert not target.exists()

def test_browser_start_after_preview_blocks_cleanup(tmp_path):
    cleaner, root = engine(tmp_path)
    cleaner._custom_categories = [Category('test-cache', 'Test cache', root, 'Fixture', ('test-browser.exe',))]
    target = root / 'old.tmp'
    target.write_text('fixture')
    with patch.object(cleaner, '_running', return_value=[]):
        plan = old_plan(cleaner)
    with patch.object(cleaner, '_running', return_value=['test-browser.exe']), patch('optimus.cleaner.time.time', return_value=plan['created_at']):
        result = cleaner.apply(plan)
    assert result['deleted_count'] == 0
    assert target.exists()

@pytest.mark.skipif(os.name != 'nt', reason='Windows hardlink identity')
def test_hardlinked_file_excluded(tmp_path):
    cleaner, root = engine(tmp_path)
    target = root / 'first.tmp'
    target.write_text('shared')
    os.link(target, root / 'second.tmp')
    assert old_plan(cleaner)['files'] == []


def test_zero_age_preview_includes_newly_created_eligible_cache_files(tmp_path):
    cleaner,root=engine(tmp_path)
    target=root/'new-cache.tmp';target.write_bytes(b'owned-disposable-fixture')
    assert cleaner.scan(['test-cache'],7)['files']==[]
    plan=cleaner.scan(['test-cache'],0)
    assert plan['min_age_days']==0
    assert [item['path'] for item in plan['files']]==[str(target)]
    assert target.read_bytes()==b'owned-disposable-fixture'


@pytest.mark.parametrize('invalid',[-1,3651,True,False,0.5,'0',None])
def test_cleanup_age_requires_a_bounded_whole_day_count(tmp_path,invalid):
    cleaner,_=engine(tmp_path)
    with pytest.raises(ValueError):
        cleaner.scan(['test-cache'],invalid)


@pytest.mark.skipif(os.name!='nt',reason='Windows exclusive locks and guarded deletion.')
def test_zero_age_cleanup_preserves_locked_readonly_hardlinked_and_outside_files(tmp_path):
    import ctypes
    from ctypes import wintypes
    cleaner,root=engine(tmp_path)
    eligible=root/'eligible.tmp';eligible.write_text('owned-disposable-fixture')
    locked=root/'locked.tmp';locked.write_text('locked-fixture')
    readonly=root/'readonly.tmp';readonly.write_text('read-only-fixture')
    outside=tmp_path/'outside-personal-fixture.txt';outside.write_text('must remain')
    os.link(outside,root/'hardlinked.tmp')
    kernel=ctypes.WinDLL('kernel32',use_last_error=True)
    kernel.SetFileAttributesW.argtypes=[wintypes.LPCWSTR,wintypes.DWORD]
    kernel.SetFileAttributesW.restype=wintypes.BOOL
    kernel.CreateFileW.argtypes=[wintypes.LPCWSTR,wintypes.DWORD,wintypes.DWORD,ctypes.c_void_p,wintypes.DWORD,wintypes.DWORD,wintypes.HANDLE]
    kernel.CreateFileW.restype=wintypes.HANDLE
    kernel.CloseHandle.argtypes=[wintypes.HANDLE]
    assert kernel.SetFileAttributesW(str(readonly),1)
    handle=None
    try:
        plan=cleaner.scan(['test-cache'],0)
        assert {Path(item['path']).name for item in plan['files']}=={'eligible.tmp','locked.tmp'}
        # Open after the preview to exercise the apply-time exclusive-open guard.
        handle=kernel.CreateFileW(str(locked),0x80000000,0,None,3,0x80,None)
        assert handle!=ctypes.c_void_p(-1).value,ctypes.get_last_error()
        result=cleaner.apply(plan)
        assert result['deleted_count']==1,result
        assert result['skipped_count']==1
        assert not eligible.exists()
        assert locked.exists() and readonly.exists()
        assert outside.read_text()=='must remain'
        assert (root/'hardlinked.tmp').exists()
    finally:
        if handle is not None and handle!=ctypes.c_void_p(-1).value:
            kernel.CloseHandle(handle)
        kernel.SetFileAttributesW(str(readonly),0x80)


def test_zero_age_apply_still_uses_private_plan_instead_of_caller_paths(tmp_path):
    cleaner,root=engine(tmp_path)
    target=root/'approved.tmp';target.write_text('owned-disposable-fixture')
    outside=tmp_path/'outside.txt';outside.write_text('must remain')
    plan=cleaner.scan(['test-cache'],0)
    plan['files'][0]['path']=str(outside)
    deleted=[]
    with patch.object(cleaner,'_delete_exact',side_effect=lambda record:deleted.append(record['path'])):
        result=cleaner.apply(plan)
    assert deleted==[str(target)]
    assert result['deleted_count']==1
    assert outside.read_text()=='must remain'


def test_zero_age_disables_age_cutoff_even_when_file_clock_leads_process_clock(tmp_path):
    cleaner,root=engine(tmp_path)
    target=root/'clock-leading.tmp';target.write_text('owned-disposable-fixture')
    ahead=time.time()+2
    os.utime(target,(ahead,ahead))
    assert cleaner.scan(['test-cache'],1)['files']==[]
    assert [row['path'] for row in cleaner.scan(['test-cache'],0)['files']]==[str(target)]
