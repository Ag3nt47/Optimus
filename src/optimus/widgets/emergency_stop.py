"""A reachable stop-and-exit control shared by the desktop surfaces."""
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QPushButton, QSizePolicy


class EmergencyStopButton(QPushButton):
    def __init__(self, owner, parent=None):
        super().__init__('STOP', parent)
        self.setObjectName('EmergencyStop')
        self.setAutoDefault(False)
        self.setDefault(False)
        self.setCursor(Qt.PointingHandCursor)
        self.setMinimumSize(66, 34)
        self.setSizePolicy(QSizePolicy.Minimum, QSizePolicy.Fixed)
        self.setAccessibleName('Stop Optimus and exit')
        self.setToolTip('Immediately stop new Optimus work and cancel supported operations, then exit. '
            'Active system or vendor installers must finish safely; unrelated programs stay open.')
        self.setAccessibleDescription(self.toolTip())
        self.setStyleSheet('QPushButton {background:#b32636;color:white;border:1px solid #ff7483;'
            'border-radius:7px;padding:7px 12px;font-weight:800;}'
            'QPushButton:hover {background:#d03948;}'
            'QPushButton:pressed {background:#8f1728;}'
            'QPushButton:focus {border:2px solid white;}'
            'QPushButton:disabled {background:#6b2330;color:#f3c7cf;border-color:#914451;}')
        callback = getattr(owner, 'request_emergency_stop', None)
        self.setEnabled(callable(callback))
        if callable(callback):
            self.clicked.connect(callback)
