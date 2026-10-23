"""Smart Maintenance choices; construction/status refresh never triggers cleanup."""
from __future__ import annotations

import json
import threading
from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QWidget, QVBoxLayout, QHBoxLayout, QLabel, QCheckBox, QSpinBox, QPushButton, QPlainTextEdit

from ..smart_maintenance import validate_smart_configuration


class SmartMaintenancePanel(QWidget):
    def __init__(self, window):
        super().__init__(window)
        self.window, self.backend = window, window.backend
        self._closed = False
        self._cancel = threading.Event()
        layout = QVBoxLayout(self)
        introduction = QLabel('Smart Cleaning checks your saved temporary-file categories while Optimus is open. '
            'Browser-exit cleaning uses your saved browser categories and cookie exceptions after an observed browser exit. '
            'It never closes a browser. Incomplete scans, unknown browser state, active maintenance and unresolved installers are skipped.')
        introduction.setWordWrap(True)
        layout.addWidget(introduction)
        self.controls = {}
        for key, label in (('smart_cleaning_enabled', 'Automatically clean saved junk categories at the threshold'),
                           ('browser_exit_cleaning_enabled', 'Automatically clean selected browsers after they exit')):
            control = QCheckBox(label)
            self.controls[key] = control
            layout.addWidget(control)
        for key, label, minimum, maximum, suffix in (
            ('smart_cleaning_threshold_bytes', 'Junk threshold', 1, 1_000_000, ' MB (decimal)'),
            ('smart_cleaning_cooldown_seconds', 'Minimum time between attempts', 60, 604800, ' seconds'),
            ('browser_exit_grace_seconds', 'Browser-exit grace period', 1, 300, ' seconds'),
            ('smart_maintenance_poll_seconds', 'Monitoring interval', 10, 3600, ' seconds')):
            row = QHBoxLayout()
            row.addWidget(QLabel(label))
            control = QSpinBox()
            control.setRange(minimum, maximum)
            control.setSuffix(suffix)
            row.addWidget(control)
            row.addStretch()
            self.controls[key] = control
            layout.addLayout(row)
        self.note = QLabel('500 MB means 500,000,000 bytes. Browser transitions can be missed between polls. '
            'Enabling cookie/history cleaning can sign you out and removes those records from the selected profiles; private rollback backups are retained.')
        self.note.setWordWrap(True)
        layout.addWidget(self.note)
        bar = QHBoxLayout()
        self.save_button = QPushButton('Save choices')
        self.save_button.clicked.connect(self.save)
        self.preview_button = QPushButton('Preview eligible cleanup')
        self.preview_button.clicked.connect(self.preview)
        self.pause_button = QPushButton('Pause for this session')
        self.pause_button.clicked.connect(self.pause)
        self.cancel_button = QPushButton('Cancel preview')
        self.cancel_button.setToolTip('Cancellation takes effect after the current read-only check.')
        self.cancel_button.clicked.connect(lambda: self._cancel.set())
        self.cancel_button.hide()
        for button in (self.save_button, self.pause_button):
            bar.addWidget(button)
        layout.addLayout(bar)
        preview_bar = QHBoxLayout()
        preview_bar.addWidget(self.preview_button)
        preview_bar.addWidget(self.cancel_button)
        layout.addLayout(preview_bar)
        self.summary = QLabel('Monitoring status is unavailable until this backend supplies Smart Maintenance.')
        self.summary.setWordWrap(True)
        layout.addWidget(self.summary)
        self.details = QPlainTextEdit()
        self.details.setReadOnly(True)
        layout.addWidget(self.details, 1)
        reader = getattr(self.backend, 'preferences', None)
        self._preferences_available = False
        try:
            if not callable(reader):
                raise ValueError('This backend does not expose saved cleanup choices.')
            saved = validate_smart_configuration(reader())
            self._preferences_available = True
        except (OSError, ValueError, TypeError, RuntimeError):
            saved = validate_smart_configuration({})
            for control in self.controls.values():
                control.setEnabled(False)
            self.save_button.setEnabled(False)
            self.preview_button.setEnabled(False)
            self.summary.setText('Saved cleanup choices are unavailable; Smart Maintenance changes and preview are disabled.')
        self._threshold_original = saved['smart_cleaning_threshold_bytes']
        self._threshold_displayed = max(1, self._threshold_original // 1_000_000)
        for key, control in self.controls.items():
            if isinstance(control, QCheckBox):
                control.setChecked(saved[key])
            else:
                control.setValue(self._threshold_displayed if key == 'smart_cleaning_threshold_bytes' else saved[key])
        self._timer = QTimer(self)
        self._timer.setInterval(2000)
        self._timer.timeout.connect(self.refresh_status)
        self.refresh_status()

    def showEvent(self, event):
        super().showEvent(event)
        if not self._closed:
            self.refresh_status()
            self._timer.start()

    def hideEvent(self, event):
        self._timer.stop()
        super().hideEvent(event)

    def choices(self):
        settings = {}
        for key, control in self.controls.items():
            value = control.isChecked() if isinstance(control, QCheckBox) else control.value()
            if key == 'smart_cleaning_threshold_bytes':
                value = self._threshold_original if value == self._threshold_displayed else value * 1_000_000
            settings[key] = value
        return validate_smart_configuration(settings)

    def _run(self, name, *args, **kwargs):
        if self._closed or self.window.busy:
            self.summary.setText('Wait for the current operation before changing Smart Maintenance.')
            return False
        if name != 'smart_maintenance_stop' and not self._preferences_available:
            self.summary.setText('Saved cleanup choices could not be verified; no Smart Maintenance request was started.')
            return False
        if not callable(getattr(self.backend, name, None)):
            self.summary.setText('This backend does not provide Smart Maintenance yet.')
            return False
        self.window._run(name, *args, done=self._result, **kwargs)
        return True

    def save(self):
        if not self.window.smoke_test:
            self._run('configure_smart_maintenance', self.choices())

    def preview(self):
        self._cancel = threading.Event()
        self.cancel_button.show()
        if not self._run('smart_maintenance_preview', cancel_event=self._cancel):
            self.cancel_button.hide()

    def pause(self):
        if not self.window.smoke_test:
            self._run('smart_maintenance_stop')

    def _result(self, value):
        self.cancel_button.hide()
        self.details.setPlainText(json.dumps(value, indent=2, ensure_ascii=False, default=str))
        self.refresh_status()

    def refresh_status(self):
        if not self.window.busy:
            self.cancel_button.hide()
        method = getattr(self.backend, 'smart_maintenance_status', None)
        if self._closed or not callable(method):
            return
        try:
            result = method()
            self.summary.setText(str(result.get('status', 'unknown')).replace('_', ' ').capitalize()
                + (' — monitoring while Optimus is open.' if result.get('running') else ' — monitoring is stopped.')
                + '\n' + str(result.get('note') or ''))
        except Exception as error:
            self.summary.setText('Smart Maintenance status could not be read: ' + type(error).__name__)

    def shutdown(self):
        self._closed = True
        self._cancel.set()
        self._timer.stop()
