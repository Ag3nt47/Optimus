"""Recognized Opera layouts use disposable profiles and mocked process checks."""
from contextlib import closing
import hashlib
from pathlib import Path
import sqlite3
from unittest.mock import Mock

import pytest

from optimus.browser_paths import browser_definitions, native_locations, process_matches
from optimus import browser_privacy, cleaner as cleaner_module
from optimus.browser_cleanup import BROWSERS, _PROFILE_BROWSER
from test_browser_privacy import chrome_fixture
from test_browser_cleanup import service, profile, cache, configuration, cleaner, opened, closed
from optimus.browser_cleanup import run_browser_cleanup


@pytest.mark.parametrize('system', ['win32', 'darwin', 'linux'])
def test_recognized_native_opera_id_process_and_locations(system, tmp_path, monkeypatch):
    monkeypatch.setenv('LOCALAPPDATA', str(tmp_path / 'local'))
    monkeypatch.setenv('APPDATA', str(tmp_path / 'roaming'))
    monkeypatch.setenv('XDG_CONFIG_HOME', str(tmp_path / 'config'))
    monkeypatch.setenv('XDG_CACHE_HOME', str(tmp_path / 'cache'))
    definition = browser_definitions(system)['opera']
    assert process_matches('opera', definition['process'], system)
    assert not process_matches('opera', 'not-opera.exe', system)
    locations = [row for row in native_locations(system, tmp_path) if row['id'] == 'opera']
    assert len(locations) == 1 and locations[0]['legacy_direct_profile']
    assert locations[0]['profile_root'] != locations[0]['cache_root']
    assert BROWSERS['opera']['name'] == 'Opera' and _PROFILE_BROWSER['Opera'] == 'opera'


@pytest.mark.parametrize('layout', ['legacy', 'default'])
def test_windows_opera_privacy_keeps_domains_passwords_bookmarks_and_restores(tmp_path, monkeypatch, layout):
    local, roaming = tmp_path / 'local', tmp_path / 'roaming'
    monkeypatch.setenv('LOCALAPPDATA', str(local))
    monkeypatch.setenv('APPDATA', str(roaming))
    monkeypatch.setattr(browser_privacy, '_PLATFORM', 'win32')
    base = roaming / 'Opera Software/Opera Stable'
    root = base if layout == 'legacy' else base / 'Default'
    chrome_fixture(root)
    tool = browser_privacy.BrowserPrivacy(tmp_path / 'data')
    monkeypatch.setattr(tool, '_closed', Mock())
    profiles = tool.profiles()
    assert len(profiles) == 1 and profiles[0]['browser'] == 'Opera'
    protected = {name: hashlib.sha256((root / name).read_bytes()).hexdigest() for name in ('Bookmarks', 'Login Data')}
    plan = tool.scan(profiles[0]['id'], categories=['history', 'cookies'], keep_domains=['example.com'])
    outcome = tool.apply(dict(planId=plan['planId'], selectedCategories=['history', 'cookies'], confirmed=True))
    assert outcome['state'] == 'applied'
    with closing(sqlite3.connect(root / 'Network/Cookies')) as db:
        assert db.execute('select count(*) from cookies').fetchone()[0] == 3
    tool.restore_backup(outcome['backupId'], confirmed=True)
    with closing(sqlite3.connect(root / 'Network/Cookies')) as db:
        assert db.execute('select count(*) from cookies').fetchone()[0] == 6
    assert all(hashlib.sha256((root / name).read_bytes()).hexdigest() == digest for name, digest in protected.items())


@pytest.mark.parametrize('system', ['darwin', 'linux'])
@pytest.mark.parametrize('layout', ['legacy', 'default'])
def test_native_opera_profiles_and_cache_categories_are_narrow(tmp_path, monkeypatch, system, layout):
    monkeypatch.setattr(Path, 'home', classmethod(lambda cls: tmp_path))
    monkeypatch.setenv('XDG_CONFIG_HOME', str(tmp_path / 'config'))
    monkeypatch.setenv('XDG_CACHE_HOME', str(tmp_path / 'cache'))
    monkeypatch.setattr(browser_privacy, '_PLATFORM', system)
    monkeypatch.setattr(cleaner_module, '_PLATFORM', system)
    location = next(row for row in native_locations(system, tmp_path) if row['id'] == 'opera')
    root = location['profile_root'] if layout == 'legacy' else location['profile_root'] / 'Default'
    root.mkdir(parents=True)
    (root / 'History').write_bytes(b'fixture profile marker')
    cache_root = location['cache_root'] if layout == 'legacy' else location['cache_root'] / 'Default'
    (cache_root / 'Cache').mkdir(parents=True)
    profiles = browser_privacy.BrowserPrivacy(tmp_path / 'data').profiles()
    opera = [row for row in profiles if row['browser'] == 'Opera']
    assert len(opera) == 1 and Path(opera[0]['path']) == root
    categories = cleaner_module.Cleaner(tmp_path / 'data')._definitions()
    matched = [row for row in categories if row.id.startswith('opera:') and row.path == cache_root / 'Cache']
    assert len(matched) == 1
    assert all(row.path.name in ('Cache', 'Code Cache', 'GPUCache') for row in categories if row.id.startswith('opera:'))


def test_windows_opera_cache_preview_does_not_include_profile_databases(tmp_path, monkeypatch):
    local, roaming = tmp_path / 'local', tmp_path / 'roaming'
    monkeypatch.setenv('LOCALAPPDATA', str(local))
    monkeypatch.setenv('APPDATA', str(roaming))
    monkeypatch.setattr(cleaner_module, '_PLATFORM', 'win32')
    cache_path = local / 'Opera Software/Opera Stable/Default/Cache'
    cache_path.mkdir(parents=True)
    (cache_path / 'fixture.tmp').write_bytes(b'cache')
    profile_path = roaming / 'Opera Software/Opera Stable/Default'
    profile_path.mkdir(parents=True)
    (profile_path / 'History').write_bytes(b'preserve history')
    tool = cleaner_module.Cleaner(tmp_path / 'data')
    monkeypatch.setattr(tool, '_running', lambda _: [])
    category = next(row for row in tool._definitions() if row.path == cache_path)
    plan = tool.scan([category.id], 0)
    assert [Path(row['path']).name for row in plan['files']] == ['fixture.tmp']
    assert (profile_path / 'History').read_bytes() == b'preserve history'


def test_opera_uses_existing_close_or_skip_and_fresh_privacy_paths():
    backend = service(profiles=[profile('opera')], caches=[cache('opera')])
    settings = configuration(browser_cleanup_browsers=['opera'])
    backend.browser_cleanup_status.return_value = opened('opera')
    clean = cleaner()
    skipped = run_browser_cleanup(backend, settings, clean=clean, browser_prompt=lambda _: False)
    assert skipped['items'][0]['status'] == 'skipped'
    backend.privacy_apply.assert_not_called()
    backend.browser_cleanup_close.assert_not_called()
    backend.browser_cleanup_status.return_value = closed()
    result = run_browser_cleanup(backend, settings, clean=clean)
    assert result['items'][0]['profiles'][0]['status'] == 'applied'
    backend.privacy_scan.assert_called_once()


def test_privacy_discard_releases_only_its_exact_preview(tmp_path):
    tool = browser_privacy.BrowserPrivacy(tmp_path / 'data')
    tool._plans = {'a' * 32: {'fixture': 'own'}, 'b' * 32: {'fixture': 'manual'}}
    assert tool.discard_plan('a' * 32)
    assert tool._plans == {'b' * 32: {'fixture': 'manual'}}
    assert not tool.discard_plan('a' * 32)
    with pytest.raises(ValueError):
        tool.discard_plan('../forged')
    assert not (tmp_path / 'data').exists()
