"""Read-only presentation of normalized battery and physical-drive telemetry.

This widget never discovers hardware, starts a process, writes a setting or
refreshes a provider. The controller supplies reports and handles refreshRequested.
"""
from __future__ import annotations

import copy
from datetime import datetime, timezone
import math

from PySide6.QtCore import QEvent, Qt, QTimer, Signal
from PySide6.QtWidgets import (QGridLayout, QGroupBox, QHBoxLayout, QLabel,
    QLayout, QPushButton, QScrollArea, QSizePolicy, QVBoxLayout, QWidget)

from .hardware_health_plot import HardwareHealthPlotWidget


_UNAVAILABLE = 'Unavailable'
_STATUS = {'completed': 'Hardware check complete', 'partial': 'Hardware check partial',
           'unavailable': 'Hardware telemetry unavailable', 'cancelled': 'Hardware check cancelled',
           'disabled': 'Automatic hardware checks disabled'}


def _text(value, maximum=500):
    if not isinstance(value, str):
        return ''
    return ' '.join(''.join(c if c.isprintable() else ' ' for c in value[:4096]).split())[:maximum]


def _number(value, suffix='', *, digits=0, integer=False):
    if (type(value) not in (int, float) or abs(value) > 10**15
            or not math.isfinite(value) or integer and type(value) is not int):
        return _UNAVAILABLE
    return format(value, ',.' + str(digits) + 'f') + suffix


def _percent(value):
    if type(value) not in (int, float) or not 0 <= value <= 1000 or not math.isfinite(value):
        return _UNAVAILABLE
    return f'{value:.1f}%'


def _condition(value):
    value = _text(value, 160)
    return (_UNAVAILABLE if value.casefold().replace('_', ' ') in ('', 'unknown', 'unavailable', 'n/a', 'not reported', 'not available', 'none')
            else value.replace('_', ' '))


def _captured(value):
    if type(value) not in (int, float) or not 0 < value < 32503680000 or not math.isfinite(value):
        return 'Capture time unavailable'
    try:
        return 'Captured ' + datetime.fromtimestamp(value, timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')
    except (OSError, OverflowError, ValueError):
        return 'Capture time unavailable'


def _label(text='', *, bold=False):
    label = QLabel(text)
    label.setTextFormat(Qt.PlainText)
    label.setWordWrap(True)
    label.setTextInteractionFlags(Qt.TextSelectableByMouse)
    label.setMinimumWidth(0)
    label.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
    if bold:
        font = label.font()
        font.setBold(True)
        label.setFont(font)
    return label


def hardware_health_summary(report):
    """A bounded factual summary for a compact worker; never a health score."""
    if not isinstance(report, dict) or report.get('read_only') is not True:
        return 'Battery and physical-drive health: unavailable.'
    batteries = report.get('batteries') if isinstance(report.get('batteries'), list) else []
    drives = report.get('drives') if isinstance(report.get('drives'), list) else []
    battery_parts = [_percent(row.get('capacity_retained_percent')) + ' of design'
                     for row in batteries[:4] if isinstance(row, dict)]
    drive_parts = [_condition(row.get('health_status')) for row in drives[:4] if isinstance(row, dict)]
    return ('Battery: ' + (', '.join(battery_parts) if battery_parts else 'no battery telemetry reported')
        + ' · Physical drives: ' + (', '.join(drive_parts) if drive_parts else 'telemetry unavailable'))[:700]


class _DeviceCard(QGroupBox):
    def __init__(self, kind, row, captured, parent=None):
        super().__init__('Battery' if kind == 'battery' else 'Physical drive', parent)
        self.kind = kind
        self.labels = {}
        self.setMinimumWidth(0)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        layout = QVBoxLayout(self)
        layout.setSpacing(5)
        self.name = _label(_text(row.get('name')) or ('System battery' if kind == 'battery' else 'Physical drive'), bold=True)
        self.name.setAccessibleName(self.title() + ' name')
        layout.addWidget(self.name)
        source = _text(row.get('source')) or _UNAVAILABLE
        self.setToolTip('Source: ' + source + '\n' + captured)

        def add(key, title, value):
            label = _label(title + ': ' + value)
            label.setToolTip(title + ': ' + value + '\nSource: ' + source + '\n' + captured)
            label.setAccessibleName(title)
            self.labels[key] = label
            layout.addWidget(label)

        if kind == 'battery':
            unit = _text(row.get('capacity_unit'), 20)
            suffix = ' ' + unit if unit else ''
            add('condition', 'Provider condition', _condition(row.get('condition')))
            add('capacity_retained', 'Full-charge / design capacity', _percent(row.get('capacity_retained_percent')))
            self.labels['capacity_retained'].setToolTip('Capacity retained compares the provider-reported full-charge capacity with design capacity. '
                'This is not the current charge level or a maintenance completion percentage.\nSource: ' + source + '\n' + captured)
            add('full_charge', 'Full-charge capacity', _number(row.get('full_charge_capacity'), suffix))
            add('design', 'Design capacity', _number(row.get('design_capacity'), suffix))
            add('cycles', 'Charge cycles', _number(row.get('cycle_count'), integer=True))
            add('temperature', 'Temperature', _number(row.get('temperature_c', row.get('temp_c')), ' °C', digits=1))
        else:
            add('condition', 'Provider condition', _condition(row.get('health_status')))
            add('media', 'Media type', _condition(row.get('media_type')))
            add('wear', 'SSD life used', _percent(row.get('wear_percent_used')))
            self.labels['wear'].setToolTip('Wear is the provider-reported percentage of rated SSD life used, not life remaining or a health score. '
                'A missing or unsupported counter stays unavailable.\nSource: ' + source + '\n' + captured)
            add('temperature', 'Temperature', _number(row.get('temperature_c'), ' °C', digits=1))
            add('power_on', 'Power-on time', _number(row.get('power_on_hours'), ' hours'))
            add('read_errors', 'Uncorrected read errors', _number(row.get('uncorrected_read_errors'), integer=True))
            add('write_errors', 'Uncorrected write errors', _number(row.get('uncorrected_write_errors'), integer=True))
            volumes = row.get('volumes')
            volumes = [_text(item, 64) for item in volumes[:32] if _text(item, 64)] if isinstance(volumes, list) else []
            add('volumes', 'Volumes', ', '.join(volumes) if volumes else _UNAVAILABLE)
            identifier = _text(row.get('device_id'))
            self.name.setToolTip('Device: ' + (identifier or _UNAVAILABLE) + '\nSource: ' + source + '\n' + captured)
        add('source', 'Source', source)
        note = _text(row.get('note'), 1000)
        if note:
            add('note', 'Note', note)
        layout.addStretch(1)


class _DeviceGrid(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.cards = []
        self.columns = 0
        self.grid = QGridLayout(self)
        self.grid.setContentsMargins(0, 0, 0, 0)
        self.grid.setSpacing(8)
        self.grid.setSizeConstraint(QLayout.SetNoConstraint)
        self.setMinimumWidth(0)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        self._layout_timer = QTimer(self)
        self._layout_timer.setSingleShot(True)
        self._layout_timer.timeout.connect(self.reflow)

    def replace(self, cards):
        for old in self.cards:
            old.hide()
            old.setParent(None)
            old.deleteLater()
        self.cards = cards
        for card in cards:
            card.installEventFilter(self)
        self.columns = 0
        self.reflow()

    def reflow(self):
        # Labels wrap within a card. Basing column width on the entire longest
        # sentence needlessly forces one column, especially with fallback fonts.
        minimum = max(280, self.fontMetrics().height() * 18)
        spacing = self.grid.horizontalSpacing()
        columns = max(1, min(3, (self.width() + spacing) // (minimum + spacing)))
        if columns != self.columns:
            while self.grid.count():
                self.grid.takeAt(0)
            self.columns = columns
            for index, card in enumerate(self.cards):
                self.grid.addWidget(card, index // columns, index % columns)
            for index in range(3):
                self.grid.setColumnStretch(index, 1 if index < columns else 0)
        height = max(self.grid.minimumSize().height(), self.grid.heightForWidth(max(1, self.width())))
        if self.minimumHeight() != height:
            self.setMinimumHeight(height)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self.reflow()

    def changeEvent(self, event):
        super().changeEvent(event)
        if hasattr(self, '_layout_timer') and event.type() in (QEvent.FontChange, QEvent.StyleChange):
            self._layout_timer.start(0)

    def eventFilter(self, watched, event):
        if event.type() in (QEvent.FontChange, QEvent.StyleChange, QEvent.LayoutRequest) and not self._layout_timer.isActive():
            self._layout_timer.start(0)
        return super().eventFilter(watched, event)


class HardwareHealthPanel(QWidget):
    """Display supplied reports; the controller owns all collection and scheduling."""
    refreshRequested = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self._report = None
        self.setMinimumWidth(0)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        top = QHBoxLayout()
        self.title = _label('Battery and physical-drive health', bold=True)
        top.addWidget(self.title, 1)
        self.refresh_button = QPushButton('Refresh hardware check')
        self.refresh_button.setAutoDefault(False)
        self.refresh_button.setToolTip('Request a new read-only hardware report. This does not change battery settings, repair disks, optimize volumes or install drivers.')
        self.refresh_button.clicked.connect(self.refreshRequested.emit)
        top.addWidget(self.refresh_button)
        layout.addLayout(top)
        self.scope = _label('Read-only provider measurements. Battery retention compares full-charge capacity with design capacity; '
            'SSD wear is life used. Missing telemetry stays unavailable.')
        layout.addWidget(self.scope)
        self.status = _label('No hardware check has run in this view.')
        layout.addWidget(self.status)
        self.captured = _label('Capture time unavailable')
        layout.addWidget(self.captured)
        self.errors = _label()
        self.errors.hide()
        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.scroll.setMinimumWidth(0)
        self.content = QWidget()
        body = QVBoxLayout(self.content)
        body.setContentsMargins(3, 3, 3, 3)
        body.addWidget(self.errors)
        self.empty = _label('Battery and physical-drive readings will appear after the automatic check or a requested refresh.')
        body.addWidget(self.empty)
        self.device_grid = _DeviceGrid(self.content)
        body.addWidget(self.device_grid)
        self.history_plot = HardwareHealthPlotWidget(self.content)
        body.addWidget(self.history_plot, 1)
        self.scroll.setWidget(self.content)
        layout.addWidget(self.scroll, 1)

    @property
    def report(self):
        return copy.deepcopy(self._report)

    @property
    def cards(self):
        return tuple(self.device_grid.cards)

    def set_busy(self, busy):
        self.refresh_button.setEnabled(not busy)
        if busy:
            self.status.setText('Reading battery and physical-drive provider telemetry…')

    def set_history(self, history):
        self.history_plot.set_history(history)

    def set_report(self, report):
        if not isinstance(report, dict) or report.get('read_only') is not True:
            report = {'schema': 1, 'read_only': True, 'status': 'unavailable', 'captured_at': None,
                'batteries': [], 'drives': [], 'errors': ['A normalized read-only hardware report is unavailable.']}
        self._report = copy.deepcopy(report)
        batteries = report.get('batteries') if isinstance(report.get('batteries'), list) else []
        drives = report.get('drives') if isinstance(report.get('drives'), list) else []
        captured = _captured(report.get('captured_at'))
        self.captured.setText(captured)
        self.captured.setToolTip(captured + '\nThis is when the supplied provider report was captured; the panel does not collect hardware itself.')
        status = report.get('status')
        status_text = _STATUS.get(status, 'Hardware check status unavailable') if isinstance(status, str) else 'Hardware check status unavailable'
        errors = report.get('errors') if isinstance(report.get('errors'), list) else []
        notes = []
        recording = report.get('history_status')
        recorded = (isinstance(recording, dict) and (
            recording.get('status') in ('saved', 'recorded') and recording.get('saved') is True
            or recording.get('status') == 'already_recorded' and recording.get('saved') is False))
        if recording is not None and not recorded:
            recording = recording if isinstance(recording, dict) else {}
            state = _text(recording.get('status'), 80).replace('_', ' ') or 'unavailable'
            reason = _text(recording.get('reason') or recording.get('note'), 700)
            notes.append('History not confirmed recorded (' + state + ')' + (': ' + reason if reason else '.'))
            status_text += ' · history not recorded'
        warnings = report.get('warnings') if isinstance(report.get('warnings'), list) else []
        for warning in warnings[:16]:
            if isinstance(warning, dict):
                warning = warning.get('message') or warning.get('reason') or warning.get('note')
            if (text := _text(warning, 350)):
                notes.append('Warning: ' + text)
        self.status.setText(status_text)
        for error in errors[:16]:
            if isinstance(error, dict):
                error = error.get('message') or error.get('reason') or error.get('error')
            if (text := _text(error, 350)):
                notes.append(text)
        if len(batteries) > 64 or len(drives) > 64:
            notes.append('Showing the first 64 battery and 64 physical-drive records.')
        self.errors.setText('Provider notes: ' + ' · '.join(notes))
        self.errors.setToolTip(self.errors.text())
        self.errors.setVisible(bool(notes))
        empty = []
        if not batteries:
            empty.append('Battery: no battery telemetry reported.')
        if not drives:
            empty.append('Physical drives: no telemetry reported.')
        self.empty.setText(' '.join(empty))
        self.empty.setVisible(bool(empty))
        cards = [_DeviceCard(kind, row, captured, self.device_grid)
            for kind, rows in (('battery', batteries), ('drive', drives)) for row in rows[:64]
            if isinstance(row, dict)]
        self.device_grid.replace(cards)
        self.refresh_button.setEnabled(True)
