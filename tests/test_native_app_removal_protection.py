"""Saved application protections use synthetic inventories and uninstallers."""
import copy
import json

import pytest

from optimus.app_removal_protection import (WINDOWS_APP_FAMILY, WINDOWS_APP_NAME, WINDOWS_APP_RECORD,
    debloat_target_protected, is_removal_protected, removal_app_record, validate_app_removal_exclusions)
from optimus.launch_review import launch_warning
from optimus.maintenance import Maintenance
from optimus.unattended_configuration import approve_unattended_settings, matches_unattended
from optimus.workflow import DEFAULTS, Preferences, Workflow, full_optimization_settings

from test_full_optimization import fixture_backend


def package(name, identifier, *, publisher='8wekyb3d8bbwe', kind='appx'):
    return {'id': identifier, 'name': name, 'kind': kind, 'fingerprint': 'a' * 64,
            'scope': 'current-user' if kind == 'appx' else 'future-users',
            'packageFullName': name + '_7.5.9.0_x64__' + publisher}


class Bridge:
    def __init__(self, *targets):
        self.targets = list(targets)
        self.applied = []

    def request(self, operation, **arguments):
        if operation == 'maintenance_preview':
            return {'targets': copy.deepcopy(self.targets), 'summary': 'Fixture preview.'}
        assert operation == 'maintenance_apply'
        self.applied.append(arguments)
        return {'results': [{'id': row['id'], 'removed': True} for row in arguments['targets']]}


def test_exact_store_inventory_and_aliases_keep_identity_across_versions():
    assert removal_app_record({'Type': 'Store', 'Identity': WINDOWS_APP_FAMILY, 'Name': WINDOWS_APP_NAME,
                               'DisplayName': 'Windows App'}) == WINDOWS_APP_RECORD
    assert removal_app_record({'Type': 'Store', 'Identity': WINDOWS_APP_FAMILY.replace('_', '/'),
                               'Name': WINDOWS_APP_NAME}) is None
    assert removal_app_record({'Type': []}) is None
    rows = [WINDOWS_APP_RECORD]
    assert debloat_target_protected(rows, package(WINDOWS_APP_NAME, 'current'))
    assert debloat_target_protected(rows, package(WINDOWS_APP_NAME, 'provisioned', kind='provisioned'))
    assert not debloat_target_protected(rows, package(WINDOWS_APP_NAME, 'other', publisher='otherpub00000'))
    assert not debloat_target_protected(rows, package(WINDOWS_APP_NAME + '.Preview', 'similarly-named'))
    assert is_removal_protected(rows, identity='9N1F85V9T8BN')
    assert is_removal_protected(rows, names=('Windows App',))
    assert not is_removal_protected(rows, names=('Windows Application',))
    with pytest.raises(ValueError, match='unique exact'):
        validate_app_removal_exclusions([WINDOWS_APP_RECORD, dict(WINDOWS_APP_RECORD, id=WINDOWS_APP_FAMILY.lower())])
    with pytest.raises(ValueError, match='identity'):
        validate_app_removal_exclusions([dict(WINDOWS_APP_RECORD, command='not data')])


def test_stale_mixed_plan_skips_protected_app_and_applies_only_other_exact_target(tmp_path):
    protected = package(WINDOWS_APP_NAME, 'windows-app')
    other = package('Microsoft.BingWeather', 'weather')
    bridge = Bridge(protected, other)
    rows = []
    tool = Maintenance(bridge, tmp_path, removal_exclusions_reader=lambda: rows)
    plan = tool.debloat_preview('consumer-apps')
    assert [item['id'] for item in plan['targets']] == ['windows-app', 'weather']
    rows.append(WINDOWS_APP_RECORD)
    outcome = tool.debloat_apply({'planId': plan['planId'], 'selectedIds': ['windows-app', 'weather'], 'confirmed': True})
    assert [item['id'] for item in bridge.applied[0]['targets']] == ['weather']
    assert bridge.applied[0]['removalExclusions'] == rows
    assert [(item['id'], item['removed']) for item in outcome['results']] == [('windows-app', False), ('weather', True)]
    assert outcome['results'][0]['protected'] is True
    saved = json.loads((tool.plan_dir / (plan['planId'] + '.json')).read_text())
    assert saved['state'] == 'completed'
    assert saved['result']['results'] == outcome['results']
    refreshed = tool.debloat_preview('consumer-apps')
    assert refreshed['protectedCount'] == 1
    assert [item['id'] for item in refreshed['targets']] == ['weather']


def test_all_protected_stale_selection_does_not_cross_native_boundary(tmp_path):
    bridge = Bridge(package(WINDOWS_APP_NAME, 'windows-app'))
    rows = []
    tool = Maintenance(bridge, tmp_path, removal_exclusions_reader=lambda: rows)
    plan = tool.debloat_preview('consumer-apps')
    rows.append(WINDOWS_APP_RECORD)
    result = tool.debloat_apply({'planId': plan['planId'], 'selectedIds': ['windows-app'], 'confirmed': True})
    assert result['skipped'] is True and result['results'][0]['reason'] == 'app_removal_excluded'
    assert bridge.applied == []
    assert tool.debloat_preview('consumer-apps')['canApply'] is False


@pytest.mark.parametrize('kind', ['edge', 'desktop-edge-script', 'office-dialog'])
def test_desktop_target_uses_exact_registered_identity_or_known_edge_alias(kind):
    display = 'Microsoft Office Fixture' if kind == 'office-dialog' else 'Microsoft Edge'
    row = removal_app_record({'Type': 'Desktop', 'Identity': '{FAKE-KEY}', 'Name': display})
    target = {'kind': kind, 'name': 'Microsoft Edge browser', 'appIdentity': '{FAKE-KEY}'}
    assert debloat_target_protected([row], target)
    assert not debloat_target_protected([dict(row, id='other-key', name=display + ' Beta')], target)


def test_workflow_preserves_windows_app_with_software_updater_off(tmp_path):
    backend, _, _ = fixture_backend(tmp_path)
    backend.debloat_preview.return_value = {'planId': 'fixture', 'targets': [
        package(WINDOWS_APP_NAME, 'windows-app'), package('Microsoft.BingWeather', 'weather')]}
    backend.debloat_apply.return_value = {'results': [{'id': 'weather', 'removed': True}]}
    config = dict(DEFAULTS, cleanup_enabled=False, browser_cache_enabled=False, consumer_debloat_enabled=True,
                  consumer_debloat_package_names=[WINDOWS_APP_NAME, 'Microsoft.BingWeather'],
                  app_removal_exclusions=[WINDOWS_APP_RECORD])
    assert config['software_updates_enabled'] is False
    report = Workflow(backend).run(config, lambda _: None)
    backend.debloat_apply.assert_called_once_with({'planId': 'fixture', 'selectedIds': ['weather'], 'confirmed': True})
    stage = next(row for row in report['stages'] if row['title'] == 'Remove selected consumer app group')
    assert stage['status'] == 'completed'
    assert stage['result']['protected_count'] == 1
    assert stage['result']['results'][0]['protected'] is True
    assert full_optimization_settings(config)['app_removal_exclusions'] == [WINDOWS_APP_RECORD]
    assert Preferences.validate(config)['app_removal_exclusions'] == [WINDOWS_APP_RECORD]
    text = launch_warning(config)
    assert 'Microsoft Windows App' in text and WINDOWS_APP_NAME not in text


def test_unattended_approval_accepts_new_empty_default_but_binds_nonempty_protections():
    previous = dict(DEFAULTS, setup_version=2, unattended_auto_clean_enabled=True)
    previous.pop('app_removal_exclusions')
    approved = approve_unattended_settings(previous)
    assert matches_unattended(dict(approved, app_removal_exclusions=[]))
    protected = dict(approved, app_removal_exclusions=[WINDOWS_APP_RECORD])
    assert not matches_unattended(protected)
    assert matches_unattended(approve_unattended_settings(protected))
    assert not matches_unattended(dict(approve_unattended_settings(protected), app_removal_exclusions=[]))
