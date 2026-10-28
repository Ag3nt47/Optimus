"""Provider integration for owned update payloads; no shared-cache authority."""
from __future__ import annotations

import json
import inspect
import os
from pathlib import Path

from .filesystem import _ordinary_data_path


def cleanup_enabled(data_dir):
    """A missing legacy flag defaults on; invalid/unreadable settings preserve."""
    path = Path(data_dir) / 'preferences.json'
    try:
        before = _ordinary_data_path(path, file=True)
        if before is None:
            return True
        if before.st_size > 4 * 1024**2:
            return False
        with path.open('rb') as stream:
            opened = os.fstat(stream.fileno())
            raw = stream.read(4 * 1024**2 + 1)
            after = os.fstat(stream.fileno())
        final = _ordinary_data_path(path, file=True)
        identity = before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns
        if (len(raw) > 4 * 1024**2 or final is None or any(
                (row.st_dev, row.st_ino, row.st_size, row.st_mtime_ns) != identity
                for row in (opened, after, final))):
            return False
        def pairs(values):
            result = {}
            for key, value in values:
                if key in result:
                    raise ValueError('Duplicate preference key.')
                result[key] = value
            return result
        saved = json.loads(raw.decode('utf-8-sig'), object_pairs_hook=pairs)
        if not isinstance(saved, dict):
            return False
        enabled = saved.get('update_payload_cleanup_enabled', True)
        if type(enabled) is not bool:
            return False
        from .workflow import Preferences
        Preferences.validate(saved)
        return enabled
    except (OSError, ValueError, TypeError):
        return False


def retained_receipt(provider, enabled, reason, *, status=None):
    return {'schema': 1, 'provider': provider, 'ownership': 'provider_managed',
            'supported': False, 'requested_enabled': enabled,
            'status': status or ('disabled' if not enabled else 'retained_unverified'),
            'removed_files': 0, 'removed_bytes': 0, 'reason': str(reason)[:1000]}


def accepts_child_environment(operation):
    try:
        parameters = inspect.signature(operation).parameters
        return 'environment' in parameters or any(value.kind == inspect.Parameter.VAR_KEYWORD for value in parameters.values())
    except (TypeError, ValueError):
        return False


def console_quiescent(result):
    """A completed direct process alone cannot establish descendant exit."""
    proof = getattr(result, 'console_receipt', None)
    return (isinstance(proof, dict) and type(proof.get('schema')) is int and proof['schema'] == 1
        and proof.get('verified') is True and proof.get('quiescent') is True
        and proof.get('job_verified') is True and proof.get('output_eof') is True
        and all(type(proof.get(key)) is int and proof[key] == expected for key, expected in (
            ('job_active_processes', 0), ('console_client_count', 1),
            ('supervisor_exit_code', 0), ('target_exit_code', 0)))
        and getattr(result, 'capture_complete', False) is True
        and type(getattr(result, 'returncode', None)) is int and result.returncode == 0)


class PayloadAttempt:
    def __init__(self, data_dir, provider, package_id, *, environment_supported=True):
        self.data_dir = Path(data_dir)
        self.enabled = cleanup_enabled(data_dir)
        self.provider, self.workspace, self.environment = provider, None, None
        self.fallback = retained_receipt(provider, self.enabled,
            'Update payload cleanup is disabled in Settings.' if not self.enabled else
            'The private update payload workspace could not be prepared or routed to this provider; provider caches are preserved.')
        if self.enabled and environment_supported:
            try:
                from .update_payloads import PayloadWorkspace
                self.workspace = PayloadWorkspace.create(data_dir, provider, package_id, enabled=True)
                self.environment = self.workspace.env_overrides()
            except Exception:
                self.workspace = None

    def finish(self, outcome, *, verified=False, quiescent=False, restart_pending=True, download_hashes=()):
        if self.workspace is None:
            return dict(self.fallback)
        if not cleanup_enabled(self.data_dir):
            return retained_receipt(self.provider, False,
                'Update payload cleanup was disabled or its saved choice became unreadable; the owned workspace is preserved.')
        try:
            return self.workspace.finish(outcome, verified=verified,
                quiescent=quiescent, restart_pending=restart_pending, download_hashes=download_hashes)
        except Exception:
            value = retained_receipt(self.provider, self.enabled,
                'Owned payload cleanup could not be verified; remaining files and all provider caches are preserved.')
            value['ownership'] = 'optimus_private_update_workspace'
            return value
