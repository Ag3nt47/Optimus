"""A small live-item ring for the pinned worker panel; progress stays native."""
from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QFont, QFontMetricsF, QPainter, QPen
from PySide6.QtWidgets import QSizePolicy, QWidget

from .live_activity import WorkerActivityRing


class CompactWorkerHeader(QWidget):
    """Measured text and an optional compact footer beside one fixed ring."""
    def __init__(self, heading, status, ring, parent=None, *, footer=None):
        super().__init__(parent)
        self.heading, self.status, self.ring = heading, status, ring
        self.footer = footer
        self.setMinimumWidth(0)
        self.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Fixed)
        for widget in (heading, status, ring):
            widget.setParent(self)
        if footer is not None:
            footer.setParent(self)

    def required_height(self):
        footer = self.footer.sizeHint().height() if self.footer is not None and not self.footer.isHidden() else 0
        return max(self.ring.height(), self.heading.height() + self.status.height() + footer)

    def fit(self):
        height = self.required_height()
        if self.minimumHeight() != height:
            self.setFixedHeight(height)
        width = max(1, self.width() - self.ring.width() - 4)
        self.heading.setGeometry(0, 0, width, self.heading.height())
        self.status.setGeometry(0, self.heading.height(), width, self.status.height())
        if self.footer is not None and not self.footer.isHidden():
            self.footer.setGeometry(0, self.heading.height() + self.status.height(), width, self.footer.sizeHint().height())
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

    def caption_presentation(self):
        """Fit a complete short caption; the full phase stays accessible."""
        caption = 'Item' if self.phase_caption == 'Current item' else self.phase_caption
        aliases = {'Download': 'Get', 'Uninstall': 'Remove', 'Optimize': 'Tune',
            'Working': 'Work', 'Waiting': 'Wait', 'Devices': 'Device', 'Drivers': 'Driver'}
        rectangle = QRectF(0, 44, 48, 10)
        font = QFont(self.font())
        font.setBold(False)
        for text in (caption, aliases.get(caption, 'Item')):
            for pixels in (9, 8, 7):
                font.setPixelSize(pixels)
                bounds = QFontMetricsF(font).tightBoundingRect(text)
                if bounds.width() <= rectangle.width() and bounds.height() <= rectangle.height():
                    baseline = QPointF(rectangle.center().x() - bounds.center().x(),
                        rectangle.center().y() - bounds.center().y())
                    return {'text': text, 'font': font, 'baseline': baseline,
                        'bounds': bounds.translated(baseline), 'rectangle': rectangle}
        # All native phase captions have a short alternative; retain a bounded
        # meaningful fallback for an unexpected font or externally supplied text.
        font.setPixelSize(7)
        bounds = QFontMetricsF(font).tightBoundingRect('Item')
        baseline = QPointF(rectangle.center().x() - bounds.center().x(),
            rectangle.center().y() - bounds.center().y())
        return {'text': 'Item', 'font': font, 'baseline': baseline,
            'bounds': bounds.translated(baseline), 'rectangle': rectangle}

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
        caption = self.caption_presentation()
        painter.setFont(caption['font'])
        painter.drawText(caption['baseline'], caption['text'])
