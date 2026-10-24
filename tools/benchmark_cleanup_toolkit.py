"""Measure toolkit operations on disposable fixtures, never real application data."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import platform
import sys
import tempfile
import time
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))

from optimus.application_caches import ApplicationCacheCleaner, CacheRecipe, process_state, DEFAULTS as CACHE_DEFAULTS
from optimus.storage_growth import StorageGrowthTracker, DEFAULTS as GROWTH_DEFAULTS
from optimus.recovery_retention import RecoveryRetention, DEFAULTS as RETENTION_DEFAULTS


def benchmark(entries=1000, *, native_process_checks=False):
    if type(entries) is not int or not 1 <= entries <= 5000:
        raise ValueError('Choose between one and 5000 disposable fixture entries.')
    result = {'schema': 1, 'fixture_only': True, 'real_application_cleanup': False,
              'platform': platform.platform(), 'python': platform.python_version(), 'entries': entries,
              'native_process_checks': native_process_checks, 'measurements': {}}
    now = time.time() + 60 * 86400
    with tempfile.TemporaryDirectory(prefix='optimus-toolkit-benchmark-') as temporary:
        root = Path(temporary)
        cache, storage, data = root / 'FixtureApp/Cache', root / 'growth', root / 'data'
        cache.mkdir(parents=True)
        storage.mkdir()
        payload = b'x' * 4096
        for index in range(entries):
            (cache / f'f_{index:08x}').write_bytes(payload)
            (storage / f'fixture-{index}.bin').write_bytes(payload)
        recipes = [CacheRecipe('benchmark:fixture', 'discord', cache, ('not-a-real-application',))]
        started = time.perf_counter()
        cleaner = ApplicationCacheCleaner(data, recipe_provider=lambda: recipes,
            process_reader=process_state if native_process_checks else lambda _: {'complete': True, 'active': False},
            open_reader=lambda: (set(), True), clock=lambda: now)
        tracker = StorageGrowthTracker(data, clock=lambda: now, recipe_provider=lambda: [])
        retention = RecoveryRetention(data, clock=lambda: now)
        result['measurements']['constructors_seconds'] = time.perf_counter() - started
        config = dict(CACHE_DEFAULTS, enabled=True, apps=['discord'], max_seconds=60, max_entries=entries + 1)
        started = time.perf_counter()
        plan = cleaner.scan(config)
        result['measurements']['cache_preview_seconds'] = time.perf_counter() - started
        result['cache_preview'] = {'files': len(plan['files']), 'bytes': plan['total_bytes'], 'complete': plan['complete']}
        started = time.perf_counter()
        removed = cleaner.apply(plan, config)
        result['measurements']['cache_apply_seconds'] = time.perf_counter() - started
        result['cache_apply'] = {key: removed[key] for key in ('deleted_count', 'deleted_bytes', 'complete')}
        config = dict(GROWTH_DEFAULTS, enabled=True, roots=[str(storage)], max_seconds=60, max_entries=entries + 20)
        started = time.perf_counter()
        growth = tracker.scan(config)
        result['measurements']['storage_first_scan_seconds'] = time.perf_counter() - started
        (storage / 'added-fixture.bin').write_bytes(payload)
        tracker.clock = lambda: now + 86400
        started = time.perf_counter()
        second = tracker.scan(config)
        result['measurements']['storage_comparison_seconds'] = time.perf_counter() - started
        result['storage_growth'] = {'first_bytes': growth['scanned_bytes'], 'second_bytes': second['scanned_bytes'],
                                   'growth_bytes': second['roots'][0]['growth_bytes'], 'complete': second['complete']}
        backup = data / 'BrowserPrivacyBackups' / uuid.uuid4().hex
        backup.mkdir(parents=True)
        database = backup / 'database-0.sqlite'
        database.write_bytes(payload)
        manifest = {'backupId': backup.name, 'profileId': uuid.uuid4().hex, 'browser': 'Google Chrome',
            'profilePath': str(root / 'fixture-profile'), 'profileName': 'fixture', 'categories': ['cookies'], 'state': 'applied',
            'createdUtc': datetime.fromtimestamp(now - 45 * 86400, timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ'),
            'databases': [{'file': database.name, 'sha256': hashlib.sha256(payload).hexdigest(), 'committed': True}]}
        (backup / 'manifest.json').write_text(json.dumps(manifest))
        config = dict(RETENTION_DEFAULTS, enabled=True, browser_backups=True, lose_browser_undo_approved=True, max_seconds=60)
        started = time.perf_counter()
        expired = retention.run(config)
        result['measurements']['retention_verify_and_expire_seconds'] = time.perf_counter() - started
        result['recovery_retention'] = {key: expired[key] for key in ('expired_groups', 'deleted_count', 'deleted_bytes', 'complete')}
        result['passed'] = (result['cache_preview']['files'] == entries and removed['deleted_count'] == entries
                            and removed['deleted_bytes'] == entries * len(payload) and removed['complete']
                            and second['roots'][0]['growth_bytes'] == len(payload) and second['complete']
                            and expired['expired_groups'] == 1 and expired['complete'])
    result['temporary_fixture_removed'] = not Path(temporary).exists()
    return result


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--entries', type=int, default=1000)
    parser.add_argument('--output', type=Path)
    parser.add_argument('--native-process-checks', action='store_true', help='Include read-only real process census overhead; cleanup still targets disposable fixtures only.')
    arguments = parser.parse_args()
    report = benchmark(arguments.entries, native_process_checks=arguments.native_process_checks)
    rendered = json.dumps(report, indent=2, allow_nan=False)
    if arguments.output is not None:
        with arguments.output.open('x', encoding='utf-8') as stream:
            stream.write(rendered + '\n')
    print(rendered)
    raise SystemExit(0 if report['passed'] and report['temporary_fixture_removed'] else 2)
