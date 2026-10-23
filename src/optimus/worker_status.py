"""Bounded, thread-safe status for independent optional maintenance workers.

This is observation and cooperative cancellation, never an installer receipt or
an alternative to the maintenance lease. Services keep their own durable logs.
"""
from __future__ import annotations

from collections import OrderedDict
from contextlib import contextmanager
import re
import threading
import time
import uuid

_IDENTITY = re.compile(r'[a-z][a-z0-9_.-]{0,63}\Z')
_STATES = frozenset(('queued', 'scanning', 'running', 'waiting', 'applying',
                     'stopping', 'completed', 'cancelled', 'needs_review', 'failed'))
_FINAL = frozenset(('completed', 'cancelled', 'needs_review', 'failed'))


def _line(value, limit=1000):
    if not isinstance(value, str):
        return ''
    return ''.join(char for char in value if char >= ' ' or char == '\n')[:limit]


class WorkerStatus:
    """In-memory status only; constructing or polling it performs no host work."""

    def __init__(self, *, clock=time.monotonic, limit=48):
        if type(limit) is not int or not 4 <= limit <= 128:
            raise ValueError('Worker history limit must be between 4 and 128.')
        self._clock, self._limit = clock, limit
        self._lock = threading.RLock()
        self._jobs = OrderedDict()

    def begin(self, identity, title, *, description='', status='queued'):
        if not isinstance(identity, str) or not _IDENTITY.fullmatch(identity):
            raise ValueError('Use one bounded worker identity.')
        if not isinstance(title, str) or not title.strip() or len(title) > 100:
            raise ValueError('Use a short worker title.')
        if status not in _STATES - _FINAL:
            raise ValueError('A new worker must have an active state.')
        with self._lock:
            previous = self._jobs.get(identity)
            if previous and previous['status'] not in _FINAL:
                raise RuntimeError('That worker is already active.')
            while len(self._jobs) >= self._limit and identity not in self._jobs:
                removable = next((key for key, row in self._jobs.items() if row['status'] in _FINAL), None)
                if removable is None:
                    raise RuntimeError('The worker status limit is occupied by active work.')
                del self._jobs[removable]
            token = uuid.uuid4().hex
            self._jobs[identity] = {'id': identity, 'title': title, 'token': token,
                'status': status, 'description': _line(description), 'current': None,
                'total': None, 'started': self._clock(), 'finished': None,
                'cancel': threading.Event(), 'processes': []}
            self._jobs.move_to_end(identity)
            return token

    def _owned(self, identity, token):
        row = self._jobs.get(identity)
        if row is None or row['token'] != token:
            raise ValueError('Worker status belongs to a different operation.')
        return row

    def cancellation(self, identity, token):
        with self._lock:
            return self._owned(identity, token)['cancel']

    def update(self, identity, token, *, status=None, description=None,
               current=None, total=None, processes=None):
        with self._lock:
            row = self._owned(identity, token)
            if row['status'] in _FINAL:
                return False
            if status is not None and status not in _STATES:
                raise ValueError('Unknown worker state.')
            if current is not None or total is not None:
                if (type(current) is not int or current < 0 or type(total) is not int
                        or total < 0 or current > total):
                    raise ValueError('Item counts must be explicit ordered nonnegative integers.')
            if processes is not None and not isinstance(processes, list):
                raise ValueError('Worker process descriptions must be a list.')
            if status is not None:
                # Acknowledging cancellation is distinct from completed work.
                row['status'] = status
                if status in _FINAL:
                    row['finished'] = self._clock()
            if description is not None:
                row['description'] = _line(description)
            if current is not None or total is not None:
                row.update(current=current, total=total)
            if processes is not None:
                row['processes'] = [_line(value, 200) for value in processes[:8] if isinstance(value, str)]
            return True

    def request_stop(self):
        """Ask services to stop; do not claim they or their installers exited."""
        with self._lock:
            requested = []
            for row in self._jobs.values():
                if row['status'] not in _FINAL:
                    row['cancel'].set()
                    row['status'] = 'stopping'
                    requested.append(row['id'])
            return requested

    def snapshot(self):
        with self._lock:
            now = self._clock()
            rows = []
            for row in self._jobs.values():
                visible = {key: row[key] for key in ('id', 'title', 'status', 'description', 'current', 'total')}
                visible['processes'] = list(row['processes'])
                visible['elapsed_seconds'] = max(0, (row['finished'] if row['finished'] is not None else now) - row['started'])
                visible['cancellation_requested'] = row['cancel'].is_set()
                visible['terminal'] = row['status'] in _FINAL
                rows.append(visible)
            return rows

    @contextmanager
    def operation(self, identity, title, *, description='', status='running'):
        token = self.begin(identity, title, description=description, status=status)
        try:
            yield token, self.cancellation(identity, token)
        except BaseException:
            self.update(identity, token, status='needs_review',
                        description='The worker did not return a verified result. Review its operation log.')
            raise
        else:
            # The owner supplies a terminal outcome. Returning from a Python
            # call alone cannot establish that an OS or cloud operation worked.
            with self._lock:
                row = self._owned(identity, token)
                if row['status'] not in _FINAL:
                    self.update(identity, token, status='needs_review',
                                description='The operation returned without a recorded final result.')
