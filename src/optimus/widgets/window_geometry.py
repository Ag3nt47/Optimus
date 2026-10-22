"""Screen-aware geometry for Optimus content windows, in Qt logical pixels.

The coordinator only touches widgets in this QApplication. Native file choosers,
message boxes, menus and other transient OS surfaces retain their own behavior.
Normal user resizing is never replaced with a preferred/default size.
"""
from __future__ import annotations

import weakref

from PySide6.QtCore import QEvent, QObject, QRect, QTimer, Qt
from PySide6.QtWidgets import QApplication, QDialog, QFileDialog, QMainWindow, QMenu, QMessageBox


SCREEN_MARGIN = 12


def _content_window(window):
    return (isinstance(window, (QMainWindow, QDialog))
            and not isinstance(window, (QFileDialog, QMenu, QMessageBox))
            and window.isWindow()
            and window.windowType() not in (Qt.ToolTip, Qt.Popup, Qt.SplashScreen)
            and not bool(window.property('_optimus_no_screen_fit')))


def available_screen_geometry(window):
    """Return the work area of this window's screen, without scaling it again."""
    handle = window.windowHandle()
    screen = handle.screen() if handle is not None else None
    if screen is None and window.parentWidget() is not None:
        screen = window.parentWidget().screen()
    if screen is None:
        screen = window.screen()
    if screen is None:
        app = QApplication.instance()
        screen = app.primaryScreen() if app is not None else None
    return QRect(screen.availableGeometry()) if screen is not None else QRect()


def _frame_margins(window):
    handle = window.windowHandle()
    if handle is not None:
        margins = handle.frameMargins()
        values = (margins.left(), margins.top(), margins.right(), margins.bottom())
        if any(values):
            return tuple(max(0, value) for value in values)
    frame, client = window.frameGeometry(), window.geometry()
    return (max(0, client.left() - frame.left()), max(0, client.top() - frame.top()),
            max(0, frame.right() - client.right()), max(0, frame.bottom() - client.bottom()))


def fit_window_to_screen(window, *, available=None):
    """Shrink/reposition a content window when necessary; never enlarge it.

    ``available`` is a QRect override for an isolated fixture or an explicitly
    selected screen. Qt's availableGeometry already accounts for display scale,
    taskbars and monitor coordinates; multiplying by devicePixelRatio is wrong.
    """
    if not _content_window(window) or window.isMaximized() or window.isFullScreen() or window.isMinimized():
        return QRect(window.geometry())
    bounds = QRect(available) if available is not None else available_screen_geometry(window)
    if bounds.width() <= 0 or bounds.height() <= 0:
        return QRect(window.geometry())
    margin = min(SCREEN_MARGIN, max(0, min(bounds.width(), bounds.height()) // 8))
    bounds.adjust(margin, margin, -margin, -margin)
    left, top, right, bottom = _frame_margins(window)
    width = max(1, bounds.width() - left - right)
    height = max(1, bounds.height() - top - bottom)
    minimum = window.minimumSize()
    # Explicit desktop minima must not make the entire dialog inaccessible on a
    # smaller laptop work area or after changing display scaling.
    if minimum.width() > width or minimum.height() > height:
        window.setMinimumSize(min(minimum.width(), width), min(minimum.height(), height))
    desired_width, desired_height = min(window.width(), width), min(window.height(), height)
    if (desired_width, desired_height) != (window.width(), window.height()):
        window.resize(desired_width, desired_height)
    frame = window.frameGeometry()
    x = min(max(frame.x(), bounds.left()), max(bounds.left(), bounds.right() - frame.width() + 1))
    y = min(max(frame.y(), bounds.top()), max(bounds.top(), bounds.bottom() - frame.height() + 1))
    if (x, y) != (frame.x(), frame.y()):
        window.move(x, y)
    return QRect(window.geometry())


class WindowGeometryCoordinator(QObject):
    """Fit on presentation/screen changes, retaining chosen session geometry."""
    def __init__(self, app):
        super().__init__(app)
        self.app = app
        self._seen = weakref.WeakSet()
        self._owners = weakref.WeakSet()
        self._scheduled = weakref.WeakSet()
        self._handles = weakref.WeakSet()
        self._screens = weakref.WeakSet()
        self._session = weakref.WeakKeyDictionary()
        self._window_owners = weakref.WeakKeyDictionary()
        self._screen_events = {QEvent.ScreenChangeInternal}
        if hasattr(QEvent, 'DevicePixelRatioChange'):
            self._screen_events.add(QEvent.DevicePixelRatioChange)
        app.screenAdded.connect(self._screen_added)
        app.screenRemoved.connect(self._screen_changed)
        app.primaryScreenChanged.connect(self._screen_changed)
        for screen in app.screens():
            self._screen_added(screen)

    def _key(self, window):
        return (type(window).__module__, type(window).__qualname__, window.windowTitle())

    def register_owner(self, owner):
        self._owners.add(owner)

    def _owned(self, window):
        if not _content_window(window):
            return False
        if type(window).__module__.startswith('optimus.'):
            return True
        current = window
        while current is not None:
            if current in self._owners or type(current).__module__.startswith('optimus.'):
                return True
            current = current.parentWidget()
        return False

    def _owner_for(self, window):
        candidates = [window.parentWidget(), getattr(window, '_main_window', None),
                      getattr(window, 'owner', None)]
        backend = getattr(window, 'backend', None)
        if backend is not None:
            candidates.extend(owner for owner in self._owners
                              if getattr(owner, 'backend', None) is backend)
        for owner in candidates:
            if owner is not None and owner is not window:
                try:
                    owner = owner.window()
                    if owner.isVisible() and not owner.isMinimized() and self._owned(owner):
                        return owner
                except (AttributeError, RuntimeError):
                    continue
        return None

    def _initial_geometry(self, window):
        owner = self._owner_for(window)
        if owner is None:
            fit_window_to_screen(window)
            return
        # Detached Settings/Sandbox windows may initially inherit the primary
        # screen even when their visible owner is on a different monitor.
        screen = available_screen_geometry(owner)
        bounds = owner.geometry().intersected(screen)
        if bounds.isEmpty():
            fit_window_to_screen(window)
            return
        left, top, right, bottom = _frame_margins(window)
        hint = window.minimumSizeHint().expandedTo(window.minimumSize())
        feasible = (hint.width() + left + right + 2 * SCREEN_MARGIN <= bounds.width()
                    and hint.height() + top + bottom + 2 * SCREEN_MARGIN <= bounds.height())
        # A small confirmation keeps its compact size. A large tools window is
        # capped by its visible owner only when its usable minimum still fits.
        fit_window_to_screen(window, available=bounds if feasible else screen)
        frame = window.frameGeometry()
        frame.moveCenter(bounds.center())
        window.move(frame.topLeft())
        fit_window_to_screen(window, available=bounds if feasible else screen)

    def clear_session_geometry(self):
        """Forget only in-memory geometry; useful for independent fixture apps."""
        self._session.clear()

    def _remember(self, window):
        if window in self._seen and not window.isMinimized() and not window.isMaximized() and not window.isFullScreen():
            reference = self._window_owners.get(window)
            owner = reference() if reference is not None else None
            if owner is not None:
                self._session.setdefault(owner, {})[self._key(window)] = QRect(window.geometry())

    def _screen_added(self, screen):
        if screen not in self._screens:
            self._screens.add(screen)
            screen.availableGeometryChanged.connect(self._screen_changed)
            screen.logicalDotsPerInchChanged.connect(self._screen_changed)
        self._screen_changed()

    def _screen_changed(self, *_):
        for window in self.app.topLevelWidgets():
            if self._owned(window) and window.isVisible():
                self._schedule(window)

    def _schedule(self, window):
        if window in self._scheduled:
            return
        self._scheduled.add(window)
        reference = weakref.ref(window)
        def finish():
            target = reference()
            if target is None:
                return
            self._scheduled.discard(target)
            try:
                if target.isVisible():
                    fit_window_to_screen(target)
            except RuntimeError:
                pass  # A dialog may be destroyed before its queued first frame.
        QTimer.singleShot(0, finish)

    def _present(self, window):
        first = window not in self._seen
        if first:
            self._seen.add(window)
            owner = self._owner_for(window) or window
            # Bind memory to the original live owner, including when that owner
            # is hidden before a child receives its own Hide/Close event.
            self._window_owners[window] = weakref.ref(owner)
            if isinstance(window, QDialog):
                window.setSizeGripEnabled(True)
            remembered = self._session.get(owner, {}).get(self._key(window))
            if remembered is not None:
                window.setGeometry(remembered)
            else:
                self._initial_geometry(window)
        fit_window_to_screen(window)
        handle = window.windowHandle()
        if handle is not None and handle not in self._handles:
            self._handles.add(handle)
            handle.screenChanged.connect(self._screen_changed)
        self._schedule(window)  # Include native frame margins after first show.

    def eventFilter(self, watched, event):
        kind = event.type()
        if kind not in (QEvent.Show, QEvent.Hide, QEvent.Close) and kind not in self._screen_events:
            return False
        try:
            if not self._owned(watched):
                return False
            if kind == QEvent.Show:
                self._present(watched)
            elif kind in (QEvent.Hide, QEvent.Close):
                self._remember(watched)
            elif watched.isVisible():
                self._schedule(watched)
        except RuntimeError:
            pass
        return False


def install_window_geometry(owner):
    """Install one lightweight app-local coordinator, independently of motion."""
    app = QApplication.instance()
    if app is None:
        return None
    coordinator = getattr(app, '_optimus_window_geometry', None)
    if not isinstance(coordinator, WindowGeometryCoordinator):
        coordinator = WindowGeometryCoordinator(app)
        app._optimus_window_geometry = coordinator
    coordinator.register_owner(owner)
    # The fitter sees Show before the reveal coordinator captures its mask.
    app.removeEventFilter(coordinator)
    app.installEventFilter(coordinator)
    return coordinator
