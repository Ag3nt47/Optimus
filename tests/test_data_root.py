"""MSIX merged directory authority uses ordinary-file fixtures, never user data."""
from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
from types import SimpleNamespace

import pytest

from optimus.filesystem import resolve_data_root
from optimus.source_workspace import SourceWorkspace


@pytest.fixture
def merged(tmp_path, monkeypatch):
    logical = tmp_path / 'logical' / 'Optimus'
    physical = tmp_path / 'physical' / 'Optimus'
    logical.mkdir(parents=True)
    physical.mkdir(parents=True)
    anchor = physical / 'preferences.json'
    anchor.write_text('{"cleanup_enabled": false}', encoding='utf-8')
    original_resolve, original_lstat = Path.resolve, Path.lstat
    def resolve(path, *args, **kwargs):
        if path == logical:
            return logical
        if path == logical / 'preferences.json':
            assert kwargs.get('strict') is True
            return anchor
        return original_resolve(path, *args, **kwargs)
    def lstat(path, *args, **kwargs):
        if path == logical / 'preferences.json':
            return original_lstat(anchor, *args, **kwargs)
        return original_lstat(path, *args, **kwargs)
    monkeypatch.setattr(Path, 'resolve', resolve)
    monkeypatch.setattr(Path, 'lstat', lstat)
    return logical, physical


def test_existing_ordinary_preferences_override_self_resolving_merged_directory(merged):
    logical, physical = merged
    assert logical.resolve() == logical
    assert resolve_data_root(logical) == physical
    assert not (logical / 'Sandbox').exists()


def test_missing_anchor_falls_back_without_creating_any_data(tmp_path):
    root = tmp_path / 'not-created' / 'Optimus'
    assert resolve_data_root(root) == root.resolve()
    assert not root.exists()


def test_backend_reads_and_saves_only_the_authoritative_directory(merged):
    from optimus.backend import Backend
    logical, physical = merged
    backend = Backend(logical)
    try:
        assert backend.data_dir == physical
        assert backend.bridge.data_dir == physical / 'engine'
        assert backend.preference_store.path == physical / 'preferences.json'
        assert backend.preferences()['cleanup_enabled'] is False
        backend.save_preferences({'cleanup_enabled': False, 'browser_cache_enabled': False})
        assert json.loads((physical / 'preferences.json').read_text())['browser_cache_enabled'] is False
        assert not (logical / 'preferences.json').exists()
    finally:
        backend.close()


def test_workspace_seeds_reads_saves_and_preserves_existing_physical_edits(merged, tmp_path):
    logical, physical = merged
    source = tmp_path / 'source'
    (source / 'src' / 'optimus').mkdir(parents=True)
    (source / 'src' / 'optimus' / 'app.py').write_text('VALUE = 1\n')
    existing = physical / 'Sandbox' / 'workspace' / 'README.md'
    existing.parent.mkdir(parents=True)
    existing.write_text('Preserve this physical edit.\n')
    workspace = SourceWorkspace(source, logical)
    assert workspace.root == physical / 'Sandbox' / 'workspace'
    assert workspace.read('README.md') == 'Preserve this physical edit.\n'
    assert workspace.read('src/optimus/app.py') == 'VALUE = 1\n'
    workspace.save('src/optimus/app.py', 'VALUE = 2\n')
    assert workspace.read('src/optimus/app.py') == 'VALUE = 2\n'
    assert workspace.versions('src/optimus/app.py')
    assert not (logical / 'Sandbox').exists()
    with pytest.raises(ValueError, match='outside'):
        workspace._path('../outside.py')


@pytest.mark.parametrize('target', ('logical-root', 'logical-parent', 'logical-anchor', 'physical-root', 'physical-parent', 'physical-anchor'))
def test_reparse_points_on_both_sides_of_anchor_are_refused(merged, monkeypatch, target):
    logical, physical = merged
    paths = {'logical-root': logical, 'logical-parent': logical.parent,
             'logical-anchor': logical / 'preferences.json', 'physical-root': physical,
             'physical-parent': physical.parent, 'physical-anchor': physical / 'preferences.json'}
    selected = paths[target]
    previous = Path.lstat
    def lstat(path, *args, **kwargs):
        info = previous(path, *args, **kwargs)
        if path == selected:
            return SimpleNamespace(st_mode=info.st_mode, st_nlink=info.st_nlink, st_dev=info.st_dev,
                                   st_ino=info.st_ino, st_file_attributes=0x400)
        return info
    monkeypatch.setattr(Path, 'lstat', lstat)
    with pytest.raises(ValueError, match='Indirect'):
        resolve_data_root(logical)


def test_hardlinked_preferences_cannot_become_an_authority(tmp_path):
    root = tmp_path / 'Optimus'; root.mkdir()
    original = tmp_path / 'ordinary-preferences.json'; original.write_text('{}')
    try:
        os.link(original, root / 'preferences.json')
    except OSError as error:
        pytest.skip('Fixture hardlink unavailable: ' + str(error))
    with pytest.raises(ValueError, match='hardlinks'):
        resolve_data_root(root)


def test_anchor_directory_and_permission_failure_fail_closed(tmp_path, monkeypatch):
    root = tmp_path / 'Optimus'; root.mkdir()
    anchor = root / 'preferences.json'; anchor.mkdir()
    with pytest.raises(ValueError, match='ordinary file'):
        resolve_data_root(root)
    anchor.rmdir()
    previous = Path.lstat
    def lstat(path, *args, **kwargs):
        if path == anchor:
            raise PermissionError('Fixture anchor access failure')
        return previous(path, *args, **kwargs)
    monkeypatch.setattr(Path, 'lstat', lstat)
    with pytest.raises(PermissionError):
        resolve_data_root(root)


def test_anchor_identity_change_is_refused(merged, monkeypatch):
    logical, physical = merged
    previous = Path.lstat
    def lstat(path, *args, **kwargs):
        info = previous(path, *args, **kwargs)
        if path == physical / 'preferences.json':
            return SimpleNamespace(st_mode=info.st_mode, st_nlink=info.st_nlink,
                                   st_dev=info.st_dev, st_ino=info.st_ino + 1,
                                   st_file_attributes=0)
        return info
    monkeypatch.setattr(Path, 'lstat', lstat)
    with pytest.raises(ValueError, match='changed'):
        resolve_data_root(logical)


@pytest.mark.parametrize('frozen', (False, True))
def test_launch_gate_uses_anchor_and_executes_distinct_edited_app(tmp_path, frozen):
    project = Path(__file__).parents[1]
    baseline = tmp_path / 'baseline'
    baseline.mkdir()
    shutil.copyfile(project / 'launch.py', baseline / 'launch.py')
    shipped = baseline / ('_internal/optimus' if frozen else 'src/optimus')
    shipped.mkdir(parents=True)
    shutil.copyfile(project / 'src/optimus/filesystem.py', shipped / 'filesystem.py')
    # A premature normal package import must fail instead of executing edited code.
    (shipped / '__init__.py').write_text('raise RuntimeError("PREMATURE BUNDLED PACKAGE IMPORT")\n')
    logical = tmp_path / 'logical' / 'Optimus'; logical.mkdir(parents=True)
    physical = tmp_path / 'physical' / 'Optimus'; physical.mkdir(parents=True)
    (physical / 'preferences.json').write_text('{}')
    workspace = physical / 'Sandbox/workspace'
    edited = workspace / 'src/optimus'; edited.mkdir(parents=True)
    (edited / '__init__.py').write_text('')
    (edited / 'app.py').write_text('def main():\n    print("DISTINCT_EDITED_APP_EXECUTED")\n    return 0\n')
    runner = tmp_path / 'runner.py'
    runner.write_text('''import os,runpy,sys
from pathlib import Path
logical=Path(sys.argv[1]);physical=Path(sys.argv[2]);launcher=Path(sys.argv[3]);workspace=Path(sys.argv[4]);frozen=sys.argv[5]=='1'
original_resolve=Path.resolve;original_lstat=Path.lstat
def resolve(path,*args,**kwargs):
    if path==logical:return logical
    if path==logical/'preferences.json':return physical/'preferences.json'
    return original_resolve(path,*args,**kwargs)
def lstat(path,*args,**kwargs):
    return original_lstat(physical/'preferences.json' if path==logical/'preferences.json' else path,*args,**kwargs)
Path.resolve=resolve;Path.lstat=lstat
os.environ['LOCALAPPDATA']=str(logical.parent)
if frozen:sys.frozen=True;sys._MEIPASS=str(launcher.parent/'_internal')
sys.path.insert(0,str(launcher.parent/('' if frozen else 'src')))
sys.argv=[str(launcher),'--source-workspace',str(workspace)]
runpy.run_path(str(launcher),run_name='__main__')
''', encoding='utf-8')
    result = subprocess.run([sys.executable, '-I', str(runner), str(logical), str(physical),
                             str(baseline / 'launch.py'), str(workspace), '1' if frozen else '0'],
                            capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout.strip() == 'DISTINCT_EDITED_APP_EXECUTED'


@pytest.mark.parametrize('placement', ('Sandbox', 'workspace'))
def test_launch_gate_rejects_junction_before_allowed_workspace_resolution(tmp_path, placement):
    from test_source_workspace import make_junction
    project = Path(__file__).parents[1]
    baseline = tmp_path / 'baseline'; baseline.mkdir()
    shutil.copyfile(project / 'launch.py', baseline / 'launch.py')
    helper = baseline / 'src/optimus/filesystem.py'; helper.parent.mkdir(parents=True)
    shutil.copyfile(project / 'src/optimus/filesystem.py', helper)
    local = tmp_path / 'local'; data = local / 'Optimus'; data.mkdir(parents=True)
    (data / 'preferences.json').write_text('{}')
    outside = tmp_path / 'outside'; outside.mkdir()
    if placement == 'Sandbox':
        make_junction(data / 'Sandbox', outside)
        edited = outside / 'workspace/src/optimus'
    else:
        (data / 'Sandbox').mkdir()
        make_junction(data / 'Sandbox/workspace', outside)
        edited = outside / 'src/optimus'
    edited.mkdir(parents=True)
    (edited / '__init__.py').write_text('')
    (edited / 'app.py').write_text('def main():\n    print("UNAPPROVED_JUNCTION_EXECUTED")\n    return 0\n')
    result = subprocess.run([sys.executable, '-I', str(baseline / 'launch.py'),
        '--source-workspace', str(data / 'Sandbox/workspace')],
        env=dict(os.environ, LOCALAPPDATA=str(local)), capture_output=True, text=True, timeout=30)
    assert result.returncode != 0
    assert 'UNAPPROVED_JUNCTION_EXECUTED' not in result.stdout
    assert 'could not be verified' in result.stderr
