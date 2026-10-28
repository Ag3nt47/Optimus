"""Bounded presentation scaling owned by one window, without polling."""
from __future__ import annotations

import math
import weakref

from PySide6.QtCore import QEvent, QObject, QTimer
from PySide6.QtWidgets import QApplication


def window_scale(width, height, baseline=(850, 650)):
    """Grow only when both dimensions allow it; never reduce readable text."""
    try:
        base_width, base_height = (float(value) for value in baseline)
    except (TypeError, ValueError):
        raise ValueError('The responsive baseline must contain two positive dimensions.') from None
    if not all(math.isfinite(value) and value > 0 for value in (base_width, base_height)):
        raise ValueError('The responsive baseline must contain two positive dimensions.')
    try:
        width, height = float(width), float(height)
    except (TypeError, ValueError):
        return 1.0
    if not all(math.isfinite(value) for value in (width, height)):
        return 1.0
    ratio = max(1.0, min(1.35, width / base_width, height / base_height))
    # Stable 5% steps avoid repeatedly rebuilding styles for adjacent pixels.
    return min(1.35, max(1.0, math.floor(ratio * 20 + 0.5) / 20))


def apply_generated_style(widget, style):
    """Replace an opted-in generated stylesheet without adopting custom CSS.

    Surfaces record their initial generated CSS in ``_optimus_surface_style``.
    A later external replacement remains authoritative through every resize.
    """
    previous = widget.property('_optimus_surface_style')
    if not isinstance(previous, str) or widget.styleSheet() != previous:
        return False
    if widget.styleSheet() != style:
        widget.setStyleSheet(style)
    widget.setProperty('_optimus_surface_style', style)
    return True


class ResponsiveWindowScale(QObject):
    """Coalesce local geometry/style changes into a presentation callback.

    The callback owns the window's styling and accessibility decisions. This
    helper never saves preferences, changes application-wide styling or starts
    work. Its first callback is deferred until construction can finish.
    """

    _EVENTS = frozenset((QEvent.Resize, QEvent.Show, QEvent.FontChange,
        QEvent.StyleChange, QEvent.PaletteChange, QEvent.ApplicationFontChange,
        QEvent.ApplicationPaletteChange, QEvent.WindowStateChange))

    def __init__(self, window, callback, baseline=(850, 650)):
        super().__init__(window)
        if not callable(callback):
            raise TypeError('A responsive presentation callback is required.')
        self.baseline = tuple(baseline)
        window_scale(1, 1, self.baseline)  # Validate before installing anything.
        self._window = weakref.ref(window)
        # A bound presentation method must not keep a closed window alive.
        self._callback = (weakref.WeakMethod(callback)
            if getattr(callback, '__self__', None) is not None
            and getattr(callback, '__func__', None) is not None else lambda: callback)
        self._applying = False
        self._last_state = None
        self._last_scale = None
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(self._apply)
        window.installEventFilter(self)
        self._queue()

    def _snapshot(self, window):
        # Include inherited CSS: a parent theme can change without changing the
        # child's own stylesheet or base font. Post-callback snapshots also
        # suppress events caused by the callback's own stylesheet updates.
        styles = []
        ancestor = window
        while ancestor is not None:
            styles.append((id(ancestor), ancestor.styleSheet()))
            ancestor = ancestor.parentWidget()
        app = QApplication.instance()
        return (window.width(), window.height(), window.windowState().value,
            window.font().key(), window.palette().cacheKey(),
            window.style().objectName(), tuple(styles), app.styleSheet() if app else '')

    def _queue(self):
        if not self._applying and not self._timer.isActive():
            self._timer.start(0)

    def eventFilter(self, watched, event):
        if event.type() in self._EVENTS:
            self._queue()
        return super().eventFilter(watched, event)

    def _apply(self):
        window = self._window()
        callback = self._callback()
        if window is None or callback is None:
            return
        state = self._snapshot(window)
        scale = window_scale(window.width(), window.height(), self.baseline)
        # Callers can resize panels within one font-scale step. They can skip
        # rebuilding identical CSS while still responding to the new geometry.
        if state == self._last_state:
            return
        self._applying = True
        try:
            callback(scale)
        finally:
            self._last_scale = scale
            self._last_state = self._snapshot(window)
            self._applying = False
