"""Durable update continuation after a user-controlled reboot.

Plans contain approval snapshots, never downloaded commands or executable paths.
The caller owns the maintenance lease and supplies its normal fresh-discovery
runner. A started plan is never automatically replayed after a crash.
"""
from __future__ import annotations

import copy
import hashlib
import json
import math
from pathlib import Path
import time
import uuid

from .automation import CURRENT_SETUP_VERSION, RunLease
from .filesystem import _ordinary_data_path
from .speed_enhancements import _current_owner, _private_json
from .update_recovery import _decode, _read

SCOPE_KEYS = ('setup_version', 'toolkit_settings', 'software_updates_enabled', 'software_update_policy',
    'software_update_ids', 'software_update_all_approved', 'software_update_exclude_ids',
    'software_update_agreements_accepted', 'update_compatibility_mode_enabled',
    'software_app_rules', 'driver_updates_enabled', 'driver_policy', 'driver_changes_approved',
    'driver_update_agreements_accepted', 'driver_approved_updates', 'driver_approved_devices')
KIND = 'reboot-update-continuation-v1'


def _enabled(configuration):
    toolkit = configuration.get('toolkit_settings', {}) if isinstance(configuration, dict) else {}
    resume = toolkit.get('resume', {}) if isinstance(toolkit, dict) else {}
    return isinstance(resume, dict) and resume.get('enabled') is True


def boot_identity():
    """OS boot time, rather than an Optimus launch/session identifier."""
    import psutil
    value = psutil.boot_time()
    if type(value) not in (int, float) or not math.isfinite(value) or value <= 0:
        raise ValueError('The current operating-system boot identity is unavailable.')
    return round(value)


def _scope(configuration):
    if not isinstance(configuration, dict) or configuration.get('setup_version') != CURRENT_SETUP_VERSION:
        raise ValueError('Current saved setup approval is required for reboot continuation.')
    scope = {key: copy.deepcopy(configuration.get(key)) for key in SCOPE_KEYS}
    raw = json.dumps(scope, allow_nan=False, sort_keys=True, separators=(',', ':')).encode()
    if len(raw) > 1024 * 1024:
        raise ValueError('The saved update approval scope exceeds its bound.')
    return scope, hashlib.sha256(raw).hexdigest()


def _facts(value):
    """Inspect bounded structured receipts; never infer success from a message."""
    stack, visited, restart, unknown = [value], 0, False, False
    while stack:
        row = stack.pop()
        visited += 1
        if visited > 20000:
            raise ValueError('The update receipt exceeds its continuation bound.')
        if isinstance(row, dict):
            restart = restart or row.get('restartNeeded') is True or row.get('rebootRequired') is True
            unknown = unknown or row.get('outcomeKnown') is False or row.get('running') is True
            if row.get('installer_started') is True:
                # The software batch aggregates installer_started/outcomeKnown
                # but its actual per-package receipts hold the running flag.
                # Do not invent that flag on the aggregate or ignore a leaf.
                aggregate = isinstance(row.get('items'), list) and bool(row['items'])
                unknown = unknown or (not aggregate and
                    (row.get('outcomeKnown') is not True or row.get('running') is not False))
            unknown = unknown or row.get('status') in ('uncertain', 'running', 'prepared')
            stack.extend(v for k, v in row.items() if k not in ('scan', 'processes', 'Processes')
                         and isinstance(v, (dict, list)))
        elif isinstance(row, list):
            if len(row) > 10000:
                raise ValueError('The update receipt list exceeds its continuation bound.')
            stack.extend(v for v in row if isinstance(v, (dict, list)))
    return restart, unknown


class UpdateResumeService:
    """Lease-bound plans; no task registration, automatic restart or preference save.

    Configuration contract: the existing validated preference dictionary with
    ``toolkit_settings.resume.enabled`` (explicit Boolean opt-in). The runner
    accepts a fresh copy of that dictionary, performs update-only discovery under
    the already-owned lease, and returns a terminal report. Normal resume must
    not run app presence, general cleanup, or replay commands from this plan.
    A separately approved optimize request may explicitly pass
    ``maintenance_approved=True`` to consume the intent around one ordinary
    full workflow; that additional maintenance is authorized by the current
    request, never by the update-only intent.
    """
    def __init__(self, backend, *, boot=None, now=None):
        self.backend = backend
        self.data_dir = Path(backend.data_dir)
        self.folder = self.data_dir / 'update-resume'
        self.boot = boot_identity if boot is None else boot
        self.now = time.time if now is None else now

    def _lease(self, lease):
        if not (isinstance(lease, RunLease) and lease.handle is not None
                and lease.path == self.data_dir / 'maintenance-run.lock'):
            raise ValueError('A current maintenance lease is required for reboot continuation.')
        _ordinary_data_path(self.folder)

    def _plans(self):
        _ordinary_data_path(self.folder)
        paths = list(self.folder.glob('*.json')) if self.folder.exists() else []
        if len(paths) > 1000:
            raise ValueError('The reboot continuation inventory exceeds its bound.')
        return paths

    def _boot(self):
        value = self.boot()
        if type(value) is not int or value <= 0 or value > 32503680000:
            raise ValueError('The operating-system boot identity is unavailable.')
        return value

    def capture(self, configuration, report, *, lease):
        """Save a recheck intent only for known terminal restart-required work."""
        self._lease(lease)
        if not _enabled(configuration):
            return {'status': 'disabled', 'plan': None}
        scope, digest = _scope(configuration)
        restart, unknown = _facts(report)
        if unknown:
            return {'status': 'unresolved_installer', 'plan': None}
        if not restart:
            return {'status': 'no_restart_required', 'plan': None}
        boot = self._boot()
        for path in self._plans():
            previous = self._load(path)
            if previous['status'] == 'pending' and previous['boot_identity'] == boot and previous['scope_sha256'] == digest:
                return {'status': 'already_pending', 'plan': str(path)}
        identifier = uuid.uuid4().hex
        path = self.folder / (identifier + '.json')
        value = {'schema': 1, 'kind': KIND, 'id': identifier, 'owner': _current_owner(),
            'status': 'pending', 'created_at': self.now(), 'boot_identity': boot,
            'scope': scope, 'scope_sha256': digest,
            'updates_only': True, 'restart_requested': False, 'preferences_saved': False,
            'note': 'After your reboot, recheck installed versions and currently approved update offers. No captured installer command is replayed.'}
        _private_json(path, value)
        self._load(path)
        return {'status': 'pending_reboot', 'plan': str(path)}

    def _load(self, path):
        value = _decode(_read(path, 2 * 1024 * 1024))
        if not (value.get('schema') == 1 and type(value.get('schema')) is int
                and value.get('kind') == KIND and value.get('owner') == _current_owner()
                and value.get('id') == path.stem and len(path.stem) == 32
                and all(c in '0123456789abcdef' for c in path.stem)
                and value.get('status') in ('pending', 'started', 'completed', 'needs_review', 'cancelled')
                and type(value.get('boot_identity')) is int and value['boot_identity'] > 0
                and type(value.get('created_at')) in (int, float) and math.isfinite(value['created_at'])
                and 0 < value['created_at'] <= 32503680000
                and value.get('updates_only') is True and value.get('restart_requested') is False
                and value.get('preferences_saved') is False):
            raise ValueError('The saved reboot continuation ownership or lifecycle is invalid.')
        _, digest = _scope(value.get('scope'))
        if digest != value.get('scope_sha256'):
            raise ValueError('The saved reboot continuation approvals changed.')
        return value

    def resume(self, configuration, runner, *, lease, maintenance_approved=False):
        """Resume at most one plan after a new boot and live barrier revalidation.

        Missing/changed approval, an uncertain prior outcome, or a still-active
        installer pauses this intent. Completed work is never replayed merely
        because the application was reopened.
        """
        self._lease(lease)
        if type(maintenance_approved) is not bool:
            raise ValueError('Full maintenance authorization must be explicit.')
        if not _enabled(configuration):
            return {'status': 'disabled'}
        _, digest = _scope(configuration)
        boot = self._boot()
        plans = [(path, self._load(path)) for path in self._plans()]
        # A directory enumeration order cannot hide a prior started attempt
        # behind a newer pending one, even if its first native call never wrote.
        for path, value in plans:
            if value['status'] == 'started':
                return {'status': 'needs_review', 'plan': str(path), 'reason': 'A prior continuation has no terminal receipt; it was not replayed.'}
        for path, value in sorted(plans, key=lambda item: item[1]['created_at']):
            if value['status'] != 'pending':
                continue
            if value['scope_sha256'] != digest:
                return {'status': 'approval_changed', 'plan': str(path)}
            # psutil derives boot epoch from OS clocks on some platforms; a
            # rounding/clock-sampling fluctuation is not evidence of a reboot.
            if abs(boot - value['boot_identity']) <= 10:
                return {'status': 'waiting_for_reboot', 'plan': str(path)}
            if boot < value['boot_identity']:
                return {'status': 'boot_identity_unverified', 'plan': str(path)}
            barrier = self.backend.pending_updates_barrier()
            if barrier:
                return {'status': 'blocked', 'plan': str(path), 'reason': str(barrier)[:1000]}
            if not callable(runner):
                raise ValueError('A fresh-discovery update runner is required.')
            value.update(status='started', resumed_at=self.now(), resumed_boot_identity=boot,
                execution_mode='fresh_approved_maintenance' if maintenance_approved else 'updates_only')
            _private_json(path, value)  # Crash before/during runner cannot replay.
            try:
                result = runner(copy.deepcopy(configuration))
                restart, unknown = _facts(result)
                unknown = unknown or not isinstance(result, dict) or result.get('status') not in (
                    'completed', 'completed_with_errors', 'completed_with_skips', 'no_enabled_updates',
                    'blocked', 'needs_review', 'cancelled', 'failed', 'error')
                receipt = {'status': result.get('status') if isinstance(result, dict) else None,
                    'report': result.get('report') if isinstance(result, dict) else None,
                    'restart_required': restart, 'outcome_unresolved': unknown}
                final_status = ('needs_review' if unknown else
                    'cancelled' if result.get('status') == 'cancelled' else 'completed')
                value.update(status=final_status, finished_at=self.now(), result=receipt)
                _private_json(path, value)
            except BaseException:
                # Keep started: callback may have launched a vendor installer.
                raise
            return {'status': value['status'], 'plan': str(path), 'result': result}
        return {'status': 'no_pending_plan'}
