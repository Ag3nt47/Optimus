"""Batch privacy reads keep failed snapshots and actual writes fail-closed."""
import copy
import json
import os
from pathlib import Path
import subprocess
import threading

import pytest

import optimus.system_privacy as privacy


SETTINGS = {'privacy_advertising_enabled': True, 'privacy_diagnostics_enabled': True}
SELECTED = [key for key, value in privacy._CONTROLS.items()
            if value['master'] in SETTINGS]


def dword(value):
    return {'exists': True, 'type': 'DWord', 'value': value}


class NativeFixture:
    def __init__(self):
        self.calls = []
        self.values = {key: dword(value['desired']) for key, value in privacy._CONTROLS.items()}
        self.context = {'platform': 'Windows', 'build': 26100, 'ubr': 5000,
                        'edition': 'Enterprise', 'is_admin': True}
        self.snapshot_hook = None
        self.write_hook = None
        self.write_current = None

    def __call__(self, operation, payload, **options):
        self.calls.append((operation, copy.deepcopy(payload)))
        if operation == 'privacy_snapshot':
            result = {'ok': True, 'schema': 1, 'context': copy.deepcopy(self.context),
                      'controls': {key: {'ok': True, 'current': copy.deepcopy(self.values[key])}
                                   for key in payload['controls']}}
            if self.snapshot_hook:
                self.snapshot_hook(result)
            return result
        if operation == 'context':
            return copy.deepcopy(self.context)
        if operation == 'registry_read':
            return copy.deepcopy(self.values[payload['control']])
        assert operation == 'registry_write', 'Unexpected native mutation'
        if self.write_hook:
            self.write_hook(payload)
        if self.values[payload['control']] != payload['expected']:
            return {'ok': False}
        self.values[payload['control']] = dword(payload['value'])
        return {'ok': True, 'current': copy.deepcopy(self.write_current or self.values[payload['control']])}


@pytest.fixture
def batch(tmp_path, monkeypatch):
    native = NativeFixture()
    monkeypatch.setattr(privacy, '_native_request', native)
    return privacy.SystemPrivacy(tmp_path), native


def test_default_native_batches_nine_reads_and_publishes_checking_before_wait(batch, tmp_path):
    tool, native = batch
    events = []
    preferences = tmp_path / 'preferences.json'
    preferences.write_bytes(b'{"saved":true}\r\n')
    def checking_before_native_result(value):
        assert events and events[0]['item_progress']['percent'] is None
    native.snapshot_hook = checking_before_native_result
    report = tool.apply_privacy(SETTINGS, progress_callback=events.append)
    assert [operation for operation, _ in native.calls] == ['privacy_snapshot']
    assert native.calls[0][1] == {'controls': SELECTED} and len(SELECTED) == 9
    assert report['status'] == 'completed' and report['checked_count'] == 9
    assert report['changed_count'] == report['failed_items'] == 0
    assert all(item['status'] == 'already_configured' for item in report['items'])
    assert events[0]['item_progress']['percent'] is None
    assert 'native query' in events[0]['description']
    assert events[-1]['item_progress']['percent'] == 100
    saved = json.loads((tool.directory / (report['id'] + '.json')).read_text())
    assert saved['checked_count'] == 9 and len(saved['items']) == 9
    assert preferences.read_bytes() == b'{"saved":true}\r\n'


def test_injected_legacy_runner_keeps_existing_context_and_read_contract(tmp_path):
    native = NativeFixture()
    report = privacy.SystemPrivacy(tmp_path, runner=native).apply_privacy(SETTINGS)
    assert [operation for operation, _ in native.calls] == ['context'] + ['registry_read'] * 9
    assert report['status'] == 'completed' and report['failed_items'] == 0


@pytest.mark.parametrize('bad', [
    {'ok': False, 'error_type': 'UnauthorizedAccessException'},
    {'ok': True, 'current': {'exists': True, 'type': 'String', 'value': '0'}},
    {'ok': True, 'current': {'exists': True, 'type': 'DWord', 'value': True}},
    {'ok': True, 'current': {'exists': True, 'type': 'DWord', 'value': 99}},
    {'ok': True}, None,
])
def test_denied_or_malformed_control_is_failed_without_losing_other_outcomes(batch, bad):
    tool, native = batch
    native.snapshot_hook = lambda result: result['controls'].__setitem__('advertising_id', bad)
    report = tool.apply_privacy(SETTINGS)
    assert report['status'] == 'completed_with_errors' and report['failed_items'] == 1
    assert report['items'][0]['status'] == 'failed' and report['checked_count'] == 9
    assert all(item['status'] == 'already_configured' for item in report['items'][1:])
    assert [operation for operation, _ in native.calls] == ['privacy_snapshot']
    saved = json.loads((tool.directory / (report['id'] + '.json')).read_text())
    assert saved['items'][0]['status'] == 'failed'
    if bad and bad.get('error_type'):
        assert saved['items'][0]['read_error_type'] == bad['error_type']


@pytest.mark.parametrize('change', [
    lambda result: result['controls'].pop('advertising_id'),
    lambda result: result['controls'].__setitem__('unknown_control', {'ok': True, 'current': dword(0)}),
    lambda result: result.__setitem__('schema', True),
    lambda result: result.__setitem__('ok', False),
    lambda result: result['context'].__setitem__('build', True),
    lambda result: result['context'].__setitem__('is_admin', 'true'),
])
def test_incomplete_or_malformed_batch_never_becomes_healthy_or_writes(batch, change):
    tool, native = batch
    native.snapshot_hook = change
    report = tool.apply_privacy(SETTINGS)
    assert report['status'] == 'completed_with_errors' and report['failed_items'] == 9
    assert all(item['status'] == 'failed' for item in report['items'])
    assert report['changed_count'] == 0
    assert [operation for operation, _ in native.calls] == ['privacy_snapshot']


def test_cancel_during_batch_journals_cancelled_outcome_without_writes(batch):
    tool, native = batch
    event = threading.Event()
    native.snapshot_hook = lambda result: event.set()
    report = tool.apply_privacy(SETTINGS, cancel_event=event)
    assert report['status'] == 'cancelled' and report['checked_count'] == 0
    assert report['cancelled'] and report['items'] == []
    assert [operation for operation, _ in native.calls] == ['privacy_snapshot']
    assert json.loads((tool.directory / (report['id'] + '.json')).read_text())['cancelled']


def test_cancel_before_batch_never_starts_native_process_or_journal(batch):
    tool, native = batch
    event = threading.Event()
    event.set()
    report = tool.apply_privacy(SETTINGS, cancel_event=event)
    assert report['status'] == 'cancelled' and native.calls == []
    assert not tool.directory.exists()


def test_stale_snapshot_cannot_overwrite_fresh_value_and_original_is_durable(batch):
    tool, native = batch
    native.values['advertising_id'] = dword(1)
    def changed_after_snapshot(result):
        native.values['advertising_id'] = dword(0)
    native.snapshot_hook = changed_after_snapshot
    def before_write(payload):
        saved = json.loads(next(tool.directory.glob('*.json')).read_text())
        assert saved['items'][0]['status'] == 'prepared'
        assert saved['items'][0]['before'] == payload['expected'] == dword(1)
    native.write_hook = before_write
    report = tool.apply_privacy(SETTINGS)
    assert report['items'][0]['status'] == 'failed' and report['failed_items'] == 1
    assert native.values['advertising_id'] == dword(0)
    assert report['changed_count'] == 0
    assert [operation for operation, _ in native.calls] == ['privacy_snapshot', 'registry_write']


def test_write_still_requires_post_write_native_readback(batch):
    tool, native = batch
    native.values['advertising_id'] = dword(1)
    native.write_current = dword(1)
    report = tool.apply_privacy(SETTINGS)
    assert report['items'][0]['status'] == 'failed' and report['changed_count'] == 0
    assert report['failed_items'] == 1 and report['checked_count'] == 9


def test_verified_write_remains_separate_from_single_batch_and_retains_before(batch):
    tool, native = batch
    native.values['advertising_id'] = dword(1)
    report = tool.apply_privacy(SETTINGS)
    assert report['status'] == 'completed' and report['changed_count'] == 1
    assert report['items'][0]['before'] == dword(1)
    assert report['items'][0]['status'] == 'applied'
    assert [operation for operation, _ in native.calls] == ['privacy_snapshot', 'registry_write']


@pytest.mark.skipif(os.name != 'nt', reason='Exercises actual Windows PowerShell with mocked registry functions.')
@pytest.mark.parametrize('controls', [SELECTED, ['advertising_id', 'unknown_control'], ['advertising_id'] * 2])
def test_actual_powershell_batch_is_read_only_and_isolates_control_error(controls):
    # Override every native reader before the operation switch. No registry API,
    # security cmdlet, service or process-control action is called by this fixture.
    script = privacy._script('privacy_snapshot', {'controls': controls})
    marker = "try {\n $result=switch('privacy_snapshot')"
    overrides = """function Context { @{platform='Windows';build=26100;ubr=5000;edition='Enterprise';is_admin=$false} }
function Read-Control($id) {
 if($id -eq 'advertising_id'){throw [UnauthorizedAccessException]::new('Fixture denied')}
 @{exists=$true;type='DWord';value=0}
}
"""
    assert script.count(marker) == 1
    script = script.replace(marker, overrides + marker)
    executable = Path(os.environ.get('SystemRoot', r'C:\Windows')) / 'System32/WindowsPowerShell/v1.0/powershell.exe'
    process = subprocess.run([str(executable), '-NoLogo', '-NoProfile', '-NonInteractive', '-Command', script],
        capture_output=True, text=True, encoding='utf-8', timeout=20,
        creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    result = json.loads(process.stdout.strip())
    if controls == SELECTED:
        assert process.returncode == 0 and result['ok'] is True and result['schema'] == 1
        assert set(result['controls']) == set(SELECTED)
        assert result['controls']['advertising_id']['ok'] is False
        assert result['controls']['advertising_id']['error_type'] == 'UnauthorizedAccessException'
        assert all(value['ok'] is True for key, value in result['controls'].items() if key != 'advertising_id')
    else:
        assert result['ok'] is False and 'controls' not in result
