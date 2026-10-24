"""Application-cache fixtures never enumerate or clean the signed-in user's apps."""
import copy
import os
from pathlib import Path
import threading
import time
import sys

import pytest

from optimus import application_caches as caches


@pytest.fixture
def environment(tmp_path):
    root = tmp_path / 'discord/Cache'
    root.mkdir(parents=True)
    recipe = caches.CacheRecipe('fixture:discord', 'discord', root, ('fixture-discord',))
    config = dict(caches.DEFAULTS, enabled=True, apps=['discord'])
    clock = time.time() + 40 * 86400
    def file(name, content=b'cache'):
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        return path
    def service(**changes):
        return caches.ApplicationCacheCleaner(tmp_path / 'data', recipe_provider=lambda: [recipe],
            process_reader=changes.pop('process_reader', lambda _: {'complete': True, 'active': False}),
            open_reader=changes.pop('open_reader', lambda: (set(), True)), clock=lambda: clock, **changes)
    return root, recipe, config, file, service


def test_only_exact_cache_entries_removed_preserving_account_download_game_shader_files(environment):
    root, _, config, file, service = environment
    cache = [file('data_0'), file('cache_data/f_012abc'), file('index-dir/the-real-index'), file('js/0123456789abcdef_0')]
    personal = [file('Cookies'), file('History'), file('Login Data'), file('settings.json'), file('song.mp3'),
                file('Downloads/f_1234'), file('userdata/f_1234'), file('GPUCache/f_1234'), file('game.exe'), file('state.db')]
    cleaner = service()
    plan = cleaner.scan(config)
    assert plan['complete'] and len(plan['files']) == 4
    result = cleaner.apply(plan, config)
    assert result['deleted_count'] == 4
    assert result['deleted_bytes'] == sum(len(b'cache') for _ in cache)
    assert all(not path.exists() for path in cache)
    assert all(path.exists() for path in personal)
    assert root.exists()


@pytest.mark.parametrize('state', [{'complete': False, 'active': False}, {'complete': True, 'active': True}, {}, None])
def test_active_or_unknown_process_inventory_preserves_app(environment, state):
    _, _, config, file, service = environment
    path = file('data_0')
    result = service(process_reader=lambda _: state).run(config)
    assert result['deleted_count'] == 0 and path.exists()


def test_process_start_between_scan_and_apply_preserves_files(environment):
    _, _, config, file, service = environment
    path = file('data_0')
    state = {'complete': True, 'active': False}
    cleaner = service(process_reader=lambda _: dict(state))
    plan = cleaner.scan(config)
    state['active'] = True
    assert cleaner.apply(plan, config)['skipped_count'] == 1
    assert path.exists()


def test_saved_choices_revoked_before_apply_preserves_files(environment):
    _, _, config, file, service = environment
    path = file('data_0')
    saved = copy.deepcopy(config)
    cleaner = service(scope_reader=lambda: saved)
    plan = cleaner.scan(config)
    saved['enabled'] = False
    with pytest.raises(ValueError, match='choices changed'):
        cleaner.apply(plan, config)
    assert path.exists()


def test_changed_file_identity_and_preview_tampering_cannot_authorize_delete(environment, tmp_path):
    _, _, config, file, service = environment
    path, outside = file('data_0'), tmp_path / 'personal.txt'
    outside.write_text('preserve')
    cleaner = service()
    plan = cleaner.scan(config)
    plan['files'][0]['path'] = str(outside)
    plan['files'][0]['size'] = outside.stat().st_size
    path.write_bytes(b'changed after preview')
    result = cleaner.apply(plan, config)
    assert result['deleted_count'] == 0 and path.exists() and outside.read_text() == 'preserve'


def test_consumed_plan_cannot_replay(environment):
    _, _, config, file, service = environment
    file('data_0')
    cleaner = service()
    plan = cleaner.scan(config)
    cleaner.apply(plan, config)
    with pytest.raises(ValueError, match='expired'):
        cleaner.apply(plan, config)


def test_hardlinks_and_coordination_names_preserved(environment, tmp_path):
    _, _, config, file, service = environment
    original = file('data_0')
    os.link(original, tmp_path / 'linked-cache')
    lock = file('SingletonLock')
    result = service().run(config)
    assert result['deleted_count'] == 0 and original.exists() and lock.exists()


def test_symlink_scope_and_entry_preserved(environment, tmp_path):
    root, recipe, config, file, service = environment
    outside = tmp_path / 'personal'
    outside.mkdir()
    target = outside / 'data_0'
    target.write_text('personal')
    link = root / 'data_0'
    try:
        link.symlink_to(target)
    except OSError:
        pytest.skip('Creating symlinks is not available to this fixture user.')
    assert service().run(config)['deleted_count'] == 0
    assert target.read_text() == 'personal'


def test_caps_count_every_entry_including_directories_and_unrecognized_files(environment):
    _, _, config, file, service = environment
    for index in range(30):
        file('personal-' + str(index))
    file('data_0')
    cleaner = service()
    plan = cleaner.scan(dict(config, max_entries=2))
    assert not plan['complete'] and plan['examined'] <= 3 and len(plan['files']) <= 2


def test_byte_cap_does_not_approve_oversized_file(environment):
    _, _, config, file, service = environment
    path = file('data_0', b'123456')
    result = service().run(dict(config, max_bytes=3))
    assert not result['complete'] and result['deleted_bytes'] == 0 and path.exists()


@pytest.mark.parametrize('opened', [(set(), False), ({'fixture'}, False)])
def test_incomplete_open_inventory_preserves_scope(environment, opened):
    _, _, config, file, service = environment
    path = file('data_0')
    assert service(open_reader=lambda: opened).run(config)['deleted_count'] == 0
    assert path.exists()


def test_locked_file_is_preserved_and_other_cache_continues(environment):
    _, _, config, file, service = environment
    locked, other = file('data_0'), file('data_1')
    def delete(record):
        if record['path'] == str(locked):
            raise PermissionError('fixture locked')
        caches.Cleaner._delete_exact(record)
    result = service(delete=delete).run(config)
    assert result['deleted_count'] == 1 and result['skipped_count'] == 1
    assert locked.exists() and not other.exists()


def test_cancelled_scan_never_deletes(environment):
    _, _, config, file, service = environment
    path = file('data_0')
    event = threading.Event()
    event.set()
    result = service().run(config, cancel_event=event)
    assert result['deleted_bytes'] == 0 and path.exists()


def test_defaults_and_invalid_configuration_do_not_widen_approval():
    assert caches.DEFAULTS['enabled'] is False and caches.DEFAULTS['apps'] == []
    for update in ({'apps': ['unknown']}, {'apps': ['discord', 'discord']}, {'min_age_days': 0},
                   {'max_entries': True}, {'max_bytes': -1}, {'enabled': 1}, {'root': 'C:/'}):
        with pytest.raises(ValueError):
            caches.validate_configuration(update)


@pytest.mark.parametrize('platform', ['win32', 'darwin', 'linux'])
def test_native_recipes_are_narrow_cache_leaves_without_whole_profiles(platform, monkeypatch, tmp_path):
    monkeypatch.setattr(caches, '_known_folder', lambda _: tmp_path)
    recipes = caches.discover_recipes(system=platform, home=tmp_path)
    assert recipes
    assert all(recipe.root.name in ('Cache', 'Code Cache') for recipe in recipes)
    assert not any('Storage' in recipe.root.parts or 'steamapps' in recipe.root.parts or 'GPUCache' in recipe.root.parts for recipe in recipes)
    assert len({recipe.id for recipe in recipes}) == len(recipes)


@pytest.mark.parametrize('names,complete,active', [(['Registry', 'fixture-discord.exe'], True, True),
                                                (['Registry'], True, False), (['Registry'], False, False)])
def test_native_snapshot_preserves_unknown_and_active_semantics(monkeypatch, tmp_path, names, complete, active):
    monkeypatch.setattr(caches.sys, 'platform', 'win32')
    monkeypatch.setattr(caches, '_windows_process_names', lambda: (names, complete))
    state = caches.process_state(caches.CacheRecipe('fixture', 'discord', tmp_path, ('fixture-discord.exe',)))
    assert state['complete'] is complete and state['active'] is active


@pytest.mark.skipif(sys.platform != 'win32', reason='Native Windows Toolhelp process snapshot.')
def test_real_native_process_snapshot_is_readonly_and_detects_current_interpreter(tmp_path):
    state = caches.process_state(caches.CacheRecipe('fixture', 'discord', tmp_path, (Path(sys.executable).name,)))
    assert state['complete'] is True and state['active'] is True


@pytest.mark.parametrize('flag', [caches.REPARSE, caches.OFFLINE, caches.RECALL])
def test_cloud_and_indirect_roots_rejected_without_symlink_privilege(environment, monkeypatch, flag):
    root, _, config, file, service = environment
    path = file('data_0')
    original = Path.lstat
    class HazardStat:
        def __init__(self, value):
            self.value = value
        def __getattr__(self, name):
            if name == 'st_file_attributes':
                return getattr(self.value, name, 0) | flag
            return getattr(self.value, name)
    monkeypatch.setattr(Path, 'lstat', lambda self, *args, **kwargs: HazardStat(original(self, *args, **kwargs)) if self == root else original(self, *args, **kwargs))
    assert service().run(config)['deleted_count'] == 0 and path.exists()
