"""Defender progress and removal use inert native fixtures exclusively."""
import copy
import json
import io
import os
from pathlib import Path
import subprocess
import threading
from unittest.mock import Mock

import pytest

from optimus.defender_scan import ScanProgress, threat_snapshot, validate_remediation_consent, read_scan_process
from optimus.item_progress import normalize_item_progress
from optimus.system_privacy import SystemPrivacy, _script
from test_system_privacy import NativeFixture


def active(ids=(42,), **changes):
    value = {'complete': True, 'active_count': len(ids), 'items': [
        {'id': identity, 'name': 'Fixture detection', 'active': True} for identity in ids],
        'active_fingerprint': 'a' * 64, 'active_fingerprint_complete': True}
    value.update(changes)
    return value


def consent(ids=(42,), **changes):
    return dict({'confirmed': True, 'scope': 'all_current_active_defender_threats',
                 'threat_ids': list(ids), 'resource_fingerprint': 'a' * 64}, **changes)


def test_actual_provider_fraction_and_invalid_values_keep_completion_separate():
    clock = Mock(side_effect=[0, 1, 2, 3, 4, 5, 6])
    events = []
    progress = ScanProgress(events.append, clock=clock)
    progress.emit('preparing', percent=0)
    progress.native({'progress_source': 'defender_cim_progress', 'native_percent': 12})
    progress.native({'progress_source': 'defender_cim_progress', 'native_percent': -1})
    progress.native({'progress_source': 'defender_cim_progress', 'native_percent': 100})
    progress.emit('completed', percent=100, verified=True)
    progress.native({'progress_source': 'defender_cim_progress', 'native_percent': 1})
    assert [event['item_progress']['percent'] for event in events] == [0, 12, None, None, 100]
    normalized = normalize_item_progress(events[1]['item_progress'])
    assert normalized['progress_source'] == 'defender_cim_progress' and normalized['estimated'] is True
    assert [event['elapsed_seconds'] for event in events] == [1, 2, 3, 4, 5]


@pytest.mark.parametrize('value', [True, '50', -1, 101, None, float('nan')])
def test_untyped_or_out_of_range_provider_values_cannot_produce_scan_percent(value):
    events = []
    ScanProgress(events.append).native({'progress_source': 'defender_cim_progress', 'native_percent': value})
    assert events[-1]['item_progress']['percent'] is None


def test_fallback_elapsed_is_not_converted_into_numeric_scan_progress():
    events = []
    progress = ScanProgress(events.append, clock=Mock(side_effect=[0, 400]))
    progress.native({'progress_source': 'defender_powershell', 'ppm_progress': 500000})
    assert events[-1]['elapsed_seconds'] == 400 and events[-1]['item_progress']['percent'] is None


def test_quarantine_and_active_records_are_bounded_and_never_claim_clean():
    value = active((), detections=[{'threat_id': 42, 'status_id': 3, 'action_success': True,
        'Resources': ['PRIVATE/user/path'], 'DomainUser': 'PRIVATE'}], detections_complete=True)
    snapshot = threat_snapshot(value)
    assert snapshot['detections'] == [{'threat_id': 42, 'status_id': 3, 'status': 'quarantined', 'action_success': True}]
    assert snapshot['active_count'] == 0 and snapshot['removal_requires_confirmation'] is True
    assert snapshot['review_action'] == 'review_security' and 'PRIVATE' not in json.dumps(snapshot)
    value = active(tuple(range(201)))
    snapshot = threat_snapshot(value)
    assert len(snapshot['items']) == 200 and not snapshot['active_ids_complete']
    assert validate_remediation_consent(consent(tuple(range(200))), snapshot) is None


@pytest.mark.parametrize('value', [None, True, -1, 1000001, '0'])
def test_invalid_active_count_remains_unknown(value):
    assert threat_snapshot(active((), active_count=value))['active_count'] is None


def test_scan_streams_unknown_ticks_then_only_verified_completion_100(tmp_path):
    native = NativeFixture()
    def runner(operation, payload, *, progress_callback=None, **options):
        if operation == 'quick_scan':
            for _ in range(3):
                progress_callback({'progress_source': 'defender_powershell'})
        return native(operation, payload, **options)
    events = []
    result = SystemPrivacy(tmp_path, runner=runner).scan_spyware(progress_callback=events.append)
    assert result['completion_verified'] is True and result['elapsed_seconds'] >= 0
    assert [event['item_progress']['percent'] for event in events] == [0, None, None, None, None, 100]
    assert result['progress_capability']['numeric_available'] is False
    assert all('malware' in event['item_progress']['label'] for event in events)
    assert [call[0] for call in native.calls].count('quick_scan') == 1


@pytest.mark.parametrize('outcome', [{'ok': False, 'may_continue': True}, {'ok': True, 'completion_verified': False},
    {'ok': False, 'cancelled': True, 'may_continue': True}])
def test_incomplete_or_cancelled_scan_never_shows_100_or_clean(tmp_path, outcome):
    native = NativeFixture(); native.quick_result = outcome
    events = []
    result = SystemPrivacy(tmp_path, runner=native).scan_spyware(progress_callback=events.append)
    assert result['completion_verified'] is False
    assert 100 not in [event['item_progress']['percent'] for event in events]
    assert 'active_threat_count' not in result


def test_quarantined_history_keeps_manual_windows_security_review_after_scan(tmp_path):
    native = NativeFixture()
    history = active((), detections=[{'threat_id': 42, 'status_id': 3, 'action_success': True}], detections_complete=True)
    native.quick_result['threats'] = copy.deepcopy(history)
    native.state['threats'] = copy.deepcopy(history)
    result = SystemPrivacy(tmp_path, runner=native).scan_spyware()
    assert result['threats']['detections'][0]['status'] == 'quarantined'
    assert 'review_security' in {row['id'] for row in result['recommendations']}
    assert 'remediate_defender_threats' not in {row['id'] for row in result['recommendations']}
    assert not any(call[0] == 'remediate_defender_threats' for call in native.calls)


@pytest.mark.parametrize('approval', [None, consent(confirmed=False), consent(scope='one_file'),
    consent((99,)), consent((42, 42)), consent((True,)), consent((42,), extra=True),
    consent(resource_fingerprint='b' * 64), consent(resource_fingerprint=None)])
def test_unconfirmed_changed_or_partial_remediation_scope_blocks(tmp_path, approval):
    native = NativeFixture(); native.state['threats'] = active()
    tool = SystemPrivacy(tmp_path, runner=native)
    result = tool.apply_recommendation('remediate_defender_threats', remediation_consent=approval)
    assert result['status'] == 'blocked'
    assert all(call[0] == 'security_status' for call in native.calls)


def test_exact_all_active_consent_is_forwarded_and_action_reverified(tmp_path):
    native = NativeFixture(); native.state['threats'] = active((42, 43))
    tool = SystemPrivacy(tmp_path, runner=native)
    recommendation = next(row for row in tool.security_status()['recommendations'] if row['id'] == 'remediate_defender_threats')
    assert recommendation['threat_ids'] == [42, 43] and recommendation['threat_ids_complete']
    result = tool.apply_recommendation('remediate_defender_threats', remediation_consent=consent((43, 42)))
    assert result['status'] == 'completed'
    operation = next(call for call in native.calls if call[0] == 'remediate_defender_threats')
    assert operation[1]['remediation_consent']['threat_ids'] == [42, 43]


@pytest.mark.skipif(os.name != 'nt', reason='Inert Windows PowerShell native consent gate.')
@pytest.mark.parametrize('approved,actual,resource_change,expected_calls', [
    ([42], [42], False, 1), ([42], [43], False, 0), ([42], [42, 43], False, 0), ([42], [42], True, 0)])
def test_actual_native_second_check_never_remediates_changed_scope(approved, actual, resource_change, expected_calls):
    script = _script('remediate_defender_threats', {'remediation_consent': consent(approved)})
    ids = ','.join(map(str, actual))
    fake = r'''
function Is-Admin {return $true}
function Get-MpComputerStatus {return [pscustomobject]@{AMServiceEnabled=$true;AntivirusEnabled=$true;AMRunningMode='Normal'}}
$script:FixtureIDs=@(__IDS__)
$script:FixtureResources=@('file:C:\Fixture\inert-a.bin')
function Get-MpThreat {foreach($id in $script:FixtureIDs){[pscustomobject]@{ThreatID=$id;ThreatName='Inert fixture';IsActive=$true;Resources=$script:FixtureResources}}}
function Get-MpThreatDetection {return @()}
function Remove-MpThreat {[Console]::WriteLine('MOCK_REMOVAL_ONLY');$script:FixtureIDs=@()}
$P.remediation_consent.resource_fingerprint=(Threats).active_fingerprint
__CHANGE__
'''.replace('__IDS__', ids)
    fake = fake.replace('__CHANGE__', "$script:FixtureResources+=@('file:C:\\Fixture\\inert-b.bin')" if resource_change else '')
    marker = "try {\n $result=switch('__OPERATION__')".replace('__OPERATION__', 'remediate_defender_threats')
    assert marker in script
    script = script.replace(marker, fake + '\n' + marker, 1)
    executable = Path(os.environ['SystemRoot']) / 'System32/WindowsPowerShell/v1.0/powershell.exe'
    completed = subprocess.run([str(executable), '-NoLogo', '-NoProfile', '-NonInteractive', '-Command', script],
                               capture_output=True, text=True, timeout=20)
    assert completed.stdout.count('MOCK_REMOVAL_ONLY') == expected_calls, completed.stderr
    assert completed.returncode == (0 if expected_calls else 1)


def test_owned_child_streams_actual_provider_percent_and_keeps_terminal_proof_separate():
    identity = '12345678-1234-1234-1234-123456789abc'
    frames = [{'kind': 'defender_scan_progress', 'scan_job_id': identity, 'percent': value}
              for value in (None, 25, 50, 100)]
    frames.append({'ok': True, 'scan_job_id': identity, 'completion_verified': True})
    process = Mock(stdout=io.StringIO(''.join(json.dumps(frame) + '\n' for frame in frames)), returncode=0)
    process.poll.return_value = 0
    events = []
    result = read_scan_process(process, timeout=5, progress_callback=events.append)
    assert result['completion_verified'] is True and result['numeric_progress_observed'] is True
    assert [row['native_percent'] for row in events] == [None, 25, 50, None]
    assert all(row['progress_source'] == 'defender_cim_progress' for row in events)
    assert not any('Activity' in row or 'CurrentOperation' in row for row in events)
    process.terminate.assert_not_called()


def test_provider_regression_disables_percent_until_verified_completion():
    identity = '12345678-1234-1234-1234-123456789abc'
    frames = [{'kind': 'defender_scan_progress', 'scan_job_id': identity, 'percent': value}
              for value in (25, 10, 50)]
    frames.append({'ok': True, 'scan_job_id': identity, 'completion_verified': False})
    process = Mock(stdout=io.StringIO(''.join(json.dumps(frame) + '\n' for frame in frames)), returncode=0)
    process.poll.return_value = 0
    events = []
    result = read_scan_process(process, timeout=5, progress_callback=events.append)
    assert [row['native_percent'] for row in events] == [25, None, None]
    assert result['completion_verified'] is False


def test_mismatched_job_cannot_supply_terminal_completion():
    frames = [{'kind': 'defender_scan_progress', 'scan_job_id': '12345678-1234-1234-1234-123456789abc', 'percent': 25},
              {'ok': True, 'scan_job_id': 'aaaaaaaa-1234-1234-1234-123456789abc', 'completion_verified': True}]
    process = Mock(stdout=io.StringIO(''.join(json.dumps(frame) + '\n' for frame in frames)), returncode=0)
    process.poll.return_value = 0
    result = read_scan_process(process, timeout=5)
    assert result['ok'] is False and result['may_continue'] is True
    assert result.get('completion_verified') is not True


def test_observer_error_never_restarts_or_abandons_completed_scan(tmp_path):
    native = NativeFixture()
    def broken(_event):
        raise RuntimeError('Inert display failure')
    result = SystemPrivacy(tmp_path, runner=native).scan_spyware(progress_callback=broken)
    assert result['completion_verified'] is True
    assert [call[0] for call in native.calls].count('quick_scan') == 1


def test_cancelled_monitor_retires_only_owned_client_and_never_claims_scan_stopped():
    identity = '12345678-1234-1234-1234-123456789abc'
    frame = {'kind': 'defender_scan_progress', 'scan_job_id': identity, 'percent': 25}
    process = Mock(stdout=io.StringIO(json.dumps(frame) + '\n'), returncode=None)
    process.poll.return_value = None
    process.wait.return_value = 0
    cancelled = threading.Event()
    result = read_scan_process(process, timeout=5, cancel_event=cancelled,
                               progress_callback=lambda _event: cancelled.set())
    assert result['cancelled'] is True and result['may_continue'] is True
    assert result.get('completion_verified') is not True
    process.terminate.assert_called_once_with()
    process.kill.assert_not_called()


def test_scan_event_preserves_actual_cim_progress_source():
    events = []
    ScanProgress(events.append).native({'progress_source': 'defender_cim_progress', 'native_percent': 25})
    value = normalize_item_progress(events[-1]['item_progress'])
    assert value['percent'] == 25 and value['progress_source'] == 'defender_cim_progress'
    assert value['estimated'] is True


@pytest.mark.skipif(os.name != 'nt', reason='Inert PowerShell Job.Progress fixtures.')
@pytest.mark.parametrize('percentage,verified', [(25, True), (-1, True), (25, False)])
def test_native_owned_defender_job_reports_only_provider_progress(percentage, verified):
    script = _script('quick_scan', {})
    fake = r'''
function Is-Admin {return $true}
function Get-MpComputerStatus {return [pscustomobject]@{AMServiceEnabled=$true;AntivirusEnabled=$true;AMRunningMode='Normal';QuickScanEndTime=__END__}}
function Get-MpThreat {return @()}
function Get-MpThreatDetection {return @()}
$script:FixtureTick=0
$script:FixtureProgress=New-Object Collections.ArrayList
$record=New-Object Management.Automation.ProgressRecord(1,'PRIVATE PATH MUST NOT LEAVE JOB','PRIVATE')
$record.PercentComplete=__PERCENT__
[void]$script:FixtureProgress.Add($record)
$script:FixtureJob=[pscustomobject]@{InstanceId=[guid]'12345678-1234-1234-1234-123456789abc';Progress=$script:FixtureProgress;ChildJobs=@()}
$script:FixtureJob|Add-Member ScriptProperty State {if($script:FixtureTick -lt 1){return 'Running'}else{return 'Completed'}}
function Start-MpScan {param($ScanType,[switch]$AsJob,$ErrorAction)
 if($ScanType -ne 'QuickScan' -or -not $AsJob){throw 'Unexpected scan fixture request'}
 return $script:FixtureJob
}
function Start-Sleep {param($Milliseconds) if($Milliseconds -ne 200){throw 'Unexpected fixture wait'};$script:FixtureTick++}
function Receive-Job {param($Job,$ErrorAction) if(-not [object]::ReferenceEquals($Job,$script:FixtureJob)){throw 'Different job queried'}}
'''.replace('__PERCENT__', str(percentage)).replace('__END__', '([datetime]::UtcNow.AddSeconds(1))' if verified else "([datetime]'2000-01-01')")
    marker = "try {\n $result=switch('quick_scan')"
    script = script.replace(marker, fake + '\n' + marker, 1)
    executable = Path(os.environ['SystemRoot']) / 'System32/WindowsPowerShell/v1.0/powershell.exe'
    completed = subprocess.run([str(executable), '-NoLogo', '-NoProfile', '-NonInteractive', '-Command', script],
                               capture_output=True, text=True, timeout=20)
    assert completed.returncode == 0, completed.stderr
    frames = [json.loads(line) for line in completed.stdout.splitlines() if line.strip()]
    assert frames[-1]['completion_verified'] is verified
    assert frames[-1]['scan_job_id'] == '12345678-1234-1234-1234-123456789abc'
    assert [row['percent'] for row in frames[:-1]] == [percentage if percentage >= 0 else None]
    assert 'PRIVATE' not in completed.stdout
