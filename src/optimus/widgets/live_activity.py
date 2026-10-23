"""Measured activity displays. Sampling is owned separately by the live run."""
from __future__ import annotations

import math
from PySide6.QtCore import QRectF, Qt, QTimer
from PySide6.QtGui import QColor, QFont, QPainter, QPen
from PySide6.QtWidgets import QLabel, QVBoxLayout, QWidget


def number(value, minimum=0, maximum=100):
    return (float(value) if isinstance(value, (int, float)) and not isinstance(value, bool)
            and math.isfinite(value) and minimum <= value <= maximum else None)


class WorkerActivityRing(QWidget):
    COLORS = {'working': '#55c987', 'waiting': '#e8b34b', 'pressure': '#ed914b',
              'error': '#ef6262', 'unverified': '#7291a8', 'complete': '#55c987'}

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedSize(66, 72)
        self.cpu_percent, self.health, self.active, self.angle = None, 'unverified', False, 0
        self.timer = QTimer(self)
        self.timer.setInterval(65)
        self.timer.timeout.connect(self._tick)
        self.setAccessibleName('Measured worker CPU activity')

    def set_activity(self, cpu_percent, health, *, active=False):
        self.cpu_percent = number(cpu_percent)
        self.health = health if health in self.COLORS else 'unverified'
        self.active = active is True
        self.setAccessibleDescription('Worker thread CPU: ' + self.value_text() + '. ' + self.health + '.')
        self._sync()
        self.update()

    def value_text(self):
        return '—' if self.cpu_percent is None else f'{self.cpu_percent:.1f}%'

    def _sync(self):
        from .header_controls import motion_allowed
        owner = self
        while owner is not None and not hasattr(owner, 'config'):
            owner = owner.parentWidget()
        if self.isVisible() and not self.window().isMinimized() and self.active and motion_allowed(getattr(owner, 'config', None)):
            self.timer.start()
        else:
            self.timer.stop()

    def _tick(self):
        self.angle = (self.angle + 9) % 360
        self.update()

    def showEvent(self, event):
        super().showEvent(event)
        self._sync()

    def hideEvent(self, event):
        self.timer.stop()
        super().hideEvent(event)

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        ellipse = QRectF(5, 3, 56, 56)
        pen = QPen(QColor('#263b4d'), 4)
        painter.setPen(pen)
        painter.drawEllipse(ellipse)
        pen.setColor(QColor(self.COLORS[self.health]))
        pen.setCapStyle(Qt.RoundCap)
        painter.setPen(pen)
        if self.active:
            painter.drawArc(ellipse, int((90 - self.angle) * 16), -255 * 16)
        else:
            painter.drawEllipse(ellipse)
        font = QFont(self.font())
        font.setPointSize(9)
        font.setBold(True)
        painter.setFont(font)
        painter.setPen(QColor('#edf6ff'))
        painter.drawText(ellipse, Qt.AlignCenter, self.value_text())
        font.setPointSize(7)
        font.setBold(False)
        painter.setFont(font)
        painter.drawText(QRectF(0, 59, 66, 12), Qt.AlignCenter, 'CPU activity')


class LiveSystemStatus(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 2, 0, 2)
        layout.setSpacing(3)
        self.computer = QLabel('Computer: measuring CPU and RAM…')
        self.network = QLabel('Network: measuring connection and receive rate…')
        self.note = QLabel('Worker colors describe observed activity, waiting, resource pressure or errors.')
        for label in (self.computer, self.network, self.note):
            label.setTextFormat(Qt.PlainText)
            label.setWordWrap(True)
            label.setMinimumWidth(0)
            layout.addWidget(label)
        self.note.setStyleSheet('color:#a6b8c7;font-size:11px;')

    def update_metrics(self, payload):
        payload = payload if isinstance(payload, dict) else {}
        computer = payload.get('computer')
        computer = computer if isinstance(computer, dict) else {}
        cpu, ram = number(computer.get('cpu_percent')), number(computer.get('memory_percent'))
        frequency = number(computer.get('cpu_frequency_mhz'), 1, 100000)
        cpu_text = '—' if cpu is None else f'{cpu:.1f}%'
        ram_text = '—' if ram is None else f'{ram:.1f}%'
        self.computer.setText('Computer: CPU ' + cpu_text
            + (f' · {frequency / 1000:.2f} GHz' if frequency is not None else '')
            + ' · RAM ' + ram_text
            + (' · Resource pressure' if computer.get('pressure') is True or computer.get('pressure') == 'high' else ''))
        network = payload.get('network')
        network = network if isinstance(network, dict) else {}
        kind = 'Wi-Fi' if network.get('kind') == 'wifi' else 'Network'
        adapter = str(network.get('adapter') or '')[:80]
        receive = number(network.get('receive_mbps'), 0, 1000000)
        send = number(network.get('send_mbps'), 0, 1000000)
        link = number(network.get('link_mbps'), 0.001, 1000000)
        state = ('Connected' if network.get('is_up') is True else
                 'Disconnected' if network.get('is_up') is False else 'Connection unverified')
        text = kind + (': ' + adapter if adapter else '') + ' · ' + state
        text += ' · Receive ' + (f'{receive:.2f} Mbps' if receive is not None else '—')
        if send is not None:
            text += f' · Send {send:.2f} Mbps'
        if link is not None:
            text += f' · Link {link:.0f} Mbps'
        internet = network.get('internet_state')
        if isinstance(internet, str) and internet:
            text += ' · Internet: ' + {'os_reported_internet': 'reported available',
                'os_reported_no_internet': 'OS reports limited access', 'unverified': 'unverified'}.get(internet, internet[:80])
            age = number(network.get('internet_report_age_seconds'), 0, 100000)
            if age is not None and internet != 'unverified':
                text += f' (OS report {age:.0f}s old)'
        self.network.setText(text)
        reason = network.get('reason')
        self.network.setToolTip(str(reason or 'Passive traffic counters; internet capacity and server reachability are unverified.')[:2000])
        self.computer.setToolTip('CPU use and clock are observed counters. RAM use can explain pressure; they do not establish a universal computer speed percentage.')
