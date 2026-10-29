"""Internal updater dispatch precedes backend/GUI/UAC; fixture dispatch only."""
from unittest.mock import Mock
from types import SimpleNamespace

import pytest

from optimus import app


@pytest.mark.parametrize('outcome,expected', [(0, 0), (2, 2), (True, 2), (False, 2),
    (None, 2), ({'status': 'launched'}, 2)])
def test_internal_worker_never_constructs_backend_gui_or_requests_admin(monkeypatch, outcome, expected):
    import optimus.backend
    import optimus.self_update
    worker = Mock(return_value=outcome)
    monkeypatch.setattr(optimus.self_update, 'worker_main', worker)
    backend = Mock(side_effect=AssertionError('Internal worker must not construct the normal backend.'))
    gui = Mock(side_effect=AssertionError('Internal worker must not claim/show the GUI.'))
    monkeypatch.setattr(optimus.backend, 'Backend', backend)
    monkeypatch.setattr(app, '_run_gui', gui)
    assert app.main(['--self-update-worker', 'a' * 32]) == expected
    worker.assert_called_once_with('a' * 32)
    backend.assert_not_called()
    gui.assert_not_called()


def test_internal_worker_id_is_exact_and_exclusive_with_probe():
    for arguments in (['--self-update-worker', '../bad'], ['--self-update-worker', 'a' * 32, '--smoke-test'],
                      ['--self-update-resume', 'a' * 32, '--smoke-test'],
                      ['--self-update-worker', 'a' * 32, '--self-update-resume', 'b' * 32]):
        with pytest.raises(SystemExit):
            app.parser().parse_args(arguments)


def test_resume_is_a_bound_transaction_argument_not_a_maintenance_approval():
    arguments = app.parser().parse_args(['--self-update-resume', 'a' * 32])
    assert arguments.self_update_resume == 'a' * 32
    assert not arguments.self_update_worker
    with pytest.raises(SystemExit):
        app.parser().parse_args(['--self-update-resume', 'not-a-transaction'])


def test_resume_is_not_consumed_by_regular_parent_before_normal_uac_handoff(monkeypatch, tmp_path):
    import optimus.backend
    import optimus.self_update
    backend = SimpleNamespace(data_dir=tmp_path, close=Mock())
    monkeypatch.setattr(optimus.backend, 'Backend', lambda: backend)
    monkeypatch.setattr(app.sys, 'platform', 'win32')
    lease = Mock()
    lease.try_claim.return_value = True
    monkeypatch.setattr('optimus.gui_instance.GuiInstance', lambda path: lease)
    request = Mock(return_value={'started': True})
    monkeypatch.setattr('optimus.elevation.startup_administrator', request)
    core = Mock(side_effect=AssertionError('Resume cannot be consumed in the UAC parent that exits.'))
    monkeypatch.setattr(optimus.self_update, 'SelfUpdater', core)
    gui = Mock()
    monkeypatch.setattr(app, '_show_gui', gui)
    arguments = ['--self-update-resume', 'a' * 32]
    assert app.main(arguments) == 0
    assert request.call_args.args[2] == arguments
    core.assert_not_called()
    gui.assert_not_called()
    backend.close.assert_called_once_with()


def test_update_resume_waits_for_previous_gui_lease_without_activating_old_version(monkeypatch, tmp_path):
    """The transaction argument reaches the new GUI after a short lock race."""
    events = []
    backend = SimpleNamespace(data_dir=tmp_path, close=Mock(), preferences=lambda: {})
    lease = Mock()
    lease.try_claim.side_effect = lambda: events.append('claim') or False
    lease.wait_for_handoff.side_effect = lambda: events.append('wait') or True
    monkeypatch.setattr(app.sys, 'platform', 'win32')
    monkeypatch.setattr('optimus.gui_instance.GuiInstance', lambda path: lease)
    elevation = Mock(side_effect=lambda *args: events.append('authorization') or {'started': False})
    monkeypatch.setattr('optimus.elevation.startup_administrator', elevation)
    priority = Mock()
    priority.apply.return_value = {'status': 'applied'}
    monkeypatch.setattr('optimus.runtime_priority.InterfacePriority', lambda: priority)
    gui = Mock(side_effect=lambda *args: events.append('gui') or 0)
    monkeypatch.setattr(app, '_show_gui', gui)
    arguments = ['--self-update-resume', 'a' * 32]
    parsed = app.parser().parse_args(arguments)
    assert app._run_gui(backend, parsed, arguments) == 0
    assert events == ['claim', 'wait', 'authorization', 'gui']
    lease.wait_for_handoff.assert_called_once_with()
    lease.activate_existing.assert_not_called()
    assert gui.call_args.args[1].self_update_resume == 'a' * 32
    assert gui.call_args.args[3] is lease
    backend.close.assert_called_once_with()
    lease.close.assert_called_once_with()


@pytest.mark.parametrize('admin_relaunch', [False, True])
def test_update_resume_lock_timeout_never_drops_transaction_into_old_gui(monkeypatch, tmp_path, admin_relaunch):
    backend = SimpleNamespace(data_dir=tmp_path, close=Mock())
    lease = Mock()
    lease.try_claim.return_value = False
    lease.wait_for_handoff.return_value = False
    monkeypatch.setattr(app.sys, 'platform', 'win32')
    monkeypatch.setattr('optimus.gui_instance.GuiInstance', lambda path: lease)
    elevation = Mock(side_effect=AssertionError('No authorization before claiming the GUI lease.'))
    monkeypatch.setattr('optimus.elevation.startup_administrator', elevation)
    gui = Mock(side_effect=AssertionError('No GUI or cleanup before claiming the lease.'))
    monkeypatch.setattr(app, '_show_gui', gui)
    arguments = ['--self-update-resume', 'a' * 32] + (['--admin-relaunch'] if admin_relaunch else [])
    assert app._run_gui(backend, app.parser().parse_args(arguments), arguments) == 2
    lease.wait_for_handoff.assert_called_once_with()
    lease.activate_existing.assert_not_called()
    elevation.assert_not_called()
    gui.assert_not_called()
    backend.close.assert_called_once_with()
    lease.close.assert_called_once_with()


@pytest.mark.parametrize('is_admin,expected', [(False, 'not_elevated'), (True, 'already_administrator')])
def test_update_restart_preserves_inherited_admin_context_without_another_uac_request(monkeypatch, is_admin, expected):
    from optimus import elevation
    backend = SimpleNamespace(is_admin=Mock(return_value=is_admin),
        preferences=Mock(return_value={'request_admin_on_startup': True}))
    request = Mock(side_effect=AssertionError('The update restart must not request a second UAC handoff.'))
    monkeypatch.setattr(elevation, 'request_administrator', request)
    arguments = ['--self-update-resume', 'a' * 32, '--admin-relaunch']
    result = elevation.startup_administrator(backend, app.parser().parse_args(arguments), arguments)
    assert result['status'] == expected and result['started'] is False
    backend.is_admin.assert_called_once_with()
    backend.preferences.assert_not_called()
    request.assert_not_called()
    if not is_admin:
        assert 'standard access' in result['note'] and 'protected actions' in result['note']
