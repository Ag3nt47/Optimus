"""System privacy/security checks use registry/Defender fixtures exclusively."""
import copy
import json
import os
import threading
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest

from optimus.system_privacy import SystemPrivacy, _CONTROLS, _script


def dword(value):
    return {'exists': True, 'type': 'DWord', 'value': value}


def security(**changes):
    value = {'context': {'platform': 'Windows', 'build': 26100, 'edition': 'Professional', 'is_admin': True},
             'defender': {'available': True, 'service_enabled': True, 'antivirus_enabled': True,
                          'realtime_enabled': True, 'running_mode': 'Normal', 'signature_age_days': 0},
             'antivirus_providers': {'complete': True, 'items': ['Windows Defender']},
             'firewall_providers': {'complete': True, 'items': ['Windows Firewall']},
             'firewall': {'complete': True, 'profiles': [{'name': name, 'enabled': True} for name in ('Domain', 'Private', 'Public')]},
             'reboot_required': False, 'threats': {'complete': True, 'items': [], 'active_count': 0}}
    # Windows Firewall is a native provider name, not a third-party product.
    value['firewall_providers']['items'] = ['Windows Defender Firewall']
    value.update(changes)
    return value


class NativeFixture:
    def __init__(self):
        self.calls = []
        self.context = security()['context']
        self.values = {key: dword(1) for key, control in _CONTROLS.items() if control.get('existing_only')}
        self.state = security()
        self.before_write = None
        self.quick_result = {'ok': True, 'completion_verified': True, 'threats': {'complete': True, 'active_count': 0}}

    def __call__(self, operation, payload, **options):
        self.calls.append((operation, copy.deepcopy(payload), options))
        if operation == 'context':
            return copy.deepcopy(self.context)
        if operation == 'registry_read':
            return copy.deepcopy(self.values.get(payload['control'], {'exists': False}))
        if operation == 'registry_write':
            if self.before_write:
                self.before_write(payload)
            if payload['expected'] != self.values.get(payload['control'], {'exists': False}):
                return {'ok': False}
            self.values[payload['control']] = {'exists': False} if payload['remove'] else dword(payload['value'])
            return {'ok': True, 'current': copy.deepcopy(self.values[payload['control']])}
        if operation == 'security_status':
            return copy.deepcopy(self.state)
        if operation == 'quick_scan':
            return copy.deepcopy(self.quick_result)
        if operation == 'enable_defender_realtime':
            self.state['defender']['realtime_enabled'] = True
        elif operation == 'enable_firewall':
            for item in self.state['firewall']['profiles']:
                item['enabled'] = True
        elif operation == 'update_defender_signatures':
            self.state['defender']['signature_age_days'] = 0
        elif operation == 'remediate_defender_threats':
            self.state['threats']['active_count'] = 0
        elif operation in ('review_security', 'review_windows_update', 'restart_required'):
            return {'ok': True, 'opened': 'windowsdefender:' if operation == 'review_security' else 'ms-settings:windowsupdate'}
        else:
            raise AssertionError('Unapproved native fixture operation: ' + operation)
        return {'ok': True}


@pytest.fixture
def fixture(tmp_path):
    runner = NativeFixture()
    return SystemPrivacy(tmp_path, runner=runner), runner


def settings(**changes):
    return dict({'privacy_advertising_enabled': True, 'privacy_diagnostics_enabled': True}, **changes)


def mutation_calls(runner):
    return [entry for entry in runner.calls if entry[0] not in ('context', 'registry_read', 'security_status')]


def _history_record(tool, identifier='1' * 32, **changes):
    tool.directory.mkdir(parents=True, exist_ok=True)
    value = {'id': identifier, 'kind': 'privacy', 'version': 1, 'status': 'completed',
             'changed_count': 1, 'created_at': 1234.0,
             'items': [{'before': {'private': 'never exposed'}}]}
    value.update(changes)
    path = tool.directory / (identifier + '.json')
    path.write_text(json.dumps(value), encoding='utf-8')
    return path


def test_history_missing_is_read_only_and_does_not_create_directory(fixture):
    tool, runner = fixture
    assert tool.history() == {'items': [], 'errors': [], 'complete': True, 'failed_items': 0}
    assert not tool.directory.exists() and runner.calls == []


def test_history_returns_only_summaries_of_changed_privacy_records(fixture):
    tool, runner = fixture
    _history_record(tool)
    _history_record(tool, '2' * 32, created_at=2345, status='cancelled', changed_count=2)
    _history_record(tool, '3' * 32, kind='security-recommendation')
    _history_record(tool, '4' * 32, changed_count=0)
    result = tool.history()
    assert result['complete'] and result['failed_items'] == 0
    assert [item['id'] for item in result['items']] == ['2' * 32, '1' * 32]
    assert all(set(item) == {'id', 'kind', 'status', 'created_at', 'changed_count'} for item in result['items'])
    assert 'never exposed' not in str(result) and runner.calls == []


@pytest.mark.parametrize('changes', [dict(changed_count=True), dict(changed_count=999),
    dict(created_at=float('nan')), dict(created_at=10 ** 500), dict(version=True), dict(id='forged'), dict(status='unrecognized')])
def test_history_rejects_invalid_summaries_without_authorizing_restore(fixture, changes):
    tool, runner = fixture
    _history_record(tool, **changes)
    result = tool.history()
    assert not result['complete'] and result['failed_items'] == 1 and result['items'] == []
    assert runner.calls == []


def test_history_reports_malformed_oversized_and_linked_records(fixture):
    tool, runner = fixture
    path = _history_record(tool)
    os.link(path, tool.directory / ('2' * 32 + '.json'))
    (tool.directory / ('3' * 32 + '.json')).write_text('private malformed payload')
    with (tool.directory / ('4' * 32 + '.json')).open('wb') as stream:
        stream.truncate(1024 * 1024 + 1)
    result = tool.history()
    assert not result['complete'] and result['failed_items'] == 4 and result['items'] == []
    assert 'private malformed' not in str(result) and runner.calls == []


def test_history_refuses_indirect_location_without_reading_records(fixture):
    tool, runner = fixture
    with patch('optimus.system_privacy._ordinary', side_effect=ValueError('fixture junction')):
        result = tool.history()
    assert result['items'] == [] and result['failed_items'] == 1 and not result['complete']
    assert runner.calls == []


def test_history_inventory_bound_is_explicit_even_for_non_json_entries(fixture):
    tool, runner = fixture
    tool.directory.mkdir(parents=True)
    class Entries:
        def __enter__(self):
            return iter([SimpleNamespace(name='ignored.txt')] * 1001)
        def __exit__(self, *args):
            return False
    with patch('optimus.system_privacy.os.scandir', return_value=Entries()):
        result = tool.history()
    assert not result['complete'] and result['failed_items'] == 1 and result['items'] == []
    assert 'limit' in result['errors'][0] and runner.calls == []


def test_constructor_and_disabled_masters_do_no_work(fixture):
    tool, runner = fixture
    assert runner.calls == []
    report = tool.apply_privacy({})
    assert report['status'] == 'completed' and report['changed_count'] == 0
    assert runner.calls == []


def test_selected_privacy_controls_have_exact_scopes_and_required_diagnostics_floor(fixture):
    tool, runner = fixture
    runner.values['optional_diagnostics'] = dword(3)
    runner.values['app_diagnostics'] = dword(0)
    result = tool.apply_privacy(settings())
    assert result['status'] == 'completed' and result['changed_count'] == len(_CONTROLS)
    assert result['failed_items'] == 0
    assert runner.values['advertising_id'] == dword(0)
    assert runner.values['tailored_experiences'] == dword(1)
    assert runner.values['optional_diagnostics'] == dword(1)
    assert runner.values['app_diagnostics'] == dword(2)
    assert {operation for operation, _, _ in mutation_calls(runner)} == {'registry_write'}
    assert all(payload['control'] in _CONTROLS for _, payload, _ in mutation_calls(runner))


def test_originals_are_durably_saved_before_each_registry_write(fixture):
    tool, runner = fixture
    def inspect(payload):
        paths = list(tool.directory.glob('*.json'))
        assert len(paths) == 1
        report = json.loads(paths[0].read_text())
        item = next(item for item in report['items'] if item['id'] == payload['control'])
        assert item['status'] == 'prepared' and item['before'] == payload['expected']
    runner.before_write = inspect
    assert tool.apply_privacy(settings())['changed_count'] == len(_CONTROLS)


def test_journal_failure_prevents_every_mutation(fixture):
    tool, runner = fixture
    with patch('optimus.system_privacy.atomic_json', side_effect=PermissionError('fixture denied')):
        with pytest.raises(PermissionError):
            tool.apply_privacy(settings())
    assert mutation_calls(runner) == []


def test_each_subflag_can_exclude_its_exact_controls(fixture):
    tool, runner = fixture
    result = tool.apply_privacy(settings(privacy_disable_advertising_id=False, privacy_disable_tailored_experiences=False,
        privacy_disable_optional_diagnostics=False, privacy_disable_app_diagnostics=False, privacy_disable_suggestions=False))
    assert result['items'] == [] and runner.calls == []


def test_device_policies_need_admin_but_peruser_controls_can_complete(fixture):
    tool, runner = fixture
    runner.context['is_admin'] = False
    result = tool.apply_privacy(settings())
    assert result['failed_items'] == 2
    assert [item['id'] for item in result['items'] if item['status'] == 'requires_admin'] == ['optional_diagnostics', 'app_diagnostics']
    assert not any(_CONTROLS[payload['control']]['scope'] == 'device' for _, payload, _ in mutation_calls(runner))


def test_home_does_not_claim_unsupported_policy_controls_work(fixture):
    tool, runner = fixture
    runner.context['edition'] = 'Core'
    result = tool.apply_privacy(settings())
    unsupported = {item['id'] for item in result['items'] if item['status'] == 'unsupported'}
    assert unsupported == {'tailored_experiences', 'optional_diagnostics', 'app_diagnostics'}
    assert runner.values['advertising_id'] == dword(0)


def test_absent_version_dependent_preferences_are_not_invented(fixture):
    tool, runner = fixture
    runner.values = {}
    result = tool.apply_privacy(settings(privacy_diagnostics_enabled=False, privacy_disable_tailored_experiences=False))
    assert all(item['status'] == 'unsupported' for item in result['items'])
    assert mutation_calls(runner) == []


@pytest.mark.parametrize('value', [{'exists': True, 'type': 'String', 'value': '1'},
    {'exists': True, 'type': 'DWord', 'value': True}, {'exists': True, 'type': 'DWord', 'value': 99}, {}, None])
def test_unrecognized_registry_values_are_not_overwritten(fixture, value):
    tool, runner = fixture
    runner.values['advertising_id'] = value
    result = tool.apply_privacy(settings(privacy_diagnostics_enabled=False, privacy_disable_tailored_experiences=False, privacy_disable_suggestions=False))
    assert result['failed_items'] == 1
    assert mutation_calls(runner) == []


def test_repeated_run_is_idempotent_and_does_not_rewrite_values(fixture):
    tool, runner = fixture
    tool.apply_privacy(settings())
    runner.calls.clear()
    result = tool.apply_privacy(settings())
    assert result['changed_count'] == 0
    assert all(item['status'] == 'already_configured' for item in result['items'])
    assert mutation_calls(runner) == []


def test_guarded_restore_restores_originals_and_removes_only_new_values(fixture):
    tool, runner = fixture
    before = copy.deepcopy(runner.values)
    changed = tool.apply_privacy(settings())
    result = tool.restore_privacy(changed['id'])
    assert result['failed_items'] == 0 and all(item['status'] == 'restored' for item in result['items'])
    assert all(runner.values[key] == value for key, value in before.items())
    assert runner.values['tailored_experiences'] == runner.values['optional_diagnostics'] == runner.values['app_diagnostics'] == {'exists': False}


def test_restore_preserves_newer_setting_edits(fixture):
    tool, runner = fixture
    original = tool.apply_privacy(settings())
    runner.values['optional_diagnostics'] = dword(3)
    result = tool.restore_privacy(original['id'])
    entry = next(item for item in result['items'] if item['id'] == 'optional_diagnostics')
    assert entry['status'] == 'skipped' and runner.values['optional_diagnostics'] == dword(3)


def test_tampered_history_cannot_select_arbitrary_registry_path(fixture):
    tool, runner = fixture
    original = tool.apply_privacy(settings())
    path = tool.directory / (original['id'] + '.json')
    data = json.loads(path.read_text())
    data['items'].append({'id': 'DisableAntivirus', 'desired': 1, 'before': {'exists': False}})
    path.write_text(json.dumps(data))
    runner.calls.clear()
    with pytest.raises(ValueError):
        tool.restore_privacy(original['id'])
    assert mutation_calls(runner) == []


@pytest.mark.parametrize('identity', ['../escape', 'C:/outside.json', 'x', None])
def test_restore_identity_is_scoped_to_private_journal(fixture, identity):
    tool, runner = fixture
    with pytest.raises(ValueError):
        tool.restore_privacy(identity)
    assert runner.calls == []


def test_cancelled_privacy_does_not_query_or_write_registry(fixture):
    tool, runner = fixture
    event = threading.Event();event.set()
    assert tool.apply_privacy(settings(), event)['cancelled']
    assert runner.calls == []


def test_cancellation_during_preview_prevents_registry_write(fixture):
    tool, runner = fixture
    event = threading.Event()
    def cancel_on_read(operation, payload, **options):
        result = runner(operation, payload, **options)
        if operation == 'registry_read':
            event.set()
        return result
    tool.runner = cancel_on_read
    assert tool.apply_privacy(settings(), event)['cancelled']
    assert mutation_calls(runner) == []


def test_security_status_reads_only_and_reports_pending_restart(fixture):
    tool, runner = fixture
    runner.state['reboot_required'] = True
    report = tool.security_status()
    assert report['status'] == 'completed' and report['active_threat_count'] == 0
    assert 'restart_required' in {item['id'] for item in report['recommendations']}
    assert mutation_calls(runner) == []
    assert not tool.directory.exists()


def test_real_time_and_firewall_findings_offer_unselected_stable_recommendations(fixture):
    tool, runner = fixture
    runner.state['defender']['realtime_enabled'] = False
    runner.state['defender']['signature_age_days'] = 3
    runner.state['firewall']['profiles'][1]['enabled'] = False
    runner.state['threats']['active_count'] = 2
    found = {item['id']: item for item in tool.security_status()['recommendations']}
    assert {'enable_defender_realtime', 'enable_firewall', 'update_defender_signatures', 'remediate_defender_threats'} <= found.keys()
    assert all(item['action'] == identity and item['requires_admin'] for identity, item in found.items())
    assert mutation_calls(runner) == []


def test_another_antivirus_or_firewall_provider_is_preserved(fixture):
    tool, runner = fixture
    runner.state['defender']['realtime_enabled'] = False
    runner.state['antivirus_providers']['items'].append('Third Party Antivirus')
    runner.state['firewall']['profiles'][0]['enabled'] = False
    runner.state['firewall_providers']['items'] = ['Third Party Firewall']
    found = {item['id'] for item in tool.security_status()['recommendations']}
    assert 'enable_defender_realtime' not in found and 'enable_firewall' not in found
    assert tool.apply_recommendation('enable_defender_realtime')['status'] == 'blocked'
    assert tool.apply_recommendation('enable_firewall')['status'] == 'blocked'
    assert mutation_calls(runner) == []


@pytest.mark.parametrize('field', ['antivirus_providers', 'firewall_providers', 'firewall', 'defender'])
def test_unknown_provider_or_protection_state_is_partial(fixture, field):
    tool, runner = fixture
    runner.state[field] = {}
    assert tool.security_status()['status'] == 'partial'
    assert mutation_calls(runner) == []


def test_native_status_error_never_becomes_a_clean_bill_of_health(fixture):
    tool, runner = fixture
    tool.runner = Mock(side_effect=RuntimeError('PRIVATE https://secret.invalid'))
    report = tool.security_status()
    assert report['status'] == 'partial' and report['failed_items']
    assert report['active_threat_count'] is None
    assert 'PRIVATE' not in json.dumps(report)


@pytest.mark.parametrize('action,field', [('enable_defender_realtime', 'realtime'), ('enable_firewall', 'firewall'),
    ('update_defender_signatures', 'signatures'), ('remediate_defender_threats', 'threats')])
def test_explicit_security_action_rechecks_then_verifies_outcome(fixture, action, field):
    tool, runner = fixture
    if field == 'realtime':runner.state['defender']['realtime_enabled'] = False
    if field == 'firewall':runner.state['firewall']['profiles'][0]['enabled'] = False
    if field == 'signatures':runner.state['defender']['signature_age_days'] = 3
    if field == 'threats':runner.state['threats']['active_count'] = 1
    report = tool.apply_recommendation(action)
    assert report['status'] == 'completed' and report['failed_items'] == 0
    assert [entry[0] for entry in mutation_calls(runner)] == [action]
    assert [entry[0] for entry in runner.calls].count('security_status') == 2


def test_recommendation_disappearance_does_not_apply_old_fix(fixture):
    tool, runner = fixture
    assert tool.apply_recommendation('enable_defender_realtime')['status'] == 'blocked'
    assert mutation_calls(runner) == []


def test_security_actions_cannot_bypass_administrator_requirement(fixture):
    tool, runner = fixture
    runner.state['context']['is_admin'] = False
    runner.state['defender']['realtime_enabled'] = False
    assert tool.apply_recommendation('enable_defender_realtime')['status'] == 'blocked'
    assert mutation_calls(runner) == []


def test_native_success_without_observable_security_change_is_not_reported_fixed(fixture):
    tool, runner = fixture
    runner.state['defender']['realtime_enabled'] = False
    def ignored(operation, payload, **options):
        return {'ok': True} if operation == 'enable_defender_realtime' else runner(operation, payload, **options)
    tool.runner = ignored
    assert tool.apply_recommendation('enable_defender_realtime')['status'] == 'failed'


def test_restart_recommendation_opens_settings_without_reboot(fixture):
    tool, runner = fixture
    runner.state['reboot_required'] = True
    result = tool.apply_recommendation('restart_required')
    assert result['status'] == 'manual_action_required' and result['opened'] == 'ms-settings:windowsupdate'
    assert [entry[0] for entry in mutation_calls(runner)] == ['restart_required']


@pytest.mark.parametrize('identity', ['disable_defender', 'DisableFirewall', '--force', 'calc.exe;evil'])
def test_unrecognized_security_actions_are_refused(fixture, identity):
    tool, runner = fixture
    with pytest.raises(ValueError):tool.apply_recommendation(identity)
    assert runner.calls == []


def test_scan_uses_active_defender_quickscan_and_preserves_standard_remediation(fixture):
    tool, runner = fixture
    result = tool.scan_spyware()
    assert result['status'] == 'completed' and result['active_threat_count'] == 0
    assert [entry[0] for entry in mutation_calls(runner)] == ['quick_scan']
    assert mutation_calls(runner)[0][2]['timeout'] == 900


@pytest.mark.parametrize('reason', ['passive', 'unavailable', 'permission'])
def test_scan_does_not_replace_other_providers_or_request_elevation(fixture, reason):
    tool, runner = fixture
    if reason == 'passive':runner.state['defender']['running_mode'] = 'Passive'
    if reason == 'unavailable':runner.state['defender']['available'] = False
    if reason == 'permission':runner.state['context']['is_admin'] = False
    assert tool.scan_spyware()['status'] == 'blocked'
    assert mutation_calls(runner) == []


@pytest.mark.parametrize('outcome,status', [
    ({'ok': True, 'completion_verified': False}, 'incomplete'),
    ({'ok': False, 'timed_out': True, 'may_continue': True}, 'incomplete'),
    ({'ok': False, 'cancelled': True, 'may_continue': True}, 'cancelled')])
def test_scan_timeout_cancellation_and_unverified_completion_never_claim_clean(fixture, outcome, status):
    tool, runner = fixture
    runner.quick_result = outcome
    result = tool.scan_spyware()
    assert result['status'] == status
    assert len(mutation_calls(runner)) == 1
    assert 'active_threat_count' not in result


def test_cancelled_scan_and_action_do_no_host_work(fixture):
    tool, runner = fixture
    event = threading.Event();event.set()
    assert tool.scan_spyware(event)['status'] == 'cancelled'
    assert tool.apply_recommendation('enable_firewall', event)['status'] == 'cancelled'
    assert runner.calls == []


def test_fixed_native_scripts_have_no_protection_disable_or_arbitrary_delete():
    script = _script('quick_scan', {})
    assert 'Start-MpScan -ScanType QuickScan' in script
    assert 'Remove-MpThreat -ErrorAction Stop' in script
    assert 'Set-MpPreference -DisableRealtimeMonitoring $false' in script
    assert 'Set-NetFirewallProfile -Name $fwItem.Name -Enabled True' in script
    assert not any(value in script for value in ('Remove-Item', 'Stop-Service', 'Set-Service', '-ExclusionPath',
        '-DisableRealtimeMonitoring $true', '-Enabled False', '-ExecutionPolicy', '-Force', 'Restart-Computer', 'Clear-MpThreat'))
    with pytest.raises(ValueError):_script('quick_scan;calc', {})


def test_existing_more_restrictive_diagnostics_policy_is_never_raised(fixture):
    tool, runner = fixture
    runner.values['optional_diagnostics'] = dword(0)
    result = tool.apply_privacy(settings(privacy_advertising_enabled=False, privacy_disable_app_diagnostics=False))
    assert runner.values['optional_diagnostics'] == dword(0)
    assert result['items'][0]['status'] == 'already_configured'
    assert mutation_calls(runner) == []


def test_boolean_preference_with_unknown_numeric_value_is_not_overwritten(fixture):
    tool, runner = fixture
    runner.values['advertising_id'] = dword(2)
    result = tool.apply_privacy(settings(privacy_diagnostics_enabled=False, privacy_disable_tailored_experiences=False, privacy_disable_suggestions=False))
    assert result['failed_items'] == 1 and mutation_calls(runner) == []


def test_mitre_mappings_are_linked_control_context_and_do_not_claim_coverage(fixture):
    tool, runner = fixture
    runner.state['defender']['realtime_enabled'] = False
    runner.state['firewall']['profiles'][0]['enabled'] = False
    runner.state['reboot_required'] = True
    result = tool.security_status()
    checks = {item['id']: item for item in result['checks']}
    assert checks['antivirus']['mitigations'][0]['id'] == 'M1049'
    assert checks['firewall']['mitigations'][0]['id'] == 'M1037'
    assert checks['windows_updates']['mitigations'][0]['id'] == 'M1051'
    for item in result['recommendations']:
        assert item['mitigations'][0]['url'].startswith('https://attack.mitre.org/mitigations/M')
    assert 'not complete ATT&CK coverage' in result['mitre_note']


def test_windows_firewall_provider_name_is_not_mistaken_for_third_party(fixture):
    tool, runner = fixture
    runner.state['firewall_providers']['items'] = ['Windows Firewall']
    runner.state['firewall']['profiles'][0]['enabled'] = False
    assert 'enable_firewall' in {item['id'] for item in tool.security_status()['recommendations']}


def test_restore_permission_failure_is_reported_without_losing_original_history(fixture):
    tool, runner = fixture
    original = tool.apply_privacy(settings(privacy_advertising_enabled=False, privacy_disable_app_diagnostics=False))
    original_path = tool.directory / (original['id'] + '.json')
    original_text = original_path.read_text()
    def denied(operation, payload, **options):
        if operation == 'registry_read':
            return {'ok': False, 'permission_denied': True}
        return runner(operation, payload, **options)
    tool.runner = denied
    runner.calls.clear()
    result = tool.restore_privacy(original['id'])
    assert result['status'] == 'completed_with_errors' and result['failed_items'] == 1
    assert original_path.read_text() == original_text
    assert mutation_calls(runner) == []


def test_cancel_during_restore_snapshot_prevents_restoring_value(fixture):
    tool, runner = fixture
    original = tool.apply_privacy(settings(privacy_advertising_enabled=False, privacy_disable_app_diagnostics=False))
    event = threading.Event()
    def cancelled(operation, payload, **options):
        result = runner(operation, payload, **options)
        if operation == 'registry_read':event.set()
        return result
    tool.runner = cancelled
    runner.calls.clear()
    assert tool.restore_privacy(original['id'], event)['status'] == 'cancelled'
    assert mutation_calls(runner) == []


def test_unreadable_active_threat_inventory_remains_unknown_and_partial(fixture):
    tool, runner = fixture
    runner.state['threats'] = {'complete': False, 'active_count': None}
    report = tool.security_status()
    assert report['status'] == 'partial' and report['active_threat_count'] is None
    assert report['threat_status_complete'] is False
    assert mutation_calls(runner) == []
