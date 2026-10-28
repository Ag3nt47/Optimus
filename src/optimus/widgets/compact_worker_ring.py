"""A small live-item ring for the pinned worker panel; progress stays native."""
from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QColor, QFont, QPainter, QPen
from PySide6.QtWidgets import QSizePolicy, QWidget

from .live_activity import WorkerActivityRing


class CompactWorkerHeader(QWidget):
    """Two measured text rows beside one fixed ring, with no cached row hints."""
    def __init__(self, heading, status, ring, parent=None):
        super().__init__(parent)
        self.heading, self.status, self.ring = heading, status, ring
        self.setMinimumWidth(0)
        self.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Fixed)
        for widget in (heading, status, ring):
            widget.setParent(self)

    def fit(self):
        height = max(self.ring.height(), self.heading.height() + self.status.height())
        if self.minimumHeight() != height:
            self.setFixedHeight(height)
        width = max(1, self.width() - self.ring.width() - 4)
        self.heading.setGeometry(0, 0, width, self.heading.height())
        self.status.setGeometry(0, self.heading.height(), width, self.status.height())
        self.ring.move(max(0, self.width() - self.ring.width()), 0)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self.fit()


class CompactWorkerActivityRing(WorkerActivityRing):
    def __init__(self, parent=None):
        super().__init__(parent)
        self._presentation_scale = 1.0
        self.setFixedSize(48, 54)

    def set_presentation_scale(self, scale):
        scale = max(1.0, min(1.6, float(scale)))
        if self._presentation_scale == scale:
            return
        self._presentation_scale = scale
        self.setFixedSize(round(48 * scale), round(54 * scale))
        self.update()

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        painter.scale(self._presentation_scale, self._presentation_scale)
        ellipse = QRectF(3, 2, 42, 42)
        pen = QPen(QColor('#263b4d'), 3)
        painter.setPen(pen)
        painter.drawEllipse(ellipse)
        pen.setColor(QColor(self.COLORS[self.health]))
        pen.setCapStyle(Qt.RoundCap)
        painter.setPen(pen)
        if self.value_percent is not None:
            painter.drawArc(ellipse, 90 * 16, -int(self.value_percent * 360 * 16 / 100))
        elif self.active:
            painter.drawArc(ellipse, int((90 - self.angle) * 16), -255 * 16)
        else:
            painter.drawEllipse(ellipse)
        font = QFont(self.font())
        font.setPixelSize(11)
        font.setBold(True)
        painter.setFont(font)
        painter.setPen(QColor('#edf6ff'))
        painter.drawText(ellipse, Qt.AlignCenter, self.value_text())
        font.setPixelSize(9)
        font.setBold(False)
        painter.setFont(font)
        caption = painter.fontMetrics().elidedText(self.phase_caption, Qt.ElideRight, 48)
        painter.drawText(QRectF(0, 44, 48, 10), Qt.AlignCenter, caption)
