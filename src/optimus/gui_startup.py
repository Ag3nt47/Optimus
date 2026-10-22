"""First-run questions and opt-in maintenance cadence for the running GUI."""
from __future__ import annotations

import time
from . import automation
from .automation import CURRENT_SETUP_VERSION, due, load_attempt


class SetupAutomationController:
    """Called on the GUI thread; every apply still uses the normal workflow worker."""

    def __init__(self, backend, window, *, modal_active=None, clock=None):
        self.backend = backend
        self.window = window
        self.modal_active = modal_active or (lambda: False)
        self.clock = clock or time.time
        self.prompted = False
        self.stopped = False
        self._session_id = automation.SESSION_ID
        self._startup_attempted = False

    def stop(self):
        self.stopped = True

    def _available(self):
        window = self.window
        if self.stopped or getattr(window, 'smoke_test', False) or getattr(window, '_quitting', False):
            return False
        if getattr(window, 'busy', False) or self.modal_active():
            return False
        for name in ('_setup_wizard', '_settings_window', '_sandbox_window', '_progress_window'):
            child = getattr(window, name, None)
            if child is not None and (getattr(child, 'busy', False) or child.isVisible()):
                return False
        return not getattr(getattr(self.backend, 'workflow', None), 'active', False)

    def tick(self):
        if not self._available():
            return
        try:
            settings = self.backend.preferences()
            if settings.get('setup_version', 0) < CURRENT_SETUP_VERSION:
                if not self.prompted:
                    self.prompted = True
                    self.window.open_initial_setup(first_run=True)
                return
            startup_mode = settings.get('automation_mode') == 'startup'
            if startup_mode and self._startup_attempted:
                return
            last_attempt = load_attempt(self.backend.data_dir)
            if isinstance(last_attempt, dict) and last_attempt.get('status') == 'invalid':
                self.window.status.setText(last_attempt.get('error', 'Automatic maintenance is paused; review setup.'))
                return
            if startup_mode:
                own_state = isinstance(last_attempt, dict) and last_attempt.get('session_id') == self._session_id
                own_report = getattr(getattr(self.backend, 'workflow', None), 'last_report', None)
                if own_state or isinstance(own_report, dict) and own_report:
                    self._startup_attempted = True
                    return
            if due(settings, last_attempt, now=self.clock()):
                if startup_mode:
                    # Another process may overwrite the shared journal. It must
                    # never re-arm this process's already dispatched startup run.
                    # A failed attempt still permits an explicit Optimize retry.
                    self._startup_attempted = True
                self.window.start_optimization()
        except Exception as error:
            self.window.status.setText('Automatic maintenance could not start: ' + str(error))
