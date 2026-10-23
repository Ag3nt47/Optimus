"""Explicit saved approval for an exact unattended scope; no host access."""
from __future__ import annotations

import copy
import hashlib
import hmac
import json
import re

UNATTENDED_DEFAULTS = {'unattended_auto_clean_enabled': False,
                       'unattended_scope_hash': '', 'unattended_browser_policy': 'skip_open'}
UNATTENDED_SETUP_VERSION = 2
_EXCLUDED = frozenset(('unattended_scope_hash', 'reduce_motion', 'theme', 'motion',
                       'ui_theme', 'ui_motion_enabled'))


def validate_unattended_configuration(settings):
    if not isinstance(settings, dict):
        raise ValueError('Unattended choices must be an object.')
    result = {key: copy.deepcopy(settings.get(key, value)) for key, value in UNATTENDED_DEFAULTS.items()}
    if type(result['unattended_auto_clean_enabled']) is not bool:
        raise ValueError('Unattended Auto Clean must be explicitly on or off.')
    scope = result['unattended_scope_hash']
    if not isinstance(scope, str) or scope != '' and not re.fullmatch('[0-9a-f]{64}', scope):
        raise ValueError('The unattended approval identity is invalid; review and save the choices again.')
    if result['unattended_browser_policy'] not in ('skip_open', 'close_gracefully'):
        raise ValueError('Choose Skip open browsers or Close browsers gracefully for unattended cleanup.')
    # Disabling the option revokes its prior approval, including ordinary
    # preference patches. Re-enabling it cannot revive an old saved hash.
    if not result['unattended_auto_clean_enabled']:
        result['unattended_scope_hash'] = ''
    return result


def _scope_hash(settings):
    value = {key: value for key, value in settings.items() if key not in _EXCLUDED}
    try:
        encoded = json.dumps(value, sort_keys=True, separators=(',', ':'),
                             ensure_ascii=True, allow_nan=False).encode('ascii')
    except (TypeError, OverflowError, RecursionError) as error:
        raise ValueError('The unattended scope cannot be verified.') from error
    if len(encoded) > 4 * 1024 * 1024:
        raise ValueError('The unattended approval scope exceeds its supported limit.')
    return hashlib.sha256(encoded).hexdigest()


def approve_unattended_settings(settings):
    """Stamp already validated settings only after an explicit UI save.

    This helper cannot grant package/device agreements, choose file scopes or
    validate platform-specific paths. The caller must first run its normal
    Windows/native preference validator. Ordinary backend saves must not call
    this helper: changed scope then invalidates the existing hash.
    """
    configuration = validate_unattended_configuration(settings)
    if not configuration['unattended_auto_clean_enabled']:
        raise ValueError('Select unattended Auto Clean explicitly before approving its scope.')
    if type(settings.get('setup_version')) is not int or settings['setup_version'] != UNATTENDED_SETUP_VERSION:
        raise ValueError('Complete the current setup before approving unattended Auto Clean.')
    value = copy.deepcopy(settings)
    value.update(configuration)
    value.update(launch_at_sign_in=True, automation_mode='startup')
    value['unattended_scope_hash'] = _scope_hash(value)
    return value


def matches_unattended(settings):
    """Return False for missing, revoked, changed or unverifiable approvals."""
    try:
        configuration = validate_unattended_configuration(settings)
        return (configuration['unattended_auto_clean_enabled'] is True
            and type(settings.get('setup_version')) is int
            and settings['setup_version'] == UNATTENDED_SETUP_VERSION
            and settings.get('launch_at_sign_in') is True
            and settings.get('automation_mode') == 'startup'
            and bool(configuration['unattended_scope_hash'])
            and hmac.compare_digest(configuration['unattended_scope_hash'], _scope_hash(settings)))
    except (ValueError, TypeError, OverflowError, RecursionError):
        return False
