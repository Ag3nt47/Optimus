"""Review an app or driver update worker that stopped with "needs review" or "blocked".

The window collects everything Optimus recorded about the stop in one place:
the worker's live status and reason, the latest update journal for that lane,
the search summary and its errors, and each offer with its outcome. Two
decisions are offered:

* Proceed with update: run Optimus's existing saved-update continuation. It
  installs only offers you already approved, skips individual bad offers, and
  keeps every native installer/restart barrier. Nothing new is approved here.
* Deny: install nothing now. The decision is recorded locally beside the
  journal and the worker card shows that you declined; saved approvals and
  the journal itself are left unchanged, so the evidence remains reviewable.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import threading
import time

from PySide6.QtCore import Qt, QThreadPool, QTimer, Signal
from PySide6.QtWidgets import (
    QDialog, QHBoxLayout, QLabel, QMessageBox, QPlainTextEdit, QPushButton, QVBoxLayout,
)

LANES = {'software_updates': 'App updates', 'driver_updates': 'Driver updates'}
ATTENTION = ('needs_review', 'failed', 'blocked', 'uncertain', 'completed_with_errors')
DECISIONS_FILE = 'update-review-decisions.json'
MAX_JOURNAL_BYTES = 2 * 1024 * 1024
_WORKERS = {}  # Keep runnables alive until the global pool finishes them.
_CONTINUATIONS = {}
_CONTINUATION_LOCK = threading.RLock()


def _release_continuation(backend_key, token, worker_key=None):
    # This callback must survive a review dialog being closed or replaced.
    with _CONTINUATION_LOCK:
        if _CONTINUATIONS.get(backend_key) is token:
            _CONTINUATIONS.pop(backend_key, None)
    if worker_key is not None:
        _WORKERS.pop(worker_key, None)


def journal_lane(record):
    from ..parallel_updates import WINDOWS_DRIVER_REVIEW, WINDOWS_DRIVERS
    title = record.get('title') if isinstance(record, dict) else None
    return 'driver_updates' if title in (WINDOWS_DRIVERS, WINDOWS_DRIVER_REVIEW) else 'software_updates'


def latest_journal(data_dir, lane):
    """Newest update journal for one lane that ended needing attention."""
    folder = Path(data_dir) / 'parallel-updates'
    try:
        entries = [entry for entry in os.scandir(folder)
                   if entry.name.endswith('.json') and entry.is_file(follow_symlinks=False)]
    except OSError:
        return None, None
    for entry in sorted(entries, key=lambda item: item.stat().st_mtime, reverse=True)[:200]:
        try:
            if entry.stat().st_size > MAX_JOURNAL_BYTES:
                continue
            with open(entry.path, 'rb') as stream:
                record = json.loads(stream.read().decode('utf-8'))
        except (OSError, ValueError, UnicodeDecodeError):
            continue
        if (isinstance(record, dict) and journal_lane(record) == lane
                and record.get('status') in ATTENTION):
            return record, Path(entry.path)
    return None, None


def _text(value, limit=2000):
    return ' '.join(str(value).split())[:limit] if value not in (None, '') else ''


def _when(value):
    try:
        return time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(float(value)))
    except (TypeError, ValueError, OverflowError, OSError):
        return ''


def describe(lane, state, record, path):
    """Plain-text report of everything recorded about this stop."""
    state = state if isinstance(state, dict) else {}
    lines = [f'{LANES.get(lane, lane)} — review', '']
    lines.append('Current worker status: ' + (_text(state.get('status')) or 'unknown').replace('_', ' '))
    for label, key in (('What it reported', 'description'), ('Why it waited', 'wait_reason'),
                       ('Why installation is blocked', 'installation_block_reason'),
                       ('Search', 'scan_label'), ('Item', 'current_package')):
        if _text(state.get(key)):
            lines.append(f'{label}: {_text(state.get(key))}')
    if state.get('blocked_by_prior_installer') is True:
        lines.append('A previous installer has an unresolved outcome. Optimus never retries it automatically.')
    if state.get('approval_required') is True:
        lines.append('Offers were found that still need your approval in Settings > App rules before they can install.')
    if record is None:
        lines += ['', 'No saved update journal needing attention was found for this worker.',
                  'Proceed runs the saved-update continuation, which searches again and installs only approved offers.']
        return '\n'.join(lines)
    result = record.get('result') if isinstance(record.get('result'), dict) else {}
    lines += ['', 'Saved update journal',
              f'  Title: {_text(record.get("title"))}',
              f'  Outcome: {_text(record.get("status")).replace("_", " ")}',
              f'  Started: {_when(record.get("started_at"))}   Finished: {_when(record.get("finished_at"))}',
              f'  Installer started: {"yes" if result.get("installer_started") else "no"}'
              f'   Outcome known: {"yes" if result.get("outcomeKnown") is not False else "no"}']
    if _text(record.get('note')):
        lines.append(f'  Note: {_text(record.get("note"))}')
    scan = next((result[key] for key in ('app_scan', 'driver_scan') if isinstance(result.get(key), dict)), None)
    if scan:
        lines += ['', 'Update search',
                  f'  Source: {_text(scan.get("source"))}',
                  f'  Complete: {"yes" if scan.get("complete") is True else "no"}'
                  f'   Offers: {scan.get("offers_count") if scan.get("offers_count") is not None else "unknown"}']
        if _text(scan.get('summary')):
            lines.append(f'  Summary: {_text(scan.get("summary"))}')
        for error in scan.get('errors') or []:
            if isinstance(error, dict):
                lines.append(f'  Problem ({_text(error.get("source"), 200)}): {_text(error.get("error"))}')
        if _text(scan.get('coverage_limit')):
            lines.append(f'  Coverage: {_text(scan.get("coverage_limit"))}')
    items = result.get('items') if isinstance(result.get('items'), list) else []
    lines += ['', f'Offers in this run: {len(items)}']
    for row in items[:200]:
        if not isinstance(row, dict):
            continue
        name = _text(row.get('name') or row.get('Name') or row.get('id'), 200)
        version = ' → '.join(v for v in (_text(row.get('version') or row.get('Version'), 60),
                                         _text(row.get('available') or row.get('Available'), 60)) if v)
        outcome = row.get('result') if isinstance(row.get('result'), dict) else {}
        reason = _text(outcome.get('error') or outcome.get('reason') or outcome.get('note') or row.get('reason'), 500)
        lines.append(f'  • {name}' + (f' ({version})' if version else '')
                     + f': {_text(row.get("status")).replace("_", " ") or "unknown"}'
                     + (f' — {reason}' if reason else ''))
    lines += ['', f'Journal file: {path}']
    return '\n'.join(lines)


def record_decision(data_dir, lane, record, decision):
    """Append one review decision; never modifies the journal or approvals."""
    from ..workflow import atomic_json
    path = Path(data_dir) / DECISIONS_FILE
    try:
        with open(path, 'rb') as stream:
            existing = json.loads(stream.read(MAX_JOURNAL_BYTES).decode('utf-8'))
        entries = existing.get('decisions') if isinstance(existing, dict) else None
        entries = entries if isinstance(entries, list) else []
    except (OSError, ValueError, UnicodeDecodeError):
        entries = []
    entry = {'lane': lane, 'decision': decision, 'decided_at': time.time(),
             'journal_id': record.get('id') if isinstance(record, dict) else None,
             'journal_status': record.get('status') if isinstance(record, dict) else None}
    atomic_json(path, {'schema': 1, 'decisions': (entries + [entry])[-500:]})
    return entry


class UpdateReviewDialog(QDialog):
    """Shows the full stop record; Proceed continues approved updates, Deny records a refusal."""
    proceeded = Signal(str, object)   # lane, continuation report
    denied = Signal(str, object)      # lane, decision entry

    def __init__(self, backend, lane, state, parent=None, *, data_dir=None):
        super().__init__(parent)
        self.backend, self.lane, self.state = backend, lane, dict(state or {})
        self._continuation_active = False
        self._continuation_complete = False
        self._waiting_for_maintenance = False
        self._worker_review_ready = str(self.state.get('status')) not in (
            'running', 'installing', 'scanning', 'preparing', 'pending')
        self.data_dir = Path(data_dir if data_dir is not None else backend.data_dir)
        self.record, self.record_path = latest_journal(self.data_dir, lane)
        self.setWindowTitle('Optimus — review ' + LANES.get(lane, 'updates').lower())
        self.resize(760, 560)
        self.setMinimumSize(480, 340)
        layout = QVBoxLayout(self)
        intro = QLabel('Everything Optimus recorded about why this worker stopped. Proceed installs only '
                       'updates you already approved and skips individual failures. Deny installs nothing '
                       'and keeps the record for later review.')
        intro.setWordWrap(True)
        intro.setTextFormat(Qt.PlainText)
        layout.addWidget(intro)
        self.report = QPlainTextEdit(self)
        self.report.setObjectName('UpdateReviewReport')
        self.report.setReadOnly(True)
        self.report.setPlainText(describe(lane, self.state, self.record, self.record_path))
        layout.addWidget(self.report, 1)
        self.status = QLabel('')
        self.status.setWordWrap(True)
        self.status.setTextFormat(Qt.PlainText)
        self.status.setTextInteractionFlags(Qt.TextSelectableByMouse | Qt.TextSelectableByKeyboard)
        layout.addWidget(self.status)
        buttons = QHBoxLayout()
        self.proceed_button = QPushButton('Proceed with update')
        self.proceed_button.setObjectName('UpdateReviewProceed')
        self.proceed_button.setToolTip('Install only the updates you already approved, skipping individual '
                                       'failures. Installer and restart safeguards still apply.')
        self.deny_button = QPushButton('Deny')
        self.deny_button.setObjectName('UpdateReviewDeny')
        self.deny_button.setToolTip('Install nothing now. Your decision is recorded; approvals and the '
                                    'saved record are unchanged.')
        self.close_button = QPushButton('Close')
        for button in (self.proceed_button, self.deny_button, self.close_button):
            button.setAutoDefault(False)
            button.setDefault(False)
        self.proceed_button.clicked.connect(self.proceed)
        self.deny_button.clicked.connect(self.deny)
        self.close_button.clicked.connect(self.close)
        buttons.addWidget(self.proceed_button)
        buttons.addWidget(self.deny_button)
        buttons.addStretch(1)
        buttons.addWidget(self.close_button)
        layout.addLayout(buttons)
        self._maintenance_timer = QTimer(self)
        self._maintenance_timer.setInterval(500)
        self._maintenance_timer.timeout.connect(self._refresh_maintenance_state)
        if not self._worker_review_ready:
            self._set_busy('This worker is still running. Review again when it stops.')
        else:
            self._refresh_maintenance_state()

    def _maintenance_running(self):
        with _CONTINUATION_LOCK:
            if id(self.backend) in _CONTINUATIONS:
                return True
        progress = self.parentWidget()
        owner = getattr(progress, 'owner', None)
        in_progress = getattr(owner, '_work_in_progress', None)
        if callable(in_progress) and in_progress():
            return True
        if bool(getattr(owner, 'busy', False)):
            return True
        return bool(getattr(getattr(self.backend, 'workflow', None), 'active', False))

    def _refresh_maintenance_state(self):
        if not self._worker_review_ready or self._continuation_active or self._continuation_complete:
            return
        if self._maintenance_running():
            self._waiting_for_maintenance = True
            self.proceed_button.setEnabled(False)
            self.status.setText('Maintenance is still running. Proceed becomes available when it finishes; '
                                'your saved update approvals and holds still apply.')
        elif self._waiting_for_maintenance:
            self._waiting_for_maintenance = False
            self.proceed_button.setEnabled(True)
            self.status.setText('Maintenance has finished. Proceed searches again using your saved approvals.')

    def showEvent(self, event):
        super().showEvent(event)
        self._refresh_maintenance_state()
        self._maintenance_timer.start()

    def hideEvent(self, event):
        self._maintenance_timer.stop()
        super().hideEvent(event)

    def _set_busy(self, message):
        self.proceed_button.setEnabled(False)
        self.deny_button.setEnabled(False)
        self.status.setText(message)

    def _run_continuation(self):
        from ..continue_updates import run_saved_updates
        return run_saved_updates(self.backend)

    def proceed(self):
        from ..ui import BackendWorker
        if not self._worker_review_ready or self._continuation_active or self._continuation_complete:
            return
        if self._maintenance_running():
            self._refresh_maintenance_state()
            return
        token = object()
        backend_key = id(self.backend)
        with _CONTINUATION_LOCK:
            if backend_key in _CONTINUATIONS:
                self._refresh_maintenance_state()
                return
            _CONTINUATIONS[backend_key] = token
        self._continuation_active = True
        self._set_busy('Continuing your approved updates… This window can stay open; installers keep '
                       'their own safeguards.')
        worker = None
        try:
            worker = BackendWorker(self._run_continuation, (), {})
            _WORKERS[id(worker)] = worker
            worker.signals.result.connect(self._proceed_finished)
            worker.signals.error.connect(self._proceed_failed)
            worker.signals.finished.connect(lambda key=id(worker), backend_key=backend_key, claim=token:
                _release_continuation(backend_key, claim, key))
            worker.signals.finished.connect(self._continuation_finished)
            QThreadPool.globalInstance().start(worker)
        except Exception as error:
            _release_continuation(backend_key, token, id(worker) if worker is not None else None)
            self._continuation_active = False
            self._proceed_failed(error)

    def _continuation_finished(self):
        self._continuation_active = False
        self._refresh_maintenance_state()

    def _proceed_finished(self, report):
        report = report if isinstance(report, dict) else {}
        status = str(report.get('status') or 'unknown')
        self._continuation_complete = status in ('completed', 'no_enabled_updates')
        messages = {
            'completed': 'Approved updates finished.',
            'completed_with_skips': 'Approved updates finished; some individual offers were skipped.',
            'no_enabled_updates': 'No app or driver updates are enabled in your saved choices.',
            'blocked': 'Still blocked: ' + _text(report.get('installation_block_reason')
                                                  or 'an installer or restart safeguard is active.'),
            'needs_review': 'The update search is still incomplete, so nothing more was installed.',
            'cancelled': 'The continuation was cancelled.',
        }
        record_decision(self.data_dir, self.lane, self.record, 'proceeded:' + status)
        self.status.setText(messages.get(status, 'Continuation ended: ' + status.replace('_', ' ')))
        self.record, self.record_path = latest_journal(self.data_dir, self.lane)
        self.report.setPlainText(describe(self.lane, self.state, self.record, self.record_path)
                                 + '\n\nContinuation result\n' + json.dumps(report, indent=1, default=str)[:20000])
        self.proceed_button.setEnabled(status not in ('completed', 'no_enabled_updates'))
        self.deny_button.setEnabled(True)
        self.proceeded.emit(self.lane, report)
        self._refresh_maintenance_state()

    def _proceed_failed(self, error):
        self.status.setText('Could not continue updates: ' + _text(error, 1000))
        self.proceed_button.setEnabled(True)
        self.deny_button.setEnabled(True)
        self._refresh_maintenance_state()

    def deny(self):
        try:
            entry = record_decision(self.data_dir, self.lane, self.record, 'denied')
        except OSError as error:
            QMessageBox.warning(self, 'Optimus', 'Your decision could not be saved: ' + _text(error, 500))
            return
        self.denied.emit(self.lane, entry)
        self.accept()
