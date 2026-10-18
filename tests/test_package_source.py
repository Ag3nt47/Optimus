"""Public-source packaging boundaries, using only disposable temp files."""
import importlib.util
from pathlib import Path
from types import SimpleNamespace
import pytest

spec = importlib.util.spec_from_file_location('optimus_package_source', Path(__file__).parents[1] / 'tools/package_source.py')
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)

@pytest.fixture
def project(tmp_path):
    source = tmp_path / 'project'
    (source/'src').mkdir(parents=True)
    (source/'src/app.py').write_text('VALUE = 1')
    (source/'README.md').write_text('Public fixture')
    (source/'private').mkdir()
    (source/'private/report.json').write_text('private fixture')
    (source/'src/.env').write_text('secret fixture')
    (source/'src/license.lic').write_text('private fixture')
    return source, tmp_path/'output'

def test_only_allowed_files_are_packaged(project):
    source, destination = project
    assert m.package(source, destination) == 2
    assert sorted(str(p.relative_to(destination)).replace('\\','/') for p in destination.rglob('*') if p.is_file()) == ['README.md','src/app.py']

def test_windows_reparse_attribute_is_indirect():
    path = SimpleNamespace(lstat=lambda: SimpleNamespace(st_file_attributes=0x400), is_symlink=lambda: False)
    assert m.indirect(path)
    path.lstat = lambda: SimpleNamespace(st_file_attributes=0)
    assert not m.indirect(path)

def test_indirect_nested_file_and_folder_are_excluded(project, monkeypatch):
    source, destination = project
    (source/'src/redirect').mkdir()
    (source/'src/redirect/private.py').write_text('private fixture')
    original = m.indirect
    monkeypatch.setattr(m,'indirect',lambda path: path in (source/'src/redirect', source/'src/app.py') or original(path))
    assert m.package(source,destination) == 1
    assert not (destination/'src/redirect').exists()
    assert not (destination/'src/app.py').exists()

def test_indirect_source_or_destination_ancestry_is_rejected(project, monkeypatch):
    source, destination = project
    original = m.indirect
    monkeypatch.setattr(m,'indirect',lambda path: path == destination.parent or original(path))
    with pytest.raises(ValueError,match='Indirect'):
        m.package(source,destination)
    assert not destination.exists()

def test_overlapping_destination_refused(project):
    source, _ = project
    with pytest.raises(ValueError,match='overlaps'):
        m.package(source,source/'src/output')
