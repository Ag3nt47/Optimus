"""Compact shared setup controls; all changes remain unsaved until acceptance."""
from PySide6.QtWidgets import QHBoxLayout, QLabel, QSpinBox

from ..smart_configuration import SMART_BOUNDS, validate_smart_configuration


class SmartSetupChoices:
    def __init__(self, layout, initial, *, add_check, add_note):
        saved = validate_smart_configuration(initial)
        add_note('Optional Smart Maintenance — off until you enable it. It runs only while Optimus is open '
            'and the current session is approved. It uses the saved cleanup/browser scopes below; it never closes a browser.')
        self.checks = {
            'smart_cleaning_enabled': add_check('smart_cleaning_enabled', 'Automatically clean my saved junk categories at the threshold'),
            'browser_exit_cleaning_enabled': add_check('browser_exit_cleaning_enabled', 'Automatically clean selected browser data after an observed browser exit'),
        }
        self.numbers = {}
        self._threshold_original = saved['smart_cleaning_threshold_bytes']
        self._threshold_displayed = max(1, self._threshold_original // 1_000_000)
        for key, label in (('smart_cleaning_threshold_bytes', 'Junk threshold (decimal MB)'),
            ('smart_cleaning_cooldown_seconds', 'Minimum seconds between cleanup attempts'),
            ('browser_exit_grace_seconds', 'Browser-exit grace period (seconds)'),
            ('smart_maintenance_poll_seconds', 'Monitoring interval (seconds)')):
            row = QHBoxLayout()
            text = QLabel(label)
            text.setWordWrap(True)
            row.addWidget(text, 1)
            control = QSpinBox()
            minimum, maximum = SMART_BOUNDS[key]
            control.setRange(minimum // 1_000_000 if key == 'smart_cleaning_threshold_bytes' else minimum,
                             maximum // 1_000_000 if key == 'smart_cleaning_threshold_bytes' else maximum)
            control.setValue(self._threshold_displayed if key == 'smart_cleaning_threshold_bytes' else saved[key])
            self.numbers[key] = control
            row.addWidget(control)
            layout.addLayout(row)
        add_note('500 MB = 500,000,000 bytes. Incomplete scans and unknown browser state are skipped. '
            'Browser-exit cleaning can clear selected history/cookies and sign you out; kept-cookie domains and '
            'private rollback backups still apply. No separate background service is installed.')

    def collect(self):
        value = {key: control.isChecked() for key, control in self.checks.items()}
        value.update({key: control.value() for key, control in self.numbers.items()})
        shown = value['smart_cleaning_threshold_bytes']
        value['smart_cleaning_threshold_bytes'] = self._threshold_original if shown == self._threshold_displayed else shown * 1_000_000
        return validate_smart_configuration(value)
