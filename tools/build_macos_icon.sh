#!/bin/sh
# Regenerate src/optimus/assets/optimus.icns from optimus.svg (macOS only).
# Needs Python with PySide6 (QtSvg); uses the built-in sips and iconutil.
set -eu
ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
PYTHON=${PYTHON:-python3}
WORK=$(mktemp -d)
trap 'rm -rf "$WORK"' EXIT
QT_QPA_PLATFORM=offscreen "$PYTHON" - "$ROOT/src/optimus/assets/optimus.svg" "$WORK/master.png" <<'PY'
import sys
from PySide6.QtCore import Qt, QRectF
from PySide6.QtGui import QGuiApplication, QImage, QPainter
from PySide6.QtSvg import QSvgRenderer
app = QGuiApplication([])
renderer = QSvgRenderer(sys.argv[1])
assert renderer.isValid()
image = QImage(1024, 1024, QImage.Format_ARGB32)
image.fill(Qt.transparent)
painter = QPainter(image)
painter.setRenderHint(QPainter.Antialiasing)
renderer.render(painter, QRectF(100, 100, 824, 824))  # macOS icon grid inset
painter.end()
assert image.save(sys.argv[2])
PY
mkdir "$WORK/Optimus.iconset"
for size in 16 32 128 256 512; do
    sips -z "$size" "$size" "$WORK/master.png" --out "$WORK/Optimus.iconset/icon_${size}x${size}.png" >/dev/null
    double=$((size * 2))
    sips -z "$double" "$double" "$WORK/master.png" --out "$WORK/Optimus.iconset/icon_${size}x${size}@2x.png" >/dev/null
done
iconutil -c icns "$WORK/Optimus.iconset" -o "$ROOT/src/optimus/assets/optimus.icns"
printf '%s\n' "Wrote $ROOT/src/optimus/assets/optimus.icns"
