"""Opt-in, exact-offer Python relocation preparation; never an installer.

Only private AppUpdateVerification attempts for the current version pair can
identify the native 1326/448 failure. The approved mapping still has to pass the
native repair primitive's current path, byte-hash, security and probe checks.
Normal update revalidation, installer holds and version readback remain required.
"""
from __future__ import annotations

import copy
import hashlib
import itertools
import json
import math
import os
from pathlib import Path
import re
import sys
import time

from .app_update_verification import AppUpdateVerification, _read
from .automation import RunLease
from .filesystem import _ordinary_data_path
from .python_relocation import (
    ApprovedPythonRelocation, diagnose_python_relocation_failure,
    preflight_python_relocation, repair_python_relocation,
)

_PYTHON = re.compile(r'Python\.Python\.3\.[1-9][0-9]?\Z')
_ATTEMPT = re.compile(r'[0-9a-f]{32}\Z')
_CORE_LOG = re.compile(r'winget_[0-9]{3}_core_JustForMe\.log\Z', re.I)
_MAX_ATTEMPTS = 64
_MAX_LOGS = 64
_MAX_LOG = 4 * 1024**2


def _windows():
    return sys.platform == 'win32'


def _cancelled(event):
    return event is not None and event.is_set()


def _result(package_id, status, reason, *, proceed=False, **extra):
    return dict(schema=1, package_id=package_id, source='winget', status=status,
        reason=str(reason)[:2000], proceed=proceed, installer_started=False,
        outcomeKnown=True, running=False, failure_scope='package', **extra)


def _current_package(backend, package_id):
    maintenance = getattr(backend, 'maintenance', None)
    packages = getattr(maintenance, '_last_update_packages', None)
    current = packages.get(package_id) if isinstance(packages, dict) else None
    scan_time = getattr(maintenance, '_last_update_scan', None)
    identities = getattr(maintenance, '_last_update_ids', ())
    if (not isinstance(current, dict) or current.get('id') != package_id
            or current.get('source') != 'winget' or package_id not in identities
            or type(scan_time) not in (int, float) or not math.isfinite(scan_time)
            or not 0 < scan_time <= time.time() + 1 or time.time() - scan_time > 1200
            or any(not isinstance(current.get(key), str) or not current[key]
                or len(current[key]) > 256 for key in ('version', 'available'))):
        raise ValueError('The exact Python offer is absent from the current successful WinGet scope.')
    from .automated_software import is_newer_version
    if not is_newer_version(current['version'], current['available'],
            package_id=package_id, source='winget'):
        raise ValueError('The current Python offer has no provably newer version.')
    return {key: current[key] for key in ('id', 'source', 'version', 'available')}


def _require_lease(backend):
    owned = getattr(RunLease, 'owned_by_current_process', None)
    if not callable(owned) or owned(backend.data_dir) is not True:
        raise ValueError('The current process must hold the maintenance lease before Python relocation preparation.')


def _entries(directory, limit):
    _ordinary_data_path(directory)
    with os.scandir(directory) as entries:
        rows = [Path(entry.path) for entry in itertools.islice(entries, limit + 1)]
    if len(rows) > limit:
        raise ValueError('The owned Python diagnostic inventory exceeds its bound.')
    return rows


def _diagnoses(backend, package, cancel_event):
    service = AppUpdateVerification(backend.data_dir, package)
    _ordinary_data_path(service.base)
    if not service.base.exists():
        return []
    service._owner_anchor(service.base)
    _ordinary_data_path(service.directory)
    if not service.directory.exists():
        return []
    service._owner_anchor()
    attempts = [row for row in _entries(service.directory, 4096)
                if _ATTEMPT.fullmatch(row.name)]
    if len(attempts) > _MAX_ATTEMPTS:
        raise ValueError('Too many exact-offer Python diagnostic attempts require review.')
    records, log_count = [], 0
    for folder in attempts:
        if _cancelled(cancel_event):
            raise InterruptedError('Python relocation preparation was cancelled.')
        _ordinary_data_path(folder)
        path = folder / 'verification.json'
        if not path.exists():
            continue  # An unfinished attempt cannot authorize a repair.
        raw = _read(path, 65536)
        row = json.loads(raw.decode('utf-8'))
        if (not isinstance(row, dict) or type(row.get('schema')) is not int or row['schema'] != 1
                or row.get('owner') != service.owner or row.get('id') != package['id']
                or row.get('from_version') != package['version']
                or row.get('to_version') != package['available']
                or type(row.get('recorded_at')) not in (int, float)
                or not math.isfinite(row['recorded_at'])
                or not 0 < row['recorded_at'] <= time.time() + 1):
            raise ValueError('The owned Python attempt does not match this exact owner and offer.')
        # Let the existing activation/version checks resolve successful or
        # pending attempts. Old failure text must not override their evidence.
        if row.get('status') in ('verified', 'pending_activation', 'pending_app_close', 'pending_verification'):
            return []
        if row.get('status') != 'not_verified':
            continue
        logs = [value for value in _entries(folder, 256) if _CORE_LOG.fullmatch(value.name)]
        log_count += len(logs)
        if log_count > _MAX_LOGS:
            raise ValueError('Too many exact-offer Python native logs require review.')
        for log in logs:
            if _cancelled(cancel_event):
                raise InterruptedError('Python relocation preparation was cancelled.')
            digest = hashlib.sha256(_read(log, _MAX_LOG)).hexdigest()
            diagnosis = diagnose_python_relocation_failure(package['id'], log, owned_log_root=folder)
            if diagnosis is None:
                continue
            if (diagnosis.get('package_id') != package['id'] or diagnosis.get('error') != 1326
                    or diagnosis.get('win32_error') != 448 or diagnosis.get('log_sha256') != digest
                    or hashlib.sha256(_read(log, _MAX_LOG)).hexdigest() != digest):
                raise ValueError('The exact native Python diagnostic changed during inspection.')
            records.append({'attempt': folder.name, 'verification_sha256': hashlib.sha256(raw).hexdigest(),
                'log': log.name, **diagnosis})
    return records


def _approved_mapping(settings, package_id, diagnoses):
    values = settings.get('python_relocation_approvals', [])
    if not isinstance(values, list) or len(values) > 64:
        raise ValueError('The saved exact Python relocation approvals are invalid.')
    approvals = [ApprovedPythonRelocation.from_dict(value) for value in values]
    sources = {row['source'].casefold() for row in diagnoses}
    if len(sources) != 1:
        raise ValueError('The owned Python failure names ambiguous relocation paths.')
    matched = [row for row in approvals if row.package_id == package_id
               and row.source.casefold() in sources
               and all(value['dll'].casefold() == row.dll.casefold() for value in diagnoses)]
    if len(matched) != 1:
        raise ValueError('This exact Python relocation needs one explicit saved mapping approval.')
    return matched[0]


def _native_result(value):
    if (not isinstance(value, dict) or value.get('status') not in
            ('ready', 'repaired', 'already_trusted', 'review_required', 'failed', 'unsupported')
            or type(value.get('mutated')) is not bool
            or not isinstance(value.get('code'), str) or not value['code']
            or not isinstance(value.get('note'), str)):
        raise ValueError('The Python relocation check returned no supported terminal receipt.')
    status = value['status']
    if ((status in ('repaired', 'already_trusted') and
            (type(value.get('probe_error')) is not int or value['probe_error'] != 0))
            or (status == 'repaired' and value['mutated'] is not True)
            or (status == 'already_trusted' and value['mutated'] is not False)
            or (status == 'ready' and (value['mutated'] is not False
                or type(value.get('probe_error')) is not int or value['probe_error'] != 448))):
        raise ValueError('The Python relocation receipt has no matching enforced native probe.')
    encoded = json.dumps(value, allow_nan=False)
    if len(encoded) > 65536:
        raise ValueError('The Python relocation receipt exceeds its bound.')
    return copy.deepcopy(value)


def prepare_python_update(backend, package_id, cancel_event=None, progress_callback=None):
    """Prepare one approved mapping, or return a known package-only skip.

    None leaves the normal updater unchanged. A receipt with proceed=True still
    requires every normal update check and fresh installed-version verification;
    this function never starts an installer, edits holds, or changes preferences.
    """
    if not _windows() or not isinstance(package_id, str) or not _PYTHON.fullmatch(package_id):
        return None
    repair_attempted = False
    try:
        settings = backend.preferences()
        if not isinstance(settings, dict):
            raise ValueError('Saved Python relocation settings are unavailable.')
        if settings.get('python_relocation_repair_enabled', False) is not True:
            return None
        if _cancelled(cancel_event):
            raise InterruptedError('Python relocation preparation was cancelled.')
        package = _current_package(backend, package_id)
        diagnoses = _diagnoses(backend, package, cancel_event)
        if not diagnoses:
            return None
        approval = _approved_mapping(settings, package_id, diagnoses)
        # The normal exclusive installer lane is an outer caller invariant.
        # Independently require its real maintenance lease before native work;
        # an arbitrary backend callback cannot turn this into a standalone repair.
        _require_lease(backend)
        if callable(progress_callback):
            try:
                progress_callback({'phase': 'checking', 'percent': None,
                    'description': 'Checking the approved Python relocation against its exact native failure.'})
            except Exception:
                pass  # A display observer cannot authorize or interrupt repair.
        if _cancelled(cancel_event):
            raise InterruptedError('Python relocation preparation was cancelled.')
        checked = _native_result(preflight_python_relocation(approval))
        if checked['status'] not in ('ready', 'already_trusted'):
            return _result(package_id, checked['status'], checked.get('note', checked.get('code', 'Needs review.')),
                receipt=checked, offer=package, approval=approval.as_dict(), evidence=diagnoses)
        # A UI save or changed source while evidence was inspected cannot widen
        # this exact mapping. The original updater revalidates the offer again.
        latest = backend.preferences()
        if (latest.get('python_relocation_repair_enabled') is not True
                or _approved_mapping(latest, package_id, diagnoses) != approval
                or _current_package(backend, package_id) != package):
            raise ValueError('The exact Python relocation approval or offer changed during preparation.')
        if _cancelled(cancel_event):
            raise InterruptedError('Python relocation preparation was cancelled.')
        _require_lease(backend)
        if checked['status'] == 'already_trusted':
            result = checked
        else:
            repair_attempted = True
            result = _native_result(repair_python_relocation(approval))
        proceed = result['status'] in ('repaired', 'already_trusted')
        return _result(package_id, result['status'], result.get('note', result.get('code', 'Needs review.')),
            proceed=proceed, receipt=result, offer=package, approval=approval.as_dict(), evidence=diagnoses)
    except InterruptedError as error:
        return _result(package_id, 'cancelled', error, cancelled=True, repair_attempted=repair_attempted)
    except Exception as error:
        # This is prelaunch work. Even an unexpected native-repair failure must
        # not turn into an unknown installer outcome or stop unrelated packages.
        return _result(package_id, 'review_required', type(error).__name__ + ': ' + str(error),
            repair_attempted=repair_attempted)
