"""Session-only full-screen controls that leave each window's close guards intact."""
from __future__ import annotations

from PySide6.QtCore import QEvent, QObject, QPointF, QRect, QSize, Qt
from PySide6.QtGui import QColor, QIcon, QPainter, QPen, QPixmap
from PySide6.QtWidgets import QApplication, QPushButton, QSizePolicy, QWidget


def _fullscreen_icon(exiting):
    """Draw four corners, pointing inward when the action restores the window."""
    pixmap = QPixmap(40, 40)
    pixmap.setDevicePixelRatio(2)
    pixmap.fill(Qt.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.Antialiasing)
    pen = QPen(QColor('#67e8f9'), 1.7)
    pen.setCapStyle(Qt.RoundCap)
    pen.setJoinStyle(Qt.RoundJoin)
    painter.setPen(pen)
    for x, y, dx, dy in ((3, 3, 1, 1), (17, 3, -1, 1),
                         (3, 17, 1, -1), (17, 17, -1, -1)):
        if exiting:
            x, y, dx, dy = x + 4 * dx, y + 4 * dy, -dx, -dy
        corner = QPointF(x, y)
        painter.drawLine(corner, QPointF(x + 4 * dx, y))
        painter.drawLine(corner, QPointF(x, y + 4 * dy))
    painter.end()
    return QIcon(pixmap)


class FullScreenControl(QObject):
    """Expose an icon button plus F11/Esc for one top-level content window.

    Add ``button`` to a fixed header or toolbar. The application event filter
    handles focused child editors too, but never intercepts a separate dialog's
    keys. Escape is consumed only while exiting full screen; the next press
    follows the window's existing rejection/cancellation rules.
    """
    def __init__(self, window, button_parent=None):
        super().__init__(window)
        self.window = window
        self._restore_geometry = QRect(window.normalGeometry())
        if not self._restore_geometry.isValid():
            self._restore_geometry = QRect(window.geometry())
        self._restore_maximized = window.isMaximized()
        self._changing = False
        self._swallowed_keys = set()
        self._icons = (_fullscreen_icon(False), _fullscreen_icon(True))
        self.button = QPushButton(button_parent or window)
        self.button.setObjectName('FullScreenButton')
        self.button.setIconSize(QSize(16, 16))
        self.button.setCursor(Qt.PointingHandCursor)
        self.button.setCheckable(True)
        self.button.setAutoDefault(False)
        self.button.setDefault(False)
        self.button.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)
        self.button.clicked.connect(self.toggle)
        self._sync_button()
        # Reserve the longer label so entering full screen never shifts a row.
        self.button.setMinimumWidth(self.button.fontMetrics().horizontalAdvance('Exit full screen') + 58)
        QApplication.instance().installEventFilter(self)

    def _remember(self, state=None):
        state = self.window.windowState() if state is None else state
        self._restore_maximized = bool(state & Qt.WindowMaximized)
        normal = self.window.normalGeometry()
        if normal.isValid():
            self._restore_geometry = QRect(normal)

    def _sync_button(self):
        active = self.window.isFullScreen()
        label = 'Exit full screen' if active else 'Full screen'
        description = ('Restore the previous window size. Press F11 or Esc to exit full screen.'
                       if active else 'Use the whole screen. Press F11 to enter full screen.')
        self.button.setText(label)
        self.button.setIcon(self._icons[int(active)])
        self.button.setChecked(active)
        self.button.setAccessibleName(label)
        self.button.setAccessibleDescription(description)
        self.button.setToolTip(description)
        self.button.setStatusTip(description)

    def toggle(self, checked=False):
        if self.exit_full_screen():
            return
        self._remember()
        self._changing = True
        try:
            self.window.showFullScreen()
        finally:
            self._changing = False
        self._sync_button()

    def exit_full_screen(self):
        """Restore the prior normal/maximized state, without closing anything."""
        if not self.window.isFullScreen():
            return False
        self._changing = True
        try:
            self.window.showNormal()
            if self._restore_geometry.isValid():
                self.window.setGeometry(self._restore_geometry)
            if self._restore_maximized:
                self.window.showMaximized()
        finally:
            self._changing = False
        self._sync_button()
        return True

    def eventFilter(self, watched, event):
        kind = event.type()
        if kind not in (QEvent.WindowStateChange, QEvent.ShortcutOverride,
                        QEvent.KeyPress, QEvent.KeyRelease):
            return False
        if not isinstance(watched, QWidget) or watched.window() is not self.window:
            return False
        if watched is self.window and kind == QEvent.WindowStateChange:
            if not self._changing:
                if self.window.isFullScreen():
                    if not event.oldState() & Qt.WindowFullScreen:
                        self._remember(event.oldState())
                elif not self.window.isMinimized():
                    self._remember()
            self._sync_button()
        if kind not in (QEvent.ShortcutOverride, QEvent.KeyPress, QEvent.KeyRelease):
            return False
        key = event.key()
        if key not in (Qt.Key_F11, Qt.Key_Escape):
            return False
        if kind == QEvent.KeyRelease:
            if key in self._swallowed_keys:
                if not event.isAutoRepeat():
                    self._swallowed_keys.discard(key)
                return True
            return False
        handles = (key in self._swallowed_keys or
                   event.modifiers() == Qt.NoModifier and
                   (key == Qt.Key_F11 or self.window.isFullScreen()))
        if not handles:
            return False
        event.accept()
        if kind == QEvent.ShortcutOverride:
            return True
        if key in self._swallowed_keys:
            return True  # Holding Esc/F11 cannot close or repeatedly flip state.
        self._swallowed_keys.add(key)
        if key == Qt.Key_F11:
            self.toggle()
        else:
            self.exit_full_screen()
        return True
