"""Explicit file-copy and overwrite tools; no actions during construction."""
from __future__ import annotations

import json
import threading

from PySide6.QtCore import QObject, QRunnable, Qt, Signal, Slot
from PySide6.QtWidgets import (QComboBox, QFileDialog, QFormLayout, QGroupBox,
    QHBoxLayout, QLabel, QLineEdit, QPlainTextEdit, QProgressBar, QPushButton,
    QSpinBox, QTabWidget, QVBoxLayout, QWidget)

from .questions import ask_yes_no


def _preview(value, depth=0):
    if depth > 6:
        return 'Further detail is retained in the exact operation plan or journal.'
    if isinstance(value, dict):
        return {str(key): _preview(item, depth + 1) for key, item in list(value.items())[:80]}
    if isinstance(value, list):
        rows = [_preview(item, depth + 1) for item in value[:30]]
        if len(value) > 30:
            rows.append({'additional_rows': len(value) - 30,
                         'note': 'Review a smaller scope for individual detail; the full selection remains bound to the plan.'})
        return rows
    return value[:1600] + '…' if isinstance(value, str) and len(value) > 1600 else value


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
        restore = lower_worker_priority()
        try:
            self.signals.result.emit(self.operation(self.signals.progress.emit, self.cancel))
        except Exception as error:
            self.signals.error.emit(str(error))
        finally:
            self.operation = None  # Release the password-bearing closure.
            restore()
            self.signals.finished.emit()


class FileUtilitiesPanel(QGroupBox):
    def __init__(self, window):
        super().__init__('File utilities', window)
        self.window, self.backend = window, window.backend
        self._worker, self._plan = None, None
        self._cancel = threading.Event()
        self._closed = False
        self._paths = []
        self._controls = []
        outer = QVBoxLayout(self)
        explanation = QLabel('Selected-file tools run only when you request them. Encrypt/decrypt and split/join keep the original files and require new output names. Overwrite is irreversible, needs a reviewed selection, and is never part of automatic Optimize.')
        explanation.setWordWrap(True)
        explanation.setTextFormat(Qt.PlainText)
        outer.addWidget(explanation)
        self.tabs = QTabWidget()
        outer.addWidget(self.tabs)
        self._encryption_tab()
        self._split_tab()
        self._overwrite_tab()
        self.status = QLabel('Select a tool and its exact files. No files have been inspected or changed.')
        self.status.setWordWrap(True)
        self.status.setTextFormat(Qt.PlainText)
        outer.addWidget(self.status)
        self.progress = QProgressBar()
        self.progress.setRange(0, 100)
        self.progress.hide()
        outer.addWidget(self.progress)
        self.details = QPlainTextEdit()
        self.details.setReadOnly(True)
        self.details.setMinimumHeight(110)
        outer.addWidget(self.details)
        self.cancel_button = QPushButton('Cancel safely')
        self.cancel_button.clicked.connect(self.cancel)
        self.cancel_button.hide()
        outer.addWidget(self.cancel_button)
        self._update()

    def _page(self, label):
        body = QWidget()
        layout = QVBoxLayout(body)
        self.tabs.addTab(body, label)
        return layout

    def _path_field(self, layout, label, *, save=False, directory=False):
        row = QHBoxLayout()
        field, browse = QLineEdit(), QPushButton('Browse…')
        field.setPlaceholderText(label)
        field.setMinimumWidth(0)
        def choose():
            value = (QFileDialog.getExistingDirectory(self, label) if directory else
                     QFileDialog.getSaveFileName(self, label)[0] if save else
                     QFileDialog.getOpenFileName(self, label)[0])
            if value:
                field.setText(value)
        browse.clicked.connect(choose)
        row.addWidget(field, 1)
        row.addWidget(browse)
        layout.addLayout(row)
        self._controls += [field, browse]
        return field

    def _button(self, layout, label, callback):
        button = QPushButton(label)
        button.setMinimumHeight(36)
        button.setAutoDefault(False)
        button.clicked.connect(callback)
        layout.addWidget(button)
        self._controls.append(button)
        return button

    def _encryption_tab(self):
        body = self._page('Encrypt / Decrypt')
        note = QLabel('Password-protected AES-256-GCM container. A forgotten password cannot be recovered. Decryption publishes a new file only after every chunk and the final manifest authenticate.')
        note.setWordWrap(True)
        body.addWidget(note)
        self.crypto_source = self._path_field(body, 'Source file')
        self.crypto_destination = self._path_field(body, 'New output filename', save=True)
        form = QFormLayout()
        self.password, self.password_confirmation = QLineEdit(), QLineEdit()
        for field in (self.password, self.password_confirmation):
            field.setEchoMode(QLineEdit.Password)
            field.setMaxLength(1024)
            self._controls.append(field)
        form.addRow('Password', self.password)
        form.addRow('Repeat for encryption', self.password_confirmation)
        body.addLayout(form)
        row = QHBoxLayout()
        self.encrypt_button = self._button(row, 'Encrypt to new file', lambda: self._crypt('encrypt'))
        self.decrypt_button = self._button(row, 'Authenticate and decrypt', lambda: self._crypt('decrypt'))
        body.addLayout(row)

    def _split_tab(self):
        body = self._page('Split / Join')
        self.split_source = self._path_field(body, 'File to split')
        self.split_directory = self._path_field(body, 'Existing output folder for parts', directory=True)
        self.part_size = QSpinBox()
        self.part_size.setRange(1, 1024)
        self.part_size.setValue(64)
        self.part_size.setSuffix(' MiB per part')
        body.addWidget(self.part_size)
        self._controls.append(self.part_size)
        self.split_button = self._button(body, 'Split with SHA-256 manifest', self._split)
        self.join_manifest = self._path_field(body, 'Optimus split manifest (.split.json)')
        self.join_destination = self._path_field(body, 'New output filename for joined file', save=True)
        self.join_button = self._button(body, 'Verify and join parts', self._join)

    def _overwrite_tab(self):
        body = self._page('Reviewed overwrite')
        warning = QLabel('Permanently overwrite selected ordinary files, then remove them after readback verification. Cancellation may leave partially overwritten files. SSD wear leveling, snapshots, copy-on-write, backups and cloud copies can retain data; this tool cannot guarantee physical erasure. Personal document folders require individual file selection. Free-space wiping, whole-drive wiping and deleted-file recovery are unavailable in this module.')
        warning.setWordWrap(True)
        warning.setTextFormat(Qt.PlainText)
        body.addWidget(warning)
        self.overwrite_paths = QPlainTextEdit()
        self.overwrite_paths.setReadOnly(True)
        self.overwrite_paths.setMaximumHeight(100)
        self.overwrite_paths.setPlaceholderText('Select exact files or an ordinary non-profile folder to review.')
        body.addWidget(self.overwrite_paths)
        row = QHBoxLayout()
        self._button(row, 'Select files', self._choose_files)
        self._button(row, 'Select folder', self._choose_folder)
        body.addLayout(row)
        self.passes = QComboBox()
        for count in (1, 3, 7, 35):
            self.passes.addItem(str(count) + ' write pass' + ('' if count == 1 else 'es'), count)
        self.passes.currentIndexChanged.connect(self._invalidate)
        body.addWidget(self.passes)
        self._controls.append(self.passes)
        self.review_button = self._button(body, 'Review exact overwrite selection', self._review)
        self.overwrite_button = self._button(body, 'Overwrite reviewed files…', self._overwrite)

    def _choose_files(self):
        paths, _ = QFileDialog.getOpenFileNames(self, 'Select files to review for permanent overwrite')
        if paths:
            self._paths = paths
            self.overwrite_paths.setPlainText('\n'.join(paths))
            self._invalidate()

    def _choose_folder(self):
        path = QFileDialog.getExistingDirectory(self, 'Select an ordinary folder for permanent overwrite review')
        if path:
            self._paths = [path]
            self.overwrite_paths.setPlainText(path)
            self._invalidate()

    def _invalidate(self, *_):
        self._plan = None
        self._update()

    def _update(self):
        idle = self._worker is None and not self._closed
        for control in self._controls:
            control.setEnabled(idle)
        self.overwrite_button.setEnabled(idle and isinstance(self._plan, dict) and self._plan.get('status') == 'review')

    def _start(self, kind, hook, arguments, *, writes=True):
        owner = getattr(self.window, '_main_window', None)
        if self._worker is not None or self._closed or self.window.busy or owner is not None and owner.busy:
            self.status.setText('Wait for the current operation before using file utilities.')
            return False
        if writes and getattr(self.window, 'smoke_test', False):
            self.status.setText('File changes are disabled during an interface smoke test.')
            return False
        method = getattr(self.backend, hook, None)
        if not callable(method):
            self.status.setText('This file tool is unavailable with the current backend.')
            return False
        self._kind = kind
        self._cancel = threading.Event()
        self.window.busy = True
        self.progress.setValue(0)
        self.progress.show()
        self.cancel_button.setEnabled(True)
        self.cancel_button.show()
        self.status.setText('Working on the explicitly selected files. Cancellation is available.')
        worker = self._worker = _Worker(lambda progress, cancel: method(*arguments,
            progress_callback=progress, cancel_event=cancel), self._cancel)
        self.window._workers.add(worker)
        worker.signals.progress.connect(self._progress)
        worker.signals.result.connect(self._result)
        worker.signals.error.connect(self._error)
        worker.signals.finished.connect(self._finished)
        self._update()
        self.window.pool.start(worker)
        return True

    def _crypt(self, kind):
        password = self.password.text()
        if not 8 <= len(password) <= 1024:
            self.status.setText('Use a password of at least 8 characters.')
            return
        if kind == 'encrypt' and password != self.password_confirmation.text():
            self.status.setText('The encryption passwords do not match.')
            return
        arguments = (self.crypto_source.text().strip(), self.crypto_destination.text().strip(), password)
        if self._start(kind, 'file_utilities_' + kind, arguments):
            self.password.clear()
            self.password_confirmation.clear()

    def _split(self):
        self._start('split', 'file_utilities_split', (self.split_source.text().strip(),
            self.split_directory.text().strip(), self.part_size.value() * 1024 * 1024))

    def _join(self):
        self._start('join', 'file_utilities_join', (self.join_manifest.text().strip(), self.join_destination.text().strip()))

    def _review(self):
        self._plan = None
        self._update()
        self._start('review', 'file_utilities_shred_plan', (list(self._paths), self.passes.currentData()), writes=False)

    def _approve_overwrite(self):
        plan = self._plan or {}
        text = (f"Permanently overwrite {plan.get('file_count', 0)} reviewed files ({plan.get('bytes', 0)} bytes) "
                f"with {plan.get('passes', 1)} passes, then remove them?\n\n"
                'This cannot be undone. Cancellation can leave partially overwritten files. Physical erasure on SSDs, snapshots and backups is not guaranteed.')
        confirm = getattr(self.window, '_confirm', None)
        if callable(confirm):
            return confirm('Permanently overwrite reviewed files?', text, details=plan, phrase='OVERWRITE')
        return ask_yes_no(self, 'Permanently overwrite reviewed files?', text,
                          yes_label='Yes — overwrite reviewed files', no_label='No — keep files')

    def _overwrite(self):
        if self._plan is None or not self._approve_overwrite():
            return
        identifier = self._plan['plan_id']
        # Confirmed is positional to keep the common worker transport limited to
        # progress/cancellation keywords; the service still checks exact True.
        if self._start('shred', 'file_utilities_shred_apply', (identifier, True)):
            self._plan = None

    @Slot(object)
    def _progress(self, value):
        if self._closed or not isinstance(value, dict):
            return
        percentage = value.get('percentage')
        if type(percentage) is int:
            self.progress.setValue(max(0, min(99, percentage)))
        self.status.setText(str(value.get('description') or 'Working on selected files.'))

    @Slot(object)
    def _result(self, value):
        if self._closed:
            return
        if not isinstance(value, dict):
            self.status.setText('No usable result was returned; the outcome is unverified.')
            return
        if self._kind == 'review':
            self._plan = value if value.get('status') == 'review' else None
        self.status.setText(str(value.get('status', 'unverified')).replace('_', ' ').capitalize()
            + ': ' + str(value.get('reason') or value.get('warning') or 'Review the recorded result.'))
        self.details.setPlainText(json.dumps(_preview(value), indent=2, ensure_ascii=False, default=str))
        if value.get('status') == 'completed':
            self.progress.setValue(100)

    @Slot(str)
    def _error(self, value):
        if not self._closed:
            self.status.setText('The file utility could not complete: ' + value)

    @Slot()
    def _finished(self):
        self.window._workers.discard(self._worker)
        self._worker = None
        self.window.busy = False
        self.cancel_button.hide()
        self.progress.hide()
        self._update()

    def cancel(self):
        self._cancel.set()
        self.cancel_button.setEnabled(False)
        self.status.setText('Cancellation requested. Copy outputs remain unpublished until verification; an overwrite may already have changed selected bytes.')

    def shutdown(self):
        self._closed = True
        self._cancel.set()
        self.password.clear()
        self.password_confirmation.clear()
        self._update()
