"""Local Next uses inert providers; no real scan, install or cleanup runs."""
import copy
import subprocess
import threading
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from optimus.automated_system import run_drivers
from optimus.automated_software import run_software
from optimus.maintenance import Maintenance
from optimus.navigation import NavigationHub
from optimus.workflow import DEFAULTS, Workflow, Preferences
from optimus.native_workflow import NativeWorkflow
from test_workflow import MockBackend
from test_automated_system import backend as driver_backend, driver, driver_config
from test_automated_software import backend as app_backend, configuration


def issue(hub, identity, action='next'):
    state = hub.snapshot()
    row = next(row for row in state['jobs'] if row['id'] == identity)
    return hub.request(state['session_id'], identity, row['revision'], action)


def own(service):
    event = threading.Event()
    hub = NavigationHub(); hub.begin('a' * 32, cancel_event=event)
    service.workflow = SimpleNamespace(navigation=hub)
    return hub, event


@pytest.mark.parametrize('identity', ['maintenance', 'software_updates', 'driver_updates', 'security_scan'])
@pytest.mark.parametrize('mutation', [False, True])
def test_acknowledgement_is_immediate_local_and_requires_terminal_receipt(identity, mutation):
    hub, global_stop = own(SimpleNamespace())
    cursor = hub.cursor(identity); cursor.begin_item('first', 'Current exact item', kind='mutation' if mutation else 'scan')
    if mutation:
        cursor.phase('installing', mutation_started=True)
    sibling = hub.cursor('sibling'); sibling.begin_item('other', 'Other work')
    reply = issue(hub, identity)
    assert reply['accepted'] and reply['pending']['action'] == 'next'
    expected = 'finishing_current_installer' if mutation else 'waiting_for_safe_stop'
    assert reply['control_status'] == expected
    row = next(row for row in hub.snapshot()['jobs'] if row['id'] == identity)
    assert row['control_status'] == expected and row['pending_action'] == 'next'
    assert 'Skip requested' in row['message'] and not row['can_next']
    assert cursor.cancellation().is_set() and not sibling.cancellation().is_set() and not global_stop.is_set()
    with pytest.raises(RuntimeError, match='preceding item'):
        cursor.begin_item('second', 'Cannot advance until provider has returned')
    cursor.finish('completed' if mutation else 'cancelled', mutation_started=mutation)
    assert cursor.begin_item('second', 'Next independent item')
    assert not cursor.cancellation().is_set() and not global_stop.is_set()


def test_security_stage_has_own_cursor_token_and_following_stage_runs(tmp_path):
    service = MockBackend(tmp_path); workflow = Workflow(service); service.workflow = workflow
    global_stop = threading.Event(); observed = []
    def scan(cancel_event=None, *, progress_callback=None):
        snapshot = workflow.navigation.snapshot()
        maintenance = next(row for row in snapshot['jobs'] if row['id'] == 'maintenance')
        security = next(row for row in snapshot['jobs'] if row['id'] == 'security_scan')
        assert maintenance['current'] is None and security['current'] is not None
        assert issue(workflow.navigation, 'security_scan')['accepted']
        assert cancel_event.is_set() and not global_stop.is_set()
        assert not workflow.navigation.cursor('maintenance').cancellation().is_set()
        observed.append('client_retired')
        return {'cancelled': True, 'completion_verified': False, 'scan_may_continue': True,
                'status': 'cancelled', 'failed_items': 0}
    service.scan_spyware = scan
    result = workflow.run(dict(DEFAULTS, spyware_scan_enabled=True), lambda _: None, global_stop)
    stage = next(row for row in result['stages'] if row['title'] == 'Scan for spyware and malware')
    assert observed == ['client_retired'] and stage['status'] == 'cancelled'
    assert stage['result']['navigation_cancelled'] is True
    assert not result['cancelled'] and not global_stop.is_set()
    assert result['stages'][-1]['status'] == 'completed'
    assert service.calls.count(('audit',)) == 2
    state = next(row for row in workflow.navigation.snapshot()['jobs'] if row['id'] == 'security_scan')
    assert state['history_count'] == 1 and not state['can_next']


def test_file_cleanup_acknowledgement_uses_safe_checkpoint_instead_of_installer_wait():
    hub, _ = own(SimpleNamespace())
    cursor = hub.cursor('maintenance')
    cursor.begin_item('cleanup', 'Clean temporary files', kind='mutation')
    cursor.phase('applying', mutation_started=True)
    reply = issue(hub, 'maintenance')
    assert reply['control_status'] == 'waiting_for_safe_stop'
    assert 'safe checkpoint' in reply['message'] and 'finishing current installer' not in reply['message']


@pytest.mark.parametrize('native', [False, True])
def test_cleaner_receives_local_token_stops_per_file_and_advances(tmp_path, native):
    service = MockBackend(tmp_path)
    service.system = 'linux' if native else 'win32'
    service.preference_store = SimpleNamespace(validate=Preferences.validate)
    workflow = (NativeWorkflow if native else Workflow)(service); service.workflow = workflow
    global_stop = threading.Event(); removed = []
    def scan(category_ids, age, *, cancel_event=None):
        assert cancel_event is not None and not cancel_event.is_set()
        return {'files': [1, 2, 3], 'total_bytes': 3, 'complete': True}
    def apply(plan, *, cancel_event=None):
        for item in plan['files']:
            if cancel_event.is_set():
                break
            removed.append(item)
            assert issue(workflow.navigation, 'maintenance')['accepted']
        return {'deleted_count': len(removed), 'deleted_bytes': len(removed), 'skipped_count': 0,
                'cancelled': cancel_event.is_set()}
    service.cleanup_scan, service.cleanup_apply = scan, apply
    result = workflow.run(dict(DEFAULTS, cleanup_category_ids=[], browser_cache_enabled=False,
        browser_history_enabled=False, browser_cookies_enabled=False, browser_download_history_enabled=False),
        lambda _: None, global_stop)
    stage = next(row for row in result['stages'] if row['title'].startswith('Clean '))
    assert removed == [1] and stage['status'] == 'cancelled'
    assert stage['result']['result']['deleted_count'] == 1
    assert not result['cancelled'] and not global_stop.is_set()
    assert service.calls.count(('audit',)) == 2


def test_driver_download_token_reaches_provider_and_next_offer_runs_after_abort_receipt():
    service = driver_backend(); hub, global_stop = own(service)
    first, second = driver(), driver(id='00000000-0000-0000-0000-000000000002')
    service.driver_updates_scan.return_value['updates'] = [first, second]
    order = []
    def apply(record, accept_eula=False, *, cancel_event=None):
        assert cancel_event is not None
        order.append(record['id'])
        if record['id'] == first['id']:
            assert issue(hub, 'driver_updates')['accepted'] and cancel_event.is_set()
            # Native provider returns only after RequestAbort + EndDownload.
            return {'cancelled': True, 'installer_started': False, 'outcomeKnown': True, 'running': False}
        assert not cancel_event.is_set()
        return {'resultCode': 2, 'hresult': 0, 'installer_started': True, 'outcomeKnown': True, 'running': False}
    service.driver_update_apply_unattended = apply
    result = run_drivers(service, driver_config(driver_approved_updates=[first, second]), cancel_event=global_stop)
    assert order == [first['id'], second['id']] and result['navigation_cancelled_items'] == 1
    assert not result['cancelled'] and not global_stop.is_set()


def test_app_live_installer_waits_for_quiescence_then_continues_without_retry():
    service = app_backend(); hub, global_stop = own(service)
    entered, release = threading.Event(), threading.Event(); calls = []
    def apply(identity, *, cancel_event=None):
        calls.append(identity)
        if identity == 'Vendor.One':
            entered.set()
            assert release.wait(3)
            assert cancel_event.is_set()
        return {'exitCode': 0, 'verified': True, 'installer_started': True, 'outcomeKnown': True, 'running': False}
    service.update_app_unattended = apply
    results = []
    thread = threading.Thread(target=lambda: results.append(run_software(service, configuration(), cancel_event=global_stop)))
    thread.start()
    try:
        assert entered.wait(3)
        assert issue(hub, 'software_updates')['control_status'] == 'finishing_current_installer'
        assert calls == ['Vendor.One'] and not global_stop.is_set()
        release.set(); thread.join(3)
        assert not thread.is_alive() and calls == ['Vendor.One', 'Vendor.Two']
        assert not results[0]['cancelled'] and not global_stop.is_set()
    finally:
        release.set(); thread.join(3)


def test_exact_read_only_runner_rejects_installer_command_without_launch(monkeypatch):
    launch = Mock(); monkeypatch.setattr('optimus.maintenance.subprocess.Popen', launch)
    with pytest.raises(ValueError, match='read-only'):
        Maintenance._read_only_winget(['fixture', 'upgrade', '--id', 'Vendor.One'], threading.Event())
    launch.assert_not_called()


def test_update_revalidation_cancellation_returns_known_skip_before_pin_or_installer(tmp_path):
    tool = Maintenance(SimpleNamespace(), tmp_path)
    row = {'id': 'Vendor.One', 'source': 'winget', 'version': '1.0', 'available': '2.0'}
    tool._last_update_ids = {'Vendor.One'}; tool._last_update_packages = {'Vendor.One': row}
    tool._last_update_scan = 1.0
    stop = threading.Event()
    def scan(*, cancel_event=None):
        assert cancel_event is stop
        stop.set()
        return {'structured': False, 'exitCode': 130}
    tool.updates_scan = scan; tool._pin_status = Mock(); tool._run = Mock()
    result = tool.updates_apply('Vendor.One', confirmed=True, unattended=True, cancel_event=stop)
    assert result['cancelled'] and result['installer_started'] is False
    assert result['outcomeKnown'] is True and result['running'] is False
    tool._pin_status.assert_not_called(); tool._run.assert_not_called()
