"""The requested decorative GIF stays bounded, local and separate from real progress."""
from pathlib import Path
import struct
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from PySide6.QtCore import QCoreApplication, QEvent, QPoint, Qt
from PySide6.QtGui import QMovie
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QWidget

from optimus.widgets.local_loading import DEFAULT_ASSET, MAX_BYTES, MAX_DECODED_BYTES, MAX_DISPLAY_EXTENT, LocalLoadingAnimation
from test_native_worker_cards import app, fonts, progress


@pytest.fixture
def animation(app, monkeypatch):
    monkeypatch.setattr('optimus.widgets.local_loading.motion_allowed', lambda *args: True)
    owner = QWidget()
    owner.setAttribute(Qt.WA_DontShowOnScreen, True)
    owner.config = SimpleNamespace(ui_motion_enabled=True)
    value = LocalLoadingAnimation(owner, extent=128)
    owner.resize(150, 120)
    owner.show()
    app.processEvents()
    yield owner, value
    value.stop()
    owner.close()
    owner.deleteLater()
    QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)
    app.processEvents()


def test_bundled_animation_is_small_local_bounded_and_credited(animation, monkeypatch):
    import urllib.request
    forbidden = Mock(side_effect=AssertionError('Loading decorations must not use the network.'))
    monkeypatch.setattr(urllib.request, 'urlopen', forbidden)
    owner, widget = animation
    assert widget.ready and DEFAULT_ASSET.stat().st_size < 256 * 1024
    assert widget._movie.cacheMode() == QMovie.CacheAll
    assert 96 * 88 * widget._movie.frameCount() * 4 <= MAX_DECODED_BYTES
    assert (widget._movie.scaledSize().width(), widget._movie.scaledSize().height()) == (128, 117)
    assert 128 * 117 * widget._movie.frameCount() * 4 <= MAX_DECODED_BYTES
    assert widget._movie.frameCount() > 1
    assert 'Decorative' in widget.accessibleDescription()
    notice = DEFAULT_ASSET.with_name('RACCOON-NOTICE.txt').read_text(encoding='utf-8')
    assert 'Steveo0825' in notice and 'https://tenor.com/view/' in notice
    assert 'separate from Optimus' in notice and 'does not' in notice
    advanced = []
    widget._movie.frameChanged.connect(lambda frame: advanced.append(frame) if frame > 0 else None)
    widget.start()
    assert widget._movie.state() == QMovie.Running
    for unused in range(10):
        if advanced:
            break
        QTest.qWait(100)
    assert advanced, 'The actual movie timer must advance beyond the initial frame within one second.'
    # Observe a full loop through the actual timer, including its rewind.
    widget._movie.setSpeed(1000)
    QTest.qWait(800)
    assert widget.ready and widget._movie.state() == QMovie.Running
    forbidden.assert_not_called()


def test_hiding_and_minimizing_pause_playback_and_restore_resumes(animation, app):
    owner, widget = animation
    widget.start()
    assert widget._movie.state() == QMovie.Running
    owner.hide()
    app.processEvents()
    assert widget._movie.state() != QMovie.Running
    frame = widget._movie.currentFrameNumber()
    QTest.qWait(220)
    assert widget._movie.currentFrameNumber() == frame
    owner.show()
    app.processEvents()
    assert widget._movie.state() == QMovie.Running
    owner.showMinimized()
    app.processEvents()
    assert widget._movie.state() != QMovie.Running
    owner.showNormal()
    app.processEvents()
    assert widget._movie.state() == QMovie.Running
    widget.stop()
    assert widget._movie.state() == QMovie.NotRunning and not widget.isVisible()


def test_reduced_motion_uses_static_raccoon_poster(animation, app, monkeypatch):
    owner, widget = animation
    monkeypatch.setattr('optimus.widgets.local_loading.motion_allowed', lambda *args: False)
    widget.start()
    assert widget.isVisible() and not widget.pixmap().isNull()
    assert widget._movie.state() != QMovie.Running
    frame = widget._movie.currentFrameNumber()
    QTest.qWait(100)
    assert widget._movie.currentFrameNumber() == frame


@pytest.mark.parametrize('kind', ['missing', 'url', 'network-share', 'large', 'large-canvas', 'large-frame-budget', 'invalid'])
def test_invalid_decorations_fail_quietly_without_disrupting_progress(progress, app, tmp_path, kind):
    path = tmp_path / 'bad.gif'
    if kind == 'url':
        target = 'https://example.invalid/loading.gif'
    elif kind == 'network-share':
        target = r'\\example.invalid\loading.gif'
    else:
        target = path
        if kind == 'large':
            path.write_bytes(b'x' * (MAX_BYTES + 1))
        elif kind in ('large-canvas', 'large-frame-budget'):
            blob = bytearray(DEFAULT_ASSET.read_bytes())
            dimension = 300 if kind == 'large-canvas' else 256
            blob[6:10] = struct.pack('<HH', dimension, dimension)
            path.write_bytes(blob)
        elif kind == 'invalid':
            path.write_bytes(b'not a GIF')
    widget = progress.loading_animation
    widget.set_asset(target)
    widget.start()
    assert not widget.ready and not widget.isVisible() and widget.error_message
    progress.update_progress({'stage': 'Fixture resource review', 'percentage': 35})
    assert progress.spinner.isVisible() and progress.progress.value() == 35
    assert set(progress.worker_cards) == {'maintenance', 'software_updates', 'driver_updates', 'security_scan'}
    assert all(card.isVisibleTo(progress) for card in progress.worker_cards.values())


def test_progress_completion_stops_icon_and_followup_can_restart_it(progress, app, monkeypatch):
    monkeypatch.setattr('optimus.widgets.local_loading.motion_allowed', lambda *args: True)
    progress.details_toggle.setChecked(True)
    app.processEvents()
    progress.loading_animation.sync_playback()
    assert progress.loading_animation._movie.state() == QMovie.Running
    progress.finish({'status': 'completed', 'stages': []})
    assert progress.loading_animation._movie.state() == QMovie.NotRunning
    assert progress.loading_animation.isVisible()  # A static local poster remains in the visible information panel.
    import threading
    progress.begin_auto_continue(threading.Event())
    assert progress.loading_animation.isVisible()
    assert progress.loading_animation._movie.state() == QMovie.Running
    QTest.qWait(220)
    assert progress.loading_animation.ready
    assert progress.loading_animation._movie.currentFrameNumber() > 0


def test_large_loading_icon_does_not_push_worker_panels_or_controls_out_of_compact_view(progress, app):
    progress.resize(850, 650)
    progress.update_progress({'stage': 'Waiting for background updates', 'waiting_for': 'installer_updates',
        'percentage': 40, 'description': 'Independent maintenance is complete. Background updates must finish before final verification.',
        'jobs': [{'id': 'software_updates', 'status': 'installing', 'current': 0, 'total': 2,
                  'description': 'Installing approved fixture app.'},
                 {'id': 'driver_updates', 'status': 'waiting_for_installer', 'current': 0, 'total': 1,
                  'description': 'The application installer owns the shared installer lane.'}]})
    app.processEvents()
    icon = progress.loading_animation
    assert (icon.width(), icon.height()) == (64, 58)
    header = progress._progress_header
    assert progress.analysis_panel.isAncestorOf(icon)
    assert not progress.content_scroll.isAncestorOf(icon)
    assert not header.isAncestorOf(icon)
    assert all(widget.parentWidget() is header for widget in (progress.title, progress.spinner,
        progress.progress, progress.resource_comparison))
    comparison = progress.resource_comparison
    assert progress.height() == 650
    assert header.height() - comparison.height() - header.layout().verticalSpacing() < 170
    assert progress.progress.geometry().bottom() < comparison.geometry().top()
    assert comparison.mapTo(progress, comparison.rect().bottomLeft()).y() < progress.worker_grid.mapTo(progress, QPoint()).y()
    cards = list(progress.worker_cards.values())
    assert max(card.width() for card in cards) - min(card.width() for card in cards) <= 1
    header_bottom = header.mapTo(progress, header.rect().bottomLeft()).y()
    cancel_top = progress.cancel_button.mapTo(progress, progress.cancel_button.rect().topLeft()).y()
    assert all(card.mapTo(progress, card.rect().topLeft()).y() > header_bottom for card in cards)
    # The real local animation and live information stay visible independently
    # of the optional process/log section at the normal dashboard size.
    assert set(progress.worker_cards) == {'maintenance', 'software_updates', 'driver_updates', 'security_scan'}
    assert progress.content_scroll.horizontalScrollBar().maximum() == 0
    assert progress.body_scroll.verticalScrollBar().maximum() == 0
    assert icon.isVisibleTo(progress) and icon.ready
    for card in cards:
        assert card.isVisibleTo(progress)
        assert progress.rect().contains(card.rect().translated(card.mapTo(progress, QPoint())))
        for button in card.navigation_buttons.values():
            assert progress.rect().contains(button.rect().translated(button.mapTo(progress, QPoint())))
            assert button.mapTo(progress, button.rect().bottomLeft()).y() < cancel_top
    progress.details_toggle.setChecked(True)
    for _ in range(3):
        app.processEvents()
    assert icon.isVisibleTo(progress) and icon.ready
    progress.body_scroll.ensureWidgetVisible(icon)
    for _ in range(3):
        app.processEvents()
    assert progress.body_scroll.viewport().rect().contains(icon.rect().translated(icon.mapTo(progress.body_scroll.viewport(), QPoint())))
    assert progress.rect().contains(progress.cancel_button.rect().translated(progress.cancel_button.mapTo(progress, QPoint())))
    assert progress.cancel_button.isEnabled()


def test_display_extent_is_capped_and_scaled_frames_must_fit_the_existing_memory_budget(animation, monkeypatch):
    owner, widget = animation
    capped = LocalLoadingAnimation(owner, extent=4096)
    assert capped.ready and capped.width() == MAX_DISPLAY_EXTENT
    capped.stop()
    # The source frames still fit 2 MiB, but the 128px scaled frames do not.
    monkeypatch.setattr('optimus.widgets.local_loading.MAX_DECODED_BYTES', 2 * 1024 * 1024)
    small = LocalLoadingAnimation(owner, extent=64)
    large = LocalLoadingAnimation(owner, extent=128)
    assert small.ready and not large.ready and 'decoded-frame budget' in large.error_message

