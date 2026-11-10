"""Recent Defender evidence is advisory; all scan/native fixtures are inert."""
import base64
import copy
from datetime import datetime, timezone
import json
import os
import re
from pathlib import Path
import subprocess
import threading
import time
from unittest.mock import Mock

import pytest

from optimus.scan_cadence import recent_quick_scan, validate_reuse_hours
from optimus.system_privacy import SystemPrivacy, _ACTIONS, _CONTROLS, _script
from optimus.verification import OutcomeVerification
from test_system_privacy import NativeFixture


def stamp(value):
    return datetime.fromtimestamp(value, timezone.utc).isoformat()


def evidence(now=1800000000):
    start, end = now - 600, now - 540
    identity = '12345678-1234-1234-1234-123456789abc'
    defender = {key: True for key in ('available', 'service_enabled', 'antivirus_enabled',
        'antispyware_enabled', 'realtime_enabled', 'on_access_enabled', 'behavior_enabled', 'ioav_enabled')}
    defender.update(running_mode='Normal', quick_scan_overdue=False, signatures_out_of_date=False,
        quick_scan_start=stamp(start), quick_scan_end=stamp(end), full_scan_metadata_complete=True,
        full_scan_start='', full_scan_end='')
    for prefix in ('antivirus', 'antispyware'):
        defender[prefix + '_signature_version'] = '1.2.3.4'
        defender[prefix + '_signature_updated'] = stamp(now - 3600)
    return {'ok': True, 'schema': 1, 'observed_at': stamp(now - 1), 'defender': defender,
        'antivirus_providers': {'complete': True, 'items': ['Microsoft Defender Antivirus']},
        'threats': {'complete': True, 'active_count': 0, 'items': [], 'truncated': False,
            'active_fingerprint_complete': True, 'active_fingerprint': 'a' * 64,
            'detections_complete': True, 'detections': []},
        'lifecycle': {'complete': True, 'boot_time': stamp(now - 43200), 'log_oldest': stamp(now - 86400),
            'latest_record_id': 102, 'fence_record_id': 102,
            'events': [{'record_id': 101, 'id': 1000, 'scan_id': identity, 'time': stamp(start)},
                       {'record_id': 102, 'id': 1001, 'scan_id': identity, 'time': stamp(end)}]}}


def change(value, path, replacement):
    for key in path[:-1]:
        value = value[key]
    value[path[-1]] = replacement


def test_recent_native_lifecycle_reuse_preserves_provenance_without_completion_claim():
    value = evidence()
    before = copy.deepcopy(value)
    result = recent_quick_scan(value, 6, now=1800000000)
    assert result['reuse'] is True and result['age_seconds'] == 540
    assert result['start_event_record_id'] == 101 and result['completion_event_record_id'] == 102
    assert result['active_threat_count_observed'] == 0 and 'completion_verified' not in result
    assert 'not a new scan' in result['note'] and value == before


@pytest.mark.parametrize('hours', [False, True, None, '6', 6.0, -1, 25, float('nan')])
def test_invalid_reuse_policy_is_rejected(hours):
    with pytest.raises(ValueError):
        validate_reuse_hours(hours)


@pytest.mark.parametrize('hours', [0, 1, 6, 24])
def test_supported_reuse_hours(hours):
    assert validate_reuse_hours(hours) == hours
    assert recent_quick_scan(evidence(), hours, now=1800000000)['reuse'] is (hours != 0)


@pytest.mark.parametrize('path,replacement', [
    (('ok',), 1), (('schema',), True), (('observed_at',), stamp(1800000001)),
    (('observed_at',), stamp(1799999800)), (('observed_at',), '2027-01-15T00:00:00'),
    (('defender', 'quick_scan_start'), stamp(1799970000)),
    (('defender', 'quick_scan_start'), stamp(1799999500)),
    (('defender', 'quick_scan_end'), stamp(1800000001)),
    (('defender', 'quick_scan_overdue'), True), (('defender', 'quick_scan_overdue'), None),
    (('defender', 'signatures_out_of_date'), True), (('defender', 'realtime_enabled'), 1),
    (('defender', 'running_mode'), 'Passive'), (('defender', 'behavior_enabled'), False),
    (('defender', 'on_access_enabled'), None), (('defender', 'antispyware_enabled'), False),
    (('defender', 'ioav_enabled'), False), (('defender', 'full_scan_metadata_complete'), False),
    (('defender', 'full_scan_start'), stamp(1799999900)),
    (('defender', 'full_scan_end'), stamp(1799999800)),
    (('defender', 'antivirus_signature_updated'), stamp(1799999500)),
    (('defender', 'antispyware_signature_updated'), stamp(1799900000)),
    (('defender', 'antivirus_signature_version'), 'unavailable'),
    (('antivirus_providers', 'complete'), False),
    (('antivirus_providers', 'items'), ['Microsoft Defender', 'Third party antivirus']),
    (('antivirus_providers', 'items'), []), (('threats', 'complete'), False),
    (('threats', 'active_count'), True), (('threats', 'active_count'), 1),
    (('threats', 'items'), [{'id': 42}]), (('threats', 'truncated'), True),
    (('threats', 'active_fingerprint'), 'unknown'), (('threats', 'active_fingerprint_complete'), False),
    (('threats', 'detections_complete'), False), (('threats', 'detections'), [None]),
    (('threats', 'detections'), [{'threat_id': 42, 'status_id': 3, 'action_success': 'False'}]),
    (('threats', 'detections'), [{'threat_id': 42, 'status_id': 1, 'action_success': True}]),
    (('threats', 'detections'), [{'threat_id': True, 'status_id': 3, 'action_success': True}]),
    (('lifecycle', 'complete'), False), (('lifecycle', 'log_oldest'), stamp(1799999000)),
    (('lifecycle', 'boot_time'), stamp(1799999500)), (('lifecycle', 'fence_record_id'), 103),
    (('lifecycle', 'latest_record_id'), True), (('lifecycle', 'events'), []),
    (('lifecycle', 'events', 0, 'record_id'), 102), (('lifecycle', 'events', 0, 'id'), True),
    (('lifecycle', 'events', 0, 'scan_id'), 'not-a-guid'),
    (('lifecycle', 'events', 1, 'id'), 1002),
    (('lifecycle', 'events', 1, 'scan_id'), 'aaaaaaaa-1234-1234-1234-123456789abc'),
    (('lifecycle', 'events', 1, 'time'), stamp(1799999300)),
])
def test_uncertain_or_stale_native_state_always_requests_fresh_scan(path, replacement):
    value = evidence()
    change(value, path, replacement)
    assert recent_quick_scan(value, 6, now=1800000000)['reuse'] is False


@pytest.mark.parametrize('value', [None, [], {}, True])
def test_missing_observations_are_not_reusable(value):
    assert recent_quick_scan(value, 6, now=1800000000)['reuse'] is False


def test_all_current_scans_must_have_terminal_native_events():
    value = evidence()
    value['lifecycle']['events'].append({'record_id': 103, 'id': 1000,
        'scan_id': 'aaaaaaaa-1234-1234-1234-123456789abc', 'time': stamp(1799999800)})
    value['lifecycle'].update(latest_record_id=103, fence_record_id=103)
    assert recent_quick_scan(value, 6, now=1800000000)['reuse'] is False
    value['lifecycle']['events'].append({'record_id': 104, 'id': 1002,
        'scan_id': 'aaaaaaaa-1234-1234-1234-123456789abc', 'time': stamp(1799999900)})
    value['lifecycle'].update(latest_record_id=104, fence_record_id=104)
    assert recent_quick_scan(value, 6, now=1800000000)['reuse'] is True


def test_resolved_history_does_not_claim_quarantine_removal_or_a_new_scan():
    value = evidence()
    value['threats']['detections'] = [{'threat_id': 42, 'status_id': 3, 'action_success': True}]
    assert recent_quick_scan(value, 6, now=1800000000)['reuse'] is True


@pytest.mark.parametrize('initial,changed,success,expected', [
    (stamp(1799900000), stamp(1799910000), True, True),
    (stamp(1799999500), stamp(1799999501), True, False),
    (stamp(1799900000), stamp(1799999400), True, False),  # exactly scan start is not before it
    (stamp(1799900000), stamp(1800000001), True, False),
    (stamp(1799910000), stamp(1799900000), True, False),
    ('2020-01-01T00:00:00', stamp(1799910000), True, False),
    (True, stamp(1799910000), True, False), (None, stamp(1799910000), True, False),
    (stamp(1799900000), None, True, False), (stamp(1799900000), 'malformed', True, False),
    (stamp(1799900000), stamp(1799910000), False, False),
])
def test_only_inactive_abandoned_history_predating_the_successful_scan_can_be_reused(initial, changed, success, expected):
    value = evidence()
    value['threats']['detections'] = [{'threat_id': 42, 'status_id': 106, 'action_success': success,
        'initial_detection_time': initial, 'last_status_change_time': changed}]
    assert recent_quick_scan(value, 6, now=1800000000)['reuse'] is expected
    value['threats']['active_count'] = 1
    assert recent_quick_scan(value, 6, now=1800000000)['reuse'] is False


class CadenceFixture(NativeFixture):
    def __init__(self):
        super().__init__()
        self.observed = evidence(time.time())
        self.probe = None

    def __call__(self, operation, payload, **options):
        if operation != 'quick_scan_reuse_status':
            return super().__call__(operation, payload, **options)
        self.calls.append((operation, copy.deepcopy(payload), options))
        return self.probe() if self.probe else copy.deepcopy(self.observed)


def test_manual_scan_always_fresh_even_with_recent_native_evidence(tmp_path):
    runner = CadenceFixture()
    result = SystemPrivacy(tmp_path, runner=runner).scan_spyware()
    assert result['status'] == 'completed' and result['completion_verified'] is True
    assert [call[0] for call in runner.calls].count('quick_scan') == 1
    assert not any(call[0] == 'quick_scan_reuse_status' for call in runner.calls)


def test_automatic_reuse_has_no_fake_100_or_additional_scan(tmp_path):
    runner, events = CadenceFixture(), []
    result = SystemPrivacy(tmp_path, runner=runner).scan_spyware(reuse_hours=6, progress_callback=events.append)
    assert result['status'] == 'skipped_recent_scan' and result['skipped'] and result['read_only']
    assert result['completion_verified'] is False and result['cadence_check']['reuse'] is True
    assert all(event['item_progress']['percent'] != 100 for event in events)
    assert not any(call[0] == 'quick_scan' for call in runner.calls)
    assert [call[0] for call in runner.calls] == ['security_status', 'quick_scan_reuse_status']
    assert json.loads(Path(result['log_path']).read_text())['completion_verified'] is False


@pytest.mark.parametrize('failure', ['old_scan', 'malformed', 'exception'])
def test_automatic_uncertainty_runs_one_fresh_scan(tmp_path, failure):
    runner = CadenceFixture()
    if failure == 'old_scan':
        runner.observed['defender']['quick_scan_overdue'] = True
    elif failure == 'malformed':
        runner.observed = {'ok': True}
    else:
        runner.probe = Mock(side_effect=OSError('Inert provider failure'))
    result = SystemPrivacy(tmp_path, runner=runner).scan_spyware(reuse_hours=6)
    assert result['status'] == 'completed' and result['cadence_check']['reuse'] is False
    assert [call[0] for call in runner.calls].count('quick_scan') == 1


@pytest.mark.parametrize('boundary', ['before', 'security_status', 'probe'])
def test_cancellation_never_starts_scan_after_cadence_preparation(tmp_path, boundary):
    event, runner = threading.Event(), CadenceFixture()
    if boundary == 'before':
        event.set()
    elif boundary == 'probe':
        def probe():
            event.set()
            return runner.observed
        runner.probe = probe
    else:
        original = runner.__call__
        def invoke(operation, payload, **options):
            result = original(operation, payload, **options)
            if operation == 'security_status':
                event.set()
            return result
        runner = invoke
    calls = runner.calls if isinstance(runner, CadenceFixture) else []
    result = SystemPrivacy(tmp_path, runner=runner).scan_spyware(event, reuse_hours=6)
    assert result['status'] == 'cancelled' and result['completion_verified'] is False
    assert not any(call[0] == 'quick_scan' for call in calls)


def test_reuse_is_read_only_without_admin_but_fresh_manual_scan_still_requires_admin(tmp_path):
    runner = CadenceFixture()
    runner.state['context']['is_admin'] = False
    tool = SystemPrivacy(tmp_path, runner=runner)
    assert tool.scan_spyware(reuse_hours=6)['status'] == 'skipped_recent_scan'
    assert tool.scan_spyware()['status'] == 'blocked'
    assert not any(call[0] == 'quick_scan' for call in runner.calls)


def test_reuse_is_not_a_new_verification_confirmation(tmp_path):
    result = SystemPrivacy(tmp_path, runner=CadenceFixture()).scan_spyware(reuse_hours=6)
    native = Mock(side_effect=AssertionError('No new scan postcondition may be requested'))
    report = {'id': 'a' * 32, 'stages': [{'title': 'Scan for spyware and malware',
        'status': 'completed', 'result': result}]}
    checked = OutcomeVerification(tmp_path, native_reader=native).verify(report)
    assert checked['counts'] == {'confirmed': 0, 'needs_review': 0, 'unverified': 0, 'not_applicable': 1}
    assert checked['stages'][0]['checks'][0]['kind'] == 'defender_scan_reuse'
    native.assert_not_called()
    result['completion_verified'] = True
    checked = OutcomeVerification(tmp_path, native_reader=native).verify(report)
    assert checked['counts']['confirmed'] == 0 and checked['counts']['needs_review'] == 1


def powershell():
    return str(Path(os.environ['SystemRoot']) / 'System32/WindowsPowerShell/v1.0/powershell.exe')


def test_all_supported_native_payloads_fit_the_quoted_windows_command_limit():
    longest = max(_CONTROLS, key=len)
    remediation = {'remediation_consent': {'confirmed': True,
        'scope': 'all_current_active_defender_threats', 'resource_fingerprint': 'a' * 64,
        'threat_ids': list(range(2 ** 63 - 200, 2 ** 63))}}
    payloads = dict.fromkeys(_ACTIONS | {'context', 'security_status', 'quick_scan', 'quick_scan_reuse_status'}, {})
    payloads.update(registry_read={'control': longest}, privacy_snapshot={'controls': list(_CONTROLS)},
        registry_write={'control': longest, 'value': 2 ** 31 - 1, 'remove': False,
            'expected': {'exists': True, 'type': 'DWord', 'value': 2 ** 31 - 1}},
        remediate_defender_threats=remediation)
    for operation, payload in payloads.items():
        script = _script(operation, payload)
        arguments = [r'C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe',
            '-NoLogo', '-NoProfile', '-NonInteractive', '-Command', script]
        assert len(subprocess.list2cmdline(arguments)) < 32767, operation
        assert ('function Quick-Scan-ReuseStatus' in script) is (operation == 'quick_scan_reuse_status')


@pytest.mark.parametrize('operation', sorted(_ACTIONS | {'context', 'registry_read', 'privacy_snapshot',
    'registry_write', 'security_status', 'quick_scan', 'quick_scan_reuse_status'}))
def test_only_registry_operations_receive_the_privacy_control_allowlist(operation):
    script = _script(operation, {})
    match = re.search(r"\$C=\[Text.Encoding\]::UTF8.GetString\(\[Convert\]::FromBase64String\('([^']+)'\)\)", script)
    assert match, operation
    controls = json.loads(base64.b64decode(match.group(1)))
    expected = json.loads(json.dumps(_CONTROLS)) if operation in {'registry_read', 'privacy_snapshot', 'registry_write'} else {}
    assert controls == expected


def test_security_command_size_is_independent_of_future_privacy_catalog_growth(monkeypatch):
    before = _script('quick_scan_reuse_status', {})
    monkeypatch.setitem(_CONTROLS, 'future_optional_policy', {'source': 'x' * 50000})
    assert _script('quick_scan_reuse_status', {}) == before
    arguments = [r'C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe',
        '-NoLogo', '-NoProfile', '-NonInteractive', '-Command', before]
    assert len(subprocess.list2cmdline(arguments)) < 32767


@pytest.mark.skipif(os.name != 'nt', reason='Native PowerShell parser only; no operations execute.')
def test_all_native_privacy_operation_branches_parse_without_running(tmp_path):
    operations = {'context', 'registry_read', 'privacy_snapshot', 'registry_write',
        'security_status', 'quick_scan', 'quick_scan_reuse_status'} | _ACTIONS
    payload = tmp_path / 'scripts.json'
    payload.write_text(json.dumps({op: _script(op, {}) for op in sorted(operations)}), encoding='utf-8')
    script = r'''
$scripts=Get-Content -LiteralPath '__PATH__' -Raw|ConvertFrom-Json
foreach($property in $scripts.PSObject.Properties){
 $tokens=$null;$errors=$null
 [void][Management.Automation.Language.Parser]::ParseInput($property.Value,[ref]$tokens,[ref]$errors)
 if($errors.Count){throw ('Parse failure in '+$property.Name+': '+[string]$errors[0])}
}
[Console]::Write($scripts.PSObject.Properties.Name.Count)
'''.replace('__PATH__', str(payload).replace("'", "''"))
    completed = subprocess.run([powershell(), '-NoLogo', '-NoProfile', '-NonInteractive', '-Command', script],
        capture_output=True, text=True, timeout=20)
    assert completed.returncode == 0, completed.stderr
    assert int(completed.stdout) == len(operations)


_NATIVE_FIXTURE = r'''
$script:Now=[datetime]::UtcNow
$script:Boot=$script:Now.AddHours(-12)
function Fixture-Event($ID,$Record,$Time){
 $event=[pscustomobject]@{Id=[int]$ID;RecordId=[long]$Record;TimeCreated=$Time;ProviderName='Microsoft-Windows-Windows Defender'}
 $event|Add-Member ScriptMethod ToXml {return '<Event><EventData><Data Name="Scan ID">{12345678-1234-1234-1234-123456789abc}</Data></EventData></Event>'}
 return $event
}
$script:Started=Fixture-Event 1000 101 $script:Now.AddMinutes(-10)
$script:Finished=Fixture-Event __TERMINAL__ 102 $script:Now.AddMinutes(-9)
function Get-WinEvent {param($ListLog,$LogName,[switch]$Oldest,$FilterHashtable,$MaxEvents,$ErrorAction)
 if($ListLog){return [pscustomobject]@{IsEnabled=$true}}
 if($Oldest){return [pscustomobject]@{TimeCreated=$script:Boot.AddDays(-1)}}
 if($MaxEvents -eq 1){return $script:Finished}
 return @($script:Finished,$script:Started)
}
function Get-CimInstance {param($ClassName,$Namespace,$ErrorAction)
 if($ClassName -eq 'Win32_OperatingSystem'){return [pscustomobject]@{LastBootUpTime=$script:Boot}}
 if($ClassName -eq 'AntiVirusProduct'){return [pscustomobject]@{displayName='Microsoft Defender Antivirus'}}
 throw 'Unexpected native inventory fixture'
}
function Get-MpComputerStatus {param($ErrorAction)
 return [pscustomobject]@{AMServiceEnabled=$true;AntivirusEnabled=$true;AntispywareEnabled=$true;
 RealTimeProtectionEnabled=$true;AMRunningMode='Normal';OnAccessProtectionEnabled=$true;
 BehaviorMonitorEnabled=$true;IoavProtectionEnabled=$true;QuickScanOverdue=$false;DefenderSignaturesOutOfDate=$false;
 QuickScanStartTime=$script:Now.AddMinutes(-10);QuickScanEndTime=$script:Now.AddMinutes(-9);
 FullScanStartTime=__FULL_START__;FullScanEndTime=__FULL_END__;
 AntivirusSignatureVersion='1.2.3.4';AntivirusSignatureLastUpdated=$script:Now.AddHours(-1);
 AntispywareSignatureVersion='1.2.3.4';AntispywareSignatureLastUpdated=$script:Now.AddHours(-1)}
}
function Get-MpThreat {param($ErrorAction) return @()}
function Get-MpThreatDetection {param($ErrorAction) return __DETECTIONS__}
function Start-MpScan {throw 'FORBIDDEN_NATIVE_SCAN'}
function Set-MpPreference {throw 'FORBIDDEN_NATIVE_WRITE'}
function Update-MpSignature {throw 'FORBIDDEN_NATIVE_UPDATE'}
function Remove-MpThreat {throw 'FORBIDDEN_NATIVE_REMOVAL'}
'''


@pytest.mark.skipif(os.name != 'nt', reason='Inert native metadata fixtures only.')
@pytest.mark.parametrize('terminal,full_start,full_end,detections,expected', [
    (1001, '$null', '$null', '@()', True),
    (1002, '$null', '$null', '@()', False),
    (1001, "'malformed'", "'malformed'", '@()', False),
    (1001, '$false', '$false', '@()', False),
    (1001, '$null', '$script:Now.AddHours(-1)', '@()', False),
    (1001, '$script:Now.AddHours(-2)', '$script:Now.AddHours(-1)', '@()', True),
    (1001, '$null', '$null', "@([pscustomobject]@{ThreatID=[uint64]42;ThreatStatusID=[uint32]3;ActionSuccess=$true})", True),
    (1001, '$null', '$null', "@([pscustomobject]@{ThreatID=[long]42;ThreatStatusID=[byte]3;ActionSuccess=$true})", True),
    (1001, '$null', '$null', "@([pscustomobject]@{ThreatID=42;ThreatStatusID=3;ActionSuccess='False'})", False),
    (1001, '$null', '$null', "@([pscustomobject]@{ThreatID=42;ThreatStatusID='3';ActionSuccess=$true})", False),
    (1001, '$null', '$null', "@([pscustomobject]@{ThreatID='42';ThreatStatusID=3;ActionSuccess=$true})", False),
    (1001, '$null', '$null', "@([pscustomobject]@{ThreatID=[uint64]::MaxValue;ThreatStatusID=3;ActionSuccess=$true})", False),
    (1001, '$null', '$null', "@([pscustomobject]@{ThreatID=[long]42;ThreatStatusID=[byte]106;ActionSuccess=$true;InitialDetectionTime=$script:Now.AddHours(-24);LastThreatStatusChangeTime=$script:Now.AddHours(-23)})", True),
    (1001, '$null', '$null', "@([pscustomobject]@{ThreatID=[long]42;ThreatStatusID=[byte]106;ActionSuccess=$true;InitialDetectionTime=$script:Now.AddHours(-24);LastThreatStatusChangeTime=$script:Now.AddMinutes(-5)})", False),
    (1001, '$null', '$null', "@([pscustomobject]@{ThreatID=[long]42;ThreatStatusID=[byte]106;ActionSuccess=$true;InitialDetectionTime=$script:Now.AddHours(-24);LastThreatStatusChangeTime=$script:Now.AddHours(1)})", False),
    (1001, '$null', '$null', "@([pscustomobject]@{ThreatID=[long]42;ThreatStatusID=[byte]106;ActionSuccess=$true;InitialDetectionTime='2020-01-01';LastThreatStatusChangeTime=$script:Now.AddHours(-23)})", False),
    (1001, '$null', '$null', "@([pscustomobject]@{ThreatID=[long]42;ThreatStatusID=[byte]106;ActionSuccess=$true})", False),
])
def test_native_probe_preserves_unknown_types_and_never_executes_scan(tmp_path, terminal, full_start, full_end, detections, expected):
    native = (_NATIVE_FIXTURE.replace('__TERMINAL__', str(terminal)).replace('__FULL_START__', full_start)
        .replace('__FULL_END__', full_end).replace('__DETECTIONS__', detections))
    marker = "try {\n $result=switch('quick_scan_reuse_status')"
    script = _script('quick_scan_reuse_status', {})
    assert script.count(marker) == 1
    script = script.replace(marker, native + '\n' + marker, 1)
    path = tmp_path / 'native-fixture.ps1'
    path.write_text(script, encoding='utf-8')
    completed = subprocess.run([powershell(), '-NoLogo', '-NoProfile', '-NonInteractive',
        '-ExecutionPolicy', 'Bypass', '-File', str(path)], capture_output=True, text=True, timeout=20)
    assert completed.returncode == 0, completed.stderr
    observed = json.loads(completed.stdout)
    assert observed['ok'] is True
    assert recent_quick_scan(observed, 6)['reuse'] is expected
    assert 'FORBIDDEN_NATIVE' not in completed.stdout + completed.stderr
