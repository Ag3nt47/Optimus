"""Short-lived copies of complete audit metadata for workflow reviews only.

No process, update offer, approval, security state or mutation decision is cached.
The normal backend inventory methods remain fresh. One invalidation closes the
seed for the rest of a run, including while an installer runs on another thread.
"""
from __future__ import annotations

import copy
from functools import wraps
import json
import math
import threading
import time

MAX_ROWS = 5000
MAX_BYTES = 2 * 1024 * 1024
TTL_SECONDS = 30
_CREATION = threading.RLock()


def _bounded_rows(value):
    if type(value) is not list or len(value) > MAX_ROWS or any(type(row) is not dict for row in value):
        return False
    pending, nodes, characters = [(value, 0)], 0, 0
    while pending:
        item, depth = pending.pop()
        nodes += 1
        if nodes > 50000 or depth > 12:
            return False
        if type(item) is dict:
            if any(type(key) is not str for key in item):
                return False
            pending.extend((part, depth + 1) for pair in item.items() for part in pair)
        elif type(item) is list:
            pending.extend((part, depth + 1) for part in item)
        elif type(item) is str:
            characters += len(item)
            if len(item) > 65536 or characters > MAX_BYTES:
                return False
        elif type(item) in (int, float):
            try:
                if not math.isfinite(item):
                    return False
            except (OverflowError, ValueError):
                return False
        elif item is not None and type(item) is not bool:
            return False
    size = 0
    try:
        for fragment in json.JSONEncoder(ensure_ascii=False, allow_nan=False).iterencode(value):
            size += len(fragment.encode('utf-8'))
            if size > MAX_BYTES:
                return False
    except (UnicodeError, ValueError):
        return False
    return True


class RunReviewInventory:
    def __init__(self, *, clock=time.monotonic):
        self.clock = clock
        self._lock = threading.RLock()
        self._active = False
        self._seed_closed = True
        self._captured = None
        self._sections = {}
        self._hits = 0

    def begin(self, *, enabled=True):
        with self._lock:
            self._active = enabled is True
            self._seed_closed = not self._active
            self._captured = None
            self._sections = {}
            self._hits = 0

    def seed(self, audit, *, captured_at=None):
        with self._lock:
            if not self._active or self._seed_closed:
                return
            self._seed_closed = True
            # A partial or ambiguous producer never becomes a reusable complete
            # inventory. The existing Windows audit explicitly reports errors.
            if type(audit) is not dict or audit.get('InventoryErrors') != []:
                return
            for name, field in (('applications', 'InstalledApps'), ('startup', 'Startup')):
                rows = audit.get(field)
                if name == 'startup' and audit.get('StartupInventoryComplete') is not True:
                    continue
                if _bounded_rows(rows):
                    self._sections[name] = copy.deepcopy(rows)
            now = self.clock()
            stamp = now if captured_at is None else captured_at
            if type(stamp) not in (int, float) or not math.isfinite(stamp) or not 0 <= now - stamp < TTL_SECONDS:
                self._sections = {}
            self._captured = stamp if self._sections else None

    def get(self, name):
        with self._lock:
            if not self._active or self._captured is None or name not in self._sections:
                return None
            age = self.clock() - self._captured
            if not 0 <= age < TTL_SECONDS:
                self._sections = {}
                self._captured = None
                return None
            self._hits += 1
            return copy.deepcopy(self._sections[name])

    def invalidate(self):
        with self._lock:
            self._sections = {}
            self._captured = None
            self._seed_closed = True

    def end(self):
        with self._lock:
            self.invalidate()
            self._active = False

    def status(self):
        with self._lock:
            return {'enabled': self._active, 'reused_inventories': self._hits,
                    'scope': 'Current workflow read-only application/startup reviews only',
                    'maximum_age_seconds': TTL_SECONDS}


def service(backend, *, create=False):
    with _CREATION:
        value = getattr(backend, '_run_review_inventory', None)
        if value is None and create:
            value = RunReviewInventory()
            backend._run_review_inventory = value
        return value


def invalidate_on_mutation(operation):
    """Drop review evidence before entry and after every returned/failed call."""
    @wraps(operation)
    def guarded(backend, *args, **kwargs):
        inventory = service(backend)
        if inventory is not None:
            inventory.invalidate()
        try:
            return operation(backend, *args, **kwargs)
        finally:
            if inventory is not None:
                inventory.invalidate()
    return guarded
