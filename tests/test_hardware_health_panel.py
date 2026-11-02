"""Battery and physical-drive presentation never queries or changes hardware."""
import copy
import os
from unittest.mock import Mock

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

import pytest
from PySide6.QtCore import QPoint, Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QProgressBar, QWidget

from optimus.widgets.hardware_health_panel import HardwareHealthPanel, hardware_health_summary
from optimus.widgets.theme import build_qss


def fixture_report():
    return {'schema': 1, 'read_only': True, 'status': 'completed', 'captured_at': 1793633738.0,
        'batteries': [{'name': 'Fixture main battery', 'condition': 'Normal',
            'capacity_retained_percent': 82.5, 'design_capacity': 50000, 'full_charge_capacity': 41250,
            'capacity_unit': 'mWh', 'cycle_count': 231, 'temp_c': 31.5,
            'source': 'Fixture battery provider', 'note': 'Fixture telemetry only.'}],
        'drives': [{'name': 'Fixture SSD', 'device_id': 'fixture-drive-0', 'volumes': ['C:', 'D:'],
            'media_type': 'SSD', 'health_status': 'Healthy', 'wear_percent_used': 14,
            'temperature_c': 42.5, 'power_on_hours': 12000, 'uncorrected_read_errors': 0,
            'uncorrected_write_errors': 0, 'source': 'Fixture storage provider', 'note': 'Fixture telemetry only.'}],
        'errors': []}


@pytest.fixture(scope='module')
def app():
    value = QApplication.instance() or QApplication([])
    value.setQuitOnLastWindowClosed(False)
    return value


@pytest.fixture
def panel(app):
    owner = QWidget()
    owner.setStyleSheet(build_qss('cyber'))
    owner.resize(1700, 1050)
    owner.show()
    value = HardwareHealthPanel(owner)
    value.resize(850, 650)
    value.show()
    app.processEvents()
    yield value
    value.hide()
    value.deleteLater()
    owner.deleteLater()
    app.processEvents()


def settle(app):
    for _ in range(4):
        app.processEvents()
        QTest.qWait(5)


def test_constructing_and_refreshing_only_emits_a_controller_request(panel, app, monkeypatch):
    import subprocess
    forbidden = Mock(side_effect=AssertionError('Hardware collection belongs to the controller.'))
    monkeypatch.setattr(subprocess, 'Popen', forbidden)
    monkeypatch.setattr(subprocess, 'run', forbidden)
    emitted = Mock()
    panel.refreshRequested.connect(emitted)
    assert panel.report is None and panel.cards == ()
    assert 'No hardware check' in panel.status.text()
    assert not panel.findChildren(QProgressBar)
    QTest.mouseClick(panel.refresh_button, Qt.LeftButton)
    emitted.assert_called_once_with()
    forbidden.assert_not_called()
    assert panel.report is None


def test_battery_retention_is_full_charge_vs_design_not_current_charge(panel):
    report = fixture_report()
    report['batteries'][0]['charge_percent'] = 97
    before = copy.deepcopy(report)
    panel.set_report(report)
    battery = panel.cards[0]
    assert battery.kind == 'battery'
    assert battery.labels['capacity_retained'].text() == 'Full-charge / design capacity: 82.5%'
    assert battery.labels['full_charge'].text() == 'Full-charge capacity: 41,250 mWh'
    assert battery.labels['design'].text() == 'Design capacity: 50,000 mWh'
    assert 'not the current charge' in battery.labels['capacity_retained'].toolTip()
    assert all('97' not in label.text() for label in battery.labels.values())
    assert report == before
    returned = panel.report
    returned['batteries'][0]['name'] = 'Mutated caller copy'
    assert panel.report['batteries'][0]['name'] == before['batteries'][0]['name']


def test_drive_wear_is_life_used_and_errors_preserve_valid_zero(panel):
    panel.set_report(fixture_report())
    drive = panel.cards[1]
    assert drive.labels['wear'].text() == 'SSD life used: 14.0%'
    assert 'not life remaining or a health score' in drive.labels['wear'].toolTip()
    assert drive.labels['read_errors'].text() == 'Uncorrected read errors: 0'
    assert drive.labels['write_errors'].text() == 'Uncorrected write errors: 0'
    assert drive.labels['power_on'].text() == 'Power-on time: 12,000 hours'
    assert drive.labels['volumes'].text() == 'Volumes: C:, D:'
    assert 'Fixture storage provider' in drive.toolTip()
    assert 'UTC' in drive.toolTip()
    assert 'fixture-drive-0' in drive.name.toolTip()


@pytest.mark.parametrize('bad', [None, True, '92', float('nan'), float('inf'), -1, 10**1000])
def test_unknown_or_invalid_retention_and_wear_do_not_become_health_scores(panel, bad):
    value = fixture_report()
    value['status'] = 'partial'
    value['batteries'][0]['capacity_retained_percent'] = bad
    value['drives'][0]['wear_percent_used'] = bad
    panel.set_report(value)
    assert panel.cards[0].labels['capacity_retained'].text().endswith('Unavailable')
    assert panel.cards[1].labels['wear'].text().endswith('Unavailable')
    assert 'partial' in panel.status.text()
    assert not panel.findChildren(QProgressBar)


def test_provider_reported_values_over_100_are_not_reversed_or_silently_clamped(panel):
    value = fixture_report()
    value['batteries'][0]['capacity_retained_percent'] = 104.2
    value['drives'][0]['wear_percent_used'] = 112
    panel.set_report(value)
    assert panel.cards[0].labels['capacity_retained'].text().endswith('104.2%')
    assert panel.cards[1].labels['wear'].text().endswith('112.0%')


def test_unavailable_temperature_cycles_and_capacity_do_not_look_like_zero(panel):
    value = fixture_report()
    value['batteries'][0].update(condition=None, temp_c=None, cycle_count=None,
        full_charge_capacity=None, design_capacity=None, source=None)
    value['drives'][0].update(temperature_c=None, power_on_hours=None,
        uncorrected_read_errors=None, uncorrected_write_errors=None, health_status='unknown')
    panel.set_report(value)
    for card, fields in ((panel.cards[0], ('condition', 'temperature', 'cycles', 'full_charge', 'design', 'source')),
                         (panel.cards[1], ('condition', 'temperature', 'power_on', 'read_errors', 'write_errors'))):
        assert all(card.labels[field].text().endswith('Unavailable') for field in fields)


@pytest.mark.parametrize('history_status', [
    {'status': 'limit_reached', 'saved': False, 'note': 'Existing baseline preserved; local history is full.'},
    {'status': 'unavailable', 'saved': False, 'reason': 'Existing history could not be read safely.'},
])
def test_warnings_and_unrecorded_history_are_visible_and_do_not_imply_success(panel, history_status):
    value = fixture_report()
    value['warnings'] = ['Reported battery capacity is below the review threshold.']
    value['history_status'] = history_status
    panel.set_report(value)
    assert 'history not recorded' in panel.status.text()
    assert 'Warning: Reported battery capacity' in panel.errors.text()
    assert history_status.get('reason', history_status.get('note')) in panel.errors.text()
    assert panel.errors.isVisibleTo(panel)


@pytest.mark.parametrize('history_status', [
    {'status': 'saved', 'saved': True}, {'status': 'already_recorded', 'saved': False},
])
def test_recorded_history_is_not_mislabeled_as_failure(panel, history_status):
    value = fixture_report()
    value['history_status'] = history_status
    panel.set_report(value)
    assert 'not recorded' not in panel.status.text()
    assert 'not confirmed recorded' not in panel.errors.text()


def test_many_provider_notes_scroll_with_the_report_instead_of_expanding_window(panel, app):
    value = fixture_report()
    value['warnings'] = ['Fixture warning with detailed context. ' * 20 for _ in range(16)]
    panel.set_report(value)
    settle(app)
    assert panel.size().toTuple() == (850, 650)
    assert panel.scroll.isAncestorOf(panel.errors)
    assert panel.scroll.verticalScrollBar().maximum() > 0


@pytest.mark.parametrize('status', ['completed', 'partial', 'unavailable', 'cancelled', 'disabled'])
def test_empty_report_explains_missing_hardware_without_fabricated_results(panel, status):
    panel.set_report({'schema': 1, 'read_only': True, 'status': status, 'captured_at': None,
        'batteries': [], 'drives': [], 'errors': ['Fixture provider cannot expose SMART.']})
    assert panel.cards == ()
    assert 'no battery telemetry' in panel.empty.text()
    assert 'no telemetry' in panel.empty.text()
    assert 'SMART' in panel.errors.text() and panel.errors.isVisibleTo(panel)
    assert panel.captured.text() == 'Capture time unavailable'
    assert '%' not in panel.empty.text() and '%' not in panel.status.text()


@pytest.mark.parametrize('bad', [None, [], {}, {'read_only': False}])
def test_missing_readonly_report_stays_unavailable(panel, bad):
    panel.set_report(bad)
    assert panel.report['status'] == 'unavailable'
    assert panel.cards == ()
    assert 'unavailable' in panel.status.text()


def test_refresh_disabled_while_controller_is_busy_and_report_returns(panel):
    emitted = Mock()
    panel.refreshRequested.connect(emitted)
    panel.set_busy(True)
    panel.refresh_button.click()
    emitted.assert_not_called()
    assert 'Reading' in panel.status.text()
    panel.set_report(fixture_report())
    assert panel.refresh_button.isEnabled()
    panel.refresh_button.click()
    emitted.assert_called_once_with()


@pytest.mark.parametrize('width,height,expected_columns', [(450, 650, 1), (850, 650, 2), (1600, 977, 3)])
def test_device_cards_fit_equal_columns_and_scroll_without_horizontal_overflow(panel, app, width, height, expected_columns):
    value = fixture_report()
    value['batteries'][0]['name'] = 'FixtureBatteryNameWithoutSpaces' * 12
    value['drives'] = [dict(value['drives'][0], name='Fixture physical drive ' + str(index),
        device_id='fixture-device-' * 30) for index in range(5)]
    panel.set_report(value)
    panel.resize(width, height)
    settle(app)
    assert panel.width() == width and panel.height() == height
    assert panel.device_grid.columns == expected_columns
    assert panel.scroll.horizontalScrollBar().maximum() == 0
    assert panel.scroll.verticalScrollBar().maximum() > 0
    assert panel.rect().contains(panel.refresh_button.rect().translated(panel.refresh_button.mapTo(panel, QPoint())))
    first_row = panel.cards[:expected_columns]
    assert max(card.width() for card in first_row) - min(card.width() for card in first_row) <= 1
    for card in panel.cards:
        panel.scroll.ensureWidgetVisible(card.labels['source'], 0, 2)
        settle(app)
        label = card.labels['source']
        rectangle = label.rect().translated(label.mapTo(panel.scroll.viewport(), QPoint()))
        assert panel.scroll.viewport().rect().contains(rectangle)
        assert card.width() <= panel.scroll.viewport().width()


def test_larger_late_font_reflows_and_keeps_metrics_reachable(panel, app):
    panel.set_report(fixture_report())
    panel.setStyleSheet('QWidget {font-size:24px;}')
    settle(app)
    assert panel.device_grid.columns == 1
    assert panel.scroll.horizontalScrollBar().maximum() == 0
    for card in panel.cards:
        for label in card.labels.values():
            panel.scroll.ensureWidgetVisible(label, 0, 2)
            settle(app)
            assert panel.scroll.viewport().rect().contains(label.rect().translated(label.mapTo(panel.scroll.viewport(), QPoint())))


def test_repeated_reports_replace_cards_and_preserve_plain_text(panel, app):
    value = fixture_report()
    value['drives'][0]['name'] = '<b>Fixture provider name</b>'
    panel.set_report(value)
    panel.set_report({'schema': 1, 'read_only': True, 'status': 'completed',
        'batteries': [], 'drives': value['drives'], 'errors': []})
    settle(app)
    assert len(panel.cards) == 1 and panel.cards[0].kind == 'drive'
    assert panel.cards[0].name.textFormat() == Qt.PlainText
    assert panel.cards[0].name.text() == '<b>Fixture provider name</b>'


def test_compact_summary_has_evidence_and_never_invents_health_score():
    value = fixture_report()
    before = copy.deepcopy(value)
    text = hardware_health_summary(value)
    assert '82.5% of design' in text and 'Healthy' in text
    assert 'health score' not in text and '86%' not in text and 'charge level' not in text
    assert hardware_health_summary(None).endswith('unavailable.')
    assert value == before
