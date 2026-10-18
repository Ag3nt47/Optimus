"""Registry UI/backend contracts with mocked transport; no registry changes."""
from types import SimpleNamespace
from unittest.mock import Mock
import pytest
from PySide6.QtWidgets import QApplication
from optimus.backend import Backend
from optimus.ui import DetailedSettingsWindow

@pytest.fixture(scope='module')
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app

@pytest.fixture
def window(qapp):
    value = DetailedSettingsWindow(SimpleNamespace(), smoke_test=True)
    yield value
    value.shutdown()
    value.close()
    value.deleteLater()
    qapp.processEvents()

def issue(**overrides):
    record = {'Id':'a'*32, 'Name':'FixtureMissing.exe', 'Kind':'RegistryEntry',
              'Target':r'C:\MissingFixture\FixtureMissing.exe', 'Scope':r'HKCU\Software\Microsoft\Windows\CurrentVersion\App Paths',
              'KeyPath':r'HKCU\Software\Microsoft\Windows\CurrentVersion\App Paths\FixtureMissing.exe',
              'CanRemove':True, 'Fingerprint':'b'*64, 'ValueCount':2,
              'Evidence':'Missing target does not prove this registration is unused.'}
    record.update(overrides)
    return record

def test_backend_registry_requests_are_structured_and_selective():
    backend = Backend.__new__(Backend)
    backend.bridge = Mock()
    backend.registry_scan()
    backend.bridge.request.assert_called_with('registry_scan')
    selected = issue()
    backend.registry_remove(selected)
    backend.bridge.request.assert_called_with('registry_remove', record=selected)
    change = {'Id':'c'*32, 'Kind':'RegistryEntry'}
    backend.registry_restore(change)
    backend.bridge.request.assert_called_with('registry_restore', change=change)
    backend.restore(change)
    backend.bridge.request.assert_called_with('restore', change=change)

def test_registry_page_constructs_without_scanning(window):
    assert 'Registry review' in window.pages
    assert window.registry_table.rowCount() == 0
    assert not window.registry_remove_button.isEnabled()
    assert window.services_panel.table.rowCount() == 0

def test_explicit_scan_only_requests_read_only_scan(window):
    calls = []
    window._run = lambda *args, **kwargs: calls.append((args, kwargs))
    window.load_registry()
    assert calls[0][0] == ('registry_scan',)
    assert calls[0][1]['done'] == window._registry_ready

def test_registry_evidence_is_visible_and_unknown_records_cannot_remove(window):
    window._registry_ready({'issues':[issue(CanRemove=False)], 'note':'Missing target is not proof.', 'skipped':3})
    assert 'not proof' in window.registry_note.text()
    assert 'Excluded or unreadable entries: 3' in window.registry_note.text()
    assert 'Missing target does not prove' in window.registry_details.toPlainText()
    assert not window.registry_remove_button.isEnabled()
    window._run = Mock()
    window.remove_registry_issue()
    window._run.assert_not_called()
    assert 'not approved' in window.last_error

def test_registry_removal_cancel_never_sends_mutation(window):
    window._registry_ready({'issues':[issue()], 'note':'Review', 'skipped':0})
    window._confirm = lambda *args, **kwargs: False
    window._run = Mock()
    window.remove_registry_issue()
    window._run.assert_not_called()

def test_confirmed_registry_removal_sends_only_selected_approved_record(window):
    first, selected = issue(), issue(Id='d'*32, Name='Chosen.exe')
    window._registry_ready({'issues':[first, selected], 'note':'Review'})
    for row in range(window.registry_table.rowCount()):
        if window.registry_table.item(row, 0).text() == 'Chosen.exe':
            window.registry_table.selectRow(row)
    prompts, calls = [], []
    window._confirm = lambda *args, **kwargs: prompts.append((args, kwargs)) or True
    window._run = lambda *args, **kwargs: calls.append((args, kwargs))
    window.remove_registry_issue()
    assert calls[0][0] == ('registry_remove', selected)
    assert prompts[0][1]['phrase'] == 'REMOVE REGISTRATION'
    assert prompts[0][1]['details'] == selected
    assert not window.registry_remove_button.isEnabled()

def test_completed_removal_invalidates_review_and_exposes_history(window):
    window._registry_ready({'issues':[issue()]})
    result = {'Id':'e'*32, 'Kind':'RegistryEntry','Status':'Applied'}
    window._registry_removed(result)
    assert window.registry_table.rowCount() == 0
    assert not window.registry_remove_button.isEnabled()
    assert 'Registry review' not in window.loaded_pages
    assert 'RegistryEntry' in window.result_text.toPlainText()
    assert window.stack.currentWidget() is window.pages['Restore/history']
