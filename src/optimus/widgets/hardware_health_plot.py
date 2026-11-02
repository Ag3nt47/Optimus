"""Passive Qt history plot for prepared, identity-bound hardware series."""
from __future__ import annotations

import copy
from datetime import datetime, timezone
import json
import math

from PySide6.QtCore import QEvent, QPointF, QRectF, Qt, QTimer
from PySide6.QtGui import QColor, QPainter, QPen
from PySide6.QtWidgets import (QComboBox, QFormLayout, QLabel, QSizePolicy,
    QToolTip, QVBoxLayout, QWidget)


METRICS = {
    'capacity_retained_percent': 'Battery capacity retained',
    'cycle_count': 'Charge cycles',
    'wear_percent_used': 'Estimated SSD endurance used',
    'temperature_c': 'Temperature', 'temp_c': 'Temperature',
    'power_on_hours': 'Power-on hours',
    'uncorrected_read_errors': 'Uncorrected read errors',
    'uncorrected_write_errors': 'Uncorrected write errors',
    'provider_condition_code': 'Provider condition',
}
CONDITIONS = {0: 'Healthy/normal', 1: 'Warning', 2: 'Unhealthy'}


def _text(value, maximum=300):
    return ' '.join(''.join(c if c.isprintable() else ' ' for c in value[:2000]).split())[:maximum] if isinstance(value, str) else ''


def _finite(value, low=0, high=10**15):
    return type(value) in (int, float) and low <= value <= high and math.isfinite(value)


def _stamp(value, *, short=False):
    if not _finite(value, 0.001, 32503679999):
        return 'Time unavailable'
    return datetime.fromtimestamp(value, timezone.utc).strftime('%m-%d\n%H:%M UTC' if short else '%Y-%m-%d %H:%M:%S UTC')


def _series_rows(history):
    rows = history.get('series') if isinstance(history, dict) else None
    if not isinstance(rows, list):
        return []
    result = []
    for index, row in enumerate(rows[:512]):
        if not isinstance(row, dict) or row.get('metric') not in METRICS:
            continue
        metric = row['metric']
        identity = _text(row.get('id'), 256)
        stable = bool(identity) and row.get('identity_quality') == 'stable'
        kind = row.get('kind') if row.get('kind') in ('battery', 'drive') else 'device'
        key = json.dumps((kind, identity) if stable else (kind, identity, 'isolated', index), separators=(',', ':'))
        points = row.get('points') if isinstance(row.get('points'), list) else []
        # Keep the explicitly retained first-analysis marker when bounding a
        # caller's larger series; all other visible samples are recent ones.
        if len(points) > 500:
            baseline = next((point for point in points if isinstance(point, dict) and point.get('baseline') is True), None)
            recent = points[-499:] if baseline is not None else points[-500:]
            if baseline is not None and all(point is not baseline for point in recent):
                if recent and next(index for index, point in enumerate(points) if point is baseline) < len(points) - 500:
                    recent = [dict(recent[0], gap_before=True) if isinstance(recent[0], dict) else recent[0]] + recent[1:]
                points = [baseline] + recent
            else:
                points = recent
        prepared = []
        for point in points:
            if not isinstance(point, dict):
                continue
            captured = point.get('captured_at')
            captured = captured if _finite(captured, 0.001, 32503679999) else None
            value = point.get('value')
            low = -273.15 if metric in ('temperature_c', 'temp_c') else 0
            maximum = 1000 if metric in ('capacity_retained_percent', 'wear_percent_used') else 10**15
            value = value if _finite(value, low, maximum) else None
            if metric == 'provider_condition_code' and value not in CONDITIONS:
                value = None
            scan_index = point.get('scan_index')
            prepared.append({'scan_id': _text(point.get('scan_id'), 128),
                'scan_index': scan_index if type(scan_index) is int and 1 <= scan_index <= 10**12 else None,
                'captured_at': captured, 'value': value, 'baseline': point.get('baseline') is True,
                'gap_before': point.get('gap_before') is True})
        result.append({'key': key, 'id': identity, 'label': _text(row.get('label')) or 'Unnamed hardware',
            'kind': kind, 'stable': stable, 'metric': metric, 'unit': _text(row.get('unit'), 24), 'points': prepared})
    return result


def plot_geometry(series, rect):
    """Measured points and consecutive valid segments; missing values are gaps."""
    points = series.get('points', []) if isinstance(series, dict) else []
    timed = [point['captured_at'] for point in points if point.get('captured_at') is not None]
    values = [point['value'] for point in points if point.get('captured_at') is not None and point.get('value') is not None]
    if not timed or not values or rect.width() <= 0 or rect.height() <= 0:
        return {'points': [], 'segments': [], 'unknown': [], 'baseline_x': [], 'range': None}
    left, right = min(timed), max(timed)
    bottom, top = min(values), max(values)
    pad = max(abs(top) * 0.03, 1.0) if bottom == top else (top - bottom) * 0.1
    low, high = bottom - pad, top + pad
    if bottom >= 0:
        low = max(0, low)
    if low == high:
        high = low + 1
    if series.get('metric') in ('capacity_retained_percent', 'wear_percent_used'):
        low, high = 0.0, max(100.0, math.ceil(top / 10.0) * 10.0)
    categorical = series.get('metric') == 'provider_condition_code'
    if categorical:
        low, high = -0.2, 2.2
    positions, segments, unknown, baseline_x = [], [], [], []
    previous = None
    for point in points:
        if point.get('gap_before') is True:
            previous = None
        timestamp, value = point.get('captured_at'), point.get('value')
        if timestamp is None:
            previous = None
            continue
        x = rect.center().x() if left == right else rect.left() + (timestamp - left) / (right - left) * rect.width()
        if point.get('baseline'):
            baseline_x.append(x)
        if value is None:
            unknown.append((QPointF(x, rect.bottom()), point))
            previous = None
            continue
        position = QPointF(x, rect.bottom() - (value - low) / (high - low) * rect.height())
        if previous is not None and series.get('stable') is True and not categorical and timestamp > previous[1]['captured_at']:
            segments.append((previous[0], position))
        positions.append((position, point))
        previous = (position, point)
    return {'points': positions, 'segments': segments, 'unknown': unknown,
        'baseline_x': baseline_x, 'range': (left, right, low, high)}


class HardwareHistoryCanvas(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.series = None
        self.last_geometry = {'points': [], 'segments': [], 'unknown': [], 'baseline_x': [], 'range': None}
        self.setMinimumWidth(0)
        self.setMinimumHeight(250)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        self.setMouseTracking(True)
        self.setAccessibleName('Hardware history graph')

    def set_series(self, series):
        self.series = copy.deepcopy(series)
        self.last_geometry = {'points': [], 'segments': [], 'unknown': [], 'baseline_x': [], 'range': None}
        QToolTip.hideText()
        self.setAccessibleDescription('No measurements available.' if series is None else
            METRICS.get(series.get('metric'), 'Hardware metric') + '. Missing values break the line; baseline markers identify the first analysis.')
        self.update()

    def point_tooltip(self, point):
        value = point.get('value')
        unit = self.series.get('unit', '') if isinstance(self.series, dict) else ''
        if isinstance(self.series, dict) and self.series.get('metric') == 'provider_condition_code':
            rendered = CONDITIONS.get(value, 'Unavailable')
        else:
            rendered = 'Unavailable' if value is None else f'{value:,.2f}'.rstrip('0').rstrip('.') + (' ' + unit if unit else '')
        scan_index = point.get('scan_index')
        scan = 'Analysis #' + str(scan_index) + ' · ' if type(scan_index) is int else ''
        return ('Baseline · ' if point.get('baseline') else '') + scan + _stamp(point.get('captured_at')) + '\n' + rendered

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        foreground = self.palette().color(self.foregroundRole())
        painter.setPen(foreground)
        bounds = QRectF(self.rect()).adjusted(4, 4, -4, -4)
        metrics = self.fontMetrics()
        bottom_padding = max(50, metrics.lineSpacing() * 2 + 16)
        plot = bounds.adjusted(70, 18, -15, -bottom_padding)
        self.last_geometry = plot_geometry(self.series, plot)
        categorical = isinstance(self.series, dict) and self.series.get('metric') == 'provider_condition_code'
        if self.last_geometry['range'] is not None:
            low, high = self.last_geometry['range'][2:]
            ticks = list(CONDITIONS.values()) if categorical else [f'{low + index / 4 * (high - low):,.1f}'.rstrip('0').rstrip('.') for index in range(5)]
            left_padding = max(70, max(metrics.horizontalAdvance(label) for label in ticks) + 12)
            plot = bounds.adjusted(left_padding, 18, -15, -bottom_padding)
            self.last_geometry = plot_geometry(self.series, plot)
        geometry = self.last_geometry
        if geometry['range'] is None:
            painter.drawText(bounds, Qt.AlignCenter | Qt.TextWordWrap,
                'No numeric measurements available.\nUnavailable readings are not plotted as zero.')
            painter.end()
            return
        start, end, low, high = geometry['range']
        grid = QColor(foreground)
        grid.setAlpha(65)
        painter.setPen(QPen(grid, 1))
        painter.drawRect(plot)
        ticks = [(code, title) for code, title in CONDITIONS.items()] if categorical else [
            (low + index / 4 * (high - low), f'{low + index / 4 * (high - low):,.1f}'.rstrip('0').rstrip('.')) for index in range(5)]
        for value, label in ticks:
            y = plot.bottom() - (value - low) / (high - low) * plot.height()
            painter.drawLine(QPointF(plot.left(), y), QPointF(plot.right(), y))
            painter.setPen(foreground)
            painter.drawText(QRectF(bounds.left(), y - metrics.height() / 2 - 2,
                plot.left() - bounds.left() - 8, metrics.height() + 4), Qt.AlignRight | Qt.AlignVCenter, label)
            painter.setPen(QPen(grid, 1))
        painter.setPen(QColor('#5dd8ff'))
        baseline_pen = QPen(QColor('#5dd8ff'), 1, Qt.DashLine)
        for x in geometry['baseline_x']:
            painter.setPen(baseline_pen)
            painter.drawLine(QPointF(x, plot.top()), QPointF(x, plot.bottom()))
        painter.setPen(QPen(QColor('#56cd92'), 2))
        for first, second in geometry['segments']:
            painter.drawLine(first, second)
        for position, point in geometry['points']:
            color = QColor('#56cd92')
            if categorical:
                color = QColor({0: '#56cd92', 1: '#f8c75f', 2: '#ff6d72'}.get(point['value'], '#56cd92'))
            if point['baseline']:
                color = QColor('#5dd8ff')
            painter.setPen(QPen(color, 2))
            painter.setBrush(color if not point['baseline'] else self.palette().window())
            painter.drawEllipse(position, 4 if point['baseline'] else 3, 4 if point['baseline'] else 3)
        painter.setPen(QPen(QColor('#a2aebe'), 1))
        for position, point in geometry['unknown']:
            painter.drawLine(position + QPointF(-3, -3), position + QPointF(3, 3))
            painter.drawLine(position + QPointF(-3, 3), position + QPointF(3, -3))
        painter.setPen(foreground)
        short = lambda value: '\n'.join(metrics.elidedText(line, Qt.ElideRight, int(max(1, plot.width() / 2))) for line in _stamp(value, short=True).splitlines())
        painter.drawText(QRectF(plot.left(), plot.bottom() + 10, plot.width() / 2, bottom_padding - 8), Qt.AlignLeft | Qt.AlignTop, short(start))
        painter.drawText(QRectF(plot.center().x(), plot.bottom() + 10, plot.width() / 2, bottom_padding - 8), Qt.AlignRight | Qt.AlignTop, short(end))
        painter.end()

    def mouseMoveEvent(self, event):
        position = event.position()
        candidates = self.last_geometry['points'] + self.last_geometry['unknown']
        nearest = min(candidates, key=lambda item: (item[0].x() - position.x())**2 + (item[0].y() - position.y())**2, default=None)
        if nearest is not None and (nearest[0].x() - position.x())**2 + (nearest[0].y() - position.y())**2 < 400:
            QToolTip.showText(event.globalPosition().toPoint(), self.point_tooltip(nearest[1]), self)
        else:
            QToolTip.hideText()
        super().mouseMoveEvent(event)

    def leaveEvent(self, event):
        QToolTip.hideText()
        super().leaveEvent(event)


class HardwareHealthPlotWidget(QWidget):
    """Device/metric selectors over prepared local history, without I/O."""
    def __init__(self, parent=None):
        super().__init__(parent)
        self._series = []
        self.setMinimumWidth(0)
        self._fit_timer = QTimer(self)
        self._fit_timer.setSingleShot(True)
        self._fit_timer.timeout.connect(self._fit_height)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self.title = QLabel('Hardware health over time')
        self.title.setTextFormat(Qt.PlainText)
        self.title.setWordWrap(True)
        self.title.setMinimumWidth(0)
        self.title.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        layout.addWidget(self.title)
        form = QFormLayout()
        form.setRowWrapPolicy(QFormLayout.WrapLongRows)
        self.device_combo = QComboBox()
        self.metric_combo = QComboBox()
        for combo in (self.device_combo, self.metric_combo):
            combo.setMinimumWidth(0)
            combo.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Fixed)
        form.addRow('Device', self.device_combo)
        form.addRow('Metric', self.metric_combo)
        layout.addLayout(form)
        self.description = QLabel('First-analysis markers are baselines. Missing readings create gaps. Hover a point for its timestamp and value.')
        self.description.setTextFormat(Qt.PlainText)
        self.description.setWordWrap(True)
        self.description.setMinimumWidth(0)
        self.description.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        layout.addWidget(self.description)
        self.canvas = HardwareHistoryCanvas(self)
        layout.addWidget(self.canvas, 1)
        self.note = QLabel('No history has been supplied. The graph does not run checks or collect data.')
        self.note.setTextFormat(Qt.PlainText)
        self.note.setWordWrap(True)
        self.note.setMinimumWidth(0)
        self.note.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        layout.addWidget(self.note)
        self.device_combo.currentIndexChanged.connect(self._select_device)
        self.metric_combo.currentIndexChanged.connect(self._select_metric)

    def _fit_height(self):
        layout = self.layout()
        height = max(layout.minimumSize().height(), layout.heightForWidth(max(1, self.width())))
        if self.minimumHeight() != height:
            self.setMinimumHeight(height)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._fit_height()

    def changeEvent(self, event):
        super().changeEvent(event)
        if hasattr(self, '_fit_timer') and event.type() in (QEvent.FontChange, QEvent.StyleChange):
            self._fit_timer.start(0)

    def event(self, event):
        handled = super().event(event)
        if hasattr(self, '_fit_timer') and event.type() == QEvent.LayoutRequest:
            self._fit_timer.start(0)
        return handled

    def set_history(self, history):
        selected = self.device_combo.currentData()
        self._series = _series_rows(history)
        self.device_combo.blockSignals(True)
        self.device_combo.clear()
        seen = set()
        for row in self._series:
            if row['key'] in seen:
                continue
            seen.add(row['key'])
            label = row['label'] + ('' if row['stable'] else ' · identity unverified; isolated scan')
            self.device_combo.addItem(label, row['key'])
        index = self.device_combo.findData(selected) if selected is not None else -1
        if index < 0:
            useful = next((row['key'] for row in self._series if any(
                point['value'] is not None and point['captured_at'] is not None for point in row['points'])), None)
            index = self.device_combo.findData(useful) if useful is not None else -1
        self.device_combo.setCurrentIndex(max(0, index))
        self.device_combo.blockSignals(False)
        self.note.setText(_text(history.get('note'), 1000) if isinstance(history, dict) and _text(history.get('note'), 1000)
            else 'Only the supplied local history is shown. Up to 500 points per series include the retained first-analysis baseline.')
        self._select_device()

    def _select_device(self, *_):
        selected = self.metric_combo.currentData()
        key = self.device_combo.currentData()
        self.metric_combo.blockSignals(True)
        self.metric_combo.clear()
        for row in self._series:
            if row['key'] == key:
                title = METRICS[row['metric']]
                label = title + (' (' + row['unit'] + ')' if row['unit'] and row['unit'].casefold() not in title.casefold() else '')
                self.metric_combo.addItem(label, row['metric'])
        choice = self.metric_combo.findData(selected) if selected is not None else -1
        if choice < 0:
            useful = next((row['metric'] for row in self._series if row['key'] == key and any(
                point['value'] is not None and point['captured_at'] is not None for point in row['points'])), None)
            choice = self.metric_combo.findData(useful) if useful is not None else -1
        self.metric_combo.setCurrentIndex(max(0, choice))
        self.metric_combo.blockSignals(False)
        self.device_combo.setEnabled(self.device_combo.count() > 0)
        self.metric_combo.setEnabled(self.metric_combo.count() > 0)
        self._select_metric()

    def _select_metric(self, *_):
        key, metric = self.device_combo.currentData(), self.metric_combo.currentData()
        row = next((row for row in self._series if row['key'] == key and row['metric'] == metric), None)
        self.canvas.set_series(row)
        self.device_combo.setToolTip(self.device_combo.currentText())
        self.metric_combo.setToolTip(self.metric_combo.currentText())
        if row is not None:
            meaning = {
                'provider_condition_code': 'Provider conditions are categorical markers, not a calculated health score. ',
                'wear_percent_used': 'SSD endurance is a provider estimate of life used, not remaining life or a failure probability. ',
                'capacity_retained_percent': 'Battery capacity compares reported full-charge capacity with design capacity; it is not current charge. ',
            }.get(row['metric'], 'Reported counters are observations, not a prediction of remaining service life. ')
            self.description.setText(('First analysis is marked as the baseline. Missing measurements create gaps. '
                if row['stable'] else 'Hardware identity is unverified. Samples are isolated; no line joins them. ')
                + meaning
                + 'Hover a point for its timestamp and value.')
