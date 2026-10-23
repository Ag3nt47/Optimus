"""Worker controls use fixture scans/installers only; no host maintenance."""
import copy
import subprocess
import threading
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from optimus.navigation import NavigationHub, WorkItem
from optimus.automated_software import run_software
from optimus.automated_system import run_drivers
from optimus.native_workflow import run_native_software
from optimus.parallel_updates import (installer_barrier, _journal_result, _uncertain, _driver_completion_counts, update_completion_state,
    ParallelAppUpdates, ParallelInstallerQueue, ProgressMux, WINDOWS_SOFTWARE, WINDOWS_DRIVERS)
from optimus.maintenance import Maintenance
from optimus.workflow import Workflow, DEFAULTS
from test_automated_software import backend as app_backend, configuration, package
from test_automated_system import backend as driver_backend, driver, driver_config
from test_workflow import MockBackend


def owned(service):
    hub = NavigationHub()
    hub.begin('1' * 32)
    service.workflow = SimpleNamespace(navigation=hub, active=True)
    return hub


def issue(hub, job_id, action):
    snapshot = hub.snapshot()
    job = next(row for row in snapshot['jobs'] if row['id'] == job_id)
    return hub.request(snapshot['session_id'], job_id, job['revision'], action)


def success(**fields):
    return dict(exitCode=0, verified=True, installer_started=True,
                outcomeKnown=True, running=False, **fields)


def test_constructor_and_poll_are_inert_and_identity_bound():
    hub = NavigationHub()
    assert hub.snapshot()['jobs'] == []
    assert not hub.request('1' * 32, 'maintenance', 0, 'next')['accepted']
    hub.begin('1' * 32)
    cursor = hub.cursor('maintenance')
    cursor.begin_item('first', 'First read-only scan')
    original = hub.snapshot()
    job = original['jobs'][0]
    assert job['current']['thread_id'] == threading.get_native_id()
    assert not hub.request('2' * 32, job['id'], job['revision'], 'next')['accepted']
    assert not hub.request(original['session_id'], job['id'], True, 'next')['accepted']
    assert not hub.request(original['session_id'], job['id'], job['revision'], [])['accepted']
    cursor.phase('checking source')
    assert not hub.request(original['session_id'], job['id'], job['revision'], 'next')['accepted']
    assert issue(hub, 'maintenance', 'next')['accepted']
    assert cursor.local_requested()
    assert not issue(hub, 'maintenance', 'cancel')['accepted']


def test_back_and_history_next_do_not_replay_or_cancel_live_mutation():
    hub = NavigationHub(); hub.begin('1' * 32)
    cursor = hub.cursor('software_updates')
    cursor.begin_item('one', 'Already installed', kind='mutation')
    cursor.phase('installing', mutation_started=True)
    cursor.finish('completed')
    cursor.begin_item('two', 'Live second update', kind='mutation')
    assert issue(hub, cursor.id, 'back')['accepted']
    state = hub.snapshot()['jobs'][0]
    assert state['viewing_history'] and state['view_item']['key'] == 'one'
    assert state['next_mode'] == 'history' and 'Revert' in state['back_reason'] or state['viewing_history']
    assert issue(hub, cursor.id, 'next')['accepted']
    assert not cursor.local_requested()
    assert not hub.snapshot()['jobs'][0]['viewing_history']
    cursor.finish('completed', mutation_started=True)
    with pytest.raises(ValueError, match='cannot be replayed'):
        cursor.begin_item('one', 'Never reinstall through Back', kind='mutation')


def test_cancel_while_viewing_history_targets_live_item_only():
    hub = NavigationHub(); hub.begin('1' * 32)
    cursor = hub.cursor('maintenance')
    cursor.begin_item('one', 'Old scan'); cursor.finish('completed')
    cursor.begin_item('two', 'Live scan')
    issue(hub, cursor.id, 'back')
    response = issue(hub, cursor.id, 'cancel')
    assert response['accepted'] and response['pending']['item_key'] == 'two'
    assert 'Live scan' in hub.snapshot()['jobs'][0]['cancel_reason']


def test_local_request_does_not_cancel_other_workers_or_next_unit():
    hub = NavigationHub(); hub.begin('1' * 32)
    one, two = hub.cursor('software_updates'), hub.cursor('driver_updates')
    one.begin_item('a', 'App'); two.begin_item('b', 'Driver')
    issue(hub, one.id, 'next')
    assert one.cancellation().is_set() and not two.cancellation().is_set()
    one.finish('cancelled', mutation_started=False)
    one.begin_item('c', 'Next app')
    assert not one.cancellation().is_set()


def test_unknown_outcome_and_new_session_reject_late_commands():
    hub = NavigationHub(); hub.begin('1' * 32)
    cursor = hub.cursor('driver_updates'); cursor.begin_item('one', 'Driver', kind='mutation')
    old = hub.snapshot()
    cursor.finish('failed', outcome_known=False)
    assert cursor.begin_item('two', 'Never launch', kind='mutation') is False
    assert cursor.begin_item('verify', 'Read-only outcome verification', kind='scan') is True
    cursor.finish('completed')
    assert not any(hub.snapshot()['jobs'][0][field] for field in ('can_next', 'can_cancel', 'can_back'))
    hub.end(); hub.begin('2' * 32)
    hub.cursor('driver_updates').begin_item('three', 'New run')
    assert not hub.request(old['session_id'], 'driver_updates', old['jobs'][0]['revision'], 'next')['accepted']


def test_history_and_workers_are_bounded():
    hub = NavigationHub(); hub.begin('1' * 32)
    cursor = hub.cursor('maintenance')
    for index in range(100):
        cursor.begin_item(str(index), 'Read-only'); cursor.finish('completed')
    assert len(cursor.history) == 64
    for index in range(11): hub.cursor('worker' + str(index))
    with pytest.raises(ValueError, match='limit'): hub.cursor('overflow')


@pytest.mark.parametrize('action', ['next', 'cancel'])
def test_app_control_skips_exact_prelaunch_item_and_continues(action):
    service = app_backend(); service.update_app_unattended.return_value = success()
    hub = owned(service)
    def progress(state):
        if state['id'] == 'Vendor.One': assert issue(hub, 'software_updates', action)['accepted']
    result = run_software(service, configuration(), item_callback=progress)
    assert [call.args[0] for call in service.update_app_unattended.call_args_list] == ['Vendor.Two']
    assert result['items'][0]['status'] == 'cancelled'
    assert result['navigation_cancelled_items'] == 1 and not result['cancelled']
    assert installer_barrier(result) is None and not _uncertain(result)
    assert _journal_result(result)['items'][0]['result']['navigation_cancelled'] is True


def test_live_app_installer_drains_actual_completion_before_next():
    service = app_backend(); hub = owned(service); order = []
    def install(identity, **options):
        order.append(identity)
        if identity == 'Vendor.One':
            assert issue(hub, 'software_updates', 'next')['accepted']
            assert options['cancel_event'].is_set()
            assert 'force-stopped' in hub.snapshot()['jobs'][0]['cancel_reason']
        return success()
    service.update_app_unattended.side_effect = install
    result = run_software(service, configuration())
    assert order == ['Vendor.One', 'Vendor.Two']
    assert [row['status'] for row in result['items']] == ['completed', 'completed']
    assert result['navigation_cancelled_items'] == 0


def test_unknown_app_result_stops_before_next_and_disables_navigation():
    service = app_backend(); hub = owned(service)
    service.update_app_unattended.return_value = dict(installer_started=True, outcomeKnown=False, running=True)
    result = run_software(service, configuration())
    assert service.update_app_unattended.call_count == 1 and installer_barrier(result)
    assert not hub.snapshot()['jobs'][0]['can_next']


@pytest.mark.parametrize('native', [False, True])
@pytest.mark.parametrize('bad', [
    {'installer_started':True,'outcomeKnown':False,'running':True},
    {'installer_started':False,'outcomeKnown':True,'running':False,'restartNeeded':True},
])
def test_navigation_marker_cannot_hide_unknown_or_restart(native, bad):
    service = app_backend(); service.system = 'linux' if native else 'win32'
    service.updates_scan.return_value['complete'] = True
    hub = owned(service)
    service.update_app_unattended.return_value = dict(exitCode=0, verified=True,
        navigation_cancelled=True, **bad)
    result = (run_native_software if native else run_software)(service, configuration(setup_version=2))
    assert service.update_app_unattended.call_count == 1
    assert installer_barrier(result)


def test_work_item_never_bypasses_closed_or_unknown_cursor():
    hub = NavigationHub(); hub.begin('1' * 32)
    cursor = hub.cursor('maintenance'); cursor.end()
    with pytest.raises(RuntimeError, match='unresolved'):
        WorkItem(cursor,'new','Never run a refused item')


def test_item_receipt_failure_stops_next_native_launch():
    service = app_backend(); service.update_app_unattended.return_value = success()
    hub = owned(service)
    cursor = hub.cursor('software_updates')
    cursor.receipt_callback = Mock(side_effect=OSError('Fixture terminal receipt write failed'))
    result = run_software(service, configuration())
    assert service.update_app_unattended.call_count == 1
    assert installer_barrier(result) and not hub.snapshot()['jobs'][0]['can_next']


def test_global_cancel_disables_all_lanes():
    event = threading.Event(); hub = NavigationHub(); hub.begin('1' * 32, cancel_event=event)
    for identity in ('maintenance','software_updates','driver_updates'):
        hub.cursor(identity).begin_item('one','Live')
    event.set()
    assert all(not row['active'] and not row['can_cancel'] and not row['can_next'] for row in hub.snapshot()['jobs'])


def test_app_search_skip_discards_results_without_installer():
    service = app_backend(); hub = owned(service)
    original = service.updates_scan.return_value
    service.updates_scan.side_effect = lambda: (issue(hub, 'software_updates', 'next'), original)[1]
    result = run_software(service, configuration())
    assert result['navigation_cancelled'] and result['installer_started'] is False
    service.update_app_unattended.assert_not_called()
    assert installer_barrier(result) is None


def test_presence_and_update_share_one_reentrant_installer_slot(tmp_path, monkeypatch):
    service = app_backend(); service.data_dir = tmp_path; service.bridge = SimpleNamespace(_process=None)
    service.system = 'win32'; service.update_app_unattended.return_value = success()
    hub = owned(service)
    calls = []
    def presence(backend, settings, before_install=None, **kwargs):
        calls.append('presence')
        assert before_install({'candidate_count': 1, 'review_only': False, 'scan_complete': True})
        return {'items': [], 'failed_items': 0, 'installer_started': True, 'outcomeKnown': True, 'running': False}
    monkeypatch.setattr('optimus.app_presence.run_app_presence', presence)
    config = configuration(software_app_rules=[{'id':'Vendor.One','source':'winget','update_policy':'follow_global','presence':'keep_installed'}])
    def operation(worker, item_callback, prepared_callback, before_install):
        return run_software(worker, config, item_callback=item_callback, prepared_callback=prepared_callback, before_install=before_install)
    queue = ParallelInstallerQueue(service, '1' * 32, [(WINDOWS_SOFTWARE, operation)], ProgressMux(lambda _: None))
    queue.maintenance_ready = True
    queue.start()
    try:
        queue.jobs[0].thread.join(3)
        assert not queue.jobs[0].thread.is_alive(), 'Presence/update gate reentry deadlocked.'
        assert calls == ['presence'] and service.update_app_unattended.call_count == 2
        assert queue.join()[0]['result']['app_presence']['installer_started'] is True
        assert len(queue.jobs[0].record['terminal_items']) == 2
    finally:
        queue.cancel()


def test_native_casefold_rules_and_exact_next_skip():
    service = app_backend(package('apt:One'), package('apt:Two'))
    service.system = 'linux'; service.updates_scan.return_value['complete'] = True
    service.update_app_unattended.return_value = success()
    hub = owned(service)
    config = configuration(setup_version=2, software_update_policy='selected', software_update_ids=['APT:ONE','apt:two'])
    result = run_native_software(service, config, item_callback=lambda state: issue(hub, 'software_updates', 'next') if state['id'] == 'apt:One' else None)
    assert [call.args[0] for call in service.update_app_unattended.call_args_list] == ['apt:Two']
    assert result['navigation_cancelled_items'] == 1 and not result['cancelled']
    assert installer_barrier(result) is None


def test_driver_targeted_backup_blocks_only_unbacked_offer():
    service = driver_backend(); hub = owned(service)
    first, second = driver(), driver(id='00000000-0000-0000-0000-000000000002')
    service.driver_updates_scan.return_value['updates'] = [first, second]
    service.driver_backup_for_updates = Mock(return_value={'exit_code':0,'destination':'private-fixture',
        'backed_updates':[{'id':second['id'],'revision':1}],
        'blocked_updates':[{'id':first['id'],'revision':1,'reason':'Fixture missing exact OEM package'}]})
    service.driver_update_apply_unattended.return_value.update(installer_started=True,outcomeKnown=True,running=False)
    result = run_drivers(service, driver_config(driver_approved_updates=[first, second]))
    service.driver_backup_automatic.assert_not_called()
    assert [call.args[0]['id'] for call in service.driver_update_apply_unattended.call_args_list] == [second['id']]
    assert result['items'][0]['reason'] == 'Fixture missing exact OEM package'
    assert result['outcomeKnown'] is True


def test_driver_next_skips_one_offer_and_retains_real_second_outcome():
    service = driver_backend(); hub = owned(service)
    first, second = driver(), driver(id='00000000-0000-0000-0000-000000000002')
    service.driver_updates_scan.return_value['updates'] = [first, second]
    service.driver_update_apply_unattended.return_value.update(installer_started=True,outcomeKnown=True,running=False)
    result = run_drivers(service, driver_config(driver_approved_updates=[first,second]),
        item_callback=lambda state: issue(hub,'driver_updates','next') if state['binding']['id'] == first['id'] else None)
    assert [call.args[0]['id'] for call in service.driver_update_apply_unattended.call_args_list] == [second['id']]
    assert result['navigation_cancelled_items'] == 1 and not result['cancelled']
    assert installer_barrier(result) is None and result['outcomeKnown'] is True
    assert _driver_completion_counts(result,result['driver_scan'])['skipped'] == 1


def test_driver_cancel_marker_cannot_bypass_unknown_native_receipt():
    service = driver_backend(); owned(service)
    first, second = driver(), driver(id='00000000-0000-0000-0000-000000000002')
    service.driver_updates_scan.return_value['updates'] = [first, second]
    service.driver_update_apply_unattended.return_value.update(navigation_cancelled=True,
        installer_started=True,outcomeKnown=False,running=True)
    result = run_drivers(service, driver_config(driver_approved_updates=[first,second]))
    assert service.driver_update_apply_unattended.call_count == 1 and installer_barrier(result)


def test_driver_failed_targeted_backup_never_uses_legacy_or_installs():
    service = driver_backend(); owned(service)
    first = driver()
    service.driver_backup_for_updates = Mock(return_value={'exit_code':1,'destination':'private-fixture',
        'error':'Fixture export timeout300s', 'backed_updates':[{'id':first['id'],'revision':1}], 'blocked_updates':[]})
    result = run_drivers(service, driver_config())
    service.driver_backup_automatic.assert_not_called(); service.driver_update_apply_unattended.assert_not_called()
    assert 'Fixture export timeout' in _journal_result(result)['error']


def test_backup_progress_is_not_driver_installer_intent(tmp_path):
    service = driver_backend(); service.data_dir = tmp_path; owned(service)
    job = ParallelAppUpdates(service,'1'*32,WINDOWS_DRIVERS,lambda *_:None,ProgressMux(lambda _:None))
    job.start(inline=True)
    job.item_progress({'phase':'backup','description':'Export exact OEM package','current':0,'total':1})
    assert job.record['installer_prepared'] is False
    job.item_progress({'description':'Install exact reviewed offer','binding':driver(),'current':1,'total':1})
    assert job.record['installer_prepared'] is True


def test_cancelled_export_is_not_an_unresolved_driver_installer():
    service = driver_backend(); hub = owned(service)
    def backup(*args, **kwargs):
        issue(hub,'driver_updates','next')
        return {'exit_code':1,'destination':'private-fixture','backed_updates':[], 'blocked_updates':[],
                'packages':[{'export_result':{'cancelled':True,'timed_out':False,'exit_code':1}}]}
    service.driver_backup_for_updates = backup
    result = run_drivers(service,driver_config())
    assert result['navigation_cancelled'] and result['installer_started'] is False
    assert installer_barrier(result) is None and _uncertain(result) is False
    service.driver_update_apply_unattended.assert_not_called()


def test_presence_completed_install_and_removal_remain_visible_with_zero_update_offers():
    value = {'items': [], 'app_scan': {'complete':True,'offers_count':0,'summary':'No update offers.'},
        'app_presence': {'items': [
            {'id':'Vendor.One','source':'winget','presence':'keep_installed','status':'completed','result':success()},
            {'id':'Vendor.Two','source':'winget','presence':'keep_uninstalled','status':'completed','result':success()},
            {'id':'Vendor.Unknown','presence':'keep_installed','status':'completed','result':{'exitCode':0}}]}}
    state = update_completion_state(value)
    assert state['scan_label'] == '1 installed / 1 removed / 0 updates completed'
    assert state['presence_counts']['unverified'] == 1 and 'Application rules:' in state['description']
    saved = _journal_result(value)
    assert saved['app_presence']['items'][0]['presence'] == 'keep_installed'
    assert saved['app_presence']['items'][0]['result']['verified'] is True
    assert update_completion_state(saved)['presence_counts'] == state['presence_counts']


def test_presence_receipts_survive_later_update_search_exception(monkeypatch):
    service = app_backend(); owned(service)
    receipt = {'items':[{'id':'Vendor.One','presence':'keep_installed','status':'completed','result':success()}],
        'failed_items':0,'installer_started':True,'outcomeKnown':True,'running':False}
    monkeypatch.setattr('optimus.app_presence.run_app_presence', lambda *_args, **_kwargs: receipt)
    service.updates_scan.side_effect = RuntimeError('Fixture later discovery failure')
    config = configuration(software_app_rules=[{'id':'Vendor.One','source':'winget','update_policy':'follow_global','presence':'keep_installed'}])
    with pytest.raises(RuntimeError) as caught:
        run_software(service, config)
    assert caught.value.app_presence == receipt


@pytest.mark.parametrize('native', [False, True])
def test_known_package_failure_advances_to_next_app(native):
    service = app_backend(); owned(service); service.system = 'linux' if native else 'win32'
    service.updates_scan.return_value['complete'] = True
    service.update_app_unattended.side_effect = [
        {'exitCode':1603,'verified':False,'installer_started':True,'outcomeKnown':True,'running':False,
         'requiresReview':True,'note':'Fixture package failed, native installer exited.'}, success()]
    result = (run_native_software if native else run_software)(service,configuration(setup_version=2))
    assert service.update_app_unattended.call_count == 2
    assert [row['status'] for row in result['items']] == ['failed','completed']
    assert result['failed_items'] >= 1 and not result['deferred_ids']
    assert installer_barrier(result) is None


@pytest.mark.parametrize('failure', [
    {'resultCode':4,'hresult':0x8024200B,'installer_started':True,'outcomeKnown':True,'running':False,'requiresReview':True},
    {'resultCode':4,'hresult':0x8024200A,'installer_started':False,'outcomeKnown':True,'running':False,
     'requiresReview':True,'failureKind':'quiet_mode_unavailable'},
])
def test_known_terminal_driver_failure_advances(failure):
    service = driver_backend(); owned(service)
    first, second = driver(), driver(id='00000000-0000-0000-0000-000000000002')
    service.driver_updates_scan.return_value['updates'] = [first,second]
    service.driver_update_apply_unattended.side_effect = [failure,
        {'resultCode':2,'hresult':0,'installer_started':True,'outcomeKnown':True,'running':False}]
    result = run_drivers(service,driver_config(driver_approved_updates=[first,second]))
    assert service.driver_update_apply_unattended.call_count == 2
    assert [row['status'] for row in result['items']] == ['failed','applied']
    assert result['failed_items'] == 1 and result['outcomeKnown'] is True
    assert installer_barrier(result) is None


def test_driver_shared_installer_conflict_blocks_later_installs_with_native_reason():
    service = driver_backend(); owned(service)
    first, second = driver(), driver(id='00000000-0000-0000-0000-000000000002')
    service.driver_updates_scan.return_value['updates'] = [first,second]
    service.driver_update_apply_unattended.return_value = {'resultCode':4,'hresult':0x80240016,
        'installer_started':True,'outcomeKnown':True,'running':False,'requiresReview':True,
        'note':'Another installation or mandatory restart prevents driver installation.'}
    result = run_drivers(service,driver_config(driver_approved_updates=[first,second]))
    assert service.driver_update_apply_unattended.call_count == 1
    assert installer_barrier(result) == 'Another installation or mandatory restart prevents driver installation.'
    assert result['deferred'] == [second]


def test_known_package_local_preflight_advances_but_shared_disk_stops():
    for failure, count in [({'failureKind':'unsupported_installer'}, 2), ({'failureKind':'insufficient_disk'},1)]:
        service = app_backend(); owned(service)
        service.update_app_unattended.side_effect = [dict(exitCode=None,skipped=True,requiresReview=True,
            installer_started=False,outcomeKnown=True,running=False,note='Fixture explicit refusal',**failure), success()]
        result = run_software(service,configuration())
        assert service.update_app_unattended.call_count == count
        assert bool(installer_barrier(result)) is (count == 1)


def test_maintenance_next_before_stage_starts_skips_it_and_continues(tmp_path):
    service = MockBackend(tmp_path); workflow = Workflow(service); service.workflow = workflow
    def progress(state):
        if state.get('stage') == 'Inspect current resource usage' and state.get('stage_status') == 'running':
            issue(workflow.navigation, 'maintenance', 'next')
    report = workflow.run(dict(DEFAULTS, applications_review_enabled=True), progress)
    assert report['stages'][0]['status'] == 'cancelled' and not report['cancelled']
    assert ('installed_apps',) in service.calls
    assert report['status'] == 'completed_with_errors' and report['cancelled_stages'] == 1
    assert workflow.navigation.snapshot()['active'] is False


def test_readonly_winget_search_cancellation_waits_for_quiescence(monkeypatch):
    event = threading.Event(); event.set()
    child = Mock(); monkeypatch.setattr('optimus.maintenance.subprocess.Popen', child)
    service = Maintenance.__new__(Maintenance); service._winget = lambda: 'fixture-winget'
    result = service._read_only_update_search(event)
    child.assert_not_called(); assert result.returncode == 130
    event.clear(); process = Mock(); process.returncode = 0
    def communicate(timeout):
        if process.communicate.call_count == 1:
            event.set(); raise subprocess.TimeoutExpired('fixture', timeout)
        assert process.terminate.called
        return ('', '')
    process.communicate.side_effect = communicate; child.return_value = process
    result = service._read_only_update_search(event)
    assert result.returncode == 130 and process.terminate.call_count == 1
    args = child.call_args.args[0]
    assert args[1:] == ['upgrade','--source','winget','--disable-interactivity']
    assert '--id' not in args and '--all' not in args
