"""Portable installation locations use ordinary owned fixture folders only."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
from types import SimpleNamespace

import pytest

from optimus import filesystem
from optimus.source_workspace import SourceWorkspace


def config(installation, directory, **changes):
    installation.mkdir(parents=True,exist_ok=True)
    value=dict(schema=1,data_dir=str(directory));value.update(changes)
    path=installation/filesystem.LOCATION_FILE
    path.write_text(json.dumps(value),encoding='utf-8')
    return path


def test_configured_data_folder_is_canonical_and_created_only_by_its_consumer(tmp_path,monkeypatch):
    installation=tmp_path/'App';data=tmp_path/'Data';legacy=tmp_path/'Legacy'
    monkeypatch.setenv('LOCALAPPDATA',str(legacy))
    config(installation,data)
    assert filesystem.default_data_root(installation)==data.resolve()
    assert not data.exists() and not legacy.exists()


def test_absent_config_keeps_legacy_default_without_creating_it(tmp_path,monkeypatch):
    installation=tmp_path/'App';installation.mkdir()
    legacy=tmp_path/'Legacy';monkeypatch.setenv('LOCALAPPDATA',str(legacy))
    assert filesystem.default_data_root(installation)==(legacy/'Optimus').resolve()
    assert not legacy.exists()


@pytest.mark.parametrize('contents',[None,[],{}, {'schema':True,'data_dir':'D:\\Data'},
    {'schema':2,'data_dir':'D:\\Data'},{'schema':1,'data_dir':'D:\\Data','unknown':True},
    '{broken','x'*4097])
def test_present_invalid_config_never_silently_falls_back(tmp_path,monkeypatch,contents):
    installation=tmp_path/'App';installation.mkdir()
    path=installation/filesystem.LOCATION_FILE
    path.write_text(contents if isinstance(contents,str) else json.dumps(contents))
    legacy=tmp_path/'Legacy';monkeypatch.setenv('LOCALAPPDATA',str(legacy))
    with pytest.raises(ValueError):filesystem.default_data_root(installation)
    assert not legacy.exists()


@pytest.mark.parametrize('invalid',['relative-data','D:relative','D:\\',r'\\server\share\Data',
    r'\\?\D:\Data',r'D:\Data:stream',r'D:\Data\..\Outside',r'D:\Data.\Child',
    r'D:\Data\%USERPROFILE%',r'D:\Data\*.json',None])
def test_configured_data_path_requires_concrete_ordinary_local_drive(tmp_path,invalid):
    installation=tmp_path/'App'
    config(installation,tmp_path/'Data',data_dir=invalid)
    with pytest.raises(ValueError):filesystem.default_data_root(installation)


def test_hardlinked_config_is_refused(tmp_path):
    installation=tmp_path/'App';installation.mkdir()
    original=tmp_path/'ordinary.json';original.write_text(json.dumps({'schema':1,'data_dir':str(tmp_path/'Data')}))
    os.link(original,installation/filesystem.LOCATION_FILE)
    with pytest.raises(ValueError,match='hardlinks'):filesystem.default_data_root(installation)


@pytest.mark.parametrize('kind',['config','install','data'])
def test_reparse_installation_config_or_data_is_refused(tmp_path,monkeypatch,kind):
    installation=tmp_path/'App';data=tmp_path/'Data';data.mkdir()
    path=config(installation,data)
    target={'config':path,'install':installation,'data':data}[kind]
    previous=Path.lstat
    def lstat(item,*args,**kwargs):
        info=previous(item,*args,**kwargs)
        return SimpleNamespace(st_mode=info.st_mode,st_nlink=info.st_nlink,st_dev=info.st_dev,
            st_ino=info.st_ino,st_size=info.st_size,st_file_attributes=0x400) if item==target else info
    monkeypatch.setattr(Path,'lstat',lstat)
    with pytest.raises(ValueError,match='Indirect'):filesystem.default_data_root(installation)


def test_frozen_default_uses_executable_installation_not_editable_source(tmp_path,monkeypatch):
    installation=tmp_path/'App';data=tmp_path/'Data'
    config(installation,data)
    monkeypatch.setattr(filesystem.sys,'frozen',True,raising=False)
    monkeypatch.setattr(filesystem.sys,'executable',str(installation/'Optimus.exe'))
    monkeypatch.setenv('OPTIMUS_ORIGINAL_SOURCE',str(tmp_path/'Unrelated'))
    assert filesystem.default_data_root()==data.resolve()


def test_source_restart_reads_original_portable_installation_configuration(tmp_path,monkeypatch):
    installation=tmp_path/'App';source=installation/'source';source.mkdir(parents=True)
    data=tmp_path/'Data';config(installation,data)
    monkeypatch.setattr(filesystem.sys,'frozen',False,raising=False)
    monkeypatch.setenv('OPTIMUS_ORIGINAL_SOURCE',str(source))
    assert filesystem.default_data_root()==data.resolve()
    (source/'src/optimus').mkdir(parents=True)
    (source/'src/optimus/app.py').write_text('VALUE = 1\n')
    workspace=SourceWorkspace(source)
    assert workspace.root==data.resolve()/'Sandbox/workspace'
    assert workspace.read('src/optimus/app.py')=='VALUE = 1\n'


def test_explicit_source_workspace_root_is_preserved(tmp_path,monkeypatch):
    source=tmp_path/'source';source.mkdir();(source/'fixture.py').write_text('VALUE=1\n')
    monkeypatch.setenv('OPTIMUS_ORIGINAL_SOURCE',str(tmp_path/'missing-installation'))
    explicit=tmp_path/'ExplicitData'
    workspace=SourceWorkspace(source,explicit)
    assert workspace.root==explicit.resolve()/'Sandbox/workspace'


@pytest.mark.parametrize('frozen',[False,True])
def test_launch_gate_uses_configured_data_without_importing_bundled_package(tmp_path,frozen):
    project=Path(__file__).parents[1]
    installation=tmp_path/'App';source=installation/'source';source.mkdir(parents=True)
    launcher=(installation if frozen else source)/'launch.py'
    shutil.copyfile(project/'launch.py',launcher)
    helper=(installation/'_internal/optimus' if frozen else source/'src/optimus')
    helper.mkdir(parents=True)
    shutil.copyfile(project/'src/optimus/filesystem.py',helper/'filesystem.py')
    (helper/'__init__.py').write_text('raise RuntimeError("PREMATURE_PACKAGE_IMPORT")\n')
    data=tmp_path/'Data';config(installation,data)
    workspace=data/'Sandbox/workspace';edited=workspace/'src/optimus';edited.mkdir(parents=True)
    (edited/'__init__.py').write_text('')
    (edited/'app.py').write_text('def main():\n    print("CONFIGURED_D_DATA_WORKSPACE")\n    return 0\n')
    runner=tmp_path/'runner.py'
    runner.write_text('''import os,runpy,sys
from pathlib import Path
launcher=Path(sys.argv[1]);workspace=Path(sys.argv[2]);installation=Path(sys.argv[3]);frozen=sys.argv[4]=='1'
os.environ['OPTIMUS_ORIGINAL_SOURCE']=str(installation/'source')
if frozen:sys.frozen=True;sys._MEIPASS=str(installation/'_internal');sys.executable=str(installation/'Optimus.exe')
sys.argv=[str(launcher),'--source-workspace',str(workspace)]
runpy.run_path(str(launcher),run_name='__main__')
''')
    result=subprocess.run([sys.executable,'-I',str(runner),str(launcher),str(workspace),str(installation),'1' if frozen else '0'],
        env=dict(os.environ,LOCALAPPDATA=str(tmp_path/'WrongLegacyLocation')),capture_output=True,text=True,timeout=30)
    assert result.returncode==0,result.stdout+result.stderr
    assert result.stdout.strip()=='CONFIGURED_D_DATA_WORKSPACE'
