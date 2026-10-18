# Adapted from AngeronaSuite sandbox_editor.py, copyright2026Angerona contributors (MIT).
from typing import List
from PySide6.QtCore import QRegularExpression
from PySide6.QtGui import QColor, QFont, QSyntaxHighlighter, QTextCharFormat
class PythonHighlighter(QSyntaxHighlighter):
    """Minimal but professional Python highlighter (keywords, strings, comments,
    decorators, numbers, def/class names)."""

    _KEYWORDS = (
        "False None True and as assert async await break class continue def del "
        "elif else except finally for from global if import in is lambda nonlocal "
        "not or pass raise return try while with yield"
    ).split()

    def __init__(self, document) -> None:
        super().__init__(document)
        self._rules: List[tuple[QRegularExpression, QTextCharFormat]] = []

        def fmt(hex_color: str, bold: bool = False, italic: bool = False) -> QTextCharFormat:
            f = QTextCharFormat()
            f.setForeground(QColor(hex_color))
            if bold:
                f.setFontWeight(QFont.Bold)
            f.setFontItalic(italic)
            return f

        kw_fmt = fmt("#38bdf8", bold=True)
        for kw in self._KEYWORDS:
            self._rules.append((QRegularExpression(rf"\b{kw}\b"), kw_fmt))

        self._rules.append((QRegularExpression(r"\bdef\s+(\w+)"), fmt("#c084fc", bold=True)))
        self._rules.append((QRegularExpression(r"\bclass\s+(\w+)"), fmt("#f59e0b", bold=True)))
        self._rules.append((QRegularExpression(r"@\w+"), fmt("#f472b6")))            # decorators
        self._rules.append((QRegularExpression(r"\b[0-9]+\.?[0-9]*\b"), fmt("#fbbf24")))  # numbers
        # strings (single/double, incl. simple f/r prefixes)
        self._rules.append((QRegularExpression(r"[rbfRBF]?'[^'\\]*(\\.[^'\\]*)*'"), fmt("#34d399")))
        self._rules.append((QRegularExpression(r'[rbfRBF]?"[^"\\]*(\\.[^"\\]*)*"'), fmt("#34d399")))
        self._comment_fmt = fmt("#64748b", italic=True)

    def highlightBlock(self, text: str) -> None:  # noqa: N802 (Qt signature)
        for rx, f in self._rules:
            it = rx.globalMatch(text)
            while it.hasNext():
                m = it.next()
                # If the rule captured a group (def/class name) prefer it.
                if m.lastCapturedIndex() >= 1:
                    self.setFormat(m.capturedStart(1), m.capturedLength(1), f)
                else:
                    self.setFormat(m.capturedStart(), m.capturedLength(), f)
        # Comments last so they win over everything on the line.
        hs = text.find("#")
        if hs >= 0:
            self.setFormat(hs, len(text) - hs, self._comment_fmt)



