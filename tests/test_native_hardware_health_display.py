"""Progress/settings hardware integration uses fixture services, never the PC."""
import copy
import os
import threading
import time
from types import SimpleNamespace
from unittest.mock import Mock

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

import pytest
from PySide6.QtCore import QCoreApplication, QEvent, QPoint, Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QWidget
from shiboken6 import isValid

from optimus.hardware_health import HEALTH_STAGE, normalize_hardware_health
from optimus.ui import DetailedSettingsWindow, OptimizationProgressWindow
from optimus.widgets.hardware_health_dialog import SnapshotHealthDialog
from optimus.widgets.theme import build_qss
from test_native_critical_review import bot
from test_native_worker_cards import fonts


CORE = ('maintenance', 'software_updates', 'driver_updates', 'security_scan')


def report():
    return normalize_hardware_health({'schema': 1, 'read_only': True, 'status': 'completed',
        'platform': 'Fixture', 'captured_at': time.time(),
        'batteries': [{'name': 'Fixture battery', 'condition': 'Normal',
            'full_charge_capacity': 41250, 'design_capacity': 50000, 'capacity_unit': 'mWh',
            'temp_c': 31.5, 'cycle_count': 123, 'source': 'Fixture battery provider'}],
        'storage': [{'name': 'Fixture SSD', 'device_id': 'fixture-drive', 'media_type': 'SSD',
            'health_status': 'Healthy', 'wear_percent_used': 12, 'temperature_c': 41,
            'source': 'Fixture storage provider', 'volumes': ['C:']}], 'errors': []})


def history():
    return {'schema': 1, 'status': 'available', 'series': [], 'scan_count': 0,
        'note': 'Fixture local history; no native queries.'}


def completed(**extra):
    return dict(id='fixture-run', status='completed', cancelled=False, stages=[],
        final_verification={'status': 'unverified', 'counts': {'unverified': 1}}, **extra)


def wait_until(app, predicate, seconds=4):
    deadline = time.monotonic() + seconds
    while not predicate() and time.monotonic() < deadline:
        app.processEvents()
        QTest.qWait(5)
    app.processEvents()
    assert predicate(), 'The hardware integration fixture did not settle.'


def settle(app):
    for _ in range(4):
        app.processEvents()
        QTest.qWait(5)


@pytest.fixture(scope='module')
def app():
    value = QApplication.instance() or QApplication([])
    value.setQuitOnLastWindowClosed(False)
    return value


@pytest.fixture(autouse=True)
def no_native_operations(monkeypatch):
    import subprocess
    import optimus.runtime_priority
    forbidden = Mock(side_effect=AssertionError('UI fixtures must never query the host.'))
    monkeypatch.setattr(subprocess, 'Popen', forbidden)
    monkeypatch.setattr(subprocess, 'run', forbidden)
    monkeypatch.setattr(optimus.runtime_priority, 'lower_worker_priority', lambda: lambda: None)
    monkeypatch.setenv('OPTIMUS_REDUCE_MOTION', '1')
    yield forbidden
    forbidden.assert_not_called()


@pytest.fixture
def progress(app):
    backend = SimpleNamespace(hardware_health_history=Mock(return_value=history()),
        hardware_health_scan=Mock(side_effect=AssertionError('No native hardware scan.')))
    owner = QWidget()
    owner.smoke_test, owner.busy, owner.backend = True, True, backend
    owner.setStyleSheet(build_qss('cyber'))
    value = OptimizationProgressWindow(owner, threading.Event())
    value.configure_workers({'hardware_health_enabled': True, 'software_updates_enabled': True,
        'driver_updates_enabled': True, 'spyware_scan_enabled': False})
    value.show()
    settle(app)
    yield value, backend, owner
    dialog = value._hardware_health_dialog
    if dialog is not None and isValid(dialog):
        dialog.close()
        dialog.deleteLater()
    value.completed = True
    value.spinner.stop()
    value.close()
    value.deleteLater()
    owner.close()
    owner.deleteLater()
    QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)
    app.processEvents()


@pytest.fixture
def settings(tmp_path, app):
    backend = bot.FixtureBackend(tmp_path / 'fixture-state', 'win32')
    backend.hardware_health_scan = Mock(return_value=report())
    def details(*, refresh=False):
        if refresh:
            backend.hardware_health_scan()
        return {'report': report(), 'history': history()}
    backend.hardware_health_details = Mock(side_effect=details)
    value = DetailedSettingsWindow(backend, smoke_test=True)
    value.show()
    settle(app)
    yield value, backend
    value.shutdown()
    value.close()
    value.deleteLater()
    QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)
    app.processEvents()


def test_health_summary_stays_in_the_existing_maintenance_card(progress):
    window, backend, _ = progress
    assert tuple(window.worker_cards) == CORE
    maintenance = window.worker_cards['maintenance']
    assert maintenance.hardware_health_box.isVisibleTo(window)
    assert 'queued' in maintenance.battery_health.text() and 'queued' in maintenance.drive_health.text()
    observed = report()
    before = copy.deepcopy(observed)
    window.update_progress({'stage': HEALTH_STAGE, 'percentage': 10, 'hardware_health': observed})
    assert '82.5%' in maintenance.battery_health.text()
    assert 'Healthy' in maintenance.drive_health.text() and '12% life used' in maintenance.drive_health.text()
    assert 'snapshot' in maintenance.health_details_button.toolTip()
    assert all(not window.worker_cards[key].hardware_health_box.isVisibleTo(window) for key in CORE[1:])
    assert observed == before
    backend.hardware_health_scan.assert_not_called()


def test_report_survives_other_stages_main_completion_and_automatic_followup(progress, app):
    window, _, _ = progress
    observed = report()
    window.update_progress({'stage': HEALTH_STAGE, 'percentage': 8, 'hardware_health': observed})
    card = window.worker_cards['maintenance']
    expected = (card.battery_health.text(), card.drive_health.text())
    window.update_progress({'stage': 'Clean temporary files', 'percentage': 40,
        'description': 'Fixture cleanup only; no native operation.'})
    assert (card.battery_health.text(), card.drive_health.text()) == expected
    window.finish(completed())
    window.begin_auto_continue(threading.Event(), automatic=True)
    window.update_progress({'stage': 'Recheck approved fixture updates', 'percentage': 55})
    window.finish_auto_continue(completed())
    settle(app)
    assert card._hardware_health_report == observed
    assert (card.battery_health.text(), card.drive_health.text()) == expected
    assert card.health_details_button.isVisibleTo(window)


def test_saved_opt_out_stays_hidden_even_if_a_report_is_present(progress):
    window, _, _ = progress
    window.configure_workers({'hardware_health_enabled': False})
    card = window.worker_cards['maintenance']
    assert card.hardware_health_box.isHidden()
    window.update_progress({'stage': 'Fixture saved result', 'percentage': 10, 'hardware_health': report()})
    assert card.hardware_health_box.isHidden()
    window.finish(completed(hardware_health=report()))
    assert card.hardware_health_box.isHidden()


@pytest.mark.parametrize('cancelled,expected', [(False, 'unavailable'), (True, 'cancelled')])
def test_missing_final_observation_stops_showing_queued(progress, cancelled, expected):
    window, _, _ = progress
    result = completed()
    result['cancelled'] = cancelled
    window.finish(result)
    card = window.worker_cards['maintenance']
    assert card._hardware_health_report['status'] == expected
    assert 'queued' not in card.battery_health.text() and 'queued' not in card.drive_health.text()


def test_snapshot_button_opens_readonly_while_main_worker_is_busy(progress, app):
    window, backend, owner = progress
    observed = report()
    window.update_progress({'stage': HEALTH_STAGE, 'percentage': 10, 'hardware_health': observed})
    assert owner.busy is True and not window.completed
    window.worker_cards['maintenance'].health_details_button.click()
    dialog = window._hardware_health_dialog
    assert isinstance(dialog, SnapshotHealthDialog) and dialog.isVisible()
    assert dialog.panel.report == observed and not dialog.panel.refresh_button.isEnabled()
    wait_until(app, lambda: backend.hardware_health_history.call_count == 1 and dialog._worker is None)
    assert 'Local history loaded' in dialog.history_status.text()
    window.worker_cards['maintenance'].health_details_button.click()
    assert window._hardware_health_dialog is dialog
    backend.hardware_health_history.assert_called_once_with()
    backend.hardware_health_scan.assert_not_called()


def test_settings_reads_cached_metadata_until_explicit_refresh(settings, app):
    window, backend = settings
    backend.hardware_health_details.assert_not_called()
    window.show_hardware_health()
    wait_until(app, lambda: backend.hardware_health_details.call_count == 1 and not window.busy)
    backend.hardware_health_details.assert_called_once_with(refresh=False)
    backend.hardware_health_scan.assert_not_called()
    assert window.dashboard_tabs.currentWidget() is window.hardware_health_panel
    assert window.hardware_health_panel.report['batteries'][0]['capacity_retained_percent'] == 82.5
    assert window.hardware_health_panel.cards[0].labels['temperature'].text().endswith('31.5 °C')
    window.hardware_health_panel.refresh_button.click()
    wait_until(app, lambda: backend.hardware_health_details.call_count == 2 and not window.busy)
    assert backend.hardware_health_details.call_args.kwargs == {'refresh': True}
    backend.hardware_health_scan.assert_called_once_with()


def test_selecting_the_dashboard_tab_loads_only_cached_metadata(settings, app):
    window, backend = settings
    window.dashboard_tabs.setCurrentWidget(window.hardware_health_panel)
    wait_until(app, lambda: backend.hardware_health_details.call_count == 1 and not window.busy)
    backend.hardware_health_details.assert_called_once_with(refresh=False)
    backend.hardware_health_scan.assert_not_called()


def test_busy_settings_does_not_disable_refresh_or_start_a_second_operation(settings):
    window, backend = settings
    window.busy = True
    assert window.hardware_health_panel.refresh_button.isEnabled()
    window.show_hardware_health(refresh=True)
    assert window.hardware_health_panel.refresh_button.isEnabled()
    backend.hardware_health_details.assert_not_called()
    backend.hardware_health_scan.assert_not_called()
    window.busy = False


def test_settings_details_error_leaves_refresh_available_and_no_stuck_reading_state(settings, app):
    window, backend = settings
    backend.hardware_health_details.side_effect = OSError('Fixture local-history read failed.')
    window.show_hardware_health()
    wait_until(app, lambda: backend.hardware_health_details.called and not window.busy)
    assert window.hardware_health_panel.refresh_button.isEnabled()
    assert not window.hardware_health_panel.status.text().startswith('Reading')
    assert 'Fixture' in window.status.text() or 'unavailable' in window.hardware_health_panel.status.text().lower()
    backend.hardware_health_scan.assert_not_called()


@pytest.mark.parametrize('width,height,font_pixels', [(850, 650, None), (1600, 977, None), (1920, 1080, None), (850, 650, 24)])
def test_all_four_cards_and_health_controls_fit_or_scroll_cleanly(progress, app, width, height, font_pixels):
    window, _, _ = progress
    window.update_progress({'stage': HEALTH_STAGE, 'percentage': 12, 'hardware_health': report()})
    window.resize(width, height)
    if font_pixels is not None:
        window.setStyleSheet(window.styleSheet() + '\nQWidget {font-size:24px;}')
    settle(app)
    assert tuple(window.worker_cards) == CORE
    assert window.body_scroll.horizontalScrollBar().maximum() == 0
    for card in window.worker_cards.values():
        for widget in (card.heading, card.progress, *card.navigation_buttons.values()):
            window.body_scroll.ensureWidgetVisible(widget, 0, 2)
            settle(app)
            assert window.body_scroll.viewport().rect().contains(widget.rect().translated(widget.mapTo(window.body_scroll.viewport(), QPoint())))
            assert widget.width() > 0 and widget.height() > 0
    maintenance = window.worker_cards['maintenance']
    for label in (maintenance.battery_health, maintenance.drive_health):
        window.body_scroll.ensureWidgetVisible(label, 0, 2)
        settle(app)
        assert label.height() >= max(label.fontMetrics().lineSpacing(), label.fontMetrics().height())
        assert label.visible_text and label.isVisibleTo(window)
        assert window.body_scroll.viewport().rect().contains(label.rect().translated(label.mapTo(window.body_scroll.viewport(), QPoint())))
    button = maintenance.health_details_button
    window.body_scroll.ensureWidgetVisible(button, 0, 2)
    settle(app)
    assert window.body_scroll.viewport().rect().contains(button.rect().translated(button.mapTo(window.body_scroll.viewport(), QPoint())))
    assert button.width() >= max(button.fontMetrics().horizontalAdvance(line) for line in button.text().splitlines()) + 12


def test_normal_dashboard_fits_health_and_populated_drive_warnings_without_scroll(progress, app):
    window, _, _ = progress
    window.resize(850, 650)
    window.update_progress({'stage': 'Clean temporary files', 'percentage': 44, 'hardware_health': report()})
    window.live_system_status.update_metrics({'computer': {'cpu_percent': 73.5,
        'memory_percent': 42., 'cpu_frequency_mhz': 3200}, 'network': {'kind': 'wifi',
        'adapter': 'Fixture radio', 'receive_mbps': 8., 'send_mbps': 1., 'link_mbps': 866.,
        'is_up': True, 'internet_state': 'unverified'}})
    window._update_space_guard({'enabled': True, 'complete': True, 'target_bytes': 2147483648,
        'volumes': [{'name': 'C:', 'free_bytes': 3221225472, 'target_bytes': 2147483648,
            'shortfall_bytes': 0, 'below_target': False},
            {'name': 'D:', 'free_bytes': 1073741824, 'target_bytes': 2147483648,
            'shortfall_bytes': 1073741824, 'below_target': True}],
        'warnings': ['Fixture drive D: below target.']})
    window.background_status.setText('An approved fixture application update. ' * 8)
    window.background_status.show()
    window.description.hide()
    settle(app)
    assert window.body_scroll.verticalScrollBar().maximum() == 0
    assert window.body_scroll.horizontalScrollBar().maximum() == 0
    heights = [card.height() for card in window.worker_cards.values()]
    assert max(heights) - min(heights) <= 1
    controls = [window.space_guard_status, window.live_system_status.computer,
        window.live_system_status.network, window.background_status]
    for card in window.worker_cards.values():
        controls.extend((card.heading, card.status, card.progress, *card.navigation_buttons.values()))
    card = window.worker_cards['maintenance']
    controls.extend((card.battery_health, card.drive_health, card.health_details_button))
    for control in controls:
        assert window.body_scroll.viewport().rect().contains(
            control.rect().translated(control.mapTo(window.body_scroll.viewport(), QPoint())))
    assert 'C:' in window.space_guard_status.text() and 'D:' in window.space_guard_status.text()
