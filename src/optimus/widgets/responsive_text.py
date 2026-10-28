"""Scale a view's text without replacing its inherited theme or saved choices."""
from __future__ import annotations

from PySide6.QtGui import QFontInfo


def scale_text(widget, scale, *, force=False):
    """Apply a reversible local font override, measured from the current theme.

    The original local stylesheet is retained. Removing our previous override
    before measuring respects larger inherited accessibility fonts and prevents
    cumulative scaling through repeated expand/contract cycles.
    """
    current = widget.styleSheet()
    previous = getattr(widget, '_optimus_scaled_text', None)
    if (not force and previous and len(previous) == 3
            and current == previous[1] and scale == previous[2]):
        return
    original = previous[0] if previous and current == previous[1] else current
    if current != original:
        widget.setStyleSheet(original)
    if scale <= 1:
        widget._optimus_scaled_text = (original, original, scale)
        return
    widget.ensurePolished()
    pixels = max(1, QFontInfo(widget.font()).pixelSize())
    # Qt distinguishes declaration-only sheets from selector sheets. Keep the
    # existing colours, font family and emphasis valid in either representation.
    base = original if '{' in original or not original.strip() else 'QWidget {' + original + '}'
    rendered = base + '\nQWidget {font-size:' + str(round(pixels * min(1.35, scale))) + 'px;}'
    widget.setStyleSheet(rendered)
    widget._optimus_scaled_text = (original, rendered, scale)


def set_scaled_style(widget, style, scale):
    """Change status colours without repolishing identical progress snapshots."""
    previous = getattr(widget, '_optimus_scaled_text', None)
    if (previous and len(previous) == 3 and previous[0] == style
            and previous[1] == widget.styleSheet() and previous[2] == scale):
        return
    if widget.styleSheet() != style:
        widget.setStyleSheet(style)
    scale_text(widget, scale)
