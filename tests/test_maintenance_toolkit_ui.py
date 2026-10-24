"""Toolkit controls exercise review/approval flow without a host scan or worker."""
import copy
import os
from unittest.mock import Mock

os.environ.setdefault('QT_QPA_PLATFORM','offscreen')

import pytest
from PySide6.QtCore import QCoreApplication,QEvent,Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication,QPushButton,QWidget

from optimus.toolkit_configuration import DEFAULTS
from optimus.widgets.maintenance_toolkit_panel import MaintenanceToolkitPanel,ToolkitChoices


@pytest.fixture(scope='module')
def app():return QApplication.instance() or QApplication([])


class FixtureWindow(QWidget):
    def __init__(self,settings=None):
        super().__init__()
        self.busy=False;self.smoke_test=False;self.calls=[];self.pending=[]
        self.settings={'toolkit_settings':copy.deepcopy(settings or {}),'unrelated_setting':'preserve'}
        self.backend=Mock()
        self.backend.preferences.side_effect=lambda:copy.deepcopy(self.settings)
        def configure(value):
            self.settings['toolkit_settings']=copy.deepcopy(value)
            return {'schema':1,'status':'saved','note':'Saved choices only.'}
        self.backend.configure_toolkit.side_effect=configure
    def _run(self,method,*args,done=None,**kwargs):
        self.calls.append((method,args,kwargs));self.pending.append((method,args,kwargs,done));self.busy=True
    def finish(self,result=None):
        method,args,kwargs,done=self.pending.pop(0)
        if result is None:result=getattr(self.backend,method)(*args,**kwargs)
        self.busy=False
        if done:done(result)


@pytest.fixture
def panel(app):
    window=FixtureWindow();value=MaintenanceToolkitPanel(window)
    yield value
    value.shutdown();window.close();window.deleteLater()
    QCoreApplication.sendPostedEvents(None,QEvent.DeferredDelete);app.processEvents()


def test_constructing_settings_never_runs_scans_or_changes_preferences(panel):
    assert panel.editor.choices()==DEFAULTS
    assert panel.details.isReadOnly() and not panel.window.calls
    assert panel.window.settings=={'toolkit_settings':{},'unrelated_setting':'preserve'}
    assert panel.backend.method_calls==[('preferences',(),{})]


def test_low_space_choice_is_default_off_and_saves_only_on_explicit_save(panel):
    field=panel.editor.fields[('low_space_cleaning','enabled')]
    assert not field.isChecked()
    panel.editor.tabs.setCurrentIndex(4)
    field.setChecked(True)
    assert panel.window.settings['toolkit_settings']=={} and not panel.window.calls
    panel.save();panel.window.finish()
    assert panel.window.settings['toolkit_settings']['low_space_cleaning']=={'enabled':True}
    assert panel.window.settings['unrelated_setting']=='preserve'
    assert len(panel.window.calls)==1


def test_select_all_apps_neither_enables_cleanup_nor_approves_loss_of_undo(panel):
    before=panel.editor.choices()
    button=next(item for item in panel.editor.findChildren(QPushButton) if item.text()=='Select all apps')
    button.click()
    choices=panel.editor.choices()
    assert len(choices['application_caches']['apps'])==len(panel.editor.cache_apps)
    assert not choices['application_caches']['enabled']
    assert choices['recovery_retention']==before['recovery_retention']
    assert panel.window.calls==[]


def test_save_goes_through_window_worker_and_preserves_other_preferences(panel):
    panel.editor.fields[('history','enabled')].setChecked(True)
    panel.save()
    assert panel.window.calls[0][0]=='configure_toolkit'
    assert panel.window.settings['toolkit_settings']=={}
    panel.window.finish()
    assert panel.window.settings['toolkit_settings']['history']['enabled'] is True
    assert panel.window.settings['unrelated_setting']=='preserve'
    assert len(panel.window.calls)==1


def test_invalid_browser_backup_approval_never_saves(panel):
    panel.editor.fields[('recovery_retention','enabled')].setChecked(True)
    panel.editor.fields[('recovery_retention','browser_backups')].setChecked(True)
    panel.save()
    assert not panel.window.calls
    assert 'approv' in panel.status.text().casefold() or 'undo' in panel.status.text().casefold()


def test_apply_requires_current_preview_and_explicit_yes(panel,monkeypatch):
    panel.editor.tabs.setCurrentIndex(1)
    ask=Mock(return_value=True)
    monkeypatch.setattr('optimus.widgets.maintenance_toolkit_panel.ask_yes_no',ask)
    panel.apply()
    assert not ask.called and not panel.window.calls
    panel.preview();plan={'id':'a'*32,'kind':'application_caches','status':'preview','items':[]}
    panel.window.finish(plan)
    ask.return_value=False;panel.apply()
    assert len(panel.window.calls)==1
    ask.return_value=True;panel.apply()
    assert panel.window.calls[-1][0:2]==('toolkit_apply',('application_caches',plan))


def test_switching_tabs_cannot_relabel_async_preview_authority(panel,monkeypatch):
    panel.editor.tabs.setCurrentIndex(1);panel.preview()
    panel.editor.tabs.setCurrentIndex(3)
    panel.window.finish({'id':'a'*32,'kind':'application_caches','items':[]})
    ask=Mock(return_value=True)
    monkeypatch.setattr('optimus.widgets.maintenance_toolkit_panel.ask_yes_no',ask)
    panel.apply()
    assert not ask.called
    assert not any(method=='toolkit_apply' for method,args,kwargs in panel.window.calls)


def test_refresh_failure_removes_old_preview_authority(panel,monkeypatch):
    panel.editor.tabs.setCurrentIndex(1);panel.preview();panel.window.finish({'id':'a'*32,'items':[]})
    panel.preview();panel.window.finish({'status':'failed','error':'scan was incomplete'})
    ask=Mock(return_value=True)
    monkeypatch.setattr('optimus.widgets.maintenance_toolkit_panel.ask_yes_no',ask)
    panel.apply()
    assert not ask.called
    assert not any(method=='toolkit_apply' for method,args,kwargs in panel.window.calls)


def test_apply_button_only_enabled_for_reviewed_cleanup_scope(panel):
    button=panel.buttons['Apply reviewed cleanup']
    assert not button.isEnabled()
    panel.editor.tabs.setCurrentIndex(1)
    assert not button.isEnabled()
    panel.preview();panel.window.finish({'id':'a'*32,'items':[]})
    assert button.isEnabled()
    panel.editor.tabs.setCurrentIndex(2)
    assert not button.isEnabled()


def test_explicit_repair_no_answer_never_dispatches(panel,monkeypatch):
    ask=Mock(return_value=False)
    monkeypatch.setattr('optimus.widgets.maintenance_toolkit_panel.ask_yes_no',ask)
    panel.integrity(True)
    assert ask.called and not panel.window.calls
    ask.return_value=True;panel.integrity(True)
    assert panel.window.calls[0][0]=='toolkit_integrity'
    assert panel.window.calls[0][2]['repair'] is True and panel.window.calls[0][2]['approved'] is True


@pytest.mark.parametrize('size',[(850,650),(1024,768)])
def test_panel_flow_controls_fit_without_clipping_at_supported_sizes(panel,app,size):
    panel.resize(*size);panel.show();app.processEvents()
    for title,button in panel.buttons.items():
        assert button.width()>=button.fontMetrics().horizontalAdvance(title)+8
        assert button.height()>=button.fontMetrics().height()+4
        parent=button.parentWidget()
        assert button.x()>=0 and button.y()>=0 and button.x()+button.width()<=parent.width()+1
    assert panel.editor.width()<=panel.width()
    panel.hide()


def test_shutdown_cancels_pending_safe_boundary_and_ignores_late_results(panel):
    panel.check();cancel=panel.window.calls[0][2]['cancel_event']
    panel.shutdown()
    assert cancel.is_set()
    panel.window.finish({'id':'a'*32,'note':'late'})
    assert panel.details.toPlainText()==''
