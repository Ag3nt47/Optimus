"""Explicit controls for the reversible speed package; construction is read-only."""
from __future__ import annotations

import json
import sys
import threading

from PySide6.QtCore import QObject, QRunnable, Qt, Signal, Slot
from PySide6.QtWidgets import (QCheckBox, QComboBox, QGroupBox, QGridLayout, QLabel,
    QPlainTextEdit, QProgressBar, QPushButton, QVBoxLayout, QSizePolicy)

from .questions import ask_yes_no
from .wrapping_checkbox import WrappingCheckBox


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
        from optimus.runtime_priority import lower_worker_priority
        restore_priority = lower_worker_priority()
        try:
            self.signals.result.emit(self.operation(self.signals.progress.emit, self.cancel))
        except Exception as error:
            self.signals.error.emit(str(error))
        finally:
            restore_priority()
            self.signals.finished.emit()


def _text(value):
    if value is None:
        return ''
    return json.dumps(value, ensure_ascii=False, default=str) if isinstance(value, (dict, list)) else str(value)


class SpeedEnhancementsPanel(QGroupBox):
    def __init__(self, window, settings=None):
        super().__init__('Speed enhancements', window)
        self.window = window
        self.backend = window.backend
        self._saved_enabled = bool((settings or {}).get('speed_enhancements_enabled', False))
        self._worker = None
        self._kind = None
        self._cancel = threading.Event()
        self._save_choice = None
        self._shutdown = False
        layout = QVBoxLayout(self)
        self.enabled = WrappingCheckBox('Automatically apply supported speed enhancements during Optimize')
        self.enabled.setChecked(self._saved_enabled)
        layout.addWidget(self.enabled)
        platform = getattr(self.backend, 'system', None) or sys.platform
        scope = ('Windows 11: performance power mode only for AC power. Older Windows: an existing High Performance plan only on a verified desktop with no battery.'
            if platform not in ('linux', 'darwin') else
            'macOS: turn off Low Power Mode for AC power only when already authorized as administrator.'
            if platform == 'darwin' else
            'Linux: an available performance power profile only on a verified desktop on mains power, with no active profile holds.')
        self.scope = QLabel(scope + ' Battery use, custom or managed policies, missing permissions and unsupported controls are deferred. '
            'Changes are recorded for guarded restore and may increase power use or heat. Optimize also uses your existing resource-analysis and cleanup choices; no performance gain is assumed.')
        self.scope.setTextFormat(Qt.PlainText)
        self.scope.setWordWrap(True)
        layout.addWidget(self.scope)
        self._action_columns = None
        self.action_grid = QGridLayout()
        self.save_button = QPushButton('Save automatic choice')
        self.save_button.clicked.connect(self.save_choice)
        self.review_button = QPushButton('Review current opportunities')
        self.review_button.clicked.connect(self.review)
        self.apply_button = QPushButton('Apply supported enhancements now')
        self.apply_button.clicked.connect(self.apply_now)
        self.apply_button.setObjectName('Primary')
        layout.addLayout(self.action_grid)
        self.status = QLabel('The saved choice applies on Optimize. Review and Apply now run only when selected.')
        self.status.setTextFormat(Qt.PlainText)
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        self.progress = QProgressBar()
        self.progress.setRange(0, 100)
        self.progress.hide()
        layout.addWidget(self.progress)
        self.details = QPlainTextEdit()
        self.details.setReadOnly(True)
        self.details.setPlaceholderText('Review shows current policy, supported changes and reasons for deferred items.')
        self.details.setMinimumHeight(100)
        self.details.setMaximumHeight(140)
        layout.addWidget(self.details)
        self.history_grid = QGridLayout()
        self.history = QComboBox()
        self.history.setMinimumWidth(0)
        self.history.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Fixed)
        self.history.setPlaceholderText('Refresh recorded enhancements to select a restore')
        self.history.currentIndexChanged.connect(self._update_controls)
        self.history_button = QPushButton('Refresh restore history')
        self.history_button.clicked.connect(self.refresh_history)
        self.restore_button = QPushButton('Restore selected enhancements')
        self.restore_button.clicked.connect(self.restore_selected)
        layout.addLayout(self.history_grid)
        self.cancel_button = QPushButton('Cancel after current item')
        self.cancel_button.clicked.connect(self.cancel)
        self.cancel_button.hide()
        layout.addWidget(self.cancel_button)
        for button in (self.save_button, self.review_button, self.apply_button,
                       self.history_button, self.restore_button, self.cancel_button):
            button.setAutoDefault(False)
            button.setMinimumHeight(36)
            button.setCursor(Qt.PointingHandCursor)
        self._reflow_actions()
        self.enabled.toggled.connect(self._update_controls)
        self._update_controls()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._reflow_actions()

    def _reflow_actions(self):
        columns = 1 if self.width() < 520 else 2
        if columns == self._action_columns:
            return
        self._action_columns = columns
        for grid in (self.action_grid, self.history_grid):
            while grid.count():
                grid.takeAt(0)
        for index, button in enumerate((self.save_button, self.review_button)):
            self.action_grid.addWidget(button, index // columns, index % columns)
        self.action_grid.addWidget(self.apply_button, 2 if columns == 1 else 1, 0, 1, columns)
        self.history_grid.addWidget(self.history, 0, 0, 1, columns)
        for index, button in enumerate((self.history_button, self.restore_button)):
            self.history_grid.addWidget(button, 1 + index // columns, index % columns)
        for column in range(2):
            self.action_grid.setColumnStretch(column, int(column < columns))
            self.history_grid.setColumnStretch(column, int(column < columns))

    def sync_settings(self, settings):
        if not isinstance(settings, dict):
            return
        self._saved_enabled = bool(settings.get('speed_enhancements_enabled', False))
        self.enabled.setChecked(self._saved_enabled)
        self._update_controls()

    def _update_controls(self, *_):
        idle = self._worker is None and not self._shutdown
        dirty = self.enabled.isChecked() != self._saved_enabled
        self.enabled.setEnabled(idle)
        self.save_button.setEnabled(idle and dirty)
        self.review_button.setEnabled(idle)
        self.apply_button.setEnabled(idle and self._saved_enabled and self.enabled.isChecked() and not dirty)
        self.apply_button.setToolTip('Apply the supported package after a fresh eligibility check.'
            if self._saved_enabled and not dirty else 'Enable and save the automatic choice before applying enhancements.')
        self.history_button.setEnabled(idle)
        self.history.setEnabled(idle)
        record = self.history.currentData()
        self.restore_button.setEnabled(idle and isinstance(record, dict)
            and isinstance(record.get('id'), str) and record.get('restore_available') is True)

    def _start(self, kind, operation, *, writes=False):
        owner = getattr(self.window, '_main_window', None) or getattr(self.window, 'owner', None)
        if self._worker is not None or self._shutdown or self.window.busy or owner is not None and owner.busy:
            self.status.setText('Wait for the active operation before using speed enhancements.')
            return
        if writes and self.window.smoke_test:
            self.status.setText('Changes are disabled during an interface smoke test.')
            return
        self._kind = kind
        self._cancel = threading.Event()
        self.window.busy = True
        self.progress.setValue(0)
        self.progress.show()
        self.cancel_button.setEnabled(True)
        self.cancel_button.show()
        self.status.setText({'save': 'Saving the automatic speed choice.', 'review': 'Reading current speed opportunities.',
            'apply': 'Applying supported enhancements after fresh checks.', 'history': 'Reading recorded enhancements.',
            'restore': 'Restoring the selected recorded enhancements after fresh checks.'}[kind])
        worker = self._worker = _Worker(operation, self._cancel)
        self.window._workers.add(worker)
        worker.signals.progress.connect(self._progress)
        worker.signals.result.connect(self._result)
        worker.signals.error.connect(self._error)
        worker.signals.finished.connect(self._finished)
        self._update_controls()
        self.window.pool.start(worker)

    def _capability(self, name):
        method = getattr(self.backend, name, None)
        if not callable(method):
            self.status.setText('Speed enhancements are unavailable with this backend. No change was requested.')
            return None
        return method

    def save_choice(self):
        if self.enabled.isChecked() == self._saved_enabled:
            return
        save = self._capability('save_preferences')
        read = self._capability('preferences')
        if save is None or read is None:
            return
        self._save_choice = self.enabled.isChecked()
        choice = self._save_choice
        def operation(progress, cancel):
            if cancel.is_set():
                raise RuntimeError('Saving was cancelled before any preference change.')
            current = dict(read())
            current['speed_enhancements_enabled'] = choice
            return save(current)
        self._start('save', operation, writes=True)

    def review(self):
        method = self._capability('speed_enhancements_scan')
        if method is not None:
            self._start('review', lambda progress, cancel: method())

    def apply_now(self):
        if not self._saved_enabled or not self.enabled.isChecked():
            self.status.setText('Enable and save the automatic choice before applying enhancements.')
            return
        method = self._capability('apply_speed_enhancements')
        if method is not None:
            self._start('apply', lambda progress, cancel: method(progress_callback=progress, cancel_event=cancel), writes=True)

    def refresh_history(self):
        method = self._capability('speed_enhancements_history')
        if method is not None:
            self._start('history', lambda progress, cancel: method())

    def _approve_restore(self, record):
        return ask_yes_no(self, 'Restore recorded speed enhancements?',
            'Restore the original values from this recorded run only if the current policy still matches the applied values?\n\n'
            'Run ID: ' + record['id'] + '\n' + _text(record.get('summary'))
            + '\n\nThe automatic choice stays as saved. Disable it to keep later Optimize runs from reapplying supported enhancements.',
            yes_label='Yes — restore selected run', no_label='No — keep current policy')

    def restore_selected(self):
        record = self.history.currentData()
        if (self._worker is not None or self.window.busy or not isinstance(record, dict)
                or not isinstance(record.get('id'), str) or record.get('restore_available') is not True):
            return
        if self.window.smoke_test:
            self.status.setText('Changes are disabled during an interface smoke test.')
            return
        method = self._capability('restore_speed_enhancements')
        if method is None or not self._approve_restore(record):
            return
        identifier = record['id']
        self._start('restore', lambda progress, cancel: method(identifier, progress_callback=progress, cancel_event=cancel), writes=True)

    @Slot(object)
    def _progress(self, event):
        if not isinstance(event, dict):
            self.details.appendPlainText(_text(event))
            return
        percentage = event.get('percentage')
        if isinstance(percentage, (int, float)):
            self.progress.setValue(max(0, min(100, int(percentage))))
        description = event.get('description') or event.get('stage')
        if description:
            self.status.setText(_text(description))
        if event.get('log'):
            self.details.appendPlainText(_text(event['log']))

    @Slot(object)
    def _result(self, result):
        if self._kind == 'save':
            saved = result if isinstance(result, dict) else {'speed_enhancements_enabled': self._save_choice}
            self.sync_settings(saved)
            baseline = getattr(self.window, 'workflow_control_baseline', None)
            if isinstance(baseline, dict):
                baseline['speed_enhancements_enabled'] = self._saved_enabled
            owner = getattr(self.window, '_main_window', None) or getattr(self.window, 'owner', None)
            if owner is not None:
                owner.workflow_settings['speed_enhancements_enabled'] = self._saved_enabled
            self.status.setText('Automatic speed enhancements ' + ('enabled' if self._saved_enabled else 'disabled')
                + '. This choice takes effect on Optimize; no speed policy was changed now.')
        elif self._kind == 'history':
            records = result.get('items', []) if isinstance(result, dict) else result
            self.history.clear()
            for record in records if isinstance(records, list) else []:
                if isinstance(record, dict) and isinstance(record.get('id'), str):
                    self.history.addItem(_text(record.get('created_at') or record.get('started_at'))
                        + ' — ' + _text(record.get('summary') or record.get('status') or record['id']), record)
            if self.history.count():
                self.history.setCurrentIndex(0)
            self.status.setText(f'{self.history.count()} recorded enhancement runs. Select a restorable run to review its exact ID.')
        else:
            self._show_report(result)
            if self._kind == 'apply' and isinstance(result, dict) and result.get('restore_available') is True and isinstance(result.get('id'), str):
                self.history.clear()
                self.history.addItem(_text(result.get('summary') or result['id']), result)
                self.history.setCurrentIndex(0)
            elif self._kind == 'restore':
                self.history.clear()
                self.status.setText(self.status.text() + ' Refresh history before another restore.')

    def _show_report(self, result):
        if not isinstance(result, dict):
            self.status.setText('The operation returned no usable report. Its outcome is unverified.')
            self.details.setPlainText(_text(result))
            return
        summary = _text(result.get('summary') or result.get('status') or 'Review the recorded outcomes.')
        self.status.setText(summary)
        lines = [summary]
        counts = result.get('counts')
        if isinstance(counts, dict):
            lines.append('Recorded outcomes: ' + '; '.join(_text(value) + ' ' + str(key).replace('_', ' ') for key, value in counts.items()))
        for item in result.get('items', []) if isinstance(result.get('items'), list) else []:
            if not isinstance(item, dict):
                continue
            lines.extend(['', _text(item.get('title') or item.get('id')) + ' — ' + _text(item.get('status')),
                _text(item.get('reason'))])
            if 'before' in item:
                lines.append('Before: ' + _text(item['before']))
            if 'desired' in item:
                lines.append('Target: ' + _text(item['desired']))
        if result.get('log_path'):
            lines.append('\nSaved report: ' + _text(result['log_path']))
        lines.append('\nFULL RECORDED REPORT\n' + json.dumps(result, indent=2, ensure_ascii=False, default=str))
        self.details.setPlainText('\n'.join(lines))

    @Slot(str)
    def _error(self, error):
        self.status.setText('Speed enhancements could not complete: ' + error + '. Review recorded outcomes before retrying.')
        self.details.appendPlainText(self.status.text())
        if self._kind == 'restore':
            self.history.clear()

    @Slot()
    def _finished(self):
        self.window._workers.discard(self._worker)
        self._worker = None
        self.window.busy = False
        self.cancel_button.hide()
        self.progress.hide()
        self._update_controls()

    def cancel(self):
        if self._worker is not None:
            self._cancel.set()
            self.cancel_button.setEnabled(False)
            self.status.setText('Cancellation requested. Waiting for the current item and final readback.')

    def shutdown(self):
        self._shutdown = True
        self.cancel()
        self._update_controls()
