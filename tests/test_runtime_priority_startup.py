"""GUI priority ownership is mocked; diagnostics never request a boost."""
from types import SimpleNamespace
from unittest.mock import Mock
import pytest
from optimus import app
from optimus import runtime_priority


@pytest.fixture
def session(tmp_path, monkeypatch):
    backend = SimpleNamespace(data_dir=tmp_path, close=Mock(),
                              preferences=Mock(return_value={'interface_priority_boost_enabled': True}))
    lease = Mock()
    lease.try_claim.return_value = True
    monkeypatch.setattr('optimus.gui_instance.GuiInstance', Mock(return_value=lease))
    monkeypatch.setattr('optimus.elevation.startup_administrator', Mock(return_value={'started': False}))
    controller = Mock()
    controller.apply.return_value = {'status': 'applied'}
    factory = Mock(return_value=controller)
    monkeypatch.setattr(runtime_priority, 'InterfacePriority', factory)
    gui = Mock(return_value=0)
    monkeypatch.setattr(app, '_show_gui', gui)
    return backend, lease, controller, factory, gui


def test_normal_gui_applies_saved_boost_and_restores_on_exit(session):
    backend, lease, controller, factory, gui = session
    assert app._run_gui(backend, app.parser().parse_args([]), []) == 0
    factory.assert_called_once_with()
    controller.apply.assert_called_once_with(True)
    controller.restore.assert_called_once_with()
    backend.close.assert_called_once_with()
    lease.close.assert_called_once_with()


def test_disabled_choice_is_retained(session):
    backend, _, controller, _, _ = session
    backend.preferences.return_value['interface_priority_boost_enabled'] = False
    assert app._run_gui(backend, app.parser().parse_args([]), []) == 0
    controller.apply.assert_called_once_with(False)


@pytest.mark.parametrize('option', ['--startup-probe', '--smoke-test'])
def test_diagnostic_gui_never_constructs_priority_session(session, option):
    backend, _, _, factory, _ = session
    assert app._run_gui(backend, app.parser().parse_args([option]), [option]) == 0
    factory.assert_not_called()
    backend.preferences.assert_not_called()


def test_failed_gui_restores_priority_and_closes_backend(session):
    backend, _, controller, _, gui = session
    gui.side_effect = RuntimeError('Fixture GUI failed')
    assert app._run_gui(backend, app.parser().parse_args([]), []) == 2
    controller.restore.assert_called_once_with()
    backend.close.assert_called_once_with()


def test_duplicate_activation_does_not_read_preferences_or_boost(session):
    backend, lease, _, factory, gui = session
    lease.try_claim.return_value = False
    lease.activate_existing.return_value = True
    assert app._run_gui(backend, app.parser().parse_args([]), []) == 0
    factory.assert_not_called()
    backend.preferences.assert_not_called()
    gui.assert_not_called()
