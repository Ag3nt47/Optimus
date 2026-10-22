"""A bounded local GIF adds decoration without network access or progress claims."""
from __future__ import annotations

from pathlib import Path
import stat

from PySide6.QtCore import QBuffer, QByteArray, QEvent, QIODevice, QSize, Qt
from PySide6.QtGui import QImageReader, QMovie, QPixmap
from PySide6.QtWidgets import QLabel

from .header_controls import motion_allowed


MAX_BYTES = 2 * 1024 * 1024
MAX_CANVAS = 256
MAX_FRAMES = 300
MAX_DECODED_BYTES = 4 * 1024 * 1024
DEFAULT_ASSET = Path(__file__).resolve().parents[1] / 'assets' / 'raccoon-cane-dance.gif'


def _local_bytes(path):
    path = Path(path)
    if not path.is_absolute() or str(path).startswith(('\\\\', '//')):
        raise ValueError('The loading asset must be an ordinary local file.')
    info = path.lstat()
    if (not stat.S_ISREG(info.st_mode) or path.is_symlink()
            or getattr(info, 'st_file_attributes', 0) & 0x400 or info.st_size > MAX_BYTES):
        raise ValueError('The loading asset is indirect, unsupported or too large.')
    with path.open('rb') as stream:
        value = stream.read(MAX_BYTES + 1)
    if len(value) > MAX_BYTES:
        raise ValueError('The loading asset exceeds the size limit.')
    return value


class LocalLoadingAnimation(QLabel):
    """A supplementary icon: pause while hidden/minimized; honor reduced motion."""
    def __init__(self, parent=None, *, asset_path=DEFAULT_ASSET, extent=64):
        super().__init__(parent)
        self.setAlignment(Qt.AlignCenter)
        self.setAccessibleName('Dancing raccoon with a cane loading icon')
        self.setAccessibleDescription('Decorative animation. The worker panels report actual operation progress.')
        self.setToolTip('Racoon Dance Cane Dance — Tenor / Steveo0825. See the bundled RACCOON-NOTICE.txt.')
        self._extent = max(16, min(96, int(extent)))
        self._movie = None
        self._buffer = None
        self._poster = QPixmap()
        self._requested = False
        self._host = None
        self.ready = False
        self.error_message = ''
        self.hide()
        self.set_asset(asset_path)

    def set_asset(self, path):
        self.stop()
        self.ready = False
        self.error_message = ''
        if self._movie is not None:
            self._movie.deleteLater()
        if self._buffer is not None:
            self._buffer.deleteLater()
        self._movie = self._buffer = None
        self._poster = QPixmap()
        self.clear()
        buffer = movie = None
        try:
            blob = _local_bytes(path)
            if blob[:6] not in (b'GIF87a', b'GIF89a'):
                raise ValueError('The loading asset is not a GIF.')
            buffer = QBuffer(self)
            buffer.setData(QByteArray(blob))
            buffer.open(QIODevice.ReadOnly)
            reader = QImageReader(buffer, b'gif')
            size, frames = reader.size(), reader.imageCount()
            if (not size.isValid() or size.width() > MAX_CANVAS or size.height() > MAX_CANVAS
                    or not 1 <= frames <= MAX_FRAMES):
                raise ValueError('The loading asset exceeds the canvas or frame limits.')
            if size.width() * size.height() * frames * 4 > MAX_DECODED_BYTES:
                raise ValueError('The loading asset exceeds the decoded-frame budget.')
            image = reader.read()
            if image.isNull():
                raise ValueError('The loading asset could not be decoded.')
            reader = None
            scaled = size.scaled(QSize(self._extent, self._extent), Qt.KeepAspectRatio)
            self.setFixedSize(scaled)
            self._poster = QPixmap.fromImage(image).scaled(scaled, Qt.KeepAspectRatio, Qt.SmoothTransformation)
            # Only the bundled asset has an accompanying trusted static poster.
            if Path(path) == DEFAULT_ASSET:
                poster = QImageReader(str(DEFAULT_ASSET.with_suffix('.png')))
                if poster.size().isValid() and max(poster.size().width(), poster.size().height()) <= MAX_CANVAS:
                    static = poster.read()
                    if not static.isNull():
                        self._poster = QPixmap.fromImage(static).scaled(scaled, Qt.KeepAspectRatio, Qt.SmoothTransformation)
            buffer.seek(0)
            movie = QMovie(self)
            movie.setDevice(buffer)
            movie.setFormat(b'gif')
            # A bounded cache avoids Qt's QBuffer/CacheNone loop replay errors.
            # The complete source frames above fit within a 4 MiB pixel budget.
            movie.setCacheMode(QMovie.CacheAll)
            movie.setScaledSize(scaled)
            if not movie.isValid():
                raise ValueError('The local GIF animation is unavailable.')
            self._buffer, self._movie = buffer, movie
            movie.error.connect(self._decode_failed)
            self.ready = True
            self.setPixmap(self._poster)
        except (OSError, ValueError, RuntimeError, TypeError) as error:
            for item in (movie, buffer):
                if item is not None:
                    item.deleteLater()
            self.error_message = str(error)[:400]
            self.hide()

    def _decode_failed(self, *unused):
        self.error_message = 'The decorative GIF could not continue; ordinary progress remains available.'
        self.stop()
        self.ready = False

    def _watch_host(self):
        host = self.window()
        if host is not self._host:
            if self._host is not None:
                self._host.removeEventFilter(self)
            self._host = host
            host.installEventFilter(self)

    def start(self):
        self._requested = True
        if not self.ready:
            self.hide()
            return
        self._watch_host()
        self.show()
        self.sync_playback()

    def stop(self):
        self._requested = False
        if self._movie is not None:
            self._movie.stop()
        self.setMovie(None)
        if not self._poster.isNull():
            self.setPixmap(self._poster)
        self.hide()

    def sync_playback(self):
        if self._movie is None:
            return
        host = self.window()
        visible = self._requested and self.isVisible() and host.isVisible() and not host.isMinimized()
        owner = self
        while owner is not None and not hasattr(owner, 'config'):
            owner = owner.parentWidget()
        animate = visible and motion_allowed(getattr(owner, 'config', None))
        if animate:
            self.setMovie(self._movie)
            if self._movie.state() == QMovie.Paused:
                self._movie.setPaused(False)
            elif self._movie.state() != QMovie.Running:
                self._movie.start()
        else:
            if self._movie.state() == QMovie.Running:
                self._movie.setPaused(True)
            if visible:
                self.setMovie(None)
                self.setPixmap(self._poster)

    def showEvent(self, event):
        super().showEvent(event)
        self.sync_playback()

    def hideEvent(self, event):
        if self._movie is not None and self._movie.state() == QMovie.Running:
            self._movie.setPaused(True)
        super().hideEvent(event)

    def eventFilter(self, watched, event):
        if watched is self._host and event.type() in (QEvent.WindowStateChange, QEvent.Show, QEvent.Hide):
            self.sync_playback()
        return False
