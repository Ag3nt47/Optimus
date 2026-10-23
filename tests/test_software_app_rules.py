"""Exact app choices use copied settings and disposable GUI fixtures only."""
import copy
import os
from types import SimpleNamespace
from unittest.mock import Mock

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

import pytest
from PySide6.QtWidgets import QApplication

from optimus.launch_review import LaunchConsent, launch_warning
from optimus.native_settings import NativePreferences, full_native_settings
from optimus.software_app_rules import apply_update_rules, app_rules_summary, rule_for, validate_app_rules
from optimus.unattended_configuration import approve_unattended_settings, matches_unattended
from optimus.widgets.app_rules_editor import AppRulesEditor
from optimus.workflow import DEFAULTS, Preferences


def rule(identity='Fixture.App', *, source='winget', update='always_update', presence='keep_current'):
    return dict(id=identity, source=source, update_policy=update, presence=presence)


def settings(**changes):
    return dict(copy.deepcopy(DEFAULTS), setup_version=2, **changes)


@pytest.fixture(scope='module')
def app():
    return QApplication.instance() or QApplication([])


@pytest.mark.parametrize('rows', [None, {}, [dict(rule(), unexpected=True)],
    [dict(rule(), id='--bad')], [dict(rule(), source='unknown')],
    [dict(rule(), update_policy='force')], [dict(rule(), presence='delete')],
    [rule(), rule('fixture.app')], [rule(presence='keep_uninstalled')], [rule()] * 101])
def test_invalid_or_ambiguous_rules_are_rejected(rows):
    with pytest.raises(ValueError):
        validate_app_rules(rows)


def test_native_identity_source_and_path_validation():
    value = rule('brew:homebrew/core/wget', source='brew')
    assert validate_app_rules([value], native=True) == [value]
    for bad in (rule('brew:../evil', source='brew'), rule('apt:firefox', source='brew'), value):
        with pytest.raises(ValueError):
            validate_app_rules([bad], native=False)
    with pytest.raises(ValueError):
        validate_app_rules([rule('brew:../evil', source='brew')], native=True)


def test_review_global_only_explicit_update_rules_are_effective_without_mutating_approvals():
    value = settings(software_app_rules=[rule()], software_update_ids=['Legacy.Unlisted'])
    before = copy.deepcopy(value)
    effective = apply_update_rules(value)
    assert effective['software_update_policy'] == 'selected'
    assert effective['software_update_ids'] == ['Fixture.App']
    assert not effective['software_updates_enabled']
    assert not effective['software_update_agreements_accepted']
    assert value == before


def test_exclusions_win_without_erasing_legacy_ids_or_other_controls():
    value = settings(software_update_policy='selected', software_update_ids=['Legacy.Unlisted'],
        software_update_exclude_ids=['FIXTURE.APP'], software_app_rules=[rule()], custom_pin='keep')
    effective = apply_update_rules(value)
    assert effective['software_update_ids'] == ['Legacy.Unlisted']
    assert effective['software_update_exclude_ids'] == ['FIXTURE.APP']
    assert effective['custom_pin'] == 'keep'
    assert rule_for(value['software_app_rules'], 'fixture.app') == rule()


def test_all_supported_does_not_expand_irrelevant_selected_scope():
    value = settings(software_update_policy='all_supported', software_update_ids=[f'Legacy.App{i}' for i in range(100)],
                     software_app_rules=[rule()])
    assert apply_update_rules(value)['software_update_ids'] == value['software_update_ids']


@pytest.mark.parametrize('validator,native', [(Preferences.validate, False), (NativePreferences.validate, True)])
def test_explicit_rules_require_agreements_and_current_setup(validator, native):
    identity, source = ('apt:fixture', 'apt') if native else ('Fixture.App', 'winget')
    value = settings(software_updates_enabled=True, software_app_rules=[rule(identity, source=source)])
    if native:
        value = dict(full_native_settings(value), **{key:value[key] for key in ('software_updates_enabled','software_app_rules')})
    with pytest.raises(ValueError):
        validator(value)
    value['software_update_agreements_accepted'] = True
    assert validator(value)['software_app_rules'] == value['software_app_rules']
    with pytest.raises(ValueError):
        validator(dict(value, setup_version=0))
    assert validator(dict(value, software_update_policy='selected'))['software_update_ids'] == []


def test_app_scope_changes_invalidate_both_launch_and_unattended_approval():
    old = approve_unattended_settings(Preferences.validate(settings(unattended_auto_clean_enabled=True)))
    gate = LaunchConsent(); gate.approve(old)
    changed = dict(old, software_app_rules=[rule(update='always_ignore')])
    assert not matches_unattended(changed)
    assert not gate.allows(changed)


def test_warning_names_exact_reinstall_and_removal_scopes_and_irreversible_risk():
    value = settings(software_updates_enabled=True, software_update_agreements_accepted=True,
        cleanup_enabled=False, browser_cache_enabled=False, software_app_rules=[
            rule('Fixture.Keep', presence='keep_installed'),
            rule('Fixture.Remove', update='always_ignore', presence='keep_uninstalled')])
    text = launch_warning(value)
    assert 'Install or reinstall these exact apps if missing: Fixture.Keep [winget]' in text
    assert 'Uninstall these exact apps if installed' in text and 'Fixture.Remove [winget]' in text
    assert 'irreversible' in text and 'app-local data can be permanently lost' in text
    assert 'Software update discovery only' not in text
    assert app_rules_summary(value)['automatic_actions']


def test_widget_only_saves_explicit_rules_and_resolves_conflicting_choices(app):
    value = settings(software_update_exclude_ids=['FIXTURE.APP', 'Legacy.Unlisted'])
    editor = AppRulesEditor(value)
    editor.set_candidates([dict(id='Fixture.App', source='winget', name='Fixture')])
    assert editor.apply_to(value)['software_app_rules'] == []
    key = ('winget', 'fixture.app')
    editor._render(key)
    editor.update_choice.setCurrentIndex(editor.update_choice.findData('always_update'))
    assert value['software_update_exclude_ids'] == ['FIXTURE.APP', 'Legacy.Unlisted']
    draft = editor.apply_to(value)
    assert draft['software_update_exclude_ids'] == ['Legacy.Unlisted']
    assert draft['software_app_rules'][0]['update_policy'] == 'always_update'
    editor.presence_choice.setCurrentIndex(editor.presence_choice.findData('keep_uninstalled'))
    assert editor.update_choice.currentData() == 'always_ignore'
    editor.update_choice.setCurrentIndex(editor.update_choice.findData('follow_global'))
    assert editor.presence_choice.currentData() == 'keep_current'
    editor.deleteLater()


def test_widget_native_presence_is_disabled_and_saved_unlisted_rule_survives_scan(app):
    value = settings(software_app_rules=[rule('apt:fixture', source='apt', presence='keep_installed')])
    editor = AppRulesEditor(value, native=True)
    assert not editor.presence_choice.isEnabled()
    editor.set_candidates([dict(id='apt:other', source='apt')])
    assert editor.apply_to(value)['software_app_rules'] == value['software_app_rules']
    editor.deleteLater()


def test_settings_draft_and_save_use_existing_approval_then_no_host_action(app, tmp_path):
    from optimus.ui import DetailedSettingsWindow
    value = Preferences.validate(settings(software_updates_enabled=True))
    backend = SimpleNamespace(data_dir=tmp_path, preferences=lambda: copy.deepcopy(value),
        cleanup_categories=lambda: [], preference_store=SimpleNamespace(validate=Preferences.validate))
    detail = DetailedSettingsWindow(backend, smoke_test=True)
    detail.smoke_test = False
    captured = []
    detail._run = lambda *args, **kwargs: captured.append((args, kwargs))
    detail._error = Mock()
    detail.app_rules_editor.set_candidates([dict(id='Fixture.App', source='winget')])
    detail.app_rules_editor.update_choice.setCurrentIndex(1)
    detail._save_app_rules()
    assert not captured and detail._error.call_count == 1
    detail.app_rules_agreements.setChecked(True)
    detail._save_app_rules()
    assert len(captured) == 1
    args, callbacks = captured[0]
    assert args[0] == 'save_preferences'
    assert args[1]['software_app_rules'] == [rule()]
    assert not value['software_app_rules']
    callbacks['done'](args[1])
    value['software_update_exclude_ids'] = ['External.Scope']
    detail._save_app_rules()
    assert len(captured) == 1 and detail._error.call_count == 2
    detail.smoke_test = True
    detail.shutdown(); detail.close(); detail.deleteLater()


def test_windows_setup_rule_actions_survive_global_review_and_summary(app, tmp_path):
    from optimus.onboarding import InitialSetupWizard
    backend = SimpleNamespace(data_dir=tmp_path, preferences=lambda: settings(), cleanup_categories=lambda: [])
    wizard = InitialSetupWizard(backend)
    wizard.controls['software_updates_enabled'].setChecked(True)
    wizard.software_app_rules.set_candidates([dict(id='Fixture.App', source='winget')])
    wizard.software_app_rules.presence_choice.setCurrentIndex(2)
    assert wizard.controls['software_update_agreements_accepted'].isEnabled()
    wizard.controls['software_update_agreements_accepted'].setChecked(True)
    value = wizard.collect_choices()
    assert value['software_update_policy'] == 'review'
    assert value['software_update_agreements_accepted']
    assert value['software_app_rules'] == [rule(update='always_ignore', presence='keep_uninstalled')]
    wizard.refresh_summary()
    assert 'Fixture.App' in wizard.summary.toPlainText()
    wizard.reject(); wizard.deleteLater()


def test_native_setup_preserves_unoffered_and_ineligible_exact_ids(app, tmp_path):
    from optimus.native_ui import NativeSetupWizard
    value = NativePreferences.validate(dict(full_native_settings(), setup_version=2,
        software_update_policy='selected', software_update_ids=['apt:missing', 'apt:held'],
        software_update_agreements_accepted=True))
    backend = SimpleNamespace(data_dir=tmp_path, preferences=lambda: copy.deepcopy(value), cleanup_categories=lambda: [])
    wizard = NativeSetupWizard(backend)
    wizard._software_ready({'updates':[dict(id='apt:held', name='Held', eligible=False), dict(id='apt:new', eligible=True)]})
    assert set(wizard.collect()['software_update_ids']) == {'apt:missing', 'apt:held'}
    assert wizard.controls['software_update_agreements_accepted'].isChecked()
    wizard.reject(); wizard.deleteLater()
