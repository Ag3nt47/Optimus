"""Review UI contracts without any real cleanup, driver or package mutation."""
from __future__ import annotations
from types import SimpleNamespace
import pytest
from PySide6.QtCore import QPoint, Qt
from PySide6.QtWidgets import QApplication, QPushButton
from optimus.ui import DetailedSettingsWindow, OptimusWindow
from test_native_auto_continue_ui import wait_until


@pytest.fixture(scope='module')
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app


@pytest.fixture
def window(qapp, tmp_path):
    value = DetailedSettingsWindow(SimpleNamespace(data_dir=tmp_path), smoke_test=True)
    yield value
    value.shutdown()
    value.close()
    value.deleteLater()
    qapp.processEvents()


def preview(window):
    window._debloat_actions_ready([{'id':'fixture-one','name':'Fixture one'},{'id':'fixture-two','name':'Fixture two'}])
    plan = {'planId':'fixture-plan','canApply':True,'summary':'Two disposable fixture packages','targets':[
        {'id':'target-one','name':'Fixture.One','scope':'Current user'},
        {'id':'target-two','name':'Fixture.Two','scope':'Current user'}], 'recovery':'Fixture only.'}
    window._debloat_ready(plan)
    return plan


def test_debloat_defaults_to_no_selection(window):
    preview(window)
    assert window.debloat_targets.count() == 2
    assert all(window.debloat_targets.item(i).checkState() == Qt.Unchecked for i in range(2))
    assert not window.debloat_apply_button.isEnabled()


def test_debloat_apply_payload_contains_only_checked_target(window):
    preview(window)
    calls, prompts = [], []
    window._run = lambda *a, **kw: calls.append((a, kw))
    window._confirm = lambda *a, **kw: prompts.append((a, kw)) or True
    window.debloat_targets.item(1).setCheckState(Qt.Checked)
    assert window.debloat_apply_button.isEnabled()
    window.apply_debloat()
    assert calls[0][0] == ('debloat_apply', {'planId':'fixture-plan','selectedIds':['target-two'],'confirmed':True})
    assert prompts[0][1]['phrase'] == 'APPLY REVIEWED PLAN'
    assert prompts[0][1]['details']['selectedTargets'][0]['name'] == 'Fixture.Two'


def test_debloat_cancel_sends_no_apply(window):
    preview(window)
    calls = []
    window._run = lambda *a, **kw: calls.append((a, kw))
    window._confirm = lambda *a, **kw: False
    window.debloat_targets.item(0).setCheckState(Qt.Checked)
    window.apply_debloat()
    assert calls == []


def test_debloat_action_change_invalidates_selection(window):
    preview(window)
    window.debloat_targets.item(0).setCheckState(Qt.Checked)
    window.debloat_combo.setCurrentIndex(1)
    assert window.debloat_plan is None
    assert window.debloat_targets.count() == 0
    assert not window.debloat_apply_button.isEnabled()


def test_debloat_failed_preview_cannot_apply(window):
    window._debloat_ready({'planId':'failed','canApply':False,'summary':'Preview failed','targets':[{'id':'one','name':'Fixture'}]})
    assert not window.debloat_apply_button.isEnabled()
    assert not window.debloat_targets.item(0).flags() & Qt.ItemIsEnabled


def test_driver_install_payload_is_selected_update_identity(window):
    calls = []
    record = {'id':'fixture-update','revision':7,'title':'Fixture device driver','downloaded':False,'eulaAccepted':False}
    window._driver_updates_ready({'updates':[record],'source':'Fixture WUA','resultCode':2})
    assert window.driver_install_button.isEnabled()
    window._driver_admin = lambda: True
    window._confirm = lambda *a, **kw: kw.get('phrase') == 'INSTALL DRIVER'
    window._run = lambda *a, **kw: calls.append((a, kw))
    window.install_driver_update()
    assert calls[0][0] == ('driver_update_apply', record)


def test_driver_install_cancel_does_not_apply(window):
    window._driver_updates_ready({'updates':[{'id':'fixture','revision':1,'title':'Fixture'}]})
    window._driver_admin = lambda: True
    window._confirm = lambda *a, **kw: False
    calls = []
    window._run = lambda *a, **kw: calls.append(a)
    window.install_driver_update()
    assert calls == []


def test_cloud_page_constructs_without_connecting(window):
    assert 'Cloud drive' in window.pages
    window.select_page('Cloud drive', load=False)
    assert window.cloud_panel._session is None
    assert window.cloud_panel._plan is None


def test_primary_window_has_four_requested_buttons(qapp, tmp_path, monkeypatch):
    monkeypatch.setenv('OPTIMUS_REDUCE_MOTION', '1')
    value = OptimusWindow(SimpleNamespace(data_dir=tmp_path, preferences=lambda: {}), smoke_test=True)
    try:
        assert [button.text() for button in value.main_buttons] == ['1 click Optimize and debloat','Settings','Sandbox','Revert']
        fullscreen = value.fullscreen.button
        assert fullscreen not in value.main_buttons
        assert set(value.findChildren(QPushButton)) == set(value.main_buttons) | {fullscreen}
        assert not value.landing_scroll.isAncestorOf(fullscreen)
        value.show()
        def reachable():
            if not fullscreen.isVisibleTo(value) or not fullscreen.isEnabled():
                return False
            ancestor = fullscreen.parentWidget()
            while ancestor is not None:
                if not ancestor.contentsRect().contains(fullscreen.rect().translated(fullscreen.mapTo(ancestor, QPoint()))):
                    return False
                if ancestor is value:
                    hit = value.childAt(fullscreen.mapTo(value, fullscreen.rect().center()))
                    return hit is fullscreen or hit is not None and fullscreen.isAncestorOf(hit)
                ancestor = ancestor.parentWidget()
            return False
        wait_until(qapp, reachable)
        assert fullscreen.accessibleName() == 'Full screen'
        assert value.windowTitle() == 'Optimus: Computer optimization tool'
    finally:
        value.shutdown()
        value.close()
        value.deleteLater()
        qapp.processEvents()
