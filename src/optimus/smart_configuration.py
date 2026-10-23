"""Pure Smart Maintenance preference defaults and validation; no host access."""
from __future__ import annotations

import copy

SMART_DEFAULTS = {
    'smart_cleaning_enabled': False,
    'smart_cleaning_threshold_bytes': 500_000_000,
    'smart_cleaning_cooldown_seconds': 3600,
    'browser_exit_cleaning_enabled': False,
    'browser_exit_grace_seconds': 15,
    'smart_maintenance_poll_seconds': 60,
}
SMART_BOUNDS = {'smart_cleaning_threshold_bytes': (1_000_000, 1_000_000_000_000),
               'smart_cleaning_cooldown_seconds': (60, 604800),
               'browser_exit_grace_seconds': (1, 300),
               'smart_maintenance_poll_seconds': (10, 3600)}


def validate_smart_configuration(settings):
    if not isinstance(settings, dict):
        raise ValueError('Smart Maintenance choices must be an object.')
    result = {key: copy.deepcopy(settings.get(key, default)) for key, default in SMART_DEFAULTS.items()}
    for key in ('smart_cleaning_enabled', 'browser_exit_cleaning_enabled'):
        if type(result[key]) is not bool:
            raise ValueError(key + ' must be on or off.')
    for key, (minimum, maximum) in SMART_BOUNDS.items():
        if type(result[key]) is not int or not minimum <= result[key] <= maximum:
            raise ValueError(key + ' is outside its supported whole-number range.')
    return result
