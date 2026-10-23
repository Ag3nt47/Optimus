"""Retention and guard workflow tests delete only newly owned fixture files."""
import copy
import json
import os
from pathlib import Path
import threading
import time
from types import SimpleNamespace

import pytest

from optimus.retention_cleanup import RetentionCleanup, RetentionScope, discover_scopes, process_state, _read_authority
from optimus.workflow import DEFAULTS, Preferences, full_optimization_settings, Workflow
from optimus.native_workflow import NativeWorkflow
from optimus.parallel_updates import parallel_installer_stages, WINDOWS_SOFTWARE


def settings(kind='failed_downloads', days=7, **changes):
    value = dict(DEFAULTS, **{kind + '_cleanup_enabled': True, kind + '_retention_days': days})
    value.update(changes)
    return value


@pytest.fixture
def environment(tmp_path):
    root = tmp_path / 'Downloads'
    root.mkdir()
    clock = time.time() + 40 * 86400
    def file(name, *, age=10, data=b'fixture'):
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        stamp = clock - age * 86400
        os.utime(path, (stamp, stamp))
        return path
    def service(*, scopes=None, **options):
        return RetentionCleanup(tmp_path / 'data', clock=lambda: clock,
            scope_provider=lambda *_: scopes or [RetentionScope('fixture-downloads', root, suffixes=('.crdownload', '.part', '.partial'))],
            process_reader=lambda _: {'complete': True, 'active': False},
            open_reader=lambda: (set(), True), **options)
    return SimpleNamespace(root=root, file=file, service=service, clock=clock, data=tmp_path / 'data')


def test_defaults_keep_all_new_mutating_retention_disabled_and_guard_readonly_on():
    assert len(DEFAULTS) == 99
    for prefix in ('codex_archives', 'angerona_logs', 'failed_downloads'):
        assert DEFAULTS[prefix + '_cleanup_enabled'] is False
        assert DEFAULTS[prefix + '_retention_days'] == 7
        assert full_optimization_settings()[prefix + '_cleanup_enabled'] is False
    assert Preferences.safe_defaults()['drive_space_guard_enabled'] is True
    assert DEFAULTS['drive_space_guard_target_gib'] == 2.0


@pytest.mark.parametrize('prefix', ['codex_archives', 'angerona_logs', 'failed_downloads'])
@pytest.mark.parametrize('invalid', [True, 0, -1, 3651, 1.5, '7', None])
def test_retention_age_is_strict_independent_and_never_broad_temp_zero(prefix, invalid):
    with pytest.raises(ValueError):
        Preferences.validate({prefix + '_retention_days': invalid})
    assert Preferences.validate({prefix + '_retention_days': 1, 'cleanup_age_days': 0})[prefix + '_retention_days'] == 1


@pytest.mark.parametrize('invalid', [True, False, 0, 2.01, -1, float('nan'), float('inf'), '2', None])
def test_space_guard_target_rejects_invalid_values_before_any_backend_query(invalid):
    with pytest.raises(ValueError):
        Preferences.validate({'drive_space_guard_target_gib': invalid})


def test_disabled_retention_never_discovers_or_deletes(environment):
    path = environment.file('kept.crdownload')
    service = environment.service()
    service.scope_provider = lambda *_: (_ for _ in ()).throw(AssertionError('No scope discovery while disabled'))
    result = service.run('failed_downloads', DEFAULTS)
    assert result['disabled'] is True and result['deleted_count'] == 0 and path.exists()
    assert not environment.data.exists()


def test_only_old_top_level_supported_remnants_are_removed(environment):
    old = environment.file('old.crdownload', data=b'old download')
    recent = environment.file('recent.part', age=1)
    complete = environment.file('complete.zip')
    normal = environment.file('manual.txt')
    nested = environment.file('nested/old.part')
    directory = environment.root / 'bundle.download'
    directory.mkdir()
    events = []
    result = environment.service().run('failed_downloads', settings(), progress_callback=events.append)
    assert not old.exists()
    assert all(path.exists() for path in (recent, complete, normal, nested, directory))
    assert result['deleted_bytes'] == len(b'old download') and result['deleted_count'] == 1
    assert events[-1]['deleted_bytes'] == result['deleted_bytes']
    receipt = json.loads(Path(result['log_path']).read_text())
    assert receipt['state'] == 'completed' and receipt['result']['deleted_count'] == 1


def test_exact_angerona_diagnostics_preserve_security_and_unknown_logs(environment):
    old = environment.file('startup-dashboard.previous.log')
    evidence = environment.file('runtime_alerts.log')
    audit = environment.file('audit.log')
    database = environment.file('state.db')
    scopes = [RetentionScope('fixture-angerona', environment.root, names=('startup-dashboard.previous.log',))]
    result = environment.service(scopes=scopes).run('angerona_logs', settings('angerona_logs'))
    assert not old.exists()
    assert all(path.exists() for path in (evidence, audit, database))
    assert result['deleted_count'] == 1


@pytest.mark.parametrize('state', [{'complete': False, 'active': False}, {'complete': True, 'active': True}, {}])
def test_active_or_unknown_program_state_preserves_all_files(environment, state):
    path = environment.file('old.crdownload')
    service = environment.service()
    service.process_reader = lambda _: state
    result = service.run('failed_downloads', settings())
    assert path.exists() and result['deleted_count'] == 0 and result['skipped'] is True


def test_incomplete_open_file_inventory_preserves_scope(environment):
    path = environment.file('old.part')
    service = environment.service()
    service.open_reader = lambda: (set(), False)
    result = service.run('failed_downloads', settings())
    assert path.exists() and result['deleted_count'] == 0


def test_open_file_is_preserved_even_when_aged(environment):
    path = environment.file('old.part')
    service = environment.service()
    service.open_reader = lambda: ({os.path.normcase(os.path.realpath(path))}, True)
    result = service.run('failed_downloads', settings())
    assert path.exists() and result['deleted_count'] == 0


def test_hardlinks_and_indirect_roots_are_preserved(environment, tmp_path):
    path = environment.file('old.part')
    linked = environment.root / 'linked.part'
    try:
        os.link(path, linked)
    except OSError:
        pytest.skip('Hard links unavailable in this disposable fixture filesystem')
    result = environment.service().run('failed_downloads', settings())
    assert path.exists() and linked.exists() and result['deleted_count'] == 0
    alias = tmp_path / 'alias'
    try:
        alias.symlink_to(environment.root, target_is_directory=True)
    except OSError:
        return  # The hard-link assertion above still ran on Windows without link privilege.
    result = environment.service(scopes=[RetentionScope('indirect', alias, suffixes=('.part',))]).run('failed_downloads', settings())
    assert path.exists() and result['failed_items'] == 1 and result['deleted_count'] == 0


def test_cancel_after_preview_preserves_files_without_deletion(environment):
    path = environment.file('old.part')
    event = threading.Event()
    def progress(state):
        if state['item_progress']['phase'] == 'cleanup':
            event.set()
    result = environment.service().run('failed_downloads', settings(), progress_callback=progress, cancel_event=event)
    assert path.exists() and result['cancelled'] is True and result['deleted_count'] == 0


def test_changed_saved_scope_before_apply_preserves_files(environment):
    path = environment.file('old.part')
    calls = []
    def current():
        calls.append(1)
        return settings(days=7 if len(calls) == 1 else 30)
    result = environment.service(scope_reader=current).run('failed_downloads', settings())
    assert path.exists() and result['deleted_count'] == 0 and result['complete'] is False


def test_locked_file_failure_continues_to_other_eligible_file(environment):
    first, second = environment.file('one.part'), environment.file('two.part')
    from optimus.cleaner import Cleaner
    def delete(record):
        if Path(record['path']) == first:
            raise PermissionError('Fixture active file')
        Cleaner._delete_exact(record)
    result = environment.service(delete=delete).run('failed_downloads', settings())
    assert first.exists() and not second.exists()
    assert result['deleted_count'] == 1 and result['skipped_count'] == 1


def test_changed_file_after_scan_is_preserved(environment):
    path = environment.file('old.part')
    def progress(state):
        if state['item_progress']['phase'] == 'cleanup' and state['item_progress']['percent'] == 0:
            path.write_bytes(b'new user content')
    result = environment.service().run('failed_downloads', settings(), progress_callback=progress)
    assert path.read_bytes() == b'new user content' and result['deleted_count'] == 0


def test_duplicate_scope_roots_do_not_duplicate_preview_or_removed_totals(environment):
    path = environment.file('old.part')
    scope = RetentionScope('fixture', environment.root, suffixes=('.part',))
    result = environment.service(scopes=[scope, scope]).run('failed_downloads', settings())
    assert not path.exists() and result['deleted_count'] == 1


def test_native_angerona_has_no_invented_windows_paths(tmp_path):
    assert discover_scopes('angerona_logs', tmp_path / 'data', system='linux', home=tmp_path) == []


def test_native_download_folder_configuration_is_not_shell_evaluated(tmp_path):
    configuration = tmp_path / '.config/user-dirs.dirs'
    configuration.parent.mkdir()
    configuration.write_text('XDG_DOWNLOAD_DIR="$HOME/MyDownloads"\n')
    scopes = discover_scopes('failed_downloads', tmp_path / 'data', system='linux', home=tmp_path)
    assert scopes[0].root == tmp_path / 'MyDownloads'
    configuration.write_text('XDG_DOWNLOAD_DIR="$(touch /tmp/not-run)"\n')
    with pytest.raises(ValueError):
        discover_scopes('failed_downloads', tmp_path / 'data', system='linux', home=tmp_path)


@pytest.mark.parametrize('record,active,complete', [
    ({'name': 'python.exe', 'cmdline': ['python', 'launch.py'], 'exe': 'D:/AngeronaSuite/venv/python.exe',
      'cwd': 'D:/AngeronaSuite'}, True, True),
    ({'name': 'python.exe', 'cmdline': ['python', '-m', 'optimus'], 'exe': 'D:/AngeronaSuite/venv/python.exe',
      'cwd': 'D:/Optimus/source'}, False, True),
    ({'name': 'python.exe', 'cmdline': ['python', 'launch.py'], 'exe': 'D:/AngeronaSuite/venv/python.exe',
      'cwd': None}, False, False),
    ({'name': 'python.exe', 'cmdline': None, 'exe': None, 'cwd': None}, False, False),
    ({'name': 'AngeronaStartup.exe', 'cmdline': [], 'exe': None, 'cwd': None}, True, True),
    ({'name': 'powershell.exe', 'cmdline': ['powershell', '-File', 'D:/AngeronaSuite/start-angerona.ps1']}, True, True),
])
def test_process_attribution_preserves_source_launchers_without_confusing_shared_interpreter(monkeypatch, record, active, complete):
    monkeypatch.setattr('optimus.retention_cleanup.psutil.process_iter',
                        lambda *_args, **_kw: iter([SimpleNamespace(info=record)]))
    result = process_state('angerona_logs')
    assert result['active'] is active and result['complete'] is complete


def test_native_browser_helpers_block_incomplete_download_cleanup(monkeypatch):
    monkeypatch.setattr('optimus.retention_cleanup.psutil.process_iter', lambda *_args, **_kw:
                        iter([SimpleNamespace(info={'name': 'Google Chrome Helper (Renderer)'})]))
    monkeypatch.setattr('optimus.browser_paths.sys.platform', 'darwin')
    assert process_state('failed_downloads')['active'] is True


def test_empty_inventory_is_not_proof_that_programs_are_closed(monkeypatch):
    monkeypatch.setattr('optimus.retention_cleanup.psutil.process_iter', lambda *_args, **_kw: iter([]))
    assert process_state('angerona_logs')['complete'] is False
    assert process_state('failed_downloads')['complete'] is False


def test_windows_angerona_roots_use_native_shell_and_exact_disposable_names(tmp_path, monkeypatch):
    native = tmp_path / 'NativeLocal'
    untrusted = tmp_path / 'UntrustedEnvironment'
    monkeypatch.setenv('LOCALAPPDATA', str(untrusted))
    monkeypatch.setattr('optimus.retention_cleanup._known_folder', lambda _: native)
    monkeypatch.setattr('optimus.retention_cleanup._read_authority', lambda _: None)
    scopes = discover_scopes('angerona_logs', tmp_path / 'Optimus/Data', system='win32')
    assert any(scope.root == native / 'Angerona/SourceData/logs' for scope in scopes)
    assert all('shared_logs' not in scope.root.parts and 'Recovery' not in scope.root.parts for scope in scopes)
    assert all(not str(scope.root).startswith(str(untrusted)) for scope in scopes)
    assert all(scope.names and not scope.suffixes for scope in scopes)
    assert all('runtime_alerts.log' not in scope.names and 'audit.log' not in scope.names for scope in scopes)


def test_owner_bound_angerona_authority_selects_only_verified_sibling_profile(tmp_path, monkeypatch):
    installation = tmp_path / 'AngeronaCleanup'
    installation.mkdir()
    repository = tmp_path / 'AngeronaSuite'
    marker = repository / 'src/angerona/app.py'
    marker.parent.mkdir(parents=True)
    marker.write_text('# disposable authority fixture')
    path = installation / 'scope-authority.json'
    record = {'schema': 1, 'installation_id': 'a' * 32, 'owner_sid': 'fixture-owner',
              'install_directory': str(installation), 'repository_root': str(repository)}
    path.write_text(json.dumps(record))
    monkeypatch.setattr('optimus.speed_enhancements._current_owner', lambda: 'sid:fixture-owner')
    assert _read_authority(path) == tmp_path / 'AngeronaData'
    path.write_text(json.dumps(dict(record, owner_sid='another-user')))
    with pytest.raises(ValueError):
        _read_authority(path)
    path.write_text(json.dumps(dict(record, repository_root=str(tmp_path / 'UnrelatedApp'))))
    with pytest.raises(ValueError):
        _read_authority(path)


def test_missing_authority_source_marker_does_not_expand_log_scopes(tmp_path, monkeypatch):
    installation = tmp_path / 'AngeronaCleanup'
    installation.mkdir()
    path = installation / 'scope-authority.json'
    path.write_text(json.dumps({'schema': 1, 'installation_id': 'a' * 32, 'owner_sid': 'fixture-owner',
        'install_directory': str(installation), 'repository_root': str(tmp_path / 'AngeronaSuite')}))
    monkeypatch.setattr('optimus.speed_enhancements._current_owner', lambda: 'sid:fixture-owner')
    with pytest.raises(ValueError):
        _read_authority(path)


def test_early_space_guard_precedes_cleanup_and_installer_release():
    rows = [(name, None) for name in ('Audit', 'Check drive space buffers', 'Clean temporary files', WINDOWS_SOFTWARE,
                                     'Capture final resource report', 'Recheck drive space buffers', 'Verify optimization outcomes')]
    planned, operations, release = parallel_installer_stages(rows, (WINDOWS_SOFTWARE,))
    names = [row[0] for row in planned]
    assert names.index('Check drive space buffers') < names.index('Clean temporary files')
    assert release == 'Clean temporary files' and len(operations) == 1


def test_workflow_guards_are_persistent_and_retention_facts_join_common_totals(tmp_path):
    from test_workflow import MockBackend
    backend = MockBackend(tmp_path)
    calls = []
    def guard(config):
        calls.append('guard')
        return {'schema': 1, 'review_only': True, 'status': 'needs_review', 'complete': True,
                'volumes': [{'name': 'C:', 'free_bytes': 100, 'target_bytes': 200, 'shortfall_bytes': 100, 'below_target': True}],
                'warnings': ['Low temporary-space buffer on C:'], 'summary': 'Low temporary-space buffer on C:'}
    backend.space_guard_check = guard
    def retained(kind, config, **options):
        calls.append(kind)
        return {'cleanup_id': kind, 'deleted_count': 1, 'deleted_bytes': 20, 'failed_items': 0}
    backend.retention_cleanup = retained
    backend.codex_archive_cleanup = lambda config, **options: retained('codex_archives', config, **options)
    configuration = settings(codex_archives_cleanup_enabled=True, angerona_logs_cleanup_enabled=True,
                             cleanup_enabled=False, browser_cache_enabled=False)
    events = []
    report = Workflow(backend).run(configuration, events.append)
    assert calls[0] == calls[-1] == 'guard'
    assert report['resource_totals']['removed_bytes'] == 60
    assert report['resource_totals']['removed_files'] == 3
    assert report['space_guard']['warnings'] == ['Low temporary-space buffer on C:']
    assert events[-1]['space_guard'] == report['space_guard']
    assert report['initial_space_guard'] == report['space_guard']


def test_native_workflow_uses_same_retention_hooks_and_no_fixture_host_guard(tmp_path):
    from test_workflow import MockBackend
    backend = MockBackend(tmp_path)
    backend.system = 'linux'
    backend.preference_store = SimpleNamespace(validate=Preferences.validate)
    seen = []
    def retained(kind, config, **options):
        seen.append(kind)
        return {'cleanup_id': kind, 'deleted_count': 1, 'deleted_bytes': 5, 'failed_items': 0}
    backend.retention_cleanup = retained
    backend.codex_archive_cleanup = lambda config, **options: retained('codex_archives', config, **options)
    configuration = settings(codex_archives_cleanup_enabled=True, angerona_logs_cleanup_enabled=True,
                             cleanup_enabled=False, browser_cache_enabled=False)
    report = NativeWorkflow(backend).run(configuration, lambda _: None)
    assert seen == ['codex_archives', 'angerona_logs', 'failed_downloads']
    assert report['resource_totals']['removed_bytes'] == 15
    assert report['space_guard']['status'] == 'unsupported'
