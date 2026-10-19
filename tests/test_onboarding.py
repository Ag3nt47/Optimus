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
    expected = {'health','cleanup','uninstaller','duplicates','cloud','performance','startup','software','drivers'}
    assert expected.issubset(wizard.page_ids)
    assert len(wizard.pageIds()) == 11
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
    assert selected['setup_version'] == 1
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
    assert 'no installation' in text
    assert 'No automatic duplicate/cloud deletion' in text
    assert 'sign-in required' in text


def test_finish_records_baseline_before_saving_without_run(wizard, backend):
    wizard.accept()
    assert wizard.result() == QDialog.Accepted
    assert backend.events == ['configured','save']
    assert len(backend.saved) == 1
    assert wizard.saved_preferences['setup_version'] == 1
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
    assert len(window.main_buttons) == 3
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
    assert len(calls) == 1 and calls[0]['setup_version'] == 1
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
    assert [button.text() for button in main.main_buttons] == ['1 click Optimize and debloat','Settings','Sandbox']
    main.shutdown()
    main.close()
    detail.close()
    main.deleteLater()
    detail.deleteLater()
