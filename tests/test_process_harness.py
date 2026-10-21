"""The process harness uses fixture inventories and mocked mutation services only."""
import copy
import json
import threading
from unittest.mock import Mock, patch

import pytest

from optimus.process_harness import ProcessHarness
from test_onboarding import startup_fixture


def process(**changes):
    value = {'pid': 100, 'name': 'Fixture.exe', 'exe': r'C:\Fixture\app.exe', 'create_time': 1000.0,
             'ppid': None, 'username': 'FixtureUser', 'protected': False, 'can_sleep': True,
             'memory_rss': 1024, 'private_bytes': 512, 'reason': ''}
    value.update(changes)
    return value


class Backend:
    def __init__(self, directory):
        self.data_dir = directory
        self.processes = Mock(return_value=[process()])
        self.slowdown_report = Mock(return_value={'complete': True, 'cancelled': False,
            'applications': [{'exe': r'C:\Fixture\app.exe', 'cpu_percent': 0.1, 'memory_rss': 1024,
                              'severity': 'normal', 'measured_intervals': 2}]})
        self.startup_list = Mock(return_value=[startup_fixture()])
        self.installed_apps = Mock(return_value=[{'Name': 'Fixture application', 'InstallLocation': r'C:\Fixture'}])
        self.sleep_process = Mock(return_value={'pid': 100, 'status': 'sleeping', 'changed': True})
        self.startup_set = Mock(return_value={'Id': 'fixture-recovery-record'})
        self.uninstall = Mock(side_effect=AssertionError('The harness must never uninstall programs.'))


@pytest.fixture
def tool(tmp_path):
    return ProcessHarness(Backend(tmp_path))


def action(plan, kind):
    return next(item['id'] for item in plan['actions'] if item['kind'] == kind)


def request(plan, *ids):
    return {'plan_id': plan['id'], 'selected_ids': list(ids), 'confirmed': True}


def test_scan_retains_protected_and_inaccessible_processes_without_changes(tool):
    tool.backend.processes.return_value += [process(pid=200, protected=True, can_sleep=False, reason='Fixture security'),
                                           process(pid=300, exe='', create_time=None, protected=True, can_sleep=False)]
    plan = tool.scan()
    assert len(plan['processes']) == 3
    assert plan['summary']['protected'] == 2
    assert not any(item['selected'] for item in plan['actions'])
    assert len([item for item in plan['actions'] if item['kind'] == 'sleep_process']) == 1
    assert plan['processes'][2]['measurement_available'] is False
    tool.backend.sleep_process.assert_not_called()
    tool.backend.startup_set.assert_not_called()
    tool.backend.uninstall.assert_not_called()


@pytest.mark.parametrize('name,exe', [('BingWallpaper.exe', r'C:\Fixture\BingWallpaper.exe'),
    ('Codex.exe', r'C:\Fixture\Codex.exe'), ('Codex launcher', r'C:\Fixture\launcher.exe'), ('msmpeng.exe', r'C:\Fixture\msmpeng.exe'),
    ('Any.exe', r'C:\Windows\System32\Any.exe'), ('Any.exe', r'C:\Program Files\Malwarebytes\Any.exe')])
def test_preserved_executables_have_no_sleep_or_startup_action(tool, name, exe):
    tool.backend.processes.return_value = [process(name=name, exe=exe)]
    tool.backend.startup_list.return_value = [startup_fixture(Name=name, Command='"' + exe + '" --fixture')]
    plan = tool.scan()
    assert plan['processes'][0]['protected'] is True
    assert plan['actions'] == []


def test_ownership_uses_path_boundaries_and_most_specific_registry_location(tool):
    tool.backend.installed_apps.return_value = [{'Name': 'Incorrect prefix', 'InstallLocation': r'C:\Fixt'},
        {'Name': 'No location', 'Publisher': 'Fixture'},
        {'Name': 'Broad folder', 'InstallLocation': 'C:\\'},
        {'Name': 'Specific fixture application', 'InstallLocation': r'C:\Fixture'}]
    row = tool.scan()['processes'][0]
    assert row['owners'] == ['Specific fixture application']
    assert 'InstallLocation' in row['ownership_evidence']


def test_parent_must_predate_child_and_startup_match_is_only_evidence(tool):
    tool.backend.processes.return_value = [process(ppid=200),
        process(pid=200, name='Parent.exe', exe=r'C:\Other\parent.exe', create_time=1100)]
    row = tool.scan()['processes'][0]
    assert row['parent'] is None
    assert row['startup_matches']
    assert 'does not prove' in row['why_running']
    tool.backend.processes.return_value[1]['create_time'] = 900
    assert tool.scan()['processes'][0]['parent']['pid'] == 200


def test_name_heuristic_is_inference_and_missing_metrics_are_unavailable(tool):
    tool.backend.processes.return_value = [process(name='FixtureUpdater.exe', exe=r'C:\Other\helper.exe')]
    tool.backend.startup_list.return_value = []
    tool.backend.slowdown_report.return_value['applications'] = []
    row = tool.scan()['processes'][0]
    assert row['why_running_confidence'] == 'inferred'
    assert 'inference' in row['why_running']
    assert row['metrics'] is None
    assert row['measurement_available'] is False
    assert 'obsolete' not in row['recommendation'].casefold()


def test_low_resource_startup_registration_is_still_an_optional_review_action(tool):
    plan = tool.scan()
    assert action(plan, 'disable_startup')
    assert action(plan, 'sleep_process')
    assert plan['processes'][0]['metrics']['cpu_percent'] == 0.1
    assert not any(item['selected'] for item in plan['actions'])


def test_export_contains_no_command_arguments_or_caller_changes(tool, tmp_path):
    tool.backend.processes.return_value[0]['cmdline'] = ['Fixture.exe', '--token', 'secret-marker']
    tool.backend.startup_list.return_value[0]['Command'] += ' --token secret-marker https://private.fixture/token'
    tool.backend.startup_list.return_value[0]['RawValue'] = tool.backend.startup_list.return_value[0]['Command']
    plan = tool.scan()
    plan['processes'][0]['cmdline'] = ['caller-secret']
    exported = tool.export(plan['id'], tmp_path / 'exports')
    text = open(exported['path'], encoding='utf-8').read()
    assert not any(value in text for value in ('secret-marker', 'private.fixture', 'caller-secret', 'RawValue', 'Command'))
    assert json.loads(text)['processes'][0]['exe'] == r'C:\Fixture\app.exe'
    with pytest.raises(FileExistsError):
        tool.export(plan['id'], exported['path'])


@pytest.mark.parametrize('section', ('processes', 'slowdown_report', 'startup_list', 'installed_apps'))
def test_unavailable_inventory_is_reported_partial_and_apply_is_blocked(tool, section):
    getattr(tool.backend, section).side_effect = PermissionError('private token must not leak')
    plan = tool.scan()
    assert plan['complete'] is False
    assert plan['errors']
    assert 'private token' not in json.dumps(plan)
    with pytest.raises(ValueError, match='incomplete'):
        tool.apply(request(plan))
    tool.backend.sleep_process.assert_not_called()
    tool.backend.startup_set.assert_not_called()


def test_cancelled_scan_never_offers_apply_authority(tool):
    cancelled = threading.Event(); cancelled.set()
    plan = tool.scan(cancelled)
    assert plan['cancelled'] is True
    assert plan['complete'] is False
    assert plan['processes'] == []
    tool.backend.processes.assert_not_called()
    with pytest.raises(ValueError):
        tool.apply(request(plan))


@pytest.mark.parametrize('invalid', ({}, {'confirmed': 1}, {'confirmed': 'yes'}))
def test_explicit_boolean_confirmation_is_required(tool, invalid):
    plan = tool.scan()
    payload = request(plan, action(plan, 'sleep_process')); payload.update(invalid)
    if not invalid:
        payload.pop('confirmed')
    with pytest.raises(ValueError):
        tool.apply(payload)
    tool.backend.sleep_process.assert_not_called()


def test_opaque_single_use_authority_ignores_caller_target_records(tool):
    plan = tool.scan()
    selected = action(plan, 'sleep_process')
    plan['processes'][0]['exe'] = r'C:\Unapproved\malicious.exe'
    payload = request(plan, selected)
    payload['record'] = process(exe=r'C:\Unapproved\malicious.exe')
    result = tool.apply(payload)
    assert result['status'] == 'completed'
    assert tool.backend.sleep_process.call_args.args[0]['exe'] == r'C:\Fixture\app.exe'
    with pytest.raises(ValueError, match='session'):
        tool.apply(payload)


def test_unknown_actions_and_cross_session_scan_ids_are_rejected(tool, tmp_path):
    plan = tool.scan()
    with pytest.raises(ValueError, match='does not belong'):
        tool.apply(request(plan, 'f' * 32))
    other = ProcessHarness(Backend(tmp_path / 'other'))
    with pytest.raises(ValueError, match='session'):
        other.apply(request(plan, action(plan, 'sleep_process')))


def test_expiry_and_batch_limit_fail_before_mutation(tool):
    plan = tool.scan()
    with patch('optimus.process_harness.time.time', return_value=plan['expires_at'] + 1):
        with pytest.raises(ValueError, match='expired'):
            tool.apply(request(plan, action(plan, 'sleep_process')))
    with pytest.raises(ValueError, match='25'):
        tool.apply(request(plan, *[f'{index:032x}' for index in range(26)]))
    selected = action(plan, 'sleep_process')
    with pytest.raises(ValueError):
        tool.apply(request(plan, selected, selected))
    tool.backend.sleep_process.assert_not_called()


@pytest.mark.parametrize('change', ({'create_time': 1001.0}, {'exe': r'C:\Other\new.exe'},
                                  {'protected': True}, {'can_sleep': False}))
def test_sleep_revalidates_exact_current_lifetime_and_protection(tool, change):
    plan = tool.scan()
    tool.backend.processes.return_value[0].update(change)
    result = tool.apply(request(plan, action(plan, 'sleep_process')))
    assert result['failed_items'] == 1
    assert result['status'] == 'completed_with_errors'
    tool.backend.sleep_process.assert_not_called()


@pytest.mark.parametrize('change', ({'Fingerprint': 'c' * 64}, {'Command': r'C:\Other\new.exe'},
                                  {'Protected': True}, {'Enabled': False}))
def test_startup_action_revalidates_fingerprint_command_and_protection(tool, change):
    plan = tool.scan()
    tool.backend.startup_list.return_value[0].update(change)
    result = tool.apply(request(plan, action(plan, 'disable_startup')))
    assert result['failed_items'] == 1
    tool.backend.startup_set.assert_not_called()


def test_only_selected_unchanged_startup_is_disabled_with_recovery_record(tool):
    plan = tool.scan()
    result = tool.apply(request(plan, action(plan, 'disable_startup')))
    tool.backend.processes.assert_called()
    tool.backend.startup_set.assert_called_once()
    assert tool.backend.startup_set.call_args.args[1] is False
    assert result['items'][0]['change_id'] == 'fixture-recovery-record'
    tool.backend.sleep_process.assert_not_called()


def test_journal_is_durable_before_action_and_failure_to_write_prevents_action(tool):
    plan = tool.scan()
    def sleep(record):
        paths = list((tool.data_dir / 'actions').glob('*.json'))
        journal = json.loads(paths[0].read_text())
        assert journal['items'][0]['status'] == 'prepared'
        return {'status': 'sleeping', 'changed': True}
    tool.backend.sleep_process.side_effect = sleep
    result = tool.apply(request(plan, action(plan, 'sleep_process')))
    assert json.loads(open(result['log_path'], encoding='utf-8').read())['status'] == 'completed'
    plan = tool.scan()
    tool.backend.sleep_process.reset_mock()
    with patch('optimus.process_harness.atomic_json', side_effect=OSError('Fixture disk full')):
        with pytest.raises(OSError):
            tool.apply(request(plan, action(plan, 'sleep_process')))
    tool.backend.sleep_process.assert_not_called()


def test_cancellation_after_fresh_startup_inventory_prevents_mutation(tool):
    plan = tool.scan()
    cancelled = threading.Event()
    def inventory():
        cancelled.set()
        return [startup_fixture()]
    tool.backend.startup_list.side_effect = inventory
    result = tool.apply(request(plan, action(plan, 'disable_startup')), cancelled)
    assert result['cancelled'] is True
    tool.backend.startup_set.assert_not_called()


def test_backend_failure_is_retained_without_leaking_raw_exception(tool):
    plan = tool.scan()
    tool.backend.sleep_process.side_effect = RuntimeError('secret-marker https://private.fixture/token')
    result = tool.apply(request(plan, action(plan, 'sleep_process')))
    assert result['failed_items'] == 1
    assert 'secret-marker' not in json.dumps(result)
    assert 'Wake All' in result['note']


def test_cancel_after_one_action_journals_remaining_selections_without_applying(tool):
    plan = tool.scan()
    cancelled = threading.Event()
    def sleep(record):
        cancelled.set()
        return {'status': 'sleeping', 'changed': True}
    tool.backend.sleep_process.side_effect = sleep
    sleep_id, startup_id = action(plan, 'sleep_process'), action(plan, 'disable_startup')
    result = tool.apply(request(plan, sleep_id, startup_id), cancelled)
    assert result['status'] == 'cancelled'
    assert result['deferred_ids'] == [startup_id]
    assert result['selected_ids'] == [sleep_id, startup_id]
    tool.backend.startup_set.assert_not_called()


def test_cli_process_scan_only_exports_a_read_only_report(tmp_path, capsys):
    from optimus.app import main
    backend = Mock()
    backend.process_harness_scan.return_value = {'id': 'a' * 32}
    backend.process_harness_export.return_value = {'path': str(tmp_path / 'fixture.json'), 'changes': 0}
    with patch('optimus.app.sys.platform', 'win32'), patch('optimus.backend.Backend', return_value=backend):
        assert main(['--process-scan', '--output', str(tmp_path)]) == 0
    backend.process_harness_scan.assert_called_once_with()
    backend.process_harness_export.assert_called_once_with('a' * 32, tmp_path)
    backend.process_harness_apply.assert_not_called()
    backend.optimize.assert_not_called()
    backend.export_report.assert_not_called()
    backend.close.assert_called_once_with()
    assert json.loads(capsys.readouterr().out)['changes'] == 0
