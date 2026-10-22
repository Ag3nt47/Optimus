"""Source Sandbox regressions. All edits and links stay inside tmp_path fixtures."""
from __future__ import annotations
import os
import subprocess
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
import pytest
from optimus.source_workspace import SourceWorkspace


@pytest.fixture
def project(tmp_path):
    source = tmp_path / 'source'
    (source / 'src' / 'optimus').mkdir(parents=True)
    (source / 'src' / 'optimus' / 'ui.py').write_text('VALUE = 1\n', encoding='utf-8')
    (source / 'launch.py').write_text('# Fixture launcher; never executed.\n', encoding='utf-8')
    (source / 'README.md').write_text('Fixture documentation.\n', encoding='utf-8')
    (source / 'LICENSE').write_text('Fixture license.\n', encoding='utf-8')
    return source, tmp_path / 'data'


@pytest.fixture
def workspace(project):
    return SourceWorkspace(*project)


def make_junction(link: Path, target: Path):
    """Create a directory alias only between paths in one pytest fixture root."""
    assert link.parent.resolve().is_relative_to(target.parent.parent.resolve()) or target.parent.resolve().is_relative_to(link.parent.parent.resolve())
    if os.name != 'nt':
        link.symlink_to(target, target_is_directory=True)
        return
    completed = subprocess.run(['cmd.exe', '/d', '/c', 'mklink', '/J', str(link), str(target)], capture_output=True, text=True)
    if completed.returncode:
        pytest.skip('Directory junction creation unavailable: ' + completed.stderr)


def test_seed_is_a_separate_working_copy(project, workspace):
    source, _ = project
    assert workspace.read('src/optimus/ui.py') == 'VALUE = 1\n'
    assert workspace.root != source
    assert workspace.root.is_dir()
    assert (source / 'src/optimus/ui.py').read_text() == 'VALUE = 1\n'


def test_file_names_are_forward_slash_relative_paths(workspace):
    names = workspace.files()
    assert 'src/optimus/ui.py' in names
    assert all('\\' not in name and not Path(name).is_absolute() for name in names)


def test_powershell_modules_survive_edit_history_restore_and_export(project):
    source, data = project
    module = source / 'tools' / 'angerona-cleanup' / 'Fixture.psm1'
    module.parent.mkdir(parents=True)
    original = 'throw "Fixture source must never execute"\n'
    module.write_text(original, encoding='utf-8')
    workspace = SourceWorkspace(source, data)
    relative = 'tools/angerona-cleanup/Fixture.psm1'
    assert relative in workspace.files()
    assert workspace.read(relative) == original
    workspace.save(relative, '# Edited module fixture\n')
    assert workspace.versions(relative)
    destination = data.parent / 'exported-modules'
    workspace.export(destination)
    assert (destination / relative).read_text() == '# Edited module fixture\n'
    assert workspace.restore_original(relative) == original
    assert module.read_text() == original


def test_seed_skips_private_build_reports_and_binary_files(project):
    source, data = project
    for name in ('private', 'reports', 'build', 'dist', '.git', '.venv'):
        (source / name).mkdir()
        (source / name / 'omit.py').write_text('SECRET_FIXTURE = True\n')
    (source / 'payload.exe').write_bytes(b'fixture-binary')
    result = SourceWorkspace(source, data).files()
    assert not any('omit.py' in item or item.endswith('.exe') for item in result)
    assert 'LICENSE' in result


def test_source_text_is_never_executed_during_seed_or_save(project):
    source, data = project
    (source / 'src/optimus/ui.py').write_text('raise RuntimeError("MUST NOT EXECUTE")\n')
    workspace = SourceWorkspace(source, data)
    workspace.save('src/optimus/ui.py', 'raise RuntimeError("STILL NOT EXECUTED")\n')
    assert 'STILL NOT EXECUTED' in workspace.read('src/optimus/ui.py')


def test_existing_edits_survive_reopening(project, workspace):
    workspace.save('src/optimus/ui.py', 'VALUE = 2\n')
    reopened = SourceWorkspace(*project)
    assert reopened.read('src/optimus/ui.py') == 'VALUE = 2\n'


def test_save_retains_previous_text_and_does_not_modify_release(project, workspace):
    source, _ = project
    result = workspace.save('src/optimus/ui.py', 'VALUE = 2\n')
    assert result['saved'] is True
    assert len(workspace.versions('src/optimus/ui.py')) == 1
    assert workspace.read('src/optimus/ui.py') == 'VALUE = 2\n'
    assert (source / 'src/optimus/ui.py').read_text() == 'VALUE = 1\n'


def test_invalid_python_is_refused_before_source_or_history_changes(workspace):
    before = workspace.read('src/optimus/ui.py')
    with pytest.raises(SyntaxError):
        workspace.save('src/optimus/ui.py', 'def broken(:\n')
    assert workspace.read('src/optimus/ui.py') == before
    assert workspace.versions('src/optimus/ui.py') == []


def test_revert_restores_saved_previous_version(workspace):
    workspace.save('src/optimus/ui.py', 'VALUE = 2\n')
    previous = workspace.versions('src/optimus/ui.py')[0]
    assert workspace.revert('src/optimus/ui.py', previous) == 'VALUE = 1\n'
    assert len(workspace.versions('src/optimus/ui.py')) == 2


def test_original_version_restores_release_copy(project, workspace):
    workspace.save('src/optimus/ui.py', 'VALUE = 99\n')
    assert workspace.restore_original('src/optimus/ui.py') == 'VALUE = 1\n'
    assert workspace.read('src/optimus/ui.py') == 'VALUE = 1\n'


@pytest.mark.parametrize('revision', ['../escape.txt', '..\\escape.txt', 'missing.txt'])
def test_unknown_or_traversal_history_revision_is_refused(workspace, revision):
    workspace.save('src/optimus/ui.py', 'VALUE = 2\n')
    with pytest.raises(ValueError):
        workspace.revert('src/optimus/ui.py', revision)


@pytest.mark.parametrize('relative', ['../outside.txt', 'src/../../../outside.txt'])
def test_read_save_and_original_refuse_path_traversal(tmp_path, workspace, relative):
    outside = tmp_path / 'outside.txt'
    outside.write_text('outside-fixture')
    for action in (lambda: workspace.read(relative), lambda: workspace.save(relative, 'changed'), lambda: workspace.restore_original(relative)):
        with pytest.raises(ValueError):
            action()
    assert outside.read_text() == 'outside-fixture'


def test_absolute_outside_path_is_refused(tmp_path, workspace):
    outside = tmp_path / 'outside.py'
    outside.write_text('VALUE = 0\n')
    with pytest.raises(ValueError):
        workspace.save(str(outside), 'VALUE = 3\n')
    assert outside.read_text() == 'VALUE = 0\n'


@pytest.mark.skipif(os.name != 'nt', reason='Backslash traversal is a Windows path rule.')
def test_windows_backslash_traversal_is_refused(workspace):
    with pytest.raises(ValueError):
        workspace.read('..\\outside.txt')


def test_validate_all_reports_bad_fixture_without_executing(workspace):
    target = workspace.root / 'src/optimus/ui.py'
    target.write_text('def broken(:\n')
    errors = workspace.validate_all()
    assert errors and 'ui.py' in errors[0]


def test_version_retention_is_bounded(workspace):
    for index in range(54):
        workspace.save('src/optimus/ui.py', f'VALUE = {index}\n')
    assert len(workspace.versions('src/optimus/ui.py')) == 50


def test_export_copies_editable_source_into_empty_destination(tmp_path, workspace):
    destination = tmp_path / 'export'
    result = workspace.export(destination)
    assert result['files'] == len(workspace.files())
    assert (destination / 'src/optimus/ui.py').read_text() == 'VALUE = 1\n'
    with pytest.raises(ValueError):
        workspace.export(destination)


def test_source_nested_junction_is_not_seeded(tmp_path, project):
    source, data = project
    outside = tmp_path / 'outside'
    outside.mkdir()
    (outside / 'never_copy.py').write_text('VALUE = 0\n')
    make_junction(source / 'linked', outside)
    workspace = SourceWorkspace(source, data)
    assert not any('never_copy.py' in name for name in workspace.files())
    assert not (workspace.root / 'linked').exists()


def test_workspace_nested_junction_is_not_listed_or_read(tmp_path, workspace):
    outside = tmp_path / 'outside'
    outside.mkdir()
    target = outside / 'outside.py'
    target.write_text('VALUE = 0\n')
    make_junction(workspace.root / 'linked', outside)
    assert not any('outside.py' in name for name in workspace.files())
    with pytest.raises(ValueError):
        workspace.read('linked/outside.py')
    with pytest.raises(ValueError):
        workspace.save('linked/outside.py', 'VALUE = 1\n')
    assert target.read_text() == 'VALUE = 0\n'


def test_seed_refuses_preexisting_destination_junction(tmp_path, project):
    source, data = project
    destination = data / 'Sandbox/workspace'
    destination.mkdir(parents=True)
    outside = tmp_path / 'outside'
    outside.mkdir()
    make_junction(destination / 'src', outside)
    with pytest.raises(ValueError):
        SourceWorkspace(source, data)
    assert not (outside / 'optimus/ui.py').exists()


def test_data_root_junction_is_refused(tmp_path, project):
    source, _ = project
    outside = tmp_path / 'outside'
    outside.mkdir()
    alias = tmp_path / 'data-alias'
    make_junction(alias, outside)
    with pytest.raises(ValueError):
        SourceWorkspace(source, alias)
    assert not (outside / 'Sandbox').exists()


def test_history_child_junction_is_refused_before_write(tmp_path, workspace):
    outside = tmp_path / 'outside'
    outside.mkdir()
    history_dir = workspace._history_dir('src/optimus/ui.py')
    make_junction(history_dir, outside)
    with pytest.raises(ValueError):
        workspace.save('src/optimus/ui.py', 'VALUE = 2\n')
    assert list(outside.iterdir()) == []
    assert workspace.read('src/optimus/ui.py') == 'VALUE = 1\n'


def test_source_editor_exposes_controls_and_saves_fixture_only(project, tmp_path, monkeypatch):
    pytest.importorskip('PySide6')
    from PySide6.QtWidgets import QApplication, QMessageBox, QPushButton
    from optimus.sandbox import SandboxWindow
    app = QApplication.instance() or QApplication([])
    source, data = project
    backend = SimpleNamespace(source_root=lambda: source, data_dir=data, workflow=SimpleNamespace(active=False))
    def refuse_warning(*args, **kwargs):
        raise AssertionError('Unexpected source editor warning: ' + str(args[-1] if args else kwargs))
    monkeypatch.setattr(QMessageBox, 'warning', refuse_warning)
    window = SandboxWindow(backend)
    try:
        labels = {button.text() for button in window.findChildren(QPushButton)}
        assert {'Save','Revert','Original version','History','Restart Optimus','Export project'} <= labels
        assert window.current == 'src/optimus/ui.py'
        assert window.editor.toPlainText() == 'VALUE = 1\n'
        window.editor.setPlainText('VALUE = 7\n')
        assert window.save() is True
        assert window.workspace.read('src/optimus/ui.py') == 'VALUE = 7\n'
        assert (source / 'src/optimus/ui.py').read_text() == 'VALUE = 1\n'
        assert window.restarting is False
        assert not hasattr(window, 'child')
    finally:
        window.editor.document().setModified(False)
        window.close()
        window.deleteLater()
        app.processEvents()


def test_binary_build_assets_seed_and_export_without_text_editing(project, tmp_path):
    source, data = project
    assets = source / 'src/optimus/assets'
    assets.mkdir()
    icon = b'\x00\x00\x01\x00fixture-icon'
    image = b'\x89PNG\r\nfixture-image'
    animation = b'GIF89a-fixture-loading-animation'
    (assets / 'optimus.ico').write_bytes(icon)
    (assets / 'loading.gif').write_bytes(animation)
    (source / 'docs').mkdir()
    (source / 'docs/optimus.png').write_bytes(image)
    (source / 'Build-Windows.bat').write_text('python -m PyInstaller --icon src/optimus/assets/optimus.ico launch.py\n', encoding='utf-8')
    workspace = SourceWorkspace(source, data)
    assert (workspace.root / 'src/optimus/assets/optimus.ico').read_bytes() == icon
    assert (workspace.root / 'src/optimus/assets/loading.gif').read_bytes() == animation
    assert (workspace.root / 'docs/optimus.png').read_bytes() == image
    assert not any(name.endswith(('.ico', '.png', '.gif')) for name in workspace.files())
    destination = tmp_path / 'exported'
    report = workspace.export(destination)
    assert (destination / 'src/optimus/assets/optimus.ico').read_bytes() == icon
    assert (destination / 'src/optimus/assets/loading.gif').read_bytes() == animation
    assert (destination / 'docs/optimus.png').read_bytes() == image
    assert 'src/optimus/assets/optimus.ico' in (destination / 'Build-Windows.bat').read_text()
    assert report['files'] == len(workspace.files()) + 3


def test_oversized_and_unsupported_binary_assets_are_not_seeded(project):
    from optimus.source_workspace import MAX_ASSET_BYTES
    source, data = project
    assets = source / 'src/optimus/assets'
    assets.mkdir()
    with (assets / 'oversized.png').open('wb') as stream:
        stream.truncate(MAX_ASSET_BYTES + 1)
    (assets / 'unsupported.exe').write_bytes(b'fixture')
    workspace = SourceWorkspace(source, data)
    assert not (workspace.root / 'src/optimus/assets/oversized.png').exists()
    assert not (workspace.root / 'src/optimus/assets/unsupported.exe').exists()


def test_binary_reparse_asset_is_never_seeded_or_exported(project, tmp_path):
    source, data = project
    assets = source / 'src/optimus/assets'
    assets.mkdir()
    (assets / 'safe.ico').write_bytes(b'fixture-icon')
    original_lstat = Path.lstat
    def synthetic_reparse(path, *args, **kwargs):
        result = original_lstat(path, *args, **kwargs)
        if path.name == 'redirect.png':
            return SimpleNamespace(st_file_attributes=0x400, st_mode=result.st_mode)
        return result
    (assets / 'redirect.png').write_bytes(b'private fixture')
    with patch.object(Path, 'lstat', synthetic_reparse):
        workspace = SourceWorkspace(source, data)
        assert not (workspace.root / 'src/optimus/assets/redirect.png').exists()
        (workspace.root / 'src/optimus/assets/redirect.png').write_bytes(b'private fixture')
        destination = tmp_path / 'exported'
        workspace.export(destination)
        assert not (destination / 'src/optimus/assets/redirect.png').exists()
        assert (destination / 'src/optimus/assets/safe.ico').read_bytes() == b'fixture-icon'
