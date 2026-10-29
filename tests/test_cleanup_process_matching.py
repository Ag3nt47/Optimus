"""Keep cleanup's fresh browser blockers while avoiding per-process rule allocation."""
from types import SimpleNamespace

import pytest

from optimus import browser_paths, cleaner
from optimus.cleaner import Category, Cleaner


@pytest.mark.parametrize('platform', ('win32', 'darwin', 'linux'))
@pytest.mark.parametrize('selected', (('chrome',), ('opera', 'edge'), ('firefox', 'brave', 'chromium', 'vivaldi'), ()))
def test_prepared_matching_preserves_all_existing_blocker_rules(platform, selected):
    definitions = browser_paths.browser_definitions(platform)
    names = ['not-a-browser', '', None, 23, 'chrome.exe.backup', 'Google Chrome Helper (Renderer) extra']
    for definition in definitions.values():
        for name in definition['process_names']:
            names.extend((name, name.upper(), name[:15], name[:15].upper(), name + ' (Renderer)',
                          name + ' (GPU)', name + ' (Renderer', name + '.unrelated'))
    matches = browser_paths.process_matcher(selected, platform)
    for name in names:
        assert matches(name) is any(browser_paths.process_matches(identity, name, platform) for identity in selected), (platform, selected, name)


@pytest.mark.parametrize(('platform', 'browser', 'name'), (
    ('win32', 'chrome.exe', 'CHROME.EXE'),
    ('darwin', 'Google Chrome', 'Google Chrome Helper (Renderer)'),
    ('linux', 'microsoft-edge', 'microsoft-edge-'),
))
def test_cleanup_matches_browser_helpers_without_rebuilding_rules_for_every_process(monkeypatch, platform, browser, name):
    processes = [SimpleNamespace(info={'name': 'unrelated-' + str(index)}) for index in range(400)]
    processes.extend((SimpleNamespace(info={'name': name}), SimpleNamespace(info={'name': name})))
    monkeypatch.setattr(cleaner, '_PLATFORM', platform)
    monkeypatch.setattr(cleaner.psutil, 'process_iter', lambda attrs: iter(processes))
    original = browser_paths.browser_definitions
    calls = []

    def definitions(selected):
        calls.append(selected)
        return original(selected)

    monkeypatch.setattr(cleaner, 'browser_definitions', definitions)
    monkeypatch.setattr(browser_paths, 'browser_definitions', definitions)
    assert Cleaner._running((browser,)) == [name]
    assert calls == [platform, platform]


def test_running_checks_take_a_new_process_inventory_on_every_call(monkeypatch):
    inventories = iter(([], [SimpleNamespace(info={'name': 'chrome.exe'})], []))
    calls = []

    def processes(attrs):
        calls.append(tuple(attrs))
        return iter(next(inventories))

    monkeypatch.setattr(cleaner, '_PLATFORM', 'win32')
    monkeypatch.setattr(cleaner.psutil, 'process_iter', processes)
    assert Cleaner._running(('chrome.exe',)) == []
    assert Cleaner._running(('chrome.exe',)) == ['chrome.exe']
    assert Cleaner._running(('chrome.exe',)) == []
    assert calls == [('name',)] * 3


def test_browser_start_between_individual_deletions_still_blocks_remaining_files(tmp_path, monkeypatch):
    root = tmp_path / 'cache'
    root.mkdir()
    for index in range(2):
        (root / f'{index}.tmp').write_bytes(b'cache fixture')
    service = Cleaner(tmp_path / 'data', [Category('fixture', 'Fixture cache', root, 'Fixture only.', ('chrome.exe',))])
    inventories = iter(([], [], [SimpleNamespace(info={'name': 'chrome.exe'})]))
    monkeypatch.setattr(cleaner, '_PLATFORM', 'win32')
    monkeypatch.setattr(cleaner.psutil, 'process_iter', lambda attrs: iter(next(inventories)))
    monkeypatch.setattr(service, '_open_files_snapshot', lambda: (set(), True))
    removed = []
    monkeypatch.setattr(service, '_delete_exact', lambda record: removed.append(record['path']))
    plan = service.scan(['fixture'], 0)
    result = service.apply(plan)
    assert result['deleted_count'] == 1
    assert result['skipped_count'] == 1
    assert len(removed) == 1
    assert 'browser started after the preview' in result['errors'][0]


def test_no_browser_category_does_not_enumerate_processes(monkeypatch):
    monkeypatch.setattr(cleaner.psutil, 'process_iter', lambda attrs: pytest.fail('No process names require checking.'))
    assert Cleaner._running(()) == []
