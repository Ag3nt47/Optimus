"""Approved startup/driver policies; host Windows mutations are always mocked."""
import copy
from threading import Event
from types import SimpleNamespace
from unittest.mock import Mock
import pytest
from optimus.automation import CURRENT_SETUP_VERSION
from optimus import automated_system as system


def startup(**changes):
    value={'Id':'Registry-'+'a'*64,'Name':'Optional fixture','Source':'Run registry (CurrentUser, Registry64)',
           'Command':r'"C:\Fixture\Optional.exe" --tray','Location':r'CurrentUser\Software\Microsoft\Windows\CurrentVersion\Run',
           'EntryType':'Registry','Fingerprint':'b'*64,'Hive':'CurrentUser','RegistryView':'Registry64',
           'RegistryPath':r'Software\Microsoft\Windows\CurrentVersion\Run','ValueName':'Optional fixture','ValueKind':'String',
           'RawValue':r'"C:\Fixture\Optional.exe" --tray','Enabled':True,'CanManage':True,'Protected':False,'BackupId':'','FilePath':''}
    value.update(changes);return value


def driver(**changes):
    value={'id':'00000000-0000-0000-0000-000000000001','revision':1,'title':'Fixture display driver',
           'hardwareId':r'PCI\VEN_1234&DEV_5678','provider':'Fixture provider','driverClass':'Display','driverDate':'2026-10-01T00:00:00Z',
           'canRequestUserInput':False,'rebootBehavior':2,'eulaAccepted':False}
    value.update(changes);return value


def backend():
    return SimpleNamespace(startup_list=Mock(return_value=[startup()]),startup_set=Mock(return_value={'Kind':'Startup'}),
           is_admin=Mock(return_value=True),driver_updates_scan=Mock(return_value={'updates':[driver()],'resultCode':2}),
           driver_backup_automatic=Mock(return_value={'exit_code':0,'destination':'fixture-backup'}),
           driver_update_apply_unattended=Mock(return_value={'resultCode':2,'hresult':0,'restartNeeded':False}))


def startup_config(**changes):
    value={'setup_version':CURRENT_SETUP_VERSION,'startup_review_enabled':True,'startup_policy':'disable_selected','startup_changes_approved':True,
           'startup_approved_entries':[system.startup_descriptor(startup())]}
    value.update(changes);return value


def driver_config(**changes):
    value={'setup_version':CURRENT_SETUP_VERSION,'driver_updates_enabled':True,'driver_policy':'install_approved','driver_changes_approved':True,
           'driver_update_agreements_accepted':True,'driver_approved_updates':[system.driver_descriptor(driver())],'driver_approved_devices':[]}
    value.update(changes);return value


def test_descriptors_are_copies_and_preserve_raw_startup_identity():
    original=startup();value=system.startup_descriptor(original)
    assert value['RawValue']==original['RawValue'] and value['Fingerprint']=='B'*64
    value['Name']='changed';assert original['Name']=='Optional fixture'


@pytest.mark.parametrize('changes',[{'Enabled':False},{'CanManage':False},{'Protected':True},{'Name':'Bing Wallpaper'}, {'Command':r'C:\Bing-Wallpaper\BingWallpaper.exe'},{'Fingerprint':'bad'},{'EntryType':'Unknown'}])
def test_invalid_or_protected_startup_is_not_approved(changes):
    with pytest.raises(ValueError):system.startup_descriptor(startup(**changes))


def test_approval_bounds_and_duplicate_identity():
    with pytest.raises(ValueError):system.validate_startup_approvals([startup()]*101)
    with pytest.raises(ValueError):system.validate_startup_approvals([startup(),startup()])
    with pytest.raises(ValueError):system.validate_driver_approvals([driver(),driver()])


def test_startup_disables_only_exact_fresh_snapshot_with_existing_backend():
    value=backend();events=[]
    result=system.run_startup(value,startup_config(),item_callback=events.append)
    value.startup_set.assert_called_once_with(value.startup_list.return_value[0],False)
    assert result['items'][0]['status']=='applied' and result['failed_items']==0 and events[0]['current']==1


@pytest.mark.parametrize('changes',[{'Command':'changed'}, {'Fingerprint':'c'*64},{'Location':'changed'}, {'Source':'changed'},{'Protected':True},{'Name':'BingWallpaper'},{'Enabled':False}])
def test_changed_protected_or_already_disabled_startup_is_skipped(changes):
    value=backend();value.startup_list.return_value=[startup(**changes)]
    result=system.run_startup(value,startup_config())
    value.startup_set.assert_not_called();assert result['skipped_items']==1


def test_startup_refuses_stale_setup_and_missing_consent_before_inventory():
    for changes in ({'setup_version':0},{'startup_changes_approved':False},{'startup_approved_entries':[]}):
        value=backend()
        with pytest.raises(ValueError):system.run_startup(value,startup_config(**changes))
        value.startup_list.assert_not_called();value.startup_set.assert_not_called()


def test_cancel_stops_before_mutation_and_read_only_never_applies():
    value=backend();event=Event();event.set()
    assert system.run_startup(value,startup_config(),event)['cancelled']
    value.startup_list.assert_not_called()
    system.run_startup(value,{'startup_policy':'review'})
    value.startup_set.assert_not_called()
    system.run_drivers(value,{'driver_policy':'review'})
    value.driver_backup_automatic.assert_not_called();value.driver_update_apply_unattended.assert_not_called()


def test_driver_snapshot_and_future_rule_include_exact_known_device_date():
    rule=system.driver_device_rule(driver())
    assert rule=={'hardwareId':r'PCI\VEN_1234&DEV_5678','provider':'Fixture provider','class':'Display','notOlderThanDriverDate':'2026-10-01'}
    assert system.validate_driver_devices([rule])==[rule]
    with pytest.raises(ValueError):system.driver_device_rule(driver(driverDate=''))
    with pytest.raises(ValueError):system.driver_device_rule(driver(hardwareId='*'))


def test_exact_driver_requires_successful_backup_before_install():
    value=backend();calls=[]
    value.driver_backup_automatic.side_effect=lambda: calls.append('backup') or {'exit_code':0,'destination':'fixture'}
    value.driver_update_apply_unattended.side_effect=lambda *a,**kw:calls.append('install') or {'resultCode':2,'hresult':0}
    result=system.run_drivers(value,driver_config())
    assert calls==['backup','install'] and result['failed_items']==0
    value.driver_update_apply_unattended.assert_called_once_with(system.driver_descriptor(driver()),accept_eula=True)


@pytest.mark.parametrize('changes',[{'setup_version':0},{'driver_changes_approved':False},{'driver_update_agreements_accepted':False},{'driver_approved_updates':[]}])
def test_driver_changes_require_current_explicit_approval(changes):
    value=backend()
    with pytest.raises(ValueError):system.run_drivers(value,driver_config(**changes))
    value.driver_updates_scan.assert_not_called();value.driver_update_apply_unattended.assert_not_called()


def test_non_admin_never_launches_elevation_or_installs():
    value=backend();value.is_admin.return_value=False
    result=system.run_drivers(value,driver_config())
    assert result['skipped'];value.driver_updates_scan.assert_not_called();value.driver_update_apply_unattended.assert_not_called()


@pytest.mark.parametrize('changes',[{'revision':2},{'title':'changed'},{'hardwareId':'changed'},{'provider':'changed'},{'canRequestUserInput':True},{'canRequestUserInput':None},{'rebootBehavior':1}])
def test_changed_or_ineligible_driver_never_installs(changes):
    value=backend();value.driver_updates_scan.return_value={'resultCode':2,'updates':[driver(**changes)]}
    result=system.run_drivers(value,driver_config())
    assert result['skipped_items']==1;value.driver_backup_automatic.assert_not_called();value.driver_update_apply_unattended.assert_not_called()


def test_future_matching_requires_same_device_provider_class_and_date_floor():
    value=backend();future=driver(id='00000000-0000-0000-0000-000000000002',revision=3,title='Future fixture',driverDate='2026-10-20T00:00:00Z')
    value.driver_updates_scan.return_value={'resultCode':2,'updates':[future]}
    config=driver_config(driver_policy='install_matching',driver_approved_devices=[system.driver_device_rule(driver())])
    result=system.run_drivers(value,config)
    assert result['failed_items']==0
    applied=value.driver_update_apply_unattended.call_args.args[0]
    assert applied['id']==future['id'] and applied['approvalMode']=='matching' and applied['notOlderThanDriverDate']=='2026-10-01'
    for change in ({'provider':'Other'},{'hardwareId':'Other'},{'driverClass':'Other'},{'driverDate':'2026-09-01T00:00:00Z'}):
        value=backend();value.driver_updates_scan.return_value={'resultCode':2,'updates':[driver(**change)]}
        system.run_drivers(value,config);value.driver_update_apply_unattended.assert_not_called()


def test_backup_failure_prevents_all_installation():
    value=backend();value.driver_backup_automatic.return_value={'exit_code':1,'destination':'fixture'}
    result=system.run_drivers(value,driver_config())
    assert result['failed_items']==1;value.driver_update_apply_unattended.assert_not_called()


def test_driver_result_failure_and_cancelled_callback_are_recorded():
    value=backend();value.driver_update_apply_unattended.return_value={'resultCode':3,'hresult':1}
    assert system.run_drivers(value,driver_config())['failed_items']==1
    value=backend();event=Event()
    result=system.run_drivers(value,driver_config(),event,lambda _:event.set())
    assert result['cancelled'];value.driver_update_apply_unattended.assert_not_called()


def test_off_switch_revokes_both_saved_policies_without_read_or_apply():
    value=backend()
    result=system.run_startup(value,startup_config(startup_review_enabled=False))
    assert result['skipped'];value.startup_list.assert_not_called();value.startup_set.assert_not_called()
    result=system.run_drivers(value,driver_config(driver_updates_enabled=False))
    assert result['skipped'];value.is_admin.assert_not_called();value.driver_updates_scan.assert_not_called()


def test_failed_or_unknown_fresh_driver_scan_is_a_reported_failure():
    for scan in ({'resultCode':3,'updates':[]},{'resultCode':None,'updates':[]},None):
        value=backend();value.driver_updates_scan.return_value=scan
        result=system.run_drivers(value,driver_config())
        assert result['failed_items']==1 and result['items'][0]['binding']['stage']=='driver-scan'
        value.driver_backup_automatic.assert_not_called();value.driver_update_apply_unattended.assert_not_called()


@pytest.mark.parametrize('outcome',[RuntimeError('fixture timeout; still running'),{'resultCode':3,'hresult':1},{'resultCode':2,'aggregateResultCode':3,'hresult':0}])
def test_install_failure_or_uncertainty_defers_remaining_driver_ids(outcome):
    value=backend();second=driver(id='00000000-0000-0000-0000-000000000002',title='Second fixture')
    value.driver_updates_scan.return_value={'resultCode':2,'updates':[driver(),second]}
    config=driver_config(driver_approved_updates=[system.driver_descriptor(driver()),system.driver_descriptor(second)])
    if isinstance(outcome,Exception):value.driver_update_apply_unattended.side_effect=outcome
    else:value.driver_update_apply_unattended.return_value=outcome
    result=system.run_drivers(value,config)
    assert result['failed_items']==1 and value.driver_update_apply_unattended.call_count==1
    assert result['deferred'][0]['id']==second['id']
