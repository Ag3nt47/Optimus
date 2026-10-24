"""Keep sidebar action hit areas inside the real native scroll viewport."""
from PySide6.QtCore import QEvent, QTimer, Qt
from PySide6.QtWidgets import QScrollArea


class NavigationScrollArea(QScrollArea):
    """Fit the sidebar to measured text instead of scrolling hidden pixels.

    Native font and scrollbar metrics vary. A fixed 205-pixel sidebar can be a
    pixel narrower than its longest action after text scaling, even while its
    horizontal scrollbar is hidden. Recalculate only at layout/style boundaries;
    this does not run a persistent timer or alter a user's window size.
    """
    def __init__(self, sidebar, *, minimum_sidebar_width=205):
        super().__init__(sidebar)
        self._sidebar = sidebar
        self._minimum_sidebar_width = minimum_sidebar_width
        self._fit_timer = QTimer(self)
        self._fit_timer.setSingleShot(True)
        self._fit_timer.timeout.connect(self._fit_sidebar)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.installEventFilter(self)
        self.viewport().installEventFilter(self)

    def setWidget(self, widget):
        super().setWidget(widget)
        widget.installEventFilter(self)
        self._schedule_fit()

    def eventFilter(self, watched, event):
        if event.type() in (QEvent.LayoutRequest, QEvent.Resize, QEvent.Show,
                            QEvent.StyleChange, QEvent.FontChange, QEvent.Polish):
            self._schedule_fit()
        return super().eventFilter(watched, event)

    def _schedule_fit(self):
        if not self._fit_timer.isActive():
            self._fit_timer.start(0)

    def _fit_sidebar(self):
        content = self.widget()
        if content is None or self.viewport().width() <= 0:
            return
        layout = content.layout()
        if layout is not None:
            layout.activate()
        required = max(content.minimumWidth(), content.minimumSizeHint().width())
        # The measured difference includes actual native scrollbar/frame width.
        # Two layout pixels of breathing room prevent integer-rounding feedback.
        target = max(self._minimum_sidebar_width,
                     self._sidebar.width() + required - self.viewport().width() + 2)
        if target != self._sidebar.width():
            self._sidebar.setFixedWidth(target)
        if required <= self.viewport().width():
            self.horizontalScrollBar().setValue(0)
