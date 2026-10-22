# Adapted from Ag3nt47/AngeronaSuite (MIT); see bundled NOTICE and LICENSE-Angerona.txt.
"""Polished header controls and low-cost panel reveal animation.

The header intentionally uses vector icons drawn by Qt instead of emoji.  That
keeps every icon crisp, unique, theme-independent, and consistent on Windows
systems whose colour-emoji fonts differ.
"""
from __future__ import annotations

import ctypes
import os
import sys
import time
import weakref
from functools import lru_cache
from html import escape
from typing import Callable

from PySide6.QtCore import (
    QEvent,
    Property,
    QEasingCurve,
    QPoint,
    QPointF,
    QPropertyAnimation,
    QRect,
    QRectF,
    QSize,
    QTimer,
    Qt,
)
from PySide6.QtGui import (
    QColor,
    QIcon,
    QPainter,
    QPainterPath,
    QPen,
    QPixmap,
    QRegion,
)
from PySide6.QtWidgets import (
    QApplication,
    QMessageBox,
    QPushButton,
    QSizePolicy,
    QStyle,
    QStyleOptionButton,
    QStylePainter,
    QToolTip,
    QWidget,
)


_ICON_COLORS = {
    "adaptation": "#67e8f9",
    "selftest": "#5eead4",
    "simulation": "#fb7185",
    "eco": "#4ade80",
    "world": "#38bdf8",
    "attack": "#fb923c",
    "intel": "#60a5fa",
    "forensics": "#c084fc",
    "operations": "#22d3ee",
    "console": "#2dd4bf",
    "setup": "#f472b6",
    "help": "#facc15",
    "settings": "#cbd5e1",
    "stop": "#ffffff",
}


def _icon_pixmap(kind: str, color: str, size: int = 22) -> QPixmap:
    """Draw one small, high-DPI vector icon."""
    px = QPixmap(size * 2, size * 2)
    px.setDevicePixelRatio(2.0)
    px.fill(Qt.transparent)
    p = QPainter(px)
    p.setRenderHint(QPainter.Antialiasing)
    c = QColor(color)
    pen = QPen(c, 1.75)
    pen.setCapStyle(Qt.RoundCap)
    pen.setJoinStyle(Qt.RoundJoin)
    p.setPen(pen)
    p.setBrush(Qt.NoBrush)
    s = float(size)
    cx, cy = s / 2.0, s / 2.0

    if kind == "adaptation":
        # Host adaptation: a protected core with two bounded feedback arrows.
        p.drawEllipse(QRectF(7.5, 7.5, 7.0, 7.0))
        upper = QPainterPath()
        upper.moveTo(3.0, 9.0)
        upper.cubicTo(4.0, 4.0, 9.0, 2.5, 13.5, 4.0)
        upper.lineTo(12.0, 1.8)
        upper.moveTo(13.5, 4.0)
        upper.lineTo(10.8, 5.4)
        lower = QPainterPath()
        lower.moveTo(19.0, 13.0)
        lower.cubicTo(18.0, 18.0, 13.0, 19.5, 8.5, 18.0)
        lower.lineTo(10.0, 20.2)
        lower.moveTo(8.5, 18.0)
        lower.lineTo(11.2, 16.6)
        p.drawPath(upper)
        p.drawPath(lower)
    elif kind == "selftest":
        p.drawEllipse(QRectF(3.0, 3.0, s - 6.0, s - 6.0))
        p.drawLine(QPointF(7.0, 11.5), QPointF(10.0, 14.5))
        p.drawLine(QPointF(10.0, 14.5), QPointF(16.0, 8.0))
    elif kind == "simulation":
        p.drawEllipse(QRectF(3.0, 3.0, s - 6.0, s - 6.0))
        p.drawEllipse(QRectF(7.0, 7.0, s - 14.0, s - 14.0))
        p.drawLine(QPointF(cx, 1.5), QPointF(cx, 6.0))
        p.drawLine(QPointF(cx, s - 1.5), QPointF(cx, s - 6.0))
        p.drawLine(QPointF(1.5, cy), QPointF(6.0, cy))
        p.drawLine(QPointF(s - 1.5, cy), QPointF(s - 6.0, cy))
    elif kind == "eco":
        leaf = QPainterPath()
        leaf.moveTo(4.0, 17.5)
        leaf.cubicTo(4.5, 7.0, 11.0, 3.0, 18.5, 3.5)
        leaf.cubicTo(18.0, 12.0, 13.0, 18.0, 4.0, 17.5)
        p.drawPath(leaf)
        p.drawLine(QPointF(5.0, 17.0), QPointF(15.5, 7.0))
    elif kind == "world":
        p.drawEllipse(QRectF(2.5, 2.5, s - 5.0, s - 5.0))
        p.drawEllipse(QRectF(7.0, 2.5, s - 14.0, s - 5.0))
        p.drawLine(QPointF(3.5, 8.0), QPointF(s - 3.5, 8.0))
        p.drawLine(QPointF(3.5, 14.0), QPointF(s - 3.5, 14.0))
    elif kind == "attack":
        path = QPainterPath()
        path.moveTo(11.0, 2.5)
        path.cubicTo(14.5, 6.5, 17.8, 8.0, 17.0, 13.0)
        path.cubicTo(16.5, 17.2, 13.8, 19.5, 10.5, 19.5)
        path.cubicTo(6.5, 19.5, 4.5, 16.5, 5.0, 13.5)
        path.cubicTo(5.5, 10.5, 8.2, 9.0, 8.0, 5.0)
        path.cubicTo(9.5, 5.8, 10.4, 7.0, 11.2, 8.5)
        path.cubicTo(12.5, 6.0, 12.0, 4.2, 11.0, 2.5)
        p.drawPath(path)
    elif kind == "intel":
        shield = QPainterPath()
        shield.moveTo(cx, 2.5)
        shield.lineTo(18.0, 5.0)
        shield.lineTo(17.0, 13.0)
        shield.cubicTo(16.5, 16.5, 13.5, 19.0, cx, 20.0)
        shield.cubicTo(8.5, 19.0, 5.5, 16.5, 5.0, 13.0)
        shield.lineTo(4.0, 5.0)
        shield.closeSubpath()
        p.drawPath(shield)
        p.drawLine(QPointF(8.0, 11.0), QPointF(10.2, 13.2))
        p.drawLine(QPointF(10.2, 13.2), QPointF(14.5, 8.7))
    elif kind == "forensics":
        p.drawEllipse(QRectF(3.0, 3.0, 11.5, 11.5))
        p.drawLine(QPointF(13.0, 13.0), QPointF(19.0, 19.0))
        p.drawLine(QPointF(6.0, 8.0), QPointF(11.5, 8.0))
        p.drawLine(QPointF(8.75, 5.25), QPointF(8.75, 10.75))
    elif kind == "operations":
        # Local SOC hub: a protected central core linked to four workflows.
        p.drawEllipse(QRectF(8.0, 8.0, 6.0, 6.0))
        for point in (
            QPointF(4.0, 4.0), QPointF(18.0, 4.0),
            QPointF(4.0, 18.0), QPointF(18.0, 18.0),
        ):
            p.drawLine(QPointF(cx, cy), point)
            p.drawEllipse(QRectF(point.x() - 1.5, point.y() - 1.5, 3.0, 3.0))
    elif kind == "console":
        p.drawRoundedRect(QRectF(2.5, 4.0, s - 5.0, s - 8.0), 2.0, 2.0)
        p.drawLine(QPointF(6.0, 8.0), QPointF(9.0, 11.0))
        p.drawLine(QPointF(9.0, 11.0), QPointF(6.0, 14.0))
        p.drawLine(QPointF(11.5, 14.0), QPointF(16.0, 14.0))
    elif kind == "setup":
        p.drawLine(QPointF(5.0, 17.0), QPointF(16.5, 5.5))
        p.drawEllipse(QRectF(13.0, 2.0, 6.5, 6.5))
        p.drawLine(QPointF(4.0, 5.0), QPointF(4.0, 9.0))
        p.drawLine(QPointF(2.0, 7.0), QPointF(6.0, 7.0))
        p.drawLine(QPointF(14.0, 15.0), QPointF(14.0, 19.0))
        p.drawLine(QPointF(12.0, 17.0), QPointF(16.0, 17.0))
    elif kind == "help":
        p.drawEllipse(QRectF(3.0, 3.0, s - 6.0, s - 6.0))
        q = QPainterPath()
        q.moveTo(8.0, 8.2)
        q.cubicTo(8.8, 5.7, 14.8, 5.6, 14.8, 9.0)
        q.cubicTo(14.8, 11.6, 11.0, 11.3, 11.0, 14.0)
        p.drawPath(q)
        p.drawPoint(QPointF(11.0, 17.0))
    elif kind == "settings":
        p.drawEllipse(QRectF(8.0, 8.0, 6.0, 6.0))
        for i in range(8):
            import math
            angle = math.radians(i * 45.0)
            inner = QPointF(cx + 6.0 * math.cos(angle), cy + 6.0 * math.sin(angle))
            outer = QPointF(cx + 9.0 * math.cos(angle), cy + 9.0 * math.sin(angle))
            p.drawLine(inner, outer)
        p.drawEllipse(QRectF(4.5, 4.5, 13.0, 13.0))
    elif kind == "stop":
        p.setBrush(c)
        p.drawRoundedRect(QRectF(5.0, 5.0, s - 10.0, s - 10.0), 1.5, 1.5)
    else:
        p.drawEllipse(QRectF(4.0, 4.0, s - 8.0, s - 8.0))

    p.end()
    return px


@lru_cache(maxsize=64)
def navigation_icon(kind: str, color: str | None = None, size: int = 22) -> QIcon:
    """Return a cached vector icon for a named header destination."""
    tone = color or _ICON_COLORS.get(kind, "#e2e8f0")
    return QIcon(_icon_pixmap(kind, tone, size))


class HeaderActionButton(QPushButton):
    """Header button with a unique icon and an immediate definition card."""

    def __init__(
        self,
        label: str,
        icon_kind: str,
        title: str,
        definition: str,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(label, parent)
        self._full_label = str(label)
        self._compact = False
        self._compact_extent = 42
        self._hover_title = str(title)
        self._hover_definition = str(definition)
        self.setIcon(navigation_icon(icon_kind))
        self.setIconSize(QSize(20, 20))
        self.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Fixed)
        self.setCursor(Qt.PointingHandCursor)
        self.setAccessibleName(self._hover_title)
        self.setAccessibleDescription(self._hover_definition)
        self.setToolTip(
            "<div style='max-width:360px'>"
            f"<b style='font-size:13px'>{escape(self._hover_title)}</b><br>"
            f"<span>{escape(self._hover_definition)}</span>"
            "</div>"
        )

    def set_compact(self, compact: bool, extent: int = 42) -> None:
        """Switch between a full label and a square, icon-only control."""
        requested = bool(compact)
        requested_extent = max(32, int(extent))
        if requested == self._compact and (
            not requested or requested_extent == self._compact_extent
        ):
            if not requested:
                self._fit_label()
            return
        self._compact = requested
        self._compact_extent = requested_extent
        if self._compact:
            self.setText("")
            self.setMinimumWidth(self._compact_extent)
            self.setMaximumWidth(self._compact_extent)
            self.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)
        else:
            self.setMinimumWidth(0)
            self.setMaximumWidth(16_777_215)
            self.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Fixed)
            # Restore the complete label before asking the layout for space.
            # Eliding here would make sizeHint() request only the already-short
            # string and could trap a button at its former compact width.
            self.setText(self._full_label)
        self.updateGeometry()

    def is_compact(self) -> bool:
        return self._compact

    def sizeHint(self):  # noqa: N802 - Qt signature
        hint = super().sizeHint()
        if not self._compact:
            # Elision must not become the next layout's preferred width. Keep
            # requesting room for the semantic label when space returns.
            metrics = self.fontMetrics()
            hint.setWidth(hint.width() + max(0,
                metrics.horizontalAdvance(self._full_label) - metrics.horizontalAdvance(self.text())))
        return hint

    def set_full_label(self, label: str) -> None:
        """Change the semantic label while preserving the responsive mode."""
        self._full_label = str(label)
        self.setText("" if self._compact else self._full_label)
        self.updateGeometry()
        if not self._compact:
            QTimer.singleShot(0, self._fit_label)

    def _fit_label(self) -> None:
        """Elide a full-mode label instead of allowing hard visual clipping."""
        if self._compact:
            if self.text():
                self.setText("")
            return
        # During construction Qt has not assigned a useful width yet. Keep the
        # complete label so sizeHint() can request the correct natural width.
        if self.width() < 40:
            fitted = self._full_label
        else:
            icon_space = self.iconSize().width() + 30
            available = max(12, self.width() - icon_space)
            fitted = self.fontMetrics().elidedText(
                self._full_label, Qt.ElideRight, available
            )
        if fitted != self.text():
            self.setText(fitted)

    def resizeEvent(self, event) -> None:  # noqa: N802 - Qt signature
        super().resizeEvent(event)
        self._fit_label()

    def paintEvent(self, event) -> None:  # noqa: N802 - Qt signature
        if not self._compact:
            super().paintEvent(event)
            return
        # Paint the normal themed button frame, then center the icon ourselves.
        # This deliberately ignores text-button horizontal padding, which would
        # otherwise squeeze a compact icon at smaller UI scales.
        painter = QStylePainter(self)
        option = QStyleOptionButton()
        self.initStyleOption(option)
        option.text = ""
        option.icon = QIcon()
        painter.drawControl(QStyle.CE_PushButton, option)
        icon_size = min(
            self.iconSize().width(),
            max(1, self.width() - 8),
            max(1, self.height() - 8),
        )
        icon_rect = QRect(
            (self.width() - icon_size) // 2,
            (self.height() - icon_size) // 2,
            icon_size,
            icon_size,
        )
        mode = QIcon.Normal if self.isEnabled() else QIcon.Disabled
        state = QIcon.On if self.isChecked() else QIcon.Off
        self.icon().paint(painter, icon_rect, Qt.AlignCenter, mode, state)

    def enterEvent(self, event) -> None:  # noqa: N802 - Qt signature
        QToolTip.showText(
            self.mapToGlobal(QPoint(self.width() // 2, self.height() + 6)),
            self.toolTip(),
            self,
        )
        super().enterEvent(event)

    def leaveEvent(self, event) -> None:  # noqa: N802 - Qt signature
        QToolTip.hideText()
        super().leaveEvent(event)

    def focusInEvent(self, event) -> None:  # noqa: N802 - Qt signature
        QToolTip.showText(
            self.mapToGlobal(QPoint(self.width() // 2, self.height() + 6)),
            self.toolTip(),
            self,
        )
        super().focusInEvent(event)


def _windows_client_animations_enabled() -> bool:
    """Honor the Windows accessibility preference when it is available."""
    if sys.platform != "win32":
        return True
    try:
        # SPI_GETCLIENTAREAANIMATION
        enabled = ctypes.c_int(1)
        ok = ctypes.windll.user32.SystemParametersInfoW(
            0x1042, 0, ctypes.byref(enabled), 0
        )
        return bool(enabled.value) if ok else True
    except Exception:
        return True


def motion_allowed(config=None) -> bool:
    """Central motion gate used by the reveal animation."""
    if str(os.environ.get("OPTIMUS_REDUCE_MOTION", "")).strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }:
        return False
    if config is not None and not bool(getattr(config, "ui_motion_enabled", True)):
        return False
    return _windows_client_animations_enabled()


class _RevealFrame(QWidget):
    """Accent edge painted inside the window being revealed."""

    def __init__(self, parent: QWidget, color: QColor) -> None:
        super().__init__(parent)
        self._color = QColor(color)
        self._rect = QRect()
        self._progress = 0.0
        self.setAttribute(Qt.WA_TransparentForMouseEvents)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.hide()

    def set_reveal(self, rect: QRect, progress: float) -> None:
        self._rect = QRect(rect)
        self._progress = float(progress)
        self.update()

    def paintEvent(self, _event) -> None:  # noqa: N802 - Qt signature
        if self._rect.isEmpty():
            return
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        edge = QColor(self._color)
        edge.setAlpha(
            max(0, int(235 * (1.0 - max(0.0, self._progress - 0.84) / 0.16)))
        )
        fill = QColor(self._color)
        fill.setAlpha(max(0, int(24 * (1.0 - self._progress))))
        painter.setPen(QPen(edge, 2.2))
        painter.setBrush(fill)
        outline = QRectF(self._rect).adjusted(1.2, 1.2, -1.2, -1.2)
        painter.drawRoundedRect(outline, 9.0, 9.0)
        if self._rect.width() <= 8:
            glow = QColor(self._color)
            glow.setAlpha(90)
            painter.setPen(QPen(glow, 7.0))
            painter.drawLine(
                QPointF(outline.center().x(), outline.top()),
                QPointF(outline.center().x(), outline.bottom()),
            )
        painter.end()


class PanelRevealOverlay(QWidget):
    """Reveal and dismiss real destination windows through one accent line.

    The previous implementation animated a dashboard overlay, removed it, and
    only then opened an unrelated dialog. This coordinator watches for the
    window produced by the action and applies the widening mask to that window
    itself, so its live contents are visibly generated inside the expanding box.
    A captured window's normal close request is intercepted once and plays the
    same geometry in reverse before the real close proceeds.
    """

    def __init__(self, parent: QWidget) -> None:
        super().__init__(parent)
        self.setAttribute(Qt.WA_TransparentForMouseEvents)
        self._progress = 0.0
        self._origin = QPoint()
        self._source_global = QPoint()
        self._color = QColor("#38bdf8")
        self._armed = False
        self._target: QWidget | None = None
        self._frame: _RevealFrame | None = None
        self._previous_mask = QRegion()
        self._mode = "idle"
        self._global_windows = False
        self._shut_down = False
        self._motion_config = None
        self._pending_windows: list[weakref.ReferenceType[QWidget]] = []
        self._captured_windows: weakref.WeakSet[QWidget] = weakref.WeakSet()
        self._last_click_global = QPoint()
        self._last_click_at = 0.0
        self._animation = QPropertyAnimation(self, b"revealProgress", self)
        self._animation.setDuration(240)
        self._animation.setEasingCurve(QEasingCurve.OutCubic)
        self._animation.finished.connect(self._finish)
        self.hide()

    def set_motion_config(self, config=None) -> None:
        """Use an explicit motion policy when the owner has no ``config`` attr."""
        self._motion_config = config

    def _config(self):
        if self._motion_config is not None:
            return self._motion_config
        try:
            return getattr(self.window(), "config", None)
        except RuntimeError:
            return None

    @staticmethod
    def _widget_flag(widget: QWidget, name: str) -> bool:
        """Read opt-out flags set either as Python or dynamic Qt properties."""
        try:
            if bool(getattr(widget, name, False)):
                return True
        except RuntimeError:
            return True
        try:
            return bool(widget.property(name))
        except RuntimeError:
            return True

    def _is_reveal_destination(self, widget: QWidget) -> bool:
        """Return whether *widget* is an Optimus-owned content window.

        Tooltips, menus and splash surfaces are not content windows. They
        already own purpose-built motion and masking them here causes flicker
        or a duplicate animation.
        """
        try:
            if isinstance(widget, QMessageBox):
                return False
            if not widget.isWindow() or widget is self.window():
                return False
            if widget.windowType() in (
                Qt.ToolTip,
                Qt.Popup,
                Qt.SplashScreen,
            ):
                return False
            if self._widget_flag(widget, "_optimus_no_reveal"):
                return False
            return True
        except RuntimeError:
            return False

    def enable_global_windows(self, enabled: bool = True) -> None:
        """Apply the reveal/reverse-close transition to newly shown app windows."""
        if self._shut_down:
            return
        self._global_windows = bool(enabled)
        app = QApplication.instance()
        if app is None:
            return
        if self._global_windows:
            app.removeEventFilter(self)
            app.installEventFilter(self)
        elif not self._armed and self._target is None:
            self._clear_pending_windows()
            app.removeEventFilter(self)

    def shutdown(self) -> None:
        """Release every filter and mask owned by this transition coordinator."""
        self._shut_down = True
        self._global_windows = False
        self._armed = False
        self._animation.stop()
        app = QApplication.instance()
        if app is not None:
            app.removeEventFilter(self)
        self._clear_pending_windows()
        for target in tuple(self._captured_windows):
            try:
                target.removeEventFilter(self)
                original = getattr(target, "_optimus_reveal_original_mask", None)
                if isinstance(original, QRegion):
                    if original.isEmpty():
                        target.clearMask()
                    else:
                        target.setMask(original)
                setattr(target, "_optimus_reverse_reveal_close", False)
                setattr(target, "_optimus_close_bypass", True)
            except RuntimeError:
                # Qt may have destroyed a captured window before Python does.
                pass
        self._captured_windows.clear()
        if self._frame is not None:
            try:
                self._frame.deleteLater()
            except RuntimeError:
                pass
        self._frame = None
        self._target = None
        self._previous_mask = QRegion()
        self._progress = 0.0
        self._mode = "idle"

    def _animation_busy(self) -> bool:
        """Treat a torn-down Qt animation as unavailable during app shutdown."""
        try:
            return self._animation.state() == QPropertyAnimation.Running
        except RuntimeError:
            # QApplication teardown can delete child QObjects before the
            # application-wide event filter receives its final Show events.
            # Refusing another reveal is the safe and quiet shutdown behavior.
            return True

    def reveal(
        self,
        source: QWidget,
        after: Callable[[], object],
        color: str | QColor | None = None,
    ) -> bool:
        """Run an action and reveal the top-level window it creates."""
        if (
            self._armed
            or self._target is not None
            or self._animation_busy()
        ):
            return False
        app = QApplication.instance()
        if app is None:
            after()
            return True
        self._source_global = source.mapToGlobal(source.rect().center())
        self._color = QColor(color or "#38bdf8")
        self._armed = True
        if not self._global_windows:
            app.installEventFilter(self)
        try:
            result = after()
            if self._armed and isinstance(result, QWidget):
                if not result.isVisible():
                    result.show()
                self._capture(result)
        except Exception:
            self._cancel_capture()
            raise
        # Actions that did not create a window must not leave a global event
        # filter armed to accidentally capture some later notification.
        if self._armed:
            self._cancel_capture()
        return True

    def eventFilter(self, watched, event):  # noqa: N802 - Qt signature
        # This filter sees every application event, including paint/layout and
        # worker delivery bursts. Reject those before inspecting animation or
        # window state; repeated PySide enum conversion here taxes every widget
        # created while a destination window is being shown.
        if not isinstance(watched, QWidget):
            return False
        event_type = event.type()
        if event_type not in (QEvent.MouseButtonPress, QEvent.Close, QEvent.Show):
            return False
        if event_type == QEvent.MouseButtonPress:
            if not self._global_windows:
                return False
            try:
                self._last_click_global = watched.mapToGlobal(
                    event.position().toPoint()
                )
            except (AttributeError, RuntimeError):
                try:
                    self._last_click_global = watched.mapToGlobal(
                        watched.rect().center()
                    )
                except RuntimeError:
                    self._last_click_global = QPoint()
            self._last_click_at = time.monotonic()
            return False
        if event_type == QEvent.Close:
            if (
                not bool(getattr(watched, "_optimus_reverse_reveal_close", False))
                or bool(getattr(watched, "_optimus_close_bypass", False))
            ):
                return False
            app = QApplication.instance()
            try:
                shutting_down = bool(app is not None and app.closingDown())
            except Exception:
                shutting_down = False
            busy_with_other = (
                self._target is not None and self._target is not watched
            )
            if (
                shutting_down
                or busy_with_other
                or not motion_allowed(self._config())
                or not watched.isVisible()
            ):
                return False
            event.ignore()
            self._start_target_close(watched)
            return True
        # Only top-level Show events can reach the reveal machinery. In
        # particular, an opted-out security prompt never waits behind another
        # window's animation or acquires its temporary mask.
        if not self._is_reveal_destination(watched):
            return False
        if self._armed:
            self._capture(watched)
        elif (
            self._global_windows
            and self._target is not None
            and watched is not self._target
            and motion_allowed(self._config())
        ):
            # Two destinations can appear in one action (or a confirmation can
            # be created while another window is collapsing). Keep the later
            # window as a narrow live slice until its own turn rather than
            # flashing it at full size or silently skipping the transition.
            self._queue_pending_window(watched)
        elif (
            self._global_windows
            and self._target is None
            and not self._animation_busy()
            # A dialog may be intentionally hidden and reused. Its previous
            # transition marker is not an opt-out: every later Show receives a
            # fresh opening animation too.
            and motion_allowed(self._config())
        ):
            if (
                self._last_click_at
                and time.monotonic() - self._last_click_at <= 2.0
                and not self._last_click_global.isNull()
            ):
                self._source_global = QPoint(self._last_click_global)
            else:
                try:
                    self._source_global = watched.mapToGlobal(
                        watched.rect().center()
                    )
                except RuntimeError:
                    return False
            self._color = QColor(
                getattr(watched, "_optimus_reveal_color", "#38bdf8")
            )
            self._armed = True
            self._capture(watched)
        return False

    def _capture(self, target: QWidget) -> None:
        if not self._armed:
            return
        self._armed = False
        app = QApplication.instance()
        if app is not None and not self._global_windows:
            app.removeEventFilter(self)
        self._target = target
        self._captured_windows.add(target)
        pending_mask = getattr(
            target,
            "_optimus_pending_reveal_original_mask",
            None,
        )
        self._previous_mask = (
            QRegion(pending_mask)
            if isinstance(pending_mask, QRegion)
            else QRegion(target.mask())
        )
        setattr(target, "_optimus_pending_reveal_original_mask", None)
        self._mode = "opening"
        # Windows may opt into an opening reveal while retaining their own
        # close handler. Other content windows receive a matching reverse reveal.
        setattr(
            target,
            "_optimus_reverse_reveal_close",
            not self._widget_flag(target, "_optimus_reveal_open_only"),
        )
        setattr(target, "_optimus_close_bypass", False)
        setattr(
            target,
            "_optimus_reveal_original_mask",
            QRegion(self._previous_mask),
        )
        setattr(
            target,
            "_optimus_reveal_source_global",
            QPoint(self._source_global),
        )
        setattr(target, "_optimus_reveal_color", QColor(self._color))
        try:
            target.removeEventFilter(self)
        except RuntimeError:
            return
        target.installEventFilter(self)
        # Apply the first narrow slice during QEvent.Show itself. Waiting for
        # the next event-loop turn would allow one fully painted dialog frame
        # to flash before the reveal begins.
        bounds = target.rect()
        local = target.mapFromGlobal(self._source_global)
        self._origin = QPoint(
            max(0, min(bounds.width(), local.x())),
            max(0, min(bounds.height(), local.y())),
        )
        initial = QRect(
            self._origin.x() - 2,
            self._origin.y() - 14,
            4,
            28,
        ).intersected(bounds)
        if not initial.isEmpty():
            target.setMask(QRegion(initial))
        # Let layouts and the window manager settle before taking the final
        # dimensions. Modal exec() enters a nested event loop, so this also works
        # for existing blocking dialogs without rewriting every destination.
        QTimer.singleShot(0, self._start_target_reveal)

    def _queue_pending_window(self, target: QWidget) -> None:
        for reference in self._pending_windows:
            if reference() is target:
                return
        try:
            source_global = (
                QPoint(self._last_click_global)
                if self._last_click_at
                and time.monotonic() - self._last_click_at <= 2.0
                and not self._last_click_global.isNull()
                else target.mapToGlobal(target.rect().center())
            )
            setattr(
                target,
                "_optimus_pending_reveal_original_mask",
                QRegion(target.mask()),
            )
            setattr(target, "_optimus_reveal_source_global", source_global)
            setattr(
                target,
                "_optimus_reveal_color",
                QColor(getattr(target, "_optimus_reveal_color", "#38bdf8")),
            )
            bounds = target.rect()
            local = target.mapFromGlobal(source_global)
            origin = QPoint(
                max(0, min(bounds.width(), local.x())),
                max(0, min(bounds.height(), local.y())),
            )
            initial = QRect(
                origin.x() - 2,
                origin.y() - 14,
                4,
                28,
            ).intersected(bounds)
            if not initial.isEmpty():
                target.setMask(QRegion(initial))
            self._pending_windows.append(weakref.ref(target))
        except RuntimeError:
            return

    def _start_next_pending_window(self) -> None:
        if (
            self._target is not None
            or self._armed
            or self._animation_busy()
        ):
            return
        while self._pending_windows:
            target = self._pending_windows.pop(0)()
            if target is None:
                continue
            try:
                if not target.isVisible():
                    self._restore_pending_mask(target)
                    continue
                self._source_global = QPoint(
                    getattr(
                        target,
                        "_optimus_reveal_source_global",
                        target.mapToGlobal(target.rect().center()),
                    )
                )
                self._color = QColor(
                    getattr(target, "_optimus_reveal_color", "#38bdf8")
                )
                self._armed = True
                self._capture(target)
                return
            except RuntimeError:
                continue

    @staticmethod
    def _restore_pending_mask(target: QWidget) -> None:
        try:
            original = getattr(
                target,
                "_optimus_pending_reveal_original_mask",
                None,
            )
            if isinstance(original, QRegion):
                if original.isEmpty():
                    target.clearMask()
                else:
                    target.setMask(original)
            setattr(target, "_optimus_pending_reveal_original_mask", None)
        except RuntimeError:
            pass

    def _clear_pending_windows(self) -> None:
        for reference in self._pending_windows:
            target = reference()
            if target is not None:
                self._restore_pending_mask(target)
        self._pending_windows.clear()

    def _cancel_capture(self) -> None:
        self._armed = False
        app = QApplication.instance()
        if app is not None and not self._global_windows:
            app.removeEventFilter(self)

    def _start_target_reveal(self) -> None:
        target = self._target
        if target is None or not target.isVisible():
            if target is not None:
                if self._previous_mask.isEmpty():
                    target.clearMask()
                else:
                    target.setMask(self._previous_mask)
            self._target = None
            self._previous_mask = QRegion()
            return
        bounds = target.rect()
        local = target.mapFromGlobal(self._source_global)
        self._origin = QPoint(
            max(0, min(bounds.width(), local.x())),
            max(0, min(bounds.height(), local.y())),
        )
        self._frame = _RevealFrame(target, self._color)
        self._frame.setGeometry(bounds)
        self._frame.show()
        self._frame.raise_()
        self._progress = 0.0
        self._mode = "opening"
        self._animation.stop()
        self._animation.setDuration(240)
        self._animation.setEasingCurve(QEasingCurve.OutCubic)
        self._animation.setStartValue(0.0)
        self._animation.setEndValue(1.0)
        self._animation.start()

    def _start_target_close(self, target: QWidget) -> None:
        """Collapse one captured destination, then allow its real close."""
        if self._target is target and self._mode == "closing":
            return

        # A very fast X press can arrive while the opening animation is still
        # running. Reverse from the exact visible progress instead of flashing
        # the fully opened window first.
        opening_progress = (
            self._progress
            if self._target is target and self._mode == "opening"
            else 1.0
        )
        self._animation.stop()
        self._target = target
        self._mode = "closing"
        self._previous_mask = QRegion(
            getattr(
                target,
                "_optimus_reveal_original_mask",
                target.mask(),
            )
        )
        source_global = getattr(
            target,
            "_optimus_reveal_source_global",
            target.mapToGlobal(target.rect().center()),
        )
        self._source_global = QPoint(source_global)
        self._color = QColor(
            getattr(target, "_optimus_reveal_color", QColor("#38bdf8"))
        )
        bounds = target.rect()
        local = target.mapFromGlobal(self._source_global)
        self._origin = QPoint(
            max(0, min(bounds.width(), local.x())),
            max(0, min(bounds.height(), local.y())),
        )
        if self._frame is None:
            self._frame = _RevealFrame(target, self._color)
        self._frame.setGeometry(bounds)
        self._frame.show()
        self._frame.raise_()

        start = max(0.0, min(1.0, float(opening_progress)))
        self._progress = start
        if start <= 0.01:
            self._finish()
            return
        self._animation.setDuration(max(80, round(180 * start)))
        self._animation.setEasingCurve(QEasingCurve.InCubic)
        self._animation.setStartValue(start)
        self._animation.setEndValue(0.0)
        self._animation.start()

    def get_reveal_progress(self) -> float:
        return self._progress

    def set_reveal_progress(self, value: float) -> None:
        self._progress = max(0.0, min(1.0, float(value)))
        target = self._target
        if target is None:
            return
        try:
            bounds = target.rect()
            vertical = min(1.0, self._progress / 0.30)
            horizontal = max(0.0, min(1.0, (self._progress - 0.10) / 0.90))
            center_x = self._origin.x() + (
                bounds.center().x() - self._origin.x()
            ) * horizontal
            center_y = self._origin.y() + (
                bounds.center().y() - self._origin.y()
            ) * vertical
            width = max(4, round(bounds.width() * horizontal))
            height = max(28, round(bounds.height() * vertical))
            rect = QRect(
                round(center_x - width / 2),
                round(center_y - height / 2),
                width,
                height,
            ).intersected(bounds)
            target.setMask(QRegion(rect))
            if self._frame is not None:
                if self._frame.geometry() != bounds:
                    self._frame.setGeometry(bounds)
                self._frame.set_reveal(rect, self._progress)
        except RuntimeError:
            self._animation.stop()
            self._target = None

    revealProgress = Property(
        float,
        get_reveal_progress,
        set_reveal_progress,
    )

    def _finish(self) -> None:
        target = self._target
        mode = self._mode
        previous_mask = QRegion(self._previous_mask)
        frame = self._frame
        try:
            if target is not None and mode == "closing":
                # Hide while the mask is still a line so restoring the original
                # mask cannot flash one full-size frame before closeEvent runs.
                target.hide()
                if previous_mask.isEmpty():
                    target.clearMask()
                else:
                    target.setMask(previous_mask)
                setattr(target, "_optimus_close_bypass", True)
                target.removeEventFilter(self)
                # Release this transition before closeEvent runs. A destination
                # may create a nested confirmation dialog from closeEvent; the
                # global coordinator can now animate that prompt immediately in
                # its nested event loop instead of deadlocking behind its owner.
                self._frame = None
                self._target = None
                self._previous_mask = QRegion()
                self._progress = 0.0
                self._mode = "idle"
                if frame is not None:
                    frame.deleteLater()
                closed = target.close()
                if not closed and getattr(target, "_optimus_deferred_close", False):
                    # A tool window may be waiting for a bounded Qt worker to
                    # finish. Its reverse animation is already done; leave it
                    # hidden rather than risking QThread destruction.
                    return
                if not closed:
                    # A destination is allowed to veto close for unsaved work.
                    # Restore it fully and re-arm the reverse transition.
                    setattr(target, "_optimus_close_bypass", False)
                    target.installEventFilter(self)
                    prior_opt_out = getattr(
                        target,
                        "_optimus_no_reveal",
                        False,
                    )
                    setattr(target, "_optimus_no_reveal", True)
                    target.show()
                    setattr(target, "_optimus_no_reveal", prior_opt_out)
                    target.raise_()
                    target.activateWindow()
            elif target is not None:
                if previous_mask.isEmpty():
                    target.clearMask()
                else:
                    target.setMask(previous_mask)
                target.raise_()
                target.activateWindow()
        except RuntimeError:
            pass
        finally:
            # A nested close confirmation may already own the coordinator. Do
            # not clear its state when the outer close resumes.
            if self._target is target:
                if self._frame is not None:
                    self._frame.deleteLater()
                self._frame = None
                self._target = None
                self._previous_mask = QRegion()
                self._progress = 0.0
                self._mode = "idle"
            app = QApplication.instance()
            if (
                app is not None
                and not self._global_windows
                and not self._armed
                and self._target is None
            ):
                app.removeEventFilter(self)
            QTimer.singleShot(0, self._start_next_pending_window)


def install_global_window_reveal(
    owner: QWidget,
    *,
    config=None,
) -> PanelRevealOverlay:
    """Install one shared transition coordinator on a top-level Optimus UI.

    Main Optimus, Black Box, the Watchdog/Scanner status windows, and the
    standalone Sandbox/Upgrade Console each own a QApplication in some launch
    modes. Installing the coordinator in each entry point keeps every
    Optimus-created dialog on the same open and reverse-close path.
    """
    existing = getattr(owner, "_optimus_window_reveal", None)
    if isinstance(existing, PanelRevealOverlay):
        existing.set_motion_config(config)
        existing.enable_global_windows(True)
        return existing
    overlay = PanelRevealOverlay(owner)
    overlay.set_motion_config(config)
    overlay.enable_global_windows(True)
    setattr(owner, "_optimus_window_reveal", overlay)
    return overlay


def show_with_window_reveal(
    window: QWidget,
    *,
    config=None,
    color: str | QColor = "#38bdf8",
) -> PanelRevealOverlay:
    """Show a standalone Optimus window through its real-content reveal."""
    overlay = install_global_window_reveal(window, config=config)
    if not motion_allowed(config):
        window.show()
        return overlay

    def _show():
        window.show()
        window.raise_()
        return window

    # Mapping a hidden top-level widget is supported by Qt and gives the
    # transition a stable centre-origin before its first visible frame.
    overlay.reveal(window, _show, color)
    return overlay

