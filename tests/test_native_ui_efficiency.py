"""Real Qt presentation fixtures; no backend or maintenance is started."""
import math
import os

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

import pytest
from PySide6.QtCore import QCoreApplication, QEvent, QObject, Qt
from PySide6.QtGui import QFont, QImage, QTransform
from PySide6.QtWidgets import QApplication

from optimus.ui import StarfieldCanvas, WorkflowSummaryLabel
from optimus.widgets.loading import RunSpinner, color_for_pct


@pytest.fixture(scope='module')
def app():
    return QApplication.instance() or QApplication([])


class StyleEvents(QObject):
    def __init__(self, widget):
        super().__init__(widget)
        self.count = 0
        widget.installEventFilter(self)

    def eventFilter(self, watched, event):
        if event.type() == QEvent.StyleChange:
            self.count += 1
        return False


def test_spinner_heartbeats_do_not_repolish_unchanged_percentage(app):
    spinner = RunSpinner()
    try:
        spinner.start('Install exact package')
        spinner.set_pct(25)
        events = StyleEvents(spinner._label)
        expected = spinner._label.text(), spinner._label.styleSheet(), spinner.toolTip()
        for _ in range(1000):
            spinner.set_pct(25.1)
            spinner.set_text('Install exact package')
        assert events.count == 0
        assert (spinner._label.text(), spinner._label.styleSheet(), spinner.toolTip()) == expected
        spinner.set_pct(50)
        assert events.count == 1
        assert spinner._label.text() == 'Install exact package  50%'
        assert spinner._ring._color == color_for_pct(50)
        spinner.set_text('Verify exact package')
        assert events.count == 1
        assert spinner.toolTip() == spinner._label.text() == 'Verify exact package  50%'
        style_at_50 = spinner._label.styleSheet()
        spinner._label.setStyleSheet('font-size:30px;color:#ffffff;')
        spinner.set_pct(50)
        assert events.count == 3
        assert spinner._label.styleSheet() == style_at_50
        spinner.succeed('Verified')
        assert spinner._label.text() == '✓  Verified  100%'
        spinner.start('Next package')
        assert spinner._label.text() == 'Next package  0%'
        assert spinner._ring._color == color_for_pct(0)
    finally:
        spinner.stop()
        spinner.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)


def test_summary_heartbeats_skip_fitting_but_font_resize_and_data_still_refit(app):
    label = WorkflowSummaryLabel('Initial', max_lines=2)
    label.resize(180, 40)
    label.show()
    app.processEvents()
    calls = []
    original = label._fit_summary

    def fit():
        calls.append(True)
        original()

    label._fit_summary = fit
    try:
        label.set_summary('A longer current package status that should wrap or elide', 'Full accessible package status')
        visible = label.visible_text
        calls.clear()
        for _ in range(1000):
            label.set_summary('A longer current package status that should wrap or elide', 'Full accessible package status')
        assert calls == []
        assert label.visible_text == visible
        assert label.text() == label.toolTip() == label.accessibleDescription() == label.whatsThis() == 'Full accessible package status'
        label.resize(400, 40)
        app.processEvents()
        assert calls
        assert label.visible_text != visible
        calls.clear()
        font = QFont(label.font())
        font.setPointSize(22)
        label.setFont(font)
        app.processEvents()
        assert calls
        calls.clear()
        label.set_summary('A different compact summary', 'Full accessible package status')
        assert calls
        assert label.text() == 'Full accessible package status'
        calls.clear()
        label.set_summary('A different compact summary', 'Changed complete accessible description')
        assert calls
        assert label.toolTip() == label.accessibleDescription() == label.whatsThis() == 'Changed complete accessible description'
    finally:
        label.close()
        label.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)


@pytest.mark.parametrize('size', [(320, 280), (1280, 820), (1920, 1080)])
@pytest.mark.parametrize('phase', [0, 0.5, 3.9, 21.7])
def test_precomputed_spirals_keep_the_original_geometry(app, size, phase):
    canvas = StarfieldCanvas(motion=False)
    try:
        canvas.resize(*size)
        w, h = size
        extent = max(w, h) * 0.72
        assert len(canvas._spiral) == 160 and len(canvas.stars) == 190
        for strand in range(3):
            transform = QTransform().translate(w * .5, h * .5).scale(extent, extent * .63)
            transform.rotateRadians(phase + strand * math.tau / 3)
            points = transform.map(canvas._spiral)
            for index, point in enumerate(points):
                t = index / 159
                radius = extent * (.16 + t * .72)
                angle = t * 5.3 + phase + strand * math.tau / 3
                assert math.isclose(point.x(), w * .5 + math.cos(angle) * radius, abs_tol=1e-9)
                assert math.isclose(point.y(), h * .5 + math.sin(angle) * radius * .63, abs_tol=1e-9)
        canvas.phase = phase
        image = QImage(w, h, QImage.Format_ARGB32_Premultiplied)
        image.fill(Qt.transparent)
        canvas.render(image)
        assert image.pixelColor(w // 2, h // 2).alpha() == 255
        assert not canvas.timer.isActive()
    finally:
        canvas.close()
        canvas.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)
