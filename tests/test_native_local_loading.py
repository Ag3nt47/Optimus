"""The requested decorative GIF stays bounded, local and separate from real progress."""
from pathlib import Path
import struct
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from PySide6.QtCore import QCoreApplication, QEvent, Qt
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
    widget.start()
    assert widget._movie.state() == QMovie.Running
    QTest.qWait(220)
    assert widget._movie.currentFrameNumber() > 0
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
    assert len(progress.worker_cards) == 3


def test_progress_completion_stops_icon_and_followup_can_restart_it(progress, app, monkeypatch):
    monkeypatch.setattr('optimus.widgets.local_loading.motion_allowed', lambda *args: True)
    progress.loading_animation.sync_playback()
    assert progress.loading_animation._movie.state() == QMovie.Running
    progress.finish({'status': 'completed', 'stages': []})
    assert progress.loading_animation._movie.state() == QMovie.NotRunning
    assert not progress.loading_animation.isVisible()
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
    assert (icon.width(), icon.height()) == (128, 117)
    header = icon.parentWidget()
    assert all(widget.parentWidget() is header for widget in (progress.title, progress.spinner,
        progress.progress, progress.stage, progress.description))
    assert progress.height() == 650 and header.height() < 170
    assert progress.title.geometry().right() < icon.geometry().left()
    cards = list(progress.worker_cards.values())
    assert max(card.width() for card in cards) - min(card.width() for card in cards) <= 1
    assert all(card.geometry().top() > header.geometry().bottom() for card in cards)
    assert all(card.isVisible() and card.geometry().bottom() < progress.cancel_button.geometry().top() for card in cards)
    assert progress.log.height() >= 45 and progress.processes.height() >= 45
    assert progress.cancel_button.geometry().bottom() <= 650 and progress.cancel_button.isEnabled()


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

