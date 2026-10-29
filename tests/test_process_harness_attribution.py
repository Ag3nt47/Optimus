"""Attribution remains exact while inventories are indexed afresh per scan."""
import ntpath
from unittest.mock import patch

import pytest

from optimus import process_harness as harness
from test_process_harness import Backend, process


def legacy_owners(executable, applications):
    """Reference containment contract, independent of the indexed lookup."""
    matches = []
    for application in applications:
        if not isinstance(application, dict):
            continue
        root = application.get('InstallLocation') or application.get('install_location')
        if isinstance(root, str) and ntpath.isabs(root) and harness._within(executable, root):
            matches.append((len(harness._norm(root)), harness._text(application.get('Name') or application.get('name') or 'Installed application')))
    maximum = max((length for length, _ in matches), default=-1)
    return sorted({owner for length, owner in matches if length == maximum})


APPLICATIONS = [
    {'Name': 'Whole drive', 'InstallLocation': 'C:\\'},
    {'Name': 'Application', 'InstallLocation': r'C:\Apps\Fixture'},
    {'name': 'Same application alias', 'install_location': r'c:/apps/FIXTURE/.'},
    {'Name': 'Nested helper', 'InstallLocation': r'C:\Apps\Fixture\Helpers'},
    {'Name': 'Prefix lookalike', 'InstallLocation': r'C:\Apps\Fix'},
    {'Name': 'UNC share', 'InstallLocation': r'\\server\share'},
    {'Name': 'UNC share with slash', 'InstallLocation': '\\\\server\\share\\'},
    {'Name': 'UNC nested', 'InstallLocation': r'\\server\share\app'},
    {'Name': 'Rooted location', 'InstallLocation': '\\'},
    {'Name': 'Double slash root', 'InstallLocation': '\\\\'},
    {'Name': 'Rooted child', 'InstallLocation': r'\a'},
    {'Name': 'Do not match relative', 'InstallLocation': r'Apps\Fixture'},
    {'Name': 'Do not match drive-relative', 'InstallLocation': 'C:'},
    {'Name': 'No registered location'},
    {'Name': 'Wrong location type', 'InstallLocation': 42},
    {'Name': 'Omit https://private.invalid/token', 'InstallLocation': r'D:\Other'},
    None, 'invalid inventory row',
]


@pytest.mark.parametrize('executable', [
    r'C:\Apps\Fixture\app.exe', r'c:/APPS/Fixture/app.exe',
    r'C:\Apps\Fixture\Helpers\helper.exe', r'C:\Apps\Fixture\Helpers\..\app.exe',
    r'C:\Apps\Fixture2\app.exe', r'C:\Apps\Fix\app.exe', r'C:\Apps\Fix',
    'C:\\', 'C:', 'C:app.exe', r'D:\Other\app.exe',
    r'\\server\share\app\helper.exe', r'\\server\share\app2\helper.exe',
    r'\\server\share', '\\\\server\\share\\',
    r'\a\app.exe', r'\b\app.exe', '', 'relative.exe', None,
])
def test_indexed_ownership_preserves_longest_exact_containment(executable):
    actual, reason = harness._indexed_ownership(executable, harness._ownership_index(APPLICATIONS))
    assert actual == legacy_owners(executable, APPLICATIONS)
    assert ('No installed-application' in reason) == (not actual)


def test_many_locations_are_normalized_once_not_once_per_executable():
    applications = [{'Name': f'Application {index}', 'InstallLocation': rf'C:\Apps\Application{index}'} for index in range(1000)]
    original = harness._norm
    with patch.object(harness, '_norm', wraps=original) as normalize:
        index = harness._ownership_index(applications)
        for number in range(200):
            owners, _ = harness._indexed_ownership(rf'C:\Apps\Application{number}\app.exe', index)
            assert owners == [f'Application {number}']
    assert normalize.call_count == 1200  # Not 200 inventories times 1000 locations.


def test_scan_reuses_attribution_but_next_scan_reads_fresh_locations(tmp_path):
    backend = Backend(tmp_path)
    backend.processes.return_value = [process(pid=100), process(pid=101)]
    scanner = harness.ProcessHarness(backend)
    with patch.object(harness, '_indexed_ownership', wraps=harness._indexed_ownership) as lookup:
        first = scanner.scan()
        assert lookup.call_count == 1
        assert [row['owners'] for row in first['processes']] == [['Fixture application'], ['Fixture application']]
        first['processes'][0]['owners'].append('Caller edit')
        assert first['processes'][1]['owners'] == ['Fixture application']
        backend.installed_apps.return_value = [{'Name': 'New application', 'InstallLocation': r'C:\Fixture'}]
        second = scanner.scan()
        assert lookup.call_count == 2
        assert all(row['owners'] == ['New application'] for row in second['processes'])
    assert backend.installed_apps.call_count == 2
    assert backend.startup_list.call_count == 2
    assert backend.processes.call_count == 2
    backend.sleep_process.assert_not_called()
    backend.startup_set.assert_not_called()
    backend.uninstall.assert_not_called()


def test_duplicate_startup_paths_keep_every_exact_registration(tmp_path):
    backend = Backend(tmp_path)
    second = dict(backend.startup_list.return_value[0], Name='Second launch', Command='"c:/FIXTURE/app.exe" --second')
    backend.startup_list.return_value.append(second)
    scanner = harness.ProcessHarness(backend)
    report = scanner.scan()
    assert report['complete'] is True
    assert len(report['processes'][0]['startup_matches']) == 2
    assert len([action for action in report['actions'] if action['kind'] == 'disable_startup']) == 2
    assert all(action['selected'] is False for action in report['actions'])
