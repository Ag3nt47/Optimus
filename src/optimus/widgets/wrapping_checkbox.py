"""A normal accessible checkbox whose label wraps in narrow settings pages."""
from PySide6.QtCore import QRect, QSize, Qt
from PySide6.QtGui import QPalette
from PySide6.QtWidgets import QCheckBox, QSizePolicy, QStyle, QStyleOptionButton, QStylePainter


class WrappingCheckBox(QCheckBox):
    def __init__(self, text='', parent=None):
        super().__init__(text, parent)
        policy = QSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        policy.setHeightForWidth(True)
        self.setSizePolicy(policy)

    def hasHeightForWidth(self):
        return True

    def heightForWidth(self, width):
        label = self.fontMetrics().boundingRect(QRect(0, 0, max(40, width - 36), 10000),
            Qt.TextWordWrap, self.text())
        return max(32, label.height() + 12)

    def minimumSizeHint(self):
        return QSize(120, 32)

    def sizeHint(self):
        width = min(440, max(120, super().sizeHint().width()))
        return QSize(width, self.heightForWidth(width))

    def hitButton(self, position):
        return self.rect().contains(position)

    def paintEvent(self, event):
        option = QStyleOptionButton()
        self.initStyleOption(option)
        option.text = ''
        painter = QStylePainter(self)
        painter.drawControl(QStyle.CE_CheckBox, option)
        content = self.style().subElementRect(QStyle.SE_CheckBoxContents, option, self)
        group = QPalette.Active if self.isEnabled() else QPalette.Disabled
        painter.setPen(option.palette.color(group, QPalette.WindowText))
        painter.drawText(content, Qt.AlignVCenter | Qt.TextWordWrap, self.text())
