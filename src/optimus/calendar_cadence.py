"""Calendar-month cadence without assuming every month contains thirty days."""
from __future__ import annotations

import calendar
from datetime import datetime, tzinfo
import math
import time


def _epoch(value):
    if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
        raise ValueError('Use a finite nonnegative epoch timestamp.')
    return float(value)


def next_month_timestamp(attempted_at, *, zone: tzinfo | None = None):
    """Same local wall time next month, clamped to its final calendar day.

    With no explicit zone, Python uses the computer's local timezone rules when
    converting each date. Passing a ZoneInfo object retains its DST rules.
    Cadence is based on the previous attempt, including failed attempts.
    """
    try:
        previous = datetime.fromtimestamp(_epoch(attempted_at), zone)
        year, month = previous.year + (previous.month == 12), previous.month % 12 + 1
        day = min(previous.day, calendar.monthrange(year, month)[1])
        return previous.replace(year=year, month=month, day=day, fold=0).timestamp()
    except (OverflowError, OSError, TypeError) as error:
        raise ValueError('The monthly cadence timestamp or timezone is unsupported.') from error


def calendar_month_due(attempted_at, now=None, *, zone: tzinfo | None = None):
    current = _epoch(time.time() if now is None else now)
    previous = _epoch(attempted_at)
    return current >= previous and current >= next_month_timestamp(previous, zone=zone)
