"""Protected-process fallback never closes a real browser or touches real data."""
import sqlite3
from types import SimpleNamespace

import pytest

from optimus import browser_privacy as browser


PROFILE = {'browser': 'Google Chrome', 'browser_id': 'chrome', 'process': 'chrome.exe'}


@pytest.fixture
def native_fallback(monkeypatch):
    monkeypatch.setattr(browser, '_PLATFORM', 'win32')
    monkeypatch.setattr(browser.psutil, 'process_iter', lambda *args, **kwargs: [SimpleNamespace(info={'pid': 140, 'name': None})])


def test_protected_process_name_can_be_resolved_without_admin_or_browser_action(native_fallback, monkeypatch):
    monkeypatch.setattr(browser, '_windows_process_names', lambda: (['Registry', 'System'], True))
    assert browser.BrowserPrivacy._closed(PROFILE) is None


@pytest.mark.parametrize('names,complete', [(['Registry', 'chrome.exe'], True), (['Registry'], False), ([], True)])
def test_active_or_unknown_native_snapshot_preserves_browser(native_fallback, monkeypatch, names, complete):
    monkeypatch.setattr(browser, '_windows_process_names', lambda: (names, complete))
    with pytest.raises(RuntimeError):
        browser.BrowserPrivacy._closed(PROFILE)


def test_native_failure_preserves_browser_instead_of_downgrading_unknown(native_fallback, monkeypatch):
    def failed():
        raise OSError('fixture snapshot unavailable')
    monkeypatch.setattr(browser, '_windows_process_names', failed)
    with pytest.raises(RuntimeError, match='completely'):
        browser.BrowserPrivacy._closed(PROFILE)


def test_nonwindows_inaccessible_process_is_still_unknown(monkeypatch):
    monkeypatch.setattr(browser, '_PLATFORM', 'linux')
    monkeypatch.setattr(browser.psutil, 'process_iter', lambda *args, **kwargs: [SimpleNamespace(info={'name': None})])
    monkeypatch.setattr(browser, '_windows_process_names', lambda: (_ for _ in ()).throw(AssertionError('No native Windows snapshot on Linux.')))
    with pytest.raises(RuntimeError):
        browser.BrowserPrivacy._closed(PROFILE)


@pytest.mark.parametrize('platform', ['win32', 'darwin', 'linux'])
def test_empty_process_census_is_not_proof_of_browser_inactivity(monkeypatch, platform):
    monkeypatch.setattr(browser, '_PLATFORM', platform)
    monkeypatch.setattr(browser.psutil, 'process_iter', lambda *args, **kwargs: [])
    with pytest.raises(RuntimeError, match='completely'):
        browser.BrowserPrivacy._closed(PROFILE)


def test_fresh_native_guard_rechecks_after_backup_before_any_delete(native_fallback, monkeypatch, tmp_path):
    profile = tmp_path / 'Profiles/Default'
    profile.mkdir(parents=True)
    connection = sqlite3.connect(profile / 'Cookies')
    try:
        connection.execute('CREATE TABLE cookies(host_key TEXT, name TEXT, value TEXT)')
        connection.execute('INSERT INTO cookies VALUES(?,?,?)', ('example.fixture', 'fixture', 'private'))
        connection.commit()
    finally:
        connection.close()
    tool = browser.BrowserPrivacy(tmp_path / 'data')
    tool._discover = lambda: [dict(PROFILE, name='Default', path=str(profile), root=str(profile.parent), family='chromium')]
    snapshots = iter([(['Registry'], True), (['Registry'], True), (['Registry', 'chrome.exe'], True)])
    monkeypatch.setattr(browser, '_windows_process_names', lambda: next(snapshots))
    plan = tool.scan(tool.profiles()[0]['id'], categories=['cookies'])
    with pytest.raises(RuntimeError, match='Close every'):
        tool.apply({'confirmed': True, 'planId': plan['planId']})
    connection = sqlite3.connect(profile / 'Cookies')
    try:
        assert connection.execute('SELECT COUNT(*) FROM cookies').fetchone()[0] == 1
    finally:
        connection.close()
    assert tool.list_backups()[0]['state'] == 'failed'
