# Adapted from Ag3nt47/AngeronaSuite (MIT); see bundled NOTICE and LICENSE-Angerona.txt.
"""Always-on-top holographic Optimus token and minimize transitions.

The orb is deliberately presentation-only.  It paints from Qt primitives,
reads no host telemetry, and starts no background process.  Minimized Optimus
windows are hidden after their normal geometry is remembered, represented by a
short outline-to-line-to-dot animation, and restored through the reverse path.
"""
from __future__ import annotations

import math
import weakref
from dataclasses import dataclass
from typing import Callable

from PySide6.QtCore import (
    QEasingCurve,
    QEvent,
    QObject,
    QPoint,
    QPointF,
    Property,
    QPropertyAnimation,
    QRect,
    QRectF,
    Qt,
    QTimer,
    Signal,
)
from PySide6.QtGui import (
    QColor,
    QCursor,
    QFont,
    QGuiApplication,
    QLinearGradient,
    QMouseEvent,
    QPainter,
    QPainterPath,
    QPen,
    QRadialGradient,
)
from PySide6.QtWidgets import (
    QApplication,
    QMainWindow,
    QMenu,
    QToolTip,
    QWidget,
)
from shiboken6 import isValid

from optimus.widgets.header_controls import motion_allowed


_CYAN = QColor("#38bdf8")
_BLUE = QColor("#1f9cff")
_MINT = QColor("#2fe38a")
_VIOLET = QColor("#c084fc")
_AMBER = QColor("#fbbf24")
_INK = QColor("#07111f")


@dataclass(frozen=True)
class OrbService:
    key: str
    label: str
    description: str
    color: QColor


_SERVICES = (
    OrbService(
        "core",
        "OPTIMUS",
        "Restore the Optimus dashboard.",
        _CYAN,
    ),
    OrbService(
        "watchdog",
        "PERFORMANCE",
        "Review application memory and CPU controls.",
        _MINT,
    ),
    OrbService(
        "scanner",
        "CLEANER",
        "Preview temporary files and browser cache cleanup.",
        _VIOLET,
    ),
    OrbService(
        "blackbox",
        "HISTORY",
        "Review recorded changes and restore supported settings.",
        _AMBER,
    ),
)


def _lerp(a: float, b: float, progress: float) -> float:
    return a + (b - a) * max(0.0, min(1.0, progress))


def _ease(progress: float) -> float:
    value = max(0.0, min(1.0, progress))
    return 1.0 - (1.0 - value) ** 3


def _virtual_desktop() -> QRect:
    screens = QGuiApplication.screens()
    if not screens:
        return QRect(0, 0, 1920, 1080)
    result = QRect(screens[0].geometry())
    for screen in screens[1:]:
        result = result.united(screen.geometry())
    return result


class CollapseTrail(QWidget):
    """Click-through holographic outline used for collapse and restore."""

    completed = Signal()
    ANIMATION_DURATION_MS = 440
    COMPLETION_GRACE_MS = 750

    def __init__(
        self,
        source: QRect,
        destination: QPoint,
        *,
        expanding: bool = False,
        color: QColor = _CYAN,
    ) -> None:
        flags = Qt.Tool | Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint
        transparent_input = getattr(Qt, "WindowTransparentForInput", None)
        if transparent_input is not None:
            flags |= transparent_input
        super().__init__(None, flags)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setAttribute(Qt.WA_TransparentForMouseEvents)
        self.setAttribute(Qt.WA_ShowWithoutActivating)
        self.setProperty("_optimus_orb_ignore", True)
        self._desktop = _virtual_desktop()
        self.setGeometry(self._desktop)
        self._source = QRect(source).translated(-self._desktop.topLeft())
        self._destination = QPoint(destination) - self._desktop.topLeft()
        self._expanding = bool(expanding)
        self._color = QColor(color)
        self._progress = 0.0
        self._completed = False
        self._animation = QPropertyAnimation(self, b"trailProgress", self)
        self._animation.setDuration(self.ANIMATION_DURATION_MS)
        self._animation.setEasingCurve(QEasingCurve.InOutCubic)
        self._animation.setStartValue(0.0)
        self._animation.setEndValue(1.0)
        self._animation.finished.connect(self._finish)
        # QPropertyAnimation normally emits ``finished`` after 440 ms.  A
        # heavily loaded GUI thread, display-driver transition, or an external
        # stop can leave that signal delayed or absent, though.  Restoring a
        # hidden window must not depend on a presentation-only animation, so a
        # separate one-shot guard completes the same idempotent path.
        self._completion_guard = QTimer(self)
        self._completion_guard.setSingleShot(True)
        self._completion_guard.timeout.connect(self._finish)

    def animated_rect(self, progress: float | None = None) -> QRectF:
        """Return the current outline geometry in overlay-local coordinates."""
        t = self._progress if progress is None else float(progress)
        if self._expanding:
            t = 1.0 - t
        t = max(0.0, min(1.0, t))
        start = QRectF(self._source)
        start_center = start.center()
        end = QPointF(self._destination)
        line_height = max(34.0, min(180.0, start.height() * 0.34))
        if t <= 0.48:
            local = _ease(t / 0.48)
            width = _lerp(start.width(), 4.0, local)
            height = _lerp(start.height(), line_height, local)
            center = start_center
        else:
            local = _ease((t - 0.48) / 0.52)
            width = _lerp(4.0, 3.0, local)
            height = _lerp(line_height, 3.0, local)
            center = QPointF(
                _lerp(start_center.x(), end.x(), local),
                _lerp(start_center.y(), end.y(), local),
            )
        return QRectF(
            center.x() - width / 2.0,
            center.y() - height / 2.0,
            width,
            height,
        )

    def start(self) -> None:
        if self._completed:
            return
        self.show()
        self.raise_()
        self._completion_guard.start(
            self.ANIMATION_DURATION_MS + self.COMPLETION_GRACE_MS
        )
        self._animation.start()

    def get_trail_progress(self) -> float:
        return self._progress

    def set_trail_progress(self, value: float) -> None:
        self._progress = max(0.0, min(1.0, float(value)))
        self.update()

    trailProgress = Property(
        float,
        get_trail_progress,
        set_trail_progress,
    )

    def _finish(self) -> None:
        if self._completed:
            return
        self._completed = True
        self._completion_guard.stop()
        self._animation.stop()
        self.set_trail_progress(1.0)
        self.hide()
        self.completed.emit()
        self.deleteLater()

    def paintEvent(self, _event) -> None:  # noqa: N802 - Qt signature
        rect = self.animated_rect()
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)

        color = QColor(self._color)
        color.setAlpha(230)
        glow = QColor(self._color)
        glow.setAlpha(48)
        painter.setBrush(Qt.NoBrush)
        painter.setPen(QPen(glow, 10.0))
        painter.drawRoundedRect(rect, 8.0, 8.0)
        painter.setPen(QPen(color, 2.2))
        painter.drawRoundedRect(rect, 7.0, 7.0)

        center = rect.center()
        if rect.width() <= 8.0:
            tail = QColor(self._color)
            tail.setAlpha(95)
            painter.setPen(QPen(tail, 1.1, Qt.DashLine))
            painter.drawLine(center, QPointF(self._destination))

        dot = QColor("#e0f7ff")
        dot.setAlpha(max(0, round(235 * self._progress)))
        painter.setPen(Qt.NoPen)
        painter.setBrush(dot)
        painter.drawEllipse(QPointF(self._destination), 2.3, 2.3)
        painter.end()


class HolographicOrb(QWidget):
    """Draggable spinning globe with an inward-facing radial service menu."""

    serviceTriggered = Signal(str)
    restoreRecentRequested = Signal()
    hiddenByOperator = Signal()

    _COLLAPSED_SIZE = 116
    _GLOBE_RADIUS = 40.0
    _NODE_RADIUS = 22.0

    def __init__(self, config=None) -> None:
        flags = Qt.Tool | Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint
        super().__init__(None, flags)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setAttribute(Qt.WA_ShowWithoutActivating)
        self.setMouseTracking(True)
        self.setFocusPolicy(Qt.StrongFocus)
        self.setAccessibleName("Optimus holographic orb")
        self.setAccessibleDescription(
            "A minimized Optimus token. Activate it to open Core, Watchdog, "
            "Scanner, and Black Box controls."
        )
        self.setToolTip(
            "Optimus Orb — click for Core, Watchdog, Scanner, and Black Box. "
            "Double-click to restore the most recently minimized window."
        )
        self.setProperty("_optimus_orb_ignore", True)
        self._config = config
        self._phase = 0.0
        self._pulse = 0.0
        self._menu_progress = 0.0
        self._menu_expanded = False
        self._anchor = QPoint(-1, -1)
        self._orb_center = QPointF()
        self._node_centers: dict[str, QPointF] = {}
        self._node_rects: dict[str, QRectF] = {}
        self._hover_key = ""
        self._press_global = QPoint()
        self._press_local = QPoint()
        self._drag_anchor_start = QPoint()
        self._dragging = False
        self._tick = QTimer(self)
        self._tick.setInterval(50)  # 20 FPS: smooth enough, negligible idle work.
        self._tick.timeout.connect(self._advance)
        self._menu_animation = QPropertyAnimation(self, b"menuProgress", self)
        self._menu_animation.setDuration(260)
        self._menu_animation.setEasingCurve(QEasingCurve.OutCubic)
        self._menu_animation.finished.connect(self._menu_animation_finished)
        self.hide()

    @property
    def services(self) -> tuple[OrbService, ...]:
        return _SERVICES

    @property
    def anchor(self) -> QPoint:
        return QPoint(self._anchor)

    def _motion_enabled(self) -> bool:
        return motion_allowed(self._config)

    @staticmethod
    def _distance_to_screen(point: QPoint, screen) -> int:
        """Squared distance from a global point to a screen's usable rectangle."""
        available = screen.availableGeometry()
        nearest_x = max(available.left(), min(point.x(), available.right()))
        nearest_y = max(available.top(), min(point.y(), available.bottom()))
        return (point.x() - nearest_x) ** 2 + (point.y() - nearest_y) ** 2

    def _screen_for_point(self, point: QPoint):
        """Return the containing or nearest screen for any global coordinate.

        Monitor layouts can contain gaps and can extend left/up into negative
        coordinates. Falling back to the orb's previous screen while the mouse
        crossed such a gap trapped the orb on that display.
        """
        direct = QGuiApplication.screenAt(point)
        if direct is not None:
            return direct
        screens = list(QGuiApplication.screens())
        if screens:
            return min(screens, key=lambda item: self._distance_to_screen(point, item))
        return QGuiApplication.primaryScreen()

    def _screen(self):
        if self._anchor == QPoint(-1, -1):
            return self._screen_for_point(QCursor.pos())
        return self._screen_for_point(self._anchor)

    def _default_anchor(self) -> QPoint:
        screen = self._screen_for_point(QCursor.pos())
        if screen is None:
            return QPoint(1840, 960)
        available = screen.availableGeometry()
        return QPoint(available.right() - 66, available.bottom() - 66)

    def _saved_anchor(self) -> QPoint:
        try:
            x = int(getattr(self._config, "holographic_orb_x", -1))
            y = int(getattr(self._config, "holographic_orb_y", -1))
            # (-1, -1) is the legacy "not positioned" sentinel. Other negative
            # coordinates are valid on monitors placed left of or above primary.
            if (x, y) != (-1, -1):
                return QPoint(x, y)
        except (TypeError, ValueError):
            pass
        return self._default_anchor()

    def _clamp_anchor(self, point: QPoint) -> QPoint:
        screen = self._screen_for_point(point)
        if screen is None:
            return QPoint(point)
        available = screen.availableGeometry()
        margin = self._COLLAPSED_SIZE // 2 + 6
        return QPoint(
            max(available.left() + margin, min(point.x(), available.right() - margin)),
            max(available.top() + margin, min(point.y(), available.bottom() - margin)),
        )

    def show_token(self) -> None:
        if self._anchor == QPoint(-1, -1):
            self._anchor = self._clamp_anchor(self._saved_anchor())
        self._apply_geometry(expanded=self._menu_expanded)
        self.show()
        self.raise_()
        if self._motion_enabled():
            self._tick.start()
        else:
            self._tick.stop()
            self._phase = 0.55
            self._pulse = 0.0
        self.update()

    def hide_token(self) -> None:
        self._tick.stop()
        self._menu_animation.stop()
        self._menu_expanded = False
        self._menu_progress = 0.0
        self.hide()

    def set_anchor(self, point: QPoint, *, persist: bool = False) -> None:
        self._anchor = self._clamp_anchor(point)
        self._apply_geometry(expanded=self._menu_expanded)
        self.update()
        if persist and self._config is not None:
            try:
                self._config.holographic_orb_x = self._anchor.x()
                self._config.holographic_orb_y = self._anchor.y()
                self._config.save()
            except Exception:
                pass

    def _node_offsets(self) -> dict[str, QPointF]:
        screen = self._screen()
        if screen is None:
            center = QPointF(960.0, 540.0)
            available = QRect(0, 0, 1920, 1080)
        else:
            available = screen.availableGeometry()
            center = QPointF(available.center())
        direction = math.atan2(center.y() - self._anchor.y(), center.x() - self._anchor.x())
        offsets: dict[str, QPointF] = {}
        for index, service in enumerate(_SERVICES):
            delta = math.radians(-48.0 + index * 32.0)
            radius = 150.0 + (12.0 if index in (1, 2) else 0.0)
            angle = direction + delta
            candidate = QPointF(
                self._anchor.x() + radius * math.cos(angle),
                self._anchor.y() + radius * math.sin(angle),
            )
            # Keep the complete icon and its name chip on-screen even when the
            # orb is dragged into a corner. This preserves the radial direction
            # while preventing an edge-clipped destination.
            candidate.setX(
                max(available.left() + 54, min(candidate.x(), available.right() - 54))
            )
            candidate.setY(
                max(available.top() + 34, min(candidate.y(), available.bottom() - 52))
            )
            offsets[service.key] = QPointF(
                candidate.x() - self._anchor.x(),
                candidate.y() - self._anchor.y(),
            )
        return offsets

    def _expanded_geometry(self) -> QRect:
        points = [QPointF(self._anchor)]
        for offset in self._node_offsets().values():
            points.append(QPointF(self._anchor) + offset)
        left = min(point.x() for point in points) - 82
        right = max(point.x() for point in points) + 82
        top = min(point.y() for point in points) - 66
        bottom = max(point.y() for point in points) + 66
        desired = QRect(
            math.floor(left),
            math.floor(top),
            math.ceil(right - left),
            math.ceil(bottom - top),
        )
        screen = self._screen()
        if screen is None:
            return desired
        available = screen.availableGeometry()
        # The vectors point toward the screen center, so this normally only
        # trims label breathing room at an extreme edge.
        return desired.intersected(available.adjusted(2, 2, -2, -2))

    def _apply_geometry(self, *, expanded: bool) -> None:
        if expanded:
            self.setGeometry(self._expanded_geometry())
        else:
            half = self._COLLAPSED_SIZE // 2
            self.setGeometry(
                self._anchor.x() - half,
                self._anchor.y() - half,
                self._COLLAPSED_SIZE,
                self._COLLAPSED_SIZE,
            )
        self._orb_center = QPointF(self.mapFromGlobal(self._anchor))

    def set_menu_expanded(self, expanded: bool, *, animated: bool = True) -> None:
        requested = bool(expanded)
        if requested == self._menu_expanded and (
            (requested and self._menu_progress >= 1.0)
            or (not requested and self._menu_progress <= 0.0)
        ):
            return
        self._menu_expanded = requested
        if requested:
            self._apply_geometry(expanded=True)
            self.raise_()
        self._menu_animation.stop()
        target = 1.0 if requested else 0.0
        if animated and self._motion_enabled():
            self._menu_animation.setStartValue(self._menu_progress)
            self._menu_animation.setEndValue(target)
            self._menu_animation.start()
        else:
            self.set_menu_progress(target)
            self._menu_animation_finished()

    def toggle_menu(self) -> None:
        self.set_menu_expanded(not self._menu_expanded)

    def get_menu_progress(self) -> float:
        return self._menu_progress

    def set_menu_progress(self, value: float) -> None:
        self._menu_progress = max(0.0, min(1.0, float(value)))
        self.update()

    menuProgress = Property(
        float,
        get_menu_progress,
        set_menu_progress,
    )

    def _menu_animation_finished(self) -> None:
        if not self._menu_expanded and self._menu_progress <= 0.0:
            self._apply_geometry(expanded=False)
        self.update()

    def _advance(self) -> None:
        if not self.isVisible():
            self._tick.stop()
            return
        self._phase = (self._phase + 0.055) % (math.tau)
        self._pulse = (self._pulse + 0.075) % (math.tau)
        self.update()

    def _paint_globe(self, painter: QPainter) -> None:
        center = self._orb_center
        radius = self._GLOBE_RADIUS
        pulse = (math.sin(self._pulse) + 1.0) / 2.0

        glow = QRadialGradient(center, radius + 17.0)
        glow.setColorAt(0.0, QColor(19, 156, 255, 32))
        glow.setColorAt(0.68, QColor(36, 203, 255, 24 + round(pulse * 15)))
        glow.setColorAt(1.0, QColor(20, 125, 255, 0))
        painter.setPen(Qt.NoPen)
        painter.setBrush(glow)
        painter.drawEllipse(center, radius + 17.0, radius + 17.0)

        shell = QRadialGradient(
            QPointF(center.x() - 11.0, center.y() - 13.0),
            radius * 1.35,
        )
        shell.setColorAt(0.0, QColor(57, 210, 255, 56))
        shell.setColorAt(0.50, QColor(7, 28, 51, 210))
        shell.setColorAt(1.0, QColor(2, 10, 25, 242))
        painter.setBrush(shell)
        painter.setPen(QPen(QColor(83, 218, 255, 225), 1.7))
        painter.drawEllipse(center, radius, radius)

        painter.save()
        clip = QPainterPath()
        clip.addEllipse(center, radius - 1.0, radius - 1.0)
        painter.setClipPath(clip)
        grid = QColor(71, 210, 255, 120)
        painter.setBrush(Qt.NoBrush)
        painter.setPen(QPen(grid, 1.0))

        for offset in (0.0, math.pi / 3.0, 2.0 * math.pi / 3.0):
            angle = self._phase + offset
            width = max(4.0, abs(math.cos(angle)) * radius * 1.95)
            painter.drawEllipse(
                QRectF(
                    center.x() - width / 2.0,
                    center.y() - radius,
                    width,
                    radius * 2.0,
                )
            )
        for latitude in (-0.55, 0.0, 0.55):
            y = center.y() + latitude * radius
            half_width = math.sqrt(max(0.0, 1.0 - latitude * latitude)) * radius
            height = max(5.0, 12.0 * (1.0 - abs(latitude)))
            painter.drawEllipse(
                QRectF(
                    center.x() - half_width,
                    y - height / 2.0,
                    half_width * 2.0,
                    height,
                )
            )

        sweep = QLinearGradient(
            center.x() - radius,
            center.y(),
            center.x() + radius,
            center.y(),
        )
        sweep.setColorAt(0.0, QColor(47, 227, 138, 0))
        sweep.setColorAt(0.50 + 0.34 * math.sin(self._phase), QColor(47, 227, 138, 90))
        sweep.setColorAt(1.0, QColor(47, 227, 138, 0))
        painter.setPen(QPen(sweep, 1.6))
        painter.drawLine(
            QPointF(center.x() - radius, center.y()),
            QPointF(center.x() + radius, center.y()),
        )
        painter.restore()

        orbit_angle = self._phase * 1.8
        orbit = QPointF(
            center.x() + math.cos(orbit_angle) * (radius + 6.0),
            center.y() + math.sin(orbit_angle) * (radius + 6.0) * 0.38,
        )
        painter.setPen(Qt.NoPen)
        painter.setBrush(QColor("#d9fbff"))
        painter.drawEllipse(orbit, 2.1, 2.1)

        painter.setPen(QColor("#e6faff"))
        font = QFont("Consolas", 7)
        font.setBold(True)
        font.setLetterSpacing(QFont.AbsoluteSpacing, 0.8)
        painter.setFont(font)
        painter.drawText(
            QRectF(center.x() - 33, center.y() - 7, 66, 14),
            Qt.AlignCenter,
            "ANGERONA",
        )

    def _draw_service_icon(
        self,
        painter: QPainter,
        service: OrbService,
        center: QPointF,
        alpha: int,
    ) -> None:
        color = QColor(service.color)
        color.setAlpha(alpha)
        painter.setPen(QPen(color, 1.7))
        painter.setBrush(Qt.NoBrush)
        x, y = center.x(), center.y()
        if service.key == "core":
            path = QPainterPath()
            path.moveTo(x, y - 10)
            path.lineTo(x + 10, y)
            path.lineTo(x, y + 10)
            path.lineTo(x - 10, y)
            path.closeSubpath()
            painter.drawPath(path)
            painter.drawEllipse(center, 3.5, 3.5)
            for dx, dy in ((0, -15), (15, 0), (0, 15), (-15, 0)):
                painter.drawLine(
                    QPointF(x + dx * 0.68, y + dy * 0.68),
                    QPointF(x + dx, y + dy),
                )
        elif service.key == "watchdog":
            shield = QPainterPath()
            shield.moveTo(x, y - 12)
            shield.lineTo(x + 11, y - 7)
            shield.lineTo(x + 9, y + 6)
            shield.quadTo(x, y + 14, x - 9, y + 6)
            shield.lineTo(x - 11, y - 7)
            shield.closeSubpath()
            painter.drawPath(shield)
            painter.drawEllipse(QRectF(x - 5.5, y - 4.0, 11.0, 7.0))
            painter.drawEllipse(center, 1.8, 1.8)
        elif service.key == "scanner":
            painter.drawArc(QRectF(x - 12, y - 12, 24, 24), 10 * 16, 140 * 16)
            painter.drawArc(QRectF(x - 8, y - 8, 16, 16), 10 * 16, 140 * 16)
            painter.drawLine(QPointF(x, y), QPointF(x + 10, y - 7))
            painter.drawEllipse(center, 2.0, 2.0)
        else:
            front = QRectF(x - 10, y - 8, 20, 17)
            painter.drawRoundedRect(front, 2.0, 2.0)
            painter.drawLine(QPointF(x - 10, y - 8), QPointF(x - 5, y - 13))
            painter.drawLine(QPointF(x + 10, y - 8), QPointF(x + 5, y - 13))
            painter.drawLine(QPointF(x - 5, y - 13), QPointF(x + 5, y - 13))
            painter.drawLine(QPointF(x, y - 13), QPointF(x, y + 9))

    def _paint_services(self, painter: QPainter) -> None:
        progress = _ease(self._menu_progress)
        self._node_centers.clear()
        self._node_rects.clear()
        if progress <= 0.01:
            return
        offsets = self._node_offsets()
        for index, service in enumerate(_SERVICES):
            offset = offsets[service.key]
            center = self._orb_center + offset * progress
            self._node_centers[service.key] = center
            rect = QRectF(
                center.x() - self._NODE_RADIUS,
                center.y() - self._NODE_RADIUS,
                self._NODE_RADIUS * 2,
                self._NODE_RADIUS * 2,
            )
            self._node_rects[service.key] = rect.adjusted(-5, -5, 5, 5)
            alpha = max(0, min(255, round((progress - 0.18) / 0.82 * 255)))

            line_start = self._orb_center + offset * (self._GLOBE_RADIUS / max(1.0, math.hypot(offset.x(), offset.y())))
            line_end = center - offset * (self._NODE_RADIUS / max(1.0, math.hypot(offset.x(), offset.y())))
            gradient = QLinearGradient(line_start, line_end)
            start_color = QColor("#38bdf8")
            start_color.setAlpha(max(0, alpha // 3))
            end_color = QColor(service.color)
            end_color.setAlpha(alpha)
            gradient.setColorAt(0.0, start_color)
            gradient.setColorAt(1.0, end_color)
            painter.setPen(QPen(gradient, 1.5))
            painter.drawLine(line_start, line_end)

            hover = self._hover_key == service.key
            halo = QColor(service.color)
            halo.setAlpha(min(115, alpha // (1 if hover else 2)))
            painter.setPen(QPen(halo, 5.5 if hover else 3.0))
            painter.setBrush(QColor(4, 15, 30, min(235, alpha)))
            painter.drawEllipse(rect)
            ring = QColor(service.color)
            ring.setAlpha(alpha)
            painter.setPen(QPen(ring, 1.5))
            painter.drawEllipse(rect)
            self._draw_service_icon(painter, service, center, alpha)

            if progress >= 0.68:
                label_alpha = round((progress - 0.68) / 0.32 * 230)
                dx = center.x() - self._orb_center.x()
                dy = center.y() - self._orb_center.y()
                if service.key == "watchdog":
                    # Put the watchdog name between its node and the globe.
                    label_rect = QRectF(
                        center.x() + (28 if dx < 0 else -122),
                        center.y() - 9.5,
                        94,
                        19,
                    )
                elif service.key == "scanner":
                    # Put the scanner name on the outer side of its radar node.
                    label_rect = QRectF(
                        center.x() + (-122 if dx < 0 else 28),
                        center.y() - 9.5,
                        94,
                        19,
                    )
                else:
                    # Core and Black Box labels sit beyond their nodes, away
                    # from the globe, so adjacent icons remain unobstructed.
                    label_rect = QRectF(
                        center.x() - 47,
                        center.y() + (-46 if dy < 0 else 27),
                        94,
                        19,
                    )
                painter.setPen(QPen(QColor(66, 190, 255, label_alpha // 2), 1.0))
                painter.setBrush(QColor(3, 12, 26, min(220, label_alpha)))
                painter.drawRoundedRect(label_rect, 6.0, 6.0)
                text_color = QColor("#dff8ff")
                text_color.setAlpha(label_alpha)
                painter.setPen(text_color)
                label_font = QFont("Consolas", 7)
                label_font.setBold(True)
                painter.setFont(label_font)
                painter.drawText(label_rect, Qt.AlignCenter, service.label)

    def paintEvent(self, _event) -> None:  # noqa: N802 - Qt signature
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        self._paint_services(painter)
        self._paint_globe(painter)
        painter.end()

    def _orb_hit(self, point: QPointF) -> bool:
        delta = point - self._orb_center
        return math.hypot(delta.x(), delta.y()) <= self._GLOBE_RADIUS + 10.0

    def _service_at(self, point: QPointF) -> str:
        for key, rect in self._node_rects.items():
            if rect.contains(point):
                return key
        return ""

    def mousePressEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        if event.button() == Qt.LeftButton:
            self._press_global = event.globalPosition().toPoint()
            self._press_local = event.position().toPoint()
            self._drag_anchor_start = QPoint(self._anchor)
            self._dragging = False
            event.accept()
            return
        if event.button() == Qt.RightButton:
            menu = QMenu()
            open_action = menu.addAction("Open radial controls")
            recent_action = menu.addAction("Restore recent window")
            menu.addSeparator()
            hide_action = menu.addAction("Hide orb until the next minimize")
            chosen = menu.exec(event.globalPosition().toPoint())
            if chosen is open_action:
                self.set_menu_expanded(True)
            elif chosen is recent_action:
                self.restoreRecentRequested.emit()
            elif chosen is hide_action:
                self.hiddenByOperator.emit()
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        if event.buttons() & Qt.LeftButton and not self._menu_expanded:
            current = event.globalPosition().toPoint()
            distance = current - self._press_global
            if self._dragging or distance.manhattanLength() >= 5:
                self._dragging = True
                # Track the total pointer displacement from mouse-down. If the
                # anchor were incremented and clamped on every small move, it
                # would remain pinned to the old screen edge while the pointer
                # crossed a monitor gap.
                self.set_anchor(self._drag_anchor_start + distance)
                event.accept()
                return
        key = self._service_at(event.position())
        if key != self._hover_key:
            self._hover_key = key
            if key:
                service = next(item for item in _SERVICES if item.key == key)
                QToolTip.showText(
                    event.globalPosition().toPoint(),
                    f"<b>{service.label}</b><br>{service.description}",
                    self,
                )
            else:
                QToolTip.hideText()
            self.update()
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        if event.button() != Qt.LeftButton:
            super().mouseReleaseEvent(event)
            return
        if self._dragging:
            self.set_anchor(self._anchor, persist=True)
            self._dragging = False
            event.accept()
            return
        point = event.position()
        key = self._service_at(point)
        if key:
            self.set_menu_expanded(False)
            self.serviceTriggered.emit(key)
        elif self._orb_hit(point):
            self.toggle_menu()
        event.accept()

    def mouseDoubleClickEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        if event.button() == Qt.LeftButton and self._orb_hit(event.position()):
            self.restoreRecentRequested.emit()
            event.accept()
            return
        super().mouseDoubleClickEvent(event)

    def keyPressEvent(self, event) -> None:  # noqa: N802
        if event.key() in (Qt.Key_Return, Qt.Key_Enter, Qt.Key_Space):
            self.toggle_menu()
            event.accept()
            return
        if event.key() == Qt.Key_Escape:
            self.set_menu_expanded(False)
            event.accept()
            return
        super().keyPressEvent(event)


class HolographicOrbController(QObject):
    """Coordinates top-level Optimus windows with the holographic orb."""

    def __init__(self, main_window: QMainWindow, config=None) -> None:
        super().__init__(main_window)
        self.main_window = main_window
        self.config = config
        self.orb = HolographicOrb(config)
        self.orb.serviceTriggered.connect(self._service_triggered)
        self.orb.restoreRecentRequested.connect(self.restore_recent)
        self.orb.hiddenByOperator.connect(self.orb.hide_token)
        self._normal_geometries: weakref.WeakKeyDictionary[QWidget, QRect] = (
            weakref.WeakKeyDictionary()
        )
        self._collapsed: list[weakref.ReferenceType[QWidget]] = []
        self._component_windows: dict[str, QMainWindow] = {}
        self._trails: list[CollapseTrail] = []
        self._shutting_down = False
        self._enabled = bool(getattr(config, "holographic_orb_enabled", True))
        app = QApplication.instance()
        if app is not None:
            app.installEventFilter(self)
        self._remember_geometry(main_window)

    def enabled(self) -> bool:
        return self._enabled and not self._shutting_down

    def sync_config(self) -> None:
        requested = bool(getattr(self.config, "holographic_orb_enabled", True))
        if self._enabled and not requested:
            self.restore_all(immediate=True)
            self.orb.hide_token()
        self._enabled = requested
        if self.orb.isVisible():
            self.orb.show_token()

    def _is_managed_window(self, watched) -> bool:
        if (not isinstance(watched, QWidget) or not isValid(watched)
                or not watched.isWindow()):
            return False
        if watched is self.orb or bool(watched.property("_optimus_orb_ignore")):
            return False
        if watched.windowType() in (Qt.Popup, Qt.ToolTip, Qt.SplashScreen):
            return False
        if watched is self.main_window:
            return True
        if watched in self._component_windows.values():
            return True
        parent = watched.parentWidget()
        while parent is not None:
            if parent is self.main_window:
                return True
            parent = parent.parentWidget()
        # Optimus owns the QApplication and does not embed unrelated product
        # windows. Managing the remaining ordinary top-level Qt window is what
        # makes "minimize any Optimus window" consistent even for a future
        # plugin dialog that deliberately has no parent.
        return True

    def eventFilter(self, watched, event):  # noqa: N802 - Qt signature
        if self._shutting_down:
            return False
        event_type = event.type()
        # QApplication filters also see construction/destruction traffic.
        # isValid() can still be true during QWidget's Destroy event, when its
        # native window state is already being dismantled. Drop Python-only
        # bookkeeping without calling isWindow/property/windowType/parentWidget.
        if event_type == QEvent.Destroy:
            self._drop_window(watched)
            return False
        if event_type not in (
            QEvent.Show, QEvent.Move, QEvent.Resize, QEvent.WindowStateChange,
        ):
            return False
        if not self._is_managed_window(watched):
            return False
        if event_type in (QEvent.Show, QEvent.Move, QEvent.Resize):
            if not (watched.windowState() & Qt.WindowMinimized):
                self._remember_geometry(watched)
        elif event_type == QEvent.WindowStateChange and self.enabled():
            if watched.windowState() & Qt.WindowMinimized:
                self._defer_collapse(watched)
        return False

    def _defer_collapse(self, window: QWidget) -> None:
        # A dialog can be restored or deleted before this deferred action runs.
        # Qt cancels the callback with its target context; weak references also
        # avoid keeping a deleted widget's Python wrapper alive until delivery.
        # A context-free callback could otherwise reach recycled native widget
        # state and corrupt a later paint/garbage-collection pass.
        window_ref, controller_ref = weakref.ref(window), weakref.ref(self)

        def collapse() -> None:
            target, controller = window_ref(), controller_ref()
            if (target is None or controller is None
                    or not isValid(target) or not isValid(controller)):
                return
            if target.windowState() & Qt.WindowMinimized:
                controller.collapse_window(target)

        QTimer.singleShot(0, window, collapse)

    def _remember_geometry(self, window: QWidget) -> None:
        try:
            geometry = window.geometry()
            if geometry.width() > 80 and geometry.height() > 60:
                # Store client geometry for exact restoration. Re-applying
                # frameGeometry as client geometry would grow a decorated
                # window by its title-bar/border thickness on every cycle.
                self._normal_geometries[window] = QRect(geometry)
        except RuntimeError:
            pass

    def _drop_window(self, window: QWidget) -> None:
        self._collapsed = [
            ref for ref in self._collapsed if ref() is not None and ref() is not window
        ]
        self._normal_geometries.pop(window, None)

    def _collapsed_windows(self) -> list[QWidget]:
        live: list[QWidget] = []
        kept: list[weakref.ReferenceType[QWidget]] = []
        for reference in self._collapsed:
            window = reference()
            if window is not None:
                live.append(window)
                kept.append(reference)
        self._collapsed = kept
        return live

    def is_collapsed(self, window: QWidget) -> bool:
        return any(item is window for item in self._collapsed_windows())

    def _add_collapsed(self, window: QWidget) -> None:
        if not self.is_collapsed(window):
            self._collapsed.append(weakref.ref(window))

    def _remove_collapsed(self, window: QWidget) -> None:
        self._collapsed = [
            ref for ref in self._collapsed if ref() is not None and ref() is not window
        ]

    def _orb_destination(self) -> QPoint:
        if self.orb.anchor == QPoint(-1, -1):
            self.orb.set_anchor(self.orb._saved_anchor())
        return self.orb.anchor

    def _start_trail(
        self,
        source: QRect,
        destination: QPoint,
        *,
        expanding: bool,
        finished: Callable[[], None] | None = None,
    ) -> None:
        trail = CollapseTrail(source, destination, expanding=expanding)
        self._trails.append(trail)

        def _done() -> None:
            try:
                self._trails.remove(trail)
            except ValueError:
                pass
            if finished is not None:
                finished()

        trail.completed.connect(_done)
        trail.start()

    def collapse_window(self, window: QWidget) -> None:
        if not self.enabled() or self._shutting_down:
            return
        if window is self.orb or self.is_collapsed(window):
            return
        self._remember_geometry(window)
        source = QRect(window.frameGeometry())
        if source.isEmpty():
            source = QRect(self._normal_geometries.get(window, window.geometry()))
        if source.isEmpty():
            return
        self._add_collapsed(window)
        try:
            window.hide()
            window.setWindowState(window.windowState() & ~Qt.WindowMinimized)
        except RuntimeError:
            self._drop_window(window)
            return
        destination = self._orb_destination()
        self.orb.set_menu_expanded(False, animated=False)
        if motion_allowed(self.config):
            QTimer.singleShot(250, self.orb.show_token)
            self._start_trail(source, destination, expanding=False)
        else:
            self.orb.show_token()

    def _show_window(self, window: QWidget, geometry: QRect | None = None) -> None:
        try:
            window.setWindowState(Qt.WindowNoState)
            if geometry is not None and not geometry.isEmpty():
                # Stored client geometry prevents title-bar/border inflation
                # across repeated collapse/restore cycles.
                window.setGeometry(geometry)
            window.showNormal()
            window.raise_()
            window.activateWindow()
            self._remember_geometry(window)
        except RuntimeError:
            self._drop_window(window)

    def restore_window(self, window: QWidget, *, immediate: bool = False) -> None:
        geometry = QRect(
            self._normal_geometries.get(window, window.frameGeometry())
        )
        self._remove_collapsed(window)

        def _finished() -> None:
            self._show_window(window, geometry)
            if not self._collapsed_windows():
                self.orb.hide_token()

        if immediate or not motion_allowed(self.config):
            _finished()
            return
        self.orb.set_menu_expanded(False)
        self._start_trail(
            geometry,
            self._orb_destination(),
            expanding=True,
            finished=_finished,
        )

    def restore_recent(self) -> None:
        windows = self._collapsed_windows()
        if windows:
            self.restore_window(windows[-1])
        else:
            self.restore_main()

    def restore_main(self) -> None:
        if self.is_collapsed(self.main_window) or not self.main_window.isVisible():
            self.restore_window(self.main_window)
        else:
            self._show_window(
                self.main_window,
                self._normal_geometries.get(self.main_window),
            )
            if not self._collapsed_windows():
                self.orb.hide_token()

    def restore_all(self, *, immediate: bool = False) -> None:
        for window in list(self._collapsed_windows()):
            self.restore_window(window, immediate=immediate)

    def _service_triggered(self, key: str) -> None:
        self.restore_main()
        pages = {"core": "Dashboard", "watchdog": "Performance", "scanner": "Cleaner", "blackbox": "Restore/history"}
        navigate = getattr(self.main_window, "select_page", None)
        if callable(navigate) and key in pages:
            navigate(pages[key])
    def shutdown(self) -> None:
        self._shutting_down = True
        app = QApplication.instance()
        if app is not None:
            app.removeEventFilter(self)
        self.orb.hide_token()
        self.orb.deleteLater()
        for trail in list(self._trails):
            trail.hide()
            trail.deleteLater()
        self._trails.clear()

