"""Native ports use disposable files and mock process actions only.

POSIX runners additionally exercise the real no-follow file deletion primitive
and read-only psutil sampling. No installed browser or application is changed.
"""
from contextlib import nullcontext
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import time
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from optimus import browser_cleanup, browser_paths, browser_privacy, cleaner, performance, sampler, slowdown


@pytest.mark.parametrize('platform,expected', [('darwin', 'Library/Application Support/Google/Chrome'),
                                               ('linux', '.config/google-chrome')])
def test_native_profile_locations_are_real_platform_layouts(tmp_path, monkeypatch, platform, expected):
    monkeypatch.delenv('XDG_CONFIG_HOME', raising=False)
    monkeypatch.delenv('XDG_CACHE_HOME', raising=False)
    locations = browser_paths.native_locations(platform, tmp_path)
    chrome = next(item for item in locations if item['id'] == 'chrome')
    assert chrome['profile_root'] == tmp_path / expected
    assert chrome['profile_root'] != chrome['cache_root']
    assert not chrome['process'].endswith('.exe')
    assert {'chrome', 'chromium', 'firefox', 'edge', 'brave', 'vivaldi'} <= {item['id'] for item in locations}


def test_relative_xdg_configuration_cannot_redirect_cleanup_roots(tmp_path, monkeypatch):
    monkeypatch.setenv('XDG_CONFIG_HOME', 'relative-config')
    monkeypatch.setenv('XDG_CACHE_HOME', 'relative-cache')
    chrome = next(item for item in browser_paths.native_locations('linux', tmp_path) if item['id'] == 'chrome')
    assert chrome['profile_root'] == tmp_path / '.config/google-chrome'
    assert chrome['cache_root'] == tmp_path / '.cache/google-chrome'


@pytest.mark.parametrize('platform,browser,name,expected', [
    ('darwin', 'chrome', 'Google Chrome Helper (Renderer)', True),
    ('darwin', 'chrome', 'Google Chrome Helper fake', False),
    ('linux', 'chromium', 'chromium-browser', True),
    ('linux', 'chrome', 'not-google-chrome', False),
    ('linux', 'firefox', 'firefox-bin', True),
    ('win32', 'chrome', 'chrome.exe', True),
    ('win32', 'chrome', 'chrome.exe fake', False),
])
def test_browser_process_names_require_known_native_names(platform, browser, name, expected):
    assert browser_paths.process_matches(browser, name, platform) is expected


def native_home(tmp_path, monkeypatch, platform='linux'):
    home = tmp_path / 'Home'
    home.mkdir()
    monkeypatch.setattr(Path, 'home', classmethod(lambda cls: home))
    monkeypatch.setattr(cleaner, '_PLATFORM', platform)
    monkeypatch.setattr(browser_privacy, '_PLATFORM', platform)
    monkeypatch.setenv('XDG_CACHE_HOME', str(home / '.cache'))
    monkeypatch.setenv('XDG_CONFIG_HOME', str(home / '.config'))
    monkeypatch.delenv('TMPDIR', raising=False)
    return home


@pytest.mark.parametrize('platform', ['linux', 'darwin'])
def test_native_cleanup_categories_never_include_entire_home_or_shared_temp(tmp_path, monkeypatch, platform):
    home = native_home(tmp_path, monkeypatch, platform)
    categories = cleaner.Cleaner(tmp_path / 'data').categories()
    assert categories
    for item in categories:
        assert Path(item['path']) not in (home, Path('/'), Path('/tmp'), Path('/var/tmp'))
        assert 'Windows' not in item['name']
    assert not any(item['id'] == 'windows-temp' for item in categories)


def test_native_full_temp_floor_is_visible_and_recent_files_stay(tmp_path, monkeypatch):
    monkeypatch.setattr(cleaner, '_PLATFORM', 'linux')
    root = tmp_path / 'private-temp'
    root.mkdir()
    target = root / 'recent.tmp'
    target.write_bytes(b'active session fixture')
    engine = cleaner.Cleaner(tmp_path / 'Data', [cleaner.Category('user-temp', 'Private temp', root, '')])
    monkeypatch.setattr(engine, '_open_files_snapshot', lambda: (set(), True))
    plan = engine.scan(['user-temp'], min_age_days=0)
    assert plan['files'] == [] and target.exists()
    assert engine.categories()[0]['minimum_age_days'] == 7
    assert plan['native_temp_minimum_age_days'] == 7
    assert plan['categories'][0]['effective_min_age_days'] == 7


@pytest.mark.skipif(os.name == 'nt', reason='Native open-file protections are exercised with real POSIX fixture deletion.')
def test_native_file_opened_after_preview_is_preserved(tmp_path, monkeypatch):
    root = tmp_path / 'cache'
    root.mkdir()
    target = root / 'selected.tmp'
    target.write_bytes(b'preserve while open')
    engine = cleaner.Cleaner(tmp_path / 'Data', [cleaner.Category('fixture', 'Cache', root, '')])
    monkeypatch.setattr(engine, '_open_files_snapshot', lambda: (set(), True))
    plan = engine.scan(['fixture'], min_age_days=0)
    monkeypatch.setattr(engine, '_open_files_snapshot', lambda: ({os.path.normcase(os.path.realpath(target))}, True))
    result = engine.apply(plan)
    assert result['deleted_count'] == 0 and target.read_bytes() == b'preserve while open'
    assert 'currently open' in result['errors'][0]


@pytest.mark.skipif(os.name == 'nt', reason='Actual POSIX mutation must fail closed for incomplete broad-temp process coverage.')
def test_native_unknown_open_file_coverage_blocks_broad_temp(tmp_path, monkeypatch):
    root = tmp_path / 'private-temp'
    root.mkdir()
    target = root / 'old.tmp'
    target.write_bytes(b'preserve on unknown ownership')
    engine = cleaner.Cleaner(tmp_path / 'Data', [cleaner.Category('user-temp', 'Private temp', root, '')])
    monkeypatch.setattr(engine, '_open_files_snapshot', lambda: (set(), True))
    future = time.time() + 8 * 86400
    monkeypatch.setattr(cleaner.time, 'time', lambda: future)
    plan = engine.scan(['user-temp'], min_age_days=0)
    assert plan['files'][0]['effective_min_age_days'] == 7
    monkeypatch.setattr(engine, '_open_files_snapshot', lambda: (set(), False))
    result = engine.apply(plan)
    assert result['deleted_count'] == 0 and target.exists()
    assert 'could not be completely verified' in result['errors'][0]
    repeat = engine.scan(['user-temp'], min_age_days=0)
    assert not repeat['files'] and 'could not be completely verified' in repeat['skipped'][0]


@pytest.mark.skipif(os.name == 'nt', reason='Native coordination-file exclusions are evaluated before POSIX unlink.')
def test_native_coordination_lock_and_pid_files_never_enter_cleanup_plan(tmp_path):
    root = tmp_path / 'cache'
    root.mkdir()
    for name in ('session.lock', 'app.pid', 'SingletonLock', 'cache.tmp'):
        (root / name).write_text('fixture')
    engine = cleaner.Cleaner(tmp_path / 'Data', [cleaner.Category('fixture', 'Cache', root, '')])
    plan = engine.scan(['fixture'], min_age_days=0)
    assert [Path(item['path']).name for item in plan['files']] == ['cache.tmp']


@pytest.mark.parametrize('platform', ['linux', 'darwin'])
def test_native_browser_cache_and_profile_discovery_preserve_personal_files(tmp_path, monkeypatch, platform):
    home = native_home(tmp_path, monkeypatch, platform)
    locations = browser_paths.native_locations(platform, home)
    chrome = next(item for item in locations if item['id'] == 'chrome')
    profile = chrome['profile_root'] / 'Default'
    profile.mkdir(parents=True)
    (profile / 'Bookmarks').write_text('keep')
    cache = chrome['cache_root'] / 'Default/Cache'
    cache.mkdir(parents=True)
    (cache / 'disposable').write_bytes(b'cache')
    monkeypatch.setattr(cleaner.psutil, 'process_iter', lambda *args, **kwargs: [])
    engine = cleaner.Cleaner(tmp_path / 'Data')
    ids = [item['id'] for item in engine.categories() if item['id'].startswith('chrome:')]
    assert ids
    plan = engine.scan(ids, min_age_days=0)
    assert [Path(item['path']) for item in plan['files']] == [cache / 'disposable']
    profiles = browser_privacy.BrowserPrivacy(tmp_path / 'Data').profiles()
    assert [(item['browser'], item['name']) for item in profiles] == [('Google Chrome', 'Default')]
    assert (profile / 'Bookmarks').read_text() == 'keep'


@pytest.mark.skipif(os.name == 'nt', reason='Uses actual POSIX dir_fd/O_NOFOLLOW primitives, exercised on both native CI runners.')
def test_posix_cleanup_deletes_only_previewed_regular_inode_and_journals(tmp_path):
    root = tmp_path / 'cache'
    root.mkdir()
    target, personal = root / 'old.tmp', tmp_path / 'personal.txt'
    target.write_bytes(b'cache fixture')
    personal.write_text('preserve')
    (root / 'link').symlink_to(personal)
    engine = cleaner.Cleaner(tmp_path / 'Data', [cleaner.Category('fixture', 'Fixture', root, '')])
    plan = engine.scan(['fixture'], min_age_days=0)
    assert [item['path'] for item in plan['files']] == [str(target)]
    result = engine.apply(plan)
    assert result['deleted_count'] == 1 and not target.exists()
    assert (root / 'link').is_symlink() and personal.read_text() == 'preserve'
    assert Path(result['log_path']).is_file()


@pytest.mark.skipif(os.name == 'nt', reason='Uses actual POSIX inode and no-follow operations.')
def test_posix_changed_file_and_replaced_parent_are_preserved(tmp_path):
    root = tmp_path / 'cache'
    root.mkdir()
    target = root / 'changed.tmp'
    target.write_bytes(b'old')
    engine = cleaner.Cleaner(tmp_path / 'Data', [cleaner.Category('fixture', 'Fixture', root, '')])
    plan = engine.scan(['fixture'], min_age_days=0)
    target.write_bytes(b'changed contents')
    result = engine.apply(plan)
    assert result['deleted_count'] == 0 and target.read_bytes() == b'changed contents'
    plan = engine.scan(['fixture'], min_age_days=0)
    moved = tmp_path / 'moved'
    root.rename(moved)
    root.symlink_to(moved, target_is_directory=True)
    result = engine.apply(plan)
    assert result['deleted_count'] == 0 and target.read_bytes() == b'changed contents'


@pytest.mark.skipif(os.name == 'nt', reason='Exercises real POSIX atomic rename and inode revalidation.')
def test_posix_replacement_during_delete_is_restored_without_deleting_its_bytes(tmp_path, monkeypatch):
    root = tmp_path / 'cache'
    root.mkdir()
    target = root / 'racing.tmp'
    target.write_bytes(b'previewed bytes')
    engine = cleaner.Cleaner(tmp_path / 'Data', [cleaner.Category('fixture', 'Fixture', root, '')])
    plan = engine.scan(['fixture'], min_age_days=0)
    rename = os.rename
    def race_before_rename(source, destination, **options):
        target.write_bytes(b'new unapproved bytes')
        return rename(source, destination, **options)
    monkeypatch.setattr(cleaner.os, 'rename', race_before_rename)
    result = engine.apply(plan)
    assert result['deleted_count'] == 0 and result['skipped_count'] == 1
    assert target.read_bytes() == b'new unapproved bytes'
    assert not list(root.glob('.optimus-delete-*'))


@pytest.mark.skipif(os.name == 'nt', reason='Exercises actual POSIX conflict preservation and subsequent scan exclusions.')
def test_posix_unrestorable_conflict_is_journaled_and_never_reselected(tmp_path, monkeypatch):
    root = tmp_path / 'cache'
    root.mkdir()
    target = root / 'racing.tmp'
    target.write_bytes(b'previewed bytes')
    engine = cleaner.Cleaner(tmp_path / 'Data', [cleaner.Category('fixture', 'Fixture', root, '')])
    plan = engine.scan(['fixture'], min_age_days=0)
    rename = os.rename
    def race_before_rename(source, destination, **options):
        target.write_bytes(b'unapproved bytes to preserve')
        rename(source, destination, **options)
        target.write_bytes(b'new application state at reused original name')
    monkeypatch.setattr(cleaner.os, 'rename', race_before_rename)
    result = engine.apply(plan)
    retained = list(root.glob('.optimus-delete-*'))
    assert len(retained) == 1 and retained[0].read_bytes() == b'unapproved bytes to preserve'
    assert str(retained[0]) in result['errors'][0]
    assert target.read_bytes() == b'new application state at reused original name'
    next_plan = engine.scan(['fixture'], min_age_days=0)
    assert str(retained[0]) not in {item['path'] for item in next_plan['files']}


@pytest.mark.parametrize('platform', ['linux', 'darwin'])
def test_native_privacy_cleanup_backs_up_restores_and_preserves_bookmarks(tmp_path, monkeypatch, platform):
    home = native_home(tmp_path, monkeypatch, platform)
    firefox = next(item for item in browser_paths.native_locations(platform, home) if item['id'] == 'firefox')
    profile = firefox['profile_root'] / 'fixture.default'
    profile.mkdir(parents=True)
    with sqlite3.connect(profile / 'places.sqlite') as database:
        database.executescript('CREATE TABLE moz_places(id INTEGER PRIMARY KEY,url TEXT);'
            'CREATE TABLE moz_historyvisits(id INTEGER PRIMARY KEY,place_id INTEGER);'
            'CREATE TABLE moz_bookmarks(id INTEGER PRIMARY KEY,fk INTEGER);'
            'INSERT INTO moz_places VALUES(1,"https://fixture.invalid");'
            'INSERT INTO moz_historyvisits VALUES(1,1); INSERT INTO moz_bookmarks VALUES(1,1);')
    monkeypatch.setattr(browser_privacy.psutil, 'process_iter', lambda *args, **kwargs: [])
    tool = browser_privacy.BrowserPrivacy(tmp_path / 'Data')
    identity = tool.profiles()[0]['id']
    plan = tool.scan(identity, ['history'])
    assert plan['counts']['history'] == 1
    outcome = tool.apply({'planId': plan['planId'], 'confirmed': True})
    with sqlite3.connect(profile / 'places.sqlite') as database:
        assert database.execute('SELECT COUNT(*) FROM moz_historyvisits').fetchone()[0] == 0
        assert database.execute('SELECT COUNT(*) FROM moz_bookmarks').fetchone()[0] == 1
    tool.restore_backup(outcome['backupId'], confirmed=True)
    with sqlite3.connect(profile / 'places.sqlite') as database:
        assert database.execute('SELECT COUNT(*) FROM moz_historyvisits').fetchone()[0] == 1


def test_native_mac_helper_blocks_privacy_before_opening_database(monkeypatch):
    monkeypatch.setattr(browser_privacy, '_PLATFORM', 'darwin')
    monkeypatch.setattr(browser_privacy.psutil, 'process_iter', lambda *args: [SimpleNamespace(info={'name': 'Google Chrome Helper (Renderer)'})])
    with pytest.raises(RuntimeError, match='Close every'):
        browser_privacy.BrowserPrivacy._closed({'browser': 'Google Chrome', 'browser_id': 'chrome', 'process': 'Google Chrome'})


@pytest.mark.parametrize('platform', ['linux', 'darwin'])
def test_native_close_rechecks_lifetime_and_requires_complete_exit(monkeypatch, platform):
    monkeypatch.setattr(browser_cleanup, '_PLATFORM', platform)
    controller = browser_cleanup.BrowserSessionController(timeout_seconds=0.1)
    record = {'pid': 123, 'name': 'chrome', 'verified': True}
    opened = {'complete': True, 'can_close': True, 'running': True, 'processes': [record]}
    closed = {'complete': True, 'can_close': True, 'running': False, 'processes': []}
    controller._inventory = Mock(side_effect=[opened, closed])
    controller._same_process = Mock(return_value=True)
    controller._windows = Mock(return_value=[(100, 123)])
    controller._post_close = Mock(return_value=True)
    quit_request = Mock()
    monkeypatch.setattr(browser_cleanup, '_mac_quit', quit_request)
    outcome = controller.close_browser('chrome')
    assert outcome['closed'] and outcome['complete']
    assert controller._same_process.call_count >= 1
    if platform == 'darwin':
        quit_request.assert_called_once_with('chrome')
        controller._post_close.assert_not_called()
    else:
        controller._post_close.assert_called_once_with(100, 123)


def test_native_wayland_session_without_x11_is_clear_failure(monkeypatch):
    monkeypatch.delenv('DISPLAY', raising=False)
    with pytest.raises(OSError, match='Close the browser yourself or skip'):
        browser_cleanup._x11_connection()


class FakeNativeProcess:
    def __init__(self, executable):
        self.pid, self.image, self.owner, self.created = 12345, str(executable), 'fixtureuser', 123.0
        self.priority, self.actions = 0, []
    def name(self): return 'FixtureApp'
    def exe(self): return self.image
    def username(self): return self.owner
    def uids(self): return SimpleNamespace(real=1001, effective=1001)
    def create_time(self): return self.created
    def parents(self): return []
    def oneshot(self): return nullcontext()
    def ppid(self): return 99
    def nice(self, value=None):
        if value is not None:
            self.priority = value
            self.actions.append(('nice', value))
        return self.priority
    def cpu_percent(self, interval=None): return 0
    def memory_info(self): return SimpleNamespace(rss=100)
    def io_counters(self): return SimpleNamespace(read_bytes=0, write_bytes=0)
    def cpu_affinity(self, values=None):
        if values is not None: self.actions.append(('affinity', values))
        return [0, 1]
    def num_threads(self): return 1
    def num_handles(self): return 0
    def status(self): return 'running'
    def suspend(self): self.actions.append(('suspend',))
    def resume(self): self.actions.append(('resume',))


def native_process(tmp_path, monkeypatch, platform='linux'):
    monkeypatch.setattr(performance, '_PLATFORM', platform)
    monkeypatch.setattr(performance, '_current_user', lambda: 'fixtureuser')
    monkeypatch.setattr(performance, '_ancestor_ids', lambda: {999})
    monkeypatch.setattr(performance.os, 'getuid', lambda: 1001, raising=False)
    monkeypatch.setattr(performance.os, 'geteuid', lambda: 1001, raising=False)
    monkeypatch.setattr(performance, '_PRIORITY_VALUES', {'Normal': 0, 'AboveNormal': -5, 'High': -10, 'BelowNormal': 10, 'Idle': 19})
    monkeypatch.setattr(performance, '_PRIORITY_NAMES', {value: str(value) for value in range(-20, 20)})
    process = FakeNativeProcess(tmp_path / 'FixtureApp')
    monkeypatch.setattr(performance.psutil, 'Process', lambda pid: process)
    return process


@pytest.mark.parametrize('platform', ['linux', 'darwin'])
def test_native_nice_and_sleep_are_explicit_owned_actions_with_journal(tmp_path, monkeypatch, platform):
    process = native_process(tmp_path, monkeypatch, platform)
    controller = performance.PerformanceController(tmp_path / 'Data/state.json')
    record = performance._record(process)
    assert not record['protected'] and process.actions == []
    outcome = controller.set_process_priority(record, 'AboveNormal')
    assert outcome['applied'] and process.actions == [('nice', -5)]
    controller.restore_priority(outcome['change_id'])
    assert process.actions[-1] == ('nice', 0)
    controller.sleep_process(record)
    assert process.actions[-1] == ('suspend',)


def test_native_reused_pid_and_other_owner_cannot_be_changed(tmp_path, monkeypatch):
    process = native_process(tmp_path, monkeypatch)
    record = performance._record(process)
    process.created += 1
    with pytest.raises(performance.PerformanceError, match='reused'):
        performance.PerformanceController(tmp_path / 'state.json').set_process_priority(record, 'Normal')
    process.created = record['create_time']
    process.owner = 'anotheruser'
    assert performance._base_protection(process)
    assert process.actions == []


@pytest.mark.parametrize('platform', ['linux', 'darwin'])
@pytest.mark.parametrize('priority,nice_value', [('BelowNormal', 10), ('Idle', 19)])
def test_native_lower_priority_presets_use_positive_nice_values_without_elevation(tmp_path, monkeypatch, platform, priority, nice_value):
    process = native_process(tmp_path, monkeypatch, platform)
    record = performance._record(process)
    controller = performance.PerformanceController(tmp_path / 'Data/state.json')
    outcome = controller.set_process_priority(record, priority)
    assert outcome['applied'] and outcome['priority'] == priority
    assert process.actions == [('nice', nice_value)]
    assert 'restoring a lower nice value may require OS permission' in record['priority_note']


def test_mac_affinity_is_explicitly_unavailable(tmp_path, monkeypatch):
    process = native_process(tmp_path, monkeypatch, 'darwin')
    record = performance._record(process)
    assert not record['affinity_supported']
    with pytest.raises(performance.PerformanceError, match='macOS'):
        performance.PerformanceController(tmp_path / 'state.json').set_affinity(record, [0])
    assert process.actions == []


@pytest.mark.skipif(sys.platform != 'linux', reason='Actual Linux CPU affinity is proved with a disposable owned child on the Linux runner.')
def test_linux_owned_disposable_child_affinity_applies_and_restores(tmp_path):
    if os.geteuid() == 0:
        pytest.skip('Root-owned processes are intentionally protected; this positive test requires the ordinary desktop/CI user.')
    child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(120)'],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    process = performance.psutil.Process(child.pid)
    try:
        original = process.cpu_affinity()
        assert original, 'The Linux runner must expose at least one allowed CPU.'
        record = performance._record(process)
        assert not record['protected'], record['reason']
        controller = performance.PerformanceController(tmp_path / 'Data/child-allocation.json')
        outcome = controller.set_affinity(record, [original[0]])
        assert outcome['applied'] and process.cpu_affinity() == [original[0]]
        restored = controller.restore_affinity(outcome['change_id'])
        assert restored['restored'] and set(process.cpu_affinity()) == set(original)
    finally:
        # psutil verifies the captured PID/create-time identity before signals.
        # Only this newly spawned fixture child is eligible for cleanup.
        if child.poll() is None:
            try:
                process.terminate()
                child.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                child.wait(timeout=5)
            except performance.psutil.NoSuchProcess:
                child.wait(timeout=5)


def test_mac_missing_process_io_does_not_break_resource_sampling(monkeypatch):
    process = SimpleNamespace(info={'pid': 123, 'name': 'Fixture', 'memory_info': SimpleNamespace(rss=100),
        'create_time': 1, 'nice': 0}, cpu_times=lambda: SimpleNamespace(user=1, system=1))
    monkeypatch.setattr(sampler.psutil, 'process_iter', lambda *args: [process])
    outcome = sampler.ResourceSampler().sample()
    assert outcome['process_count'] == 1
    assert 'read_bytes' not in outcome['processes'][0]


@pytest.mark.skipif(sys.platform == 'win32', reason='Read-only actual POSIX psutil availability is verified by native CI.')
def test_real_native_read_only_resource_diagnostics():
    outcome = sampler.ResourceSampler().sample()
    assert outcome['memory']['total'] > 0 and outcome['process_count'] > 0
    report = slowdown.analyze_slowdowns(sample_count=3, interval_seconds=0.25)
    assert report['complete'] and report['sample_count'] == 3
