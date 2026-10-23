"""Bounded current-item progress, separate from queue counts and CPU activity."""
from __future__ import annotations

import math
import inspect
import re
import threading
import time


def normalize_item_progress(value):
    """Accept explicit producer progress; invalid percentages stay unknown.

    A missing/invalid identity or phase rejects the event. Never substitute a
    queue fraction, elapsed time or CPU percentage for an unknown item value.
    """
    if not isinstance(value, dict):
        return None
    key, label, phase = (value.get(name) for name in ('key', 'label', 'phase'))
    if (not isinstance(key, str) or not key or len(key) > 512 or key != key.strip()
            or any(ord(char) < 32 or ord(char) == 127 for char in key)
            or not isinstance(label, str) or not label.strip() or len(label) > 2000
            or not isinstance(phase, str) or re.fullmatch(r'[a-z][a-z0-9_-]{0,47}', phase) is None):
        return None
    percent = value.get('percent')
    if type(percent) not in (int, float) or not 0 <= percent <= 100 or not math.isfinite(percent):
        percent = None
    else:
        percent = float(percent)
    normalized = {'key': key, 'label': ' '.join(label.split())[:512], 'phase': phase, 'percent': percent}
    if value.get('estimated') is True:
        normalized['estimated'] = True
    return normalized


def accepts_progress_callback(operation):
    """Opt in before invocation; never retry an operation after TypeError."""
    try:
        return 'progress_callback' in inspect.signature(operation).parameters
    except (TypeError, ValueError):
        return False


class BoundItemProgress:
    """Bind transient reporter ticks to one current item until its call ends."""
    def __init__(self, callback, key, label, context=None):
        self.callback, self.key, self.label = callback, key, label
        self.context = dict(context or {})
        self.lock = threading.RLock()
        self.closed, self.previous, self.last_sent = False, None, None

    def __call__(self, state):
        if not isinstance(state, dict):
            return
        source = state.get('item_progress', state)
        if not isinstance(source, dict) or ('key' in source and source['key'] != self.key):
            return
        value = normalize_item_progress(dict(source, key=self.key, label=self.label))
        if value is None:
            return
        with self.lock:
            if self.closed or value == self.previous:
                return
            now = time.monotonic()
            if (self.previous is not None and value['phase'] == self.previous['phase']
                    and value.get('estimated') == self.previous.get('estimated')
                    and value['percent'] not in (None, 0, 100) and self.last_sent is not None
                    and now - self.last_sent < 0.1):
                return
            self.previous, self.last_sent = value, now
            event = dict(self.context, item_progress=value, phase=value['phase'], progress_only=True)
            event['description'] = value['phase'].replace('_', ' ').capitalize() + ': ' + value['label']
            try:
                if callable(self.callback):
                    self.callback(event)
            except Exception:
                pass  # A display callback cannot abandon an active installer.
            if state.get('item_terminal') is True and value['phase'] == 'install' and value['percent'] == 100:
                self.closed = True

    def close(self):
        with self.lock:
            self.closed = True
