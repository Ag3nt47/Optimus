"""Removal enforcement runs only against temporary data and fake backends."""
from __future__ import annotations

import copy
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from optimus.app_removal_protection import WINDOWS_APP_FAMILY, WINDOWS_APP_NAME, WINDOWS_APP_RECORD
from optimus.automation import RunLease
from optimus.removal_enforcement import (GROUPS, SCOPE_KEY, RemovalGuardRegistration, approved,
    approval_scope, blocked_package_ids, package_blocked, run_saved_removals, scope_hash, stamp_approval)
from optimus.workflow import DEFAULTS


def configuration(**flags):
    result = {flag: False for flag, _ in GROUPS}
    result.update(setup_version=2, windows_removal_enforcement_enabled=True,
                  consumer_debloat_package_names=[], app_removal_exclusions=[dict(WINDOWS_APP_RECORD)])
    result.update(flags)
    return result


def target(identity, name=None, **values):
    return dict(id=identity, name=name or identity, appIdentity=identity, **values)


class FakeBackend:
    system = 'win32'

    def __init__(self, directory, settings, *, targets=None, results=None, errors=None):
        self.data_dir = Path(directory)
        self.data_dir.mkdir(exist_ok=True)
        self.settings = copy.deepcopy(settings)
        self.targets = targets or {}
        self.results = results or {}
        self.errors = errors or {}
        self.previews = []
        self.applied = []
        self.privacy_calls = []
        self.policy_calls = []
        self.policy_error = None
        self.after_preview = None
        self.after_apply = None

    def preferences(self):
        return copy.deepcopy(self.settings)

    def debloat_preview(self, options):
        self.previews.append(dict(options))
        action = options['action']
        if action in self.errors:
            raise RuntimeError(self.errors[action])
        value = {'planId': action + '-plan', 'targets': copy.deepcopy(self.targets.get(action, [target(action)]))}
        if self.after_preview:
            self.after_preview(action)
        return value

    def debloat_apply(self, selection):
        self.applied.append(copy.deepcopy(selection))
        action = selection['planId'].removesuffix('-plan')
        if self.after_apply:
            self.after_apply(action)
        return copy.deepcopy(self.results.get(action, {'results': [
            {'id': identity, 'removed': True, 'restartNeeded': False}
            for identity in selection['selectedIds']]}))

    def apply_system_privacy(self, settings):
        self.privacy_calls.append(copy.deepcopy(settings))
        return {'status': 'fixture_privacy_applied'}

    def edge_install_policy(self):
        self.policy_calls.append(True)
        if self.policy_error:
            raise RuntimeError(self.policy_error)
        return {'supported': False, 'applied': False, 'reason': 'fixture_unsupported'}


def test_default_choices_without_saved_hash_cannot_launch_uninstallers(tmp_path):
    settings = copy.deepcopy(DEFAULTS)
    settings['setup_version'] = 2
    settings[SCOPE_KEY] = ''
    backend = FakeBackend(tmp_path, settings)
    report = run_saved_removals(backend)
    assert report['status'] == 'approval_required'
    assert backend.previews == backend.applied == backend.privacy_calls == []
    assert backend.policy_calls == []
    assert not (tmp_path / 'maintenance-run.lock').exists()


@pytest.mark.parametrize('change', [
    {'edge_debloat_enabled': False},
    {'consumer_debloat_package_names': ['Microsoft.BingWeather']},
    {'app_removal_exclusions': []},
    {'privacy_advertising_enabled': True},
    {'windows_removal_enforcement_enabled': False},
])
def test_saved_approval_is_bound_to_choices_and_exclusions(change):
    settings = stamp_approval(configuration(edge_debloat_enabled=True))
    assert approved(settings)
    assert not approved(dict(settings, **change))
    assert approval_scope(settings)['preserved'] == [WINDOWS_APP_FAMILY, 'Microsoft.EdgeWebView2']


def test_only_saved_groups_and_selected_consumer_names_are_applied(tmp_path):
    settings = stamp_approval(configuration(consumer_debloat_enabled=True, edge_debloat_enabled=True,
        office_desktop_debloat_enabled=True, consumer_debloat_package_names=['Microsoft.BingWeather', WINDOWS_APP_NAME]))
    backend = FakeBackend(tmp_path, settings, targets={
        'consumer-apps': [target('weather', 'Microsoft.BingWeather'), target('news', 'Microsoft.BingNews'),
                          target('windows', WINDOWS_APP_NAME, kind='appx'),
                          target(WINDOWS_APP_FAMILY, 'Renamed display label', kind='appx')],
        'office-desktop': [target('office')], 'edge': [target('edge-machine')],
    })
    report = run_saved_removals(backend)
    assert report['status'] == 'completed'
    assert [row['action'] for row in backend.previews] == ['consumer-apps', 'office-desktop', 'edge']
    assert [row['includeProvisioned'] for row in backend.previews] == [True, False, True]
    assert [row['selectedIds'] for row in backend.applied] == [['weather'], ['office'], ['edge-machine']]
    assert all(row['confirmed'] is True for row in backend.applied)
    assert backend.privacy_calls == [settings]
    saved = json.loads((tmp_path / 'removal-enforcement' / 'latest.json').read_text())
    assert saved == report and saved['windows_app_preserved'] and saved['webview2_preserved']


def test_failed_group_and_failed_item_do_not_block_next_group(tmp_path):
    settings = stamp_approval(configuration(m365_debloat_enabled=True,
        office_desktop_debloat_enabled=True, edge_debloat_enabled=True))
    backend = FakeBackend(tmp_path, settings, errors={'m365': 'Fixture scan failed'}, results={
        'office-desktop': {'results': [{'id': 'office-first', 'removed': False, 'error': 'Fixture uninstall failed'},
                                        {'id': 'office-next', 'removed': True}]}})
    report = run_saved_removals(backend, apply_privacy=False)
    assert report['status'] == 'completed_with_findings'
    assert [row['action'] for row in backend.previews] == ['m365', 'office-desktop', 'edge']
    assert [row['planId'] for row in backend.applied] == ['office-desktop-plan', 'edge-plan']
    assert report['actions'][0]['status'] == 'failed'
    assert report['actions'][1]['result']['results'][1]['removed'] is True
    assert backend.privacy_calls == []


def test_live_unknown_installer_defers_later_conflicting_removals(tmp_path):
    settings = stamp_approval(configuration(office_desktop_debloat_enabled=True, edge_debloat_enabled=True))
    backend = FakeBackend(tmp_path, settings, results={'office-desktop': {'results': [
        {'id': 'office', 'removed': False, 'running': True, 'outcomeKnown': False}]}})
    report = run_saved_removals(backend, apply_privacy=False)
    assert report['status'] == 'completed_with_findings'
    assert len(backend.previews) == len(backend.applied) == 1
    assert report['actions'][-1]['status'] == 'installer_outcome_pending'


def test_native_timeout_keeps_run_lease_until_pending_uninstaller_settles(tmp_path):
    settings = stamp_approval(configuration(office_desktop_debloat_enabled=True, edge_debloat_enabled=True))
    backend = FakeBackend(tmp_path, settings)
    observed = []

    def await_pending(operation):
        assert operation == 'maintenance_apply'
        assert RunLease.owned_by_current_process(tmp_path)
        with pytest.raises(RuntimeError):
            RunLease(tmp_path).acquire()
        observed.append(operation)

    backend.bridge = SimpleNamespace(wait_for_pending_operation=await_pending)
    backend.after_apply = lambda unused: (_ for _ in ()).throw(TimeoutError('Fixture native call still running'))
    report = run_saved_removals(backend, apply_privacy=False)
    assert observed == ['maintenance_apply']
    assert len(backend.previews) == len(backend.applied) == 1
    assert report['status'] == 'completed_with_findings'
    assert report['actions'][-1]['status'] == 'installer_outcome_pending'
    with RunLease(tmp_path):
        pass


def test_bridge_pending_observer_waits_without_submitting_or_terminating(monkeypatch, tmp_path):
    from optimus.bridge import PowerShellBridge
    bridge = PowerShellBridge(tmp_path)
    bridge._pending['fixture'] = ('maintenance_apply', 123)
    observed = []

    def settle(delay):
        observed.append(delay)
        bridge._pending.clear()

    monkeypatch.setattr('optimus.bridge.time.sleep', settle)
    bridge.wait_for_pending_operation('maintenance_apply')
    assert observed and bridge._process is None


def test_changed_scope_after_one_action_prevents_later_actions(tmp_path):
    settings = stamp_approval(configuration(m365_debloat_enabled=True, edge_debloat_enabled=True))
    backend = FakeBackend(tmp_path, settings)
    backend.after_apply = lambda unused: backend.settings.update(edge_debloat_enabled=False)
    report = run_saved_removals(backend)
    assert report['status'] == 'completed_with_findings'
    assert len(backend.applied) == 1
    assert report['actions'][-1] == {'action': 'edge', 'status': 'approval_changed'}
    assert backend.privacy_calls == []


def test_scope_changed_while_previewing_cannot_launch_that_removal(tmp_path):
    settings = stamp_approval(configuration(edge_debloat_enabled=True))
    backend = FakeBackend(tmp_path, settings)
    backend.after_preview = lambda unused: backend.settings.update(edge_debloat_enabled=False)
    report = run_saved_removals(backend)
    assert backend.applied == []
    assert backend.privacy_calls == []
    assert report['status'] == 'completed_with_findings'
    assert any(row.get('status') == 'approval_changed' for row in report['actions'])


def test_scope_changed_while_acquiring_lease_prevents_policy_and_removal(tmp_path, monkeypatch):
    settings = stamp_approval(configuration(edge_debloat_enabled=True))
    backend = FakeBackend(tmp_path, settings)

    class ChangeAfterAcquire:
        def __init__(self, directory):
            self.actual = RunLease(directory)

        def acquire(self):
            self.actual.acquire()
            backend.settings = stamp_approval(dict(backend.settings, edge_debloat_enabled=False))
            return self.actual

    monkeypatch.setattr('optimus.automation.RunLease', ChangeAfterAcquire)
    report = run_saved_removals(backend)
    assert backend.policy_calls == backend.previews == backend.applied == backend.privacy_calls == []
    assert report['status'] == 'completed_with_findings'


def test_policy_error_is_reported_without_blocking_removal(tmp_path):
    backend = FakeBackend(tmp_path, stamp_approval(configuration(edge_debloat_enabled=True)))
    backend.policy_error = 'Fixture policy write denied'
    report = run_saved_removals(backend)
    assert len(backend.applied) == 1
    assert report['edge_install_policy']['error'] == 'Fixture policy write denied'
    assert report['status'] == 'completed_with_findings'


def test_busy_native_lease_launches_no_operation_and_is_not_replaced(tmp_path):
    settings = stamp_approval(configuration(edge_debloat_enabled=True))
    backend = FakeBackend(tmp_path, settings)
    with RunLease(tmp_path):
        report = run_saved_removals(backend)
        assert report['status'] == 'busy_deferred'
        assert backend.previews == backend.applied == backend.privacy_calls == []
    with RunLease(tmp_path):
        pass


def test_native_platform_reports_unavailable_without_windows_actions(tmp_path):
    backend = FakeBackend(tmp_path, stamp_approval(configuration(edge_debloat_enabled=True)))
    backend.system = 'darwin'
    assert run_saved_removals(backend)['status'] == 'unavailable'
    assert backend.previews == backend.applied == []


def test_update_exclusions_are_exact_and_preserve_shared_webview_and_windows_app():
    settings = configuration(edge_debloat_enabled=True, office365_desktop_debloat_enabled=True,
                             office_desktop_debloat_enabled=True, m365_debloat_enabled=True)
    blocked = blocked_package_ids(settings)
    assert 'Microsoft.Edge' in blocked and 'Microsoft.Office365ProPlus' in blocked
    assert 'Microsoft.Office2024' in blocked and 'Microsoft.MicrosoftOfficeHub' in blocked
    assert package_blocked(settings, 'microsoft.edge')
    for identity in ('Microsoft.EdgeWebView2', 'Microsoft.EdgeWebView2Runtime', WINDOWS_APP_FAMILY,
                     WINDOWS_APP_NAME, 'Microsoft.WindowsApp', '9N1F85V9T8BN', 'Google.Chrome'):
        assert not package_blocked(settings, identity)
    assert not package_blocked(settings, None)
    assert not package_blocked(settings, 'Microsoft.Edge.Beta')  # No approved beta-channel removal exists.


def test_scope_hash_is_stable_for_consumer_selection_order():
    first = configuration(consumer_debloat_package_names=['Microsoft.BingWeather', 'Microsoft.BingNews'])
    second = configuration(consumer_debloat_package_names=['Microsoft.BingNews', 'Microsoft.BingWeather'])
    assert scope_hash(first) == scope_hash(second)
    with pytest.raises(ValueError, match='booleans'):
        scope_hash(configuration(edge_debloat_enabled='yes'))


def test_registration_uses_only_fixed_operations_and_owned_launch_manifest(tmp_path):
    calls = []
    settings = stamp_approval(configuration(edge_debloat_enabled=True))
    tool = RemovalGuardRegistration(tmp_path, runner=lambda request: calls.append(request) or {'ok': True})
    assert tool.configure(True, settings) == {'ok': True}
    assert tool.configure(False) == {'ok': True}
    assert tool.status() == {'ok': True}
    assert [row['operation'] for row in calls] == ['enable', 'disable', 'status']
    assert all(row['dataRoot'] == str(tmp_path) and row['launchManifest'] == str(tmp_path / 'startup-launch.json')
               for row in calls)
    assert calls[0]['approvedScope'] == approval_scope(settings)
    assert calls[0]['scopeHash'] == scope_hash(settings)
    assert set(calls[0]) == {'operation', 'dataRoot', 'launchManifest', 'approvedScope', 'scopeHash'}
    assert all(set(row) == {'operation', 'dataRoot', 'launchManifest'} for row in calls[1:])


@pytest.mark.parametrize('settings', [None, configuration(edge_debloat_enabled=True)])
def test_registration_requires_saved_approval_before_native_task(settings, tmp_path):
    calls = []
    tool = RemovalGuardRegistration(tmp_path, runner=lambda request: calls.append(request))
    with pytest.raises(ValueError, match='Save the current removal choices'):
        tool.configure(True, settings)
    assert calls == []


def test_registration_rejects_native_error_without_running_a_real_task(tmp_path, monkeypatch):
    calls = []

    def native(arguments, **options):
        calls.append((arguments, options))
        return SimpleNamespace(returncode=1, stdout='{"ok": false, "error": "Fixture native refusal"}')

    monkeypatch.setattr('optimus.removal_enforcement.subprocess.run', native)
    with pytest.raises(RuntimeError, match='Fixture native refusal'):
        RemovalGuardRegistration(tmp_path).configure(True, stamp_approval(configuration(edge_debloat_enabled=True)))
    assert len(calls) == 1 and '-RequestJson' in calls[0][0]
    assert json.loads(calls[0][1]['input'])['operation'] == 'enable'
