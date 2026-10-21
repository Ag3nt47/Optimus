"""Maintenance UI contracts with fixture inventories only; no real host operations."""
import copy
import time
from types import SimpleNamespace
from unittest.mock import Mock
import pytest
from PySide6.QtCore import Qt
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import QApplication,QPushButton,QDialog
from optimus.onboarding import InitialSetupWizard
from optimus.ui import DetailedSettingsWindow,OptimusWindow
from optimus.workflow import DEFAULTS,Preferences


class FixtureBackend:
    def __init__(self, root):
        self.data_dir=root
        self.settings=copy.deepcopy(DEFAULTS)
        self.saved=[]
        self.updates_scan=Mock(return_value=software_scan())
        self.driver_devices=Mock(return_value=coverage_report())
        self.analysis_start=Mock();self.analysis_stop=Mock();self.optimize=Mock()
    def preferences(self):return copy.deepcopy(self.settings)
    def cleanup_categories(self):return []
    def save_preferences(self,value):
        self.saved.append(copy.deepcopy(value));self.settings=copy.deepcopy(value)
        return copy.deepcopy(value)


def software_scan(**changes):
    value={'structured':True,'exitCode':0,'source':'winget','output':'Fixture update output','packages':[
        {'name':'Fixture One','id':'Fixture.One','package_id':'Fixture.One','version':'1.0','available':'2.0','source':'winget'},
        {'name':'Fixture Two','id':'Fixture.Two','package_id':'Fixture.Two','version':'2.0','available':'3.0','source':'winget'}]}
    value.update(changes)
    return value


def rule(identifier='PCI\\VEN_FFFF&DEV_0001'):
    return {'hardwareId':identifier,'provider':'Fixture Hardware','class':'Display','notOlderThanDriverDate':'2026-10-01'}


def coverage_report():
    return {'devices':[{'displayName':'Fixture adapter','deviceId':'FIXTURE-DEVICE','deviceClass':'Display','manufacturer':'Fixture Hardware',
        'problemCode':0,'problem':'Windows reports no problem.','status':'no_offer','statusDescription':'No Windows Update offer; vendor latest version not established.',
        'hardwareIds':[r'PCI\VEN_FFFF&DEV_0001'],'installedDrivers':[{'version':'1.2.3'}],'updateOffers':[],
        'supportLinks':[{'label':'NVIDIA driver downloads','url':'https://www.nvidia.com/en-us/drivers/'}],
        'approvalRules':[rule()],'approvalUnavailableReason':''}],
        'summary':{'devices':1,'offered':0,'deviceProblems':0,'noOffer':1},'inventoryComplete':True,'updateScanComplete':True,
        'notes':['Windows Update does not cover every vendor catalog.'],'systemSupport':[]}


@pytest.fixture(scope='module')
def qapp():return QApplication.instance() or QApplication([])


@pytest.fixture
def backend(tmp_path,monkeypatch):
    monkeypatch.setenv('LOCALAPPDATA',str(tmp_path))
    return FixtureBackend(tmp_path)


@pytest.fixture
def wizard(qapp,backend):
    value=InitialSetupWizard(backend)
    yield value
    value.reject();value.deleteLater();qapp.processEvents()


@pytest.fixture
def window(qapp,backend):
    value=DetailedSettingsWindow(backend,smoke_test=True)
    value._run=Mock()
    yield value
    value.shutdown();value.close();value.deleteLater();qapp.processEvents()


def wait_probe(wizard,qapp):
    deadline=time.monotonic()+3
    while wizard.busy and time.monotonic()<deadline:
        qapp.processEvents();time.sleep(0.005)
    assert not wizard.busy


def test_new_software_scope_starts_without_broad_approval(wizard,backend):
    assert wizard.software_policy.currentData()=='review'
    assert not wizard.software_all_consent.isChecked()
    backend.updates_scan.assert_not_called();backend.driver_devices.assert_not_called()
    assert backend.saved==[]


def test_all_supported_requires_both_broad_scope_and_agreements(wizard):
    wizard.controls['software_updates_enabled'].setChecked(True)
    wizard.software_policy.setCurrentIndex(2)
    with pytest.raises(ValueError,match='All-supported'):
        wizard.collect_choices()
    wizard.software_all_consent.setChecked(True)
    with pytest.raises(ValueError,match='All-supported'):
        wizard.collect_choices()
    wizard.software_agreements.setChecked(True)
    wizard.software_exclusions.setPlainText('Fixture.One, Fixture.Pinned')
    assert not wizard.software_all_consent.isChecked()
    wizard.software_all_consent.setChecked(True);wizard.software_agreements.setChecked(True)
    payload=Preferences.validate(wizard.collect_choices())
    assert payload['software_update_policy']=='all_supported'
    assert payload['software_update_all_approved'] is True
    assert payload['software_update_agreements_accepted'] is True
    assert payload['software_update_exclude_ids']==['Fixture.One','Fixture.Pinned']
    wizard.refresh_summary()
    assert 'future matching applications' in wizard.summary.toPlainText()
    assert 'Fixture.Pinned' in wizard.summary.toPlainText()


def test_review_revokes_software_apply_authorization(wizard):
    wizard.controls['software_updates_enabled'].setChecked(True)
    wizard.software_policy.setCurrentIndex(2)
    wizard.software_all_consent.setChecked(True);wizard.software_agreements.setChecked(True)
    wizard.software_policy.setCurrentIndex(0)
    payload=wizard.collect_choices()
    assert payload['software_update_all_approved'] is False
    assert payload['software_update_agreements_accepted'] is False


def test_loaded_ids_move_to_checklist_and_can_be_revoked(wizard):
    wizard.controls['software_updates_enabled'].setChecked(True);wizard.software_policy.setCurrentIndex(1)
    wizard.software_ids.setPlainText('Fixture.One\nFixture.Advanced')
    wizard._software_candidates_ready(software_scan())
    assert wizard.software_targets.count()==2
    assert wizard.software_targets.item(0).checkState()==Qt.Checked
    assert wizard.software_ids.toPlainText()=='Fixture.Advanced'
    wizard.software_targets.item(0).setCheckState(Qt.Unchecked)
    wizard.software_targets.item(1).setCheckState(Qt.Checked)
    wizard.software_agreements.setChecked(True)
    assert wizard.collect_choices()['software_update_ids']==['Fixture.Advanced','Fixture.Two']


def test_software_checklist_does_not_guess_unknown_truncated_or_duplicate_entries(wizard):
    rows=software_scan()['packages']
    rows += [dict(rows[0]),dict(rows[1],id='Fixture.Unknown',package_id='Fixture.Unknown',version='Unknown'),
             dict(rows[1],id='Fixture.Truncated...',package_id='Fixture.Truncated...'),
             dict(rows[1],id='Fixture.Store',package_id='Fixture.Store',source='msstore')]
    wizard._software_candidates_ready(software_scan(packages=rows))
    assert wizard.software_targets.count()==1
    assert wizard.software_targets.item(0).data(Qt.UserRole)['id']=='Fixture.Two'


def test_software_scan_is_async_and_cancel_saves_nothing(wizard,backend,qapp):
    wizard.load_software_candidates();wait_probe(wizard,qapp)
    backend.updates_scan.assert_called_once_with()
    wizard.software_targets.item(0).setCheckState(Qt.Checked)
    wizard.reject()
    assert backend.saved==[]
    backend.optimize.assert_not_called()


def test_selected_software_payload_saved_only_after_finish(wizard,backend,monkeypatch):
    import optimus.automation
    monkeypatch.setattr(optimus.automation,'save_setup',lambda root,prefs,saver,**kw:saver(prefs))
    wizard.controls['software_updates_enabled'].setChecked(True);wizard.software_policy.setCurrentIndex(1)
    wizard._software_candidates_ready(software_scan())
    wizard.software_targets.item(1).setCheckState(Qt.Checked);wizard.software_agreements.setChecked(True)
    assert backend.saved==[]
    wizard.accept()
    assert wizard.result()==QDialog.Accepted
    assert backend.saved[0]['software_update_ids']==['Fixture.Two']
    assert backend.saved[0]['software_update_all_approved'] is False
    backend.optimize.assert_not_called()


def test_present_hardware_without_current_offer_can_be_approved(wizard,backend,qapp):
    wizard.controls['driver_updates_enabled'].setChecked(True);wizard.driver_policy.setCurrentIndex(2)
    wizard.load_hardware_approvals();wait_probe(wizard,qapp)
    backend.driver_devices.assert_called_once_with()
    wizard.select_all_hardware()
    wizard.driver_consent.setChecked(True);wizard.driver_agreements.setChecked(True)
    payload=Preferences.validate(wizard.collect_choices())
    assert payload['driver_policy']=='install_matching'
    assert payload['driver_approved_updates']==[]
    assert payload['driver_approved_devices']==[rule()]


def test_select_all_hardware_respects_rule_bound_and_explains_skips(wizard,monkeypatch):
    import optimus.onboarding as onboarding
    assert onboarding.DEVICE_RULE_LIMIT==2048
    monkeypatch.setattr(onboarding,'DEVICE_RULE_LIMIT',2)
    wizard.driver_policy.setCurrentIndex(2)
    device=coverage_report()['devices'][0]
    devices=[dict(device,displayName='Device '+str(index),approvalRules=[rule('PCI\\DEVICE_'+str(index))]) for index in range(3)]
    wizard._hardware_ready({'devices':devices})
    wizard.select_all_hardware()
    assert len(wizard._driver_rules())==2
    assert 'remain unapproved' in wizard.hardware_status.text()
    assert not wizard.driver_consent.isChecked()


def test_changing_driver_mode_invalidates_hardware_selection_and_consent(wizard):
    wizard.driver_policy.setCurrentIndex(2);wizard._hardware_ready(coverage_report());wizard.select_all_hardware()
    wizard.driver_consent.setChecked(True);wizard.driver_agreements.setChecked(True)
    wizard.driver_policy.setCurrentIndex(1)
    assert wizard.hardware_targets.count()==0 and not wizard._hardware_loaded
    assert not wizard.driver_consent.isChecked() and not wizard.driver_agreements.isChecked()


def test_driver_coverage_is_honest_about_missing_vendor_offers(window):
    window._driver_coverage_ready(coverage_report())
    assert window.driver_coverage_table.rowCount()==1
    assert window.driver_tabs.currentIndex()==2
    assert 'not proof' in window.driver_coverage_details.toPlainText()
    assert '1.2.3' in window.driver_coverage_details.toPlainText()
    window._run.assert_not_called()


def test_driver_support_opens_only_known_official_routes(window,monkeypatch):
    opened=Mock(return_value=True);monkeypatch.setattr(QDesktopServices,'openUrl',opened)
    window._driver_coverage_ready(coverage_report());window.open_driver_support()
    assert opened.call_args[0][0].toString()=='https://www.nvidia.com/en-us/drivers/'
    window.driver_support_links.clear();window.driver_support_links.addItem('Untrusted','https://example.invalid/driver.exe')
    window.open_driver_support()
    assert opened.call_count==1 and 'recognized official' in window.last_error


def slowdown_fixture():
    base={'exe':'D:\\Fixture\\app.exe','process_count':1,'pids':[1234],'cpu_percent':30.0,'peak_cpu_percent':35.0,
          'memory_rss':104857600,'memory_percent':2.0,'read_bytes_per_second':1048576,'write_bytes_per_second':0,
          'measured_intervals':2,'io_measured_intervals':2,'reasons':['Measured sustained CPU use.'],'recommendation':'Review current work before pausing.'}
    return {'sample_count':3,'duration_seconds':1.5,'complete':True,'system_cpu_percent':40,'memory':{'percent':60},'applications':[
        dict(base,name='Fixture normal',severity='normal',exe='D:\\Fixture\\normal.exe'),dict(base,name='Fixture busy',severity='high')],
        'inaccessible_process_count':1,'notes':['This is a short measurement, not proof a program is unwanted.']}


def test_measured_slowdown_shows_evidence_without_mutating(window):
    window._slowdown_ready(slowdown_fixture())
    assert window.slowdown_table.rowCount()==2
    assert window.slowdown_table.selected()['name']=='Fixture busy'
    assert 'Measured sustained CPU use' in window.slowdown_details.toPlainText()
    assert 'D:\\Fixture\\app.exe' in window.slowdown_details.toPlainText()
    assert '3 samples' in window.slowdown_summary.text()
    window._run.assert_not_called()


def test_unmeasured_cpu_and_io_show_unavailable(window):
    report=slowdown_fixture();report['applications'][0].update(measured_intervals=0,io_measured_intervals=0)
    window._slowdown_ready(report)
    index=next(index for index in range(window.slowdown_table.rowCount()) if window.slowdown_table.item(index,0).data(Qt.UserRole)['name']=='Fixture normal')
    assert window.slowdown_table.item(index,2).text()=='Unavailable'
    assert window.slowdown_table.item(index,6).text()=='Unavailable'


def test_slowdown_routes_exact_program_to_existing_process_controls(window):
    window._slowdown_ready(slowdown_fixture());window._slowdown_open_processes()
    assert window.process_search.text()=='D:\\Fixture\\app.exe'
    assert window.performance_tabs.currentIndex()==0
    assert window._run.call_args[0]==('processes',)


def test_updates_table_uses_same_strict_candidate_rules(window):
    scan=software_scan();scan['packages'].append(dict(scan['packages'][0]))
    window._updates_ready(scan)
    assert window.updates_candidates_table.rowCount()==1
    assert window.update_id.text()=='Fixture.Two'
    window._updates_ready(software_scan(structured=False))
    assert window.updates_candidates_table.rowCount()==0


def test_store_update_button_uses_native_route(window):
    button=next(button for button in window.findChildren(QPushButton) if button.text()=='Microsoft Store updates')
    button.click()
    assert window._run.call_args[0]==('system_tool','store-updates')


def test_junk_preview_is_invalidated_before_changed_age(window):
    window._cleanup_ready({'files':[{'path':'D:\\Fixture\\old.tmp','size_mb':1,'age_days':10,'category':'Fixture temp'}], 'total_mb':1,'complete':True,'skipped':[]})
    assert window.clean_apply.isEnabled()
    window.clean_age.setValue(1)
    assert window.cleanup_plan is None and not window.clean_apply.isEnabled()
    window._run.assert_not_called()


def test_main_controls_remain_exactly_three(qapp,backend):
    main=OptimusWindow(backend,smoke_test=True)
    assert [button.text() for button in main.main_buttons]==['1 click Optimize and debloat','Settings','Sandbox']
    main.shutdown();main.close();main.deleteLater()


def test_matching_setup_focuses_present_devices_without_hidden_offer_approvals(wizard):
    wizard.driver_policy.setCurrentIndex(2)
    assert wizard.driver_offer_load_button.isHidden()
    assert wizard.driver_status.isHidden()
    assert wizard.driver_targets.isHidden() and wizard.driver_details.isHidden()
    assert not wizard.hardware_load_button.isHidden() and not wizard.hardware_targets.isHidden()
    wizard.driver_policy.setCurrentIndex(1)
    assert not wizard.driver_offer_load_button.isHidden()
    assert wizard.hardware_targets.isHidden()


def test_failed_software_reload_retains_checked_exact_ids(wizard):
    wizard.controls['software_updates_enabled'].setChecked(True);wizard.software_policy.setCurrentIndex(1)
    wizard._software_candidates_ready(software_scan())
    wizard.software_targets.item(0).setCheckState(Qt.Checked)
    wizard._software_candidates_ready(software_scan(structured=False))
    assert wizard.software_targets.count()==0
    assert wizard.software_ids.toPlainText()=='Fixture.One'
    wizard.software_agreements.setChecked(True)
    assert wizard.collect_choices()['software_update_ids']==['Fixture.One']
