"""Run-local display inventories never replace a fresh mutation gate."""
import threading
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from optimus.backend import Backend
from optimus.run_review_inventory import MAX_ROWS, RunReviewInventory, invalidate_on_mutation


def snapshot(**changes):
    value = {'InstalledApps': [{'Identity': 'fixture', 'Name': 'Fixture', 'Version': '1'}],
             'Startup': [{'Identity': 'startup-fixture', 'Name': 'Fixture startup'}],
             'InventoryErrors': [], 'StartupInventoryComplete': True,
             'Processes': [{'pid': 999}], 'ToolVersion': 'fixture'}
    value.update(changes)
    return value


def backend_fixture():
    backend = Backend.__new__(Backend)
    backend.bridge = SimpleNamespace(request=Mock(side_effect=lambda operation, **_: {
        'audit': snapshot(), 'installed_apps': [{'Name': 'Fresh application'}],
        'startup_list': [{'Name': 'Fresh startup'}], 'startup_set': {'changed': True},
    }[operation]))
    return backend


def test_only_explicit_enabled_workflow_can_reuse_and_exit_discards_copies():
    backend = backend_fixture()
    backend.audit()
    assert backend.installed_apps_review() == [{'Name': 'Fresh application'}]
    backend.begin_review_inventory(enabled=False)
    backend.audit()
    assert backend.startup_list_review() == [{'Name': 'Fresh startup'}]
    backend.begin_review_inventory(enabled=True)
    backend.audit()
    assert backend.installed_apps_review() == snapshot()['InstalledApps']
    assert backend.startup_list_review() == snapshot()['Startup']
    assert backend.review_inventory_status()['reused_inventories'] == 2
    backend.end_review_inventory()
    assert backend.installed_apps_review() == [{'Name': 'Fresh application'}]
    assert backend.review_inventory_status()['enabled'] is False


def test_repeated_review_avoids_inventory_calls_but_action_readers_stay_fresh():
    backend = backend_fixture()
    backend.begin_review_inventory()
    backend.audit()
    backend.bridge.request.reset_mock()
    for _ in range(3):
        assert backend.installed_apps_review() == snapshot()['InstalledApps']
        assert backend.startup_list_review() == snapshot()['Startup']
    backend.bridge.request.assert_not_called()
    assert backend.installed_apps() == [{'Name': 'Fresh application'}]
    assert backend.startup_list() == [{'Name': 'Fresh startup'}]
    assert backend.bridge.request.call_count == 2
    # This observation cache has no API for processes, security or update offers.
    assert backend._run_review_inventory.get('Processes') is None


def test_seed_and_get_are_detached_from_audit_and_other_callers():
    inventory = RunReviewInventory()
    inventory.begin()
    audit = snapshot()
    inventory.seed(audit)
    audit['InstalledApps'][0]['Name'] = 'Changed audit'
    first = inventory.get('applications')
    first[0]['Version'] = 'Changed caller'
    assert inventory.get('applications') == snapshot()['InstalledApps']


@pytest.mark.parametrize('changes,apps,startup', [
    ({'InventoryErrors': ['Unavailable scheduled task']}, False, False),
    ({'InventoryErrors': None}, False, False),
    ({'InventoryErrors': ''}, False, False),
    ({'InventoryErrors': False}, False, False),
    ({'StartupInventoryComplete': False}, True, False),
    ({'StartupInventoryComplete': 1}, True, False),
    ({'InstalledApps': None}, False, True),
    ({'Startup': {}}, True, False),
])
def test_incomplete_or_ambiguous_sections_are_not_reused(changes, apps, startup):
    inventory = RunReviewInventory()
    inventory.begin()
    inventory.seed(snapshot(**changes))
    assert (inventory.get('applications') is not None) is apps
    assert (inventory.get('startup') is not None) is startup


@pytest.mark.parametrize('rows', [
    [{'Name': 'x'}] * (MAX_ROWS + 1),
    [{'Name': 'x' * 65537}],
    [{'Name': 'x' * 60000, 'other': str(index)} for index in range(36)],
    [{'Name': '\U0001f600' * 60000} for _ in range(10)],
    [{'Name': float('nan')}], [{'Name': float('inf')}], [{'Name': object()}],
    [{1: 'Non-string key'}], [{'Name': (1, 2)}], [None], [{'Name': '\ud800'}],
])
def test_oversized_or_non_json_producer_data_is_not_reused(rows):
    inventory = RunReviewInventory()
    inventory.begin()
    inventory.seed(snapshot(InstalledApps=rows))
    assert inventory.get('applications') is None


@pytest.mark.parametrize('elapsed', [30, 300, -1])
def test_expired_or_reversed_clock_never_reuses_earlier_data(elapsed):
    now = [100.0]
    inventory = RunReviewInventory(clock=lambda: now[0])
    inventory.begin()
    inventory.seed(snapshot())
    now[0] += elapsed
    assert inventory.get('applications') is None
    # A later final audit cannot repopulate the first-audit cache.
    inventory.seed(snapshot())
    assert inventory.get('startup') is None


def test_invalidation_before_seed_or_after_seed_cannot_be_repopulated():
    inventory = RunReviewInventory()
    for seed_first in (False, True):
        inventory.begin()
        if seed_first:
            inventory.seed(snapshot())
        inventory.invalidate()
        inventory.seed(snapshot())
        assert inventory.get('applications') is None
        assert inventory.get('startup') is None
    inventory.begin()
    inventory.seed(snapshot())
    assert inventory.get('applications') is not None


def test_mutator_invalidates_before_operation_after_failure_and_keeps_fresh_validation():
    backend = backend_fixture()
    backend.begin_review_inventory()
    backend.audit()
    original = backend.bridge.request.side_effect

    def request(operation, **arguments):
        if operation == 'startup_set':
            assert backend._run_review_inventory.get('startup') is None
            backend.audit()  # A final/background audit during the call cannot reseed.
            raise OSError('Fixture refused')
        return original(operation, **arguments)

    backend.bridge.request.side_effect = request
    with pytest.raises(OSError, match='Fixture refused'):
        backend.startup_set({'Name': 'Fixture'}, False)
    assert backend.startup_list_review() == [{'Name': 'Fresh startup'}]


def test_mutation_decorator_preserves_signature_and_shared_background_instance():
    import inspect
    from optimus.parallel_updates import isolated_backend
    backend = backend_fixture()
    backend.begin_review_inventory()
    backend.audit()
    worker = isolated_backend(backend)
    assert worker._run_review_inventory is backend._run_review_inventory
    entered, finish = threading.Event(), threading.Event()

    @invalidate_on_mutation
    def mutate(owner, record, *, accepted=False):
        entered.set()
        assert finish.wait(2)
        return accepted

    assert list(inspect.signature(mutate).parameters) == ['owner', 'record', 'accepted']
    thread = threading.Thread(target=mutate, args=(worker, {}))
    thread.start()
    try:
        assert entered.wait(2)
        assert backend.installed_apps_review() == [{'Name': 'Fresh application'}]
        backend.audit()
        assert backend._run_review_inventory.get('applications') is None
    finally:
        finish.set()
        thread.join(2)
    assert not thread.is_alive()


@pytest.mark.parametrize('stamp', [0, 70.0, 100.1, float('nan'), True])
def test_slow_audit_or_invalid_capture_age_does_not_extend_the_review_ttl(stamp):
    inventory = RunReviewInventory(clock=lambda: 100.0)
    inventory.begin()
    inventory.seed(snapshot(), captured_at=stamp)
    assert inventory.get('applications') is None


def test_uninstall_selection_does_not_trust_reviewed_application(monkeypatch):
    backend = backend_fixture()
    backend.begin_review_inventory()
    backend.audit()
    opened = Mock()
    monkeypatch.setattr('optimus.backend.os.startfile', opened, raising=False)
    with pytest.raises(ValueError, match='selection changed'):
        backend.uninstall({'Identity': 'fixture', 'Name': 'Fixture'})
    opened.assert_not_called()
    assert any(call.args[0] == 'installed_apps' for call in backend.bridge.request.call_args_list)


def test_status_never_exposes_private_inventory_and_clear_is_memory_only(tmp_path):
    backend = backend_fixture()
    backend.data_dir = tmp_path
    backend.begin_review_inventory()
    backend.audit()
    assert 'Fixture' not in str(backend.review_inventory_status())
    backend.invalidate_review_inventory()
    backend.end_review_inventory()
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize('reuse,window,expected', [(True, 6, 6), (True, 2, 2), (False, 6, 0)])
def test_automatic_defender_wrapper_passes_validated_reuse_window_without_affecting_manual(reuse, window, expected):
    from optimus.workflow import DEFAULTS, Preferences
    backend = backend_fixture()
    privacy = SimpleNamespace(scan_spyware=Mock(return_value={'status': 'fixture'}))
    backend._privacy_service = Mock(return_value=privacy)
    configuration = Preferences.validate(dict(DEFAULTS, defender_scan_reuse_enabled=reuse,
        defender_scan_reuse_hours=window))
    cancel, progress = threading.Event(), Mock()
    assert backend.scan_spyware_automatic(configuration, cancel_event=cancel,
        progress_callback=progress) == {'status': 'fixture'}
    privacy.scan_spyware.assert_called_once_with(reuse_hours=expected,
        cancel_event=cancel, progress_callback=progress)
    privacy.scan_spyware.reset_mock()
    backend.scan_spyware(cancel_event=cancel, progress_callback=progress)
    privacy.scan_spyware.assert_called_once_with(cancel_event=cancel, progress_callback=progress)


def test_automatic_defender_wrapper_validates_before_any_provider_call():
    backend = backend_fixture()
    backend._privacy_service = Mock(side_effect=AssertionError('Invalid configuration must not query Defender.'))
    with pytest.raises(ValueError):
        backend.scan_spyware_automatic({'defender_scan_reuse_hours': True})
    backend._privacy_service.assert_not_called()


@pytest.mark.parametrize('reentry', [False, True])
def test_parallel_installer_invalidates_reviews_before_both_successful_slot_paths(reentry):
    from optimus.parallel_updates import ParallelInstallerQueue
    backend = backend_fixture()
    backend.preferences = lambda: {'toolkit_settings': {'adaptive': {'enabled': False}}}
    backend.begin_review_inventory()
    backend.audit()
    queue = ParallelInstallerQueue.__new__(ParallelInstallerQueue)
    queue.backend = backend
    queue.condition = threading.Condition(threading.RLock())
    queue.barrier, queue.maintenance_ready = None, True
    job = SimpleNamespace(prepared=threading.Event(), cancel_event=threading.Event(), navigation=None,
        will_install=False, result=None, mux=Mock(), gate_refusal=None)
    queue.active_job = job if reentry else None
    queue.jobs = [job]
    assert backend._run_review_inventory.get('applications') is not None
    assert queue._before_install(job, {'candidate_count': 1, 'review_only': False}) is True
    assert queue.active_job is job
    assert backend._run_review_inventory.get('applications') is None
    backend.audit()
    assert backend._run_review_inventory.get('applications') is None
