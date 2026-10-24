"""Opt-in exact Chocolatey updates using the existing private installer lane.

Only an already installed ordinary Chocolatey executable is used. This adapter
does not install package managers, edit sources, accept unknown packages, or
upgrade language/project environments. Catalog claims and installed footprints
are kept separate from verified native completion.
"""
from __future__ import annotations

import copy
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import time
import uuid

from .filesystem import _ordinary_data_path

SOURCE = 'https://community.chocolatey.org/api/v2/'
DEFAULTS = {'enabled': False, 'ids': [], 'agreements_accepted': False}
_ID = re.compile(r'^[A-Za-z0-9][A-Za-z0-9_.-]{0,99}$')
_VERSION = re.compile(r'^[0-9][A-Za-z0-9_.+\-]{0,99}$')
_RESERVED_IDS = frozenset(('all', 'chocolatey'))


def validate_configuration(value):
    if not isinstance(value, dict) or set(value) - set(DEFAULTS):
        raise ValueError('Unsupported additional-updater settings.')
    result = dict(DEFAULTS, **copy.deepcopy(value))
    if any(type(result[key]) is not bool for key in ('enabled', 'agreements_accepted')):
        raise ValueError('Additional updater choices must be explicitly on or off.')
    ids = result['ids']
    if type(ids) is not list or len(ids) > 100 or any(type(item) is not str or not _ID.fullmatch(item) or item.casefold() in _RESERVED_IDS for item in ids) or len(set(item.casefold() for item in ids)) != len(ids):
        raise ValueError('Choose at most 100 unique exact Chocolatey package IDs.')
    if result['enabled'] and (not ids or not result['agreements_accepted']):
        raise ValueError('Choose exact Chocolatey packages and approve their provider agreements before enabling updates.')
    return result


def parse_outdated(text):
    if not isinstance(text, str) or len(text) > 1024**2:
        raise ValueError('The additional update inventory exceeds its bound.')
    rows, seen = [], set()
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        parts = line.split('|')
        if len(parts) != 4 or not _ID.fullmatch(parts[0]) or any(not _VERSION.fullmatch(item) for item in parts[1:3]) or parts[3].casefold() not in ('true', 'false'):
            raise ValueError('Chocolatey returned an incomplete or unfamiliar update inventory.')
        key = parts[0].casefold()
        if key in seen or len(rows) >= 2000:
            raise ValueError('Chocolatey returned duplicate or excessive package identities.')
        seen.add(key)
        rows.append({'id': parts[0], 'installed_version': parts[1], 'available_version': parts[2],
                     'pinned': parts[3].casefold() == 'true', 'source': SOURCE})
    return rows


def _version_newer(current, offered):
    # Unknown version ordering is a skipped offer, never permission to downgrade.
    if not re.fullmatch(r'\d+(?:\.\d+){0,7}', current) or not re.fullmatch(r'\d+(?:\.\d+){0,7}', offered):
        return False
    first, last = [int(i) for i in current.split('.')], [int(i) for i in offered.split('.')]
    size = max(len(first), len(last))
    return first + [0] * (size - len(first)) < last + [0] * (size - len(last))


def _ordinary_executable(path):
    path = Path(path)
    info = _ordinary_data_path(path, file=True)
    if info is None or info.st_size > 64 * 1024**2:
        raise ValueError('An ordinary existing Chocolatey executable is required.')
    identity = lambda entry: (entry.st_dev, entry.st_ino, entry.st_size, entry.st_mtime_ns)
    with path.open('rb') as stream:
        if identity(os.fstat(stream.fileno())) != identity(info):
            raise ValueError('The package manager changed before opening.')
        raw = stream.read(64 * 1024**2 + 1)
        current = _ordinary_data_path(path, file=True)
        if (len(raw) > 64 * 1024**2 or current is None or identity(current) != identity(info)
                or identity(os.fstat(stream.fileno())) != identity(info)):
            raise ValueError('The package manager changed during identity verification.')
    return hashlib.sha256(raw).hexdigest()


def _discover():
    if os.name != 'nt':
        return None
    from .retention_cleanup import _known_folder
    path = _known_folder('62AB5D82-FDC1-4DC3-A9DD-070D1D495D97') / 'chocolatey/bin/choco.exe'
    if not path.exists():
        return None
    _ordinary_executable(path)
    return path


def _read_command(arguments):
    completed = subprocess.run(arguments, capture_output=True, text=True, errors='replace', timeout=60,
                               creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0), check=False)
    # The provider optionally reports outdated packages / an empty local list
    # with enhanced exit 2. These remain strict parsed read-only responses.
    accepted = (0, 2) if len(arguments) > 1 and arguments[1] in ('outdated', 'list') else (0,)
    if len(completed.stdout) > 1024**2 or len(completed.stderr) > 65536 or completed.returncode not in accepted:
        raise RuntimeError('The Chocolatey read-only query failed or exceeded its output bound.')
    return completed.stdout


def _installed_version(text, identifier):
    if not isinstance(text, str) or len(text) > 1024**2:
        raise ValueError('The installed package inventory exceeds its bound.')
    seen, version = set(), None
    for line in text.splitlines():
        if not line.strip():
            continue
        parts = line.strip().split('|')
        if len(parts) != 2 or not _ID.fullmatch(parts[0]) or not _VERSION.fullmatch(parts[1]):
            raise ValueError('The installed package inventory has an unfamiliar row.')
        identity = parts[0].casefold()
        if identity in seen or len(seen) >= 2000:
            raise ValueError('The installed package inventory has duplicate or excessive identities.')
        seen.add(identity)
        if identity == identifier.casefold():
            version = parts[1]
    return version


def _native_receipt(native, arguments):
    receipt = getattr(native, 'console_receipt', None)
    code = getattr(native, 'returncode', None)
    if (getattr(native, 'capture_complete', False) is not True or getattr(native, 'args', None) != arguments
            or type(code) is not int or not 0 <= code <= 0xffffffff or not isinstance(receipt, dict)
            or type(receipt.get('schema')) is not int or receipt['schema'] != 1
            or any(receipt.get(key) is not True for key in ('verified', 'quiescent', 'job_verified', 'output_eof'))
            or any(type(receipt.get(key)) is not int or receipt[key] != expected for key, expected in
                   (('job_active_processes', 0), ('console_client_count', 1), ('supervisor_exit_code', 0), ('target_exit_code', code)))):
        raise RuntimeError('The additional provider did not establish an exact complete quiescent native result.')
    return code, copy.deepcopy(receipt)


def _ownership_note(preferences):
    """No dual-provider mapping is established by a package name guess."""
    if preferences.get('software_updates_enabled') is False:
        return None
    if preferences.get('software_updates_enabled') is True and preferences.get('software_update_policy') == 'review':
        from .software_app_rules import app_rules_summary
        try:
            if app_rules_summary(preferences)['automatic_actions'] is False:
                return None
        except ValueError:
            pass  # Unknown saved policy cannot establish non-overlapping ownership.
    return ('Choose one automatic updater per application. WinGet automatic ownership overlaps or is unverified; '
            'use WinGet review-only with no automatic app rules, or turn its Software Updater off before enabling Chocolatey updates. '
            'Optimus does not guess cross-provider identities or edit existing exclusions.')


class AdditionalPackages:
    def __init__(self, backend, *, discover=_discover, reader=_read_command, installer=None):
        self.backend, self.data_dir = backend, Path(backend.data_dir)
        self.discover, self.reader = discover, reader
        if installer is None:
            from .winget_console import run_winget_console
            installer = run_winget_console
        self.installer = installer

    def scan(self):
        executable = self.discover()
        if executable is None:
            return {'schema': 1, 'read_only': True, 'available': False, 'complete': False,
                    'items': [], 'source': SOURCE,
                    'note': 'An existing standard Chocolatey installation was not found. Optimus does not install a package manager.'}
        digest = _ordinary_executable(executable)
        version = self.reader([str(executable), '--version']).strip()
        if not re.fullmatch(r'\d+\.\d+(?:\.\d+){0,2}', version) or int(version.split('.')[0]) < 2:
            raise ValueError('This adapter requires a recognized stable Chocolatey 2 or newer.')
        rows = parse_outdated(self.reader([str(executable), 'outdated', '--limit-output', '--no-progress', '--source=' + SOURCE]))
        if _ordinary_executable(executable) != digest:
            raise ValueError('The package manager changed during inventory.')
        return {'schema': 1, 'read_only': True, 'available': True, 'complete': True,
                'executable': str(executable), 'executable_sha256': digest, 'provider_version': version,
                'source': SOURCE, 'items': rows, 'checked_at': time.time(),
                'note': 'Only this provider reports these package identities. Pinned or unrecognized version ordering stays excluded.'}

    def run(self, configuration, *, lease, progress_callback=None, cancel_event=None):
        from .automation import RunLease
        from .parallel_updates import pending_updates_barrier
        from .speed_enhancements import _current_owner, _private_json
        from .winget_console import ConsolePreflightError
        config = validate_configuration(configuration)
        if not config['enabled']:
            return {'skipped': True, 'items': [], 'installer_started': False, 'failed_items': 0}
        if not isinstance(lease, RunLease) or lease.handle is None or lease.handle.closed or lease.path != self.data_dir / 'maintenance-run.lock':
            raise ValueError('The existing maintenance lease is required for additional provider updates.')
        def fresh_config():
            preferences = self.backend.preferences()
            toolkit = preferences.get('toolkit_settings', {})
            if not isinstance(toolkit, dict):
                raise ValueError('Saved additional-provider settings are invalid.')
            return validate_configuration(toolkit.get('providers', {}))
        if fresh_config() != config:
            raise ValueError('Saved additional-provider choices changed.')
        ownership = _ownership_note(self.backend.preferences())
        if ownership:
            return {'skipped': True, 'deferred': True, 'items': [], 'installer_started': False,
                    'failed_items': 0, 'outcomeKnown': True, 'running': False, 'note': ownership}
        barrier = pending_updates_barrier(self.backend)
        if barrier:
            return {'skipped': True, 'installer_started': False, 'failed_items': 0, 'note': barrier}
        scan = self.scan()
        if not scan['available']:
            return dict(scan, skipped=True, failed_items=0)
        rows, selected = [], {item.casefold() for item in config['ids']}
        result = {'schema': 1, 'items': rows, 'installer_started': False, 'failed_items': 0, 'restartNeeded': False,
                  'outcomeKnown': True, 'running': False, 'provider': 'chocolatey', 'source': SOURCE}
        approval_hash = hashlib.sha256(json.dumps(config, sort_keys=True, separators=(',', ':')).encode('utf-8')).hexdigest()
        for offer in scan['items']:
            if offer['id'].casefold() not in selected:
                continue
            ownership = _ownership_note(self.backend.preferences())
            if ownership:
                rows.append(dict(offer, status='deferred', reason=ownership))
                break
            if cancel_event is not None and cancel_event.is_set():
                result['cancelled'] = True
                break
            readiness = getattr(self.backend, 'maintenance_readiness', None)
            if callable(readiness):
                assessment = readiness(network=True)
                if not assessment['allowed']:
                    rows.append(dict(offer, status='deferred', reason=' '.join(assessment['reasons'])))
                    continue
            if offer['pinned'] or not _version_newer(offer['installed_version'], offer['available_version']):
                rows.append(dict(offer, status='skipped', reason='Pinned or version ordering cannot establish a newer stable release.'))
                continue
            try:
                current = self.scan()
            except Exception as error:
                rows.append(dict(offer, status='skipped', reason='A fresh read-only inventory could not verify this offer: ' + type(error).__name__))
                continue
            if (fresh_config() != config or current.get('executable') != scan['executable']
                    or current.get('provider_version') != scan['provider_version']
                    or current.get('executable_sha256') != scan['executable_sha256']
                    or [row for row in current.get('items', []) if row['id'].casefold() == offer['id'].casefold()] != [offer]):
                rows.append(dict(offer, status='skipped', reason='Package identity, version or saved choices changed.'))
                continue
            arguments = [scan['executable'], 'upgrade', offer['id'], '--version=' + offer['available_version'], '--yes',
                         '--limit-output', '--no-progress', '--skip-if-not-installed', '--fail-on-not-installed',
                         '--fail-on-unfound', '--require-checksums', '--ignore-remembered-arguments', '--ignore-dependencies', '--exclude-prerelease',
                         '--use-package-exit-codes', '--exit-when-reboot-detected', '--source=' + SOURCE]
            barrier = pending_updates_barrier(self.backend)
            if barrier:
                rows.append(dict(offer, status='deferred', reason=barrier))
                result.update(outcomeKnown=False, uncertain=True)
                break
            identifier = uuid.uuid4().hex
            path = self.data_dir / 'parallel-updates' / (identifier + '.json')
            _ordinary_data_path(path.parent)
            record = {'schema': 1, 'id': identifier, 'parent_run_id': identifier, 'owner': _current_owner(),
                      'title': 'Update approved Chocolatey packages', 'status': 'running', 'installer_prepared': True,
                      'started_at': time.time(), 'current_item': dict(offer), 'provider': 'chocolatey',
                      'approval_sha256': approval_hash, 'executable_sha256': scan['executable_sha256'],
                      'provider_version': scan['provider_version'], 'arguments': arguments,
                      'argv_sha256': hashlib.sha256(json.dumps(arguments, ensure_ascii=True, separators=(',', ':')).encode('ascii')).hexdigest()}
            _private_json(path, record)
            row = dict(offer, status='needs_review', uncertain=True)
            try:
                try:
                    if cancel_event is not None and cancel_event.is_set():
                        raise ValueError('Cancelled before the additional package installer started.')
                    if (fresh_config() != config or _ownership_note(self.backend.preferences())
                            or _ordinary_executable(Path(scan['executable'])) != scan['executable_sha256']):
                        raise ValueError('Saved choices or the package manager changed before installation.')
                except Exception as error:
                    raise ConsolePreflightError(str(error)[:500]) from error
                result['installer_started'] = True
                native = self.installer(arguments, timeout=1800, progress_callback=progress_callback or (lambda event: None))
                code, receipt = _native_receipt(native, arguments)
                restart = code in (1641, 3010, 350, 1604)
                if code == 0:
                    # Native completion remains known even if this separate
                    # post-install read fails. It must not block the next app.
                    verification_error = None
                    try:
                        if _ordinary_executable(Path(scan['executable'])) != scan['executable_sha256']:
                            raise ValueError('The provider identity changed before version verification.')
                        installed = self.reader([scan['executable'], 'list', '--limit-output', '--no-progress'])
                        verified = _installed_version(installed, offer['id']) == offer['available_version']
                    except Exception as error:
                        verified = False
                        verification_error = type(error).__name__ + ': ' + str(error)[:300]
                    row.update(status='completed' if verified else 'needs_review', uncertain=False, verified=verified,
                               result={'exitCode': code, 'outcomeKnown': True, 'running': False, 'restartNeeded': False,
                                       'installer_started': True, 'verified': verified, 'console_receipt': receipt})
                    if verification_error:
                        row['reason'] = 'Native completion was verified; the separate provider-version check was unavailable: ' + verification_error
                else:
                    row.update(status='restart_required' if restart else 'failed', uncertain=False,
                               result={'exitCode': code, 'outcomeKnown': True, 'running': False, 'restartNeeded': restart,
                                       'installer_started': True, 'console_receipt': receipt})
                record.update(status='needs_review' if restart else 'completed', finished_at=time.time(), result={'items': [row], 'outcomeKnown': True,
                              'running': False, 'restartNeeded': restart})
                result['restartNeeded'] = restart
            except ConsolePreflightError as error:
                result['installer_started'] = any(item.get('result', {}).get('installer_started') is True for item in rows)
                row.update(status='failed', uncertain=False, reason=str(error),
                           result={'outcomeKnown': True, 'running': False, 'installer_started': False})
                record.update(status='completed', installer_prepared=False, result={'items': [row], 'outcomeKnown': True, 'running': False})
            except Exception as error:
                row.update(reason=type(error).__name__ + ': ' + str(error)[:500])
                record.update(status='uncertain', result={'items': [row], 'outcomeKnown': False, 'uncertain': True})
                result.update(outcomeKnown=False, uncertain=True)
            _private_json(path, record)
            rows.append(row)
            result['failed_items'] += row['status'] in ('failed', 'needs_review')
            if result.get('uncertain') or result['restartNeeded']:
                break
        result['note'] = 'Individual known failures were retained while eligible packages continued. Active or unknown native outcomes retain the shared installer gate.'
        return result
