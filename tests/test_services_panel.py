"""Service review contracts with fake requests only; no live service operations."""
from types import SimpleNamespace
import pytest
from PySide6.QtWidgets import QApplication
from optimus.widgets.services_panel import ServicesPanel

@pytest.fixture
def panel():
    app = QApplication.instance() or QApplication([])
    calls, prompts, errors = [], [], []
    window = SimpleNamespace(_run=lambda *a, **kw: calls.append((a, kw)), _confirm=lambda *a, **kw: prompts.append((a, kw)) or True, _error=errors.append, _show_result=lambda value: None)
    widget = ServicesPanel(window)
    yield widget, window, calls, prompts, errors
    widget.close()
    widget.deleteLater()
    app.processEvents()

def record(**values):
    result = {'Name':'FixtureService','DisplayName':'Disposable fixture','State':'Running','StartMode':'Auto','PathName':'C:/Fixture/service.exe','ProcessId':123,'Protected':False,'Reason':'Third-party fixture'}
    result.update(values)
    return result

def test_constructor_is_read_only(panel):
    widget, _, calls, _, _ = panel
    assert calls == []
    assert all(not button.isEnabled() for button in widget.actions.values())

def test_refresh_requests_inventory_only(panel):
    widget, _, calls, _, _ = panel
    widget.refresh()
    assert calls[0][0] == ('services',)

def test_protected_and_unknown_refuse_apply(panel):
    widget, _, calls, prompts, errors = panel
    for protected in (True, None, 'False'):
        widget._loaded([record(Protected=protected)])
        widget.table.selectRow(0)
        widget.apply('Stop')
    assert calls == [] and prompts == [] and len(errors) == 3

def test_stop_requires_running_and_confirmation(panel):
    widget, window, calls, prompts, errors = panel
    widget._loaded([record(State='Stopped')]);widget.table.selectRow(0)
    widget.apply('Stop')
    assert calls == [] and errors
    widget._loaded([record()]);widget.table.selectRow(0)
    window._confirm = lambda *a, **kw: False
    widget.apply('Stop')
    assert calls == []

def test_selected_service_payload_and_phrases(panel):
    widget, _, calls, prompts, _ = panel
    item = record()
    widget._loaded([item]);widget.table.selectRow(0)
    widget.apply('Stop')
    assert calls[-1][0] == ('service_set', item, 'Stop')
    assert prompts[-1][1]['phrase'] == 'STOP SERVICE'
    widget.apply('Disabled')
    assert calls[-1][0] == ('service_set', item, 'Disabled')
    assert prompts[-1][1]['phrase'] == 'DISABLE SERVICE'

def test_inventory_defaults_no_selection_and_result_deselects(panel):
    widget, _, calls, _, _ = panel
    widget._loaded([record()])
    assert widget.selected() is None
    widget.table.selectRow(0)
    widget._applied({'fixture':True})
    assert widget.selected() is None and calls == []
