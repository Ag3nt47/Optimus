"""Independent opt-in retention controls; constructing them never deletes data."""
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QHBoxLayout, QLabel, QSpinBox

from ..retention_configuration import RETENTION_SCOPES, validate_retention_scope


class RetentionCleanupChoices:
    def __init__(self, layout, initial, *, add_check, add_note, controls=None):
        validate_retention_scope(initial)
        self.toggles, self.days = {}, {}
        add_note('Optional retention cleanup — each option is off by default and has its own age limit. '
            'These choices are separate from general temporary-file cleanup, browser history and the Full preset. '
            'Select personal-history deletion individually after reviewing its warning.')
        for prefix, title, description in RETENTION_SCOPES:
            enabled_key, age_key = prefix + '_cleanup_enabled', prefix + '_retention_days'
            enabled = add_check(enabled_key, title)
            self.toggles[enabled_key] = enabled
            add_note(description)
            row = QHBoxLayout()
            label = QLabel('Minimum age before removal')
            label.setTextFormat(Qt.PlainText)
            label.setWordWrap(True)
            row.addWidget(label, 1)
            age = QSpinBox()
            age.setMinimumHeight(30)
            age.setRange(1, 3650)
            age.setSuffix(' days')
            age.setValue(initial.get(age_key, 7))
            age.setAccessibleName(title + ': minimum age in days')
            age.setToolTip('Independent retention limit from 1 to 3650 days. The general cleanup intensity does not change it.')
            age.setEnabled(enabled.isChecked())
            enabled.toggled.connect(age.setEnabled)
            self.days[age_key] = age
            if controls is not None:
                controls[age_key] = age
            row.addWidget(age)
            layout.addLayout(row)

    def collect(self):
        return {**{key: control.isChecked() for key, control in self.toggles.items()},
                **{key: control.value() for key, control in self.days.items()}}

    def sync_enabled(self):
        for prefix, _, _ in RETENTION_SCOPES:
            self.days[prefix + '_retention_days'].setEnabled(self.toggles[prefix + '_cleanup_enabled'].isChecked())
