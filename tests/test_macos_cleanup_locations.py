"""macOS-only cleanup locations: on by default, added once, and safely scanned."""
from __future__ import annotations

import json
import os
from pathlib import Path
import sys

import pytest

from optimus import browser_privacy, cleaner, workflow


def _mac_home(tmp_path, monkeypatch):
    home = tmp_path / 'Home'
    home.mkdir()
    monkeypatch.setattr(Path, 'home', classmethod(lambda cls: home))
    monkeypatch.setattr(cleaner, '_PLATFORM', 'darwin')
    monkeypatch.setattr(browser_privacy, '_PLATFORM', 'darwin')
    monkeypatch.delenv('TMPDIR', raising=False)
    return home


def test_every_default_macos_id_is_a_real_category(tmp_path, monkeypatch):
    _mac_home(tmp_path, monkeypatch)
    ids = {item['id'] for item in cleaner.Cleaner(tmp_path / 'data').categories()}
    assert set(workflow.MACOS_CLEANUP_CATEGORY_IDS) <= ids
    for identity in workflow.MACOS_CLEANUP_CATEGORY_IDS:
        assert workflow._CATEGORY_ID.fullmatch(identity)


def test_macos_locations_stay_inside_the_users_library(tmp_path, monkeypatch):
    home = _mac_home(tmp_path, monkeypatch)
    for item in cleaner.Cleaner(tmp_path / 'data').categories():
        if item['id'] in workflow.MACOS_CLEANUP_CATEGORY_IDS:
            path = Path(item['path'])
            assert path.is_relative_to(home / 'Library') and path != home / 'Library'
            assert item['process_names'] == [] and item['requires_admin'] is False


def test_macos_locations_are_not_offered_on_linux(tmp_path, monkeypatch):
    home = _mac_home(tmp_path, monkeypatch)
    monkeypatch.setattr(cleaner, '_PLATFORM', 'linux')
    monkeypatch.setenv('XDG_CACHE_HOME', str(home / '.cache'))
    ids = {item['id'] for item in cleaner.Cleaner(tmp_path / 'data').categories()}
    assert not ids & set(workflow.MACOS_CLEANUP_CATEGORY_IDS)


@pytest.mark.skipif(os.name == 'nt', reason='POSIX ownership and open-file checks')
def test_scan_keeps_todays_logs_and_only_ipsw_files(tmp_path, monkeypatch):
    home = _mac_home(tmp_path, monkeypatch)
    reports = home / 'Library/Logs/DiagnosticReports'
    reports.mkdir(parents=True)
    (reports / 'today.ips').write_bytes(b'y' * 10)
    updates = home / 'Library/iTunes/iPhone Software Updates'
    updates.mkdir(parents=True)
    (updates / 'iPhone.ipsw').write_bytes(b'z' * 10)
    (updates / 'notes.txt').write_bytes(b'keep')
    derived = home / 'Library/Developer/Xcode/DerivedData/App-abc/Build'
    derived.mkdir(parents=True)
    (derived / 'object.o').write_bytes(b'o' * 10)
    engine = cleaner.Cleaner(tmp_path / 'data')
    monkeypatch.setattr(engine, '_open_files_snapshot', lambda: (set(), True))
    plan = engine.scan(['mac-diagnostic-reports', 'mac-user-logs', 'ios-update-files', 'xcode-derived-data'],
                       min_age_days=0)
    names = sorted(Path(item['path']).name for item in plan['files'])
    # Today's crash report is kept (1-day floor); other locations have no floor.
    # Older reports are eligible; age uses the later of mtime/ctime, which a test cannot backdate.
    assert names == ['iPhone.ipsw', 'object.o']
    floors = {row['id']: row['effective_min_age_days'] for row in plan['categories']}
    assert floors['mac-diagnostic-reports'] == 1 and floors['mac-user-logs'] == 1
    assert floors['xcode-derived-data'] == 0


def test_full_preset_turns_macos_locations_on(monkeypatch):
    monkeypatch.setattr(sys, 'platform', 'darwin')
    value = workflow.full_optimization_settings()
    assert set(workflow.MACOS_CLEANUP_CATEGORY_IDS) <= set(value['cleanup_category_ids'])
    assert value['cleanup_enabled'] is True and value['macos_cleanup_locations_added'] is True


def test_full_preset_on_windows_is_unchanged(monkeypatch):
    monkeypatch.setattr(sys, 'platform', 'win32')
    value = workflow.full_optimization_settings()
    assert not set(workflow.MACOS_CLEANUP_CATEGORY_IDS) & set(value['cleanup_category_ids'])


def test_existing_mac_settings_get_locations_once_and_keep_a_later_untick(tmp_path, monkeypatch):
    monkeypatch.setattr(sys, 'platform', 'darwin')
    store = workflow.Preferences(tmp_path)
    saved = workflow.full_optimization_settings()
    saved.pop('macos_cleanup_locations_added')
    saved['cleanup_category_ids'] = ['user-temp']
    store.path.write_text(json.dumps(saved), encoding='utf-8')

    before = store.path.read_bytes()
    loaded = store.load()
    assert loaded['cleanup_category_ids'][0] == 'user-temp'
    assert set(workflow.MACOS_CLEANUP_CATEGORY_IDS) <= set(loaded['cleanup_category_ids'])
    assert loaded['macos_cleanup_locations_added'] is True
    assert store.path.read_bytes() == before  # loading never writes

    loaded['cleanup_category_ids'].remove('xcode-derived-data')
    store.save(loaded)
    assert 'xcode-derived-data' not in store.load()['cleanup_category_ids']


def test_migration_does_not_run_off_macos(tmp_path, monkeypatch):
    monkeypatch.setattr(sys, 'platform', 'linux')
    store = workflow.Preferences(tmp_path)
    saved = workflow.Preferences.safe_defaults()
    saved['cleanup_category_ids'] = ['user-temp']
    store.path.write_text(json.dumps(saved), encoding='utf-8')
    assert store.load()['cleanup_category_ids'] == ['user-temp']


def test_current_settings_round_trip_unchanged_on_macos(tmp_path, monkeypatch):
    monkeypatch.setattr(sys, 'platform', 'darwin')
    store = workflow.Preferences(tmp_path)
    value = workflow.Preferences.safe_defaults()
    value['cleanup_category_ids'] = ['user-temp']
    saved = store.save(value)
    assert store.load() == saved
