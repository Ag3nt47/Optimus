"""Recovery UI exercises in-memory records; no host undo or scans."""
import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

from types import SimpleNamespace
from unittest.mock import Mock
import pytest
from PySide6.QtCore import QPoint, QRect, Qt
from PySide6.QtGui import QCloseEvent, QFont, QFontDatabase
from PySide6.QtWidgets import QApplication, QWidget

from optimus.widgets import revert_dialog as module
from optimus.ui import OptimusWindow
from optimus.native_ui import NativeOptimusWindow


class InlinePool:
    def start(self, worker):
        worker.run()


def inventory():
    return {'plan_id':'a'*32, 'complete':True, 'items':[
        {'selection_id':'b'*32,'record_id':'c'*32,'provider':'change','kind':'Startup',
         'target':'Fixture startup','status':'applied','undo_available':True,'reason':'Original restorer revalidates'},
        {'selection_id':'d'*32,'record_id':'e'*32,'provider':'history:runs','kind':'Deleted temporary files',
         'target':'Fixture cleanup receipt','status':'completed','undo_available':False,'reason':'No automatic undo'}]}


@pytest.fixture
def dialog(monkeypatch):
    app = QApplication.instance() or QApplication([])
    previous_font = app.font()
    # The Windows offscreen plugin lacks the normal desktop font database.
    # Register an already installed font so glyph measurements remain real.
    from pathlib import Path
    font_path = Path(os.environ.get('WINDIR', r'C:\Windows')) / 'Fonts' / 'segoeui.ttf'
    font_id = QFontDatabase.addApplicationFont(str(font_path)) if font_path.is_file() else -1
    if font_id >= 0:
        app.setFont(QFont('Segoe UI', 10))
    owner = QWidget()
    owner.backend = SimpleNamespace(change_history_scan=Mock(return_value=inventory()),
        change_history_revert=Mock(return_value={'status':'completed','restored':1,'note':'1 supported record restored.'}))
    owner.busy, owner.smoke_test, owner._close_when_idle = False, False, False
    owner.pool, owner.quit_application, owner.open_settings = InlinePool(), Mock(), Mock()
    value = module.RevertDialog(owner)
    prompts = []
    monkeypatch.setattr(module, 'ask_yes_no', lambda *args, **kwargs:prompts.append((args,kwargs)) or True)
    yield value, owner, prompts
    value.shutdown(); value._worker = None; owner.busy = False
    value.close(); owner.close(); value.deleteLater(); owner.deleteLater(); app.processEvents()
    app.setFont(previous_font)
    if font_id >= 0:
        QFontDatabase.removeApplicationFont(font_id)


def test_constructor_performs_no_backend_action_and_defaults_to_no_selection(dialog):
    value, owner, _ = dialog
    owner.backend.change_history_scan.assert_not_called(); owner.backend.change_history_revert.assert_not_called()
    assert not value.revert_all_button.isEnabled() and not value.revert_selected_button.isEnabled()
    assert value.close_button.isDefault() and value._selected() == []


def test_refresh_is_read_only_and_only_supported_records_can_be_selected(dialog):
    value, owner, _ = dialog
    value.refresh()
    assert value.table.rowCount() == 2 and value._selected() == [] and value.revert_all_button.isEnabled()
    assert not value.table.item(1,0).flags() & Qt.ItemIsUserCheckable
    value.select_all()
    assert [row['selection_id'] for row in value._selected()] == ['b'*32]
    value.clear_selection(); assert not value._selected()
    owner.backend.change_history_revert.assert_not_called()


@pytest.mark.parametrize('all_records', [False, True])
def test_confirmed_selected_or_all_restores_only_exact_available_ids(dialog, all_records):
    value, owner, prompts = dialog
    value.refresh()
    if all_records:
        value.revert_all()
    else:
        value.select_all(); value.revert_selected()
    call = owner.backend.change_history_revert.call_args
    assert call.args == ('a'*32, ['b'*32]) and call.kwargs['confirmed'] is True
    assert callable(call.kwargs['progress_callback']) and not call.kwargs['cancel_event'].is_set()
    summary = prompts[0][0][2]
    assert 'Fixture startup' in summary and 'newest first' in summary
    assert 'Saved maintenance settings can apply these changes again' in summary
    assert prompts[0][1]['no_label'].startswith('No')
    assert value._plan is None and not value.revert_all_button.isEnabled()
    assert not owner.busy and '1 supported record restored' in value.status.text()


def test_declining_confirmation_and_empty_selection_do_not_restore(dialog, monkeypatch):
    value, owner, _ = dialog
    value.refresh(); value.revert_selected()
    owner.backend.change_history_revert.assert_not_called()
    monkeypatch.setattr(module, 'ask_yes_no', lambda *args, **kwargs:False)
    value.revert_all()
    owner.backend.change_history_revert.assert_not_called()


def test_closing_active_operation_requests_cancel_and_waits_for_outcome(dialog):
    value, owner, _ = dialog
    owner.pool = Mock()
    value.refresh()
    assert owner.busy and value._worker is not None
    event = QCloseEvent(); value.closeEvent(event)
    assert not event.isAccepted() and value._cancel.is_set() and owner.busy
    assert 'Waiting for the current restorer' in value.status.text()
    value._result(inventory()); value._finished()
    assert not owner.busy and value._worker is None


def test_backend_error_disables_replay_and_never_claims_completed_undo(dialog):
    value, owner, _ = dialog
    value.refresh()
    owner.backend.change_history_revert.side_effect = OSError('fixture outcome unknown')
    value.revert_all()
    assert value._plan is None and not value.revert_all_button.isEnabled()
    assert 'did not complete' in value.status.text() and 'fixture outcome unknown' in value.status.text()


def test_settings_route_is_explicit_and_preserves_revert_warning(dialog):
    value, owner, _ = dialog
    value.adjust_settings()
    owner.open_settings.assert_called_once_with(page='Settings')
    owner.backend.change_history_revert.assert_not_called()


@pytest.mark.parametrize('size', [(480,400),(980,720)])
def test_dialog_fits_small_and_large_viewports_with_reachable_labelled_actions(dialog, size):
    value, _, _ = dialog
    app = QApplication.instance()
    value.setAttribute(Qt.WA_DontShowOnScreen,True)
    value.resize(*size); value.show(); app.processEvents(); value.resize(*size); app.processEvents()
    assert (value.width(),value.height()) == size
    for button in value._buttons:
        if button.isVisible():
            rect = QRect(button.mapTo(value,QPoint()),button.size())
            assert value.rect().contains(rect)
            assert button.fontMetrics().horizontalAdvance(button.text()) + 10 <= button.width()


@pytest.mark.parametrize('factory', [OptimusWindow,NativeOptimusWindow])
def test_main_revert_button_is_red_equal_width_and_visible_at_basic_window_size(tmp_path, factory):
    app = QApplication.instance() or QApplication([])
    backend = SimpleNamespace(data_dir=tmp_path,preferences=lambda:{'reduce_motion':True},
                              change_history_scan=Mock(),change_history_revert=Mock())
    owner = factory(backend,smoke_test=True)
    owner.setAttribute(Qt.WA_DontShowOnScreen,True)
    owner.resize(640,480); owner.show(); app.processEvents()
    try:
        assert [button.text() for button in owner.main_buttons][-1] == 'Revert'
        assert '#a42b3e' in owner.main_buttons[-1].styleSheet()
        assert len({button.width() for button in owner.main_buttons}) == 1
        for button in owner.main_buttons:
            rect = QRect(button.mapTo(owner.landing_scroll.viewport(),QPoint()),button.size())
            assert owner.landing_scroll.viewport().rect().contains(rect)
        owner.main_buttons[-1].click(); app.processEvents()
        assert owner._revert_dialog is not None
        backend.change_history_scan.assert_not_called(); backend.change_history_revert.assert_not_called()
    finally:
        if owner._revert_dialog:
            owner._revert_dialog.close()
        owner.shutdown();owner.close();owner.deleteLater();app.processEvents()
