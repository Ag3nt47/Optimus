"""Legible, unanimated confirmation prompts with an explicit negative default."""
from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QMessageBox


_QUESTION_STYLE = """
QMessageBox { background-color: #0f141c; color: #f1f5f9; }
QMessageBox QLabel { background: transparent; color: #f1f5f9; font-size: 13px; }
QMessageBox QPushButton {
    background-color: #1d2a3b; color: #f1f5f9;
    border: 1px solid #657b96; border-radius: 6px;
    min-width: 140px; min-height: 24px; padding: 8px 14px;
    font-size: 13px; font-weight: 600;
}
QMessageBox QPushButton:hover {
    background-color: #294866; color: #ffffff; border-color: #a4d6ff;
}
QMessageBox QPushButton:focus, QMessageBox QPushButton:default {
    background-color: #21354c; color: #ffffff; border: 2px solid #9ed3ff;
}
QMessageBox QPushButton:pressed {
    background-color: #12324d; color: #ffffff; border-color: #a4d6ff;
}
"""


def make_yes_no_dialog(parent, title: str, text: str, *,
                       yes_label: str = 'Yes — apply',
                       no_label: str = 'No — skip') -> QMessageBox:
    """Use Qt-painted, explicitly labelled buttons; never animate their mask."""
    dialog = QMessageBox(parent)
    dialog.setOption(QMessageBox.Option.DontUseNativeDialog, True)
    dialog.setProperty('_optimus_no_reveal', True)
    dialog.setWindowTitle(title)
    dialog.setTextFormat(Qt.PlainText)
    dialog.setIcon(QMessageBox.Question)
    dialog.setText(text)
    dialog.setStyleSheet(_QUESTION_STYLE)
    dialog.setStandardButtons(QMessageBox.Yes | QMessageBox.No)
    for standard, label in ((QMessageBox.Yes, yes_label), (QMessageBox.No, no_label)):
        button = dialog.button(standard)
        button.setText(label)
        button.setAccessibleName(label)
        button.setMinimumSize(max(160, button.fontMetrics().horizontalAdvance(label) + 32), 42)
        button.setAutoDefault(True)
    dialog.setDefaultButton(QMessageBox.No)
    dialog.setEscapeButton(QMessageBox.No)
    dialog.button(QMessageBox.No).setFocus(Qt.OtherFocusReason)
    return dialog


def ask_yes_no(parent, title: str, text: str, **labels) -> bool:
    dialog = make_yes_no_dialog(parent, title, text, **labels)
    try:
        return dialog.exec() == QMessageBox.Yes
    finally:
        dialog.deleteLater()
