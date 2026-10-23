"""Owner-scoped exact-offer holds for unattended Windows software updates.

This reader never writes a hold, invokes an updater or changes manual approval.
An untrusted hold file fails closed before any package installer can start.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import stat

from .filesystem import _ordinary_data_path

_PACKAGE = re.compile(r'[A-Za-z0-9][A-Za-z0-9_.+\-]{1,127}\Z')
_VERSION = re.compile(r'[vV]?[0-9][0-9A-Za-z._+~\-]{0,127}\Z')
_OWNER = re.compile(r'(?:sid:S-1-[0-9]+(?:-[0-9]+){1,16}|uid:[0-9]+)\Z')
_MAX_BYTES = 64 * 1024


class UpdateHoldError(ValueError):
    installer_started = False
    outcomeKnown = True
    running = False


def _require(value):
    if not value:
        raise UpdateHoldError('Automatic update holds are unreadable or invalid; review update-holds.json before unattended installation. No installer was started.')


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        _require(key not in result)
        result[key] = value
    return result


def load_update_holds(data_dir, *, owner=None):
    """Return validated exact-version holds; a missing ordinary file means none."""
    path = Path(data_dir) / 'update-holds.json'
    try:
        _require(path.is_absolute())
        before = _ordinary_data_path(path, file=True)
        if before is None:
            return []
        _require(before.st_size <= _MAX_BYTES)
        if os.name != 'nt':
            _require(before.st_uid == os.getuid() and not before.st_mode & 0o077)
        with path.open('rb') as stream:
            opened = os.fstat(stream.fileno())
            _require(stat.S_ISREG(opened.st_mode) and opened.st_nlink == 1
                     and (before.st_dev, before.st_ino) == (opened.st_dev, opened.st_ino))
            raw = stream.read(_MAX_BYTES + 1)
        after = _ordinary_data_path(path, file=True)
        _require(after is not None and len(raw) <= _MAX_BYTES
                 and (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
                 == (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns))
        def invalid_constant(value):
            raise UpdateHoldError('Non-finite automatic update hold data is invalid.')
        value = json.loads(raw.decode('utf-8-sig'), object_pairs_hook=_pairs, parse_constant=invalid_constant)
        _require(isinstance(value, dict) and set(value) == {'schema', 'owner', 'holds'}
                 and type(value.get('schema')) is int and value['schema'] == 1)
        if owner is None:
            from .speed_enhancements import _current_owner
            owner = _current_owner()
        _require(isinstance(owner, str) and _OWNER.fullmatch(owner)
                 and value.get('owner') == owner)
        rows = value.get('holds')
        _require(isinstance(rows, list) and len(rows) <= 100)
        seen, result = set(), []
        for row in rows:
            _require(isinstance(row, dict) and set(row) == {'id', 'available_version', 'reason'})
            identity, version, reason = row.get('id'), row.get('available_version'), row.get('reason')
            _require(isinstance(identity, str) and _PACKAGE.fullmatch(identity)
                     and not identity.endswith('.') and '..' not in identity)
            _require(isinstance(version, str) and _VERSION.fullmatch(version)
                     and not version.endswith('.') and '..' not in version)
            _require(isinstance(reason, str) and 0 < len(reason) <= 1000 and reason.strip() == reason
                     and all(ord(char) >= 32 and ord(char) != 127 for char in reason))
            binding = (identity.casefold(), version)
            _require(binding not in seen)
            seen.add(binding)
            result.append(dict(row))
        return result
    except UpdateHoldError:
        raise
    except (OSError, ValueError, TypeError, RecursionError) as error:
        raise UpdateHoldError('Automatic update holds could not be verified; no unattended installer was started.') from error


def matching_update_hold(package, holds):
    identity, available = package.get('id'), package.get('available')
    # Import at call time: the software runner imports this hold reader. A v
    # prefix, trailing zero or build label cannot turn the same bad release
    # into a new automatic authorization; opaque versions remain exact-only.
    from .automated_software import same_release_version
    return next((row for row in holds if isinstance(identity, str)
                 and row['id'].casefold() == identity.casefold()
                 and (row['available_version'] == available
                      or same_release_version(row['available_version'], available))), None)


def held_update_result(package, hold, *, recheck=None):
    result = {'id': package['id'], 'package_id': package['id'], 'source': 'winget',
            'status': 'skipped', 'skipped': True, 'exitCode': None, 'unattended': True,
            'failureKind': 'automatic_update_hold', 'requiresReview': True,
            'installer_started': False, 'outcomeKnown': True, 'running': False,
            'from_version': package['version'], 'to_version': package['available'],
            'note': 'Automatic update held: ' + hold['reason']}
    if isinstance(recheck, dict):
        result['automatic_recheck'] = dict(recheck)
        result['note'] += ' Publisher replacement awaited; automatic checks continue.'
    return result


def is_known_update_hold(outcome):
    """Only this exact known preflight refusal may continue the app batch."""
    return (isinstance(outcome, dict) and outcome.get('failureKind') == 'automatic_update_hold'
            and outcome.get('status') == 'skipped' and outcome.get('skipped') is True and outcome.get('requiresReview') is True
            and outcome.get('installer_started') is False and outcome.get('outcomeKnown') is True
            and outcome.get('running') is False and outcome.get('restartNeeded') is not True
            and outcome.get('reboot_required') is not True
            and outcome.get('cancelled') is not True and outcome.get('uncertain') is not True
            and outcome.get('exitCode') is None)
