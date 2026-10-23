"""Saved read-only free-space check controls; no allocation or cleanup."""
import math
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QDoubleSpinBox, QHBoxLayout, QLabel


class DriveGuardChoices:
    def __init__(self, layout, initial, *, add_check, add_note, controls=None):
        self.enabled = add_check('drive_space_guard_enabled', 'Check drive free space before and after maintenance')
        self.enabled.setChecked(initial.get('drive_space_guard_enabled', True) is True)
        value = initial.get('drive_space_guard_target_gib', 2.0)
        if type(value) not in (int, float) or not math.isfinite(value) or not 1 <= value <= 2:
            raise ValueError('Drive-space target must be a finite number from 1 to 2 GiB.')
        row = QHBoxLayout()
        label = QLabel('Free-space warning target')
        label.setTextFormat(Qt.PlainText)
        label.setWordWrap(True)
        row.addWidget(label, 1)
        self.target = QDoubleSpinBox()
        self.target.setMinimumHeight(30)
        self.target.setRange(1.0, 2.0)
        self.target.setDecimals(2)
        self.target.setSingleStep(0.25)
        self.target.setSuffix(' GiB')
        self.target.setValue(value)
        self.target.setAccessibleName('Drive-space warning target in GiB')
        self.target.setToolTip('Read-only free-space check from 1 to 2 GiB. It does not reserve disk space, allocate a file, move files or authorize deletion.')
        self.target.setEnabled(self.enabled.isChecked())
        self.enabled.toggled.connect(self.target.setEnabled)
        row.addWidget(self.target)
        layout.addLayout(row)
        add_note('This is a warning threshold, not an exclusive storage reservation. Optimus checks supported drives '
            'before and after maintenance and reports free space and shortfall. It does not fill a drive with a reserve file '
            'or automatically remove additional data to reach the target. Installers can require more space than this target.')
        if controls is not None:
            controls['drive_space_guard_target_gib'] = self.target

    def collect(self):
        return {'drive_space_guard_enabled': self.enabled.isChecked(),
                'drive_space_guard_target_gib': self.target.value()}

    def sync_enabled(self):
        self.target.setEnabled(self.enabled.isChecked())
