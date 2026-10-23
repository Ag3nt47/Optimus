"""Bounded catalog checks for exact held offers; never an install authorization.

WinGet show is submitted catalog metadata, not proof of a corrected executable:
https://learn.microsoft.com/windows/package-manager/winget/show
Refreshing an existing source does not require resetting its trust or agreements:
https://learn.microsoft.com/windows/package-manager/winget/source
"""
from __future__ import annotations

import hashlib
import json
import math
import os
from pathlib import Path
import re
import stat
import subprocess
import time
from urllib.parse import urlsplit

from .filesystem import _ordinary_data_path
from .speed_enhancements import _current_owner, _private_json
from .update_holds import load_update_holds, matching_update_hold

_MESSAGE = 'Publisher replacement awaited; automatic checks continue.'
_SOURCE = {'Name': 'winget', 'Arg': 'https://cdn.winget.microsoft.com/cache',
           'Type': 'Microsoft.PreIndexed.Package', 'Identifier': 'Microsoft.Winget.Source_8wekyb3d8bbwe',
           'Data': 'Microsoft.Winget.Source_8wekyb3d8bbwe'}
_MAX_BYTES = 131072


def _pairs(values):
    result = {}
    for key, value in values:
        if key in result:
            raise ValueError('Repeated metadata key.')
        result[key] = value
    return result


def _json(raw):
    def reject(value):
        raise ValueError('Non-finite metadata.')
    return json.loads(raw, object_pairs_hook=_pairs, parse_constant=reject)


def _verified_source(raw):
    value = _json(raw)
    # source export has used both a single source object and a Sources envelope.
    if isinstance(value, dict) and set(value) == {'Sources'}:
        if not isinstance(value['Sources'], list) or len(value['Sources']) != 1:
            raise ValueError('The named source is not unique.')
        value = value['Sources'][0]
    if (not isinstance(value, dict) or any(value.get(key) != expected for key, expected in _SOURCE.items())
            or type(value.get('Explicit')) is not bool or not isinstance(value.get('TrustLevel'), list)
            or set(value['TrustLevel']) != {'Trusted', 'StoreOrigin'} or len(value['TrustLevel']) != 2):
        raise ValueError('The existing source is not the verified official WinGet source.')
    return dict(_SOURCE, Explicit=value['Explicit'], TrustLevel=['Trusted', 'StoreOrigin'])


def _metadata(raw, package):
    lines = raw.replace('\r', '').splitlines()
    found = re.findall(r'^Found .+ \[([^\]\r\n]+)\]$', raw.replace('\r', ''), re.M)
    if len(found) != 1 or found[0] != package['id']:
        raise ValueError('The exact metadata identity was not recognized.')
    def one(label):
        values = [line.strip()[len(label):].strip() for line in lines if line.strip().startswith(label)]
        if len(values) != 1 or not values[0] or len(values[0]) > 4096:
            raise ValueError('The metadata field was missing or ambiguous.')
        return values[0]
    version, digest, url = one('Version:'), one('Installer SHA256:'), one('Installer Url:')
    if version != package['available'] or re.fullmatch(r'[a-fA-F0-9]{64}', digest) is None:
        raise ValueError('The exact metadata version or installer hash was not recognized.')
    location = urlsplit(url)
    if (location.scheme != 'https' or not location.hostname or location.username is not None
            or location.password is not None or location.fragment or any(ord(c) < 33 for c in url)):
        raise ValueError('The installer metadata URL is not an ordinary HTTPS location.')
    # Store only a digest of the public URL; catalog text is never executable.
    value = {'id': package['id'], 'available_version': version, 'source': 'winget',
             'installer_sha256': digest.lower(), 'url_sha256': hashlib.sha256(url.encode()).hexdigest()}
    value['metadata_sha256'] = hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
    return value


def _native_command(executable, arguments, *, timeout, cancel_event=None):
    """Terminate/drain only this owned catalog child, never a package installer."""
    command = [executable, *arguments]
    child = subprocess.Popen(command, shell=False, stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding='utf-8', errors='replace',
        creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    deadline, stopping, force_at = time.monotonic() + timeout, False, None
    while True:
        try:
            output, errors = child.communicate(timeout=0.1)
            if len(output or '') > _MAX_BYTES or len(errors or '') > _MAX_BYTES:
                raise ValueError('Catalog metadata exceeded its bounded result size.')
            return subprocess.CompletedProcess(command, 130 if stopping else child.returncode, output, errors)
        except subprocess.TimeoutExpired:
            if (cancel_event is not None and cancel_event.is_set()) or time.monotonic() >= deadline:
                if not stopping:
                    stopping, force_at = True, time.monotonic() + 2
                    try: child.terminate()
                    except OSError: pass
                elif force_at is not None and time.monotonic() >= force_at:
                    try: child.kill()
                    except OSError: pass
                    force_at = None
                # communicate must establish that our read-only child exited.


class HeldUpdateRechecks:
    """Owner-scoped cooldown, at most five offers and 45 seconds per invocation."""
    def __init__(self, maintenance, *, owner=None, runner=None, clock=None, monotonic=None,
                 cooldown=21600, budget=45):
        self.maintenance = maintenance
        self.path = Path(maintenance.data_dir) / 'update-hold-rechecks.json'
        self.owner = owner
        self.runner = runner or _native_command
        self.clock, self.monotonic = clock or time.time, monotonic or time.monotonic
        self.cooldown, self.budget = cooldown, budget

    def _read(self, owner):
        if not self.path.is_absolute():
            raise ValueError('Catalog rechecks require an absolute owned data path.')
        before = _ordinary_data_path(self.path, file=True)
        if before is None:
            return {'schema': 1, 'owner': owner, 'offers': {}, 'catalog_uncertain': False}
        if before.st_size > _MAX_BYTES or os.name != 'nt' and (before.st_uid != os.getuid() or before.st_mode & 0o077):
            raise ValueError('The catalog recheck receipt is not private or bounded.')
        with self.path.open('rb') as stream:
            opened = os.fstat(stream.fileno())
            if not stat.S_ISREG(opened.st_mode) or opened.st_nlink != 1 or (before.st_dev, before.st_ino) != (opened.st_dev, opened.st_ino):
                raise ValueError('The catalog recheck receipt changed.')
            raw = stream.read(_MAX_BYTES + 1)
        after = _ordinary_data_path(self.path, file=True)
        fields = lambda value: (value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns)
        if after is None or fields(before) != fields(after) or len(raw) > _MAX_BYTES:
            raise ValueError('The catalog recheck receipt changed.')
        value = _json(raw.decode('utf-8-sig'))
        if (not isinstance(value, dict) or set(value) != {'schema', 'owner', 'offers', 'catalog_uncertain'}
                or type(value['schema']) is not int or value['schema'] != 1 or value['owner'] != owner
                or type(value['catalog_uncertain']) is not bool
                or not isinstance(value['offers'], dict) or len(value['offers']) > 100):
            raise ValueError('The catalog recheck receipt is invalid.')
        for key, row in value['offers'].items():
            if (re.fullmatch(r'[a-f0-9]{64}', key) is None or not isinstance(row, dict)
                    or type(row.get('attempted_at')) not in (int, float) or not math.isfinite(row['attempted_at'])
                    or row['attempted_at'] < 0 or row['attempted_at'] > self.clock()
                    or any(re.fullmatch(r'[a-f0-9]{64}', row[field]) is None
                           for field in ('metadata_sha256', 'installer_sha256') if field in row)):
                raise ValueError('The catalog recheck receipt contains invalid evidence.')
        return value

    def _write(self, state):
        _ordinary_data_path(self.path, file=True)
        _private_json(self.path, state)

    def run(self, packages, *, cancel_event=None, item_callback=None):
        result = {'items': [], 'refresh_attempted': False, 'source_refreshed': False,
                  'needs_fresh_scan': False, 'source_verified_after_refresh': False,
                  'authorization_granted': False}
        if not isinstance(packages, list) or len(packages) > 100:
            return dict(result, error='The exact held-offer selection was not bounded.')
        owner = self.owner or _current_owner()
        holds = load_update_holds(self.maintenance.data_dir, owner=owner)
        selected, seen = [], set()
        for package in packages:
            if not isinstance(package, dict) or package.get('source') != 'winget' or matching_update_hold(package, holds) is None:
                continue
            key = hashlib.sha256((package['id'].casefold() + '\n' + package['available']).encode()).hexdigest()
            if key not in seen:
                selected.append((key, package))
                seen.add(key)
        try:
            state = self._read(owner)
        except (OSError, ValueError, TypeError, RecursionError):
            return dict(result, needs_fresh_scan=True,
                        error='Automatic catalog rechecks require a valid private receipt; the exact update hold is preserved.')
        result['needs_fresh_scan'] = state['catalog_uncertain']
        if not selected and not state['catalog_uncertain']:
            return result
        now, pending = self.clock(), []
        for key, package in selected:
            previous = state['offers'].get(key, {})
            entry = {'id': package['id'], 'available_version': package['available'],
                'held_available_version': matching_update_hold(package, holds)['available_version'],
                'status': 'awaiting_publisher', 'automatic_recheck': True, 'authorization_granted': False,
                'recheck_schedule': 'next_approved_run_after_cooldown',
                'source_verified': False, 'source_refreshed': False, 'installer_started': False,
                'outcomeKnown': True, 'running': False, 'note': _MESSAGE}
            result['items'].append(entry)
            if now - previous.get('attempted_at', -self.cooldown) < self.cooldown:
                entry.update(recheck_status='cooldown', last_attempt_at=previous['attempted_at'],
                             next_check_at=previous['attempted_at'] + self.cooldown)
            else:
                pending.append((key, package, previous, entry))
        pending.sort(key=lambda row: row[2].get('attempted_at', -1))
        for row in pending[5:]:
            row[3]['recheck_status'] = 'per_run_limit'
        pending = pending[:5]
        if not pending and not state['catalog_uncertain']:
            return result
        deadline = self.monotonic() + self.budget
        def command(arguments, limit=15):
            if cancel_event is not None and cancel_event.is_set():
                raise ValueError('Catalog recheck cancelled before a new command.')
            remaining = deadline - self.monotonic()
            if remaining <= 0:
                raise ValueError('The catalog recheck time budget was reached.')
            if item_callback is not None:
                description = ('Recheck held exact-version publisher metadata' if arguments[0] == 'show'
                               else 'Refresh the existing verified WinGet catalog' if arguments[1] == 'update'
                               else 'Verify the existing WinGet source identity and trust')
                item_callback({'phase': 'metadata_recheck', 'description': description})
            value = self.runner(self.maintenance._winget(), arguments,
                timeout=min(limit, remaining), cancel_event=cancel_event)
            if (type(value.returncode) is not int or value.returncode != 0 or (value.stderr or '').strip()
                    or not isinstance(value.stdout, str) or len(value.stdout) > _MAX_BYTES):
                raise ValueError('The source command failed or requires interaction.')
            return value.stdout
        try:
            if not pending:
                _verified_source(command(['source', 'export', '--name', 'winget', '--disable-interactivity']))
                result['source_verified_after_refresh'] = True
                state['catalog_uncertain'] = False
                self._write(state)
                return result
            # Persist attempts BEFORE network calls, so a failed/crashed launch
            # cannot cause an unbounded retry loop on the next optimization run.
            for key, package, previous, entry in pending:
                state['offers'][key] = dict(previous, attempted_at=now)
                entry.update(checked_at=now, recheck_status='checking', next_check_at=now + self.cooldown)
            if len(state['offers']) > 100:
                raise ValueError('The catalog recheck history is full.')
            # An explicitly observed bad source is a shared trust failure even
            # when no refresh has begun. Persist this before checking, so a
            # cooldown cannot let another app bypass the failed source check.
            state['catalog_uncertain'] = True
            result['needs_fresh_scan'] = True
            self._write(state)
            before = _verified_source(command(['source', 'export', '--name', 'winget', '--disable-interactivity']))
            for _, _, _, entry in pending:
                entry['source_verified'] = True
            result.update(refresh_attempted=True, needs_fresh_scan=True)
            state['catalog_uncertain'] = True
            self._write(state)
            command(['source', 'update', '--name', 'winget', '--disable-interactivity'], limit=20)
            after = _verified_source(command(['source', 'export', '--name', 'winget', '--disable-interactivity']))
            if after != before:
                raise ValueError('The existing source identity changed during refresh.')
            result.update(source_refreshed=True, source_verified_after_refresh=True)
            state['catalog_uncertain'] = False
            for key, package, previous, entry in pending:
                try:
                    if item_callback is not None:
                        item_callback({'id': package['id'], 'phase': 'metadata_recheck',
                            'description': 'Rechecking held publisher metadata: ' + package['id']})
                    metadata = _metadata(command(['show', '--id', package['id'], '--exact', '--source', 'winget',
                        '--version', package['available'], '--disable-interactivity']), package)
                    entry.update(metadata, source_refreshed=True, recheck_status='metadata_checked',
                        metadata_changed=(metadata['metadata_sha256'] != previous['metadata_sha256'])
                        if 'metadata_sha256' in previous else None,
                        artifact_verified=False, source_refreshed_at=self.clock())
                    state['offers'][key].update(metadata)
                except (OSError, ValueError, TypeError, subprocess.SubprocessError):
                    entry.update(recheck_status='metadata_unavailable', source_refreshed=True,
                                 error='The exact-version catalog metadata could not be verified; the hold remains active.')
            self._write(state)
        except (OSError, ValueError, TypeError, subprocess.SubprocessError):
            for _, _, _, entry in pending:
                if entry['recheck_status'] == 'checking':
                    entry.update(recheck_status='source_unavailable', error='The existing source could not be refreshed and verified; the hold remains active.')
            if result['refresh_attempted'] and not result['source_verified_after_refresh']:
                try:
                    after = _verified_source(command(['source', 'export', '--name', 'winget', '--disable-interactivity']))
                    result['source_verified_after_refresh'] = after == before
                    if result['source_verified_after_refresh']:
                        state['catalog_uncertain'] = False
                        self._write(state)
                except (OSError, ValueError, TypeError, subprocess.SubprocessError):
                    pass
        return result
