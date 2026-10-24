import copy
from pathlib import Path

import pytest

from optimus.toolkit_configuration import DEFAULTS, launch_descriptions, validate_configuration


def test_default_toolkit_never_expands_automatic_scope():
    config=validate_configuration({})
    assert all(section['enabled'] is False for section in config.values())
    assert config['providers']['ids']==[] and not config['providers']['agreements_accepted']
    assert config['application_caches']['apps']==[]
    assert not config['recovery_retention']['lose_browser_undo_approved']
    assert not config['resume']['resident_handoff_enabled']
    assert launch_descriptions({})==([],[])


def test_default_objects_do_not_share_mutable_approval_scope():
    first=validate_configuration({});second=validate_configuration({})
    first['application_caches']['apps'].append('discord')
    first['diagnostics']['integrity_symptoms'].append('windows_update_errors')
    assert second==DEFAULTS


@pytest.mark.parametrize('group',list(DEFAULTS))
@pytest.mark.parametrize('enabled',[1,'true',None,[]])
def test_toolkit_enabled_values_require_actual_booleans(group,enabled):
    with pytest.raises(ValueError):validate_configuration({group:{'enabled':enabled}})


@pytest.mark.parametrize('value',[None,[],{'unknown':{}},{'history':{'unknown':1}},{'history':[]},{'providers':True}])
def test_unknown_toolkit_schema_refused(value):
    with pytest.raises(ValueError):validate_configuration(value)


@pytest.mark.parametrize('duration',[0,31,True,1.5,float('nan')])
def test_observation_duration_bound(duration):
    with pytest.raises(ValueError):validate_configuration({'history':{'sample_seconds':duration}})


@pytest.mark.parametrize('symptoms',[['windows_update_errors']*2,['unknown'],['system_file_errors',None],'system_file_errors'])
def test_exact_symptom_scope_only(symptoms):
    with pytest.raises(ValueError):validate_configuration({'diagnostics':{'integrity_symptoms':symptoms}})


@pytest.mark.parametrize('estimate',[0,-1,True,1.5,1024**4+1])
def test_installer_estimate_bounds(estimate):
    with pytest.raises(ValueError):validate_configuration({'installer_space':{'required_bytes':estimate}})


def test_known_and_unknown_installer_sizes_are_distinct(tmp_path):
    unknown=validate_configuration({'installer_space':{'enabled':True,'destination':str(tmp_path)}})
    known=validate_configuration({'installer_space':{'enabled':True,'destination':str(tmp_path),'required_bytes':1}})
    assert unknown['installer_space']['required_bytes'] is None
    assert known['installer_space']['required_bytes']==1


@pytest.mark.parametrize('path',['relative/app','../apps','\\\\server\\share\\apps','//server/share/apps','C:\\apps\x00bad'])
def test_installer_destination_must_be_local_absolute(path):
    with pytest.raises(ValueError):validate_configuration({'installer_space':{'destination':path}})


def test_storage_growth_accepts_exact_user_subfolder(tmp_path):
    config=validate_configuration({'storage_growth':{'enabled':True,'roots':[str(tmp_path/'selected')]}})
    assert config['storage_growth']['roots']==[str(tmp_path/'selected')]


@pytest.mark.parametrize('roots',[['relative'],['/'],['\\\\server\\share'],['/tmp/../private'],[]*17])
def test_invalid_growth_roots_are_refused(roots):
    if roots==[]:roots=['/tmp/example'+str(index) for index in range(17)]
    with pytest.raises(ValueError):validate_configuration({'storage_growth':{'roots':roots}})


def test_browser_undo_loss_needs_separate_approval():
    with pytest.raises(ValueError):validate_configuration({'recovery_retention':{'enabled':True,'browser_backups':True}})
    value=validate_configuration({'recovery_retention':{'enabled':True,'browser_backups':True,'lose_browser_undo_approved':True}})
    actions,checks=launch_descriptions({'toolkit_settings':value})
    assert any('undo will be lost' in item for item in actions)
    assert checks==[]


def test_selected_cache_scope_is_named_in_launch_warning():
    value=validate_configuration({'application_caches':{'enabled':True,'apps':['discord'],'min_age_days':12}})
    actions,checks=launch_descriptions({'toolkit_settings':value})
    assert any('discord' in item and '12 days' in item for item in actions)
    assert not checks


def test_checks_do_not_silently_approve_cleanup_or_repairs():
    value=validate_configuration({'history':{'enabled':True},'diagnostics':{'enabled':True,'integrity_symptoms':['system_file_errors']},'resume':{'enabled':True}})
    actions,checks=launch_descriptions({'toolkit_settings':value})
    assert not actions and len(checks)==3
    assert any('do not approve repairs' in item for item in checks)


def test_no_descriptions_modify_the_original_settings():
    value={'toolkit_settings':{'history':{'enabled':True}}};before=copy.deepcopy(value)
    launch_descriptions(value)
    assert value==before
