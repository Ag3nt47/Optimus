from types import SimpleNamespace
from unittest.mock import Mock, patch
from optimus.backend import Backend


def test_low_space_defers_before_vendor_installer():
    backend = object.__new__(Backend)
    backend.maintenance = SimpleNamespace(updates_apply=Mock())
    with patch('shutil.disk_usage', return_value=SimpleNamespace(free=256 * 1024 ** 2)):
        result = backend.update_app_unattended('Fixture.Package')
    assert result['skipped'] and result['requiresReview']
    assert result['disk_readiness']['ready'] is False
    backend.maintenance.updates_apply.assert_not_called()


def test_sufficient_space_reuses_exact_update_and_cancellation():
    backend = object.__new__(Backend)
    backend.maintenance = SimpleNamespace(updates_apply=Mock(return_value={'exitCode': 0}))
    event = object()
    with patch('shutil.disk_usage', return_value=SimpleNamespace(free=8 * 1024 ** 3)):
        result = backend.update_app_unattended('Fixture.Package', cancel_event=event)
    assert result == {'exitCode': 0}
    backend.maintenance.updates_apply.assert_called_once_with('Fixture.Package', confirmed=True, unattended=True, cancel_event=event)


def test_unknown_disk_status_defers():
    backend = object.__new__(Backend)
    with patch('shutil.disk_usage', side_effect=OSError('fixture')):
        result = backend.update_disk_readiness()
    assert result['ready'] is False
