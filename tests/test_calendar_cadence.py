from datetime import datetime, timezone, timedelta

import pytest

from optimus.calendar_cadence import calendar_month_due, next_month_timestamp


def stamp(text):
    return datetime.fromisoformat(text).replace(tzinfo=timezone.utc).timestamp()


@pytest.mark.parametrize(('start', 'expected'), [
    ('2025-01-31T12:30', '2025-02-28T12:30'),
    ('2024-01-31T12:30', '2024-02-29T12:30'),
    ('2024-12-31T12:30', '2025-01-31T12:30'),
    ('2024-02-29T12:30', '2024-03-29T12:30'),
])
def test_months_use_calendar_days(start, expected):
    deadline = next_month_timestamp(stamp(start), zone=timezone.utc)
    assert deadline == stamp(expected)
    assert not calendar_month_due(stamp(start), deadline - 1, zone=timezone.utc)
    assert calendar_month_due(stamp(start), deadline, zone=timezone.utc)


@pytest.mark.parametrize('invalid', [True, -1, float('nan'), float('inf'), None, '123'])
def test_invalid_attempts_never_authorize_cadence(invalid):
    with pytest.raises(ValueError):
        next_month_timestamp(invalid)


def test_rollback_clock_defers_and_explicit_zone_preserves_wall_time():
    previous = stamp('2026-10-22T08:11')
    assert not calendar_month_due(previous, previous - 1)
    zone = timezone(timedelta(hours=-7))
    expected = datetime(2026, 11, 22, 1, 11, tzinfo=zone).timestamp()
    assert next_month_timestamp(previous, zone=zone) == expected


def test_dst_month_preserves_wall_time_when_zone_database_is_available():
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
    try:
        zone = ZoneInfo('America/Denver')
    except ZoneInfoNotFoundError:
        pytest.skip('No optional IANA timezone database on this host.')
    previous = datetime(2026, 2, 15, 9, tzinfo=zone).timestamp()
    expected = datetime(2026, 3, 15, 9, tzinfo=zone).timestamp()
    assert next_month_timestamp(previous, zone=zone) == expected
    assert expected - previous == 28 * 86400 - 3600
