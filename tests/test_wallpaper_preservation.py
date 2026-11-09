"""Wallpaper protection exercises only temporary files and read-only policy checks."""
from __future__ import annotations

from contextlib import contextmanager
import json
import os
from pathlib import Path
import subprocess
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from optimus import cleaner as cleanup
from optimus import wallpaper_preservation as wallpaper_state
from optimus.file_utilities import FileUtilities
from optimus.wallpaper_preservation import protected_wallpaper_path


@pytest.mark.parametrize('path', [
    r'C:\Users\Fixture\Pictures\Bing Wallpaper\today.jpg',
    r'D:\Cache\Bing-Wallpaper\settings.json',
    r'C:\Users\Fixture\AppData\Local\Packages\Microsoft.BingWallpaper_8wekyb3d8bbwe\LocalState\image.jpg',
    r'D:\WpSystem\Fixture\AppData\Local\Packages\Microsoft.BingWallpaper_8wekyb3d8bbwe\LocalState',
    r'C:\Program Files\WindowsApps\Microsoft.BingWallpaper_1.0_x86__fixture\BingWallpaper.exe',
    'C:/Users/Fixture/AppData/Roaming/Microsoft/Windows/Themes/TranscodedWallpaper',
    r'C:\Windows\Web\Wallpaper\Windows\img0.jpg',
    r'c:\cache\BINGWALLPAPER\WebView\Cache\data',
])
def test_wallpaper_and_store_state_paths_are_protected(path):
    assert protected_wallpaper_path(path)


@pytest.mark.parametrize('path', [
    r'C:\Users\Fixture\Pictures\holiday.jpg',
    r'D:\Cache\ordinary.tmp',
    r'D:\Notes\Themes\example.txt',
    r'D:\Notes\WindowsApps-readme.txt',
    r'D:\Notes\WpSystem-backup-notes.txt',
])
def test_unrelated_personal_files_remain_eligible(path):
    assert not protected_wallpaper_path(path)


def fixture_cleaner(tmp_path, root):
    return cleanup.Cleaner(tmp_path / 'state', [
        cleanup.Category('fixture-cache', 'Fixture cache', root, 'Disposable test fixtures only.')])


@contextmanager
def fixture_junction(tmp_path, physical_name):
    """Create one fixture alias; remove only its reparse entry on exit."""
    if os.name != 'nt':
        pytest.skip('NTFS directory junction behavior is Windows-specific.')
    physical, alias = tmp_path / physical_name, tmp_path / 'ordinary-alias'
    physical.mkdir()
    quote = lambda value: "'" + str(value).replace("'", "''") + "'"
    command = 'New-Item -ItemType Junction -Path ' + quote(alias) + ' -Target ' + quote(physical) + ' | Out-Null'
    subprocess.run(['powershell.exe', '-NoProfile', '-NonInteractive', '-Command', command],
        capture_output=True, text=True, check=True, timeout=30,
        creationflags=subprocess.CREATE_NO_WINDOW)
    try:
        assert os.path.samefile(alias, physical)
        yield alias, physical
    finally:
        if os.path.lexists(alias):
            assert alias.parent.resolve() == tmp_path.resolve()
            assert getattr(alias.lstat(), 'st_file_attributes', 0) & 0x400
            assert os.path.samefile(alias, physical)
            os.rmdir(alias)
        assert physical.is_dir()


@pytest.mark.skipif(os.name != 'nt', reason='Windows TEMP environment selection is being checked.')
def test_redirected_temp_cannot_turn_bing_data_into_cleanup_scope(tmp_path, monkeypatch):
    root = tmp_path / 'Bing Wallpaper'
    root.mkdir()
    wallpaper = root / 'today.jpg'
    wallpaper.write_bytes(b'preserved wallpaper fixture')
    monkeypatch.setenv('TEMP', str(root))
    monkeypatch.setenv('LOCALAPPDATA', str(tmp_path / 'Local'))
    cleaner = cleanup.Cleaner(tmp_path / 'state')

    plan = cleaner.scan(['user-temp'], min_age_days=0)
    outcome = cleaner.apply(plan)

    assert plan['files'] == []
    assert outcome['deleted_count'] == 0
    assert wallpaper.read_bytes() == b'preserved wallpaper fixture'


def test_nested_wallpaper_state_is_preserved_while_ordinary_sibling_is_cleaned(tmp_path):
    root = tmp_path / 'cache'
    protected = root / 'Microsoft.BingWallpaper_8wekyb3d8bbwe' / 'LocalState'
    protected.mkdir(parents=True)
    wallpaper = protected / 'image.jpg'
    wallpaper.write_bytes(b'wallpaper fixture')
    ordinary = root / 'ordinary.tmp'
    ordinary.write_bytes(b'ordinary disposable fixture')
    cleaner = fixture_cleaner(tmp_path, root)

    plan = cleaner.scan(['fixture-cache'], min_age_days=0)
    assert [item['path'] for item in plan['files']] == [str(ordinary)]
    outcome = cleaner.apply(plan)

    assert outcome['deleted_count'] == 1, outcome
    assert not ordinary.exists()
    assert wallpaper.read_bytes() == b'wallpaper fixture'


def test_protection_is_rechecked_for_previously_issued_delete_plan(tmp_path, monkeypatch):
    root = tmp_path / 'Bing Wallpaper'
    root.mkdir()
    wallpaper = root / 'today.jpg'
    wallpaper.write_bytes(b'stale-plan fixture')
    cleaner = fixture_cleaner(tmp_path, root)
    with monkeypatch.context() as previous_version:
        previous_version.setattr(cleanup, 'protected_wallpaper_path', lambda path: False)
        plan = cleaner.scan(['fixture-cache'], min_age_days=0)
    assert len(plan['files']) == 1

    outcome = cleaner.apply(plan)

    assert outcome['deleted_count'] == 0
    assert outcome['skipped_count'] == 1
    assert wallpaper.read_bytes() == b'stale-plan fixture'


@pytest.mark.parametrize('encrypted_directory', [False, True], ids=['encrypted-file', 'encrypted-directory'])
def test_encrypted_app_data_is_never_cleanup_eligible(tmp_path, monkeypatch, encrypted_directory):
    root = tmp_path / 'cache'
    root.mkdir()
    target = root / 'private.bin'
    target.write_bytes(b'encrypted-state fixture')
    encrypted_path = root if encrypted_directory else target
    original = Path.lstat

    def encrypted_metadata(path, *args, **kwargs):
        value = original(path, *args, **kwargs)
        if path == encrypted_path:
            fields = {name: getattr(value, name) for name in dir(value) if name.startswith('st_')}
            fields['st_file_attributes'] = getattr(value, 'st_file_attributes', 0) | 0x4000
            return SimpleNamespace(**fields)
        return value

    monkeypatch.setattr(Path, 'lstat', encrypted_metadata)
    cleaner = fixture_cleaner(tmp_path, root)
    plan = cleaner.scan(['fixture-cache'], min_age_days=0)

    assert plan['files'] == []
    assert target.read_bytes() == b'encrypted-state fixture'


def test_explicit_overwrite_refuses_bing_wallpaper(tmp_path):
    root = tmp_path / 'Pictures' / 'Bing Wallpaper'
    root.mkdir(parents=True)
    wallpaper = root / 'today.jpg'
    wallpaper.write_bytes(b'never overwrite this fixture')
    service = FileUtilities(SimpleNamespace(data_dir=tmp_path / 'state'), owner='fixture-owner')

    result = service.shred_plan([str(wallpaper)])

    assert result['status'] == 'blocked', result
    assert wallpaper.read_bytes() == b'never overwrite this fixture'


def test_arbitrary_current_wallpaper_is_preserved_during_cleanup(tmp_path, monkeypatch):
    root = tmp_path / 'cache'
    root.mkdir()
    active = root / 'family-holiday.jpg'
    active.write_bytes(b'current wallpaper outside any named wallpaper folder')
    ordinary = root / 'ordinary.tmp'
    ordinary.write_bytes(b'disposable cache fixture')
    monkeypatch.setattr(wallpaper_state, 'current_wallpaper_paths', lambda **kwargs: {str(active)})
    cleaner = fixture_cleaner(tmp_path, root)

    plan = cleaner.scan(['fixture-cache'], min_age_days=0)
    assert [item['path'] for item in plan['files']] == [str(ordinary)]
    outcome = cleaner.apply(plan)

    assert outcome['deleted_count'] == 1, outcome
    assert not ordinary.exists()
    assert active.read_bytes() == b'current wallpaper outside any named wallpaper folder'


def test_wallpaper_selected_after_preview_is_preserved_at_delete_time(tmp_path, monkeypatch):
    root = tmp_path / 'cache'
    root.mkdir()
    active = root / 'personal-photo.jpg'
    active.write_bytes(b'newly selected current wallpaper fixture')
    current = set()
    monkeypatch.setattr(wallpaper_state, 'current_wallpaper_paths', lambda **kwargs: current.copy())
    cleaner = fixture_cleaner(tmp_path, root)
    plan = cleaner.scan(['fixture-cache'], min_age_days=0)
    assert len(plan['files']) == 1

    current.add(str(active))
    outcome = cleaner.apply(plan)

    assert outcome['deleted_count'] == 0
    assert outcome['skipped_count'] == 1
    assert active.read_bytes() == b'newly selected current wallpaper fixture'


def test_explicit_overwrite_refuses_current_wallpaper_with_arbitrary_filename(tmp_path, monkeypatch):
    active = tmp_path / 'personal-photo.jpg'
    active.write_bytes(b'current personal wallpaper fixture')
    monkeypatch.setattr(wallpaper_state, 'current_wallpaper_paths', lambda **kwargs: {str(active)})
    service = FileUtilities(SimpleNamespace(data_dir=tmp_path / 'state'), owner='fixture-owner')

    result = service.shred_plan([str(active)])

    assert result['status'] == 'blocked', result
    assert active.read_bytes() == b'current personal wallpaper fixture'


@pytest.mark.parametrize(('path', 'name'), [
    (r'Control Panel\Desktop', 'Wallpaper'),
    (r'Software\Microsoft\Windows\CurrentVersion\Policies\System', 'NoChangingWallpaper'),
    (r'Software\Microsoft\Windows\CurrentVersion\Explorer\Wallpapers', 'BackgroundHistoryPath0'),
])
def test_privacy_write_boundary_rejects_wallpaper_targets_before_native_call(tmp_path, monkeypatch, path, name):
    from optimus import system_privacy
    monkeypatch.setitem(system_privacy._CONTROLS, 'fixture-wallpaper-control', {'path': path, 'name': name})
    native = Mock(side_effect=AssertionError('Wallpaper registry writes must never reach native code.'))
    service = system_privacy.SystemPrivacy(tmp_path / 'state', runner=native)

    with pytest.raises(ValueError, match='wallpaper'):
        service._call('registry_write', {'control': 'fixture-wallpaper-control', 'value': 0})

    native.assert_not_called()


@pytest.mark.skipif(os.name != 'nt', reason='Windows COM apartment and registration flags are being checked.')
def test_monitor_discovery_allows_the_registered_local_com_server(monkeypatch):
    import ctypes
    ole = SimpleNamespace(CoInitializeEx=Mock(return_value=0),
        CoCreateInstance=Mock(return_value=-2147221164),
        CoTaskMemFree=Mock(), CoUninitialize=Mock())
    monkeypatch.setattr(ctypes, 'WinDLL', lambda name: ole)

    assert wallpaper_state._monitor_wallpapers() == ()

    # DesktopWallpaper is registered as a local COM server on this Windows
    # installation; an in-process-only request silently misses every monitor.
    assert ole.CoCreateInstance.call_args.args[2] & 4
    ole.CoUninitialize.assert_called_once_with()


@pytest.mark.skipif(os.name != 'nt', reason='Windows SPI and registry read failures are being checked.')
def test_wallpaper_discovery_retains_known_paths_when_native_reads_fail(tmp_path, monkeypatch):
    import ctypes
    import winreg
    known = str(tmp_path / 'known-current-wallpaper.jpg')
    monkeypatch.setattr(wallpaper_state, '_cached_wallpapers', (known,))
    monkeypatch.setattr(wallpaper_state, '_cached_at', -float('inf'))
    monkeypatch.setattr(wallpaper_state, '_monitor_wallpapers', lambda: ())
    monkeypatch.setattr(ctypes.windll.user32, 'SystemParametersInfoW', Mock(return_value=False))
    monkeypatch.setattr(winreg, 'OpenKey', Mock(side_effect=FileNotFoundError))

    assert known in wallpaper_state.current_wallpaper_paths(refresh=True)


def test_ordinary_junction_alias_cannot_hide_a_bing_wallpaper_folder(tmp_path):
    with fixture_junction(tmp_path, 'Bing Wallpaper') as (alias, physical):
        image = physical / 'today.jpg'
        image.write_bytes(b'named physical wallpaper fixture')

        assert protected_wallpaper_path(alias / image.name)
        assert image.read_bytes() == b'named physical wallpaper fixture'


def test_current_wallpaper_alias_protects_its_physical_image(tmp_path, monkeypatch):
    with fixture_junction(tmp_path, 'personal-images') as (alias, physical):
        image = physical / 'personal-photo.jpg'
        image.write_bytes(b'current wallpaper under an ordinary physical folder')
        monkeypatch.setattr(wallpaper_state, 'current_wallpaper_paths',
            lambda **kwargs: {str(alias / image.name)})

        assert protected_wallpaper_path(image)
        assert not protected_wallpaper_path(physical / 'ordinary.tmp')
        assert image.read_bytes() == b'current wallpaper under an ordinary physical folder'


def test_windows_duplicate_engine_resolves_cached_wallpaper_alias_without_registry_writes(tmp_path):
    with fixture_junction(tmp_path, 'personal-images') as (alias, physical):
        image = physical / 'personal-photo.jpg'
        image.write_bytes(b'current physical wallpaper fixture')
        script = Path(__file__).parents[1] / 'src/optimus/engine/FileTools.ps1'
        quote = lambda value: "'" + str(value).replace("'", "''") + "'"
        command = ". " + quote(script) + '; '
        command += '$script:SCMWallpaperFiles = @(' + quote(alias / image.name) + '); '
        command += '$script:SCMWallpaperCheckedAt = [datetime]::MinValue; '
        command += '[void](Get-SCMFileToolCurrentWallpapers -Refresh); '
        command += '[pscustomobject]@{Canonical=[OptimusWallpaperReadOnly]::CanonicalPath(' + quote(alias / image.name) + '); '
        command += 'Protected=(Test-SCMFileToolExcludedPath -Path ' + quote(image) + ' -ExcludedRoots @() -RefreshWallpaper)} | ConvertTo-Json -Compress'
        result = subprocess.run(['powershell.exe', '-NoProfile', '-NonInteractive', '-Command', command],
            capture_output=True, text=True, check=True, timeout=30,
            creationflags=subprocess.CREATE_NO_WINDOW)
        outcome = json.loads(result.stdout.lstrip('\ufeff'))

        assert os.path.normcase(outcome['Canonical']) == os.path.normcase(str(image))
        assert outcome['Protected'] is True
        assert image.read_bytes() == b'current physical wallpaper fixture'


@pytest.mark.skipif(os.name != 'nt', reason='Windows duplicate-recycling engine uses PowerShell.')
def test_windows_duplicate_engine_excludes_wallpaper_paths_without_mutation():
    script = Path(__file__).parents[1] / 'src/optimus/engine/FileTools.ps1'
    command = ". '" + str(script).replace("'", "''") + "'; " + r'''
$paths = @(
 'C:\Users\Fixture\Pictures\Bing Wallpaper\today.jpg',
 'D:\WpSystem\Fixture\AppData\Local\Packages\app\state',
 'C:\Program Files\WindowsApps\app\state',
 'C:\Users\Fixture\AppData\Roaming\Microsoft\Windows\Themes\TranscodedWallpaper',
 'C:\Windows\Web\Wallpaper\img0.jpg',
 'D:\Fixture\ordinary.txt'
)
@($paths | ForEach-Object { Test-SCMFileToolExcludedPath -Path $_ -ExcludedRoots @() }) | ConvertTo-Json -Compress
'''
    result = subprocess.run(['powershell.exe', '-NoProfile', '-NonInteractive', '-Command', command],
        capture_output=True, text=True, check=True, timeout=30,
        creationflags=subprocess.CREATE_NO_WINDOW)

    assert json.loads(result.stdout.lstrip('\ufeff')) == [True, True, True, True, True, False]
