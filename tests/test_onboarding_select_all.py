"""Bulk setup choices stay scoped; inventories and maintenance are fixture-only."""
from __future__ import annotations

import copy
from unittest.mock import Mock

import pytest
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication, QCheckBox, QListWidgetItem, QPushButton

from optimus.onboarding import InitialSetupWizard, REMOVALS
from test_onboarding import FixtureBackend, driver_fixture, startup_fixture, wait_for_probe


GROUPS = ('cleanup', 'uninstaller', 'performance', 'startup', 'software', 'drivers', 'hardware')
CHECKBOX_KEYS = {
    'cleanup': ('cleanup_enabled', 'browser_cache_enabled', 'browser_history_enabled',
                'browser_cookies_enabled', 'browser_download_history_enabled'),
    'uninstaller': ('applications_review_enabled', *(key for key, _ in REMOVALS)),
    'performance': ('performance_review_enabled', 'speed_enhancements_enabled', 'drive_optimization_enabled', 'codex_priority_enabled', 'background_analysis_enabled'),
}


@pytest.fixture(scope='module')
def qapp():
    yield QApplication.instance() or QApplication([])


@pytest.fixture
def wizard(qapp, tmp_path):
    backend = FixtureBackend(tmp_path)
    for name in ('startup_list', 'updates_scan', 'driver_updates_scan', 'driver_devices'):
        setattr(backend, name, Mock(side_effect=AssertionError('Bulk selection must not load inventory.')))
    result = InitialSetupWizard(backend, first_run=True)
    yield result
    result.reject()
    result.deleteLater()
    qapp.processEvents()


def states(wizard):
    return {key: control.isChecked() for key, control in wizard.controls.items()
            if isinstance(control, QCheckBox)}


def action(wizard, group, name):
    wizard._sync_selection_actions()
    wizard.selection_actions[group][name].click()


def checkbox_targets(wizard, group):
    result = [wizard.controls[key] for key in CHECKBOX_KEYS[group]]
    if group == 'cleanup':
        result.extend(wizard.category_controls.values())
        result.extend(wizard.browser_controls.values())
    return result


def add_row(table, title='Fixture', data=None, *, checked=False, enabled=True, checkable=True):
    item = QListWidgetItem(title)
    flags = item.flags() | Qt.ItemIsUserCheckable
    if not enabled:
        flags &= ~Qt.ItemIsEnabled
    if not checkable:
        flags &= ~Qt.ItemIsUserCheckable
    item.setFlags(flags)
    item.setData(Qt.UserRole, data or {})
    item.setCheckState(Qt.Checked if checked else Qt.Unchecked)
    table.addItem(item)
    return item


def seed_list(wizard, group):
    if group == 'startup':
        wizard._startup_ready([startup_fixture()])
        return wizard.startup_targets
    if group == 'software':
        wizard.controls['software_updates_enabled'].setChecked(True)
        wizard.software_policy.setCurrentIndex(wizard.software_policy.findData('selected'))
        wizard._software_candidates_ready({'structured': True, 'exitCode': 0,
            'packages': [{'id': 'Vendor.Fixture', 'name': 'Fixture', 'version': '1.0',
                          'available': '2.0', 'source': 'winget'}]})
        return wizard.software_targets
    if group == 'drivers':
        wizard.driver_policy.setCurrentIndex(wizard.driver_policy.findData('install_approved'))
        wizard._drivers_ready({'resultCode': 2, 'updates': [driver_fixture()]})
        return wizard.driver_targets
    raise AssertionError(group)


def hardware_rule(identifier):
    return {'hardwareId': 'PCI\\VEN_FFFF&DEV_' + identifier,
            'provider': 'Fixture Hardware', 'class': 'Display',
            'notOlderThanDriverDate': '2026-10-01'}


def seed_hardware(wizard, rules=None):
    wizard.driver_policy.setCurrentIndex(wizard.driver_policy.findData('install_matching'))
    wizard._hardware_ready({'devices': [{'displayName': 'Fixture device',
                                       'approvalRules': rules or [hardware_rule('0001')]}]})
    return wizard.hardware_targets


@pytest.mark.parametrize('group', GROUPS)
def test_every_multi_choice_group_has_explicit_select_and_clear(wizard, group):
    actions = wizard.selection_actions[group]
    assert isinstance(actions['select'], QPushButton)
    assert isinstance(actions['clear'], QPushButton)
    assert 'Select all' in actions['select'].text()
    assert 'Clear all' in actions['clear'].text()


@pytest.mark.parametrize('group', CHECKBOX_KEYS)
def test_checkbox_bulk_selection_changes_only_its_page_choices(wizard, group):
    targets = checkbox_targets(wizard, group)
    for control in targets:
        control.setChecked(False)
    before = states(wizard)
    browsers_before = {key: control.isChecked() for key, control in wizard.browser_controls.items()}
    priority = wizard.controls['codex_priority'].currentText()
    age = wizard.controls['cleanup_age_days'].value()
    cadence = next(key for key, button in wizard.cadence_buttons.items() if button.isChecked())
    action(wizard, group, 'select')
    assert all(control.isChecked() for control in targets if control.isEnabled())
    changed = set(CHECKBOX_KEYS[group])
    assert {key: value for key, value in states(wizard).items() if key not in changed} == {
        key: value for key, value in before.items() if key not in changed}
    assert wizard.controls['codex_priority'].currentText() == priority
    assert wizard.controls['cleanup_age_days'].value() == age
    assert wizard.cadence_buttons[cadence].isChecked()
    assert not wizard.run_now_checkbox.isChecked()
    if group != 'cleanup':
        assert {key: control.isChecked() for key, control in wizard.browser_controls.items()} == browsers_before
    action(wizard, group, 'clear')
    assert not any(control.isChecked() for control in targets if control.isEnabled())


@pytest.mark.parametrize('group', CHECKBOX_KEYS)
def test_disabled_checkbox_choices_are_untouched(wizard, group):
    targets = checkbox_targets(wizard, group)
    excluded = targets[0]
    excluded.setChecked(True)
    excluded.setEnabled(False)
    action(wizard, group, 'clear')
    assert excluded.isChecked()
    excluded.setChecked(False)
    action(wizard, group, 'select')
    assert not excluded.isChecked()


@pytest.mark.parametrize('group', ('startup', 'software', 'drivers'))
def test_list_bulk_selection_skips_disabled_and_noncheckable_rows(wizard, group):
    table = seed_list(wizard, group)
    assert table.count() == 1
    eligible = table.item(0)
    disabled = add_row(table, 'Excluded disabled row', enabled=False)
    noncheckable = add_row(table, 'Information only', checkable=False)
    before = states(wizard)
    action(wizard, group, 'select')
    assert eligible.checkState() == Qt.Checked
    assert disabled.checkState() == noncheckable.checkState() == Qt.Unchecked
    # Even pre-existing programmatic states outside the eligible rows stay intact.
    disabled.setCheckState(Qt.Checked)
    noncheckable.setCheckState(Qt.Checked)
    action(wizard, group, 'clear')
    assert eligible.checkState() == Qt.Unchecked
    assert disabled.checkState() == noncheckable.checkState() == Qt.Checked
    assert states(wizard) == before


@pytest.mark.parametrize('group', ('startup', 'software', 'drivers', 'hardware'))
def test_bulk_row_changes_revoke_consent_without_granting_new_approval(wizard, group):
    table = seed_hardware(wizard) if group == 'hardware' else seed_list(wizard, group)
    consents = ([wizard.startup_consent] if group == 'startup' else
                [wizard.software_agreements] if group == 'software' else
                [wizard.driver_consent, wizard.driver_agreements])
    for control in consents:
        control.setChecked(True)
    action(wizard, group, 'select')
    assert table.item(0).checkState() == Qt.Checked
    assert not any(control.isChecked() for control in consents)
    for control in consents:
        control.setChecked(True)
    action(wizard, group, 'clear')
    assert table.item(0).checkState() == Qt.Unchecked
    assert not any(control.isChecked() for control in consents)


@pytest.mark.parametrize('group', ('startup', 'drivers'))
def test_list_selection_does_not_enable_maintenance_module(wizard, group):
    table = seed_list(wizard, group)
    key = 'startup_review_enabled' if group == 'startup' else 'driver_updates_enabled'
    wizard.controls[key].setChecked(False)
    action(wizard, group, 'select')
    assert table.item(0).checkState() == Qt.Checked
    assert not wizard.controls[key].isChecked()


def test_disabled_software_list_cannot_be_selected_or_implicitly_enabled(wizard):
    table = seed_list(wizard, 'software')
    wizard.controls['software_updates_enabled'].setChecked(False)
    action(wizard, 'software', 'select')
    assert table.item(0).checkState() == Qt.Unchecked
    assert not wizard.controls['software_updates_enabled'].isChecked()
    assert not wizard.software_agreements.isChecked()
    assert not wizard.software_all_consent.isChecked()


@pytest.mark.parametrize('group', ('startup', 'software', 'drivers'))
def test_bulk_lists_respect_existing_hundred_entry_approval_limit(wizard, group):
    table = seed_list(wizard, group)
    template = copy.deepcopy(table.item(0).data(Qt.UserRole))
    table.clear()
    for index in range(110):
        data = dict(template)
        if group == 'software':
            data['id'] = 'Vendor.Fixture' + str(index)
        add_row(table, 'Eligible fixture ' + str(index), data)
    reserved = 0
    if group == 'software':
        wizard.software_ids.setPlainText('Vendor.AdvancedOne\nVendor.AdvancedTwo')
        reserved = 2
    action(wizard, group, 'select')
    assert sum(table.item(i).checkState() == Qt.Checked for i in range(table.count())) == 100 - reserved
    action(wizard, group, 'clear')
    assert not any(table.item(i).checkState() == Qt.Checked for i in range(table.count()))
    if group == 'software':
        assert wizard.software_ids.toPlainText() == 'Vendor.AdvancedOne\nVendor.AdvancedTwo'


def test_hardware_bulk_selection_keeps_rule_cap_and_ineligible_rows(wizard, monkeypatch):
    import optimus.onboarding as onboarding
    monkeypatch.setattr(onboarding, 'DEVICE_RULE_LIMIT', 2)
    first = hardware_rule('0001')
    second = hardware_rule('0002')
    third = hardware_rule('0003')
    table = seed_hardware(wizard, [first, second])
    duplicate = add_row(table, 'Same approved hardware identity', {'rules': [first]})
    over_cap = add_row(table, 'Would exceed rule cap', {'rules': [third]})
    disabled = add_row(table, 'Unavailable device', {'rules': [third]}, enabled=False)
    noncheckable = add_row(table, 'Information only', {'rules': [third]}, checkable=False)
    action(wizard, 'hardware', 'select')
    assert table.item(0).checkState() == duplicate.checkState() == Qt.Checked
    assert over_cap.checkState() == disabled.checkState() == noncheckable.checkState() == Qt.Unchecked
    assert len(wizard._driver_rules()) == 2
    disabled.setCheckState(Qt.Checked)
    noncheckable.setCheckState(Qt.Checked)
    action(wizard, 'hardware', 'clear')
    assert table.item(0).checkState() == duplicate.checkState() == Qt.Unchecked
    assert disabled.checkState() == noncheckable.checkState() == Qt.Checked


def test_busy_inventory_disables_bulk_buttons_without_changing_rows(wizard):
    table = seed_list(wizard, 'startup')
    wizard.busy = True
    wizard._sync_selection_actions()
    assert not wizard.selection_actions['startup']['select'].isEnabled()
    wizard.selection_actions['startup']['select'].click()
    assert table.item(0).checkState() == Qt.Unchecked
    wizard.busy = False
    action(wizard, 'startup', 'select')
    assert table.item(0).checkState() == Qt.Checked


def test_bulk_selection_never_loads_saves_or_runs_maintenance(wizard):
    for group in CHECKBOX_KEYS:
        action(wizard, group, 'select')
        action(wizard, group, 'clear')
    for name in ('startup_list', 'updates_scan', 'driver_updates_scan', 'driver_devices',
                 'optimize', 'analysis_start', 'analysis_stop'):
        getattr(wizard.backend, name).assert_not_called()
    assert wizard.backend.saved == []
    assert wizard.backend.events == []


def test_delayed_exact_offer_cannot_survive_visible_hardware_clear(qapp, tmp_path):
    """A response arriving after a mode switch cannot restore a hidden approval."""
    import threading
    from optimus.automated_system import driver_device_rule

    update = driver_fixture()
    rule = driver_device_rule(update)
    backend = FixtureBackend(tmp_path, {'setup_version': 2, 'driver_updates_enabled': True,
        'driver_policy': 'install_matching', 'driver_approved_devices': [rule],
        'driver_changes_approved': True, 'driver_update_agreements_accepted': True})
    wizard = InitialSetupWizard(backend)
    release = threading.Event()
    backend.driver_updates_scan = Mock(side_effect=lambda: (release.wait(5),
        {'resultCode': 2, 'updates': [update]})[1])
    try:
        wizard.driver_policy.setCurrentIndex(wizard.driver_policy.findData('install_approved'))
        wizard.load_driver_approvals()
        assert wizard.busy
        wizard.driver_policy.setCurrentIndex(wizard.driver_policy.findData('install_matching'))
        release.set()
        wait_for_probe(wizard, qapp)
        assert wizard.driver_targets.isHidden()
        assert wizard.driver_targets.item(0).checkState() == Qt.Unchecked
        # An offer scan does not substitute for a fresh visible hardware inventory.
        wizard.driver_consent.setChecked(True)
        wizard.driver_agreements.setChecked(True)
        with pytest.raises(ValueError, match='fresh drivers'):
            wizard.collect_choices()
        wizard._hardware_ready({'devices': [{'displayName': 'Saved fixture device',
                                            'approvalRules': [rule]}]})
        assert wizard.hardware_targets.item(0).checkState() == Qt.Checked
        action(wizard, 'hardware', 'clear')
        assert wizard._driver_rules() == []
        assert not wizard.driver_consent.isChecked()
        assert not wizard.driver_agreements.isChecked()
        wizard.driver_consent.setChecked(True)
        wizard.driver_agreements.setChecked(True)
        with pytest.raises(ValueError, match='at least one'):
            wizard.collect_choices()
        assert backend.saved == []
        backend.optimize.assert_not_called()
    finally:
        release.set()
        if wizard.busy:
            wait_for_probe(wizard, qapp)
        wizard.reject()
        wizard.deleteLater()
        qapp.processEvents()


def test_matching_rule_budget_and_saved_rules_ignore_hidden_offer_checks(wizard, monkeypatch):
    """A stale checked offer cannot consume the visible hardware rule budget."""
    import optimus.onboarding as onboarding
    monkeypatch.setattr(onboarding, 'DEVICE_RULE_LIMIT', 1)
    update = driver_fixture()
    wizard._drivers_ready({'resultCode': 2, 'updates': [update]})
    visible_rule = hardware_rule('0002')
    table = seed_hardware(wizard, [visible_rule])
    # Defense against stale state as well as the delayed-response path above.
    wizard.driver_targets.item(0).setCheckState(Qt.Checked)
    action(wizard, 'hardware', 'select')
    assert table.item(0).checkState() == Qt.Checked
    assert wizard._driver_rules() == [visible_rule]
    action(wizard, 'hardware', 'clear')
    assert wizard._driver_rules() == []
