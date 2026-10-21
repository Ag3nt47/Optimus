"""Process harness UI uses disposable fixture plans and never changes host processes."""
import copy
import threading
import time
from unittest.mock import Mock

import pytest
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication, QFileDialog, QWidget

from optimus.widgets.process_harness_panel import ProcessHarnessPanel


@pytest.fixture(scope='module')
def qapp():
    return QApplication.instance() or QApplication([])


def fixture_plan(**changes):
    value = {'id': 'fixture-plan', 'complete': True, 'cancelled': False, 'errors': [],
             'expires_at': time.time() + 900, 'notes': ['Fixture only; launch reasons may be inferred.'],
             'processes': [
                 {'pid': 12, 'ppid': 8, 'name': 'system-fixture.exe', 'application': 'Protected fixture',
                  'exe': r'C:\Windows\system-fixture.exe', 'memory_rss': 1024, 'protected': True,
                  'protection_reason': 'Core Windows fixture.'},
                 {'pid': 123, 'ppid': 99, 'create_time': 123.5, 'name': 'fixture.exe', 'application': 'Fixture app',
                  'exe': r'D:\Fixture\fixture.exe', 'memory_rss': 1024 * 1024, 'protected': False,
                  'ownership_evidence': ['Installed application path matched.'],
                  'parent': {'pid': 99, 'name': 'launcher.exe', 'exe': r'D:\Fixture\launcher.exe'},
                  'why_running': 'Observed startup registration match.', 'why_running_confidence': 'evidence',
                  'startup_matches': [{'name': 'Fixture startup', 'source': 'Run'}],
                  'metrics': {'cpu_percent': 35, 'measured_intervals': 2},
                  'recommendation': 'Save work before reviewing this application.'}],
             'actions': [
                 {'id': 'sleep-one', 'kind': 'sleep_process', 'label': 'Pause fixture.exe (PID 123)',
                  'target': 'fixture.exe', 'reason': 'Eligible selected application; can become unresponsive.'},
                 {'id': 'startup-one', 'kind': 'disable_startup', 'label': 'Disable Fixture startup',
                  'target': 'Fixture startup', 'reason': 'Exact startup registration match.'}]}
    value.update(changes)
    return value


@pytest.fixture
def panel(qapp):
    window = QWidget()
    window.busy = False
    window._run = Mock()
    window._confirm = Mock(return_value=True)
    window._error = Mock()
    window.resume_processes = Mock()
    window.select_page = Mock()
    value = ProcessHarnessPanel(window)
    yield value, window
    value.shutdown()
    window.deleteLater()
    qapp.processEvents()


def test_complete_scan_starts_unselected_and_protected_hidden_but_counted(panel):
    value, window = panel
    value._scan_ready(fixture_plan())
    assert value.table.rowCount() == 1
    assert '2 processes; 1 protected; 1 shown' in value.summary.text()
    assert not value.show_protected.isChecked()
    assert not value.apply_button.isEnabled()
    assert all(value.actions.item(index).checkState() == Qt.Unchecked for index in range(value.actions.count()))
    window._run.assert_not_called()


def test_show_all_exposes_protection_reason_and_filter_works(panel):
    value, _window = panel
    value._scan_ready(fixture_plan())
    value.show_protected.setChecked(True)
    assert value.table.rowCount() == 2
    value.search.setText('Protected fixture')
    assert value.table.rowCount() == 1
    assert 'Core Windows fixture' in value.details.toPlainText()
    value.show_protected.setChecked(False)
    assert value.table.rowCount() == 0


def test_details_explain_observed_program_parent_and_launch_confidence(panel):
    value, _window = panel
    value._scan_ready(fixture_plan())
    text = value.details.toPlainText()
    for expected in ['Fixture app', r'D:\Fixture\fixture.exe', 'launcher.exe', 'Installed application path matched',
                     'Observed startup registration match', 'confidence: evidence', 'Fixture startup',
                     'measured_intervals', 'Save work', 'not proof']:
        assert expected in text


def test_new_scan_invalidates_previous_apply_and_has_cancellation_event(panel):
    value, window = panel
    value._scan_ready(fixture_plan())
    value.actions.item(0).setCheckState(Qt.Checked)
    assert value.apply_button.isEnabled()
    value.scan()
    assert value._plan is None and value.actions.count() == 0
    assert not value.apply_button.isEnabled()
    assert window._run.call_args.args == ('process_harness_scan',)
    assert isinstance(window._run.call_args.kwargs['cancel_event'], threading.Event)
    value.cancel_scan()
    assert window._run.call_args.kwargs['cancel_event'].is_set()


@pytest.mark.parametrize('changes', [
    {'complete': False}, {'id': ''}, {'errors': ['Inventory failed']}, {'cancelled': True},
    {'expires_at': 1}, {'expires_at': 'invalid'}, {'expires_at': float('inf')}])
def test_partial_failed_cancelled_or_expired_scan_cannot_apply(panel, changes):
    value, window = panel
    value._scan_ready(fixture_plan(**changes))
    if value.actions.count():
        value.actions.item(0).setCheckState(Qt.Checked)
    value.apply_selected()
    assert not value.apply_button.isEnabled()
    assert value._selected_actions() == []
    window._run.assert_not_called()
    window._confirm.assert_not_called()


def test_apply_sends_exact_selected_opaque_ids_after_review_then_invalidates(panel):
    value, window = panel
    value._scan_ready(fixture_plan())
    value.actions.item(1).setCheckState(Qt.Checked)
    value.apply_selected()
    assert window._run.call_args.args == ('process_harness_apply', {
        'plan_id': 'fixture-plan', 'selected_ids': ['startup-one'], 'confirmed': True})
    prompt = window._confirm.call_args
    assert prompt.kwargs['phrase'] == 'APPLY SELECTED CHANGES'
    assert 'Disable Fixture startup' in prompt.kwargs['details']
    assert 'Pause fixture.exe' not in prompt.kwargs['details']
    assert 'Resume paused apps' in prompt.args[1] and 'Restore/history' in prompt.args[1]
    assert value._plan is None and value.actions.count() == 0
    assert not value.apply_button.isEnabled()


def test_declining_apply_preserves_choices_without_host_changes(panel):
    value, window = panel
    value._scan_ready(fixture_plan())
    value.actions.item(0).setCheckState(Qt.Checked)
    window._confirm.return_value = False
    value.apply_selected()
    window._run.assert_not_called()
    assert value._plan['id'] == 'fixture-plan'


def test_expiry_while_reviewing_prevents_apply(panel):
    value, window = panel
    value._scan_ready(fixture_plan())
    value.actions.item(0).setCheckState(Qt.Checked)

    def expire(*args, **kwargs):
        value._plan['expires_at'] = 1
        return True

    window._confirm.side_effect = expire
    value.apply_selected()
    window._run.assert_not_called()
    assert not value.apply_button.isEnabled()
    assert value.actions.item(0).checkState() == Qt.Unchecked


def test_more_than_batch_limit_cannot_apply(panel):
    value, window = panel
    actions = [dict(fixture_plan()['actions'][0], id='sleep-' + str(index)) for index in range(26)]
    value._scan_ready(fixture_plan(actions=actions))
    for index in range(value.actions.count()):
        value.actions.item(index).setCheckState(Qt.Checked)
    assert not value.apply_button.isEnabled()
    value.apply_selected()
    window._run.assert_not_called()


def test_export_sends_only_plan_id_and_chosen_destination(panel, monkeypatch):
    value, window = panel
    value._scan_ready(fixture_plan())
    monkeypatch.setattr(QFileDialog, 'getSaveFileName', lambda *args: (r'D:\Fixture\report.json', ''))
    value.export_scan()
    assert window._run.call_args.args == ('process_harness_export', 'fixture-plan', r'D:\Fixture\report.json')


def test_full_details_uses_only_selected_process(panel):
    value, window = panel
    plan = fixture_plan()
    value._scan_ready(plan)
    value.full_details()
    assert window._run.call_args.args == ('process_details', plan['processes'][1])


def test_shutdown_cancels_scan_stops_expiry_timer_and_ignores_late_results(panel):
    value, window = panel
    value.scan()
    cancel = window._run.call_args.kwargs['cancel_event']
    value.shutdown()
    value._scan_ready(fixture_plan())
    assert cancel.is_set()
    assert not value._timer.isActive()
    assert value._plan is None


def test_actual_harness_fixture_report_fits_panel_contract(panel, tmp_path):
    from optimus.process_harness import ProcessHarness
    from test_process_harness import Backend
    value, window = panel
    backend = Backend(tmp_path)
    tool = ProcessHarness(backend)
    report = tool.scan()
    value._scan_ready(report)
    assert value._can_apply()
    assert value.actions.count() == len(report['actions'])
    assert value.table.rowCount() == len(report['processes'])
    assert 'Fixture application' in value.details.toPlainText()
    assert 'same executable grouped' in value.details.toPlainText()
    backend.sleep_process.assert_not_called()
    backend.startup_set.assert_not_called()
    backend.uninstall.assert_not_called()
    window._run.assert_not_called()


@pytest.mark.parametrize('failure', [False, True])
def test_harness_scan_uses_existing_worker_without_blocking_gui(qapp, tmp_path, monkeypatch, failure):
    from optimus.ui import DetailedSettingsWindow
    monkeypatch.setenv('LOCALAPPDATA', str(tmp_path))
    seen = []

    class Backend:
        data_dir = tmp_path

        def process_harness_scan(self, cancel_event=None):
            seen.append(threading.get_ident())
            if failure:
                raise RuntimeError('Fixture scan failed')
            return copy.deepcopy(fixture_plan())

    window = DetailedSettingsWindow(Backend(), smoke_test=True)
    try:
        value = window.process_harness_panel
        value.scan()
        deadline = time.monotonic() + 2
        while window.busy and time.monotonic() < deadline:
            qapp.processEvents()
            time.sleep(0.005)
        assert not window.busy
        assert seen and seen[0] != threading.get_ident()
        if failure:
            assert value._plan is None and not value.apply_button.isEnabled()
            assert 'Fixture scan failed' in window.last_error
        else:
            assert value._plan['id'] == 'fixture-plan'
            assert value.table.rowCount() == 1
        assert window.performance_tabs.tabText(2) == 'Process cleanup harness'
    finally:
        window.pool.waitForDone(2000)
        window.shutdown()
        window.close()
        window.deleteLater()
        qapp.processEvents()
