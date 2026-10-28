"""Window-local presentation scaling never runs maintenance or saves choices."""
import os
import weakref

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

import pytest
from PySide6.QtCore import QCoreApplication, QEvent
from PySide6.QtGui import QFont
from PySide6.QtWidgets import QApplication, QLabel, QWidget

from optimus.widgets.responsive import ResponsiveWindowScale, window_scale


@pytest.fixture(scope='module')
def app():
    return QApplication.instance() or QApplication([])


def settle(app):
    for _ in range(12):
        app.processEvents()


@pytest.mark.parametrize('size,expected', [
    ((320, 280), 1.0), ((850, 650), 1.0), ((900, 700), 1.05),
    ((1000, 780), 1.2), ((1920, 450), 1.0), ((480, 1440), 1.0),
    ((1920, 1080), 1.35), ((2560, 1440), 1.35), ((3840, 2160), 1.35),
    ((float('nan'), 800), 1.0), ((1000, float('inf')), 1.0),
])
def test_scale_uses_both_dimensions_and_a_readable_bounded_band(size, expected):
    assert window_scale(*size) == expected


def test_custom_baseline_and_quantized_neighbors():
    assert window_scale(1920, 1080, baseline=(1280, 820)) == 1.3
    assert window_scale(940, 720) == window_scale(941, 721) == 1.1
    assert window_scale('unavailable', 800) == 1.0
    for baseline in ((0, 650), (850, -1), (float('inf'), 650), (850,), None):
        with pytest.raises(ValueError):
            window_scale(850, 650, baseline)


def test_initial_callback_is_deferred_and_resize_bursts_are_coalesced(app):
    window = QWidget()
    window.resize(850, 650)
    calls = []
    helper = ResponsiveWindowScale(window, calls.append)
    try:
        assert calls == [] and helper.parent() is window
        settle(app)
        assert calls == [1.0]
        window.show()
        window.resize(1000, 780)
        window.resize(1920, 1080)
        settle(app)
        assert calls == [1.0, 1.35]
        window.resize(1921, 1081)
        settle(app)
        assert calls == [1.0, 1.35, 1.35]
        window.resize(850, 650)
        settle(app)
        assert calls == [1.0, 1.35, 1.35, 1.0]
    finally:
        window.close()
        window.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)


def test_callback_style_and_geometry_updates_do_not_start_an_event_loop(app):
    window = QWidget()
    window.resize(850, 650)
    label = QLabel('Presentation only', window)
    calls = []

    def restyle(scale):
        calls.append(scale)
        window.setStyleSheet(f'QLabel {{font-size:{round(13 * scale)}px;}}')
        window.resize(window.width() + 1, window.height() + 1)
        label.resize(200, 30)

    helper = ResponsiveWindowScale(window, restyle)
    try:
        window.show()
        settle(app)
        assert calls == [1.0]
        settle(app)
        assert calls == [1.0] and not helper._timer.isActive()
        window.resize(1920, 1080)
        settle(app)
        assert calls == [1.0, 1.35]
        settle(app)
        assert calls == [1.0, 1.35] and not helper._timer.isActive()
    finally:
        window.close()
        window.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)


def test_external_font_and_inherited_theme_changes_reapply_once(app):
    owner = QWidget()
    window = QWidget(owner)
    window.resize(850, 650)
    calls = []
    helper = ResponsiveWindowScale(window, calls.append)
    try:
        settle(app)
        baseline = len(calls)
        font = QFont(window.font())
        font.setPixelSize(26)
        window.setFont(font)
        settle(app)
        assert len(calls) == baseline + 1 and calls[-1] == 1.0
        baseline = len(calls)
        owner.setStyleSheet('QLabel { color: #fafafa; }')
        settle(app)
        assert len(calls) == baseline + 1
        settle(app)
        assert len(calls) == baseline + 1 and not helper._timer.isActive()
    finally:
        owner.close()
        owner.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)


def test_filter_is_window_local_and_destroying_window_discards_queued_callback(app):
    first, other = QWidget(), QWidget()
    calls = []
    observed = []

    class ObservedScale(ResponsiveWindowScale):
        def eventFilter(self, watched, event):
            observed.append(watched)
            return super().eventFilter(watched, event)

    helper = ObservedScale(first, calls.append)
    settle(app)
    before = list(calls)
    other.resize(1920, 1080)
    app.sendEvent(other, QEvent(QEvent.StyleChange))
    settle(app)
    assert calls == before
    assert other not in observed
    app.sendEvent(first, QEvent(QEvent.StyleChange))
    assert helper._timer.isActive()
    reference = weakref.ref(helper)
    first.deleteLater()
    QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)
    del helper
    settle(app)
    assert reference() is None
    assert calls == before
    other.deleteLater()
    QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)


def test_state_change_reapplies_at_the_same_scale_without_polling(app):
    window = QWidget()
    window.resize(850, 650)
    calls = []
    helper = ResponsiveWindowScale(window, calls.append)
    try:
        window.show()
        settle(app)
        baseline = len(calls)
        window.showMinimized()
        settle(app)
        assert len(calls) == baseline + 1
        window.showNormal()
        settle(app)
        assert len(calls) == baseline + 2
        assert not helper._timer.isActive()
    finally:
        window.close()
        window.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)
