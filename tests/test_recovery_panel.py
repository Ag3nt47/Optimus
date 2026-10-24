"""Recovery UI uses fake asynchronous backends; no host recovery work."""
import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

import pytest
from PySide6.QtCore import Qt
from PySide6.QtGui import QFontDatabase
from PySide6.QtWidgets import QApplication, QWidget, QVBoxLayout

from optimus.widgets import recovery_panel as module


class InlinePool:
    def start(self, worker):
        worker.run()


@pytest.fixture
def panel(monkeypatch, app):
    backend = SimpleNamespace(**{name:Mock(return_value={'status':'completed','summary':'Fixture result'}) for name in (
        'driver_recovery_scan','driver_backup_export','driver_backup_verify','driver_rollback',
        'system_restore_scan','system_restore_create','system_restore_delete','system_restore_open')})
    backend.driver_recovery_scan.return_value = {'status':'completed','plan_id':'a'*32,
        'devices':[{'selection_id':'b'*32,'name':'Fixture display','device_id':'FIXTURE\\DEVICE',
                    'rollback_eligible':True,'drivers':[{'version':'2.0'}]}],
        'backups':[{'id':'c'*32,'kind':'driver-export','status':'completed','summary':'Fixture export'}]}
    backend.system_restore_scan.return_value = {'status':'completed','plan_id':'d'*32,'points':[
        {'selection_id':'e'*32,'sequence_number':7,'description':'Fixture point','created_at':'20261020'}]}
    window = QWidget()
    window.backend, window.busy, window.smoke_test = backend, False, False
    window.pool, window._workers = InlinePool(), set()
    widget = module.RecoveryPanel(window)
    layout = QVBoxLayout(window); layout.addWidget(widget)
    prompts = []
    monkeypatch.setattr(module, 'ask_yes_no', lambda *a, **kw:prompts.append((a,kw)) or True)
    yield widget, window, backend, prompts
    widget.shutdown(); widget.close(); window.close()
    widget.deleteLater(); window.deleteLater(); app.processEvents()


@pytest.fixture(scope='module')
def app():
    application = QApplication.instance() or QApplication([])
    loaded = []
    # The Windows offscreen plugin may expose no system fonts. Geometry checks
    # need real glyph metrics rather than the missing-font placeholder boxes.
    if os.name == 'nt' and not QFontDatabase.families():
        for name in ('segoeui.ttf', 'seguisb.ttf'):
            font_path = Path(os.environ.get('SystemRoot', r'C:\Windows')) / 'Fonts' / name
            if font_path.is_file():
                identity = QFontDatabase.addApplicationFont(str(font_path))
                if identity >= 0:
                    loaded.append(identity)
    yield application
    for identity in loaded:
        QFontDatabase.removeApplicationFont(identity)


def test_constructor_runs_nothing_and_defaults_to_no_selected_recovery(panel):
    widget, _, backend, _ = panel
    assert all(not value.called for value in vars(backend).values())
    assert not widget.driver_rollback.isEnabled() and not widget.restore_open.isEnabled()
    assert not widget.backup_verify.isEnabled() and not widget.restore_delete.isEnabled()
    assert widget.details.isReadOnly()


def test_refresh_is_read_only_and_requires_explicit_device_choice(panel):
    widget, window, backend, prompts = panel
    widget.refresh_drivers()
    backend.driver_recovery_scan.assert_called_once_with()
    assert widget.device_choices.currentIndex() == -1 and not widget.driver_rollback.isEnabled()
    assert widget.backup_choices.currentIndex() == -1 and not prompts and not window.busy
    widget.device_choices.setCurrentIndex(0)
    assert widget.driver_rollback.isEnabled()


def test_export_and_verify_hooks_keep_confirmation_and_cancellation_contract(panel):
    widget, _, backend, prompts = panel
    backend.driver_backup_export.return_value = {'id':'c'*32,'kind':'driver-export','status':'completed','summary':'Exported'}
    widget.export_drivers()
    kwargs = backend.driver_backup_export.call_args.kwargs
    assert kwargs['confirmed'] is True and callable(kwargs['progress_callback']) and not kwargs['cancel_event'].is_set()
    assert prompts and widget.backup_verify.isEnabled()
    widget.verify_backup()
    assert backend.driver_backup_verify.call_args.args == ('c'*32,)


def test_rollback_passes_only_opaque_exact_selection_after_concrete_confirmation(panel):
    widget, _, backend, prompts = panel
    widget.refresh_drivers(); widget.device_choices.setCurrentIndex(0)
    widget.rollback_selected()
    assert backend.driver_rollback.call_args.args == ('a'*32, 'b'*32)
    assert backend.driver_rollback.call_args.kwargs['confirmed'] is True
    assert 'FIXTURE\\DEVICE' in prompts[-1][0][2] and '2.0' in prompts[-1][0][2]
    assert 'restart' in prompts[-1][0][2].casefold()
    assert widget._driver_plan is None and not widget.driver_rollback.isEnabled()


def test_declining_rollback_and_restore_prompts_does_not_call_backend(panel, monkeypatch):
    widget, _, backend, _ = panel
    widget.refresh_drivers(); widget.device_choices.setCurrentIndex(0)
    widget.refresh_restore_points(); widget.restore_choices.setCurrentIndex(0)
    monkeypatch.setattr(module, 'ask_yes_no', lambda *a, **kw:False)
    widget.rollback_selected(); widget.delete_restore_point(); widget.open_restore()
    backend.driver_rollback.assert_not_called(); backend.system_restore_delete.assert_not_called(); backend.system_restore_open.assert_not_called()


def test_system_restore_create_exact_delete_and_reviewed_handoff(panel):
    widget, _, backend, prompts = panel
    widget.restore_description.setText('Before fixture update')
    widget.create_restore_point()
    assert backend.system_restore_create.call_args.args == ('Before fixture update',)
    assert backend.system_restore_create.call_args.kwargs['confirmed'] is True
    widget.refresh_restore_points(); widget.restore_choices.setCurrentIndex(0)
    widget.delete_restore_point()
    assert backend.system_restore_delete.call_args.args == ('d'*32, 'e'*32)
    assert '7' in prompts[-1][0][2] and 'cannot be undone' in prompts[-1][0][2]
    widget.refresh_restore_points(); widget.restore_choices.setCurrentIndex(0)
    backend.system_restore_open.return_value = {'status':'manual_action_required','reason':'Windows chooser opened; no restore applied','restored':False}
    widget.open_restore()
    assert backend.system_restore_open.call_args.args == ('d'*32, 'e'*32)
    assert backend.system_restore_open.call_args.kwargs == {'confirmed':True,'restart_acknowledged':True}
    assert 'restart' in prompts[-1][0][2].casefold() and 'does not apply' in prompts[-1][0][2]
    assert 'no restore applied' in widget.status.text()


@pytest.mark.parametrize('condition', ['busy','smoke','shutdown'])
def test_busy_smoke_or_shutdown_never_starts_new_recovery_or_prompt(panel, condition):
    widget, window, backend, prompts = panel
    if condition == 'busy': window.busy = True
    if condition == 'smoke': window.smoke_test = True
    if condition == 'shutdown': widget.shutdown()
    widget.export_drivers(); widget.restore_description.setText('Fixture'); widget.create_restore_point()
    assert not prompts
    backend.driver_backup_export.assert_not_called(); backend.system_restore_create.assert_not_called()


def test_cancellation_requests_event_without_terminating_running_native_call(panel):
    widget, _, _, _ = panel
    widget.cancel()
    assert widget._cancel.is_set()
    assert 'not terminated' in widget.status.text()


def test_large_backup_preview_is_bounded_and_narrow_layout_reflows(panel):
    widget, window, _, _ = panel
    widget._kind = 'backup_verify'
    widget._result({'status':'completed','items':[{'path':'fixture-'+str(index),'verified':True} for index in range(50000)]})
    assert len(widget.details.toPlainText()) < 10000 and 'additional_rows' in widget.details.toPlainText()
    window.resize(560,1050); window.show(); widget.show(); QApplication.processEvents()

    def resize_and_check_controls(width, height):
        # Native Windows may adjust the first shown top-level geometry, even
        # for a bare QWidget. Exercise the user's explicit resize after show.
        window.resize(width, height)
        for _ in range(3):
            QApplication.processEvents()
        assert window.width() == width
        assert window.rect().contains(widget.geometry())
        control_groups = (
            (widget.driver_refresh, widget.driver_export, widget.device_choices,
             widget.driver_rollback, widget.backup_choices, widget.backup_verify),
            (widget.restore_refresh, widget.restore_description, widget.restore_create,
             widget.restore_choices, widget.restore_delete, widget.restore_open))
        for tab_index, controls in enumerate(control_groups):
            widget.tabs.setCurrentIndex(tab_index)
            for _ in range(3):
                QApplication.processEvents()
            page = widget.tabs.currentWidget()
            assert page.parentWidget().rect().contains(page.geometry())
            for control in controls:
                assert page.rect().contains(control.geometry())
                if callable(getattr(control, 'text', None)):
                    assert control.fontMetrics().horizontalAdvance(control.text()) <= control.contentsRect().width()
            assert widget.details.horizontalScrollBar().maximum() == 0

    resize_and_check_controls(560,1050)
    assert window.width() <= 600
    assert widget._columns == 1
    resize_and_check_controls(850,1050)
    assert widget._columns == 2
    window.resize(920,800); QApplication.processEvents()
    assert widget._columns == 2


def test_missing_backend_hooks_are_explicitly_unavailable_without_work(panel):
    widget, _, backend, prompts = panel
    del backend.driver_backup_export
    widget._update_controls()
    assert not widget.driver_export.isEnabled()
    widget.export_drivers()
    assert 'unavailable' in widget.status.text().casefold() and not prompts
