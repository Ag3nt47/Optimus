from types import SimpleNamespace
import threading

import pytest

from optimus.extended_backend import ExtendedMaintenanceBackend
from optimus.workflow import Preferences


class FixtureBackend(ExtendedMaintenanceBackend):
    def __init__(self, path):
        self.data_dir = path
        self.saved = Preferences.safe_defaults()

    def preferences(self):
        return dict(self.saved)

    def save_preferences(self, settings):
        self.saved = Preferences.validate(settings)
        return self.preferences()


def test_cached_poll_and_stop_do_not_construct_services(tmp_path):
    backend = FixtureBackend(tmp_path)
    assert backend.worker_status() == []
    assert backend.smart_maintenance_status()['running'] is False
    assert backend.request_optional_stop() == []
    assert '_smart_maintenance' not in backend.__dict__
    assert list(tmp_path.iterdir()) == []


def test_smart_patch_preserves_existing_choices_without_starting(tmp_path):
    backend = FixtureBackend(tmp_path)
    backend.saved.update(smart_cleaning_threshold_bytes=123000000,
                         browser_cleanup_browsers=['firefox'], browser_keep_domains=['example.com'])
    result = backend.configure_smart_maintenance({'smart_cleaning_enabled': True})
    assert result['running'] is False
    assert backend.saved['smart_cleaning_threshold_bytes'] == 123000000
    assert backend.saved['browser_cleanup_browsers'] == ['firefox']
    assert backend.saved['browser_keep_domains'] == ['example.com']
    assert list(tmp_path.iterdir()) == []
    with pytest.raises(ValueError):
        backend.configure_smart_maintenance({'consumer_debloat_enabled': True})


def test_restore_wrappers_match_nonprogress_service_contract(tmp_path):
    backend = FixtureBackend(tmp_path)
    calls = []
    def create(description, confirmed=False, cancel_event=None):
        calls.append((description, confirmed, cancel_event.is_set()))
        return {'status': 'completed', 'reason': 'Fixture point verified.'}
    backend._system_restore = SimpleNamespace(create=create)
    assert backend.system_restore_create('Fixture', confirmed=True)['status'] == 'completed'
    assert calls == [('Fixture', True, False)]
    assert backend.worker_status()[0]['status'] == 'completed'


def test_optional_stop_reaches_entered_worker_and_does_not_claim_success(tmp_path):
    backend = FixtureBackend(tmp_path)
    entered, release = threading.Event(), threading.Event()
    observed = []
    def operation(*, cancel_event, progress_callback):
        entered.set()
        release.wait(5)
        observed.append(cancel_event.is_set())
        return {'status': 'cancelled'}
    worker = threading.Thread(target=lambda: backend._tool_operation('fixture', 'Fixture worker', operation))
    worker.start()
    try:
        assert entered.wait(5)
        assert backend.request_optional_stop() == ['fixture']
        assert backend.worker_status()[0]['status'] == 'stopping'
    finally:
        release.set()
        worker.join(5)
    assert not worker.is_alive() and observed == [True]
    assert backend.worker_status()[0]['status'] == 'cancelled'


def test_recovery_unknown_receipts_block_shared_guard(tmp_path):
    from optimus.parallel_updates import pending_updates_barrier
    backend = FixtureBackend(tmp_path)
    directory = tmp_path / 'recovery' / 'driver-backups'
    directory.mkdir(parents=True)
    backend._driver_recovery = SimpleNamespace(pending_barrier=lambda: 'Unknown rollback outcome.')
    assert pending_updates_barrier(backend) == 'Unknown rollback outcome.'
    directory.rmdir()
    (tmp_path / 'recovery' / 'system-restore').mkdir()
    def interrupted():
        raise ValueError('Interrupted restore operation.')
    backend._system_restore = SimpleNamespace(_pending=interrupted)
    assert 'Interrupted restore operation.' in pending_updates_barrier(backend)


def test_invalid_progress_never_changes_terminal_state():
    from optimus.worker_status import WorkerStatus
    registry = WorkerStatus()
    token = registry.begin('fixture', 'Fixture')
    with pytest.raises(ValueError):
        registry.update('fixture', token, status='completed', current=3, total=2)
    assert registry.snapshot()[0]['status'] == 'queued'
