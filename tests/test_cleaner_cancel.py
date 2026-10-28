"""Skip cleanup at file boundaries; preserve actual facts and remaining bytes."""
import json
import threading

import pytest

from optimus.cleaner import Category, Cleaner


@pytest.fixture
def cache(tmp_path, monkeypatch):
    folder = tmp_path / 'cache'
    folder.mkdir()
    for index in range(5):
        (folder / f'{index}.bin').write_bytes(b'fixture')
    cleaner = Cleaner(tmp_path / 'data', [Category('fixture', 'Fixture', folder, 'Disposable only')])
    monkeypatch.setattr(cleaner, '_running', lambda _: [])
    monkeypatch.setattr(cleaner, '_open_files_snapshot', lambda: (set(), True))
    return cleaner, folder


def test_cancelled_scan_cannot_be_applied_and_never_claims_complete(cache):
    cleaner, folder = cache
    event = threading.Event()
    ticks = []
    def progress(value):
        ticks.append(value)
        event.set()
    plan = cleaner.scan(['fixture'], 0, progress_callback=progress, cancel_event=event)
    assert plan['cancelled'] and not plan['complete']
    assert all(tick['item_progress']['percent'] is None for tick in ticks)
    assert len(list(folder.iterdir())) == 5
    with pytest.raises(ValueError, match='expired'):
        cleaner.apply(plan)


def test_skip_after_one_file_preserves_rest_and_saves_partial_counts(cache, monkeypatch):
    cleaner, folder = cache
    plan = cleaner.scan(['fixture'], 0)
    event = threading.Event()
    ticks = []
    delete = cleaner._delete_exact
    def delete_one(record):
        delete(record)
        event.set()
    monkeypatch.setattr(cleaner, '_delete_exact', delete_one)
    def progress(value):
        ticks.append(value)
    result = cleaner.apply(plan, progress_callback=progress, cancel_event=event)
    assert result['cancelled'] and result['deleted_count'] == result['processed_count'] == 1
    assert result['remaining_count'] == result['skipped_count'] == 4
    assert result['deleted_bytes'] == 7 and len(list(folder.iterdir())) == 4
    assert ticks[-1]['item_progress']['percent'] is None
    saved = json.loads(open(result['log_path'], encoding='utf-8').read())
    assert saved['state'] == 'cancelled' and saved['result'] == result
    with pytest.raises(ValueError, match='expired'):
        cleaner.apply(plan)


def test_request_during_final_safety_check_prevents_current_file_deletion(cache, monkeypatch):
    cleaner, folder = cache
    plan = cleaner.scan(['fixture'], 0)
    event = threading.Event()
    def processes(_):
        event.set()
        return []
    monkeypatch.setattr(cleaner, '_running', processes)
    result = cleaner.apply(plan, cancel_event=event)
    assert result['cancelled'] and result['deleted_count'] == result['deleted_bytes'] == 0
    assert result['remaining_count'] == 5 and len(list(folder.iterdir())) == 5
