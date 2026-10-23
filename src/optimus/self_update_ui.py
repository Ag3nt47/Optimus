"""Startup update consent and asynchronous handoff; maintenance stays gated."""
from __future__ import annotations

import copy
import html
import re
import threading
from urllib.parse import urlsplit

from PySide6.QtCore import QObject, QRunnable, QThreadPool, QTimer, Signal, Qt
from PySide6.QtWidgets import QDialog, QHBoxLayout, QLabel, QPlainTextEdit, QPushButton, QVBoxLayout

from .automation import CURRENT_SETUP_VERSION
from .launch_review import launch_warning, scope_fingerprint
from .ui import OptimizationProgressWindow


def make_startup_update_dialog(parent, offer, settings):
    """Construct a testable offline warning; merely displaying it approves nothing."""
    dialog = QDialog(parent)
    dialog.setWindowTitle('Optimus — update before Auto Clean')
    dialog.setProperty('_optimus_no_reveal', True)
    dialog.setMinimumSize(480, 400)
    dialog.resize(800, 680)
    layout = QVBoxLayout(dialog)
    setup_complete = settings.get('setup_version', 0) >= CURRENT_SETUP_VERSION
    title = QLabel('Optimus ' + str(offer.get('version', 'update')) + ' is available on GitHub.')
    title.setTextFormat(Qt.PlainText)
    title.setWordWrap(True)
    layout.addWidget(title)
    note = QLabel('Yes updates Optimus first, then restarts it. ' +
        ('After the verified restart, Auto Clean can run only these unchanged saved choices.' if setup_complete else
         'After the verified restart, setup opens first. Cleanup still requires a fresh review of completed setup.') +
        ' No keeps the current version and continues its normal launch review. Adjust settings keeps maintenance paused.')
    note.setTextFormat(Qt.PlainText)
    note.setWordWrap(True)
    layout.addWidget(note)
    details = QPlainTextEdit(launch_warning(settings))
    details.setReadOnly(True)
    details.setAccessibleName('Saved Auto Clean actions and irreversible data-removal warning after update')
    layout.addWidget(details, 1)
    # Full-width choices stay legible after native font/style/DPI changes.
    buttons = QVBoxLayout()
    yes = QPushButton('Yes — update then ' + ('Auto Clean' if setup_complete else 'setup'))
    no = QPushButton('No — continue current version')
    adjust = QPushButton('Adjust settings')
    yes.setAccessibleName('Yes, update Optimus first; ' + ('run only these unchanged saved Auto Clean actions after restart' if setup_complete else 'open setup after restart'))
    no.setAccessibleName('No update; continue the current version and its normal launch review')
    adjust.setAccessibleName('Adjust settings; automatic maintenance stays paused')
    for button in (yes, no, adjust):
        button.setAutoDefault(False)
        button.setMinimumHeight(42)
        buttons.addWidget(button)
    adjust.setAutoDefault(True)
    adjust.setDefault(True)
    adjust.setFocus(Qt.OtherFocusReason)
    layout.addLayout(buttons)
    dialog.update_choice = None
    dialog.update_buttons = {'yes': yes, 'no': no, 'adjust': adjust}
    dialog.saved_actions = details
    def select(value):
        dialog.update_choice = value
        dialog.accept()
    yes.clicked.connect(lambda: select('yes_update'))
    no.clicked.connect(lambda: select('no_update'))
    adjust.clicked.connect(lambda: select('adjust_settings'))
    return dialog


def ask_startup_update(parent, offer, settings):
    """A fresh Yes approves this update and only the displayed saved scope."""
    dialog = make_startup_update_dialog(parent, offer, settings)
    parent._self_update_dialog = dialog
    try:
        dialog.exec()
        return dialog.update_choice
    finally:
        parent._self_update_dialog = None
        dialog.deleteLater()


def make_manual_update_dialog(parent, result):
    """A supported release can still require a reviewed native/manual installer."""
    offer = result.get('offer') if isinstance(result.get('offer'), dict) else {}
    dialog = QDialog(parent)
    dialog.setWindowTitle('Optimus — manual update available')
    dialog.setMinimumSize(480, 320)
    dialog.resize(620, 360)
    layout = QVBoxLayout(dialog)
    text = QLabel('Optimus ' + str(offer.get('version', 'update')) + ' is available. Automatic installation is unavailable here.')
    text.setTextFormat(Qt.PlainText)
    text.setWordWrap(True)
    layout.addWidget(text)
    reason = QPlainTextEdit(str(result.get('reason') or 'Use the platform download from the GitHub release, then reopen Optimus.'))
    reason.setReadOnly(True)
    reason.setAccessibleName('Why this platform requires a manual Optimus update')
    layout.addWidget(reason, 1)
    url = offer.get('release_url')
    location = urlsplit(url) if isinstance(url, str) else None
    if (location and location.scheme == 'https' and location.netloc == 'github.com'
            and location.path.startswith('/Ag3nt47/Optimus/releases/') and not location.query and not location.fragment):
        link = QLabel('<a href="' + html.escape(url, quote=True) + '">Open the GitHub release downloads</a>')
        link.setOpenExternalLinks(True)
        link.setAccessibleName('Open the verified Optimus GitHub release for manual update')
        layout.addWidget(link)
    note = QLabel('Auto Clean has not started. Continue to the normal saved-actions review using the current version.')
    note.setWordWrap(True)
    layout.addWidget(note)
    proceed = QPushButton('Continue current version')
    proceed.setMinimumHeight(42)
    proceed.setAccessibleName('Continue the current Optimus version and its normal saved-actions review')
    proceed.setDefault(True)
    proceed.clicked.connect(dialog.accept)
    layout.addWidget(proceed)
    return dialog


def show_manual_update_notice(parent, result):
    dialog = make_manual_update_dialog(parent, result)
    parent._self_update_dialog = dialog
    try:
        dialog.exec()
    finally:
        parent._self_update_dialog = None
        dialog.deleteLater()


class SelfUpdateProgressWindow(OptimizationProgressWindow):
    """Reuse the progress presentation without inventing a maintenance worker."""
    def __init__(self, owner, cancel):
        super().__init__(owner, cancel)
        self.setWindowTitle('Optimus — update progress')
        self.title.setText('UPDATING OPTIMUS')
        self.stage.setText('Preparing verified release download')
        self.description.setText('Optimus updates first. Auto Clean has not started.')
        self.progress.setRange(0, 0)
        self.spinner._label.setText('Preparing verified update')
        self.spinner.setToolTip('Preparing verified update')
        self.cancel_button.setText('Cancel update')
        self.worker_grid.hide()
        self.live_system_status.hide()
        self.worker_navigation.refresh(enabled=False)
        self._worker_elapsed_timer.stop()
        self._live_metrics_service = None
        self.processes_label.setText('CURRENT UPDATE OPERATION')
        self.processes.setPlainText('Download and verification only; no maintenance workflow is running.')
        self._last_update_phase = None

    def update_phase(self, value):
        if self.completed or not isinstance(value, dict):
            return
        phase = str(value.get('phase') or 'Preparing update')[:120]
        description = str(value.get('description') or value.get('message') or phase)[:4000]
        self.stage.setText(phase)
        self.spinner.start(phase)
        self.description.setText(description)
        received, total = value.get('bytes_received', value.get('received')), value.get('bytes_total', value.get('total'))
        if type(received) is int and type(total) is int and 0 <= received <= total and total > 0:
            self.progress.setRange(0, 100)
            self.progress.setValue(min(99, received * 100 // total))
            self.progress.setFormat('Download %p%')
            self.spinner.set_pct(self.progress.value())
        elif type(value.get('current')) is int and type(total) is int and 0 <= value['current'] <= total and total > 0:
            self.progress.setRange(0, 100)
            self.progress.setValue(min(99, value['current'] * 100 // total))
            self.progress.setFormat('Update preparation %p%')
            self.spinner.set_pct(self.progress.value())
        elif type(value.get('percentage')) in (int, float) and 0 <= value['percentage'] <= 100:
            self.progress.setRange(0, 100)
            self.progress.setValue(min(99, int(value['percentage'])))
            self.progress.setFormat('Update preparation %p%')
            self.spinner.set_pct(self.progress.value())
        else:
            self.progress.setRange(0, 0)
            self.spinner._label.setText(phase + ' — in progress')
            self.spinner.setToolTip(phase + ' — in progress')
        if (phase, description) != self._last_update_phase:
            self.log.appendPlainText(phase + ': ' + description)
            self._last_update_phase = phase, description

    def settled(self, message, *, staged=False):
        self.completed = True
        self._worker_elapsed_timer.stop()
        self.spinner.stop()
        self.loading_animation.stop()
        self.progress.setRange(0, 100)
        self.progress.setValue(100 if staged else 0)
        self.progress.setFormat('Download verified' if staged else 'Update not applied')
        self.stage.setText('Ready to restart' if staged else 'Continuing the current version')
        self.description.setText(message)
        self.log.appendPlainText(message)
        self.cancel_button.setText('Close update results')
        self.cancel_button.setEnabled(True)

    def request_close(self):
        if self.completed:
            self.close()
        else:
            self.cancel.set()
            self.cancel_button.setEnabled(False)
            self.description.setText('Cancelling the update. Waiting for the owned download/check worker to return; Auto Clean has not started.')


class _Signals(QObject):
    progress = Signal(object)
    result = Signal(object)
    error = Signal(object)
    finished = Signal()


class _Job(QRunnable):
    def __init__(self, action):
        super().__init__()
        self.action, self.signals = action, _Signals()
        self.outcome = None

    def run(self):
        try:
            self.outcome = self.action(self.signals.progress.emit)
            self.signals.result.emit(self.outcome)
        except Exception as error:
            self.signals.error.emit(error)
        finally:
            self.signals.finished.emit()


class StartupUpdateController(QObject):
    """One check/prompt, then update or the normal per-launch review."""
    def __init__(self, window, backend, *, resume_id=None, updater=None, continue_startup=None,
                 ask=None, progress_factory=None, manual_notice=None):
        super().__init__(window)
        self.window, self.backend = window, backend
        self.resume_id, self.updater = resume_id, updater
        self.continue_startup = continue_startup or window.review_launch
        self.ask = ask or ask_startup_update
        self.manual_notice = manual_notice or show_manual_update_notice
        self.progress_factory = progress_factory or SelfUpdateProgressWindow
        self.pool = QThreadPool(self)
        self.pool.setMaxThreadCount(1)
        self.cancel = threading.Event()
        self.pending, self._closed, self._closing, self._started = True, False, False, False
        self._jobs = set()
        self._progress = None
        self._snapshot = None
        self._cleanup_approved = False
        self._handoff_id = None
        self._stage_id = None
        self._revocation_pending = self._revocation_attempted = self._revoked = self._handoff_committed = False
        self._revocation_note = ''

    def _service(self):
        if self.updater is None:
            from .self_update import SelfUpdater
            self.updater = SelfUpdater(self.backend)
        return self.updater

    def _submit(self, phase, action, done):
        job = _Job(action)
        job.phase = phase
        self._jobs.add(job)
        job.signals.progress.connect(self._progress_event)
        def result(value):
            if not self._closed:
                try:
                    done(value)
                except Exception as error:
                    self._error(phase, error)
        job.signals.result.connect(result)
        job.signals.error.connect(lambda error: self._error(phase, error) if not self._closed else None)
        job.signals.finished.connect(lambda: self._finished(job))
        self.pool.start(job)

    def _finished(self, job):
        self._jobs.discard(job)
        if self._closing and not self._jobs and not self._closed:
            if self._stage_id and not self._revoked and not self._handoff_committed:
                if not self._revocation_attempted:
                    self._begin_revoke('Update cancelled before closing. The approved restart cleanup was revoked.')
                return
            self.pending = False
            self.window._startup_update_pending = False
            QTimer.singleShot(0, self._exit_after_stop)

    def _exit_after_stop(self):
        if not self.window.quit_application():
            # The normal Sandbox unsaved-work guard can still cancel exit.
            # A cancelled exit leaves a usable current GUI with no approval.
            self._closing = False
            self.window._close_when_idle = False
            self._release()

    def start(self):
        if self._started or self._closed or self.window.smoke_test:
            return
        self._started = True
        self.window._startup_update_pending = True
        self.window.invalidate_session_review('Checking for an Optimus update before Auto Clean. Maintenance is paused.')
        for button in self.window.main_buttons:
            button.setEnabled(False)
        try:
            service = self._service()  # Constructor is deliberately offline.
        except Exception as error:
            self._error('check', error)
            return
        if self.resume_id:
            self._submit('resume', lambda progress: service.consume_resume(self.resume_id), self._resume)
        else:
            self._submit('check', lambda progress: service.check(cancel_event=self.cancel), self._checked)

    def _progress_event(self, value):
        if not self._closed and self._progress is not None:
            self._progress.update_phase(value)

    def _checked(self, result):
        if self._closing or self.cancel.is_set():
            self._fallback('Optimus update check cancelled.')
            return
        if (isinstance(result, dict) and result.get('status') == 'available'
                and isinstance(result.get('offer'), dict) and result.get('automatic_install_supported') is not True):
            self.manual_notice(self.window, result)
            self._fallback('Automatic Optimus update is unavailable on this platform; continuing the current version.')
            return
        if (not isinstance(result, dict) or result.get('status') != 'available'
                or result.get('automatic_install_supported') is not True or not isinstance(result.get('offer'), dict)):
            self._fallback(str(result.get('reason') or 'Continuing the current Optimus version.')
                           if isinstance(result, dict) else 'The update check was unavailable; continuing normal launch review.')
            return
        self._snapshot = copy.deepcopy(self.window._settings())
        choice = self.ask(self.window, result['offer'], self._snapshot)
        if self._closing:
            return
        if choice == 'adjust_settings':
            self._release()
            self.window.open_settings(page='Settings')
            return
        if choice not in ('yes_update', 'no_update'):
            self._release()
            self.window.status.setText('The update prompt was closed. Auto Clean remains paused; choose Optimize to review this launch.')
            return
        if choice == 'no_update':
            self._fallback('Continuing the current Optimus version.')
            return
        self._cleanup_approved = (self._snapshot.get('setup_version', 0) >= CURRENT_SETUP_VERSION
            and scope_fingerprint(self.window._settings()) == scope_fingerprint(self._snapshot))
        self._progress = self.progress_factory(self.window, self.cancel)
        self._progress.show()
        self._submit('stage', lambda progress: self._service().stage(result['offer'], confirmed=True,
            cancel_event=self.cancel, progress_callback=progress), self._staged)

    def _staged(self, result):
        if (isinstance(result, dict) and result.get('status') == 'staged'
                and isinstance(result.get('stage_id'), str) and re.fullmatch('[a-f0-9]{32}', result['stage_id'])):
            self._stage_id = result['stage_id']
        if self._closing or self.cancel.is_set():
            self._fallback('Optimus update cancelled before handoff. The current version is still open.')
            return
        if (not isinstance(result, dict) or result.get('status') != 'staged'
                or not isinstance(result.get('stage_id'), str) or re.fullmatch('[a-f0-9]{32}', result['stage_id']) is None):
            self._fallback('The Optimus update could not be staged and verified. Continuing normal launch review.')
            return
        if not self._prepare_exit():
            self._fallback('Update handoff was cancelled. Save or discard Sandbox edits before updating.')
            return
        current = self.window._settings()
        approved = self._cleanup_approved and scope_fingerprint(current) == scope_fingerprint(self._snapshot)
        self._progress_event({'phase': 'Prepare restart', 'description':
            'Verified update is ready. Binding a single restart to unchanged saved choices; maintenance has not started.'})
        self._submit('prepare', lambda progress: self._service().prepare_handoff(result['stage_id'],
            cleanup_approved=approved, approved_settings=self._snapshot if approved else None), self._prepared)

    def _prepare_exit(self):
        window = self.window
        bridge = getattr(self.backend, 'bridge', None)
        if (window.busy or window._post_review_active or window._setup_wizard is not None
                or window._settings_window is not None and window._settings_window.busy
                or bool(getattr(getattr(self.backend, 'workflow', None), 'active', False))
                or bool(getattr(bridge, '_busy', False)) or bool(getattr(bridge, 'busy', False))
                or bool(getattr(bridge, '_pending', {})) or bool(getattr(bridge, 'pending', {}))):
            return False
        sandbox = window._sandbox_window
        if sandbox is not None:
            bypass = getattr(sandbox, '_optimus_close_bypass', False)
            sandbox._optimus_close_bypass = True
            try:
                accepted = sandbox.close()
            finally:
                sandbox._optimus_close_bypass = bypass
            if not accepted:
                return False
            window._sandbox_window = None
        return True

    def _prepared(self, result):
        if (isinstance(result, dict) and result.get('status') == 'ready'
                and isinstance(result.get('handoff_id'), str) and re.fullmatch('[a-f0-9]{32}', result['handoff_id'])):
            self._handoff_id = result['handoff_id']
        if self._closing or self.cancel.is_set():
            self._fallback('Update cancelled before starting the restart worker.')
            return
        if (not isinstance(result, dict) or result.get('status') != 'ready'
                or not isinstance(result.get('handoff_id'), str) or re.fullmatch('[a-f0-9]{32}', result['handoff_id']) is None):
            self._fallback('The verified update handoff was unavailable. Continuing normal launch review.')
            return
        self._handoff_id = result['handoff_id']
        self._submit('dispatch', lambda progress: self._service().dispatch_worker(self._handoff_id), self._dispatched)

    def _dispatched(self, result):
        if self._closing or self.cancel.is_set():
            self._begin_revoke('Update cancelled. The pending restart and automatic cleanup must be revoked before continuing.')
            return
        if isinstance(result, dict) and result.get('status') == 'dispatched' and result.get('started') is True:
            if self._progress is not None:
                self._progress.settled('The verified update worker is waiting for Optimus to close, then will restart the new version.', staged=True)
            self.pending = False
            self._handoff_committed = True
            self.window._startup_update_pending = False
            if not self.window.quit_application():
                self._handoff_committed = False
                self.pending = True
                self.window._startup_update_pending = True
                self._begin_revoke('Restart was cancelled by the normal close guard. Its automatic cleanup approval was revoked.')
        elif isinstance(result, dict) and result.get('started') is False:
            self._begin_revoke('The update restart worker could not start. Its unused restart approval was revoked.')
        else:
            self._begin_revoke('The restart worker outcome was unconfirmed. Its pending automatic cleanup approval was revoked.')

    def _begin_revoke(self, note):
        if self._revoked:
            self._fallback(note)
            return
        if self._revocation_pending or self._revocation_attempted:
            return
        identity = self._handoff_id or self._stage_id
        if not identity:
            self._fallback(note)
            return
        self._revocation_pending = self._revocation_attempted = True
        self._revocation_note = note
        self.window.status.setText('Revoking the pending update restart approval before continuing. Maintenance remains paused.')
        self._submit('revoke', lambda progress: self._service().cancel_handoff(identity), self._revocation_result)

    def _revocation_result(self, result):
        self._revocation_pending = False
        if isinstance(result, dict) and result.get('revoked') is True and result.get('cleanup_approved') is False:
            self._revoked = True
            self._fallback(self._revocation_note)
        else:
            self.window.status.setText('The update restart approval could not be revoked. Maintenance and automatic exit remain paused for review.')

    def _resume(self, result):
        if self._closing:
            return
        current = self.window._settings()
        accepted = (isinstance(result, dict) and result.get('status') == 'accepted'
                    and isinstance(result.get('settings'), dict)
                    and scope_fingerprint(result['settings']) == scope_fingerprint(current))
        if accepted:
            launchers = result.get('launchers') if isinstance(result.get('launchers'), dict) else {}
            if launchers.get('manual_action_required') is True:
                items = launchers.get('manual_items') if isinstance(launchers.get('manual_items'), list) else []
                reasons = [str(row.get('reason') or row.get('kind') or 'Launcher review required')[:300]
                           for row in items[:5] if isinstance(row, dict)]
                self.window._self_update_notice = ('Optimus updated. Some shortcuts or sign-in links need manual review. '
                    + ' '.join(reasons))[:1800]
                self.window.status.setText(self.window._self_update_notice)
        if accepted and result.get('cleanup_approved') is True and current.get('setup_version', 0) >= CURRENT_SETUP_VERSION:
            self._release()
            self.window._launch_consent.approve(current)
            self.window.start_optimization()
            callback = self.window._session_approved_callback
            if callable(callback) and self.window.session_approved():
                callback()
            return
        self._release()
        if accepted and current.get('setup_version', 0) < CURRENT_SETUP_VERSION:
            self.window.open_initial_setup(first_run=True)
        QTimer.singleShot(0, self.continue_startup)

    def _release(self):
        self.pending = False
        self.window._startup_update_pending = False
        if not self._closing:
            for button in self.window.main_buttons:
                button.setEnabled(True)

    def _fallback(self, note):
        if self._stage_id and not self._revoked and not self._handoff_committed:
            self._begin_revoke(note)
            return
        if self._progress is not None:
            self._progress.settled(note)
        self.window.status.setText(note)
        if self._closing:
            return
        self._release()
        QTimer.singleShot(0, self.continue_startup)

    def _error(self, phase, error):
        if phase == 'revoke':
            self._revocation_pending = False
            self.window.status.setText('The update restart approval could not be revoked. Maintenance and automatic exit remain paused for review.')
            return
        self._fallback('Optimus update ' + phase + ' was unavailable; continuing normal launch review. ' + str(error)[:400])

    def request_safe_stop(self):
        self._closing = True
        self.cancel.set()
        self.window._close_when_idle = True
        dialog = getattr(self.window, '_self_update_dialog', None)
        if dialog is not None:
            dialog.reject()
        if self._progress is not None:
            self._progress.request_close()
        self.window.status.setText('Stopping the update check/download safely before closing. Maintenance has not started.')
        if self._stage_id and not self._handoff_committed:
            if self._revocation_attempted and not self._revocation_pending and not self._revoked:
                self._revocation_attempted = False  # An explicit new close request may retry revocation.
            self._begin_revoke('Update cancelled before closing. Its automatic cleanup approval was revoked.')
        if not self._jobs:
            if self._stage_id and not self._revoked and not self._handoff_committed:
                return True
            self.pending = False
            self.window._startup_update_pending = False
            QTimer.singleShot(0, self._exit_after_stop)
        return True

    def shutdown(self):
        self._closed = True
        self.cancel.set()
        pending = list(self._jobs)
        self.pool.waitForDone()
        # External QApplication.quit can bypass normal close events. Drain our
        # private pool, then revoke a staged/prepared resume before backend exit.
        if not self._handoff_committed:
            for job in pending:
                value = job.outcome if isinstance(job.outcome, dict) else {}
                if job.phase == 'stage' and value.get('status') == 'staged':
                    self._stage_id = value.get('stage_id')
                if job.phase == 'prepare' and value.get('status') == 'ready':
                    self._handoff_id = value.get('handoff_id')
            if (self._handoff_id or self._stage_id) and not self._revoked:
                try:
                    value = self._service().cancel_handoff(self._handoff_id or self._stage_id)
                    self._revoked = (isinstance(value, dict) and value.get('revoked') is True
                                     and value.get('cleanup_approved') is False)
                except Exception:
                    self.window.status.setText('Closing update approval needs review; automatic cleanup was not started by this window.')
        if self._progress is not None:
            self._progress.settled('Optimus is closing; maintenance did not start during its update.')
