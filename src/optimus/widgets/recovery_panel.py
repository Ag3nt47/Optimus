"""Asynchronous, explicit driver recovery and System Restore controls.

Constructing this panel performs no inventory, export, recovery or backend call.
Backend hooks are optional so unsupported platforms remain honest and read-only.
"""
from __future__ import annotations

import json
import threading

from PySide6.QtCore import QObject, QRunnable, QThreadPool, Qt, Signal, Slot
from PySide6.QtWidgets import (QComboBox, QGridLayout, QGroupBox, QLabel, QLineEdit,
    QPlainTextEdit, QProgressBar, QPushButton, QSizePolicy, QTabWidget, QVBoxLayout, QWidget)

from .questions import ask_yes_no


class _Signals(QObject):
    progress = Signal(object)
    result = Signal(object)
    error = Signal(str)
    finished = Signal()


class _Worker(QRunnable):
    def __init__(self, operation, cancel):
        super().__init__()
        self.operation, self.cancel = operation, cancel
        self.signals = _Signals()

    def run(self):
        try:
            self.signals.result.emit(self.operation(self.signals.progress.emit, self.cancel))
        except Exception as error:
            self.signals.error.emit(str(error))
        finally:
            self.signals.finished.emit()


def _plain(value):
    return str(value or '')


def _preview(value, depth=0):
    """Keep large verified file catalogs from blocking the GUI's text editor."""
    if depth > 6:
        return 'Further detail retained in the private recovery journal.'
    if isinstance(value, dict):
        return {str(key): _preview(item, depth + 1) for key, item in list(value.items())[:100]}
    if isinstance(value, list):
        rows = [_preview(item, depth + 1) for item in value[:25]]
        if len(value) > 25:
            rows.append({'additional_rows': len(value) - 25, 'note': 'Full records remain in the private journal.'})
        return rows
    return value[:2000] + ('…' if len(value) > 2000 else '') if isinstance(value, str) else value


class RecoveryPanel(QGroupBox):
    def __init__(self, window):
        parent = window if isinstance(window, QWidget) else None
        super().__init__('Recovery and restore', parent)
        self.window, self.backend = window, window.backend
        self.setMinimumWidth(0)
        self._worker, self._kind = None, None
        self._cancel = threading.Event()
        self._shutdown = False
        self._driver_plan, self._restore_plan = None, None
        self._columns = None
        self.pool = getattr(window, 'pool', None) or QThreadPool.globalInstance()
        layout = QVBoxLayout(self)
        note = QLabel('Driver rollback and System Restore can change hardware or installed programs. '
                      'Each action is selected and reviewed separately. Exports, native API results and fresh readback are shown accurately; '
                      'a required restart stays under your control.')
        note.setTextFormat(Qt.PlainText)
        note.setWordWrap(True)
        layout.addWidget(note)
        self.tabs = QTabWidget()
        self.tabs.setMinimumWidth(0)
        layout.addWidget(self.tabs)
        driver = QWidget()
        body = QVBoxLayout(driver)
        self.driver_note = QLabel('Refresh to load exact current device identities and owned backup history. '
                                 'Package exports exclude inbox Windows drivers and are not a full system image. '
                                 'Rollback uses the previous driver retained by Windows, not an arbitrary exported INF.')
        self.driver_note.setTextFormat(Qt.PlainText)
        self.driver_note.setWordWrap(True)
        body.addWidget(self.driver_note)
        self.driver_grid = QGridLayout()
        body.addLayout(self.driver_grid)
        self.driver_refresh = self._button('Refresh devices and backup history', self.refresh_drivers)
        self.driver_export = self._button('Export and verify driver backup', self.export_drivers)
        self.device_choices = self._combo('Refresh to select an exact device')
        self.device_choices.currentIndexChanged.connect(self._update_controls)
        self.driver_rollback = self._button('Roll back selected device', self.rollback_selected)
        self.backup_choices = self._combo('No owned backup selected')
        self.backup_choices.currentIndexChanged.connect(self._update_controls)
        self.backup_verify = self._button('Verify selected backup files', self.verify_backup)
        body.addStretch(1)
        self.tabs.addTab(driver, 'Driver recovery')
        restore = QWidget()
        body = QVBoxLayout(restore)
        self.restore_note = QLabel('Restore points are Windows-managed snapshots, not personal-file backups. '
                                  'Availability depends on Windows client support, protection settings and policy. '
                                  'Opening the Windows restore wizard is a reviewed handoff; it does not mean restoration completed.')
        self.restore_note.setTextFormat(Qt.PlainText)
        self.restore_note.setWordWrap(True)
        body.addWidget(self.restore_note)
        self.restore_grid = QGridLayout()
        body.addLayout(self.restore_grid)
        self.restore_refresh = self._button('Refresh Windows restore points', self.refresh_restore_points)
        self.restore_description = QLineEdit()
        self.restore_description.setPlaceholderText('Description for a new restore point')
        self.restore_description.setMaxLength(160)
        self.restore_description.setAccessibleName('New restore point description')
        self.restore_description.textChanged.connect(self._update_controls)
        self.restore_create = self._button('Create and verify restore point', self.create_restore_point)
        self.restore_choices = self._combo('Refresh to select an exact restore point')
        self.restore_choices.currentIndexChanged.connect(self._update_controls)
        self.restore_delete = self._button('Delete selected restore point', self.delete_restore_point)
        self.restore_open = self._button('Review selected restore in Windows', self.open_restore)
        body.addStretch(1)
        self.tabs.addTab(restore, 'System Restore')
        self.status = QLabel('Nothing runs until you select an action. Refresh is read only.')
        self.status.setTextFormat(Qt.PlainText)
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        self.progress = QProgressBar()
        self.progress.hide()
        layout.addWidget(self.progress)
        self.details = QPlainTextEdit()
        self.details.setMinimumWidth(0)
        self.details.setReadOnly(True)
        self.details.setMinimumHeight(120)
        self.details.setMaximumHeight(240)
        self.details.setPlaceholderText('Exact backup, native rollback and restore-point results appear here.')
        layout.addWidget(self.details)
        self.cancel_button = self._button('Cancel before the next recovery step', self.cancel)
        self.cancel_button.hide()
        layout.addWidget(self.cancel_button)
        self._reflow()
        self._update_controls()

    @staticmethod
    def _combo(placeholder):
        value = QComboBox()
        value.setMinimumWidth(0)
        value.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Fixed)
        value.setPlaceholderText(placeholder)
        return value

    @staticmethod
    def _button(label, callback):
        value = QPushButton(label)
        value.setAutoDefault(False)
        value.setMinimumHeight(36)
        # A wide two-column layout must be able to shrink far enough to trigger
        # the single-column reflow; long action labels must not fix its width.
        value.setMinimumWidth(0)
        value.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Fixed)
        value.setCursor(Qt.PointingHandCursor)
        value.clicked.connect(callback)
        return value

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._reflow()

    def _reflow(self):
        columns = 1 if self.width() < 680 else 2
        if columns == self._columns:
            return
        self._columns = columns
        for grid in (self.driver_grid, self.restore_grid):
            while grid.count():
                grid.takeAt(0)
        driver_rows = ((self.driver_refresh, self.driver_export), (self.device_choices, self.driver_rollback),
                       (self.backup_choices, self.backup_verify))
        restore_rows = ((self.restore_refresh,), (self.restore_description, self.restore_create),
                        (self.restore_choices,), (self.restore_delete, self.restore_open))
        for grid, rows in ((self.driver_grid, driver_rows), (self.restore_grid, restore_rows)):
            line = 0
            for row in rows:
                if columns == 1:
                    for widget in row:
                        grid.addWidget(widget, line, 0)
                        line += 1
                else:
                    for index, widget in enumerate(row):
                        grid.addWidget(widget, line, index, 1, 2 if len(row) == 1 else 1)
                    line += 1
            for column in range(2):
                grid.setColumnStretch(column, int(column < columns))

    def _method(self, name):
        method = getattr(self.backend, name, None)
        if not callable(method):
            self.status.setText('This recovery action is unavailable on this backend. No operation was started.')
            return None
        return method

    def _can_change(self):
        owner = getattr(self.window, '_main_window', None) or getattr(self.window, 'owner', None)
        if (self._worker is not None or self._shutdown or getattr(self.window, 'busy', False)
                or owner is not None and getattr(owner, 'busy', False)):
            self.status.setText('Wait for the current operation before starting recovery.')
            return False
        if getattr(self.window, 'smoke_test', False):
            self.status.setText('Recovery changes are disabled during an interface smoke test.')
            return False
        return True

    def _update_controls(self, *_):
        idle = self._worker is None and not self._shutdown
        changes_allowed = idle and not getattr(self.window, 'smoke_test', False)
        device = self.device_choices.currentData()
        backup = self.backup_choices.currentData()
        point = self.restore_choices.currentData()
        availability = lambda name:callable(getattr(self.backend, name, None))
        self.driver_refresh.setEnabled(idle and availability('driver_recovery_scan'))
        self.driver_export.setEnabled(changes_allowed and availability('driver_backup_export'))
        self.device_choices.setEnabled(idle)
        self.driver_rollback.setEnabled(changes_allowed and availability('driver_rollback') and bool(self._driver_plan)
                                       and isinstance(device, dict) and device.get('rollback_eligible') is True)
        self.backup_choices.setEnabled(idle)
        self.backup_verify.setEnabled(idle and availability('driver_backup_verify') and isinstance(backup, dict)
                                     and backup.get('kind') == 'driver-export' and backup.get('status') == 'completed')
        self.restore_refresh.setEnabled(idle and availability('system_restore_scan'))
        self.restore_description.setEnabled(changes_allowed)
        self.restore_create.setEnabled(changes_allowed and availability('system_restore_create')
                                       and bool(self.restore_description.text().strip()))
        self.restore_choices.setEnabled(idle)
        selected = changes_allowed and bool(self._restore_plan) and isinstance(point, dict)
        self.restore_delete.setEnabled(selected and availability('system_restore_delete'))
        self.restore_open.setEnabled(selected and availability('system_restore_open'))

    def _start(self, kind, operation, *, writes=False):
        owner = getattr(self.window, '_main_window', None) or getattr(self.window, 'owner', None)
        if (self._worker is not None or self._shutdown or getattr(self.window, 'busy', False)
                or owner is not None and getattr(owner, 'busy', False)):
            self.status.setText('Wait for the current operation before starting recovery.')
            return
        if writes and getattr(self.window, 'smoke_test', False):
            self.status.setText('Recovery changes are disabled during an interface smoke test.')
            return
        self._kind, self._cancel = kind, threading.Event()
        self.window.busy = True
        self.progress.setRange(0, 0)
        self.progress.show()
        self.cancel_button.show()
        self.cancel_button.setEnabled(True)
        self.status.setText('Reading recovery information.' if not writes else 'Running the explicitly selected recovery action.')
        worker = self._worker = _Worker(operation, self._cancel)
        workers = getattr(self.window, '_workers', None)
        if workers is not None:
            workers.add(worker)
        worker.signals.progress.connect(self._progress)
        worker.signals.result.connect(self._result)
        worker.signals.error.connect(self._error)
        worker.signals.finished.connect(self._finished)
        self._update_controls()
        self.pool.start(worker)

    def refresh_drivers(self):
        method = self._method('driver_recovery_scan')
        if method:
            self._start('driver_scan', lambda progress, cancel:method())

    def export_drivers(self):
        if not self._can_change():
            return
        method = self._method('driver_backup_export')
        if method and ask_yes_no(self, 'Export driver packages?',
                'Create a new verified local export of installed third-party driver packages? '
                'It uses disk space and does not include every inbox Windows driver, device setting or personal file.',
                yes_label='Yes — export and verify', no_label='No — cancel'):
            self._start('driver_export', lambda progress, cancel:method(confirmed=True, progress_callback=progress, cancel_event=cancel), writes=True)

    def verify_backup(self):
        method, backup = self._method('driver_backup_verify'), self.backup_choices.currentData()
        if method and isinstance(backup, dict):
            self._start('backup_verify', lambda progress, cancel:method(backup['id'], progress_callback=progress, cancel_event=cancel))

    def rollback_selected(self):
        if not self._can_change():
            return
        method, device = self._method('driver_rollback'), self.device_choices.currentData()
        if not method or not self._driver_plan or not isinstance(device, dict) or device.get('rollback_eligible') is not True:
            return
        versions = ', '.join(_plain(item.get('version')) for item in device.get('drivers', []))
        if ask_yes_no(self, 'Roll back this exact device?',
                'Device: ' + _plain(device.get('name')) + '\nCurrent driver: ' + versions + '\nInstance: '
                + _plain(device.get('device_id')) + '\n\nSave your work. This device may disconnect. '
                'Optimus first exports and verifies packages, then asks Windows to use its previous driver for this device. '
                'Windows can remove the replaced third-party package. A required restart is reported, not started automatically.',
                yes_label='Yes — roll back this device', no_label='No — keep current driver'):
            plan, selection = self._driver_plan, device['selection_id']
            self._start('driver_rollback', lambda progress, cancel:method(plan, selection, confirmed=True,
                        progress_callback=progress, cancel_event=cancel), writes=True)

    def refresh_restore_points(self):
        method = self._method('system_restore_scan')
        if method:
            self._start('restore_scan', lambda progress, cancel:method())

    def create_restore_point(self):
        if not self._can_change():
            return
        method, description = self._method('system_restore_create'), self.restore_description.text().strip()
        if method and description and ask_yes_no(self, 'Create a restore point?',
                'Ask Windows to create and verify a new restore point named ' + description + '? '
                'Windows can decline or reuse a recent point; Optimus reports creation only after a new point is found.',
                yes_label='Yes — create and verify', no_label='No — cancel'):
            self._start('restore_create', lambda progress, cancel:method(description, confirmed=True, cancel_event=cancel), writes=True)

    def delete_restore_point(self):
        if not self._can_change():
            return
        method, point = self._method('system_restore_delete'), self.restore_choices.currentData()
        if not method or not self._restore_plan or not isinstance(point, dict):
            return
        if ask_yes_no(self, 'Delete this restore point?',
                'Permanently delete only restore point ' + _plain(point.get('sequence_number')) + ': '
                + _plain(point.get('description')) + '\nCreated: ' + _plain(point.get('created_at'))
                + '\n\nThis removes this recovery option and cannot be undone. Other restore points are not selected.',
                yes_label='Yes — delete this point', no_label='No — keep this point'):
            plan, selection = self._restore_plan, point['selection_id']
            self._start('restore_delete', lambda progress, cancel:method(plan, selection, confirmed=True, cancel_event=cancel), writes=True)

    def open_restore(self):
        if not self._can_change():
            return
        method, point = self._method('system_restore_open'), self.restore_choices.currentData()
        if not method or not self._restore_plan or not isinstance(point, dict):
            return
        if ask_yes_no(self, 'Review System Restore in Windows?',
                'Reviewed point: ' + _plain(point.get('sequence_number')) + ' — ' + _plain(point.get('description'))
                + '\n\nWindows will open its restore chooser. Select and verify this point there, review affected programs, '
                'and approve Finish only when ready. Applying a restore can restart the computer and change programs, drivers and settings. '
                'Opening the wizard does not apply or verify restoration.',
                yes_label='Yes — open Windows restore', no_label='No — cancel'):
            plan, selection = self._restore_plan, point['selection_id']
            self._start('restore_open', lambda progress, cancel:method(plan, selection, confirmed=True, restart_acknowledged=True), writes=True)

    @Slot(object)
    def _progress(self, event):
        if isinstance(event, dict):
            if isinstance(event.get('percentage'), (int, float)):
                self.progress.setRange(0, 100)
                self.progress.setValue(max(0, min(100, int(event['percentage']))))
            if event.get('description'):
                self.status.setText(_plain(event['description']))

    @Slot(object)
    def _result(self, result):
        if not isinstance(result, dict):
            self.status.setText('No usable recovery result was returned. The outcome remains unverified.')
            return
        self.details.setPlainText(json.dumps(_preview(result), indent=2, ensure_ascii=False, default=str))
        if self._kind == 'driver_scan':
            self._driver_plan = result.get('plan_id') if result.get('status') == 'completed' else None
            self.device_choices.clear()
            for device in result.get('devices', []):
                if isinstance(device, dict) and isinstance(device.get('selection_id'), str):
                    self.device_choices.addItem(_plain(device.get('name')) + ' — '
                        + ', '.join(_plain(item.get('version')) for item in device.get('drivers', [])), device)
            self.device_choices.setCurrentIndex(-1)
            self.backup_choices.clear()
            for backup in result.get('backups', []):
                if isinstance(backup, dict) and isinstance(backup.get('id'), str):
                    self.backup_choices.addItem(_plain(backup.get('summary') or backup.get('status')) + ' — ' + backup['id'], backup)
            self.backup_choices.setCurrentIndex(-1)
        elif self._kind == 'restore_scan':
            self._restore_plan = result.get('plan_id') if result.get('status') == 'completed' else None
            self.restore_choices.clear()
            for point in result.get('points', []):
                if isinstance(point, dict) and isinstance(point.get('selection_id'), str):
                    self.restore_choices.addItem(_plain(point.get('sequence_number')) + ' — ' + _plain(point.get('description'))
                                                + ' — ' + _plain(point.get('created_at')), point)
            self.restore_choices.setCurrentIndex(-1)
        elif self._kind == 'driver_export' and result.get('kind') == 'driver-export' and result.get('id'):
            self.backup_choices.addItem(_plain(result.get('summary') or result.get('status')),
                                       {key:result.get(key) for key in ('id', 'kind', 'status', 'summary')})
            self.backup_choices.setCurrentIndex(self.backup_choices.count() - 1)
        elif self._kind == 'driver_rollback':
            self._driver_plan = None
            self.device_choices.setCurrentIndex(-1)
        elif self._kind in ('restore_create', 'restore_delete', 'restore_open'):
            self._restore_plan = None
            self.restore_choices.setCurrentIndex(-1)
        summary = result.get('summary') or result.get('reason') or result.get('note') or result.get('status') or 'Review the exact recovery result.'
        self.status.setText(_plain(summary))
        self._update_controls()

    @Slot(str)
    def _error(self, message):
        self.status.setText('Recovery did not complete: ' + message)
        self.details.setPlainText(message)

    @Slot()
    def _finished(self):
        worker, self._worker = self._worker, None
        workers = getattr(self.window, '_workers', None)
        if workers is not None:
            workers.discard(worker)
        self.window.busy = False
        self.cancel_button.hide()
        self.progress.hide()
        self._update_controls()

    def cancel(self):
        self._cancel.set()
        self.cancel_button.setEnabled(False)
        self.status.setText('Cancellation requested before the next step. A running native recovery action is not terminated.')

    def shutdown(self):
        self._shutdown = True
        self._cancel.set()
        self._update_controls()
