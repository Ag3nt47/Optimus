"""Validate bounded configuration readback before the final outcome summary."""
from __future__ import annotations

_STATES = {'confirmed', 'needs_review', 'unverified', 'not_applicable'}


def normalized_speed_checks(value):
    if (not isinstance(value, dict) or value.get('status') not in _STATES
            or not isinstance(value.get('checks'), list) or not 1 <= len(value['checks']) <= 32):
        raise ValueError('Invalid speed-configuration verification response.')
    result = []
    for item in value['checks']:
        if (not isinstance(item, dict) or item.get('status') not in _STATES
                or any(not isinstance(item.get(key), str) or not 0 < len(item[key]) <= 4096
                       for key in ('target', 'reason'))):
            raise ValueError('Invalid speed-configuration check.')
        result.append({key: item[key] for key in ('target', 'status', 'reason')})
    observed = {item['status'] for item in result}
    expected = ('needs_review' if 'needs_review' in observed else 'unverified' if 'unverified' in observed
                else 'confirmed' if 'confirmed' in observed else 'not_applicable')
    if value['status'] != expected:
        raise ValueError('The speed summary does not match its readback checks.')
    return result
