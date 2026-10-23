"""Session-bound, cooperative navigation for actual workflow workers.

Commands never call OS APIs, terminate installers or undo completed changes.
The runner owns quiescence and durable receipts. Back reviews bounded prior
metadata. Retrying an operation requires a fresh separately approved run.
"""
from __future__ import annotations

import copy
import re
import threading
import uuid

_JOB = re.compile(r'[a-z][a-z0-9_.-]{0,63}\Z')
_SESSION = re.compile(r'[a-f0-9]{32}\Z')
_ACTIONS = frozenset(('back', 'next', 'cancel'))


def _line(value, maximum=600):
    return ''.join(c for c in str(value or '') if c >= ' ')[:maximum]


class ItemCancellation:
    """Global/lane cancellation and a current-item request remain distinguishable."""
    def __init__(self, cursor, external=None):
        self.cursor, self.external = cursor, external

    def is_set(self):
        return self.cursor.stop_requested() or self.external is not None and self.external.is_set()


class NavigationHub:
    def __init__(self):
        self.lock = threading.RLock()
        self.session_id, self.run_id, self.active = None, None, False
        self.cancel_event, self.jobs = None, {}

    def begin(self, run_id, *, cancel_event=None):
        if not isinstance(run_id, str) or not _SESSION.fullmatch(run_id):
            raise ValueError('A current workflow run identity is required for worker navigation.')
        with self.lock:
            if self.active:
                raise RuntimeError('Worker navigation already belongs to an active workflow.')
            self.session_id, self.run_id = uuid.uuid4().hex, run_id
            self.active, self.cancel_event, self.jobs = True, cancel_event, {}
            return self.session_id

    def cursor(self, job_id, title=None):
        if not isinstance(job_id, str) or not _JOB.fullmatch(job_id):
            raise ValueError('Choose an exact bounded worker identity.')
        with self.lock:
            if not self.active:
                return None
            if job_id not in self.jobs:
                if len(self.jobs) >= 12:
                    raise ValueError('The active navigation worker limit was reached.')
                self.jobs[job_id] = WorkerCursor(self, job_id, title or job_id)
            return self.jobs[job_id]

    def stopped(self):
        return not self.active or self.cancel_event is not None and self.cancel_event.is_set()

    def snapshot(self):
        with self.lock:
            return {'session_id': self.session_id, 'run_id': self.run_id, 'active': self.active,
                    'global_cancelled': self.cancel_event is not None and self.cancel_event.is_set(),
                    'jobs': [cursor._snapshot() for cursor in self.jobs.values()]}

    def request(self, session_id, job_id, revision, action):
        with self.lock:
            if (not self.active or not isinstance(session_id, str) or session_id != self.session_id
                    or not isinstance(job_id, str) or job_id not in self.jobs
                    or type(revision) is not int or not isinstance(action, str) or action not in _ACTIONS):
                return {'accepted': False, 'message': 'This worker control belongs to an inactive or different workflow. Refresh its status.'}
            cursor = self.jobs[job_id]
            if revision != cursor.revision:
                return {'accepted': False, 'message': 'This item or worker phase changed. Refresh before issuing another command.',
                        'revision': cursor.revision}
            return cursor._request(action)

    def end(self, status='completed'):
        with self.lock:
            for cursor in self.jobs.values():
                cursor.end(status)
            self.active = False


class WorkerCursor:
    def __init__(self, hub, identity, title):
        self.hub, self.id, self.title = hub, identity, _line(title, 100)
        self.revision, self.active, self.current = 0, True, None
        self.history, self.view_index = [], None
        self.pending, self.message = None, 'Waiting for this worker to begin an item.'
        self._item_stop = threading.Event()
        self._unknown = False
        self.receipt_callback = None

    def stop_requested(self):
        with self.hub.lock:
            return self._item_stop.is_set() or self.hub.stopped() or not self.active

    def local_requested(self):
        with self.hub.lock:
            return self._item_stop.is_set()

    def cancellation(self, external=None):
        return ItemCancellation(self, external)

    def begin_item(self, key, label, *, kind='scan', retryable=False, phase=None):
        if not isinstance(key, str) or not key or len(key) > 300 or kind not in ('scan', 'download', 'mutation', 'check'):
            raise ValueError('A bounded exact work item and supported operation kind are required.')
        with self.hub.lock:
            if not self.active or self.hub.stopped() or self._unknown and kind == 'mutation':
                return False
            if self.current is not None:
                raise RuntimeError('The preceding item must save its terminal outcome before another begins.')
            if any(row['key'] == key and row.get('mutation_started') is True for row in self.history):
                raise ValueError('A previously started mutation cannot be replayed through worker navigation.')
            self.current = {'key': key, 'label': _line(label, 300), 'kind': kind,
                            'thread_id': threading.get_native_id(),
                            'phase': _line(phase or ('checking' if kind == 'mutation' else kind), 80),
                            'retryable': retryable is True, 'mutation_started': False}
            self._item_stop = threading.Event()
            self.view_index, self.pending = None, None
            self.message = self.current['label']
            self.revision += 1
            return True

    def phase(self, phase, *, mutation_started=False, message=None):
        with self.hub.lock:
            if self.current is None or not self.active:
                return
            phase = _line(phase, 80)
            changed = self.current['phase'] != phase or mutation_started and not self.current['mutation_started']
            self.current['phase'] = phase
            self.current['mutation_started'] |= mutation_started is True
            if message:
                self.message = _line(message)
            if changed:
                self.revision += 1

    def finish(self, status, *, message=None, mutation_started=None, outcome_known=True):
        with self.hub.lock:
            if self.current is None:
                return None
            row = dict(self.current, status=_line(status, 64), outcome_known=outcome_known is True)
            if type(mutation_started) is bool:
                row['mutation_started'] = mutation_started
            row['retryable'] = bool(row['retryable'] and not row['mutation_started'] and outcome_known is True
                                    and status in ('skipped', 'cancelled', 'not_started'))
            if message:
                row['message'] = _line(message)
            decision = copy.deepcopy(self.pending)
            self.history.append(row)
            self.history[:] = self.history[-64:]
            self.current, self.pending, self.view_index = None, None, None
            self._item_stop.clear()
            if outcome_known is not True:
                self._unknown = True
                self.message = 'The current operation has an unknown outcome; later work requires review.'
            elif decision and row['mutation_started'] and status in ('completed', 'restored'):
                self.message = 'The native operation finished before it could be cancelled. Its actual completed outcome was retained.'
            else:
                self.message = _line(message or f"{row['label']}: {status}")
            self.revision += 1
            return decision

    def end(self, status='completed'):
        with self.hub.lock:
            self.active = False
            self.pending = None
            self._item_stop.set()
            self.revision += 1
            if not self._unknown:
                self.message = 'Worker ' + _line(status, 64) + '. Completed changes are not replayed or undone by navigation.'

    def _snapshot(self):
        stopped = self.hub.stopped() or not self.active or self._unknown
        ready = not stopped and self.pending is None
        live = self.current is not None
        viewing = self.view_index is not None
        previous = self.view_index - 1 if viewing else len(self.history) - 1
        can_back = ready and previous >= 0
        back_mode = 'history'
        label = self.current['label'] if live else 'the current item'
        critical = live and self.current['mutation_started']
        wait = ('The live installer must finish and save its outcome; no installer process is force-stopped.' if critical
                else 'The item is asked to stop cooperatively. If its native API cannot abort safely, Optimus waits for it to return.')
        return {'id': self.id, 'title': self.title, 'revision': self.revision,
                'active': self.active and not stopped, 'pending': copy.deepcopy(self.pending),
                'current': copy.deepcopy(self.current), 'phase': self.current['phase'] if live else 'idle',
                'viewing_history': viewing,
                'view_item': copy.deepcopy(self.history[self.view_index]) if viewing else None,
                'history_count': len(self.history), 'message': self.message,
                'can_back': can_back,
                'can_next': ready and (viewing or live), 'can_cancel': ready and live,
                'back_mode': back_mode, 'next_mode': 'history' if viewing else 'skip',
                'back_reason': ('Show the previous saved item details. Completed changes are not rerun or undone; use Revert where supported.' if can_back else
                                'No earlier item is available, or this worker is stopping/complete.'),
                'next_reason': ('Move forward through saved details and return to the live item; this does not cancel live work.' if viewing else
                                'Skip the live item: ' + label + '. ' + wait if live else 'No live item is available.'),
                'cancel_reason': 'Cancel only the live item: ' + label + '. ' + wait}

    def _request(self, action):
        state = self._snapshot()
        if not state['can_' + action]:
            return {'accepted': False, 'message': state[action + '_reason'], 'revision': self.revision}
        if action == 'back':
            self.view_index = self.view_index - 1 if self.view_index is not None else len(self.history) - 1
            row = self.history[self.view_index]
            self.message = 'Previous item details: ' + row['label'] + ' — ' + row['status'] + '. No operation was rerun.'
        elif action == 'next' and self.view_index is not None:
            self.view_index += 1
            if self.view_index >= len(self.history):
                self.view_index = None
                self.message = 'Returned to the live item. No cancellation was requested.'
            else:
                row = self.history[self.view_index]
                self.message = 'Saved item details: ' + row['label'] + ' — ' + row['status']
        else:
            self.pending = {'action': action,
                            'item_key': self.current['key'] if self.current else None}
            self._item_stop.set()
            self.message = ('Cancellation requested for the live item.' if action == 'cancel' else
                            'Next requested: stop or skip this live item at a safe boundary.') + ' ' + state['cancel_reason']
        self.revision += 1
        return {'accepted': True, 'message': self.message, 'revision': self.revision,
                'pending': copy.deepcopy(self.pending), 'viewing_history': self.view_index is not None}


def cursor_for(backend, job_id, title=None):
    navigation = getattr(getattr(backend, 'workflow', None), 'navigation', None)
    return navigation.cursor(job_id, title) if isinstance(navigation, NavigationHub) else None


class WorkItem:
    """Small optional adapter: legacy fixture backends keep their old tokens."""
    def __init__(self, cursor, key, label, *, external=None, kind='scan', retryable=False):
        self.cursor, self.external = cursor, external
        self.started = bool(cursor is not None and cursor.begin_item(key, label, kind=kind, retryable=retryable))
        if cursor is not None and not self.started:
            error = RuntimeError('This worker has ended or has an unresolved outcome; no new item can start.')
            error.installer_started, error.outcomeKnown, error.running = False, True, False
            raise error
        self.cancel_event = cursor.cancellation(external) if self.started else external
        self.decision = None

    def requested(self):
        return self.started and self.cursor.local_requested()

    def installing(self):
        if self.started:
            self.cursor.phase('installing', mutation_started=True)

    def finish(self, status, *, result=None, message=None, mutation_started=None, outcome_known=True):
        if self.started:
            if result is not None and callable(self.cursor.receipt_callback):
                try:
                    self.cursor.receipt_callback(result)
                except Exception as error:
                    self.cursor.finish('failed', mutation_started=mutation_started, outcome_known=False,
                        message='The terminal item receipt could not be saved; later work requires review.')
                    self.started = False
                    error.outcomeKnown, error.installer_started, error.running = False, True, False
                    raise
            self.decision = self.cursor.finish(status, message=message,
                mutation_started=mutation_started, outcome_known=outcome_known)
            self.started = False
        return self.decision


def navigation_skip(note='You skipped this exact item before any installer started.'):
    return {'cancelled': True, 'skipped': True, 'navigation_cancelled': True,
            'installer_started': False, 'outcomeKnown': True, 'running': False,
            'exitCode': None, 'note': note}


def merge_presence(result, presence):
    """Retain separate presence receipts and combine only explicit lifecycle."""
    if presence is None:
        return result
    result = dict(result, app_presence=presence)
    result['failed_items'] = result.get('failed_items', 0) + presence.get('failed_items', 0)
    result['navigation_cancelled_items'] = result.get('navigation_cancelled_items', 0) + presence.get('navigation_cancelled_items', 0)
    if presence.get('cancelled') is True:
        result['cancelled'] = True
    if presence.get('installer_started') is True:
        result['installer_started'] = True
    if presence.get('outcomeKnown') is False:
        result['outcomeKnown'] = False
    if presence.get('running') is True:
        result['running'] = True
    if presence.get('restartNeeded') is True:
        result['restartNeeded'] = True
    return result


def local_skip(result):
    """Only proven pre-launch local cancellation is safe to continue past."""
    return (isinstance(result, dict) and result.get('navigation_cancelled') is True
            and result.get('installer_started') is False and result.get('outcomeKnown') is True
            and result.get('running') is False and result.get('uncertain') is not True
            and result.get('restartNeeded') is not True and result.get('exitCode') not in (3010, 1641))


_PACKAGE_REFUSALS = frozenset(('version_changed', 'pinned', 'update_hold', 'unsupported_installer',
    'unsupported_application', 'package_unavailable', 'quiet_mode_unavailable', 'compatibility_block'))
_SHARED_REFUSALS = frozenset(('pin_inventory_untrusted', 'source_untrusted', 'source_unavailable',
    'admin_required', 'authorization', 'insufficient_disk', 'restart_required', 'approvals_changed', 'security_policy'))
# WUA documents these as concurrent installs/restart, missing authorization,
# self-update/shutdown or shared service/network conditions, not bad drivers.
# https://learn.microsoft.com/windows/deployment/update/windows-update-error-reference
_SHARED_HRESULTS = frozenset((0x80240016, 0x80240018, 0x80240019, 0x8024001B,
    0x8024001E, 0x8024001F, 0x80240020, 0x80070005, 0x80070070, 0x8A150102))


def shared_update_refusal(value):
    if not isinstance(value, dict):
        return None
    shared = (value.get('shared_barrier') is True or value.get('failure_scope') in ('shared', 'system', 'batch')
              or value.get('failureKind') in _SHARED_REFUSALS)
    for key in ('hresult', 'aggregateHresult', 'exitCode'):
        code = value.get(key)
        if type(code) is int and (code & 0xffffffff in _SHARED_HRESULTS or key == 'exitCode' and code == 1618):
            shared = True
    if (value.get('requiresReview') is True and value.get('installer_started') is False
            and value.get('failure_scope') != 'package' and value.get('failureKind') not in _PACKAGE_REFUSALS):
        shared = True
    return _line(value.get('note') or value.get('error') or 'Shared update safety checks require review before another installer.', 1000) if shared else None


def can_fail_forward(value):
    """Advance only after explicit terminal evidence, preserving shared gates."""
    if isinstance(value, dict):
        if any(key in value and (type(value[key]) is not int or value[key] not in (2, 3, 4, 5))
               for key in ('resultCode', 'aggregateResultCode')):
            return False
        if any(key in value and type(value[key]) is not int for key in ('hresult', 'aggregateHresult')):
            return False
    return (isinstance(value, dict) and value.get('outcomeKnown') is True
            and value.get('running') is False and type(value.get('installer_started')) is bool
            and value.get('cancelled') is not True and value.get('uncertain') is not True
            and value.get('restartNeeded') is not True and value.get('exitCode') not in (3010, 1641)
            and not shared_update_refusal(value))
