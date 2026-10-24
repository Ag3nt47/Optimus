"""Readable scoped controls for maintenance extensions; no work on construction."""
from __future__ import annotations
import copy
import json
import re
import sys
import threading
from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QWidget, QVBoxLayout, QHBoxLayout, QFormLayout, QLabel,
    QCheckBox, QSpinBox, QLineEdit, QPlainTextEdit, QPushButton, QTabWidget, QScrollArea, QFileDialog)
from ..toolkit_configuration import DEFAULTS, validate_configuration
from .questions import ask_yes_no


class ToolkitChoices(QWidget):
    """Pure configuration editor, also reusable in first-start questions."""
    def __init__(self, configuration=None, parent=None):
        super().__init__(parent)
        self.initial = validate_configuration(configuration or {})
        self.fields, self.scaled = {}, {}
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        self.tabs = QTabWidget()
        self.tabs.setDocumentMode(True)
        self.tabs.setUsesScrollButtons(True)
        self.tabs.setElideMode(Qt.ElideNone)
        outer.addWidget(self.tabs)
        self._adaptive()
        self._caches()
        self._storage()
        self._retention()
        self._health()
        self._updates()

    def _page(self, name, description, *, short_name=None):
        scroll, body = QScrollArea(), QWidget()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.NoFrame)
        layout = QVBoxLayout(body)
        label = QLabel(description)
        label.setWordWrap(True)
        label.setTextFormat(Qt.PlainText)
        layout.addWidget(label)
        form = QFormLayout()
        form.setFieldGrowthPolicy(QFormLayout.AllNonFixedFieldsGrow)
        form.setRowWrapPolicy(QFormLayout.WrapLongRows)
        layout.addLayout(form)
        layout.addStretch()
        scroll.setWidget(body)
        index = self.tabs.addTab(scroll, short_name or name)
        self.tabs.setTabToolTip(index, name + ': ' + description)
        self.tabs.tabBar().setAccessibleTabName(index, name)
        return form

    def _check(self, form, group, key, title):
        from .wrapping_checkbox import WrappingCheckBox
        field = WrappingCheckBox(title)
        field.setChecked(self.initial[group][key])
        self.fields[(group, key)] = field
        form.addRow(field)
        return field

    def _spin(self, form, group, key, title, minimum, maximum, suffix='', scale=1, nullable=False):
        field = QSpinBox()
        field.setRange(minimum, maximum)
        field.setSuffix(suffix)
        original = self.initial[group][key]
        displayed = 0 if original is None else max(minimum, original // scale)
        field.setValue(displayed)
        if nullable:
            field.setSpecialValueText('Unknown / not entered')
        self.fields[(group, key)] = field
        self.scaled[(group, key)] = (original, displayed, scale, nullable)
        form.addRow(title, field)

    def _text(self, form, group, key, title, *, multiline=False):
        field = QPlainTextEdit() if multiline else QLineEdit()
        value = self.initial[group][key]
        text = '\n'.join(value) if isinstance(value, list) else value
        if multiline:
            field.setPlainText(text)
            field.setFixedHeight(85)
        else:
            field.setText(text)
        field.setMinimumWidth(0)
        self.fields[(group, key)] = field
        form.addRow(title, field)
        return field

    def _adaptive(self):
        form = self._page('Workload', 'Adaptive mode checks safe boundaries before starting more work. '
            'Resource pressure, selected hours, battery use or a metered connection can defer an item to a later run. Installers already running finish normally.')
        self._check(form, 'adaptive', 'enabled', 'Use adaptive maintenance limits')
        self._spin(form, 'adaptive', 'cpu_limit', 'Defer new work at CPU use', 10, 100, '%')
        self._spin(form, 'adaptive', 'memory_limit', 'Defer new work at memory use', 10, 100, '%')
        self._spin(form, 'adaptive', 'idle_seconds', 'Minimum idle period (0 disables)', 0, 3600, ' seconds')
        self._check(form, 'adaptive', 'require_ac', 'Require confirmed AC power')
        self._check(form, 'adaptive', 'avoid_fullscreen', 'Defer while a detected foreground app occupies the screen')
        self._check(form, 'adaptive', 'avoid_metered', 'Require a confirmed unrestricted connection before updates')
        self._spin(form, 'adaptive', 'start_hour', 'Maintenance window starts', 0, 23, ':00')
        self._spin(form, 'adaptive', 'end_hour', 'Maintenance window ends (equal hours means anytime)', 0, 23, ':00')

    def _caches(self):
        form = self._page('App caches', 'Only tested rebuildable web-cache files are eligible. Accounts, saved media, '
            'Steam games, downloads and shader caches stay outside these recipes. Active or unknown applications are skipped.', short_name='Caches')
        self._check(form, 'application_caches', 'enabled', 'Include selected application caches in Optimize')
        self.cache_apps = {}
        from ..application_caches import APP_NAMES
        for key, title in APP_NAMES.items():
            control = QCheckBox(title)
            control.setChecked(key in self.initial['application_caches']['apps'])
            self.cache_apps[key] = control
            form.addRow(control)
        row, actions = QWidget(), QHBoxLayout()
        row.setLayout(actions)
        for title, checked in (('Select all apps', True), ('Clear all apps', False)):
            button = QPushButton(title)
            button.clicked.connect(lambda _, checked=checked: [control.setChecked(checked) for control in self.cache_apps.values()])
            actions.addWidget(button)
        form.addRow(row)
        self._spin(form, 'application_caches', 'min_age_days', 'Minimum cache age', 1, 3650, ' days')
        self._spin(form, 'application_caches', 'max_bytes', 'Maximum candidate bytes per run', 1, 8192, ' MiB', scale=1024**2)
        self._spin(form, 'application_caches', 'max_entries', 'Maximum entries inspected', 1, 50000)
        self._spin(form, 'application_caches', 'max_seconds', 'Maximum scan time', 1, 60, ' seconds')

    def _storage(self):
        form = self._page('Folder growth', 'Compare complete metadata scans of exact selected local folders. '
            'Drive roots, junctions, remote/cloud filesystems and placeholders are excluded. Partial scans do not replace a complete baseline.', short_name='Growth')
        self._check(form, 'storage_growth', 'enabled', 'Track selected folder growth during Optimize')
        roots = self._text(form, 'storage_growth', 'roots', 'Folders, one per line', multiline=True)
        add = QPushButton('Add a local folder…')
        def choose():
            path = QFileDialog.getExistingDirectory(self, 'Choose a local folder below a drive root')
            if path:
                roots.setPlainText((roots.toPlainText().rstrip() + '\n' + path).strip())
        add.clicked.connect(choose)
        form.addRow(add)
        self._spin(form, 'storage_growth', 'max_entries', 'Maximum entries inspected', 1, 100000)
        self._spin(form, 'storage_growth', 'max_seconds', 'Maximum scan time', 1, 60, ' seconds')
        self._spin(form, 'storage_growth', 'max_depth', 'Maximum folder depth', 1, 32)

    def _retention(self):
        form = self._page('Recovery data', 'Expiry is permanent. Completed browser backup groups are eligible only '
            'with the separate loss-of-undo approval below. Driver backups, Windows restore points, original maintenance journals, '
            'partial browser backups and pre-restore safety snapshots are protected.', short_name='Recovery')
        self._check(form, 'recovery_retention', 'enabled', 'Include selected expired recovery/report data in Optimize')
        self._spin(form, 'recovery_retention', 'min_age_days', 'Minimum age', 1, 3650, ' days')
        self._check(form, 'recovery_retention', 'exported_reports', 'Expire eligible disposable exported report copies')
        self._check(form, 'recovery_retention', 'browser_backups', 'Expire eligible completed browser backup groups')
        self._check(form, 'recovery_retention', 'lose_browser_undo_approved', 'I approve permanently losing undo for expired browser backup groups')
        self._spin(form, 'recovery_retention', 'max_bytes', 'Maximum candidate bytes', 1, 8192, ' MiB', scale=1024**2)
        self._spin(form, 'recovery_retention', 'max_entries', 'Maximum entries inspected', 1, 50000)
        self._spin(form, 'recovery_retention', 'max_seconds', 'Maximum scan time', 1, 60, ' seconds')

    def _health(self):
        form = self._page('Measurements', 'Short observations retain comparable CPU, memory, disk and network measurements '
            'locally. Hardware, boot and pagefile details depend on native provider access. Missing measurements remain unavailable. '
            'Integrity repairs always require a separate explicit action.', short_name='Health')
        self._check(form, 'history', 'enabled', 'Record before/after observations during Optimize')
        self._spin(form, 'history', 'sample_seconds', 'Observation window', 1, 30, ' seconds')
        self._check(form, 'diagnostics', 'enabled', 'Include read-only hardware, boot and memory diagnostics')
        self._check(form, 'installer_space', 'enabled', 'Check system, temporary and destination volume headroom')
        self._text(form, 'installer_space', 'destination', 'Optional existing installer destination')
        self._spin(form, 'installer_space', 'required_bytes', 'Optional destination size estimate', 0, 1048576, ' MiB', scale=1024**2, nullable=True)
        self.symptoms = {}
        for key, label in (('windows_update_errors', 'Observed Windows Update errors'),
                           ('component_corruption', 'Reported Windows component corruption'),
                           ('system_file_errors', 'Reported system-file errors'),
                           ('repeated_system_crashes', 'Repeated system crashes')):
            control = QCheckBox(label)
            control.setChecked(key in self.initial['diagnostics']['integrity_symptoms'])
            self.symptoms[key] = control
            form.addRow(control)

    def _updates(self):
        form = self._page('Updates', 'Resume saves only already-approved remaining updates and revalidates them after '
            'a user-initiated reboot. Additional provider updates need exact package IDs and separate provider approval. '
            'Optimus never installs a package manager or upgrades project-language environments.')
        self._check(form, 'resume', 'enabled', 'Save eligible remaining updates for continuation after restart')
        self._check(form, 'resume', 'resident_handoff_enabled', 'Experimental Ollama installer handoff: retain a passive Optimus process until Ollama exits')
        self._check(form, 'providers', 'enabled', 'Update selected existing Chocolatey packages during Optimize (Windows)')
        self._text(form, 'providers', 'ids', 'Exact Chocolatey IDs, one per line', multiline=True)
        self._check(form, 'providers', 'agreements_accepted', 'I approve these exact packages and their provider agreements and installation scripts')

    def choices(self, *, validate=True):
        result = copy.deepcopy(self.initial)
        for (group, key), field in self.fields.items():
            if isinstance(field, QCheckBox):
                value = field.isChecked()
            elif isinstance(field, QSpinBox):
                original, displayed, scale, nullable = self.scaled[(group, key)]
                value = original if field.value() == displayed else None if nullable and field.value() == 0 else field.value() * scale
            elif isinstance(field, QPlainTextEdit):
                text = field.toPlainText()
                value = [item.strip() for item in re.split(r'[\r\n,]+' if key == 'ids' else r'[\r\n]+', text) if item.strip()]
            else:
                value = field.text().strip()
            result[group][key] = value
        result['application_caches']['apps'] = [key for key, control in self.cache_apps.items() if control.isChecked()]
        result['diagnostics']['integrity_symptoms'] = [key for key, control in self.symptoms.items() if control.isChecked()]
        return validate_configuration(result) if validate else result


class MaintenanceToolkitPanel(QWidget):
    def __init__(self, window):
        super().__init__(window)
        self.window, self.backend = window, window.backend
        self._closed, self._plans, self._last = False, {}, None
        self._cancel = threading.Event()
        layout = QVBoxLayout(self)
        note = QLabel('Save the exact scopes below before running them. New options start off. Preview does not delete; '
                      'Apply reviewed cleanup requires a fresh matching saved scope.')
        note.setWordWrap(True)
        layout.addWidget(note)
        try:
            settings = self.backend.preferences().get('toolkit_settings', {})
        except Exception:
            settings = {}
        self.editor = ToolkitChoices(settings, self)
        layout.addWidget(self.editor, 1)
        from .flow_layout import FlowLayout
        controls = QWidget()
        flow = FlowLayout(controls)
        layout.addWidget(controls)
        self.buttons = {}
        actions = [('Save toolkit choices', self.save), ('Read-only performance check', self.check),
                   ('Preview current tool', self.preview), ('Apply reviewed cleanup', self.apply),
                   ('Observe / view history', self.history), ('Resume after restart', self.resume),
                   ('Archive last report copy', lambda: self._run('toolkit_archive_report')),
                   ('Windows integrity check', lambda: self.integrity(False)), ('Repair Windows…', lambda: self.integrity(True))]
        for index, (title, operation) in enumerate(actions):
            button = QPushButton(title)
            button.setAutoDefault(False)
            button.clicked.connect(operation)
            self.buttons[title] = button
            flow.addWidget(button)
        self.editor.tabs.currentChanged.connect(self._refresh_apply)
        for field in (*self.editor.fields.values(), *self.editor.cache_apps.values(), *self.editor.symptoms.values()):
            signal = (field.toggled if isinstance(field, QCheckBox) else field.valueChanged if isinstance(field, QSpinBox) else field.textChanged)
            signal.connect(self._invalidate_plans)
        if sys.platform != 'win32':
            for title in ('Windows integrity check', 'Repair Windows…'):
                self.buttons[title].setEnabled(False)
                self.buttons[title].setToolTip('Windows native servicing is unavailable on this platform.')
        self.status = QLabel('Choose a tool and save its scope. No maintenance has run.')
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        self.details = QPlainTextEdit()
        self.details.setReadOnly(True)
        self.details.setMinimumHeight(95)
        self.details.setMaximumHeight(180)
        layout.addWidget(self.details)
        stop = QPushButton('Cancel at the next safe boundary')
        stop.clicked.connect(lambda: self._cancel.set())
        layout.addWidget(stop)
        self._refresh_apply()

    def _refresh_apply(self, *_):
        button = self.buttons.get('Apply reviewed cleanup')
        if button is not None:
            button.setEnabled(not self.window.smoke_test and self._key() in self._plans)

    def _invalidate_plans(self, *_):
        self._plans.clear()
        self._refresh_apply()

    def _saved_scope_matches(self):
        try:
            return self.editor.choices() == validate_configuration(self.backend.preferences().get('toolkit_settings', {}))
        except (ValueError, TypeError, AttributeError):
            return False

    def _key(self):
        return {1: 'application_caches', 2: 'storage_growth', 3: 'recovery_retention', 4: 'history', 5: 'providers'}.get(self.editor.tabs.currentIndex())

    def _run(self, method, *args, **kwargs):
        if self._closed or self.window.busy:
            self.status.setText('Wait for the current operation to finish.')
            return
        if not callable(getattr(self.backend, method, None)):
            self.status.setText('This backend does not expose that toolkit capability.')
            return
        self._cancel = threading.Event()
        origin = args[0] if method == 'toolkit_preview' and args else None
        if origin is not None:
            self._plans.pop(origin, None)
            self._refresh_apply()
        requested_scope = self.editor.choices(validate=False)
        self.window._run(method, *args, done=lambda result: self._result(result, origin=origin, requested_scope=requested_scope), cancel_event=self._cancel, **kwargs)

    def _result(self, result, *, origin=None, requested_scope=None):
        if self._closed:
            return
        self._last = result
        if (origin in ('application_caches', 'recovery_retention') and isinstance(result, dict)
                and result.get('id') and requested_scope == self.editor.choices(validate=False)
                and self._saved_scope_matches()):
            self._plans[origin] = result
        self._refresh_apply()
        self.details.setPlainText(json.dumps(result, indent=2, ensure_ascii=False, default=str)[:80000])
        self.status.setText(str(result.get('note') or result.get('summary') or result.get('status') or 'Results ready.') if isinstance(result, dict) else 'Results ready.')

    def save(self):
        if self.window.smoke_test:
            return
        try:
            choices = self.editor.choices()
        except ValueError as error:
            self.status.setText(str(error))
            return
        if self.window.busy:
            self.status.setText('Wait for the current operation to finish before saving.')
            return
        self._plans.clear()
        self.window._run('configure_toolkit', choices, done=self._result)

    def check(self):
        self._run('toolkit_check')

    def history(self):
        self._run('toolkit_observe')

    def preview(self):
        key = self._key()
        if key:
            if not self._saved_scope_matches():
                self.status.setText('Save the current toolkit choices before previewing their scope.')
                return
            self._run('toolkit_preview', key)
        else:
            self._run('toolkit_check')

    def apply(self):
        key = self._key()
        if self.window.smoke_test or key not in ('application_caches', 'recovery_retention'):
            self.status.setText('Choose App caches or Recovery data, then preview the saved scope.')
            return
        plan = self._plans.get(key)
        if plan is None:
            self.status.setText('Preview this saved scope before applying it.')
            return
        if ask_yes_no(self, 'Apply reviewed cleanup?',
                      'Permanently remove the eligible files in this reviewed plan? Expiring browser backups removes their undo ability. '
                      'Targets and saved scope will be checked again before deletion.'):
            self._plans.pop(key, None)
            self._refresh_apply()
            self._run('toolkit_apply', key, plan)

    def resume(self):
        if not self.window.smoke_test:
            self._run('toolkit_resume')

    def integrity(self, repair):
        if self.window.smoke_test:
            return
        if repair and not ask_yes_no(self, 'Run supported Windows repair?',
                'Run DISM component repair and System File Checker? This can take time and download official Windows components. '
                'Administrator permission is required. Optimus will retain the native outcome and will not restart the computer.'):
            return
        self._run('toolkit_integrity', repair=repair, approved=repair)

    def shutdown(self):
        self._closed = True
        self._cancel.set()
