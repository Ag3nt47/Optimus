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
