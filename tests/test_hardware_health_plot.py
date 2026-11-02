"""Identity-bound history plots use supplied fixtures, never live collection."""
import copy
import os
from unittest.mock import Mock

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

import pytest
from PySide6.QtCore import QPoint, QRectF
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QWidget

from optimus.widgets.hardware_health_plot import HardwareHealthPlotWidget, _series_rows, plot_geometry
from optimus.widgets.hardware_health_panel import HardwareHealthPanel
from optimus.widgets.theme import build_qss


START = 1793633738.0


def series(identity='fixture-battery', *, quality='stable', metric='capacity_retained_percent', values=(90, 88, 87)):
    unit = {'capacity_retained_percent': '%', 'wear_percent_used': '%', 'cycle_count': 'cycles',
        'provider_condition_code': 'provider condition', 'temperature_c': '°C', 'temp_c': '°C',
        'power_on_hours': 'hours'}.get(metric, 'count')
    return {'id': identity, 'label': 'Fixture battery', 'kind': 'battery', 'identity_quality': quality,
        'metric': metric, 'unit': unit,
        'points': [{'scan_id': str(index), 'captured_at': START + index * 86400,
                    'value': value, 'baseline': index == 0} for index, value in enumerate(values)]}


@pytest.fixture(scope='module')
def app():
    result = QApplication.instance() or QApplication([])
    result.setQuitOnLastWindowClosed(False)
    return result


@pytest.fixture
def plot(app):
    owner = QWidget()
    owner.setStyleSheet(build_qss('cyber'))
    owner.resize(1700, 1050)
    owner.show()
    result = HardwareHealthPlotWidget(owner)
    result.resize(850, 400)
    result.show()
    app.processEvents()
    yield result
    result.hide()
    result.deleteLater()
    owner.deleteLater()
    app.processEvents()


def settle(app):
    for _ in range(3):
        app.processEvents()
        QTest.qWait(5)


def geometry(row):
    return plot_geometry(_series_rows({'series': [row]})[0], QRectF(50, 20, 600, 250))


def test_passive_constructor_and_selection_never_launch_a_collector(plot, app, monkeypatch):
    import subprocess
    forbidden = Mock(side_effect=AssertionError('History must already be prepared by the controller.'))
    monkeypatch.setattr(subprocess, 'Popen', forbidden)
    monkeypatch.setattr(subprocess, 'run', forbidden)
    value = {'schema': 1, 'series': [series(), series(metric='cycle_count', values=(10, 11, 12))],
             'note': 'Fixture retained baseline and recent reports.'}
    before = copy.deepcopy(value)
    plot.set_history(value)
    assert plot.device_combo.count() == 1 and plot.metric_combo.count() == 2
    plot.metric_combo.setCurrentIndex(1)
    settle(app)
    assert plot.canvas.series['metric'] == 'cycle_count'
    assert [point['value'] for point in plot.canvas.series['points']] == [10, 11, 12]
    assert value == before
    forbidden.assert_not_called()


def test_numeric_gaps_break_lines_without_inventing_zero_values():
    value = geometry(series(values=(90, None, 85)))
    assert len(value['points']) == 2 and len(value['unknown']) == 1
    assert value['segments'] == []
    assert [point['value'] for _, point in value['points']] == [90, 85]
    assert len(value['baseline_x']) == 1
    assert value['range'][2:] == (0.0, 100.0)


def test_omitted_historical_interval_never_draws_a_line_across_the_gap():
    row = series(values=(90, 87, 86))
    row['points'][1].update(gap_before=True, scan_index=700)
    prepared = _series_rows({'series': [row]})[0]
    value = plot_geometry(prepared, QRectF(50, 20, 600, 250))
    assert len(value['points']) == 3 and len(value['segments']) == 1
    assert value['segments'][0][0] == value['points'][1][0]
    assert prepared['points'][1]['gap_before'] is True and prepared['points'][1]['scan_index'] == 700


def test_percentage_axes_are_fixed_to_100_and_extend_for_actual_higher_values():
    assert geometry(series(values=(90, 89)))['range'][2:] == (0.0, 100.0)
    assert geometry(series(metric='wear_percent_used', values=(5, 112)))['range'][2:] == (0.0, 120.0)
    assert geometry(series(metric='temperature_c', values=(20, 40)))['range'][3] < 100


def test_provider_conditions_are_categories_without_numeric_score_interpolation():
    value = geometry(series(metric='provider_condition_code', values=(0, 1, 2)))
    assert value['range'][2:] == (-0.2, 2.2)
    assert len(value['points']) == 3 and value['segments'] == []
    assert [point['value'] for _, point in value['points']] == [0, 1, 2]


def test_provider_condition_tooltip_translates_codes_and_keeps_unknown_unavailable(plot):
    plot.set_history({'series': [series(metric='provider_condition_code', values=(0, None, 2))]})
    points = plot.canvas.series['points']
    assert 'Healthy/normal' in plot.canvas.point_tooltip(points[0])
    assert 'Unavailable' in plot.canvas.point_tooltip(points[1])
    assert 'Unhealthy' in plot.canvas.point_tooltip(points[2])
    assert '%' not in plot.canvas.point_tooltip(points[0])
    assert 'categorical markers' in plot.description.text()


def test_invalid_provider_condition_is_a_gap():
    value = geometry(series(metric='provider_condition_code', values=(0, 100, 1)))
    assert len(value['points']) == 2 and len(value['unknown']) == 1
    assert value['segments'] == []


def test_baseline_remains_marked_when_its_measurement_is_unknown():
    value = geometry(series(values=(None, 90, 85)))
    assert len(value['baseline_x']) == 1
    assert len(value['unknown']) == 1 and value['unknown'][0][1]['baseline'] is True
    assert len(value['segments']) == 1


def test_all_unknown_measurements_are_not_an_empty_healthy_100_percent():
    value = geometry(series(values=(None, None, None)))
    assert value['range'] is None and value['points'] == [] and value['segments'] == []


@pytest.mark.parametrize('quality', ['weak', 'unknown', None])
def test_unverified_identity_never_connects_or_groups_series_by_name(plot, quality):
    value = series(quality=quality)
    assert geometry(value)['segments'] == []
    plot.set_history({'series': [value, series(quality=quality, metric='cycle_count')]})
    assert plot.device_combo.count() == 2
    assert 'identity unverified' in plot.device_combo.currentText()
    assert 'isolated' in plot.description.text()


def test_same_device_name_does_not_join_two_stable_identities(plot):
    plot.set_history({'series': [series('device-one'), series('device-two')]})
    assert plot.device_combo.count() == 2
    assert plot.device_combo.itemData(0) != plot.device_combo.itemData(1)


def test_first_view_selects_available_provider_condition_when_capacity_or_wear_is_unknown(plot):
    battery = series('battery', values=(None, None))
    wear = dict(series('drive', metric='wear_percent_used', values=(None, None)), kind='drive')
    condition = dict(series('drive', metric='provider_condition_code', values=(0, 0)), kind='drive')
    plot.set_history({'series': [battery, wear, condition]})
    assert plot.canvas.series['kind'] == 'drive'
    assert plot.canvas.series['metric'] == 'provider_condition_code'
    plot.metric_combo.setCurrentIndex(plot.metric_combo.findData('wear_percent_used'))
    plot.set_history({'series': [battery, wear, condition]})
    assert plot.canvas.series['metric'] == 'wear_percent_used'  # Explicit user choice survives refresh.


def test_metric_and_device_selection_survive_reordered_fresh_history(plot):
    rows = [series('one'), series('one', metric='cycle_count'), series('two')]
    plot.set_history({'series': rows})
    plot.metric_combo.setCurrentIndex(plot.metric_combo.findData('cycle_count'))
    selected = plot.device_combo.currentData()
    plot.set_history({'series': list(reversed(rows))})
    assert plot.device_combo.currentData() == selected
    assert plot.metric_combo.currentData() == 'cycle_count'
    assert plot.canvas.series['id'] == 'one'


def test_visible_bound_retains_first_analysis_baseline_and_recent_samples():
    value = series(values=tuple(range(900)))
    row = _series_rows({'series': [value]})[0]
    assert len(row['points']) == 500
    assert row['points'][0]['baseline'] is True and row['points'][0]['captured_at'] == START
    assert row['points'][-1]['captured_at'] == START + 899 * 86400
    assert row['points'][1]['captured_at'] == START + 401 * 86400
    assert row['points'][1]['gap_before'] is True


@pytest.mark.parametrize('bad', [True, '95', float('nan'), float('inf'), -1, 10**1000])
def test_invalid_numeric_readings_become_gaps(bad):
    value = geometry(series(values=(90, bad, 85)))
    assert len(value['points']) == 2 and len(value['unknown']) == 1 and value['segments'] == []


def test_unknown_timestamp_breaks_a_line_instead_of_joining_unrelated_samples():
    value = series()
    value['points'][1]['captured_at'] = None
    result = geometry(value)
    assert len(result['points']) == 2 and result['segments'] == []


def test_zero_error_count_is_a_real_measurement_and_temperature_can_be_below_zero():
    error = geometry(series(metric='uncorrected_read_errors', values=(0, None, 1)))
    assert error['points'][0][1]['value'] == 0 and len(error['unknown']) == 1
    cold = geometry(series(metric='temperature_c', values=(-5, 0, 10)))
    assert [point['value'] for _, point in cold['points']] == [-5, 0, 10]


def test_tooltip_identifies_baseline_timestamp_actual_value_and_unknown(plot):
    plot.set_history({'series': [series(values=(90, None, 85))]})
    points = plot.canvas.series['points']
    assert plot.canvas.point_tooltip(points[0]).startswith('Baseline · ')
    assert 'UTC' in plot.canvas.point_tooltip(points[0]) and '90 %' in plot.canvas.point_tooltip(points[0])
    unknown = plot.canvas.point_tooltip(points[1])
    assert 'Unavailable' in unknown and '0 %' not in unknown


@pytest.mark.parametrize('width', [350, 850, 1600])
def test_selectors_and_chart_fit_resized_panel_and_render_real_points(plot, app, width):
    plot.set_history({'series': [series()]})
    plot.resize(width, 420)
    settle(app)
    image = plot.grab()
    assert image.width() / image.devicePixelRatio() == width
    for widget in (plot.device_combo, plot.metric_combo, plot.canvas):
        assert plot.rect().contains(widget.rect().translated(widget.mapTo(plot, QPoint())))
    assert len(plot.canvas.last_geometry['points']) == 3
    assert len(plot.canvas.last_geometry['segments']) == 2


def test_health_panel_accepts_history_without_collecting_or_replacing_current_report(app):
    panel = HardwareHealthPanel()
    try:
        report = {'schema': 1, 'read_only': True, 'status': 'completed',
                  'batteries': [], 'drives': [], 'errors': []}
        panel.set_report(report)
        panel.set_history({'series': [series()]})
        assert panel.report == report
        assert panel.history_plot.device_combo.count() == 1
    finally:
        panel.deleteLater()
        app.processEvents()
