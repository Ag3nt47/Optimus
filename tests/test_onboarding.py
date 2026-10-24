"""Setup questions use mock backends; no scans, cleanup or updates are executed."""
from __future__ import annotations
import copy
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock
import pytest
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication, QDialog, QPushButton, QWizard
from optimus.onboarding import InitialSetupWizard
from optimus.workflow import DEFAULTS


class FixtureBackend:
    def __init__(self, root, settings=None):
        self.data_dir = Path(root)
        self.settings = copy.deepcopy(DEFAULTS)
        if settings:
            self.settings.update(settings)
        self.saved = []
        self.events = []
        self.setup_transactions = []
        self.analysis_start = Mock()
        self.analysis_stop = Mock()
        self.optimize = Mock(side_effect=AssertionError('Real workflow must not run in setup fixture.'))

    def preferences(self):
        return copy.deepcopy(self.settings)

    def cleanup_categories(self):
        return [{'id':'user-temp','name':'User temp','available':True,'process_names':[]},
                {'id':'fixture-shaders','name':'Fixture shader cache','available':True,'process_names':[]},
                {'id':'fixture-errors','name':'Fixture error reports','available':False,'process_names':[]},
                {'id':'fixture-browser','name':'Fixture browser cache','available':True,'process_names':['fixture-browser.exe']}]

    def save_preferences(self, settings):
        self.events.append('save')
        self.saved.append(copy.deepcopy(settings))
        self.settings = copy.deepcopy(settings)
        return copy.deepcopy(settings)


@pytest.fixture(scope='module')
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app


@pytest.fixture
def backend(tmp_path, monkeypatch):
    result = FixtureBackend(tmp_path)
    monkeypatch.setenv('LOCALAPPDATA', str(tmp_path))
    def baseline(data_dir, settings, status='started', **unused):
        assert data_dir == result.data_dir
        assert status == 'configured'
        result.events.append('configured')
        return {'status':status}
    import optimus.automation
    monkeypatch.setattr(optimus.automation, 'record_attempt', baseline)
    def save_setup(data_dir, settings, saver, *, run_now=False):
        result.setup_transactions.append({'run_now':run_now, 'settings':copy.deepcopy(settings)})
        optimus.automation.record_attempt(data_dir, settings, status='configured')
        return saver(settings)
    monkeypatch.setattr(optimus.automation, 'save_setup', save_setup, raising=False)
    return result


@pytest.fixture
def wizard(qapp, backend):
    result = InitialSetupWizard(backend, first_run=True)
    yield result
    result.reject()
    result.deleteLater()
    qapp.processEvents()


def test_all_nine_tool_categories_have_questions(wizard):
    expected = {'health','cleanup','uninstaller','duplicates','cloud','performance','startup','software','drivers','toolkit'}
    assert expected.issubset(wizard.page_ids)
    assert len(wizard.pageIds()) == 12
    toolkit_page = wizard.page(wizard.page_ids['toolkit'])
    assert toolkit_page.isAncestorOf(wizard.toolkit_choices)
    assert 'Additional maintenance' in toolkit_page.title()
    assert all(section['enabled'] is False for section in wizard.collect_choices()['toolkit_settings'].values())
    assert wizard.size().width() >= 1000
    assert wizard.size().height() >= 780
    assert wizard.buttonText(QWizard.FinishButton) == 'Save choices'


def test_construct_and_cancel_do_not_write_or_run(wizard, backend):
    initial = backend.preferences()
    wizard.controls['consumer_debloat_enabled'].setChecked(True)
    wizard.cadence_buttons['weekly'].setChecked(True)
    wizard.reject()
    assert backend.preferences() == initial
    assert backend.saved == []
    assert backend.events == []
    backend.optimize.assert_not_called()
    backend.analysis_start.assert_not_called()
    backend.analysis_stop.assert_not_called()


def test_safe_starting_values_and_run_now_off(wizard):
    assert wizard.cadence_buttons['manual'].isChecked()
    assert not wizard.run_now_checkbox.isChecked()
    for key in ('consumer_debloat_enabled','m365_debloat_enabled','outlook_debloat_enabled','edge_debloat_enabled','duplicates_enabled','cloud_review_enabled','software_updates_enabled','driver_updates_enabled'):
        assert not wizard.controls[key].isChecked()
    assert 'user-temp' not in wizard.category_controls
    assert 'fixture-browser' not in wizard.category_controls
    assert not any(wizard.controls[key].isChecked() for key in
                   ('browser_history_enabled','browser_cookies_enabled','browser_download_history_enabled'))
    assert set(wizard.browser_controls) == {'edge','chrome','brave','vivaldi','firefox','chromium','opera'}
    assert all(wizard.browser_controls[key].isChecked() for key in ('edge','chrome','brave','vivaldi','firefox'))
    assert not wizard.browser_controls['opera'].isChecked()
    assert not wizard.browser_controls['chromium'].isChecked()


def test_existing_choices_preserved_without_reenabling_others(qapp, tmp_path):
    original = {'intensity':'High','cleanup_age_days':1,'consumer_debloat_enabled':False,
                'm365_debloat_enabled':True,'background_analysis_enabled':True,'analysis_interval':9,
                'automation_mode':'weekly','cleanup_category_ids':['fixture-errors'],'private_fixture_key':'retained'}
    backend = FixtureBackend(tmp_path, original)
    wizard = InitialSetupWizard(backend)
    selected = wizard.collect_choices()
    for key,value in original.items():
        assert selected[key] == value
    assert not selected['edge_debloat_enabled']
    wizard.deleteLater()


def test_high_preset_only_changes_age(wizard):
    wizard.controls['intensity'].setCurrentText('High')
    assert wizard.controls['cleanup_age_days'].value() == 1
    assert not wizard.controls['consumer_debloat_enabled'].isChecked()
    assert not wizard.controls['edge_debloat_enabled'].isChecked()
    assert not wizard.controls['codex_priority_enabled'].isChecked()
    assert not any(wizard.controls[key].isChecked() for key in
                   ('browser_history_enabled','browser_cookies_enabled','browser_download_history_enabled'))


def test_browser_scope_and_keep_hosts_are_explicit_and_cancel_preserves_preferences(wizard,backend):
    initial=backend.preferences()
    for key in ('browser_history_enabled','browser_cookies_enabled','browser_download_history_enabled'):
        wizard.controls[key].setChecked(True)
    wizard.controls['browser_cache_enabled'].setChecked(False)
    for key,control in wizard.browser_controls.items():
        control.setChecked(key=='chrome')
    wizard.browser_keep_domains.setPlainText('.EXAMPLE.com., example.com\nbücher.de')
    selected=wizard.collect_choices()
    assert all(selected[key] for key in ('browser_history_enabled','browser_cookies_enabled','browser_download_history_enabled'))
    assert selected['browser_cache_enabled'] is False
    assert selected['browser_cleanup_browsers']==['chrome']
    assert selected['browser_keep_domains']==['example.com','xn--bcher-kva.de']
    wizard.reject()
    assert backend.preferences()==initial
    assert backend.saved==[]
    backend.optimize.assert_not_called()


def test_browser_privacy_summary_names_categories_scope_and_kept_domains(wizard):
    for key in ('browser_history_enabled','browser_cookies_enabled','browser_download_history_enabled'):
        wizard.controls[key].setChecked(True)
    for key,control in wizard.browser_controls.items():
        control.setChecked(key=='firefox')
    wizard.browser_keep_domains.setPlainText('EXAMPLE.com')
    before=wizard.collect_choices()
    wizard.refresh_summary()
    text=wizard.summary.toPlainText().casefold()
    assert all(word in text for word in ('history','cookies','download','firefox','example.com'))
    assert 'downloaded files stay' in text
    assert wizard.collect_choices()==before


def test_browser_keep_hosts_require_plain_domains_in_setup(wizard):
    wizard.browser_keep_domains.setPlainText('https://example.com/path')
    with pytest.raises(ValueError,match='plain domain'):
        wizard.collect_choices()


def test_browser_cleanup_requires_a_selected_browser(wizard):
    for control in wizard.browser_controls.values():
        control.setChecked(False)
    with pytest.raises(ValueError):
        wizard.collect_choices()
    wizard.controls['browser_cache_enabled'].setChecked(False)
    assert wizard.collect_choices()['browser_cleanup_browsers']==[]


def test_collect_explicit_categories_roots_and_exact_ids(wizard):
    wizard.cadence_buttons['daily'].setChecked(True)
    wizard.category_controls['fixture-shaders'].setChecked(True)
    wizard.controls['duplicates_enabled'].setChecked(True)
    wizard.duplicate_roots.setPlainText('C:\\Fixture\\Downloads\nD:\\Fixture\\Documents\nC:\\Fixture\\Downloads')
    wizard.controls['software_updates_enabled'].setChecked(True)
    wizard.software_policy.setCurrentIndex(1)
    wizard.software_ids.setPlainText('Vendor.One, Vendor.Two\nVendor.One')
    wizard.software_agreements.setChecked(True)
    selected = wizard.collect_choices()
    assert selected['setup_version'] == 2
    assert selected['automation_mode'] == 'daily'
    assert selected['cleanup_category_ids'] == ['fixture-shaders']
    assert selected['duplicate_roots'] == ['C:\\Fixture\\Downloads','D:\\Fixture\\Documents']
    assert selected['software_update_ids'] == ['Vendor.One','Vendor.Two']
    assert selected['software_update_agreements_accepted'] is True


def test_selected_install_requires_ids_and_agreements(wizard, backend):
    wizard.controls['software_updates_enabled'].setChecked(True)
    wizard.software_policy.setCurrentIndex(1)
    with pytest.raises(ValueError, match='package ID'):
        wizard.collect_choices()
    wizard.software_ids.setPlainText('Vendor.One')
    with pytest.raises(ValueError, match='agreement authorization'):
        wizard.collect_choices()
    wizard.accept()
    assert backend.saved == []
    assert backend.events == []
    assert 'agreement' in wizard.save_error.text()


def test_review_only_does_not_accept_agreements(wizard):
    wizard.controls['software_updates_enabled'].setChecked(True)
    wizard.software_agreements.setChecked(True)
    assert wizard.software_policy.currentData() == 'review'
    assert not wizard.software_agreements.isEnabled()
    assert wizard.collect_choices()['software_update_agreements_accepted'] is False


@pytest.mark.parametrize('text', ['Vendor.One --all', 'https://example.invalid/app', '--all', 'Vendor.One | anything', 'X', 'X' * 129])
def test_commands_are_not_package_ids(wizard, text):
    wizard.controls['software_updates_enabled'].setChecked(True)
    wizard.software_policy.setCurrentIndex(1)
    wizard.software_ids.setPlainText(text)
    wizard.software_agreements.setChecked(True)
    with pytest.raises(ValueError, match='exact WinGet'):
        wizard.collect_choices()


@pytest.mark.parametrize('root', ['relative-folder', '\\\\server\\share'])
def test_duplicate_roots_must_be_local_absolute(wizard, root):
    wizard.controls['duplicates_enabled'].setChecked(True)
    wizard.duplicate_roots.setPlainText(root)
    with pytest.raises(ValueError, match='absolute local'):
        wizard.collect_choices()


def test_summary_discloses_mutations_and_review_only_limits(wizard):
    wizard.cadence_buttons['weekly'].setChecked(True)
    wizard.controls['consumer_debloat_enabled'].setChecked(True)
    wizard.controls['driver_updates_enabled'].setChecked(True)
    wizard.controls['cloud_review_enabled'].setChecked(True)
    wizard.refresh_summary()
    text = wizard.summary.toPlainText()
    assert 'Weekly while the app is open' in text
    assert 'Permanently delete' in text
    assert 'Remove the curated consumer-app group' in text
    assert 'Bing Wallpaper is protected' in text
    assert 'Driver inventory/update scan/report' in text
    assert 'Review policies do not apply changes' in text
    assert 'sign-in required' in text


def test_finish_records_baseline_before_saving_without_run(wizard, backend):
    wizard.accept()
    assert wizard.result() == QDialog.Accepted
    assert backend.events == ['configured','save']
    assert len(backend.saved) == 1
    assert wizard.saved_preferences['setup_version'] == 2
    assert wizard.run_now is False
    backend.optimize.assert_not_called()


def test_baseline_failure_never_enables_auto_preferences(wizard, backend, monkeypatch):
    import optimus.automation
    def refuse(*args, **kwargs):
        raise OSError('Fixture cadence baseline could not be written')
    monkeypatch.setattr(optimus.automation, 'record_attempt', refuse)
    wizard.cadence_buttons['daily'].setChecked(True)
    wizard.accept()
    assert backend.saved == []
    assert wizard.saved_preferences is None
    assert 'baseline' in wizard.save_error.text()
    backend.optimize.assert_not_called()


def test_save_failure_keeps_dialog_open_after_safe_baseline(wizard, backend):
    backend.save_preferences = Mock(side_effect=OSError('Fixture save refused'))
    wizard.accept()
    assert backend.events == ['configured']
    assert wizard.result() != QDialog.Accepted
    assert wizard.saved_preferences is None
    assert 'Fixture save refused' in wizard.save_error.text()


def test_explicit_run_now_is_passed_to_setup_transaction(wizard, backend):
    wizard.run_now_checkbox.setChecked(True)
    wizard.accept()
    assert backend.events == ['configured','save']
    assert backend.setup_transactions[-1]['run_now'] is True
    assert wizard.run_now is True
    backend.optimize.assert_not_called()


def test_optimize_opens_setup_before_first_run(qapp, backend):
    from optimus.ui import OptimusWindow
    window = OptimusWindow(backend, smoke_test=True)
    window.smoke_test = False
    window.open_initial_setup = Mock(return_value=False)
    window.start_optimization()
    window.open_initial_setup.assert_called_once_with(first_run=True)
    backend.optimize.assert_not_called()
    assert not window.busy
    window.smoke_test = True
    window.shutdown()
    window.close()
    window.deleteLater()


def test_saved_setup_updates_sampler_only_after_finish(qapp, backend, monkeypatch):
    from optimus.ui import OptimusWindow
    def accept(self):
        self.controls['background_analysis_enabled'].setChecked(False)
        self.accept()
        return self.result()
    monkeypatch.setattr(InitialSetupWizard, 'exec', accept)
    window = OptimusWindow(backend, smoke_test=True)
    window.start_optimization = Mock()
    assert window.open_initial_setup(first_run=True) is True
    backend.analysis_stop.assert_called_once_with()
    backend.analysis_start.assert_not_called()
    window.start_optimization.assert_not_called()
    assert window._setup_wizard is None
    assert len(window.main_buttons) == 4
    window.shutdown()
    window.close()
    window.deleteLater()


def test_run_now_starts_existing_path_after_saving(qapp, backend, monkeypatch):
    from optimus.ui import OptimusWindow
    def accept(self):
        self.controls['background_analysis_enabled'].setChecked(True)
        self.controls['analysis_interval'].setValue(13)
        self.run_now_checkbox.setChecked(True)
        self.accept()
        return self.result()
    monkeypatch.setattr(InitialSetupWizard, 'exec', accept)
    window = OptimusWindow(backend, smoke_test=True)
    calls = []
    window.start_optimization = lambda: calls.append(copy.deepcopy(backend.settings))
    assert window.open_initial_setup() is True
    backend.analysis_start.assert_called_once_with(13)
    assert len(calls) == 1 and calls[0]['setup_version'] == 2
    assert backend.events == ['configured','save']
    assert backend.setup_transactions[-1]['run_now'] is True
    window.shutdown()
    window.close()
    window.deleteLater()


def test_cancel_leaves_running_sampler_and_preferences_unchanged(qapp, backend, monkeypatch):
    from optimus.ui import OptimusWindow
    monkeypatch.setattr(InitialSetupWizard, 'exec', lambda self: QDialog.Rejected)
    window = OptimusWindow(backend, smoke_test=True)
    assert window.open_initial_setup() is False
    backend.analysis_start.assert_not_called()
    backend.analysis_stop.assert_not_called()
    assert backend.saved == []
    assert backend.events == []
    window.shutdown()
    window.close()
    window.deleteLater()


def test_setup_busy_guard_prevents_sandbox_or_quit(qapp, backend):
    from optimus.ui import OptimusWindow
    window = OptimusWindow(backend, smoke_test=True)
    window._setup_wizard = object()
    window.open_sandbox()
    assert window._sandbox_window is None
    assert window.quit_application() is False
    window._setup_wizard = None
    window.shutdown()
    window.close()
    window.deleteLater()



def test_legacy_settings_save_preserves_new_wizard_choices(qapp, backend):
    from optimus.ui import OptimusWindow, DetailedSettingsWindow
    main = OptimusWindow(backend, smoke_test=True)
    detail = DetailedSettingsWindow(backend, smoke_test=True)
    main._settings_window = detail
    main._install_workflow_preferences(detail)
    backend.settings.update({'setup_version':1, 'automation_mode':'weekly', 'duplicates_enabled':True,
                             'duplicate_roots':['D:\\Fixture'], 'driver_updates_enabled':True})
    calls = []
    detail._run = lambda *args, **kwargs: calls.append((args,kwargs))
    save = next(button for button in detail.findChildren(QPushButton) if button.text() == 'Save one-click configuration')
    save.click()
    payload = calls[0][0][1]
    assert payload['setup_version'] == 1
    assert payload['automation_mode'] == 'weekly'
    assert payload['duplicate_roots'] == ['D:\\Fixture']
    assert payload['driver_updates_enabled'] is True
    assert any(button.text() == 'Run setup questions again' for button in detail.findChildren(QPushButton))
    assert [button.text() for button in main.main_buttons] == ['1 click Optimize and debloat','Settings','Sandbox','Revert']
    main.shutdown()
    main.close()
    detail.close()
    main.deleteLater()
    detail.deleteLater()



def startup_fixture(**changes):
    record = {'Id':'Registry-'+'a'*64,'Name':'Fixture app','Source':'HKCU Run',
        'Command':'C:\\Fixture\\app.exe','Location':'HKCU Run Fixture','EntryType':'Registry','Fingerprint':'b'*64,
        'Hive':'CurrentUser','RegistryView':'Registry64','RegistryPath':r'Software\Microsoft\Windows\CurrentVersion\Run',
        'ValueName':'Fixture app','ValueKind':'String','RawValue':'C:\\Fixture\\app.exe',
        'Enabled':True,'CanManage':True,'Protected':False}
    record.update(changes)
    return record


def driver_fixture(**changes):
    record = {'id':'11111111-1111-4111-8111-111111111111','revision':3,'title':'Fixture display driver',
        'hardwareId':r'PCI\VEN_FFFF&DEV_ABCD','provider':'Fixture Hardware','driverClass':'Display',
        'driverDate':'2026-10-01','canRequestUserInput':False,'rebootBehavior':0,'eulaAccepted':False}
    record.update(changes)
    return record


def checked_hardware_fixture(wizard, update=None):
    """Matching approvals come from the visible installed-hardware checklist."""
    from optimus.automated_system import driver_device_rule
    update = update or driver_fixture()
    rule = driver_device_rule(update)
    wizard._hardware_ready({'devices': [{'displayName': update['title'], 'approvalRules': [rule]}]})
    wizard.hardware_targets.item(0).setCheckState(Qt.Checked)
    return rule


def cloud_fixture(wizard, *, folder='folder-one', drive='drive-one'):
    wizard.cloud_folders.setPlainText(folder)
    wizard._cloud_scope_ready({'inputs':[folder],'scopes':[{'drive_id':drive,'folder_id':folder,
        'drive_name':'Fixture Personal Drive','folder_name':'Fixture folder','provider':'Personal OneDrive'}]})


def wait_for_probe(wizard, qapp):
    import time
    deadline = time.monotonic()+3
    while wizard.busy and time.monotonic()<deadline:
        qapp.processEvents()
        time.sleep(0.005)
    assert not wizard.busy


def test_v1_cannot_silently_inherit_new_apply_approvals(qapp, tmp_path):
    backend=FixtureBackend(tmp_path,{'setup_version':1,'duplicate_policy':'keep_oldest',
        'duplicate_changes_approved':True,'startup_policy':'disable_selected','startup_changes_approved':True,
        'driver_policy':'install_matching','driver_changes_approved':True,'driver_update_agreements_accepted':True})
    wizard=InitialSetupWizard(backend)
    for policy in (wizard.duplicate_policy,wizard.cloud_policy,wizard.startup_policy,wizard.driver_policy):
        assert policy.currentData()=='review'
    for control in (wizard.duplicate_consent,wizard.cloud_consent,wizard.startup_consent,wizard.driver_consent,wizard.driver_agreements):
        assert not control.isChecked()
    wizard.deleteLater()


def test_duplicate_approval_binds_roots_policy_age_and_limit(wizard):
    wizard.controls['duplicates_enabled'].setChecked(True)
    wizard.duplicate_roots.setPlainText('D:\\Fixture\\Photos')
    wizard.duplicate_policy.setCurrentIndex(1)
    with pytest.raises(ValueError,match='Approve automatic duplicate'):
        wizard.collect_choices()
    wizard.duplicate_consent.setChecked(True)
    selected=wizard.collect_choices()
    assert selected['duplicate_policy']=='keep_oldest'
    assert selected['duplicate_roots']==['D:\\Fixture\\Photos']
    assert selected['duplicate_max_recycle']==25 and selected['duplicate_min_age_days']==7
    wizard.controls['duplicate_max_recycle'].setValue(26)
    assert not wizard.duplicate_consent.isChecked()
    wizard.duplicate_consent.setChecked(True)
    wizard.duplicate_roots.appendPlainText('D:\\Fixture\\More')
    assert not wizard.duplicate_consent.isChecked()


def test_cloud_scope_never_falls_back_to_root_or_unverified_input(wizard, backend):
    backend.cloud_approval_scope=Mock()
    wizard.controls['cloud_review_enabled'].setChecked(True)
    wizard.verify_cloud_scope()
    backend.cloud_approval_scope.assert_not_called()
    with pytest.raises(ValueError,match='Verify an explicit'):
        wizard.collect_choices()
    wizard.controls['cloud_review_enabled'].setChecked(False)
    wizard.cloud_folders.setPlainText('root')
    assert wizard.collect_choices()['cloud_folder_ids']==[]


def test_cloud_approval_saves_concrete_drive_scope_and_edits_revoke(wizard):
    wizard.controls['cloud_review_enabled'].setChecked(True)
    cloud_fixture(wizard)
    wizard.cloud_policy.setCurrentIndex(2)
    wizard.cloud_consent.setChecked(True)
    selected=wizard.collect_choices()
    assert selected['cloud_policy']=='keep_newest'
    assert selected['cloud_drive_id']=='drive-one'
    assert selected['cloud_folder_ids']==['folder-one']
    wizard.cloud_folders.setPlainText('folder-two')
    assert wizard._cloud_binding is None
    assert not wizard.cloud_consent.isChecked()
    with pytest.raises(ValueError,match='Verify an explicit'):
        wizard.collect_choices()


def test_cloud_late_response_cannot_approve_edited_scope(wizard):
    wizard.cloud_folders.setPlainText('folder-new')
    wizard._cloud_scope_ready({'inputs':['folder-old'],'scopes':[{'drive_id':'drive-one','folder_id':'folder-old','provider':'Personal OneDrive'}]})
    assert wizard._cloud_binding is None
    assert 'changed' in wizard.cloud_scope_status.text()


def test_cloud_mixed_drives_and_nonpersonal_scope_refused(wizard):
    wizard.cloud_folders.setPlainText('folder-one,folder-two')
    scopes=[{'drive_id':'drive-one','folder_id':'folder-one','provider':'Personal OneDrive'},
            {'drive_id':'drive-two','folder_id':'folder-two','provider':'Personal OneDrive'}]
    with pytest.raises(ValueError,match='one verified personal'):
        wizard._cloud_scope_ready({'inputs':['folder-one','folder-two'],'scopes':scopes})
    assert wizard._cloud_binding is None


def test_cloud_verify_button_runs_mock_inventory_asynchronously(wizard, backend, qapp):
    wizard.cloud_folders.setPlainText('folder-one')
    backend.cloud_approval_scope=Mock(return_value={'drive_id':'drive-one','folder_id':'folder-one','provider':'Personal OneDrive'})
    wizard.verify_cloud_scope()
    wait_for_probe(wizard,qapp)
    backend.cloud_approval_scope.assert_called_once_with('folder-one')
    assert wizard._cloud_binding['drive_id']=='drive-one'
    assert not wizard.cloud_consent.isChecked()


def test_startup_approval_is_exact_fresh_snapshot_and_bing_excluded(wizard):
    from optimus.automated_system import startup_descriptor
    valid=startup_fixture()
    wizard._startup_ready([valid,startup_fixture(Name='Bing Wallpaper'),startup_fixture(Protected=True)])
    assert wizard.startup_targets.count()==1
    wizard.controls['startup_review_enabled'].setChecked(True)
    wizard.startup_policy.setCurrentIndex(1)
    wizard.startup_targets.item(0).setCheckState(Qt.Checked)
    wizard.startup_consent.setChecked(True)
    selected=wizard.collect_choices()
    assert selected['startup_approved_entries']==[startup_descriptor(valid)]
    assert selected['startup_changes_approved'] is True
    wizard.startup_targets.item(0).setCheckState(Qt.Unchecked)
    assert not wizard.startup_consent.isChecked()


def test_changed_startup_command_is_not_preselected(qapp,tmp_path):
    from optimus.automated_system import startup_descriptor
    old=startup_descriptor(startup_fixture())
    backend=FixtureBackend(tmp_path,{'setup_version':2,'startup_policy':'disable_selected','startup_approved_entries':[old], 'startup_changes_approved':True})
    wizard=InitialSetupWizard(backend)
    wizard._startup_ready([startup_fixture(Command='C:\\Fixture\\new.exe',Fingerprint='c'*64)])
    assert wizard.startup_targets.item(0).checkState()==Qt.Unchecked
    wizard.deleteLater()


def test_exact_driver_identity_revision_and_consent_saved(wizard):
    from optimus.automated_system import driver_descriptor
    update=driver_fixture()
    wizard._drivers_ready({'updates':[update],'resultCode':2})
    wizard.controls['driver_updates_enabled'].setChecked(True)
    wizard.driver_policy.setCurrentIndex(1)
    wizard.driver_targets.item(0).setCheckState(Qt.Checked)
    wizard.driver_consent.setChecked(True)
    with pytest.raises(ValueError,match='agreements'):
        wizard.collect_choices()
    wizard.driver_agreements.setChecked(True)
    selected=wizard.collect_choices()
    assert selected['driver_policy']=='install_approved'
    assert selected['driver_approved_updates']==[driver_descriptor(update)]
    assert selected['driver_update_agreements_accepted'] is True


def test_driver_matching_rule_preserves_provider_class_and_date_floor(wizard):
    from optimus.automated_system import driver_device_rule
    update=driver_fixture()
    wizard.controls['driver_updates_enabled'].setChecked(True)
    wizard.driver_policy.setCurrentIndex(2)
    rule=driver_device_rule(update)
    wizard._hardware_ready({'devices':[{'displayName':'Fixture display device','approvalRules':[rule]},
                                       {'displayName':'Unknown installed driver date','approvalRules':[]}]})
    assert not wizard.hardware_targets.item(1).flags() & Qt.ItemIsEnabled
    wizard.hardware_targets.item(0).setCheckState(Qt.Checked)
    wizard.driver_consent.setChecked(True)
    wizard.driver_agreements.setChecked(True)
    selected=wizard.collect_choices()
    assert selected['driver_policy']=='install_matching'
    assert selected['driver_approved_devices']==[driver_device_rule(update)]
    assert selected['driver_approved_devices'][0]['notOlderThanDriverDate']=='2026-10-01'


def test_new_driver_revision_not_preselected_in_exact_mode(qapp,tmp_path):
    from optimus.automated_system import driver_descriptor
    backend=FixtureBackend(tmp_path,{'setup_version':2,'driver_policy':'install_approved','driver_approved_updates':[driver_descriptor(driver_fixture())]})
    wizard=InitialSetupWizard(backend)
    wizard._drivers_ready({'updates':[driver_fixture(revision=4)],'resultCode':2})
    assert wizard.driver_targets.item(0).checkState()==Qt.Unchecked
    wizard.deleteLater()


def test_incomplete_driver_scan_cannot_make_approval(wizard):
    with pytest.raises(ValueError,match='complete successful'):
        wizard._drivers_ready({'updates':[driver_fixture()],'resultCode':3})
    assert not wizard._drivers_loaded


def test_review_policies_revoke_all_apply_consents(wizard):
    for key in ('duplicate_changes_approved','cloud_changes_approved','startup_changes_approved','driver_changes_approved','driver_update_agreements_accepted'):
        wizard.controls[key].setChecked(True)
    selected=wizard.collect_choices()
    assert all(selected[key] is False for key in ('duplicate_changes_approved','cloud_changes_approved','startup_changes_approved','driver_changes_approved','driver_update_agreements_accepted'))


def test_cancel_during_async_read_does_not_save_or_apply(wizard,backend,qapp):
    import threading
    release=threading.Event()
    backend.startup_list=Mock(side_effect=lambda: (release.wait(2),[startup_fixture()])[1])
    wizard.load_startup_approvals()
    assert wizard.busy
    wizard.reject()
    release.set()
    wait_for_probe(wizard,qapp)
    assert backend.saved==[] and backend.events==[]
    backend.optimize.assert_not_called()


def test_v1_optimize_gate_opens_new_setup(qapp,backend):
    from optimus.ui import OptimusWindow
    backend.settings['setup_version']=1
    window=OptimusWindow(backend,smoke_test=True)
    window.smoke_test=False
    window.open_initial_setup=Mock()
    window.start_optimization()
    window.open_initial_setup.assert_called_once_with(first_run=True)
    backend.optimize.assert_not_called()
    window.smoke_test=True
    window.shutdown();window.close();window.deleteLater()


def test_account_connection_changes_invalidate_saved_cloud_binding(wizard,backend,qapp):
    cloud_fixture(wizard)
    wizard.cloud_consent.setChecked(True)
    backend.cloud_logout=Mock(return_value={'connected':False})
    wizard._run('cloud_logout')
    assert wizard._cloud_binding is None and not wizard.cloud_consent.isChecked()
    wait_for_probe(wizard,qapp)
    assert backend.saved==[]


def test_embedded_connection_panel_cannot_invoke_file_actions(wizard,backend,qapp):
    backend.cloud_status=Mock(return_value={'connected':False,'configured':False})
    backend.cloud_recycle=Mock()
    wizard.open_cloud_connection()
    wait_for_probe(wizard,qapp)
    assert wizard._connection_panel.table.isHidden()
    assert wizard._connection_panel.recycle.isHidden()
    wizard._run('cloud_recycle',{'anything':'unapproved'})
    backend.cloud_recycle.assert_not_called()
    assert 'unavailable' in wizard.cloud_scope_status.text()


def test_complete_scoped_payload_passes_backend_preference_validator(wizard):
    from optimus.workflow import Preferences
    wizard.controls['duplicates_enabled'].setChecked(True)
    wizard.duplicate_roots.setPlainText('D:\\Fixture\\Photos')
    wizard.duplicate_policy.setCurrentIndex(1)
    wizard.duplicate_consent.setChecked(True)
    wizard.controls['cloud_review_enabled'].setChecked(True)
    cloud_fixture(wizard)
    wizard.cloud_policy.setCurrentIndex(2)
    wizard.cloud_consent.setChecked(True)
    wizard.controls['startup_review_enabled'].setChecked(True)
    wizard.startup_policy.setCurrentIndex(1)
    wizard._startup_ready([startup_fixture()])
    wizard.startup_targets.item(0).setCheckState(Qt.Checked)
    wizard.startup_consent.setChecked(True)
    wizard.controls['driver_updates_enabled'].setChecked(True)
    wizard.driver_policy.setCurrentIndex(2)
    checked_hardware_fixture(wizard)
    wizard.driver_consent.setChecked(True)
    wizard.driver_agreements.setChecked(True)
    selected=wizard.collect_choices()
    normalized=Preferences.validate(selected)
    for key in ('duplicate_changes_approved','cloud_changes_approved','startup_changes_approved','driver_changes_approved','driver_update_agreements_accepted'):
        assert normalized[key] is True
    assert normalized['setup_version']==2
    assert normalized['driver_approved_devices'][0]['notOlderThanDriverDate']=='2026-10-01'


def test_summary_is_readable_without_changing_approval_payload(wizard):
    wizard.controls['startup_review_enabled'].setChecked(True)
    wizard.startup_policy.setCurrentIndex(1)
    wizard._startup_ready([startup_fixture()])
    wizard.startup_targets.item(0).setCheckState(Qt.Checked)
    wizard.startup_consent.setChecked(True)
    wizard.controls['driver_updates_enabled'].setChecked(True)
    wizard.driver_policy.setCurrentIndex(2)
    checked_hardware_fixture(wizard)
    wizard.driver_consent.setChecked(True)
    wizard.driver_agreements.setChecked(True)
    before=wizard.collect_choices()
    wizard.refresh_summary()
    text=wizard.summary.toPlainText()
    assert 'Disable sign-in entry: Fixture app' in text
    assert 'Command: C:\\Fixture\\app.exe' in text and 'Source: HKCU Run' in text
    assert 'Approved driver provider: Fixture Hardware; class: Display' in text
    assert 'Driver date must be 2026-10-01 or later' in text
    assert 'RawValue' not in text and 'Fingerprint' not in text and 'RegistryView' not in text
    assert 'disable_selected' not in text and 'install_matching' not in text
    assert wizard.collect_choices()==before
    wizard.driver_policy.setCurrentIndex(1)
    wizard._drivers_ready({'updates':[driver_fixture()],'resultCode':2})
    wizard.driver_targets.item(0).setCheckState(Qt.Checked)
    wizard.driver_consent.setChecked(True)
    wizard.driver_agreements.setChecked(True)
    wizard.refresh_summary()
    assert 'Approved driver update: Fixture display driver; revision 3' in wizard.summary.toPlainText()
