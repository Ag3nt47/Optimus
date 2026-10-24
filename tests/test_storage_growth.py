"""Growth is measured only from complete metadata snapshots of fixture roots."""
import json
from pathlib import Path
import threading
import time

import pytest

from optimus import storage_growth as growth


def service(tmp_path, *, clock=None):
    return growth.StorageGrowthTracker(tmp_path / 'data', root_validator=lambda value: growth._ordinary_root(Path(value)),
        recipe_provider=lambda: [], clock=clock or time.time)


def test_complete_comparable_snapshots_report_actual_growth_and_metadata_only(tmp_path):
    root = tmp_path / 'selected'
    (root / 'logs').mkdir(parents=True)
    path = root / 'logs/application.log'
    path.write_bytes(b'123')
    stamps = iter([1000, 2000])
    tracker = service(tmp_path, clock=lambda: next(stamps))
    config = dict(growth.DEFAULTS, enabled=True, roots=[str(root)])
    first = tracker.scan(config)['roots'][0]
    assert first['bytes'] == 3 and first['growth_bytes'] is None
    path.write_bytes(b'123456789')
    second = tracker.scan(config)['roots'][0]
    assert second['bytes'] == 9 and second['growth_bytes'] == 6
    assert second['interval_seconds'] == 1000 and len(second['history']) == 2
    assert second['owner'] is None
    assert path.read_bytes() == b'123456789'


def test_partial_scan_never_overwrites_complete_baseline_or_claims_growth(tmp_path):
    root = tmp_path / 'selected'
    root.mkdir()
    for index in range(5):
        (root / str(index)).write_bytes(b'123')
    tracker = service(tmp_path)
    config = dict(growth.DEFAULTS, enabled=True, roots=[str(root)])
    tracker.scan(config)
    baseline = next((tmp_path / 'data/storage-growth').iterdir())
    previous = baseline.read_bytes()
    result = tracker.scan(dict(config, max_entries=1))
    assert not result['complete'] and result['roots'][0]['growth_bytes'] is None
    assert baseline.read_bytes() == previous


def test_depth_limit_is_partial_and_no_growth(tmp_path):
    root = tmp_path / 'selected'
    (root / 'deep/deeper').mkdir(parents=True)
    (root / 'deep/deeper/data').write_text('keep')
    result = service(tmp_path).scan(dict(growth.DEFAULTS, enabled=True, roots=[str(root)], max_depth=1))
    assert not result['complete'] and result['roots'][0]['growth_bytes'] is None


def test_hardlinks_not_double_counted_and_mark_partial(tmp_path):
    import os
    root = tmp_path / 'selected'
    root.mkdir()
    (root / 'a').write_text('keep')
    os.link(root / 'a', root / 'b')
    result = service(tmp_path).scan(dict(growth.DEFAULTS, enabled=True, roots=[str(root)]))
    assert not result['complete'] and result['scanned_bytes'] == 0


def test_invalid_or_indirect_history_does_not_get_replaced(tmp_path):
    root = tmp_path / 'selected'
    root.mkdir()
    (root / 'a').write_text('keep')
    tracker = service(tmp_path)
    config = dict(growth.DEFAULTS, enabled=True, roots=[str(root)])
    tracker.scan(config)
    baseline = next((tmp_path / 'data/storage-growth').iterdir())
    baseline.write_text('{invalid')
    result = tracker.scan(config)
    assert not result['complete'] and baseline.read_text() == '{invalid'


def test_cancelled_scan_no_baseline_or_data_mutation(tmp_path):
    root = tmp_path / 'selected'
    root.mkdir()
    event = threading.Event()
    event.set()
    result = service(tmp_path).scan(dict(growth.DEFAULTS, enabled=True, roots=[str(root)]), cancel_event=event)
    assert result['cancelled'] and not (tmp_path / 'data').exists()


def test_fixed_app_cache_mapping_is_explicit_evidence(tmp_path):
    from optimus.application_caches import CacheRecipe
    root = tmp_path / 'Cache'
    root.mkdir()
    tracker = service(tmp_path)
    tracker.recipe_provider = lambda: [CacheRecipe('fixed:fixture', 'discord', root, ('fixture',))]
    item = tracker.scan(dict(growth.DEFAULTS, enabled=True, roots=[str(root)]))['roots'][0]
    assert item['owner'] == 'discord' and 'fixed:fixture' in item['owner_evidence']


@pytest.mark.parametrize('update', [{'roots': ['/']}, {'roots': ['relative']}, {'roots': ['//server/share']},
    {'roots': ['C:/folder/../private']}, {'max_depth': True}, {'max_seconds': 0}, {'max_entries': 100001}, {'enabled': 1}])
def test_bad_settings_rejected(update):
    with pytest.raises(ValueError):
        growth.validate_configuration(update)


def test_default_off_does_not_touch_filesystem(tmp_path):
    assert service(tmp_path).scan({})['disabled'] and not (tmp_path / 'data').exists()


def test_logical_byte_budget_is_partial_and_never_claims_reclaimable_bytes(tmp_path):
    root = tmp_path / 'selected'
    root.mkdir()
    (root / 'large.bin').write_bytes(b'123456789')
    result = service(tmp_path).scan(dict(growth.DEFAULTS, enabled=True, roots=[str(root)], max_bytes=3))
    assert not result['complete'] and result['scanned_bytes'] == 0
    assert result['roots'][0]['growth_bytes'] is None


def test_storage_never_reads_file_contents(tmp_path, monkeypatch):
    root = tmp_path / 'selected'
    root.mkdir()
    path = root / 'private.txt'
    path.write_text('private')
    previous = Path.read_bytes
    def guarded_read(self, *args, **kwargs):
        if self == path:
            raise AssertionError('Storage scans must not read selected file contents.')
        return previous(self, *args, **kwargs)
    monkeypatch.setattr(Path, 'read_bytes', guarded_read)
    assert service(tmp_path).scan(dict(growth.DEFAULTS, enabled=True, roots=[str(root)]))['scanned_bytes'] == len('private')
