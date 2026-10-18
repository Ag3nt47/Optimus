# Adapted from MIT-licensed Ag3nt47/AngeronaSuite; see LICENSE-Angerona.txt.
from __future__ import annotations
from PySide6.QtCore import QTimer, QRectF, Qt
from PySide6.QtGui import QColor, QPainter
from PySide6.QtWidgets import QHBoxLayout, QLabel, QWidget
class PresentationTimer(QTimer):
    def __init__(self, parent=None, **presentation_options):
        super().__init__(parent)
_C_RED = (255, 77, 77)

_C_YEL = (255, 176, 32)

_C_GRN = (47, 227, 138)

def _mix(a, b, t: float) -> QColor:
    t = max(0.0, min(1.0, t))
    return QColor(int(a[0] + (b[0] - a[0]) * t),
                  int(a[1] + (b[1] - a[1]) * t),
                  int(a[2] + (b[2] - a[2]) * t))

def color_for_pct(pct: float) -> QColor:
    """Red at 0%, warming through amber at the midpoint, to green near 100%."""
    p = max(0.0, min(100.0, float(pct))) / 100.0
    return _mix(_C_RED, _C_YEL, p / 0.5) if p < 0.5 else _mix(_C_YEL, _C_GRN, (p - 0.5) / 0.5)

class _SpinRing(QWidget):
    """A small, continuously-rotating arc — the 'it's working' wheel. Smooth
    (~30 fps) and its colour tracks the current percentage."""

    def __init__(self, parent: QWidget | None = None, d: int = 18) -> None:
        super().__init__(parent)
        self.setFixedSize(d, d)
        self._angle = 0.0
        self._color = QColor(*_C_RED)
        self._timer = PresentationTimer(self, cosmetic=True)
        self._timer.timeout.connect(self._tick)

    def start(self) -> None:
        if not self._timer.isActive():
            self._timer.start(33)
        self.show()

    def stop(self) -> None:
        self._timer.stop()

    def set_color(self, c: QColor) -> None:
        self._color = c
        self.update()

    def _tick(self) -> None:
        self._angle = (self._angle + 11.0) % 360.0
        self.update()

    def paintEvent(self, _evt) -> None:  # noqa: N802
        from PySide6.QtGui import QPen
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        w, h = self.width(), self.height()
        m = 3.0
        rect = QRectF(m, m, w - 2 * m, h - 2 * m)
        p.setPen(QPen(QColor(255, 255, 255, 30), 2.4))
        p.drawArc(rect, 0, 360 * 16)
        pen = QPen(self._color, 2.6)
        pen.setCapStyle(Qt.RoundCap)
        p.setPen(pen)
        p.drawArc(rect, int(-self._angle * 16), 105 * 16)   # a sweeping 105° arc
        p.end()

class RunSpinner(QWidget):
    """Compact 'actively running' indicator: a smooth spinning ring next to a
    live percentage, both coloured red→amber→green as progress climbs.

    Determinate use (self-test, eco wake): call ``start(label)`` then
    ``set_progress(done, total)``; ``finish()`` snaps to a green 100% and fades.
    Estimated use (a drill with no exact count): ``begin_estimated(seconds)``
    eases the bar toward 95% over the expected duration; ``finish()`` completes it.
    """

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._ring = _SpinRing(self)
        self._label = QLabel("")
        lay = QHBoxLayout(self)
        lay.setContentsMargins(8, 0, 8, 0)
        lay.setSpacing(8)
        lay.addWidget(self._ring)
        lay.addWidget(self._label)
        self._text = ""
        self._pct = 0
        self._t0 = 0.0
        self._secs = 1.0
        self._est_timer = QTimer(self)
        self._est_timer.timeout.connect(self._ease)
        self._done_timer = QTimer(self)
        self._done_timer.setSingleShot(True)
        self._done_timer.timeout.connect(self.stop)
        self._compact = False
        self.hide()

    def set_compact(self, compact: bool, extent: int = 34) -> None:
        """Keep the progress ring visible when dashboard header space is tight."""
        self._compact = bool(compact)
        self._label.setVisible(not self._compact)
        if self._compact:
            width = max(28, int(extent))
            self.layout().setContentsMargins(4, 0, 4, 0)
            self.layout().setSpacing(0)
            self.setMinimumWidth(width)
            self.setMaximumWidth(width)
        else:
            self.layout().setContentsMargins(8, 0, 8, 0)
            self.layout().setSpacing(8)
            self.setMinimumWidth(0)
            self.setMaximumWidth(16_777_215)

    def start(self, text: str = "") -> None:
        self._text = text
        self._pct = 0
        self._est_timer.stop()
        self._done_timer.stop()
        self._ring.start()
        self.show()
        self._render()

    def set_pct(self, pct: float) -> None:
        self._pct = max(0, min(100, int(round(pct))))
        self._render()

    def set_text(self, text: str) -> None:
        self._text = str(text or "")
        self._render()

    def set_progress(self, done: int, total: int) -> None:
        self.set_pct(0 if total <= 0 else done / total * 100.0)

    def begin_estimated(self, seconds: float, text: str = "") -> None:
        import time as _t
        self.start(text or self._text)
        self._secs = max(1.0, float(seconds))
        self._t0 = _t.time()
        self._est_timer.start(60)

    def _ease(self) -> None:
        import time as _t
        e = min(1.0, (_t.time() - self._t0) / self._secs)
        self.set_pct(95.0 * (1.0 - (1.0 - e) ** 2))   # ease-out toward 95%

    def finish(self, text: str | None = None) -> None:
        self._est_timer.stop()
        if text is not None:
            self._text = text
        self.set_pct(100)
        self._done_timer.start(1500)   # linger on green, then fade out

    def succeed(self, text: str = "Fixed and verified") -> None:
        """Become a persistent green checkmark until the next start/stop."""
        self._est_timer.stop()
        self._done_timer.stop()
        self._ring.stop()
        self._ring.hide()
        self._pct = 100
        self._text = text
        self._label.setText(f"✓  {text}  100%")
        self._label.setStyleSheet(
            "color:rgb(47,227,138); font-weight:800; letter-spacing:0.4px;"
        )
        self.show()

    def stop(self) -> None:
        self._est_timer.stop()
        self._done_timer.stop()
        self._ring.stop()
        self.hide()

    def _render(self) -> None:
        c = color_for_pct(self._pct)
        self._ring.set_color(c)
        label = f"{self._text}  {self._pct}%" if self._text else f"{self._pct}%"
        self._label.setText(label)
        self._label.setStyleSheet(
            f"color: rgb({c.red()},{c.green()},{c.blue()}); font-weight:700; letter-spacing:0.3px;")
        self.setToolTip(label)

