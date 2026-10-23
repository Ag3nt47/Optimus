"""Cleanup progress uses disposable selected cache files and no process scans."""
import copy
import itertools
import json
from pathlib import Path

import pytest

from optimus.cleaner import Category, Cleaner
from optimus.item_progress import normalize_item_progress


@pytest.fixture
def fixture(tmp_path, monkeypatch):
    root = tmp_path / 'selected-cache'
    root.mkdir()
    tool = Cleaner(tmp_path / 'data', [Category('fixture-cache', 'Fixture cache', root, 'Disposable cache')])
    monkeypatch.setattr(tool, '_running', lambda _: [])
    monkeypatch.setattr(tool, '_open_files_snapshot', lambda: (set(), True))
    return tool, root


def selected(tool, root, count=3):
    for i in range(count):
        (root / f'{i}.tmp').write_bytes(('fixture' + str(i)).encode())
    return tool.scan(['fixture-cache'], 0)


def test_discovery_is_unknown_until_bounded_scan_actually_finishes(fixture, monkeypatch):
    tool, root = fixture
    for i in range(130):
        (root / f'{i}.tmp').write_bytes(b'fixture')
    ticks = itertools.count()
    monkeypatch.setattr('optimus.cleaner.time.monotonic', lambda: next(ticks) / 10)
    events = []
    plan = tool.scan(['fixture-cache'], 0, max_seconds=120, progress_callback=events.append)
    assert plan['complete'] and len(plan['files']) == 130
    assert all(normalize_item_progress(e['item_progress']) is not None for e in events)
    assert all(e['item_progress']['phase'] == 'scan' for e in events)
    assert all(e['item_progress']['percent'] is None for e in events[:-1])
    assert events[-1]['item_progress']['percent'] == 100
    assert any('64 files checked' in e['description'] for e in events)
    assert all(path.exists() for path in root.iterdir())
    assert not (tool.data_dir / 'cleanup').exists()


def test_partial_discovery_never_claims_total_known_or_completed(fixture):
    tool, root = fixture
    for i in range(3):
        (root / f'{i}.tmp').write_bytes(b'fixture')
    events = []
    plan = tool.scan(['fixture-cache'], 0, max_files=1, progress_callback=events.append)
    assert not plan['complete'] and len(plan['files']) == 1
    assert events and all(e['item_progress']['percent'] is None for e in events)
    assert 'could not be fully checked' in events[-1]['description']


def test_file_checks_use_fixed_approved_denominator_and_receipt_precedes_100(fixture, monkeypatch):
    tool, root = fixture
    plan = selected(tool, root)
    ticks = itertools.count()
    monkeypatch.setattr('optimus.cleaner.time.monotonic', lambda: next(ticks))
    events, failures = [], []
    def receive(event):
        # Observe outside the exception-isolated callback contract; collect any
        # inspection error and assert it in the test after the operation.
        try:
            saved = json.loads((tool.data_dir / 'cleanup' / (plan['id'] + '.json')).read_text(encoding='utf-8'))
            if event['item_progress']['percent'] == 100:
                assert saved['state'] == 'completed'
                assert saved['result']['deleted_count'] == 3
            else:
                assert saved['state'] == 'prepared'
        except Exception as error:
            failures.append(error)
        events.append(copy.deepcopy(event))
    result = tool.apply(plan, progress_callback=receive)
    assert not failures
    assert result['deleted_count'] == 3 and not list(root.iterdir())
    assert [e['item_progress']['percent'] for e in events] == [0, 100 / 3, 200 / 3, 99, 100]
    assert all(e['item_progress']['key'] == 'cleanup:' + plan['id'] for e in events)
    assert '3/3 files checked; 3 removed, 0 preserved' in events[-1]['description']


def test_changed_file_is_counted_preserved_instead_of_removed(fixture):
    tool, root = fixture
    plan = selected(tool, root, 1)
    (root / '0.tmp').write_bytes(b'changed after preview')
    events = []
    result = tool.apply(plan, progress_callback=events.append)
    assert result['deleted_count'] == 0 and result['skipped_count'] == 1
    assert (root / '0.tmp').read_bytes() == b'changed after preview'
    assert events[-1]['item_progress']['percent'] == 100
    assert '0 removed, 1 preserved' in events[-1]['description']


def test_bad_callback_does_not_consume_cached_approval(fixture):
    tool, root = fixture
    plan = selected(tool, root, 1)
    with pytest.raises(ValueError, match='callback'):
        tool.apply(plan, progress_callback='invalid')
    assert (root / '0.tmp').exists()
    assert tool.apply(plan)['deleted_count'] == 1


def test_callback_exception_does_not_replay_or_abandon_file_operations(fixture, monkeypatch):
    tool, root = fixture
    exact = tool._delete_exact
    called = []
    def delete(record):
        called.append(record['path'])
        return exact(record)
    monkeypatch.setattr(tool, '_delete_exact', delete)
    def fail(_):
        raise RuntimeError('Observer unavailable')
    plan = tool.scan(['fixture-cache'], 0, progress_callback=fail)
    assert not plan['files']
    for i in range(2):
        (root / f'{i}.tmp').write_bytes(b'fixture')
    plan = tool.scan(['fixture-cache'], 0, progress_callback=fail)
    result = tool.apply(plan, progress_callback=fail)
    assert result['deleted_count'] == 2 and len(set(called)) == len(called) == 2


@pytest.mark.parametrize('when', ['prepared', 'completed'])
def test_failed_receipt_never_reports_100(fixture, monkeypatch, when):
    tool, root = fixture
    plan = selected(tool, root, 1)
    write = Path.write_text
    def guarded(path, text, *args, **kwargs):
        value = json.loads(text)
        if path.parent == tool.data_dir / 'cleanup' and value.get('state') == when:
            raise PermissionError('Fixture receipt refused')
        return write(path, text, *args, **kwargs)
    monkeypatch.setattr(Path, 'write_text', guarded)
    events = []
    with pytest.raises(PermissionError):
        tool.apply(plan, progress_callback=events.append)
    assert all(e['item_progress']['percent'] != 100 for e in events)
    assert (root / '0.tmp').exists() is (when == 'prepared')


def test_empty_cached_plan_has_defined_zero_then_completed_progress(fixture):
    tool, root = fixture
    plan = tool.scan(['fixture-cache'], 0)
    events = []
    result = tool.apply(plan, progress_callback=events.append)
    assert result['deleted_count'] == result['skipped_count'] == 0
    assert [e['item_progress']['percent'] for e in events] == [0, 100]
