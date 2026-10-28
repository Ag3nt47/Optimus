"""Cached worker navigation wiring; the workflow owns cancellation and receipts."""
from __future__ import annotations

from PySide6.QtCore import QObject, Signal


class WorkerNavigationController(QObject):
    """Connect cards to one live backend without starting or inspecting host work."""
    stateChanged = Signal()

    def __init__(self, backend, cards, parent=None):
        super().__init__(parent)
        self.backend, self.cards = backend, cards
        self._connected = set()
        self._enabled = True

    def _connect_cards(self):
        for identity, card in self.cards.items():
            if identity not in self._connected:
                card.navigationRequested.connect(
                    lambda action, state, identity=identity: self.request(identity, action, state))
                self._connected.add(identity)

    def refresh(self, *, enabled=True):
        self._enabled = enabled is True
        self._connect_cards()
        getter = getattr(self.backend, 'workflow_navigation_status', None)
        snapshot = None
        if self._enabled and callable(getter):
            try:
                snapshot = getter()
            except Exception:
                # A failed status read cannot authorize a control.
                pass
        snapshot = snapshot if isinstance(snapshot, dict) else {}
        active = snapshot.get('active') is True
        session = snapshot.get('session_id')
        jobs = snapshot.get('jobs')
        jobs = {row.get('id'): row for row in jobs if isinstance(row, dict)} if isinstance(jobs, list) else {}
        requester = getattr(self.backend, 'request_workflow_navigation', None)
        for identity, card in self.cards.items():
            row = jobs.get(identity)
            if isinstance(row, dict) and isinstance(session, str) and callable(requester) and self._enabled and active:
                card.set_navigation(dict(row, session_id=session), enabled=True)
            else:
                card.set_navigation({}, enabled=False)
        self.stateChanged.emit()

    def request(self, identity, action, state):
        card = self.cards.get(identity)
        if card is None or not self._enabled or not isinstance(state, dict):
            return
        request = getattr(self.backend, 'request_workflow_navigation', None)
        if not callable(request) or action not in ('back', 'next', 'cancel'):
            return
        # Bind to the exact visible session/item revision. A refreshed view must
        # never silently retarget a queued button click to another operation.
        result = None
        try:
            result = request(state.get('session_id'), identity, state.get('revision'), action)
            if not isinstance(result, dict):
                raise ValueError('The worker returned an invalid navigation result.')
            accepted = result.get('accepted') is True
            message = result.get('message')
            if not isinstance(message, str) or not message.strip():
                message = ('Request queued; waiting for the current operation to return.' if accepted else
                           'The worker has moved on. Review its current item before trying again.')
        except Exception:
            message = 'The request could not be verified. Refresh the worker status before trying again.'
        self.refresh(enabled=self._enabled)
        if not (isinstance(result, dict) and result.get('accepted') is True
                and card._navigation_state.get('viewing_history') is True):
            card.set_navigation_message(message)
        self.stateChanged.emit()
