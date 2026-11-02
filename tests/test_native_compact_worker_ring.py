"""Actual painted-caption bounds fit the compact ring without ellipsis."""
import os

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

import pytest
from PySide6.QtCore import QRectF
from PySide6.QtGui import QFont
from PySide6.QtWidgets import QApplication

from optimus.widgets.compact_worker_ring import CompactWorkerActivityRing
from test_native_worker_cards import fonts


@pytest.fixture(scope='module')
def app():
    return QApplication.instance() or QApplication([])


@pytest.mark.parametrize('phase', ['', 'download', 'install', 'uninstall', 'backup', 'verify',
    'scan', 'cleanup', 'optimize', 'prepare', 'working', 'waiting', 'driver_offers',
    'device_query', 'driver_records', 'system_query'])
@pytest.mark.parametrize('scale', [1., 1.35, 1.6])
def test_full_short_caption_and_glyphs_fit_actual_ring_space(app, phase, scale):
    ring = CompactWorkerActivityRing()
    try:
        ring.set_presentation_scale(scale)
        ring.set_progress(37, 'working', phase=phase, description='Full fixture operation detail.')
        presentation = ring.caption_presentation()
        assert presentation['text'] and '…' not in presentation['text']
        assert presentation['rectangle'].contains(presentation['bounds'])
        assert QRectF(0, 0, 48, 54).contains(presentation['bounds'])
        assert ring.size().toTuple() == (round(48 * scale), round(54 * scale))
        assert ring.value_text() == '37%'
        assert ring.phase_caption in ring.accessibleDescription()
        assert ring.toolTip() == 'Full fixture operation detail.'
        if not phase:
            assert ring.phase_caption == 'Current item' and presentation['text'] == 'Item'
        ring.show()
        app.processEvents()
        assert not ring.grab().isNull()
    finally:
        ring.close()
        ring.deleteLater()
        app.processEvents()


def test_late_large_font_does_not_clip_caption_or_replace_item_progress(app):
    ring = CompactWorkerActivityRing()
    try:
        ring.setFont(QFont('DejaVu Sans Mono', 28))
        ring.set_progress(99.9, 'working', phase='uninstall', estimated=True)
        presentation = ring.caption_presentation()
        assert presentation['rectangle'].contains(presentation['bounds'])
        assert presentation['text'] in ('Uninstall', 'Remove')
        assert ring.value_text() == '~99%'
    finally:
        ring.deleteLater()
        app.processEvents()
