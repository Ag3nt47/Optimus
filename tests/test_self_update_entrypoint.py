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
