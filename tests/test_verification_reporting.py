"""Failure-injection checks: truthful partial scans, workflow outcomes and versions."""
import copy
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock,patch
import pytest
from optimus import __version__
from optimus.backend import Backend
from optimus.cleaner import Category,Cleaner
from optimus.ui import DetailedSettingsWindow
from test_workflow import MockBackend,workflow_module


def test_directory_access_failure_cannot_claim_complete(tmp_path):
    root=tmp_path/'cache';root.mkdir()
    cleaner=Cleaner(tmp_path/'data',[Category('fixture','Fixture cache',root,'Disposable fixture')])
    with patch('optimus.cleaner.os.scandir',side_effect=PermissionError(13,'Access denied')):
        plan=cleaner.scan(['fixture'])
    assert plan['complete'] is False
    assert len(plan['errors']) == 1
    assert plan['files'] == []


def test_entry_access_failure_is_recorded_instead_of_silently_complete(tmp_path):
    root=tmp_path/'cache';root.mkdir()
    cleaner=Cleaner(tmp_path/'data',[Category('fixture','Fixture cache',root,'Disposable fixture')])
    entry=Mock(path=str(root/'unreadable.fixture'))
    entry.stat.side_effect=PermissionError(13,'Access denied')
    context=Mock()
    context.__enter__=Mock(return_value=[entry]);context.__exit__=Mock(return_value=False)
    with patch('optimus.cleaner.os.scandir',return_value=context):
        plan=cleaner.scan(['fixture'])
    assert plan['complete'] is False
    assert len(plan['errors']) == 1
    assert 'unreadable.fixture' in plan['errors'][0]


@pytest.mark.parametrize('complete,errors,files',[(False,[],[]),(False,['Access denied'],[]),(True,['Access denied'],[]),(False,['Access denied'],[{'path':'verified-mock-file'}])])
def test_incomplete_cleanup_cannot_report_success(tmp_path,complete,errors,files):
    backend=MockBackend(tmp_path)
    backend.cleanup_scan=Mock(return_value={'files':files,'total_bytes':100 if files else 0,'complete':complete,'skipped':[],'errors':errors})
    backend.cleanup_apply=Mock(return_value={'deleted_count':len(files),'deleted_bytes':100 if files else 0,'skipped_count':0})
    config=copy.deepcopy(workflow_module.DEFAULTS);config['browser_cache_enabled']=False
    report=workflow_module.Workflow(backend).run(config,lambda _:None)
    stage=next(stage for stage in report['stages'] if stage['title']=='Clean old temporary files')
    assert stage['status']=='failed'
    assert stage['result']['partial'] is True
    assert stage['result']['preview_summary']['errors']==errors
    assert report['status']=='completed_with_errors'
    assert backend.cleanup_apply.call_count==bool(files)
    stored=json.loads(Path(report['log_path']).read_text())
    assert stored['stages'][1]['result']['partial'] is True


def test_cleaner_ui_explains_errors_even_for_older_complete_payload():
    window=SimpleNamespace(clean_files=Mock(),clean_summary=Mock(),clean_apply=Mock())
    DetailedSettingsWindow._cleanup_ready(window,{'files':[],'total_mb':0,'complete':True,'errors':['Access denied'],'skipped':[]})
    text=window.clean_summary.setText.call_args.args[0]
    assert 'Partial scan' in text
    assert '1 scan errors' in text
    window.clean_apply.setEnabled.assert_called_once_with(False)


def test_audit_separates_app_version_from_legacy_engine_version():
    backend=Backend.__new__(Backend)
    original={'ToolVersion':'1.0.0','System':{'fixture':True}}
    backend.bridge=Mock();backend.bridge.request.return_value=original
    report=backend.audit()
    assert report['ToolVersion']==__version__
    assert report['ApplicationVersion']==__version__
    assert report['EngineVersion']=='1.0.0'
    assert original['ToolVersion']=='1.0.0'


@pytest.mark.parametrize('invalid',[None,[],False])
def test_invalid_native_audit_is_not_reported_success(invalid):
    backend=Backend.__new__(Backend);backend.bridge=Mock();backend.bridge.request.return_value=invalid
    with pytest.raises(RuntimeError,match='no usable report'):
        backend.audit()
