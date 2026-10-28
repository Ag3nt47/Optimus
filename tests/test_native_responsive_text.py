"""Presentation fonts retain accessibility choices without repeated repolish."""
from PySide6.QtCore import QEvent, QObject
from PySide6.QtGui import QFontInfo
from PySide6.QtWidgets import QLabel, QWidget

from optimus.widgets.responsive_text import scale_text, set_scaled_style
from optimus.widgets.theme import build_qss
from test_native_worker_cards import app, fonts


def test_inherited_accessible_font_and_local_colour_survive_expand_contract(app):
    owner = QWidget()
    owner.setStyleSheet(build_qss() + '\nQLabel {font-size:26px;}')
    label = QLabel('Readable status', owner)
    label.setStyleSheet('color:#e8b34b;font-weight:700;')
    original_style = label.styleSheet()
    label.ensurePolished()
    try:
        for _ in range(3):
            scale_text(label, 1.35)
            assert QFontInfo(label.font()).pixelSize() == 35
            assert label.font().bold()
            assert label.palette().color(label.foregroundRole()).name() == '#e8b34b'
            scale_text(label, 1.0)
            assert QFontInfo(label.font()).pixelSize() == 26
            assert label.styleSheet() == original_style
        scale_text(label, 1.35)
        owner.setStyleSheet(build_qss() + '\nQLabel {font-size:30px;}')
        scale_text(label, 1.35, force=True)
        assert QFontInfo(label.font()).pixelSize() == 40
        scale_text(label, 1)
        assert QFontInfo(label.font()).pixelSize() == 30
    finally:
        owner.close()
        owner.deleteLater()
        app.processEvents()


def test_identical_status_updates_do_not_trigger_another_stylesheet_pass(app):
    class StyleCounter(QObject):
        count = 0

        def eventFilter(self, watched, event):
            if event.type() == QEvent.StyleChange:
                self.count += 1
            return False

    owner = QWidget()
    owner.setStyleSheet(build_qss())
    label = QLabel('Installing', owner)
    label.ensurePolished()
    counter = StyleCounter(label)
    label.installEventFilter(counter)
    try:
        set_scaled_style(label, 'color:#55c987;', 1.35)
        initial = counter.count
        initial_pixels = QFontInfo(label.font()).pixelSize()
        for _ in range(100):
            set_scaled_style(label, 'color:#55c987;', 1.35)
        assert counter.count == initial
        assert QFontInfo(label.font()).pixelSize() == initial_pixels
        set_scaled_style(label, 'color:#ef6262;', 1.35)
        assert label.palette().color(label.foregroundRole()).name() == '#ef6262'
        assert QFontInfo(label.font()).pixelSize() == initial_pixels
    finally:
        owner.close()
        owner.deleteLater()
        app.processEvents()
